import json
import os
import shutil
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import yaml


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CANDIDATES_FILE = os.path.join(ROOT, "data", "candidates.yaml")
SETTINGS_FILE = os.path.join(ROOT, "config", "settings.yaml")
RESULT_FILE = os.path.join(ROOT, "data", "test-results.json")
MIHOMO_BIN = os.path.join(ROOT, "bin", "mihomo")

YOUTUBE_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

BASE_PROXY_PORT = 20000
BASE_CONTROLLER_PORT = 21000

DEFAULT_STAGE_TIMEOUT = 3.0
DEFAULT_STARTUP_TIMEOUT = 5.0
DEFAULT_MAX_DOWNLOAD_BYTES = 1024 * 1024
DEFAULT_CONCURRENCY = 20


def load_yaml(path):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def safe_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def build_mihomo_config(node, proxy_port, controller_port):
    test_node = dict(node)

    test_node["name"] = "__TEST_PROXY__"

    config = {
        "mixed-port": proxy_port,
        "allow-lan": False,
        "mode": "rule",
        "log-level": "error",
        "external-controller": f"127.0.0.1:{controller_port}",
        "proxies": [
            test_node
        ],
        "proxy-groups": [
            {
                "name": "TEST",
                "type": "select",
                "proxies": [
                    "__TEST_PROXY__"
                ]
            }
        ],
        "rules": [
            "MATCH,TEST"
        ]
    }

    return config


def wait_for_mihomo(controller_port, timeout):
    url = f"http://127.0.0.1:{controller_port}/version"

    start = time.monotonic()

    while time.monotonic() - start < timeout:
        try:
            response = requests.get(
                url,
                timeout=0.5
            )

            if response.status_code == 200:
                return True, None

        except requests.RequestException:
            pass

        time.sleep(0.05)

    return False, "mihomo_startup_timeout"


def start_mihomo(config_path):
    process = subprocess.Popen(
        [
            MIHOMO_BIN,
            "-f",
            config_path
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )

    return process


def stop_process(process):
    if process is None:
        return

    if process.poll() is None:
        process.terminate()

        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()

            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass


def test_youtube_page(proxy_port, stage_timeout):
    proxy = f"http://127.0.0.1:{proxy_port}"

    result = {
        "success": False,
        "status_code": None,
        "elapsed": None,
        "ttfb": None,
        "bytes": 0,
        "error": None
    }

    start = time.monotonic()

    try:
        response = requests.get(
            YOUTUBE_URL,
            proxies={
                "http": proxy,
                "https": proxy
            },
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 "
                    "(KHTML, like Gecko) "
                    "Chrome/153.0 Safari/537.36"
                )
            },
            stream=True,
            timeout=(2, 1)
        )

        result["status_code"] = response.status_code

        first_byte_time = None

        for chunk in response.iter_content(chunk_size=65536):
            now = time.monotonic()

            if first_byte_time is None:
                first_byte_time = now
                result["ttfb"] = now - start

            if chunk:
                result["bytes"] += len(chunk)

            if now - start >= stage_timeout:
                break

        result["elapsed"] = time.monotonic() - start

        if response.status_code == 200:
            result["success"] = True
        else:
            result["error"] = f"http_status_{response.status_code}"

        response.close()

    except requests.RequestException as exc:
        result["elapsed"] = time.monotonic() - start
        result["error"] = f"{type(exc).__name__}: {exc}"

    except Exception as exc:
        result["elapsed"] = time.monotonic() - start
        result["error"] = f"{type(exc).__name__}: {exc}"

    return result


def extract_media_url(proxy_port, stage_timeout):
    proxy = f"http://127.0.0.1:{proxy_port}"

    command = [
        "yt-dlp",
        "--proxy",
        proxy,
        "--no-warnings",
        "--quiet",
        "--skip-download",
        "-f",
        (
            "bestvideo[height<=1080][vcodec^=vp9]/"
            "bestvideo[height<=1080][vcodec^=avc1]/"
            "bestvideo[height<=1080]"
        ),
        "-g",
        YOUTUBE_URL
    ]

    env = os.environ.copy()
    env["HTTP_PROXY"] = proxy
    env["HTTPS_PROXY"] = proxy
    env["http_proxy"] = proxy
    env["https_proxy"] = proxy

    start = time.monotonic()

    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=stage_timeout,
            env=env
        )

        elapsed = time.monotonic() - start

        if completed.returncode != 0:
            error = completed.stderr.strip()

            if not error:
                error = f"yt-dlp exit code {completed.returncode}"

            return {
                "success": False,
                "elapsed": elapsed,
                "url": None,
                "error": error
            }

        lines = [
            line.strip()
            for line in completed.stdout.splitlines()
            if line.strip()
        ]

        if not lines:
            return {
                "success": False,
                "elapsed": elapsed,
                "url": None,
                "error": "yt_dlp_empty_output"
            }

        return {
            "success": True,
            "elapsed": elapsed,
            "url": lines[0],
            "error": None
        }

    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "elapsed": time.monotonic() - start,
            "url": None,
            "error": "yt_dlp_timeout"
        }

    except Exception as exc:
        return {
            "success": False,
            "elapsed": time.monotonic() - start,
            "url": None,
            "error": f"{type(exc).__name__}: {exc}"
        }


def download_media(media_url, proxy_port, stage_timeout, max_bytes):
    proxy = f"http://127.0.0.1:{proxy_port}"

    result = {
        "success": False,
        "status": None,
        "bytes": 0,
        "elapsed": None,
        "ttfb": None,
        "throughput_mbps": 0.0,
        "error": None
    }

    start = time.monotonic()

    try:
        response = requests.get(
            media_url,
            proxies={
                "http": proxy,
                "https": proxy
            },
            headers={
                "User-Agent": "Mozilla/5.0"
            },
            stream=True,
            timeout=(2, 1)
        )

        first_byte_time = None

        for chunk in response.iter_content(chunk_size=65536):
            now = time.monotonic()

            if chunk:
                if first_byte_time is None:
                    first_byte_time = now
                    result["ttfb"] = now - start

                remaining = max_bytes - result["bytes"]

                if remaining > 0:
                    result["bytes"] += len(chunk[:remaining])

            if result["bytes"] >= max_bytes:
                result["status"] = "completed_1mb"
                break

            if now - start >= stage_timeout:
                result["status"] = "time_limit"
                break

        result["elapsed"] = time.monotonic() - start

        if result["bytes"] > 0 and result["elapsed"] > 0:
            result["throughput_mbps"] = (
                result["bytes"] * 8
                / result["elapsed"]
                / 1000000
            )

        if result["status"] is None:
            result["status"] = "connection_closed"

        if result["bytes"] > 0:
            result["success"] = True

        response.close()

    except requests.RequestException as exc:
        result["elapsed"] = time.monotonic() - start

        if result["bytes"] > 0:
            result["success"] = True
            result["status"] = "transport_error_after_data"

        result["error"] = f"{type(exc).__name__}: {exc}"

        if result["bytes"] > 0 and result["elapsed"] > 0:
            result["throughput_mbps"] = (
                result["bytes"] * 8
                / result["elapsed"]
                / 1000000
            )

    except Exception as exc:
        result["elapsed"] = time.monotonic() - start
        result["error"] = f"{type(exc).__name__}: {exc}"

        if result["bytes"] > 0:
            result["success"] = True
            result["status"] = "transport_error_after_data"

            if result["elapsed"] > 0:
                result["throughput_mbps"] = (
                    result["bytes"] * 8
                    / result["elapsed"]
                    / 1000000
                )

    return result


def test_node(node, index, stage_timeout, startup_timeout, max_bytes):
    name = str(node.get("name", f"node-{index}"))
    protocol = str(node.get("type", "unknown"))

    proxy_port = BASE_PROXY_PORT + index
    controller_port = BASE_CONTROLLER_PORT + index

    result = {
        "index": index,
        "name": name,
        "type": protocol,
        "proxy_port": proxy_port,
        "controller_port": controller_port,
        "startup": {},
        "youtube": {},
        "yt_dlp": {},
        "media": {},
        "success": False,
        "failure_reason": None
    }

    temp_dir = tempfile.mkdtemp(prefix="free_nodes_")
    config_path = os.path.join(temp_dir, "config.yaml")

    process = None

    try:
        config = build_mihomo_config(
            node,
            proxy_port,
            controller_port
        )

        with open(config_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(
                config,
                f,
                allow_unicode=True,
                sort_keys=False
            )

        startup_start = time.monotonic()

        process = start_mihomo(config_path)

        ready, startup_error = wait_for_mihomo(
            controller_port,
            startup_timeout
        )

        startup_elapsed = time.monotonic() - startup_start

        result["startup"] = {
            "success": ready,
            "elapsed": startup_elapsed,
            "error": startup_error
        }

        if not ready:
            result["failure_reason"] = startup_error
            return result

        youtube = test_youtube_page(
            proxy_port,
            stage_timeout
        )

        result["youtube"] = youtube

        if not youtube["success"]:
            result["failure_reason"] = "youtube_page_failed"
            return result

        extraction = extract_media_url(
            proxy_port,
            stage_timeout
        )

        result["yt_dlp"] = {
            "success": extraction["success"],
            "elapsed": extraction["elapsed"],
            "error": extraction["error"]
        }

        if not extraction["success"]:
            result["failure_reason"] = extraction["error"]
            return result

        media = download_media(
            extraction["url"],
            proxy_port,
            stage_timeout,
            max_bytes
        )

        result["media"] = media

        if not media["success"]:
            result["failure_reason"] = media["error"] or "media_failed"
            return result

        result["success"] = True

        if media["status"] == "completed_1mb":
            result["failure_reason"] = None
        elif media["status"] == "time_limit":
            result["failure_reason"] = "media_3s_limit"

        return result

    except Exception as exc:
        result["failure_reason"] = (
            f"{type(exc).__name__}: {exc}"
        )
        return result

    finally:
        stop_process(process)
        shutil.rmtree(temp_dir, ignore_errors=True)


def main():
    settings = load_yaml(SETTINGS_FILE) or {}

    test_settings = settings.get("test", {})

    stage_timeout = safe_float(
        test_settings.get(
            "timeout_seconds",
            DEFAULT_STAGE_TIMEOUT
        )
    )

    if stage_timeout is None:
        stage_timeout = DEFAULT_STAGE_TIMEOUT

    max_bytes = int(
        test_settings.get(
            "max_download_bytes",
            DEFAULT_MAX_DOWNLOAD_BYTES
        )
    )

    concurrency = int(
        settings.get(
            "concurrency",
            DEFAULT_CONCURRENCY
        )
    )

    candidates_data = load_yaml(CANDIDATES_FILE)

    if isinstance(candidates_data, dict):
        candidates = candidates_data.get("proxies", [])
    elif isinstance(candidates_data, list):
        candidates = candidates_data
    else:
        raise RuntimeError(
            "data/candidates.yaml must contain a list "
            "or a dictionary with a 'proxies' list"
        )

    candidates = [
        node
        for node in candidates
        if isinstance(node, dict)
    ]

    total = len(candidates)

    print()
    print("free-nodes - batch YouTube test")
    print()
    print(f"Candidates: {total}")
    print(f"Concurrency: {concurrency}")
    print(f"Stage timeout: {stage_timeout:.1f}s")
    print(f"Maximum media download: {max_bytes:,} bytes")
    print()

    start = time.monotonic()

    results = []

    with ThreadPoolExecutor(
        max_workers=concurrency
    ) as executor:

        future_map = {}

        for index, node in enumerate(candidates):
            future = executor.submit(
                test_node,
                node,
                index,
                stage_timeout,
                DEFAULT_STARTUP_TIMEOUT,
                max_bytes
            )

            future_map[future] = index

        completed = 0

        for future in as_completed(future_map):
            completed += 1

            index = future_map[future]

            try:
                result = future.result()

            except Exception as exc:
                result = {
                    "index": index,
                    "success": False,
                    "failure_reason": (
                        f"worker_exception: "
                        f"{type(exc).__name__}: {exc}"
                    )
                }

            results.append(result)

            name = result.get("name", f"node-{index}")
            success = result.get("success", False)
            reason = result.get("failure_reason")

            if success:
                media = result.get("media", {})

                print(
                    f"[{completed}/{total}] PASS "
                    f"{name} | "
                    f"media={media.get('bytes', 0):,}B | "
                    f"speed={media.get('throughput_mbps', 0):.2f}Mbps"
                )

            else:
                print(
                    f"[{completed}/{total}] FAIL "
                    f"{name} | "
                    f"{reason}"
                )

    results.sort(
        key=lambda item: item.get("index", 0)
    )

    elapsed = time.monotonic() - start

    os.makedirs(
        os.path.dirname(RESULT_FILE),
        exist_ok=True
    )

    output = {
        "generated_at": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime()
        ),
        "youtube_url": YOUTUBE_URL,
        "candidates": total,
        "tested": len(results),
        "concurrency": concurrency,
        "stage_timeout_seconds": stage_timeout,
        "max_download_bytes": max_bytes,
        "elapsed_seconds": elapsed,
        "results": results
    }

    with open(
        RESULT_FILE,
        "w",
        encoding="utf-8"
    ) as f:
        json.dump(
            output,
            f,
            ensure_ascii=False,
            indent=2
        )

    passed = sum(
        1
        for result in results
        if result.get("success")
    )

    failed = len(results) - passed

    print()
    print("BATCH TEST COMPLETE")
    print(f"Candidates: {total}")
    print(f"Tested: {len(results)}")
    print(f"Passed: {passed}")
    print(f"Failed: {failed}")
    print(f"Elapsed: {elapsed:.1f}s")
    print(f"Results: {RESULT_FILE}")


if __name__ == "__main__":
    main()

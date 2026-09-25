import json
import os
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

HTTP_TIMEOUT = 3.0
STARTUP_TIMEOUT = 5.0
MAX_DOWNLOAD_BYTES = 1024 * 1024


def load_yaml(path):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def now():
    return time.time()


def safe_float(value):
    if value is None:
        return None
    return round(float(value), 4)


def build_mihomo_config(proxy, proxy_port, controller_port):
    """
    Create an isolated Mihomo config for exactly one candidate node.

    The original node name is replaced with a unique internal name so that
    duplicate display names cannot cause proxy-group ambiguity.
    """

    test_proxy_name = "__TEST_PROXY__"

    proxy_config = dict(proxy)
    proxy_config["name"] = test_proxy_name

    config = {
        "mixed-port": proxy_port,
        "allow-lan": False,

        "external-controller": f"127.0.0.1:{controller_port}",

        "log-level": "error",

        "proxies": [
            proxy_config
        ],

        "proxy-groups": [
            {
                "name": "__TEST_GROUP__",
                "type": "select",
                "proxies": [
                    test_proxy_name
                ]
            }
        ],

        "rules": [
            "MATCH,__TEST_GROUP__"
        ]
    }

    return config


def wait_for_mihomo(controller_port, process):
    """
    Determine whether Mihomo itself is ready.
    This does NOT test whether the node can reach YouTube.
    """

    url = f"http://127.0.0.1:{controller_port}/version"

    deadline = now() + STARTUP_TIMEOUT

    while now() < deadline:

        if process.poll() is not None:
            return False, "mihomo_process_exit"

        try:
            r = requests.get(url, timeout=0.5)

            if r.status_code == 200:
                return True, None

        except requests.RequestException:
            pass

        time.sleep(0.05)

    return False, "mihomo_startup_timeout"


def test_youtube_page(proxy_port):
    """
    Test YouTube control-plane/page connectivity.

    Returns:
        success
        ttfb
        elapsed
        bytes
        error
    """

    proxy = f"http://127.0.0.1:{proxy_port}"

    proxies = {
        "http": proxy,
        "https": proxy,
    }

    start = now()
    ttfb = None
    total_bytes = 0

    try:
        with requests.get(
            YOUTUBE_URL,
            proxies=proxies,
            timeout=(2, 1),
            stream=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 "
                    "Chrome/153.0.0.0 Safari/537.36"
                )
            },
        ) as response:

            ttfb = now() - start

            if response.status_code != 200:
                return {
                    "success": False,
                    "status_code": response.status_code,
                    "ttfb": safe_float(ttfb),
                    "elapsed": safe_float(now() - start),
                    "bytes": 0,
                    "error": f"http_status_{response.status_code}",
                }

            for chunk in response.iter_content(chunk_size=65536):

                if now() - start > HTTP_TIMEOUT:
                    break

                if not chunk:
                    continue

                total_bytes += len(chunk)

                # Page response is only a control-plane test.
                # Do not spend the whole timeout downloading HTML.
                if total_bytes >= 1024 * 1024:
                    break

            elapsed = now() - start

            return {
                "success": True,
                "status_code": response.status_code,
                "ttfb": safe_float(ttfb),
                "elapsed": safe_float(elapsed),
                "bytes": total_bytes,
                "error": None,
            }

    except Exception as e:
        return {
            "success": False,
            "status_code": None,
            "ttfb": safe_float(ttfb),
            "elapsed": safe_float(now() - start),
            "bytes": total_bytes,
            "error": f"{type(e).__name__}: {str(e)[:500]}",
        }


def extract_media_url(proxy_port):
    """
    Use yt-dlp through the candidate proxy to obtain a real YouTube
    media URL.

    Important:
    --proxy is explicitly supplied.
    """

    proxy = f"http://127.0.0.1:{proxy_port}"

    format_selector = (
        "bestvideo[height<=1080][vcodec^=vp9]/"
        "bestvideo[height<=1080][vcodec^=avc1]/"
        "bestvideo[height<=1080]"
    )

    command = [
        "yt-dlp",

        "--no-warnings",
        "--no-playlist",

        "--socket-timeout",
        "2",

        "--proxy",
        proxy,

        "-f",
        format_selector,

        "-g",
        YOUTUBE_URL,
    ]

    env = os.environ.copy()
    env["HTTP_PROXY"] = proxy
    env["HTTPS_PROXY"] = proxy

    start = now()

    try:
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=HTTP_TIMEOUT,
            env=env,
        )

        elapsed = now() - start

        if completed.returncode != 0:
            return {
                "success": False,
                "elapsed": safe_float(elapsed),
                "media_url": None,
                "error": completed.stderr[-1000:] or "yt_dlp_failed",
            }

        media_url = completed.stdout.strip().splitlines()

        if not media_url:
            return {
                "success": False,
                "elapsed": safe_float(elapsed),
                "media_url": None,
                "error": "empty_media_url",
            }

        return {
            "success": True,
            "elapsed": safe_float(elapsed),
            "media_url": media_url[0],
            "error": None,
        }

    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "elapsed": safe_float(now() - start),
            "media_url": None,
            "error": "yt_dlp_timeout",
        }

    except Exception as e:
        return {
            "success": False,
            "elapsed": safe_float(now() - start),
            "media_url": None,
            "error": f"{type(e).__name__}: {str(e)[:500]}",
        }


def download_media(media_url, proxy_port):
    """
    Download actual YouTube media through the same candidate proxy.

    Maximum:
        3 seconds
        1 MiB

    If the node downloads less than 1 MiB within 3 seconds,
    it is still a valid measurement rather than an automatic failure.
    """

    proxy = f"http://127.0.0.1:{proxy_port}"

    proxies = {
        "http": proxy,
        "https": proxy,
    }

    start = now()
    ttfb = None
    total_bytes = 0

    try:
        with requests.get(
            media_url,
            proxies=proxies,
            timeout=(2, 1),
            stream=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 "
                    "Chrome/153.0.0.0 Safari/537.36"
                )
            },
        ) as response:

            ttfb = now() - start

            if response.status_code not in (200, 206):
                return {
                    "success": False,
                    "status_code": response.status_code,
                    "status": "http_error",
                    "bytes": 0,
                    "elapsed": safe_float(now() - start),
                    "ttfb": safe_float(ttfb),
                    "throughput_mbps": 0,
                    "error": f"http_status_{response.status_code}",
                }

            for chunk in response.iter_content(chunk_size=65536):

                elapsed = now() - start

                if elapsed >= HTTP_TIMEOUT:
                    break

                if not chunk:
                    continue

                remaining = MAX_DOWNLOAD_BYTES - total_bytes

                if remaining <= 0:
                    break

                if len(chunk) > remaining:
                    chunk = chunk[:remaining]

                total_bytes += len(chunk)

                if total_bytes >= MAX_DOWNLOAD_BYTES:
                    break

            elapsed = now() - start

            if total_bytes >= MAX_DOWNLOAD_BYTES:
                status = "completed_1mb"
            else:
                status = "time_limit"

            throughput = (
                total_bytes * 8 / elapsed / 1_000_000
                if elapsed > 0
                else 0
            )

            return {
                "success": True,
                "status_code": response.status_code,
                "status": status,
                "bytes": total_bytes,
                "elapsed": safe_float(elapsed),
                "ttfb": safe_float(ttfb),
                "throughput_mbps": safe_float(throughput),
                "error": None,
            }

    except Exception as e:

        elapsed = now() - start

        throughput = (
            total_bytes * 8 / elapsed / 1_000_000
            if elapsed > 0
            else 0
        )

        return {
            "success": False,
            "status_code": None,
            "status": "request_error",
            "bytes": total_bytes,
            "elapsed": safe_float(elapsed),
            "ttfb": safe_float(ttfb),
            "throughput_mbps": safe_float(throughput),
            "error": f"{type(e).__name__}: {str(e)[:1000]}",
        }


def test_node(index, proxy):
    """
    Complete test of one candidate.
    """

    original_name = proxy.get("name", f"node-{index}")
    protocol = proxy.get("type", "unknown")

    proxy_port = BASE_PROXY_PORT + index
    controller_port = BASE_CONTROLLER_PORT + index

    result = {
        "index": index,
        "name": original_name,
        "protocol": protocol,

        "proxy_port": proxy_port,
        "controller_port": controller_port,

        "success": False,

        "mihomo": {},
        "youtube": {},
        "yt_dlp": {},
        "media": {},

        "failure_stage": None,
        "failure_reason": None,

        "started_at": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime()
        ),
    }

    temp_dir = None
    process = None

    overall_start = now()

    try:

        temp_dir = tempfile.mkdtemp(prefix=f"free-node-{index}-")

        config_path = os.path.join(temp_dir, "config.yaml")

        config = build_mihomo_config(
            proxy,
            proxy_port,
            controller_port,
        )

        with open(config_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(
                config,
                f,
                allow_unicode=True,
                sort_keys=False,
            )

        # ---------------------------------------------------------
        # STEP 0 — Mihomo startup
        # ---------------------------------------------------------

        start = now()

        process = subprocess.Popen(
            [
                MIHOMO_BIN,
                "-d",
                temp_dir,
                "-f",
                config_path,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        ready, error = wait_for_mihomo(
            controller_port,
            process,
        )

        startup_elapsed = now() - start

        result["mihomo"] = {
            "success": ready,
            "startup_time": safe_float(startup_elapsed),
            "error": error,
        }

        if not ready:
            result["failure_stage"] = "mihomo"
            result["failure_reason"] = error
            return result

        # ---------------------------------------------------------
        # STEP 1 — YouTube page
        # ---------------------------------------------------------

        youtube = test_youtube_page(proxy_port)

        result["youtube"] = youtube

        if not youtube["success"]:
            result["failure_stage"] = "youtube_page"
            result["failure_reason"] = youtube["error"]
            return result

        # ---------------------------------------------------------
        # STEP 2 — yt-dlp
        # ---------------------------------------------------------

        yt_dlp = extract_media_url(proxy_port)

        # Never save the actual signed media URL into the result file.
        # It is temporary and may contain query parameters.
        yt_dlp_result = dict(yt_dlp)
        yt_dlp_result.pop("media_url", None)

        result["yt_dlp"] = yt_dlp_result

        if not yt_dlp["success"]:
            result["failure_stage"] = "yt_dlp"
            result["failure_reason"] = yt_dlp["error"]
            return result

        # ---------------------------------------------------------
        # STEP 3 — actual YouTube media
        # ---------------------------------------------------------

        media = download_media(
            yt_dlp["media_url"],
            proxy_port,
        )

        result["media"] = media

        if not media["success"]:
            result["failure_stage"] = "media"
            result["failure_reason"] = media["error"]
            return result

        # A partial download within 3 seconds is still a usable
        # measurement. Do not require 1 MiB.
        result["success"] = True

        if media["status"] == "completed_1mb":
            result["failure_stage"] = None
            result["failure_reason"] = None
        else:
            result["failure_stage"] = None
            result["failure_reason"] = "media_3s_limit"

        return result

    except Exception as e:

        result["failure_stage"] = "internal"
        result["failure_reason"] = (
            f"{type(e).__name__}: {str(e)[:1000]}"
        )

        return result

    finally:

        result["elapsed_total"] = safe_float(
            now() - overall_start
        )

        result["finished_at"] = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime()
        )

        if process is not None:

            try:
                process.terminate()
                process.wait(timeout=1)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass

        if temp_dir is not None:

            try:
                import shutil
                shutil.rmtree(temp_dir, ignore_errors=True)
            except Exception:
                pass


def main():

    settings = load_yaml(SETTINGS_FILE)
    candidates = load_yaml(CANDIDATES_FILE)

    if not isinstance(candidates, list):
        raise RuntimeError(
            "data/candidates.yaml must contain a list of proxies"
        )

    test_settings = settings.get("test", {})

    global HTTP_TIMEOUT
    global MAX_DOWNLOAD_BYTES

    HTTP_TIMEOUT = float(
        test_settings.get(
            "timeout_seconds",
            3
        )
    )

    MAX_DOWNLOAD_BYTES = int(
        test_settings.get(
            "max_download_bytes",
            1024 * 1024
        )
    )

    concurrency = int(
        settings.get(
            "concurrency",
            20
        )
    )

    print()
    print("free-nodes - formal batch test")
    print()
    print(f"Candidates: {len(candidates)}")
    print(f"Concurrency: {concurrency}")
    print(f"Stage timeout: {HTTP_TIMEOUT:.1f}s")
    print(
        f"Maximum media download: "
        f"{MAX_DOWNLOAD_BYTES:,} bytes"
    )
    print()

    os.makedirs(
        os.path.dirname(RESULT_FILE),
        exist_ok=True
    )

    results = []

    start_time = now()

    with ThreadPoolExecutor(
        max_workers=concurrency
    ) as executor:

        futures = {}

        for index, proxy in enumerate(candidates):

            future = executor.submit(
                test_node,
                index,
                proxy,
            )

            futures[future] = index

        completed = 0

        for future in as_completed(futures):

            completed += 1

            index = futures[future]

            try:
                result = future.result()
            except Exception as e:
                result = {
                    "index": index,
                    "success": False,
                    "failure_stage": "worker",
                    "failure_reason": (
                        f"{type(e).__name__}: {str(e)}"
                    ),
                }

            results.append(result)

            name = result.get(
                "name",
                f"node-{index}"
            )

            protocol = result.get(
                "protocol",
                "unknown"
            )

            success = result.get(
                "success",
                False
            )

            if success:

                media = result.get(
                    "media",
                    {}
                )

                speed = media.get(
                    "throughput_mbps",
                    0
                )

                media_status = media.get(
                    "status",
                    ""
                )

                print(
                    f"[{completed}/{len(candidates)}] "
                    f"PASS | "
                    f"{protocol} | "
                    f"{speed} Mbps | "
                    f"{media_status} | "
                    f"{name}"
                )

            else:

                stage = result.get(
                    "failure_stage",
                    "unknown"
                )

                reason = result.get(
                    "failure_reason",
                    "unknown"
                )

                reason = str(reason).replace(
                    "\n",
                    " "
                )[:120]

                print(
                    f"[{completed}/{len(candidates)}] "
                    f"FAIL | "
                    f"{protocol} | "
                    f"{stage} | "
                    f"{reason} | "
                    f"{name}"
                )

    # Restore original candidate order.
    results.sort(
        key=lambda x: x.get("index", 0)
    )

    passed = sum(
        1
        for r in results
        if r.get("success") is True
    )

    failed = len(results) - passed

    elapsed = now() - start_time

    output = {
        "version": 1,

        "test": {
            "youtube_url": YOUTUBE_URL,
            "stage_timeout_seconds": HTTP_TIMEOUT,
            "max_download_bytes": MAX_DOWNLOAD_BYTES,
            "concurrency": concurrency,
        },

        "summary": {
            "candidates": len(candidates),
            "tested": len(results),
            "passed": passed,
            "failed": failed,
            "elapsed_seconds": safe_float(elapsed),
        },

        "results": results,
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
            indent=2,
        )

    print()
    print("BATCH TEST COMPLETE")
    print(f"Candidates: {len(candidates)}")
    print(f"Tested:    {len(results)}")
    print(f"Passed:    {passed}")
    print(f"Failed:    {failed}")
    print(f"Elapsed:   {elapsed:.1f}s")
    print(f"Results:   data/test-results.json")


if __name__ == "__main__":
    main()

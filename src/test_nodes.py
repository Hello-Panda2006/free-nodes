import json
import os
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
import yaml


ROOT = Path(__file__).resolve().parent.parent
CANDIDATES_FILE = ROOT / "data" / "candidates.yaml"
RESULT_FILE = ROOT / "data" / "test-results.json"
MIHOMO = ROOT / "bin" / "mihomo"

MAX_NODES = 20
CONCURRENCY = 1

STAGE_TIMEOUT = 3
MIHOMO_STARTUP_TIMEOUT = 5
MAX_DOWNLOAD_BYTES = 1024 * 1024

YOUTUBE_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def load_candidates():
    with open(CANDIDATES_FILE, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    if isinstance(data, dict):
        nodes = data.get("proxies", [])
    elif isinstance(data, list):
        nodes = data
    else:
        raise ValueError("Invalid candidates.yaml format")

    if not isinstance(nodes, list):
        raise ValueError("candidates.yaml proxies must be a list")

    return nodes[:MAX_NODES]


def make_config(node, proxy_port, controller_port):
    config = {
        "mixed-port": proxy_port,
        "external-controller": f"127.0.0.1:{controller_port}",
        "mode": "rule",
        "log-level": "error",
        "allow-lan": False,
        "ipv6": True,
        "proxies": [node],
        "proxy-groups": [
            {
                "name": "TEST",
                "type": "select",
                "proxies": [node["name"]],
            }
        ],
        "rules": [
            "MATCH,TEST"
        ],
    }

    return config


def wait_controller(port):
    url = f"http://127.0.0.1:{port}/version"
    deadline = time.monotonic() + MIHOMO_STARTUP_TIMEOUT

    while time.monotonic() < deadline:
        try:
            r = requests.get(url, timeout=0.3)
            if r.status_code == 200:
                return True
        except Exception:
            pass

        time.sleep(0.05)

    return False


def wait_proxy(port):
    deadline = time.monotonic() + 2

    while time.monotonic() < deadline:
        try:
            sock = requests.Session()
            sock.proxies.update({
                "http": f"http://127.0.0.1:{port}",
                "https": f"http://127.0.0.1:{port}",
            })

            r = sock.get(
                "http://www.gstatic.com/generate_204",
                timeout=0.5,
            )

            if r.status_code in (200, 204, 301, 302):
                return True

        except Exception:
            pass

        time.sleep(0.05)

    return False


def start_mihomo(node, proxy_port, controller_port):
    temp_dir = tempfile.mkdtemp(prefix="free_nodes_")
    config_file = Path(temp_dir) / "config.yaml"
    log_file = Path(temp_dir) / "mihomo.log"

    config = make_config(node, proxy_port, controller_port)

    with open(config_file, "w", encoding="utf-8") as f:
        yaml.safe_dump(config, f, allow_unicode=True, sort_keys=False)

    log = open(log_file, "w", encoding="utf-8")

    start = time.monotonic()

    process = subprocess.Popen(
        [
            str(MIHOMO),
            "-d",
            temp_dir,
            "-f",
            str(config_file),
        ],
        stdout=log,
        stderr=subprocess.STDOUT,
        text=True,
    )

    if not wait_controller(controller_port):
        process.terminate()
        try:
            process.wait(timeout=1)
        except Exception:
            process.kill()

        log.close()

        return None, {
            "success": False,
            "reason": "mihomo_controller_timeout",
            "elapsed": time.monotonic() - start,
        }

    if not wait_proxy(proxy_port):
        process.terminate()
        try:
            process.wait(timeout=1)
        except Exception:
            process.kill()

        log.close()

        return None, {
            "success": False,
            "reason": "mihomo_proxy_not_ready",
            "elapsed": time.monotonic() - start,
        }

    return {
        "process": process,
        "log": log,
        "temp_dir": temp_dir,
    }, {
        "success": True,
        "elapsed": time.monotonic() - start,
    }


def stop_mihomo(instance):
    if instance is None:
        return

    process = instance["process"]
    log = instance["log"]

    try:
        process.terminate()
        process.wait(timeout=1)
    except Exception:
        try:
            process.kill()
        except Exception:
            pass

    try:
        log.close()
    except Exception:
        pass


def test_youtube(proxy_port):
    proxies = {
        "http": f"http://127.0.0.1:{proxy_port}",
        "https": f"http://127.0.0.1:{proxy_port}",
    }

    start = time.monotonic()
    ttfb = None
    total_bytes = 0

    try:
        response = requests.get(
            YOUTUBE_URL,
            proxies=proxies,
            timeout=(STAGE_TIMEOUT, STAGE_TIMEOUT),
            stream=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/131 Safari/537.36"
                )
            },
        )

        ttfb = time.monotonic() - start

        for chunk in response.iter_content(chunk_size=65536):
            if chunk:
                total_bytes += len(chunk)

            if time.monotonic() - start >= STAGE_TIMEOUT:
                break

        elapsed = time.monotonic() - start

        return {
            "success": response.status_code == 200,
            "status": response.status_code,
            "elapsed": elapsed,
            "ttfb": ttfb,
            "bytes": total_bytes,
            "error": None,
        }

    except Exception as e:
        return {
            "success": False,
            "status": None,
            "elapsed": time.monotonic() - start,
            "ttfb": ttfb,
            "bytes": total_bytes,
            "error": type(e).__name__ + ": " + str(e),
        }


def extract_media_url(proxy_port):
    start = time.monotonic()

    env = os.environ.copy()
    env["HTTP_PROXY"] = f"http://127.0.0.1:{proxy_port}"
    env["HTTPS_PROXY"] = f"http://127.0.0.1:{proxy_port}"
    env["ALL_PROXY"] = f"http://127.0.0.1:{proxy_port}"

    command = [
        "yt-dlp",
        "--proxy",
        f"http://127.0.0.1:{proxy_port}",
        "--no-warnings",
        "--no-playlist",
        "-g",
        "-f",
        "bestvideo[height<=1080]/best[height<=1080]",
        YOUTUBE_URL,
    ]

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=STAGE_TIMEOUT,
            env=env,
        )

        elapsed = time.monotonic() - start

        if result.returncode != 0:
            return {
                "success": False,
                "elapsed": elapsed,
                "url": None,
                "error": result.stderr[-1000:],
            }

        url = result.stdout.strip().splitlines()[0]

        return {
            "success": True,
            "elapsed": elapsed,
            "url": url,
            "error": None,
        }

    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "elapsed": time.monotonic() - start,
            "url": None,
            "error": "yt_dlp_timeout",
        }

    except Exception as e:
        return {
            "success": False,
            "elapsed": time.monotonic() - start,
            "url": None,
            "error": type(e).__name__ + ": " + str(e),
        }


def download_media(media_url, proxy_port):
    proxies = {
        "http": f"http://127.0.0.1:{proxy_port}",
        "https": f"http://127.0.0.1:{proxy_port}",
    }

    start = time.monotonic()
    first_byte = None
    total = 0

    try:
        response = requests.get(
            media_url,
            proxies=proxies,
            timeout=(STAGE_TIMEOUT, STAGE_TIMEOUT),
            stream=True,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Range": f"bytes=0-{MAX_DOWNLOAD_BYTES - 1}",
            },
        )

        for chunk in response.iter_content(chunk_size=65536):
            if not chunk:
                continue

            if first_byte is None:
                first_byte = time.monotonic()

            remaining = MAX_DOWNLOAD_BYTES - total

            if len(chunk) > remaining:
                chunk = chunk[:remaining]

            total += len(chunk)

            if total >= MAX_DOWNLOAD_BYTES:
                break

            if time.monotonic() - start >= STAGE_TIMEOUT:
                break

        elapsed = time.monotonic() - start

        if total > 0:
            throughput = total * 8 / elapsed / 1000000
            valid = True
        else:
            throughput = 0
            valid = False

        return {
            "success": response.status_code in (200, 206),
            "status": response.status_code,
            "elapsed": elapsed,
            "ttfb": (
                first_byte - start
                if first_byte is not None
                else None
            ),
            "bytes": total,
            "throughput_mbps": throughput,
            "measurement_valid": valid,
            "error": None,
        }

    except Exception as e:
        return {
            "success": False,
            "status": None,
            "elapsed": time.monotonic() - start,
            "ttfb": None,
            "bytes": total,
            "throughput_mbps": 0,
            "measurement_valid": False,
            "error": type(e).__name__ + ": " + str(e),
        }


def test_node(index, node):
    proxy_port = 20000 + index
    controller_port = 21000 + index

    name = node.get("name", f"node-{index}")
    node_type = node.get("type", "unknown")

    print(f"[{index + 1}/{MAX_NODES}] TEST {name}", flush=True)

    result = {
        "index": index,
        "name": name,
        "type": node_type,
        "startup": {},
        "youtube": {},
        "yt_dlp": {},
        "media": {},
        "measurement_valid": False,
        "failure_reason": None,
    }

    instance, startup = start_mihomo(
        node,
        proxy_port,
        controller_port,
    )

    result["startup"] = startup

    if instance is None:
        result["failure_reason"] = startup["reason"]
        print(
            f"    FAIL | {startup['reason']}",
            flush=True,
        )
        return result

    try:
        youtube = test_youtube(proxy_port)
        result["youtube"] = youtube

        if not youtube["success"]:
            result["failure_reason"] = "youtube_page_failed"
            print(
                f"    FAIL | YouTube | {youtube['error']}",
                flush=True,
            )
            return result

        print(
            f"    YouTube OK | "
            f"{youtube['elapsed']:.2f}s | "
            f"TTFB {youtube['ttfb']:.2f}s",
            flush=True,
        )

        extraction = extract_media_url(proxy_port)

        result["yt_dlp"] = {
            k: v for k, v in extraction.items()
            if k != "url"
        }

        if not extraction["success"]:
            result["failure_reason"] = "yt_dlp_failed"
            print(
                f"    FAIL | yt-dlp | {extraction['error']}",
                flush=True,
            )
            return result

        print(
            f"    yt-dlp OK | "
            f"{extraction['elapsed']:.2f}s",
            flush=True,
        )

        media = download_media(
            extraction["url"],
            proxy_port,
        )

        result["media"] = media
        result["measurement_valid"] = media["measurement_valid"]

        if media["measurement_valid"]:
            result["failure_reason"] = None

            print(
                f"    MEDIA OK | "
                f"{media['bytes']} bytes | "
                f"{media['throughput_mbps']:.2f} Mbps",
                flush=True,
            )
        else:
            result["failure_reason"] = "media_failed"

            print(
                f"    FAIL | Media | {media['error']}",
                flush=True,
            )

        return result

    finally:
        stop_mihomo(instance)


def main():
    start = time.monotonic()

    if not MIHOMO.exists():
        raise FileNotFoundError(
            f"Mihomo binary not found: {MIHOMO}"
        )

    nodes = load_candidates()

    print()
    print("======================================")
    print("free-nodes diagnostic test")
    print("======================================")
    print(f"Candidates: {len(nodes)}")
    print(f"Concurrency: {CONCURRENCY}")
    print(f"Stage timeout: {STAGE_TIMEOUT}s")
    print(f"Max media download: {MAX_DOWNLOAD_BYTES} bytes")
    print("======================================")
    print()

    results = []

    with ThreadPoolExecutor(
        max_workers=CONCURRENCY
    ) as executor:

        futures = [
            executor.submit(test_node, i, node)
            for i, node in enumerate(nodes)
        ]

        for future in futures:
            results.append(future.result())

    summary = {
        "mihomo_startup_success": sum(
            1
            for r in results
            if r.get("startup", {}).get("success")
        ),
        "youtube_success": sum(
            1
            for r in results
            if r.get("youtube", {}).get("success")
        ),
        "yt_dlp_success": sum(
            1
            for r in results
            if r.get("yt_dlp", {}).get("success")
        ),
        "media_measurement_valid": sum(
            1
            for r in results
            if r.get("measurement_valid")
        ),
    }

    output = {
        "generated_at": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime(),
        ),
        "candidates": len(nodes),
        "tested": len(results),
        "elapsed_seconds": time.monotonic() - start,
        "summary": summary,
        "results": results,
    }

    RESULT_FILE.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with open(
        RESULT_FILE,
        "w",
        encoding="utf-8",
    ) as f:
        json.dump(
            output,
            f,
            ensure_ascii=False,
            indent=2,
        )

    print()
    print("========== SUMMARY ==========")
    print(f"Candidates: {len(nodes)}")
    print(f"Tested: {len(results)}")
    print(
        "Mihomo startup success:",
        summary["mihomo_startup_success"],
    )
    print(
        "YouTube page success:",
        summary["youtube_success"],
    )
    print(
        "yt-dlp success:",
        summary["yt_dlp_success"],
    )
    print(
        "Media measurement valid:",
        summary["media_measurement_valid"],
    )
    print(
        f"Elapsed: {output['elapsed_seconds']:.1f}s"
    )
    print(f"Results: {RESULT_FILE}")
    print("==============================")
    print()


if __name__ == "__main__":
    main()

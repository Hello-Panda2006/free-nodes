import json
import os
import socket
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
import yaml


# ============================================================
# Basic configuration
# ============================================================

ROOT = Path(__file__).resolve().parent.parent

CANDIDATES_FILE = ROOT / "data" / "candidates.yaml"
RESULT_FILE = ROOT / "data" / "test-results.json"
MIHOMO_BIN = ROOT / "bin" / "mihomo"

MAX_NODES = 20
CONCURRENCY = 5

STAGE_TIMEOUT = 3.0
MIHOMO_STARTUP_TIMEOUT = 5.0
MAX_DOWNLOAD_BYTES = 1024 * 1024

YOUTUBE_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

BASE_PROXY_PORT = 20000
BASE_CONTROLLER_PORT = 21000


# ============================================================
# Load candidates
# ============================================================

def load_candidates():
    with open(CANDIDATES_FILE, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    if isinstance(data, dict):
        candidates = data.get("proxies", [])
    elif isinstance(data, list):
        candidates = data
    else:
        raise RuntimeError("Unsupported candidates.yaml format")

    if not isinstance(candidates, list):
        raise RuntimeError("candidates.yaml must contain a list of proxies")

    candidates = candidates[:MAX_NODES]

    return candidates


# ============================================================
# Build temporary Mihomo configuration
# ============================================================

def make_config(node, proxy_port, controller_port):
    node_name = node.get("name")

    if not node_name:
        raise RuntimeError("Node has no name")

    config = {
        "mixed-port": proxy_port,
        "external-controller": "127.0.0.1:" + str(controller_port),

        "mode": "rule",
        "log-level": "error",

        "allow-lan": False,
        "ipv6": True,

        "proxies": [node],

        "proxy-groups": [
            {
                "name": "TEST",
                "type": "select",
                "proxies": [node_name],
            }
        ],

        "rules": [
            "MATCH,TEST"
        ]
    }

    return config


# ============================================================
# Wait for Mihomo Controller
# ============================================================

def wait_controller(controller_port, process, timeout):
    url = "http://127.0.0.1:" + str(controller_port) + "/version"

    start = time.monotonic()

    while time.monotonic() - start < timeout:
        if process.poll() is not None:
            return False, "mihomo_exited"

        try:
            r = requests.get(url, timeout=0.3)

            if r.status_code == 200:
                return True, None

        except requests.RequestException:
            pass

        time.sleep(0.05)

    return False, "controller_timeout"


# ============================================================
# Check whether local proxy port is listening
#
# IMPORTANT:
# Do NOT test Google/Youtube here.
# This function only checks whether Mihomo's local TCP
# proxy listener has started.
# ============================================================

def wait_proxy_port(proxy_port, process, timeout):
    start = time.monotonic()

    while time.monotonic() - start < timeout:
        if process.poll() is not None:
            return False, "mihomo_exited"

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(0.2)

        try:
            sock.connect(("127.0.0.1", proxy_port))
            sock.close()
            return True, None

        except OSError:
            sock.close()

        time.sleep(0.05)

    return False, "proxy_port_timeout"


# ============================================================
# Start Mihomo
# ============================================================

def start_mihomo(node, proxy_port, controller_port, config_dir):
    config = make_config(
        node,
        proxy_port,
        controller_port
    )

    config_file = config_dir / "config.yaml"

    with open(config_file, "w", encoding="utf-8") as f:
        yaml.safe_dump(
            config,
            f,
            allow_unicode=True,
            sort_keys=False
        )

    log_file = open(
        config_dir / "mihomo.log",
        "w",
        encoding="utf-8"
    )

    start_time = time.monotonic()

    process = subprocess.Popen(
        [
            str(MIHOMO_BIN),
            "-d",
            str(config_dir)
        ],
        stdout=log_file,
        stderr=subprocess.STDOUT
    )

    ok, error = wait_controller(
        controller_port,
        process,
        MIHOMO_STARTUP_TIMEOUT
    )

    if not ok:
        process.terminate()

        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()

        log_file.close()

        return None, {
            "success": False,
            "reason": error,
            "startup_time": time.monotonic() - start_time
        }

    ok, error = wait_proxy_port(
        proxy_port,
        process,
        MIHOMO_STARTUP_TIMEOUT
    )

    if not ok:
        process.terminate()

        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()

        log_file.close()

        return None, {
            "success": False,
            "reason": error,
            "startup_time": time.monotonic() - start_time
        }

    startup_time = time.monotonic() - start_time

    return (process, log_file), {
        "success": True,
        "startup_time": startup_time
    }


# ============================================================
# YouTube page test
# ============================================================

def test_youtube(proxy_port):
    proxy = "http://127.0.0.1:" + str(proxy_port)

    proxies = {
        "http": proxy,
        "https": proxy
    }

    start = time.monotonic()

    try:
        response = requests.get(
            YOUTUBE_URL,
            proxies=proxies,
            stream=True,
            timeout=(STAGE_TIMEOUT, STAGE_TIMEOUT),
            headers={
                "User-Agent": (
                    "Mozilla/5.0 "
                    "(Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 "
                    "(KHTML, like Gecko) "
                    "Chrome/153.0 Safari/537.36"
                )
            }
        )

        first_byte_time = time.monotonic()

        bytes_read = 0

        for chunk in response.iter_content(chunk_size=65536):
            if chunk:
                bytes_read += len(chunk)

                if bytes_read >= 256 * 1024:
                    break

        elapsed = time.monotonic() - start
        ttfb = first_byte_time - start

        status = response.status_code

        response.close()

        if status != 200:
            return {
                "success": False,
                "reason": "http_status_" + str(status),
                "elapsed": elapsed,
                "ttfb": ttfb,
                "bytes": bytes_read
            }

        return {
            "success": True,
            "elapsed": elapsed,
            "ttfb": ttfb,
            "bytes": bytes_read
        }

    except requests.RequestException as e:
        elapsed = time.monotonic() - start

        return {
            "success": False,
            "reason": type(e).__name__ + ": " + str(e),
            "elapsed": elapsed
        }


# ============================================================
# yt-dlp media URL extraction
# ============================================================

def extract_media_url(proxy_port):
    proxy = "http://127.0.0.1:" + str(proxy_port)

    command = [
        "yt-dlp",

        "--proxy",
        proxy,

        "--no-playlist",

        "-g",

        "-f",
        (
            "bestvideo[height<=1080]"
            "/best[height<=1080]"
        ),

        YOUTUBE_URL
    ]

    env = os.environ.copy()

    env["HTTP_PROXY"] = proxy
    env["HTTPS_PROXY"] = proxy
    env["ALL_PROXY"] = proxy

    start = time.monotonic()

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=STAGE_TIMEOUT,
            env=env
        )

    except subprocess.TimeoutExpired:
        elapsed = time.monotonic() - start

        return {
            "success": False,
            "reason": "yt_dlp_timeout",
            "elapsed": elapsed
        }

    elapsed = time.monotonic() - start

    if result.returncode != 0:
        error_text = result.stderr.strip()

        if len(error_text) > 1000:
            error_text = error_text[-1000:]

        return {
            "success": False,
            "reason": "yt_dlp_error",
            "error": error_text,
            "elapsed": elapsed
        }

    urls = [
        line.strip()
        for line in result.stdout.splitlines()
        if line.strip()
    ]

    if not urls:
        return {
            "success": False,
            "reason": "yt_dlp_empty_output",
            "elapsed": elapsed
        }

    media_url = urls[0]

    return {
        "success": True,
        "elapsed": elapsed,
        "url": media_url
    }


# ============================================================
# Real YouTube media download
#
# Classification:
#
# complete = downloaded 1 MiB
# partial  = downloaded >0 bytes but did not reach 1 MiB
# failed   = 0 bytes / transport error
# ============================================================

def download_media(media_url, proxy_port):
    proxy = "http://127.0.0.1:" + str(proxy_port)

    proxies = {
        "http": proxy,
        "https": proxy
    }

    headers = {
        "Range": "bytes=0-" + str(MAX_DOWNLOAD_BYTES - 1),
        "User-Agent": (
            "Mozilla/5.0 "
            "(Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 "
            "(KHTML, like Gecko) "
            "Chrome/153.0 Safari/537.36"
        )
    }

    start = time.monotonic()
    ttfb = None
    bytes_read = 0

    try:
        response = requests.get(
            media_url,
            proxies=proxies,
            headers=headers,
            stream=True,
            timeout=(STAGE_TIMEOUT, STAGE_TIMEOUT)
        )

        first_chunk = True

        for chunk in response.iter_content(chunk_size=65536):

            now = time.monotonic()

            if first_chunk:
                ttfb = now - start
                first_chunk = False

            if not chunk:
                continue

            remaining = MAX_DOWNLOAD_BYTES - bytes_read

            if len(chunk) > remaining:
                chunk = chunk[:remaining]

            bytes_read += len(chunk)

            if bytes_read >= MAX_DOWNLOAD_BYTES:
                break

            if now - start >= STAGE_TIMEOUT:
                break

        elapsed = time.monotonic() - start

        response.close()

        if bytes_read >= MAX_DOWNLOAD_BYTES:
            status = "complete"

        elif bytes_read > 0:
            status = "partial"

        else:
            status = "failed"

        throughput_mbps = 0.0

        if elapsed > 0:
            throughput_mbps = (
                bytes_read * 8 / elapsed / 1000000
            )

        return {
            "success": bytes_read > 0,
            "status": status,
            "bytes": bytes_read,
            "elapsed": elapsed,
            "ttfb": ttfb,
            "throughput_mbps": throughput_mbps
        }

    except requests.RequestException as e:
        elapsed = time.monotonic() - start

        throughput_mbps = 0.0

        if elapsed > 0:
            throughput_mbps = (
                bytes_read * 8 / elapsed / 1000000
            )

        return {
            "success": bytes_read > 0,
            "status": "failed" if bytes_read == 0 else "partial",
            "bytes": bytes_read,
            "elapsed": elapsed,
            "ttfb": ttfb,
            "throughput_mbps": throughput_mbps,
            "error": type(e).__name__ + ": " + str(e)
        }


# ============================================================
# Test one node
# ============================================================

def test_node(index, node):
    node_name = node.get("name", "UNKNOWN")
    protocol = node.get("type", "UNKNOWN")

    proxy_port = BASE_PROXY_PORT + index
    controller_port = BASE_CONTROLLER_PORT + index

    print(
        "[%d] TEST %s"
        % (index + 1, node_name),
        flush=True
    )

    result = {
        "index": index + 1,
        "name": node_name,
        "type": protocol,
        "proxy_port": proxy_port,
        "controller_port": controller_port,
        "startup": {},
        "youtube": {},
        "yt_dlp": {},
        "media": {},
        "success": False
    }

    config_dir = Path(
        tempfile.mkdtemp(
            prefix="free_nodes_%d_" % index
        )
    )

    mihomo = None

    try:
        # ----------------------------------------------------
        # STEP 0
        # ----------------------------------------------------

        mihomo, startup = start_mihomo(
            node,
            proxy_port,
            controller_port,
            config_dir
        )

        result["startup"] = startup

        if not startup.get("success"):
            print(
                "    FAIL | %s"
                % startup.get("reason"),
                flush=True
            )

            result["reason"] = startup.get("reason")

            return result

        print(
            "    Mihomo READY | %.3fs"
            % startup.get("startup_time", 0),
            flush=True
        )

        # ----------------------------------------------------
        # STEP 1
        # ----------------------------------------------------

        youtube = test_youtube(proxy_port)

        result["youtube"] = youtube

        if not youtube.get("success"):
            print(
                "    FAIL | YouTube | %s"
                % youtube.get("reason"),
                flush=True
            )

            result["reason"] = "youtube_page_failed"

            return result

        print(
            "    YouTube OK | %.2fs | TTFB %.2fs"
            % (
                youtube.get("elapsed", 0),
                youtube.get("ttfb", 0)
            ),
            flush=True
        )

        # ----------------------------------------------------
        # STEP 2
        # ----------------------------------------------------

        extraction = extract_media_url(proxy_port)

        result["yt_dlp"] = extraction

        if not extraction.get("success"):
            print(
                "    FAIL | yt-dlp | %s"
                % extraction.get("reason"),
                flush=True
            )

            result["reason"] = extraction.get("reason")

            return result

        print(
            "    yt-dlp OK | %.2fs"
            % extraction.get("elapsed", 0),
            flush=True
        )

        media_url = extraction.get("url", "")

        # Record whether the media URL contains an IPv6-looking
        # ip= parameter. This is diagnostic only.
        result["media"]["url_has_ipv6_ip_parameter"] = (
            "ip=" in media_url
            and ":" in media_url.split("ip=", 1)[1].split("&", 1)[0]
        )

        # ----------------------------------------------------
        # STEP 3
        # ----------------------------------------------------

        media = download_media(
            media_url,
            proxy_port
        )

        result["media"].update(media)

        status = media.get("status")

        if status == "complete":
            print(
                "    MEDIA COMPLETE | %d bytes | %.2f Mbps"
                % (
                    media.get("bytes", 0),
                    media.get("throughput_mbps", 0)
                ),
                flush=True
            )

            result["success"] = True
            result["reason"] = "media_complete"

        elif status == "partial":
            print(
                "    MEDIA PARTIAL | %d bytes | %.2f Mbps"
                % (
                    media.get("bytes", 0),
                    media.get("throughput_mbps", 0)
                ),
                flush=True
            )

            # Partial media is useful data, but is not considered
            # a fully successful node.
            result["success"] = False
            result["reason"] = "media_partial"

        else:
            print(
                "    MEDIA FAILED | %s"
                % media.get("error", "no_data"),
                flush=True
            )

            result["success"] = False
            result["reason"] = "media_failed"

        return result

    except Exception as e:
        result["success"] = False
        result["reason"] = (
            type(e).__name__ + ": " + str(e)
        )

        print(
            "    FAIL | INTERNAL | %s"
            % result["reason"],
            flush=True
        )

        return result

    finally:
        # ----------------------------------------------------
        # Stop Mihomo
        # ----------------------------------------------------

        if mihomo is not None:
            process, log_file = mihomo

            try:
                process.terminate()
                process.wait(timeout=1)

            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                    process.wait(timeout=1)
                except Exception:
                    pass

            except Exception:
                pass

            try:
                log_file.close()
            except Exception:
                pass


# ============================================================
# Main
# ============================================================

def main():
    print("=" * 60)
    print("free-nodes diagnostic test")
    print("=" * 60)

    if not MIHOMO_BIN.exists():
        raise RuntimeError(
            "Mihomo binary not found: %s"
            % MIHOMO_BIN
        )

    candidates = load_candidates()

    print(
        "Candidates: %d"
        % len(candidates)
    )

    print(
        "Concurrency: %d"
        % CONCURRENCY
    )

    print(
        "Stage timeout: %.0fs"
        % STAGE_TIMEOUT
    )

    print(
        "Max media download: %d bytes"
        % MAX_DOWNLOAD_BYTES
    )

    print("=" * 60)

    overall_start = time.monotonic()

    results = []

    with ThreadPoolExecutor(
        max_workers=CONCURRENCY
    ) as executor:

        futures = {}

        for index, node in enumerate(candidates):
            future = executor.submit(
                test_node,
                index,
                node
            )

            futures[future] = index

        for future in as_completed(futures):
            result = future.result()
            results.append(result)

    results.sort(
        key=lambda x: x.get("index", 0)
    )

    elapsed = time.monotonic() - overall_start

    startup_success = sum(
        1
        for r in results
        if r.get("startup", {}).get("success")
    )

    youtube_success = sum(
        1
        for r in results
        if r.get("youtube", {}).get("success")
    )

    ytdlp_success = sum(
        1
        for r in results
        if r.get("yt_dlp", {}).get("success")
    )

    media_complete = sum(
        1
        for r in results
        if r.get("media", {}).get("status") == "complete"
    )

    media_partial = sum(
        1
        for r in results
        if r.get("media", {}).get("status") == "partial"
    )

    media_failed = sum(
        1
        for r in results
        if r.get("media", {}).get("status") == "failed"
    )

    output = {
        "generated_at": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime()
        ),
        "candidates": len(candidates),
        "tested": len(results),
        "elapsed_seconds": elapsed,

        "summary": {
            "mihomo_startup_success": startup_success,
            "youtube_success": youtube_success,
            "yt_dlp_success": ytdlp_success,
            "media_complete": media_complete,
            "media_partial": media_partial,
            "media_failed": media_failed
        },

        "results": results
    }

    RESULT_FILE.parent.mkdir(
        parents=True,
        exist_ok=True
    )

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

    print()
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)

    print(
        "Candidates: %d"
        % len(candidates)
    )

    print(
        "Tested: %d"
        % len(results)
    )

    print(
        "Mihomo startup success: %d"
        % startup_success
    )

    print(
        "YouTube page success: %d"
        % youtube_success
    )

    print(
        "yt-dlp success: %d"
        % ytdlp_success
    )

    print(
        "Media complete: %d"
        % media_complete
    )

    print(
        "Media partial: %d"
        % media_partial
    )

    print(
        "Media failed: %d"
        % media_failed
    )

    print(
        "Elapsed: %.1fs"
        % elapsed
    )

    print(
        "Results: %s"
        % RESULT_FILE
    )


if __name__ == "__main__":
    main()

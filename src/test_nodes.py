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

CANDIDATES = os.path.join(ROOT, "data", "candidates.yaml")
SETTINGS = os.path.join(ROOT, "config", "settings.yaml")
RESULTS = os.path.join(ROOT, "data", "test-results.json")
MIHOMO = os.path.join(ROOT, "bin", "mihomo")

YOUTUBE = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

PROXY_BASE = 20000
CONTROLLER_BASE = 21000

STARTUP_TIMEOUT = 5
STAGE_TIMEOUT = 3
MAX_BYTES = 1024 * 1024
CONCURRENCY = 20


def read_yaml(path):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def make_config(node, proxy_port, controller_port):
    proxy = dict(node)
    proxy["name"] = "TEST_NODE"

    return {
        "mixed-port": proxy_port,
        "allow-lan": False,
        "mode": "rule",
        "log-level": "info",
        "external-controller": "127.0.0.1:" + str(controller_port),
        "proxies": [proxy],
        "proxy-groups": [
            {
                "name": "PROXY",
                "type": "select",
                "proxies": ["TEST_NODE"]
            }
        ],
        "rules": ["MATCH,PROXY"]
    }


def start_mihomo(config_file, log_file):
    f = open(log_file, "w", encoding="utf-8")

    p = subprocess.Popen(
        [MIHOMO, "-f", config_file],
        stdout=f,
        stderr=subprocess.STDOUT
    )

    return p, f


def wait_mihomo(port):
    url = "http://127.0.0.1:" + str(port) + "/version"
    begin = time.monotonic()

    while time.monotonic() - begin < STARTUP_TIMEOUT:
        try:
            r = requests.get(url, timeout=0.5)

            if r.status_code == 200:
                return True

        except requests.RequestException:
            pass

        time.sleep(0.05)

    return False


def read_log(path):
    try:
        with open(
            path,
            "r",
            encoding="utf-8",
            errors="replace"
        ) as f:
            lines = f.readlines()

        return "".join(lines[-30:])

    except Exception:
        return ""


def test_page(proxy_port):
    proxy = "http://127.0.0.1:" + str(proxy_port)

    result = {
        "success": False,
        "status": None,
        "elapsed": None,
        "ttfb": None,
        "bytes": 0,
        "error": None
    }

    begin = time.monotonic()

    try:
        r = requests.get(
            YOUTUBE,
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

        result["status"] = r.status_code

        first = None

        for chunk in r.iter_content(chunk_size=65536):
            now = time.monotonic()

            if chunk and first is None:
                first = now
                result["ttfb"] = now - begin

            if chunk:
                result["bytes"] += len(chunk)

            if now - begin >= STAGE_TIMEOUT:
                break

        result["elapsed"] = time.monotonic() - begin

        if r.status_code == 200:
            result["success"] = True
        else:
            result["error"] = "http_" + str(r.status_code)

        r.close()

    except Exception as e:
        result["elapsed"] = time.monotonic() - begin
        result["error"] = type(e).__name__ + ": " + str(e)

    return result


def get_media_url(proxy_port):
    proxy = "http://127.0.0.1:" + str(proxy_port)

    cmd = [
        "yt-dlp",
        "--proxy", proxy,
        "--no-warnings",
        "--quiet",
        "--skip-download",
        "-f",
        "bestvideo[height<=1080]",
        "-g",
        YOUTUBE
    ]

    env = os.environ.copy()
    env["HTTP_PROXY"] = proxy
    env["HTTPS_PROXY"] = proxy
    env["http_proxy"] = proxy
    env["https_proxy"] = proxy

    begin = time.monotonic()

    try:
        p = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=STAGE_TIMEOUT,
            env=env
        )

        elapsed = time.monotonic() - begin

        if p.returncode != 0:
            error = p.stderr.strip()

            if not error:
                error = "yt_dlp_exit_" + str(p.returncode)

            return {
                "success": False,
                "elapsed": elapsed,
                "url": None,
                "error": error
            }

        lines = p.stdout.splitlines()

        urls = [x.strip() for x in lines if x.strip()]

        if not urls:
            return {
                "success": False,
                "elapsed": elapsed,
                "url": None,
                "error": "empty_media_url"
            }

        return {
            "success": True,
            "elapsed": elapsed,
            "url": urls[0],
            "error": None
        }

    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "elapsed": time.monotonic() - begin,
            "url": None,
            "error": "yt_dlp_timeout"
        }

    except Exception as e:
        return {
            "success": False,
            "elapsed": time.monotonic() - begin,
            "url": None,
            "error": type(e).__name__ + ": " + str(e)
        }


def download_media(url, proxy_port):
    proxy = "http://127.0.0.1:" + str(proxy_port)

    result = {
        "bytes": 0,
        "elapsed": None,
        "ttfb": None,
        "speed_mbps": 0,
        "status": None,
        "error": None
    }

    begin = time.monotonic()

    try:
        r = requests.get(
            url,
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

        first = None

        for chunk in r.iter_content(chunk_size=65536):
            now = time.monotonic()

            if chunk and first is None:
                first = now
                result["ttfb"] = now - begin

            if chunk:
                left = MAX_BYTES - result["bytes"]
                result["bytes"] += len(chunk[:left])

            if result["bytes"] >= MAX_BYTES:
                result["status"] = "completed_1mb"
                break

            if now - begin >= STAGE_TIMEOUT:
                result["status"] = "time_limit"
                break

        result["elapsed"] = time.monotonic() - begin

        if result["bytes"] > 0 and result["elapsed"] > 0:
            result["speed_mbps"] = (
                result["bytes"] * 8
                / result["elapsed"]
                / 1000000
            )

        if result["status"] is None:
            result["status"] = "closed"

        r.close()

    except Exception as e:
        result["elapsed"] = time.monotonic() - begin
        result["error"] = type(e).__name__ + ": " + str(e)

    return result


def test_node(node, index):
    name = str(node.get("name", "node-" + str(index)))
    protocol = str(node.get("type", "unknown"))

    proxy_port = PROXY_BASE + index
    controller_port = CONTROLLER_BASE + index

    result = {
        "index": index,
        "name": name,
        "type": protocol,
        "startup": {},
        "youtube": {},
        "yt_dlp": {},
        "media": {},
        "measurement_valid": False,
        "failure_reason": None,
        "mihomo_log": None
    }

    temp = tempfile.mkdtemp(prefix="free_nodes_")
    config = os.path.join(temp, "config.yaml")
    log = os.path.join(temp, "mihomo.log")

    process = None
    log_file = None

    try:
        cfg = make_config(
            node,
            proxy_port,
            controller_port
        )

        with open(config, "w", encoding="utf-8") as f:
            yaml.safe_dump(
                cfg,
                f,
                allow_unicode=True,
                sort_keys=False
            )

        begin = time.monotonic()

        process, log_file = start_mihomo(
            config,
            log
        )

        ready = wait_mihomo(controller_port)

        result["startup"] = {
            "success": ready,
            "elapsed": time.monotonic() - begin
        }

        if not ready:
            result["failure_reason"] = "mihomo_startup_timeout"
            result["mihomo_log"] = read_log(log)
            return result

        page = test_page(proxy_port)
        result["youtube"] = page

        if not page["success"]:
            result["failure_reason"] = "youtube_page_failed"
            return result

        media = get_media_url(proxy_port)
        result["yt_dlp"] = {
            "success": media["success"],
            "elapsed": media["elapsed"],
            "error": media["error"]
        }

        if not media["success"]:
            result["failure_reason"] = media["error"]
            return result

        download = download_media(
            media["url"],
            proxy_port
        )

        result["media"] = download

        if download["bytes"] > 0:
            result["measurement_valid"] = True
            return result

        result["failure_reason"] = (
            download["error"]
            or "media_no_data"
        )

        return result

    except Exception as e:
        result["failure_reason"] = (
            type(e).__name__ + ": " + str(e)
        )

        return result

    finally:
        if process is not None:
            if process.poll() is None:
                process.terminate()

                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()

        if log_file is not None:
            log_file.close()

        shutil.rmtree(
            temp,
            ignore_errors=True
        )


def main():
    settings = read_yaml(SETTINGS) or {}

    data = read_yaml(CANDIDATES)

    if isinstance(data, dict):
        nodes = data.get("proxies", [])
    elif isinstance(data, list):
        nodes = data
    else:
        raise RuntimeError(
            "Invalid candidates.yaml format"
        )

    nodes = [
        x for x in nodes
        if isinstance(x, dict)
    ]

    concurrency = int(
        settings.get(
            "concurrency",
            CONCURRENCY
        )
    )

    timeout = float(
        settings.get(
            "test",
            {}
        ).get(
            "timeout_seconds",
            STAGE_TIMEOUT
        )
    )

    print()
    print("free-nodes diagnostic test")
    print()
    print("Candidates:", len(nodes))
    print("Concurrency:", concurrency)
    print("Stage timeout:", timeout)
    print("Maximum media bytes:", MAX_BYTES)
    print()

    begin = time.monotonic()
    results = []

    with ThreadPoolExecutor(
        max_workers=concurrency
    ) as pool:

        futures = {}

        for index, node in enumerate(nodes):
            future = pool.submit(
                test_node,
                node,
                index
            )

            futures[future] = index

        done = 0

        for future in as_completed(futures):
            done += 1

            try:
                result = future.result()

            except Exception as e:
                result = {
                    "index": futures[future],
                    "measurement_valid": False,
                    "failure_reason": (
                        "worker_exception: "
                        + type(e).__name__
                        + ": "
                        + str(e)
                    )
                }

            results.append(result)

            if result.get("measurement_valid"):
                media = result["media"]

                print(
                    "[" + str(done)
                    + "/" + str(len(nodes))
                    + "] DATA "
                    + result["name"]
                    + " | "
                    + str(media["bytes"])
                    + "B | "
                    + format(
                        media["speed_mbps"],
                        ".2f"
                    )
                    + " Mbps"
                )

            else:
                print(
                    "[" + str(done)
                    + "/" + str(len(nodes))
                    + "] FAIL "
                    + result.get(
                        "name",
                        "unknown"
                    )
                    + " | "
                    + str(
                        result.get(
                            "failure_reason"
                        )
                    )
                )

    results.sort(
        key=lambda x: x.get("index", 0)
    )

    startup_ok = sum(
        1
        for x in results
        if x.get("startup", {}).get("success")
    )

    youtube_ok = sum(
        1
        for x in results
        if x.get("youtube", {}).get("success")
    )

    ytdlp_ok = sum(
        1
        for x in results
        if x.get("yt_dlp", {}).get("success")
    )

    media_ok = sum(
        1
        for x in results
        if x.get("measurement_valid")
    )

    elapsed = time.monotonic() - begin

    output = {
        "generated_at": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime()
        ),
        "candidates": len(nodes),
        "tested": len(results),
        "elapsed_seconds": elapsed,
        "summary": {
            "mihomo_startup_success": startup_ok,
            "youtube_success": youtube_ok,
            "yt_dlp_success": ytdlp_ok,
            "media_measurement_valid": media_ok
        },
        "results": results
    }

    os.makedirs(
        os.path.dirname(RESULTS),
        exist_ok=True
    )

    with open(
        RESULTS,
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
    print("========== SUMMARY ==========")
    print("Candidates:", len(nodes))
    print("Tested:", len(results))
    print("Mihomo startup success:", startup_ok)
    print("YouTube page success:", youtube_ok)
    print("yt-dlp success:", ytdlp_ok)
    print("Media measurement valid:", media_ok)
    print("Elapsed:", format(elapsed, ".1f"), "seconds")
    print("==============================")
    print()
    print("Results:", RESULTS)


if __name__ == "__main__":
    main()

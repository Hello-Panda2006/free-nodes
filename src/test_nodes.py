import json
import os
import subprocess
import tempfile
import time

import requests
import yaml


MIHOMO_BIN = "./bin/mihomo"

TEST_VIDEO_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

STAGE_TIMEOUT = 3.0
MIHOMO_STARTUP_TIMEOUT = 5.0
MAX_DOWNLOAD_BYTES = 1024 * 1024

CANDIDATE_FILE = "data/candidates.yaml"
RESULT_FILE = "data/test-results-debug.json"

# 诊断阶段只测试前 5 个节点
MAX_NODES = 5


def load_nodes():
    with open(CANDIDATE_FILE, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    proxies = data.get("proxies", [])

    if not proxies:
        raise RuntimeError("No proxy nodes found")

    return proxies[:MAX_NODES]


def write_mihomo_config(node, path, mixed_port, controller_port):
    config = {
        "mixed-port": mixed_port,
        "external-controller": f"127.0.0.1:{controller_port}",
        "log-level": "error",

        "proxies": [
            node
        ],

        "proxy-groups": [
            {
                "name": "TEST",
                "type": "select",
                "proxies": [
                    node["name"]
                ]
            }
        ],

        "rules": [
            "MATCH,TEST"
        ]
    }

    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(
            config,
            f,
            allow_unicode=True,
            sort_keys=False
        )


def wait_for_mihomo(process, controller_port):
    url = f"http://127.0.0.1:{controller_port}/version"

    start = time.perf_counter()

    while time.perf_counter() - start < MIHOMO_STARTUP_TIMEOUT:

        if process.poll() is not None:
            raise RuntimeError(
                f"Mihomo exited during startup, "
                f"return code={process.returncode}"
            )

        try:
            response = requests.get(
                url,
                timeout=0.5
            )

            if response.status_code == 200:
                return time.perf_counter() - start

        except requests.RequestException:
            pass

        time.sleep(0.05)

    raise TimeoutError(
        "mihomo_startup_timeout"
    )


def test_youtube_page(proxy_port):
    proxies = {
        "http": f"http://127.0.0.1:{proxy_port}",
        "https": f"http://127.0.0.1:{proxy_port}"
    }

    start = time.perf_counter()

    response = requests.get(
        TEST_VIDEO_URL,
        proxies=proxies,
        timeout=STAGE_TIMEOUT,
        headers={
            "User-Agent": (
                "Mozilla/5.0 "
                "(Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/131.0 Safari/537.36"
            )
        }
    )

    elapsed = time.perf_counter() - start

    response.raise_for_status()

    return {
        "status_code": response.status_code,
        "elapsed": elapsed,
        "bytes": len(response.content)
    }


def extract_media_url(proxy_port):
    proxy = f"http://127.0.0.1:{proxy_port}"

    env = os.environ.copy()

    # 明确要求 yt-dlp 通过 Mihomo
    env["HTTP_PROXY"] = proxy
    env["HTTPS_PROXY"] = proxy
    env["http_proxy"] = proxy
    env["https_proxy"] = proxy

    start = time.perf_counter()

    result = subprocess.run(
        [
            "yt-dlp",

            "--no-playlist",
            "--skip-download",
            "-g",

            "--no-warnings",

            "--proxy",
            proxy,

            "--socket-timeout",
            str(STAGE_TIMEOUT),

            "-f",
            (
                "bestvideo[height<=1080][vcodec^=vp9]/"
                "bestvideo[height<=1080][vcodec^=avc1]/"
                "bestvideo[height<=1080]"
            ),

            TEST_VIDEO_URL
        ],

        capture_output=True,
        text=True,

        timeout=STAGE_TIMEOUT,

        env=env
    )

    elapsed = time.perf_counter() - start

    if result.returncode != 0:

        stderr = result.stderr.strip()

        if not stderr:
            stderr = "unknown yt-dlp error"

        raise RuntimeError(
            "yt_dlp_error: " + stderr[-1500:]
        )

    media_urls = result.stdout.strip().splitlines()

    if not media_urls:
        raise RuntimeError(
            "yt_dlp_no_media_url"
        )

    return {
        "url": media_urls[0],
        "elapsed": elapsed
    }


def download_media(media_url, proxy_port):
    proxies = {
        "http": f"http://127.0.0.1:{proxy_port}",
        "https": f"http://127.0.0.1:{proxy_port}"
    }

    start = time.perf_counter()

    downloaded = 0
    first_byte_time = None

    try:

        response = requests.get(
            media_url,

            proxies=proxies,

            stream=True,

            timeout=STAGE_TIMEOUT,

            headers={
                "User-Agent": (
                    "Mozilla/5.0 "
                    "(Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 "
                    "(KHTML, like Gecko) "
                    "Chrome/131.0 Safari/537.36"
                )
            }
        )

        response.raise_for_status()

        for chunk in response.iter_content(
            chunk_size=64 * 1024
        ):

            now = time.perf_counter()

            if now - start > STAGE_TIMEOUT:

                raise TimeoutError(
                    f"media_timeout_after_{downloaded}_bytes"
                )

            if not chunk:
                continue

            if first_byte_time is None:
                first_byte_time = now

            remaining = MAX_DOWNLOAD_BYTES - downloaded

            if remaining <= 0:
                break

            if len(chunk) > remaining:
                chunk = chunk[:remaining]

            downloaded += len(chunk)

            if downloaded >= MAX_DOWNLOAD_BYTES:
                break

        response.close()

    except requests.exceptions.Timeout:

        raise TimeoutError(
            f"media_timeout_after_{downloaded}_bytes"
        )

    finally:

        try:
            response.close()
        except Exception:
            pass

    end = time.perf_counter()

    if first_byte_time is None:

        raise RuntimeError(
            "media_no_data"
        )

    total_time = end - start

    ttfb = first_byte_time - start

    throughput_mbps = (
        downloaded * 8
        / total_time
        / 1_000_000
        if total_time > 0
        else 0
    )

    return {
        "bytes": downloaded,
        "ttfb": ttfb,
        "total_time": total_time,
        "throughput_mbps": throughput_mbps
    }


def test_one(node, index):

    result = {
        "index": index,
        "name": node.get("name"),
        "type": node.get("type"),
        "result": "failed",
        "reason": None
    }

    # 每个诊断节点使用独立端口
    proxy_port = 20000 + index
    controller_port = 21000 + index

    print("")
    print("=" * 70)

    print(
        f"[{index}/{MAX_NODES}] "
        f"{node.get('name')}",
        flush=True
    )

    print(
        f"Protocol: {node.get('type')}",
        flush=True
    )

    print(
        f"Proxy port: {proxy_port}",
        flush=True
    )

    print(
        f"Controller port: {controller_port}",
        flush=True
    )

    mihomo = None
    temp_dir = None

    try:

        # --------------------------------------------------
        # STEP 0
        # --------------------------------------------------

        print(
            "[STEP 0] Starting Mihomo...",
            flush=True
        )

        temp_dir = tempfile.mkdtemp(
            prefix="free-nodes-debug-"
        )

        config_path = os.path.join(
            temp_dir,
            "config.yaml"
        )

        write_mihomo_config(
            node,
            config_path,
            proxy_port,
            controller_port
        )

        mihomo = subprocess.Popen(
            [
                MIHOMO_BIN,

                "-d",
                temp_dir,

                "-f",
                config_path
            ],

            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )

        startup_time = wait_for_mihomo(
            mihomo,
            controller_port
        )

        result["mihomo_startup"] = startup_time

        print(
            f"[STEP 0] Mihomo READY "
            f"({startup_time:.3f}s)",
            flush=True
        )

        # --------------------------------------------------
        # STEP 1
        # --------------------------------------------------

        print(
            "[STEP 1] Testing YouTube page...",
            flush=True
        )

        page = test_youtube_page(
            proxy_port
        )

        result["youtube_page"] = page

        print(
            f"[STEP 1] SUCCESS | "
            f"HTTP {page['status_code']} | "
            f"{page['elapsed']:.3f}s | "
            f"{page['bytes']:,} bytes",
            flush=True
        )

        # --------------------------------------------------
        # STEP 2
        # --------------------------------------------------

        print(
            "[STEP 2] yt-dlp extracting media URL...",
            flush=True
        )

        extraction = extract_media_url(
            proxy_port
        )

        result["extraction"] = {
            "elapsed": extraction["elapsed"]
        }

        print(
            f"[STEP 2] SUCCESS | "
            f"{extraction['elapsed']:.3f}s",
            flush=True
        )

        # --------------------------------------------------
        # STEP 3
        # --------------------------------------------------

        print(
            "[STEP 3] Downloading YouTube media "
            "(maximum 1 MiB)...",
            flush=True
        )

        media = download_media(
            extraction["url"],
            proxy_port
        )

        result["media"] = media

        print(
            f"[STEP 3] SUCCESS | "
            f"{media['bytes']:,} bytes | "
            f"TTFB {media['ttfb']:.3f}s | "
            f"Total {media['total_time']:.3f}s | "
            f"{media['throughput_mbps']:.2f} Mbps",
            flush=True
        )

        result["result"] = "passed"

        print(
            "RESULT: PASS",
            flush=True
        )

    except TimeoutError as e:

        result["reason"] = str(e)

        print(
            f"RESULT: FAIL | {result['reason']}",
            flush=True
        )

    except subprocess.TimeoutExpired:

        result["reason"] = (
            "yt_dlp_timeout"
        )

        print(
            "RESULT: FAIL | yt_dlp_timeout",
            flush=True
        )

    except requests.exceptions.Timeout:

        result["reason"] = (
            "youtube_page_timeout"
        )

        print(
            "RESULT: FAIL | youtube_page_timeout",
            flush=True
        )

    except requests.exceptions.RequestException as e:

        result["reason"] = (
            f"request_error: {type(e).__name__}"
        )

        print(
            f"RESULT: FAIL | {result['reason']}",
            flush=True
        )

    except Exception as e:

        result["reason"] = (
            f"{type(e).__name__}: {e}"
        )

        print(
            f"RESULT: FAIL | {result['reason']}",
            flush=True
        )

    finally:

        if mihomo is not None:

            try:
                mihomo.terminate()

                mihomo.wait(
                    timeout=2
                )

            except Exception:

                try:
                    mihomo.kill()
                except Exception:
                    pass

    return result


def main():

    nodes = load_nodes()

    print(
        "free-nodes - diagnostic batch test",
        flush=True
    )

    print("")

    print(
        f"Loaded {len(nodes)} diagnostic nodes",
        flush=True
    )

    print(
        "Concurrency: 1",
        flush=True
    )

    print(
        f"Stage timeout: {STAGE_TIMEOUT:.1f}s",
        flush=True
    )

    print(
        f"Maximum media download: "
        f"{MAX_DOWNLOAD_BYTES:,} bytes",
        flush=True
    )

    results = []

    start = time.perf_counter()

    for index, node in enumerate(
        nodes,
        start=1
    ):

        result = test_one(
            node,
            index
        )

        results.append(result)

    elapsed = time.perf_counter() - start

    os.makedirs(
        os.path.dirname(RESULT_FILE),
        exist_ok=True
    )

    with open(
        RESULT_FILE,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            {
                "mode": "diagnostic",

                "candidates": len(nodes),

                "elapsed": elapsed,

                "results": results
            },

            f,

            ensure_ascii=False,

            indent=2
        )

    passed = sum(
        1
        for result in results
        if result.get("result") == "passed"
    )

    print("")
    print("=" * 70)

    print(
        "DIAGNOSTIC TEST COMPLETE",
        flush=True
    )

    print(
        f"Candidates: {len(nodes)}",
        flush=True
    )

    print(
        f"Passed:    {passed}",
        flush=True
    )

    print(
        f"Failed:    {len(nodes) - passed}",
        flush=True
    )

    print(
        f"Elapsed:   {elapsed:.1f}s",
        flush=True
    )

    print(
        f"Results:   {RESULT_FILE}",
        flush=True
    )


if __name__ == "__main__":
    main()

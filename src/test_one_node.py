import os
import sys
import time
import signal
import shutil
import tempfile
import subprocess
from pathlib import Path

import requests
import yaml


MIHOMO_BIN = "./bin/mihomo"

PROXY_HOST = "127.0.0.1"
PROXY_PORT = 7890

TEST_VIDEO_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

STAGE_TIMEOUT = 3.0
MAX_DOWNLOAD_BYTES = 1024 * 1024


class StageTimeout(Exception):
    pass


def timeout_handler(signum, frame):
    raise StageTimeout(
        f"Stage exceeded {STAGE_TIMEOUT:.1f} seconds"
    )


def load_first_node():
    path = Path("data/candidates.yaml")

    if not path.exists():
        raise RuntimeError(
            "data/candidates.yaml not found"
        )

    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    nodes = data.get("proxies", [])

    if not nodes:
        raise RuntimeError(
            "No proxy nodes found"
        )

    return nodes[0]


def build_mihomo_config(node, config_path):
    config = {
        "mixed-port": PROXY_PORT,
        "allow-lan": False,
        "mode": "rule",
        "log-level": "warning",
        "ipv6": False,

        "proxies": [
            node
        ],

        "proxy-groups": [
            {
                "name": "TEST",
                "type": "select",
                "proxies": [
                    node["name"]
                ],
            }
        ],

        "rules": [
            "MATCH,TEST"
        ],
    }

    with open(
        config_path,
        "w",
        encoding="utf-8"
    ) as f:
        yaml.safe_dump(
            config,
            f,
            allow_unicode=True,
            sort_keys=False,
        )


def run_with_timeout(func, timeout):
    signal.setitimer(
        signal.ITIMER_REAL,
        timeout,
    )

    try:
        return func()

    finally:
        signal.setitimer(
            signal.ITIMER_REAL,
            0,
        )


def proxy_dict():
    return {
        "http": (
            f"http://{PROXY_HOST}:{PROXY_PORT}"
        ),
        "https": (
            f"http://{PROXY_HOST}:{PROXY_PORT}"
        ),
    }


def wait_for_mihomo():
    deadline = time.monotonic() + STAGE_TIMEOUT

    while time.monotonic() < deadline:

        try:
            response = requests.get(
                "http://www.youtube.com/",
                proxies=proxy_dict(),
                timeout=0.3,
            )

            if response.status_code < 500:
                return

        except requests.RequestException:
            pass

        time.sleep(0.05)

    raise StageTimeout(
        "Mihomo proxy did not become ready"
    )


def test_youtube_page():
    start = time.monotonic()

    response = requests.get(
        "https://www.youtube.com/",
        proxies=proxy_dict(),
        timeout=STAGE_TIMEOUT,
    )

    elapsed = time.monotonic() - start

    if response.status_code != 200:
        raise RuntimeError(
            f"YouTube returned HTTP "
            f"{response.status_code}"
        )

    return {
        "status_code": response.status_code,
        "elapsed": elapsed,
        "bytes": len(response.content),
    }


def extract_media_url():
    ytdlp = shutil.which("yt-dlp")

    if not ytdlp:
        raise RuntimeError(
            "yt-dlp executable not found"
        )

    # 固定在 1080p 以下。
    #
    # 优先：
    #   1. VP9
    #   2. AVC1
    #   3. 其他编码
    #
    # 不使用 4K，避免节点之间因为格式差异
    # 导致测速结果不可比较。
    format_selector = (
        "bestvideo[height<=1080][vcodec^=vp9]/"
        "bestvideo[height<=1080][vcodec^=avc1]/"
        "bestvideo[height<=1080]"
    )

    command = [
        ytdlp,

        "--proxy",
        f"http://{PROXY_HOST}:{PROXY_PORT}",

        "--no-playlist",
        "--skip-download",
        "--no-warnings",
        "--quiet",

        "--socket-timeout",
        str(int(STAGE_TIMEOUT)),

        "-f",
        format_selector,

        "-g",
        TEST_VIDEO_URL,
    ]

    start = time.monotonic()

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=STAGE_TIMEOUT,
    )

    elapsed = time.monotonic() - start

    if result.returncode != 0:
        raise RuntimeError(
            "yt-dlp failed: "
            + result.stderr.strip()[:500]
        )

    urls = [
        line.strip()
        for line in result.stdout.splitlines()
        if line.strip()
    ]

    if not urls:
        raise RuntimeError(
            "yt-dlp returned no media URL"
        )

    return {
        "url": urls[0],
        "elapsed": elapsed,
    }


def download_media(media_url):
    start = time.monotonic()

    response = requests.get(
        media_url,
        proxies=proxy_dict(),
        stream=True,
        timeout=STAGE_TIMEOUT,
    )

    response.raise_for_status()

    ttfb = time.monotonic() - start

    downloaded = 0

    download_start = time.monotonic()

    for chunk in response.iter_content(
        chunk_size=64 * 1024
    ):

        if not chunk:
            continue

        remaining = (
            MAX_DOWNLOAD_BYTES
            - downloaded
        )

        if len(chunk) > remaining:
            chunk = chunk[:remaining]

        downloaded += len(chunk)

        if downloaded >= MAX_DOWNLOAD_BYTES:
            break

        if (
            time.monotonic()
            - download_start
            > STAGE_TIMEOUT
        ):
            raise StageTimeout(
                "Media download exceeded "
                f"{STAGE_TIMEOUT:.1f} seconds"
            )

    download_time = (
        time.monotonic()
        - download_start
    )

    if downloaded < MAX_DOWNLOAD_BYTES:
        raise StageTimeout(
            f"Only downloaded {downloaded:,} "
            "bytes before timeout"
        )

    throughput_mbps = (
        downloaded
        * 8
        / download_time
        / 1_000_000
    )

    return {
        "downloaded": downloaded,
        "ttfb": ttfb,
        "download_time": download_time,
        "throughput_mbps": throughput_mbps,
    }


def main():

    print(
        "free-nodes - single YouTube media test"
    )
    print()

    node = load_first_node()

    print(
        f"Node name: {node['name']}"
    )

    print(
        f"Protocol:  "
        f"{node.get('type', 'unknown')}"
    )

    print(
        f"Stage timeout: "
        f"{STAGE_TIMEOUT:.1f} seconds"
    )

    print(
        f"Maximum media download: "
        f"{MAX_DOWNLOAD_BYTES:,} bytes"
    )

    print()

    temp_dir = tempfile.mkdtemp()

    config_path = os.path.join(
        temp_dir,
        "config.yaml",
    )

    build_mihomo_config(
        node,
        config_path,
    )

    print(
        f"Temporary config: {config_path}"
    )

    print("Starting Mihomo...")

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

    total_start = time.monotonic()

    try:

        # --------------------------------------------------
        # Mihomo
        # --------------------------------------------------

        print()
        print("STEP 0: Mihomo startup")

        run_with_timeout(
            wait_for_mihomo,
            STAGE_TIMEOUT,
        )

        print(
            "Mihomo proxy is ready: "
            f"{PROXY_HOST}:{PROXY_PORT}"
        )

        # --------------------------------------------------
        # STEP 1
        # --------------------------------------------------

        print()
        print("STEP 1: YouTube page")

        page = run_with_timeout(
            test_youtube_page,
            STAGE_TIMEOUT,
        )

        print(
            f"HTTP status: "
            f"{page['status_code']}"
        )

        print(
            f"Page elapsed: "
            f"{page['elapsed']:.3f} seconds"
        )

        print(
            f"Page bytes: "
            f"{page['bytes']:,}"
        )

        print(
            "YouTube page test: SUCCESS"
        )

        # --------------------------------------------------
        # STEP 2
        # --------------------------------------------------

        print()
        print(
            "STEP 2: "
            "yt-dlp media extraction"
        )

        media = run_with_timeout(
            extract_media_url,
            STAGE_TIMEOUT,
        )

        print(
            f"Extraction elapsed: "
            f"{media['elapsed']:.3f} seconds"
        )

        print(
            "yt-dlp extraction: SUCCESS"
        )

        # --------------------------------------------------
        # STEP 3
        # --------------------------------------------------

        print()
        print(
            "STEP 3: "
            "Real YouTube media download"
        )

        print(
            f"Maximum download: "
            f"{MAX_DOWNLOAD_BYTES:,} bytes"
        )

        download = run_with_timeout(
            lambda: download_media(
                media["url"]
            ),
            STAGE_TIMEOUT,
        )

        print(
            f"Downloaded: "
            f"{download['downloaded']:,} bytes"
        )

        print(
            f"Media TTFB: "
            f"{download['ttfb']:.3f} seconds"
        )

        print(
            f"Download time: "
            f"{download['download_time']:.3f} "
            "seconds"
        )

        print(
            f"Throughput: "
            f"{download['throughput_mbps']:.2f} Mbps"
        )

        print(
            "Media download: SUCCESS"
        )

        # --------------------------------------------------
        # FINAL
        # --------------------------------------------------

        total_elapsed = (
            time.monotonic()
            - total_start
        )

        print()
        print("FINAL RESULT")

        print(
            f"Node:             "
            f"{node['name']}"
        )

        print(
            f"Protocol:         "
            f"{node.get('type', 'unknown')}"
        )

        print(
            f"Total test time:  "
            f"{total_elapsed:.3f}s"
        )

        print(
            f"YouTube page:     "
            f"SUCCESS ({page['elapsed']:.3f}s)"
        )

        print(
            f"yt-dlp:           "
            f"SUCCESS ({media['elapsed']:.3f}s)"
        )

        print(
            f"Media download:   "
            f"SUCCESS"
        )

        print(
            f"Media TTFB:       "
            f"{download['ttfb']:.3f}s"
        )

        print(
            f"Media throughput: "
            f"{download['throughput_mbps']:.2f} Mbps"
        )

    except subprocess.TimeoutExpired:

        print()
        print("FINAL RESULT")
        print(
            f"Node:             "
            f"{node['name']}"
        )
        print(
            f"Protocol:         "
            f"{node.get('type', 'unknown')}"
        )
        print("Result:            TIMEOUT")
        print(
            "Reason:            "
            "Stage exceeded 3 seconds"
        )

        sys.exit(1)

    except StageTimeout as e:

        print()
        print("FINAL RESULT")
        print(
            f"Node:             "
            f"{node['name']}"
        )
        print(
            f"Protocol:         "
            f"{node.get('type', 'unknown')}"
        )
        print("Result:            TIMEOUT")
        print(
            f"Reason:            {e}"
        )

        sys.exit(1)

    except Exception as e:

        print()
        print("FINAL RESULT")
        print(
            f"Node:             "
            f"{node['name']}"
        )
        print(
            f"Protocol:         "
            f"{node.get('type', 'unknown')}"
        )
        print("Result:            FAILED")
        print(
            f"Reason:            {e}"
        )

        sys.exit(1)

    finally:

        process.terminate()

        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()

        shutil.rmtree(
            temp_dir,
            ignore_errors=True,
        )


if __name__ == "__main__":
    main()

import os
import sys
import time
import yaml
import signal
import shutil
import tempfile
import subprocess
from pathlib import Path

import requests


Mihomo_BIN = "./bin/mihomo"

PROXY_HOST = "127.0.0.1"
PROXY_PORT = 7890

TEST_VIDEO_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

MAX_TOTAL_SECONDS = 3.0
MAX_DOWNLOAD_BYTES = 1024 * 1024


class TestTimeout(Exception):
    pass


def timeout_handler(signum, frame):
    raise TestTimeout("Node test exceeded 3 seconds")


def load_first_node():
    path = Path("data/candidates.yaml")

    if not path.exists():
        raise RuntimeError("data/candidates.yaml not found")

    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    nodes = data.get("proxies", [])

    if not nodes:
        raise RuntimeError("No proxy nodes found")

    return nodes[0]


def build_mihomo_config(node, config_path):
    config = {
        "mixed-port": PROXY_PORT,
        "allow-lan": False,
        "mode": "rule",
        "log-level": "warning",
        "ipv6": False,

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
        ]
    }

    with open(config_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(
            config,
            f,
            allow_unicode=True,
            sort_keys=False
        )


def remaining_time(start_time):
    remaining = MAX_TOTAL_SECONDS - (time.monotonic() - start_time)

    if remaining <= 0:
        raise TestTimeout("Node test exceeded 3 seconds")

    return remaining


def wait_for_mihomo(start_time):
    while True:
        remaining = remaining_time(start_time)

        try:
            r = requests.get(
                "http://www.youtube.com/",
                proxies={
                    "http": f"http://{PROXY_HOST}:{PROXY_PORT}",
                    "https": f"http://{PROXY_HOST}:{PROXY_PORT}",
                },
                timeout=min(0.3, remaining),
            )

            if r.status_code < 500:
                return

        except requests.RequestException:
            pass

        time.sleep(min(0.05, remaining))


def run_ytdlp(media_url, start_time):
    """
    使用 yt-dlp 获取固定规格的视频媒体 URL。

    优先级：
    1. VP9，1080p 以下
    2. AVC1，1080p 以下
    3. 其他视频编码，1080p 以下

    不选择 4K。
    """

    remaining = remaining_time(start_time)

    ytdlp = shutil.which("yt-dlp")

    if not ytdlp:
        raise RuntimeError("yt-dlp executable not found")

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
        str(max(1, int(remaining))),

        "-f",
        format_selector,

        "-g",
        media_url,
    ]

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=max(0.1, remaining),
    )

    if result.returncode != 0:
        raise RuntimeError(
            "yt-dlp failed: "
            + result.stderr.strip()[:500]
        )

    media_url = result.stdout.strip().splitlines()

    if not media_url:
        raise RuntimeError("yt-dlp returned no media URL")

    return media_url[0]


def download_media(media_url, start_time):
    remaining = remaining_time(start_time)

    session = requests.Session()

    response = session.get(
        media_url,
        proxies={
            "http": f"http://{PROXY_HOST}:{PROXY_PORT}",
            "https": f"http://{PROXY_HOST}:{PROXY_PORT}",
        },
        stream=True,
        timeout=(min(1.0, remaining), min(1.0, remaining)),
    )

    response.raise_for_status()

    ttfb = time.monotonic() - start_time

    downloaded = 0
    download_start = time.monotonic()

    for chunk in response.iter_content(chunk_size=64 * 1024):
        remaining = remaining_time(start_time)

        if not chunk:
            continue

        remaining_bytes = MAX_DOWNLOAD_BYTES - downloaded

        if len(chunk) > remaining_bytes:
            chunk = chunk[:remaining_bytes]

        downloaded += len(chunk)

        if downloaded >= MAX_DOWNLOAD_BYTES:
            break

    download_time = time.monotonic() - download_start

    if downloaded < MAX_DOWNLOAD_BYTES:
        raise RuntimeError(
            f"Only downloaded {downloaded} bytes before timeout"
        )

    throughput_mbps = (
        downloaded * 8 / download_time / 1_000_000
    )

    return {
        "downloaded": downloaded,
        "download_time": download_time,
        "ttfb": ttfb,
        "throughput_mbps": throughput_mbps,
    }


def main():
    print("free-nodes - single YouTube media test")
    print()

    node = load_first_node()

    print(f"Node name: {node['name']}")
    print(f"Protocol:  {node.get('type', 'unknown')}")
    print(f"Maximum total test time: {MAX_TOTAL_SECONDS:.1f} seconds")
    print()

    temp_dir = tempfile.mkdtemp()

    config_path = os.path.join(
        temp_dir,
        "config.yaml"
    )

    build_mihomo_config(
        node,
        config_path
    )

    print(f"Temporary config: {config_path}")
    print("Starting Mihomo...")

    process = subprocess.Popen(
        [
            Mihomo_BIN,
            "-d",
            temp_dir,
            "-f",
            config_path,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    start_time = time.monotonic()

    signal.signal(
        signal.SIGALRM,
        timeout_handler
    )

    signal.setitimer(
        signal.ITIMER_REAL,
        MAX_TOTAL_SECONDS
    )

    try:
        # --------------------------------------------------
        # STEP 1
        # --------------------------------------------------

        print()
        print("STEP 1: YouTube page")

        wait_for_mihomo(start_time)

        remaining = remaining_time(start_time)

        response = requests.get(
            "https://www.youtube.com/",
            proxies={
                "http": f"http://{PROXY_HOST}:{PROXY_PORT}",
                "https": f"http://{PROXY_HOST}:{PROXY_PORT}",
            },
            timeout=remaining,
        )

        page_elapsed = time.monotonic() - start_time

        print(f"HTTP status: {response.status_code}")
        print(f"Page elapsed: {page_elapsed:.3f} seconds")
        print(f"Page bytes: {len(response.content):,}")

        if response.status_code != 200:
            raise RuntimeError(
                f"YouTube returned HTTP {response.status_code}"
            )

        print("YouTube page test: SUCCESS")

        # --------------------------------------------------
        # STEP 2
        # --------------------------------------------------

        print()
        print("STEP 2: yt-dlp media extraction")

        extraction_start = time.monotonic()

        media_url = run_ytdlp(
            TEST_VIDEO_URL,
            start_time
        )

        extraction_elapsed = (
            time.monotonic() - extraction_start
        )

        print(
            f"Extraction elapsed: "
            f"{extraction_elapsed:.3f} seconds"
        )

        print("yt-dlp extraction: SUCCESS")

        # --------------------------------------------------
        # STEP 3
        # --------------------------------------------------

        print()
        print("STEP 3: Real YouTube media download")
        print(
            f"Maximum download: "
            f"{MAX_DOWNLOAD_BYTES:,} bytes"
        )

        result = download_media(
            media_url,
            start_time
        )

        print(
            f"Downloaded: "
            f"{result['downloaded']:,} bytes"
        )

        print(
            f"Download time: "
            f"{result['download_time']:.3f} seconds"
        )

        print(
            f"Media TTFB: "
            f"{result['ttfb']:.3f} seconds"
        )

        print(
            f"Throughput: "
            f"{result['throughput_mbps']:.2f} Mbps"
        )

        print("Media download: SUCCESS")

        # --------------------------------------------------
        # FINAL
        # --------------------------------------------------

        total_elapsed = time.monotonic() - start_time

        print()
        print("FINAL RESULT")
        print(f"Node:             {node['name']}")
        print(f"Protocol:         {node.get('type', 'unknown')}")
        print(
            f"Total test time:  "
            f"{total_elapsed:.3f}s"
        )
        print("YouTube page:     SUCCESS")
        print("yt-dlp:           SUCCESS")
        print("Media download:   SUCCESS")
        print(
            f"Media TTFB:       "
            f"{result['ttfb']:.3f}s"
        )
        print(
            f"Media throughput: "
            f"{result['throughput_mbps']:.2f} Mbps"
        )

    except TestTimeout as e:

        print()
        print("FINAL RESULT")
        print(f"Node:             {node['name']}")
        print(f"Protocol:         {node.get('type', 'unknown')}")
        print("Result:            TIMEOUT")
        print(f"Reason:            {e}")
        print(
            f"Elapsed:           "
            f"{time.monotonic() - start_time:.3f}s"
        )

        sys.exit(1)

    except Exception as e:

        print()
        print("FINAL RESULT")
        print(f"Node:             {node['name']}")
        print(f"Protocol:         {node.get('type', 'unknown')}")
        print("Result:            FAILED")
        print(f"Reason:            {e}")
        print(
            f"Elapsed:           "
            f"{time.monotonic() - start_time:.3f}s"
        )

        sys.exit(1)

    finally:

        signal.setitimer(
            signal.ITIMER_REAL,
            0
        )

        process.terminate()

        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()

        shutil.rmtree(
            temp_dir,
            ignore_errors=True
        )


if __name__ == "__main__":
    main()

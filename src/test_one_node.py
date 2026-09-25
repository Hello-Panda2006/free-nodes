import os
import sys
import time
import shutil
import tempfile
import subprocess
from pathlib import Path

import requests
import yaml


MIHOMO_BIN = "./bin/mihomo"

PROXY_HOST = "127.0.0.1"
PROXY_PORT = 7890

TEST_VIDEO_URL = (
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
)

STAGE_TIMEOUT = 3.0
MAX_DOWNLOAD_BYTES = 1024 * 1024


def proxy_dict():
    return {
        "http": (
            f"http://{PROXY_HOST}:{PROXY_PORT}"
        ),
        "https": (
            f"http://{PROXY_HOST}:{PROXY_PORT}"
        ),
    }


def load_first_node():
    path = Path("data/candidates.yaml")

    if not path.exists():
        raise RuntimeError(
            "data/candidates.yaml not found"
        )

    with path.open(
        "r",
        encoding="utf-8"
    ) as f:
        data = yaml.safe_load(f)

    nodes = data.get("proxies", [])

    if not nodes:
        raise RuntimeError(
            "No proxy nodes found"
        )

    return nodes[0]


def build_mihomo_config(
    node,
    config_path
):
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
            sort_keys=False
        )


def wait_for_mihomo():
    deadline = (
        time.monotonic()
        + STAGE_TIMEOUT
    )

    while time.monotonic() < deadline:

        try:
            response = requests.get(
                "https://www.youtube.com/",
                proxies=proxy_dict(),
                timeout=0.3,
            )

            if response.status_code < 500:
                return

        except requests.RequestException:
            pass

        time.sleep(0.05)

    raise TimeoutError(
        "Mihomo proxy did not become ready"
    )


def test_youtube_page():

    start = time.monotonic()

    response = requests.get(
        "https://www.youtube.com/",
        proxies=proxy_dict(),
        timeout=STAGE_TIMEOUT,
    )

    elapsed = (
        time.monotonic()
        - start
    )

    if response.status_code != 200:
        raise RuntimeError(
            "YouTube returned HTTP "
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

    format_selector = (
        "bestvideo[height<=1080]"
        "[vcodec^=vp9]/"
        "bestvideo[height<=1080]"
        "[vcodec^=avc1]/"
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

    elapsed = (
        time.monotonic()
        - start
    )

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

    ttfb = (
        time.monotonic()
        - start
    )

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
            raise TimeoutError(
                "Media download exceeded "
                f"{STAGE_TIMEOUT:.1f} seconds"
            )

    download_time = (
        time.monotonic()
        - download_start
    )

    if downloaded < MAX_DOWNLOAD_BYTES:
        raise TimeoutError(
            "Only downloaded "
            f"{downloaded:,} bytes before timeout"
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


def worker():

    node = load_first_node()

    print(
        "free-nodes - single YouTube media test"
    )
    print()

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
        f"Maximum download: "
        f"{MAX_DOWNLOAD_BYTES:,} bytes"
    )

    print()

    temp_dir = tempfile.mkdtemp()

    config_path = os.path.join(
        temp_dir,
        "config.yaml"
    )

    process = None

    try:

        build_mihomo_config(
            node,
            config_path
        )

        print(
            f"Temporary config: "
            f"{config_path}"
        )

        print(
            "Starting Mihomo..."
        )

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

        # ------------------------------------------
        # STEP 0
        # ------------------------------------------

        print()
        print(
            "STEP 0: Mihomo startup"
        )

        wait_for_mihomo()

        print(
            "Mihomo proxy is ready: "
            f"{PROXY_HOST}:{PROXY_PORT}"
        )

        # ------------------------------------------
        # STEP 1
        # ------------------------------------------

        print()
        print(
            "STEP 1: YouTube page"
        )

        page = test_youtube_page()

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

        # ------------------------------------------
        # STEP 2
        # ------------------------------------------

        print()
        print(
            "STEP 2: "
            "yt-dlp media extraction"
        )

        media = extract_media_url()

        print(
            f"Extraction elapsed: "
            f"{media['elapsed']:.3f} seconds"
        )

        print(
            "yt-dlp extraction: SUCCESS"
        )

        # ------------------------------------------
        # STEP 3
        # ------------------------------------------

        print()
        print(
            "STEP 3: "
            "Real YouTube media download"
        )

        print(
            f"Maximum download: "
            f"{MAX_DOWNLOAD_BYTES:,} bytes"
        )

        download = download_media(
            media["url"]
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
            f"{download['download_time']:.3f} seconds"
        )

        print(
            f"Throughput: "
            f"{download['throughput_mbps']:.2f} Mbps"
        )

        print(
            "Media download: SUCCESS"
        )

        return 0

    finally:

        if process is not None:

            process.terminate()

            try:
                process.wait(
                    timeout=2
                )
            except subprocess.TimeoutExpired:
                process.kill()

        shutil.rmtree(
            temp_dir,
            ignore_errors=True
        )


def main():

    if len(sys.argv) > 1:
        if sys.argv[1] == "--worker":
            return worker()

    # ----------------------------------------------
    # Parent process
    # ----------------------------------------------

    print(
        "Starting single-node worker..."
    )

    command = [
        sys.executable,
        __file__,
        "--worker"
    ]

    start = time.monotonic()

    process = subprocess.Popen(
        command
    )

    try:

        process.wait(
            timeout=(
                STAGE_TIMEOUT * 3
                + 5
            )
        )

    except subprocess.TimeoutExpired:

        print()
        print(
            "FINAL RESULT"
        )
        print(
            "Result: TIMEOUT"
        )
        print(
            "Reason: Worker exceeded "
            "maximum allowed runtime"
        )

        process.terminate()

        try:
            process.wait(
                timeout=2
            )
        except subprocess.TimeoutExpired:
            process.kill()

        return 1

    elapsed = (
        time.monotonic()
        - start
    )

    print()
    print(
        "Worker finished."
    )

    print(
        f"Total elapsed: "
        f"{elapsed:.3f}s"
    )

    return process.returncode


if __name__ == "__main__":
    sys.exit(main())

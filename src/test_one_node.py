#!/usr/bin/env python3

"""
Test one proxy node against real YouTube media.

流程：
1. 读取 data/candidates.yaml
2. 选择第一个节点
3. 启动 Mihomo
4. 通过 Mihomo 访问 YouTube
5. 使用 yt-dlp 获取真实媒体信息
6. 获取真实 YouTube media URL
7. 通过 Mihomo 实际下载最多 1 MiB
8. 记录下载速度和耗时
"""

from __future__ import annotations

import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests
import yaml
from yt_dlp import YoutubeDL


ROOT = Path(__file__).resolve().parents[1]

CANDIDATES_FILE = ROOT / "data" / "candidates.yaml"
MIHOMO_BINARY = ROOT / "bin" / "mihomo"

MIXED_PORT = 7890

MIHOMO_STARTUP_TIMEOUT = 10
YOUTUBE_TIMEOUT = 10

MAX_DOWNLOAD_BYTES = 1024 * 1024

# 固定测试视频。
# 后续正式测试时我们会准备多个稳定视频并轮换。
TEST_VIDEO_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


USER_AGENT = (
    "Mozilla/5.0 "
    "(Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 "
    "(KHTML, like Gecko) "
    "Chrome/140.0 Safari/537.36"
)


def load_first_node() -> dict:
    """Load the first candidate node."""

    if not CANDIDATES_FILE.exists():
        raise FileNotFoundError(
            f"Candidate file not found: {CANDIDATES_FILE}"
        )

    with CANDIDATES_FILE.open(
        "r",
        encoding="utf-8",
    ) as f:
        document = yaml.safe_load(f)

    if not isinstance(document, dict):
        raise ValueError(
            "candidates.yaml root must be a mapping."
        )

    proxies = document.get("proxies")

    if not isinstance(proxies, list) or not proxies:
        raise ValueError(
            "No proxy nodes found."
        )

    node = proxies[0]

    if not isinstance(node, dict):
        raise ValueError(
            "First proxy is not a mapping."
        )

    return node


def wait_for_port(
    host: str,
    port: int,
    timeout: float,
) -> bool:
    """Wait until a TCP port accepts connections."""

    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        try:
            with socket.create_connection(
                (host, port),
                timeout=0.5,
            ):
                return True
        except OSError:
            time.sleep(0.2)

    return False


def main() -> int:
    print("=" * 70)
    print("free-nodes - single YouTube media test")
    print("=" * 70)

    # ------------------------------------------------------------
    # Load node
    # ------------------------------------------------------------

    try:
        node = load_first_node()
    except Exception as exc:
        print(f"ERROR loading node: {exc}")
        return 1

    node_name = node.get("name", "Unnamed")
    node_type = node.get("type", "unknown")

    print(f"Node name: {node_name}")
    print(f"Protocol:  {node_type}")

    # ------------------------------------------------------------
    # Check Mihomo
    # ------------------------------------------------------------

    if not MIHOMO_BINARY.exists():
        print(
            f"ERROR: Mihomo binary not found: "
            f"{MIHOMO_BINARY}"
        )
        return 1

    # ------------------------------------------------------------
    # Build temporary Mihomo config
    # ------------------------------------------------------------

    config = {
        "mixed-port": MIXED_PORT,
        "allow-lan": False,
        "mode": "rule",
        "log-level": "warning",
        "ipv6": False,
        "proxies": [
            node,
        ],
        "proxy-groups": [
            {
                "name": "TEST",
                "type": "select",
                "proxies": [
                    node_name,
                ],
            }
        ],
        "rules": [
            "MATCH,TEST",
        ],
    }

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        config_file = temp_path / "config.yaml"

        with config_file.open(
            "w",
            encoding="utf-8",
        ) as f:
            yaml.safe_dump(
                config,
                f,
                allow_unicode=True,
                sort_keys=False,
                default_flow_style=False,
            )

        print(f"Temporary config: {config_file}")

        # --------------------------------------------------------
        # Start Mihomo
        # --------------------------------------------------------

        print("\nStarting Mihomo...")

        process = subprocess.Popen(
            [
                str(MIHOMO_BINARY),
                "-d",
                str(temp_path),
                "-f",
                str(config_file),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        try:
            if not wait_for_port(
                "127.0.0.1",
                MIXED_PORT,
                MIHOMO_STARTUP_TIMEOUT,
            ):
                print(
                    "ERROR: Mihomo proxy port "
                    "did not start."
                )

                if process.poll() is not None:
                    print(
                        "Mihomo exited with code "
                        f"{process.returncode}"
                    )

                return 1

            print(
                "Mihomo proxy is ready: "
                f"127.0.0.1:{MIXED_PORT}"
            )

            # ----------------------------------------------------
            # Proxy configuration
            # ----------------------------------------------------

            proxy_url = (
                f"http://127.0.0.1:{MIXED_PORT}"
            )

            proxies = {
                "http": proxy_url,
                "https": proxy_url,
            }

            # ----------------------------------------------------
            # Step 1: YouTube page
            # ----------------------------------------------------

            print("\n" + "-" * 70)
            print("STEP 1: YouTube page")
            print("-" * 70)

            start = time.monotonic()

            try:
                response = requests.get(
                    "https://www.youtube.com/",
                    proxies=proxies,
                    timeout=YOUTUBE_TIMEOUT,
                    headers={
                        "User-Agent": USER_AGENT
                    },
                )

                page_elapsed = (
                    time.monotonic() - start
                )

                print(
                    f"HTTP status: {response.status_code}"
                )
                print(
                    f"Page elapsed: "
                    f"{page_elapsed:.3f} seconds"
                )
                print(
                    f"Page bytes: "
                    f"{len(response.content):,}"
                )

                if not response.ok:
                    print(
                        "YouTube page test: FAILED"
                    )
                    return 1

                print(
                    "YouTube page test: SUCCESS"
                )

            except requests.RequestException as exc:
                print(
                    f"YouTube page test: FAILED"
                )
                print(f"Error: {exc}")
                return 1

            # ----------------------------------------------------
            # Step 2: yt-dlp
            # ----------------------------------------------------

            print("\n" + "-" * 70)
            print("STEP 2: yt-dlp media extraction")
            print("-" * 70)

            ydl_opts = {
                "quiet": True,
                "no_warnings": True,
                "skip_download": True,
                "proxy": proxy_url,
                "socket_timeout": 10,
                "nocheckcertificate": False,
            }

            extraction_start = time.monotonic()

            try:
                with YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(
                        TEST_VIDEO_URL,
                        download=False,
                    )

            except Exception as exc:
                extraction_elapsed = (
                    time.monotonic()
                    - extraction_start
                )

                print(
                    "yt-dlp extraction: FAILED"
                )
                print(
                    f"Extraction elapsed: "
                    f"{extraction_elapsed:.3f} seconds"
                )
                print(f"Error: {exc}")

                return 1

            extraction_elapsed = (
                time.monotonic()
                - extraction_start
            )

            print(
                "yt-dlp extraction: SUCCESS"
            )
            print(
                f"Extraction elapsed: "
                f"{extraction_elapsed:.3f} seconds"
            )
            print(
                f"Title: "
                f"{info.get('title', 'Unknown')}"
            )

            formats = info.get("formats", [])

            # ----------------------------------------------------
            # Select a progressive HTTP format when possible.
            # Otherwise use the first usable media format.
            # ----------------------------------------------------

            media_format = None

            progressive = [
                fmt
                for fmt in formats
                if fmt.get("url")
                and fmt.get("vcodec") != "none"
                and fmt.get("acodec") != "none"
            ]

            if progressive:
                media_format = max(
                    progressive,
                    key=lambda fmt: (
                        fmt.get("height") or 0
                    ),
                )
            else:
                usable = [
                    fmt
                    for fmt in formats
                    if fmt.get("url")
                ]

                if usable:
                    media_format = usable[-1]

            if not media_format:
                print(
                    "ERROR: no usable media URL found."
                )
                return 1

            media_url = media_format["url"]

            print(
                f"Format ID: "
                f"{media_format.get('format_id')}"
            )
            print(
                f"Resolution: "
                f"{media_format.get('width')}x"
                f"{media_format.get('height')}"
            )
            print(
                f"Video codec: "
                f"{media_format.get('vcodec')}"
            )
            print(
                f"Audio codec: "
                f"{media_format.get('acodec')}"
            )

            # ----------------------------------------------------
            # Step 3: Real media download
            # ----------------------------------------------------

            print("\n" + "-" * 70)
            print(
                "STEP 3: Real YouTube media download"
            )
            print("-" * 70)

            print(
                f"Maximum download: "
                f"{MAX_DOWNLOAD_BYTES:,} bytes"
            )

            download_start = time.monotonic()

            downloaded = 0
            first_data_time = None

            try:
                with requests.get(
                    media_url,
                    proxies=proxies,
                    headers={
                        "User-Agent": USER_AGENT
                    },
                    stream=True,
                    timeout=YOUTUBE_TIMEOUT,
                ) as media_response:

                    media_response.raise_for_status()

                    for chunk in media_response.iter_content(
                        chunk_size=64 * 1024
                    ):
                        if not chunk:
                            continue

                        if first_data_time is None:
                            first_data_time = (
                                time.monotonic()
                            )

                        remaining = (
                            MAX_DOWNLOAD_BYTES
                            - downloaded
                        )

                        downloaded += min(
                            len(chunk),
                            remaining,
                        )

                        if downloaded >= MAX_DOWNLOAD_BYTES:
                            break

            except requests.RequestException as exc:
                print(
                    "Media download: FAILED"
                )
                print(f"Error: {exc}")
                return 1

            download_elapsed = (
                time.monotonic()
                - download_start
            )

            if first_data_time is not None:
                ttfb = (
                    first_data_time
                    - download_start
                )
            else:
                ttfb = None

            if download_elapsed > 0:
                throughput_mbps = (
                    downloaded
                    * 8
                    / download_elapsed
                    / 1_000_000
                )
            else:
                throughput_mbps = 0

            print(
                f"Downloaded: "
                f"{downloaded:,} bytes"
            )

            print(
                f"Download time: "
                f"{download_elapsed:.3f} seconds"
            )

            if ttfb is not None:
                print(
                    f"Media TTFB: "
                    f"{ttfb:.3f} seconds"
                )
            else:
                print(
                    "Media TTFB: unavailable"
                )

            print(
                f"Throughput: "
                f"{throughput_mbps:.2f} Mbps"
            )

            if downloaded >= MAX_DOWNLOAD_BYTES:
                print(
                    "Media download: SUCCESS"
                )
            else:
                print(
                    "Media download: INCOMPLETE"
                )
                return 1

            # ----------------------------------------------------
            # Final result
            # ----------------------------------------------------

            print("\n" + "=" * 70)
            print("FINAL RESULT")
            print("=" * 70)

            print(f"Node:             {node_name}")
            print(f"Protocol:         {node_type}")
            print(
                f"YouTube page:     SUCCESS "
                f"({page_elapsed:.3f}s)"
            )
            print(
                f"yt-dlp:           SUCCESS "
                f"({extraction_elapsed:.3f}s)"
            )
            print(
                f"Media download:   SUCCESS"
            )
            print(
                f"Media TTFB:       "
                f"{ttfb:.3f}s"
                if ttfb is not None
                else "Media TTFB:       unavailable"
            )
            print(
                f"Media throughput: "
                f"{throughput_mbps:.2f} Mbps"
            )

            print("=" * 70)

            return 0

        finally:
            print("\nStopping Mihomo...")

            process.terminate()

            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

            print("Mihomo stopped.")


if __name__ == "__main__":
    sys.exit(main())

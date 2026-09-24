#!/usr/bin/env python3

"""
Test one proxy node through Mihomo.

流程：
1. 读取 data/candidates.yaml
2. 选择第一个节点
3. 生成临时 Mihomo 配置
4. 启动 Mihomo
5. 等待本地代理端口就绪
6. 通过 Mihomo 访问 YouTube
7. 输出测试结果

这一阶段只测试：
节点 -> Mihomo -> YouTube

暂时不：
- yt-dlp
- 实际媒体下载
- 并发测试
- 节点评分
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


ROOT = Path(__file__).resolve().parents[1]

CANDIDATES_FILE = ROOT / "data" / "candidates.yaml"
MIHOMO_BINARY = ROOT / "bin" / "mihomo"

MIXED_PORT = 7890

MIHOMO_STARTUP_TIMEOUT = 10
YOUTUBE_TIMEOUT = 10

TEST_URL = "https://www.youtube.com/"


def load_first_node() -> dict:
    """Load the first candidate node."""

    if not CANDIDATES_FILE.exists():
        raise FileNotFoundError(
            f"Candidate file not found: {CANDIDATES_FILE}"
        )

    with CANDIDATES_FILE.open("r", encoding="utf-8") as f:
        document = yaml.safe_load(f)

    if not isinstance(document, dict):
        raise ValueError("candidates.yaml root must be a mapping.")

    proxies = document.get("proxies")

    if not isinstance(proxies, list) or not proxies:
        raise ValueError("No proxy nodes found.")

    node = proxies[0]

    if not isinstance(node, dict):
        raise ValueError("First proxy is not a mapping.")

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
    print("free-nodes - single node test")
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
        "log-level": "info",
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
                print("ERROR: Mihomo proxy port did not start.")

                if process.poll() is not None:
                    print(
                        f"Mihomo exited with code "
                        f"{process.returncode}"
                    )

                return 1

            print(
                f"Mihomo proxy is ready: "
                f"127.0.0.1:{MIXED_PORT}"
            )

            # ----------------------------------------------------
            # Test YouTube
            # ----------------------------------------------------

            proxy_url = (
                f"http://127.0.0.1:{MIXED_PORT}"
            )

            proxies = {
                "http": proxy_url,
                "https": proxy_url,
            }

            print(f"\nTesting: {TEST_URL}")
            print("Connecting through Mihomo...")

            start = time.monotonic()

            try:
                response = requests.get(
                    TEST_URL,
                    proxies=proxies,
                    timeout=YOUTUBE_TIMEOUT,
                    headers={
                        "User-Agent": (
                            "Mozilla/5.0 "
                            "(Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 "
                            "(KHTML, like Gecko) "
                            "Chrome/140.0 Safari/537.36"
                        )
                    },
                )

                elapsed = time.monotonic() - start

                print("\n" + "=" * 70)
                print("RESULT")
                print("=" * 70)
                print(f"HTTP status: {response.status_code}")
                print(f"Elapsed:     {elapsed:.3f} seconds")
                print(
                    f"Response:    "
                    f"{len(response.content):,} bytes"
                )

                if response.ok:
                    print("YouTube test: SUCCESS")
                    return 0

                print("YouTube test: FAILED")
                return 1

            except requests.RequestException as exc:
                elapsed = time.monotonic() - start

                print("\n" + "=" * 70)
                print("RESULT")
                print("=" * 70)
                print("YouTube test: FAILED")
                print(f"Elapsed:      {elapsed:.3f} seconds")
                print(f"Error:        {exc}")

                return 1

        finally:
            # ----------------------------------------------------
            # Stop Mihomo
            # ----------------------------------------------------

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

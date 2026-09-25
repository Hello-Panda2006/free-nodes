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

CANDIDATES_FILE = os.path.join(
    ROOT,
    "data",
    "candidates.yaml",
)

SETTINGS_FILE = os.path.join(
    ROOT,
    "config",
    "settings.yaml",
)

RESULT_FILE = os.path.join(
    ROOT,
    "data",
    "test-results.json",
)

MIHOMO_BIN = os.path.join(
    ROOT,
    "bin",
    "mihomo",
)

YOUTUBE_URL = (
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
)

BASE_PROXY_PORT = 20000
BASE_CONTROLLER_PORT = 21000

HTTP_TIMEOUT = 3.0
STARTUP_TIMEOUT = 5.0
MAX_DOWNLOAD_BYTES = 1024 * 1024


def load_yaml(path):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def safe_float(value):
    if value is None:
        return None

    return round(float(value), 4)


def build_mihomo_config(
    proxy,
    proxy_port,
    controller_port,
):
    """
    Create an isolated Mihomo configuration for one node.
    """

    test_proxy_name = "__TEST_PROXY__"

    proxy_config = dict(proxy)

    proxy_config["name"] = test_proxy_name

    config = {
        "mixed-port": proxy_port,
        "allow-lan": False,

        "external-controller": (
            f"127.0.0.1:{controller_port}"
        ),

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
                ],
            }
        ],

        "rules": [
            "MATCH,__TEST_GROUP__"
        ],
    }

    return config


def wait_for_mihomo(
    controller_port,
    process,
):
    """
    Only test whether Mihomo itself is ready.

    This does NOT test whether the candidate node can reach
    YouTube.
    """

    url = (
        f"http://127.0.0.1:"
        f"{controller_port}/version"
    )

    deadline = time.time() + STARTUP_TIMEOUT

    while time.time() < deadline:

        if process.poll() is not None:
            return False, "mihomo_process_exit"

        try:
            response = requests.get(
                url,
                timeout=0.5,
            )

            if response.status_code == 200:
                return True, None

        except requests.RequestException:
            pass

        time.sleep(0.05)

    return False, "mihomo_startup_timeout"


def test_youtube_page(proxy_port):
    """
    Test YouTube page/control-plane connectivity.

    Maximum stage time:
        HTTP_TIMEOUT

    Returns timing and response information.
    """

    proxy = (
        f"http://127.0.0.1:{proxy_port}"
    )

    proxies = {
        "http": proxy,
        "https": proxy,
    }

    start = time.time()

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
                    "Mozilla/5.0 "
                    "(Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 "
                    "(KHTML, like Gecko) "
                    "Chrome/153.0.0.0 "
                    "Safari/537.36"
                )
            },
        ) as response:

            ttfb = time.time() - start

            if response.status_code != 200:

                return {
                    "success": False,
                    "status_code": (
                        response.status_code
                    ),
                    "ttfb": safe_float(ttfb),
                    "elapsed": safe_float(
                        time.time() - start
                    ),
                    "bytes": 0,
                    "error": (
                        f"http_status_"
                        f"{response.status_code}"
                    ),
                }

            for chunk in response.iter_content(
                chunk_size=65536
            ):

                elapsed = (
                    time.time() - start
                )

                if elapsed >= HTTP_TIMEOUT:
                    break

                if not chunk:
                    continue

                total_bytes += len(chunk)

                if (
                    total_bytes
                    >= 1024 * 1024
                ):
                    break

            elapsed = (
                time.time() - start
            )

            return {
                "success": True,
                "status_code": (
                    response.status_code
                ),
                "ttfb": safe_float(ttfb),
                "elapsed": safe_float(
                    elapsed
                ),
                "bytes": total_bytes,
                "error": None,
            }

    except Exception as e:

        return {
            "success": False,
            "status_code": None,
            "ttfb": safe_float(ttfb),
            "elapsed": safe_float(
                time.time() - start
            ),
            "bytes": total_bytes,
            "error": (
                f"{type(e).__name__}: "
                f"{str(e)[:500]}"
            ),
        }


def extract_media_url(proxy_port):
    """
    Use yt-dlp through the candidate proxy.

    The --proxy argument is explicitly specified so that
    yt-dlp cannot accidentally perform the extraction directly.
    """

    proxy = (
        f"http://127.0.0.1:{proxy_port}"
    )

    format_selector = (
        "bestvideo[height<=1080]"
        "[vcodec^=vp9]/"
        "bestvideo[height<=1080]"
        "[vcodec^=avc1]/"
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

    start = time.time()

    try:

        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=HTTP_TIMEOUT,
            env=env,
        )

        elapsed = (
            time.time() - start
        )

        if completed.returncode != 0:

            return {
                "success": False,
                "elapsed": safe_float(
                    elapsed
                ),
                "media_url": None,
                "error": (
                    completed.stderr[-1000:]
                    or "yt_dlp_failed"
                ),
            }

        media_urls = (
            completed.stdout
            .strip()
            .splitlines()
        )

        if not media_urls:

            return {
                "success": False,
                "elapsed": safe_float(
                    elapsed
                ),
                "media_url": None,
                "error": "empty_media_url",
            }

        return {
            "success": True,
            "elapsed": safe_float(
                elapsed
            ),
            "media_url": media_urls[0],
            "error": None,
        }

    except subprocess.TimeoutExpired:

        return {
            "success": False,
            "elapsed": safe_float(
                time.time() - start
            ),
            "media_url": None,
            "error": "yt_dlp_timeout",
        }

    except Exception as e:

        return {
            "success": False,
            "elapsed": safe_float(
                time.time() - start
            ),
            "media_url": None,
            "error": (
                f"{type(e).__name__}: "
                f"{str(e)[:500]}"
            ),
        }


def download_media(
    media_url,
    proxy_port,
):
    """
    Download actual YouTube media.

    Maximum:
        HTTP_TIMEOUT seconds
        MAX_DOWNLOAD_BYTES bytes

    If the node downloads less than 1 MiB within
    the 3-second window, the result is still retained
    as a valid speed measurement.
    """

    proxy = (
        f"http://127.0.0.1:{proxy_port}"
    )

    proxies = {
        "http": proxy,
        "https": proxy,
    }

    start = time.time()

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
                    "Mozilla/5.0 "
                    "(Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 "
                    "(KHTML, like Gecko) "
                    "Chrome/153.0.0.0 "
                    "Safari/537.36"
                )
            },
        ) as response:

            ttfb = (
                time.time() - start
            )

            if response.status_code not in (
                200,
                206,
            ):

                return {
                    "success": False,
                    "status_code": (
                        response.status_code
                    ),
                    "status": "http_error",
                    "bytes": 0,
                    "elapsed": safe_float(
                        time.time() - start
                    ),
                    "ttfb": safe_float(ttfb),
                    "throughput_mbps": 0,
                    "error": (
                        f"http_status_"
                        f"{response.status_code}"
                    ),
                }

            for chunk in response.iter_content(
                chunk_size=65536
            ):

                elapsed = (
                    time.time() - start
                )

                if elapsed >= HTTP_TIMEOUT:
                    break

                if not chunk:
                    continue

                remaining = (
                    MAX_DOWNLOAD_BYTES
                    - total_bytes
                )

                if remaining <= 0:
                    break

                if len(chunk) > remaining:
                    chunk = chunk[:remaining]

                total_bytes += len(chunk)

                if (
                    total_bytes
                    >= MAX_DOWNLOAD_BYTES
                ):
                    break

            elapsed = (
                time.time() - start
            )

            if (
                total_bytes
                >= MAX_DOWNLOAD_BYTES
            ):
                status = "completed_1mb"
            else:
                status = "time_limit"

            throughput = (
                total_bytes
                * 8
                / elapsed
                / 1_000_000
                if elapsed > 0
                else 0
            )

            return {
                "success": True,
                "status_code": (
                    response.status_code
                ),
                "status": status,
                "bytes": total_bytes,
                "elapsed": safe_float(
                    elapsed
                ),
                "ttfb": safe_float(ttfb),
                "throughput_mbps": safe_float(
                    throughput
                ),
                "error": None,
            }

    except Exception as e:

        elapsed = (
            time.time() - start
        )

        throughput = (
            total_bytes
            * 8
            / elapsed
            / 1_000_000
            if elapsed > 0
            else 0
        )

        return {
            "success": False,
            "status_code": None,
            "status": "request_error",
            "bytes": total_bytes,
            "elapsed": safe_float(
                elapsed
            ),
            "ttfb": safe_float(ttfb),
            "throughput_mbps": safe_float(
                throughput
            ),
            "error": (
                f"{type(e).__name__}: "
                f"{str(e)[:1000]}"
            ),
        }


def test_node(
    index,
    proxy,
):
    """
    Complete test of one candidate node.
    """

    original_name = proxy.get(
        "name",
        f"node-{index}",
    )

    protocol = proxy.get(
        "type",
        "unknown",
    )

    proxy_port = (
        BASE_PROXY_PORT + index
    )

    controller_port = (
        BASE_CONTROLLER_PORT + index
    )

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
        "f

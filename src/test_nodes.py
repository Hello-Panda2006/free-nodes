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

CANDIDATES_FILE = os.path.join(ROOT, "data", "candidates.yaml")
SETTINGS_FILE = os.path.join(ROOT, "config", "settings.yaml")
RESULT_FILE = os.path.join(ROOT, "data", "test-results.json")
MIHOMO_BIN = os.path.join(ROOT, "bin", "mihomo")

YOUTUBE_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

BASE_PROXY_PORT = 20000
BASE_CONTROLLER_PORT = 21000

DEFAULT_STAGE_TIMEOUT = 3.0
DEFAULT_STARTUP_TIMEOUT = 5.0
DEFAULT_MAX_DOWNLOAD_BYTES = 1024 * 1024
DEFAULT_CONCURRENCY = 20


def load_yaml(path):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def safe_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def build_mihomo_config(node, proxy_port, controller_port):
    test_node = dict(node)
    test_node["name"] = "__TEST_PROXY__"

    return {
        "mixed-port": proxy_port,
        "allow-lan": False,
        "mode": "rule",
        "log-level": "info",
        "external-controller": f"127.0.0.1:{controller_port}",
        "proxies": [
            test_node
        ],
        "proxy-groups": [
            {
                "name": "TEST",
                "type": "select",
                "proxies": [
                    "__TEST_PROXY__"
                ]
            }
        ],
        "rules": [
            "MATCH,TEST"
        ]
    }


def wait_for_mihomo(controller_port, timeout):
    url = f"http://127.0.0.1:{controller_port}/version"
    start = time.monotonic()

    while time.monotonic() - start < timeout:
        try:
            response = requests.get(
                url,
                timeout=0.5
            )

            if response.status_code == 200:
                return True, None

        except requests.RequestException:
            pass

        time.sleep(0.05)

    return False, "mihomo_startup_timeout"


def start_mihomo(config_path, log_path):
    log_file = open(
        log_path,
        "w",
        encoding="utf-8"
    )

    process = subprocess.Popen(
        [
            MIHOMO_BIN,
            "-f",
            config_path
        ],
        stdout=log_file,
        stderr=subprocess.STDOUT
    )

    return process, log_file


def stop_process(process, log_file):
    if process is not None:
        if process.poll() is None:
            process.terminate()

            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()

                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass

    if log_file is not None:
        try:
            log_file.close()
        except Exception:
            pass


def read_log_tail(log_path, max_lines=40):
    try:
        with open(
            log_path,
            "r",
            encoding="utf-8",
            errors="replace"
        ) as f:
            lines = f.readlines()

        return "".join(lines[-max_lines:]).strip()

    except Exception as exc:
        return f"log_read_error: {type(exc).__name__}: {exc}"


def test_youtube_page(proxy_port, stage_timeout):
    proxy = f"http://127.0.0.1:{proxy_port}"

    result = {
        "success": False,
        "status_code": None,
        "elapsed": None,
        "ttfb": None,
        "bytes": 0,
        "error": None
    }

    start = time.monotonic()

    try:
        response = requests.get(
            YOUTUBE_URL,
            proxies={
                "http": proxy,
                "https": proxy
            },
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 "
                    "(KHTML, like Gecko) "
                    "Chrome/153.0 Safari/537.36"
                )
            },
            stream=True,
            timeout=(2, 1)
        )

        result["status_code"] = response.status_code

        first_byte_time = None

        for chunk in response.iter_content(
            chunk_size=65536
        ):
            now = time.monotonic()

            if first_byte_time is None and chunk:
                first_byte_time = now
                result["ttfb"] = now - start

            if chunk:
                result["bytes"] += len(chunk)

            if now - start >= stage_timeout:
                break

        result["elapsed"] = time.monotonic() - start

        if response.status_code == 200:
            result["success"] = True
        else:
            result["error"] = (
                f"http_status_{response.status_code}"
            )

        response.close()

    except requests.RequestException as exc:
        result["elapsed"] = time.monotonic() - start
        result["error"] = (
            f"{type(exc).__name__}: {exc}"
        )

    except Exception as exc:
        result["elapsed"] = time.monotonic() - start
        result["error"] = (
            f"{type(exc).__name__}: {exc}"
        )

    return result


def extract_media_url(proxy_port, stage_timeout):
    proxy = f"http://127.0.0.1:{proxy_port}"

    command = [
        "yt-dlp",
        "--proxy",
        proxy,
        "--no-warnings",
        "--quiet",
        "--skip-download",
        "-f",
        (
            "bestvideo[height<=1080][vcodec^=vp9]/"
            "bestvideo[height<=1080][vcodec^=avc1]/"
            "bestvideo[height<=1080]"
        ),
        "-g",
        YOUTUBE_URL
    ]

    env = os.environ.copy()

    env["HTTP_PROXY"] = proxy
    env["HTTPS_PROXY"] = proxy
    env["http_proxy"] = proxy
    env["https_proxy"] = proxy

    start = time.monotonic()

    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=stage_timeout,
            env=env
        )

        elapsed = time.monotonic() - start

        if completed.returncode != 0:
            error = completed.stderr.strip()

            if not error:
                error = (
                    f"yt_dlp_exit_code_"
                    f"{completed.returncode}"
                )

            return {
                "success": False,
                "elapsed": elapsed,
                "url": None,
                "error": error
            }

        lines = [
            line.strip()
            for line in completed.stdout.splitlines()
            if line.strip()
        ]

        if not lines:
            return {
                "success": False,
                "elapsed": elapsed,
                "url": None,
                "error": "yt_dlp_empty_output"
            }

        return {
            "success": True,
            "elapsed": elapsed,
            "url": lines[0],
            "error": None
        }

    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "elapsed": time.monotonic() - start,
            "url": None,
            "error": "yt_dlp_timeout"
        }

    except Exception as exc:
        return {
            "success": False,
            "elapsed": time.monotonic() - start,
            "url": None,
            "error": (
                f"{type(exc).__name__}: {exc}"
            )
        }


def download_media(
    media_url,
    proxy_port,
    stage_timeout,
    max_bytes
):
    proxy = f"http://127.0.0.1:{proxy_port}"

    result = {
        "success": False,
        "status": None,
        "bytes": 0,
        "elapsed": None,
        "ttfb": None,
        "throughput_mbps": 0.0,
        "error": None
    }

    start = time.monotonic()

    try:
        response = requests.get(
            media_url,
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

        first_byte_time = None

        for chunk in response.iter_content(

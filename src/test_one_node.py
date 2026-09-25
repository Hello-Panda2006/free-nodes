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

CONTROLLER_HOST = "127.0.0.1"
CONTROLLER_PORT = 9090

TEST_VIDEO_URL = (
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
)

# 每个网络测试阶段的最大时间
STAGE_TIMEOUT = 3.0

# Mihomo 本地进程启动时间不属于节点测试时间
MIHOMO_STARTUP_TIMEOUT = 5.0

# 实际媒体下载量
MAX_DOWNLOAD_BYTES = 1024 * 1024


def log(message=""):
    """
    实时输出，避免 GitHub Actions 日志因为 stdout 缓冲
    导致父进程/子进程输出顺序混乱。
    """
    print(message, flush=True)


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

        # 用 Controller 判断 Mihomo 是否真正启动
        "external-controller": (
            f"{CONTROLLER_HOST}:{CONTROLLER_PORT}"
        ),

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
    """
    只检查 Mihomo 本地 Controller。

    注意：
    这里不访问 YouTube。
    节点连接速度应该由后面的测试阶段负责。
    """

    deadline = (
        time.monotonic()
        + MIHOMO_STARTUP_TIMEOUT
    )

    controller_url = (
        f"http://{CONTROLLER_HOST}:"
        f"{CONTROLLER_PORT}/version"
    )

    while time.monotonic() < deadline:

        try:
            response = requests.get(
                controller_url,
                timeout=0.3,
            )

            if response.status_code == 200:
                return

        except requests.RequestException:
            pass

        time.sleep(0.05)

    raise TimeoutError(
        "Mihomo controller did not "
        "become ready within "
        f"{MIHOMO_STARTUP_TIMEOUT:.1f} seconds"
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

    # 只选择 1080p 及以下的视频流。
    #
    # 优先级：
    # 1. VP9
    # 2. AVC1
    # 3. 其他编码
    #
    # 不选择 4K，避免节点之间因为格式差异
    # 造成测速结果不可比较。
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
        error = result.stderr.strip()

        if not error:
            error = (
                f"yt-dlp exited with "
                f"code {result.returncode}"
            )

        raise RuntimeError(
            "yt-dlp failed: "
            + error[:500]
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

    # requests 返回响应头之后，
    # 这里作为媒体请求的首响应时间。
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

        # 达到 1 MiB，立即停止。
        if downloaded >= MAX_DOWNLOAD_BYTES:
            break

        # 下载阶段自己的 3 秒限制。
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
            f"{downloaded:,} bytes before "
            "timeout"
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

    log(
        "free-nodes - single YouTube media test"
    )

    log()

    log(
        f"Node name: {node['name']}"
    )

    log(
        f"Protocol:  "
        f"{node.get('type', 'unknown')}"
    )

    log(
        f"Stage timeout: "
        f"{STAGE_TIMEOUT:.1f} seconds"
    )

    log(
        f"Mihomo startup timeout: "
        f"{MIHOMO_STARTUP_TIMEOUT:.1f} seconds"
    )

    log(
        f"Maximum download: "
        f"{MAX_DOWNLOAD_BYTES:,} bytes"
    )

    log()

    temp_dir = tempfile.mkdtemp()

    config_path = os.path.join(
        temp_dir,
        "config.yaml"
    )

    process = None

    total_start = time.monotonic()

    try:

        build_mihomo_config(
            node,
            config_path
        )

        log(
            f"Temporary config: "
            f"{config_path}"
        )

        log(
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

        log()
        log(
            "STEP 0: Mihomo startup"
        )

        startup_start = time.monotonic()

        wait_for_mihomo()

        startup_elapsed = (
            time.monotonic()
            - startup_start
        )

        log(
            "Mihomo controller is ready: "
            f"{CONTROLLER_HOST}:"
            f"{CONTROLLER_PORT}"
        )

        log(
            "Mihomo proxy is ready: "
            f"{PROXY_HOST}:{PROXY_PORT}"
        )

        log(
            f"Mihomo startup time: "
            f"{startup_elapsed:.3f}s"
        )

        # ------------------------------------------
        # STEP 1
        # ------------------------------------------

        log()
        log(
            "STEP 1: YouTube page"
        )

        page = test_youtube_page()

        log(
            f"HTTP status: "
            f"{page['status_code']}"
        )

        log(
            f"Page elapsed: "
            f"{page['elapsed']:.3f} seconds"
        )

        log(
            f"Page bytes: "
            f"{page['bytes']:,}"
        )

        log(
            "YouTube page test: SUCCESS"
        )

        # ------------------------------------------
        # STEP 2
        # ------------------------------------------

        log()
        log(
            "STEP 2: "
            "yt-dlp media extraction"
        )

        media = extract_media_url()

        log(
            f"Extraction elapsed: "
            f"{media['elapsed']:.3f} seconds"
        )

        log(
            "yt-dlp extraction: SUCCESS"
        )

        # ------------------------------------------
        # STEP 3
        # ------------------------------------------

        log()
        log(
            "STEP 3: "
            "Real YouTube media download"
        )

        log(
            f"Maximum download: "
            f"{MAX_DOWNLOAD_BYTES:,} bytes"
        )

        download = download_media(
            media["url"]
        )

        log(
            f"Downloaded: "
            f"{download['downloaded']:,} bytes"
        )

        log(
            f"Media TTFB: "
            f"{download['ttfb']:.3f} seconds"
        )

        log(
            f"Download time: "
            f"{download['download_time']:.3f} seconds"
        )

        log(
            f"Throughput: "
            f"{download['throughput_mbps']:.2f} Mbps"
        )

        log(
            "Media download: SUCCESS"
        )

        # ------------------------------------------
        # FINAL
        # ------------------------------------------

        total_elapsed = (
            time.monotonic()
            - total_start
        )

        log()
        log(
            "FINAL RESULT"
        )

        log(
            f"Node:             "
            f"{node['name']}"
        )

        log(
            f"Protocol:         "
            f"{node.get('type', 'unknown')}"
        )

        log(
            f"Mihomo startup:   "
            f"{startup_elapsed:.3f}s"
        )

        log(
            f"YouTube page:     "
            f"SUCCESS "
            f"({page['elapsed']:.3f}s)"
        )

        log(
            f"yt-dlp:           "
            f"SUCCESS "
            f"({media['elapsed']:.3f}s)"
        )

        log(
            "Media download:   SUCCESS"
        )

        log(
            f"Media TTFB:       "
            f"{download['ttfb']:.3f}s"
        )

        log(
            f"Media throughput: "
            f"{download['throughput_mbps']:.2f} Mbps"
        )

        log(
            f"Total test time:  "
            f"{total_elapsed:.3f}s"
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

    # ==========================================
    # Worker process
    # ==========================================

    if len(sys.argv) > 1:

        if sys.argv[1] == "--worker":

            try:
                return worker()

            except Exception as e:

                log()
                log(
                    "FINAL RESULT"
                )

                log(
                    "Result:            FAILED"
                )

                log(
                    f"Reason:            {e}"
                )

                return 1

    # ==========================================
    # Parent process
    # ==========================================

    log(
        "Starting single-node worker..."
    )

    command = [
        sys.executable,
        "-u",
        __file__,
        "--worker",
    ]

    start = time.monotonic()

    process = subprocess.Popen(
        command
    )

    try:

        # 5 秒 Mihomo 启动
        # +
        # 3 个测试阶段各 3 秒
        # +
        # 额外安全余量
        maximum_runtime = (
            MIHOMO_STARTUP_TIMEOUT
            + STAGE_TIMEOUT * 3
            + 5
        )

        process.wait(
            timeout=maximum_runtime
        )

    except subprocess.TimeoutExpired:

        log()
        log(
            "FINAL RESULT"
        )

        log(
            "Result:            TIMEOUT"
        )

        log(
            "Reason:            "
            "Worker exceeded maximum "
            "allowed runtime"
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

    log()
    log(
        "Worker finished."
    )

    log(
        f"Total elapsed: "
        f"{elapsed:.3f}s"
    )

    return process.returncode


if __name__ == "__main__":
    sys.exit(main())

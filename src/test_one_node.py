import json
import os
import subprocess
import tempfile
import time
import multiprocessing
from pathlib import Path

import requests
import yaml

# ============================================================

# Basic configuration

# ============================================================

MIHOMO_BIN = "./bin/mihomo"

MIHOMO_PROXY = "127.0.0.1:7890"
MIHOMO_CONTROLLER = "127.0.0.1:9090"

TEST_VIDEO_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

STAGE_TIMEOUT = 3.0
MIHOMO_STARTUP_TIMEOUT = 5.0

MAX_DOWNLOAD_BYTES = 1024 * 1024  # 1 MiB

CANDIDATE_FILE = "data/candidates.yaml"

# ============================================================

# Utility

# ============================================================

def load_first_node():
with open(CANDIDATE_FILE, "r", encoding="utf-8") as f:
data = yaml.safe_load(f)

```
proxies = data.get("proxies", [])

if not proxies:
    raise RuntimeError("No proxy nodes found in candidates file")

return proxies[0]
```

def write_mihomo_config(node, path):
config = {
"mixed-port": 7890,
"external-controller": "127.0.0.1:9090",

```
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
```

def wait_for_mihomo(process):
"""
Check Mihomo Controller instead of testing YouTube.
This separates:
Mihomo startup
from:
node connectivity
"""

```
url = "http://" + MIHOMO_CONTROLLER + "/version"

start = time.perf_counter()

while time.perf_counter() - start < MIHOMO_STARTUP_TIMEOUT:

    if process.poll() is not None:
        raise RuntimeError(
            f"Mihomo exited during startup, return code={process.returncode}"
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

raise TimeoutError("Mihomo controller did not become ready")
```

# ============================================================

# Step 1

# YouTube page test

# ============================================================

def test_youtube_page():
proxies = {
"http": "http://" + MIHOMO_PROXY,
"https": "http://" + MIHOMO_PROXY,
}

```
start = time.perf_counter()

response = requests.get(
    TEST_VIDEO_URL,
    proxies=proxies,
    timeout=STAGE_TIMEOUT,
    headers={
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
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
    "bytes": len(response.content),
}
```

# ============================================================

# Step 2

# yt-dlp media extraction

# ============================================================

def extract_media_info():

```
output = subprocess.run(
    [
        "yt-dlp",

        "--no-playlist",

        "--skip-download",

        "--dump-single-json",

        "--no-warnings",

        "--socket-timeout",
        str(STAGE_TIMEOUT),

        "-f",
        (
            "bestvideo[height<=1080][vcodec^=vp9]"
            "/bestvideo[height<=1080][vcodec^=avc1]"
            "/bestvideo[height<=1080]"
        ),

        TEST_VIDEO_URL,
    ],
    capture_output=True,
    text=True,
    timeout=STAGE_TIMEOUT,
)

if output.returncode != 0:
    raise RuntimeError(
        "yt-dlp failed: " +
        output.stderr[-1000:]
    )

data = json.loads(output.stdout)

requested_downloads = data.get("requested_downloads") or []

if not requested_downloads:
    raise RuntimeError(
        "yt-dlp did not return requested_downloads"
    )

media = requested_downloads[0]

media_url = media.get("url")

if not media_url:
    raise RuntimeError(
        "yt-dlp did not return media URL"
    )

return {
    "media_url": media_url,

    "format_id": media.get("format_id"),

    "width": media.get("width"),
    "height": media.get("height"),

    "resolution": (
        f"{media.get('width')}x{media.get('height')}"
        if media.get("width") and media.get("height")
        else None
    ),

    "vcodec": media.get("vcodec"),

    "acodec": media.get("acodec"),

    "fps": media.get("fps"),

    "tbr": media.get("tbr"),

    "vbr": media.get("vbr"),

    "abr": media.get("abr"),

    "protocol": media.get("protocol"),

    "ext": media.get("ext"),

    "filesize": media.get("filesize"),
}
```

# ============================================================

# Step 3

# Real YouTube media download

# ============================================================

def download_media(media_url):

```
proxies = {
    "http": "http://" + MIHOMO_PROXY,
    "https": "http://" + MIHOMO_PROXY,
}

stage_start = time.perf_counter()

downloaded = 0
first_byte_time = None

with requests.get(
    media_url,
    proxies=proxies,
    stream=True,
    timeout=STAGE_TIMEOUT,
    headers={
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 "
            "(KHTML, like Gecko) "
            "Chrome/131.0 Safari/537.36"
        )
    }
) as response:

    response.raise_for_status()

    header_time = time.perf_counter()

    for chunk in response.iter_content(
        chunk_size=64 * 1024
    ):

        now = time.perf_counter()

        if first_byte_time is None:
            first_byte_time = now

        if now - stage_start > STAGE_TIMEOUT:
            raise TimeoutError(
                f"Media download exceeded {STAGE_TIMEOUT:.1f} seconds"
            )

        if not chunk:
            continue

        remaining = MAX_DOWNLOAD_BYTES - downloaded

        if remaining <= 0:
            break

        if len(chunk) > remaining:
            chunk = chunk[:remaining]

        downloaded += len(chunk)

        if downloaded >= MAX_DOWNLOAD_BYTES:
            break

stage_end = time.perf_counter()

if first_byte_time is None:
    raise RuntimeError(
        "Media server returned no data"
    )

ttfb = first_byte_time - stage_start
download_time = stage_end - first_byte_time
total_time = stage_end - stage_start

if total_time <= 0:
    throughput_mbps = 0
else:
    throughput_mbps = (
        downloaded * 8
        / total_time
        / 1_000_000
    )

return {
    "bytes": downloaded,
    "ttfb": ttfb,
    "download_time": download_time,
    "total_time": total_time,
    "throughput_mbps": throughput_mbps,
}
```

# ============================================================

# Worker

# ============================================================

def worker(node):

```
print("", flush=True)
print("free-nodes - single YouTube media test", flush=True)
print("", flush=True)

print(
    f"Node name: {node.get('name')}",
    flush=True
)

print(
    f"Protocol:  {node.get('type')}",
    flush=True
)

print(
    f"Stage timeout: {STAGE_TIMEOUT:.1f} seconds",
    flush=True
)

print(
    f"Mihomo startup timeout: "
    f"{MIHOMO_STARTUP_TIMEOUT:.1f} seconds",
    flush=True
)

print(
    f"Maximum download: "
    f"{MAX_DOWNLOAD_BYTES:,} bytes",
    flush=True
)

mihomo = None

try:

    # ----------------------------------------------------
    # Temporary Mihomo configuration
    # ----------------------------------------------------

    temp_dir = tempfile.mkdtemp(
        prefix="free-nodes-mihomo-"
    )

    c
```

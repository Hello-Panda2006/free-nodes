import json
import multiprocessing
import os
import subprocess
import tempfile
import time

import requests
import yaml

MIHOMO_BIN = "./bin/mihomo"

MIHOMO_PROXY = "127.0.0.1:7890"
MIHOMO_CONTROLLER = "127.0.0.1:9090"

TEST_VIDEO_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

STAGE_TIMEOUT = 3.0
MIHOMO_STARTUP_TIMEOUT = 5.0

MAX_DOWNLOAD_BYTES = 1024 * 1024

CANDIDATE_FILE = "data/candidates.yaml"

def load_first_node():
with open(CANDIDATE_FILE, "r", encoding="utf-8") as f:
data = yaml.safe_load(f)

```
proxies = data.get("proxies", [])

if not proxies:
    raise RuntimeError("No proxy nodes found")

return proxies[0]
```

def write_mihomo_config(node, config_path):
config = {
"mixed-port": 7890,
"external-controller": "127.0.0.1:9090",
"log-level": "error",

```
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

with open(config_path, "w", encoding="utf-8") as f:
    yaml.safe_dump(
        config,
        f,
        allow_unicode=True,
        sort_keys=False
    )
```

def wait_for_mihomo(process):
url = "http://" + MIHOMO_CONTROLLER + "/version"

```
start = time.perf_counter()

while time.perf_counter() - start < MIHOMO_STARTUP_TIMEOUT:

    if process.poll() is not None:
        raise RuntimeError(
            "Mihomo exited during startup: "
            + str(process.returncode)
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
    "Mihomo controller did not become ready"
)
```

def test_youtube_page():
proxies = {
"http": "http://" + MIHOMO_PROXY,
"https": "http://" + MIHOMO_PROXY
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
    "bytes": len(response.content)
}
```

def extract_media_info():
command = [
"yt-dlp",

```
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

    TEST_VIDEO_URL
]

result = subprocess.run(
    command,
    capture_output=True,
    text=True,
    timeout=STAGE_TIMEOUT
)

if result.returncode != 0:
    raise RuntimeError(
        "yt-dlp failed: "
        + result.stderr[-1000:]
    )

data = json.loads(result.stdout)

downloads = data.get("requested_downloads") or []

if not downloads:
    raise RuntimeError(
        "yt-dlp returned no requested_downloads"
    )

media = downloads[0]

media_url = media.get("url")

if not media_url:
    raise RuntimeError(
        "yt-dlp returned no media URL"
    )

return {
    "media_url": media_url,
    "format_id": media.get("format_id"),
    "width": media.get("width"),
    "height": media.get("height"),
    "vcodec": media.get("vcodec"),
    "acodec": media.get("acodec"),
    "fps": media.get("fps"),
    "tbr": media.get("tbr"),
    "vbr": media.get("vbr"),
    "abr": media.get("abr"),
    "protocol": media.get("protocol"),
    "ext": media.get("ext"),
    "filesize": media.get("filesize")
}
```

def download_media(media_url):
proxies = {
"http": "http://" + MIHOMO_PROXY,
"https": "http://" + MIHOMO_PROXY
}

```
start = time.perf_counter()

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

    for chunk in response.iter_content(
        chunk_size=64 * 1024
    ):

        now = time.perf_counter()

        if now - start > STAGE_TIMEOUT:
            raise TimeoutError(
                "Media download exceeded "
                f"{STAGE_TIMEOUT:.1f} seconds"
            )

        if not chunk:
            continue

        if first_byte_time is None:
            first_byte_time = now

        remaining = (
            MAX_DOWNLOAD_BYTES - downloaded
        )

        if remaining <= 0:
            break

        if len(chunk) > remaining:
            chunk = chunk[:remaining]

        downloaded += len(chunk)

        if downloaded >= MAX_DOWNLOAD_BYTES:
            break

end = time.perf_counter()

if first_byte_time is None:
    raise RuntimeError(
        "Media server returned no data"
    )

total_time = end - start
ttfb = first_byte_time - start
download_time = end - first_byte_time

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
    "throughput_mbps": throughput_mbps
}
```

def worker(node):

```
print("")
print("free-nodes - single YouTube media test")
print("")

print("Node name:", node.get("name"))
print("Protocol:", node.get("type"))
print(
    f"Stage timeout: {STAGE_TIMEOUT:.1f} seconds"
)
print(
    "Mihomo startup timeout: "
    f"{MIHOMO_STARTUP_TIMEOUT:.1f} seconds"
)
print(
    "Maximum download:",
    f"{MAX_DOWNLOAD_BYTES:,} bytes"
)

mihomo = None

try:

    temp_dir = tempfile.mkdtemp(
        prefix="free-nodes-mihomo-"
    )

    config_path = os.path.join(
        temp_dir,
        "config.yaml"
    )

    write_mihomo_config(
        node,
        config_path
    )

    print(
        "Temporary config:",
        config_path
    )

    print("Starting Mihomo...")

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

    print("")
    print("STEP 0: Mihomo startup")

    startup_time = wait_for_mihomo(
        mihomo
    )

    print(
        "Mihomo controller is ready:",
        MIHOMO_CONTROLLER
    )

    print(
        "Mihomo proxy is ready:",
        MIHOMO_PROXY
    )

    print(
        f"Mihomo startup time: "
        f"{startup_time:.3f}s"
    )

    print("")
    print("STEP 1: YouTube page")

    page = test_youtube_page()

    print(
        "HTTP status:",
        page["status_code"]
    )

    print(
        f"Page elapsed: "
        f"{page['elapsed']:.3f}s"
    )

    print(
        f"Page bytes: "
        f"{page['bytes']:,}"
    )

    print("YouTube page test: SUCCESS")

    print("")
    print("STEP 2: yt-dlp media extraction")

    extraction_start = time.perf_counter()

    media_info = extract_media_info()

    extraction_time = (
        time.perf_counter()
        - extraction_start
    )

    print(
        f"Extraction elapsed: "
        f"{extraction_time:.3f}s"
    )

    print(
        "Format ID:",
        media_info["format_id"]
```

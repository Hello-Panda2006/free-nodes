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
    proxies = data.get("proxies", [])
    if not proxies:
        raise RuntimeError("No proxy nodes found")
    return proxies[0]


def write_mihomo_config(node, path):
    config = {
        "mixed-port": 7890,
        "external-controller": "127.0.0.1:9090",
        "log-level": "error",
        "proxies": [node],
        "proxy-groups": [
            {"name": "TEST", "type": "select", "proxies": [node["name"]]}
        ],
        "rules": ["MATCH,TEST"],
    }
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(config, f, allow_unicode=True, sort_keys=False)


def wait_for_mihomo(process):
    url = "http://" + MIHOMO_CONTROLLER + "/version"
    start = time.perf_counter()
    while time.perf_counter() - start < MIHOMO_STARTUP_TIMEOUT:
        if process.poll() is not None:
            raise RuntimeError(
                f"Mihomo exited during startup, return code={process.returncode}"
            )
        try:
            response = requests.get(url, timeout=0.5)
            if response.status_code == 200:
                return time.perf_counter() - start
        except requests.RequestException:
            pass
        time.sleep(0.05)
    raise TimeoutError("Mihomo controller did not become ready")


def test_youtube_page():
    proxies = {"http": "http://" + MIHOMO_PROXY, "https": "http://" + MIHOMO_PROXY}
    start = time.perf_counter()
    response = requests.get(
        TEST_VIDEO_URL,
        proxies=proxies,
        timeout=STAGE_TIMEOUT,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
        },
    )
    elapsed = time.perf_counter() - start
    response.raise_for_status()
    return {"status_code": response.status_code, "elapsed": elapsed, "bytes": len(response.content)}


def extract_media_url():
    output = subprocess.run(
        [
            "yt-dlp", "--no-playlist", "--skip-download", "-g", "--no-warnings",
            "--socket-timeout", str(STAGE_TIMEOUT),
            "-f",
            "bestvideo[height<=1080][vcodec^=vp9]/bestvideo[height<=1080][vcodec^=avc1]/bestvideo[height<=1080]",
            TEST_VIDEO_URL,
        ],
        capture_output=True,
        text=True,
        timeout=STAGE_TIMEOUT,
    )
    if output.returncode != 0:
        raise RuntimeError("yt-dlp failed: " + output.stderr[-1000:])
    media_url = output.stdout.strip().splitlines()
    if not media_url:
        raise RuntimeError("yt-dlp returned no media URL")
    return media_url[0]


def download_media(media_url):
    proxies = {"http": "http://" + MIHOMO_PROXY, "https": "http://" + MIHOMO_PROXY}
    stage_start = time.perf_counter()
    downloaded = 0
    first_byte_time = None
    response = requests.get(
        media_url,
        proxies=proxies,
        stream=True,
        timeout=STAGE_TIMEOUT,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
        },
    )
    response.raise_for_status()
    try:
        for chunk in response.iter_content(chunk_size=64 * 1024):
            now = time.perf_counter()
            if now - stage_start > STAGE_TIMEOUT:
                raise TimeoutError(f"Media download exceeded {STAGE_TIMEOUT:.1f} seconds")
            if not chunk:
                continue
            if first_byte_time is None:
                first_byte_time = now
            remaining = MAX_DOWNLOAD_BYTES - downloaded
            if remaining <= 0:
                break
            if len(chunk) > remaining:
                chunk = chunk[:remaining]
            downloaded += len(chunk)
            if downloaded >= MAX_DOWNLOAD_BYTES:
                break
    finally:
        response.close()
    stage_end = time.perf_counter()
    if first_byte_time is None:
        raise RuntimeError("Media server returned no data")
    ttfb = first_byte_time - stage_start
    download_time = stage_end - first_byte_time
    total_time = stage_end - stage_start
    throughput_mbps = downloaded * 8 / total_time / 1_000_000
    return {
        "bytes": downloaded,
        "ttfb": ttfb,
        "download_time": download_time,
        "total_time": total_time,
        "throughput_mbps": throughput_mbps,
    }


def worker(node):
    print("", flush=True)
    print("free-nodes - single YouTube media test", flush=True)
    print("", flush=True)
    print(f"Node name: {node.get('name')}", flush=True)
    print(f"Protocol:  {node.get('type')}", flush=True)
    print(f"Stage timeout: {STAGE_TIMEOUT:.1f} seconds", flush=True)
    print(f"Mihomo startup timeout: {MIHOMO_STARTUP_TIMEOUT:.1f} seconds", flush=True)
    print(f"Maximum download: {MAX_DOWNLOAD_BYTES:,} bytes", flush=True)
    mihomo = None
    try:
        temp_dir = tempfile.mkdtemp(prefix="free-nodes-mihomo-")
        config_path = os.path.join(temp_dir, "config.yaml")
        write_mihomo_config(node, config_path)
        print(f"Temporary config: {config_path}", flush=True)
        print("Starting Mihomo...", flush=True)
        mihomo = subprocess.Popen(
            [MIHOMO_BIN, "-d", temp_dir, "-f", config_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        print("", flush=True)
        print("STEP 0: Mihomo startup", flush=True)
        startup_time = wait_for_mihomo(mihomo)
        print(f"Mihomo controller is ready: {MIHOMO_CONTROLLER}", flush=True)
        print(f"Mihomo proxy is ready: {MIHOMO_PROXY}", flush=True)
        print(f"Mihomo startup time: {startup_time:.3f}s", flush=True)
        print("", flush=True)
        print("STEP 1: YouTube page", flush=True)
        page = test_youtube_page()
        print(f"HTTP status: {page['status_code']}", flush=True)
        print(f"Page elapsed: {page['elapsed']:.3f}s", flush=True)
        print(f"Page bytes: {page['bytes']:,}", flush=True)
        print("YouTube page test: SUCCESS", flush=True)
        print("", flush=True)
        print("STEP 2: yt-dlp media extraction", flush=True)
        extraction_start = time.perf_counter()
        media_url = extract_media_url()
        extraction_time = time.perf_counter() - extraction_start
        print(f"Extraction elapsed: {extraction_time:.3f}s", flush=True)
        print("yt-dlp extraction: SUCCESS", flush=True)
        print("", flush=True)
        print("STEP 3: Real YouTube media download", flush=True)
        print(f"Maximum download: {MAX_DOWNLOAD_BYTES:,} bytes", flush=True)
        media = download_media(media_url)
        print(f"Media download: {media['bytes']:,} bytes", flush=True)
        print(f"Download time: {media['download_time']:.3f}s", flush=True)
        print(f"Media TTFB: {media['ttfb']:.3f}s", flush=True)
        print(f"Throughput: {media['throughput_mbps']:.2f} Mbps", flush=True)
        print("", flush=True)
        print("FINAL RESULT", flush=True)
        print("Result:            SUCCESS", flush=True)
        return 0
    except TimeoutError as e:
        print("", flush=True)
        print("FINAL RESULT", flush=True)
        print("Result:            FAILED", flush=True)
        print(f"Reason:            {e}", flush=True)
        return 1
    except subprocess.TimeoutExpired:
        print("", flush=True)
        print("FINAL RESULT", flush=True)
        print("Result:            FAILED", flush=True)
        print("Reason:            yt-dlp extraction exceeded 3.0 seconds", flush=True)
        return 1
    except Exception as e:
        print("", flush=True)
        print("FINAL RESULT", flush=True)
        print("Result:            FAILED", flush=True)
        print(f"Reason:            {type(e).__name__}: {e}", flush=True)
        return 1
    finally:
        if mihomo is not None:
            try:
                mihomo.terminate()
                try:
                    mihomo.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    mihomo.kill()
                    mihomo.wait(timeout=2)
            except Exception:
                pass


def worker_entry(node):
    exit_code = worker(node)
    raise SystemExit(exit_code)


def main():
    node = load_first_node()
    print("Starting single-node worker...", flush=True)
    process = multiprocessing.Process(target=worker_entry, args=(node,))
    process.start()
    maximum_runtime = MIHOMO_STARTUP_TIMEOUT + STAGE_TIMEOUT * 3 + 5
    process.join(timeout=maximum_runtime)
    if process.is_alive():
        print("Worker exceeded safety timeout. Terminating.", flush=True)
        process.terminate()
        process.join(timeout=2)
        if process.is_alive():
            process.kill()
            process.join()
        raise SystemExit(1)
    if process.exitcode is None:
        raise SystemExit(1)
    if process.exitcode == 0:
        raise SystemExit(0)
    raise SystemExit(1)


if __name__ == "__main__":
    main()

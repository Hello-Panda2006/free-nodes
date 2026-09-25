import json
import multiprocessing
import os
import subprocess
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor, as_completed

import requests
import yaml

MIHOMO_BIN = "./bin/mihomo"
CANDIDATE_FILE = "data/candidates.yaml"
RESULT_FILE = "data/test-results.json"

BASE_PROXY_PORT = 20000
BASE_CONTROLLER_PORT = 22000
CONCURRENCY = 20
STAGE_TIMEOUT = 3.0
MIHOMO_STARTUP_TIMEOUT = 5.0
MAX_DOWNLOAD_BYTES = 1024 * 1024
TEST_VIDEO_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0 Safari/537.36"
)


def load_nodes():
    with open(CANDIDATE_FILE, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    nodes = data.get("proxies", [])
    if not nodes:
        raise RuntimeError("No proxy nodes found")
    return [n for n in nodes if isinstance(n, dict) and n.get("name") and n.get("type")]


def write_mihomo_config(node, path, proxy_port, controller_port):
    config = {
        "mixed-port": proxy_port,
        "external-controller": f"127.0.0.1:{controller_port}",
        "log-level": "error",
        "proxies": [node],
        "proxy-groups": [
            {"name": "TEST", "type": "select", "proxies": [node["name"]]}
        ],
        "rules": ["MATCH,TEST"],
    }
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(config, f, allow_unicode=True, sort_keys=False)


def wait_for_mihomo(process, controller_port):
    url = f"http://127.0.0.1:{controller_port}/version"
    start = time.perf_counter()
    while time.perf_counter() - start < MIHOMO_STARTUP_TIMEOUT:
        if process.poll() is not None:
            raise RuntimeError(
                f"Mihomo exited during startup, return code={process.returncode}"
            )
        try:
            r = requests.get(url, timeout=0.3)
            if r.status_code == 200:
                return time.perf_counter() - start
        except requests.RequestException:
            pass
        time.sleep(0.05)
    raise TimeoutError("Mihomo controller did not become ready")


def test_youtube_page(proxy_port):
    proxies = {
        "http": f"http://127.0.0.1:{proxy_port}",
        "https": f"http://127.0.0.1:{proxy_port}",
    }
    start = time.perf_counter()
    r = requests.get(
        TEST_VIDEO_URL,
        proxies=proxies,
        stream=True,
        timeout=(1.0, STAGE_TIMEOUT),
        headers={"User-Agent": USER_AGENT},
    )
    try:
        if r.status_code < 200 or r.status_code >= 400:
            raise RuntimeError(f"HTTP status {r.status_code}")
        total = 0
        deadline = start + STAGE_TIMEOUT
        for chunk in r.iter_content(chunk_size=64 * 1024):
            if time.perf_counter() > deadline:
                raise TimeoutError("YouTube page exceeded 3.0 seconds")
            if chunk:
                total += len(chunk)
                # We only need proof that the page is actually responding.
                if total >= 64 * 1024:
                    break
        elapsed = time.perf_counter() - start
        return {"success": True, "status_code": r.status_code, "elapsed": elapsed, "bytes": total}
    finally:
        r.close()


def extract_media_url(proxy_port):
    proxy = f"http://127.0.0.1:{proxy_port}"
    env = os.environ.copy()
    env["HTTP_PROXY"] = proxy
    env["HTTPS_PROXY"] = proxy
    env["ALL_PROXY"] = proxy
    start = time.perf_counter()
    output = subprocess.run(
        [
            "yt-dlp",
            "--no-playlist",
            "--skip-download",
            "-g",
            "--no-warnings",
            "--socket-timeout",
            str(STAGE_TIMEOUT),
            "--proxy",
            proxy,
            "-f",
            "bestvideo[height<=1080][vcodec^=vp9]/bestvideo[height<=1080][vcodec^=avc1]/bestvideo[height<=1080]",
            TEST_VIDEO_URL,
        ],
        capture_output=True,
        text=True,
        timeout=STAGE_TIMEOUT,
        env=env,
    )
    elapsed = time.perf_counter() - start
    if output.returncode != 0:
        raise RuntimeError("yt-dlp failed: " + output.stderr[-500:])
    urls = [x.strip() for x in output.stdout.splitlines() if x.strip()]
    if not urls:
        raise RuntimeError("yt-dlp returned no media URL")
    return urls[0], elapsed


def download_media(media_url, proxy_port):
    proxies = {
        "http": f"http://127.0.0.1:{proxy_port}",
        "https": f"http://127.0.0.1:{proxy_port}",
    }
    stage_start = time.perf_counter()
    deadline = stage_start + STAGE_TIMEOUT
    downloaded = 0
    first_byte_time = None
    r = requests.get(
        media_url,
        proxies=proxies,
        stream=True,
        timeout=(1.0, 1.0),
        headers={"User-Agent": USER_AGENT},
    )
    try:
        r.raise_for_status()
        for chunk in r.iter_content(chunk_size=64 * 1024):
            now = time.perf_counter()
            if now > deadline:
                raise TimeoutError(
                    f"Media download exceeded {STAGE_TIMEOUT:.1f} seconds; bytes={downloaded}"
                )
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
        r.close()

    end = time.perf_counter()
    if first_byte_time is None:
        raise RuntimeError("Media server returned no data")
    total_time = end - stage_start
    ttfb = first_byte_time - stage_start
    download_time = end - first_byte_time
    throughput = downloaded * 8 / total_time / 1_000_000
    return {
        "bytes": downloaded,
        "ttfb": ttfb,
        "download_time": download_time,
        "total_time": total_time,
        "throughput_mbps": throughput,
    }


def test_node(node, index):
    # Ports are derived from the node index, so every node gets a unique pair.
    # This avoids clashes when ProcessPoolExecutor runs nodes concurrently.
    proxy_port = BASE_PROXY_PORT + index
    controller_port = BASE_CONTROLLER_PORT + index
    result = {
        "name": node.get("name"),
        "type": node.get("type"),
        "success": False,
        "reason": None,
        "mihomo_startup": None,
        "page": None,
        "extraction": None,
        "media": None,
    }
    mihomo = None
    temp_dir = None
    try:
        temp_dir = tempfile.mkdtemp(prefix="free-nodes-mihomo-")
        config_path = os.path.join(temp_dir, "config.yaml")
        write_mihomo_config(node, config_path, proxy_port, controller_port)
        mihomo = subprocess.Popen(
            [MIHOMO_BIN, "-d", temp_dir, "-f", config_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        result["mihomo_startup"] = wait_for_mihomo(mihomo, controller_port)

        page = test_youtube_page(proxy_port)
        result["page"] = page

        media_url, extraction_time = extract_media_url(proxy_port)
        result["extraction"] = {"success": True, "elapsed": extraction_time}

        media = download_media(media_url, proxy_port)
        result["media"] = media
        result["success"] = True
        result["reason"] = "ok"
    except TimeoutError as e:
        result["reason"] = "timeout: " + str(e)
        if result["media"] is None and "bytes=" in str(e):
            try:
                result["media"] = {"bytes": int(str(e).split("bytes=")[-1])}
            except ValueError:
                pass
    except subprocess.TimeoutExpired:
        result["reason"] = "yt-dlp extraction exceeded 3.0 seconds"
    except Exception as e:
        result["reason"] = f"{type(e).__name__}: {e}"
    finally:
        if mihomo is not None:
            try:
                mihomo.terminate()
                mihomo.wait(timeout=1.5)
            except Exception:
                try:
                    mihomo.kill()
                    mihomo.wait(timeout=1.5)
                except Exception:
                    pass
    return result


def main():
    nodes = load_nodes()
    print(f"Loaded {len(nodes)} candidate nodes")
    print(f"Concurrency: {CONCURRENCY}")
    print(f"Stage timeout: {STAGE_TIMEOUT:.1f}s")
    print(f"Maximum media download: {MAX_DOWNLOAD_BYTES:,} bytes")
    print()

    results = []
    completed = 0
    start_all = time.perf_counter()

    # Each node gets its own deterministic port pair, so 20 concurrent Mihomo
    # instances can coexist without port clashes.
    with ProcessPoolExecutor(max_workers=CONCURRENCY) as executor:
        futures = {}
        for index, node in enumerate(nodes):
            future = executor.submit(test_node, node, index)
            futures[future] = index
            if len(futures) >= CONCURRENCY * 2:
                # Keep the in-flight queue bounded so the runner does not hold
                # all 1210 node configs in active worker state at once.
                done = next(as_completed(futures))
                idx = futures.pop(done)
                result = done.result()
                result["index"] = idx
                results.append(result)
                completed += 1
                status = "PASS" if result["success"] else "FAIL"
                print(f"[{completed}/{len(nodes)}] {status} | {result['name']}", flush=True)

        for future in as_completed(futures):
            idx = futures[future]
            result = future.result()
            result["index"] = idx
            results.append(result)
            completed += 1
            status = "PASS" if result["success"] else "FAIL"
            print(f"[{completed}/{len(nodes)}] {status} | {result['name']}", flush=True)

    results.sort(key=lambda x: x["index"])
    elapsed = time.perf_counter() - start_all

    os.makedirs(os.path.dirname(RESULT_FILE), exist_ok=True)
    with open(RESULT_FILE, "w", encoding="utf-8") as f:
        json.dump(
            {
                "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "candidate_count": len(nodes),
                "tested_count": len(results),
                "concurrency": CONCURRENCY,
                "stage_timeout_seconds": STAGE_TIMEOUT,
                "max_download_bytes": MAX_DOWNLOAD_BYTES,
                "elapsed_seconds": elapsed,
                "results": results,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )

    passed = sum(1 for r in results if r["success"])
    print()
    print("BATCH TEST COMPLETE")
    print(f"Candidates: {len(nodes)}")
    print(f"Tested:    {len(results)}")
    print(f"Passed:    {passed}")
    print(f"Failed:    {len(results) - passed}")
    print(f"Elapsed:   {elapsed:.1f}s")
    print(f"Results:   {RESULT_FILE}")


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()

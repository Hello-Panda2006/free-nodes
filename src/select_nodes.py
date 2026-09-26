import json
from pathlib import Path
import yaml


# ============================================================
# Paths
# ============================================================

BASE_DIR = Path(__file__).resolve().parent.parent

CANDIDATES_FILE = BASE_DIR / "data" / "candidates.yaml"
RESULTS_FILE = BASE_DIR / "data" / "test-results.json"
OUTPUT_FILE = BASE_DIR / "data" / "free-nodes.yaml"


# ============================================================
# Load candidates
# ============================================================

def load_candidates():
    with open(CANDIDATES_FILE, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    candidates = data.get("proxies", [])

    if not isinstance(candidates, list):
        raise ValueError("candidates.yaml 中的 proxies 不是列表")

    return candidates


# ============================================================
# Load test results
# ============================================================

def load_test_results():
    with open(RESULTS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    # 兼容几种常见结构
    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        if isinstance(data.get("results"), list):
            return data["results"]

        if isinstance(data.get("test_results"), list):
            return data["test_results"]

    raise ValueError("无法识别 test-results.json 的结构")


# ============================================================
# Determine whether YouTube test passed
# ============================================================

def youtube_passed(result):
    """
    只判断 YouTube 是否真正跑通。

    不考虑：
    - throughput
    - elapsed
    - speed ranking
    - 12 Mbps threshold
    - node count
    """

    if not isinstance(result, dict):
        return False

    # 最终媒体下载必须 complete
    media = result.get("media")

    if isinstance(media, dict):
        if media.get("status") == "complete":
            return True

    # 兼容某些测试结果直接记录 youtube.status
    youtube = result.get("youtube")

    if isinstance(youtube, dict):
        if youtube.get("status") == "success":
            return True

    return False


# ============================================================
# Build test-result index
# ============================================================

def build_result_map(results):
    """
    test_nodes.py 使用 1-based index：
        1 -> candidates[0]
        2 -> candidates[1]
        ...

    同时兼容 result 中没有 index 的情况。
    """

    result_map = {}

    for position, result in enumerate(results, start=1):

        if not isinstance(result, dict):
            continue

        index = result.get("index")

        if index is None:
            index = position

        try:
            index = int(index)
        except (TypeError, ValueError):
            continue

        result_map[index] = result

    return result_map


# ============================================================
# Node identity for deduplication
# ============================================================

def node_fingerprint(proxy):
    """
    用节点实际配置进行去重。

    name 不参与去重。
    client-fingerprint 不参与去重。

    其他节点参数全部保留并参与判断。
    """

    if not isinstance(proxy, dict):
        return None

    identity = {}

    for key, value in proxy.items():

        if key in ("name", "client-fingerprint"):
            continue

        # 内部字段不应该影响节点身份
        if key.startswith("_"):
            continue

        identity[key] = value

    # 使用 JSON 保证不同字典顺序不会造成不同 fingerprint
    return json.dumps(
        identity,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


# ============================================================
# Remove internal fields
# ============================================================

def clean_output_proxy(proxy):
    """
    输出前只删除程序自己的内部字段。

    不修改任何实际节点参数。
    """

    return {
        key: value
        for key, value in proxy.items()
        if not key.startswith("_")
    }


# ============================================================
# Main
# ============================================================

def main():

    print("=" * 70)
    print("FREE NODE EXPORT")
    print("=" * 70)

    # --------------------------------------------------------
    # Load
    # --------------------------------------------------------

    candidates = load_candidates()
    results = load_test_results()

    print(f"Candidates:          {len(candidates)}")
    print(f"Test results:        {len(results)}")

    result_map = build_result_map(results)

    # --------------------------------------------------------
    # Select YouTube-working nodes
    # --------------------------------------------------------

    passed_nodes = []

    for index, result in result_map.items():

        # index 是 1-based
        candidate_index = index - 1

        if candidate_index < 0:
            continue

        if candidate_index >= len(candidates):
            continue

        if not youtube_passed(result):
            continue

        proxy = candidates[candidate_index]

        if not isinstance(proxy, dict):
            continue

        passed_nodes.append(proxy)

    print(f"YouTube passed:     {len(passed_nodes)}")

    # --------------------------------------------------------
    # Deduplicate
    # --------------------------------------------------------

    unique_nodes = []
    seen = set()

    duplicate_count = 0

    for proxy in passed_nodes:

        fingerprint = node_fingerprint(proxy)

        if fingerprint is None:
            continue

        if fingerprint in seen:
            duplicate_count += 1
            continue

        seen.add(fingerprint)

        unique_nodes.append(
            clean_output_proxy(proxy)
        )

    # --------------------------------------------------------
    # Save
    # --------------------------------------------------------

    output = {
        "proxies": unique_nodes
    }

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        yaml.safe_dump(
            output,
            f,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        )

    # --------------------------------------------------------
    # Summary
    # --------------------------------------------------------

    print(f"Duplicates removed: {duplicate_count}")
    print(f"Nodes exported:     {len(unique_nodes)}")
    print(f"Output file:        {OUTPUT_FILE}")

    print("=" * 70)


if __name__ == "__main__":
    main()

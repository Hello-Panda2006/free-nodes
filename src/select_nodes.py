import json
import yaml
from pathlib import Path


CANDIDATES_FILE = Path("data/candidates.yaml")
RESULTS_FILE = Path("data/test-results.json")
OUTPUT_FILE = Path("data/free-nodes.yaml")

MIN_SPEED_MBPS = 12.0
MAX_NODES = 100


def load_candidates():
    with CANDIDATES_FILE.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    if isinstance(data, dict):
        candidates = data.get("proxies", [])
    elif isinstance(data, list):
        candidates = data
    else:
        candidates = []

    return candidates


def load_results():
    with RESULTS_FILE.open("r", encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, dict):
        return data.get("results", [])

    if isinstance(data, list):
        return data

    return []


def main():
    if not CANDIDATES_FILE.exists():
        raise FileNotFoundError(
            f"Candidates file not found: {CANDIDATES_FILE}"
        )

    if not RESULTS_FILE.exists():
        raise FileNotFoundError(
            f"Test results file not found: {RESULTS_FILE}"
        )

    candidates = load_candidates()
    results = load_results()

    print(f"Loaded candidates: {len(candidates)}")
    print(f"Loaded test results: {len(results)}")

    # 根据 index 建立节点索引
    candidate_map = {}

    for index, node in enumerate(candidates, start=1):
        if isinstance(node, dict):
            candidate_map[index] = node

    qualified = []

    for result in results:
        if not isinstance(result, dict):
            continue

        index = result.get("index")

        if not isinstance(index, int):
            continue

        # 必须存在对应的完整节点配置
        node = candidate_map.get(index)

        if not isinstance(node, dict):
            continue

        media = result.get("media")

        if not isinstance(media, dict):
            continue

        # 必须完整下载 1 MiB
        if media.get("status") != "complete":
            continue

        throughput = media.get("throughput_mbps")

        if throughput is None:
            continue

        try:
            throughput = float(throughput)
        except (TypeError, ValueError):
            continue

        # 速度必须 >= 12 Mbps
        if throughput < MIN_SPEED_MBPS:
            continue

        qualified.append({
            "index": index,
            "name": result.get("name", node.get("name", "")),
            "type": result.get("type", node.get("type", "")),
            "throughput_mbps": throughput,
            "node": node,
        })

    # 按实际媒体下载速度从高到低排序
    qualified.sort(
        key=lambda x: x["throughput_mbps"],
        reverse=True
    )

    # 最多保留 100 个
    selected = qualified[:MAX_NODES]

    # 只输出完整节点配置
    proxies = []

    for item in selected:
        node = dict(item["node"])

        # 删除内部字段
        node = {
            key: value
            for key, value in node.items()
            if not str(key).startswith("_")
        }

        proxies.append(node)

    output = {
        "proxies": proxies
    }

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)

    with OUTPUT_FILE.open("w", encoding="utf-8") as f:
        yaml.safe_dump(
            output,
            f,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False
        )

    # 输出统计信息
    print()
    print("=" * 60)
    print("FREE NODE SELECTION")
    print("=" * 60)

    print(f"Minimum speed:      {MIN_SPEED_MBPS:.1f} Mbps")
    print(f"Maximum nodes:      {MAX_NODES}")
    print(f"Qualified nodes:    {len(qualified)}")
    print(f"Selected nodes:     {len(selected)}")
    print(f"Output file:        {OUTPUT_FILE}")

    if selected:
        print()
        print("Top selected nodes:")

        for position, item in enumerate(selected[:20], start=1):
            print(
                f"{position:03d}. "
                f"{item['throughput_mbps']:8.2f} Mbps  "
                f"{item['type']:12s}  "
                f"{item['name']}"
            )

        print()

        print(
            f"Fastest: {selected[0]['throughput_mbps']:.2f} Mbps"
        )

        print(
            f"Slowest selected: "
            f"{selected[-1]['throughput_mbps']:.2f} Mbps"
        )

    print("=" * 60)


if __name__ == "__main__":
    main()

import json
import yaml
from pathlib import Path


RESULTS_FILE = Path("data/test-results.json")
OUTPUT_FILE = Path("data/free-nodes.yaml")

MIN_SPEED_MBPS = 12.0
MAX_NODES = 100


def main():
    if not RESULTS_FILE.exists():
        raise FileNotFoundError(f"Results file not found: {RESULTS_FILE}")

    with RESULTS_FILE.open("r", encoding="utf-8") as f:
        results = json.load(f)

    if isinstance(results, dict):
        results = results.get("results", [])

    candidates = []

    for item in results:
        if not isinstance(item, dict):
            continue

        throughput = item.get("throughput_mbps")

        if throughput is None:
            continue

        try:
            throughput = float(throughput)
        except (TypeError, ValueError):
            continue

        # 只保留实际媒体下载速度 >= 12 Mbps 的节点
        if throughput < MIN_SPEED_MBPS:
            continue

        # 必须是实际媒体测试成功的节点
        media_status = item.get("media_status")

        if media_status not in ("complete", "partial"):
            continue

        proxy = item.get("proxy")

        if not isinstance(proxy, dict):
            continue

        node = dict(proxy)

        # 清理内部字段
        node = {
            k: v
            for k, v in node.items()
            if not str(k).startswith("_")
        }

        candidates.append({
            "throughput_mbps": throughput,
            "node": node,
            "name": node.get("name", ""),
        })

    # 按实际下载速度从高到低排序
    candidates.sort(
        key=lambda x: x["throughput_mbps"],
        reverse=True
    )

    # 最多保留 100 个
    selected = candidates[:MAX_NODES]

    # 最终只输出节点配置
    proxies = []

    for item in selected:
        proxies.append(item["node"])

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

    print()
    print("=" * 60)
    print("FREE NODE SELECTION")
    print("=" * 60)
    print(f"Minimum speed: {MIN_SPEED_MBPS} Mbps")
    print(f"Maximum nodes: {MAX_NODES}")
    print(f"Qualified nodes: {len(candidates)}")
    print(f"Selected nodes: {len(selected)}")

    if selected:
        print()
        print("Top selected nodes:")

        for i, item in enumerate(selected[:10], 1):
            print(
                f"{i:02d}. "
                f"{item['throughput_mbps']:.2f} Mbps - "
                f"{item['name']}"
            )

    print()
    print(f"Output: {OUTPUT_FILE}")
    print("=" * 60)


if __name__ == "__main__":
    main()

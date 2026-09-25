import json
import yaml
from pathlib import Path
from collections import OrderedDict


CANDIDATES_FILE = Path("data/candidates.yaml")
RESULTS_FILE = Path("data/test-results.json")
OUTPUT_FILE = Path("data/free-nodes.yaml")

MIN_SPEED_MBPS = 12.0
MAX_NODES = 100


def normalize_value(value):
    """
    将 YAML 中的值转换成稳定、可比较的形式。
    """

    if isinstance(value, dict):
        return tuple(
            sorted(
                (str(k), normalize_value(v))
                for k, v in value.items()
            )
        )

    if isinstance(value, list):
        return tuple(
            normalize_value(v)
            for v in value
        )

    return value


def get_node_identity(node):
    """
    判断两个节点是否属于同一个核心节点。

    不参与身份判断：
    - name
    - client-fingerprint

    其他字段全部参与判断。
    """

    identity = {}

    for key, value in node.items():

        if key == "name":
            continue

        if key == "client-fingerprint":
            continue

        identity[key] = normalize_value(value)

    return tuple(
        sorted(identity.items())
    )


def load_candidates():
    with CANDIDATES_FILE.open(
        "r",
        encoding="utf-8"
    ) as f:

        data = yaml.safe_load(f)

    if isinstance(data, dict):
        candidates = data.get("proxies", [])

    elif isinstance(data, list):
        candidates = data

    else:
        candidates = []

    return candidates


def load_results():
    with RESULTS_FILE.open(
        "r",
        encoding="utf-8"
    ) as f:

        data = json.load(f)

    if isinstance(data, dict):
        return data.get("results", [])

    if isinstance(data, list):
        return data

    return []


def main():

    if not CANDIDATES_FILE.exists():
        raise FileNotFoundError(
            f"Candidates file not found: "
            f"{CANDIDATES_FILE}"
        )

    if not RESULTS_FILE.exists():
        raise FileNotFoundError(
            f"Test results file not found: "
            f"{RESULTS_FILE}"
        )

    candidates = load_candidates()
    results = load_results()

    print(
        f"Loaded candidates: {len(candidates)}"
    )

    print(
        f"Loaded test results: {len(results)}"
    )

    # --------------------------------------------------
    # 建立候选节点索引
    # --------------------------------------------------

    candidate_map = {}

    for index, node in enumerate(
        candidates,
        start=1
    ):

        if isinstance(node, dict):
            candidate_map[index] = node

    # --------------------------------------------------
    # 第一步：
    # 筛选完整媒体下载，并满足最低速度
    # --------------------------------------------------

    qualified = []

    for result in results:

        if not isinstance(result, dict):
            continue

        index = result.get("index")

        if not isinstance(index, int):
            continue

        node = candidate_map.get(index)

        if not isinstance(node, dict):
            continue

        media = result.get("media")

        if not isinstance(media, dict):
            continue

        # 必须是完整的 1 MiB 媒体下载
        if media.get("status") != "complete":
            continue

        throughput = media.get(
            "throughput_mbps"
        )

        if throughput is None:
            continue

        try:
            throughput = float(throughput)

        except (
            TypeError,
            ValueError
        ):
            continue

        # 最低速度要求
        if throughput < MIN_SPEED_MBPS:
            continue

        qualified.append({
            "index": index,
            "name": result.get(
                "name",
                node.get("name", "")
            ),
            "type": result.get(
                "type",
                node.get("type", "")
            ),
            "throughput_mbps": throughput,
            "node": node,
        })

    # --------------------------------------------------
    # 第二步：
    # 按实际媒体下载速度从高到低排序
    # --------------------------------------------------

    qualified.sort(
        key=lambda x: x["throughput_mbps"],
        reverse=True
    )

    # --------------------------------------------------
    # 第三步：
    # 只取前 100 个
    #
    # 注意：
    # 这里先取 100，再去重。
    #
    # 去重后绝对不会从第 101 名补充。
    # --------------------------------------------------

    top_candidates = qualified[:MAX_NODES]

    # --------------------------------------------------
    # 第四步：
    # 对前 100 个进行核心节点身份去重
    #
    # 因为 top_candidates 已经按照速度降序排列，
    # 所以同一节点的第一个出现者就是最快变体。
    # --------------------------------------------------

    selected = []

    seen_identities = set()

    duplicate_count = 0

    duplicate_groups = OrderedDict()

    for item in top_candidates:

        node = item["node"]

        identity = get_node_identity(node)

        if identity in seen_identities:

            duplicate_count += 1

            duplicate_groups.setdefault(
                identity,
                []
            ).append(item)

            continue

        seen_identities.add(identity)

        selected.append(item)

    # --------------------------------------------------
    # 第五步：
    # 输出最终节点
    # --------------------------------------------------

    proxies = []

    for item in selected:

        node = dict(
            item["node"]
        )

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

    OUTPUT_FILE.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    with OUTPUT_FILE.open(
        "w",
        encoding="utf-8"
    ) as f:

        yaml.safe_dump(
            output,
            f,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False
        )

    # --------------------------------------------------
    # 输出统计信息
    # --------------------------------------------------

    print()
    print("=" * 70)
    print("FREE NODE SELECTION")
    print("=" * 70)

    print(
        f"Minimum speed:       "
        f"{MIN_SPEED_MBPS:.1f} Mbps"
    )

    print(
        f"Maximum candidates:  "
        f"{MAX_NODES}"
    )

    print(
        f"Qualified nodes:     "
        f"{len(qualified)}"
    )

    print(
        f"Top candidates:      "
        f"{len(top_candidates)}"
    )

    print(
        f"Duplicate removed:   "
        f"{duplicate_count}"
    )

    print(
        f"Final selected:      "
        f"{len(selected)}"
    )

    print(
        f"Output file:         "
        f"{OUTPUT_FILE}"
    )

    # --------------------------------------------------
    # 重复节点统计
    # --------------------------------------------------

    if duplicate_count > 0:

        print()
        print(
            f"Duplicate groups "
            f"within top {len(top_candidates)}: "
            f"{len(duplicate_groups)}"
        )

    # --------------------------------------------------
    # 输出最终节点列表
    # --------------------------------------------------

    if selected:

        print()
        print("Top selected nodes:")

        for position, item in enumerate(
            selected[:20],
            start=1
        ):

            print(
                f"{position:03d}. "
                f"{item['throughput_mbps']:8.2f} Mbps  "
                f"{item['type']:12s}  "
                f"{item['name']}"
            )

        print()

        print(
            f"Fastest: "
            f"{selected[0]['throughput_mbps']:.2f} Mbps"
        )

        print(
            f"Slowest selected: "
            f"{selected[-1]['throughput_mbps']:.2f} Mbps"
        )

    print("=" * 70)


if __name__ == "__main__":
    main()

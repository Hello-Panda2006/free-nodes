import json
import yaml
from pathlib import Path


CANDIDATES_FILE = Path("data/candidates.yaml")
RESULTS_FILE = Path("data/test-results.json")
OUTPUT_FILE = Path("data/free-nodes.yaml")


def load_candidates():
    with CANDIDATES_FILE.open(
        "r",
        encoding="utf-8"
    ) as f:
        data = yaml.safe_load(f)

    if isinstance(data, dict):
        return data.get("proxies", [])

    if isinstance(data, list):
        return data

    return []


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
    # 全量转移
    #
    # 不限制：
    # - 最低速度
    # - 节点数量
    # - 排名
    # - 去重
    #
    # 只要求：
    # test-results.json 中存在对应 index
    # --------------------------------------------------

    selected = []

    for result in results:

        if not isinstance(result, dict):
            continue

        index = result.get("index")

        if not isinstance(index, int):
            continue

        node = candidate_map.get(index)

        if not isinstance(node, dict):
            continue

        selected.append(node)

    # --------------------------------------------------
    # 生成最终 YAML
    #
    # 节点本身不做任何修改
    # --------------------------------------------------

    output = {
        "proxies": selected
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
    # 统计
    # --------------------------------------------------

    print()
    print("=" * 70)
    print("FREE NODE EXPORT")
    print("=" * 70)

    print(
        f"Candidates:          {len(candidates)}"
    )

    print(
        f"Test results:        {len(results)}"
    )

    print(
        f"Nodes exported:      {len(selected)}"
    )

    print(
        f"Output file:         {OUTPUT_FILE}"
    )

    print("=" * 70)


if __name__ == "__main__":
    main()

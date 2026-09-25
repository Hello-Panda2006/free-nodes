import yaml
from pathlib import Path
from collections import defaultdict


INPUT_FILE = Path("data/free-nodes.yaml")


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
        return tuple(normalize_value(v) for v in value)

    return value


def get_node_identity(node):
    """
    判断两个节点是否属于同一个“实际节点”。

    排除：
    - name
    - client-fingerprint

    保留其他核心连接参数。
    """

    identity = {}

    for key, value in node.items():

        # 名称不是节点身份
        if key == "name":
            continue

        # fingerprint 不作为节点身份
        # chrome / firefox / random / ios 等通常只是客户端指纹
        if key == "client-fingerprint":
            continue

        identity[key] = normalize_value(value)

    return tuple(sorted(identity.items()))


def main():

    if not INPUT_FILE.exists():
        raise FileNotFoundError(
            f"File not found: {INPUT_FILE}"
        )

    with INPUT_FILE.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    if not isinstance(data, dict):
        raise ValueError("YAML root must be a dictionary")

    nodes = data.get("proxies", [])

    if not isinstance(nodes, list):
        raise ValueError("'proxies' must be a list")

    print("=" * 70)
    print("FREE NODE DUPLICATE ANALYSIS")
    print("=" * 70)

    print(f"Total nodes: {len(nodes)}")

    groups = defaultdict(list)

    for index, node in enumerate(nodes, start=1):

        if not isinstance(node, dict):
            continue

        identity = get_node_identity(node)

        groups[identity].append({
            "index": index,
            "name": node.get("name", ""),
            "type": node.get("type", ""),
            "server": node.get("server", ""),
            "port": node.get("port", ""),
            "fingerprint": node.get("client-fingerprint", ""),
        })

    duplicate_groups = [
        group
        for group in groups.values()
        if len(group) > 1
    ]

    duplicate_nodes = sum(
        len(group)
        for group in duplicate_groups
    )

    unique_nodes = len(groups)

    redundant_nodes = len(nodes) - unique_nodes

    print(f"Unique node identities: {unique_nodes}")
    print(f"Duplicate groups:       {len(duplicate_groups)}")
    print(f"Nodes in duplicate groups: {duplicate_nodes}")
    print(f"Redundant nodes:        {redundant_nodes}")

    print()
    print("=" * 70)
    print("DUPLICATE GROUPS")
    print("=" * 70)

    if not duplicate_groups:
        print("No duplicate node identities found.")
        print("=" * 70)
        return

    # 重复数量最多的组排在前面
    duplicate_groups.sort(
        key=len,
        reverse=True
    )

    for group_number, group in enumerate(
        duplicate_groups,
        start=1
    ):

        print()
        print(
            f"[Group {group_number}] "
            f"{len(group)} variants"
        )

        for item in group:

            fingerprint = item["fingerprint"]

            if not fingerprint:
                fingerprint = "(none)"

            print(
                f"  {item['index']:03d}. "
                f"{item['type']:12s} "
                f"{item['server']}:{item['port']}  "
                f"fingerprint={fingerprint}  "
                f"name={item['name']}"
            )

    print()
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)

    print(
        f"Current nodes:       {len(nodes)}"
    )

    print(
        f"Unique identities:   {unique_nodes}"
    )

    print(
        f"Could remove:        {redundant_nodes}"
    )

    print(
        f"Potential final size: {unique_nodes}"
    )

    print("=" * 70)


if __name__ == "__main__":
    main()

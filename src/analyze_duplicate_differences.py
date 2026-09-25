import yaml
from pathlib import Path
from collections import defaultdict


INPUT_FILE = Path("data/free-nodes.yaml")


def normalize_value(value):
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


def get_identity(node):
    """
    用于判断“核心节点身份”。

    暂时排除：
    - name
    - client-fingerprint
    """

    identity = {}

    for key, value in node.items():

        if key == "name":
            continue

        if key == "client-fingerprint":
            continue

        identity[key] = normalize_value(value)

    return tuple(sorted(identity.items()))


def format_value(value):
    text = repr(value)

    if len(text) > 200:
        text = text[:200] + "..."

    return text


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

    groups = defaultdict(list)

    for index, node in enumerate(nodes, start=1):

        if not isinstance(node, dict):
            continue

        identity = get_identity(node)

        groups[identity].append({
            "index": index,
            "node": node,
        })

    duplicate_groups = [
        group
        for group in groups.values()
        if len(group) > 1
    ]

    duplicate_groups.sort(
        key=len,
        reverse=True
    )

    print("=" * 80)
    print("DUPLICATE NODE FIELD DIFFERENCE ANALYSIS")
    print("=" * 80)

    print(f"Total nodes: {len(nodes)}")
    print(f"Duplicate groups: {len(duplicate_groups)}")

    if not duplicate_groups:
        print("No duplicate groups found.")
        return

    for group_number, group in enumerate(
        duplicate_groups,
        start=1
    ):

        print()
        print("=" * 80)
        print(
            f"GROUP {group_number} "
            f"({len(group)} variants)"
        )
        print("=" * 80)

        # 所有字段
        all_keys = set()

        for item in group:
            all_keys.update(item["node"].keys())

        # 不需要比较的字段
        all_keys.discard("name")
        all_keys.discard("client-fingerprint")

        # 找出真正发生差异的字段
        different_keys = []

        for key in sorted(all_keys):

            values = set()

            for item in group:
                value = normalize_value(
                    item["node"].get(key)
                )
                values.add(repr(value))

            if len(values) > 1:
                different_keys.append(key)

        print()
        print("Members:")

        for item in group:

            node = item["node"]

            print(
                f"  [{item['index']:03d}] "
                f"{node.get('type', ''):12s} "
                f"{node.get('server', '')}:"
                f"{node.get('port', '')}  "
                f"fingerprint="
                f"{node.get('client-fingerprint', '(none)')}  "
                f"name="
                f"{node.get('name', '')}"
            )

        print()

        if not different_keys:

            print(
                "Core fields are identical. "
                "Only name/fingerprint differ."
            )

            continue

        print("Different core fields:")

        for key in different_keys:

            print()
            print(f"  FIELD: {key}")

            for item in group:

                node = item["node"]

                value = node.get(key)

                print(
                    f"    [{item['index']:03d}] "
                    f"{format_value(value)}"
                )

    print()
    print("=" * 80)
    print("END")
    print("=" * 80)


if __name__ == "__main__":
    main()

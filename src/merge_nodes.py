#!/usr/bin/env python3

"""
Merge and deduplicate free-node sources.

第二阶段负责：
1. 读取 config/sources.yaml
2. 下载所有启用的 YAML 来源
3. 提取 proxies
4. 进行最低限度的有效性检查
5. 根据节点实际配置生成 fingerprint
6. 去除重复节点
7. 输出 data/candidates.yaml
8. 输出统计结果

本阶段不负责：
- 使用 Mihomo
- 测试节点
- 测试 YouTube
- 测试速度
- 排名节点
- 限制节点数量
- 生成最终 free-nodes.yaml
- 修改节点连接参数
"""

from __future__ import annotations

import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import requests
import yaml


# ============================================================
# Paths
# ============================================================

ROOT = Path(__file__).resolve().parents[1]

CONFIG_FILE = ROOT / "config" / "sources.yaml"
OUTPUT_FILE = ROOT / "data" / "candidates.yaml"


# ============================================================
# HTTP settings
# ============================================================

USER_AGENT = (
    "Mozilla/5.0 "
    "(Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 "
    "(KHTML, like Gecko) "
    "Chrome/140.0 Safari/537.36"
)

REQUEST_TIMEOUT = 30


# ============================================================
# Load source configuration
# ============================================================

def load_sources() -> list[dict]:
    """Load source definitions from config/sources.yaml."""

    if not CONFIG_FILE.exists():
        raise FileNotFoundError(
            f"Configuration file not found: {CONFIG_FILE}"
        )

    with CONFIG_FILE.open("r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    if not isinstance(config, dict):
        raise ValueError(
            "sources.yaml must contain a YAML mapping."
        )

    sources = config.get("sources")

    if not isinstance(sources, list):
        raise ValueError(
            "sources.yaml must contain a 'sources' list."
        )

    return sources


# ============================================================
# Download YAML source
# ============================================================

def download_yaml(name: str, url: str) -> dict:
    """Download and parse one YAML source."""

    print(f"\n[{name}]")
    print(f"URL: {url}")
    print("Downloading...")

    response = requests.get(
        url,
        headers={"User-Agent": USER_AGENT},
        timeout=REQUEST_TIMEOUT,
    )

    response.raise_for_status()

    print(
        f"HTTP {response.status_code}, "
        f"{len(response.content):,} bytes"
    )

    try:
        text = response.content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(
            f"{name}: response is not valid UTF-8"
        ) from exc

    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(
            f"{name}: YAML parsing failed: {exc}"
        ) from exc

    if not isinstance(document, dict):
        raise ValueError(
            f"{name}: YAML root must be a mapping."
        )

    return document


# ============================================================
# Normalize values for fingerprint
# ============================================================

def normalize_value(value: Any) -> Any:
    """
    Recursively normalize values so that fingerprinting
    is independent of dictionary key ordering.
    """

    if isinstance(value, dict):
        return {
            str(key): normalize_value(value[key])
            for key in sorted(value.keys(), key=str)
        }

    if isinstance(value, list):
        return [
            normalize_value(item)
            for item in value
        ]

    return value


# ============================================================
# Build node fingerprint
# ============================================================

def build_fingerprint(proxy: dict) -> str:
    """
    Build a stable fingerprint from the actual node configuration.

    The following fields do NOT participate in deduplication:

    - name
    - internal fields beginning with "_"

    Everything else is considered part of the node configuration.

    Important:
    This function does not modify the original node.
    """

    fingerprint_data = {
        key: value
        for key, value in proxy.items()
        if key != "name"
        and not key.startswith("_")
    }

    normalized = normalize_value(fingerprint_data)

    serialized = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )

    return hashlib.sha256(
        serialized.encode("utf-8")
    ).hexdigest()


# ============================================================
# Basic proxy validation
# ============================================================

def validate_proxy(proxy: Any) -> bool:
    """
    Perform only the minimum validation required to treat
    an item as a proxy node.

    No actual node configuration is modified.
    """

    if not isinstance(proxy, dict):
        return False

    # type is required
    proxy_type = proxy.get("type")

    if not proxy_type:
        return False

    if not isinstance(proxy_type, str):
        return False

    # name is required for later OpenClash/Mihomo use
    name = proxy.get("name")

    if not name:
        return False

    if not isinstance(name, str):
        return False

    return True


# ============================================================
# Save candidates
# ============================================================

def save_candidates(nodes: list[dict]) -> None:
    """
    Save deduplicated candidate nodes.

    Only internal fields beginning with "_" are removed.
    Actual node configuration is otherwise preserved.
    """

    OUTPUT_FILE.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    output_nodes = []

    for proxy in nodes:
        output_proxy = {
            key: value
            for key, value in proxy.items()
            if not key.startswith("_")
        }

        output_nodes.append(output_proxy)

    document = {
        "proxies": output_nodes,
    }

    with OUTPUT_FILE.open(
        "w",
        encoding="utf-8",
        newline="\n",
    ) as f:
        yaml.safe_dump(
            document,
            f,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        )

    print(
        f"\nCandidate file written: "
        f"{OUTPUT_FILE.relative_to(ROOT)}"
    )

    print(
        f"Candidate nodes written: "
        f"{len(output_nodes):,}"
    )


# ============================================================
# Main
# ============================================================

def main() -> int:

    print("=" * 70)
    print("free-nodes - merge and deduplicate")
    print("=" * 70)

    # --------------------------------------------------------
    # Load sources
    # --------------------------------------------------------

    try:
        sources = load_sources()

    except Exception as exc:
        print(f"ERROR: {exc}")
        return 1

    enabled_sources = [
        source
        for source in sources
        if isinstance(source, dict)
        and source.get("enabled", True)
    ]

    if not enabled_sources:
        print("ERROR: no enabled sources.")
        return 1

    # --------------------------------------------------------
    # Collect nodes
    # --------------------------------------------------------

    all_nodes: list[dict] = []

    source_counts: dict[str, int] = {}
    source_invalid: dict[str, int] = {}

    for source in enabled_sources:

        name = source.get("name", "unknown")
        url = source.get("url")

        if not url:
            print(
                f"\n[{name}] ERROR: missing URL."
            )
            continue

        try:

            document = download_yaml(
                name,
                url,
            )

            proxies = document.get("proxies")

            if not isinstance(proxies, list):
                print(
                    f"ERROR: {name}: "
                    "no valid proxies list."
                )
                continue

            valid_count = 0
            invalid_count = 0

            for proxy in proxies:

                if not validate_proxy(proxy):
                    invalid_count += 1
                    continue

                # ------------------------------------------------
                # IMPORTANT:
                # Do not modify the actual node configuration.
                #
                # We only make a shallow copy so that internal
                # processing metadata cannot alter the parsed
                # source object.
                # ------------------------------------------------

                node = dict(proxy)

                # Internal source information.
                # This is NOT included in fingerprinting.
                node["_source"] = name

                all_nodes.append(node)

                valid_count += 1

            source_counts[name] = valid_count
            source_invalid[name] = invalid_count

            print(
                f"Valid nodes: {valid_count:,}"
            )

            print(
                f"Invalid nodes: {invalid_count:,}"
            )

        except requests.RequestException as exc:

            print(
                f"ERROR downloading source: {exc}"
            )

        except Exception as exc:

            print(
                f"ERROR processing source: {exc}"
            )

    # --------------------------------------------------------
    # Source summary
    # --------------------------------------------------------

    print("\n" + "=" * 70)
    print("SOURCE SUMMARY")
    print("=" * 70)

    for name in source_counts:

        print(
            f"{name:<16} "
            f"{source_counts[name]:>6,} valid   "
            f"{source_invalid[name]:>4,} invalid"
        )

    print("-" * 70)

    print(
        "Total valid nodes before deduplication: "
        f"{len(all_nodes):,}"
    )

    # --------------------------------------------------------
    # Deduplicate
    # --------------------------------------------------------

    unique_nodes: dict[str, dict] = {}

    duplicate_count = 0

    for proxy in all_nodes:

        fingerprint = build_fingerprint(proxy)

        if fingerprint in unique_nodes:

            duplicate_count += 1
            continue

        # Internal fingerprint is used only during processing.
        # It will be removed before candidates.yaml is written.
        proxy["_fingerprint"] = fingerprint

        unique_nodes[fingerprint] = proxy

    nodes = list(unique_nodes.values())

    print(
        f"Duplicate nodes removed: "
        f"{duplicate_count:,}"
    )

    print(
        f"Unique nodes after deduplication: "
        f"{len(nodes):,}"
    )

    # --------------------------------------------------------
    # Protocol statistics
    # --------------------------------------------------------

    protocol_counter = Counter(
        proxy.get("type", "unknown")
        for proxy in nodes
    )

    print("\nProtocols after deduplication:")

    for protocol, count in sorted(
        protocol_counter.items(),
        key=lambda item: (-item[1], item[0]),
    ):

        print(
            f"  {protocol:<16} "
            f"{count:,}"
        )

    # --------------------------------------------------------
    # Save candidates
    # --------------------------------------------------------

    save_candidates(nodes)

    # --------------------------------------------------------
    # Final result
    # --------------------------------------------------------

    print("\n" + "=" * 70)
    print("RESULT")
    print("=" * 70)

    print(
        f"Raw valid nodes:          "
        f"{len(all_nodes):,}"
    )

    print(
        f"Duplicates removed:      "
        f"{duplicate_count:,}"
    )

    print(
        f"Unique candidate nodes:  "
        f"{len(nodes):,}"
    )

    print(
        f"Output file:              "
        f"{OUTPUT_FILE.relative_to(ROOT)}"
    )

    print("=" * 70)

    return 0


# ============================================================
# Entry point
# ============================================================

if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3

"""
Merge and deduplicate free-node sources.

第二阶段负责：
1. 读取 config/sources.yaml
2. 下载所有启用的 YAML 来源
3. 提取 proxies
4. 基础清洗
5. 根据节点配置生成 fingerprint
6. 去除重复节点
7. 输出统计结果

暂时不：
- 使用 Mihomo
- 测试节点
- 测试 YouTube
- 排名节点
- 生成最终 free-nodes.yaml
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


ROOT = Path(__file__).resolve().parents[1]
CONFIG_FILE = ROOT / "config" / "sources.yaml"

USER_AGENT = (
    "Mozilla/5.0 "
    "(Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 "
    "(KHTML, like Gecko) "
    "Chrome/140.0 Safari/537.36"
)

REQUEST_TIMEOUT = 30


def load_sources() -> list[dict]:
    """Load source definitions."""

    if not CONFIG_FILE.exists():
        raise FileNotFoundError(
            f"Configuration file not found: {CONFIG_FILE}"
        )

    with CONFIG_FILE.open("r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    if not isinstance(config, dict):
        raise ValueError("sources.yaml must contain a YAML mapping.")

    sources = config.get("sources")

    if not isinstance(sources, list):
        raise ValueError(
            "sources.yaml must contain a 'sources' list."
        )

    return sources


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


def normalize_value(value: Any) -> Any:
    """
    Recursively normalize values so that the fingerprint
    is stable regardless of dictionary ordering.
    """

    if isinstance(value, dict):
        return {
            str(key): normalize_value(value[key])
            for key in sorted(value.keys(), key=str)
        }

    if isinstance(value, list):
        return [normalize_value(item) for item in value]

    return value


def build_fingerprint(proxy: dict) -> str:
    """
    Build a stable fingerprint from the node configuration.

    The node name is intentionally excluded because different
    sources may assign different names to the same node.
    """

    fingerprint_data = {
        key: value
        for key, value in proxy.items()
        if key != "name"
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


def clean_proxy(proxy: Any) -> dict | None:
    """Perform basic validation and return a cleaned proxy."""

    if not isinstance(proxy, dict):
        return None

    proxy_type = proxy.get("type")

    if not proxy_type:
        return None

    if not isinstance(proxy_type, str):
        return None

    cleaned = dict(proxy)

    # Normalize protocol name.
    cleaned["type"] = proxy_type.lower().strip()

    # A proxy should have a name for later OpenClash use.
    if not cleaned.get("name"):
        return None

    if not isinstance(cleaned["name"], str):
        return None

    return cleaned


def main() -> int:
    print("=" * 70)
    print("free-nodes - merge and deduplicate")
    print("=" * 70)

    try:
        sources = load_sources()
    except Exception as exc:
        print(f"ERROR: {exc}")
        return 1

    enabled_sources = [
        source
        for source in sources
        if source.get("enabled", True)
    ]

    if not enabled_sources:
        print("ERROR: no enabled sources.")
        return 1

    all_nodes: list[dict] = []

    source_counts = {}
    source_invalid = {}

    for source in enabled_sources:
        name = source.get("name", "unknown")
        url = source.get("url")

        if not url:
            print(f"\n[{name}] ERROR: missing URL.")
            continue

        try:
            document = download_yaml(name, url)

            proxies = document.get("proxies")

            if not isinstance(proxies, list):
                print("ERROR: no valid proxies list.")
                continue

            valid_count = 0
            invalid_count = 0

            for proxy in proxies:
                cleaned = clean_proxy(proxy)

                if cleaned is None:
                    invalid_count += 1
                    continue

                cleaned["_source"] = name

                all_nodes.append(cleaned)
                valid_count += 1

            source_counts[name] = valid_count
            source_invalid[name] = invalid_count

            print(f"Valid nodes: {valid_count:,}")
            print(f"Invalid nodes: {invalid_count:,}")

        except requests.RequestException as exc:
            print(f"ERROR downloading source: {exc}")

        except Exception as exc:
            print(f"ERROR processing source: {exc}")

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
    print(f"Total valid nodes before deduplication: {len(all_nodes):,}")

    # ------------------------------------------------------------
    # Deduplicate
    # ------------------------------------------------------------

    unique_nodes: dict[str, dict] = {}
    duplicate_count = 0

    for proxy in all_nodes:
        fingerprint = build_fingerprint(proxy)

        if fingerprint in unique_nodes:
            duplicate_count += 1
            continue

        proxy["_fingerprint"] = fingerprint
        unique_nodes[fingerprint] = proxy

    nodes = list(unique_nodes.values())

    print(f"Duplicate nodes removed: {duplicate_count:,}")
    print(f"Unique nodes after deduplication: {len(nodes):,}")

    # ------------------------------------------------------------
    # Protocol statistics
    # ------------------------------------------------------------

    protocol_counter = Counter(
        proxy["type"]
        for proxy in nodes
    )

    print("\nProtocols after deduplication:")

    for protocol, count in sorted(
        protocol_counter.items(),
        key=lambda item: (-item[1], item[0]),
    ):
        print(f"  {protocol:<16} {count:,}")

    print("\n" + "=" * 70)
    print("RESULT")
    print("=" * 70)
    print(f"Raw valid nodes:          {len(all_nodes):,}")
    print(f"Duplicates removed:      {duplicate_count:,}")
    print(f"Unique candidate nodes:  {len(nodes):,}")
    print("=" * 70)

    return 0


if __name__ == "__main__":
    sys.exit(main())

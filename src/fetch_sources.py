#!/usr/bin/env python3

"""
Download and export free-node YAML sources.

第一阶段只负责：

1. 读取 config/sources.yaml
2. 下载启用的节点源
3. 解析 YAML
4. 提取 proxies
5. 原样导出节点

注意：
- 不清洗节点
- 不修改节点字段
- 不修改字段值
- 不去重
- 不添加 fingerprint
- 不添加 source 等内部字段
- 不测速
- 不使用 Mihomo
"""

from __future__ import annotations

import sys
from pathlib import Path
from collections import Counter

import requests
import yaml


ROOT = Path(__file__).resolve().parents[1]
CONFIG_FILE = ROOT / "config" / "sources.yaml"
OUTPUT_DIR = ROOT / "data"

USER_AGENT = (
    "Mozilla/5.0 "
    "(Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 "
    "(KHTML, like Gecko) "
    "Chrome/140.0 Safari/537.36"
)

REQUEST_TIMEOUT = 30


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


def download_yaml(name: str, url: str) -> bytes:
    """Download one YAML source without modifying its content."""

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

    return response.content


def parse_yaml(data: bytes, name: str) -> dict:
    """Parse downloaded YAML."""

    try:
        text = data.decode("utf-8")
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


def inspect_proxies(document: dict, name: str) -> dict:
    """Inspect proxies without modifying them."""

    proxies = document.get("proxies")

    if proxies is None:
        raise ValueError(
            f"{name}: no 'proxies' section found."
        )

    if not isinstance(proxies, list):
        raise ValueError(
            f"{name}: 'proxies' is not a list."
        )

    protocol_counter = Counter()
    invalid = 0

    for proxy in proxies:

        if not isinstance(proxy, dict):
            invalid += 1
            continue

        proxy_type = proxy.get("type")

        if not proxy_type:
            invalid += 1
            continue

        protocol_counter[str(proxy_type).lower()] += 1

    print(f"Nodes: {len(proxies):,}")
    print(f"Invalid/basic malformed entries: {invalid:,}")

    if protocol_counter:

        print("Protocols:")

        for protocol, count in sorted(
            protocol_counter.items(),
            key=lambda item: (-item[1], item[0]),
        ):
            print(
                f"  {protocol:<16} {count:,}"
            )

    return {
        "total": len(proxies),
        "protocols": protocol_counter,
        "invalid": invalid,
    }


def export_proxies(
    proxies: list,
    name: str,
) -> Path:
    """
    Export proxies.

    这里唯一做的结构变化是：
    把源 YAML 中的 proxies 列表重新放进一个
    'proxies:' 根节点下。

    节点对象本身不进行任何字段修改。
    """

    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    output_file = (
        OUTPUT_DIR / f"{name}.yaml"
    )

    document = {
        "proxies": proxies
    }

    with output_file.open(
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
        f"Exported: "
        f"{output_file.relative_to(ROOT)}"
    )

    print(
        f"Exported nodes: "
        f"{len(proxies):,}"
    )

    return output_file


def main() -> int:

    print("=" * 70)
    print("free-nodes - raw source exporter")
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

    print(
        f"Configured sources: "
        f"{len(enabled_sources)}"
    )

    results = []

    for source in enabled_sources:

        name = source.get(
            "name",
            "unknown",
        )

        url = source.get("url")

        if not url:
            print(
                f"\n[{name}] ERROR: missing URL."
            )
            continue

        try:

            data = download_yaml(
                name,
                url,
            )

            document = parse_yaml(
                data,
                name,
            )

            proxies = document.get(
                "proxies"
            )

            if not isinstance(
                proxies,
                list,
            ):
                raise ValueError(
                    f"{name}: invalid proxies list."
                )

            result = inspect_proxies(
                document,
                name,
            )

            output_file = export_proxies(
                proxies,
                name,
            )

            results.append(
                {
                    "name": name,
                    "output": output_file,
                    **result,
                }
            )

        except requests.RequestException as exc:

            print(
                f"ERROR downloading source: "
                f"{exc}"
            )

        except Exception as exc:

            print(
                f"ERROR processing source: "
                f"{exc}"
            )

    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)

    total_nodes = 0

    for result in results:

        name = result["name"]
        total = result["total"]
        invalid = result["invalid"]

        total_nodes += total

        print(
            f"{name:<16} "
            f"{total:>6,} nodes   "
            f"{invalid:>4,} malformed   "
            f"{result['output'].relative_to(ROOT)}"
        )

    print("-" * 70)
    print(
        f"Raw nodes exported: "
        f"{total_nodes:,}"
    )

    print("=" * 70)

    if not results:

        print(
            "ERROR: no source was successfully processed."
        )

        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())

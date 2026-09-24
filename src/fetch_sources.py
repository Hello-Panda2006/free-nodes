```python
#!/usr/bin/env python3

"""
Download and inspect free-node YAML sources.

第一阶段只负责：
1. 读取 config/sources.yaml
2. 下载启用的节点源
3. 解析 YAML
4. 检查 proxies
5. 统计节点数量和协议类型

暂时不：
- 使用 Mihomo
- 测试节点
- 测试 YouTube
- 修改节点
- 生成最终 free-nodes.yaml
"""

from __future__ import annotations

import sys
from pathlib import Path
from collections import Counter

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
    """Load source definitions from config/sources.yaml."""

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


def download_yaml(name: str, url: str) -> bytes:
    """Download one YAML source."""

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
    """Inspect the proxies section without modifying it."""

    proxies = document.get("proxies")

    if proxies is None:
        print("ERROR: no 'proxies' section found.")
        return {
            "total": 0,
            "protocols": Counter(),
            "invalid": 0,
        }

    if not isinstance(proxies, list):
        print("ERROR: 'proxies' is not a list.")
        return {
            "total": 0,
            "protocols": Counter(),
            "invalid": 0,
        }

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

    total = len(proxies)

    print(f"Nodes: {total:,}")
    print(f"Invalid/basic malformed entries: {invalid:,}")

    if protocol_counter:
        print("Protocols:")

        for protocol, count in sorted(
            protocol_counter.items(),
            key=lambda item: (-item[1], item[0]),
        ):
            print(f"  {protocol:<16} {count:,}")

    return {
        "total": total,
        "protocols": protocol_counter,
        "invalid": invalid,
    }


def main() -> int:
    print("=" * 70)
    print("free-nodes - source inspection")
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

    print(f"Configured sources: {len(enabled_sources)}")

    results = []

    for source in enabled_sources:
        name = source.get("name", "unknown")
        url = source.get("url")

        if not url:
            print(f"\n[{name}] ERROR: missing URL.")
            continue

        try:
            data = download_yaml(name, url)
            document = parse_yaml(data, name)
            result = inspect_proxies(document, name)

            results.append(
                {
                    "name": name,
                    **result,
                }
            )

        except requests.RequestException as exc:
            print(f"ERROR downloading source: {exc}")

        except Exception as exc:
            print(f"ERROR processing source: {exc}")

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
            f"{invalid:>4,} malformed"
        )

    print("-" * 70)
    print(f"Raw nodes across sources: {total_nodes:,}")
    print("=" * 70)

    if not results:
        print("ERROR: no source was successfully processed.")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
```

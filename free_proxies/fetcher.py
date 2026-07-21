"""Fetch the subscription sources listed in subscriptions.txt and parse out nodes."""

from __future__ import annotations

from pathlib import Path

import requests

from .parser import dedupe, parse_subscription

UA = "clash.meta/1.19; free-proxies-validator"


def load_sources(path: str | Path) -> list[str]:
    urls = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            urls.append(line)
    return urls


def _fetch_one(url: str, timeout: int, retries: int) -> list[dict] | None:
    last = None
    for _ in range(retries):
        try:
            resp = requests.get(url, timeout=timeout, headers={"User-Agent": UA})
            resp.raise_for_status()
            return parse_subscription(resp.text)
        except requests.RequestException as e:
            last = e  # jsDelivr/CDN occasionally resets TLS; one retry usually fixes it
    print(f"  [failed] {url} -> {type(last).__name__}")
    return None


def fetch_all(sources: list[str], timeout: int = 20, retries: int = 3) -> list[dict]:
    all_nodes: list[dict] = []
    for url in sources:
        if url.startswith("bjedu:"):  # special source: go through the bjedu quick-test API
            from .bjedu import fetch_bjedu
            try:
                subs = url[len("bjedu:"):].split(",") if url != "bjedu:" else None
                all_nodes.extend(fetch_bjedu(subs))
            except requests.RequestException as e:
                print(f"  [failed] {url} -> {type(e).__name__}")
            continue
        nodes = _fetch_one(url, timeout, retries)
        if nodes is None:
            continue
        print(f"  [{len(nodes):>4} nodes] {url}")
        all_nodes.extend(nodes)
    unique = dedupe(all_nodes)
    print(f"{len(all_nodes)} nodes total, {len(unique)} after dedupe")
    return unique

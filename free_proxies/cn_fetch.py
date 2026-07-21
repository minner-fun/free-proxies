"""Fetch generic free HTTP/SOCKS5 proxy lists (cn_sources.txt).

Unlike provider subscriptions: these sources are plain-text proxy IP lists that requests
can connect to directly, without mihomo protocol conversion. They are parsed into proxy
dicts matching mihomo's format (type=http/socks5), so the good_cn.yaml produced by
validation can be reused as-is by the existing ProxyPool / serve (just change the port).
"""

from __future__ import annotations

import re
from pathlib import Path

import requests

from .fetcher import load_sources

UA = "Mozilla/5.0 (free-proxies cn-fetcher)"

# ip:port, port 1-65535; loosely matches the first address on a line
# (tolerates lines carrying extra fields such as country / latency)
_ADDR = re.compile(r"(\d{1,3}(?:\.\d{1,3}){3}):(\d{1,5})")
_SCHEME = re.compile(r"^\s*(https?|socks5h?|socks4)://", re.I)


def parse_proxy_list(text: str, default_type: str = "http") -> list[dict]:
    """Parse a block of plain-text proxy list into mihomo proxy dicts.

    Accepts: ``http://1.2.3.4:8080`` / ``socks5://1.2.3.4:1080`` / bare ``1.2.3.4:8080`` /
    lines with extra fields (``1.2.3.4:8080 # US``). socks4 is normalized to socks5
    (mihomo does not distinguish them; only reachability matters).
    """
    out: list[dict] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m_scheme = _SCHEME.match(line)
        ptype = default_type
        if m_scheme:
            s = m_scheme.group(1).lower()
            ptype = "socks5" if s.startswith("socks") else "http"
        m = _ADDR.search(line)
        if not m:
            continue
        ip, port = m.group(1), int(m.group(2))
        if not (0 < port < 65536) or any(int(o) > 255 for o in ip.split(".")):
            continue
        out.append({"name": f"{ptype}-{ip}-{port}", "type": ptype, "server": ip, "port": port})
    return out


def _type_hint(url: str) -> str:
    """Guess the default protocol from the source URL (socks5 if it mentions socks, else http)."""
    return "socks5" if re.search(r"socks5?", url, re.I) else "http"


def _fetch_one(url: str, timeout: int, retries: int) -> list[dict] | None:
    last = None
    for _ in range(retries):
        try:
            resp = requests.get(url, timeout=timeout, headers={"User-Agent": UA})
            resp.raise_for_status()
            return parse_proxy_list(resp.text, default_type=_type_hint(url))
        except requests.RequestException as e:
            last = e
    print(f"  [failed] {url} -> {type(last).__name__}")
    return None


def dedupe(proxies: list[dict]) -> list[dict]:
    """Dedupe by (type, server, port)."""
    seen: set[tuple] = set()
    out: list[dict] = []
    for p in proxies:
        key = (p["type"], p["server"], p["port"])
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def fetch_all(sources: list[str], timeout: int = 20, retries: int = 3) -> list[dict]:
    all_proxies: list[dict] = []
    for url in sources:
        proxies = _fetch_one(url, timeout, retries)
        if proxies is None:
            continue
        print(f"  [{len(proxies):>5} proxies] {url}")
        all_proxies.extend(proxies)
    unique = dedupe(all_proxies)
    print(f"{len(all_proxies)} proxies total, {len(unique)} after dedupe")
    return unique


__all__ = ["load_sources", "parse_proxy_list", "dedupe", "fetch_all"]

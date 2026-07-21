"""Fetch candidate nodes from the quick-test API of if.bjedu.tech.

This API aggregates several open-source subscription repos and returns tens of
thousands of nodes at once (with their original share links).
Note: its own latency / status fields are unreliable (about 97% have latency=0),
so only the `url` field is used as a candidate source; real availability always
goes through this project's own mihomo validation.
"""

from __future__ import annotations

import requests

from .parser import dedupe, parse_link

API = "https://if.bjedu.tech/api/quick-test"
DEFAULT_SUBS = [
    "automerge", "v2rayfree", "freeservers",
    "proxypool", "subcrawler", "freeservers2",
]
HEADERS = {
    "content-type": "application/json",
    "origin": "https://if.bjedu.tech",
    "referer": "https://if.bjedu.tech/",
    "user-agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"),
}


def fetch_bjedu(subs: list[str] | None = None, timeout: int = 120) -> list[dict]:
    """Return parsed and deduped mihomo node dicts (availability not validated)."""
    payload = {
        "timeout": 8000,
        "concurrent": 15,
        "enableIspTest": False,
        "selectedIsps": None,
        "selectedSubs": subs or DEFAULT_SUBS,
    }
    resp = requests.post(API, json=payload, headers=HEADERS, timeout=timeout)
    resp.raise_for_status()
    results = resp.json().get("results", [])
    nodes = [n for n in (parse_link(r.get("url", "")) for r in results) if n]
    unique = dedupe(nodes)
    print(f"  [bjedu] raw {len(results)}, parsed {len(nodes)}, deduped {len(unique)}")
    return unique

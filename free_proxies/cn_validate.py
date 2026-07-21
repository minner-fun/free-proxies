"""Validate generic HTTP/SOCKS5 proxies: concurrent direct requests + exit IP + exit country.

No mihomo needed: requests can connect to these proxies directly, so we just use them as
proxies for a geo endpoint and get "reachability + latency + exit IP + exit country" in a
single call. Uses ip-api.com (http, free; when reached through a proxy the source is the
proxy IP, so the local rate limit does not apply).

Output mirrors the provider line:
- data/good_cn.yaml -- working proxies (mihomo proxy dicts, sorted by latency ascending),
  readable by ProxyPool as-is
- data/report_cn.json -- details (protocol / exit IP / country / latency)
Pass --cn-only to keep only proxies whose exit is in mainland China.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
# Free tier is http only; accessed through the proxy, so the source is the proxy IP
# and the local 45/min limit is not triggered
GEO_URL = "http://ip-api.com/json/?fields=status,message,query,countryCode,country"


def _proxies_for(p: dict) -> dict:
    if p["type"] == "socks5":
        url = f"socks5h://{p['server']}:{p['port']}"  # h: let the proxy resolve DNS
    else:
        url = f"http://{p['server']}:{p['port']}"
    return {"http": url, "https": url}


def _probe(p: dict, timeout_ms: int) -> dict | None:
    """Request the geo endpoint through the proxy.

    Returns the node with _delay/_exit_ip/_country on success, None on failure.
    """
    t0 = time.monotonic()
    try:
        r = requests.get(GEO_URL, proxies=_proxies_for(p),
                         timeout=timeout_ms / 1000, headers={"User-Agent": "curl/8"})
        if r.status_code != 200:
            return None
        data = r.json()
        if data.get("status") != "success":
            return None
    except (requests.RequestException, ValueError):
        return None
    p = dict(p)
    p["_delay"] = int((time.monotonic() - t0) * 1000)
    p["_exit_ip"] = data.get("query")
    p["_country"] = data.get("countryCode")
    p["_country_name"] = data.get("country")
    return p


def validate(
    proxies: list[dict],
    timeout_ms: int = 8000,
    workers: int = 128,
    cn_only: bool = False,
) -> list[dict]:
    total = len(proxies)
    print(f"Validating {total} proxies "
          f"(concurrency {workers}, timeout {timeout_ms}ms, exit probe via ip-api)")
    good: list[dict] = []
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(_probe, p, timeout_ms): p for p in proxies}
        for fut in as_completed(futures):
            done += 1
            res = fut.result()
            if res is not None:
                good.append(res)
            if done % 1000 == 0 or done == total:
                print(f"  progress {done}/{total}, working so far {len(good)}")

    good.sort(key=lambda n: n["_delay"])
    dist = Counter(n["_country"] for n in good)
    print("Exit country distribution (Top 10):",
          ", ".join(f"{c}:{n}" for c, n in dist.most_common(10)) or "none")

    if cn_only:
        good = [n for n in good if n["_country"] == "CN"]
        print(f"--cn-only: keeping only mainland China exits, {len(good)} left")

    report = [
        {
            "name": n["name"], "type": n["type"],
            "server": n["server"], "port": n["port"],
            "delay_ms": n["_delay"], "exit_ip": n.get("_exit_ip"),
            "country": n.get("_country"), "country_name": n.get("_country_name"),
        }
        for n in good
    ]
    clean = [{k: v for k, v in n.items() if not k.startswith("_")} for n in good]

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    import yaml
    (DATA_DIR / "good_cn.yaml").write_text(
        yaml.safe_dump({"proxies": clean}, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    (DATA_DIR / "report_cn.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{len(good)}/{total} proxies working, "
          f"written to data/good_cn.yaml and data/report_cn.json")
    return good

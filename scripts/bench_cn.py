"""Benchmark the CN/general proxy pool: rotate proxies over bulk IP-lookup requests and report proxy health.

Each request rotates through one proxy from good_cn.yaml (direct http/socks5, so the
exit naturally changes every time). Runs N requests concurrently and summarizes
success rate / unique exit IPs / country distribution / latency percentiles / failure reasons.

Usage:
    python scripts/bench_cn.py                          # default 1000 runs, ip-api
    python scripts/bench_cn.py --runs 500 --workers 80
    python scripts/bench_cn.py --service cipcc          # use https://www.cip.cc/
    python scripts/bench_cn.py --good data/good.yaml    # can also benchmark the provider line (needs serve/pool forwarding; normally use good_cn)
"""

from __future__ import annotations

import argparse
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent

SERVICES = {
    # name: (url, ua) -- parsing happens in the parse_* functions
    "ipapi": ("http://ip-api.com/json/?fields=status,query,countryCode,country,city,isp",
              "Mozilla/5.0 (bench_cn)"),
    "cipcc": ("https://www.cip.cc/", "curl/8.4.0"),  # only a curl UA returns plain text
}

_CIP_IP = re.compile(r"IP\s*[:：]\s*([0-9.]+)")
_CIP_ADDR = re.compile(r"地址\s*[:：]\s*(.+)")


def load_proxies(good_yaml: Path) -> list[dict]:
    doc = yaml.safe_load(good_yaml.read_text(encoding="utf-8")) or {}
    proxies = doc.get("proxies") or []
    if not proxies:
        raise SystemExit(f"{good_yaml} has no proxies; run cn-all first to generate it")
    return proxies


def proxies_dict(p: dict) -> dict:
    if p["type"] == "socks5":
        url = f"socks5h://{p['server']}:{p['port']}"
    else:
        url = f"http://{p['server']}:{p['port']}"
    return {"http": url, "https": url}


def parse_ipapi(r: requests.Response) -> tuple[str, str] | None:
    d = r.json()
    if d.get("status") != "success":
        return None
    return d.get("query"), d.get("countryCode") or "?"


def parse_cipcc(r: requests.Response) -> tuple[str, str] | None:
    text = r.text
    m = _CIP_IP.search(text)
    if not m:
        return None
    ip = m.group(1)
    addr = _CIP_ADDR.search(text)
    # The address looks like "中国  北京 电信" / "美国 ..."; take the first word as the country label
    country = addr.group(1).split()[0] if addr else "?"
    return ip, country


PARSERS = {"ipapi": parse_ipapi, "cipcc": parse_cipcc}


def one_request(p: dict, url: str, ua: str, parser, timeout_ms: int) -> dict:
    t0 = time.monotonic()
    try:
        r = requests.get(url, proxies=proxies_dict(p),
                         timeout=timeout_ms / 1000, headers={"User-Agent": ua})
        elapsed = int((time.monotonic() - t0) * 1000)
        if r.status_code != 200:
            return {"ok": False, "err": f"HTTP {r.status_code}", "elapsed": elapsed}
        parsed = parser(r)
        if parsed is None:
            return {"ok": False, "err": "parse failed / unexpected response", "elapsed": elapsed}
        ip, country = parsed
        return {"ok": True, "ip": ip, "country": country, "elapsed": elapsed,
                "proxy": f"{p['type']}://{p['server']}:{p['port']}"}
    except requests.RequestException as e:
        return {"ok": False, "err": type(e).__name__,
                "elapsed": int((time.monotonic() - t0) * 1000)}


def pct(sorted_vals: list[int], q: float) -> int:
    if not sorted_vals:
        return 0
    i = min(len(sorted_vals) - 1, int(q * len(sorted_vals)))
    return sorted_vals[i]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--good", default=PROJECT_ROOT / "data" / "good_cn.yaml")
    ap.add_argument("--runs", type=int, default=1000)
    ap.add_argument("--workers", type=int, default=50)
    ap.add_argument("--timeout", type=int, default=8000, help="per-request timeout (ms)")
    ap.add_argument("--service", choices=list(SERVICES), default="ipapi")
    args = ap.parse_args()

    proxies = load_proxies(Path(args.good))
    url, ua = SERVICES[args.service]
    parser = PARSERS[args.service]
    print(f"Pool: {len(proxies)} proxies | target: {args.service} ({url})")
    print(f"Running {args.runs} requests, concurrency {args.workers}, timeout {args.timeout}ms, rotating one proxy per request\n")

    # Pre-assign tasks, round-robin for even rotation (if runs > proxy count, proxies are reused)
    tasks = [proxies[i % len(proxies)] for i in range(args.runs)]

    results: list[dict] = []
    t_start = time.monotonic()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = [ex.submit(one_request, p, url, ua, parser, args.timeout) for p in tasks]
        done = 0
        for fut in as_completed(futs):
            results.append(fut.result())
            done += 1
            if done % 100 == 0 or done == args.runs:
                ok = sum(1 for r in results if r["ok"])
                print(f"  progress {done}/{args.runs}, succeeded {ok}", flush=True)
    wall = time.monotonic() - t_start

    ok = [r for r in results if r["ok"]]
    bad = [r for r in results if not r["ok"]]
    ok_delays = sorted(r["elapsed"] for r in ok)
    uniq_ip = {r["ip"] for r in ok}
    countries = Counter(r["country"] for r in ok)
    errors = Counter(r["err"] for r in bad)

    print("\n" + "=" * 56)
    print(f"Total requests {args.runs}  |  succeeded {len(ok)} ({len(ok)/args.runs*100:.1f}%)  |  failed {len(bad)}")
    print(f"Elapsed {wall:.1f}s  |  throughput {args.runs/wall:.1f} req/s")
    print(f"Unique exit IPs: {len(uniq_ip)}")
    if ok_delays:
        print(f"Success latency (ms)  p50={pct(ok_delays,0.5)}  p90={pct(ok_delays,0.9)}  "
              f"p99={pct(ok_delays,0.99)}  min={ok_delays[0]}  max={ok_delays[-1]}")
    print("\nTop 10 exit countries/regions (successful requests):")
    for c, n in countries.most_common(10):
        print(f"  {c:<8} {n:>4}  ({n/len(ok)*100:.0f}%)" if ok else f"  {c} {n}")
    print("\nFailure reason distribution:")
    for e, n in errors.most_common():
        print(f"  {e:<24} {n:>4}  ({n/args.runs*100:.0f}%)")
    print("=" * 56)


if __name__ == "__main__":
    main()

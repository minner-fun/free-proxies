"""Quickly probe a batch of subscription URLs for current validity, reporting how many nodes each one yields.

    uv run python scripts/probe_subs.py data/candidate_subs.txt
"""

import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from free_proxies.parser import parse_subscription  # noqa: E402

UA = "clash.meta/1.19; free-proxies-validator"


def probe(url: str):
    last = "?"
    for attempt in range(2):  # simple retry to work around occasional rate limiting from GitHub etc.
        try:
            r = requests.get(url, timeout=25, headers={"User-Agent": UA})
            r.raise_for_status()
            n = len(parse_subscription(r.text))
            return url, n, "ok" if n else "0 nodes / unparseable"
        except requests.RequestException as e:
            last = f"{type(e).__name__}"
            if isinstance(e, requests.HTTPError) and e.response is not None:
                last += f"({e.response.status_code})"
    return url, 0, last


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else "data/candidate_subs.txt"
    urls = [l.strip() for l in Path(src).read_text().splitlines() if l.strip()]
    with ThreadPoolExecutor(max_workers=4) as ex:
        results = list(ex.map(probe, urls))
    results.sort(key=lambda x: -x[1])
    good = []
    for url, n, status in results:
        mark = "✓" if n else "✗"
        print(f"{mark} {n:>5}  {status:<20} {url}")
        if n:
            good.append(url)
    print(f"\n{len(good)}/{len(urls)} sources valid")
    Path("data/valid_subs.txt").write_text("\n".join(good) + "\n")
    print("Valid sources written to data/valid_subs.txt")


if __name__ == "__main__":
    main()

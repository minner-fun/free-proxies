"""Example: scrape with requests through the pool of validated free nodes.

First run `uv run python -m free_proxies all` to generate data/good.yaml, then:
    uv run python example_scrape.py
"""

import requests

from free_proxies.pool import ProxyPool

with ProxyPool(top_n=10) as pool:  # use only the 10 lowest-latency nodes, exit rotates
    print(f"Proxy pool ready: {len(pool.node_names)} nodes, "
          f"local entry {pool.requests_proxies['http']}")
    for i in range(5):
        try:
            ip = requests.get(
                "https://api.ipify.org",
                proxies=pool.requests_proxies,
                timeout=15,
            ).text.strip()
            print(f"Request {i + 1}: exit IP = {ip}")
        except requests.RequestException as e:
            print(f"Request {i + 1}: failed ({type(e).__name__})")

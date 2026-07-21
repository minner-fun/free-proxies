# free-proxies

Collect free proxy nodes from public sources, validate that they actually work, and expose the
working ones on a local proxy port that `requests` can use directly for scraping.

Supported protocols: vmess / vless / trojan / shadowsocks / hysteria2. Connections are handled by
the [mihomo](https://github.com/MetaCubeX/mihomo) (Clash Meta) core; Python only fetches
subscriptions, parses share links, and drives mihomo's REST API for bulk latency tests and node
switching.

## Why mihomo is needed

`requests` can only speak http/socks proxies, but nearly all free nodes use protocols like
vmess/vless/trojan that `requests` cannot connect to directly. mihomo converts all of them into a
single local `mixed-port` (http+socks), and `requests` just connects to that local port.

```
subscription sources -> parsed nodes -> mihomo bulk latency test -> working nodes
                     -> mihomo local port -> requests
```

## Install

Dependencies are managed with uv. The mihomo binary lives in `bin/` (v1.19.28).

```bash
uv sync
bash scripts/setup.sh    # installs deps and downloads the mihomo binary (Linux)
```

> On Windows, `scripts/setup.sh` does not apply: create a venv, `pip install -e .`, and download
> the Windows mihomo build to `bin/mihomo.exe` manually. `MIHOMO_BIN` picks the right filename
> per platform automatically.

## Usage

1. Edit `subscriptions.txt`, one subscription URL per line (a few free GitHub sources are
   preloaded; they expire constantly, so add and remove as needed).

2. Fetch + validate:

```bash
uv run python -m free_proxies all           # fetch + validate in one step
# or run them separately:
uv run python -m free_proxies fetch         # fetch subscriptions -> data/nodes.yaml
uv run python -m free_proxies validate      # validate           -> data/good.yaml + data/report.json
```

Common options:

```bash
uv run python -m free_proxies validate \
    --test-url https://www.google.com/generate_204 \  # change the test target (default: gstatic)
    --timeout 5000 \                                   # per-node timeout (ms)
    --workers 128 \                                    # concurrency
    --batch-size 4000 \                                # nodes per validation batch
    --check-ip 10                                      # re-check the exit IP of the top 10 with requests
```

> When there are a lot of nodes (tens of thousands — see the bjedu source below), validation runs
> in **batches**: one dedicated mihomo instance per batch. This keeps a single instance from
> getting slow and isolates malformed-node failures inside a batch (bad nodes are dropped
> automatically and the batch retries).

3. Use the node pool from your scraping code:

```python
import requests
from free_proxies.pool import ProxyPool

with ProxyPool(top_n=10) as pool:          # use the 10 lowest-latency nodes, exit rotates automatically
    r = requests.get("https://example.com",
                     proxies=pool.requests_proxies, timeout=15)
    print(r.status_code)
```

See `example_scrape.py` (running it shows the exit IP rotating between requests).

You can also run the pool as a standing local proxy for any program:

```bash
uv run python -m free_proxies run --top 10   # listens on 127.0.0.1:7890
```

### Long-running service mode (high availability)

`serve` adds **health checks** and **scheduled refresh** on top of `run`, which is what you want
for a proxy that stays up on a server. The two lines (regions) each run as their own process on
their own port:

```bash
uv run python -m free_proxies serve                  # provider line (default) -> 127.0.0.1:7890
uv run python -m free_proxies serve --region cn      # generic HTTP/SOCKS5 line -> 127.0.0.1:7891

# Common options (same for both lines):
uv run python -m free_proxies serve --region cn \
    --health-every 5 \      # test the pool every 5 min; evict after 2 consecutive failures (hot reload, no dropped connections)
    --refresh-every 180 \   # re-fetch + re-validate all sources every 3 hours, replacing the pool
    --min-nodes 20 \        # refresh early when fewer than 20 nodes are alive
    --cn-only               # cn line only: keep mainland China exits only
```

Both lines share the health check and hot-reload logic; only the refresh backend differs (the
provider line validates through mihomo, the generic line validates with direct requests). Each
uses its own workdir and ports, so they never interfere. On first start, if the corresponding
`good_*.yaml` is missing, a full refresh runs automatically.

For server deployment see `scripts/free-proxies@.service` (a systemd **template** unit: bring up
`free-proxies@overseas` and/or `free-proxies@cn`, with enable-on-boot and auto-restart on crash).

Connecting to the long-running service from your scraper (pick the port per target site):

```python
from free_proxies.service import local_proxies
requests.get("https://example.com", proxies=local_proxies(7890), timeout=15)  # provider line
requests.get("http://example.cn",  proxies=local_proxies(7891), timeout=15)   # generic line
```

## Generic HTTP/SOCKS5 proxy line (IP rotation for scraping)

A second, independent line alongside the provider line: it fetches public HTTP/SOCKS5 proxy IP
lists (`cn_sources.txt`) and validates them with direct requests, **without mihomo protocol
conversion**. This is the line to use when you need to rotate IPs frequently to avoid blocks.

```bash
uv run python -m free_proxies cn-all                 # fetch + validate in one step
# or separately:
uv run python -m free_proxies cn-fetch               # fetch lists -> data/nodes_cn.yaml
uv run python -m free_proxies cn-validate            # validate    -> data/good_cn.yaml + report_cn.json
uv run python -m free_proxies cn-validate --cn-only  # keep only proxies exiting in mainland China
```

Validation hits ip-api.com once per proxy to get connectivity, exit IP, and exit country in a
single request. The resulting `good_cn.yaml` uses the same mihomo proxy-dict format as the
provider line's `good.yaml`, so `ProxyPool` / `serve` can consume it directly:

```python
from free_proxies.pool import ProxyPool
with ProxyPool(good_yaml="data/good_cn.yaml", mixed_port=7891, api_port=9091, top_n=20) as pool:
    r = requests.get("http://example.com", proxies=pool.requests_proxies, timeout=15)
```

> Note: exits of generic free proxies are spread worldwide and **mainland China exits are rare**
> (measured: only a few dozen CN out of ~7000), mostly US/GB/HK instead. If your target site does
> not require a CN exit, just use the full pool for IP rotation; use `--cn-only` when it does.
> The yield is around 15% (far better than the provider line), but these proxies are equally
> short-lived and vary in anonymity — use them for non-sensitive scraping only.

## Benchmarking the pool

`scripts/bench_cn.py` rotates through the pool over N requests and reports success rate, unique
exit IPs, country distribution, latency percentiles, and failure reasons:

```bash
python scripts/bench_cn.py --runs 1000 --workers 50   # default backend: ip-api
python scripts/bench_cn.py --service cipcc            # use https://www.cip.cc/ instead
```

> Different IP geolocation databases disagree substantially about which exits count as "China".
> This project treats ip-api's `countryCode` as authoritative; cip.cc will report a much higher
> share of Chinese exits for the same proxies. Pick one and state which you mean.

## Outputs

- `data/nodes.yaml` / `data/nodes_cn.yaml` — all fetched and deduped nodes (provider / generic)
- `data/good.yaml` / `data/good_cn.yaml` — validated working nodes, sorted by latency; this is
  what `ProxyPool` reads
- `data/report.json` / `data/report_cn.json` — reports on working nodes (protocol / server /
  latency / exit IP / country)

## Validate from where you will actually use it

The single most surprising result from running this: **the same sources yielded 1974 working
nodes when validated from a US VPS, but only 232 when validated from a machine in China — an
8.5x difference.** Most nodes are perfectly healthy and simply unreachable from certain
networks.

So validation is not a property of the node list, it is a property of the node list *plus the
place you test from*. Run `validate` on the machine that will actually use the proxies. A yield
measured somewhere else — especially from behind the restriction you are trying to route
around — tells you very little.

## Notes and caveats

- Free nodes are low quality and short-lived: of ~1900 nodes, typically only a few dozen work at
  any given moment. Run `validate` before scraping, or re-run it periodically.
- `ProxyPool(rotate=True)` (the default) uses mihomo's `load-balance` with round-robin, so every
  new connection gets a different exit. With `rotate=False` a single node is pinned and you can
  switch manually via `pool.select(name)`.
- Occasional SSL errors from an exit IP are just free-node instability and are expected; add
  retries in your scraping code.
- These are public free nodes operated by third parties. They are suitable only for
  non-sensitive data collection — never send private data or account credentials through them.

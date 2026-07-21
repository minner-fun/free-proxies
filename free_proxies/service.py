"""Long-running proxy service: ProxyPool + scheduled refresh + runtime health check.

    uv run python -m free_proxies serve                 # provider line (default, 7890)
    uv run python -m free_proxies serve --region cn      # generic HTTP/SOCKS5 line (7891)

On top of `run`, two things are added to keep the pool usable long term
("high availability"):

1. **Health check** (every 5 minutes by default): uses mihomo's group delay API to run
   a concurrent speed test over every node in the pool; nodes that fail N times in a row
   are evicted (hot reload, so the port stays up and connections stay alive).
2. **Scheduled refresh** (every 3 hours by default, or triggered early when the pool
   shrinks below --min-nodes): re-fetch + re-validate all sources, then replace the whole
   pool with the new set of usable nodes.

Both lines (region) share the health check + hot reload logic (both are mihomo pools);
only the refresh backend differs:
- `overseas`: provider subscriptions -> mihomo validation (side-channel `port+10000`
  instance) -> good.yaml
- `cn`      : generic proxy list -> direct requests validation (no port used) -> good_cn.yaml

Each line runs its own process, listens on its own port and uses its own workdir, so they
do not interfere. On first start, if the matching good_*.yaml is missing, a full refresh
runs before the service comes up.
"""

from __future__ import annotations

import time
import traceback
from pathlib import Path

import requests
import yaml

from .mihomo import Mihomo, build_config

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"

# region -> (default subscription file, default good file)
_REGION_DEFAULTS = {
    "overseas": (PROJECT_ROOT / "subscriptions.txt", DATA_DIR / "good.yaml"),
    "cn": (PROJECT_ROOT / "cn_sources.txt", DATA_DIR / "good_cn.yaml"),
}

# Refresh early when below min_nodes, but keep at least this gap between two refreshes so
# we do not refresh non-stop when the sources dry up
MIN_REFRESH_GAP = 30 * 60


def _log(region: str, msg: str) -> None:
    print(time.strftime("[%m-%d %H:%M:%S]"), f"[{region}]", msg, flush=True)


class ProxyService:
    def __init__(
        self,
        region: str = "overseas",
        sources: str | Path | None = None,
        good_yaml: str | Path | None = None,
        mixed_port: int = 7890,
        api_port: int = 9090,
        top_n: int | None = None,
        test_url: str = "https://www.gstatic.com/generate_204",
        timeout_ms: int = 5000,
        workers: int = 64,
        batch_size: int = 4000,
        cn_only: bool = False,
        refresh_every: int = 180 * 60,
        health_every: int = 5 * 60,
        max_fails: int = 2,
        min_nodes: int = 20,
    ):
        if region not in _REGION_DEFAULTS:
            raise ValueError(f"unknown region: {region} (choose from {list(_REGION_DEFAULTS)})")
        def_sources, def_good = _REGION_DEFAULTS[region]
        self.region = region
        self.sources = sources or def_sources
        self.good_yaml = Path(good_yaml or def_good)
        self.workdir = DATA_DIR / f"pool-{region}"
        self.mixed_port = mixed_port
        self.api_port = api_port
        self.top_n = top_n
        self.test_url = test_url
        self.timeout_ms = timeout_ms
        self.workers = workers
        self.batch_size = batch_size
        self.cn_only = cn_only
        self.refresh_every = refresh_every
        self.health_every = health_every
        self.max_fails = max_fails
        self.min_nodes = min_nodes

        self.proxies: list[dict] = []          # nodes currently in the pool
        self.fails: dict[str, int] = {}        # node name -> consecutive failure count
        self.mihomo: Mihomo | None = None
        self.last_refresh = 0.0

    def _log(self, msg: str) -> None:
        _log(self.region, msg)

    # ---------- node set management ----------

    def _load_good(self) -> list[dict]:
        if not self.good_yaml.exists():
            return []
        doc = yaml.safe_load(self.good_yaml.read_text(encoding="utf-8")) or {}
        proxies = doc.get("proxies") or []
        return proxies[: self.top_n] if self.top_n else proxies

    def _apply(self, proxies: list[dict]) -> None:
        """Apply the node set to the running mihomo instance (hot reload)."""
        self.proxies = proxies
        config = build_config(
            proxies, mixed_port=self.mixed_port, api_port=self.api_port,
            group_type="load-balance",
        )
        self.mihomo.reload(config)

    def _persist_eviction(self, evicted: set[str]) -> None:
        """Sync evictions back to good_*.yaml so dead nodes do not come back on restart."""
        doc = yaml.safe_load(self.good_yaml.read_text(encoding="utf-8")) or {}
        kept = [p for p in doc.get("proxies") or [] if p["name"] not in evicted]
        self.good_yaml.write_text(
            yaml.safe_dump({"proxies": kept}, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )

    # ---------- refresh ----------

    def _refresh(self) -> None:
        """Re-fetch + re-validate; on success replace the whole pool, on failure keep it."""
        self.last_refresh = time.time()
        if self.region == "cn":
            self._refresh_cn()
        else:
            self._refresh_overseas()

    def _refresh_overseas(self) -> None:
        from .fetcher import fetch_all, load_sources
        from .validator import validate

        self._log("refresh started: fetching provider subscriptions...")
        nodes = fetch_all(load_sources(self.sources))
        if not nodes:
            self._log("refresh aborted: no nodes fetched, keeping the current pool")
            return
        self._log(f"refresh: validating {len(nodes)} nodes "
                  f"(side-channel port {self.mixed_port + 10000})...")
        good = validate(
            nodes, test_url=self.test_url, timeout_ms=self.timeout_ms,
            workers=self.workers, check_ip=0,
            mixed_port=self.mixed_port + 10000, api_port=self.api_port + 10000,
            batch_size=self.batch_size,
        )
        self._after_refresh(good)

    def _refresh_cn(self) -> None:
        from .cn_fetch import fetch_all, load_sources
        from .cn_validate import validate

        self._log("refresh started: fetching generic proxy lists...")
        nodes = fetch_all(load_sources(self.sources))
        if not nodes:
            self._log("refresh aborted: no proxies fetched, keeping the current pool")
            return
        self._log(f"refresh: validating {len(nodes)} proxies directly via requests...")
        good = validate(
            nodes, timeout_ms=self.timeout_ms, workers=self.workers,
            cn_only=self.cn_only,
        )
        self._after_refresh(good)

    def _after_refresh(self, good: list[dict]) -> None:
        if not good:
            self._log("refresh aborted: validation yielded 0 usable, keeping the current pool")
            return
        proxies = self._load_good()
        if self.mihomo:
            self._apply(proxies)
        self.fails = {}
        self._log(f"refresh done: pool replaced with {len(proxies)} nodes")

    # ---------- health check ----------

    def _health_check(self) -> int:
        """Test one round of pool nodes, evict repeat failures; return the alive count."""
        delays = self.mihomo.group_delay("POOL", self.test_url, self.timeout_ms)
        evicted: set[str] = set()
        for p in self.proxies:
            name = p["name"]
            if name in delays:
                self.fails.pop(name, None)
            else:
                self.fails[name] = self.fails.get(name, 0) + 1
                if self.fails[name] >= self.max_fails:
                    evicted.add(name)
        if evicted:
            kept = [p for p in self.proxies if p["name"] not in evicted]
            if not kept:
                # An empty group cannot be hot reloaded; keep the old config running and
                # let min_nodes trigger a refresh as the fallback
                self._log("health check: every node failed repeatedly, "
                          "keeping the old config and waiting for a refresh")
                return 0
            self._apply(kept)
            self._persist_eviction(evicted)
            for name in evicted:
                self.fails.pop(name, None)
        alive = len(delays)
        self._log(f"health check: {alive}/{len(self.proxies) + len(evicted)} passed"
                  + (f", evicted {len(evicted)} ({self.max_fails} consecutive failures)"
                     if evicted else ""))
        return len(self.proxies)

    # ---------- main loop ----------

    def run_forever(self) -> None:
        if not self._load_good():
            self._log(f"no {self.good_yaml.name}, running a full refresh first...")
            self._refresh()
        proxies = self._load_good()
        if not proxies:
            raise RuntimeError(
                f"still no usable nodes after refresh, check {self.sources} / your network"
            )

        self.proxies = proxies
        config = build_config(
            proxies, mixed_port=self.mixed_port, api_port=self.api_port,
            group_type="load-balance",
        )
        self.mihomo = Mihomo(config, workdir=self.workdir)
        self.mihomo.start()
        self.last_refresh = self.last_refresh or time.time()
        self._log(f"proxy pool started: {len(proxies)} nodes, "
                  f"entry http://127.0.0.1:{self.mixed_port}, API :{self.api_port}")
        self._log(f"health check every {self.health_every // 60} min; "
                  f"scheduled refresh every {self.refresh_every // 3600:.1f} h; "
                  f"refresh early when alive < {self.min_nodes}")

        next_health = time.time() + self.health_every
        try:
            while True:
                time.sleep(min(30.0, max(1.0, next_health - time.time())))
                now = time.time()
                try:
                    if now - self.last_refresh >= self.refresh_every:
                        self._refresh()
                        next_health = time.time() + self.health_every
                    elif now >= next_health:
                        alive = self._health_check()
                        next_health = now + self.health_every
                        if alive < self.min_nodes and now - self.last_refresh >= MIN_REFRESH_GAP:
                            self._log(f"alive nodes {alive} < {self.min_nodes}, refreshing early")
                            self._refresh()
                            next_health = time.time() + self.health_every
                except Exception:
                    self._log("error inside the loop (service keeps running):\n"
                              + traceback.format_exc())
        except KeyboardInterrupt:
            self._log("received exit signal")
        finally:
            self.mihomo.stop()


# For reuse by collectors: requests proxies pointing at the local service
def local_proxies(mixed_port: int = 7890) -> dict:
    addr = f"http://127.0.0.1:{mixed_port}"
    return {"http": addr, "https": addr}


def wait_ready(api_port: int = 9090, timeout: float = 30.0) -> bool:
    """Wait for the local service to be ready (probe the mihomo API), for dependents on boot."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            requests.get(f"http://127.0.0.1:{api_port}/version", timeout=2)
            return True
        except requests.RequestException:
            time.sleep(1)
    return False

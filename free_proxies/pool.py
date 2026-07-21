"""ProxyPool: expose validated nodes as a local proxy for requests-based scraping.

Usage::

    from free_proxies.pool import ProxyPool
    import requests

    with ProxyPool() as pool:
        r = requests.get("https://example.com", proxies=pool.requests_proxies, timeout=15)

In load-balance + round-robin mode every new connection rotates to another node,
so the exit IP changes automatically.
"""

from __future__ import annotations

from pathlib import Path

import requests
import yaml

from .mihomo import Mihomo, build_config

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


class ProxyPool:
    def __init__(
        self,
        good_yaml: str | Path = DATA_DIR / "good.yaml",
        mixed_port: int = 7890,
        api_port: int = 9090,
        rotate: bool = True,
        top_n: int | None = None,
    ):
        doc = yaml.safe_load(Path(good_yaml).read_text(encoding="utf-8"))
        proxies = doc["proxies"]
        if not proxies:
            raise ValueError(f"no working nodes in {good_yaml}, run validate first")
        if top_n:
            proxies = proxies[:top_n]
        self.node_names = [p["name"] for p in proxies]
        config = build_config(
            proxies, mixed_port=mixed_port, api_port=api_port,
            group_type="load-balance" if rotate else "select",
        )
        self._mihomo = Mihomo(config, workdir=DATA_DIR / "pool")

    def start(self) -> "ProxyPool":
        self._mihomo.start()
        return self

    @property
    def requests_proxies(self) -> dict:
        return self._mihomo.requests_proxies

    def session(self) -> requests.Session:
        s = requests.Session()
        s.proxies = self.requests_proxies
        return s

    def select(self, name: str) -> None:
        """Manually switch node in select mode (rotate=False)."""
        self._mihomo.select(name)

    def close(self) -> None:
        self._mihomo.stop()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.close()

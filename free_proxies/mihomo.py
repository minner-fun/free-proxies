"""mihomo (Clash Meta) process management + REST API wrapper."""

from __future__ import annotations

import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote

import requests
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MIHOMO_BIN = PROJECT_ROOT / "bin" / ("mihomo.exe" if sys.platform == "win32" else "mihomo")


def build_config(
    proxies: list[dict],
    mixed_port: int = 7890,
    api_port: int = 9090,
    group_type: str = "select",
    strategy: str = "round-robin",
) -> dict:
    group: dict = {
        "name": "POOL",
        "type": group_type,
        "proxies": [p["name"] for p in proxies],
    }
    if group_type == "load-balance":
        group.update({
            "strategy": strategy,
            "url": "https://www.gstatic.com/generate_204",
            "interval": 300,
        })
    return {
        "mixed-port": mixed_port,
        "external-controller": f"127.0.0.1:{api_port}",
        "log-level": "warning",
        "mode": "rule",
        "ipv6": False,
        "profile": {"store-selected": False},
        "dns": {
            "enable": True,
            "ipv6": False,
            "nameserver": ["223.5.5.5", "119.29.29.29", "8.8.8.8"],
        },
        "proxies": proxies,
        "proxy-groups": [group],
        "rules": ["MATCH,POOL"],
    }


class Mihomo:
    def __init__(self, config: dict, workdir: str | Path = PROJECT_ROOT / "data" / "mihomo"):
        self.config = config
        self.workdir = Path(workdir)
        self.api = f"http://127.0.0.1:{config['external-controller'].split(':')[1]}"
        self.mixed_port = config["mixed-port"]
        self.proc: subprocess.Popen | None = None

    def _drop_proxy(self, idx: int) -> str | None:
        """Drop the idx-th proxy from config and remove its name from all proxy-groups."""
        proxies = self.config.get("proxies", [])
        if not 0 <= idx < len(proxies):
            return None
        name = proxies.pop(idx).get("name")
        for g in self.config.get("proxy-groups", []):
            if name in g.get("proxies", []):
                g["proxies"].remove(name)
        return name

    def start(self, wait: float = 15.0, max_prune: int = 50) -> None:
        """Start mihomo. If some node makes parsing fail (mihomo validates strictly),
        automatically evict the offending node and retry, so a single malformed node
        cannot take down the whole validation run."""
        self.workdir.mkdir(parents=True, exist_ok=True)
        cfg_path = self.workdir / "config.yaml"
        pruned = 0
        while True:
            cfg_path.write_text(
                yaml.safe_dump(self.config, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
            log_path = self.workdir / "mihomo.log"
            with open(log_path, "w") as log:
                self.proc = subprocess.Popen(
                    [str(MIHOMO_BIN), "-d", str(self.workdir), "-f", str(cfg_path)],
                    stdout=log, stderr=subprocess.STDOUT,
                )
            deadline = time.time() + wait
            while time.time() < deadline:
                if self.proc.poll() is not None:
                    err = log_path.read_text(encoding="utf-8", errors="ignore")
                    m = re.search(r"proxy (\d+):", err)
                    if m and pruned < max_prune:
                        dropped = self._drop_proxy(int(m.group(1)))
                        pruned += 1
                        print(f"  [dropped malformed node #{m.group(1)} {dropped}] "
                              f"{err.strip().splitlines()[-1][-80:]}")
                        break  # rewrite config and restart
                    raise RuntimeError(f"mihomo failed to start, see log at {log_path}")
                try:
                    requests.get(f"{self.api}/version", timeout=1)
                    if pruned:
                        print(f"  started successfully after dropping {pruned} malformed nodes")
                    return
                except requests.RequestException:
                    time.sleep(0.3)
            else:
                raise TimeoutError("mihomo API was not ready in time")

    def reload(self, config: dict) -> None:
        """Hot reload the config (PUT /configs); ports stay the same and
        already-established connections are unaffected."""
        self.config = config
        cfg_path = self.workdir / "config.yaml"
        cfg_path.write_text(
            yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        requests.put(
            f"{self.api}/configs", params={"force": "true"},
            json={"path": str(cfg_path), "payload": ""}, timeout=30,
        ).raise_for_status()

    def group_delay(self, group: str = "POOL", test_url: str = "https://www.gstatic.com/generate_204",
                    timeout_ms: int = 5000) -> dict[str, int]:
        """Run one concurrent latency test over the whole group, returning
        {node name: latency in ms}; failed nodes are not in the result."""
        r = requests.get(
            f"{self.api}/group/{quote(group)}/delay",
            params={"url": test_url, "timeout": timeout_ms},
            timeout=timeout_ms / 1000 + 60,
        )
        r.raise_for_status()
        return r.json()

    def delay(self, name: str, test_url: str, timeout_ms: int = 5000) -> int | None:
        """URL test; returns latency in ms, or None on failure."""
        try:
            r = requests.get(
                f"{self.api}/proxies/{quote(name)}/delay",
                params={"url": test_url, "timeout": timeout_ms},
                timeout=timeout_ms / 1000 + 5,
            )
            if r.status_code == 200:
                return r.json().get("delay")
        except requests.RequestException:
            pass
        return None

    def select(self, name: str, group: str = "POOL") -> None:
        requests.put(f"{self.api}/proxies/{quote(group)}",
                     json={"name": name}, timeout=5).raise_for_status()

    @property
    def requests_proxies(self) -> dict:
        addr = f"http://127.0.0.1:{self.mixed_port}"
        return {"http": addr, "https": addr}

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()

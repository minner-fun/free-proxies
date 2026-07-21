"""Batch node validation: concurrent mihomo latency tests + optional requests exit IP re-verify.

When there are very many nodes (tens of thousands), loading them all into a single mihomo
instance is both slow and fragile (one malformed node drags down the whole batch), so
validation is batched by batch_size: each batch gets its own mihomo instance, evicting a bad
node only requires rewriting a small yaml, and faults are isolated within the batch.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
import yaml

from .mihomo import Mihomo, build_config

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
IP_API = "https://api.ipify.org"


def _rename(nodes: list[dict]) -> list[dict]:
    """Normalize node names to the p00001 form (avoids special characters and
    duplicates); the original name is kept in _orig."""
    out = []
    for i, n in enumerate(nodes):
        n = dict(n)
        n["_orig"] = str(n.get("name", ""))
        n["name"] = f"p{i:05d}"
        out.append(n)
    return out


def _test_batch(
    batch: list[dict], test_url: str, timeout_ms: int, workers: int,
    mixed_port: int, api_port: int,
) -> list[dict]:
    """Start one mihomo, run a concurrent latency test over a batch of nodes,
    and return the working ones (with _delay)."""
    config = build_config(batch, mixed_port=mixed_port, api_port=api_port)
    good: list[dict] = []
    with Mihomo(config, workdir=DATA_DIR / "mihomo") as m:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(m.delay, n["name"], test_url, timeout_ms): n for n in batch}
            for fut in as_completed(futures):
                node, delay = futures[fut], fut.result()
                if delay is not None:
                    node["_delay"] = delay
                    good.append(node)
    return good


def validate(
    nodes: list[dict],
    test_url: str = "https://www.gstatic.com/generate_204",
    timeout_ms: int = 5000,
    workers: int = 64,
    check_ip: int = 0,
    mixed_port: int = 7890,
    api_port: int = 9090,
    batch_size: int = 4000,
) -> list[dict]:
    """Return the list of working nodes (with delay / exit_ip fields), and write
    data/good.yaml and data/report.json."""
    nodes = _rename(nodes)
    origs = {n["name"]: n.pop("_orig") for n in nodes}
    total = len(nodes)

    good: list[dict] = []
    batches = [nodes[i:i + batch_size] for i in range(0, total, batch_size)]
    print(f"Testing {total} nodes in {len(batches)} batches "
          f"(<={batch_size} each, url={test_url}, timeout={timeout_ms}ms)")
    for bi, batch in enumerate(batches, 1):
        found = _test_batch(batch, test_url, timeout_ms, workers, mixed_port, api_port)
        good.extend(found)
        print(f"  batch {bi}/{len(batches)} done, {len(found)} working in this batch, "
              f"{len(good)} total")

    good.sort(key=lambda n: n["_delay"])

    if check_ip and good:
        n_check = min(check_ip, len(good))
        print(f"Re-verifying the exit IP of the top {n_check} nodes with requests...")
        config = build_config(good[:n_check], mixed_port=mixed_port, api_port=api_port)
        verified = []
        with Mihomo(config, workdir=DATA_DIR / "mihomo") as m:
            for node in good[:n_check]:
                m.select(node["name"])
                try:
                    ip = requests.get(IP_API, proxies=m.requests_proxies, timeout=10).text.strip()
                    node["_exit_ip"] = ip
                    verified.append(node)
                except requests.RequestException:
                    pass
        print(f"  re-verify passed {len(verified)}/{n_check}")
        good = verified + good[n_check:]

    report = [
        {
            "name": n["name"], "orig_name": origs[n["name"]],
            "type": n["type"], "server": n["server"], "port": n["port"],
            "delay_ms": n["_delay"], "exit_ip": n.get("_exit_ip"),
        }
        for n in good
    ]
    clean = [{k: v for k, v in n.items() if not k.startswith("_")} for n in good]

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "good.yaml").write_text(
        yaml.safe_dump({"proxies": clean}, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    (DATA_DIR / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{len(good)}/{total} working nodes, written to data/good.yaml and data/report.json")
    return good

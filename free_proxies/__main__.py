"""CLI entry point:
    uv run python -m free_proxies fetch      # pull subscriptions -> data/nodes.yaml
    uv run python -m free_proxies validate   # validate          -> data/good.yaml + report.json
    uv run python -m free_proxies all        # fetch + validate
    uv run python -m free_proxies run        # start the proxy pool on a local port
"""

from __future__ import annotations

import argparse
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
NODES_YAML = DATA_DIR / "nodes.yaml"
CN_NODES_YAML = DATA_DIR / "nodes_cn.yaml"


def cmd_fetch(args) -> list[dict]:
    from .fetcher import fetch_all, load_sources

    nodes = fetch_all(load_sources(args.sources))
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    NODES_YAML.write_text(
        yaml.safe_dump({"proxies": nodes}, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    print(f"written to {NODES_YAML}")
    return nodes


def cmd_validate(args, nodes: list[dict] | None = None):
    from .validator import validate

    if nodes is None:
        nodes = yaml.safe_load(NODES_YAML.read_text(encoding="utf-8"))["proxies"]
    if args.limit:
        nodes = nodes[: args.limit]
    validate(
        nodes,
        test_url=args.test_url,
        timeout_ms=args.timeout,
        workers=args.workers,
        check_ip=args.check_ip,
        mixed_port=args.port,
        api_port=args.api_port,
        batch_size=args.batch_size,
    )


def cmd_cn_fetch(args) -> list[dict]:
    from .cn_fetch import fetch_all, load_sources

    proxies = fetch_all(load_sources(args.sources))
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CN_NODES_YAML.write_text(
        yaml.safe_dump({"proxies": proxies}, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    print(f"written to {CN_NODES_YAML}")
    return proxies


def cmd_cn_validate(args, proxies: list[dict] | None = None):
    from .cn_validate import validate

    if proxies is None:
        proxies = yaml.safe_load(CN_NODES_YAML.read_text(encoding="utf-8"))["proxies"]
    if args.limit:
        proxies = proxies[: args.limit]
    validate(proxies, timeout_ms=args.timeout, workers=args.workers, cn_only=args.cn_only)


def cmd_run(args):
    import time

    from .pool import ProxyPool

    with ProxyPool(mixed_port=args.port, api_port=args.api_port,
                   rotate=not args.no_rotate, top_n=args.top) as pool:
        print(f"proxy pool started: {len(pool.node_names)} nodes")
        print(f"requests usage: proxies={pool.requests_proxies}")
        print("Ctrl+C to exit")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            print("\nexiting")


def main():
    ap = argparse.ArgumentParser(prog="free_proxies")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_fetch = sub.add_parser("fetch", help="pull nodes from subscription sources")
    p_fetch.add_argument("--sources", default=PROJECT_ROOT / "subscriptions.txt")

    def add_validate_args(p):
        p.add_argument("--test-url", default="https://www.gstatic.com/generate_204")
        p.add_argument("--timeout", type=int, default=5000, help="per-node timeout (ms)")
        p.add_argument("--workers", type=int, default=64)
        p.add_argument("--check-ip", type=int, default=0,
                       help="re-check the exit IP of the first N nodes with requests, 0=skip")
        p.add_argument("--limit", type=int, default=0,
                       help="only test the first N nodes, 0=all")
        p.add_argument("--port", type=int, default=7890)
        p.add_argument("--api-port", type=int, default=9090)
        p.add_argument("--batch-size", type=int, default=4000,
                       help="nodes per validation batch; keeps a single instance from "
                            "getting slow/overwhelmed when there are many nodes")

    add_validate_args(sub.add_parser("validate", help="validate node availability"))

    p_all = sub.add_parser("all", help="fetch + validate")
    p_all.add_argument("--sources", default=PROJECT_ROOT / "subscriptions.txt")
    add_validate_args(p_all)

    p_run = sub.add_parser("run", help="start the proxy pool")
    p_run.add_argument("--port", type=int, default=7890)
    p_run.add_argument("--api-port", type=int, default=9090)
    p_run.add_argument("--top", type=int, default=0,
                       help="only use the N lowest-latency nodes")
    p_run.add_argument("--no-rotate", action="store_true",
                       help="no rotation, select nodes manually")

    # --- domestic/generic HTTP-SOCKS5 proxy line (direct requests, no mihomo) ---
    def add_cn_validate_args(p):
        p.add_argument("--timeout", type=int, default=8000, help="per-proxy timeout (ms)")
        p.add_argument("--workers", type=int, default=128)
        p.add_argument("--limit", type=int, default=0, help="only test the first N, 0=all")
        p.add_argument("--cn-only", action="store_true",
                       help="keep only proxies whose exit is in mainland China")

    p_cnf = sub.add_parser("cn-fetch", help="fetch generic HTTP/SOCKS5 proxy lists")
    p_cnf.add_argument("--sources", default=PROJECT_ROOT / "cn_sources.txt")

    add_cn_validate_args(sub.add_parser("cn-validate", help="validate HTTP/SOCKS5 proxies"))

    p_cnall = sub.add_parser("cn-all", help="cn-fetch + cn-validate")
    p_cnall.add_argument("--sources", default=PROJECT_ROOT / "cn_sources.txt")
    add_cn_validate_args(p_cnall)

    p_serve = sub.add_parser(
        "serve", help="long-running service: proxy pool + scheduled refresh + health check")
    p_serve.add_argument("--region", choices=["overseas", "cn"], default="overseas",
                         help="overseas=provider line (default, 7890); "
                              "cn=generic HTTP/SOCKS5 line (7891)")
    p_serve.add_argument("--sources", default=None,
                         help="source file; defaults per region")
    p_serve.add_argument("--port", type=int, default=0,
                         help="entry port, 0=pick per region")
    p_serve.add_argument("--api-port", type=int, default=0,
                         help="mihomo API port, 0=pick per region")
    p_serve.add_argument("--top", type=int, default=0,
                         help="only use the N lowest-latency nodes, 0=all")
    p_serve.add_argument("--test-url", default=None,
                         help="health check test URL; defaults per region")
    p_serve.add_argument("--timeout", type=int, default=0,
                         help="per-node timeout (ms), 0=pick per region")
    p_serve.add_argument("--workers", type=int, default=0,
                         help="refresh validation concurrency, 0=pick per region")
    p_serve.add_argument("--batch-size", type=int, default=4000)
    p_serve.add_argument("--cn-only", action="store_true",
                         help="region=cn only: keep only mainland China exits")
    p_serve.add_argument("--refresh-every", type=int, default=180,
                         help="full scheduled refresh interval (minutes), default 180")
    p_serve.add_argument("--health-every", type=int, default=5,
                         help="health check interval (minutes), default 5")
    p_serve.add_argument("--max-fails", type=int, default=2,
                         help="evict a node after N consecutive failures, default 2")
    p_serve.add_argument("--min-nodes", type=int, default=20,
                         help="trigger an early refresh when alive nodes drop below N, "
                              "default 20")

    args = ap.parse_args()
    if args.cmd == "fetch":
        cmd_fetch(args)
    elif args.cmd == "validate":
        cmd_validate(args)
    elif args.cmd == "all":
        nodes = cmd_fetch(args)
        cmd_validate(args, nodes)
    elif args.cmd == "cn-fetch":
        cmd_cn_fetch(args)
    elif args.cmd == "cn-validate":
        cmd_cn_validate(args)
    elif args.cmd == "cn-all":
        proxies = cmd_cn_fetch(args)
        cmd_cn_validate(args, proxies)
    elif args.cmd == "run":
        args.top = args.top or None
        cmd_run(args)
    elif args.cmd == "serve":
        from .service import ProxyService

        cn = args.region == "cn"
        ProxyService(
            region=args.region,
            sources=args.sources,
            mixed_port=args.port or (7891 if cn else 7890),
            api_port=args.api_port or (9091 if cn else 9090),
            top_n=args.top or None,
            test_url=args.test_url or (
                "http://www.gstatic.com/generate_204" if cn
                else "https://www.gstatic.com/generate_204"),
            timeout_ms=args.timeout or (8000 if cn else 5000),
            workers=args.workers or (128 if cn else 64),
            batch_size=args.batch_size,
            cn_only=args.cn_only,
            refresh_every=args.refresh_every * 60,
            health_every=args.health_every * 60,
            max_fails=args.max_fails,
            min_nodes=args.min_nodes,
        ).run_forever()


if __name__ == "__main__":
    main()

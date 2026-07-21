"""Parse subscription content (base64 share-link list / Clash YAML) into mihomo proxy dicts."""

from __future__ import annotations

import base64
import binascii
import json
import re
from urllib.parse import parse_qs, unquote, urlparse

import yaml


def _b64decode(s: str) -> bytes:
    s = s.strip().replace("-", "+").replace("_", "/")
    s += "=" * (-len(s) % 4)
    return base64.b64decode(s)


def _maybe_b64_text(s: str) -> str | None:
    try:
        text = _b64decode(s).decode("utf-8", errors="strict")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    return text


def _qs(url) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(url.query).items() if v}


def _valid_x25519_pubkey(pbk: str) -> bool:
    """A REALITY public key must be 32 bytes encoded as base64/base64url."""
    if not pbk:
        return False
    try:
        return len(_b64decode(pbk)) == 32
    except (binascii.Error, ValueError):
        return False


def parse_vmess(link: str) -> dict | None:
    raw = _maybe_b64_text(link[len("vmess://"):])
    if not raw:
        return None
    try:
        info = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not info.get("add") or not info.get("port") or not info.get("id"):
        return None
    net = info.get("net") or "tcp"
    node = {
        "name": info.get("ps") or f'{info["add"]}:{info["port"]}',
        "type": "vmess",
        "server": str(info["add"]),
        "port": int(info["port"]),
        "uuid": info["id"],
        "alterId": int(info.get("aid") or 0),
        "cipher": info.get("scy") or "auto",
        "udp": True,
        "network": net,
        "skip-cert-verify": True,
    }
    if str(info.get("tls") or "").lower() in ("tls", "true", "1"):
        node["tls"] = True
        sni = info.get("sni") or info.get("host")
        if sni:
            node["servername"] = sni
    host, path = info.get("host"), info.get("path")
    if net == "ws":
        node["ws-opts"] = {"path": path or "/"}
        if host:
            node["ws-opts"]["headers"] = {"Host": host}
    elif net == "grpc":
        node["grpc-opts"] = {"grpc-service-name": path or ""}
    elif net in ("h2", "http"):
        node["network"] = "h2"
        node["h2-opts"] = {"path": path or "/", "host": [host] if host else []}
    return node


def parse_vless(link: str) -> dict | None:
    u = urlparse(link)
    if not u.hostname or not u.port or not u.username:
        return None
    q = _qs(u)
    net = q.get("type", "tcp")
    node = {
        "name": unquote(u.fragment) or f"{u.hostname}:{u.port}",
        "type": "vless",
        "server": u.hostname,
        "port": u.port,
        "uuid": u.username,
        "udp": True,
        "network": net,
        "client-fingerprint": q.get("fp") or "chrome",
        "skip-cert-verify": True,
    }
    security = q.get("security", "none")
    if security in ("tls", "reality"):
        node["tls"] = True
        if q.get("sni"):
            node["servername"] = q["sni"]
    if security == "reality":
        pbk = q.get("pbk", "")
        # mihomo requires the REALITY public key to base64-decode to 32 bytes,
        # otherwise it refuses to start -> drop this node outright
        if not _valid_x25519_pubkey(pbk):
            return None
        node["reality-opts"] = {"public-key": pbk}
        sid = q.get("sid", "")
        # short-id must be a hex string of even length and <=8 bytes (16 hex chars),
        # otherwise mihomo refuses to start
        if (sid and len(sid) % 2 == 0 and len(sid) <= 16
                and all(c in "0123456789abcdefABCDEF" for c in sid)):
            node["reality-opts"]["short-id"] = sid
    # mihomo now only supports vision; older flow types are rejected
    if q.get("flow") == "xtls-rprx-vision":
        node["flow"] = q["flow"]
    if net == "ws":
        node["ws-opts"] = {"path": unquote(q.get("path", "/"))}
        if q.get("host"):
            node["ws-opts"]["headers"] = {"Host": q["host"]}
    elif net == "grpc":
        node["grpc-opts"] = {"grpc-service-name": unquote(q.get("serviceName", ""))}
    return node


def parse_trojan(link: str) -> dict | None:
    u = urlparse(link)
    if not u.hostname or not u.port or not u.username:
        return None
    q = _qs(u)
    node = {
        "name": unquote(u.fragment) or f"{u.hostname}:{u.port}",
        "type": "trojan",
        "server": u.hostname,
        "port": u.port,
        "password": unquote(u.username),
        "udp": True,
        "skip-cert-verify": True,
    }
    if q.get("sni"):
        node["sni"] = q["sni"]
    net = q.get("type", "tcp")
    if net == "ws":
        node["network"] = "ws"
        node["ws-opts"] = {"path": unquote(q.get("path", "/"))}
        if q.get("host"):
            node["ws-opts"]["headers"] = {"Host": q["host"]}
    elif net == "grpc":
        node["network"] = "grpc"
        node["grpc-opts"] = {"grpc-service-name": unquote(q.get("serviceName", ""))}
    return node


# shadowsocks ciphers supported by mihomo; the rest (e.g. "ss", "none", rc4) are rejected
_SS_CIPHERS = {
    "aes-128-gcm", "aes-192-gcm", "aes-256-gcm",
    "aes-128-cfb", "aes-192-cfb", "aes-256-cfb",
    "aes-128-ctr", "aes-192-ctr", "aes-256-ctr",
    "chacha20-ietf-poly1305", "xchacha20-ietf-poly1305",
    "chacha20-ietf", "chacha20", "rc4-md5",
    "2022-blake3-aes-128-gcm", "2022-blake3-aes-256-gcm",
    "2022-blake3-chacha20-poly1305",
}


def parse_ss(link: str) -> dict | None:
    body = link[len("ss://"):]
    name = ""
    if "#" in body:
        body, frag = body.split("#", 1)
        name = unquote(frag).strip()
    plugin = ""
    if "?" in body:
        body, query = body.split("?", 1)
        plugin = {k: v[0] for k, v in parse_qs(query).items()}.get("plugin", "")
    # free nodes using simple-obfs/v2ray-plugin are rare and complex to configure; skip them
    if plugin:
        return None
    if "@" in body:  # SIP002: base64(method:pass)@host:port
        userinfo, hostpart = body.rsplit("@", 1)
        decoded = _maybe_b64_text(userinfo) or unquote(userinfo)
        if ":" not in decoded or ":" not in hostpart:
            return None
        method, password = decoded.split(":", 1)
        host, _, port = hostpart.rpartition(":")
    else:  # legacy: base64(method:pass@host:port)
        decoded = _maybe_b64_text(body)
        if not decoded or "@" not in decoded:
            return None
        userinfo, hostpart = decoded.rsplit("@", 1)
        if ":" not in userinfo or ":" not in hostpart:
            return None
        method, password = userinfo.split(":", 1)
        host, _, port = hostpart.rpartition(":")
    host = host.strip("[]")
    if not port.isdigit() or method.lower() not in _SS_CIPHERS:
        return None
    return {
        "name": name or f"{host}:{port}",
        "type": "ss",
        "server": host,
        "port": int(port),
        "cipher": method,
        "password": password,
        "udp": True,
    }


def parse_hysteria2(link: str) -> dict | None:
    u = urlparse(link)
    if not u.hostname or not u.port:
        return None
    q = _qs(u)
    node = {
        "name": unquote(u.fragment) or f"{u.hostname}:{u.port}",
        "type": "hysteria2",
        "server": u.hostname,
        "port": u.port,
        "password": unquote(u.username or ""),
        "skip-cert-verify": True,
    }
    if q.get("sni"):
        node["sni"] = q["sni"]
    obfs_pw = q.get("obfs-password")
    # mihomo requires obfs and its password to come as a pair; skip obfs if either is missing
    if q.get("obfs") and obfs_pw:
        node["obfs"] = q["obfs"]
        node["obfs-password"] = obfs_pw
    return node


_PARSERS = {
    "vmess://": parse_vmess,
    "vless://": parse_vless,
    "trojan://": parse_trojan,
    "ss://": parse_ss,
    "hysteria2://": parse_hysteria2,
    "hy2://": parse_hysteria2,
}


def parse_link(link: str) -> dict | None:
    link = link.strip()
    for prefix, fn in _PARSERS.items():
        if link.startswith(prefix):
            if prefix == "hy2://":
                link = "hysteria2://" + link[len("hy2://"):]
            try:
                return fn(link)
            except (ValueError, KeyError, TypeError):
                return None
    return None


_LINK_RE = re.compile(r"^(vmess|vless|trojan|ss|ssr|hysteria2|hy2)://", re.M)


def parse_subscription(content: str) -> list[dict]:
    """Subscription body -> list of mihomo proxy dicts.

    Auto-detects YAML / base64 / plain share-link list.
    """
    content = content.strip()
    if not content:
        return []

    # Clash YAML subscription
    if "proxies:" in content:
        try:
            doc = yaml.safe_load(content)
            if isinstance(doc, dict) and isinstance(doc.get("proxies"), list):
                return [p for p in doc["proxies"] if isinstance(p, dict) and p.get("server")]
        except yaml.YAMLError:
            pass

    # link list encoded as one whole base64 blob
    if not _LINK_RE.search(content):
        decoded = _maybe_b64_text(content)
        if decoded and _LINK_RE.search(decoded):
            content = decoded

    nodes = []
    for line in content.splitlines():
        node = parse_link(line)
        if node:
            nodes.append(node)
    return nodes


def dedupe(nodes: list[dict]) -> list[dict]:
    seen, out = set(), []
    for n in nodes:
        key = (n.get("type"), str(n.get("server")).lower(), n.get("port"),
               n.get("uuid") or n.get("password") or "")
        if key in seen:
            continue
        seen.add(key)
        out.append(n)
    return out

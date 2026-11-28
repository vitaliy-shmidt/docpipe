"""Client address, HTTPS and network access decisions (hardening H5).

There is exactly one place where forwarded headers are evaluated: here.
Uvicorn must not rewrite them itself (it does so by default for 127.0.0.1)
- DocPipe's image starts Uvicorn with `--no-proxy-headers`, and the web
interface refuses to start when it can see that the running Uvicorn would
rewrite them (see `uvicorn_rewrites_proxy_headers`).

Algorithm (docs/architecture/monitoring-dashboard.md §3.6):

1. The TCP peer is scope["client"][0]. Missing or unparsable -> deny.
2. Peer not in `trusted_proxies` -> the peer IS the client; every
   Forwarded / X-Forwarded-* header is ignored; HTTPS = scope["scheme"].
3. Peer in `trusted_proxies` -> only X-Forwarded-For is read: all its
   header lines are joined, entries are read right to left, trusted proxy
   addresses are skipped, the first untrusted entry is the client. Any
   malformed entry, or a list consisting only of proxies, -> deny.
   HTTPS = the rightmost X-Forwarded-Proto value == "https" (missing -> not
   HTTPS). `Forwarded` (RFC 7239) and `X-Real-IP` are never evaluated.

IPv4-mapped IPv6 addresses (::ffff:a.b.c.d) are treated as IPv4.
"""

from __future__ import annotations

import ipaddress
import os
import sys
from dataclasses import dataclass

Address = ipaddress.IPv4Address | ipaddress.IPv6Address
MAX_FORWARDED_ENTRIES = 32


@dataclass(frozen=True)
class AccessDecision:
    allowed: bool
    reason: str  # "ok" or a registry reason for the audit log
    client_ip: Address | None = None
    https: bool = False


def parse_ip(value: str) -> Address | None:
    value = value.strip()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    if not value or len(value) > 45 or "%" in value:
        return None
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def _in(address: Address, networks) -> bool:
    return any(address.version == n.version and address in n for n in networks)


def _header_values(scope, name: bytes) -> list[str]:
    values = []
    for key, value in scope.get("headers") or ():
        if key.lower() == name:
            values.append(value.decode("latin-1"))
    return values


def resolve(scope, web) -> AccessDecision:
    """Client address and HTTPS state of a request, or a denial."""
    client = scope.get("client")
    peer = parse_ip(client[0]) if client and isinstance(client[0], str) else None
    if peer is None:
        return AccessDecision(False, "no_peer_address")

    if not _in(peer, web.trusted_proxies):
        https = scope.get("scheme") == "https"
        return AccessDecision(True, "ok", peer, https)

    entries = [e for line in _header_values(scope, b"x-forwarded-for") for e in line.split(",")]
    if not entries or len(entries) > MAX_FORWARDED_ENTRIES:
        return AccessDecision(False, "forwarded_header_invalid")
    client_ip = None
    for raw in reversed(entries):
        address = parse_ip(raw)
        if address is None:
            return AccessDecision(False, "forwarded_header_invalid")
        if _in(address, web.trusted_proxies):
            continue
        client_ip = address
        break
    if client_ip is None:
        return AccessDecision(False, "forwarded_header_invalid")
    protos = [
        p.strip().lower() for line in _header_values(scope, b"x-forwarded-proto") for p in line.split(",")
    ]
    https = bool(protos) and protos[-1] == "https"
    return AccessDecision(True, "ok", client_ip, https)


def decide(scope, web) -> AccessDecision:
    """Full network decision for a /monitor request (before any auth)."""
    resolved = resolve(scope, web)
    if not resolved.allowed:
        return resolved
    if web.access_mode == "allowlist" and not _in(resolved.client_ip, web.allowed_networks):
        return AccessDecision(False, "network_not_allowed", resolved.client_ip, resolved.https)
    if web.require_https and not resolved.https:
        return AccessDecision(False, "https_required", resolved.client_ip, resolved.https)
    return resolved


def uvicorn_rewrites_proxy_headers(argv: list[str] | None = None, env=None) -> bool:
    """True if this process was started by the Uvicorn CLI without
    `--no-proxy-headers` (Uvicorn would then rewrite scope["client"] and
    scope["scheme"] from X-Forwarded-* for 127.0.0.1 before DocPipe sees the
    request - a second, conflicting evaluation). Other launchers (tests,
    programmatic servers) cannot be inspected and return False; the
    deployment documentation states the requirement for them."""
    argv = sys.argv if argv is None else argv
    env = os.environ if env is None else env
    # Normalize Windows separators so the check works for either path style.
    launcher = argv[0].replace("\\", "/").rsplit("/", 1)[-1].lower() if argv else ""
    started_by_uvicorn = (
        launcher.startswith("uvicorn") or argv[0].replace("\\", "/").endswith("uvicorn/__main__.py")
        if argv
        else False
    )
    if not started_by_uvicorn:
        return False
    if "--no-proxy-headers" in argv:
        return False
    return env.get("UVICORN_PROXY_HEADERS", "").strip().lower() not in ("false", "0", "no")

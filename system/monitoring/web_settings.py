"""`monitoring.web:` - the operator web interface (Phase M3).

Separate from data collection (`monitoring.enabled`): the interface can stay
disabled while data is collected. It can only be enabled together with data
collection, and only with a complete, safe access configuration - every
structural problem is a startup ConfigError (like every other config error),
so a misconfiguration can never result in an unprotected interface.

Access modes (docs/architecture/monitoring-dashboard.md §3):

- allowlist (default): requests only from `allowed_networks` (non-empty,
  no 0.0.0.0/0 or ::/0), plus password and TOTP.
- public_mfa: explicitly enabled; reachable from anywhere, HTTPS mandatory,
  password and TOTP, rate limits, audit.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from pathlib import Path

ACCESS_MODES = ("allowlist", "public_mfa")
DEFAULT_USERS_FILE = Path("monitoring-users.yaml")
_ORIGIN = re.compile(
    r"https?://[A-Za-z0-9.\-]{1,253}(:[0-9]{1,5})?|https?://\[[0-9A-Fa-f:.]{2,45}\](:[0-9]{1,5})?"
)

Network = ipaddress.IPv4Network | ipaddress.IPv6Network


class WebConfigError(ValueError):
    """An invalid `monitoring.web` value."""


@dataclass(frozen=True)
class LoginLimits:
    ip_max_failures: int = 10
    user_max_failures: int = 5
    window_minutes: int = 15
    lockout_minutes: int = 15
    mfa_max_attempts: int = 5
    # Argon2id verifications: at most this many at the same time per process,
    # and at most `argon2_per_minute` per process - beyond, a login attempt is
    # answered with the uniform failure without running Argon2 at all.
    argon2_max_concurrency: int = 2
    argon2_per_minute: int = 30
    max_request_bytes: int = 4096


@dataclass(frozen=True)
class WebSettings:
    enabled: bool = False
    access_mode: str = "allowlist"
    allowed_networks: tuple[Network, ...] = ()
    trusted_proxies: tuple[Network, ...] = ()
    require_https: bool = True
    # Exact expected Origin (scheme://host[:port]) for state-changing
    # requests; if unset, derived from the request (scheme + Host header).
    public_origin: str | None = None
    users_file: Path = DEFAULT_USERS_FILE
    session_idle_minutes: int = 15
    session_absolute_hours: int = 8
    mfa_pending_seconds: int = 300
    max_sessions_per_user: int = 3
    totp_window_steps: int = 1
    audit_days: int = 90
    max_audit_rows: int = 100_000
    auth_db_max_bytes: int = 64 * 1024 * 1024
    # Dashboard polling (Phase M4), sent to the browser by the server; the
    # data API additionally caps requests per session and minute.
    poll_activity_seconds: int = 5
    poll_overview_seconds: int = 15
    api_requests_per_minute: int = 240
    login: LoginLimits = field(default_factory=LoginLimits)


_WEB_BOUNDS = {
    "session_idle_minutes": (5, 60),
    "session_absolute_hours": (1, 12),
    "mfa_pending_seconds": (60, 600),
    "max_sessions_per_user": (1, 10),
    "totp_window_steps": (0, 1),
    "audit_days": (1, 3650),
    "max_audit_rows": (1000, 10_000_000),
    "auth_db_max_bytes": (4 * 1024 * 1024, 1024 * 1024 * 1024),
    "poll_activity_seconds": (2, 60),
    "poll_overview_seconds": (5, 300),
    "api_requests_per_minute": (30, 2000),
}
_LOGIN_BOUNDS = {
    "ip_max_failures": (3, 100),
    "user_max_failures": (3, 20),
    "window_minutes": (1, 60),
    "lockout_minutes": (1, 1440),
    "mfa_max_attempts": (1, 10),
    "argon2_max_concurrency": (1, 8),
    "argon2_per_minute": (1, 600),
    "max_request_bytes": (512, 65536),
}


def _int_values(raw: dict, bounds: dict, name: str) -> dict:
    values = {}
    for key, (low, high) in bounds.items():
        if key not in raw:
            continue
        value = raw[key]
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise WebConfigError(f"{name}.{key} must be an integer between {low} and {high}.")
        values[key] = value
    return values


def _networks(raw: object, name: str) -> tuple[Network, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(v, str) for v in raw):
        raise WebConfigError(f"{name} must be a list of CIDR strings.")
    networks = []
    for value in raw:
        try:
            networks.append(ipaddress.ip_network(value.strip(), strict=False))
        except ValueError as exc:
            raise WebConfigError(f"{name}: {value!r} is not a valid IP network.") from exc
    return tuple(networks)


def parse_web(raw: object, *, collection_enabled: bool) -> WebSettings:
    if raw is None:
        return WebSettings()
    if not isinstance(raw, dict):
        raise WebConfigError("monitoring.web must be a mapping.")
    allowed_keys = {
        "enabled",
        "access_mode",
        "allowed_networks",
        "trusted_proxies",
        "require_https",
        "public_origin",
        "users_file",
        "login",
        *_WEB_BOUNDS,
    }
    unknown = set(raw) - allowed_keys
    if unknown:
        raise WebConfigError(f"monitoring.web has unknown keys: {sorted(unknown)}.")
    for key in ("enabled", "require_https"):
        if key in raw and not isinstance(raw[key], bool):
            raise WebConfigError(f"monitoring.web.{key} must be true or false.")
    enabled = raw.get("enabled", False)
    mode = raw.get("access_mode", "allowlist")
    if mode not in ACCESS_MODES:
        raise WebConfigError(f"monitoring.web.access_mode must be one of {list(ACCESS_MODES)}.")
    allowed = _networks(raw.get("allowed_networks"), "monitoring.web.allowed_networks")
    proxies = _networks(raw.get("trusted_proxies"), "monitoring.web.trusted_proxies")
    require_https = raw.get("require_https", True)

    origin = raw.get("public_origin")
    if isinstance(origin, str):
        origin = origin.strip().rstrip("/")
    if origin is not None and (not isinstance(origin, str) or not _ORIGIN.fullmatch(origin)):
        raise WebConfigError("monitoring.web.public_origin must look like https://host[:port].")
    users_file = raw.get("users_file", str(DEFAULT_USERS_FILE))
    if not isinstance(users_file, str) or not users_file.strip():
        raise WebConfigError("monitoring.web.users_file must be a non-empty path.")

    raw_login = raw.get("login") or {}
    if not isinstance(raw_login, dict):
        raise WebConfigError("monitoring.web.login must be a mapping.")
    unknown_login = set(raw_login) - set(_LOGIN_BOUNDS)
    if unknown_login:
        raise WebConfigError(f"monitoring.web.login has unknown keys: {sorted(unknown_login)}.")
    login = LoginLimits(**_int_values(raw_login, _LOGIN_BOUNDS, "monitoring.web.login"))

    settings = WebSettings(
        enabled=enabled,
        access_mode=mode,
        allowed_networks=allowed,
        trusted_proxies=proxies,
        require_https=require_https,
        public_origin=origin.rstrip("/") if origin else None,
        users_file=Path(users_file.strip()),
        login=login,
        **_int_values(raw, _WEB_BOUNDS, "monitoring.web"),
    )
    if settings.enabled:
        validate_enabled(settings, collection_enabled=collection_enabled)
    return settings


def validate_enabled(settings: WebSettings, *, collection_enabled: bool) -> None:
    """Rules that make an enabled interface safe. Raises WebConfigError."""
    if not collection_enabled:
        raise WebConfigError("monitoring.web.enabled requires monitoring.enabled: true.")
    if settings.access_mode == "allowlist":
        if not settings.allowed_networks:
            raise WebConfigError(
                "monitoring.web.allowed_networks must not be empty in access_mode allowlist "
                "(an empty allowlist never means 'allow everyone')."
            )
        if any(n.prefixlen == 0 for n in settings.allowed_networks):
            raise WebConfigError(
                "monitoring.web.allowed_networks must not contain 0.0.0.0/0 or ::/0 - use "
                "access_mode: public_mfa to deliberately allow public access."
            )
        if not settings.require_https and not all(n.is_loopback for n in settings.allowed_networks):
            raise WebConfigError(
                "monitoring.web.require_https: false is only allowed when every allowed network "
                "is loopback (local development)."
            )
    else:
        if settings.allowed_networks:
            raise WebConfigError("monitoring.web.allowed_networks is not used in access_mode public_mfa.")
        if not settings.require_https:
            raise WebConfigError("monitoring.web.require_https cannot be disabled in access_mode public_mfa.")
        if settings.public_origin and not settings.public_origin.startswith("https://"):
            raise WebConfigError("monitoring.web.public_origin must use https in access_mode public_mfa.")
    if any(n.prefixlen == 0 for n in settings.trusted_proxies):
        raise WebConfigError("monitoring.web.trusted_proxies must not contain 0.0.0.0/0 or ::/0.")
    if settings.session_absolute_hours * 60 < settings.session_idle_minutes:
        raise WebConfigError("monitoring.web.session_absolute_hours must cover session_idle_minutes.")

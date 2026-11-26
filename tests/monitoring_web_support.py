"""Helpers for the monitoring web interface tests (synthetic users only).

Argon2id is used with deliberately cheap parameters in tests (the hash format
stays argon2id); production parameters are tested separately.
"""

from __future__ import annotations

import dataclasses
import ipaddress
import os
import re
import time
from contextlib import contextmanager
from pathlib import Path

import yaml
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from system import main as main_module
from system.monitoring.web import passwords, totp
from system.monitoring.web_settings import WebSettings
from tests.monitoring_support import build_settings

PASSWORD = "synthetic-operator-passphrase"
ORIGIN = "https://testserver"
FAST_HASHER = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)


def use_fast_argon2(monkeypatch) -> None:
    monkeypatch.setattr(passwords, "HASHER", FAST_HASHER)
    monkeypatch.setattr(passwords, "_DUMMY_HASH", FAST_HASHER.hash("dummy"))


def write_users(path: Path, users: list[dict]) -> None:
    path.write_text(yaml.safe_dump({"version": 1, "users": users}), encoding="utf-8")
    if os.name == "posix":
        os.chmod(path, 0o600)


def user_entry(
    username: str = "alice", password: str = PASSWORD, secret: str | None = None, enabled: bool = True
) -> dict:
    return {
        "username": username,
        "password_hash": FAST_HASHER.hash(password),
        "totp_secret": secret or totp.new_secret(),
        "enabled": enabled,
    }


def web_settings(tmp_path: Path, **overrides) -> WebSettings:
    values = dict(
        enabled=True,
        access_mode="allowlist",
        allowed_networks=(ipaddress.ip_network("127.0.0.0/8"), ipaddress.ip_network("::1/128")),
        users_file=tmp_path / "users.yaml",
    )
    values.update(overrides)
    return WebSettings(**values)


def build_web_settings(tmp_path: Path, **web_overrides):
    base = build_settings(tmp_path)
    monitoring = dataclasses.replace(base.monitoring, web=web_settings(tmp_path, **web_overrides))
    return dataclasses.replace(base, monitoring=monitoring)


@contextmanager
def web_client(
    monkeypatch,
    tmp_path: Path,
    *,
    settings=None,
    client_addr=("127.0.0.1", 50000),
    base_url="https://testserver",
    users: list[dict] | None = None,
    **web_overrides,
):
    """App with monitoring + web interface enabled. Requires the conftest fake
    Stirling/Ollama fixtures."""
    use_fast_argon2(monkeypatch)
    settings = settings or build_web_settings(tmp_path, **web_overrides)
    users_file = settings.monitoring.web.users_file
    if users is not None:
        write_users(users_file, users)
    monkeypatch.setattr(main_module, "load_settings", lambda: settings)
    with TestClient(
        main_module.app, base_url=base_url, client=client_addr, raise_server_exceptions=False
    ) as client:
        client.web = main_module.app.state.monitoring_web
        client.users = users
        yield client


def code_for(secret: str, offset_steps: int = 0, at: float | None = None) -> str:
    now = time.time() if at is None else at
    return totp.code_at(totp.decode_secret(secret), totp.counter_at(now) + offset_steps)


_CSRF_FIELD = re.compile(r'name="csrf" value="([^"]+)"')


def csrf_of(response) -> str:
    match = _CSRF_FIELD.search(response.text)
    assert match, response.text[:500]
    return match.group(1)


def post_form(client, path: str, data: dict, *, origin: str | None = ORIGIN, headers: dict | None = None):
    hdrs = {"Content-Type": "application/x-www-form-urlencoded"}
    if origin is not None:
        hdrs["Origin"] = origin
    hdrs.update(headers or {})
    return client.post(path, data=data, headers=hdrs, follow_redirects=False)


def password_step(client, username: str = "alice", password: str = PASSWORD):
    page = client.get("/monitor/login")
    return post_form(
        client, "/monitor/login", {"csrf": csrf_of(page), "username": username, "password": password}
    )


def full_login(client, secret: str, username: str = "alice", password: str = PASSWORD, offset_steps: int = 0):
    first = password_step(client, username, password)
    assert first.status_code == 303 and first.headers["location"] == "/monitor/login/mfa", first.text[:300]
    mfa_page = client.get("/monitor/login/mfa")
    return post_form(
        client, "/monitor/login/mfa", {"csrf": csrf_of(mfa_page), "code": code_for(secret, offset_steps)}
    )

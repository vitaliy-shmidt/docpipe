"""Web interface activation, configuration safety and users file (Phase M3)."""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from system import main as main_module
from system.monitoring.settings import MonitoringConfigError, parse_monitoring
from system.monitoring.web import netaccess
from system.monitoring.web.authstore import AUTH_DB_FILENAME
from system.monitoring.web.users import UsersFile, UsersUnavailable, parse_users
from system.monitoring.web_settings import WebSettings
from tests.monitoring_support import monitored_client
from tests.monitoring_web_support import (
    FAST_HASHER,
    full_login,
    user_entry,
    web_client,
    write_users,
)

ROUTES = [
    "/monitor",
    "/monitor/login",
    "/monitor/login/mfa",
    "/monitor/api/v1/session",
    "/monitor/api/v1/status",
    "/monitor/static/css/monitor.css",
    "/monitor/static/js/monitor.js",
]


def test_web_is_disabled_by_default():
    assert WebSettings().enabled is False
    assert parse_monitoring({"enabled": True}).web.enabled is False


def base(**web):
    return {"enabled": True, "web": {"enabled": True, "allowed_networks": ["10.8.0.0/24"], **web}}


@pytest.mark.parametrize(
    "raw, message",
    [
        (
            {"enabled": False, "web": {"enabled": True, "allowed_networks": ["10.0.0.0/8"]}},
            "requires monitoring",
        ),
        (base(allowed_networks=[]), "must not be empty"),
        ({"enabled": True, "web": {"enabled": True}}, "must not be empty"),
        (base(allowed_networks=["0.0.0.0/0"]), "public_mfa"),
        (base(allowed_networks=["::/0"]), "public_mfa"),
        (base(allowed_networks=["not-a-network"]), "not a valid IP network"),
        (base(allowed_networks="10.0.0.0/8"), "list of CIDR"),
        (base(require_https=False), "loopback"),
        (base(access_mode="open"), "access_mode"),
        (
            {"enabled": True, "web": {"enabled": True, "access_mode": "public_mfa", "require_https": False}},
            "cannot be disabled",
        ),
        (
            {
                "enabled": True,
                "web": {"enabled": True, "access_mode": "public_mfa", "allowed_networks": ["10.0.0.0/8"]},
            },
            "not used",
        ),
        (
            {
                "enabled": True,
                "web": {"enabled": True, "access_mode": "public_mfa", "public_origin": "http://x.example"},
            },
            "https",
        ),
        (base(trusted_proxies=["0.0.0.0/0"]), "trusted_proxies"),
        (base(session_idle_minutes=120), "session_idle_minutes"),
        (base(session_absolute_hours=0), "session_absolute_hours"),
        (base(totp_window_steps=3), "totp_window_steps"),
        (base(login={"ip_max_failures": 1000}), "ip_max_failures"),
        (base(login={"unknown": 1}), "unknown keys"),
        (base(public_origin="javascript:alert(1)"), "public_origin"),
        (base(bogus=True), "unknown keys"),
    ],
)
def test_unsafe_or_incomplete_web_config_is_rejected(raw, message):
    with pytest.raises(MonitoringConfigError, match=message):
        parse_monitoring(raw)


def test_valid_modes_parse():
    allow = parse_monitoring(base(trusted_proxies=["127.0.0.1/32", "::1/128"]))
    assert allow.web.access_mode == "allowlist" and len(allow.web.trusted_proxies) == 2
    public = parse_monitoring(
        {
            "enabled": True,
            "web": {
                "enabled": True,
                "access_mode": "public_mfa",
                "public_origin": "https://mon.example.org/",
            },
        }
    )
    assert public.web.require_https is True and public.web.public_origin == "https://mon.example.org"
    local = parse_monitoring(base(allowed_networks=["127.0.0.1/32"], require_https=False))
    assert local.web.require_https is False


def test_fully_disabled_monitoring_has_no_web_and_no_files(client, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for path in ROUTES:
        assert client.get(path).status_code == 404
    assert client.app.state.monitoring_web is None
    assert list(tmp_path.iterdir()) == []


def test_collection_without_web_interface_exposes_nothing(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path
):
    with monitored_client(monkeypatch, tmp_path) as client:
        unknown = client.get("/definitely-unknown").json()
        for path in ROUTES:
            response = client.get(path)
            assert response.status_code == 404 and response.json() == unknown
        assert client.app.state.monitoring_web is None
    assert not (tmp_path / "mon" / AUTH_DB_FILENAME).exists()


def test_missing_users_file_locks_the_interface(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path
):
    with web_client(monkeypatch, tmp_path, users=None) as client:
        for path in ("/monitor/login", "/monitor", "/monitor/api/v1/status"):
            response = client.get(path, follow_redirects=False)
            assert response.status_code == 503, path
            assert "alice" not in response.text and "collector" not in response.text
        audit = client.web.store._db().execute("SELECT event, reason FROM audit").fetchall()
    assert ("security_config_error", "users_file_missing") in audit


@pytest.mark.parametrize(
    "mutate, reason",
    [
        (lambda u: u.update(totp_secret=""), "mfa_not_configured"),
        (lambda u: u.pop("totp_secret"), "mfa_not_configured"),
        (lambda u: u.update(totp_secret="not base32!"), "mfa_not_configured"),
        (lambda u: u.update(totp_secret="ABCDEFGH"), "mfa_not_configured"),  # < 128 bit
        (lambda u: u.update(password_hash="plaintext-password"), "users_file_invalid"),
        (lambda u: u.update(password_hash="$2b$12$bcrypt"), "users_file_invalid"),
        (lambda u: u.update(username="Bad Name"), "users_file_invalid"),
        (lambda u: u.update(extra="x"), "users_file_invalid"),
        (lambda u: u.update(enabled="yes"), "users_file_invalid"),
    ],
)
def test_invalid_users_are_rejected(mutate, reason):
    import yaml

    entry = user_entry()
    mutate(entry)
    with pytest.raises(UsersUnavailable) as exc:
        parse_users(yaml.safe_dump({"version": 1, "users": [entry]}))
    assert exc.value.reason == reason


@pytest.mark.parametrize(
    "text",
    ["", "users: []", "version: 2\nusers: []", "{{{", "version: 1\nusers: {}", "version: 1\nusers: []"],
)
def test_malformed_users_files_are_rejected(text):
    with pytest.raises(UsersUnavailable):
        parse_users(text)


def test_duplicate_usernames_are_rejected():
    import yaml

    with pytest.raises(UsersUnavailable):
        parse_users(yaml.safe_dump({"version": 1, "users": [user_entry(), user_entry()]}))


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o660, 0o666, 0o700])
def test_users_file_with_too_wide_permissions_is_rejected(tmp_path, mode):
    path = tmp_path / "users.yaml"
    write_users(path, [user_entry()])
    os.chmod(path, mode)
    with pytest.raises(UsersUnavailable) as exc:
        UsersFile(path).users()
    assert exc.value.reason == "users_file_permissions"


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_users_file_of_another_owner_is_rejected(tmp_path, monkeypatch):
    """Phase M7 review: the owner check had no test. A users file that does not
    belong to the process user (e.g. root-owned copy) locks the interface."""
    from system.monitoring.web import users as users_module

    path = tmp_path / "users.yaml"
    write_users(path, [user_entry()])
    owner = path.stat().st_uid
    monkeypatch.setattr(users_module.os, "geteuid", lambda: owner + 1)
    with pytest.raises(UsersUnavailable) as exc:
        UsersFile(path).users()
    assert exc.value.reason == "users_file_owner"


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_users_file_0600_and_0400_are_accepted(tmp_path):
    path = tmp_path / "users.yaml"
    write_users(path, [user_entry()])
    for mode in (0o600, 0o400):
        os.chmod(path, mode)
        assert set(UsersFile(path).users()) == {"alice"}


def test_windows_development_mode_reports_unchecked_permissions(tmp_path):
    path = tmp_path / "users.yaml"
    write_users(path, [user_entry()])
    users = UsersFile(path)
    assert set(users.users()) == {"alice"}
    assert users.permissions_checked is (os.name == "posix")


def test_users_file_is_reloaded_and_fails_closed_on_breakage(tmp_path):
    path = tmp_path / "users.yaml"
    write_users(path, [user_entry("alice")])
    users = UsersFile(path)
    assert set(users.users()) == {"alice"}
    write_users(path, [user_entry("alice"), user_entry("bob")])
    os.utime(path, ns=(1, 1))  # make sure the signature changes even on coarse clocks
    assert set(users.users()) == {"alice", "bob"}
    path.write_text("version: 1\nusers: broken", encoding="utf-8")
    with pytest.raises(UsersUnavailable):
        users.users()
    with pytest.raises(UsersUnavailable):
        users.users()  # stays closed until fixed
    path.unlink()
    with pytest.raises(UsersUnavailable):
        users.get("alice")


def test_password_hashes_use_production_argon2id_parameters():
    from system.monitoring.web import passwords

    hashed = passwords.hash_password("a sufficiently long passphrase")
    assert hashed.startswith("$argon2id$v=19$m=65536,t=3,p=1$")
    with pytest.raises(ValueError):
        passwords.hash_password("short")
    assert FAST_HASHER.hash("x").startswith("$argon2id$")


# --- uvicorn proxy header guard (H5) ---------------------------------------------------------------


@pytest.mark.parametrize(
    "argv, env, rewrites",
    [
        (["/usr/local/bin/uvicorn", "system.main:app", "--host", "0.0.0.0"], {}, True),
        (["/usr/local/bin/uvicorn", "system.main:app", "--no-proxy-headers"], {}, False),
        (["uvicorn", "system.main:app"], {"UVICORN_PROXY_HEADERS": "false"}, False),
        (["/x/site-packages/uvicorn/__main__.py", "system.main:app"], {}, True),
        (["C:\\venv\\Scripts\\uvicorn.exe", "system.main:app"], {}, True),
        (["/usr/bin/pytest", "tests"], {}, False),
        ([], {}, False),
    ],
)
def test_uvicorn_proxy_header_rewriting_is_detected(argv, env, rewrites):
    assert netaccess.uvicorn_rewrites_proxy_headers(argv, env) is rewrites


def test_interface_is_locked_when_uvicorn_would_rewrite_forwarded_headers(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path
):
    entry = user_entry()
    monkeypatch.setattr(netaccess, "uvicorn_rewrites_proxy_headers", lambda argv=None, env=None: True)
    with web_client(monkeypatch, tmp_path, users=[entry]) as client:
        assert client.web.locked_reason == "uvicorn_proxy_headers"
        assert client.get("/monitor/login").status_code == 503
        assert client.get("/api/v1/health").status_code == 200  # the API is unaffected


def test_web_interface_requires_https_by_default(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path
):
    entry = user_entry()
    with web_client(monkeypatch, tmp_path, users=[entry], base_url="http://testserver") as client:
        assert client.get("/monitor/login").status_code == 403


def test_local_development_without_https_on_loopback_only(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path
):
    import ipaddress

    entry = user_entry()
    with web_client(
        monkeypatch,
        tmp_path,
        users=[entry],
        base_url="http://testserver",
        require_https=False,
        allowed_networks=(ipaddress.ip_network("127.0.0.1/32"),),
    ) as client:
        assert client.get("/monitor/login").status_code == 200


def test_unconstructable_interface_is_absent_not_open(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path
):
    from system.monitoring.web import app as web_app

    monkeypatch.setattr(web_app, "MonitorWeb", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    with web_client(monkeypatch, tmp_path, users=[user_entry()]) as client:
        assert client.web is None
        assert client.get("/monitor").status_code == 404
        assert client.get("/api/v1/health").status_code == 200


def test_full_login_works_end_to_end(fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path):
    entry = user_entry()
    with web_client(monkeypatch, tmp_path, users=[entry]) as client:
        response = full_login(client, entry["totp_secret"])
        assert response.status_code == 303 and response.headers["location"] == "/monitor"
        assert client.get("/monitor").status_code == 200
    assert TestClient  # imported for type parity with other modules
    assert main_module.app.state.monitoring_web is None  # shut down with the app

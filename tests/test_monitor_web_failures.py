"""Fail-closed behaviour, customer keys, canaries, headers and UI rules (Phase M3)."""

from __future__ import annotations

import logging
import re
import sqlite3

import pytest

from system.monitoring.web.app import SECURITY_HEADERS, SESSION_COOKIE
from tests.conftest import (
    AI_KEY,
    ASSISTANT_KEY,
    DISABLED_KEY,
    LAB_KEY,
    LAB_NO_AI_KEY,
    NO_AI_KEY,
    NO_DOCS_KEY,
    VALID_KEY,
    auth_headers,
)
from tests.monitoring_support import CANARY, dump_text
from tests.monitoring_web_support import (
    PASSWORD,
    csrf_of,
    full_login,
    password_step,
    post_form,
    user_entry,
    web_client,
    write_users,
)

CUSTOMER_KEYS = [
    VALID_KEY,
    NO_DOCS_KEY,
    DISABLED_KEY,
    AI_KEY,
    NO_AI_KEY,
    "ai-override-key",
    LAB_KEY,
    LAB_NO_AI_KEY,
    ASSISTANT_KEY,
]
PROTECTED_GET = ["/monitor", "/monitor/api/v1/session", "/monitor/api/v1/status"]


@pytest.mark.parametrize("key", CUSTOMER_KEYS)
def test_customer_api_keys_never_grant_monitoring_access(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path, key
):
    with web_client(monkeypatch, tmp_path, users=[user_entry()]) as client:
        for path in PROTECTED_GET:
            for headers in (auth_headers(key), {"X-Api-Key": key}, {"Cookie": f"{SESSION_COOKIE}={key}"}):
                response = client.get(path, headers=headers, follow_redirects=False)
                assert response.status_code in (303, 401), (path, headers)
                assert "collector" not in response.text and "username" not in response.text
        logout = post_form(client, "/monitor/logout", {"csrf": key}, headers=auth_headers(key))
        assert logout.status_code == 303 and logout.headers["location"] == "/monitor/login"


def test_unauthenticated_requests_get_no_technical_information(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path
):
    with web_client(monkeypatch, tmp_path, users=[user_entry()]) as client:
        assert client.get("/monitor", follow_redirects=False).status_code == 303
        for path in ("/monitor/api/v1/session", "/monitor/api/v1/status"):
            response = client.get(path)
            assert response.status_code == 401 and response.json() == {"ok": False, "code": "unauthorized"}
        login = client.get("/monitor/login")
        for forbidden in ("collector", "store", "ollama", "stirling", "0.1.0", "alice", "client"):
            assert forbidden not in login.text.lower().replace("stirling pdf", "")


def test_auth_store_failure_fails_closed(fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path):
    entry = user_entry()
    with web_client(monkeypatch, tmp_path, users=[entry]) as client:
        full_login(client, entry["totp_secret"])
        assert client.get("/monitor/api/v1/status").status_code == 200

        def broken(*args, **kwargs):
            raise sqlite3.OperationalError("disk I/O error")

        for name in ("get_session", "locked", "create_pending", "audit", "complete_login"):
            monkeypatch.setattr(client.web.store, name, broken)
        for path in PROTECTED_GET:
            response = client.get(path, follow_redirects=False)
            assert response.status_code == 503 and "collector" not in response.text, path
        assert client.get("/monitor/login").status_code == 503  # no form, no CSRF token
        assert password_step_status(client) in (403, 503)  # denied either way (CSRF is checked first)
        assert client.get("/api/v1/health").status_code == 200  # the API is unaffected


def test_unopenable_auth_store_locks_the_interface(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path
):
    from system.monitoring.web import authstore

    monkeypatch.setattr(
        authstore.AuthStore, "open", lambda self: (_ for _ in ()).throw(sqlite3.OperationalError())
    )
    with web_client(monkeypatch, tmp_path, users=[user_entry()]) as client:
        assert client.web.locked_reason == "auth_store_unavailable"
        assert client.get("/monitor/login").status_code == 503
        assert password_step_status(client) == 503


def password_step_status(client) -> int:
    return post_form(
        client, "/monitor/login", {"csrf": "x", "username": "alice", "password": PASSWORD}
    ).status_code


def test_telemetry_store_failure_does_not_affect_authentication(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path
):
    entry = user_entry()
    with web_client(monkeypatch, tmp_path, users=[entry]) as client:
        client.monitor = client.app.state.monitoring
        client.monitor.store.close()
        monkeypatch.setattr(
            client.monitor.store, "open", lambda: (_ for _ in ()).throw(OSError("telemetry down"))
        )
        assert full_login(client, entry["totp_secret"]).status_code == 303
        status = client.get("/monitor/api/v1/status").json()
        assert status["ok"] is True and status["store"] in ("open", "unavailable", "write_failing")


def test_broken_users_file_ends_access_for_existing_sessions(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path
):
    import os

    entry = user_entry()
    with web_client(monkeypatch, tmp_path, users=[entry]) as client:
        full_login(client, entry["totp_secret"])
        client.web.users.path.write_text("version: 1\nusers: [", encoding="utf-8")
        os.utime(client.web.users.path, ns=(3, 3))
        for path in PROTECTED_GET:
            assert client.get(path, follow_redirects=False).status_code == 503
        write_users(client.web.users.path, [entry])
        os.utime(client.web.users.path, ns=(4, 4))
        assert client.get("/monitor/api/v1/session").status_code == 200


def test_no_secrets_in_audit_logs_html_or_api(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path, caplog
):
    caplog.set_level(logging.DEBUG)
    entry = user_entry(password=CANARY + "-pw")
    with web_client(monkeypatch, tmp_path, users=[entry]) as client:
        password_step(client, CANARY.lower(), CANARY)  # canary as typed username and password
        password_step(client, "alice", CANARY + "-wrong")
        login = full_login(client, entry["totp_secret"], password=CANARY + "-pw")
        session_token = client.cookies.get(SESSION_COOKIE)
        shell = client.get("/monitor")
        bodies = [
            shell.text,
            client.get("/monitor/api/v1/session").text,
            client.get("/monitor/api/v1/status").text,
            login.text,
        ]
        auth_dump = dump_text(client.web.store.path)
        telemetry_dump = (
            dump_text(client.app.state.monitoring.store.path)
            if client.app.state.monitoring.store.is_open
            else ""
        )
        files = b"".join(
            p.read_bytes()
            for p in client.web.store.path.parent.iterdir()
            if p.is_file() and p.name.startswith("monitor-auth")
        )
    for text in (*bodies, auth_dump, telemetry_dump, caplog.text):
        assert CANARY not in text and CANARY.lower() not in text
        assert session_token not in text
        assert entry["totp_secret"] not in text and entry["password_hash"] not in text
    assert CANARY.encode() not in files and session_token.encode() not in files


def test_security_headers_and_csp_on_every_response(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path
):
    entry = user_entry()
    with web_client(monkeypatch, tmp_path, users=[entry]) as client:
        responses = [
            client.get("/monitor/login"),
            client.get("/monitor/nope"),
            client.put("/monitor"),
            client.get("/monitor/api/v1/status"),
            client.get("/monitor/static/js/monitor.js"),
            client.get("/monitor/static/%2e%2e/%2e%2e/config.yaml"),
            client.get("/monitor", follow_redirects=False),
        ]
        full_login(client, entry["totp_secret"])
        responses += [client.get("/monitor"), client.get("/monitor/api/v1/session")]
    for response in responses:
        for name, value in SECURITY_HEADERS.items():
            assert response.headers.get(name) == value, (response.url, name)
        csp = response.headers["content-security-policy"]
        assert "'unsafe-inline'" not in csp and "'unsafe-eval'" not in csp and "http" not in csp
    static = responses[4]
    assert static.status_code == 200 and static.headers["cache-control"] == "private, max-age=3600"
    assert all(r.headers["cache-control"] == "no-store" for r in responses if r is not static)
    assert responses[5].status_code == 404  # no path traversal


def test_pages_have_no_inline_code_no_external_resources_and_escape_output(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path
):
    entry = user_entry()
    with web_client(monkeypatch, tmp_path, users=[entry]) as client:
        pages = [
            client.get("/monitor/login"),
            client.get("/monitor/login", headers={"Accept-Language": "de-DE"}),
        ]
        full_login(client, entry["totp_secret"])
        pages.append(client.get("/monitor"))
    for page in pages:
        html = page.text
        assert not re.search(r"<script(?![^>]*\bsrc=)", html)  # every script is a local file
        assert not re.search(r"\son[a-z]+=", html) and " style=" not in html and "javascript:" not in html
        assert not re.search(r"(src|href)=\"(https?:)?//", html)  # no CDN, no external resource
    assert pages[0].text.count('lang="en"') >= 1 and "Sign in" in pages[0].text
    assert 'lang="de"' in pages[1].text and "Anmelden" in pages[1].text
    shell = pages[2].text
    assert "Übersicht" not in shell and "Overview" in shell
    # not yet implemented (Logs: M6, Connectors: #2) are disabled; Ollama and Stirling are live since M5
    assert shell.count('class="nav-link disabled') == 2 and shell.count("planned") >= 2
    assert 'href="/monitor/ollama"' in shell and 'href="/monitor/stirling"' in shell


def test_language_choice_and_fallback():
    from system.monitoring.web.i18n import choose_language

    assert choose_language(None, "de-DE,de;q=0.9,en;q=0.8") == "de"
    assert choose_language(None, "fr-FR,en;q=0.5,de;q=0.4") == "en"
    assert choose_language(None, "fr-FR,es") == "en"
    assert choose_language(None, None) == "en"
    assert choose_language("de", "en") == "de"
    assert choose_language("xx", "de") == "de"
    assert choose_language(None, "en;q=0, de;q=0.1") == "de"


def test_language_switch_cookie_is_honoured(fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path):
    with web_client(monkeypatch, tmp_path, users=[user_entry()]) as client:
        client.cookies.set("dp_mon_lang", "de")
        assert "Benutzername" in client.get("/monitor/login", headers={"Accept-Language": "en"}).text
        client.cookies.set("dp_mon_lang", "<script>")
        assert "Username" in client.get("/monitor/login").text


def test_shell_csrf_token_is_bound_to_the_session(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path
):
    entry = user_entry()
    with web_client(monkeypatch, tmp_path, users=[entry]) as client:
        full_login(client, entry["totp_secret"])
        token = client.cookies.get(SESSION_COOKIE)
        csrf = csrf_of(client.get("/monitor"))
    assert csrf != token and token not in csrf and len(csrf) == 64

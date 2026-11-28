"""Login, MFA, replay protection and session management (Phase M3)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from system import main as main_module
from system.monitoring.web.app import MFA_COOKIE, SESSION_COOKIE
from tests.monitoring_web_support import (
    PASSWORD,
    code_for,
    csrf_of,
    full_login,
    password_step,
    post_form,
    user_entry,
    web_client,
    write_users,
)


def audit(client) -> list[tuple]:
    return client.web.store._db().execute("SELECT event, username, reason FROM audit ORDER BY id").fetchall()


def set_clock(client, ms: int) -> None:
    client.web._clock = lambda: ms


def test_successful_login_creates_a_secure_session(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path
):
    entry = user_entry()
    with web_client(monkeypatch, tmp_path, users=[entry]) as client:
        response = full_login(client, entry["totp_secret"])
        cookie = next(
            h for h in response.headers.get_list("set-cookie") if h.startswith(SESSION_COOKIE + "=")
        )
        attributes = {part.strip().split("=")[0].lower() for part in cookie.split(";")[1:]}
        assert {"httponly", "secure", "path", "samesite"} <= attributes
        assert "domain" not in attributes and "max-age" not in attributes
        assert "samesite=strict" in cookie.lower() and "path=/" in cookie.lower()
        token = cookie.split(";")[0].split("=", 1)[1]
        shell = client.get("/monitor")
        stored = client.web.store._db().execute("SELECT id_hash FROM sessions").fetchall()
        events = audit(client)
    assert shell.status_code == 200 and "alice" in shell.text
    assert len(stored) == 1 and stored[0][0] != token and len(stored[0][0]) == 64  # only the hash is stored
    assert ("login_success", "alice", None) in events


def test_wrong_password_and_unknown_user_look_identical(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path
):
    with web_client(monkeypatch, tmp_path, users=[user_entry()]) as client:
        wrong = password_step(client, "alice", "wrong-password-123")
        unknown = password_step(client, "mallory", PASSWORD)
        invalid = password_step(client, "x" * 200, PASSWORD)
        events = audit(client)
    assert wrong.status_code == unknown.status_code == invalid.status_code == 401
    strip = lambda r: r.text.replace(csrf_of(r), "")  # noqa: E731
    assert strip(wrong) == strip(unknown) == strip(invalid)
    assert MFA_COOKIE not in str(wrong.headers) and MFA_COOKIE not in str(unknown.headers)
    assert ("login_failed", "alice", "credentials") in events
    assert ("login_failed", None, "credentials") in events  # the typed unknown name is never stored
    assert "mallory" not in str(events)


def test_disabled_user_cannot_sign_in(fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path):
    with web_client(monkeypatch, tmp_path, users=[user_entry(enabled=False)]) as client:
        assert password_step(client).status_code == 401


def test_no_session_before_the_second_factor(fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path):
    entry = user_entry()
    with web_client(monkeypatch, tmp_path, users=[entry]) as client:
        password_step(client)
        assert client.get("/monitor", follow_redirects=False).status_code == 303
        assert client.get("/monitor/api/v1/status").status_code == 401
        assert client.web.store._db().execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


def test_invalid_totp_is_rejected_and_attempts_are_limited(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path
):
    entry = user_entry()
    with web_client(monkeypatch, tmp_path, users=[entry]) as client:
        password_step(client)
        page = client.get("/monitor/login/mfa")
        csrf = csrf_of(page)
        wrong = str((int(code_for(entry["totp_secret"])) + 1) % 1_000_000).zfill(6)
        results = [
            post_form(client, "/monitor/login/mfa", {"csrf": csrf, "code": wrong}).status_code
            for _ in range(5)
        ]
        # attempts exhausted: even the correct code no longer works for this pending login
        after = post_form(
            client, "/monitor/login/mfa", {"csrf": csrf, "code": code_for(entry["totp_secret"])}
        )
        events = [e[0] for e in audit(client)]
    assert results == [401] * 5
    assert after.status_code == 401 and "expired" in after.text.lower()
    assert events.count("mfa_failed") == 5


def test_totp_code_cannot_be_reused(fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path):
    entry = user_entry()
    with web_client(monkeypatch, tmp_path, users=[entry]) as client:
        assert full_login(client, entry["totp_secret"]).status_code == 303
        shell = client.get("/monitor")
        post_form(client, "/monitor/logout", {"csrf": csrf_of(shell)})
        replay = full_login(client, entry["totp_secret"])  # same code, same time step
        events = [e[0] for e in audit(client)]
        later = full_login(client, entry["totp_secret"], offset_steps=1)  # next step is fine
    assert replay.status_code == 401
    assert "totp_replay" in events
    assert later.status_code == 303


def test_parallel_mfa_requests_with_the_same_code_succeed_once(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path
):
    entry = user_entry()
    with web_client(monkeypatch, tmp_path, users=[entry]) as first:
        clients = [first] + [
            TestClient(main_module.app, base_url="https://testserver", client=("127.0.0.1", 6000 + i))
            for i in range(7)
        ]
        forms = []
        for c in clients:
            assert password_step(c).status_code == 303
            forms.append(csrf_of(c.get("/monitor/login/mfa")))
        code = code_for(entry["totp_secret"])
        with ThreadPoolExecutor(max_workers=8) as pool:
            statuses = list(
                pool.map(
                    lambda pair: (
                        post_form(pair[0], "/monitor/login/mfa", {"csrf": pair[1], "code": code}).status_code
                    ),
                    zip(clients, forms),
                )
            )
        sessions = first.web.store._db().execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    assert statuses.count(303) == 1 and statuses.count(401) == 7
    assert sessions == 1


def test_replay_check_is_atomic_across_store_connections(tmp_path):
    from system.monitoring.web.authstore import AuthStore

    stores = [AuthStore(tmp_path / "auth.db", 64 * 1024 * 1024) for _ in range(4)]  # like 4 workers
    for s in stores:
        s.open()
    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda i: stores[i % 4].accept_totp_counter("alice", 1000), range(64)))
    assert results.count(True) == 1
    assert stores[0].accept_totp_counter("alice", 999) is False
    assert stores[1].accept_totp_counter("alice", 1001) is True
    for s in stores:
        s.close()


def test_session_fixation_is_impossible(fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path):
    entry = user_entry()
    with web_client(monkeypatch, tmp_path, users=[entry]) as client:
        planted = "attacker-chosen-session-value-1234567890"
        client.cookies.set(SESSION_COOKIE, planted, domain="testserver.local", path="/")
        assert client.get("/monitor", follow_redirects=False).status_code == 303
        response = full_login(client, entry["totp_secret"])
        issued = next(
            h for h in response.headers.get_list("set-cookie") if h.startswith(SESSION_COOKIE + "=")
        )
        assert planted not in issued
        # an existing valid session is replaced (rotation), not reused
        first_token = issued.split(";")[0].split("=", 1)[1]
        shell = client.get("/monitor")
        post_form(client, "/monitor/logout", {"csrf": csrf_of(shell)})
        client.cookies.set(SESSION_COOKIE, first_token)
        assert client.get("/monitor/api/v1/session").status_code == 401


def test_idle_timeout_and_polling_does_not_extend_it(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path
):
    entry = user_entry()
    with web_client(monkeypatch, tmp_path, users=[entry]) as client:
        base = client.web._clock()
        set_clock(client, base)
        full_login(client, entry["totp_secret"])
        for minute in range(1, 15):  # dashboard polls every minute for 14 minutes
            set_clock(client, base + minute * 60_000)
            assert client.get("/monitor/api/v1/status", headers={"X-Monitor-Poll": "1"}).status_code == 200
            # omitting the header does not help either: API requests never count as activity
            assert client.get("/monitor/api/v1/session").status_code == 200
        set_clock(client, base + 15 * 60_000 + 1)
        assert client.get("/monitor/api/v1/status").status_code == 401
        assert ("session_expired", "alice", "idle") in audit(client)


def test_page_navigation_is_activity(fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path):
    entry = user_entry()
    with web_client(monkeypatch, tmp_path, users=[entry]) as client:
        base = client.web._clock()
        set_clock(client, base)
        full_login(client, entry["totp_secret"])
        set_clock(client, base + 10 * 60_000)
        assert client.get("/monitor").status_code == 200  # navigation
        set_clock(client, base + 20 * 60_000)
        assert client.get("/monitor/api/v1/session").status_code == 200


def test_absolute_timeout_ends_even_an_active_session(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path
):
    entry = user_entry()
    with web_client(monkeypatch, tmp_path, users=[entry]) as client:
        base = client.web._clock()
        set_clock(client, base)
        full_login(client, entry["totp_secret"])
        for step in range(1, 60):
            set_clock(client, base + step * 12 * 60_000)  # active every 12 minutes for ~8 h
            if client.get("/monitor", follow_redirects=False).status_code != 200:
                break
        assert step * 12 * 60_000 >= 8 * 3_600_000
        assert ("session_expired", "alice", "absolute") in audit(client)


def test_logout_invalidates_the_session_server_side(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path
):
    entry = user_entry()
    with web_client(monkeypatch, tmp_path, users=[entry]) as client:
        full_login(client, entry["totp_secret"])
        token = client.cookies.get(SESSION_COOKIE)
        shell = client.get("/monitor")
        response = post_form(client, "/monitor/logout", {"csrf": csrf_of(shell)})
        assert response.status_code == 303 and response.headers["location"] == "/monitor/login"
        assert any(h.startswith(SESSION_COOKIE + '=""') for h in response.headers.get_list("set-cookie"))
        client.cookies.set(SESSION_COOKIE, token)  # replay the old cookie
        assert client.get("/monitor/api/v1/session").status_code == 401
        assert client.web.store._db().execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
        assert ("logout", "alice", None) in audit(client)


def test_manipulated_session_cookies_are_rejected(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path
):
    entry = user_entry()
    with web_client(monkeypatch, tmp_path, users=[entry]) as client:
        full_login(client, entry["totp_secret"])
        token = client.cookies.get(SESSION_COOKIE)
        for bad in (
            token[:-1] + ("A" if token[-1] != "A" else "B"),
            token + "x",
            "",
            "a" * 500,
            "x;y",
            "%00",
        ):
            client.cookies.clear()
            client.cookies.set(SESSION_COOKIE, bad)
            assert client.get("/monitor/api/v1/status").status_code == 401, bad
            assert client.get("/monitor", follow_redirects=False).status_code == 303


def test_changed_or_disabled_user_loses_sessions(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path
):
    import os

    entry = user_entry()
    with web_client(monkeypatch, tmp_path, users=[entry]) as client:
        full_login(client, entry["totp_secret"])
        assert client.get("/monitor/api/v1/session").status_code == 200
        write_users(client.web.users.path, [{**entry, "enabled": False}])
        os.utime(client.web.users.path, ns=(2, 2))
        assert client.get("/monitor/api/v1/session").status_code == 401
        assert ("session_revoked", "alice", "credentials_changed") in audit(client)


def test_sessions_per_user_are_limited(fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path):
    entry = user_entry()
    with web_client(monkeypatch, tmp_path, users=[entry], max_sessions_per_user=2):
        tokens = []
        for step in range(3):
            c = TestClient(main_module.app, base_url="https://testserver", client=("127.0.0.1", 7000 + step))
            assert full_login(c, entry["totp_secret"], offset_steps=step - 1).status_code == 303
            tokens.append(c)
        statuses = [c.get("/monitor/api/v1/session").status_code for c in tokens]
    assert statuses == [401, 200, 200]  # the oldest session was ended


def test_expired_pending_login_requires_a_new_password_step(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path
):
    entry = user_entry()
    with web_client(monkeypatch, tmp_path, users=[entry]) as client:
        base = client.web._clock()
        set_clock(client, base)
        password_step(client)
        csrf = csrf_of(client.get("/monitor/login/mfa"))
        set_clock(client, base + 301_000)
        response = post_form(
            client, "/monitor/login/mfa", {"csrf": csrf, "code": code_for(entry["totp_secret"])}
        )
        assert (
            response.status_code == 401
            and client.web.store._db().execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
        )
        assert client.get("/monitor/login/mfa", follow_redirects=False).status_code == 303

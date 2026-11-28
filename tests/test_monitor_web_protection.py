"""CSRF, origin checks, brute-force protection and rate limits (Phase M3)."""

from __future__ import annotations

import threading
import time

from system.monitoring.web import passwords
from system.monitoring.web.app import CSRF_COOKIE
from system.monitoring.web.authstore import AuthStore
from system.monitoring.web.passwords import VerificationLimiter
from system.monitoring.web_settings import LoginLimits
from tests.monitoring_web_support import (
    PASSWORD,
    code_for,
    csrf_of,
    full_login,
    password_step,
    post_form,
    user_entry,
    web_client,
)


def events(client) -> list[str]:
    return [r[0] for r in client.web.store._db().execute("SELECT event FROM audit ORDER BY id")]


def test_login_requires_the_csrf_token(fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path):
    with web_client(monkeypatch, tmp_path, users=[user_entry()]) as client:
        page = client.get("/monitor/login")
        token = csrf_of(page)
        data = {"username": "alice", "password": PASSWORD}
        missing = post_form(client, "/monitor/login", data)
        wrong = post_form(client, "/monitor/login", {**data, "csrf": token[:-2] + "xx"})
        client.cookies.delete(CSRF_COOKIE)
        no_cookie = post_form(client, "/monitor/login", {**data, "csrf": token})
        recorded = events(client)
    assert missing.status_code == wrong.status_code == no_cookie.status_code == 403
    assert "csrf_failed" in recorded and "login_failed" not in recorded  # rejected before any password check


def test_login_csrf_from_a_foreign_origin_is_rejected(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path
):
    with web_client(monkeypatch, tmp_path, users=[user_entry()]) as client:
        token = csrf_of(client.get("/monitor/login"))
        data = {"csrf": token, "username": "alice", "password": PASSWORD}
        foreign = post_form(client, "/monitor/login", data, origin="https://evil.example")
        null = post_form(client, "/monitor/login", data, origin="null")
        no_origin = post_form(client, "/monitor/login", data, origin=None)
        cross_site = post_form(
            client, "/monitor/login", data, origin=None, headers={"Sec-Fetch-Site": "cross-site"}
        )
        same_site = post_form(
            client, "/monitor/login", data, origin=None, headers={"Sec-Fetch-Site": "same-origin"}
        )
    assert foreign.status_code == null.status_code == no_origin.status_code == cross_site.status_code == 403
    assert same_site.status_code == 303  # same-origin request without Origin header (older browsers)


def test_logout_requires_the_session_bound_token(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path
):
    entry = user_entry()
    with web_client(monkeypatch, tmp_path, users=[entry]) as client:
        full_login(client, entry["totp_secret"])
        shell = client.get("/monitor")
        assert post_form(client, "/monitor/logout", {"csrf": "forged"}).status_code == 403
        assert (
            post_form(
                client, "/monitor/logout", {"csrf": csrf_of(shell)}, origin="https://evil.example"
            ).status_code
            == 403
        )
        assert client.get("/monitor/api/v1/session").status_code == 200  # still signed in
        assert post_form(client, "/monitor/logout", {"csrf": csrf_of(shell)}).status_code == 303


def test_state_changing_requests_need_form_encoding_and_bounded_size(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path
):
    with web_client(monkeypatch, tmp_path, users=[user_entry()]) as client:
        token = csrf_of(client.get("/monitor/login"))
        as_json = client.post(
            "/monitor/login",
            json={"csrf": token, "username": "alice", "password": PASSWORD},
            headers={"Origin": "https://testserver"},
        )
        huge = post_form(
            client, "/monitor/login", {"csrf": token, "username": "alice", "password": "x" * 10_000}
        )
        many = client.post(
            "/monitor/login",
            content="&".join(f"f{i}=1" for i in range(50)),
            headers={"Origin": "https://testserver", "Content-Type": "application/x-www-form-urlencoded"},
        )
        get_logout = client.get("/monitor/logout")
        put = client.put("/monitor/login")
    assert as_json.status_code == 403
    assert huge.status_code == 413
    assert many.status_code in (403, 413)
    assert get_logout.status_code == 405 and put.status_code == 405


def test_brute_force_locks_the_ip(fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path):
    entries = [user_entry(f"user{i}x") for i in range(12)]
    with web_client(monkeypatch, tmp_path, users=entries) as client:
        for i in range(10):
            assert password_step(client, f"user{i}x", "wrong-password-xyz").status_code == 401
        # the IP is locked now: even correct credentials of another user fail uniformly
        locked = password_step(client, "user11x", PASSWORD)
        recorded = events(client)
    assert locked.status_code == 401
    assert "lockout" in recorded and "login_throttled" in recorded


def test_brute_force_locks_the_user_from_any_ip(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path
):
    from fastapi.testclient import TestClient

    from system import main as main_module

    entry = user_entry()
    with web_client(monkeypatch, tmp_path, users=[entry]) as client:
        for i in range(5):
            attacker = TestClient(
                main_module.app, base_url="https://testserver", client=(f"127.0.0.{i + 10}", 1)
            )
            assert password_step(attacker, "alice", "wrong-password-xyz").status_code == 401
        owner = TestClient(main_module.app, base_url="https://testserver", client=("127.0.0.99", 1))
        assert password_step(owner, "alice", PASSWORD).status_code == 401  # user locked
        base = client.web._clock()
        client.web._clock = lambda: base + 16 * 60_000  # lockout (15 min) over
        assert password_step(owner, "alice", PASSWORD).status_code == 303


def test_mfa_failures_count_towards_the_lockout(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path
):
    entry = user_entry()
    with web_client(monkeypatch, tmp_path, users=[entry]) as client:
        wrong = str((int(code_for(entry["totp_secret"])) + 7) % 1_000_000).zfill(6)
        for _ in range(5):
            password_step(client)
            csrf = csrf_of(client.get("/monitor/login/mfa"))
            post_form(client, "/monitor/login/mfa", {"csrf": csrf, "code": wrong})
        # user now locked: a correct password and code no longer produce a session
        response = full_login_allowing_failure(client, entry)
        assert response.status_code == 401
        assert client.web.store._db().execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


def full_login_allowing_failure(client, entry):
    first = password_step(client)
    if first.status_code != 303:
        return first
    csrf = csrf_of(client.get("/monitor/login/mfa"))
    return post_form(client, "/monitor/login/mfa", {"csrf": csrf, "code": code_for(entry["totp_secret"])})


def test_rate_limits_are_shared_between_worker_processes(tmp_path):
    workers = [AuthStore(tmp_path / "auth.db", 64 * 1024 * 1024) for _ in range(3)]
    for w in workers:
        w.open()
    subject = workers[0].subject("ip:10.0.0.1")
    assert all(w.subject("ip:10.0.0.1") == subject for w in workers)  # same key from the shared database
    now = 1_800_000_000_000
    results = []
    lock = threading.Lock()

    def fail(i):
        locked, _ = workers[i % 3].record_failure(
            "ip", subject, now, limit=10, window_ms=900_000, lockout_ms=900_000
        )
        with lock:
            results.append(locked)

    threads = [threading.Thread(target=fail, args=(i,)) for i in range(30)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    row = workers[1]._db().execute("SELECT failures, locked_until_ms FROM rate_limits").fetchone()
    assert row[0] == 30 and row[1] == now + 900_000  # every concurrent failure counted exactly once
    assert results.count(False) == 9 and all(w.locked("ip", subject, now + 1) for w in workers)
    for w in workers:
        w.close()


def test_argon2_verifications_are_bounded(monkeypatch):
    monkeypatch.setattr(
        passwords, "HASHER", __import__("tests.monitoring_web_support", fromlist=["FAST_HASHER"]).FAST_HASHER
    )
    hashed = passwords.HASHER.hash(PASSWORD)
    clock = [0.0]
    limiter = VerificationLimiter(max_concurrency=1, per_minute=3, clock=lambda: clock[0])
    assert [limiter.verify(hashed, PASSWORD) for _ in range(4)] == [True, True, True, None]
    clock[0] = 61.0
    assert limiter.verify(hashed, "wrong") is False
    # concurrency: a held slot makes the next caller give up instead of queueing forever
    limiter = VerificationLimiter(max_concurrency=1, per_minute=100)
    limiter._slots.acquire()
    started = time.monotonic()
    assert limiter.verify(hashed, PASSWORD) is None
    assert time.monotonic() - started < 5
    limiter._slots.release()


def test_throttled_logins_do_not_run_argon2(fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path):
    with web_client(
        monkeypatch,
        tmp_path,
        users=[user_entry()],
        login=LoginLimits(argon2_per_minute=2, ip_max_failures=50, user_max_failures=20),
    ) as client:
        calls = []
        real = passwords.HASHER

        class Counting:
            def verify(self, *args):
                calls.append(1)
                return real.verify(*args)

        monkeypatch.setattr(passwords, "HASHER", Counting())
        statuses = [password_step(client, "alice", "wrong-password-xyz").status_code for _ in range(5)]
        recorded = events(client)
    assert statuses == [401] * 5
    assert len(calls) == 2 and recorded.count("login_throttled") >= 1


def test_failed_monitoring_logins_do_not_affect_the_api(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path
):
    from tests.conftest import VALID_KEY, auth_headers, pdf_bytes

    with web_client(monkeypatch, tmp_path, users=[user_entry()]) as client:
        for _ in range(30):
            password_step(client, "alice", "wrong-password-xyz")
        response = client.post(
            "/api/v1/documents/extract-text",
            headers=auth_headers(VALID_KEY),
            files={"file": ("a.pdf", pdf_bytes(), "application/pdf")},
        )
        assert response.status_code == 200
        assert client.get("/api/v1/health").status_code == 200

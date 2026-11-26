"""Trusted proxy, allowlist, IPv4/IPv6 and HTTPS decisions (hardening H5, Phase M3)."""

from __future__ import annotations

import ipaddress

import pytest

from system.monitoring.web import netaccess
from system.monitoring.web_settings import WebSettings
from tests.monitoring_web_support import user_entry, web_client

net = ipaddress.ip_network


def scope(peer="203.0.113.5", scheme="https", headers=()):
    return {
        "type": "http",
        "client": (peer, 1234) if peer is not None else None,
        "scheme": scheme,
        "headers": [(k.lower().encode(), v.encode("latin-1")) for k, v in headers],
    }


ALLOW = WebSettings(
    enabled=True,
    allowed_networks=(net("10.8.0.0/24"), net("fd00:8::/64")),
    trusted_proxies=(net("127.0.0.1/32"), net("::1/128")),
)


def test_direct_peer_is_the_client_and_forwarded_headers_are_ignored():
    forged = [
        ("X-Forwarded-For", "10.8.0.7"),
        ("X-Forwarded-Proto", "https"),
        ("Forwarded", "for=10.8.0.7"),
        ("X-Real-IP", "10.8.0.7"),
    ]
    decision = netaccess.decide(scope("198.51.100.9", "http", forged), ALLOW)
    assert (decision.allowed, decision.reason, str(decision.client_ip)) == (
        False,
        "network_not_allowed",
        "198.51.100.9",
    )
    direct = netaccess.decide(scope("10.8.0.7", "https", forged), ALLOW)
    assert direct.allowed and str(direct.client_ip) == "10.8.0.7"


def test_trusted_proxy_uses_rightmost_untrusted_forwarded_entry():
    headers = [
        ("X-Forwarded-For", "10.8.0.99, 198.51.100.1"),
        ("X-Forwarded-For", "10.8.0.7, 127.0.0.1"),
        ("X-Forwarded-Proto", "http, https"),
    ]
    decision = netaccess.resolve(scope("127.0.0.1", "http", headers), ALLOW)
    # spoofed left-hand entries are never reached: rightmost untrusted is 10.8.0.7
    assert decision.allowed and str(decision.client_ip) == "10.8.0.7" and decision.https is True


@pytest.mark.parametrize(
    "headers, reason",
    [
        ([], "forwarded_header_invalid"),
        ([("X-Forwarded-For", "127.0.0.1")], "forwarded_header_invalid"),
        ([("X-Forwarded-For", "10.8.0.7, garbage")], "forwarded_header_invalid"),
        ([("X-Forwarded-For", "10.8.0.7:443")], "forwarded_header_invalid"),
        ([("X-Forwarded-For", ",".join(["10.8.0.7"] * 40))], "forwarded_header_invalid"),
        ([("X-Forwarded-For", "fe80::1%eth0")], "forwarded_header_invalid"),
        ([("Forwarded", "for=10.8.0.7;proto=https")], "forwarded_header_invalid"),
    ],
)
def test_malformed_or_missing_forwarding_from_a_proxy_fails_closed(headers, reason):
    decision = netaccess.decide(scope("127.0.0.1", "https", headers), ALLOW)
    assert (decision.allowed, decision.reason) == (False, reason)


def test_https_is_only_taken_from_trusted_proxies():
    direct = netaccess.decide(scope("10.8.0.7", "http", [("X-Forwarded-Proto", "https")]), ALLOW)
    assert (direct.allowed, direct.reason) == (False, "https_required")
    via_proxy = netaccess.decide(
        scope("::1", "http", [("X-Forwarded-For", "10.8.0.7"), ("X-Forwarded-Proto", "https")]), ALLOW
    )
    assert via_proxy.allowed and via_proxy.https
    no_proto = netaccess.decide(scope("::1", "https", [("X-Forwarded-For", "10.8.0.7")]), ALLOW)
    assert (no_proto.allowed, no_proto.reason) == (False, "https_required")  # proxy did not state https


@pytest.mark.parametrize(
    "peer, allowed",
    [
        ("10.8.0.1", True),
        ("10.8.1.1", False),
        ("fd00:8::17", True),
        ("fd00:9::1", False),
        ("::ffff:10.8.0.3", True),
        ("[fd00:8::5]", True),
        ("testclient", False),
        ("", False),
        (None, False),
    ],
)
def test_ipv4_ipv6_and_mapped_addresses(peer, allowed):
    decision = netaccess.decide(scope(peer, "https"), ALLOW)
    assert decision.allowed is allowed


def test_public_mode_allows_any_address_but_only_https():
    public = WebSettings(enabled=True, access_mode="public_mfa")
    assert netaccess.decide(scope("198.51.100.9", "https"), public).allowed
    assert netaccess.decide(scope("2001:db8::1", "https"), public).allowed
    assert not netaccess.decide(scope("198.51.100.9", "http"), public).allowed


# --- through the application -------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/monitor",
        "/monitor/login",
        "/monitor/login/mfa",
        "/monitor/api/v1/status",
        "/monitor/api/v1/session",
        "/monitor/static/css/monitor.css",
        "/monitor/static/vendor/bootstrap/bootstrap.min.css",
        "/monitor/nothing",
    ],
)
def test_every_monitor_path_is_denied_outside_the_allowlist(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path, path
):
    with web_client(
        monkeypatch, tmp_path, users=[user_entry()], client_addr=("198.51.100.9", 4000)
    ) as client:
        response = client.get(path, headers={"X-Forwarded-For": "127.0.0.1"}, follow_redirects=False)
        assert response.status_code == 403
        assert "bootstrap" not in response.text.lower() or "stylesheet" in response.text  # page, not the file
        assert response.headers["content-security-policy"].startswith("default-src 'none'")
        audit = client.web.store._db().execute("SELECT event, ip, reason FROM audit").fetchall()
    assert ("access_denied", "198.51.100.9", "network_not_allowed") in audit


def test_access_denied_audit_is_deduplicated(fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path):
    with web_client(
        monkeypatch, tmp_path, users=[user_entry()], client_addr=("198.51.100.9", 4000)
    ) as client:
        for _ in range(50):
            client.get("/monitor/login")
        rows = (
            client.web.store._db()
            .execute("SELECT repeat_count FROM audit WHERE event = 'access_denied'")
            .fetchall()
        )
    assert rows == [(50,)]


def test_reverse_proxy_setup_through_the_application(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path
):
    with web_client(
        monkeypatch,
        tmp_path,
        users=[user_entry()],
        base_url="http://testserver",
        trusted_proxies=(net("127.0.0.1/32"),),
        allowed_networks=(net("10.8.0.0/24"),),
    ) as client:
        ok = client.get(
            "/monitor/login", headers={"X-Forwarded-For": "10.8.0.7", "X-Forwarded-Proto": "https"}
        )
        spoofed = client.get(
            "/monitor/login",
            headers={"X-Forwarded-For": "10.8.0.7, 198.51.100.9", "X-Forwarded-Proto": "https"},
        )
        plain = client.get("/monitor/login", headers={"X-Forwarded-For": "10.8.0.7"})
    assert ok.status_code == 200
    assert spoofed.status_code == 403  # the proxy appended the real (untrusted) client
    assert plain.status_code == 403  # https required


def test_api_is_not_affected_by_the_monitoring_access_rules(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path
):
    with web_client(
        monkeypatch,
        tmp_path,
        users=[user_entry()],
        client_addr=("198.51.100.9", 4000),
        base_url="http://testserver",
    ) as client:
        assert client.get("/monitor/login").status_code == 403
        assert client.get("/api/v1/health").status_code == 200

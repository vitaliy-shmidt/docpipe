"""Release check (Phase M7): no monitoring route is reachable with customer credentials.

The route list is taken from the web app itself, so routes added later are
covered automatically. Every customer API key of the test configuration,
in every header form DocPipe accepts, plus forged cookies, must never yield
monitoring data or a state change.
"""

from __future__ import annotations

import pytest

from system.monitoring.web import app as web_app
from tests.conftest import (
    AI_KEY,
    AI_OVERRIDE_KEY,
    ASSISTANT_KEY,
    DISABLED_KEY,
    LAB_KEY,
    LAB_NO_AI_KEY,
    NO_AI_KEY,
    NO_DOCS_KEY,
    VALID_KEY,
)
from tests.monitoring_web_support import user_entry, web_client

CUSTOMER_KEYS = (
    VALID_KEY,
    NO_DOCS_KEY,
    DISABLED_KEY,
    AI_KEY,
    NO_AI_KEY,
    AI_OVERRIDE_KEY,
    LAB_KEY,
    LAB_NO_AI_KEY,
    ASSISTANT_KEY,
)
PUBLIC = {"/monitor/login", "/monitor/login/mfa"}  # sign-in pages: no data, no session
DATA_MARKERS = (
    '"totals"',
    '"items"',
    '"clients"',
    '"events"',
    '"sessions"',
    '"models"',
    '"by_operation"',
    '"username"',
    '"build"',
)


def _routes():
    for path, methods in sorted(web_app.ROUTES.items()):
        for method in sorted(methods):
            yield path, method


def _credential_variants():
    for key in CUSTOMER_KEYS:
        yield {"Authorization": f"Bearer {key}"}
        yield {"X-API-Key": key}
        forged = f"{web_app.SESSION_COOKIE}={key}; {web_app.MFA_COOKIE}={key}"
        yield {"Authorization": f"Bearer {key}", "Cookie": forged}


@pytest.mark.parametrize("path, method", list(_routes()))
def test_customer_credentials_never_open_a_monitoring_route(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path, path, method
):
    with web_client(monkeypatch, tmp_path, users=[user_entry()]) as client:
        for headers in _credential_variants():
            if method == "GET":
                response = client.get(path, headers=headers, follow_redirects=False)
            else:
                response = client.post(
                    path,
                    headers={**headers, "Origin": "https://testserver"},
                    data={"csrf": "x" * 32},
                    follow_redirects=False,
                )
            body = response.text
            if path in PUBLIC:
                # the sign-in pages render (GET) or reject the form; never a session
                assert response.status_code in (200, 303, 400, 403, 429), (path, method, response.status_code)
                assert web_app.SESSION_COOKIE + "=" not in response.headers.get("set-cookie", ""), (
                    path,
                    method,
                )
                if response.status_code == 303:
                    assert response.headers["location"].startswith("/monitor/login"), (path, method)
                continue
            assert response.status_code in (303, 401, 403), (path, method, headers, response.status_code)
            if response.status_code == 303:
                assert response.headers["location"] == "/monitor/login", (path, method)
            assert not any(marker in body for marker in DATA_MARKERS), (path, method)
            assert '"ok":true' not in body.replace(" ", ""), (path, method)


def test_every_route_is_covered_by_the_matrix():
    """Guard: the matrix above must include the M4/M5 pages and data API."""
    paths = set(web_app.ROUTES)
    assert set(web_app.PAGES) <= paths and set(web_app.DATA_API) <= paths
    assert {
        "/monitor/api/v1/ollama",
        "/monitor/api/v1/stirling",
        "/monitor/ollama",
        "/monitor/stirling",
    } <= paths
    assert not any(p.startswith("/monitor/logs") or p.endswith("/events") for p in paths)  # M6 dropped

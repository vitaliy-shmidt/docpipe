"""Real-browser test of the sign-in and MFA workflow (Phase M3).

Optional: runs only where Playwright and a Chromium-based browser are
available (skipped otherwise, e.g. in CI). A real Uvicorn server on
127.0.0.1 serves the app with synthetic users, loopback allowlist and
local development mode without TLS (require_https: false is only allowed
for loopback networks). Set DOCPIPE_SCREENSHOT_DIR to keep screenshots.
"""

from __future__ import annotations

import os
import socket
import threading
import time
from pathlib import Path

import pytest

playwright_sync = pytest.importorskip("playwright.sync_api")

from system import main as main_module  # noqa: E402
from tests.monitoring_web_support import (  # noqa: E402
    PASSWORD,
    build_web_settings,
    code_for,
    use_fast_argon2,
    user_entry,
    write_users,
)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def server(fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path):
    import ipaddress

    import uvicorn

    use_fast_argon2(monkeypatch)
    entry = user_entry()
    settings = build_web_settings(
        tmp_path, require_https=False, allowed_networks=(ipaddress.ip_network("127.0.0.1/32"),)
    )
    write_users(settings.monitoring.web.users_file, [entry])
    monkeypatch.setattr(main_module, "load_settings", lambda: settings)
    port = _free_port()
    config = uvicorn.Config(
        main_module.app, host="127.0.0.1", port=port, proxy_headers=False, log_level="warning"
    )
    srv = uvicorn.Server(config)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not srv.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert srv.started
    yield f"http://127.0.0.1:{port}", entry
    srv.should_exit = True
    thread.join(10)


def _browser(p):
    for channel in ("msedge", "chrome", None):
        try:
            return (
                p.chromium.launch(channel=channel, headless=True)
                if channel
                else p.chromium.launch(headless=True)
            )
        except Exception:
            continue
    pytest.skip("no Chromium-based browser available")


def _shot(page, name: str) -> None:
    directory = os.environ.get("DOCPIPE_SCREENSHOT_DIR")
    if directory:
        Path(directory).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(directory) / f"{name}.png"), full_page=True)


def test_login_mfa_shell_and_logout_in_a_real_browser(server):
    base, entry = server
    with playwright_sync.sync_playwright() as p:
        browser = _browser(p)
        problems: list[str] = []
        for viewport, scheme, locale, suffix in (
            ({"width": 1440, "height": 900}, "light", "de-DE", "desktop-de"),
            ({"width": 390, "height": 844}, "dark", "en-US", "mobile-en"),
        ):
            context = browser.new_context(viewport=viewport, color_scheme=scheme, locale=locale)
            page = context.new_page()
            # Expected HTTP statuses (401 after a failed sign-in, 404 favicon) are logged by the
            # browser as "Failed to load resource"; everything else (CSP violations, script
            # errors) is a failure.
            page.on(
                "console",
                lambda m: (
                    problems.append(m.text)
                    if m.type in ("error", "warning") and not m.text.startswith("Failed to load resource")
                    else None
                ),
            )
            page.on("pageerror", lambda e: problems.append(str(e)))

            page.goto(base + "/monitor")
            assert page.url.endswith("/monitor/login")
            _shot(page, f"1-login-{suffix}")
            page.fill("#username", "alice")
            page.fill("#password", "wrong password value")
            page.click("button[type=submit]")
            assert page.locator(".alert-danger").count() == 1
            _shot(page, f"2-login-failed-{suffix}")

            page.fill("#username", "alice")
            page.fill("#password", PASSWORD)
            page.click("button[type=submit]")
            page.wait_for_url("**/monitor/login/mfa")
            _shot(page, f"3-mfa-{suffix}")
            page.fill(
                "#code", code_for(entry["totp_secret"], offset_steps=0 if suffix.startswith("desktop") else 1)
            )
            page.click("button[type=submit]")
            page.wait_for_url("**/monitor")
            page.wait_for_function("document.querySelector('[data-field=collector]').textContent !== '–'")
            assert page.locator("[data-field=collector]").text_content() in ("running", "stopped", "dead")
            theme = page.evaluate("document.documentElement.getAttribute('data-bs-theme')")
            assert theme == scheme
            _shot(page, f"4-shell-{suffix}")
            cookies = {c["name"]: c for c in context.cookies()}
            session = cookies["__Host-dp_mon"]
            assert session["httpOnly"] and session["secure"] and session["sameSite"] == "Strict"
            assert page.evaluate("document.cookie").find("__Host-dp_mon=") == -1  # not readable by scripts

            if suffix.startswith("mobile"):
                page.click("button[aria-controls=dpm-nav]")
                page.wait_for_selector("#dpm-nav.show")
                _shot(page, f"5-nav-{suffix}")
                page.click("#dpm-nav .btn-close")
            page.click("form[action='/monitor/logout'] button")
            page.wait_for_url("**/monitor/login")
            assert page.goto(base + "/monitor/api/v1/status").status == 401
            context.close()
        browser.close()
    # No CSP violation, script error or other browser warning on any page.
    assert problems == [], problems

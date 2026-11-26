"""Real-browser tests of the M4 dashboard (optional: Playwright + Chromium/Edge).

A real Uvicorn server on 127.0.0.1 serves the app with synthetic data
(tests/monitoring_seed.py), loopback allowlist and local development mode
without TLS. Set DOCPIPE_SCREENSHOT_DIR to keep screenshots.
"""

from __future__ import annotations

import dataclasses
import ipaddress
import os
import socket
import threading
import time
from pathlib import Path

import pytest

playwright_sync = pytest.importorskip("playwright.sync_api")

from system import main as main_module  # noqa: E402
from system.monitoring.settings import ProbeSettings  # noqa: E402
from system.monitoring.store import MonitoringStore  # noqa: E402
from system.monitoring.web.authstore import AUTH_DB_FILENAME, AuthStore  # noqa: E402
from tests.monitoring_seed import seed, seed_audit  # noqa: E402
from tests.monitoring_web_support import (  # noqa: E402
    PASSWORD,
    build_web_settings,
    code_for,
    use_fast_argon2,
    user_entry,
    write_users,
)

VIEWPORTS = {"desktop": (1440, 900), "tablet": (768, 1024), "phone": (375, 812)}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start(monkeypatch, tmp_path, *, seeded: bool, gap=None):
    import uvicorn

    use_fast_argon2(monkeypatch)
    # One synthetic operator per browser context: TOTP replay protection allows
    # each user only one login per 30-second step (counters must increase).
    entry = [user_entry(f"operator{i}") for i in range(8)]
    settings = build_web_settings(
        tmp_path,
        require_https=False,
        poll_overview_seconds=5,
        poll_activity_seconds=2,
        allowed_networks=(ipaddress.ip_network("127.0.0.1/32"),),
    )
    monitoring = dataclasses.replace(
        settings.monitoring, probes=ProbeSettings(ollama=True, stirling=True), probe_interval_seconds=3600
    )
    settings = dataclasses.replace(settings, monitoring=monitoring)
    write_users(settings.monitoring.web.users_file, entry)
    now = int(time.time() * 1000)
    if seeded:
        store = MonitoringStore(settings.monitoring)
        store.open()
        seed(
            store,
            now_ms=now,
            minutes=26 * 60,
            per_minute=5,
            gap=gap,
            dropped_minute=35,
            clients=("enabled-client", "ai-client", "assistant-client"),
        )
        store.close()
        auth = AuthStore(settings.monitoring.data_dir / AUTH_DB_FILENAME, 64 * 1024 * 1024)
        auth.open()
        seed_audit(auth, now_ms=now)
        auth.close()
    monkeypatch.setattr(main_module, "load_settings", lambda: settings)
    port = _free_port()
    srv = uvicorn.Server(
        uvicorn.Config(main_module.app, host="127.0.0.1", port=port, proxy_headers=False, log_level="warning")
    )
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not srv.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert srv.started
    return srv, thread, f"http://127.0.0.1:{port}", entry


@pytest.fixture
def seeded_server(fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path):
    srv, thread, base, entry = _start(monkeypatch, tmp_path, seeded=True, gap=(130, 115))
    yield base, entry
    srv.should_exit = True
    thread.join(10)


@pytest.fixture
def empty_server(fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path):
    srv, thread, base, entry = _start(monkeypatch, tmp_path, seeded=False)
    yield base, entry
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


def _watch(page, problems: list) -> None:
    page.on(
        "console",
        lambda m: (
            problems.append(m.text)
            if m.type in ("error", "warning") and not m.text.startswith("Failed to load resource")
            else None
        ),
    )
    page.on("pageerror", lambda e: problems.append(str(e)))


_NEXT = [0]


def _login(page, base, entries):
    user = entries[_NEXT[0] % len(entries)]
    _NEXT[0] += 1
    page.goto(base + "/monitor")
    page.fill("#username", user["username"])
    page.fill("#password", PASSWORD)
    page.click("button[type=submit]")
    page.wait_for_url("**/monitor/login/mfa")
    page.fill("#code", code_for(user["totp_secret"]))
    page.click("button[type=submit]")
    page.wait_for_url("**/monitor")


def _wait(page, expression: str, timeout: float = 15.0) -> None:
    """Poll a JS expression via page.evaluate (CDP). page.wait_for_function cannot be
    used: it evaluates strings inside the page, which our CSP (no 'unsafe-eval') blocks."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if page.evaluate(expression):
            return
        page.wait_for_timeout(100)
    raise AssertionError(f"timeout waiting for: {expression}")


def _ready(page):
    _wait(
        page,
        "!!document.getElementById('dpm-freshness') && "
        "document.getElementById('dpm-freshness').textContent.indexOf(':') > 0",
    )
    page.wait_for_timeout(1200)  # charts finish their (short) first animation


def test_all_pages_render_in_every_layout_language_and_theme(seeded_server):
    base, entry = seeded_server
    problems: list[str] = []
    with playwright_sync.sync_playwright() as p:
        browser = _browser(p)
        combos = [
            ("desktop", "dark", "de-DE"),
            ("desktop", "light", "en-US"),
            ("tablet", "light", "de-DE"),
            ("phone", "dark", "en-US"),
        ]
        for size, scheme, locale in combos:
            w, h = VIEWPORTS[size]
            context = browser.new_context(
                viewport={"width": w, "height": h}, color_scheme=scheme, locale=locale
            )
            page = context.new_page()
            _watch(page, problems)
            _login(page, base, entry)
            pages = (
                ["overview", "activity", "clients", "performance", "server", "security"]
                if (size, scheme) == ("desktop", "dark")
                else ["overview"]
            )
            for name in pages:
                page.goto(base + ("/monitor" if name == "overview" else f"/monitor/{name}"))
                _ready(page)
                assert page.evaluate("document.documentElement.getAttribute('data-bs-theme')") == scheme
                assert page.locator("canvas").count() >= (0 if name == "activity" else 1)
                # no horizontal page scroll
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"), (
                    name,
                    size,
                )
                _shot(page, f"m4-{name}-{size}-{scheme}-{locale[:2]}")
            context.close()
        browser.close()
    assert problems == [], problems


def test_filters_live_pause_hidden_tab_network_errors_and_session_end(seeded_server):
    base, entry = seeded_server
    problems: list[str] = []
    with playwright_sync.sync_playwright() as p:
        browser = _browser(p)
        context = browser.new_context(viewport={"width": 1440, "height": 900}, locale="en-US")
        page = context.new_page()
        _watch(page, problems)
        api_calls: list[str] = []
        page.on("request", lambda r: api_calls.append(r.url) if "/monitor/api/v1/" in r.url else None)
        activity_posts: list[str] = []
        page.on(
            "request",
            lambda r: activity_posts.append(r.url) if r.url.endswith("/monitor/session/activity") else None,
        )
        _login(page, base, entry)
        _ready(page)

        # range filter -> new request with that range; a user action, so one activity POST
        page.select_option("#f-range", "24h")
        _wait(page, "document.querySelector('[data-meta=range]').textContent.indexOf('minute') >= 0")
        assert any("range=24h" in u for u in api_calls)
        assert len(activity_posts) == 1

        # incomplete data (seeded gap + drops) is marked
        assert page.locator("#ov-quality .badge", has_text="Incomplete").count() == 1

        # pause: no further polling
        page.click("#dpm-live-toggle")
        count = len(api_calls)
        page.wait_for_timeout(6500)
        assert len(api_calls) == count
        page.click("#dpm-live-toggle")
        page.wait_for_timeout(500)
        assert len(api_calls) > count

        # hidden tab: polling stops
        page.evaluate(
            "Object.defineProperty(document, 'hidden', {configurable: true, get: () => true});"
            "document.dispatchEvent(new Event('visibilitychange'))"
        )
        count = len(api_calls)
        page.wait_for_timeout(6500)
        assert len(api_calls) == count
        page.evaluate(
            "Object.defineProperty(document, 'hidden', {configurable: true, get: () => false});"
            "document.dispatchEvent(new Event('visibilitychange'))"
        )
        page.wait_for_timeout(500)
        assert len(api_calls) > count
        assert len(activity_posts) == 1  # polling never posts activity

        # network errors: data stays visible, marked stale, notice shown
        before = page.locator("#ov-kpis .dpm-kpi-value").first.text_content()
        page.route("**/monitor/api/v1/**", lambda route: route.abort())
        _wait(page, "!document.getElementById('dpm-notice').classList.contains('d-none')")
        assert page.locator("#ov-kpis .dpm-kpi-value").first.text_content() == before
        assert page.evaluate("document.getElementById('dpm-app').classList.contains('dpm-is-stale')")
        _shot(page, "m4-overview-connection-lost")
        page.unroute("**/monitor/api/v1/**")

        # session ended server-side: next poll gets 401 -> polling stops, back to sign-in
        page.request.post(
            base + "/monitor/logout",
            form={"csrf": page.get_attribute("#dpm-app", "data-csrf")},
            headers={"Origin": base},
        )
        page.wait_for_url("**/monitor/login", timeout=20000)

        # activity page: client filter
        _login(page, base, entry)
        page.goto(base + "/monitor/activity")
        _ready(page)
        page.select_option("#f-client", "ai-client")
        _wait(page, "document.querySelectorAll('#act-finished tr').length > 0")
        clients = page.eval_on_selector_all(
            "#act-finished tr td:nth-child(2)", "els => els.map(e => e.textContent)"
        )
        assert clients and set(clients) == {"ai-client"}
        context.close()
        browser.close()
    assert problems == [], problems


def test_empty_installation_shows_empty_states(empty_server):
    base, entry = empty_server
    problems: list[str] = []
    with playwright_sync.sync_playwright() as p:
        browser = _browser(p)
        context = browser.new_context(viewport={"width": 1440, "height": 900}, locale="de-DE")
        page = context.new_page()
        _watch(page, problems)
        _login(page, base, entry)
        _ready(page)
        assert page.locator("#ov-empty").is_visible()
        gpu = page.locator("#ov-system .dpm-tile", has_text="GPU").first.text_content()
        # no sample yet on a fresh installation: "not measured yet", never 0
        assert "—" in gpu and "Noch kein Messwert" in gpu
        _shot(page, "m4-overview-empty-desktop-light-de")
        context.close()
        browser.close()
    assert problems == [], problems

"""Real-browser tests of the Ollama and Stirling PDF pages (Phase M5).

Same harness as the M4 browser tests: a real Uvicorn server on 127.0.0.1,
synthetic data only, loopback allowlist, development mode without TLS.
DOCPIPE_SCREENSHOT_DIR keeps screenshots.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from tests.test_monitor_dashboard_browser import (
    VIEWPORTS,
    _browser,
    _login,
    _ready,
    _shot,
    _start,
    _wait,
    _watch,
    playwright_sync,
)


@pytest.fixture
def start_server(fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path):
    """Starts one server at a time: all of them serve the same global app object
    (lifespan state), so a new start stops the previous server first."""
    running = []

    def stop():
        while running:
            srv, thread = running.pop()
            srv.should_exit = True
            thread.join(10)

    def start(name="default", seeded=True, **seed_kwargs):
        stop()
        directory = tmp_path / name
        directory.mkdir(parents=True, exist_ok=True)
        srv, thread, base, entry = _start(monkeypatch, directory, seeded=seeded, **seed_kwargs)
        running.append((srv, thread))
        return base, entry

    yield start
    stop()


def _shot_el(page, selector: str, name: str) -> None:
    directory = os.environ.get("DOCPIPE_SCREENSHOT_DIR")
    if directory:
        Path(directory).mkdir(parents=True, exist_ok=True)
        page.locator(selector).first.screenshot(path=str(Path(directory) / f"{name}.png"))


def _no_hscroll(page) -> bool:
    return page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")


def test_pages_render_in_every_layout_language_and_theme(start_server):
    base, entry = start_server()
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
            for name in ("ollama", "stirling"):
                page.goto(f"{base}/monitor/{name}")
                _ready(page)
                assert page.evaluate("document.documentElement.getAttribute('data-bs-theme')") == scheme
                assert page.locator("canvas").count() >= 8
                assert _no_hscroll(page), (name, size)
                # the navigation entry is enabled and marked as the current page
                assert page.locator(f"a[href='/monitor/{name}'][aria-current=page]").count() >= 1
                _shot(page, f"m5-{name}-{size}-{scheme}-{locale[:2]}")
            context.close()
        browser.close()
    assert problems == [], problems


def test_ollama_models_keep_alive_and_ai_performance(start_server):
    base, entry = start_server()
    problems: list[str] = []
    with playwright_sync.sync_playwright() as p:
        browser = _browser(p)
        context = browser.new_context(viewport={"width": 1440, "height": 900}, locale="en-US")
        page = context.new_page()
        _watch(page, problems)
        _login(page, base, entry)
        page.goto(base + "/monitor/ollama")
        _ready(page)
        # installed vs loaded: three installed models, one loaded, nothing running
        assert page.locator("#ol-installed tr").count() == 3
        assert page.locator("#ol-loaded tr").count() == 1
        assert page.locator("#ol-active tr").first.text_content() == "No AI call running."
        legacy = page.locator("#ol-installed tr", has_text="legacy-model:latest").text_content()
        assert "—" in legacy  # missing fields are "not available", never 0
        # keep-alive countdown runs client-side from the measured expiry time
        first = page.locator("#ol-loaded .dpm-keepalive").text_content()
        assert re.fullmatch(r"\d+:\d\d min", first), first
        page.wait_for_timeout(2200)
        assert page.locator("#ol-loaded .dpm-keepalive").text_content() != first
        # VRAM: Ollama-reported allocation and the DocPipe host GPU are separate tiles
        gpu = page.locator("#ol-gpu").text_content()
        assert "VRAM (reported by Ollama)" in gpu and "GPU of the DocPipe host" in gpu
        assert page.locator("#ol-gpu-remote").is_visible()  # test config: Ollama is on another host
        # measured vs reported values are labelled; throughput is not a raw mean
        kpis = page.locator("#ol-kpis").text_content()
        assert "measured by DocPipe" in kpis and "reported by Ollama" in kpis and "tok/s" in kpis
        assert page.locator("#ol-warmups .dpm-kpi-value").first.text_content() not in ("0", "—")
        assert page.locator("#ol-model-table tr").count() == 2
        _shot_el(
            page, "#ol-loaded-title >> xpath=ancestor::div[contains(@class,'row')][1]", "m5-ollama-models"
        )
        _shot_el(
            page, "#ol-kpis >> xpath=following-sibling::div[contains(@class,'row')][1]", "m5-ollama-history"
        )
        context.close()
        browser.close()
    assert problems == [], problems


def test_filters_and_live_pause(start_server):
    base, entry = start_server()
    problems: list[str] = []
    with playwright_sync.sync_playwright() as p:
        browser = _browser(p)
        context = browser.new_context(viewport={"width": 1440, "height": 900}, locale="en-US")
        page = context.new_page()
        _watch(page, problems)
        calls: list[str] = []
        page.on("request", lambda r: calls.append(r.url) if "/monitor/api/v1/" in r.url else None)
        _login(page, base, entry)
        page.goto(base + "/monitor/ollama")
        _ready(page)
        page.select_option("#f-model", "light-test-model")
        _wait(page, "document.querySelectorAll('#ol-model-table tr').length === 1")
        assert any("model=light-test-model" in u for u in calls)
        page.select_option("#f-operation", "warmup")
        _wait(page, "document.querySelector('#ol-kpis .dpm-kpi-value').textContent === '0'")
        page.select_option("#f-operation", "")
        page.select_option("#f-status", "failed")
        _wait(page, "document.querySelectorAll('#ol-kpis .dpm-kpi-value')[1].textContent === '0'")
        page.select_option("#f-status", "")
        page.select_option("#f-client", "ai-client")
        _wait(page, "(window.__c = Array.from(document.querySelectorAll('#ol-model-table tr'))).length > 0")
        assert any("client=ai-client" in u for u in calls)
        page.select_option("#f-range", "7d")
        _wait(
            page,
            "document.querySelector('[data-meta=range]').textContent.indexOf('hour') >= 0 || "
            "document.querySelector('[data-meta=range]').textContent.indexOf('raw') >= 0",
        )
        # Live / Pause on the upstream pages reuses the M4 polling
        page.click("#dpm-live-toggle")
        count = len(calls)
        page.wait_for_timeout(6500)
        assert len(calls) == count
        page.click("#dpm-live-toggle")
        page.wait_for_timeout(600)
        assert len(calls) > count
        # Stirling: operation filter
        page.goto(base + "/monitor/stirling")
        _ready(page)
        page.select_option("#f-operation", "ocr")
        _wait(
            page,
            "document.querySelectorAll('#st-ops tr').length === 1 && "
            "document.querySelector('#st-ops tr td').textContent === 'OCR'",
        )
        page.select_option("#f-status", "failed")
        _wait(page, "document.querySelectorAll('#st-kpis .dpm-kpi-value')[1].textContent === '0'")
        context.close()
        browser.close()
    assert problems == [], problems


def test_offline_stale_and_missing_data(start_server):
    problems: list[str] = []
    with playwright_sync.sync_playwright() as p:
        browser = _browser(p)
        for scenario, kwargs, locale, scheme in (
            ("offline", {"ollama_down": (30, 0), "stirling_down": (25, 0)}, "de-DE", "light"),
            ("stale", {"probes_until_ago": 240}, "en-US", "dark"),
            ("empty", {"seeded": False}, "en-US", "light"),
        ):
            base, entry = start_server(scenario, **kwargs)
            context = browser.new_context(
                viewport={"width": 1440, "height": 900}, locale=locale, color_scheme=scheme
            )
            page = context.new_page()
            _watch(page, problems)
            _login(page, base, entry)
            page.goto(base + "/monitor/ollama")
            _ready(page)
            if scenario == "offline":
                assert page.locator("#ol-state .badge", has_text="Offline").count() == 1
                assert "Verbindungsfehler" in page.locator("#ol-probe-failures").text_content()
                assert page.locator("#ol-installed tr").count() == 3  # last known snapshot stays visible
                assert int(page.locator("#ol-kpis .dpm-kpi-value").first.text_content().replace(".", "")) > 0
                _shot(page, "m5-ollama-offline-desktop-light-de")
                page.goto(base + "/monitor/stirling")
                _ready(page)
                assert page.locator("#st-probe-failures .text-bg-danger").count() >= 1
                assert page.locator("#st-ops .text-bg-danger").count() >= 1  # failed Stirling operations
                _shot(page, "m5-stirling-errors-desktop-light-de")
            elif scenario == "stale":
                assert page.locator("#ol-models-meta .badge", has_text="stale").count() == 1
                assert page.locator("#ol-loaded .dpm-keepalive").text_content() == "Model snapshot is stale"
                # network failure: data stays visible, marked stale
                page.route("**/monitor/api/v1/**", lambda route: route.abort())
                _wait(page, "document.getElementById('dpm-app').classList.contains('dpm-is-stale')", 30)
                assert page.locator("#ol-installed tr").count() == 3
                _shot(page, "m5-ollama-stale-offline-desktop-dark-en")
                page.unroute("**/monitor/api/v1/**")
            else:
                assert page.locator("#ol-empty").is_visible()
                assert (
                    page.locator("#ol-installed tr").first.text_content() == "No model snapshot recorded yet."
                )
                assert page.locator("#ol-kpis .dpm-kpi-value").nth(3).text_content() == "—"
                page.goto(base + "/monitor/stirling")
                _ready(page)
                assert page.locator("#st-empty").is_visible()
                assert page.locator("#st-kpis .dpm-kpi-value").nth(3).text_content() == "—"
                _shot(page, "m5-stirling-empty-desktop-light-en")
            context.close()
        browser.close()
    assert problems == [], problems


def test_phone_layouts(start_server):
    base, entry = start_server()
    problems: list[str] = []
    with playwright_sync.sync_playwright() as p:
        browser = _browser(p)
        w, h = VIEWPORTS["phone"]
        for scheme in ("light", "dark"):
            context = browser.new_context(
                viewport={"width": w, "height": h}, color_scheme=scheme, locale="de-DE"
            )
            page = context.new_page()
            _watch(page, problems)
            _login(page, base, entry)
            for name in ("ollama", "stirling"):
                page.goto(f"{base}/monitor/{name}")
                _ready(page)
                assert _no_hscroll(page), name
                _shot(page, f"m5-{name}-phone-{scheme}-de")
            context.close()
        browser.close()
    assert problems == [], problems

# ruff: noqa: E501 - the embedded browser checks are JavaScript
"""Visual and accessibility acceptance of all dashboard pages (Phase M7).

Every page at 320, 375, 768 and 1440 px, German and English, light and dark:
no horizontal overflow, no clipped text, no JS errors or CSP violations.
Plus keyboard operation with visible focus, WCAG text contrast and
prefers-reduced-motion. Synthetic data only; DOCPIPE_SCREENSHOT_DIR keeps
the full screenshot set (the CI job uploads it as an artifact).
"""

from __future__ import annotations

from tests import test_monitor_upstreams_browser as upstream_browser
from tests.test_monitor_dashboard_browser import _browser, _login, _ready, _shot, _watch, playwright_sync

_no_hscroll = upstream_browser._no_hscroll
_overflowing = upstream_browser._overflowing
start_server = upstream_browser.start_server  # the fixture, shared with the M5 browser tests

PAGES = ("overview", "activity", "clients", "performance", "server", "security", "ollama", "stirling")
WIDTHS = {320: 720, 375: 812, 768: 1024, 1440: 900}

# Text whose box is narrower than its content while overflow is hidden. Deliberate truncation
# (ellipsis with the full text available as title) is excluded.
CLIPPED = """
Array.from(document.querySelectorAll('main *, header *, nav *')).filter(e => {
  if (!e.offsetParent || e.closest('.table-responsive') || e.closest('.text-truncate')) return false;
  if (e.id === 'dpm-freshness' || e.classList.contains('dpm-id') || e.tagName === 'CANVAS') return false;
  const s = getComputedStyle(e);
  if (s.overflowX !== 'hidden' && s.overflow !== 'hidden') return false;
  return e.scrollWidth > e.clientWidth + 1 && (e.textContent || '').trim().length > 0;
}).slice(0, 8).map(e => e.tagName + '.' + String(e.className).slice(0, 40) + ' [' + e.textContent.trim().slice(0, 30) + ']')
"""

# WCAG 2.1 contrast of visible text against its effective background (AA: 4.5, large text 3).
CONTRAST = """
(() => {
  const parse = c => { const m = c.match(/[\\d.]+/g); return m ? m.map(Number) : [0, 0, 0, 0]; };
  const lum = ([r, g, b]) => { const f = v => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b); };
  const bg = el => { for (let e = el; e; e = e.parentElement) { const c = parse(getComputedStyle(e).backgroundColor);
    if (c.length < 4 || c[3] > 0.95) return c; } return [255, 255, 255]; };
  const out = [];
  const seen = new Set();
  for (const e of document.querySelectorAll('main *, header *, nav *')) {
    if (!e.offsetParent || e.closest('[aria-hidden=true]') || e.closest('.disabled') || e.tagName === 'CANVAS') continue;
    const own = Array.from(e.childNodes).some(n => n.nodeType === 3 && n.textContent.trim());
    if (!own) continue;
    const s = getComputedStyle(e);
    if (parseFloat(s.opacity) < 1) continue;
    const fg = parse(s.color);
    const ratio = (() => { const a = lum(fg), b = lum(bg(e)); return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05); })();
    const size = parseFloat(s.fontSize), bold = parseInt(s.fontWeight, 10) >= 700;
    const need = size >= 24 || (bold && size >= 18.66) ? 3 : 4.5;
    const key = e.className + '|' + s.color;
    if (ratio + 0.01 < need && !seen.has(key)) {
      seen.add(key);
      out.push(e.tagName + '.' + String(e.className).slice(0, 40) + ' [' + e.textContent.trim().slice(0, 20) + '] ' + ratio.toFixed(2));
    }
  }
  return out.slice(0, 12);
})()
"""


def test_all_pages_all_widths_languages_and_themes(start_server):
    base, entry = start_server()
    problems: list[str] = []
    findings: dict = {}
    with playwright_sync.sync_playwright() as p:
        browser = _browser(p)
        for locale in ("de-DE", "en-US"):
            for scheme in ("light", "dark"):
                context = browser.new_context(
                    viewport={"width": 1440, "height": 900}, color_scheme=scheme, locale=locale
                )
                page = context.new_page()
                _watch(page, problems)
                _login(page, base, entry)
                for width, height in WIDTHS.items():
                    page.set_viewport_size({"width": width, "height": height})
                    for name in PAGES:
                        page.goto(base + ("/monitor" if name == "overview" else f"/monitor/{name}"))
                        _ready(page)
                        tag = f"{name}-{width}-{scheme}-{locale[:2]}"
                        _shot(page, f"m7-{tag}")
                        if not _no_hscroll(page):
                            findings[f"overflow {tag}"] = _overflowing(page)
                        clipped = page.evaluate(CLIPPED)
                        if clipped:
                            findings[f"clipped {tag}"] = clipped
                        if width == 1440:
                            low = page.evaluate(CONTRAST)
                            if low:
                                findings[f"contrast {tag}"] = low
                context.close()
        browser.close()
    assert problems == [], problems  # no JS errors, no CSP violations
    assert findings == {}, findings


def test_keyboard_operation_and_visible_focus(start_server):
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
        page.evaluate("document.activeElement && document.activeElement.blur()")
        reached, unfocused = [], []
        for _ in range(60):
            page.keyboard.press("Tab")
            info = page.evaluate(
                "(() => { const e = document.activeElement; if (!e || e === document.body) return null;"
                " const s = getComputedStyle(e);"
                " const visible = (s.outlineStyle !== 'none' && parseFloat(s.outlineWidth) > 0) || s.boxShadow !== 'none';"
                " return {id: e.id, href: e.getAttribute('href'), tag: e.tagName, visible}; })()"
            )
            if info:
                reached.append(info)
                if not info["visible"]:
                    unfocused.append(info)
        hrefs = {i["href"] for i in reached}
        ids = {i["id"] for i in reached}
        assert {"/monitor", "/monitor/ollama", "/monitor/stirling", "/monitor/security"} <= hrefs
        assert {"f-range", "f-model", "f-client", "f-operation", "f-status", "dpm-live-toggle"} <= ids
        assert unfocused == [], unfocused  # every focusable element shows a focus indicator
        # filters and Live/Pause work from the keyboard
        page.focus("#f-range")
        page.keyboard.press("ArrowDown")
        page.focus("#dpm-live-toggle")
        page.keyboard.press("Enter")
        assert page.get_attribute("#dpm-live-toggle", "aria-pressed") == "false"
        page.keyboard.press("Space")
        assert page.get_attribute("#dpm-live-toggle", "aria-pressed") == "true"
        # phone: the navigation opens from the keyboard
        page.set_viewport_size({"width": 375, "height": 812})
        page.focus("button[aria-controls=dpm-nav]")
        page.keyboard.press("Enter")
        page.wait_for_timeout(600)
        assert page.locator("#dpm-nav a[href='/monitor/stirling']").is_visible()
        context.close()
        browser.close()
    assert problems == [], problems


def test_reduced_motion_disables_animations(start_server):
    base, entry = start_server()
    problems: list[str] = []
    with playwright_sync.sync_playwright() as p:
        browser = _browser(p)
        context = browser.new_context(viewport={"width": 1440, "height": 900}, reduced_motion="reduce")
        page = context.new_page()
        _watch(page, problems)
        _login(page, base, entry)
        for name in ("overview", "ollama"):
            page.goto(base + ("/monitor" if name == "overview" else f"/monitor/{name}"))
            _ready(page)
            assert page.evaluate("Chart.defaults.animation === false")
            moving = page.evaluate(
                "Array.from(document.querySelectorAll('*')).filter(e => { const s = getComputedStyle(e);"
                " return (parseFloat(s.animationDuration) > 0.011 && s.animationName !== 'none')"
                " || parseFloat(s.transitionDuration) > 0.011; }).slice(0, 5).map(e => e.tagName + '.' + e.className)"
            )
            assert moving == [], moving
        context.close()
        browser.close()
    assert problems == [], problems

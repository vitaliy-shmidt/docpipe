"""Dashboard data API (Phase M4): access, validation, correctness, limits.

All data is synthetic (tests/monitoring_seed.py); upstreams are the conftest fakes.
"""

from __future__ import annotations

import random
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest

from system.monitoring.recorder import Batch, QualityCounters
from system.monitoring.stats import exact_percentile
from system.monitoring.web import app as web_app
from system.monitoring.web import dashboard as dashboard_module
from system.monitoring.web.app import DATA_API, PAGES, SESSION_COOKIE
from tests.conftest import AI_KEY, ASSISTANT_KEY, LAB_KEY, VALID_KEY, auth_headers
from tests.monitoring_seed import seed, seed_audit, synthetic_request
from tests.monitoring_web_support import csrf_of, full_login, post_form, user_entry, web_client

MINUTE = 60_000
HOUR = 3_600_000
ENDPOINTS = sorted(DATA_API)
CLIENTS = ("enabled-client", "ai-client", "assistant-client")


@contextmanager
def dashboard(
    fake_stirling_holder,
    fake_ollama_holder,
    monkeypatch,
    tmp_path,
    *,
    seeded=True,
    minutes=150,
    gap=None,
    dropped_minute=None,
    login=True,
    **web_overrides,
):
    entry = user_entry()
    with web_client(monkeypatch, tmp_path, users=[entry], **web_overrides) as client:
        monitor = client.app.state.monitoring
        now = monitor._clock()
        info = {}
        if seeded:
            with monitor._store_lock:
                info = seed(
                    monitor.store,
                    now_ms=now,
                    minutes=minutes,
                    gap=gap,
                    dropped_minute=dropped_minute,
                    clients=CLIENTS,
                )
            seed_audit(client.web.store, now_ms=now)
        if login:
            assert full_login(client, entry["totp_secret"]).status_code == 303
        client.entry, client.seeded, client.now = entry, info.get("requests", []), now
        yield client


def get(test_client, name, **params):
    return test_client.get(f"/monitor/api/v1/{name}", params=params)


# --- access ------------------------------------------------------------------------------------


def test_every_dashboard_endpoint_and_page_requires_a_session(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path
):
    with dashboard(fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path, login=False) as client:
        for path in ENDPOINTS:
            for headers in (
                {},
                auth_headers(VALID_KEY),
                auth_headers(AI_KEY),
                auth_headers(LAB_KEY),
                auth_headers(ASSISTANT_KEY),
                {"Cookie": f"{SESSION_COOKIE}={VALID_KEY}"},
            ):
                response = client.get(path, headers=headers)
                assert response.status_code == 401, (path, headers)
                assert response.json() == {"ok": False, "code": "unauthorized"}
                assert response.headers["cache-control"] == "no-store"
        for path in PAGES:
            assert client.get(path, follow_redirects=False).status_code == 303
        assert client.post("/monitor/api/v1/overview").status_code == 405


def test_dashboard_is_denied_outside_the_network_allowlist(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path
):
    entry = user_entry()
    with web_client(monkeypatch, tmp_path, users=[entry], client_addr=("198.51.100.9", 1)) as client:
        for path in ENDPOINTS + sorted(PAGES):
            assert client.get(path).status_code == 403


def test_expired_session_is_rejected_by_the_data_api(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path
):
    with dashboard(fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path) as client:
        assert get(client, "overview").status_code == 200
        base = client.web._clock()
        client.web._clock = lambda: base + 16 * MINUTE  # idle timeout; polling never extended it
        for path in ENDPOINTS:
            assert client.get(path).status_code == 401


def test_data_api_calls_never_extend_the_idle_time(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path
):
    with dashboard(fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path) as client:
        before = client.web.store._db().execute("SELECT last_activity_ms FROM sessions").fetchone()[0]
        base = client.web._clock()
        client.web._clock = lambda: base + 5 * MINUTE
        for path in ENDPOINTS:
            assert client.get(path).status_code == 200, path
        after = client.web.store._db().execute("SELECT last_activity_ms FROM sessions").fetchone()[0]
        assert after == before


def test_activity_route_is_csrf_protected_and_bounded(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path
):
    with dashboard(fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path) as client:
        csrf = csrf_of(client.get("/monitor"))
        db = client.web.store._db()
        last, absolute = db.execute("SELECT last_activity_ms, absolute_expiry_ms FROM sessions").fetchone()
        base = client.web._clock()
        client.web._clock = lambda: base + 10_000
        assert post_form(client, "/monitor/session/activity", {"csrf": csrf}).status_code == 204
        assert db.execute("SELECT last_activity_ms FROM sessions").fetchone()[0] == last  # < 30 s: no effect
        client.web._clock = lambda: base + 5 * MINUTE
        assert post_form(client, "/monitor/session/activity", {"csrf": "forged"}).status_code == 403
        assert (
            post_form(
                client, "/monitor/session/activity", {"csrf": csrf}, origin="https://evil.example"
            ).status_code
            == 403
        )
        assert post_form(client, "/monitor/session/activity", {"csrf": csrf}).status_code == 204
        last2, absolute2 = db.execute("SELECT last_activity_ms, absolute_expiry_ms FROM sessions").fetchone()
        assert last2 == base + 5 * MINUTE and absolute2 == absolute  # never beyond the absolute timeout
        assert client.get("/monitor/session/activity").status_code == 405


def test_per_session_rate_limit(fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path):
    with dashboard(
        fake_stirling_holder,
        fake_ollama_holder,
        monkeypatch,
        tmp_path,
        seeded=False,
        api_requests_per_minute=30,
    ) as client:
        statuses = [get(client, "meta").status_code for _ in range(35)]
        assert statuses[:30] == [200] * 30 and statuses[30:] == [429] * 5
        assert client.get("/api/v1/health").status_code == 200


# --- validation --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "params, code",
    [
        ({"range": "2y"}, "invalid_range"),
        ({"range": "custom"}, "invalid_from"),
        ({"range": "custom", "from": "1", "to": "2"}, "invalid_range"),
        ({"range": "custom", "from": "abc", "to": "2"}, "invalid_from"),
        ({"client": "nobody"}, "unknown_parameter"),
        ({"bogus": "1"}, "unknown_parameter"),
        ({"orderby": "1; DROP TABLE request_events"}, "unknown_parameter"),
    ],
)
def test_overview_rejects_invalid_parameters(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path, params, code
):
    with dashboard(fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path, seeded=False) as client:
        response = get(client, "overview", **params)
        assert response.status_code == 400 and response.json()["code"] == code


@pytest.mark.parametrize(
    "params, code",
    [
        ({"client": "x' OR '1'='1"}, "invalid_client"),
        ({"client": "enabled-client; DROP TABLE request_events--"}, "invalid_client"),
        ({"service": "documents' --"}, "invalid_service"),
        ({"status": "200 OR 1=1"}, "invalid_status"),
        ({"route": "/api/v1/x' OR 1=1"}, "invalid_route"),
        ({"cursor": "1.2.3"}, "invalid_cursor"),
        ({"cursor": "a.b"}, "invalid_cursor"),
        ({"limit": "x"}, "invalid_limit"),
    ],
)
def test_activity_filters_are_validated_and_injection_is_inert(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path, params, code
):
    with dashboard(fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path) as client:
        response = get(client, "activity", **params)
        assert response.status_code == 400 and response.json()["code"] == code
        assert get(client, "activity").status_code == 200  # tables intact


def test_query_string_shape_is_bounded(fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path):
    with dashboard(fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path, seeded=False) as client:
        assert client.get("/monitor/api/v1/overview?range=1h&range=24h").status_code == 400
        assert client.get("/monitor/api/v1/overview?" + "x" * 2000).status_code == 400
        assert client.get("/monitor/api/v1/overview?range=%ff%fe").status_code == 400
        assert client.get("/monitor?range=1h").status_code == 404  # pages take no query


def test_limits_are_enforced(fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path):
    with dashboard(fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path) as client:
        items = get(client, "activity", range="1h", limit="100000").json()["items"]
        assert len(items) == dashboard_module.MAX_PAGE
        for name in ("overview", "clients", "performance", "server"):
            for rng in ("live", "1h", "24h", "7d", "30d"):
                data = get(client, name, range=rng).json()
                assert data["ok"], (name, rng)
                assert (data["range"]["end_ms"] - data["range"]["effective_start_ms"]) / data["range"][
                    "bucket_ms"
                ] <= dashboard_module.MAX_POINTS + 1


# --- correctness --------------------------------------------------------------------------------


def _expected(client, start, end, client_id=None):
    return [
        r for r in client.seeded if start <= r.ts_ms < end and (client_id is None or r.client_id == client_id)
    ]


def test_empty_database_reports_no_values_instead_of_zero_rates(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path
):
    with dashboard(fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path, seeded=False) as client:
        data = get(client, "overview", range="1h").json()
        totals = data["totals"]
        assert totals["count"] == 0
        assert totals["success_rate"] is None and totals["failure_rate"] is None and totals["avg_ms"] is None
        assert data["percentiles"]["p95"] is None
        assert data["completeness"]["complete"] is False and data["completeness"]["range_before_first_data"]
        system = data["system"]
        assert system["gpu.utilization_percent"]["value"] is None
        assert system["gpu.utilization_percent"]["status"] in ("unavailable", "stale")
        for name in ("clients", "performance", "server", "security", "activity", "meta"):
            assert get(client, name).status_code == 200


def test_raw_counts_and_exact_percentiles_match_the_seeded_data(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path
):
    with dashboard(fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path) as client:
        data = get(client, "overview", range="1h").json()
        rng = data["range"]
        expected = _expected(client, rng["start_ms"], rng["end_ms"])
        assert rng["source"] == "raw" and rng["approximate"] is False
        assert data["totals"]["count"] == len(expected)
        assert data["totals"]["success"] == sum(1 for r in expected if 100 <= r.status_code < 400)
        durations = sorted(r.duration_ms for r in expected)
        assert data["percentiles"]["method"] == "exact"
        assert data["percentiles"]["p95"] == exact_percentile(durations, 0.95)
        series_total = sum(p["count"] or 0 for p in data["series"])
        assert series_total == len(expected)  # series and totals from one source


def test_rollups_are_never_added_to_raw_rows(fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path):
    with dashboard(fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path) as client:
        data = get(client, "overview", range="24h").json()
        rng = data["range"]
        assert rng["source"] == "minute" and rng["approximate"] is True
        assert data["percentiles"]["method"] == "histogram"
        # the minute source covers whole minutes from the bucket-aligned start
        expected = _expected(client, rng["effective_start_ms"], rng["end_ms"])
        assert data["totals"]["count"] == len(expected)
        assert sum(p["count"] or 0 for p in data["series"]) == len(expected)
        week = get(client, "overview", range="7d").json()
        assert week["range"]["source"] == "hour"
        assert week["totals"]["count"] == len(
            _expected(client, week["range"]["effective_start_ms"], week["range"]["end_ms"])
        )


def test_custom_ranges_are_utc_epoch_bounds(fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path):
    with dashboard(fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path) as client:
        start, end = client.now - 50 * MINUTE + 1234, client.now - 20 * MINUTE + 777
        data = get(client, "overview", range="custom", **{"from": str(start), "to": str(end)}).json()
        assert data["range"]["source"] == "raw"
        assert data["totals"]["count"] == len(_expected(client, start, end))
        rows = get(
            client, "activity", range="custom", limit="100", **{"from": str(start), "to": str(end)}
        ).json()
        assert all(start <= item["ts_ms"] < end for item in rows["items"])


def test_gaps_are_null_and_covered_empty_buckets_are_zero(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path
):
    with dashboard(
        fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path, gap=(40, 31), dropped_minute=10
    ) as client:
        data = get(client, "overview", range="1h").json()
        series = data["series"]
        gap_points = [p for p in series if client.now - 39 * MINUTE <= p["t"] < client.now - 32 * MINUTE]
        assert gap_points and all(p["count"] is None and p["coverage"] == 0 for p in gap_points)
        covered_empty = [p for p in series if p["coverage"] == 1 and p["count"] == 0]
        assert all(p["count"] == 0 for p in covered_empty)
        c = data["completeness"]
        assert c["complete"] is False and c["requests_dropped"] == 3 and c["collector_gap_minutes"] >= 8


def test_clients_aggregate_per_authenticated_client_and_separate_unauthenticated(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path
):
    with dashboard(fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path) as client:
        data = get(client, "clients", range="1h").json()
        rng = data["range"]
        rows = {c["client_id"]: c for c in data["clients"]}
        for cid in CLIENTS:
            expected = _expected(client, rng["start_ms"], rng["end_ms"], cid)
            assert rows[cid]["count"] == len(expected) and rows[cid]["authenticated"] is True
            assert rows[cid]["documents"] == sum(r.documents for r in expected)
            assert rows[cid]["tokens_out"] == sum(r.tokens_out or 0 for r in expected)
        anon = rows[None]
        assert anon["authenticated"] is False
        assert anon["count"] == len(
            [r for r in _expected(client, rng["start_ms"], rng["end_ms"]) if r.client_id is None]
        )
        assert abs(sum(c["share"] for c in data["clients"]) - 1) < 1e-9
        filtered = get(client, "clients", range="1h", service="ai").json()
        assert all(set(c["services"]) <= {"ai"} for c in filtered["clients"])


def test_performance_steps_inclusive_and_exclusive(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path
):
    with dashboard(fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path) as client:
        data = get(client, "performance", range="1h").json()
        rng = data["range"]
        steps = {s["step"]: s for s in data["steps"]}
        expected = _expected(client, rng["start_ms"], rng["end_ms"])
        ocr_count = sum(1 for r in expected for s in r.steps if s.step == "stirling.ocr")
        assert steps["stirling.ocr"]["count"] == ocr_count
        inspect = [r for r in expected if any(s.step == "forms.inspect" for s in r.steps)]
        exclusive = [
            next(s.duration_ms for s in r.steps if s.step == "forms.inspect")
            - sum(s.duration_ms for s in r.steps if s.step == "stirling.ocr")
            for r in inspect
        ]
        assert steps["forms.inspect"]["duration_kind"] == "inclusive"
        assert steps["forms.inspect"]["exclusive_avg_ms"] == pytest.approx(
            sum(max(0, e) for e in exclusive) / len(exclusive)
        )
        assert steps["stirling.ocr"]["parent"] == "forms.inspect"
        assert steps["ai.generate"]["ollama"]["reported_count"] > 0
        by_client = get(client, "performance", range="1h", client="ai-client").json()
        assert by_client["requests"]["count"] == len(
            _expected(client, rng["start_ms"], rng["end_ms"], "ai-client")
        )
        too_old = get(client, "performance", range="30d", client="ai-client")
        assert too_old.status_code == 400 and too_old.json()["code"] == "client_filter_requires_raw_retention"


def test_server_metrics_carry_source_status_and_never_invent_gpu(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path
):
    with dashboard(fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path) as client:
        data = get(client, "server", range="1h").json()
        latest = data["latest"]
        assert latest["host.cpu.percent"]["source"] == "host"
        assert latest["container.memory.current_bytes"]["source"] == "container"
        assert latest["process.rss_bytes"]["source"] == "process"
        assert latest["disk.data.free_bytes"]["source"] == "filesystem"
        assert latest["gpu.utilization_percent"]["value"] is None
        assert latest["gpu.utilization_percent"]["reason"] == "not_configured"
        assert data["series"]["bucket_ms"] >= MINUTE
        states = {s["service"]: s for s in data["services"]}
        assert states["docpipe"]["state"] == "online" and states["collector"]["source"] == "docpipe"
        # probes are disabled in this test configuration: shown as not configured, not as online
        assert states["ollama"]["state"] == "not_configured"


def test_security_view_contains_no_secrets_and_is_sorted(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path
):
    with dashboard(fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path) as client:
        response = get(client, "security", range="24h")
        data = response.json()
        token = client.cookies.get(SESSION_COOKIE)
        full_hash = client.web.store._db().execute("SELECT id_hash FROM sessions").fetchone()[0]
        for secret in (
            token,
            full_hash,
            client.entry["totp_secret"],
            client.entry["password_hash"],
            VALID_KEY,
        ):
            assert secret not in response.text
        assert [s["current"] for s in data["sessions"]] == [True]
        assert len(data["sessions"][0]["session_ref"]) == 12
        times = [e["ts_ms"] for e in data["events"]]
        assert times == sorted(times, reverse=True)
        assert data["counts_by_category"]["failure"] >= 8 and data["range"]["source"] == "audit"
        page2 = get(client, "security", range="24h", limit="5").json()
        nxt = get(client, "security", range="24h", limit="5", cursor=page2["next_cursor"]).json()
        assert {e["id"] for e in page2["events"]}.isdisjoint({e["id"] for e in nxt["events"]})
        assert get(client, "security", category="bogus").status_code == 400


def test_activity_pagination_and_active_requests(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path
):
    with dashboard(fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path) as client:
        first = get(client, "activity", range="1h", limit="20").json()
        second = get(client, "activity", range="1h", limit="20", cursor=first["next_cursor"]).json()
        ids1, ids2 = [i["request_id"] for i in first["items"]], [i["request_id"] for i in second["items"]]
        assert len(ids1) == 20 and set(ids1).isdisjoint(ids2)
        assert first["items"][0]["ts_ms"] >= second["items"][0]["ts_ms"]
        assert all(
            set(i) >= {"request_id", "client_id", "route", "status_code", "steps"} for i in first["items"]
        )
        client.app.state.monitoring.recorder.begin(
            "active-1", "/api/v1/documents/analyze", "POST", "ai", client.now
        )
        active = get(client, "activity").json()["active"]
        assert any(a["request_id"] == "active-1" and a["stale"] is False for a in active)
        filtered = get(client, "activity", range="1h", client="ai-client").json()["items"]
        assert filtered and {i["client_id"] for i in filtered} == {"ai-client"}
        anon = get(client, "activity", range="1h", client="unauthenticated").json()["items"]
        assert anon and {i["client_id"] for i in anon} == {None}


def test_meta_describes_sections_and_retention(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path
):
    with dashboard(fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path) as client:
        meta = get(client, "meta").json()
        assert meta["sections"]["ollama"] == "M5" and meta["sections"]["overview"] is True
        assert meta["poll_seconds"]["activity"] == 5 and meta["poll_seconds"]["overview"] == 15
        assert set(CLIENTS) <= set(meta["clients"]) and "unauthenticated" in meta["clients"]
        assert meta["build"]["source_state"] == "unknown"


# --- isolation, failures, performance ------------------------------------------------------------


def test_dashboard_reads_never_contact_upstreams_and_are_not_counted(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path
):
    with dashboard(fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path) as client:
        monitor = client.app.state.monitoring

        def forbidden(*args, **kwargs):
            raise AssertionError("dashboard must not probe upstreams")

        monkeypatch.setattr(monitor.prober, "probe_ollama", forbidden)
        monkeypatch.setattr(monitor.prober, "probe_stirling", forbidden)
        before = monitor.recorder.totals["requests_recorded"]
        for _ in range(3):
            for path in ENDPOINTS + sorted(PAGES):
                assert client.get(path).status_code == 200, path
        assert monitor.recorder.totals["requests_recorded"] == before  # monitoring traffic is not API traffic
        assert fake_ollama_holder["client"].calls == 0 and fake_stirling_holder["client"].extract_calls == 0


def test_telemetry_failure_is_reported_and_does_not_affect_auth_or_api(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path
):
    with dashboard(fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path) as client:

        def broken(self):
            raise dashboard_module.TelemetryUnavailable()

        monkeypatch.setattr(dashboard_module.Dashboard, "_connect", broken)
        response = get(client, "overview")
        assert response.status_code == 503 and response.json()["code"] == "telemetry_unavailable"
        assert get(client, "security").status_code == 200  # auth store is separate
        assert client.get("/monitor").status_code == 200
        assert client.get("/api/v1/health").status_code == 200


def test_auth_store_failure_closes_the_dashboard(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path
):
    with dashboard(fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path) as client:
        monkeypatch.setattr(
            client.web.store, "get_session", lambda *a: (_ for _ in ()).throw(sqlite3.OperationalError("io"))
        )
        for path in ENDPOINTS + sorted(PAGES):
            response = client.get(path, follow_redirects=False)
            assert response.status_code == 503 and "totals" not in response.text


def test_reads_are_not_blocked_by_a_writer_and_run_in_parallel(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path
):
    with dashboard(fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path) as client:
        writer = sqlite3.connect(str(client.app.state.monitoring.store.path), isolation_level=None)
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("DELETE FROM leases")
        try:
            assert get(client, "overview", range="1h").status_code == 200  # WAL: readers continue
        finally:
            writer.execute("ROLLBACK")
            writer.close()
        with ThreadPoolExecutor(max_workers=12) as pool:
            statuses = list(
                pool.map(
                    lambda i: (
                        get(
                            client, ["overview", "clients", "server", "activity"][i % 4], range="1h"
                        ).status_code
                    ),
                    range(24),
                )
            )
        assert statuses == [200] * 24


def test_dashboard_assets_use_no_unsafe_javascript():
    for name in ("dashboard.js", "monitor.js"):
        source = (web_app.BASE / "static" / "js" / name).read_text(encoding="utf-8")
        for forbidden in (
            "innerHTML",
            "outerHTML",
            "insertAdjacentHTML",
            "eval(",
            "new Function",
            "document.write",
        ):
            assert forbidden not in source, (name, forbidden)
    chart = (web_app.BASE / "static" / "vendor" / "chartjs" / "chart.umd.js").read_text(encoding="utf-8")
    assert "eval(" not in chart and "new Function" not in chart


def test_page_texts_are_passed_as_escaped_data_not_script(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path
):
    with dashboard(fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path, seeded=False) as client:
        html = client.get("/monitor").text
        assert 'data-texts="{&#34;' in html
        assert "<script>" not in html and "<script " in html  # only external scripts


# --- query plans and large databases ---------------------------------------------------------------


def _plan(conn, sql, params) -> str:
    return " | ".join(row[-1] for row in conn.execute("EXPLAIN QUERY PLAN " + sql, params))


def test_core_queries_use_indexes(fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path):
    with dashboard(fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path) as client:
        conn = sqlite3.connect(str(client.app.state.monitoring.store.path))
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3  # v3 (M5)
        plans = {
            "raw range": _plan(
                conn, "SELECT COUNT(*) FROM request_events WHERE ts_ms >= ? AND ts_ms < ?", (0, 1)
            ),
            "raw client": _plan(
                conn,
                "SELECT COUNT(*) FROM request_events WHERE ts_ms >= ? AND ts_ms < ? AND client_id = ?",
                (0, 1, "x"),
            ),
            "raw service": _plan(
                conn,
                "SELECT COUNT(*) FROM request_events WHERE service = ? AND ts_ms >= ? AND ts_ms < ?",
                ("ai", 0, 1),
            ),
            "rollup": _plan(
                conn,
                "SELECT SUM(count) FROM rollup_requests WHERE resolution_ms = ? AND bucket_ms >= ? "
                "AND bucket_ms < ?",
                (60000, 0, 1),
            ),
            "steps join": _plan(
                conn,
                "SELECT COUNT(*) FROM request_steps s JOIN request_events e ON "
                "e.request_id = s.request_id WHERE s.ts_ms >= ? AND s.ts_ms < ?",
                (0, 1),
            ),
            "probe": _plan(
                conn,
                "SELECT ts_ms FROM service_probes WHERE service = ? ORDER BY ts_ms DESC LIMIT 1",
                ("ollama",),
            ),
            "samples": _plan(
                conn, "SELECT * FROM system_samples WHERE ts_ms >= ? AND metric IN (?, ?)", (0, "a", "b")
            ),
            "quality": _plan(
                conn,
                "SELECT DISTINCT bucket_ms FROM data_quality WHERE heartbeats > 0 AND "
                "bucket_ms >= ? AND bucket_ms < ?",
                (0, 1),
            ),
        }
        conn.close()
    for name, plan in plans.items():
        assert "USING" in plan or "PRIMARY KEY" in plan, (name, plan)
        assert "SCAN request_events" not in plan and "SCAN rollup_requests" not in plan, (name, plan)


def test_large_database_queries_stay_fast(fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path):
    with dashboard(fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path, seeded=False) as client:
        monitor = client.app.state.monitoring
        rng = random.Random(3)
        now = client.now
        with monitor._store_lock:
            monitor.store.register_instance(
                instance_id="big", pid=1, now_ms=now - 8 * 24 * HOUR, app_version="t", build=monitor.build
            )
            for chunk in range(60):  # 120k requests over ~7 days
                ts0 = now - 7 * 24 * HOUR + chunk * (7 * 24 * HOUR // 60)
                batch = [
                    synthetic_request(rng, ts0 + rng.randrange(7 * 24 * HOUR // 60), rng.choice(CLIENTS))
                    for _ in range(2000)
                ]
                losses = {
                    m: QualityCounters(heartbeats=1)
                    for m in range(ts0 - ts0 % MINUTE, ts0 + 7 * 24 * HOUR // 60, MINUTE)
                }
                monitor.store.write_batch("big", Batch(batch, [], losses), now, [])
        timings = {}
        for name in ("overview", "clients", "performance", "server", "activity", "ollama", "stirling"):
            for rng_key in ("1h", "24h", "7d"):
                started = time.perf_counter()
                response = get(client, name, range=rng_key)
                timings[(name, rng_key)] = time.perf_counter() - started
                assert response.status_code == 200, (name, rng_key, response.text[:200])
        assert get(client, "overview", range="7d").json()["totals"]["count"] >= 110_000
    slow = {k: round(v, 2) for k, v in timings.items() if v > 2.5}
    assert not slow, slow

"""Ollama and Stirling PDF views of the data API (Phase M5). Synthetic data only."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest

from system.monitoring import store as store_module
from system.monitoring.web import dashboard as dashboard_module
from system.monitoring.web.app import SESSION_COOKIE
from system.monitoring.web.upstreams import Agg, _ollama_location
from tests.conftest import AI_KEY, ASSISTANT_KEY, LAB_KEY, VALID_KEY, auth_headers
from tests.monitoring_seed import seed
from tests.monitoring_web_support import full_login, user_entry, web_client

MINUTE = 60_000
CLIENTS = ("enabled-client", "ai-client", "assistant-client")


@contextmanager
def upstream_dashboard(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path, *, login=True, seeded=True, **seed_kwargs
):
    entry = user_entry()
    with web_client(monkeypatch, tmp_path, users=[entry]) as client:
        monitor = client.app.state.monitoring
        now = monitor._clock()
        info = {}
        if seeded:
            with monitor._store_lock:
                info = seed(monitor.store, now_ms=now, minutes=150, clients=CLIENTS, **seed_kwargs)
        if login:
            assert full_login(client, entry["totp_secret"]).status_code == 303
        client.now, client.seeded, client.entry = now, info.get("requests", []), entry
        yield client


def get(test_client, name, **params):
    return test_client.get(f"/monitor/api/v1/{name}", params=params)


def steps_in(client, start, end, names):
    return [(r, s) for r in client.seeded if start <= r.ts_ms < end for s in r.steps if s.step in names]


# --- access & validation ---------------------------------------------------------------------


def test_new_endpoints_require_a_monitoring_session(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path
):
    with upstream_dashboard(
        fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path, login=False, seeded=False
    ) as client:
        for path in ("/monitor/api/v1/ollama", "/monitor/api/v1/stirling"):
            for headers in (
                {},
                auth_headers(VALID_KEY),
                auth_headers(AI_KEY),
                auth_headers(LAB_KEY),
                auth_headers(ASSISTANT_KEY),
                {"Cookie": f"{SESSION_COOKIE}=forged-session-value-1234"},
            ):
                assert client.get(path, headers=headers).status_code == 401
        for path in ("/monitor/ollama", "/monitor/stirling"):
            assert client.get(path, follow_redirects=False).status_code == 303


def test_expired_session_is_rejected(fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path):
    with upstream_dashboard(
        fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path, seeded=False
    ) as client:
        assert get(client, "ollama").status_code == 200
        base = client.web._clock()
        client.web._clock = lambda: base + 16 * MINUTE
        assert get(client, "ollama").status_code == 401 and get(client, "stirling").status_code == 401


@pytest.mark.parametrize(
    "name, params, code",
    [
        ("ollama", {"model": "<script>alert(1)</script>"}, "invalid_model"),
        ("ollama", {"model": "x' OR '1'='1"}, "invalid_model"),
        ("ollama", {"operation": "delete_model"}, "invalid_operation"),
        ("ollama", {"status": "200 OR 1=1"}, "invalid_status"),
        ("ollama", {"client": "nobody"}, "invalid_client"),
        ("ollama", {"pull": "llama3"}, "unknown_parameter"),
        ("ollama", {"url": "http://169.254.169.254/"}, "unknown_parameter"),
        ("stirling", {"operation": "merge"}, "invalid_operation"),
        ("stirling", {"route": "/x"}, "unknown_parameter"),
        ("stirling", {"range": "1y"}, "invalid_range"),
    ],
)
def test_filters_are_validated(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path, name, params, code
):
    with upstream_dashboard(fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path) as client:
        response = get(client, name, **params)
        assert response.status_code == 400 and response.json()["code"] == code
        assert get(client, name).status_code == 200


def test_empty_installation(fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path):
    with upstream_dashboard(
        fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path, seeded=False
    ) as client:
        o = get(client, "ollama").json()
        assert o["calls"]["count"] == 0 and o["calls"]["avg_ms"] is None
        assert o["calls"]["throughput"]["generation_tokens_per_s"] is None
        assert o["models"]["installed"] == [] and o["models"]["measured_ms"] is None
        assert o["health"]["state"] == "not_configured"  # probes are off in this configuration
        s = get(client, "stirling").json()
        assert s["total"]["count"] == 0 and s["total"]["failure_rate"] is None


# --- Ollama ---------------------------------------------------------------------------------------


def test_ai_calls_warmups_and_weighted_throughput_match_the_raw_data(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path
):
    with upstream_dashboard(fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path) as client:
        data = get(client, "ollama", range="1h").json()
        rng = data["range"]
        assert rng["source"] == "raw"  # within raw retention: exact
        calls = steps_in(client, rng["start_ms"], rng["end_ms"], ("ai.generate",))
        warm = steps_in(client, rng["start_ms"], rng["end_ms"], ("ai.warmup",))
        assert data["calls"]["count"] == len(calls) and data["warmups"]["count"] == len(warm)
        assert data["calls"]["success"] == sum(1 for _, s in calls if s.outcome == "ok")
        pairs = [
            s for _, s in calls if s.tokens_out is not None and s.ollama_eval_ms and s.ollama_eval_ms > 0
        ]
        expected = sum(s.tokens_out for s in pairs) / (sum(s.ollama_eval_ms for s in pairs) / 1000)
        tp = data["calls"]["throughput"]
        assert tp["generation_tokens_per_s"] == pytest.approx(expected)
        assert tp["generation_pairs"] == len(pairs) < len(calls)  # failed and older-Ollama calls excluded
        naive = sum(s.tokens_out / (s.ollama_eval_ms / 1000) for s in pairs) / len(pairs)
        assert tp["generation_tokens_per_s"] != pytest.approx(naive)  # weighted, not a mean of rates
        p_pairs = [s for _, s in calls if s.tokens_in is not None and s.ollama_prompt_eval_ms]
        assert tp["prompt_tokens_per_s"] == pytest.approx(
            sum(s.tokens_in for s in p_pairs) / (sum(s.ollama_prompt_eval_ms for s in p_pairs) / 1000)
        )
        assert data["calls"]["tokens"]["output"] == sum(
            s.tokens_out for _, s in calls if s.tokens_out is not None
        )
        assert set(data["by_operation"]) <= {"analyze", "assistant"} and "warmup" not in data["by_operation"]
        assert data["calls"]["outcomes"].keys() <= {"ai_timeout", "ai_unavailable"}
        assert data["scope"] == "docpipe_observed"


def test_rollup_throughput_equals_raw_for_complete_buckets(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path
):
    with upstream_dashboard(fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path) as client:
        monkeypatch.setattr(dashboard_module, "RAW_EXACT_LIMIT", 1)  # force the rollup source
        import system.monitoring.web.upstreams as upstreams

        monkeypatch.setattr(upstreams, "RAW_EXACT_LIMIT", 1)
        data = get(client, "ollama", range="24h").json()
        rng = data["range"]
        assert rng["source"] == "minute" and data["calls"]["percentile_method"] == "histogram"
        calls = steps_in(client, rng["effective_start_ms"], rng["end_ms"], ("ai.generate",))
        pairs = [s for _, s in calls if s.tokens_out is not None and s.ollama_eval_ms]
        assert data["calls"]["count"] == len(calls)  # one source: no double counting
        assert data["calls"]["throughput"]["partial"] is False
        assert data["calls"]["throughput"]["generation_tokens_per_s"] == pytest.approx(
            sum(s.tokens_out for s in pairs) / (sum(s.ollama_eval_ms for s in pairs) / 1000)
        )
        # above RAW_EXACT_LIMIT even a 1 h range falls back to minute rollups
        assert get(client, "ollama", range="1h").json()["range"]["source"] == "minute"
        assert get(client, "ollama", range="24h", client="ai-client").status_code == 200  # still raw-capable
        monkeypatch.setattr(dashboard_module.Dashboard, "parse_range", _shifted_range(client.now))
        too_old = get(client, "ollama", range="30d", client="ai-client")
        assert too_old.status_code == 400 and too_old.json()["code"] == "client_filter_requires_raw_retention"


def _shifted_range(now):
    original = dashboard_module.Dashboard.parse_range

    def parse(self, params, now_ms, *, default="1h"):
        rng = original(self, params, now_ms, default=default)
        if params.get("range") == "30d":  # pretend the range starts before the raw retention
            return dashboard_module.Range(
                rng.key, now - 30 * 86_400_000, rng.end, "hour", rng.bucket, rng.first_bucket
            )
        return rng

    return parse


def test_operation_model_client_and_status_filters(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path
):
    with upstream_dashboard(fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path) as client:
        warm = get(client, "ollama", operation="warmup").json()
        assert warm["calls"]["count"] == 0 and warm["warmups"]["count"] > 0
        analyze = get(client, "ollama", operation="analyze").json()
        assert set(analyze["by_operation"]) == {"analyze"} and analyze["warmups"]["count"] == 0
        light = get(client, "ollama", model="light-test-model").json()
        assert set(light["by_model"]) == {"light-test-model"}
        ok = get(client, "ollama", status="success").json()
        assert ok["calls"]["failed"] == 0 and ok["calls"]["count"] > 0
        mine = get(client, "ollama", client="ai-client").json()
        assert [c["client_id"] for c in mine["by_client"]] == ["ai-client"]


def test_models_installed_loaded_and_snapshot_freshness(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path
):
    with upstream_dashboard(
        fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path, probes_until_ago=40
    ) as client:
        import dataclasses

        client.web.dashboard.settings = dataclasses.replace(
            client.web.dashboard.settings, probe_interval_seconds=30
        )  # stale after 3 probe intervals + 30 s
        models = get(client, "ollama").json()["models"]
        assert models["source"] == "ollama" and models["stale"] is True  # last snapshot ~40 minutes old
        names = {m["name"]: m for m in models["installed"]}
        assert set(names) == {"light-test-model", "standard-test-model", "legacy-model:latest"}
        loaded = models["loaded"]
        assert [m["name"] for m in loaded] == ["light-test-model"]
        assert (
            loaded[0]["size_vram_bytes"] == 1_050_000_000 and loaded[0]["expires_ms"] > models["measured_ms"]
        )
        assert (
            names["standard-test-model"]["size_vram_bytes"] is None
            and names["standard-test-model"]["loaded"] is False
        )
        legacy = names["legacy-model:latest"]
        assert legacy["size_bytes"] is None and legacy["family"] is None and legacy["quantization"] is None
        assert models["reported_vram_bytes"] == 1_050_000_000


def test_offline_ollama_keeps_history_and_reports_failures(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path
):
    with upstream_dashboard(
        fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path, ollama_down=(20, 0)
    ) as client:
        monkeypatch.setattr(client.app.state.monitoring.settings.probes.__class__, "ollama", True)
        data = get(client, "ollama", range="1h").json()
        probes = data["probes"]
        assert probes["failures"]["connection_error"] > 0 and probes["status_counts"]["ok"] > 0
        assert probes["recent"][0]["status"] == "connection_error"
        assert data["calls"]["count"] > 0  # history stays available while the service is down
        assert data["models"]["installed"]  # last known snapshot, from before the outage


def test_health_states_from_stored_probes(fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path):
    import dataclasses

    from system.monitoring.settings import ProbeSettings

    entry = user_entry()
    with web_client(monkeypatch, tmp_path, users=[entry]) as client:
        monitor = client.app.state.monitoring
        client.web.dashboard.settings = dataclasses.replace(
            client.web.dashboard.settings, probes=ProbeSettings(ollama=True, stirling=True)
        )
        now = monitor._clock()
        with monitor._store_lock:
            seed(
                monitor.store, now_ms=now, minutes=30, clients=CLIENTS, ollama_down=(3, 0), stirling_down=None
            )
        full_login(client, entry["totp_secret"])
        assert get(client, "ollama").json()["health"]["state"] == "offline"
        stirling = get(client, "stirling").json()["health"]
        assert stirling["state"] in ("online", "degraded") and stirling["version"] == "3.2.2"


def test_active_ai_calls_are_only_real_ollama_calls(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path
):
    with upstream_dashboard(
        fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path, seeded=False
    ) as client:
        fake = fake_ollama_holder["client"]
        release, started = threading.Event(), threading.Semaphore(0)
        original = fake.generate_structured

        def blocking(*args, **kwargs):
            started.release()
            release.wait(10)
            return original(*args, **kwargs)

        monkeypatch.setattr(fake, "generate_structured", blocking)
        stirling = fake_stirling_holder["client"]
        doc_release = threading.Event()
        original_extract = stirling.extract_text

        def slow_extract(*args, **kwargs):
            doc_release.wait(10)
            return original_extract(*args, **kwargs)

        monkeypatch.setattr(stirling, "extract_text", slow_extract)
        from tests.conftest import pdf_bytes

        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [
                pool.submit(
                    client.post,
                    "/api/v1/documents/analyze",
                    headers=auth_headers(AI_KEY),
                    json={"mode": "maintenance_extraction", "text": "x", "context": {}},
                )
                for _ in range(3)
            ]
            futures.append(
                pool.submit(
                    client.post,
                    "/api/v1/documents/extract-text",
                    headers=auth_headers(VALID_KEY),
                    files={"file": ("a.pdf", pdf_bytes(), "application/pdf")},
                )
            )
            for _ in range(3):
                assert started.acquire(timeout=10)
            active = get(client, "ollama").json()["active"]
            assert len(active) == 3  # the running document request is not AI processing
            assert {a["client_id"] for a in active} == {"ai-client"}
            assert {a["model"] for a in active} == {"light-test-model"}
            assert {a["operation"] for a in active} == {"analyze"} and all(
                a["state"] == "running" for a in active
            )
            assert all(
                set(a)
                <= {
                    "request_id",
                    "client_id",
                    "model",
                    "operation",
                    "step",
                    "started_ms",
                    "age_ms",
                    "stale",
                    "state",
                }
                for a in active
            )
            release.set()
            doc_release.set()
            assert all(f.result().status_code == 200 for f in futures)
        assert get(client, "ollama").json()["active"] == []


def test_running_ai_steps_of_other_workers_and_stale_snapshots(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path
):
    with upstream_dashboard(
        fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path, seeded=False
    ) as client:
        db = client.app.state.monitoring.store._db()
        now = client.now
        with client.app.state.monitoring._store_lock:
            db.execute(
                "INSERT INTO live_ai_steps VALUES ('worker-2', 'r-fresh', 'ai-client', 'light-test-model', "
                "'assistant', 'ai.generate', ?, ?)",
                (now - 4000, now - 1000),
            )
            db.execute(
                "INSERT INTO live_ai_steps VALUES ('worker-3', 'r-old', 'nobody', '<b>x</b>', 'evil', "
                "'ai.generate', ?, ?)",
                (now - 600_000, now - 300_000),  # older than 3 flush intervals (30 s in tests) + 5 s
            )
        active = {a["request_id"]: a for a in get(client, "ollama").json()["active"]}
        assert active["r-fresh"]["stale"] is False and active["r-fresh"]["state"] == "running"
        assert active["r-old"]["stale"] is True and active["r-old"]["state"] == "unknown"
        assert active["r-old"]["client_id"] is None and active["r-old"]["operation"] == "other"


def test_gpu_values_are_never_derived_from_ollama_and_location_is_classified(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path
):
    with upstream_dashboard(fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path) as client:
        data = get(client, "ollama").json()
        host_gpu = data["gpu"]["docpipe_host"]
        assert all(v["value"] is None for v in host_gpu.values())
        assert data["gpu"]["ollama_location"] == "remote"  # test config points to http://ollama.test
        assert "ollama.test" not in get(client, "ollama").text  # the upstream address is never exposed
    assert _ollama_location("http://127.0.0.1:11434") == "local"
    assert _ollama_location("http://localhost:11434") == "local"
    assert _ollama_location("http://[::1]:11434") == "local"
    assert _ollama_location("http://10.0.0.5:11434") == "remote"
    assert _ollama_location("not a url") == "unknown"


def test_manipulated_model_names_are_never_returned_raw(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path
):
    with upstream_dashboard(
        fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path, seeded=False
    ) as client:
        db = client.app.state.monitoring.store._db()
        with client.app.state.monitoring._store_lock:
            db.execute(
                "INSERT INTO ollama_models (ts_ms, name, installed, loaded, family, quantization) VALUES "
                "(?, '<img src=x onerror=alert(1)>', 1, 0, '\"><script>', 'q\nx')",
                (client.now,),
            )
        text = get(client, "ollama").text
        assert "<img" not in text and "<script" not in text and "onerror" not in text
        model = get(client, "ollama").json()["models"]["installed"][0]
        assert model["name"] == "<invalid>" and model["family"] is None and model["quantization"] is None


def test_rollup_rows_without_pair_sums_are_reported_as_partial():
    agg = Agg()
    base = {
        "count": 2,
        "outcome": "ok",
        "dur_sum": 100.0,
        "dur_min": 40.0,
        "dur_max": 60.0,
        "hist": [0] * len(store_module.BUCKET_COLUMNS),
        "ollama_known": 2,
        "load_sum": 0.0,
        "pe_sum": 10.0,
        "ev_sum": 50.0,
        "tokens_in": 20,
        "tokens_out": 30,
        "tokens_known": 2,
    }
    agg.add_rollup({**base, **dict.fromkeys(store_module._PAIR_COLUMNS)})  # pre-v3 bucket
    agg.add_rollup(
        {
            **base,
            "eval_pair_tokens": 30,
            "eval_pair_ms": 500.0,
            "eval_pairs": 2,
            "prompt_pair_tokens": 20,
            "prompt_pair_ms": 100.0,
            "prompt_pairs": 2,
        }
    )
    tp = agg.result(False, ai=True)["throughput"]
    assert tp["partial"] is True and tp["calls_without_pair_data"] == 2
    assert tp["generation_tokens_per_s"] == pytest.approx(60.0)  # only the bucket with pairs
    assert tp["prompt_tokens_per_s"] == pytest.approx(200.0)
    empty = Agg()
    empty.add_rollup({**base, **dict.fromkeys(store_module._PAIR_COLUMNS)})
    assert empty.result(False, ai=True)["throughput"]["generation_tokens_per_s"] is None


# --- Stirling ---------------------------------------------------------------------------------------


def test_stirling_counts_only_real_stirling_operations(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path
):
    with upstream_dashboard(fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path) as client:
        data = get(client, "stirling", range="1h").json()
        rng = data["range"]
        real = steps_in(
            client,
            rng["start_ms"],
            rng["end_ms"],
            ("stirling.extract_text", "stirling.ocr", "stirling.convert"),
        )
        assert data["total"]["count"] == len(real)
        by_op = data["by_operation"]
        assert set(by_op) == {"extract_text", "ocr", "convert"}
        assert by_op["ocr"]["count"] == sum(1 for _, s in real if s.step == "stirling.ocr")
        assert (
            "/api/v1/documents/forms/inspect" in data["by_route"]
        )  # OCR called by form analysis is Stirling use
        assert "/api/v1/documents/forms/fill" not in data["by_route"]  # native form filling is not
        assert data["total"]["failed"] == sum(1 for _, s in real if s.outcome != "ok")
        ocr_only = get(client, "stirling", operation="ocr", status="failed").json()
        assert set(ocr_only["by_operation"]) <= {"ocr"} and ocr_only["total"]["success"] == 0


def test_stirling_outage_history(fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path):
    with upstream_dashboard(
        fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path, stirling_down=(30, 10)
    ) as client:
        probes = get(client, "stirling", range="1h").json()["probes"]
        assert probes["failures"]["timeout"] > 0 and probes["status_counts"]["ok"] > 0
        assert any(f and f > 0 for f in probes["failed"]) and any(o and o > 0 for o in probes["ok"])


# --- isolation ---------------------------------------------------------------------------------------


def test_dashboard_never_contacts_ollama_or_stirling(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path
):
    with upstream_dashboard(fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path) as client:
        monitor = client.app.state.monitoring

        def forbidden(*args, **kwargs):
            raise AssertionError("no upstream request from the dashboard")

        monkeypatch.setattr(monitor.prober, "probe_ollama", forbidden)
        monkeypatch.setattr(monitor.prober, "probe_stirling", forbidden)
        for _ in range(3):
            for path in (
                "/monitor/api/v1/ollama",
                "/monitor/api/v1/stirling",
                "/monitor/ollama",
                "/monitor/stirling",
            ):
                assert client.get(path).status_code == 200
        assert fake_ollama_holder["client"].calls == 0 and fake_ollama_holder["client"].warmup_calls == 0
        assert (
            fake_stirling_holder["client"].extract_calls == 0
            and fake_stirling_holder["client"].ocr_calls == 0
        )


def test_telemetry_outage_returns_503_without_affecting_the_api(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path
):
    with upstream_dashboard(fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path) as client:

        def broken(self):
            raise dashboard_module.TelemetryUnavailable()

        monkeypatch.setattr(dashboard_module.Dashboard, "_connect", broken)
        for name in ("ollama", "stirling"):
            response = get(client, name)
            assert response.status_code == 503 and response.json()["code"] == "telemetry_unavailable"
        assert client.get("/api/v1/health").status_code == 200

"""Monitoring config parsing, defaults and build info (Phase M2)."""

from __future__ import annotations

from pathlib import Path

import pytest

from system import config as config_module
from system.config import ConfigError, Settings, load_settings
from system.monitoring.buildinfo import read_build_info
from system.monitoring.settings import MonitoringConfigError, MonitoringSettings, parse_monitoring


def test_monitoring_is_disabled_by_default():
    assert MonitoringSettings().enabled is False
    assert parse_monitoring(None).enabled is False
    assert parse_monitoring({}).enabled is False
    assert Settings.__dataclass_fields__["monitoring"].default.enabled is False


def test_defaults_match_the_approved_concept():
    s = parse_monitoring({"enabled": True})
    r = s.retention
    assert (r.raw_days, r.minute_rollup_days, r.hour_rollup_days) == (7, 14, 400)
    assert (r.system_raw_hours, r.system_minute_days, r.probe_days, r.event_days) == (48, 30, 30, 30)
    assert s.storage.max_bytes == 512 * 1024 * 1024
    assert s.flush_interval_seconds == 5 and s.buffer_max_requests == 10_000
    assert s.db_path == Path("monitoring-data") / "monitor.db"


def test_every_limit_is_configurable():
    s = parse_monitoring(
        {
            "enabled": True,
            "data_dir": "/data/mon",
            "flush_interval_seconds": 2,
            "buffer_max_requests": 500,
            "retention": {
                "raw_days": 2,
                "minute_rollup_days": 3,
                "hour_rollup_days": 30,
                "max_event_rows": 5000,
            },
            "storage": {"max_bytes": 64 * 1024 * 1024, "min_free_disk_bytes": 0},
            "probes": {"ollama": False, "stirling": True, "timeout_seconds": 1.5},
        }
    )
    assert s.data_dir == Path("/data/mon") and s.flush_interval_seconds == 2.0
    assert s.retention.raw_days == 2 and s.retention.max_event_rows == 5000
    assert s.storage.max_bytes == 64 * 1024 * 1024
    assert s.probes.ollama is False and s.probes.timeout_seconds == 1.5


@pytest.mark.parametrize(
    "raw",
    [
        {"enabled": "yes"},
        {"unknown": 1},
        {"flush_interval_seconds": 0},
        {"flush_interval_seconds": 61},
        {"flush_interval_seconds": True},
        {"buffer_max_requests": 10.5},
        {"retention": {"raw_days": 0}},
        {"retention": {"raw_days": 10, "minute_rollup_days": 5}},
        {"retention": {"bogus": 1}},
        {"storage": {"max_bytes": 1024}},
        {"probes": {"ollama": 1}},
        {"probes": {"other": True}},
        {"data_dir": ""},
        {"retention": []},
    ],
)
def test_invalid_values_are_rejected(raw):
    with pytest.raises(MonitoringConfigError):
        parse_monitoring(raw)


def test_invalid_monitoring_config_fails_startup(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text("monitoring:\n  enabled: true\n  flush_interval_seconds: 0\n", encoding="utf-8")
    monkeypatch.setenv("DOCPIPE_CONFIG", str(path))
    with pytest.raises(ConfigError, match="flush_interval_seconds"):
        load_settings()


def test_config_without_monitoring_section_loads_disabled(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text("server:\n  max_file_size_mb: 5\n", encoding="utf-8")
    monkeypatch.setenv("DOCPIPE_CONFIG", str(path))
    assert load_settings().monitoring == MonitoringSettings()
    assert config_module.DEFAULT_CONFIG_PATH == "config.yaml"


# --- build info -------------------------------------------------------------------------


def test_build_info_unknown_when_nothing_is_declared():
    info = read_build_info({})
    assert (info.commit, info.image_tag, info.local_changes, info.source_state) == (
        None,
        None,
        None,
        "unknown",
    )


def test_build_info_never_claims_an_unmodified_commit_without_a_declaration():
    info = read_build_info({"DOCPIPE_BUILD_COMMIT": "0c05f96", "DOCPIPE_IMAGE_TAG": "0c05f96-hubdix"})
    assert info.commit == "0c05f96" and info.image_tag == "0c05f96-hubdix"
    assert info.source_state == "unknown"


def test_build_info_local_patches_mark_the_build_modified():
    info = read_build_info(
        {
            "DOCPIPE_BUILD_COMMIT": "0c05f96",
            "DOCPIPE_BUILD_LOCAL_CHANGES": "false",
            "DOCPIPE_BUILD_LOCAL_PATCHES": "ollama-think-false, Bad Value!",
        }
    )
    assert info.local_patches == ("ollama-think-false",)
    assert info.source_state == "modified"
    assert read_build_info({"DOCPIPE_BUILD_LOCAL_CHANGES": "true"}).source_state == "modified"
    assert read_build_info({"DOCPIPE_BUILD_LOCAL_CHANGES": "false"}).source_state == "clean"


@pytest.mark.parametrize(
    "env",
    [
        {"DOCPIPE_BUILD_COMMIT": "not-a-commit"},
        {"DOCPIPE_BUILD_COMMIT": "0c05f96\nX"},
        {"DOCPIPE_IMAGE_TAG": "tag with space"},
        {"DOCPIPE_IMAGE_TAG": "x" * 200},
    ],
)
def test_invalid_build_values_are_dropped_not_stored(env):
    info = read_build_info(env)
    assert info.commit is None and info.image_tag is None


def test_config_example_documents_the_defaults(monkeypatch):
    monkeypatch.setenv("DOCPIPE_CONFIG", "config.example.yaml")
    assert load_settings().monitoring == MonitoringSettings()

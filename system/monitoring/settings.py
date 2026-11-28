"""`monitoring:` config section.

Disabled by default. Every interval, buffer size, retention period and
storage limit is configurable, with bounds that keep a typo from turning the
collector into a busy loop or an unbounded store. Parsing errors raise
MonitoringConfigError, which system/config.py turns into a startup
ConfigError, like every other invalid config value.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from system.monitoring.web_settings import WebConfigError, WebSettings, parse_web

DEFAULT_DATA_DIR = Path("monitoring-data")
DB_FILENAME = "monitor.db"

MIB = 1024 * 1024


class MonitoringConfigError(ValueError):
    """An invalid `monitoring:` value - always reported at startup."""


@dataclass(frozen=True)
class RetentionSettings:
    raw_days: int = 7
    minute_rollup_days: int = 14
    hour_rollup_days: int = 400
    system_raw_hours: int = 48
    system_minute_days: int = 30
    probe_days: int = 30
    event_days: int = 30
    data_quality_days: int = 30
    max_event_rows: int = 100_000


@dataclass(frozen=True)
class StorageSettings:
    # Upper bound for the database file plus its -wal and -shm files.
    max_bytes: int = 512 * MIB
    # Writes pause (and are counted as dropped) while the volume holding the
    # database has less free space than this.
    min_free_disk_bytes: int = 256 * MIB


@dataclass(frozen=True)
class ProbeSettings:
    ollama: bool = True
    stirling: bool = True
    timeout_seconds: float = 3.0


@dataclass(frozen=True)
class MonitoringSettings:
    enabled: bool = False
    data_dir: Path = DEFAULT_DATA_DIR
    flush_interval_seconds: float = 5.0
    system_sample_interval_seconds: float = 15.0
    probe_interval_seconds: float = 30.0
    maintenance_interval_seconds: float = 60.0
    buffer_max_requests: int = 10_000
    buffer_max_events: int = 1_000
    max_steps_per_request: int = 64
    retention: RetentionSettings = field(default_factory=RetentionSettings)
    storage: StorageSettings = field(default_factory=StorageSettings)
    probes: ProbeSettings = field(default_factory=ProbeSettings)
    # Operator web interface (Phase M3) - separately enabled, see web_settings.py.
    web: WebSettings = field(default_factory=WebSettings)

    @property
    def db_path(self) -> Path:
        return self.data_dir / DB_FILENAME


# key -> (min, max) for numeric values
_TOP_BOUNDS: dict[str, tuple[float, float]] = {
    "flush_interval_seconds": (1, 30),  # < 60 s: every alive minute gets a heartbeat
    "system_sample_interval_seconds": (5, 3600),
    "probe_interval_seconds": (10, 3600),
    "maintenance_interval_seconds": (10, 3600),
    "buffer_max_requests": (100, 1_000_000),
    "buffer_max_events": (10, 100_000),
    "max_steps_per_request": (1, 1024),
}
_RETENTION_BOUNDS: dict[str, tuple[float, float]] = {
    "raw_days": (1, 90),
    "minute_rollup_days": (1, 365),
    "hour_rollup_days": (1, 3650),
    "system_raw_hours": (1, 24 * 90),
    "system_minute_days": (1, 365),
    "probe_days": (1, 365),
    "event_days": (1, 365),
    "data_quality_days": (1, 3650),
    "max_event_rows": (1000, 10_000_000),
}
_STORAGE_BOUNDS: dict[str, tuple[float, float]] = {
    "max_bytes": (16 * MIB, 1024 * 1024 * MIB),
    "min_free_disk_bytes": (0, 1024 * 1024 * MIB),
}
_PROBE_BOUNDS: dict[str, tuple[float, float]] = {"timeout_seconds": (0.5, 30)}
_INT_KEYS = {
    "buffer_max_requests",
    "buffer_max_events",
    "max_steps_per_request",
    *_RETENTION_BOUNDS,
    *_STORAGE_BOUNDS,
}


def _mapping(raw: object, name: str) -> dict:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise MonitoringConfigError(f"{name} must be a mapping.")
    return raw


def _bool(raw: dict, key: str, name: str, default: bool) -> bool:
    if key not in raw:
        return default
    value = raw[key]
    if not isinstance(value, bool):
        raise MonitoringConfigError(f"{name}.{key} must be true or false.")
    return value


def _numbers(raw: dict, bounds: dict[str, tuple[float, float]], name: str, allowed: set[str]) -> dict:
    unknown = set(raw) - allowed
    if unknown:
        raise MonitoringConfigError(f"{name} has unknown keys: {sorted(unknown)}.")
    values: dict = {}
    for key, (low, high) in bounds.items():
        if key not in raw:
            continue
        value = raw[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise MonitoringConfigError(f"{name}.{key} must be a number.")
        if key in _INT_KEYS and int(value) != value:
            raise MonitoringConfigError(f"{name}.{key} must be an integer.")
        if not low <= value <= high:
            raise MonitoringConfigError(f"{name}.{key} must be between {low:g} and {high:g}.")
        values[key] = int(value) if key in _INT_KEYS else float(value)
    return values


def parse_monitoring(raw: object) -> MonitoringSettings:
    raw = _mapping(raw, "monitoring")
    top_keys = {"enabled", "data_dir", "retention", "storage", "probes", "web", *_TOP_BOUNDS}
    values = _numbers(
        {k: v for k, v in raw.items() if k in _TOP_BOUNDS}, _TOP_BOUNDS, "monitoring", set(_TOP_BOUNDS)
    )
    unknown = set(raw) - top_keys
    if unknown:
        raise MonitoringConfigError(f"monitoring has unknown keys: {sorted(unknown)}.")

    enabled = _bool(raw, "enabled", "monitoring", False)

    data_dir = DEFAULT_DATA_DIR
    if "data_dir" in raw:
        if not isinstance(raw["data_dir"], str) or not raw["data_dir"].strip():
            raise MonitoringConfigError("monitoring.data_dir must be a non-empty path.")
        data_dir = Path(raw["data_dir"].strip())

    retention = RetentionSettings(
        **_numbers(
            _mapping(raw.get("retention"), "monitoring.retention"),
            _RETENTION_BOUNDS,
            "monitoring.retention",
            set(_RETENTION_BOUNDS),
        )
    )
    storage = StorageSettings(
        **_numbers(
            _mapping(raw.get("storage"), "monitoring.storage"),
            _STORAGE_BOUNDS,
            "monitoring.storage",
            set(_STORAGE_BOUNDS),
        )
    )
    raw_probes = _mapping(raw.get("probes"), "monitoring.probes")
    probe_numbers = _numbers(
        {k: v for k, v in raw_probes.items() if k in _PROBE_BOUNDS},
        _PROBE_BOUNDS,
        "monitoring.probes",
        set(_PROBE_BOUNDS),
    )
    unknown_probe = set(raw_probes) - {"ollama", "stirling", *_PROBE_BOUNDS}
    if unknown_probe:
        raise MonitoringConfigError(f"monitoring.probes has unknown keys: {sorted(unknown_probe)}.")
    probes = ProbeSettings(
        ollama=_bool(raw_probes, "ollama", "monitoring.probes", True),
        stirling=_bool(raw_probes, "stirling", "monitoring.probes", True),
        **probe_numbers,
    )

    if retention.minute_rollup_days < retention.raw_days:
        raise MonitoringConfigError("monitoring.retention.minute_rollup_days must be >= raw_days.")
    if retention.hour_rollup_days < retention.minute_rollup_days:
        raise MonitoringConfigError("monitoring.retention.hour_rollup_days must be >= minute_rollup_days.")

    try:
        web = parse_web(raw.get("web"), collection_enabled=enabled)
    except WebConfigError as exc:
        raise MonitoringConfigError(str(exc)) from exc

    return MonitoringSettings(
        enabled=enabled, data_dir=data_dir, retention=retention, storage=storage, probes=probes, web=web,
        **values,
    )

"""System sampler: host vs container vs process labels, missing values (Phase M2).

Uses synthetic /proc and cgroup trees under tmp_path - no real system access.
"""

from __future__ import annotations

from pathlib import Path

from system.monitoring.system_metrics import NoGpuProvider, Sample, SystemSampler


def write_proc(root: Path, *, cpu=(100, 0, 100, 800, 0, 0, 0, 0), rx=1000, tx=2000, utime=100) -> None:
    (root / "self" / "fd").mkdir(parents=True, exist_ok=True)
    (root / "net").mkdir(exist_ok=True)
    (root / "stat").write_text("cpu  " + " ".join(map(str, cpu)) + "\ncpu0 1 2 3\ncpu1 1 2 3\nintr 1\n")
    (root / "loadavg").write_text("0.42 0.30 0.20 1/100 999\n")
    (root / "meminfo").write_text("MemTotal: 16000000 kB\nMemFree: 1000 kB\nMemAvailable: 4000000 kB\n")
    (root / "self" / "statm").write_text("1000 250 10 1 0 100 0\n")
    fields = ["S"] + ["0"] * 10 + [str(utime), "50"] + ["0"] * 4 + ["7"] + ["0"] * 10
    (root / "self" / "stat").write_text("123 (python (x)) " + " ".join(fields) + "\n")
    for n in range(3):
        (root / "self" / "fd" / str(n)).write_text("")
    (root / "net" / "dev").write_text(
        "Inter-|   Receive\n face |bytes\n"
        f"    lo: 999999 0 0 0 0 0 0 0 999999 0 0 0 0 0 0 0\n"
        f"  eth0: {rx} 0 0 0 0 0 0 0 {tx} 0 0 0 0 0 0 0\n"
    )


def write_cgroup(root: Path, *, usage=1_000_000, memory_max="max", cpu_max="200000 100000") -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "cgroup.controllers").write_text("cpu memory\n")
    (root / "memory.current").write_text("524288000\n")
    (root / "memory.max").write_text(f"{memory_max}\n")
    (root / "cpu.stat").write_text(f"usage_usec {usage}\nuser_usec 1\nsystem_usec 1\n")
    (root / "cpu.max").write_text(f"{cpu_max}\n")


def by_metric(samples: list[Sample]) -> dict[str, Sample]:
    return {s.metric: s for s in samples}


def sampler(tmp_path, **kwargs) -> SystemSampler:
    return SystemSampler(
        data_dir=tmp_path,
        proc_root=tmp_path / "proc",
        cgroup_root=tmp_path / "cg",
        platform="linux",
        clock_ticks=100,
        page_size=4096,
        **kwargs,
    )


def test_host_container_and_process_values_are_labeled_separately(tmp_path):
    write_proc(tmp_path / "proc")
    write_cgroup(tmp_path / "cg")
    s = sampler(tmp_path)
    first = by_metric(s.sample(10.0))
    assert (
        first["host.cpu.percent"].status == "unavailable" and first["host.cpu.percent"].reason == "warming_up"
    )
    write_proc(tmp_path / "proc", cpu=(200, 0, 200, 1400, 0, 0, 0, 0), rx=3000, tx=6000, utime=300)
    write_cgroup(tmp_path / "cg", usage=3_000_000)
    m = by_metric(s.sample(12.0))
    assert (m["host.cpu.percent"].value, m["host.cpu.percent"].source) == (25.0, "host")
    assert m["host.cpu.cores"].value == 2
    assert m["host.memory.total_bytes"].value == 16000000 * 1024
    assert m["host.memory.used_percent"].value == 75.0
    assert (m["container.memory.current_bytes"].value, m["container.memory.current_bytes"].source) == (
        524288000,
        "container",
    )
    assert m["container.memory.limit_bytes"].status == "not_applicable"
    assert m["container.memory.limit_bytes"].reason == "unlimited"
    assert m["container.cpu.cores_used"].value == 1.0  # 2 CPU-seconds in 2 s
    assert m["container.cpu.limit_cores"].value == 2.0
    assert (m["process.rss_bytes"].value, m["process.rss_bytes"].source) == (250 * 4096, "process")
    assert m["process.threads"].value == 7
    assert m["process.cpu.percent"].value == 100.0  # 2 s CPU (200 ticks) in 2 s wall
    assert m["process.open_fds"].value == 3
    assert m["net.rx_bytes_per_s"].value == 1000.0 and m["net.rx_bytes_per_s"].source == "network_namespace"
    assert m["disk.data.free_bytes"].source == "filesystem" and m["disk.data.free_bytes"].value > 0
    # Host memory and container memory are never the same metric.
    assert m["host.memory.total_bytes"].source != m["container.memory.current_bytes"].source


def test_missing_cgroup_v2_marks_only_container_values_unavailable(tmp_path):
    write_proc(tmp_path / "proc")
    m = by_metric(sampler(tmp_path).sample(1.0))
    for name in (
        "container.memory.current_bytes",
        "container.memory.limit_bytes",
        "container.cpu.cores_used",
        "container.cpu.limit_cores",
    ):
        assert (m[name].status, m[name].reason, m[name].value) == ("unavailable", "cgroup_v2_not_found", None)
    assert m["host.memory.total_bytes"].status == "ok"


def test_unreadable_files_are_read_errors_not_zero(tmp_path):
    (tmp_path / "proc").mkdir()
    write_cgroup(tmp_path / "cg")
    (tmp_path / "cg" / "memory.current").write_text("garbage\n")
    m = by_metric(sampler(tmp_path).sample(1.0))
    assert m["host.cpu.percent"].reason == "read_error" and m["host.cpu.percent"].value is None
    assert m["host.memory.total_bytes"].reason == "read_error"
    assert m["container.memory.current_bytes"].reason == "read_error"
    assert m["process.rss_bytes"].reason == "read_error"
    assert all(s.value is None for s in m.values() if s.status != "ok")


def test_non_linux_platform_reports_unavailable(tmp_path):
    s = SystemSampler(data_dir=tmp_path, platform="win32")
    m = by_metric(s.sample(1.0))
    assert m["host.cpu.percent"].reason == "not_supported_platform"
    assert m["container.memory.current_bytes"].reason == "not_supported_platform"
    assert m["disk.data.total_bytes"].status == "ok"  # filesystem stats work everywhere


def test_gpu_metrics_are_unavailable_by_default_and_pluggable(tmp_path):
    m = by_metric(SystemSampler(data_dir=tmp_path, platform="win32").sample(1.0))
    for name in NoGpuProvider.METRICS:
        assert (m[name].status, m[name].reason, m[name].source) == ("unavailable", "not_configured", "gpu")

    class FakeGpu:
        def sample(self):
            return [Sample("gpu.utilization_percent", "gpu", 33.0)]

    m = by_metric(SystemSampler(data_dir=tmp_path, platform="win32", gpu=FakeGpu()).sample(1.0))
    assert m["gpu.utilization_percent"].value == 33.0

    class BrokenGpu:
        def sample(self):
            raise RuntimeError("nvml missing")

    m = by_metric(SystemSampler(data_dir=tmp_path, platform="win32", gpu=BrokenGpu()).sample(1.0))
    assert m["gpu.utilization_percent"].reason == "read_error"


def test_counter_reset_is_not_reported_as_negative_rate(tmp_path):
    write_proc(tmp_path / "proc", rx=5000)
    s = sampler(tmp_path)
    s.sample(1.0)
    write_proc(tmp_path / "proc", rx=10)  # interface counters reset
    m = by_metric(s.sample(2.0))
    assert m["net.rx_bytes_per_s"].status == "unavailable"


def test_samples_are_persisted_with_source_and_reason(tmp_path):
    from system.monitoring.monitor import Monitor
    from tests.monitoring_support import build_settings, db_rows

    write_proc(tmp_path / "proc")
    mon = Monitor(
        build_settings(tmp_path),
        env={},
        sampler=SystemSampler(
            data_dir=tmp_path, proc_root=tmp_path / "proc", cgroup_root=tmp_path / "none", platform="linux"
        ),
    )
    mon.store.open()
    mon.sample_now()
    rows = db_rows(mon.store.path, "SELECT metric, source, status, reason, value FROM system_samples")
    mon.store.close()
    as_dict = {r[0]: r[1:] for r in rows}
    assert as_dict["container.memory.current_bytes"] == (
        "container",
        "unavailable",
        "cgroup_v2_not_found",
        None,
    )
    assert as_dict["host.load1"] == ("host", "ok", "", 0.42)
    assert as_dict["monitoring.db_bytes"][0] == "monitoring"

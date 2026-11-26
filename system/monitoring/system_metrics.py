"""Lean system metric readers (no psutil, no privileges, no Docker socket).

What an unprivileged container actually sees - and how it is labeled:

- host:      /proc/stat, /proc/meminfo, /proc/loadavg are not namespaced, so
             these are kernel-wide HOST values even inside a container.
- container: cgroup v2 files (memory.current, memory.max, cpu.stat,
             cpu.max) - the DocPipe container's own usage and limits.
- process:   /proc/self - the sampling DocPipe process only.
- filesystem: free/total bytes of the filesystems holding the monitoring
             data directory and "/".
- network_namespace: /proc/net/dev of DocPipe's network namespace - the
             host's interfaces when the container uses host networking.
- gpu:       pluggable provider; none is configured by default, so GPU
             values are reported as unavailable (reason not_configured).

A value that cannot be read is reported with status "unavailable" and a
reason - never as 0. Rate metrics need two samples; the first one is
reported as unavailable/warming_up.
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True, slots=True)
class Sample:
    metric: str
    source: str
    value: float | None
    status: str = "ok"
    reason: str = ""


def _ok(metric: str, source: str, value: float) -> Sample:
    return Sample(metric, source, float(value))


def _missing(metric: str, source: str, reason: str, status: str = "unavailable") -> Sample:
    return Sample(metric, source, None, status, reason)


class GpuProvider(Protocol):
    def sample(self) -> list[Sample]: ...


class NoGpuProvider:
    """Default: GPU metrics are not collected (would need device access)."""

    METRICS = (
        "gpu.utilization_percent",
        "gpu.memory_used_bytes",
        "gpu.memory_total_bytes",
        "gpu.temperature_celsius",
    )

    def sample(self) -> list[Sample]:
        return [_missing(m, "gpu", "not_configured") for m in self.METRICS]


HOST_METRICS = (
    "host.cpu.percent",
    "host.cpu.cores",
    "host.load1",
    "host.memory.total_bytes",
    "host.memory.available_bytes",
    "host.memory.used_percent",
)
CONTAINER_METRICS = (
    "container.memory.current_bytes",
    "container.memory.limit_bytes",
    "container.cpu.cores_used",
    "container.cpu.limit_cores",
)
PROCESS_METRICS = ("process.rss_bytes", "process.cpu.percent", "process.threads", "process.open_fds")
NET_METRICS = ("net.rx_bytes_per_s", "net.tx_bytes_per_s")


class SystemSampler:
    def __init__(
        self,
        *,
        data_dir: Path,
        proc_root: Path = Path("/proc"),
        cgroup_root: Path = Path("/sys/fs/cgroup"),
        gpu: GpuProvider | None = None,
        platform: str = sys.platform,
        clock_ticks: int | None = None,
        page_size: int | None = None,
    ) -> None:
        self._data_dir = data_dir
        self._proc = proc_root
        self._cgroup = cgroup_root
        self._gpu = gpu or NoGpuProvider()
        self._linux = platform.startswith("linux")
        self._prev: dict[str, tuple[float, float]] = {}
        sysconf = getattr(os, "sysconf", None)
        self._clock_ticks = clock_ticks or (sysconf("SC_CLK_TCK") if sysconf else None)
        self._page_size = page_size or (sysconf("SC_PAGE_SIZE") if sysconf else None)

    # --- helpers ---------------------------------------------------------------

    def _read(self, path: Path) -> str:
        return path.read_text(encoding="ascii", errors="replace")

    def _rate(self, key: str, value: float, mono: float) -> float | None:
        previous = self._prev.get(key)
        self._prev[key] = (value, mono)
        if previous is None or mono <= previous[1] or value < previous[0]:
            return None
        return (value - previous[0]) / (mono - previous[1])

    # --- readers -----------------------------------------------------------------

    def _host(self, mono: float) -> list[Sample]:
        if not self._linux:
            return [_missing(m, "host", "not_supported_platform") for m in HOST_METRICS]
        out: list[Sample] = []
        try:
            lines = self._read(self._proc / "stat").splitlines()
            fields = [float(v) for v in lines[0].split()[1:9]]
            total, idle = sum(fields), fields[3] + fields[4]
            prev = self._prev.get("host.cpu")
            self._prev["host.cpu"] = (total, idle)
            if prev is None or total <= prev[0]:
                out.append(_missing("host.cpu.percent", "host", "warming_up"))
            else:
                busy = 1 - (idle - prev[1]) / (total - prev[0])
                out.append(_ok("host.cpu.percent", "host", round(max(0.0, min(1.0, busy)) * 100, 2)))
            cores = sum(1 for line in lines if line.startswith("cpu") and line[3:4].isdigit())
            out.append(_ok("host.cpu.cores", "host", cores))
        except (OSError, ValueError, IndexError):
            out += [
                _missing("host.cpu.percent", "host", "read_error"),
                _missing("host.cpu.cores", "host", "read_error"),
            ]
        try:
            out.append(_ok("host.load1", "host", float(self._read(self._proc / "loadavg").split()[0])))
        except (OSError, ValueError, IndexError):
            out.append(_missing("host.load1", "host", "read_error"))
        try:
            info = {}
            for line in self._read(self._proc / "meminfo").splitlines():
                name, _, rest = line.partition(":")
                info[name] = float(rest.split()[0]) * 1024
            total, available = info["MemTotal"], info["MemAvailable"]
            out += [
                _ok("host.memory.total_bytes", "host", total),
                _ok("host.memory.available_bytes", "host", available),
                _ok("host.memory.used_percent", "host", round((1 - available / total) * 100, 2)),
            ]
        except (OSError, ValueError, IndexError, KeyError, ZeroDivisionError):
            out += [_missing(m, "host", "read_error") for m in HOST_METRICS[3:]]
        return out

    def _container(self, mono: float) -> list[Sample]:
        if not self._linux:
            return [_missing(m, "container", "not_supported_platform") for m in CONTAINER_METRICS]
        if not (self._cgroup / "cgroup.controllers").is_file():
            return [_missing(m, "container", "cgroup_v2_not_found") for m in CONTAINER_METRICS]
        out: list[Sample] = []
        try:
            out.append(
                _ok(
                    "container.memory.current_bytes",
                    "container",
                    int(self._read(self._cgroup / "memory.current").strip()),
                )
            )
        except (OSError, ValueError):
            out.append(_missing("container.memory.current_bytes", "container", "read_error"))
        try:
            raw = self._read(self._cgroup / "memory.max").strip()
            out.append(
                _missing("container.memory.limit_bytes", "container", "unlimited", "not_applicable")
                if raw == "max"
                else _ok("container.memory.limit_bytes", "container", int(raw))
            )
        except (OSError, ValueError):
            out.append(_missing("container.memory.limit_bytes", "container", "read_error"))
        try:
            stat = dict(line.split() for line in self._read(self._cgroup / "cpu.stat").splitlines() if line)
            cores = self._rate("container.cpu", float(stat["usage_usec"]) / 1_000_000, mono)
            out.append(
                _missing("container.cpu.cores_used", "container", "warming_up")
                if cores is None
                else _ok("container.cpu.cores_used", "container", round(cores, 4))
            )
        except (OSError, ValueError, KeyError):
            out.append(_missing("container.cpu.cores_used", "container", "read_error"))
        try:
            quota, period = self._read(self._cgroup / "cpu.max").split()[:2]
            out.append(
                _missing("container.cpu.limit_cores", "container", "unlimited", "not_applicable")
                if quota == "max"
                else _ok("container.cpu.limit_cores", "container", round(int(quota) / int(period), 4))
            )
        except (OSError, ValueError, ZeroDivisionError):
            out.append(_missing("container.cpu.limit_cores", "container", "read_error"))
        return out

    def _process(self, mono: float) -> list[Sample]:
        if not self._linux:
            return [_missing(m, "process", "not_supported_platform") for m in PROCESS_METRICS]
        out: list[Sample] = []
        self_dir = self._proc / "self"
        try:
            pages = int(self._read(self_dir / "statm").split()[1])
            out.append(_ok("process.rss_bytes", "process", pages * self._page_size))
        except (OSError, ValueError, IndexError, TypeError):
            out.append(_missing("process.rss_bytes", "process", "read_error"))
        try:
            # Fields after the "(comm)" part: utime/stime are #14/#15, threads #20.
            rest = self._read(self_dir / "stat").rsplit(")", 1)[1].split()
            cpu_seconds = (int(rest[11]) + int(rest[12])) / self._clock_ticks
            rate = self._rate("process.cpu", cpu_seconds, mono)
            out.append(
                _missing("process.cpu.percent", "process", "warming_up")
                if rate is None
                else _ok("process.cpu.percent", "process", round(rate * 100, 2))
            )
            out.append(_ok("process.threads", "process", int(rest[17])))
        except (OSError, ValueError, IndexError, TypeError):
            out += [
                _missing("process.cpu.percent", "process", "read_error"),
                _missing("process.threads", "process", "read_error"),
            ]
        try:
            out.append(_ok("process.open_fds", "process", len(os.listdir(self_dir / "fd"))))
        except OSError:
            out.append(_missing("process.open_fds", "process", "read_error"))
        return out

    def _filesystems(self) -> list[Sample]:
        out: list[Sample] = []
        for label, path in (("data", self._data_dir), ("root", Path(os.path.abspath(os.sep)))):
            try:
                usage = shutil.disk_usage(path)
                out += [
                    _ok(f"disk.{label}.total_bytes", "filesystem", usage.total),
                    _ok(f"disk.{label}.free_bytes", "filesystem", usage.free),
                ]
            except OSError:
                out += [
                    _missing(f"disk.{label}.total_bytes", "filesystem", "read_error"),
                    _missing(f"disk.{label}.free_bytes", "filesystem", "read_error"),
                ]
        return out

    def _network(self, mono: float) -> list[Sample]:
        if not self._linux:
            return [_missing(m, "network_namespace", "not_supported_platform") for m in NET_METRICS]
        try:
            rx = tx = 0
            for line in self._read(self._proc / "net" / "dev").splitlines()[2:]:
                name, _, data = line.partition(":")
                if name.strip() == "lo":
                    continue
                values = data.split()
                rx += int(values[0])
                tx += int(values[8])
        except (OSError, ValueError, IndexError):
            return [_missing(m, "network_namespace", "read_error") for m in NET_METRICS]
        out = []
        for metric, value in (("net.rx_bytes_per_s", rx), ("net.tx_bytes_per_s", tx)):
            rate = self._rate(metric, value, mono)
            out.append(
                _missing(metric, "network_namespace", "warming_up")
                if rate is None
                else _ok(metric, "network_namespace", round(rate, 1))
            )
        return out

    def sample(self, mono: float) -> list[Sample]:
        samples = [
            *self._host(mono),
            *self._container(mono),
            *self._process(mono),
            *self._filesystems(),
            *self._network(mono),
        ]
        try:
            samples += self._gpu.sample()
        except Exception:
            samples += [_missing(m, "gpu", "read_error") for m in NoGpuProvider.METRICS]
        return samples

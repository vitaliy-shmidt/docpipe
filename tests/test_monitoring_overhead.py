"""Resource use of the monitoring foundation stays small and bounded (Phase M2).

Thresholds are deliberately generous (CI machines vary); the documented
measurements come from system/tools/monitoring_overhead.py.
"""

from __future__ import annotations

import tracemalloc

from system.monitoring.recorder import FinishedRequest, Recorder
from system.tools.monitoring_overhead import micro


def test_request_path_overhead_is_far_below_a_millisecond(tmp_path):
    assert micro(5000, tmp_path) < 500  # microseconds per request (typically ~10-30)


def test_buffer_memory_is_bounded_by_its_limit():
    recorder = Recorder(max_requests=10_000, max_events=1000)
    record = dict(
        ts_ms=0,
        duration_ms=1.0,
        client_id="c",
        route="/x",
        method="GET",
        service="system",
        status_code=200,
        error_code=None,
        bytes_in=0,
        bytes_out=0,
        documents=0,
        pages=None,
        ai_provider=None,
        ai_model=None,
        ai_calls=0,
        tokens_in=None,
        tokens_out=None,
        steps_truncated=0,
    )
    tracemalloc.start()
    for i in range(50_000):  # five times the limit
        recorder.finish(FinishedRequest(request_id=f"{i:08d}", **record))
    current, _ = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert recorder.pending()["requests_buffered"] == 10_000
    assert recorder.totals["requests_dropped_buffer"] == 40_000
    assert current < 15 * 1024 * 1024

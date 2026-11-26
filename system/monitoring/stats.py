"""Latency histograms and percentiles.

Two methods, never mixed silently:

- exact: nearest-rank percentile over raw durations (used while the raw
  request rows of the whole range are still retained);
- histogram: estimate from fixed, mergeable latency buckets stored in the
  rollups. The estimate always lies inside the bucket that holds the exact
  value (linear interpolation within the bucket, clamped to the observed
  min/max), so its error is bounded by that bucket's width. Callers must
  label such values as approximate.
"""

from __future__ import annotations

import bisect
import math
from collections.abc import Sequence

# Upper bounds (ms, inclusive) of the latency buckets; a final bucket
# catches everything above the last bound. Changing these requires a
# schema migration (bucket columns h00..h20).
HISTOGRAM_BOUNDS_MS: tuple[float, ...] = (
    1,
    2,
    5,
    10,
    20,
    50,
    100,
    200,
    500,
    1_000,
    2_000,
    5_000,
    10_000,
    20_000,
    30_000,
    60_000,
    120_000,
    180_000,
    300_000,
    600_000,
)
BUCKET_COUNT = len(HISTOGRAM_BOUNDS_MS) + 1
BUCKET_COLUMNS = tuple(f"h{i:02d}" for i in range(BUCKET_COUNT))


def bucket_index(duration_ms: float) -> int:
    return bisect.bisect_left(HISTOGRAM_BOUNDS_MS, duration_ms)


def bucket_range(index: int) -> tuple[float, float]:
    lower = 0.0 if index == 0 else float(HISTOGRAM_BOUNDS_MS[index - 1])
    upper = float(HISTOGRAM_BOUNDS_MS[index]) if index < len(HISTOGRAM_BOUNDS_MS) else math.inf
    return lower, upper


def exact_percentile(sorted_values: Sequence[float], p: float) -> float | None:
    """Nearest-rank percentile of an ascending sequence (p in (0, 1])."""
    n = len(sorted_values)
    if n == 0:
        return None
    rank = max(1, math.ceil(p * n))
    return float(sorted_values[rank - 1])


def histogram_percentile(
    counts: Sequence[int], p: float, observed_min: float | None, observed_max: float | None
) -> float | None:
    total = sum(counts)
    if total == 0:
        return None
    rank = max(1, math.ceil(p * total))
    cumulative = 0
    for index, count in enumerate(counts):
        if count and cumulative + count >= rank:
            lower, upper = bucket_range(index)
            if observed_min is not None:
                lower = max(lower, observed_min)
            if observed_max is not None:
                upper = min(upper, observed_max)
            if upper < lower:
                upper = lower
            fraction = (rank - cumulative) / count
            return lower + (upper - lower) * fraction
        cumulative += count
    return observed_max


PERCENTILES = {"p50": 0.50, "p95": 0.95, "p99": 0.99}

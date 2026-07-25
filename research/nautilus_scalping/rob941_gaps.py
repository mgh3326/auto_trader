"""ROB-941 (AC7) — gap tracking: never delete/forward-fill/synthesize, only record.

``detect_gap_ranges`` finds every missing 60s bucket in a symbol's window and
returns it as an explicit ``(start_ms, end_ms)`` range — the caller must treat
the range as absent data, never fabricate a value for it. ``incomplete_buckets``
marks any 5m/15m aggregate a gap touches as incomplete (never synthesized from
partial 1m coverage). ``position_touches_gap`` is the pure predicate a trial-
rejection rule (``rejected:data_gap_in_position``) consumes; this module does
not itself reject trials.
"""

from __future__ import annotations

GRID_MS = 60_000


def detect_gap_ranges(
    open_time_ms_observed: list[int], window_start_ms: int, window_end_ms: int
) -> list[tuple[int, int]]:
    """Sorted, merged ``[start_ms, end_ms)`` ranges of missing 60s buckets in
    ``[window_start_ms, window_end_ms)``. Never fills/interpolates — the only
    output is the hole's boundaries."""
    observed = set(open_time_ms_observed)
    ranges: list[tuple[int, int]] = []
    t = window_start_ms
    while t < window_end_ms:
        if t not in observed:
            if ranges and ranges[-1][1] == t:
                ranges[-1] = (ranges[-1][0], t + GRID_MS)
            else:
                ranges.append((t, t + GRID_MS))
        t += GRID_MS
    return ranges


def incomplete_buckets(
    open_time_ms_observed: list[int],
    window_start_ms: int,
    window_end_ms: int,
    bucket_seconds: int,
) -> list[int]:
    """``bucket_start_ms`` values (5m: 300, 15m: 900) NOT fully covered by 1m
    bars — every such bucket is incomplete and must never be synthesized from
    partial coverage."""
    bucket_ms = bucket_seconds * 1000
    if bucket_ms % GRID_MS != 0:
        raise ValueError(
            f"bucket_seconds={bucket_seconds} must be a whole multiple of 60s"
        )
    slots_per_bucket = bucket_ms // GRID_MS
    observed = set(open_time_ms_observed)
    out: list[int] = []
    bucket_start = window_start_ms
    while bucket_start < window_end_ms:
        complete = all(
            (bucket_start + i * GRID_MS) in observed for i in range(slots_per_bucket)
        )
        if not complete:
            out.append(bucket_start)
        bucket_start += bucket_ms
    return out


def position_touches_gap(
    entry_ms: int, exit_ms: int, gap_ranges: list[tuple[int, int]]
) -> bool:
    """True iff ``[entry_ms, exit_ms)`` overlaps ANY gap range — the pure
    predicate behind ``rejected:data_gap_in_position``."""
    return any(entry_ms < g1 and g0 < exit_ms for g0, g1 in gap_ranges)

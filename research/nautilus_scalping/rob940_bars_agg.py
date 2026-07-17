"""ROB-942 (H2, ROB-940) — complete-only 1m -> 5m/15m OHLCV aggregation (pure, stdlib).

AC2: 5m/15m OHLCV buckets are emitted ONLY when every one of their constituent
1m bars is present, contiguous (exactly 60s apart), and aligned to the UTC
bucket grid (``bucket_start = floor(ts / bucket_ms) * bucket_ms``). A single
missing/gapped 1m bar anywhere in a bucket's span means that bucket is never
emitted (no partial/bridged buckets, no leading/trailing partials).

A gap in the underlying 1m stream also resets "warm-up": the FIRST bucket that
completes after a gap is marked ``is_segment_start=True`` so any downstream
indicator state (H3) knows it must restart rather than treat history across
the gap as continuous. This module has no opinion on what "warm-up" means for
any particular indicator — it only exposes the discontinuity boundary.

No DB/network/app/broker imports — pure stdlib, deterministic given its input.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

_MS_PER_MINUTE = 60_000


@dataclass(frozen=True)
class Bar1m:
    ts: int  # open_time, epoch ms UTC, minute-aligned
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class AggregatedBar:
    ts: int  # bucket open_time (start), epoch ms UTC
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_ts: int  # bucket close boundary == ts of the immediately-following 1m bar
    is_segment_start: bool  # True iff this is the first complete bucket after a gap


def _validate_sorted_1m(bars_1m: Sequence[Bar1m]) -> None:
    for i in range(1, len(bars_1m)):
        if bars_1m[i].ts <= bars_1m[i - 1].ts:
            raise ValueError(
                "bars_1m must be strictly increasing by ts; got "
                f"{bars_1m[i - 1].ts} then {bars_1m[i].ts}"
            )


def _split_contiguous_segments(bars_1m: Sequence[Bar1m]) -> list[list[Bar1m]]:
    """Split into maximal runs of exactly-60s-spaced 1m bars."""
    if not bars_1m:
        return []
    segments: list[list[Bar1m]] = [[bars_1m[0]]]
    for prev, cur in zip(bars_1m, bars_1m[1:], strict=False):
        if cur.ts == prev.ts + _MS_PER_MINUTE:
            segments[-1].append(cur)
        else:
            segments.append([cur])
    return segments


def aggregate_complete(
    bars_1m: Sequence[Bar1m], bucket_minutes: int
) -> list[AggregatedBar]:
    """Aggregate 1m bars into complete, UTC-grid-aligned ``bucket_minutes`` buckets.

    Only fully-covered buckets are emitted. Raises ``ValueError`` if
    ``bars_1m`` is not strictly increasing by ``ts`` or ``bucket_minutes`` is
    not positive.
    """
    if bucket_minutes <= 0:
        raise ValueError("bucket_minutes must be positive")
    _validate_sorted_1m(bars_1m)
    bucket_ms = bucket_minutes * _MS_PER_MINUTE
    out: list[AggregatedBar] = []
    for segment in _split_contiguous_segments(bars_1m):
        by_ts = {b.ts: b for b in segment}
        seen_starts: set[int] = set()
        first_in_segment = True
        for b in segment:
            start = (b.ts // bucket_ms) * bucket_ms
            if start in seen_starts:
                continue
            seen_starts.add(start)
            offsets = [start + k * _MS_PER_MINUTE for k in range(bucket_minutes)]
            if not all(o in by_ts for o in offsets):
                continue
            source = [by_ts[o] for o in offsets]
            out.append(
                AggregatedBar(
                    ts=start,
                    open=source[0].open,
                    high=max(x.high for x in source),
                    low=min(x.low for x in source),
                    close=source[-1].close,
                    volume=sum(x.volume for x in source),
                    close_ts=start + bucket_ms,
                    is_segment_start=first_in_segment,
                )
            )
            first_in_segment = False
    return out

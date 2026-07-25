"""ROB-979 (H2, ROB-974 R2) CP1 -- narrow H1 semantic ingress (pure, stdlib).

Normalizes DUCK-TYPED H1-shaped input into the H2-owned immutable DTOs from
``rob974_h2_dtos``. Every ``normalize_*`` function reads its input via
``getattr`` ONLY -- never ``isinstance`` against a concrete H1 class -- so CP1
(test-only frozen H1 fixture) and CP6 (real merged ROB-978 output) share the
exact same ingress code path without H2 ever depending on H1's concrete type
identity.

``resolve_entry_minute`` is the ROB-979 AC3 authority shared by both the S3
and S4 engines: entry is only the EXACT contiguous next minute
(``1m.open_time == signal_ts``), filled at that open. A missing exact tick
returns ``None`` (-> ``next_tick_unavailable``/NO_TRADE) and this function
never searches forward past a gap -- that would be a different, unspecified
"delayed entry" policy. ``build_minute_index`` is keyed ``(symbol, open_time)``
so two different symbols sharing an ``open_time`` can never collide, and a
duplicate ``(symbol, open_time)`` in the input is a terminal ``ValueError``
(schema-corrupt input, not ordinary missing data -- mirrors the H1 authority's
"duplicate/reversed/conflicting rows are terminal invalid input" rule, applied
defensively here since H2 must not silently pick one of two conflicting bars).

No DB/network/app/broker/order/fill/scheduler/random/current-time imports --
pure stdlib, deterministic given its input.
"""

from __future__ import annotations

from collections.abc import Sequence

from rob974_h2_dtos import MinuteBar, S3CloseFeature, S4PairLegClose

MinuteIndex = dict[tuple[str, int], MinuteBar]


def normalize_minute_bar(raw: object) -> MinuteBar:
    return MinuteBar(
        symbol=raw.symbol,
        open_time=raw.open_time,
        open=raw.open,
        high=raw.high,
        low=raw.low,
        close=raw.close,
    )


def normalize_s3_close_feature(raw: object) -> S3CloseFeature:
    return S3CloseFeature(
        symbol=raw.symbol,
        close_ts=raw.close_ts,
        close=raw.close,
        vwap24=raw.VWAP24,
        m=raw.M,
    )


def normalize_s4_pair_leg_close(raw: object) -> S4PairLegClose:
    return S4PairLegClose(symbol=raw.symbol, close_ts=raw.close_ts, close=raw.close)


def build_minute_index(bars: Sequence[MinuteBar]) -> MinuteIndex:
    index: MinuteIndex = {}
    for bar in bars:
        key = (bar.symbol, bar.open_time)
        if key in index:
            raise ValueError(
                f"duplicate minute bar for symbol={bar.symbol!r} "
                f"open_time={bar.open_time} -- terminal schema-corrupt input"
            )
        index[key] = bar
    return index


def resolve_entry_minute(
    index: MinuteIndex, symbol: str, signal_ts: int
) -> MinuteBar | None:
    """The 1m bar at ``open_time == signal_ts`` for ``symbol``, or ``None``.

    ``signal_ts`` IS the exact required ``open_time`` -- for a contiguous 1m
    grid this equals the signal bar's completed close, matching the frozen
    ROB-940 ``resolve_entry`` convention this module mirrors (composed fresh
    here, not imported, since the H1/H2 fixture shape differs: ``open_time``
    keyed by ``(symbol, ts)`` for account-global/multi-symbol lookup rather
    than a single per-symbol bar list).
    """
    return index.get((symbol, signal_ts))

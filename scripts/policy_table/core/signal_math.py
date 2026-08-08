"""Per-symbol support/resistance/RSI computation — direct D3-engine reuse.

This mirrors ``research.kr_corpus.d3_engine.engine.Engine._signal_for_session``
call-for-call (fib window scan, Wilder RSI, Bollinger bands, 1%-tolerance
confluence clustering, nearest-qualifying-support selection) so the numbers
in the policy table are produced by the *same* 3-series-verified D3 code,
not a reimplementation. The only addition here is a synthetic placeholder
bar appended to the point series so a *forward-looking* decision index
(one past the last real bar — "the next session hasn't happened yet") can
be passed to ``scan_fib_window`` without duplicating its bounds-checking
logic. The placeholder's OHLC values are never read: ``scan_fib_window``
only uses it to validate ``decision_index < len(points)`` and to mark
``excluded_index`` bookkeeping; the 120-bar window itself is sliced strictly
before it.

D3 modules imported here (unmodified): ``indicators.py``, ``signals.py``,
``tick.py``. Not reimplemented: RSI/Wilder, Bollinger bands, fib window
scan, fib level math, 1% confluence clustering, qualifying-support
selection, or the sell-target minus-one-tick rule.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from research.kr_corpus.d3_engine.constants import (
    CONFLUENCE_TOLERANCE,
    RSI_THRESHOLD,
    SUPPORT_MAX_DISTANCE,
    SUPPORT_MIN_DISTANCE,
)
from research.kr_corpus.d3_engine.indicators import (
    OhlcPoint,
    bollinger_bands,
    fib_levels,
    fib_resistance_above_close,
    rsi_wilder,
    scan_fib_window,
)
from research.kr_corpus.d3_engine.signals import (
    LevelCluster,
    PriceLevel,
    choose_l2,
    cluster_levels,
    support_distance,
)
from research.kr_corpus.d3_engine.tick import TickTable

FIB_WINDOW = 120
RSI_PERIOD = 14  # D3 engine.py calls rsi_wilder(previous_closes) — default period=14.
BB_WINDOW = 20
BB_SIGMA = Decimal("2")

D3_CONSTANTS_ECHO: dict[str, str] = {
    "fib_window": str(FIB_WINDOW),
    "rsi_period": str(RSI_PERIOD),
    "rsi_threshold": str(RSI_THRESHOLD),
    "bb_window": str(BB_WINDOW),
    "bb_sigma": str(BB_SIGMA),
    "support_min_distance": str(SUPPORT_MIN_DISTANCE),
    "support_max_distance": str(SUPPORT_MAX_DISTANCE),
    "confluence_tolerance": str(CONFLUENCE_TOLERANCE),
}


@dataclass(frozen=True, slots=True)
class ClusterView:
    representative: Decimal
    distance_pct: Decimal
    sources: tuple[str, ...]
    source_count: int
    qualifies_two_source: bool
    within_d3_support_window: bool


@dataclass(frozen=True, slots=True)
class SymbolSignal:
    previous_close: Decimal
    rsi: Decimal | None
    bb_lower: Decimal
    bb_upper: Decimal
    bb_middle: Decimal
    fib_window_low: Decimal
    fib_window_high: Decimal
    support_clusters: tuple[ClusterView, ...]
    resistance_clusters_above_close: tuple[ClusterView, ...]
    buy_l1: Decimal
    buy_l2: Decimal | None
    buy_l2_source: str | None
    sell_r1: Decimal | None
    sell_r2: Decimal | None


class InsufficientHistory(ValueError):
    """Fewer than FIB_WINDOW real bars supplied — cannot scan the fib window."""


def _cluster_view(cluster: LevelCluster, *, close: Decimal) -> ClusterView:
    distance = support_distance(cluster.representative, close)
    return ClusterView(
        representative=cluster.representative,
        distance_pct=distance,
        sources=cluster.distinct_sources,
        source_count=len(cluster.distinct_sources),
        qualifies_two_source=cluster.qualifies,
        within_d3_support_window=(
            SUPPORT_MIN_DISTANCE <= distance <= SUPPORT_MAX_DISTANCE
        ),
    )


def compute_symbol_signal(
    *,
    closes: Sequence[Decimal],
    highs: Sequence[Decimal],
    lows: Sequence[Decimal],
    tick_table: TickTable,
) -> SymbolSignal:
    """Compute buy/sell reference levels for the bar *after* the last supplied one.

    ``closes``/``highs``/``lows`` must be same-length, ascending by time,
    ending at the most recently *closed* bar (i.e. "t-1" from the
    perspective of the next, not-yet-closed session).
    """

    if not (len(closes) == len(highs) == len(lows)):
        raise ValueError("closes/highs/lows length mismatch")
    n = len(closes)
    if n < FIB_WINDOW:
        raise InsufficientHistory(f"need >= {FIB_WINDOW} closed bars, got {n}")

    points = [
        OhlcPoint(high=highs[i], low=lows[i], close=closes[i]) for i in range(n)
    ]
    points.append(points[-1])  # synthetic "t" placeholder — values never read
    decision_index = n

    window = scan_fib_window(points, decision_index=decision_index)
    previous_closes = list(closes)
    rsi_series = rsi_wilder(previous_closes, period=RSI_PERIOD)
    rsi = rsi_series[-1]
    if rsi is not None:
        rsi = rsi.quantize(Decimal("0.0001"))
    bands = bollinger_bands(previous_closes, window=BB_WINDOW, sigma=BB_SIGMA)
    previous_close = previous_closes[-1]

    support_levels = [
        PriceLevel(price, "fib_family", f"fib_{ratio}")
        for ratio, price in fib_levels(window.low, window.high).items()
    ]
    support_levels.append(PriceLevel(bands.lower, "bb_lower", "bb_lower"))
    support_clusters = cluster_levels(support_levels, close=previous_close)

    resistance_levels = [
        PriceLevel(price, "fib_resistance_family", f"fib_r_{ratio}")
        for ratio, price in fib_resistance_above_close(
            window.low, window.high, previous_close
        ).items()
    ]
    resistance_levels.append(PriceLevel(bands.upper, "bb_upper", "bb_upper"))
    resistance_clusters = cluster_levels(resistance_levels, close=previous_close)
    resistance_above = sorted(
        (c for c in resistance_clusters if c.representative > previous_close),
        key=lambda c: c.representative,
    )[:2]

    l2 = choose_l2(support_clusters, close=previous_close)
    buy_l1 = tick_table.align_buy(previous_close * Decimal("0.97"))
    buy_l2 = tick_table.align_buy(l2.representative) if l2 is not None else None

    sell_targets = [tick_table.sell_limit(c.representative) for c in resistance_above]
    sell_r1 = sell_targets[0] if len(sell_targets) > 0 else None
    sell_r2 = sell_targets[1] if len(sell_targets) > 1 else None

    return SymbolSignal(
        previous_close=previous_close,
        rsi=rsi,
        bb_lower=bands.lower,
        bb_upper=bands.upper,
        bb_middle=bands.middle,
        fib_window_low=window.low,
        fib_window_high=window.high,
        support_clusters=tuple(
            _cluster_view(c, close=previous_close) for c in support_clusters
        ),
        resistance_clusters_above_close=tuple(
            _cluster_view(c, close=previous_close) for c in resistance_above
        ),
        buy_l1=buy_l1,
        buy_l2=buy_l2,
        buy_l2_source="nearest_qualifying_support_cluster" if l2 is not None else None,
        sell_r1=sell_r1,
        sell_r2=sell_r2,
    )


__all__ = [
    "SymbolSignal",
    "ClusterView",
    "InsufficientHistory",
    "compute_symbol_signal",
    "D3_CONSTANTS_ECHO",
    "FIB_WINDOW",
]

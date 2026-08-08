"""KRX tick-size table, expressed as a D3-engine ``TickTable`` (reuse).

ROB-1230 P-2 design doc is explicit: the KR adapter must use the *runtime*
KRX tick rule (``app.mcp_server.tick_size.get_tick_size_kr`` — the same
function the live order path uses), not the D3 frozen sample copy
(``research/kr_corpus/d3_engine`` loads a hash-gated, point-in-time YAML
snapshot for backtest reproducibility; an advisory table must instead track
whatever the current order path actually enforces).

Band boundaries are discovered by binary search directly against
``get_tick_size_kr`` rather than hand-typed, so a future change to the
runtime ladder is picked up automatically instead of silently drifting from
this table. Only the D3 ``TickTable`` container (bands/alignment/
sell-minus-one-tick) is reused from ``research.kr_corpus.d3_engine.tick`` —
the ladder data itself comes from the live function, not from that module.
"""

from __future__ import annotations

from app.mcp_server.tick_size import get_tick_size_kr
from research.kr_corpus.d3_engine.tick import TickTable

# KRX equities do not trade above this (the priciest KOSPI names sit in the
# low millions of KRW); scanning up to here also proves the ladder is flat
# (tick=1000) well past the last real breakpoint (500,000).
_SCAN_UPPER_BOUND = 100_000_000


def _find_next_transition(
    *, lower: int, tick_at_lower: int, upper_bound: int
) -> int | None:
    """Binary search for the first price >= lower where the tick differs.

    Returns ``None`` if the tick is unchanged all the way to ``upper_bound``
    (i.e. ``lower`` is the start of the final, open-ended band).
    """

    if get_tick_size_kr(float(upper_bound)) == tick_at_lower:
        return None
    lo, hi = lower, upper_bound
    while lo < hi:
        mid = (lo + hi) // 2
        if get_tick_size_kr(float(mid)) == tick_at_lower:
            lo = mid + 1
        else:
            hi = mid
    return lo


def build_kr_krx_tick_table() -> TickTable:
    """Build a D3 ``TickTable`` by discovering ``get_tick_size_kr``'s ladder."""

    bands: list[dict[str, str]] = []
    lower = 0
    while True:
        tick_at_lower = get_tick_size_kr(float(lower))
        transition = _find_next_transition(
            lower=lower, tick_at_lower=tick_at_lower, upper_bound=_SCAN_UPPER_BOUND
        )
        bands.append(
            {
                "lower_inclusive": str(lower),
                "upper_exclusive": None if transition is None else str(transition),
                "tick": str(tick_at_lower),
            }
        )
        if transition is None:
            break
        lower = transition
    return TickTable.from_mapping({"bands": bands})


__all__ = ["build_kr_krx_tick_table"]

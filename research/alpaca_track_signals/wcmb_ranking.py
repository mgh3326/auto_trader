"""ROB-1061 H3 (Run A SS12.2-SS12.3) — the AP-A2 WCM-B pure ranking
primitives: descending-Score ranking with a symbol-ascending tie-break
(AC14), and the per-held-symbol exit/hold boundary (AC19: ``rank == k+b``
holds, ``rank == k+b+1`` exits).

Kept separate from ``wcmb_engine`` (which wires this together with
sizing/indicators/cash allocation into the full six-step decision) the same
way ``dats_state`` is kept separate from ``dats_engine`` — a small, pure,
directly boundary-testable core.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

__all__ = [
    "classify_held_symbol",
    "rank_symbols",
]


def rank_symbols(scored: Mapping[str, float]) -> dict[str, int]:
    """Rank 1..N, Score DESCENDING, ties broken by symbol ASCENDING (AC14:
    "내림차순 정렬, 동점은 symbol 사전순") — never insertion order, never any
    other tie-break."""
    ordered = sorted(scored.keys(), key=lambda symbol: (-scored[symbol], symbol))
    return {symbol: i + 1 for i, symbol in enumerate(ordered)}


def classify_held_symbol(
    *, score: float, rank: int, k: int, b: int
) -> Literal["EXIT", "HOLD"]:
    """SS12.3 step (1): a HELD symbol is queued for exit iff
    ``score <= 0 OR rank > k + b`` (AC19: ``rank == k+b`` still holds,
    ``rank == k+b+1`` exits); otherwise it holds with no trade."""
    if score <= 0.0 or rank > k + b:
        return "EXIT"
    return "HOLD"

"""ROB-405 Slice B — deterministic auto-verdict from pnl_pct."""

from __future__ import annotations

from decimal import Decimal

GOOD_PNL_PCT = Decimal("1.0")
BAD_PNL_PCT = Decimal("-1.0")


def classify_journal_verdict(pnl_pct: Decimal | None) -> str:
    """good if pnl_pct >= +1.0%, bad if <= -1.0%, else neutral (None → neutral)."""
    if pnl_pct is None:
        return "neutral"
    if pnl_pct >= GOOD_PNL_PCT:
        return "good"
    if pnl_pct <= BAD_PNL_PCT:
        return "bad"
    return "neutral"

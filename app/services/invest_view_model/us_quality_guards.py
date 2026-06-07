"""ROB-440: US fundamentals/valuation quality guards (shared across loaders).

Once the labels were corrected (#1154 dividend ×100, #1155 market_cap currency),
yahoo .info ratios for micro-caps and bad data points surfaced at the TOP of the
US rankings: DCX ROE 1177% on a $48M cap, NVO dividend 26.74% from a bad
trailingAnnualDividendYield. KR uses tvscreener (cleaner) so these apply to US only.

The three US loaders that build a market_valuation_snapshots candidate query
(load_fundamentals_preset_from_snapshots, load_high_yield_value_from_snapshots,
load_undervalued_breakout_from_snapshots) share this helper so the thresholds stay
in one place. Callers gate on ``market == "us"``.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import sqlalchemy as sa

from app.models.market_valuation_snapshot import MarketValuationSnapshot

# Conservative thresholds (minimise false-positives); tunable.
US_MIN_MARKET_CAP_USD = Decimal("100000000")  # $100M — drop nano-caps (DCX $48M)
US_MAX_ROE_PERCENT = Decimal("300")  # drop egregious ROE artifacts (keep real high ROE)
US_MAX_DIVIDEND_RATIO = Decimal("0.25")  # 25% — drop bad dividend data (real ≤ ~20%)


def apply_us_valuation_quality_guards(
    stmt: Any, *, uses_roe: bool = False, uses_dividend: bool = False
) -> Any:
    """Add US quality guards to a market_valuation candidate SELECT.

    - market_cap size floor (always): requires a value ≥ floor (drop nano-caps).
    - ROE sanity cap (when the preset filters/sorts on ROE): NULL passes
      (missing ≠ anomalous).
    - dividend sanity cap (when the preset filters/sorts on dividend): NULL passes.

    Returns the augmented statement. Caller must gate on ``market == "us"``.
    """
    stmt = stmt.where(
        MarketValuationSnapshot.market_cap.is_not(None),
        MarketValuationSnapshot.market_cap >= US_MIN_MARKET_CAP_USD,
    )
    if uses_roe:
        stmt = stmt.where(
            sa.or_(
                MarketValuationSnapshot.roe.is_(None),
                MarketValuationSnapshot.roe <= US_MAX_ROE_PERCENT,
            )
        )
    if uses_dividend:
        stmt = stmt.where(
            sa.or_(
                MarketValuationSnapshot.dividend_yield.is_(None),
                MarketValuationSnapshot.dividend_yield <= US_MAX_DIVIDEND_RATIO,
            )
        )
    return stmt

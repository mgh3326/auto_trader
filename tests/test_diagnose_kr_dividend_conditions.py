"""ROB-444: guard the dividend binding-condition diagnostic mirrors the loader."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from app.services.financial_fundamentals_snapshots.derive import MetricResult
from app.services.invest_view_model.fundamentals_screener import STEADY_DIVIDEND_SPEC
from scripts.diagnose_kr_dividend_conditions import _conditions


def _snap(dividend_yield):
    return SimpleNamespace(
        dividend_yield=dividend_yield,
        payout_ratio_ttm=None,
        continuous_dividend_payout=None,
        continuous_dividend_growth=None,
    )


def _dart():
    return SimpleNamespace(
        payout_ratio=MetricResult(value=Decimal("40"), state="ok"),
        dividend_paid_streak_years=MetricResult(value=Decimal("5"), state="ok"),
        earnings_increase_streak_years=MetricResult(value=Decimal("4"), state="ok"),
    )


def test_conditions_all_pass_with_dart_and_high_yield():
    conds = dict(_conditions(STEADY_DIVIDEND_SPEC, _snap(Decimal("6.0")), _dart()))
    assert all(conds.values()), conds  # 6% yield + DART payout/streak → all pass


def test_conditions_dividend_yield_fails_sub_threshold_percent():
    # PR1: 0.48% (percent) → ÷100 = 0.0048 < 0.03 → dividend_yield condition fails
    conds = _conditions(STEADY_DIVIDEND_SPEC, _snap(Decimal("0.48")), _dart())
    dy = [ok for name, ok in conds if name.startswith("dividend_yield")][0]
    assert dy is False


def test_conditions_payout_fails_when_no_dart_and_null_tvscreener():
    conds = _conditions(STEADY_DIVIDEND_SPEC, _snap(Decimal("6.0")), None)
    payout = [ok for name, ok in conds if name.startswith("payout_ratio")][0]
    assert payout is False  # DART absent + tvscreener null → fail-closed

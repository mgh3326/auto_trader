"""ROB-462: 체결강도 (execution strength) computation from KIS FHKST01010100 cttr."""

from __future__ import annotations

import pytest

from app.services.execution_strength.query_service import (
    compute_execution_strength,
)


def test_buy_dominant_when_cttr_above_100():
    raw = {"cttr": "135.5", "shnu_cntg_qty": "1200", "seln_cntg_qty": "800"}
    data = compute_execution_strength(
        raw, symbol="005930", as_of="2026-06-09T10:00:00+09:00"
    )
    assert data.symbol == "005930"
    assert data.execution_strength_pct == pytest.approx(135.5)
    assert data.buy_volume == pytest.approx(1200.0)
    assert data.sell_volume == pytest.approx(800.0)
    assert data.trend == "buy_dominant"


def test_sell_dominant_when_cttr_below_100():
    data = compute_execution_strength({"cttr": "72.0"}, symbol="005930", as_of=None)
    assert data.execution_strength_pct == pytest.approx(72.0)
    assert data.trend == "sell_dominant"
    # missing buy/sell fields stay None — never fabricated 0.
    assert data.buy_volume is None
    assert data.sell_volume is None


def test_neutral_at_exactly_100():
    data = compute_execution_strength({"cttr": "100"}, symbol="x", as_of=None)
    assert data.trend == "neutral"


def test_missing_cttr_returns_none_not_fabricated():
    data = compute_execution_strength({}, symbol="005930", as_of=None)
    assert data.execution_strength_pct is None
    assert data.trend is None

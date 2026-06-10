"""ROB-485: 체결강도 computation from KIS inquire-ccnl tday_rltv."""

from __future__ import annotations

import pytest

from app.services.execution_strength.query_service import (
    compute_execution_strength,
)


def test_buy_dominant_when_tday_rltv_above_100():
    raw = {"tday_rltv": "135.5"}
    data = compute_execution_strength(
        raw, symbol="005930", as_of="2026-06-09T10:00:00+09:00"
    )
    assert data.symbol == "005930"
    assert data.execution_strength_pct == pytest.approx(135.5)
    assert data.buy_volume is None
    assert data.sell_volume is None
    assert data.trend == "buy_dominant"


def test_sell_dominant_when_tday_rltv_below_100():
    data = compute_execution_strength(
        {"tday_rltv": "72.0"}, symbol="005930", as_of=None
    )
    assert data.execution_strength_pct == pytest.approx(72.0)
    assert data.trend == "sell_dominant"
    assert data.buy_volume is None
    assert data.sell_volume is None


def test_neutral_at_exactly_100():
    data = compute_execution_strength({"tday_rltv": "100"}, symbol="x", as_of=None)
    assert data.trend == "neutral"


def test_missing_tday_rltv_returns_none_not_fabricated():
    data = compute_execution_strength({}, symbol="005930", as_of=None)
    assert data.execution_strength_pct is None
    assert data.trend is None


def test_legacy_cttr_field_is_not_treated_as_rest_execution_strength():
    data = compute_execution_strength({"cttr": "135.5"}, symbol="005930", as_of=None)
    assert data.execution_strength_pct is None
    assert data.trend is None

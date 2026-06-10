"""ROB-485: 체결강도 computation from KIS FHKST01010300 (inquire-ccnl) tday_rltv."""

from __future__ import annotations

import pytest

from app.services.execution_strength.query_service import (
    compute_execution_strength,
)


def test_buy_dominant_when_tday_rltv_above_100():
    raw = {
        "tday_rltv": "135.5",
        "stck_cntg_hour": "100000",
        "stck_prpr": "80000",
        "acml_vol": None,
    }
    data = compute_execution_strength(
        raw, symbol="005930", as_of="2026-06-10T10:00:00+09:00"
    )
    assert data.symbol == "005930"
    assert data.execution_strength_pct == pytest.approx(135.5)
    assert data.tick_time == "100000"
    assert data.trend == "buy_dominant"
    # KIS REST 에 per-side 체결량 없음 — 항상 None (0 날조 금지).
    assert data.buy_volume is None
    assert data.sell_volume is None


def test_sell_dominant_with_live_probe_values():
    # 2026-06-10 09:42 KST 라이브 프로브 실측 row (012450, FHKST01010300).
    raw = {
        "stck_cntg_hour": "094227",
        "stck_prpr": "1031000",
        "prdy_vrss": "15000",
        "prdy_vrss_sign": "2",
        "cntg_vol": "1",
        "tday_rltv": "81.82",
        "prdy_ctrt": "1.48",
    }
    data = compute_execution_strength(raw, symbol="012450", as_of=None)
    assert data.execution_strength_pct == pytest.approx(81.82)
    assert data.trend == "sell_dominant"
    assert data.tick_time == "094227"


def test_neutral_at_exactly_100():
    data = compute_execution_strength(
        {"tday_rltv": "100", "stck_cntg_hour": "120000"}, symbol="x", as_of=None
    )
    assert data.trend == "neutral"


def test_missing_tday_rltv_returns_none_not_fabricated():
    data = compute_execution_strength({}, symbol="005930", as_of=None)
    assert data.execution_strength_pct is None
    assert data.trend is None
    assert data.tick_time is None
    assert data.buy_volume is None
    assert data.sell_volume is None


def test_legacy_fhkst01010100_dict_is_no_longer_assumed():
    # ROB-485 회귀 방지: 옛 FHKST01010100 가정 키(cttr/shnu/seln)는 더 이상
    # 파싱하지 않는다 (해당 TR 에 체결강도 필드 부재가 라이브 검증됨).
    raw = {"cttr": "135.5", "shnu_cntg_qty": "1200", "seln_cntg_qty": "800"}
    data = compute_execution_strength(raw, symbol="005930", as_of=None)
    assert data.execution_strength_pct is None
    assert data.buy_volume is None
    assert data.sell_volume is None
    assert data.trend is None


def test_blank_tick_time_normalizes_to_none():
    data = compute_execution_strength(
        {"tday_rltv": "99.0", "stck_cntg_hour": "  "}, symbol="x", as_of=None
    )
    assert data.tick_time is None
    assert data.trend == "sell_dominant"

# tests/test_analyze_stock_floor.py
import pytest

from app.mcp_server.tooling.analysis_analyze import _apply_recommendation


@pytest.mark.unit
def test_floor_holds_when_consensus_absent():
    # price + rsi 있으나 consensus 없음 → 확신적 buy 금지(hold).
    analysis = {
        "quote": {"price": 1000.0},
        "indicators": {"rsi": {"14": 25.0}},
        "support_resistance": {"supports": [{"price": 950.0}]},
        "opinions": {},  # consensus 없음
        "valuation": {},
    }
    _apply_recommendation(analysis, "equity_kr")
    rec = analysis["recommendation"]
    assert rec["action"] == "hold"
    assert rec["confidence"] == "low"
    assert "consensus" in rec["insufficient_inputs"]


@pytest.mark.unit
def test_floor_unavailable_when_price_absent():
    analysis = {
        "quote": {"price": None},
        "indicators": {},
        "opinions": {},
        "valuation": {},
    }
    _apply_recommendation(analysis, "equity_kr")
    rec = analysis["recommendation"]
    assert rec["action"] == "unavailable"
    assert rec["confidence"] == "low"
    assert "price" in rec["insufficient_inputs"]


@pytest.mark.unit
def test_no_floor_when_inputs_complete():
    # bullish RSI + bullish consensus → buy 통과, insufficient 없음.
    analysis = {
        "quote": {"price": 1000.0},
        "indicators": {"rsi": {"14": 25.0}},
        "support_resistance": {"supports": [{"price": 950.0}]},
        "opinions": {
            "consensus": {
                "buy_count": 8,
                "sell_count": 1,
                "strong_buy_count": 5,
                "total_count": 10,
                "rows_used": 10,
            }
        },
        "valuation": {},
    }
    _apply_recommendation(analysis, "equity_kr")
    rec = analysis["recommendation"]
    assert rec["action"] == "buy"
    assert rec["insufficient_inputs"] == []


@pytest.mark.unit
def test_floor_holds_when_consensus_stale_only():
    """ROB-486 (031330): 윈도우 생존 row 0인 컨센서스는 presence 로 치지 않는다."""
    analysis = {
        "quote": {"price": 15360.0},
        "indicators": {"rsi": {"14": 25.0}},
        "support_resistance": {"supports": [{"price": 14000.0}]},
        "opinions": {
            "consensus": {
                "buy_count": 0,
                "hold_count": 0,
                "sell_count": 0,
                "strong_buy_count": 0,
                "total_count": 0,
                "avg_target_price": None,
                "median_target_price": None,
                "min_target_price": None,
                "max_target_price": None,
                "upside_pct": None,
                "current_price": 15360,
                "rows_total": 2,
                "rows_used": 0,
                "rows_excluded_stale": 2,
                "rows_undated": 0,
                "newest_opinion_date": "2019-12-27",
                "window_months": 12,
            }
        },
        "valuation": {},
    }
    _apply_recommendation(analysis, "equity_kr")
    rec = analysis["recommendation"]
    assert rec["action"] == "hold"
    assert rec["confidence"] == "low"
    assert "consensus" in rec["insufficient_inputs"]


@pytest.mark.unit
def test_floor_passes_when_windowed_rows_used_positive():
    """rows_used>0 이면 presence 인정."""
    analysis = {
        "quote": {"price": 1000.0},
        "indicators": {"rsi": {"14": 25.0}},
        "support_resistance": {"supports": [{"price": 950.0}]},
        "opinions": {
            "consensus": {
                "buy_count": 8,
                "sell_count": 1,
                "strong_buy_count": 5,
                "total_count": 10,
                "rows_used": 10,
            }
        },
        "valuation": {},
    }
    _apply_recommendation(analysis, "equity_kr")
    rec = analysis["recommendation"]
    assert rec["insufficient_inputs"] == []
    assert rec["action"] == "buy"


@pytest.mark.unit
def test_floor_holds_when_us_consensus_counts_all_none():
    """US(yfinance) rows_used 없음 + total_count None → presence 불인정 (fail-closed)."""
    analysis = {
        "quote": {"price": 150.0},
        "indicators": {"rsi": {"14": 25.0}},
        "support_resistance": {"supports": []},
        "opinions": {
            "consensus": {
                "buy_count": None,
                "hold_count": None,
                "sell_count": None,
                "strong_buy_count": None,
                "total_count": None,
                "avg_target_price": 195.5,
                "upside_pct": 8.0,
                "current_price": 150.0,
            }
        },
        "valuation": {},
    }
    _apply_recommendation(analysis, "equity_us")
    rec = analysis["recommendation"]
    assert rec["action"] == "hold"
    assert "consensus" in rec["insufficient_inputs"]

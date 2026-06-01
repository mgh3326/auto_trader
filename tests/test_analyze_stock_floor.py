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
            }
        },
        "valuation": {},
    }
    _apply_recommendation(analysis, "equity_kr")
    rec = analysis["recommendation"]
    assert rec["action"] == "buy"
    assert rec["insufficient_inputs"] == []

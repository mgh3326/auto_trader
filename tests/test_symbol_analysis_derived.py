import pytest

from app.models.investment_symbol_intermediate_reports import VERDICTS
from app.services.symbol_analysis.contract import (
    ConsensusData,
    FieldBlock,
    PriceData,
    TechnicalData,
)
from app.services.symbol_analysis.derived import RULE_VERSION, derive_recommendation


def _block(value, *, is_stale=False, source="kis_live"):
    return FieldBlock(value, source, None, is_stale)


def _bullish_consensus():
    return ConsensusData(buy=8, hold=1, sell=1, strong_buy=5, total=10, upside_pct=40.0)


@pytest.mark.unit
def test_action_always_in_verdicts_vocab():
    d = derive_recommendation(
        price=_block(PriceData(1000.0)),
        technicals=_block(
            TechnicalData(rsi14=25.0, supports=(950.0,), resistances=(1100.0,))
        ),
        consensus=_block(_bullish_consensus()),
    )
    assert d.action in VERDICTS
    assert d.rule_version == RULE_VERSION


@pytest.mark.unit
def test_deterministic_same_input_same_output():
    kwargs = {
        "price": _block(PriceData(1000.0)),
        "technicals": _block(
            TechnicalData(rsi14=25.0, supports=(950.0, 900.0), resistances=(1100.0,))
        ),
        "consensus": _block(_bullish_consensus()),
    }
    assert derive_recommendation(**kwargs) == derive_recommendation(**kwargs)


@pytest.mark.unit
def test_bullish_inputs_yield_buy():
    d = derive_recommendation(
        price=_block(PriceData(1000.0)),
        technicals=_block(
            TechnicalData(rsi14=25.0, supports=(950.0,), resistances=(1100.0,))
        ),
        consensus=_block(_bullish_consensus()),
    )
    assert d.action == "buy"
    assert d.confidence in ("medium", "high")
    assert d.insufficient_inputs == ()


@pytest.mark.unit
def test_price_absent_is_unavailable_floor():
    d = derive_recommendation(
        price=_block(None),
        technicals=_block(TechnicalData(rsi14=25.0)),
        consensus=_block(_bullish_consensus()),
    )
    assert d.action == "unavailable"
    assert d.confidence == "low"
    assert d.insufficient_inputs == ("price",)
    assert d.buy_zones == () and d.sell_targets == ()


@pytest.mark.unit
def test_stale_consensus_floors_to_hold_no_flip():
    # ROB-396 증상1: core 입력 불완전이면 확신적 buy/sell 금지 → hold floor.
    d = derive_recommendation(
        price=_block(PriceData(1000.0)),
        technicals=_block(TechnicalData(rsi14=25.0, supports=(950.0,))),
        consensus=_block(None, is_stale=True),
    )
    assert d.action == "hold"
    assert d.confidence == "low"
    assert "consensus" in d.insufficient_inputs


@pytest.mark.unit
def test_buy_zones_sorted_descending_and_below_price():
    d = derive_recommendation(
        price=_block(PriceData(1000.0)),
        technicals=_block(TechnicalData(rsi14=25.0, supports=(900.0, 950.0, 1050.0))),
        consensus=_block(_bullish_consensus()),
    )
    prices = [z.price for z in d.buy_zones]
    assert prices == sorted(prices, reverse=True)
    assert all(p < 1000.0 for p in prices)  # 현재가 이상 support 는 제외

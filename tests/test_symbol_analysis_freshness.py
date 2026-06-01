from datetime import date, datetime

import pytest

from app.services.symbol_analysis.contract import (
    ConsensusData,
    FieldBlock,
    FlowData,
    PriceData,
    TechnicalData,
    ValuationData,
)
from app.services.symbol_analysis.freshness import compute_is_stale, derive_freshness

TRADING_DATE = date(2026, 6, 1)


def _fresh(value, source):
    return FieldBlock(value, source, datetime(2026, 6, 1, 9, 30), is_stale=False)


def _blocks(*, price_stale=False, consensus_stale=False, valuation_stale=False):
    return {
        "price": FieldBlock(
            PriceData(1000.0), "kis_live", datetime(2026, 6, 1, 9, 30), price_stale
        ),
        "consensus": FieldBlock(
            ConsensusData(buy=8, total=10),
            "kis_live",
            datetime(2026, 6, 1, 8, 0),
            consensus_stale,
        ),
        "technicals": FieldBlock(
            TechnicalData(rsi14=40.0), "kis_live", datetime(2026, 6, 1, 8, 0), False
        ),
        "valuation": FieldBlock(
            ValuationData(per=12.0),
            "stock_info",
            datetime(2026, 6, 1, 8, 0),
            valuation_stale,
        ),
        "flow": FieldBlock(
            FlowData(foreign_net=1.0),
            "investor_flow_snapshots",
            datetime(2026, 6, 1, 8, 0),
            False,
        ),
    }


@pytest.mark.unit
def test_prev_day_close_during_regular_session_is_stale_price():
    # ROB-396 증상2 회귀: 전일종가(as_of 날짜 < trading_date)는 정규장에서 stale.
    prev_close_as_of = datetime(2026, 5, 30, 15, 30)
    assert (
        compute_is_stale("price", prev_close_as_of, trading_date=TRADING_DATE) is True
    )
    today_fill = datetime(2026, 6, 1, 9, 30)
    assert compute_is_stale("price", today_fill, trading_date=TRADING_DATE) is False


@pytest.mark.unit
def test_missing_as_of_is_stale():
    assert compute_is_stale("consensus", None, trading_date=TRADING_DATE) is True


@pytest.mark.unit
def test_overall_unavailable_when_price_value_none():
    blocks = _blocks()
    blocks["price"] = FieldBlock(None, "kis_live", None, True)
    fresh = derive_freshness(blocks)
    assert fresh.overall == "unavailable"


@pytest.mark.unit
def test_overall_stale_when_core_field_stale():
    fresh = derive_freshness(_blocks(consensus_stale=True))
    assert fresh.overall == "stale"
    assert "consensus" in fresh.stale_fields


@pytest.mark.unit
def test_supplementary_stale_does_not_downgrade_below_partial():
    # ROB-323 anti-pattern 회피: valuation(보조)만 stale 이면 overall=partial, stale 아님.
    fresh = derive_freshness(_blocks(valuation_stale=True))
    assert fresh.overall == "partial"
    assert "valuation" in fresh.stale_fields


@pytest.mark.unit
def test_overall_fresh_when_all_fresh():
    assert derive_freshness(_blocks()).overall == "fresh"

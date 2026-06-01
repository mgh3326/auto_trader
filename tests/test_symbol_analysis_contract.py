import dataclasses
from datetime import datetime

import pytest

from app.services.symbol_analysis.contract import (
    DerivedBlock,
    FieldBlock,
    Freshness,
    GetSymbolAnalysis,
    PriceData,
    PriceLevel,
    Provenance,
    SymbolAnalysis,
)


@pytest.mark.unit
def test_field_block_is_frozen_and_carries_provenance():
    block = FieldBlock(
        value=PriceData(last=1000.0),
        source="kis_live",
        as_of=datetime(2026, 6, 1, 9, 30),
        is_stale=False,
    )
    assert block.value.last == 1000.0
    assert block.source == "kis_live"
    assert block.is_stale is False
    with pytest.raises(dataclasses.FrozenInstanceError):
        block.is_stale = True  # type: ignore[misc]


@pytest.mark.unit
def test_symbol_analysis_construction_and_frozen():
    sa = SymbolAnalysis(
        symbol="005930",
        name="삼성전자",
        market="kr",
        price=FieldBlock(PriceData(last=1000.0), "kis_live", None, False),
        valuation=FieldBlock(None, "stock_info", None, True),
        technicals=FieldBlock(None, "kis_live", None, True),
        consensus=FieldBlock(None, "kis_live", None, True),
        flow=FieldBlock(None, "investor_flow_snapshots", None, True),
        derived=DerivedBlock(
            action="hold",
            confidence="low",
            buy_zones=(),
            sell_targets=(),
            stop=None,
            rule_version="symbol_analysis.derived.v1",
            insufficient_inputs=("consensus", "technicals"),
        ),
        provenance=Provenance(
            snapshot_uuid=None,
            primary_source="kis_live",
            freshness=Freshness(overall="stale", stale_fields=("consensus",)),
        ),
    )
    assert sa.symbol == "005930"
    assert sa.provenance.freshness.overall == "stale"
    with pytest.raises(dataclasses.FrozenInstanceError):
        sa.symbol = "000660"  # type: ignore[misc]


@pytest.mark.unit
def test_price_level_holds_price_kind_reasoning():
    level = PriceLevel(price=950.0, kind="support", reasoning="Support at 950")
    assert (level.price, level.kind, level.reasoning) == (
        950.0,
        "support",
        "Support at 950",
    )


@pytest.mark.unit
def test_get_symbol_analysis_is_runtime_protocol():
    # 런타임 구현 없이 호출 계약만 타입으로 고정한다.
    assert getattr(GetSymbolAnalysis, "_is_protocol", False) is True

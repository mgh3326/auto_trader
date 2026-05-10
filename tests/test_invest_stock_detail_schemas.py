from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.schemas.invest_stock_detail import (
    CapabilityFlag,
    StockDetailCapabilities,
    StockDetailHolding,
    StockDetailOrderbook,
    StockDetailOrderbookLevel,
    StockDetailResponse,
    default_capabilities_for_market,
)


def _base_response(**overrides):
    payload = {
        "symbol": "005930",
        "market": "kr",
        "displayName": "삼성전자",
        "exchange": "KOSPI",
        "instrumentType": "equity_kr",
        "currency": "KRW",
        "assetType": "equity",
        "assetCategory": "kr_stock",
        "quote": None,
        "screenerSnapshot": None,
        "valuation": None,
        "holding": None,
        "latestAnalysis": None,
        "orderbookSupport": {"supported": False, "reason": "kr_unavailable"},
        "orderbook": None,
        "capabilities": default_capabilities_for_market("kr"),
        "meta": {"computedAt": datetime.now(UTC), "warnings": []},
    }
    payload.update(overrides)
    return payload


def test_stock_detail_rejects_unknown_market_literal():
    with pytest.raises(ValidationError):
        StockDetailResponse.model_validate(_base_response(market="jp"))


def test_execution_and_options_capabilities_are_read_only_flags():
    capabilities = StockDetailCapabilities()

    assert capabilities.execution.supported is False
    assert capabilities.execution.reason == "read_only_mvp"
    assert capabilities.options.supported is False
    assert capabilities.options.reason == "out_of_mvp_scope"

    with pytest.raises(ValidationError):
        CapabilityFlag(supported=False, reason=None)


def test_holding_and_valuation_are_optional_explicit_nulls():
    response = StockDetailResponse.model_validate(_base_response())

    assert response.holding is None
    assert response.valuation is None

    held = StockDetailHolding(
        totalQuantity=3,
        averageCost=100,
        costBasis=300,
        valueNative=330,
        valueKrw=330,
        pnlKrw=30,
        pnlRate=0.1,
        includedSources=["kis"],
        priceState="live",
    )
    response = StockDetailResponse.model_validate(_base_response(holding=held))
    assert response.holding is not None


def test_orderbook_required_iff_supported():
    supported_without_book = _base_response(
        orderbookSupport={"supported": True, "reason": None},
        capabilities=default_capabilities_for_market("kr"),
        orderbook=None,
    )
    with pytest.raises(ValidationError):
        StockDetailResponse.model_validate(supported_without_book)

    unsupported_with_book = _base_response(
        orderbookSupport={"supported": False, "reason": "us_unsupported"},
        orderbook=StockDetailOrderbook(
            asOf=datetime.now(UTC),
            asks=[StockDetailOrderbookLevel(price=101, quantity=1)],
            bids=[StockDetailOrderbookLevel(price=100, quantity=2)],
        ),
    )
    with pytest.raises(ValidationError):
        StockDetailResponse.model_validate(unsupported_with_book)

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.schemas.invest_stock_detail import (
    CapabilityFlag,
    StockDetailCandlesResponse,
    StockDetailCapabilities,
    StockDetailHolding,
    StockDetailOrder,
    StockDetailOrderbook,
    StockDetailOrderbookLevel,
    StockDetailOrderBucket,
    StockDetailOrdersResponse,
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
    assert response.meta.blockStates.holding == "provider_unwired"

    held = StockDetailHolding(
        totalQuantity=3,
        averageCost=100,
        costBasis=300,
        valueNative=330,
        valueKrw=330,
        pnlKrw=30,
        pnlRate=0.1,
        includedSources=["kis"],
        sourceBreakdown=[
            {
                "source": "kis",
                "accountName": "KIS",
                "quantity": 3,
                "averageCost": 100,
                "costBasis": 300,
                "valueNative": 330,
                "valueKrw": 330,
            }
        ],
        priceState="live",
    )
    response = StockDetailResponse.model_validate(
        _base_response(
            holding=held,
            meta={
                "computedAt": datetime.now(UTC),
                "warnings": [],
                "blockStates": {"holding": "fresh"},
            },
        )
    )
    assert response.holding is not None
    assert response.holding.sourceBreakdown[0].accountName == "KIS"
    assert response.meta.blockStates.holding == "fresh"


def test_candles_response_exposes_data_state_metadata():
    response = StockDetailCandlesResponse(
        symbol="005930",
        market="kr",
        period="1d",
        source="kis",
        candles=[],
        meta={"dataState": "missing", "warnings": []},
    )

    assert response.meta.dataState == "missing"


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


def test_orders_contract_separates_filled_history_from_pending_state():
    filled_order = StockDetailOrder(
        orderId="filled-1",
        symbol="005930",
        market="kr",
        side="buy",
        quantity=1,
        price=70000,
        filledAt=datetime.now(UTC),
        account="kis",
        source="n8n_filled_orders_service",
    )

    response = StockDetailOrdersResponse(
        symbol="005930",
        market="kr",
        filled=StockDetailOrderBucket(
            items=[filled_order],
            nextCursor=None,
            state="present",
            emptyState=None,
            source="n8n_filled_orders_service",
        ),
        pending=StockDetailOrderBucket(
            items=[],
            nextCursor=None,
            state="provider_unwired",
            emptyState=None,
            source=None,
            warnings=["pending_orders_provider_unwired"],
        ),
        items=[filled_order],
        nextCursor=None,
        meta={"emptyState": None, "warnings": ["pending_orders_provider_unwired"]},
    )

    assert response.filled.state == "present"
    assert response.pending.state == "provider_unwired"
    assert response.pending.emptyState is None
    assert response.meta.emptyState is None


def test_pending_orders_empty_state_requires_queried_pending_bucket():
    response = StockDetailOrdersResponse(
        symbol="005930",
        market="kr",
        filled=StockDetailOrderBucket(
            items=[],
            nextCursor=None,
            state="empty",
            emptyState="no_filled_orders",
            source="n8n_filled_orders_service",
        ),
        pending=StockDetailOrderBucket(
            items=[],
            nextCursor=None,
            state="empty",
            emptyState="no_pending_orders",
            source="read_only_pending_orders_snapshot",
        ),
        items=[],
        nextCursor=None,
        meta={"emptyState": "no_filled_orders", "warnings": []},
    )

    assert response.filled.emptyState == "no_filled_orders"
    assert response.pending.emptyState == "no_pending_orders"
    assert response.pending.source == "read_only_pending_orders_snapshot"


def test_empty_order_bucket_requires_empty_state_and_queried_source():
    with pytest.raises(ValidationError):
        StockDetailOrderBucket(
            items=[],
            nextCursor=None,
            state="empty",
            emptyState=None,
            source=None,
        )


def test_provider_unwired_order_bucket_rejects_false_empty_or_source():
    with pytest.raises(ValidationError):
        StockDetailOrderBucket(
            items=[],
            nextCursor=None,
            state="provider_unwired",
            emptyState="no_pending_orders",
            source=None,
        )

    with pytest.raises(ValidationError):
        StockDetailOrderBucket(
            items=[],
            nextCursor=None,
            state="provider_unwired",
            emptyState=None,
            source="read_only_pending_orders_snapshot",
        )

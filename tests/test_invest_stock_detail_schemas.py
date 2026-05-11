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
        "naverEnrichment": None,
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
    assert response.naverEnrichment is None

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


def test_naver_enrichment_documents_fixture_backed_read_only_poc():
    response = StockDetailResponse.model_validate(
        _base_response(
            naverEnrichment={
                "source": "naver_stock_detail_poc",
                "market": "kr",
                "symbol": "005930",
                "naverCode": "005930",
                "pageUrl": "https://stock.naver.com/domestic/stock/005930/price",
                "status": "fixture_backed_poc",
                "liveFetchEnabled": False,
                "endpoints": [
                    {
                        "surface": "domestic_news_aggregate_home",
                        "url": "https://stock.naver.com/api/domestic/news/aggregate/home",
                        "status": "verified_200",
                        "payloadFields": ["flashNews[].title"],
                        "mappedFields": ["news.items"],
                        "risk": "not symbol scoped",
                    }
                ],
                "usefulFields": ["source freshness / polling interval"],
                "noGoFields": ["raw public discussion post text"],
                "docsPath": "docs/invest/naver-stock-detail-raw-data-poc.md",
            }
        )
    )

    assert response.naverEnrichment is not None
    assert response.naverEnrichment.liveFetchEnabled is False
    assert response.naverEnrichment.endpoints[0].status == "verified_200"
    assert "raw public discussion post text" in response.naverEnrichment.noGoFields


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

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.schemas.invest_stock_detail import (
    CapabilityFlag,
    StockDetailCapabilities,
    StockDetailDiscussionSignal,
    StockDetailDiscussionSignalMetric,
    StockDetailFxSensitivity,
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
        "discussionSignal": None,
        "holding": None,
        "fxSensitivity": None,
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


def test_discussion_signal_kr_fixture_backed_aggregate_only():
    signal = StockDetailDiscussionSignal(
        market="kr",
        symbol="005930",
        naverCode="005930",
        status="no_go_pending_review",
        liveFetchEnabled=False,
        freshness="fixture",
        observedAt=datetime(2026, 5, 11, 6, 0, tzinfo=UTC),
        windowLabel="ROB-199 one-off aggregate rankings probe",
        activityRank=5,
        postCount=128,
        commentCount=342,
        reactionCount=911,
        momentum="rising",
        metrics=[
            StockDetailDiscussionSignalMetric(
                label="activity_rank", value=5, unit="rank"
            ),
            StockDetailDiscussionSignalMetric(
                label="post_count", value=128, unit="count"
            ),
        ],
        mappedFields=["discussion.activityRank", "discussion.postCount"],
        noGoFields=["public discussion post text"],
        risk="aggregate signal only",
        docsPath="docs/invest/naver-discussion-signal-poc.md",
    )
    assert signal.liveFetchEnabled is False
    assert signal.momentum == "rising"
    assert signal.activityRank == 5
    assert signal.source == "naver_discussion_signal_poc"


def test_discussion_signal_rejects_live_fetch():
    with pytest.raises(ValidationError, match="must not enable live fetching"):
        StockDetailDiscussionSignal(
            market="kr",
            symbol="005930",
            naverCode="005930",
            liveFetchEnabled=True,
            windowLabel="bad",
            risk="x",
            docsPath="x",
        )


def test_discussion_signal_rejects_ugc_field_labels():
    with pytest.raises(ValidationError, match="aggregate metrics only"):
        StockDetailDiscussionSignal(
            market="kr",
            symbol="005930",
            naverCode="005930",
            liveFetchEnabled=False,
            windowLabel="test",
            metrics=[
                StockDetailDiscussionSignalMetric(
                    label="post_title", value="삼성전자 전망", unit=None
                ),
            ],
            risk="x",
            docsPath="x",
        )


def test_discussion_signal_null_for_crypto_in_response():
    response = StockDetailResponse.model_validate(
        {
            "symbol": "KRW-BTC",
            "market": "crypto",
            "displayName": "비트코인",
            "exchange": "Upbit",
            "instrumentType": "crypto",
            "currency": "KRW",
            "assetType": "crypto",
            "assetCategory": "crypto",
            "quote": None,
            "screenerSnapshot": None,
            "valuation": None,
            "naverEnrichment": None,
            "discussionSignal": None,
            "holding": None,
            "orderbookSupport": {"supported": False, "reason": "crypto_deferred"},
            "orderbook": None,
            "capabilities": default_capabilities_for_market("crypto"),
            "meta": {"computedAt": datetime.now(UTC), "warnings": []},
        }
    )
    assert response.discussionSignal is None


def test_discussion_signal_wired_into_stock_detail_response():
    response = StockDetailResponse.model_validate(
        _base_response(
            discussionSignal={
                "source": "naver_discussion_signal_poc",
                "market": "kr",
                "symbol": "005930",
                "naverCode": "005930",
                "status": "no_go_pending_review",
                "liveFetchEnabled": False,
                "freshness": "fixture",
                "observedAt": "2026-05-11T06:00:00Z",
                "windowLabel": "ROB-199 one-off aggregate rankings probe",
                "activityRank": 5,
                "postCount": 128,
                "commentCount": 342,
                "reactionCount": 911,
                "momentum": "rising",
                "metrics": [
                    {"label": "activity_rank", "value": 5, "unit": "rank"},
                ],
                "mappedFields": ["discussion.activityRank"],
                "noGoFields": ["public discussion post text"],
                "risk": "aggregate signal only",
                "docsPath": "docs/invest/naver-discussion-signal-poc.md",
            }
        )
    )
    assert response.discussionSignal is not None
    assert response.discussionSignal.liveFetchEnabled is False
    assert response.discussionSignal.activityRank == 5
    assert response.discussionSignal.momentum == "rising"


def test_fx_sensitivity_available_contract_accepts_scenarios():
    response = StockDetailResponse.model_validate(
        _base_response(
            market="us",
            symbol="QQQM",
            currency="USD",
            assetCategory="us_stock",
            orderbookSupport={"supported": False, "reason": "us_unsupported"},
            capabilities=default_capabilities_for_market("us"),
            fxSensitivity={
                "status": "available",
                "currencyPair": "USD/KRW",
                "baseFxRate": 1360.0,
                "holdingValueNative": 422.68,
                "holdingValueKrw": 575000.0,
                "basis": "portfolio_value",
                "scenarios": [
                    {
                        "rateMovePct": -1.0,
                        "estimatedKrwImpact": -5748.448,
                        "estimatedValueKrw": 569096.352,
                        "label": "USD/KRW -1%",
                    },
                    {
                        "rateMovePct": 1.0,
                        "estimatedKrwImpact": 5748.448,
                        "estimatedValueKrw": 580593.248,
                        "label": "USD/KRW +1%",
                    },
                ],
                "caution": "환율 민감도는 가정치입니다.",
            },
        )
    )

    assert response.fxSensitivity is not None
    assert response.fxSensitivity.status == "available"
    assert response.fxSensitivity.scenarios[1].estimatedKrwImpact == pytest.approx(
        5748.448
    )


def test_fx_sensitivity_available_requires_rate_and_native_value():
    with pytest.raises(ValidationError, match="requires USD/KRW rate"):
        StockDetailFxSensitivity(
            status="available",
            currencyPair=None,
            holdingValueNative=10,
            scenarios=[{"rateMovePct": 1, "label": "USD/KRW +1%"}],
            caution="x",
        )

    with pytest.raises(ValidationError, match="requires positive native value"):
        StockDetailFxSensitivity(
            status="available",
            currencyPair="USD/KRW",
            baseFxRate=1360,
            holdingValueNative=0,
            scenarios=[{"rateMovePct": 1, "label": "USD/KRW +1%"}],
            caution="x",
        )


def test_fx_sensitivity_unavailable_rejects_scenarios():
    with pytest.raises(ValidationError, match="must not expose scenarios"):
        StockDetailFxSensitivity(
            status="not_applicable",
            scenarios=[{"rateMovePct": 1, "label": "USD/KRW +1%"}],
            caution="x",
        )


def test_fx_sensitivity_null_for_kr_response_is_valid():
    response = StockDetailResponse.model_validate(_base_response(fxSensitivity=None))

    assert response.fxSensitivity is None


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


def test_stock_detail_holding_exposes_tradeable_and_reference_quantities():
    from app.schemas.invest_stock_detail import StockDetailHolding

    holding = StockDetailHolding(
        totalQuantity=5,
        tradeableQuantity=3,
        sellableQuantity=2,
        pendingSellQuantity=1,
        referenceQuantity=2,
        averageCost=100,
        costBasis=500,
        valueNative=550,
        valueKrw=550,
        pnlKrw=50,
        pnlRate=0.1,
        includedSources=["kis", "toss_manual"],
        priceState="live",
    )

    assert holding.tradeableQuantity == 3
    assert holding.referenceQuantity == 2


@pytest.mark.unit
def test_decision_history_schema_forbids_extra_and_maps_sections():
    from app.schemas.invest_stock_detail import (
        StockDetailDecisionHistory,
        StockDetailDecisionHistoryBrier,
    )

    model = StockDetailDecisionHistory(
        symbol="000660",
        market="kr",
        linkQuality="symbol_window",
        priorDecisions=[
            {
                "date": "2026-06-28",
                "intent": "buy_review",
                "side": "buy",
                "decisionBucket": "new_buy_candidate",
                "confidence": 0.7,
                "rationale": "HBM 수요 지속",
            }
        ],
        priorLessons=["과열 구간 추격 금지"],
        realizedOutcomes=[
            {
                "date": "2026-06-20",
                "side": "sell",
                "outcome": "stop_loss",
                "triggerType": "stop",
                "pnlPct": -3.1,
                "realizedPnl": -31000.0,
            }
        ],
        openClaims=[
            {
                "probability": 0.7,
                "horizon": "1w",
                "reviewDate": "2026-07-10",
                "direction": "up",
                "targetPrice": 82000.0,
            }
        ],
        runningBrierSymbol=StockDetailDecisionHistoryBrier(
            n=12, meanBrier=0.18, flag="ok"
        ),
        runningBrierGlobal=StockDetailDecisionHistoryBrier(
            n=4, meanBrier=None, flag="insufficient_sample"
        ),
    )
    assert model.priorDecisions[0].confidence == 0.7
    assert model.realizedOutcomes[0].outcome == "stop_loss"
    assert model.openClaims[0].targetPrice == 82000.0
    assert model.runningBrierGlobal.flag == "insufficient_sample"
    assert "직접 연결" in model.cautionLabel  # default caution present

    with pytest.raises(ValidationError):
        StockDetailDecisionHistoryBrier(n=1, meanBrier=0.1, flag="ok", extra="x")

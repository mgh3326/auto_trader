# tests/test_kiwoom_constants.py
"""Verify Kiwoom constants are correct, KRX-only, and mock-safe."""

from __future__ import annotations

from app.services.brokers.kiwoom import constants as k


def test_base_urls_distinguish_mock_and_live():
    assert k.MOCK_BASE_URL == "https://mockapi.kiwoom.com"
    assert k.LIVE_BASE_URL == "https://api.kiwoom.com"
    assert k.MOCK_BASE_URL != k.LIVE_BASE_URL


def test_oauth_constants():
    assert k.OAUTH_API_ID == "au10001"
    assert k.OAUTH_PATH == "/oauth2/token"
    assert k.OAUTH_CONTENT_TYPE == "application/json;charset=UTF-8"


def test_order_api_ids():
    assert k.ORDER_PATH == "/api/dostk/ordr"
    assert k.ORDER_BUY_API_ID == "kt10000"
    assert k.ORDER_SELL_API_ID == "kt10001"
    assert k.ORDER_MODIFY_API_ID == "kt10002"
    assert k.ORDER_CANCEL_API_ID == "kt10003"


def test_account_api_ids():
    assert k.ACCOUNT_ORDER_DETAIL_API_ID == "kt00007"
    assert k.ACCOUNT_ORDER_STATUS_API_ID == "kt00009"
    assert k.ACCOUNT_ORDERABLE_AMOUNT_API_ID == "kt00010"
    assert k.ACCOUNT_BALANCE_API_ID == "kt00018"


def test_chart_api_ids_present_but_deferred():
    # Chart APIs are scaffolded only; not used by default OHLCV path.
    assert k.CHART_MINUTE_API_ID == "ka10080"
    assert k.CHART_DAILY_API_ID == "ka10081"
    assert k.CHART_WEEKLY_API_ID == "ka10082"
    assert k.CHART_MONTHLY_API_ID == "ka10083"


def test_krx_is_only_supported_exchange_for_mock():
    assert k.MOCK_EXCHANGE_KRX == "KRX"
    assert "NXT" in k.MOCK_REJECTED_EXCHANGES
    assert "SOR" in k.MOCK_REJECTED_EXCHANGES
    assert k.MOCK_EXCHANGE_KRX not in k.MOCK_REJECTED_EXCHANGES


def test_continuation_header_names():
    assert k.HEADER_CONT_YN == "cont-yn"
    assert k.HEADER_NEXT_KEY == "next-key"
    assert k.HEADER_API_ID == "api-id"
    assert k.HEADER_AUTHORIZATION == "authorization"


def test_kiwoom_capability_kr_paper_only():
    from app.services.brokers.capabilities import (
        BROKER_CAPABILITIES,
        Broker,
        Market,
    )

    cap = BROKER_CAPABILITIES[Broker.KIWOOM]
    assert cap.markets == frozenset({Market.KR_EQUITY})
    assert cap.supports_paper is True
    assert cap.supports_live is False

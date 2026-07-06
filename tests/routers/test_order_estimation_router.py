from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.db import get_db
from app.models.trading import InstrumentType
from app.routers.order_estimation import router


def _user(uid=1):
    return SimpleNamespace(id=uid)


@pytest.fixture
def base_app():
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    return app


# ---- Test Suite for GET /symbols/crypto/estimated-cost ----


@pytest.mark.unit
def test_order_estimation_happy_path(base_app):
    """예상 매수 비용 정상 흐름 조회"""
    # 1. Mocking dependencies
    mock_user = _user(uid=1)
    mock_defaults = SimpleNamespace(crypto_default_buy_amount=15000.0)

    mock_coins = [
        {
            "currency": "BTC",
            "balance": "0.002",
            "locked": "0.0",
            "avg_buy_price": "50000000",
        },
        {
            "currency": "ETH",
            "balance": "0.05",
            "locked": "0.0",
            "avg_buy_price": "3000000",
        },
    ]
    tradable_currencies = ["BTC", "ETH"]

    # mock settings
    mock_setting = SimpleNamespace(
        symbol="KRW-BTC",
        instrument_type=InstrumentType.crypto,
        buy_quantity_per_order=20000.0,
        buy_price_levels=2,
    )
    # ETH has no custom setting, will use defaults (15000.0, buy_price_levels=4)

    # mock analysis models
    mock_btc_analysis = SimpleNamespace(
        appropriate_buy_min=Decimal("49000000"),
        appropriate_buy_max=Decimal("51000000"),
        buy_hope_min=Decimal("48000000"),
        buy_hope_max=Decimal("47000000"),
    )
    mock_eth_analysis = SimpleNamespace(
        appropriate_buy_min=Decimal("2900000"),
        appropriate_buy_max=Decimal("3100000"),
        buy_hope_min=None,
        buy_hope_max=None,
    )

    with (
        patch(
            "app.routers.order_estimation.get_user_from_request",
            new=AsyncMock(return_value=mock_user),
        ),
        patch(
            "app.routers.order_estimation.UserTradeDefaultsService"
        ) as MockDefaultsSvc,
        patch(
            "app.services.brokers.upbit.client.fetch_my_coins",
            new=AsyncMock(return_value=mock_coins),
        ),
        patch(
            "app.routers.order_estimation.get_active_upbit_base_currencies",
            new=AsyncMock(return_value=tradable_currencies),
        ),
        patch(
            "app.routers.order_estimation.SymbolTradeSettingsService"
        ) as MockSettingsSvc,
        patch("app.routers.order_estimation.StockAnalysisService") as MockAnalysisSvc,
        patch(
            "app.routers.order_estimation.fetch_pending_crypto_buy_cost",
            new=AsyncMock(return_value=5000.0),
        ),
    ):
        # Setup mocks
        MockDefaultsSvc.return_value.get_or_create = AsyncMock(
            return_value=mock_defaults
        )
        MockSettingsSvc.return_value.get_all = AsyncMock(return_value=[mock_setting])

        def get_analysis(symbol):
            if symbol == "KRW-BTC":
                return mock_btc_analysis
            elif symbol == "KRW-ETH":
                return mock_eth_analysis
            return None

        MockAnalysisSvc.return_value.get_latest_analysis_by_symbol = AsyncMock(
            side_effect=get_analysis
        )

        client = TestClient(base_app)
        resp = client.get("/api/symbol-settings/symbols/crypto/estimated-cost")

        assert resp.status_code == 200
        data = resp.json()

        # Grand total cost = (20000 * 2 levels for BTC) + (15000 * 2 levels for ETH) = 40000 + 30000 = 70000
        assert data["grand_total_cost"] == 70000.0
        assert data["total_symbols"] == 2
        assert data["pending_buy_orders_cost"] == 5000.0
        assert data["net_estimated_cost"] == 65000.0  # 70000 - 5000


@pytest.mark.unit
def test_zero_price_guard_quantity_zero_cost_full(base_app):
    """buy_prices의 price가 0일 때 quantity=0, cost는 full buy_amount인지 검증"""
    mock_user = _user(uid=1)
    mock_defaults = SimpleNamespace(crypto_default_buy_amount=10000.0)
    mock_coins = [
        {
            "currency": "BTC",
            "balance": "1.0",
            "locked": "0.0",
            "avg_buy_price": "50000000",
        }
    ]
    tradable_currencies = ["BTC"]

    mock_btc_analysis = SimpleNamespace(
        appropriate_buy_min=Decimal("0"),
        appropriate_buy_max=Decimal("51000000"),
        buy_hope_min=None,
        buy_hope_max=None,
    )

    with (
        patch(
            "app.routers.order_estimation.get_user_from_request",
            new=AsyncMock(return_value=mock_user),
        ),
        patch(
            "app.routers.order_estimation.UserTradeDefaultsService"
        ) as MockDefaultsSvc,
        patch(
            "app.services.brokers.upbit.client.fetch_my_coins",
            new=AsyncMock(return_value=mock_coins),
        ),
        patch(
            "app.routers.order_estimation.get_active_upbit_base_currencies",
            new=AsyncMock(return_value=tradable_currencies),
        ),
        patch(
            "app.routers.order_estimation.SymbolTradeSettingsService"
        ) as MockSettingsSvc,
        patch("app.routers.order_estimation.StockAnalysisService") as MockAnalysisSvc,
        patch(
            "app.routers.order_estimation.fetch_pending_crypto_buy_cost",
            new=AsyncMock(return_value=0.0),
        ),
    ):
        MockDefaultsSvc.return_value.get_or_create = AsyncMock(
            return_value=mock_defaults
        )
        MockSettingsSvc.return_value.get_all = AsyncMock(return_value=[])
        MockAnalysisSvc.return_value.get_latest_analysis_by_symbol = AsyncMock(
            return_value=mock_btc_analysis
        )

        client = TestClient(base_app)
        resp = client.get("/api/symbol-settings/symbols/crypto/estimated-cost")

        assert resp.status_code == 200
        data = resp.json()

        btc_symbol = data["symbols"][0]
        assert btc_symbol["symbol"] == "KRW-BTC"
        assert (
            btc_symbol["total_cost"] == 20000.0
        )  # 10000 * 2 levels (one is 0 price, one is 51M)

        # Verify the level with price 0 has quantity 0, but cost is 10000
        zero_price_level = [
            level for level in btc_symbol["buy_prices"] if level["price"] == 0
        ][0]
        assert zero_price_level["quantity"] == 0
        assert zero_price_level["cost"] == 10000.0


@pytest.mark.unit
def test_net_cost_clamped_at_zero(base_app):
    """pending_buy_orders_cost가 grand_total_cost보다 클 때 net_estimated_cost가 0.0 이하로 내려가지 않는지 검증"""
    mock_user = _user(uid=1)
    mock_defaults = SimpleNamespace(crypto_default_buy_amount=10000.0)
    mock_coins = [
        {
            "currency": "BTC",
            "balance": "1.0",
            "locked": "0.0",
            "avg_buy_price": "50000000",
        }
    ]
    tradable_currencies = ["BTC"]
    mock_btc_analysis = SimpleNamespace(
        appropriate_buy_min=Decimal("50000000"),
        appropriate_buy_max=None,
        buy_hope_min=None,
        buy_hope_max=None,
    )

    with (
        patch(
            "app.routers.order_estimation.get_user_from_request",
            new=AsyncMock(return_value=mock_user),
        ),
        patch(
            "app.routers.order_estimation.UserTradeDefaultsService"
        ) as MockDefaultsSvc,
        patch(
            "app.services.brokers.upbit.client.fetch_my_coins",
            new=AsyncMock(return_value=mock_coins),
        ),
        patch(
            "app.routers.order_estimation.get_active_upbit_base_currencies",
            new=AsyncMock(return_value=tradable_currencies),
        ),
        patch(
            "app.routers.order_estimation.SymbolTradeSettingsService"
        ) as MockSettingsSvc,
        patch("app.routers.order_estimation.StockAnalysisService") as MockAnalysisSvc,
        patch(
            "app.routers.order_estimation.fetch_pending_crypto_buy_cost",
            new=AsyncMock(return_value=50000.0),
        ),
    ):  # pending cost is 50,000
        MockDefaultsSvc.return_value.get_or_create = AsyncMock(
            return_value=mock_defaults
        )
        MockSettingsSvc.return_value.get_all = AsyncMock(return_value=[])
        MockAnalysisSvc.return_value.get_latest_analysis_by_symbol = AsyncMock(
            return_value=mock_btc_analysis
        )

        client = TestClient(base_app)
        resp = client.get("/api/symbol-settings/symbols/crypto/estimated-cost")

        assert resp.status_code == 200
        data = resp.json()
        assert data["grand_total_cost"] == 10000.0  # 1 level of BTC
        assert data["pending_buy_orders_cost"] == 50000.0
        assert data["net_estimated_cost"] == 0.0  # max(0, 10000 - 50000) = 0.0


@pytest.mark.unit
def test_filtering_and_skipping_rules(base_app):
    """임계치 미달, 비거래 가능 통화, KRW 현금 제외, 분석 미존재 등으로 필터링/스킵되는 케이스 검증"""
    mock_user = _user(uid=1)
    mock_defaults = SimpleNamespace(crypto_default_buy_amount=10000.0)

    # (a) BTC: 정상
    # (b) KRW: 현금이라 스킵
    # (c) ETH: 평가금액 < 1000원이라 스킵 (0.0001 * 3,000,000 = 300원)
    # (d) XRP: 거래가능 통화 목록에 없어 스킵
    # (e) ADA: 분석 정보가 없어 스킵
    mock_coins = [
        {
            "currency": "BTC",
            "balance": "1.0",
            "locked": "0.0",
            "avg_buy_price": "50000000",
        },
        {
            "currency": "KRW",
            "balance": "1000000",
            "locked": "0.0",
            "avg_buy_price": "1",
        },
        {
            "currency": "ETH",
            "balance": "0.0001",
            "locked": "0.0",
            "avg_buy_price": "3000000",
        },
        {
            "currency": "XRP",
            "balance": "100.0",
            "locked": "0.0",
            "avg_buy_price": "800",
        },
        {
            "currency": "ADA",
            "balance": "1000.0",
            "locked": "0.0",
            "avg_buy_price": "600",
        },
    ]
    tradable_currencies = ["BTC", "ETH", "ADA"]  # XRP is not in here

    mock_btc_analysis = SimpleNamespace(
        appropriate_buy_min=Decimal("50000000"),
        appropriate_buy_max=None,
        buy_hope_min=None,
        buy_hope_max=None,
    )

    with (
        patch(
            "app.routers.order_estimation.get_user_from_request",
            new=AsyncMock(return_value=mock_user),
        ),
        patch(
            "app.routers.order_estimation.UserTradeDefaultsService"
        ) as MockDefaultsSvc,
        patch(
            "app.services.brokers.upbit.client.fetch_my_coins",
            new=AsyncMock(return_value=mock_coins),
        ),
        patch(
            "app.routers.order_estimation.get_active_upbit_base_currencies",
            new=AsyncMock(return_value=tradable_currencies),
        ),
        patch(
            "app.routers.order_estimation.SymbolTradeSettingsService"
        ) as MockSettingsSvc,
        patch("app.routers.order_estimation.StockAnalysisService") as MockAnalysisSvc,
        patch(
            "app.routers.order_estimation.fetch_pending_crypto_buy_cost",
            new=AsyncMock(return_value=0.0),
        ),
    ):
        MockDefaultsSvc.return_value.get_or_create = AsyncMock(
            return_value=mock_defaults
        )
        MockSettingsSvc.return_value.get_all = AsyncMock(return_value=[])

        def get_analysis(symbol):
            # Give every coin EXCEPT ADA a valid analysis, so that ETH / XRP /
            # KRW can only be excluded by the pre-analysis filter (threshold /
            # tradable-currency / KRW-cash) — NOT by the analysis-None skip.
            # Without this, deleting the filter would keep total_symbols == 1
            # because ETH/XRP/KRW would fall through to the None skip anyway
            # (false-green). ADA alone stays None to exercise the None skip.
            if symbol == "KRW-ADA":
                return None
            return mock_btc_analysis

        MockAnalysisSvc.return_value.get_latest_analysis_by_symbol = AsyncMock(
            side_effect=get_analysis
        )

        client = TestClient(base_app)
        resp = client.get("/api/symbol-settings/symbols/crypto/estimated-cost")

        assert resp.status_code == 200
        data = resp.json()

        # Only BTC should survive: KRW is cash, ETH is below the 1000-KRW
        # threshold, XRP is not in tradable_currencies, ADA has no analysis.
        assert data["total_symbols"] == 1
        assert data["symbols"][0]["symbol"] == "KRW-BTC"

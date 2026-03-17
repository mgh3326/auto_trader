from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pandas as pd
import pytest

import app.services.brokers.upbit.client as upbit_service
from app.services import portfolio_overview_service as portfolio_overview_module
from app.services.portfolio_overview_service import PortfolioOverviewService
from app.services.us_symbol_universe_service import USSymbolNotRegisteredError


def _sample_components() -> list[dict[str, object]]:
    return [
        {
            "market_type": "KR",
            "symbol": "005930",
            "name": "삼성전자",
            "account_key": "live:kis",
            "broker": "kis",
            "account_name": "KIS 실계좌",
            "source": "live",
            "quantity": 10.0,
            "avg_price": 70000.0,
            "current_price": 75000.0,
            "evaluation": 750000.0,
            "profit_loss": 50000.0,
            "profit_rate": 0.0714,
        },
        {
            "market_type": "KR",
            "symbol": "005930",
            "name": "삼성전자",
            "account_key": "manual:1",
            "broker": "toss",
            "account_name": "토스 계좌",
            "source": "manual",
            "quantity": 5.0,
            "avg_price": 72000.0,
            "current_price": 75000.0,
            "evaluation": 375000.0,
            "profit_loss": 15000.0,
            "profit_rate": 0.0417,
        },
        {
            "market_type": "US",
            "symbol": "AAPL",
            "name": "Apple Inc.",
            "account_key": "manual:2",
            "broker": "samsung",
            "account_name": "미국주식",
            "source": "manual",
            "quantity": 2.0,
            "avg_price": 150.0,
            "current_price": 160.0,
            "evaluation": 320.0,
            "profit_loss": 20.0,
            "profit_rate": 0.0667,
        },
        {
            "market_type": "CRYPTO",
            "symbol": "KRW-BTC",
            "name": "KRW-BTC",
            "account_key": "live:upbit",
            "broker": "upbit",
            "account_name": "Upbit 실계좌",
            "source": "live",
            "quantity": 0.1,
            "avg_price": 100000000.0,
            "current_price": 110000000.0,
            "evaluation": 11000000.0,
            "profit_loss": 1000000.0,
            "profit_rate": 0.1,
        },
    ]


@pytest.mark.asyncio
async def test_get_overview_filters_by_selected_account_keys() -> None:
    service = PortfolioOverviewService(AsyncMock())
    components = _sample_components()

    service._collect_kis_components = AsyncMock(return_value=components[:1])
    service._collect_upbit_components = AsyncMock(return_value=components[3:])
    service._collect_manual_components = AsyncMock(return_value=components[1:3])
    service._fill_missing_prices = AsyncMock(return_value=None)

    overview = await service.get_overview(
        user_id=1,
        market="ALL",
        account_keys=["live:kis", "manual:1"],
        q=None,
    )

    assert overview["summary"]["total_positions"] == 1
    assert overview["summary"]["by_market"] == {"KR": 1, "US": 0, "CRYPTO": 0}
    position = overview["positions"][0]
    assert position["symbol"] == "005930"
    assert position["quantity"] == 15.0
    assert len(position["components"]) == 2


@pytest.mark.asyncio
async def test_get_overview_applies_market_and_q_filters() -> None:
    service = PortfolioOverviewService(AsyncMock())
    components = _sample_components()

    service._collect_kis_components = AsyncMock(return_value=components[:1])
    service._collect_upbit_components = AsyncMock(return_value=components[3:])
    service._collect_manual_components = AsyncMock(return_value=components[1:3])
    service._fill_missing_prices = AsyncMock(return_value=None)

    overview = await service.get_overview(
        user_id=1,
        market="US",
        account_keys=None,
        q="apple",
    )

    assert overview["filters"]["market"] == "US"
    assert overview["summary"]["total_positions"] == 1
    assert overview["summary"]["by_market"] == {"KR": 0, "US": 1, "CRYPTO": 0}
    assert overview["positions"][0]["symbol"] == "AAPL"


@pytest.mark.asyncio
async def test_get_overview_includes_deduplicated_warnings(monkeypatch) -> None:
    service = PortfolioOverviewService(AsyncMock())

    async def collect_kis(_kis_client, warnings):
        warnings.append("KIS warning")
        warnings.append("KIS warning")
        return []

    async def collect_upbit(
        warnings,
        active_upbit_markets=None,
        enforce_upbit_universe=True,
    ):
        _ = active_upbit_markets, enforce_upbit_universe
        warnings.append("Upbit warning")
        return []

    async def collect_manual(
        _user_id,
        warnings,
        active_upbit_markets=None,
        enforce_upbit_universe=True,
    ):
        _ = active_upbit_markets, enforce_upbit_universe
        warnings.append("KIS warning")
        return []

    service._collect_kis_components = collect_kis
    service._collect_upbit_components = collect_upbit
    service._collect_manual_components = collect_manual
    service._fill_missing_prices = AsyncMock(return_value=None)

    monkeypatch.setattr(
        portfolio_overview_module,
        "get_active_upbit_markets",
        AsyncMock(return_value={"KRW-BTC"}),
    )

    overview = await service.get_overview(user_id=1)
    assert overview["warnings"] == ["KIS warning", "Upbit warning"]


def test_aggregate_positions_recalculates_totals_when_some_components_missing_eval() -> (
    None
):
    service = PortfolioOverviewService(AsyncMock())

    rows = service._aggregate_positions(
        [
            {
                "market_type": "KR",
                "symbol": "005930",
                "name": "삼성전자",
                "account_key": "live:kis",
                "broker": "kis",
                "account_name": "KIS 실계좌",
                "source": "live",
                "quantity": 10.0,
                "avg_price": 70000.0,
                "current_price": 75000.0,
                "evaluation": 750000.0,
                "profit_loss": 50000.0,
                "profit_rate": 0.0714,
            },
            {
                "market_type": "KR",
                "symbol": "005930",
                "name": "삼성전자",
                "account_key": "manual:1",
                "broker": "toss",
                "account_name": "토스 계좌",
                "source": "manual",
                "quantity": 5.0,
                "avg_price": 72000.0,
                "current_price": None,
                "evaluation": None,
                "profit_loss": None,
                "profit_rate": None,
            },
        ]
    )

    assert len(rows) == 1
    assert rows[0]["quantity"] == 15.0
    # (10 + 5) * 75,000
    assert rows[0]["evaluation"] == 1125000.0
    # 1,125,000 - ((10 * 70,000) + (5 * 72,000))
    assert rows[0]["profit_loss"] == 65000.0


@pytest.mark.asyncio
async def test_fill_missing_prices_uses_us_provider_for_missing_us_prices(
    monkeypatch,
) -> None:
    service = PortfolioOverviewService(AsyncMock())

    monkeypatch.setattr(
        portfolio_overview_module,
        "get_us_exchange_by_symbol",
        AsyncMock(return_value="NASD"),
    )
    mock_fetch_price = AsyncMock(return_value=pd.DataFrame([{"close": 195.0}]))
    monkeypatch.setattr(
        portfolio_overview_module.yahoo_service,
        "fetch_price",
        mock_fetch_price,
    )

    components = [
        {
            "market_type": "US",
            "symbol": "AAPL",
            "name": "Apple",
            "account_key": "manual:1",
            "broker": "manual",
            "account_name": "US",
            "source": "manual",
            "quantity": 2.0,
            "avg_price": 150.0,
            "current_price": None,
            "evaluation": None,
            "profit_loss": None,
            "profit_rate": None,
        }
    ]
    warnings: list[str] = []

    await service._fill_missing_prices(AsyncMock(), components, warnings)

    mock_fetch_price.assert_awaited_once_with("AAPL")
    assert components[0]["current_price"] == 195.0
    assert components[0]["evaluation"] == 390.0
    assert components[0]["profit_loss"] == 90.0
    assert warnings == []


@pytest.mark.asyncio
async def test_fill_missing_prices_raises_on_us_yahoo_error(
    monkeypatch,
) -> None:
    service = PortfolioOverviewService(AsyncMock())

    monkeypatch.setattr(
        portfolio_overview_module,
        "get_us_exchange_by_symbol",
        AsyncMock(return_value="NASD"),
    )
    monkeypatch.setattr(
        portfolio_overview_module.yahoo_service,
        "fetch_price",
        AsyncMock(side_effect=RuntimeError("upstream timeout")),
    )

    components = [
        {
            "market_type": "US",
            "symbol": "AAPL",
            "name": "Apple",
            "account_key": "manual:1",
            "broker": "manual",
            "account_name": "US",
            "source": "manual",
            "quantity": 1.0,
            "avg_price": 200.0,
            "current_price": None,
            "evaluation": None,
            "profit_loss": None,
            "profit_rate": None,
        },
        {
            "market_type": "US",
            "symbol": "MSFT",
            "name": "Microsoft",
            "account_key": "manual:2",
            "broker": "manual",
            "account_name": "US",
            "source": "manual",
            "quantity": 1.0,
            "avg_price": 300.0,
            "current_price": None,
            "evaluation": None,
            "profit_loss": None,
            "profit_rate": None,
        },
    ]
    warnings: list[str] = []

    with pytest.raises(RuntimeError, match="upstream timeout"):
        await service._fill_missing_prices(AsyncMock(), components, warnings)

    assert components[0]["current_price"] is None
    assert components[1]["current_price"] is None
    assert warnings == []


@pytest.mark.asyncio
async def test_fill_missing_prices_filters_invalid_us_symbols_before_provider_fetch(
    monkeypatch,
) -> None:
    service = PortfolioOverviewService(AsyncMock())

    async def mock_get_us_exchange_by_symbol(symbol: str, db=None) -> str:
        if symbol == "AAPL":
            return "NASD"
        raise USSymbolNotRegisteredError("not registered")

    monkeypatch.setattr(
        portfolio_overview_module,
        "get_us_exchange_by_symbol",
        mock_get_us_exchange_by_symbol,
    )
    mock_fetch_price = AsyncMock(return_value=pd.DataFrame([{"close": 205.0}]))
    monkeypatch.setattr(
        portfolio_overview_module.yahoo_service,
        "fetch_price",
        mock_fetch_price,
    )

    components = [
        {
            "market_type": "US",
            "symbol": "AAPL",
            "name": "Apple",
            "account_key": "manual:1",
            "broker": "manual",
            "account_name": "US",
            "source": "manual",
            "quantity": 1.0,
            "avg_price": 200.0,
            "current_price": None,
            "evaluation": None,
            "profit_loss": None,
            "profit_rate": None,
        },
        {
            "market_type": "US",
            "symbol": "솔라나",
            "name": "솔라나",
            "account_key": "manual:2",
            "broker": "manual",
            "account_name": "US",
            "source": "manual",
            "quantity": 1.0,
            "avg_price": 1.0,
            "current_price": None,
            "evaluation": None,
            "profit_loss": None,
            "profit_rate": None,
        },
    ]
    warnings: list[str] = []

    await service._fill_missing_prices(AsyncMock(), components, warnings)

    mock_fetch_price.assert_awaited_once_with("AAPL")
    assert components[0]["current_price"] == 205.0
    assert components[1]["current_price"] is None
    assert warnings == []


@pytest.mark.asyncio
async def test_fetch_upbit_prices_resilient_recovers_with_tradable_filter(
    monkeypatch,
) -> None:
    service = PortfolioOverviewService(AsyncMock())
    warnings: list[str] = []

    calls: list[tuple[str, ...]] = []

    async def mock_fetch_multiple_current_prices(
        symbols: list[str],
    ) -> dict[str, float]:
        key = tuple(symbols)
        calls.append(key)
        if key == ("KRW-BTC", "KRW-ETH", "KRW-FAKE"):
            raise RuntimeError("404 not found")
        if key == ("KRW-BTC", "KRW-ETH"):
            return {"KRW-BTC": 100000000.0, "KRW-ETH": 5000000.0}
        raise AssertionError(f"unexpected symbols: {symbols}")

    monkeypatch.setattr(
        upbit_service,
        "fetch_multiple_current_prices",
        mock_fetch_multiple_current_prices,
    )
    monkeypatch.setattr(
        portfolio_overview_module,
        "get_active_upbit_markets",
        AsyncMock(return_value=["KRW-BTC", "KRW-ETH"]),
    )

    result = await service._fetch_upbit_prices_resilient(
        ["KRW-BTC", "KRW-ETH", "KRW-FAKE"],
        warnings,
        stage="collect_upbit_components",
    )

    assert result == {"KRW-BTC": 100000000.0, "KRW-ETH": 5000000.0}
    assert calls == [
        ("KRW-BTC", "KRW-ETH", "KRW-FAKE"),
        ("KRW-BTC", "KRW-ETH"),
    ]
    assert warnings == []


@pytest.mark.asyncio
async def test_fetch_upbit_prices_resilient_falls_back_to_single_symbol(
    monkeypatch,
) -> None:
    service = PortfolioOverviewService(AsyncMock())
    warnings: list[str] = []

    async def mock_fetch_multiple_current_prices(
        symbols: list[str],
    ) -> dict[str, float]:
        key = tuple(symbols)
        if key == ("KRW-BTC", "KRW-ETH"):
            raise RuntimeError("batch failed")
        if key == ("KRW-BTC",):
            return {"KRW-BTC": 110000000.0}
        if key == ("KRW-ETH",):
            raise RuntimeError("single failed")
        raise AssertionError(f"unexpected symbols: {symbols}")

    monkeypatch.setattr(
        upbit_service,
        "fetch_multiple_current_prices",
        mock_fetch_multiple_current_prices,
    )
    monkeypatch.setattr(
        portfolio_overview_module,
        "get_active_upbit_markets",
        AsyncMock(return_value=["KRW-BTC", "KRW-ETH"]),
    )

    result = await service._fetch_upbit_prices_resilient(
        ["KRW-BTC", "KRW-ETH"],
        warnings,
        stage="manual_crypto",
    )

    assert result == {"KRW-BTC": 110000000.0}
    assert warnings == [
        "Upbit price fetch failed (manual_crypto) for KRW-ETH: single failed"
    ]


@pytest.mark.asyncio
async def test_fetch_upbit_prices_resilient_raises_when_tradable_lookup_fails(
    monkeypatch,
) -> None:
    service = PortfolioOverviewService(AsyncMock())
    warnings: list[str] = []

    monkeypatch.setattr(
        upbit_service,
        "fetch_multiple_current_prices",
        AsyncMock(side_effect=RuntimeError("initial failed")),
    )
    monkeypatch.setattr(
        portfolio_overview_module,
        "get_active_upbit_markets",
        AsyncMock(side_effect=RuntimeError("upbit universe unavailable")),
    )

    with pytest.raises(RuntimeError, match="upbit universe unavailable"):
        await service._fetch_upbit_prices_resilient(
            ["KRW-BTC", "KRW-ETH"],
            warnings,
            stage="collect_upbit_components",
        )

    assert warnings == []


@pytest.mark.asyncio
async def test_fetch_upbit_prices_resilient_recovers_missing_symbol_from_retry_batch(
    monkeypatch,
) -> None:
    service = PortfolioOverviewService(AsyncMock())
    warnings: list[str] = []

    call_count = 0

    async def mock_fetch_multiple_current_prices(
        symbols: list[str],
    ) -> dict[str, float]:
        nonlocal call_count
        call_count += 1
        key = tuple(symbols)
        if call_count == 1 and key == ("KRW-BTC", "KRW-ETH"):
            raise RuntimeError("initial failed")
        if call_count == 2 and key == ("KRW-BTC", "KRW-ETH"):
            return {"KRW-BTC": 101000000.0}
        if key == ("KRW-ETH",):
            return {"KRW-ETH": 5100000.0}
        raise AssertionError(f"unexpected symbols: {symbols}")

    monkeypatch.setattr(
        upbit_service,
        "fetch_multiple_current_prices",
        mock_fetch_multiple_current_prices,
    )
    monkeypatch.setattr(
        portfolio_overview_module,
        "get_active_upbit_markets",
        AsyncMock(return_value=["KRW-BTC", "KRW-ETH"]),
    )

    result = await service._fetch_upbit_prices_resilient(
        ["KRW-BTC", "KRW-ETH"],
        warnings,
        stage="manual_crypto",
    )

    assert result == {"KRW-BTC": 101000000.0, "KRW-ETH": 5100000.0}
    assert warnings == []


@pytest.mark.asyncio
async def test_collect_upbit_components_uses_resilient_fetch_helper(
    monkeypatch,
) -> None:
    service = PortfolioOverviewService(AsyncMock())
    warnings: list[str] = []

    monkeypatch.setattr(
        upbit_service,
        "fetch_my_coins",
        AsyncMock(
            return_value=[
                {
                    "currency": "BTC",
                    "unit_currency": "KRW",
                    "balance": "0.1",
                    "locked": "0",
                    "avg_buy_price": "90000000",
                }
            ]
        ),
    )
    monkeypatch.setattr(
        portfolio_overview_module,
        "get_active_upbit_markets",
        AsyncMock(return_value=["KRW-BTC"]),
    )

    service._fetch_upbit_prices_resilient = AsyncMock(
        return_value={"KRW-BTC": 100000000.0}
    )

    components = await service._collect_upbit_components(warnings)

    assert len(components) == 1
    assert components[0]["symbol"] == "KRW-BTC"
    assert components[0]["current_price"] == 100000000.0
    assert components[0]["evaluation"] == 10000000.0
    service._fetch_upbit_prices_resilient.assert_awaited_once_with(
        ["KRW-BTC"],
        warnings,
        stage="collect_upbit_components",
        active_upbit_markets={"KRW-BTC"},
        enforce_upbit_universe=True,
    )


@pytest.mark.asyncio
async def test_fill_missing_prices_uses_resilient_fetch_helper_for_manual_crypto() -> (
    None
):
    service = PortfolioOverviewService(AsyncMock())
    service._fetch_upbit_prices_resilient = AsyncMock(
        return_value={"KRW-BTC": 115000000.0}
    )

    components = [
        {
            "market_type": "CRYPTO",
            "symbol": "KRW-BTC",
            "name": "KRW-BTC",
            "account_key": "manual:1",
            "broker": "manual",
            "account_name": "crypto",
            "source": "manual",
            "quantity": 0.2,
            "avg_price": 100000000.0,
            "current_price": None,
            "evaluation": None,
            "profit_loss": None,
            "profit_rate": None,
        }
    ]
    warnings: list[str] = []

    await service._fill_missing_prices(AsyncMock(), components, warnings)

    service._fetch_upbit_prices_resilient.assert_awaited_once_with(
        ["KRW-BTC"],
        warnings,
        stage="manual_crypto",
        active_upbit_markets=None,
        enforce_upbit_universe=True,
    )
    assert components[0]["current_price"] == 115000000.0
    assert components[0]["evaluation"] == 23000000.0
    assert components[0]["profit_loss"] == 3000000.0


@pytest.mark.asyncio
async def test_collect_manual_components_filters_non_tradable_crypto_symbols(
    monkeypatch,
) -> None:
    service = PortfolioOverviewService(AsyncMock())
    warnings: list[str] = []

    broker_account = SimpleNamespace(
        id=11,
        broker_type="toss",
        account_name="토스 코인",
    )
    holdings = [
        SimpleNamespace(
            market_type="CRYPTO",
            ticker="BTC",
            quantity=0.1,
            avg_price=100000000.0,
            display_name=None,
            broker_account=broker_account,
        ),
        SimpleNamespace(
            market_type="CRYPTO",
            ticker="FAKE",
            quantity=0.2,
            avg_price=1000.0,
            display_name=None,
            broker_account=broker_account,
        ),
    ]

    service.manual_holdings_service.get_holdings_by_user = AsyncMock(
        return_value=holdings
    )
    get_active_markets = AsyncMock(return_value=["KRW-BTC"])
    monkeypatch.setattr(
        portfolio_overview_module,
        "get_active_upbit_markets",
        get_active_markets,
    )

    components = await service._collect_manual_components(user_id=1, warnings=warnings)

    get_active_markets.assert_awaited_once_with(quote_currency=None)
    assert [item["symbol"] for item in components] == ["KRW-BTC"]
    assert warnings == []


@pytest.mark.asyncio
async def test_fill_missing_prices_manual_crypto_targets_manual_source_only() -> None:
    service = PortfolioOverviewService(AsyncMock())
    service._fetch_upbit_prices_resilient = AsyncMock(
        return_value={"KRW-BTC": 120000000.0}
    )

    components = [
        {
            "market_type": "CRYPTO",
            "symbol": "KRW-BTC",
            "name": "KRW-BTC",
            "account_key": "manual:1",
            "broker": "manual",
            "account_name": "manual",
            "source": "manual",
            "quantity": 0.2,
            "avg_price": 100000000.0,
            "current_price": None,
            "evaluation": None,
            "profit_loss": None,
            "profit_rate": None,
        },
        {
            "market_type": "CRYPTO",
            "symbol": "KRW-ETH",
            "name": "KRW-ETH",
            "account_key": "live:upbit",
            "broker": "upbit",
            "account_name": "Upbit",
            "source": "live",
            "quantity": 1.0,
            "avg_price": 4000000.0,
            "current_price": None,
            "evaluation": None,
            "profit_loss": None,
            "profit_rate": None,
        },
    ]
    warnings: list[str] = []

    await service._fill_missing_prices(AsyncMock(), components, warnings)

    service._fetch_upbit_prices_resilient.assert_awaited_once_with(
        ["KRW-BTC"],
        warnings,
        stage="manual_crypto",
        active_upbit_markets=None,
        enforce_upbit_universe=True,
    )


@pytest.mark.asyncio
async def test_get_overview_keeps_crypto_when_universe_lookup_fails(
    monkeypatch,
) -> None:
    service = PortfolioOverviewService(AsyncMock())

    async def collect_upbit(
        _warnings,
        active_upbit_markets=None,
        enforce_upbit_universe=True,
    ):
        assert active_upbit_markets is None
        assert enforce_upbit_universe is False
        return [
            {
                "market_type": "CRYPTO",
                "symbol": "KRW-BTC",
                "name": "KRW-BTC",
                "account_key": "live:upbit",
                "broker": "upbit",
                "account_name": "Upbit 실계좌",
                "source": "live",
                "quantity": 0.1,
                "avg_price": 100000000.0,
                "current_price": 101000000.0,
                "evaluation": 10100000.0,
                "profit_loss": 100000.0,
                "profit_rate": 0.01,
            }
        ]

    service._collect_kis_components = AsyncMock(return_value=[])
    service._collect_upbit_components = collect_upbit
    service._collect_manual_components = AsyncMock(return_value=[])
    service._fill_missing_prices = AsyncMock(return_value=None)

    monkeypatch.setattr(
        portfolio_overview_module,
        "get_active_upbit_markets",
        AsyncMock(side_effect=RuntimeError("universe offline")),
    )

    overview = await service.get_overview(user_id=1)

    assert [item["symbol"] for item in overview["positions"]] == ["KRW-BTC"]
    assert any(
        "Upbit universe lookup failed: universe offline" in warning
        for warning in overview["warnings"]
    )


@pytest.mark.asyncio
async def test_get_overview_excludes_non_tradable_manual_crypto_everywhere(
    monkeypatch,
) -> None:
    service = PortfolioOverviewService(AsyncMock())
    warnings: list[str] = []

    broker_account = SimpleNamespace(
        id=21,
        broker_type="samsung",
        account_name="수동 코인",
    )
    holdings = [
        SimpleNamespace(
            market_type="CRYPTO",
            ticker="BTC",
            quantity=0.1,
            avg_price=100000000.0,
            display_name=None,
            broker_account=broker_account,
        ),
        SimpleNamespace(
            market_type="CRYPTO",
            ticker="FAKE",
            quantity=1.0,
            avg_price=100.0,
            display_name=None,
            broker_account=broker_account,
        ),
    ]

    service.manual_holdings_service.get_holdings_by_user = AsyncMock(
        return_value=holdings
    )
    service._collect_kis_components = AsyncMock(return_value=[])
    service._collect_upbit_components = AsyncMock(return_value=[])
    service._fill_missing_prices = AsyncMock(return_value=None)

    monkeypatch.setattr(
        portfolio_overview_module,
        "get_active_upbit_markets",
        AsyncMock(return_value=["KRW-BTC"]),
    )

    overview = await service.get_overview(user_id=7)

    assert [row["symbol"] for row in overview["positions"]] == ["KRW-BTC"]
    assert overview["summary"]["total_positions"] == 1
    assert overview["summary"]["by_market"] == {"KR": 0, "US": 0, "CRYPTO": 1}
    assert overview["facets"]["accounts"] == [
        {
            "account_key": "manual:21",
            "broker": "samsung",
            "account_name": "수동 코인",
            "source": "manual",
            "market_types": ["CRYPTO"],
        }
    ]
    assert overview["warnings"] == warnings == []


@pytest.mark.unit
class TestAggregatePositions:
    """Test _aggregate_positions handles mixed-currency US positions."""

    def _make_service(self) -> PortfolioOverviewService:
        """Create service with a mock DB session."""
        from unittest.mock import MagicMock

        return PortfolioOverviewService(MagicMock())

    def test_us_mixed_source_uses_live_profit_rate(self):
        """Issue #327: Mixed KIS+manual US positions should use KIS profit_rate.

        KIS returns avg_price in USD, manual holdings store avg_price in KRW.
        Aggregation should prefer the live source's profit_rate over recalculating
        from mixed-currency cost_basis.
        """
        service = self._make_service()
        components = [
            # KIS live: NVDA, 5 shares, $150 avg, $160 current
            {
                "market_type": "US",
                "symbol": "NVDA",
                "name": "NVIDIA",
                "account_key": "live:kis",
                "broker": "kis",
                "account_name": "KIS",
                "source": "live",
                "quantity": 5,
                "avg_price": 150.0,  # USD
                "current_price": 160.0,  # USD
                "evaluation": 800.0,  # USD
                "profit_loss": 50.0,  # USD
                "profit_rate": 0.0667,  # Correct from KIS API
            },
            # Manual (Toss): NVDA, 5 shares, ₩200,000 avg (KRW!)
            {
                "market_type": "US",
                "symbol": "NVDA",
                "name": "NVIDIA",
                "account_key": "manual:1",
                "broker": "toss",
                "account_name": "Toss",
                "source": "manual",
                "quantity": 5,
                "avg_price": 200_000.0,  # KRW! (currency mismatch)
                "current_price": 160.0,  # USD (filled by _fill_missing_prices)
                "evaluation": 800.0,  # USD (recalculated)
                "profit_loss": -199_200.0,  # Wrong: 800 - 1_000_000
                "profit_rate": -0.9992,  # Wrong: mixed currencies
            },
        ]

        positions = service._aggregate_positions(components)
        nvda = next(p for p in positions if p["symbol"] == "NVDA")

        # The position profit_rate should NOT be deeply negative
        # With the fix, it should use the live source's profit_rate as basis
        # or at minimum not produce -99% due to currency mismatch
        assert nvda["profit_rate"] > -0.5, (
            f"Expected reasonable profit_rate, got {nvda['profit_rate']}"
        )

    def test_single_source_kr_unchanged(self):
        """KR positions from single source should work as before."""
        service = self._make_service()
        components = [
            {
                "market_type": "KR",
                "symbol": "005930",
                "name": "삼성전자",
                "account_key": "live:kis",
                "broker": "kis",
                "account_name": "KIS",
                "source": "live",
                "quantity": 100,
                "avg_price": 70_000.0,
                "current_price": 75_000.0,
                "evaluation": 7_500_000.0,
                "profit_loss": 500_000.0,
                "profit_rate": 0.0714,
            },
        ]

        positions = service._aggregate_positions(components)
        samsung = next(p for p in positions if p["symbol"] == "005930")
        assert abs(samsung["profit_rate"] - 0.0714) < 0.01

    def test_single_source_us_unchanged(self):
        """US positions from KIS only should work correctly."""
        service = self._make_service()
        components = [
            {
                "market_type": "US",
                "symbol": "AAPL",
                "name": "Apple",
                "account_key": "live:kis",
                "broker": "kis",
                "account_name": "KIS",
                "source": "live",
                "quantity": 10,
                "avg_price": 180.0,  # USD
                "current_price": 190.0,  # USD
                "evaluation": 1_900.0,  # USD
                "profit_loss": 100.0,  # USD
                "profit_rate": 0.0556,  # Correct
            },
        ]

        positions = service._aggregate_positions(components)
        aapl = next(p for p in positions if p["symbol"] == "AAPL")
        assert abs(aapl["profit_rate"] - 0.0556) < 0.01

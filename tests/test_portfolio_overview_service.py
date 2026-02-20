from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.services import upbit as upbit_service
from app.services.portfolio_overview_service import PortfolioOverviewService
from app.services.price_provider import PriceFetchError


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
async def test_get_overview_includes_deduplicated_warnings() -> None:
    service = PortfolioOverviewService(AsyncMock())

    async def collect_kis(_kis_client, warnings):
        warnings.append("KIS warning")
        warnings.append("KIS warning")
        return []

    async def collect_upbit(warnings):
        warnings.append("Upbit warning")
        return []

    async def collect_manual(_user_id, warnings):
        warnings.append("KIS warning")
        return []

    service._collect_kis_components = collect_kis
    service._collect_upbit_components = collect_upbit
    service._collect_manual_components = collect_manual
    service._fill_missing_prices = AsyncMock(return_value=None)

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
async def test_fill_missing_prices_uses_us_provider_for_missing_us_prices() -> None:
    us_provider = AsyncMock()
    us_provider.fetch_many = AsyncMock(return_value=({"AAPL": 195.0}, []))
    service = PortfolioOverviewService(AsyncMock(), us_price_provider=us_provider)

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

    us_provider.fetch_many.assert_awaited_once_with(["AAPL"])
    assert components[0]["current_price"] == 195.0
    assert components[0]["evaluation"] == 390.0
    assert components[0]["profit_loss"] == 90.0
    assert warnings == []


@pytest.mark.asyncio
async def test_fill_missing_prices_keeps_none_and_warning_on_us_provider_error() -> (
    None
):
    us_provider = AsyncMock()
    us_provider.fetch_many = AsyncMock(
        return_value=(
            {"AAPL": 210.0},
            [
                PriceFetchError(
                    symbol="MSFT",
                    source="yahoo",
                    error="upstream timeout",
                )
            ],
        )
    )
    service = PortfolioOverviewService(AsyncMock(), us_price_provider=us_provider)

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

    await service._fill_missing_prices(AsyncMock(), components, warnings)

    assert components[0]["current_price"] == 210.0
    assert components[1]["current_price"] is None
    assert warnings == ["US price fetch failed for MSFT via yahoo: upstream timeout"]


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
        upbit_service,
        "fetch_all_market_codes",
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
    assert warnings == [
        "Upbit price fetch failed (collect_upbit_components) for KRW-FAKE: symbol not tradable"
    ]


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
        upbit_service,
        "fetch_all_market_codes",
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
async def test_fetch_upbit_prices_resilient_warns_when_tradable_lookup_fails(
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
        upbit_service,
        "fetch_all_market_codes",
        AsyncMock(side_effect=RuntimeError("markets endpoint down")),
    )

    result = await service._fetch_upbit_prices_resilient(
        ["KRW-BTC", "KRW-ETH"],
        warnings,
        stage="collect_upbit_components",
    )

    assert result == {}
    assert warnings == [
        "Upbit price fetch failed (collect_upbit_components) for KRW-BTC: markets endpoint down",
        "Upbit price fetch failed (collect_upbit_components) for KRW-ETH: markets endpoint down",
    ]


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
        upbit_service,
        "fetch_all_market_codes",
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
    )


@pytest.mark.asyncio
async def test_fill_missing_prices_uses_resilient_fetch_helper_for_manual_crypto() -> (
    None
):
    service = PortfolioOverviewService(AsyncMock(), us_price_provider=AsyncMock())
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
    )
    assert components[0]["current_price"] == 115000000.0
    assert components[0]["evaluation"] == 23000000.0
    assert components[0]["profit_loss"] == 3000000.0

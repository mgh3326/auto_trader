"""Unit tests for app/services/portfolio_data_collector.PortfolioDataCollector."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.portfolio_data_collector import PortfolioDataCollector

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_collector():
    db = MagicMock()
    return PortfolioDataCollector(db)


def _make_kis_kr_stock(**kwargs):
    defaults = {
        "hldg_qty": "10",
        "pchs_avg_pric": "50000",
        "prpr": "55000",
        "evlu_amt": "550000",
        "evlu_pfls_amt": "50000",
        "evlu_pfls_rt": "10.00",
        "pdno": "005930",
        "prdt_name": "삼성전자",
    }
    defaults.update(kwargs)
    return defaults


def _make_kis_us_stock(**kwargs):
    defaults = {
        "ovrs_cblc_qty": "5",
        "pchs_avg_pric": "150.00",
        "now_pric2": "170.00",
        "ovrs_stck_evlu_amt": "850.00",
        "frcr_evlu_pfls_amt": "100.00",
        "evlu_pfls_rt": "13.33",
        "ovrs_pdno": "AAPL",
        "ovrs_item_name": "Apple Inc",
    }
    defaults.update(kwargs)
    return defaults


# ---------------------------------------------------------------------------
# _collect_kis_kr_components
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_collect_kis_kr_returns_component_for_held_stock():
    collector = _make_collector()
    kis_client = AsyncMock()
    kis_client.fetch_my_stocks.return_value = [_make_kis_kr_stock()]
    warnings: list[str] = []

    components, w = await collector._collect_kis_kr_components(kis_client, warnings)

    assert len(components) == 1
    comp = components[0]
    assert comp["market_type"] == "KR"
    assert comp["symbol"] == "005930"
    assert comp["broker"] == "kis"
    assert comp["quantity"] == 10.0
    assert w == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_collect_kis_kr_skips_zero_quantity():
    collector = _make_collector()
    kis_client = AsyncMock()
    kis_client.fetch_my_stocks.return_value = [_make_kis_kr_stock(hldg_qty="0")]
    warnings: list[str] = []

    components, w = await collector._collect_kis_kr_components(kis_client, warnings)

    assert components == []
    assert w == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_collect_kis_kr_appends_warning_on_failure():
    collector = _make_collector()
    kis_client = AsyncMock()
    kis_client.fetch_my_stocks.side_effect = RuntimeError("network error")
    warnings: list[str] = []

    components, w = await collector._collect_kis_kr_components(kis_client, warnings)

    assert components == []
    assert len(w) == 1
    assert "KIS KR" in w[0]


# ---------------------------------------------------------------------------
# _collect_kis_us_components
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_collect_kis_us_returns_component_for_held_stock():
    collector = _make_collector()
    kis_client = AsyncMock()
    kis_client.fetch_my_us_stocks.return_value = [_make_kis_us_stock()]
    warnings: list[str] = []

    components, w = await collector._collect_kis_us_components(kis_client, warnings)

    assert len(components) == 1
    comp = components[0]
    assert comp["market_type"] == "US"
    assert comp["symbol"] == "AAPL"
    assert comp["broker"] == "kis"
    assert w == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_collect_kis_us_skips_zero_quantity():
    collector = _make_collector()
    kis_client = AsyncMock()
    kis_client.fetch_my_us_stocks.return_value = [_make_kis_us_stock(ovrs_cblc_qty="0")]
    warnings: list[str] = []

    components, w = await collector._collect_kis_us_components(kis_client, warnings)

    assert components == []
    assert w == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_collect_kis_us_appends_warning_on_failure():
    collector = _make_collector()
    kis_client = AsyncMock()
    kis_client.fetch_my_us_stocks.side_effect = RuntimeError("timeout")
    warnings: list[str] = []

    components, w = await collector._collect_kis_us_components(kis_client, warnings)

    assert components == []
    assert len(w) == 1
    assert "KIS US" in w[0]


# ---------------------------------------------------------------------------
# _collect_upbit_components
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_collect_upbit_appends_warning_on_fetch_failure():
    collector = _make_collector()
    warnings: list[str] = []

    with patch(
        "app.services.portfolio_data_collector.upbit_service.fetch_my_coins",
        side_effect=RuntimeError("upbit down"),
    ):
        components = await collector._collect_upbit_components(
            warnings, active_upbit_markets=None, enforce_upbit_universe=False
        )

    assert components == []
    assert len(warnings) == 1
    assert "Upbit" in warnings[0]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_collect_upbit_skips_krw_currency():
    collector = _make_collector()
    warnings: list[str] = []

    with patch(
        "app.services.portfolio_data_collector.upbit_service.fetch_my_coins",
        return_value=[
            {
                "currency": "KRW",
                "balance": "100000",
                "locked": "0",
                "avg_buy_price": "1",
            }
        ],
    ):
        components = await collector._collect_upbit_components(
            warnings, active_upbit_markets=None, enforce_upbit_universe=False
        )

    assert components == []


# ---------------------------------------------------------------------------
# _collect_manual_components
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_collect_manual_appends_warning_on_failure():
    collector = _make_collector()
    collector.manual_holdings_service = AsyncMock()
    collector.manual_holdings_service.get_holdings_by_user.side_effect = RuntimeError(
        "db error"
    )
    warnings: list[str] = []

    components = await collector._collect_manual_components(
        user_id=1,
        warnings=warnings,
        active_upbit_markets=None,
        enforce_upbit_universe=False,
    )

    assert components == []
    assert len(warnings) == 1
    assert "Manual" in warnings[0]


# ---------------------------------------------------------------------------
# _run_collection_task
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_run_collection_task_returns_empty_list_on_exception():
    collector = _make_collector()

    async def _failing_func(warnings):
        raise ValueError("boom")

    result, w = await collector._run_collection_task(_failing_func)
    assert result == []
    assert len(w) == 1
    assert "boom" in w[0]

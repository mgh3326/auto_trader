"""Tests for KIS market adapter refactor W2-6.

Verifies that OverseasAutomationAdapter.fetch_open_orders uses
BaseAutomationAdapter._extract_order_number instead of the removed
_extract_order_id static method.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.jobs.kis_market_adapters import (
    DomesticAutomationAdapter,
    OverseasAutomationAdapter,
)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _base_adapter_kwargs(**overrides):
    """Return a minimal set of kwargs to construct any AutomationAdapter."""
    defaults = {
        "kis_client_factory": MagicMock,
        "async_session_factory": MagicMock,
        "manual_holdings_service_factory": MagicMock,
        "manual_market_type": None,
        "buy_handler": AsyncMock(),
        "sell_handler": AsyncMock(),
        "send_toss_recommendation": AsyncMock(),
        "notifier_factory": MagicMock,
        "no_stocks_message": "보유 종목 없음",
    }
    defaults.update(overrides)
    return defaults


def _make_overseas_adapter(**kwargs):
    return OverseasAutomationAdapter(**_base_adapter_kwargs(**kwargs))


def _make_domestic_adapter(**kwargs):
    return DomesticAutomationAdapter(**_base_adapter_kwargs(**kwargs))


# ---------------------------------------------------------------------------
# Task 2 / Task 4: _extract_order_id must NOT exist on OverseasAutomationAdapter
# ---------------------------------------------------------------------------


class TestExtractOrderIdRemoved:
    def test_overseas_adapter_has_no_extract_order_id(self):
        """_extract_order_id must be removed; only _extract_order_number remains."""
        adapter = _make_overseas_adapter()
        assert not hasattr(adapter, "_extract_order_id"), (
            "_extract_order_id was not removed from OverseasAutomationAdapter"
        )

    def test_extract_order_number_on_base_still_works(self):
        """_extract_order_number on BaseAutomationAdapter returns expected value."""
        adapter = _make_overseas_adapter()
        assert adapter._extract_order_number({"odno": "12345"}) == "12345"
        assert adapter._extract_order_number({"ODNO": "99"}) == "99"
        assert adapter._extract_order_number({"ord_no": "77"}) == "77"
        assert adapter._extract_order_number({"ORD_NO": "55"}) == "55"
        assert adapter._extract_order_number({}) is None


# ---------------------------------------------------------------------------
# Task 2 / Task 4: fetch_open_orders dedup logic
# ---------------------------------------------------------------------------


class TestOverseasFetchOpenOrders:
    @pytest.mark.asyncio
    async def test_dedup_by_odno(self):
        """Orders with same odno from different exchanges are deduplicated."""
        order_nasd = {"odno": "111", "pdno": "AAPL", "ovrs_excg_cd": "NASD"}
        order_nyse = {"odno": "111", "pdno": "AAPL", "ovrs_excg_cd": "NYSE"}  # dup
        order_amex = {"odno": "222", "pdno": "TSLA", "ovrs_excg_cd": "AMEX"}

        kis = MagicMock()
        kis.inquire_overseas_orders = AsyncMock(
            side_effect=[
                [order_nasd],
                [order_nyse],
                [order_amex],
            ]
        )

        adapter = _make_overseas_adapter()
        result = await adapter.fetch_open_orders(kis)

        # odno=111 deduplicated → only 2 unique orders
        assert len(result) == 2
        odnos = [o.get("odno") for o in result]
        assert odnos.count("111") == 1
        assert "222" in odnos

    @pytest.mark.asyncio
    async def test_orders_without_odno_go_to_anonymous(self):
        """Orders with no extractable id are appended at the end."""
        order_with_id = {"odno": "100", "pdno": "AAPL"}
        order_no_id = {"pdno": "MSFT"}  # no order number keys

        kis = MagicMock()
        kis.inquire_overseas_orders = AsyncMock(
            side_effect=[
                [order_with_id],
                [order_no_id],
                [],
            ]
        )

        adapter = _make_overseas_adapter()
        result = await adapter.fetch_open_orders(kis)

        assert len(result) == 2
        # anonymous order should be the last
        assert result[-1] == order_no_id

    @pytest.mark.asyncio
    async def test_exchange_failure_skipped_gracefully(self):
        """A failing exchange does not abort the whole call."""
        order_amex = {"odno": "300", "pdno": "SPY"}

        kis = MagicMock()
        kis.inquire_overseas_orders = AsyncMock(
            side_effect=[
                Exception("timeout"),
                Exception("timeout"),
                [order_amex],
            ]
        )

        adapter = _make_overseas_adapter()
        result = await adapter.fetch_open_orders(kis)
        assert len(result) == 1
        assert result[0]["odno"] == "300"

    @pytest.mark.asyncio
    async def test_uses_extract_order_number_keys(self):
        """ORD_NO / ODNO uppercase keys are also recognised."""
        order_upper = {"ODNO": "AAA", "pdno": "GOOG"}

        kis = MagicMock()
        kis.inquire_overseas_orders = AsyncMock(side_effect=[[order_upper], [], []])

        adapter = _make_overseas_adapter()
        result = await adapter.fetch_open_orders(kis)
        assert len(result) == 1
        assert result[0]["ODNO"] == "AAA"


# ---------------------------------------------------------------------------
# Task 2 / Task 4: _cancel_single_order exchange_code default
# ---------------------------------------------------------------------------


class TestOverseasCancelSingleOrder:
    @pytest.mark.asyncio
    async def test_exchange_code_defaults_to_nasd(self):
        """When exchange_code is None, cancel_overseas_order receives NASD."""
        kis = MagicMock()
        kis.cancel_overseas_order = AsyncMock()

        adapter = _make_overseas_adapter()
        order = {"ft_ord_qty": "10"}
        await adapter._cancel_single_order(
            kis, "AAPL", order, "001", "buy", exchange_code=None
        )

        kis.cancel_overseas_order.assert_awaited_once()
        call_kwargs = kis.cancel_overseas_order.call_args.kwargs
        assert call_kwargs["exchange_code"] == "NASD"


# ---------------------------------------------------------------------------
# Task 2 / Task 4: execute() mock integration — both markets
# ---------------------------------------------------------------------------


def _make_async_session_factory():
    """Build an async_session_factory whose context manager returns a mock db."""
    mock_db = MagicMock()
    mock_service = MagicMock()
    mock_service.get_holdings_by_user = AsyncMock(return_value=[])

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=mock_db)
    cm.__aexit__ = AsyncMock(return_value=False)

    session_factory = MagicMock(return_value=cm)
    manual_service_factory = MagicMock(return_value=mock_service)
    return session_factory, manual_service_factory


class TestExecuteIntegration:
    """Smoke-level integration: execute() must return status=completed for both."""

    @pytest.mark.asyncio
    async def test_domestic_execute_returns_completed_when_no_stocks(self):
        kis = MagicMock()
        kis.fetch_my_stocks = AsyncMock(return_value=[])
        kis.inquire_korea_orders = AsyncMock(return_value=[])

        session_factory, manual_service_factory = _make_async_session_factory()
        adapter = _make_domestic_adapter(
            kis_client_factory=lambda: kis,
            async_session_factory=session_factory,
            manual_holdings_service_factory=manual_service_factory,
        )
        result = await adapter.execute()
        assert result["status"] == "completed"

    @pytest.mark.asyncio
    async def test_overseas_execute_returns_completed_when_no_stocks(self):
        kis = MagicMock()
        kis.fetch_my_overseas_stocks = AsyncMock(return_value=[])
        kis.inquire_overseas_orders = AsyncMock(return_value=[])

        session_factory, manual_service_factory = _make_async_session_factory()
        adapter = _make_overseas_adapter(
            kis_client_factory=lambda: kis,
            async_session_factory=session_factory,
            manual_holdings_service_factory=manual_service_factory,
        )
        result = await adapter.execute()
        assert result["status"] == "completed"

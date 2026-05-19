from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.schemas.us_action_report import KISUSAccountSnapshot
from app.services.action_report.us.account_snapshot import build_kis_us_account_snapshot


class _FakeAccount:
    def __init__(self, margin_rows=None, *, raises: Exception | None = None):
        self.margin_rows = margin_rows or [
            {
                "crcy_cd": "USD",
                "natn_name": "미국",
                "frcr_dncl_amt1": "1234.56",
                "frcr_ord_psbl_amt1": "1200.00",
            }
        ]
        self.raises = raises

    async def inquire_overseas_margin(self):
        if self.raises is not None:
            raise self.raises
        return self.margin_rows


class _FakeKISClient:
    def __init__(
        self, *, rows=None, margin_error: Exception | None = None, orders=None
    ):
        self.account = _FakeAccount(raises=margin_error)
        self.rows = (
            rows
            if rows is not None
            else [
                {
                    "ovrs_pdno": "AAPL",
                    "ovrs_item_name": "APPLE",
                    "ovrs_cblc_qty": "10",
                    "pchs_avg_pric": "150.0",
                    "now_pric2": "200.0",
                    "natn_cd": "840",
                    "natn_kor_name": "미국",
                }
            ]
        )
        self.orders = orders or []
        self.calls: list[str] = []

    async def fetch_my_overseas_stocks(self):
        self.calls.append("fetch_my_overseas_stocks")
        return self.rows

    async def inquire_overseas_orders(self, exchange_code="NASD", is_mock=False):
        self.calls.append(f"inquire_overseas_orders:{exchange_code}:{is_mock}")
        return self.orders if exchange_code == "NASD" else []

    async def order_overseas_stock(
        self, *args, **kwargs
    ):  # pragma: no cover - must never run
        raise AssertionError("live order endpoint must not be called")

    async def buy_overseas_stock(
        self, *args, **kwargs
    ):  # pragma: no cover - must never run
        raise AssertionError("live buy endpoint must not be called")

    async def sell_overseas_stock(
        self, *args, **kwargs
    ):  # pragma: no cover - must never run
        raise AssertionError("live sell endpoint must not be called")

    async def cancel_overseas_order(self, *args, **kwargs):  # pragma: no cover
        raise AssertionError("live cancel endpoint must not be called")

    async def modify_overseas_order(self, *args, **kwargs):  # pragma: no cover
        raise AssertionError("live modify endpoint must not be called")


class _FakeQuoteService:
    def __init__(self, *, raises: Exception | None = None):
        self.raises = raises
        self.calls: list[str] = []

    async def get_us_quote(self, symbol):
        self.calls.append(symbol)
        if self.raises is not None:
            raise self.raises
        return SimpleNamespace(price=200.0, state="live")


@pytest.mark.asyncio
async def test_snapshot_does_not_call_order_mutation_endpoints():
    client = _FakeKISClient(
        orders=[
            {
                "ovrs_pdno": "AAPL",
                "sll_buy_dvsn_cd": "01",
                "nccs_qty": "2",
                "odno": "O-1",
            }
        ]
    )
    quote = _FakeQuoteService()

    snap = await build_kis_us_account_snapshot(
        kis_client=client,
        quote_service=quote,
        now=lambda: datetime(2026, 5, 14, 12, 0, tzinfo=UTC),
    )

    assert isinstance(snap, KISUSAccountSnapshot)
    assert snap.captured_at == datetime(2026, 5, 14, 12, 0, tzinfo=UTC)
    assert snap.source == "kis_live"
    assert snap.source_of_truth is True
    assert snap.is_tradeable is True
    assert snap.manual_only is False
    assert snap.usd_cash == 1234.56
    assert snap.usd_buying_power == 1200.0
    assert len(snap.open_orders) == 1
    assert snap.open_orders[0].pending_qty == 2.0

    assert len(snap.holdings) == 1
    h = snap.holdings[0]
    assert h.symbol == "AAPL"
    assert h.display_name == "APPLE"
    assert h.quantity == 10.0
    assert h.average_cost_usd == 150.0
    assert h.cost_basis_usd == 1500.0
    assert h.last_price_usd == 200.0
    assert h.value_usd == 2000.0
    assert h.pnl_usd == 500.0
    assert h.pnl_rate == pytest.approx(33.3333333333)
    assert h.source_of_truth is True
    assert h.is_tradeable is True
    assert h.manual_only is False
    assert h.sellable_qty == 8.0
    assert h.pending_qty == 2.0
    assert client.calls == [
        "inquire_overseas_orders:NASD:False",
        "inquire_overseas_orders:NYSE:False",
        "inquire_overseas_orders:AMEX:False",
        "fetch_my_overseas_stocks",
    ]
    assert quote.calls == ["AAPL"]


@pytest.mark.asyncio
async def test_margin_failure_returns_snapshot_with_warning():
    snap = await build_kis_us_account_snapshot(
        kis_client=_FakeKISClient(margin_error=RuntimeError("margin down")),
        quote_service=_FakeQuoteService(),
        now=lambda: datetime(2026, 5, 14, tzinfo=UTC),
    )

    assert snap.usd_cash is None
    assert snap.usd_buying_power is None
    assert any("margin down" in warning for warning in snap.warnings)
    assert len(snap.holdings) == 1


@pytest.mark.asyncio
async def test_quote_failure_marks_price_state_missing_and_nulls_quote_derived_values():
    snap = await build_kis_us_account_snapshot(
        kis_client=_FakeKISClient(),
        quote_service=_FakeQuoteService(raises=RuntimeError("quote down")),
        now=lambda: datetime(2026, 5, 14, tzinfo=UTC),
    )

    holding = snap.holdings[0]
    assert holding.price_state == "missing"
    assert holding.last_price_usd is None
    assert holding.value_usd is None
    assert holding.pnl_usd is None
    assert holding.pnl_rate is None
    assert any("quote down" in warning for warning in snap.warnings)


@pytest.mark.asyncio
async def test_manual_toss_like_duplicate_symbol_is_not_merged_or_tradeable():
    client = _FakeKISClient(
        rows=[
            {
                "ovrs_pdno": "AAPL",
                "ovrs_item_name": "APPLE KIS",
                "ovrs_cblc_qty": "3",
                "pchs_avg_pric": "100",
                "natn_cd": "840",
            },
            {
                "ticker": "AAPL",
                "name": "APPLE TOSS",
                "quantity": "99",
                "broker_type": "toss",
                "source": "toss_manual",
                "market": "US",
            },
        ]
    )

    snap = await build_kis_us_account_snapshot(
        kis_client=client,
        quote_service=_FakeQuoteService(),
        now=lambda: datetime(2026, 5, 14, tzinfo=UTC),
    )

    assert [holding.display_name for holding in snap.holdings] == ["APPLE KIS"]
    holding = snap.holdings[0]
    assert holding.quantity == 3.0
    assert holding.sellable_qty == 3.0
    assert holding.source_of_truth is True
    assert holding.is_tradeable is True
    assert holding.manual_only is False


@pytest.mark.asyncio
async def test_mixed_overseas_rows_keep_us_and_drop_non_us():
    client = _FakeKISClient(
        rows=[
            {"ovrs_pdno": "MSFT", "ovrs_cblc_qty": "1", "natn_cd": "840"},
            {"ovrs_pdno": "7203", "ovrs_cblc_qty": "2", "natn_name": "일본"},
            {"ovrs_pdno": "BRK/B", "ovrs_cblc_qty": "4", "natn_name": "USA"},
        ]
    )

    snap = await build_kis_us_account_snapshot(
        kis_client=client,
        quote_service=_FakeQuoteService(),
        now=lambda: datetime(2026, 5, 14, tzinfo=UTC),
    )

    assert [holding.symbol for holding in snap.holdings] == ["MSFT", "BRK.B"]

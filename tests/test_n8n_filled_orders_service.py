"""Tests for filled-orders aggregation service."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.timezone import now_kst


@pytest.mark.unit
class TestKISOverseasFilledOrdersFetch:
    @pytest.mark.asyncio
    async def test_uses_single_us_wide_history_query_and_dedupes(self, monkeypatch):
        from app.services import n8n_filled_orders_service as svc

        fake_kis = MagicMock()
        fake_kis.inquire_daily_order_overseas = AsyncMock(
            return_value=[
                {
                    "odno": "US-1",
                    "pdno": "UBER",
                    "sll_buy_dvsn_cd": "01",
                    "ft_ccld_qty": "4",
                    "ft_ccld_unpr3": "77.37",
                    "ft_ccld_amt3": "309.48",
                    "ord_dt": "20260506",
                    "ord_tmd": "230005",
                },
                {
                    "odno": "US-1",
                    "pdno": "UBER",
                    "sll_buy_dvsn_cd": "01",
                    "ft_ccld_qty": "4",
                    "ft_ccld_unpr3": "77.37",
                    "ft_ccld_amt3": "309.48",
                    "ord_dt": "20260506",
                    "ord_tmd": "230005",
                },
            ]
        )
        monkeypatch.setattr(svc, "KISClient", lambda: fake_kis)

        orders, errors = await svc._fetch_kis_overseas_filled(days=7)

        assert errors == []
        assert [order["order_id"] for order in orders] == ["US-1"]
        fake_kis.inquire_daily_order_overseas.assert_awaited_once()
        assert (
            fake_kis.inquire_daily_order_overseas.await_args.kwargs["exchange_code"]
            == "NASD"
        )
        assert fake_kis.inquire_daily_order_overseas.await_args.kwargs["symbol"] == "%"

    @pytest.mark.asyncio
    async def test_multiple_fills_same_order_id_different_fill_seq_are_all_kept(
        self, monkeypatch
    ):
        """Issue 3 regression: dedup must be by (order_id, fill_seq) not order_id alone."""
        from app.services import n8n_filled_orders_service as svc

        # Two rows with same order_id but different execution times → different hash fill_seq
        fake_kis = MagicMock()
        fake_kis.inquire_daily_order_overseas = AsyncMock(
            return_value=[
                {
                    "odno": "PARTIAL-ORDER",
                    "pdno": "MSFT",
                    "sll_buy_dvsn_cd": "02",
                    "ft_ccld_qty": "5",
                    "ft_ccld_unpr3": "420.00",
                    "ft_ccld_amt3": "2100.00",
                    "ord_dt": "20260513",
                    "ord_tmd": "090000",  # different time → different hash
                },
                {
                    "odno": "PARTIAL-ORDER",
                    "pdno": "MSFT",
                    "sll_buy_dvsn_cd": "02",
                    "ft_ccld_qty": "3",
                    "ft_ccld_unpr3": "421.00",
                    "ft_ccld_amt3": "1263.00",
                    "ord_dt": "20260513",
                    "ord_tmd": "091500",  # different time → different hash
                },
            ]
        )
        monkeypatch.setattr(svc, "KISClient", lambda: fake_kis)

        orders, errors = await svc._fetch_kis_overseas_filled(days=7)

        assert errors == []
        # Both fills must be kept because they have different fill_seq
        assert len(orders) == 2
        assert all(o["order_id"] == "PARTIAL-ORDER" for o in orders)
        assert orders[0]["fill_seq"] != orders[1]["fill_seq"]


@pytest.mark.unit
class TestUpbitFilledOrdersFetch:
    @pytest.mark.asyncio
    async def test_cancel_with_partial_fill_is_accepted(self, monkeypatch):
        """Issue 1 regression: cancelled orders with executed_volume > 0 must not be dropped."""
        from app.services import n8n_filled_orders_service as svc

        recent_ts = (now_kst() - timedelta(hours=1)).isoformat()
        fake_order = {
            "state": "cancel",
            "market": "KRW-ETH",
            "side": "bid",
            "executed_volume": "0.5",
            "price": "3000000",
            "avg_price": "3000000",
            "paid_fee": "750",
            "uuid": "cancel-partial-uuid",
            "created_at": recent_ts,
            "trades": [
                {
                    "uuid": "trade-cancel-p",
                    "volume": "0.5",
                    "funds": "1500000",
                    "created_at": recent_ts,
                }
            ],
        }

        fake_upbit = MagicMock()
        fake_upbit.fetch_closed_orders = AsyncMock(return_value=[fake_order])
        fake_upbit.fetch_order_detail = AsyncMock(return_value=fake_order)
        monkeypatch.setattr(svc, "upbit_service", fake_upbit)

        orders, errors = await svc._fetch_upbit_filled(days=1)

        assert errors == []
        assert len(orders) == 1
        assert orders[0]["symbol"] == "ETH"
        assert orders[0]["quantity"] == 0.5

    @pytest.mark.asyncio
    async def test_pagination_fetches_multiple_pages(self, monkeypatch):
        """Issue 2 regression: pagination must continue until an empty or out-of-window page."""
        from app.services import n8n_filled_orders_service as svc

        recent_ts = (now_kst() - timedelta(hours=1)).isoformat()

        def _make_order(uuid_val: str) -> dict:
            return {
                "state": "done",
                "market": "KRW-BTC",
                "side": "bid",
                "executed_volume": "0.01",
                "price": "100000000",
                "avg_price": "100000000",
                "paid_fee": "500",
                "uuid": uuid_val,
                "created_at": recent_ts,
                "trades": [
                    {
                        "uuid": f"trade-{uuid_val}",
                        "volume": "0.01",
                        "funds": "1000000",
                        "created_at": recent_ts,
                    }
                ],
            }

        # Page 1: full page (100 orders) → trigger page 2
        page1 = [_make_order(f"order-p1-{i}") for i in range(100)]
        # Page 2: partial page (3 orders) → last page, stop
        page2 = [_make_order(f"order-p2-{i}") for i in range(3)]

        call_count = 0

        async def fake_fetch_closed(market, limit, page, **_kw):
            nonlocal call_count
            call_count += 1
            return page1 if page == 1 else page2

        fake_upbit = MagicMock()
        fake_upbit.fetch_closed_orders = fake_fetch_closed
        fake_upbit.fetch_order_detail = AsyncMock(
            side_effect=lambda uuid: _make_order(uuid)
        )
        monkeypatch.setattr(svc, "upbit_service", fake_upbit)

        orders, errors = await svc._fetch_upbit_filled(days=1)

        assert errors == []
        # Both pages must be consumed
        assert call_count == 2
        # 100 + 3 orders × 1 trade each = 103 fills
        assert len(orders) == 103

    @pytest.mark.asyncio
    async def test_pagination_stops_when_page_is_out_of_window(self, monkeypatch):
        """Pagination must stop once a page contains no orders within the time window."""
        from app.services import n8n_filled_orders_service as svc

        recent_ts = (now_kst() - timedelta(hours=1)).isoformat()
        old_ts = (now_kst() - timedelta(days=10)).isoformat()

        def _make_order(uuid_val: str, ts: str) -> dict:
            return {
                "state": "done",
                "market": "KRW-BTC",
                "side": "bid",
                "executed_volume": "0.01",
                "price": "100000000",
                "avg_price": "100000000",
                "paid_fee": "500",
                "uuid": uuid_val,
                "created_at": ts,
                "trades": [
                    {
                        "uuid": f"trade-{uuid_val}",
                        "volume": "0.01",
                        "funds": "1000000",
                        "created_at": ts,
                    }
                ],
            }

        page1 = [_make_order("order-recent", recent_ts)] * 100  # full page, in window
        page2 = [_make_order("order-old", old_ts)] * 5  # all out of window → stop

        call_count = 0

        async def fake_fetch_closed(market, limit, page, **_kw):
            nonlocal call_count
            call_count += 1
            return page1 if page == 1 else page2

        fake_upbit = MagicMock()
        fake_upbit.fetch_closed_orders = fake_fetch_closed
        fake_upbit.fetch_order_detail = AsyncMock(
            side_effect=lambda uuid: _make_order(
                uuid, recent_ts if "recent" in uuid else old_ts
            )
        )
        monkeypatch.setattr(svc, "upbit_service", fake_upbit)

        orders, errors = await svc._fetch_upbit_filled(days=1)

        assert errors == []
        assert call_count == 2  # stopped after finding out-of-window page
        # Only page-1 orders are within the window
        assert len(orders) == 100

    @pytest.mark.asyncio
    async def test_detail_fetch_failure_falls_back_to_aggregate_fill(self, monkeypatch):
        """When order detail fetch fails, the aggregate fill (no trades) should be returned."""
        from app.services import n8n_filled_orders_service as svc

        recent_ts = (now_kst() - timedelta(hours=1)).isoformat()
        raw_order = {
            "state": "done",
            "market": "KRW-BTC",
            "side": "ask",
            "executed_volume": "0.02",
            "price": "50000000",
            "avg_price": "50000000",
            "paid_fee": "500",
            "uuid": "order-no-detail",
            "created_at": recent_ts,
            "trades": [],  # no trades in list response
        }

        fake_upbit = MagicMock()
        fake_upbit.fetch_closed_orders = AsyncMock(return_value=[raw_order])
        fake_upbit.fetch_order_detail = AsyncMock(side_effect=RuntimeError("API error"))
        monkeypatch.setattr(svc, "upbit_service", fake_upbit)

        orders, errors = await svc._fetch_upbit_filled(days=1)

        assert errors == []
        # Falls back to aggregate fill (fill_seq=0, full executed_volume)
        assert len(orders) == 1
        assert orders[0]["fill_seq"] == 0
        assert orders[0]["quantity"] == 0.02

"""Tests for filled-orders aggregation service."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


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

"""ROB-645: KIS order-submission callsites must never re-POST a timed-out order.

Each order-mutation path passes ``retry_request_errors=False`` (no RequestError
retry) and ``max_retries_override=0`` (no EGW00215/'초과'/429 re-POST) to the shared
transport, so a single order request reaches the broker exactly once.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest


def _make_parent():
    parent = MagicMock()
    parent._ensure_token = AsyncMock()
    parent._hdr_base = {}
    parent._kis_url = lambda path: f"https://host{path}"
    settings = MagicMock()
    settings.kis_account_no = "1234567890"
    settings.kis_access_token = "test-token"
    parent._settings = settings
    parent._request_with_rate_limit = AsyncMock(
        return_value={
            "rt_cd": "0",
            "output": {"ODNO": "0001", "ORD_TMD": "090000"},
            "msg1": "정상처리",
        }
    )
    return parent


@pytest.mark.unit
class TestDomesticOrderNoDoubleSubmit:
    @pytest.fixture(autouse=True)
    def _no_nxt(self, monkeypatch):
        from app.services.brokers.kis import domestic_orders

        monkeypatch.setattr(
            domestic_orders, "is_nxt_eligible", AsyncMock(return_value=False)
        )

    def _client(self):
        from app.services.brokers.kis.domestic_orders import DomesticOrderClient

        parent = _make_parent()
        return DomesticOrderClient(parent), parent

    @pytest.mark.asyncio
    async def test_order_korea_stock_disables_retry_and_repost(self):
        instance, parent = self._client()

        await instance.order_korea_stock("005930", "buy", 1, 1000, is_mock=False)

        parent._request_with_rate_limit.assert_awaited_once()
        kwargs = parent._request_with_rate_limit.await_args.kwargs
        assert kwargs["retry_request_errors"] is False
        assert kwargs["max_retries_override"] == 0

    @pytest.mark.asyncio
    async def test_order_korea_stock_timeout_sends_exactly_once(self):
        instance, parent = self._client()
        parent._request_with_rate_limit = AsyncMock(side_effect=httpx.ReadTimeout(""))

        with pytest.raises(httpx.ReadTimeout):
            await instance.order_korea_stock("005930", "buy", 1, 1000, is_mock=False)

        assert parent._request_with_rate_limit.await_count == 1


@pytest.mark.unit
class TestOverseasOrderNoDoubleSubmit:
    def _client(self):
        from app.services.brokers.kis.overseas_orders import OverseasOrderClient

        parent = _make_parent()
        return OverseasOrderClient(parent), parent

    @pytest.mark.asyncio
    async def test_order_overseas_stock_disables_retry_and_repost(self):
        instance, parent = self._client()

        await instance.order_overseas_stock(
            "AAPL", "NASD", "buy", 1, 100.0, is_mock=False
        )

        parent._request_with_rate_limit.assert_awaited_once()
        kwargs = parent._request_with_rate_limit.await_args.kwargs
        assert kwargs["retry_request_errors"] is False
        assert kwargs["max_retries_override"] == 0

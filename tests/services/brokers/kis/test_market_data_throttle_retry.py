from __future__ import annotations

import asyncio
import random
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.brokers.kis._base_market_data import MarketDataBase

_THROTTLE = {
    "rt_cd": "1",
    "msg_cd": "EGW00201",
    "msg1": "초당 거래건수를 초과하였습니다.",
}
_SUCCESS = {"rt_cd": "0", "output": {"price": "10"}}


def _market_data(responses: list[dict[str, str]]):
    parent = MagicMock()
    parent._ensure_token = AsyncMock()
    parent._hdr_base = {}
    parent._settings = MagicMock(kis_access_token="token")
    parent._request_with_rate_limit = AsyncMock(side_effect=responses)
    return MarketDataBase(parent), parent


@pytest.mark.asyncio
async def test_market_data_retries_egw00201_and_succeeds(monkeypatch, caplog):
    client, parent = _market_data([_THROTTLE, _SUCCESS])
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(random, "uniform", lambda low, high: high)

    with caplog.at_level(
        "WARNING", logger="app.services.brokers.kis._base_market_data"
    ):
        result = await client._request_with_token_retry("TR", "https://host/read")

    assert result is _SUCCESS
    assert parent._request_with_rate_limit.await_count == 2
    assert len(sleeps) == 1
    assert 0 < sleeps[0] < 3
    assert "retrying KIS market-data request" in caplog.text


@pytest.mark.asyncio
async def test_market_data_throttle_retries_are_bounded(monkeypatch):
    client, parent = _market_data([_THROTTLE] * 10)
    sleep = AsyncMock()
    monkeypatch.setattr(asyncio, "sleep", sleep)
    monkeypatch.setattr(random, "uniform", lambda low, high: high)

    with pytest.raises(RuntimeError, match="초당 거래건수를"):
        await client._request_with_token_retry("TR", "https://host/read")

    assert parent._request_with_rate_limit.await_count == 3
    assert sleep.await_count == 2
    assert sum(call.args[0] for call in sleep.await_args_list) < 3


@pytest.mark.asyncio
async def test_market_data_token_expiry_retry_remains_unchanged(monkeypatch):
    from app.services.brokers.kis import constants

    client, parent = _market_data(
        [
            {"rt_cd": "1", "msg_cd": next(iter(constants.TOKEN_EXPIRED_CODES))},
            _SUCCESS,
        ]
    )
    parent._token_manager = MagicMock()
    parent._token_manager.clear_token = AsyncMock()
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())

    result = await client._request_with_token_retry("TR", "https://host/read")

    assert result is _SUCCESS
    assert parent._token_manager.clear_token.await_count == 1
    assert parent._request_with_rate_limit.await_count == 2


@pytest.mark.asyncio
async def test_order_throttle_repost_count_stays_at_existing_behavior(monkeypatch):
    """The market-data retry is not applied around existing order sends."""
    import app.services.brokers.kis.overseas_orders as orders_module

    parent = MagicMock()
    parent._ensure_token = AsyncMock()
    parent._hdr_base = {}
    parent._kis_url = lambda path: f"https://host{path}"
    parent._settings = MagicMock(
        kis_account_no="1234567890",
        kis_access_token="token",
    )
    parent._request_with_rate_limit = AsyncMock(
        side_effect=[
            _THROTTLE,
            {
                "rt_cd": "0",
                "output": {"ODNO": "0030808418", "ORD_TMD": "233959"},
                "msg1": "정상처리 되었습니다.",
            },
        ]
    )
    monkeypatch.setattr(orders_module.asyncio, "sleep", AsyncMock())
    from app.services.brokers.kis.overseas_orders import OverseasOrderClient

    market_data_retry = AsyncMock(
        side_effect=AssertionError("order flow entered market-data retry helper")
    )
    monkeypatch.setattr(MarketDataBase, "_request_with_token_retry", market_data_retry)

    result = await OverseasOrderClient(parent).order_overseas_stock(
        "BAC", "NYSE", "sell", 1, 62.15, is_mock=False
    )

    assert result["odno"] == "0030808418"
    market_data_retry.assert_not_awaited()
    # Baseline ROB-BAC behavior: one EGW00201 rejection, then one accepted POST.
    assert parent._request_with_rate_limit.await_count == 2

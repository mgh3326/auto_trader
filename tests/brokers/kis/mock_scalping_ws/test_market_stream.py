"""Read-only KISQuoteWebSocket client tests (ROB-321 PR2 Task 3)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.services.brokers.kis.mock_scalping_ws.market_stream import KISQuoteWebSocket
from app.services.brokers.kis.mock_scalping_ws.quote_parsers import (
    OrderBookSnapshot,
    QuoteTick,
)
from app.services.brokers.kis.mock_scalping_ws.quote_protocol import (
    DOMESTIC_ORDERBOOK_TR,
    DOMESTIC_TRADE_TR,
)

INTERNAL = "app.services.kis_websocket_internal.approval_keys"


def _trade_frame(symbol: str = "005930", price: str = "70500") -> str:
    fields = [""] * 20
    fields[0] = symbol
    fields[1] = "131502"
    fields[2] = price
    return f"0|{DOMESTIC_TRADE_TR}|001|" + "^".join(fields)


def _orderbook_frame(symbol: str = "005930") -> str:
    fields = [""] * 60
    fields[0] = symbol
    fields[3] = "70600"  # ask1
    fields[13] = "70500"  # bid1
    fields[23] = "120"  # ask qty1
    fields[33] = "200"  # bid qty1
    return f"0|{DOMESTIC_ORDERBOOK_TR}|001|" + "^".join(fields)


def _make_client(**kwargs) -> KISQuoteWebSocket:
    return KISQuoteWebSocket(
        symbols=kwargs.pop("symbols", ["005930"]),
        on_tick=kwargs.pop("on_tick", AsyncMock()),
        on_book=kwargs.pop("on_book", AsyncMock()),
        **kwargs,
    )


@pytest.mark.unit
def test_rejects_unknown_account_mode() -> None:
    with pytest.raises(ValueError, match="account_mode"):
        _make_client(account_mode="alpaca_paper")


@pytest.mark.unit
def test_build_url_live_and_mock() -> None:
    live = _make_client(account_mode="kis_live")
    assert live._build_url() == "ws://ops.koreainvestment.com:21000/tryitout"
    mock = _make_client(account_mode="kis_mock")
    assert mock._build_url() == "ws://ops.koreainvestment.com:31000/tryitout"


@pytest.mark.unit
def test_has_no_order_surface() -> None:
    client = _make_client()
    for forbidden in ("submit_order", "place_order", "send_order", "cancel_order"):
        assert not hasattr(client, forbidden)


@pytest.mark.unit
def test_subscription_request_shape() -> None:
    client = _make_client()
    client.approval_key = "approval-123"
    req = client._build_subscription_request(DOMESTIC_TRADE_TR, "005930")
    assert req["header"]["approval_key"] == "approval-123"
    assert req["header"]["tr_type"] == "1"
    assert req["body"]["input"]["tr_id"] == DOMESTIC_TRADE_TR
    assert req["body"]["input"]["tr_key"] == "005930"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dispatch_routes_tick_and_book() -> None:
    on_tick = AsyncMock()
    on_book = AsyncMock()
    client = _make_client(on_tick=on_tick, on_book=on_book)

    await client._dispatch(_trade_frame(price="70500"))
    await client._dispatch(_orderbook_frame())

    on_tick.assert_awaited_once()
    assert isinstance(on_tick.await_args.args[0], QuoteTick)
    assert on_tick.await_args.args[0].last_price == 70500.0
    on_book.assert_awaited_once()
    assert isinstance(on_book.await_args.args[0], OrderBookSnapshot)
    assert client.tick_events_received == 1
    assert client.book_events_received == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dispatch_ignores_junk_and_unknown_tr() -> None:
    on_tick = AsyncMock()
    on_book = AsyncMock()
    client = _make_client(on_tick=on_tick, on_book=on_book)

    await client._dispatch("garbage")
    await client._dispatch("0|H0STCNI0|001|005930^x")  # execution TR, not a quote

    on_tick.assert_not_awaited()
    on_book.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_listen_dispatches_from_fake_ws() -> None:
    on_tick = AsyncMock()
    on_book = AsyncMock()
    client = _make_client(on_tick=on_tick, on_book=on_book)
    client.websocket = AsyncMock()
    client.websocket.__aiter__.return_value = [
        _trade_frame(price="70500"),
        _orderbook_frame(),
    ]

    await client.listen()

    on_tick.assert_awaited_once()
    on_book.assert_awaited_once()
    assert client.messages_received == 2
    assert client.last_message_at is not None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_listen_echoes_pingpong_without_dispatch() -> None:
    on_tick = AsyncMock()
    client = _make_client(on_tick=on_tick)
    client.websocket = AsyncMock()
    client.websocket.send = AsyncMock()
    client.websocket.__aiter__.return_value = ["0|pingpong"]

    await client.listen()

    client.websocket.send.assert_awaited_once_with("0|pingpong")
    on_tick.assert_not_awaited()
    assert client.messages_received == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_listen_without_websocket_raises() -> None:
    client = _make_client()
    client.websocket = None
    with pytest.raises(RuntimeError, match="not connected"):
        await client.listen()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_connect_uses_account_mode_approval_key(mocker) -> None:
    client = _make_client(account_mode="kis_mock")
    client.is_running = True

    get_key = mocker.patch(
        f"{INTERNAL}.get_approval_key", new=AsyncMock(return_value="mock-key")
    )
    fake_ws = AsyncMock()
    fake_ws.recv = AsyncMock(return_value='{"body": {"rt_cd": "0"}}')
    mocker.patch(
        "app.services.brokers.kis.mock_scalping_ws.market_stream.websockets.connect",
        new=AsyncMock(return_value=fake_ws),
    )

    await client.connect_and_subscribe()

    get_key.assert_awaited_once_with("kis_mock")
    assert client.approval_key == "mock-key"
    assert client.is_connected is True
    # 1 symbol x 2 TRs = 2 subscription sends
    assert fake_ws.send.await_count == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_connect_raises_after_max_attempts(mocker) -> None:
    from app.services.kis_websocket_internal.protocol import KISSubscriptionAckError

    client = _make_client(account_mode="kis_mock")
    client.is_running = True
    client.reconnect_delay = 0
    client.max_reconnect_attempts = 2

    mocker.patch(f"{INTERNAL}.get_approval_key", new=AsyncMock(return_value="k"))
    mocker.patch(
        "app.services.brokers.kis.mock_scalping_ws.market_stream.websockets.connect",
        new=AsyncMock(
            side_effect=KISSubscriptionAckError("H0STCNT0", "9", "X", "boom")
        ),
    )

    with pytest.raises(RuntimeError, match="not established"):
        await client.connect_and_subscribe()
    assert client.current_attempt == 2
    assert client.is_connected is False


@pytest.mark.unit
def test_validate_ack_rejects_bad_rt_cd() -> None:
    from app.services.kis_websocket_internal.protocol import KISSubscriptionAckError

    client = _make_client()
    with pytest.raises(KISSubscriptionAckError):
        client._validate_subscription_ack(
            '{"body": {"rt_cd": "1", "msg_cd": "E", "msg1": "nope"}}',
            DOMESTIC_TRADE_TR,
            "005930",
        )


@pytest.mark.unit
def test_validate_ack_accepts_realtime_data_frame() -> None:
    client = _make_client()
    # A pipe-delimited data frame is not an ACK envelope -> accepted (no raise).
    client._validate_subscription_ack(_trade_frame(), DOMESTIC_TRADE_TR, "005930")


@pytest.mark.unit
def test_validate_ack_missing_body_raises() -> None:
    from app.services.kis_websocket_internal.protocol import KISSubscriptionAckError

    client = _make_client()
    with pytest.raises(KISSubscriptionAckError):
        client._validate_subscription_ack('{"header": {}}', DOMESTIC_TRADE_TR, "005930")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_subscribe_quotes_requires_symbols() -> None:
    client = _make_client()
    client.websocket = AsyncMock()
    client.approval_key = "k"
    client.symbols = []
    with pytest.raises(ValueError, match="No symbols"):
        await client._subscribe_quotes()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dispatch_supports_sync_callbacks() -> None:
    seen: list[str] = []
    client = _make_client(
        on_tick=lambda t: seen.append(f"tick:{t.symbol}"),
        on_book=lambda b: seen.append(f"book:{b.symbol}"),
    )
    await client._dispatch(_trade_frame())
    await client._dispatch(_orderbook_frame())
    assert seen == ["tick:005930", "book:005930"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stop_closes_websocket() -> None:
    client = _make_client()
    ws = AsyncMock()
    client.websocket = ws
    client.is_running = True

    await client.stop()

    ws.close.assert_awaited_once()
    assert client.websocket is None
    assert client.is_connected is False
    assert client.is_running is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_close_best_effort_is_none_safe() -> None:
    client = _make_client()
    client.websocket = None
    # Should not raise.
    await client._close_websocket_best_effort()
    assert client.websocket is None

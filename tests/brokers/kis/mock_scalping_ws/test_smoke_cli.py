"""Quote WS smoke gating tests (ROB-321 PR2 Task 4)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.core.config import settings
from app.services.brokers.kis.mock_scalping_ws.quote_parsers import QuoteTick
from app.services.kis_websocket_internal.protocol import KISSubscriptionAckError
from scripts import kis_mock_scalping_ws_smoke as smoke


def _fake_client(mocker, *, listen=None, connect_exc=None):
    """Patch KISQuoteWebSocket with a fake whose listen() can drive callbacks."""
    captured: dict = {}

    def _factory(**kwargs):
        captured.update(kwargs)
        client = AsyncMock()
        client.is_running = False
        if connect_exc is not None:
            client.connect_and_subscribe = AsyncMock(side_effect=connect_exc)
        else:
            client.connect_and_subscribe = AsyncMock()
        if listen is not None:
            client.listen = lambda: listen(captured)
        else:
            client.listen = AsyncMock()
        client.stop = AsyncMock()
        return client

    mocker.patch.object(smoke, "KISQuoteWebSocket", side_effect=_factory)
    return captured


@pytest.mark.unit
@pytest.mark.asyncio
async def test_smoke_disabled_is_noop(mocker) -> None:
    mocker.patch.object(settings, "kis_mock_scalping_ws_enabled", False)
    # If the gate fails, this construction would be attempted — assert it is NOT.
    construct = mocker.patch.object(smoke, "KISQuoteWebSocket")

    args = smoke._parse_args(["--symbols", "005930", "--max-events", "1"])
    rc = await smoke.run_smoke(args)

    assert rc == 0
    construct.assert_not_called()


@pytest.mark.unit
def test_smoke_defaults_to_mock_account_mode() -> None:
    args = smoke._parse_args([])
    assert args.account_mode == "kis_mock"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_smoke_success_returns_0(mocker) -> None:
    mocker.patch.object(settings, "kis_mock_scalping_ws_enabled", True)

    async def _listen(captured):
        # Drive enough events to hit max-events -> done.set()
        captured["on_tick"](QuoteTick(symbol="005930", last_price=1.0, ts="1"))

    _fake_client(mocker, listen=_listen)
    args = smoke._parse_args(["--max-events", "1", "--max-seconds", "5"])
    rc = await smoke.run_smoke(args)
    assert rc == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_smoke_ack_failure_returns_2(mocker) -> None:
    mocker.patch.object(settings, "kis_mock_scalping_ws_enabled", True)
    _fake_client(
        mocker,
        connect_exc=KISSubscriptionAckError("H0STCNT0", "9", "X", "boom"),
    )
    rc = await smoke.run_smoke(smoke._parse_args([]))
    assert rc == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_smoke_connect_failure_returns_3(mocker) -> None:
    mocker.patch.object(settings, "kis_mock_scalping_ws_enabled", True)
    _fake_client(mocker, connect_exc=RuntimeError("not established"))
    rc = await smoke.run_smoke(smoke._parse_args([]))
    assert rc == 3


@pytest.mark.unit
@pytest.mark.asyncio
async def test_smoke_no_events_returns_4(mocker) -> None:
    mocker.patch.object(settings, "kis_mock_scalping_ws_enabled", True)

    async def _listen(_captured):
        return None  # connected but emits nothing

    _fake_client(mocker, listen=_listen)
    args = smoke._parse_args(["--max-events", "5", "--max-seconds", "0.05"])
    rc = await smoke.run_smoke(args)
    assert rc == 4

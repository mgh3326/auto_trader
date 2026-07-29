"""ROB-1162 — Binance public smoke wall-clock timeout regressions."""

from __future__ import annotations

import argparse
import asyncio
import logging
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

import scripts.binance_public_smoke as smoke
from app.services.brokers.binance.errors import BinanceLiveHostBlocked


class _FakeRestClient:
    async def __aenter__(self) -> _FakeRestClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def exchange_info(self, symbol: str) -> SimpleNamespace:
        return SimpleNamespace(
            symbol=symbol,
            status="TRADING",
            base_asset="BTC",
            quote_asset="USDT",
        )

    async def klines(self, *args: Any, **kwargs: Any) -> list[object]:
        return [object()]

    async def _send(self, *args: Any, **kwargs: Any) -> None:
        raise BinanceLiveHostBlocked("expected public-host rejection")


@dataclass
class _WSState:
    event_count: int
    entered: bool = False
    exited: bool = False
    iterator_closed: bool = False
    stalled: bool = False
    stall_cancelled: bool = False


def _fake_ws_client(state: _WSState) -> type:
    class _FakeWSClient:
        def __init__(self, *, url: str) -> None:
            self.url = url

        async def __aenter__(self) -> _FakeWSClient:
            state.entered = True
            return self

        async def __aexit__(self, *exc: Any) -> None:
            state.exited = True

        async def events(self):
            try:
                for index in range(state.event_count):
                    yield {"event": index}
                if state.event_count < 3:
                    state.stalled = True
                    await asyncio.Event().wait()
            except asyncio.CancelledError:
                state.stall_cancelled = True
                raise
            finally:
                state.iterator_closed = True

    return _FakeWSClient


def _args(*, duration: float) -> argparse.Namespace:
    return argparse.Namespace(
        symbol="BTCUSDT",
        symbols="BTCUSDT,DO-NOT-LOG-THIS",
        duration=duration,
        dry_run=True,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("duration", [0, -1])
async def test_non_positive_duration_fails_before_client_construction(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    duration: int,
) -> None:
    class _UnexpectedClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise AssertionError(
                "non-positive duration must fail before network clients"
            )

    monkeypatch.setattr(smoke, "BinancePublicRestClient", _UnexpectedClient)
    monkeypatch.setattr(smoke, "BinancePublicWSClient", _UnexpectedClient)
    caplog.set_level(logging.ERROR, logger="binance_public_smoke")

    result = await smoke._run(_args(duration=duration))

    assert result == 1
    assert "--duration must be greater than zero seconds" in caplog.text


@pytest.mark.asyncio
async def test_zero_event_timeout_is_bounded_secret_free_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    state = _WSState(event_count=0)
    monkeypatch.setattr(smoke, "BinancePublicRestClient", _FakeRestClient)
    monkeypatch.setattr(smoke, "BinancePublicWSClient", _fake_ws_client(state))
    caplog.set_level(logging.INFO, logger="binance_public_smoke")

    result = await asyncio.wait_for(smoke._run(_args(duration=0.02)), timeout=1)

    assert result == 4
    assert state.entered is True
    assert state.exited is True
    assert state.stalled is True
    assert state.stall_cancelled is True
    assert state.iterator_closed is True
    assert (
        "WS FAIL: no events received before --duration=0.02 seconds elapsed"
        in caplog.text
    )
    assert "DO-NOT-LOG-THIS" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("event_count", [1, 2])
async def test_partial_events_finish_at_duration_and_clean_up(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    event_count: int,
) -> None:
    state = _WSState(event_count=event_count)
    monkeypatch.setattr(smoke, "BinancePublicRestClient", _FakeRestClient)
    monkeypatch.setattr(smoke, "BinancePublicWSClient", _fake_ws_client(state))
    caplog.set_level(logging.INFO, logger="binance_public_smoke")

    result = await asyncio.wait_for(smoke._run(_args(duration=0.02)), timeout=1)

    assert result == 0
    assert state.entered is True
    assert state.exited is True
    assert state.stalled is True
    assert state.stall_cancelled is True
    assert state.iterator_closed is True
    assert f"WS duration elapsed after receiving {event_count} event(s)" in caplog.text
    assert f"received {event_count} WS events" in caplog.text


@pytest.mark.asyncio
async def test_three_events_finish_normally_and_close_iterator_and_context(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    state = _WSState(event_count=3)
    monkeypatch.setattr(smoke, "BinancePublicRestClient", _FakeRestClient)
    monkeypatch.setattr(smoke, "BinancePublicWSClient", _fake_ws_client(state))
    caplog.set_level(logging.INFO, logger="binance_public_smoke")

    result = await asyncio.wait_for(smoke._run(_args(duration=1)), timeout=0.2)

    assert result == 0
    assert state.entered is True
    assert state.exited is True
    assert state.stalled is False
    assert state.stall_cancelled is False
    assert state.iterator_closed is True
    assert "WS duration elapsed" not in caplog.text
    assert "received 3 WS events" in caplog.text

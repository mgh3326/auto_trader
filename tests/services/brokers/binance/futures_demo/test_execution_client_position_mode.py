"""ROB-298 PR 2 — get_position_mode() hits /fapi/v1/positionSide/dual.

The client returns the Hedge mode flag (``is_hedge_mode``) from Binance's
``dualSidePosition`` field. The client does NOT raise on Hedge mode — that
guard is the CLI's responsibility (PR 2 only supports One-way mode at the
operator level). The client just surfaces the flag.
"""

from __future__ import annotations

import re

import pytest

from app.services.brokers.binance.futures_demo.dto import (
    FuturesDemoPositionModeResult,
)
from app.services.brokers.binance.futures_demo.execution_client import (
    BinanceFuturesDemoExecutionClient,
)

_FUTURES_DEMO_BASE = "https://demo-fapi.binance.com"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> BinanceFuturesDemoExecutionClient:
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_ENABLED", "true")
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_API_KEY", "DUMMY_FUTURES_DEMO_KEY")
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_API_SECRET", "DUMMY_FUTURES_DEMO_SECRET")
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_BASE_URL", _FUTURES_DEMO_BASE)
    return BinanceFuturesDemoExecutionClient.from_env()


@pytest.mark.asyncio
async def test_get_position_mode_hits_position_side_dual_path(
    client: BinanceFuturesDemoExecutionClient, httpx_mock
) -> None:
    """``get_position_mode`` dispatches GET /fapi/v1/positionSide/dual."""
    httpx_mock.add_response(
        method="GET",
        url=re.compile(
            r"^https://demo-fapi\.binance\.com/fapi/v1/positionSide/dual\?.*$"
        ),
        status_code=200,
        json={"dualSidePosition": False},
    )
    result = await client.get_position_mode()
    assert isinstance(result, FuturesDemoPositionModeResult)

    last = httpx_mock.get_request()
    assert last is not None
    assert last.method == "GET"
    assert last.url.path == "/fapi/v1/positionSide/dual"
    url_str = str(last.url)
    assert "signature=" in url_str
    assert "timestamp=" in url_str


@pytest.mark.asyncio
async def test_get_position_mode_returns_false_for_one_way(
    client: BinanceFuturesDemoExecutionClient, httpx_mock
) -> None:
    """``dualSidePosition=false`` → ``is_hedge_mode=False`` (One-way mode, PR 2 supported)."""
    httpx_mock.add_response(
        method="GET",
        url=re.compile(
            r"^https://demo-fapi\.binance\.com/fapi/v1/positionSide/dual\?.*$"
        ),
        status_code=200,
        json={"dualSidePosition": False},
    )
    result = await client.get_position_mode()
    assert result.is_hedge_mode is False


@pytest.mark.asyncio
async def test_get_position_mode_returns_true_for_hedge(
    client: BinanceFuturesDemoExecutionClient, httpx_mock
) -> None:
    """``dualSidePosition=true`` → ``is_hedge_mode=True`` (Hedge mode; CLI must refuse)."""
    httpx_mock.add_response(
        method="GET",
        url=re.compile(
            r"^https://demo-fapi\.binance\.com/fapi/v1/positionSide/dual\?.*$"
        ),
        status_code=200,
        json={"dualSidePosition": True},
    )
    result = await client.get_position_mode()
    assert result.is_hedge_mode is True

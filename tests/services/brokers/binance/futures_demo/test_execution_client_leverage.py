"""ROB-298 PR 2 — set_leverage hits /fapi/v1/leverage and verifies echo.

The Futures Demo execution client enforces the 1x leverage smoke contract:

  * ``set_leverage(symbol, leverage=1)`` POSTs a signed request to
    ``/fapi/v1/leverage`` with the requested leverage.
  * Binance echoes back the now-set leverage; the client verifies it
    matches the requested value.
  * Any mismatch (e.g. the operator set ``leverage=1`` but Binance echoes
    ``leverage=5``) raises ``BinanceFuturesDemoLeverageMismatch``.
"""

from __future__ import annotations

import re

import pytest

from app.services.brokers.binance.futures_demo.dto import FuturesDemoLeverageResult
from app.services.brokers.binance.futures_demo.errors import (
    BinanceFuturesDemoLeverageMismatch,
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
async def test_set_leverage_posts_to_leverage_path_with_params(
    client: BinanceFuturesDemoExecutionClient, httpx_mock
) -> None:
    """``set_leverage(symbol, leverage=1)`` POSTs /fapi/v1/leverage with symbol+leverage."""
    httpx_mock.add_response(
        method="POST",
        url=re.compile(r"^https://demo-fapi\.binance\.com/fapi/v1/leverage\?.*$"),
        status_code=200,
        json={"symbol": "XRPUSDT", "leverage": 1, "maxNotionalValue": "10000"},
    )
    result = await client.set_leverage(symbol="XRPUSDT", leverage=1)
    assert isinstance(result, FuturesDemoLeverageResult)
    assert result.symbol == "XRPUSDT"
    assert result.leverage == 1

    last = httpx_mock.get_request()
    assert last is not None
    assert last.method == "POST"
    assert last.url.path == "/fapi/v1/leverage"
    url_str = str(last.url)
    assert "symbol=XRPUSDT" in url_str
    assert "leverage=1" in url_str


@pytest.mark.asyncio
async def test_set_leverage_returns_result_on_matching_echo(
    client: BinanceFuturesDemoExecutionClient, httpx_mock
) -> None:
    """Response echo ``leverage=1`` matches requested → returns FuturesDemoLeverageResult."""
    httpx_mock.add_response(
        method="POST",
        url=re.compile(r"^https://demo-fapi\.binance\.com/fapi/v1/leverage\?.*$"),
        status_code=200,
        json={"symbol": "XRPUSDT", "leverage": 1, "maxNotionalValue": "10000"},
    )
    result = await client.set_leverage(symbol="XRPUSDT", leverage=1)
    assert isinstance(result, FuturesDemoLeverageResult)
    assert result.leverage == 1
    assert result.symbol == "XRPUSDT"


@pytest.mark.asyncio
async def test_set_leverage_raises_on_mismatched_echo(
    client: BinanceFuturesDemoExecutionClient, httpx_mock
) -> None:
    """Response echo ``leverage=5`` does NOT match requested 1 → mismatch error.

    Any non-matching echo indicates either a Binance-side bug or env
    tampering. The execution client must refuse to proceed.
    """
    httpx_mock.add_response(
        method="POST",
        url=re.compile(r"^https://demo-fapi\.binance\.com/fapi/v1/leverage\?.*$"),
        status_code=200,
        json={"symbol": "XRPUSDT", "leverage": 5, "maxNotionalValue": "10000"},
    )
    with pytest.raises(BinanceFuturesDemoLeverageMismatch):
        await client.set_leverage(symbol="XRPUSDT", leverage=1)


@pytest.mark.asyncio
async def test_set_leverage_rejects_leverage_5_before_http(
    client: BinanceFuturesDemoExecutionClient, httpx_mock
) -> None:
    """``set_leverage(leverage=5)`` raises BEFORE any HTTP (1x pin guard).

    ROB-298 PR 2 design lock (comment d258c471): leverage=1 forced.
    The client must refuse any other value at the boundary, BEFORE the
    signed POST is dispatched. No mocked response is needed because
    no HTTP should occur at all.
    """
    with pytest.raises(BinanceFuturesDemoLeverageMismatch):
        await client.set_leverage(symbol="XRPUSDT", leverage=5)
    assert httpx_mock.get_requests() == []


@pytest.mark.asyncio
async def test_set_leverage_rejects_leverage_0(
    client: BinanceFuturesDemoExecutionClient, httpx_mock
) -> None:
    """``set_leverage(leverage=0)`` raises BEFORE any HTTP."""
    with pytest.raises(BinanceFuturesDemoLeverageMismatch):
        await client.set_leverage(symbol="XRPUSDT", leverage=0)
    assert httpx_mock.get_requests() == []


@pytest.mark.asyncio
async def test_set_leverage_rejects_leverage_negative(
    client: BinanceFuturesDemoExecutionClient, httpx_mock
) -> None:
    """``set_leverage(leverage=-1)`` raises BEFORE any HTTP."""
    with pytest.raises(BinanceFuturesDemoLeverageMismatch):
        await client.set_leverage(symbol="XRPUSDT", leverage=-1)
    assert httpx_mock.get_requests() == []


@pytest.mark.asyncio
async def test_set_leverage_is_signed(
    client: BinanceFuturesDemoExecutionClient, httpx_mock
) -> None:
    """``set_leverage`` carries X-MBX-APIKEY header + signature/timestamp params."""
    httpx_mock.add_response(
        method="POST",
        url=re.compile(r"^https://demo-fapi\.binance\.com/fapi/v1/leverage\?.*$"),
        status_code=200,
        json={"symbol": "XRPUSDT", "leverage": 1, "maxNotionalValue": "10000"},
    )
    await client.set_leverage(symbol="XRPUSDT", leverage=1)
    last = httpx_mock.get_request()
    assert last is not None
    assert last.headers.get("X-MBX-APIKEY") == "DUMMY_FUTURES_DEMO_KEY"
    url_str = str(last.url)
    assert "signature=" in url_str
    assert "timestamp=" in url_str


@pytest.mark.asyncio
@pytest.mark.parametrize("margin", ["CROSSED", "isolated", "", None])
async def test_margin_rejects_other_values_before_http(client, httpx_mock, margin):
    with pytest.raises(ValueError):
        await client.set_margin_type(symbol="XRPUSDT", margin_type=margin, confirm=True)
    assert httpx_mock.get_requests() == []


@pytest.mark.asyncio
async def test_margin_noop_requires_isolated_readback(client, httpx_mock):
    httpx_mock.add_response(
        method="POST",
        url=re.compile(r".*/fapi/v1/marginType\?.*"),
        status_code=400,
        json={"code": -4046},
    )
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r".*/fapi/v1/positionSide/dual\?.*"),
        json={"dualSidePosition": False},
    )
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r".*/fapi/v2/positionRisk\?.*"),
        json=[
            {
                "symbol": "XRPUSDT",
                "positionSide": "BOTH",
                "positionAmt": "0",
                "leverage": "1",
                "marginType": "cross",
            }
        ],
    )
    with pytest.raises(ValueError, match="readback"):
        await client.set_margin_type(
            symbol="XRPUSDT", margin_type="ISOLATED", confirm=True
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [True, None])
async def test_preparation_requires_explicit_one_way(client, httpx_mock, mode):
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r".*/fapi/v1/positionSide/dual\?.*"),
        json={"dualSidePosition": mode},
    )
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r".*/fapi/v2/positionRisk\?.*"),
        json=[
            {
                "symbol": "XRPUSDT",
                "positionSide": "BOTH",
                "positionAmt": "0",
                "leverage": "1",
                "marginType": "isolated",
            }
        ],
        is_optional=True,
    )
    with pytest.raises(ValueError, match="one-way"):
        await client.get_preparation_state(symbol="XRPUSDT")
    assert len(httpx_mock.get_requests()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("echoed", ["OTHERUSDT", None])
async def test_preparation_leverage_requires_symbol_echo(client, httpx_mock, echoed):
    httpx_mock.add_response(
        method="POST",
        url=re.compile(r".*/fapi/v1/leverage\?.*"),
        json={"symbol": echoed, "leverage": 1},
    )
    with pytest.raises(BinanceFuturesDemoLeverageMismatch):
        await client.set_leverage(
            symbol="XRPUSDT", leverage=1, require_symbol_echo=True
        )

"""Crypto orderbook timestamps and market inference (hk 6371).

The fixture is the Upbit documented example for ``GET /v1/orderbook``
(https://docs.upbit.com/kr/reference/list-orderbooks, 2026-11-27).  Its
``timestamp`` field is documented there as "조회 요청 시각" (query request time),
so ``event_at`` labels it ``upbit_query_time``.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.core.symbol import is_upbit_market_symbol
from app.core.timezone import KST
from tests._mcp_tooling_support import build_tools

FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "upbit_orderbook"
    / "list_orderbooks_KRW-BTC_doc_example.json"
)
RECEIVED_KST = dt.datetime(2026, 11, 27, 10, 0, 0, tzinfo=KST)
RECEIVED_UTC_ISO = "2026-11-27T01:00:00+00:00"
DOC_TIMESTAMP_MS = 1751606867762
DOC_EVENT_AT_ISO = "2025-07-04T05:27:47.762000+00:00"


def _doc_raw() -> dict[str, Any]:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert isinstance(payload, list)
    return dict(payload[0])


@pytest.fixture
def patched_service(monkeypatch: pytest.MonkeyPatch):
    from app.services.market_data import service

    monkeypatch.setattr(service, "now_kst", lambda: RECEIVED_KST)

    def _install(raw: dict[str, Any] | None) -> AsyncMock:
        fetch = AsyncMock(return_value=raw)
        monkeypatch.setattr(service, "fetch_orderbook", fetch)
        return fetch

    return _install


def test_fixture_is_the_documented_upbit_example() -> None:
    raw = _doc_raw()
    assert raw["market"] == "KRW-BTC"
    assert raw["timestamp"] == DOC_TIMESTAMP_MS
    assert len(raw["orderbook_units"]) == 30


@pytest.mark.asyncio
async def test_market_omitted_krw_btc_resolves_to_crypto_with_exchange_timestamp(
    patched_service,
) -> None:
    fetch = patched_service(_doc_raw())
    tools = build_tools()

    result = await tools["get_orderbook"]("KRW-BTC")

    fetch.assert_awaited_once_with("KRW-BTC")
    assert result["instrument_type"] == "crypto"
    assert result["source"] == "upbit"
    assert result["event_at"] == DOC_EVENT_AT_ISO
    assert result["event_at_basis"] == "upbit_query_time"
    assert result["event_at_reason"] is None
    assert result["depth_basis_at"] == DOC_EVENT_AT_ISO
    assert result["depth_basis_reason"] is None
    assert result["first_received_at"] == RECEIVED_UTC_ISO
    assert result["first_received_reason"] is None
    # Our receive clock is never reported as the exchange timestamp.
    assert result["event_at"] != result["first_received_at"]


@pytest.mark.asyncio
async def test_missing_exchange_timestamp_is_null_with_reason_not_our_clock(
    patched_service,
) -> None:
    raw = _doc_raw()
    del raw["timestamp"]
    patched_service(raw)
    tools = build_tools()

    result = await tools["get_orderbook"]("KRW-BTC")

    assert result["event_at"] is None
    assert result["event_at_basis"] is None
    assert result["event_at_reason"] == "exchange_timestamp_unavailable"
    assert result["depth_basis_at"] is None
    assert result["depth_basis_reason"] == "exchange_timestamp_unavailable"
    # Receive time is still ours and still reported, separately.
    assert result["first_received_at"] == RECEIVED_UTC_ISO
    assert "as_of" not in result
    assert result["price_freshness"] == "unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [0, -5, "not-a-number", None])
async def test_invalid_exchange_timestamp_is_treated_as_missing(
    patched_service, bad: object
) -> None:
    raw = _doc_raw()
    raw["timestamp"] = bad
    patched_service(raw)
    tools = build_tools()

    result = await tools["get_orderbook"]("KRW-BTC", market="crypto")

    assert result["event_at"] is None
    assert result["event_at_reason"] == "exchange_timestamp_unavailable"
    assert result["depth_basis_at"] is None
    assert result["first_received_at"] == RECEIVED_UTC_ISO


@pytest.mark.asyncio
async def test_explicit_market_crypto_fixture_matches_market_omitted(
    patched_service,
) -> None:
    patched_service(_doc_raw())
    tools = build_tools()

    explicit = await tools["get_orderbook"]("KRW-BTC", market="crypto")
    omitted = await tools["get_orderbook"]("KRW-BTC")

    assert explicit == omitted


@pytest.mark.asyncio
@pytest.mark.parametrize("symbol", ["BTC-ETH", "USDT-BTC"])
async def test_market_omitted_non_krw_upbit_prefix_routes_to_crypto_error(
    symbol: str,
) -> None:
    tools = build_tools()

    with pytest.raises(
        ValueError, match=r"crypto orderbook only supports KRW-\* symbols"
    ):
        await tools["get_orderbook"](symbol)


@pytest.mark.asyncio
async def test_market_omitted_kr_equity_symbol_keeps_kr_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.mcp_server.tooling import market_data_quotes
    from app.services.market_data.contracts import OrderbookLevel, OrderbookSnapshot

    snapshot = OrderbookSnapshot(
        symbol="005930",
        instrument_type="equity_kr",
        source="kis",
        asks=[OrderbookLevel(price=70100, quantity=1)],
        bids=[OrderbookLevel(price=70000, quantity=1)],
        total_ask_qty=1,
        total_bid_qty=1,
        bid_ask_ratio=1.0,
    )
    get_orderbook = AsyncMock(return_value=snapshot)
    monkeypatch.setattr(
        market_data_quotes.market_data_service, "get_orderbook", get_orderbook
    )
    tools = build_tools()

    result = await tools["get_orderbook"]("5930")

    get_orderbook.assert_awaited_once_with("005930", "kr", venue=None)
    assert result["instrument_type"] == "equity_kr"
    # KR output carries none of the crypto time fields (golden unchanged).
    for key in ("event_at", "depth_basis_at", "first_received_at"):
        assert key not in result


@pytest.mark.asyncio
async def test_market_omitted_invalid_kr_symbol_still_fails_as_kr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = build_tools()

    with pytest.raises(
        ValueError, match="korean equity symbols must be 6 alphanumeric"
    ):
        await tools["get_orderbook"]("AAPL")


@pytest.mark.parametrize(
    ("symbol", "expected"),
    [
        ("KRW-BTC", True),
        ("krw-btc", True),
        ("BTC-ETH", True),
        ("USDT-BTC", True),
        ("BTC", False),
        ("005930", False),
        ("AAPL", False),
        (None, False),
        (5930, False),
    ],
)
def test_is_upbit_market_symbol(symbol: object, expected: bool) -> None:
    assert is_upbit_market_symbol(symbol) is expected

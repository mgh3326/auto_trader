from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.services.domain_errors import RateLimitError
from app.services.market_data.contracts import OrderbookLevel, OrderbookSnapshot
from tests._mcp_tooling_support import build_tools


def _make_snapshot(**overrides: object) -> OrderbookSnapshot:
    snapshot = OrderbookSnapshot(
        symbol="005930",
        instrument_type="equity_kr",
        source="kis",
        asks=[OrderbookLevel(price=70100, quantity=123)],
        bids=[OrderbookLevel(price=70000, quantity=321)],
        total_ask_qty=1000,
        total_bid_qty=1500,
        bid_ask_ratio=1.5,
        expected_price=70050,
        expected_qty=42,
    )
    for key, value in overrides.items():
        setattr(snapshot, key, value)
    return snapshot


@pytest.mark.asyncio
async def test_get_orderbook_returns_kr_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.mcp_server.tooling import market_data_quotes

    monkeypatch.setattr(
        market_data_quotes.market_data_service,
        "get_orderbook",
        AsyncMock(return_value=_make_snapshot()),
    )
    tools = build_tools()

    result = await tools["get_orderbook"]("5930")

    assert result == {
        "symbol": "005930",
        "name": "삼성전자",
        "name_resolved": True,
        "instrument_type": "equity_kr",
        "source": "kis",
        "asks": [{"price": 70100, "quantity": 123}],
        "bids": [{"price": 70000, "quantity": 321}],
        "total_ask_qty": 1000,
        "total_bid_qty": 1500,
        "bid_ask_ratio": 1.5,
        "pressure": "buy",
        "pressure_desc": "매수잔량이 매도잔량의 1.5배 - 매수 압력",
        "spread": 100,
        "spread_pct": 0.143,
        "expected_price": 70050,
        "expected_qty": 42,
        "bid_walls": [],
        "ask_walls": [],
    }
    assert type(result["asks"][0]["price"]) is int
    assert type(result["asks"][0]["quantity"]) is int
    assert type(result["total_ask_qty"]) is int
    assert type(result["spread"]) is int


@pytest.mark.asyncio
async def test_get_orderbook_returns_crypto_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.mcp_server.tooling import market_data_quotes

    get_orderbook_mock = AsyncMock(
        return_value=_make_snapshot(
            symbol="KRW-BTC",
            instrument_type="crypto",
            source="upbit",
            asks=[
                OrderbookLevel(price=10.5, quantity=1.0),
                OrderbookLevel(price=11.0, quantity=5.0),
                OrderbookLevel(price=12.0, quantity=1.0),
            ],
            bids=[
                OrderbookLevel(price=10.0, quantity=1.0),
                OrderbookLevel(price=9.5, quantity=5.0),
                OrderbookLevel(price=9.0, quantity=1.0),
            ],
            total_ask_qty=7.0,
            total_bid_qty=7.0,
            bid_ask_ratio=1.0,
            expected_price=None,
            expected_qty=None,
        )
    )
    monkeypatch.setattr(
        market_data_quotes.market_data_service,
        "get_orderbook",
        get_orderbook_mock,
    )
    tools = build_tools()

    result = await tools["get_orderbook"]("KRW-BTC", market="crypto")

    get_orderbook_mock.assert_awaited_once_with("KRW-BTC", "crypto", venue=None)
    assert result == {
        "symbol": "KRW-BTC",
        "name": "KRW-BTC",
        "name_resolved": False,
        "instrument_type": "crypto",
        "source": "upbit",
        "asks": [
            {"price": 10.5, "quantity": 1.0},
            {"price": 11.0, "quantity": 5.0},
            {"price": 12.0, "quantity": 1.0},
        ],
        "bids": [
            {"price": 10.0, "quantity": 1.0},
            {"price": 9.5, "quantity": 5.0},
            {"price": 9.0, "quantity": 1.0},
        ],
        "total_ask_qty": 7.0,
        "total_bid_qty": 7.0,
        "bid_ask_ratio": 1.0,
        "pressure": "neutral",
        "pressure_desc": "매수/매도 잔량이 균형권 - 중립",
        "spread": 0.5,
        "spread_pct": 5.0,
        "expected_price": None,
        "expected_qty": None,
        "bid_walls": [{"price": 9.5, "size": 5.0, "value_krw": 48}],
        "ask_walls": [{"price": 11.0, "size": 5.0, "value_krw": 55}],
    }
    assert type(result["asks"][0]["price"]) is float
    assert type(result["asks"][0]["quantity"]) is float
    assert type(result["total_ask_qty"]) is float
    assert type(result["spread"]) is float


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "total_ask_qty",
        "total_bid_qty",
        "bid_ask_ratio",
        "expected_pressure",
        "expected_desc",
    ),
    [
        (1000, 2100, 2.1, "strong_buy", "매수잔량이 매도잔량의 2.1배 - 강한 매수 압력"),
        (1000, 2000, 2.0, "buy", "매수잔량이 매도잔량의 2.0배 - 매수 압력"),
        (1000, 1300, 1.3, "neutral", "매수/매도 잔량이 균형권 - 중립"),
        (1000, 700, 0.7, "neutral", "매수/매도 잔량이 균형권 - 중립"),
        (1000, 500, 0.5, "sell", "매도잔량이 매수잔량의 2.0배 - 매도 압력"),
        (1000, 400, 0.4, "strong_sell", "매도잔량이 매수잔량의 2.5배 - 강한 매도 압력"),
    ],
)
async def test_get_orderbook_classifies_pressure_by_ratio_boundaries(
    monkeypatch: pytest.MonkeyPatch,
    total_ask_qty: int,
    total_bid_qty: int,
    bid_ask_ratio: float,
    expected_pressure: str,
    expected_desc: str,
) -> None:
    from app.mcp_server.tooling import market_data_quotes

    monkeypatch.setattr(
        market_data_quotes.market_data_service,
        "get_orderbook",
        AsyncMock(
            return_value=_make_snapshot(
                total_ask_qty=total_ask_qty,
                total_bid_qty=total_bid_qty,
                bid_ask_ratio=bid_ask_ratio,
            )
        ),
    )
    tools = build_tools()

    result = await tools["get_orderbook"]("005930")

    assert result["pressure"] == expected_pressure
    assert result["pressure_desc"] == expected_desc


@pytest.mark.asyncio
async def test_get_orderbook_returns_null_pressure_when_ratio_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.mcp_server.tooling import market_data_quotes

    monkeypatch.setattr(
        market_data_quotes.market_data_service,
        "get_orderbook",
        AsyncMock(
            return_value=_make_snapshot(
                total_ask_qty=0,
                total_bid_qty=1500,
                bid_ask_ratio=None,
            )
        ),
    )
    tools = build_tools()

    result = await tools["get_orderbook"]("005930")

    assert result["pressure"] is None
    assert result["pressure_desc"] is None
    assert result["spread"] == 100
    assert result["spread_pct"] == pytest.approx(0.143)


@pytest.mark.asyncio
async def test_get_orderbook_preserves_null_expected_qty_in_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.mcp_server.tooling import market_data_quotes

    monkeypatch.setattr(
        market_data_quotes.market_data_service,
        "get_orderbook",
        AsyncMock(return_value=_make_snapshot(expected_qty=None)),
    )
    tools = build_tools()

    result = await tools["get_orderbook"]("5930")

    assert result == {
        "symbol": "005930",
        "name": "삼성전자",
        "name_resolved": True,
        "instrument_type": "equity_kr",
        "source": "kis",
        "asks": [{"price": 70100, "quantity": 123}],
        "bids": [{"price": 70000, "quantity": 321}],
        "total_ask_qty": 1000,
        "total_bid_qty": 1500,
        "bid_ask_ratio": 1.5,
        "pressure": "buy",
        "pressure_desc": "매수잔량이 매도잔량의 1.5배 - 매수 압력",
        "spread": 100,
        "spread_pct": 0.143,
        "expected_price": 70050,
        "expected_qty": None,
        "bid_walls": [],
        "ask_walls": [],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("asks", "bids", "expected_spread", "expected_spread_pct"),
    [
        ([], [OrderbookLevel(price=70000, quantity=321)], None, None),
        ([OrderbookLevel(price=70100, quantity=123)], [], None, None),
        (
            [OrderbookLevel(price=70100, quantity=123)],
            [OrderbookLevel(price=0, quantity=321)],
            None,
            None,
        ),
    ],
)
async def test_get_orderbook_returns_null_spread_when_best_prices_unusable(
    monkeypatch: pytest.MonkeyPatch,
    asks: list[OrderbookLevel],
    bids: list[OrderbookLevel],
    expected_spread: int | None,
    expected_spread_pct: float | None,
) -> None:
    from app.mcp_server.tooling import market_data_quotes

    monkeypatch.setattr(
        market_data_quotes.market_data_service,
        "get_orderbook",
        AsyncMock(return_value=_make_snapshot(asks=asks, bids=bids)),
    )
    tools = build_tools()

    result = await tools["get_orderbook"]("005930")

    assert result["spread"] == expected_spread
    assert result["spread_pct"] == expected_spread_pct


@pytest.mark.asyncio
async def test_get_orderbook_returns_error_payload_on_kis_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.mcp_server.tooling import market_data_quotes

    monkeypatch.setattr(
        market_data_quotes.market_data_service,
        "get_orderbook",
        AsyncMock(side_effect=RuntimeError("kis down")),
    )
    tools = build_tools()

    result = await tools["get_orderbook"]("005930")

    assert result == {
        "error": "kis down",
        "source": "kis",
        "symbol": "005930",
        "instrument_type": "equity_kr",
    }


@pytest.mark.asyncio
async def test_get_orderbook_returns_crypto_error_payload_on_upstream_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.mcp_server.tooling import market_data_quotes

    monkeypatch.setattr(
        market_data_quotes.market_data_service,
        "get_orderbook",
        AsyncMock(side_effect=RateLimitError("slow down")),
    )
    tools = build_tools()

    result = await tools["get_orderbook"]("KRW-BTC", market="crypto")

    assert result["error"] == "slow down"
    assert result["source"] == "upbit"
    assert result["symbol"] == "KRW-BTC"
    assert result["instrument_type"] == "crypto"
    assert result["error_type"] == "RateLimitError"


@pytest.mark.asyncio
async def test_get_orderbook_detects_walls_using_side_median_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.mcp_server.tooling import market_data_quotes

    monkeypatch.setattr(
        market_data_quotes.market_data_service,
        "get_orderbook",
        AsyncMock(
            return_value=_make_snapshot(
                symbol="KRW-BTC",
                instrument_type="crypto",
                source="upbit",
                asks=[
                    OrderbookLevel(price=100.0, quantity=1.0),
                    OrderbookLevel(price=101.0, quantity=1.0),
                    OrderbookLevel(price=104.0, quantity=1.0),
                    OrderbookLevel(price=102.0, quantity=5.0),
                    OrderbookLevel(price=103.0, quantity=6.0),
                ],
                bids=[
                    OrderbookLevel(price=99.0, quantity=1.0),
                    OrderbookLevel(price=98.0, quantity=3.0),
                    OrderbookLevel(price=97.0, quantity=1.0),
                    OrderbookLevel(price=95.0, quantity=1.0),
                    OrderbookLevel(price=96.0, quantity=5.0),
                ],
                total_ask_qty=14.0,
                total_bid_qty=11.0,
                bid_ask_ratio=0.79,
                expected_price=None,
                expected_qty=None,
            )
        ),
    )
    tools = build_tools()

    result = await tools["get_orderbook"]("KRW-BTC", market="crypto")

    assert result["ask_walls"] == [
        {"price": 103.0, "size": 6.0, "value_krw": 618},
        {"price": 102.0, "size": 5.0, "value_krw": 510},
    ]
    assert result["bid_walls"] == [
        {"price": 96.0, "size": 5.0, "value_krw": 480},
        {"price": 98.0, "size": 3.0, "value_krw": 294},
    ]


@pytest.mark.asyncio
async def test_get_orderbook_raises_on_invalid_input() -> None:
    tools = build_tools()

    with pytest.raises(ValueError, match="symbol is required"):
        await tools["get_orderbook"]("")

    with pytest.raises(
        ValueError, match="korean equity symbols must be 6 alphanumeric"
    ):
        await tools["get_orderbook"]("AAPL", market="kr")

    with pytest.raises(
        ValueError,
        match="get_orderbook only supports KR equity and KRW crypto markets",
    ):
        await tools["get_orderbook"]("005930", market="us")

    with pytest.raises(
        ValueError, match=r"crypto orderbook only supports KRW-\* symbols"
    ):
        await tools["get_orderbook"]("BTC", market="crypto")

    with pytest.raises(
        ValueError, match=r"crypto orderbook only supports KRW-\* symbols"
    ):
        await tools["get_orderbook"]("USDT-BTC", market="crypto")


# ---------------------------------------------------------------------------
# Venue-aware MCP orderbook tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_orderbook_kr_venue_nxt_threads_to_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.mcp_server.tooling import market_data_quotes

    get_orderbook_mock = AsyncMock(
        return_value=_make_snapshot(
            venue="nxt",
            venue_label="NXT",
            kis_market_code="NX",
            source_endpoint="/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn",
            source_tr_id="FHKST01010200",
            is_empty_book=False,
            requires_final_recheck=False,
            empty_reason=None,
        )
    )
    monkeypatch.setattr(
        market_data_quotes.market_data_service,
        "get_orderbook",
        get_orderbook_mock,
    )
    tools = build_tools()

    result = await tools["get_orderbook"]("005930", market="kr", venue="nxt")

    get_orderbook_mock.assert_awaited_once_with("005930", "kr", venue="nxt")
    assert result["venue"] == "nxt"
    assert result["venue_label"] == "NXT"
    assert result["kis_market_code"] == "NX"
    assert (
        result["source_endpoint"]
        == "/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn"
    )
    assert result["source_tr_id"] == "FHKST01010200"
    assert result["is_empty_book"] is False
    assert result["requires_final_recheck"] is False
    assert "empty_reason" not in result


@pytest.mark.asyncio
async def test_get_orderbook_kr_venue_nxt_includes_empty_reason_when_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.mcp_server.tooling import market_data_quotes

    monkeypatch.setattr(
        market_data_quotes.market_data_service,
        "get_orderbook",
        AsyncMock(
            return_value=_make_snapshot(
                asks=[],
                bids=[],
                total_ask_qty=0,
                total_bid_qty=0,
                bid_ask_ratio=None,
                venue="nxt",
                venue_label="NXT",
                kis_market_code="NX",
                source_endpoint="/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn",
                source_tr_id="FHKST01010200",
                is_empty_book=True,
                requires_final_recheck=True,
                empty_reason="empty_kis_orderbook",
            )
        ),
    )
    tools = build_tools()

    result = await tools["get_orderbook"]("005930", market="kr", venue="nxt")

    assert result["is_empty_book"] is True
    assert result["requires_final_recheck"] is True
    assert result["empty_reason"] == "empty_kis_orderbook"


@pytest.mark.asyncio
async def test_get_orderbook_kr_default_venue_fields_included(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.mcp_server.tooling import market_data_quotes

    monkeypatch.setattr(
        market_data_quotes.market_data_service,
        "get_orderbook",
        AsyncMock(
            return_value=_make_snapshot(
                venue="krx",
                venue_label="KRX",
                kis_market_code="J",
                source_endpoint="/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn",
                source_tr_id="FHKST01010200",
                is_empty_book=False,
                requires_final_recheck=False,
                empty_reason=None,
            )
        ),
    )
    tools = build_tools()

    result = await tools["get_orderbook"]("005930")

    assert result["venue"] == "krx"
    assert result["venue_label"] == "KRX"
    assert result["kis_market_code"] == "J"


@pytest.mark.asyncio
async def test_get_orderbook_crypto_venue_none_not_forwarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.mcp_server.tooling import market_data_quotes

    get_orderbook_mock = AsyncMock(
        return_value=_make_snapshot(
            symbol="KRW-BTC",
            instrument_type="crypto",
            source="upbit",
            expected_price=None,
            expected_qty=None,
        )
    )
    monkeypatch.setattr(
        market_data_quotes.market_data_service,
        "get_orderbook",
        get_orderbook_mock,
    )
    tools = build_tools()

    await tools["get_orderbook"]("KRW-BTC", market="crypto")

    get_orderbook_mock.assert_awaited_once_with("KRW-BTC", "crypto", venue=None)


@pytest.mark.asyncio
async def test_get_orderbook_crypto_rejects_venue() -> None:
    tools = build_tools()

    with pytest.raises(
        ValueError, match="venue is only supported for KR equity orderbook"
    ):
        await tools["get_orderbook"]("KRW-BTC", market="crypto", venue="nxt")


@pytest.mark.asyncio
async def test_get_orderbook_kr_no_venue_diagnostics_when_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.mcp_server.tooling import market_data_quotes

    monkeypatch.setattr(
        market_data_quotes.market_data_service,
        "get_orderbook",
        AsyncMock(return_value=_make_snapshot()),
    )
    tools = build_tools()

    result = await tools["get_orderbook"]("5930")

    assert "venue" not in result
    assert "venue_label" not in result
    assert "kis_market_code" not in result
    assert "source_endpoint" not in result
    assert "source_tr_id" not in result
    assert "is_empty_book" not in result
    assert "requires_final_recheck" not in result
    assert "empty_reason" not in result

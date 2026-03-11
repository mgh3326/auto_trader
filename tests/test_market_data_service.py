from __future__ import annotations

import datetime as dt
from unittest.mock import AsyncMock

import httpx
import pandas as pd
import pytest

from app.services.domain_errors import (
    RateLimitError,
    SymbolNotFoundError,
    UpstreamUnavailableError,
    ValidationError,
)
from app.services.market_data import service as market_data_service
from app.services.market_data.contracts import Candle, OrderbookLevel, OrderbookSnapshot
from app.services.upbit_symbol_universe_service import UpbitSymbolUniverseLookupError
from app.services.us_symbol_universe_service import (
    USSymbolInactiveError,
    USSymbolNotRegisteredError,
    USSymbolUniverseEmptyError,
)


@pytest.mark.asyncio
async def test_get_kr_volume_rank_returns_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = [{"mksc_shrn_iscd": "005930", "acml_vol": "12345", "prdy_ctrt": "-3.2"}]

    class DummyKIS:
        async def volume_rank(self):
            return expected

    monkeypatch.setattr(market_data_service, "KISClient", lambda: DummyKIS())

    actual = await market_data_service.get_kr_volume_rank()

    assert actual == expected


@pytest.mark.asyncio
async def test_get_kr_volume_rank_maps_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    class FailingKIS:
        async def volume_rank(self):
            raise RuntimeError("upstream failed")

    monkeypatch.setattr(market_data_service, "KISClient", lambda: FailingKIS())

    with pytest.raises(UpstreamUnavailableError, match="upstream failed"):
        await market_data_service.get_kr_volume_rank()


@pytest.mark.asyncio
async def test_get_ohlcv_crypto_5m_passes_through_to_upbit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = pd.DataFrame(
        [
            {
                "datetime": pd.Timestamp("2024-01-01 09:30:00"),
                "open": 100.0,
                "high": 110.0,
                "low": 90.0,
                "close": 105.0,
                "volume": 1000.0,
                "value": 105000.0,
            }
        ]
    )
    fetch_mock = AsyncMock(return_value=frame)
    monkeypatch.setattr(market_data_service, "fetch_upbit_ohlcv", fetch_mock)

    candles = await market_data_service.get_ohlcv(
        symbol="KRW-BTC",
        market="crypto",
        period="5m",
        count=250,
    )

    fetch_mock.assert_awaited_once_with(
        market="KRW-BTC",
        days=200,
        period="5m",
        end_date=None,
    )
    assert len(candles) == 1
    assert candles[0].period == "5m"
    assert candles[0].market == "crypto"


@pytest.mark.asyncio
async def test_get_ohlcv_rejects_invalid_period_message() -> None:
    with pytest.raises(
        ValidationError,
        match="period must be 'day', 'week', 'month', '1m', '5m', '15m', '30m', '4h', or '1h'",
    ):
        await market_data_service.get_ohlcv(
            symbol="AAPL",
            market="us",
            period="hour",
            count=10,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("period", ["1m", "5m", "15m", "30m", "1h"])
async def test_get_ohlcv_us_intraday_uses_reader(
    monkeypatch: pytest.MonkeyPatch,
    period: str,
) -> None:
    frame = pd.DataFrame(
        [
            {
                "datetime": pd.Timestamp("2026-02-23 10:30:00"),
                "date": dt.date(2026, 2, 23),
                "time": dt.time(10, 30, 0),
                "open": 150.0,
                "high": 151.0,
                "low": 149.5,
                "close": 150.5,
                "volume": 5000.0,
                "value": 752500.0,
                "session": "REGULAR",
            }
        ]
    )
    read_mock = AsyncMock(return_value=frame)
    monkeypatch.setattr(market_data_service, "read_us_intraday_candles", read_mock)

    candles = await market_data_service.get_ohlcv(
        symbol="AAPL",
        market="us",
        period=period,
        count=3,
    )

    read_mock.assert_awaited_once_with(
        symbol="AAPL",
        period=period,
        count=3,
        end_date=None,
    )
    assert len(candles) == 1
    assert candles[0].symbol == "AAPL"
    assert candles[0].source == "kis"
    assert candles[0].period == period
    assert isinstance(candles[0], Candle)
    assert candles[0].timestamp == dt.datetime(2026, 2, 23, 10, 30, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exc_type", "message"),
    [
        (
            USSymbolUniverseEmptyError,
            "us_symbol_universe is empty. Sync required: uv run python scripts/sync_us_symbol_universe.py",
        ),
        (
            USSymbolNotRegisteredError,
            "US symbol 'AAPL' is not registered in us_symbol_universe. Sync required: uv run python scripts/sync_us_symbol_universe.py",
        ),
        (
            USSymbolInactiveError,
            "US symbol 'AAPL' is inactive in us_symbol_universe. Sync required: uv run python scripts/sync_us_symbol_universe.py",
        ),
    ],
)
async def test_get_ohlcv_us_intraday_propagates_universe_lookup_errors(
    monkeypatch: pytest.MonkeyPatch,
    exc_type: type[Exception],
    message: str,
) -> None:
    read_mock = AsyncMock(side_effect=exc_type(message))
    monkeypatch.setattr(market_data_service, "read_us_intraday_candles", read_mock)

    with pytest.raises(exc_type, match=message):
        await market_data_service.get_ohlcv(
            symbol="AAPL",
            market="us",
            period="5m",
            count=3,
        )


@pytest.mark.asyncio
async def test_get_ohlcv_us_day_uses_yahoo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = pd.DataFrame(
        [
            {
                "date": dt.date(2026, 2, 23),
                "open": 150.0,
                "high": 151.0,
                "low": 149.5,
                "close": 150.5,
                "volume": 5000.0,
                "value": 752500.0,
            }
        ]
    )
    fetch_mock = AsyncMock(return_value=frame)
    monkeypatch.setattr(market_data_service, "fetch_yahoo_ohlcv", fetch_mock)

    candles = await market_data_service.get_ohlcv(
        symbol="AAPL",
        market="us",
        period="day",
        count=3,
    )

    fetch_mock.assert_awaited_once_with(
        ticker="AAPL",
        days=3,
        period="day",
        end_date=None,
    )
    assert len(candles) == 1
    assert candles[0].source == "yahoo"
    assert candles[0].period == "day"


@pytest.mark.asyncio
async def test_get_ohlcv_kr_intraday_uses_shared_reader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = pd.DataFrame(
        [
            {
                "datetime": pd.Timestamp("2026-02-23 09:05:00"),
                "date": dt.date(2026, 2, 23),
                "time": dt.time(9, 5, 0),
                "open": 100.0,
                "high": 101.0,
                "low": 99.5,
                "close": 100.5,
                "volume": 1200.0,
                "value": 120000.0,
                "session": "REGULAR",
                "venues": ["KRX", "NTX"],
            }
        ]
    )
    read_mock = AsyncMock(return_value=frame)
    monkeypatch.setattr(market_data_service, "read_kr_intraday_candles", read_mock)

    candles = await market_data_service.get_ohlcv(
        symbol="005930",
        market="kr",
        period="5m",
        count=3,
    )

    read_mock.assert_awaited_once_with(
        symbol="005930",
        period="5m",
        count=3,
        end_date=None,
    )
    assert len(candles) == 1
    candle = candles[0]
    assert isinstance(candle, Candle)
    assert candle.symbol == "005930"
    assert candle.market == "equity_kr"
    assert candle.period == "5m"
    assert candle.timestamp == dt.datetime(2026, 2, 23, 9, 5, 0)


@pytest.mark.asyncio
async def test_get_orderbook_parses_kr_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class DummyKIS:
        async def inquire_orderbook_snapshot(self, code: str, market: str = "UN"):
            assert code == "005930"
            assert market == "UN"
            return (
                {
                    "askp1": "70100",
                    "askp_rsqn1": "123",
                    "askp2": "0",
                    "askp_rsqn2": "999",
                    "bidp1": "70000",
                    "bidp_rsqn1": "321",
                    "total_askp_rsqn": "1000",
                    "total_bidp_rsqn": "1500",
                },
                {"antc_cnpr": "70050", "antc_cnqn": "42"},
            )

    monkeypatch.setattr(market_data_service, "KISClient", lambda: DummyKIS())

    snapshot = await market_data_service.get_orderbook("5930", "kr")

    assert snapshot == OrderbookSnapshot(
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
    assert type(snapshot.asks[0].price) is int
    assert type(snapshot.asks[0].quantity) is int
    assert type(snapshot.total_ask_qty) is int
    assert type(snapshot.total_bid_qty) is int


@pytest.mark.asyncio
async def test_get_orderbook_falls_back_to_legacy_quantity_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class DummyKIS:
        async def inquire_orderbook_snapshot(self, code: str, market: str = "UN"):
            return (
                {
                    "askp1": "70200",
                    "askp1_rsqn": "44",
                    "bidp1": "69900",
                    "bidp1_rsqn": "55",
                    "total_askp_rsqn": "44",
                    "total_bidp_rsqn": "55",
                },
                None,
            )

    monkeypatch.setattr(market_data_service, "KISClient", lambda: DummyKIS())

    snapshot = await market_data_service.get_orderbook("005930", "kospi")

    assert snapshot.asks == [OrderbookLevel(price=70200, quantity=44)]
    assert snapshot.bids == [OrderbookLevel(price=69900, quantity=55)]
    assert snapshot.expected_price is None
    assert snapshot.expected_qty is None


@pytest.mark.asyncio
async def test_get_orderbook_defaults_market_to_kr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class DummyKIS:
        async def inquire_orderbook_snapshot(self, code: str, market: str = "UN"):
            assert code == "005930"
            assert market == "UN"
            return (
                {
                    "askp1": "70200",
                    "askp_rsqn1": "44",
                    "bidp1": "69900",
                    "bidp_rsqn1": "55",
                    "total_askp_rsqn": "44",
                    "total_bidp_rsqn": "55",
                },
                None,
            )

    monkeypatch.setattr(market_data_service, "KISClient", lambda: DummyKIS())

    snapshot = await market_data_service.get_orderbook("5930")

    assert snapshot.symbol == "005930"
    assert snapshot.instrument_type == "equity_kr"


@pytest.mark.asyncio
async def test_get_orderbook_returns_none_ratio_when_total_ask_is_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class DummyKIS:
        async def inquire_orderbook_snapshot(self, code: str, market: str = "UN"):
            return (
                {
                    "askp1": "70100",
                    "askp_rsqn1": "10",
                    "bidp1": "70000",
                    "bidp_rsqn1": "20",
                    "total_askp_rsqn": "0",
                    "total_bidp_rsqn": "20",
                },
                None,
            )

    monkeypatch.setattr(market_data_service, "KISClient", lambda: DummyKIS())

    snapshot = await market_data_service.get_orderbook("005930", "kr")

    assert snapshot.bid_ask_ratio is None


@pytest.mark.asyncio
async def test_get_orderbook_parses_crypto_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        market_data_service,
        "fetch_orderbook",
        AsyncMock(
            return_value={
                "market": "KRW-BTC",
                "timestamp": 1730000000,
                "total_ask_size": 3.75,
                "total_bid_size": 7.5,
                "orderbook_units": [
                    {
                        "ask_price": 140.1,
                        "bid_price": 139.9,
                        "ask_size": 1.25,
                        "bid_size": 2.5,
                    }
                ],
            }
        ),
    )

    snapshot = await market_data_service.get_orderbook("KRW-BTC", "crypto")

    assert snapshot == OrderbookSnapshot(
        symbol="KRW-BTC",
        instrument_type="crypto",
        source="upbit",
        asks=[OrderbookLevel(price=140.1, quantity=1.25)],
        bids=[OrderbookLevel(price=139.9, quantity=2.5)],
        total_ask_qty=3.75,
        total_bid_qty=7.5,
        bid_ask_ratio=2.0,
        expected_price=None,
        expected_qty=None,
    )
    assert type(snapshot.asks[0].price) is float
    assert type(snapshot.asks[0].quantity) is float
    assert type(snapshot.total_ask_qty) is float
    assert type(snapshot.total_bid_qty) is float


@pytest.mark.asyncio
async def test_get_orderbook_crypto_returns_none_ratio_when_total_ask_is_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        market_data_service,
        "fetch_orderbook",
        AsyncMock(
            return_value={
                "market": "KRW-BTC",
                "timestamp": 1730000000,
                "total_ask_size": 0.0,
                "total_bid_size": 2.0,
                "orderbook_units": [
                    {
                        "ask_price": 10.1,
                        "bid_price": 10.0,
                        "ask_size": 0.0,
                        "bid_size": 2.0,
                    }
                ],
            }
        ),
    )

    snapshot = await market_data_service.get_orderbook("KRW-BTC", "crypto")

    assert snapshot.bid_ask_ratio is None


@pytest.mark.asyncio
@pytest.mark.parametrize("symbol", ["BTC", "USDT-BTC"])
async def test_get_orderbook_crypto_rejects_non_krw_raw_symbols(symbol: str) -> None:
    with pytest.raises(
        ValueError, match=r"crypto orderbook only supports KRW-\* symbols"
    ):
        await market_data_service.get_orderbook(symbol, "crypto")


@pytest.mark.asyncio
async def test_get_orderbook_crypto_maps_empty_response_to_symbol_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        market_data_service,
        "fetch_orderbook",
        AsyncMock(return_value={}),
    )

    with pytest.raises(SymbolNotFoundError, match="Symbol 'KRW-BTC' not found"):
        await market_data_service.get_orderbook("KRW-BTC", "crypto")


def _make_http_status_error(status_code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://api.upbit.com/v1/orderbook?markets=KRW-BTC")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError(
        f"HTTP {status_code}",
        request=request,
        response=response,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [418, 429])
async def test_get_orderbook_crypto_maps_rate_limit_statuses(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
) -> None:
    monkeypatch.setattr(
        market_data_service,
        "fetch_orderbook",
        AsyncMock(side_effect=_make_http_status_error(status_code)),
    )

    with pytest.raises(RateLimitError, match=f"HTTP {status_code}"):
        await market_data_service.get_orderbook("KRW-BTC", "crypto")


@pytest.mark.asyncio
async def test_get_orderbook_crypto_preserves_upbit_lookup_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        market_data_service,
        "fetch_orderbook",
        AsyncMock(side_effect=UpbitSymbolUniverseLookupError("sync required")),
    )

    with pytest.raises(UpbitSymbolUniverseLookupError, match="sync required"):
        await market_data_service.get_orderbook("KRW-BTC", "crypto")


@pytest.mark.asyncio
async def test_get_orderbook_crypto_maps_provider_value_error_to_upstream_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        market_data_service,
        "fetch_orderbook",
        AsyncMock(side_effect=ValueError("malformed provider payload")),
    )

    with pytest.raises(UpstreamUnavailableError, match="malformed provider payload"):
        await market_data_service.get_orderbook("KRW-BTC", "crypto")


@pytest.mark.asyncio
@pytest.mark.parametrize("market", ["us"])
async def test_get_orderbook_rejects_unsupported_markets(market: str) -> None:
    with pytest.raises(
        ValueError,
        match="get_orderbook only supports KR equity and KRW crypto markets",
    ):
        await market_data_service.get_orderbook("005930", market)

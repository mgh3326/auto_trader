from __future__ import annotations

import datetime as dt
from unittest.mock import AsyncMock

import pandas as pd
import pytest

from app.services.domain_errors import UpstreamUnavailableError, ValidationError
from app.services.market_data import service as market_data_service
from app.services.market_data.contracts import Candle, OrderbookLevel, OrderbookSnapshot


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
async def test_get_ohlcv_non_kr_minute_period_rejected_for_us() -> None:
    with pytest.raises(
        ValidationError,
        match="period '5m' is not supported for us equity",
    ):
        await market_data_service.get_ohlcv(
            symbol="AAPL",
            market="us",
            period="5m",
            count=10,
        )


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
@pytest.mark.parametrize("market", ["us", "crypto"])
async def test_get_orderbook_rejects_non_kr_markets(market: str) -> None:
    with pytest.raises(ValueError, match="KR orderbook only supports KR market"):
        await market_data_service.get_orderbook("005930", market)

from __future__ import annotations

import datetime as dt
import logging
from typing import override
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
        _ = await market_data_service.get_kr_volume_rank()


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
        _ = await market_data_service.get_ohlcv(
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
        _ = await market_data_service.get_ohlcv(
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
        async def inquire_orderbook_snapshot(self, code: str, market: str = "J"):
            assert code == "005930"
            assert market == "J"
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
async def test_get_orderbook_normalizes_blank_expected_qty_and_logs_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class DummyKIS:
        async def inquire_orderbook_snapshot(self, code: str, market: str = "J"):
            return (
                {
                    "askp1": "70100",
                    "askp_rsqn1": "123",
                    "bidp1": "70000",
                    "bidp_rsqn1": "321",
                    "total_askp_rsqn": "1000",
                    "total_bidp_rsqn": "1500",
                },
                {"antc_cnpr": "70050", "antc_cnqn": ""},
            )

    monkeypatch.setattr(market_data_service, "KISClient", lambda: DummyKIS())

    with caplog.at_level(logging.INFO):
        snapshot = await market_data_service.get_orderbook("005930", "kr")

    assert snapshot.expected_price == 70050
    assert snapshot.expected_qty is None

    messages = [
        record.message
        for record in caplog.records
        if "expected_qty unavailable" in record.message
    ]
    assert len(messages) == 1
    message = messages[0]
    assert "symbol=005930" in message
    assert "session_hint=" in message
    assert "antc_cnpr='70050'" in message
    assert "antc_cnqn=''" in message
    assert "output2_keys=['antc_cnpr', 'antc_cnqn']" in message
    assert "askp1" not in message
    assert "total_askp_rsqn" not in message


@pytest.mark.asyncio
async def test_get_orderbook_normalizes_missing_expected_qty_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class DummyKIS:
        async def inquire_orderbook_snapshot(self, code: str, market: str = "J"):
            return (
                {
                    "askp1": "70100",
                    "askp_rsqn1": "123",
                    "bidp1": "70000",
                    "bidp_rsqn1": "321",
                    "total_askp_rsqn": "1000",
                    "total_bidp_rsqn": "1500",
                },
                {"antc_cnpr": "70050"},
            )

    monkeypatch.setattr(market_data_service, "KISClient", lambda: DummyKIS())

    snapshot = await market_data_service.get_orderbook("005930", "kr")

    assert snapshot.expected_price == 70050
    assert snapshot.expected_qty is None


@pytest.mark.asyncio
async def test_get_orderbook_normalizes_none_expected_qty_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class DummyKIS:
        async def inquire_orderbook_snapshot(self, code: str, market: str = "J"):
            return (
                {
                    "askp1": "70100",
                    "askp_rsqn1": "123",
                    "bidp1": "70000",
                    "bidp_rsqn1": "321",
                    "total_askp_rsqn": "1000",
                    "total_bidp_rsqn": "1500",
                },
                {"antc_cnpr": "70050", "antc_cnqn": None},
            )

    monkeypatch.setattr(market_data_service, "KISClient", lambda: DummyKIS())

    snapshot = await market_data_service.get_orderbook("005930", "kr")

    assert snapshot.expected_price == 70050
    assert snapshot.expected_qty is None


@pytest.mark.asyncio
async def test_get_orderbook_logs_diagnostics_when_output2_is_none(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class DummyKIS:
        async def inquire_orderbook_snapshot(self, code: str, market: str = "J"):
            return (
                {
                    "askp1": "70100",
                    "askp_rsqn1": "123",
                    "bidp1": "70000",
                    "bidp_rsqn1": "321",
                    "total_askp_rsqn": "1000",
                    "total_bidp_rsqn": "1500",
                },
                None,
            )

    monkeypatch.setattr(market_data_service, "KISClient", lambda: DummyKIS())

    with caplog.at_level(logging.INFO):
        snapshot = await market_data_service.get_orderbook("005930", "kr")

    assert snapshot.expected_price is None
    assert snapshot.expected_qty is None

    messages = [
        record.message
        for record in caplog.records
        if "expected_qty unavailable" in record.message
    ]
    assert len(messages) == 1
    message = messages[0]
    assert "symbol=005930" in message
    assert "session_hint=" in message
    assert "antc_cnpr=None" in message
    assert "antc_cnqn=None" in message
    assert "output2_keys=[]" in message
    assert "askp1" not in message
    assert "total_askp_rsqn" not in message


@pytest.mark.asyncio
async def test_get_orderbook_keeps_zero_expected_qty_without_diagnostic_log(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class DummyKIS:
        async def inquire_orderbook_snapshot(self, code: str, market: str = "J"):
            return (
                {
                    "askp1": "70100",
                    "askp_rsqn1": "123",
                    "bidp1": "70000",
                    "bidp_rsqn1": "321",
                    "total_askp_rsqn": "1000",
                    "total_bidp_rsqn": "1500",
                },
                {"antc_cnpr": "70050", "antc_cnqn": "0"},
            )

    monkeypatch.setattr(market_data_service, "KISClient", lambda: DummyKIS())

    with caplog.at_level(logging.INFO):
        snapshot = await market_data_service.get_orderbook("005930", "kr")

    assert snapshot.expected_price == 70050
    assert snapshot.expected_qty == 0
    assert all(
        "expected_qty unavailable" not in record.message for record in caplog.records
    )


@pytest.mark.asyncio
async def test_get_orderbook_falls_back_to_legacy_quantity_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class DummyKIS:
        async def inquire_orderbook_snapshot(self, code: str, market: str = "J"):
            assert code == "005930"
            assert market == "J"
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
        async def inquire_orderbook_snapshot(self, code: str, market: str = "J"):
            assert code == "005930"
            assert market == "J"
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
        async def inquire_orderbook_snapshot(self, code: str, market: str = "J"):
            assert code == "005930"
            assert market == "J"
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


@pytest.mark.asyncio
async def test_get_short_interest_maps_rows_and_uses_naver_name_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FixedDate(dt.date):
        @override
        @classmethod
        def today(cls) -> FixedDate:
            return cls(2026, 2, 20)

    class DummyKIS:
        async def inquire_short_selling(
            self,
            code: str,
            start_date: dt.date,
            end_date: dt.date,
            market: str = "J",
        ) -> tuple[dict[str, str], list[dict[str, str]]]:
            assert code == "005930"
            assert start_date == dt.date(2026, 2, 16)
            assert end_date == dt.date(2026, 2, 20)
            assert market == "J"
            return (
                {},
                [
                    {
                        "stck_bsop_date": "20260219",
                        "ssts_cntg_qty": "100",
                        "ssts_tr_pbmn": "1000",
                        "ssts_vol_rlim": "5.5",
                        "acml_vol": "2000",
                        "acml_tr_pbmn": "25000",
                    },
                    {
                        "stck_bsop_date": "20260220",
                        "ssts_cntg_qty": "80",
                        "ssts_tr_pbmn": "900",
                        "ssts_vol_rlim": "",
                        "acml_vol": "1500",
                        "acml_tr_pbmn": "20000",
                    },
                    {
                        "stck_bsop_date": "20260218",
                        "ssts_cntg_qty": "60",
                        "ssts_tr_pbmn": "700",
                        "ssts_vol_rlim": "3.0",
                        "acml_vol": "1000",
                        "acml_tr_pbmn": "15000",
                    },
                ],
            )

        async def fetch_fundamental_info(
            self, _code: str, _market: str = "J"
        ) -> dict[str, str]:
            raise AssertionError("KIS fundamental fallback should not be used")

    async def fake_fetch_company_profile(code: str) -> dict[str, str | None]:
        assert code == "005930"
        return {"name": "삼성전자"}

    monkeypatch.setattr("app.services.market_data.service.dt.date", FixedDate)
    monkeypatch.setattr(market_data_service, "KISClient", lambda: DummyKIS())
    monkeypatch.setattr(
        "app.services.market_data.service.naver_finance.fetch_company_profile",
        fake_fetch_company_profile,
    )

    assert hasattr(market_data_service, "get_short_interest")

    result = await market_data_service.get_short_interest("5930", days=2)

    assert result == {
        "symbol": "005930",
        "name": "삼성전자",
        "short_data": [
            {
                "date": "2026-02-20",
                "short_volume": 80,
                "short_amount": 900,
                "short_ratio": None,
                "total_volume": 1500,
                "total_amount": 20000,
            },
            {
                "date": "2026-02-19",
                "short_volume": 100,
                "short_amount": 1000,
                "short_ratio": 5.5,
                "total_volume": 2000,
                "total_amount": 25000,
            },
        ],
        "avg_short_ratio": 5.5,
    }


@pytest.mark.asyncio
async def test_get_short_interest_returns_clean_no_data_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FixedDate(dt.date):
        @override
        @classmethod
        def today(cls) -> FixedDate:
            return cls(2026, 2, 20)

    class DummyKIS:
        async def inquire_short_selling(
            self,
            code: str,
            start_date: dt.date,
            end_date: dt.date,
            market: str = "J",
        ) -> tuple[dict[str, str], list[dict[str, str]]]:
            assert code == "005930"
            assert start_date == dt.date(2026, 2, 18)
            assert end_date == dt.date(2026, 2, 20)
            assert market == "J"
            return ({}, [])

        async def fetch_fundamental_info(
            self, _code: str, _market: str = "J"
        ) -> dict[str, str]:
            raise AssertionError("KIS fundamental fallback should not be used")

    async def fake_fetch_company_profile(_code: str) -> dict[str, str | None]:
        raise RuntimeError("lookup failed")

    monkeypatch.setattr("app.services.market_data.service.dt.date", FixedDate)
    monkeypatch.setattr(market_data_service, "KISClient", lambda: DummyKIS())
    monkeypatch.setattr(
        "app.services.market_data.service.naver_finance.fetch_company_profile",
        fake_fetch_company_profile,
    )

    assert hasattr(market_data_service, "get_short_interest")

    result = await market_data_service.get_short_interest("005930", days=0)

    assert result == {
        "symbol": "005930",
        "name": None,
        "short_data": [],
        "avg_short_ratio": None,
    }
    assert "short_balance" not in result


@pytest.mark.asyncio
async def test_get_short_interest_caps_days_above_60(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FixedDate(dt.date):
        @override
        @classmethod
        def today(cls) -> FixedDate:
            return cls(2026, 2, 20)

    class DummyKIS:
        async def inquire_short_selling(
            self,
            code: str,
            start_date: dt.date,
            end_date: dt.date,
            market: str = "J",
        ) -> tuple[dict[str, str], list[dict[str, str]]]:
            assert code == "005930"
            assert start_date == dt.date(2025, 10, 23)
            assert end_date == dt.date(2026, 2, 20)
            assert market == "J"
            return ({"hts_kor_isnm": "삼성전자"}, [])

    monkeypatch.setattr("app.services.market_data.service.dt.date", FixedDate)
    monkeypatch.setattr(market_data_service, "KISClient", lambda: DummyKIS())

    result = await market_data_service.get_short_interest("005930", days=100)

    assert result["symbol"] == "005930"
    assert result["name"] == "삼성전자"
    assert result["short_data"] == []
    assert result["avg_short_ratio"] is None


@pytest.mark.asyncio
async def test_get_short_interest_drops_rows_with_malformed_dates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class DummyKIS:
        async def inquire_short_selling(
            self,
            code: str,
            start_date: dt.date,
            end_date: dt.date,
            market: str = "J",
        ) -> tuple[dict[str, str], list[dict[str, str]]]:
            assert code == "005930"
            assert start_date <= end_date
            assert market == "J"
            return (
                {"hts_kor_isnm": "삼성전자"},
                [
                    {
                        "stck_bsop_date": "bad-date",
                        "ssts_cntg_qty": "10",
                        "ssts_tr_pbmn": "100",
                        "ssts_vol_rlim": "1.5",
                        "acml_vol": "500",
                        "acml_tr_pbmn": "1000",
                    },
                    {
                        "stck_bsop_date": "20260219",
                        "ssts_cntg_qty": "20",
                        "ssts_tr_pbmn": "200",
                        "ssts_vol_rlim": "2.5",
                        "acml_vol": "600",
                        "acml_tr_pbmn": "1200",
                    },
                ],
            )

    monkeypatch.setattr(market_data_service, "KISClient", lambda: DummyKIS())

    result = await market_data_service.get_short_interest("005930", days=20)

    assert result["short_data"] == [
        {
            "date": "2026-02-19",
            "short_volume": 20,
            "short_amount": 200,
            "short_ratio": 2.5,
            "total_volume": 600,
            "total_amount": 1200,
        }
    ]
    assert result["avg_short_ratio"] == 2.5


@pytest.mark.asyncio
async def test_get_short_interest_maps_upstream_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class DummyKIS:
        async def inquire_short_selling(
            self,
            code: str,
            start_date: dt.date,
            end_date: dt.date,
            market: str = "J",
        ) -> tuple[dict[str, str], list[dict[str, str]]]:
            assert code == "005930"
            assert start_date <= end_date
            assert market == "J"
            raise RuntimeError("short data unavailable")

    monkeypatch.setattr(market_data_service, "KISClient", lambda: DummyKIS())

    assert hasattr(market_data_service, "get_short_interest")

    with pytest.raises(UpstreamUnavailableError, match="short data unavailable"):
        _ = await market_data_service.get_short_interest("005930")


@pytest.mark.asyncio
async def test_get_short_interest_maps_kis_construction_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failing_client() -> object:
        raise RuntimeError("kis init failed")

    monkeypatch.setattr(market_data_service, "KISClient", failing_client)

    with pytest.raises(UpstreamUnavailableError, match="kis init failed"):
        _ = await market_data_service.get_short_interest("005930")


def test_market_data_exports_get_short_interest() -> None:
    from app.services import market_data

    assert getattr(market_data, "get_short_interest", None) is getattr(
        market_data_service, "get_short_interest", None
    )

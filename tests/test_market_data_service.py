from __future__ import annotations

from unittest.mock import AsyncMock

import pandas as pd
import pytest

from app.services.domain_errors import UpstreamUnavailableError, ValidationError
from app.services.market_data import service as market_data_service


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
async def test_get_ohlcv_non_crypto_5m_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetch_mock = AsyncMock()
    monkeypatch.setattr(market_data_service, "fetch_upbit_ohlcv", fetch_mock)

    with pytest.raises(
        ValidationError, match="period '5m' is supported only for crypto"
    ):
        await market_data_service.get_ohlcv(
            symbol="AAPL",
            market="us",
            period="5m",
            count=10,
        )

    fetch_mock.assert_not_awaited()

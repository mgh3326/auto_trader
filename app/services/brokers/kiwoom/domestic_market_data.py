# app/services/brokers/kiwoom/domestic_market_data.py
"""Deferred Kiwoom chart-data skeleton (ka10080/ka10081/ka10082/ka10083).

Intentionally not wired into ``get_ohlcv`` — KIS remains the default KR OHLCV
source per ROB-97. A future issue may add ``source="kiwoom"`` for KIS rate
limit relief; until then every method raises ``NotImplementedError`` so any
accidental call is loud.
"""

from __future__ import annotations

from app.services.brokers.kiwoom import constants


class KiwoomDomesticMarketDataClient:
    """Placeholder for Kiwoom KRX chart endpoints (deferred)."""

    SUPPORTED_API_IDS = (
        constants.CHART_MINUTE_API_ID,
        constants.CHART_DAILY_API_ID,
        constants.CHART_WEEKLY_API_ID,
        constants.CHART_MONTHLY_API_ID,
    )

    async def fetch_minute_candles(self, *_, **__) -> None:  # pragma: no cover
        raise NotImplementedError(
            "Kiwoom chart support is deferred; default OHLCV source remains KIS."
        )

    async def fetch_daily_candles(self, *_, **__) -> None:  # pragma: no cover
        raise NotImplementedError(
            "Kiwoom chart support is deferred; default OHLCV source remains KIS."
        )

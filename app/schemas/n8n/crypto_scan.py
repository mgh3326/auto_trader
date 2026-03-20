# app/schemas/n8n/crypto_scan.py
"""Schemas for the n8n crypto scan endpoint."""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.schemas.n8n.common import N8nFearGreedData

__all__ = [
    "N8nCryptoScanParams",
    "N8nBtcContext",
    "N8nCryptoScanIndicators",
    "N8nSmaCross",
    "N8nCrashData",
    "N8nCryptoScanCoin",
    "N8nCryptoScanSummary",
    "N8nCryptoScanResponse",
]


class N8nCryptoScanParams(BaseModel):
    """Echo of the scan parameters used for this response."""

    top_n: int = Field(..., description="Top N coins by 24h trade amount")
    include_holdings: bool = Field(...)
    include_crash: bool = Field(...)
    include_sma_cross: bool = Field(...)
    include_fear_greed: bool = Field(...)
    ohlcv_days: int = Field(...)


class N8nBtcContext(BaseModel):
    """BTC technical context for market backdrop."""

    rsi14: float | None = Field(None, description="BTC RSI 14-period")
    sma20: float | None = Field(None, description="BTC SMA 20")
    sma60: float | None = Field(None, description="BTC SMA 60")
    sma200: float | None = Field(None, description="BTC SMA 200")
    current_price: float | None = Field(None, description="BTC current price in KRW")
    change_rate_24h: float | None = Field(None, description="BTC 24h change rate")


class N8nCryptoScanIndicators(BaseModel):
    """Per-coin technical indicators."""

    rsi14: float | None = Field(None, description="RSI 14-period")
    sma20: float | None = Field(None, description="SMA 20")
    sma60: float | None = Field(None, description="SMA 60")
    sma200: float | None = Field(None, description="SMA 200")


class N8nSmaCross(BaseModel):
    """SMA20 crossing event data."""

    type: str = Field(..., description="golden or dead")
    prev_close: float = Field(...)
    curr_close: float = Field(...)
    prev_sma20: float = Field(...)
    curr_sma20: float = Field(...)


class N8nCrashData(BaseModel):
    """Crash detection data per coin."""

    change_rate_24h: float = Field(..., description="Actual 24h change rate")
    threshold: float = Field(..., description="Crash threshold for this coin's rank")
    triggered: bool = Field(..., description="Whether abs(change) >= threshold")


class N8nCryptoScanCoin(BaseModel):
    """Single coin in the crypto scan response."""

    symbol: str = Field(..., description="Upbit market code, e.g. KRW-BTC")
    currency: str = Field(..., description="Currency code, e.g. BTC")
    name: str = Field(..., description="Korean name, e.g. 비트코인")
    rank: int | None = Field(
        None, description="Trade amount rank (1-based), null if holding-only"
    )
    is_holding: bool = Field(..., description="Whether user currently holds this coin")
    current_price: float | None = Field(None, description="Current trade price in KRW")
    change_rate_24h: float | None = Field(None, description="24h signed change rate")
    trade_amount_24h: float | None = Field(
        None, description="24h accumulated trade amount in KRW"
    )
    indicators: N8nCryptoScanIndicators = Field(...)
    sma_cross: N8nSmaCross | None = Field(None)
    crash: N8nCrashData | None = Field(None)


class N8nCryptoScanSummary(BaseModel):
    """Aggregate summary of the scan results."""

    total_scanned: int = Field(..., description="Total coins scanned")
    top_n_count: int = Field(..., description="Coins from top N by trade amount")
    holdings_added: int = Field(
        ..., description="Extra coins added because they are held"
    )
    oversold_count: int = Field(0, description="Coins with RSI < 35 (reference only)")
    overbought_count: int = Field(0, description="Coins with RSI > 70 (reference only)")
    crash_triggered_count: int = Field(
        0, description="Coins that triggered crash threshold"
    )
    sma_golden_cross_count: int = Field(0, description="SMA20 golden cross coins")
    sma_dead_cross_count: int = Field(0, description="SMA20 dead cross coins")


class N8nCryptoScanResponse(BaseModel):
    """Top-level response for GET /api/n8n/crypto-scan."""

    success: bool = Field(...)
    as_of: str = Field(..., description="Response timestamp in KST ISO8601")
    scan_params: N8nCryptoScanParams = Field(...)
    btc_context: N8nBtcContext = Field(...)
    fear_greed: N8nFearGreedData | None = Field(None)
    coins: list[N8nCryptoScanCoin] = Field(default_factory=list)
    summary: N8nCryptoScanSummary = Field(...)
    errors: list[dict[str, object]] = Field(default_factory=list)

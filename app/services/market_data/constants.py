from __future__ import annotations

OHLCV_ALLOWED_PERIODS = (
    "day",
    "week",
    "month",
    "1m",
    "5m",
    "15m",
    "30m",
    "4h",
    "1h",
)

CRYPTO_ONLY_OHLCV_PERIODS = frozenset({"1m", "5m", "15m", "30m", "4h"})
CRYPTO_MINUTE_OHLCV_PERIODS = frozenset({"1m", "5m", "15m", "30m"})
CRYPTO_MINUTE_PUBLIC_ROW_KEYS = (
    "timestamp",
    "date",
    "time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "value",
    "trade_amount",
)
CRYPTO_MINUTE_REQUIRED_SOURCE_COLUMNS = (
    "date",
    "time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "value",
)
OHLCV_PERIOD_ERROR = (
    "period must be 'day', 'week', 'month', '1m', '5m', '15m', '30m', '4h', or '1h'"
)

__all__ = [
    "CRYPTO_MINUTE_OHLCV_PERIODS",
    "CRYPTO_MINUTE_PUBLIC_ROW_KEYS",
    "CRYPTO_MINUTE_REQUIRED_SOURCE_COLUMNS",
    "CRYPTO_ONLY_OHLCV_PERIODS",
    "OHLCV_ALLOWED_PERIODS",
    "OHLCV_PERIOD_ERROR",
]

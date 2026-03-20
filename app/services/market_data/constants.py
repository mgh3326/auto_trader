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

CRYPTO_ONLY_OHLCV_PERIODS = frozenset({"4h"})
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

ALL_OHLCV_MARKETS = frozenset({"equity_kr", "equity_us", "crypto"})
KR_OHLCV_PERIODS = frozenset({"day", "week", "month", "1m", "5m", "15m", "30m", "1h"})
KR_INTRADAY_OHLCV_PERIODS = frozenset({"1m", "5m", "15m", "30m", "1h"})
US_INTRADAY_OHLCV_PERIODS = frozenset({"1m", "5m", "15m", "30m", "1h"})
US_OHLCV_PERIODS = frozenset({"day", "week", "month", "1m", "5m", "15m", "30m", "1h"})
CRYPTO_OHLCV_PERIODS = frozenset(
    {"day", "week", "month", "1m", "5m", "15m", "30m", "1h", "4h"}
)

OHLCV_PERIOD_MARKETS = {
    "day": ALL_OHLCV_MARKETS,
    "week": ALL_OHLCV_MARKETS,
    "month": ALL_OHLCV_MARKETS,
    "1m": frozenset({"equity_kr", "equity_us", "crypto"}),
    "5m": frozenset({"equity_kr", "equity_us", "crypto"}),
    "15m": frozenset({"equity_kr", "equity_us", "crypto"}),
    "30m": frozenset({"equity_kr", "equity_us", "crypto"}),
    "4h": frozenset({"crypto"}),
    "1h": ALL_OHLCV_MARKETS,
}

OHLCV_PERIOD_ERROR = (
    "period must be 'day', 'week', 'month', '1m', '5m', '15m', '30m', '4h', or '1h'"
)

_MARKET_LABELS = {
    "equity_kr": "korean equity",
    "equity_us": "us equity",
    "crypto": "crypto",
}


def validate_ohlcv_period(
    period: str,
    market: str,
    *,
    error_type: type[Exception] = ValueError,
) -> str:
    normalized = str(period or "day").strip().lower()
    if normalized not in OHLCV_ALLOWED_PERIODS:
        raise error_type(OHLCV_PERIOD_ERROR)

    allowed_markets = OHLCV_PERIOD_MARKETS[normalized]
    if market not in allowed_markets:
        if allowed_markets == frozenset({"crypto"}):
            raise error_type(f"period '{normalized}' is supported only for crypto")
        market_label = _MARKET_LABELS.get(market, market)
        raise error_type(f"period '{normalized}' is not supported for {market_label}")

    return normalized


__all__ = [
    "CRYPTO_MINUTE_OHLCV_PERIODS",
    "CRYPTO_MINUTE_PUBLIC_ROW_KEYS",
    "CRYPTO_MINUTE_REQUIRED_SOURCE_COLUMNS",
    "CRYPTO_OHLCV_PERIODS",
    "CRYPTO_ONLY_OHLCV_PERIODS",
    "KR_OHLCV_PERIODS",
    "KR_INTRADAY_OHLCV_PERIODS",
    "US_INTRADAY_OHLCV_PERIODS",
    "OHLCV_ALLOWED_PERIODS",
    "OHLCV_PERIOD_ERROR",
    "OHLCV_PERIOD_MARKETS",
    "US_OHLCV_PERIODS",
    "validate_ohlcv_period",
]

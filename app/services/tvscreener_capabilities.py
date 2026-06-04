"""Capability state and cache primitives for TvScreener service."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum


class TvScreenerCapabilityState(StrEnum):
    USABLE = "usable"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class TvScreenerCapabilitySnapshot:
    screener: str
    market: str
    statuses: dict[str, TvScreenerCapabilityState]
    fields: dict[str, object | None]

    def status(self, capability_name: str) -> TvScreenerCapabilityState:
        return self.statuses.get(capability_name, TvScreenerCapabilityState.UNKNOWN)

    def field(self, capability_name: str) -> object | None:
        return self.fields.get(capability_name)

    def is_usable(self, capability_name: str) -> bool:
        return (
            self.status(capability_name) is TvScreenerCapabilityState.USABLE
            and self.field(capability_name) is not None
        )


_STOCK_CAPABILITY_ALIASES: dict[str, tuple[str, ...]] = {
    "name": ("NAME",),
    "price": ("PRICE",),
    "rsi": ("RELATIVE_STRENGTH_INDEX_14",),
    "adx": ("AVERAGE_DIRECTIONAL_INDEX_14",),
    "volume": ("VOLUME",),
    "change_rate": ("CHANGE_PERCENT",),
    "market_cap": ("MARKET_CAPITALIZATION", "MARKET_CAP_BASIC"),
    "pe": ("PRICE_TO_EARNINGS_RATIO_TTM", "PRICE_TO_EARNINGS_TTM"),
    "pbr": ("PRICE_TO_BOOK_FQ", "PRICE_TO_BOOK_MRQ", "PRICE_BOOK_CURRENT"),
    "dividend_yield": (
        "DIVIDEND_YIELD_FORWARD",
        "DIVIDEND_YIELD_RECENT",
        "DIVIDEND_YIELD_CURRENT",
    ),
    "sector": ("SECTOR",),
    # ROB-428: KR fundamentals snapshot capabilities (tvscreener-backed).
    "roe_ttm": ("RETURN_ON_EQUITY_TTM", "RETURN_ON_EQUITY_FY"),
    "payout_ratio_ttm": (
        "DIVIDEND_PAYOUT_RATIO_TTM",
        "DIVIDEND_PAYOUT_RATIO_PERCENT_TTM",
        "DIVIDEND_PAYOUT_RATIO_FY",
    ),
    "gross_margin_ttm": ("GROSS_MARGIN_TTM", "GROSS_MARGIN_PERCENT_TTM"),
    "revenue_yoy": ("REVENUE_ANNUAL_YOY_GROWTH",),
    "eps_yoy": ("EPS_DILUTED_ANNUAL_YOY_GROWTH",),
    "eps_qoq": ("EPS_DILUTED_QUARTERLY_QOQ_GROWTH",),
    "net_income_yoy": ("NET_INCOME_ANNUAL_YOY_GROWTH",),
    "net_income_cagr_5y": ("NET_INCOME_CAGR_5Y",),
    "continuous_dividend_payout": ("CONTINUOUS_DIVIDEND_PAYOUT",),
    "continuous_dividend_growth": ("CONTINUOUS_DIVIDEND_GROWTH",),
    "week_high_52": ("WEEK_HIGH_52",),
    "industry": ("INDUSTRY",),
}

_CRYPTO_CAPABILITY_ALIASES: dict[str, tuple[str, ...]] = {
    "name": ("NAME",),
    "description": ("DESCRIPTION",),
    "price": ("PRICE",),
    "rsi": ("RELATIVE_STRENGTH_INDEX_14",),
    "adx": ("AVERAGE_DIRECTIONAL_INDEX_14",),
    "value_traded": ("VALUE_TRADED",),
    "market_cap": ("MARKET_CAP",),
}

_CAPABILITY_CACHE_MISS = object()


class _TvScreenerCapabilityRegistry:
    def __init__(self) -> None:
        self._field_cache: dict[tuple[str, str], object | None] = {}
        self._status_cache: dict[tuple[str, str, str], TvScreenerCapabilityState] = {}
        self._probe_locks: dict[tuple[str, str, str], asyncio.Lock] = {}

    def get_field(self, screener: str, capability_name: str) -> object:
        return self._field_cache.get(
            (screener, capability_name),
            _CAPABILITY_CACHE_MISS,
        )

    def set_field(
        self,
        screener: str,
        capability_name: str,
        field: object | None,
    ) -> None:
        self._field_cache[(screener, capability_name)] = field

    def get_status(
        self,
        screener: str,
        market: str,
        capability_name: str,
    ) -> TvScreenerCapabilityState | None:
        return self._status_cache.get((screener, market, capability_name))

    def set_status(
        self,
        screener: str,
        market: str,
        capability_name: str,
        status: TvScreenerCapabilityState,
    ) -> None:
        self._status_cache[(screener, market, capability_name)] = status

    def get_probe_lock(
        self,
        screener: str,
        market: str,
        capability_name: str,
    ) -> asyncio.Lock:
        cache_key = (screener, market, capability_name)
        lock = self._probe_locks.get(cache_key)
        if lock is None:
            lock = asyncio.Lock()
            self._probe_locks[cache_key] = lock
        return lock


_shared_capability_registry = _TvScreenerCapabilityRegistry()
_STOCK_CAPABILITY_PROBE_LIMIT = 3

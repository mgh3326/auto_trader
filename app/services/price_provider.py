from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol

import pandas as pd

from app.services import yahoo as yahoo_service


@dataclass(frozen=True)
class PriceFetchError:
    symbol: str
    source: str
    error: str


class UsEquityPriceProvider(Protocol):
    async def fetch_many(
        self, symbols: list[str]
    ) -> tuple[dict[str, float], list[PriceFetchError]]: ...


class YahooUsPriceProvider:
    def __init__(self, max_concurrency: int = 8):
        self._semaphore = asyncio.Semaphore(max(1, int(max_concurrency)))

    async def fetch_many(
        self, symbols: list[str]
    ) -> tuple[dict[str, float], list[PriceFetchError]]:
        normalized_symbols = self._normalize_symbols(symbols)
        if not normalized_symbols:
            return {}, []

        tasks = [self._fetch_one(symbol) for symbol in normalized_symbols]
        results = await asyncio.gather(*tasks)

        prices: dict[str, float] = {}
        errors: list[PriceFetchError] = []

        for symbol, price, error in results:
            if error is not None:
                errors.append(
                    PriceFetchError(symbol=symbol, source="yahoo", error=error)
                )
                continue
            if price is None:
                continue
            prices[symbol] = price

        return prices, errors

    async def _fetch_one(self, symbol: str) -> tuple[str, float | None, str | None]:
        async with self._semaphore:
            try:
                frame = await yahoo_service.fetch_price(symbol)
                close_price = self._extract_close(frame)
                return symbol, close_price, None
            except Exception as exc:
                return symbol, None, str(exc)

    @staticmethod
    def _normalize_symbols(symbols: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for symbol in symbols:
            candidate = str(symbol or "").strip().upper()
            if not candidate or candidate in seen:
                continue
            seen.add(candidate)
            normalized.append(candidate)
        return normalized

    @staticmethod
    def _extract_close(frame: pd.DataFrame) -> float:
        if frame.empty:
            raise ValueError("empty response")

        close_value = frame.iloc[-1].get("close")
        try:
            close_price = float(close_value)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid close price") from exc

        if close_price <= 0:
            raise ValueError("non-positive close price")
        return close_price

"""Canonical crypto identity helpers for the daily-candle store."""

from __future__ import annotations

import re

_SUPPORTED_UPBIT_QUOTE_CURRENCIES = frozenset({"KRW", "USDT"})
_UPBIT_BASE_ASSET = re.compile(r"[A-Z0-9]+")


def upbit_daily_candle_partition(symbol: str) -> str:
    """Return the canonical daily-candle partition for an Upbit market code.

    Generic crypto routing currently accepts only ``KRW-*`` and ``USDT-*``
    Upbit-style symbols.  Keep that boundary explicit: a plain symbol or an
    unknown quote prefix must not be guessed into the Upbit venue.
    """

    normalized = str(symbol).strip().upper()
    quote, separator, base = normalized.partition("-")
    if (
        separator != "-"
        or not base
        or "-" in base
        or _UPBIT_BASE_ASSET.fullmatch(base) is None
        or quote not in _SUPPORTED_UPBIT_QUOTE_CURRENCIES
    ):
        raise ValueError("Upbit daily-candle symbols must use a KRW-/USDT- market code")
    return f"upbit_{quote.lower()}"


__all__ = ["upbit_daily_candle_partition"]

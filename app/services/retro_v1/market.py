"""Market vocabulary for retro/v1: alias resolution and per-market constants."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from app.core.market import market_key
from app.core.symbol import to_db_symbol
from app.services.retro_v1.envelope import RetroReadError


@dataclass(frozen=True, slots=True)
class MarketInfo:
    key: str
    instrument_type: str
    currency: str
    quantity_unit: str


MARKETS: Final = {
    "kr": MarketInfo("kr", "equity_kr", "KRW", "shares"),
    "us": MarketInfo("us", "equity_us", "USD", "shares"),
    "crypto": MarketInfo("crypto", "crypto", "KRW", "base_asset"),
}

_CRYPTO_SYMBOL = re.compile(r"^[A-Z0-9]{2,10}-[A-Z0-9]{2,10}$")
_KR_SYMBOL = re.compile(r"^[0-9A-Z]{6}$")
_SYMBOL_PAIR = re.compile(r"^([A-Z0-9]{2,10})[-/.]([A-Z0-9]{1,10})$")
_CRYPTO_QUOTE_SYMBOLS: Final = frozenset({"KRW", "BTC", "USDT", "USD"})


def resolve_market(value: str) -> MarketInfo:
    key = market_key(value)
    if key is None:
        raise RetroReadError("validation_error")
    return MARKETS[key]


def normalize_symbol(symbol: str, market: MarketInfo) -> str:
    """DB symbol form (``app/core/symbol.py`` only; no string replacement here)."""
    text = symbol.strip().upper()
    return to_db_symbol(text) if market.key == "us" else text


def infer_market(symbol: str) -> MarketInfo:
    """Market of a bare symbol: ``KRW-BTC`` crypto, 6 chars with a digit KR, else US."""
    text = symbol.strip().upper()
    if _CRYPTO_SYMBOL.match(text):
        return MARKETS["crypto"]
    if _KR_SYMBOL.match(text) and any(ch.isdigit() for ch in text):
        return MARKETS["kr"]
    return MARKETS["us"]


def select_market(symbol: str, market: str | None) -> MarketInfo:
    """Select an explicit market, validating only shapes that cannot fit it."""
    if market is None:
        return infer_market(symbol)
    info = resolve_market(market)
    text = symbol.strip().upper()
    if info.key == "crypto" and not _CRYPTO_SYMBOL.fullmatch(text):
        raise RetroReadError("validation_error", message="Invalid market parameter.")
    if info.key == "kr" and not (
        _KR_SYMBOL.fullmatch(text) and any(c.isdigit() for c in text)
    ):
        raise RetroReadError("validation_error", message="Invalid market parameter.")
    pair = _SYMBOL_PAIR.fullmatch(text) if info.key == "us" else None
    is_crypto_pair = pair is not None and (
        pair.group(1) in _CRYPTO_QUOTE_SYMBOLS
        or pair.group(2) in _CRYPTO_QUOTE_SYMBOLS
        or len(pair.group(2)) >= 3
    )
    is_kr_symbol = _KR_SYMBOL.fullmatch(text) and any(c.isdigit() for c in text)
    if info.key == "us" and (is_crypto_pair or is_kr_symbol):
        raise RetroReadError("validation_error", message="Invalid market parameter.")
    return info

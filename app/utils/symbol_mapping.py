"""Symbol mapping utilities for converting between Upbit and TradingView formats.

Upbit Format:
    - Market code format: {QUOTE}-{BASE} (e.g., KRW-BTC)
    - Quote currency: Usually KRW (Korean Won), but can be BTC, USDT, etc.
    - Base currency: The cryptocurrency symbol (BTC, ETH, etc.)

TradingView Format:
    - Exchange:Pair format: UPBIT:{BASE}{QUOTE} (e.g., UPBIT:BTCKRW, UPBIT:BTCUSDT)
    - Exchange prefix: Always "UPBIT:"
    - Pair: Base currency followed by quote currency, no separator
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class SymbolMappingError(ValueError):
    """Base exception for symbol mapping errors."""

    pass


class InvalidUpbitSymbolError(SymbolMappingError):
    """Raised when an invalid Upbit symbol format is provided."""

    pass


class InvalidTradingViewSymbolError(SymbolMappingError):
    """Raised when an invalid TradingView symbol format is provided."""

    pass


class UnsupportedQuoteCurrencyError(SymbolMappingError):
    """Raised when an unsupported quote currency is encountered."""

    pass


def upbit_to_tradingview(upbit_symbol: str, *, target_quote: str | None = None) -> str:
    """Convert Upbit market symbol to TradingView format.

    Parameters
    ----------
    upbit_symbol : str
        Upbit market code in format "{QUOTE}-{BASE}" (e.g., "KRW-BTC")
    target_quote : str | None, optional
        Target quote currency for TradingView. If None, uses the same quote
        currency from Upbit symbol. Defaults to None.

    Returns
    -------
    str
        TradingView symbol in format "UPBIT:{BASE}{QUOTE}" (e.g., "UPBIT:BTCKRW")

    Raises
    ------
    InvalidUpbitSymbolError
        If the symbol format is invalid or missing required components
    UnsupportedQuoteCurrencyError
        If the target quote currency is not supported

    Examples
    --------
    >>> upbit_to_tradingview("KRW-BTC")
    'UPBIT:BTCKRW'
    >>> upbit_to_tradingview("KRW-ETH")
    'UPBIT:ETHKRW'
    >>> upbit_to_tradingview("BTC-ETH")
    'UPBIT:ETHBTC'
    >>> upbit_to_tradingview("KRW-BTC", target_quote="USDT")
    'UPBIT:BTCUSDT'
    """
    # Normalize input
    normalized_symbol = str(upbit_symbol or "").strip().upper()

    if not normalized_symbol:
        raise InvalidUpbitSymbolError("Upbit symbol cannot be empty")

    if "-" not in normalized_symbol:
        raise InvalidUpbitSymbolError(
            f"Invalid Upbit symbol format: '{upbit_symbol}'. "
            f"Expected format: {{QUOTE}}-{{BASE}} (e.g., 'KRW-BTC')"
        )

    parts = normalized_symbol.split("-", 1)
    if len(parts) != 2:
        raise InvalidUpbitSymbolError(
            f"Invalid Upbit symbol format: '{upbit_symbol}'. "
            f"Expected format: {{QUOTE}}-{{BASE}} (e.g., 'KRW-BTC')"
        )

    quote_currency, base_currency = parts

    if not quote_currency or not base_currency:
        raise InvalidUpbitSymbolError(
            f"Invalid Upbit symbol format: '{upbit_symbol}'. "
            f"Both quote and base currency must be non-empty"
        )

    # Use target quote if specified, otherwise use original quote
    effective_quote = (
        str(target_quote).strip().upper() if target_quote else quote_currency
    )

    if not effective_quote:
        raise UnsupportedQuoteCurrencyError(
            f"Quote currency cannot be empty for symbol '{upbit_symbol}'"
        )

    # Construct TradingView symbol
    tradingview_symbol = f"UPBIT:{base_currency}{effective_quote}"

    logger.debug(
        "Converted Upbit symbol '%s' to TradingView symbol '%s'",
        upbit_symbol,
        tradingview_symbol,
    )

    return tradingview_symbol


def tradingview_to_upbit(tradingview_symbol: str, *, default_quote: str = "KRW") -> str:
    """Convert TradingView symbol to Upbit market format.

    Parameters
    ----------
    tradingview_symbol : str
        TradingView symbol in format "UPBIT:{BASE}{QUOTE}" (e.g., "UPBIT:BTCKRW")
    default_quote : str, optional
        Default quote currency to use when detection fails. Defaults to "KRW".

    Returns
    -------
    str
        Upbit market code in format "{QUOTE}-{BASE}" (e.g., "KRW-BTC")

    Raises
    ------
    InvalidTradingViewSymbolError
        If the symbol format is invalid or not an Upbit symbol
    UnsupportedQuoteCurrencyError
        If the quote currency cannot be determined from the pair

    Examples
    --------
    >>> tradingview_to_upbit("UPBIT:BTCKRW")
    'KRW-BTC'
    >>> tradingview_to_upbit("UPBIT:ETHKRW")
    'KRW-ETH'
    >>> tradingview_to_upbit("UPBIT:BTCUSDT")
    'USDT-BTC'
    >>> tradingview_to_upbit("UPBIT:ETHBTC")
    'BTC-ETH'
    """
    # Normalize input
    normalized_symbol = str(tradingview_symbol or "").strip().upper()

    if not normalized_symbol:
        raise InvalidTradingViewSymbolError("TradingView symbol cannot be empty")

    if not normalized_symbol.startswith("UPBIT:"):
        raise InvalidTradingViewSymbolError(
            f"Invalid TradingView symbol: '{tradingview_symbol}'. "
            f"Expected format: UPBIT:{{BASE}}{{QUOTE}} (e.g., 'UPBIT:BTCKRW')"
        )

    # Extract the pair part after "UPBIT:"
    pair = normalized_symbol[6:]  # Remove "UPBIT:" prefix

    if not pair:
        raise InvalidTradingViewSymbolError(
            f"Invalid TradingView symbol: '{tradingview_symbol}'. "
            f"Pair component is empty after 'UPBIT:' prefix"
        )

    # Common quote currencies on TradingView (in order of precedence)
    # Longer strings first to avoid partial matches
    known_quotes = ["USDT", "USDC", "BUSD", "KRW", "USD", "BTC", "ETH"]

    # Try to match known quote currencies at the end of the pair
    quote_currency = None
    base_currency = None

    for quote in known_quotes:
        if pair.endswith(quote):
            quote_currency = quote
            base_currency = pair[: -len(quote)]
            break

    # If no known quote found, use default and treat the rest as base
    if quote_currency is None:
        logger.warning(
            "Could not determine quote currency from TradingView symbol '%s', "
            "using default quote '%s'",
            tradingview_symbol,
            default_quote,
        )
        quote_currency = default_quote.strip().upper()
        base_currency = pair

    if not base_currency:
        raise InvalidTradingViewSymbolError(
            f"Invalid TradingView symbol: '{tradingview_symbol}'. "
            f"Could not extract base currency from pair '{pair}'"
        )

    # Construct Upbit market code
    upbit_symbol = f"{quote_currency}-{base_currency}"

    logger.debug(
        "Converted TradingView symbol '%s' to Upbit symbol '%s'",
        tradingview_symbol,
        upbit_symbol,
    )

    return upbit_symbol


def is_valid_upbit_symbol(symbol: str) -> bool:
    """Check if a symbol is in valid Upbit format.

    Parameters
    ----------
    symbol : str
        Symbol to validate

    Returns
    -------
    bool
        True if the symbol is in valid Upbit format, False otherwise

    Examples
    --------
    >>> is_valid_upbit_symbol("KRW-BTC")
    True
    >>> is_valid_upbit_symbol("UPBIT:BTCKRW")
    False
    >>> is_valid_upbit_symbol("INVALID")
    False
    """
    try:
        upbit_to_tradingview(symbol)
        return True
    except SymbolMappingError:
        return False


def is_valid_tradingview_symbol(symbol: str) -> bool:
    """Check if a symbol is in valid TradingView format for Upbit.

    Parameters
    ----------
    symbol : str
        Symbol to validate

    Returns
    -------
    bool
        True if the symbol is in valid TradingView format for Upbit, False otherwise

    Examples
    --------
    >>> is_valid_tradingview_symbol("UPBIT:BTCKRW")
    True
    >>> is_valid_tradingview_symbol("KRW-BTC")
    False
    >>> is_valid_tradingview_symbol("BINANCE:BTCUSDT")
    False
    """
    try:
        tradingview_to_upbit(symbol)
        return True
    except SymbolMappingError:
        return False


__all__ = [
    "SymbolMappingError",
    "InvalidUpbitSymbolError",
    "InvalidTradingViewSymbolError",
    "UnsupportedQuoteCurrencyError",
    "upbit_to_tradingview",
    "tradingview_to_upbit",
    "is_valid_upbit_symbol",
    "is_valid_tradingview_symbol",
]

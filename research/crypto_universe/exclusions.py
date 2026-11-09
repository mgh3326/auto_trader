"""Shared crypto universe exclusions for research and paper input selection."""

from __future__ import annotations

import math
from collections.abc import Iterable
from datetime import date, timedelta

# This is a floor for known stable/fiat bases, not the complete peg detector.
# Binance's current stablecoin catalog also lists PYUSD, USDD, USDG and U.
# BFUSD is Binance's USD stablecoin; XUSD is StraitsX USD listed by Binance.
STABLE_BASES = frozenset(
    {
        "BUSD",
        "USDC",
        "TUSD",
        "USDP",
        "PAX",
        "FDUSD",
        "DAI",
        "USDS",
        "USDSB",
        "SUSD",
        "UST",
        "USTC",
        "USD1",
        "EUR",
        "EURI",
        "AEUR",
        "GBP",
        "AUD",
        "TRY",
        "BRL",
        "RUB",
        "UAH",
        "NGN",
        "ZAR",
        "IDRT",
        "BIDR",
        "BKRW",
        "XUSD",
        "USDE",
        "RLUSD",
        "BFUSD",
        "PAXG",
        "PYUSD",
        "USDD",
        "USDG",
        "U",
        "EURC",
        "GUSD",
        "FRAX",
        "LUSD",
        "USDJ",
        "VAI",
    }
)

# Wrapped/tokenized forms duplicate exposure to an included underlying asset.
# Binance identifies WBTC as Wrapped Bitcoin, BETH as a wrapped ETH token, and
# WBETH as a wrapped, reward-bearing representation of BETH/staked ETH.
WRAPPED_DUPLICATE_BASES = frozenset({"WBTC", "BETH", "WBETH"})

PEG_SESSION_COUNT = 30
PEG_MIN_CLOSE = 0.98
PEG_MAX_CLOSE = 1.02
_LEVERAGED_UNDERLYINGS = frozenset(
    {
        "1INCH",
        "AAVE",
        "ADA",
        "BCH",
        "BNB",
        "BTC",
        "DOT",
        "EOS",
        "ETH",
        "FIL",
        "LINK",
        "LTC",
        "SUSHI",
        "SXP",
        "TRX",
        "UNI",
        "XLM",
        "XRP",
        "XTZ",
        "YFI",
    }
)
_LEVERAGED_SUFFIXES = ("UP", "DOWN", "BULL", "BEAR")


def is_leveraged_token(base_asset: str) -> bool:
    if base_asset in {"BULL", "BEAR"}:
        return True
    return any(
        base_asset.endswith(suffix)
        and base_asset[: -len(suffix)] in _LEVERAGED_UNDERLYINGS
        for suffix in _LEVERAGED_SUFFIXES
    )


def crypto_exclusion_reason(
    base_asset: str,
    closes: Iterable[tuple[date, float]] = (),
    *,
    cutoff: date,
) -> str | None:
    """Return a stable reason when a base should be omitted at ``cutoff``.

    A peg is proven only by exactly one valid close for every calendar session
    in the final 30 completed days. Missing/duplicate/invalid observations do
    not prove a peg and leave the static-list decision in force.
    """
    # Keep the historical PIT flag for schema compatibility, but this
    # strategy explicitly retains gold-backed real-asset exposure.
    if base_asset == "PAXG":
        return None
    if is_leveraged_token(base_asset):
        return "leveraged_token"
    if base_asset in STABLE_BASES:
        return "stable_list"
    if base_asset in WRAPPED_DUPLICATE_BASES:
        return "wrapped_duplicate"

    first_day = cutoff - timedelta(days=PEG_SESSION_COUNT - 1)
    by_day: dict[date, float] = {}
    for day, close in closes:
        if first_day <= day <= cutoff:
            if day in by_day:
                return None
            try:
                value = float(close)
            except (TypeError, ValueError):
                return None
            if not math.isfinite(value):
                return None
            by_day[day] = value
    expected = {first_day + timedelta(days=i) for i in range(PEG_SESSION_COUNT)}
    if by_day.keys() != expected:
        return None
    if all(PEG_MIN_CLOSE <= by_day[day] <= PEG_MAX_CLOSE for day in expected):
        return "stable_peg_rule"
    return None

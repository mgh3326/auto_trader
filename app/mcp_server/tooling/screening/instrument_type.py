"""Instrument-type classification helpers for stock screening."""

from __future__ import annotations

import re
from typing import Literal

InstrumentType = Literal["common", "preferred", "etf", "reit", "spac", "unknown"]

_PREFERRED_KR_NAME_RE = re.compile(r"(?:\d우B?|\d우|우B?|우선주)$")
_PREFERRED_KR_CODE_SUFFIXES = ("5", "7", "9")
_PREFERRED_US_SYMBOL_RE = re.compile(r"(?:[.-]P[A-Z]?|[.-]PR[.-]?[A-Z]?)$")


def _normalize_compare_key(value: object) -> str:
    return re.sub(r"\s+", "", str(value or "").strip()).casefold()


def _has_any_token(value: object, tokens: tuple[str, ...]) -> bool:
    key = _normalize_compare_key(value)
    return any(token in key for token in tokens)


def classify_kr_instrument(
    symbol: object,
    name: object,
    tvscreener_subtype: object,
) -> InstrumentType:
    """Classify Korean instruments into the public screen_stocks taxonomy."""
    symbol_text = str(symbol or "").strip().upper()
    name_text = str(name or "").strip()
    subtype_text = str(tvscreener_subtype or "").strip()

    if _has_any_token(subtype_text, ("etf", "exchangetradedfund")) or _has_any_token(
        name_text, ("etf", "상장지수", "kodex", "tiger", "ace", "kbstar", "hanaro")
    ):
        return "etf"
    if _has_any_token(name_text, ("reit", "리츠")) or _has_any_token(
        subtype_text, ("reit",)
    ):
        return "reit"
    if _has_any_token(name_text, ("spac", "스팩")) or _has_any_token(
        subtype_text, ("spac",)
    ):
        return "spac"
    if name_text and _PREFERRED_KR_NAME_RE.search(name_text):
        return "preferred"
    if name_text and symbol_text and symbol_text[-1:] in _PREFERRED_KR_CODE_SUFFIXES:
        return "preferred"
    if name_text or subtype_text:
        return "common"
    return "unknown"


def classify_us_instrument(
    symbol: object,
    name: object,
    tvscreener_type: object,
    tvscreener_subtype: object,
) -> InstrumentType:
    """Classify US instruments into the public screen_stocks taxonomy."""
    symbol_text = str(symbol or "").strip().upper()
    name_text = str(name or "").strip()
    type_text = str(tvscreener_type or "").strip()
    subtype_text = str(tvscreener_subtype or "").strip()
    combined = " ".join((name_text, type_text, subtype_text))

    if _has_any_token(combined, ("etf", "exchangetradedfund")):
        return "etf"
    if _has_any_token(combined, ("reit", "realestateinvestmenttrust")):
        return "reit"
    if _has_any_token(combined, ("spac", "specialpurposeacquisition")) or (
        "acquisitioncorp" in _normalize_compare_key(name_text)
        and _has_any_token(name_text, ("unit", "warrant"))
    ):
        return "spac"
    if _has_any_token(combined, ("preferred", "preference")) or (
        symbol_text and _PREFERRED_US_SYMBOL_RE.search(symbol_text)
    ):
        return "preferred"
    if _has_any_token(combined, ("commonstock", "stock", "equity")) or (
        symbol_text and name_text
    ):
        return "common"
    return "unknown"


__all__ = [
    "InstrumentType",
    "_normalize_compare_key",
    "classify_kr_instrument",
    "classify_us_instrument",
]

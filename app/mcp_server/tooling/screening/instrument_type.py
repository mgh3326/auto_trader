"""Instrument-type classification helpers for stock screening."""

from __future__ import annotations

import re
from typing import Literal

InstrumentType = Literal["common", "preferred", "etf", "reit", "spac", "unknown"]

_PREFERRED_KR_NAME_RE = re.compile(r"(?:\d우B?|\d우|우B?|우선주)$")
_PREFERRED_KR_CODE_SUFFIXES = ("5", "7", "9")
_PREFERRED_US_SYMBOL_RE = re.compile(r"(?:[.-]P[A-Z]?|[.-]PR[.-]?[A-Z]?)$")

# US leveraged/inverse ETF exclusion (ROB task #922, retro U-3).  This is the
# US mirror of the KR name-token quality rule ``_KR_TOSS_EXCLUDED_NAME_TOKENS``
# = ("레버리지", "인버스") in
# app/services/invest_view_model/screener_service.py, and of the
# buy.index_etf_candidate policy tier's required ``leveraged_etf`` /
# ``inverse_etf`` exclusions in config/trading_policy.yaml.  US ranking rows
# carry only a display name, so the check is name-token based like KR.  The
# word-boundary patterns deliberately exclude only leveraged/inverse products:
# broad index ETFs and ordinary common stocks pass through, and duration
# names such as "Short-Term Treasury" are not leveraged/inverse products.
_US_LEVERAGED_INVERSE_NAME_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\b-?\d+(?:\.\d+)?\s*x\b",  # "2x", "3X", "-1x", "1.5 X"
        r"\bleveraged\b",
        r"\binverse\b",
        # ProShares leveraged brands: UltraPro (3x), UltraShort (-2x), and
        # the "ProShares Ultra <index>" family (2x).  A bare "Ultra" is NOT
        # excluded — ordinary issuers such as Ultra Clean Holdings are
        # common stocks, not leveraged products.
        r"\bultra\s*pro\b",
        r"\bultrashort\b",
        r"\bproshares\s+ultra\b",
        # "Short <index>" products are inverse; duration/bond names are not.
        r"\bshort\b(?![\s-]*(?:term|duration|maturity|municipal|bond|treasury|government|board|date|month))",
    )
)


def is_us_leveraged_inverse_name(name: object) -> bool:
    """True when a US display name marks a leveraged or inverse product."""

    text = str(name or "").strip()
    if not text:
        return False
    return any(pattern.search(text) for pattern in _US_LEVERAGED_INVERSE_NAME_PATTERNS)


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
    is_common_stock: bool | None = None,
) -> InstrumentType:
    """Classify US instruments into the public screen_stocks taxonomy.

    ``is_common_stock`` is the authoritative NASDAQ-Trader flag from
    ``us_symbol_universe`` (ROB-204). When it is ``True`` it overrides the
    yfinance-derived tokens, which intermittently mislabel a common stock as an
    ETF/fund (ROB-365 bug 5, e.g. NFLX). ``False``/``None`` fall through to the
    token-based classification (so genuine ETFs/preferred/REITs stay correct,
    and unknown symbols are still best-effort classified).
    """
    if is_common_stock is True:
        return "common"

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
    "is_us_leveraged_inverse_name",
]

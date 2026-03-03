"""Korean equity symbol mapping for trade profiles."""

from __future__ import annotations

KR_SYMBOLS: dict[str, str] = {
    "한화에어로": "012450",
    "삼양식품": "003230",
    "HD한국조선": "329180",
    "크래프톤": "259960",
    "NAVER": "035420",
    "파마리서치": "214450",
    "펩트론": "087010",
    "알테오젠": "196170",
}


def normalize_kr_symbol(symbol_input: str) -> str:
    """Resolve Korean company name or numeric code to 6-digit symbol."""
    candidate = symbol_input.strip()
    if candidate.isdigit() and len(candidate) <= 6:
        return candidate.zfill(6)
    mapped = KR_SYMBOLS.get(candidate)
    if mapped is None:
        raise ValueError(f"KR symbol mapping missing for input: {symbol_input}")
    return mapped


__all__ = ["KR_SYMBOLS", "normalize_kr_symbol"]

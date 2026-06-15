"""Handler for get_fx_rate tool."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services.exchange_rate_service import get_usd_krw_rate_details


def _isoformat_or_none(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def _normalize_fx_pair(pair: str | None) -> str:
    raw = (pair or "USDKRW").strip().upper()
    compact = raw.replace("/", "").replace("_", "").replace("-", "").replace(" ", "")
    if compact != "USDKRW":
        raise ValueError(f"Unsupported FX pair '{raw}'. Supported: USDKRW")
    return "USDKRW"


async def handle_get_fx_rate(pair: str | None = "USDKRW") -> dict[str, Any]:
    normalized_pair = _normalize_fx_pair(pair)
    quote = await get_usd_krw_rate_details()

    return {
        "pair": normalized_pair,
        "base_currency": "USD",
        "quote_currency": "KRW",
        "rate": quote.rate,
        "mid_rate": quote.mid_rate,
        "default_rate": quote.default_rate,
        "source": quote.source,
        "valid_from": _isoformat_or_none(quote.valid_from),
        "valid_until": _isoformat_or_none(quote.valid_until),
        "basis_point": quote.basis_point,
        "rate_change_type": quote.rate_change_type,
    }


__all__ = ["handle_get_fx_rate"]

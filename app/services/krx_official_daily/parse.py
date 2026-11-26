"""Pure parsing for KRX Open API daily rows (hk 6586).

Numbers are exact ``Decimal`` values parsed from strings with commas stripped.
A missing, blank, dash, or unparsable field becomes NULL with a closed reason;
it is never coerced to zero.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from app.services.krx_official_daily.errors import KrxMalformedError

SOURCE = "krx_openapi"
MARKET_KEYS = {"kospi": "KOSPI", "kosdaq": "KOSDAQ"}
NUMERIC_FIELDS: dict[str, str] = {
    "TDD_OPNPRC": "open_price",
    "TDD_HGPRC": "high_price",
    "TDD_LWPRC": "low_price",
    "TDD_CLSPRC": "close_price",
    "ACC_TRDVOL": "volume",
    "ACC_TRDVAL": "trade_value",
    "MKTCAP": "market_cap",
    "LIST_SHRS": "listed_shares",
}
# KRX 6-character short code: digits and upper-case ASCII letters (e.g. 005930, 0256C0).
# Lower-case and other lengths are refused; this is the kr_symbol_universe rule for ASCII input.
SHORT_CODE = re.compile(r"[0-9A-Z]{6}")
FIELD_REASONS = frozenset({"missing", "empty", "unparsable", "negative"})
REJECT_REASONS = frozenset(
    {"bas_dd_missing", "bas_dd_mismatch", "symbol_not_short_code", "duplicate_symbol"}
)


@dataclass
class ParsedDaily:
    rows: list[dict[str, Any]] = field(default_factory=list)
    rejected: Counter[str] = field(default_factory=Counter)


def parse_decimal(raw: object) -> tuple[Decimal | None, str | None]:
    """Return ``(value, None)`` or ``(None, closed_reason)``; never zero by default."""
    if raw is None:
        return None, "missing"
    if isinstance(raw, bool) or not isinstance(raw, (str, int)):
        return None, "unparsable"
    text = str(raw).strip().replace(",", "")
    if text in ("", "-"):
        return None, "empty"
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None, "unparsable"
    if not value.is_finite():
        return None, "unparsable"
    if value < 0:
        return None, "negative"
    return value, None


def _string(raw: object) -> str | None:
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    return text or None


def parse_daily_payload(
    payload: object, *, market: str, session_date: date
) -> ParsedDaily:
    """Parse one market's OutBlock_1 rows for the requested business date."""
    if market not in MARKET_KEYS:
        raise ValueError("market must be kospi or kosdaq")
    if not isinstance(payload, Mapping):
        raise KrxMalformedError("payload_not_object")
    block = payload.get("OutBlock_1")
    if not isinstance(block, list) or any(not isinstance(r, Mapping) for r in block):
        raise KrxMalformedError("outblock_missing")

    expected_date = session_date.strftime("%Y%m%d")
    result = ParsedDaily()
    seen: set[str] = set()
    for raw_row in block:
        bas_dd = _string(raw_row.get("BAS_DD"))
        if bas_dd is None:
            result.rejected["bas_dd_missing"] += 1
            continue
        if bas_dd != expected_date:
            result.rejected["bas_dd_mismatch"] += 1
            continue
        symbol = _string(raw_row.get("ISU_CD"))
        if symbol is None or not SHORT_CODE.fullmatch(symbol):
            result.rejected["symbol_not_short_code"] += 1
            continue
        if symbol in seen:
            result.rejected["duplicate_symbol"] += 1
            continue
        seen.add(symbol)

        values: dict[str, Decimal | None] = {}
        reasons: dict[str, str] = {}
        for source_key, column in NUMERIC_FIELDS.items():
            value, reason = parse_decimal(raw_row.get(source_key))
            values[column] = value
            if reason is not None:
                reasons[column] = reason
        volume = values["volume"]
        result.rows.append(
            {
                "symbol": symbol,
                "session_date": session_date,
                "market": market,
                "name": _string(raw_row.get("ISU_NM")),
                **values,
                "full_day_zero_volume": None if volume is None else volume == 0,
                "field_reasons": reasons,
                "source": SOURCE,
            }
        )
    return result

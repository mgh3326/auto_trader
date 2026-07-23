"""Typed provenance contracts for KR/US daily candles."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol


@dataclass(frozen=True, slots=True)
class DailySourceContract:
    source_row_version: str
    price_basis: str


DAILY_SOURCE_CONTRACTS: dict[str, DailySourceContract] = {
    # KIS domestic requests FID_ORG_ADJ_PRC=0 and overseas requests MODP=1.
    "kis": DailySourceContract("kis-adjusted-daily-v1", "provider_adjusted"),
    # Toss daily requests adjusted=true.
    "toss": DailySourceContract("toss-adjusted-daily-v1", "provider_adjusted"),
    "toss_fallback": DailySourceContract("toss-adjusted-daily-v1", "provider_adjusted"),
    # yfinance daily requests auto_adjust=false.
    "yahoo": DailySourceContract("yahoo-raw-daily-v1", "raw"),
    "yahoo_fallback": DailySourceContract("yahoo-raw-daily-v1", "raw"),
}


class _CandleLike(Protocol):
    time_utc: datetime
    symbol: str
    partition: str
    open: float
    high: float
    low: float
    close: float
    adj_close: float | None
    volume: float
    value: float
    source: str


def _canonical_number(value: float | None) -> str | None:
    if value is None:
        return None
    decimal = Decimal(str(value))
    normalized = decimal.normalize()
    return format(normalized, "f")


def daily_source_row_id(row: _CandleLike) -> str:
    """Content-address one normalized provider row for correction detection."""
    raw_date = row.time_utc.date()
    payload = {
        "source": row.source,
        "symbol": row.symbol,
        "partition": row.partition,
        "session_date": raw_date.isoformat(),
        "open": _canonical_number(row.open),
        "high": _canonical_number(row.high),
        "low": _canonical_number(row.low),
        "close": _canonical_number(row.close),
        "adj_close": _canonical_number(row.adj_close),
        "volume": _canonical_number(row.volume),
        "value": _canonical_number(row.value),
    }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def with_equity_provenance[CandleT: _CandleLike](
    row: CandleT,
    *,
    final_through_date: date | None,
) -> CandleT:
    """Attach exact source/basis/finality provenance to an equity row.

    Calendar failure intentionally yields ``is_final=False``. The row may still
    serve ordinary cache consumers, but a terminal-close resolver cannot trust it.
    """
    contract = DAILY_SOURCE_CONTRACTS.get(row.source)
    if contract is None:
        return row
    row_date = row.time_utc.date()
    return replace(
        row,
        is_final=bool(
            final_through_date is not None and row_date <= final_through_date
        ),
        session_scope="regular",
        source_row_id=daily_source_row_id(row),
        source_row_version=contract.source_row_version,
        price_basis=contract.price_basis,
    )


__all__ = [
    "DAILY_SOURCE_CONTRACTS",
    "DailySourceContract",
    "daily_source_row_id",
    "with_equity_provenance",
]

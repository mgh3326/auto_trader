"""KSD dividend calendar fetch and normalization for Korean market events."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from app.services.market_events.taxonomy import validate_category, validate_market

KSD_RESPONSE_LIMIT = 100
KSD_SOURCE = "kis_ksd"


@dataclass(frozen=True)
class KSDDividendFetch:
    rows: list[dict[str, Any]]
    truncated_windows: tuple[tuple[date, date], ...]


def _parse_date(value: Any, field: str, *, required: bool = True) -> date | None:
    if value is None or value == "":
        if required:
            raise ValueError(f"KSD dividend row missing {field}")
        return None
    if not isinstance(value, str) or len(value) != 8:
        raise ValueError(f"KSD dividend row has invalid {field}")
    try:
        return datetime.strptime(value, "%Y%m%d").date()
    except ValueError as exc:
        raise ValueError(f"KSD dividend row has invalid {field}") from exc


def _parse_amount(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("KSD dividend row has invalid per_sto_divi_amt") from exc
    if not amount.is_finite():
        raise ValueError("KSD dividend row has non-finite per_sto_divi_amt")
    return amount


def normalize_ksd_dividend_row(
    row: Mapping[str, Any], *, received_at: datetime | None = None
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Normalize one official KSD ``output1`` record without float conversion."""
    record_date = _parse_date(row.get("record_date"), "record_date")
    assert record_date is not None
    payment_date = _parse_date(row.get("divi_pay_dt"), "divi_pay_dt", required=False)
    symbol = str(row.get("sht_cd") or "").strip()
    if not (len(symbol) == 6 and symbol.isdigit()):
        raise ValueError("KSD dividend row has invalid sht_cd")
    dps = _parse_amount(row.get("per_sto_divi_amt"))
    kind = str(row.get("divi_kind") or "").strip()
    payment_key = payment_date.strftime("%Y%m%d") if payment_date else "unknown"
    source_event_id = f"{symbol}:{record_date:%Y%m%d}:{payment_key}:{kind}"
    first_received_at = received_at or datetime.now(UTC)
    validate_category("dividend")
    validate_market("kr")
    event = {
        "category": "dividend",
        "market": "kr",
        "country": "KR",
        "currency": "KRW",
        "symbol": symbol,
        "company_name": str(row.get("isin_name") or "").strip() or None,
        "title": f"{symbol} 배당 기준일",
        "event_date": record_date,
        "source_timezone": "Asia/Seoul",
        "time_hint": "unknown",
        "status": "scheduled",
        "source": KSD_SOURCE,
        "source_event_id": source_event_id,
        "first_received_at": first_received_at,
        "fetched_at": first_received_at,
        "raw_payload_json": {
            **dict(row),
            "record_date": record_date.isoformat(),
            "payment_date": payment_date.isoformat() if payment_date else None,
            "dps": str(dps) if dps is not None else None,
        },
    }
    values = (
        [{"metric_name": "dps", "actual": dps, "unit": "KRW"}]
        if dps is not None
        else []
    )
    return event, values


async def fetch_ksd_dividend_window(
    fetch: Callable[[str, str], Awaitable[list[dict[str, Any]]]],
    from_date: date,
    to_date: date,
    *,
    response_limit: int = KSD_RESPONSE_LIMIT,
) -> KSDDividendFetch:
    """Split capped responses by date; report any unsplittable capped day."""
    if from_date > to_date:
        raise ValueError("from_date must be <= to_date")
    rows: list[dict[str, Any]] = []
    truncated: list[tuple[date, date]] = []

    async def visit(start: date, end: date) -> None:
        result = await fetch(start.strftime("%Y%m%d"), end.strftime("%Y%m%d"))
        if len(result) < response_limit:
            rows.extend(result)
            return
        if start == end:
            rows.extend(result)
            truncated.append((start, end))
            return
        midpoint = start + (end - start) // 2
        await visit(start, midpoint)
        await visit(midpoint + timedelta(days=1), end)

    await visit(from_date, to_date)
    return KSDDividendFetch(rows=rows, truncated_windows=tuple(truncated))

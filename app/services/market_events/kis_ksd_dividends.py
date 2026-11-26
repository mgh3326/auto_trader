"""KSD dividend calendar fetch and normalization for Korean market events."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Any

from app.core.symbol import to_db_symbol
from app.services.market_events.taxonomy import validate_category, validate_market

KSD_RESPONSE_LIMIT = 100
KSD_SOURCE = "kis_ksd"


@dataclass(frozen=True)
class KSDEventFetch:
    rows: list[dict[str, Any]]
    truncated_windows: tuple[tuple[date, date], ...]


# Retained for callers of the pre-hk 6662 dividend-only helper.
KSDDividendFetch = KSDEventFetch


@dataclass(frozen=True)
class KSDEventSpec:
    label: str
    date_field: str
    date_fields: tuple[str, ...] = ()
    value_fields: tuple[tuple[str, str], ...] = ()
    identity_field: str | None = None


KSD_EVENT_SPECS: dict[str, KSDEventSpec] = {
    "paidin_capin": KSDEventSpec(
        "유상증자",
        "record_date",
        ("right_dt", "sub_term_ft", "sub_term", "list_date"),
        (
            ("tot_issue_stk_qty", "shares"),
            ("issue_stk_qty", "shares"),
            ("fix_rate", "%"),
            ("disc_rate", "%"),
            ("fix_price", "KRW"),
        ),
        "list_date",
    ),
    "bonus_issue": KSDEventSpec(
        "무상증자",
        "record_date",
        ("right_dt", "odd_pay_dt", "list_date"),
        (
            ("fix_rate", "%"),
            ("odd_rec_price", "KRW"),
            ("tot_issue_stk_qty", "shares"),
            ("issue_stk_qty", "shares"),
        ),
        "stk_kind",
    ),
    "purreq": KSDEventSpec(
        "주식매수청구",
        "record_date",
        ("opp_opi_rcpt_term", "buy_req_rcpt_term"),
        (("buy_req_price", "KRW"),),
        "stk_kind",
    ),
    "merger_split": KSDEventSpec(
        "합병·분할",
        "record_date",
        ("td_stop_dt", "list_dt", "odd_amt_pay_dt"),
        (
            ("merge_rate", "ratio"),
            ("tot_issue_stk_qty", "shares"),
            ("issue_stk_qty", "shares"),
        ),
        "seq",
    ),
    "rev_split": KSDEventSpec(
        "액면교체·병합",
        "record_date",
        ("td_stop_dt", "list_dt"),
        (("inter_bf_face_amt", "KRW"), ("inter_af_face_amt", "KRW")),
        "stk_kind",
    ),
    "cap_dcrs": KSDEventSpec(
        "자본감소",
        "record_date",
        ("td_stop_dt", "list_dt"),
        (("reduce_cap_rate", "%"),),
        "reduce_cap_type",
    ),
    "list_info": KSDEventSpec(
        "상장정보",
        "list_dt",
        (),
        (
            ("issue_stk_qty", "shares"),
            ("tot_issue_stk_qty", "shares"),
            ("issue_price", "KRW"),
        ),
        "issue_type",
    ),
    "pub_offer": KSDEventSpec(
        "공모주 청약",
        "record_date",
        ("subscr_dt", "pay_dt", "refund_dt", "list_dt"),
        (
            ("fix_subscr_pri", "KRW"),
            ("face_value", "KRW"),
            ("pub_bf_cap", "KRW"),
            ("pub_af_cap", "KRW"),
            ("assign_stk_qty", "shares"),
        ),
        "subscr_dt",
    ),
    "forfeit": KSDEventSpec(
        "실권주",
        "record_date",
        ("subscr_dt", "refund_dt", "list_dt"),
        (("subscr_price", "KRW"), ("subscr_stk_qty", "shares")),
        "subscr_dt",
    ),
    "mand_deposit": KSDEventSpec(
        "의무예치",
        "depo_date",
        (),
        (("stk_qty", "shares"), ("tot_issue_qty_per_rate", "%")),
        "depo_reason",
    ),
    "sharehld_meet": KSDEventSpec(
        "주주총회",
        "gen_meet_dt",
        ("record_date",),
        (("vote_tot_qty", "shares"),),
        "gen_meet_type",
    ),
}

KSD_EVENT_KINDS = tuple(KSD_EVENT_SPECS)


def _parse_date(value: Any, field: str, *, required: bool = True) -> date | None:
    if value is None or value == "":
        if required:
            raise ValueError(f"KSD row missing {field}")
        return None
    if not isinstance(value, str):
        raise ValueError(f"KSD row has invalid {field}")
    for date_format in ("%Y%m%d", "%Y/%m/%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, date_format).date()
        except ValueError:
            continue
    raise ValueError(f"KSD row has invalid {field}")


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
    """Compatibility wrapper for the shared KSD date-window fetcher."""
    return await fetch_ksd_event_window(
        fetch, from_date, to_date, response_limit=response_limit
    )


async def fetch_ksd_event_window(
    fetch: Callable[[str, str], Awaitable[list[dict[str, Any]]]],
    from_date: date,
    to_date: date,
    *,
    response_limit: int = KSD_RESPONSE_LIMIT,
) -> KSDEventFetch:
    """Split capped KSD responses by date; report unsplittable capped days."""
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
    return KSDEventFetch(rows=rows, truncated_windows=tuple(truncated))


def _event_decimal(value: Any, field: str) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, (bool, float)):
        raise ValueError(f"KSD event row has non-exact {field}")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"KSD event row has invalid {field}") from exc
    if not amount.is_finite():
        raise ValueError(f"KSD event row has non-finite {field}")
    return amount


def normalize_ksd_event_row(
    kind: str,
    row: Mapping[str, Any],
    *,
    received_at: datetime | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Normalize one official KSD event record without float conversion."""
    try:
        spec = KSD_EVENT_SPECS[kind]
    except KeyError as exc:
        raise ValueError(f"Unsupported KSD event kind: {kind}") from exc

    event_date = _parse_date(row.get(spec.date_field), spec.date_field)
    assert event_date is not None
    raw_symbol = str(row.get("sht_cd") or "").strip()
    if len(raw_symbol) != 6 or not raw_symbol.isascii() or not raw_symbol.isdigit():
        raise ValueError("KSD event row has invalid sht_cd")
    symbol = to_db_symbol(raw_symbol)
    company_name = (
        str(
            row.get("isin_name") or row.get("cust_nm") or row.get("opp_cust_nm") or ""
        ).strip()
        or None
    )

    values: list[dict[str, Any]] = []
    raw_payload = dict(row)
    for field in spec.date_fields:
        raw_value = row.get(field)
        if raw_value is None or raw_value == "":
            continue
        try:
            parsed = _parse_date(raw_value, field, required=False)
        except ValueError:
            parsed = None
        date_value = parsed.isoformat() if parsed else str(raw_value).strip()
        if parsed:
            raw_payload[field] = date_value
        values.append(
            {
                "metric_name": field,
                "value_details": {"source_field": field, "date_or_range": date_value},
            }
        )
    for field, unit in spec.value_fields:
        amount = _event_decimal(row.get(field), field)
        if amount is None:
            continue
        values.append({"metric_name": field, "actual": amount, "unit": unit})

    identity_value = (
        str(row.get(spec.identity_field) or "").strip() if spec.identity_field else ""
    )
    if not identity_value:
        identity_value = sha256(
            json.dumps(
                {"kind": kind, "symbol": symbol, "date": event_date.isoformat()},
                sort_keys=True,
            ).encode()
        ).hexdigest()[:16]
    source_event_id = f"{kind}:{symbol}:{event_date:%Y%m%d}:{identity_value}"
    raw_payload[spec.date_field] = event_date.isoformat()
    raw_payload["kind"] = kind
    received = received_at or datetime.now(UTC)
    validate_category("corporate_event")
    validate_market("kr")
    event = {
        "category": "corporate_event",
        "market": "kr",
        "country": "KR",
        "currency": "KRW",
        "symbol": symbol,
        "company_name": company_name,
        "title": f"{company_name or symbol} {spec.label}",
        "event_date": event_date,
        "source_timezone": "Asia/Seoul",
        "time_hint": "unknown",
        "status": "scheduled",
        "source": KSD_SOURCE,
        "source_event_id": source_event_id,
        "first_received_at": received,
        "fetched_at": received,
        "raw_payload_json": raw_payload,
    }
    return event, values

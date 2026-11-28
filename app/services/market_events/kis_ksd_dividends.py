"""KSD dividend calendar fetch and normalization for Korean market events."""

from __future__ import annotations

import json
import re
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

# Kinds whose primary date may be blank without making the row malformed.
# A blank here means the schedule row has no date assigned yet, so there is
# no calendar day to place it on. The row is skipped, counted separately, and
# never written. Other blank dates stay malformed. See the contract in
# docs/agent-contracts/market-data-and-screener-contracts.md.
UNSCHEDULED_BLANK_DATE_REASON: dict[str, str] = {"list_info": "no_listing_date"}
_SHAPE_KEEP = frozenset("/-~ .")
_DIGITS = frozenset("0123456789")


class KSDUnscheduledRowError(ValueError):
    """A valid KSD schedule row whose primary date is documented as blank."""

    def __init__(self, kind: str, field: str, reason: str) -> None:
        super().__init__(f"KSD row has no {field} ({reason})")
        self.kind = kind
        self.field = field
        self.reason = reason


def _shape_mask(value: str) -> str:
    """Show the layout of an unparsed value without logging its content.

    Digits become ``9``, separators are kept, and anything else becomes ``?``.
    """
    head = value[:24]
    masked = "".join(
        "9" if ch in _DIGITS else ch if ch in _SHAPE_KEEP else "?" for ch in head
    )
    return masked + ("+" if len(value) > 24 else "")


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
    identity_fields: tuple[str, ...] = ()
    # The primary date is an observed "YYYY/MM/DD ~ YYYY/MM/DD" deposit range.
    # Its start is the event date and its end is kept in value_details.
    date_is_range: bool = False


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
        ("stk_kind", "fix_price", "issue_stk_qty"),
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
        ("stk_kind", "fix_rate", "issue_stk_qty"),
    ),
    "purreq": KSDEventSpec(
        "주식매수청구",
        "record_date",
        ("opp_opi_rcpt_term", "buy_req_rcpt_term"),
        (("buy_req_price", "KRW"),),
        ("stk_kind", "buy_req_price"),
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
        ("seq", "opp_cust_cd", "merge_type"),
    ),
    "rev_split": KSDEventSpec(
        "액면교체·병합",
        "record_date",
        ("td_stop_dt", "list_dt"),
        (("inter_bf_face_amt", "KRW"), ("inter_af_face_amt", "KRW")),
        ("stk_kind", "inter_bf_face_amt", "inter_af_face_amt"),
    ),
    "cap_dcrs": KSDEventSpec(
        "자본감소",
        "record_date",
        ("td_stop_dt", "list_dt"),
        (("reduce_cap_rate", "%"),),
        ("reduce_cap_type", "stk_kind", "reduce_cap_rate"),
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
        ("issue_type", "stk_kind", "issue_price", "issue_stk_qty"),
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
        ("lead_mgr", "fix_subscr_pri", "assign_stk_qty"),
    ),
    "forfeit": KSDEventSpec(
        "실권주",
        "record_date",
        ("subscr_dt", "refund_dt", "list_dt"),
        (("subscr_price", "KRW"), ("subscr_stk_qty", "shares")),
        ("lead_mgr", "subscr_price", "subscr_stk_qty"),
    ),
    "mand_deposit": KSDEventSpec(
        "의무예치",
        "depo_date",
        (),
        (("stk_qty", "shares"), ("tot_issue_qty_per_rate", "%")),
        ("depo_reason", "stk_qty", "tot_issue_qty_per_rate"),
        date_is_range=True,
    ),
    "sharehld_meet": KSDEventSpec(
        "주주총회",
        "gen_meet_dt",
        ("record_date",),
        (("vote_tot_qty", "shares"),),
        ("gen_meet_type", "agenda"),
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
    exact_shapes = {
        "%Y%m%d": len(value) == 8 and value.isascii() and value.isdigit(),
        "%Y/%m/%d": (
            len(value) == 10
            and value.isascii()
            and value[:4].isdigit()
            and value[4] == "/"
            and value[5:7].isdigit()
            and value[7] == "/"
            and value[8:].isdigit()
        ),
        "%Y-%m-%d": (
            len(value) == 10
            and value.isascii()
            and value[:4].isdigit()
            and value[4] == "-"
            and value[5:7].isdigit()
            and value[7] == "-"
            and value[8:].isdigit()
        ),
    }
    for date_format, has_exact_shape in exact_shapes.items():
        if not has_exact_shape:
            continue
        try:
            return datetime.strptime(value, date_format).date()
        except ValueError:
            continue
    raise ValueError(f"KSD row has invalid {field} shape={_shape_mask(value)}")


# Observed shape (2026-11-28, pi read-only capture, 20/20 rows of mand_deposit):
# "YYYY/MM/DD ~ YYYY/MM/DD". Any other shape is not parsed.
_DEPOSIT_RANGE_RE = re.compile(r"(\d{4}/\d{2}/\d{2}) ~ (\d{4}/\d{2}/\d{2})", re.ASCII)


def _parse_deposit_range(value: str) -> tuple[date, date]:
    match = _DEPOSIT_RANGE_RE.fullmatch(value)
    if match is None:
        raise ValueError(f"KSD row has invalid depo_date shape={_shape_mask(value)}")
    start, end = (datetime.strptime(part, "%Y/%m/%d").date() for part in match.groups())
    if end < start:
        raise ValueError("KSD row has invalid depo_date order")
    return start, end


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
    if not (len(symbol) == 6 and symbol.isascii() and symbol.isalnum()):
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

    raw_event_date = row.get(spec.date_field)
    if kind in UNSCHEDULED_BLANK_DATE_REASON and (
        raw_event_date is None or raw_event_date == ""
    ):
        raise KSDUnscheduledRowError(
            kind, spec.date_field, UNSCHEDULED_BLANK_DATE_REASON[kind]
        )
    deposit_end: date | None = None
    if spec.date_is_range and isinstance(raw_event_date, str) and "~" in raw_event_date:
        event_date, deposit_end = _parse_deposit_range(raw_event_date)
    else:
        event_date = _parse_date(raw_event_date, spec.date_field)
    assert event_date is not None
    raw_symbol = str(row.get("sht_cd") or "").strip()
    if len(raw_symbol) != 6 or not raw_symbol.isascii() or not raw_symbol.isalnum():
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
    if deposit_end is not None:
        values.append(
            {
                "metric_name": spec.date_field,
                "value_details": {
                    "source_field": spec.date_field,
                    "date_or_range": f"{event_date.isoformat()} ~ {deposit_end.isoformat()}",
                    "deposit_start": event_date.isoformat(),
                    "deposit_end": deposit_end.isoformat(),
                },
            }
        )
        raw_payload["deposit_end"] = deposit_end.isoformat()
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

    selected_identity = {
        field: str(row.get(field) or "").strip() for field in spec.identity_fields
    }
    schedule_fields = {spec.date_field, *spec.date_fields}
    descriptive_fields = {"isin_name", "cust_nm", "opp_cust_nm"}
    provider_identity = {
        field: str(value or "").strip()
        for field, value in row.items()
        if field not in schedule_fields and field not in descriptive_fields
    }
    identity_value = sha256(
        json.dumps(
            {"selected": selected_identity, "provider": provider_identity},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()[:20]
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

"""``kis_live_get_daily_orders`` — read-only KIS live KR orders for one KST date.

hk task 3717. All-symbol, exhaustive daily order listing for one trade date,
for external research sessions on the DEFAULT profile.

Read-only by construction: the only broker call is the daily order inquiry
(``inquire_daily_order_domestic``, TTTC8001R) and the only DB access is two
SELECTs. This module imports no order placement/modify/cancel symbol and no
ledger writer; ``tests/mcp_server/test_kis_live_daily_orders.py`` pins that.

Two statuses, never merged:

* ``broker_status`` — derived from broker fields only (``broker_fields``).
* ``system_status`` — what ``review.kis_live_order_ledger`` says about the same
  order number on the same KST date. ``system_status_basis`` is ``"inference"``
  when the order's proposal rung was closed by the #1112 ``expired[inference]``
  rule (the marker lives on ``order_proposal_rungs.void_reason``, never on the
  order ledger), else ``"ledger"``.

REST daily orders carry no fill time; none is returned.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from sqlalchemy import select

from app.core.db import AsyncSessionLocal
from app.core.timezone import KST, today_kst
from app.models.order_proposals import OrderProposalRung
from app.models.review import KISLiveOrderLedger
from app.services.brokers.kis.client import KISClient
from app.services.brokers.kis.live_order_expiry import row_has_cancel_evidence
from app.services.order_proposals.kis_leftover_inference import (
    EXPIRED_INFERENCE_VOID_REASON,
)

logger = logging.getLogger(__name__)

TOOL_NAME = "kis_live_get_daily_orders"
ACCOUNT_MODE = "kis_live"
# Exhaustive paging cap — the reconcile-path default of the broker client.
MAX_PAGES = 100

BrokerStatus = Literal[
    "filled",
    "partially_filled",
    "working",
    "cancelled",
    "rejected_or_expired_eod",
    "unknown",
]

_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$", re.ASCII)
_SIDE_CODES = {"all": "00", "sell": "01", "buy": "02"}


class DailyOrdersInputError(ValueError):
    """Rejected before any broker or DB call."""


def parse_trade_date(value: object, *, today: dt.date | None = None) -> dt.date:
    """One KST date, ``YYYY-MM-DD``, not in the future."""
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        raise DailyOrdersInputError("trade_date must be a single YYYY-MM-DD date")
    try:
        parsed = dt.date.fromisoformat(value)
    except ValueError as exc:
        raise DailyOrdersInputError(
            "trade_date must be a valid YYYY-MM-DD date"
        ) from exc
    if parsed > (today or today_kst()):
        raise DailyOrdersInputError("trade_date must not be in the future (KST)")
    return parsed


def parse_side(value: object) -> str:
    normalized = "all" if value is None else str(value).strip().lower()
    if normalized not in _SIDE_CODES:
        raise DailyOrdersInputError("side must be 'buy', 'sell' or 'all'")
    return normalized


# ---------------------------------------------------------------------------
# Broker rows
# ---------------------------------------------------------------------------


def _field(row: Mapping[str, Any], name: str) -> Any:
    value = row.get(name)
    return row.get(name.upper()) if value is None else value


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _num(value: Any) -> Decimal | None:
    text = _text(value)
    if text is None:
        return None
    try:
        parsed = Decimal(text)
    except InvalidOperation:
        return None
    return parsed if parsed.is_finite() else None


def _out_num(value: Decimal | None) -> int | float | None:
    if value is None:
        return None
    return int(value) if value == value.to_integral_value() else float(value)


def _order_id_key(order_id: str | None) -> str | None:
    """Zero-stripped order number — KIS pads ODNO on some responses."""
    if order_id is None:
        return None
    stripped = order_id.lstrip("0")
    return stripped or None


def _row_identity(row: Mapping[str, Any]) -> str:
    odno = _text(_field(row, "odno"))
    if odno is not None:
        return f"odno:{odno}"
    return "raw:" + json.dumps(row, sort_keys=True, default=str)


def dedupe_rows(rows: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """KIS re-emits rows across continuation pages; keep the first of each."""
    seen: set[str] = set()
    unique: list[Mapping[str, Any]] = []
    for row in rows:
        identity = _row_identity(row)
        if identity in seen:
            continue
        seen.add(identity)
        unique.append(row)
    return unique


def derive_broker_status(
    *,
    ord_qty: Decimal | None,
    tot_ccld_qty: Decimal | None,
    rmn_qty: Decimal | None,
    rjct_qty: Decimal | None,
    cancelled: bool,
    is_cancel_confirmation: bool,
) -> BrokerStatus:
    """Status from broker fields only. ``filled`` needs ``tot == ord > 0``."""
    if is_cancel_confirmation or cancelled:
        return "cancelled"
    if ord_qty is None or tot_ccld_qty is None or ord_qty <= 0 or tot_ccld_qty < 0:
        return "unknown"
    if tot_ccld_qty > ord_qty:
        return "unknown"
    if tot_ccld_qty == ord_qty:
        return "filled"
    if tot_ccld_qty > 0:
        return "partially_filled"
    if rjct_qty is not None and rjct_qty > 0:
        return "rejected_or_expired_eod"
    if rmn_qty is not None and rmn_qty > 0:
        return "working"
    return "unknown"


def _side_of(row: Mapping[str, Any]) -> str | None:
    code = _text(_field(row, "sll_buy_dvsn_cd"))
    if code == "01":
        return "sell"
    if code == "02":
        return "buy"
    name = _text(_field(row, "sll_buy_dvsn_cd_name")) or ""
    if "매도" in name:
        return "sell"
    if "매수" in name:
        return "buy"
    return None


def normalize_row(row: Mapping[str, Any]) -> dict[str, Any]:
    order_id = _text(_field(row, "odno"))
    original = _order_id_key(_text(_field(row, "orgn_odno")))
    original_id = _text(_field(row, "orgn_odno")) if original else None
    # orgn_odno links BOTH modification and cancellation confirmation rows; only
    # real cancel evidence (cncl_yn, a '취소' side name) means cancelled.
    cancelled = row_has_cancel_evidence(dict(row))
    is_cancel_confirmation = original_id is not None and cancelled

    ord_qty = _num(_field(row, "ord_qty"))
    tot_qty = _num(_field(row, "tot_ccld_qty"))
    rmn_qty = _num(_field(row, "rmn_qty"))
    rjct_qty = _num(_field(row, "rjct_qty"))

    return {
        "order_id": order_id,
        "symbol": _text(_field(row, "pdno")),
        "name": _text(_field(row, "prdt_name")),
        "side": _side_of(row),
        "order_qty": _out_num(ord_qty),
        "order_price": _out_num(_num(_field(row, "ord_unpr"))),
        "filled_qty": _out_num(tot_qty),
        "filled_amount": _out_num(_num(_field(row, "tot_ccld_amt"))),
        "remaining_qty": _out_num(rmn_qty),
        "rejected_qty": _out_num(rjct_qty),
        "cancelled": cancelled,
        "is_cancel_confirmation": is_cancel_confirmation,
        "original_order_id": original_id,
        "exchange": _text(_field(row, "excg_id_dvsn_cd")),
        "order_date": _text(_field(row, "ord_dt")),
        "order_time": _text(_field(row, "ord_tmd")),
        "broker_status": derive_broker_status(
            ord_qty=ord_qty,
            tot_ccld_qty=tot_qty,
            rmn_qty=rmn_qty,
            rjct_qty=rjct_qty,
            cancelled=cancelled,
            is_cancel_confirmation=is_cancel_confirmation,
        ),
        "broker_status_basis": "broker_fields",
        "system_status": None,
        "system_status_basis": None,
        "system_status_error": None,
    }


# ---------------------------------------------------------------------------
# Ledger join (pure) + read-only loaders
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LedgerFact:
    order_no: str
    status: str
    lifecycle_state: str
    sent_at: dt.datetime
    idempotency_key: str | None = None
    correlation_id: str | None = None


@dataclass(frozen=True)
class InferenceRungFact:
    """A rung closed by the #1112 expired[inference] rule."""

    broker_order_id: str
    idempotency_key: str | None = None
    correlation_id: str | None = None


def _rung_is_this_order(rung: InferenceRungFact, fact: LedgerFact) -> bool:
    """Same order number, and the same order instance (not a reused number).

    ``order_proposal_rungs`` has no trade date of its own; the date comes from
    the ledger row (already required to be the requested KST date). Order
    numbers are reused across days, so the rung must also carry the same
    idempotency key or correlation id as that ledger row.
    """
    if _order_id_key(rung.broker_order_id) != _order_id_key(fact.order_no):
        return False
    if rung.idempotency_key and rung.idempotency_key == fact.idempotency_key:
        return True
    return bool(rung.correlation_id and rung.correlation_id == fact.correlation_id)


def _kst_date(moment: dt.datetime) -> dt.date:
    aware = moment if moment.tzinfo else moment.replace(tzinfo=dt.UTC)
    return aware.astimezone(KST).date()


def attach_system_status(
    rows: list[dict[str, Any]],
    *,
    trade_date: dt.date,
    ledger: Sequence[LedgerFact],
    inference_rungs: Sequence[InferenceRungFact],
) -> None:
    """Join by order number AND the KST send date (numbers are reused across days).

    ``order_no`` is unique in the ledger, so a reused number resolves to a row
    of a different date; that row must not be attached. ``system_status_basis``
    is ``"inference"`` only for a ledger row attached on this date whose rung
    carries the #1112 marker; ``"ledger"`` for any other attached row; no row
    means ``system_status`` stays ``None``.
    """
    by_order: dict[str, LedgerFact] = {}
    for fact in ledger:
        key = _order_id_key(fact.order_no)
        if key is not None and _kst_date(fact.sent_at) == trade_date:
            by_order[key] = fact
    for row in rows:
        key = _order_id_key(row["order_id"])
        fact = by_order.get(key) if key is not None else None
        if fact is None:
            continue
        inferred = any(_rung_is_this_order(rung, fact) for rung in inference_rungs)
        row["system_status"] = {
            "status": fact.status,
            "lifecycle_state": fact.lifecycle_state,
        }
        row["system_status_basis"] = "inference" if inferred else "ledger"


async def _load_system_facts(
    order_ids: Sequence[str],
) -> tuple[list[LedgerFact], list[InferenceRungFact]]:
    """Two SELECTs; no writes. Candidates by order number, dates judged later."""
    candidates = {o for o in order_ids if o}
    candidates |= {_order_id_key(o) or "" for o in order_ids}
    candidates.discard("")
    if not candidates:
        return [], []
    async with AsyncSessionLocal() as db:
        ledger_rows = (
            await db.execute(
                select(
                    KISLiveOrderLedger.order_no,
                    KISLiveOrderLedger.status,
                    KISLiveOrderLedger.lifecycle_state,
                    KISLiveOrderLedger.trade_date,
                    KISLiveOrderLedger.idempotency_key,
                    KISLiveOrderLedger.correlation_id,
                ).where(
                    KISLiveOrderLedger.account_mode == ACCOUNT_MODE,
                    KISLiveOrderLedger.instrument_type == "equity_kr",
                    KISLiveOrderLedger.order_no.in_(_padded_variants(candidates)),
                )
            )
        ).all()
        ledger = [
            LedgerFact(
                r.order_no,
                r.status,
                r.lifecycle_state,
                r.trade_date,
                r.idempotency_key,
                r.correlation_id,
            )
            for r in ledger_rows
            if r.order_no
        ]
        if not ledger:
            return [], []
        rung_rows = (
            await db.execute(
                select(
                    OrderProposalRung.broker_order_id,
                    OrderProposalRung.idempotency_key,
                    OrderProposalRung.correlation_id,
                ).where(
                    OrderProposalRung.void_reason == EXPIRED_INFERENCE_VOID_REASON,
                    OrderProposalRung.broker_order_id.in_(
                        _padded_variants(
                            {k for f in ledger if (k := _order_id_key(f.order_no))}
                        )
                        + [f.order_no for f in ledger]
                    ),
                )
            )
        ).all()
    rungs = [
        InferenceRungFact(r.broker_order_id, r.idempotency_key, r.correlation_id)
        for r in rung_rows
        if r.broker_order_id
    ]
    return ledger, rungs


def _padded_variants(stripped_ids: set[str]) -> list[str]:
    """ODNO is 10 digits zero padded on KIS; the ledger stores what KIS echoed."""
    variants: set[str] = set(stripped_ids)
    for value in stripped_ids:
        if value.isdigit():
            variants.add(value.zfill(10))
    return sorted(variants)


# ---------------------------------------------------------------------------
# Tool implementation
# ---------------------------------------------------------------------------


def _error(code: str, exc: BaseException, **extra: Any) -> dict[str, Any]:
    return {
        "success": False,
        "error_code": code,
        "error_class": exc.__class__.__name__,
        "source": "kis",
        "account_mode": ACCOUNT_MODE,
        **extra,
    }


def _is_truncation(exc: BaseException) -> bool:
    return isinstance(exc, RuntimeError) and "truncated at max_pages" in str(exc)


async def kis_live_get_daily_orders_impl(
    *,
    trade_date: str,
    side: str | None = None,
    max_pages: int = MAX_PAGES,
    client_factory: Any = None,
    system_facts_loader: Any = None,
) -> dict[str, Any]:
    try:
        day = parse_trade_date(trade_date)
        side_name = parse_side(side)
    except DailyOrdersInputError as exc:
        return {
            "success": False,
            "error_code": "invalid_input",
            "error_class": exc.__class__.__name__,
            "message": str(exc),
            "source": "mcp",
            "account_mode": ACCOUNT_MODE,
        }

    compact = day.strftime("%Y%m%d")
    base = {
        "account_mode": ACCOUNT_MODE,
        "source": "kis",
        "trade_date": day.isoformat(),
        "side": side_name,
        "max_pages": max_pages,
    }
    try:
        raw_rows = await (client_factory or KISClient)().inquire_daily_order_domestic(
            start_date=compact,
            end_date=compact,
            stock_code="",
            side=_SIDE_CODES[side_name],
            max_pages=max_pages,
        )
    except Exception as exc:  # noqa: BLE001 - closed error shape, no raw message
        if _is_truncation(exc):
            # The broker client discards partial rows when it hits max_pages, so
            # nothing is returned — only the fact that the listing is incomplete.
            return {
                "success": True,
                **base,
                "complete": False,
                "pages": max_pages,
                "error_class": exc.__class__.__name__,
                "count": 0,
                "orders": [],
                "warning": (
                    f"broker paging hit max_pages={max_pages}; listing is "
                    "incomplete and no rows are returned"
                ),
            }
        logger.warning("kis_live_get_daily_orders broker read failed: %s", exc)
        return _error("kis_daily_orders_lookup_failed", exc, **base)

    rows = [normalize_row(r) for r in dedupe_rows(raw_rows or [])]

    loader = system_facts_loader or _load_system_facts
    system_error: str | None = None
    try:
        ledger, rungs = await loader([r["order_id"] for r in rows if r["order_id"]])
        attach_system_status(rows, trade_date=day, ledger=ledger, inference_rungs=rungs)
    except Exception as exc:  # noqa: BLE001 - join is advisory; broker view stands
        logger.warning("kis_live_get_daily_orders ledger join failed: %s", exc)
        system_error = exc.__class__.__name__
        for row in rows:
            row["system_status"] = None
            row["system_status_basis"] = None
            row["system_status_error"] = system_error

    return {
        "success": True,
        **base,
        "complete": True,
        "count": len(rows),
        "orders": rows,
        "system_status_error": system_error,
        "fill_time_available": False,
    }


__all__ = [
    "TOOL_NAME",
    "kis_live_get_daily_orders_impl",
]

"""#1112 — ``expired[inference]`` for leftover KIS regular-session DAY buy rungs.

Why this exists
---------------
The KR standing buy ladder (live prompt 7-D) allows one active buy per symbol.
A KIS rung whose DAY order died at the close stays ``resting`` whenever the
broker never hands the reconcile kernel positive expiry evidence (the order is
past the ``inquire_daily_order_domestic`` reach, or the KIS read tool is not
available to the session — #678). ROB-1284 correctly refuses to close such a
rung without broker evidence, so the symbol stays blocked forever.

This module is the operator-approved (#1112) inference: a rung may be closed as
``expired`` WITHOUT a broker original only when every condition below holds.
The closed rung carries ``EXPIRED_INFERENCE_VOID_REASON`` so it can never be
mistaken for broker-confirmed expiry.

Conditions (ALL must hold; anything else keeps blocking — fail closed)
---------------------------------------------------------------------
1. ``kis_live_resting_buy_rung`` — a ``kis_live`` / ``equity_kr`` BUY rung in
   ``resting`` with a broker order id.
2. ``kis_order_ledger_row_open_and_owned`` — exactly one
   ``review.kis_live_order_ledger`` row, attributed to this rung by the
   existing unambiguous-ownership check, same order number/symbol/side, still
   open (``accepted``/``pending``). A terminal row belongs to the evidence path
   (ROB-1284), not to inference.
3. ``day_order`` — both the proposal and the ledger row are ``limit`` or
   ``market`` (the only KIS domestic ORD_DVSN this repo sends, 00/01 = DAY).
4. ``regular_session_accept`` — the recorded send instant and the broker
   ``ord_tmd`` instant are both inside the XKRX regular session of a confirmed
   trading day (calendar bounds, so late-open/late-close days are honoured) and
   the ROB-671 window classifier agrees it is ``regular``. The broker-reported
   venue, when present, is KRX or SOR (NXT-direct or any other venue blocks).
5. ``day_close_passed`` — ``now`` is strictly after the latest of: the submit
   day 15:30 KST, that day's calendar close, and the ROB-671 conservative
   expected expiry for the order (a regular-session SOR buy is conservatively
   alive until the NXT close at 20:00 unless the operator's 15:30 downgrade
   flag is on). The inference is never earlier than any expiry this repo
   already believes.
6. ``execution_ledger_covers_order_day`` — a successful, committed KIS
   execution-ledger reconcile run covered the whole window from the accept
   instant to that deadline and finished after the deadline, so "no fill" is
   an observation, not a missing read. "After" is strict: a run finishing
   exactly at the deadline does not count.
7. ``no_fill_in_execution_ledger`` — no execution-ledger fill (any source,
   including provisional websocket rows) for this order number, no partial on
   the rung, no partial on the order-ledger row.
8. ``holding_quantity_unchanged`` — the symbol has ledger rows (a known
   holding), no fill of the symbol from any order at or after the accept
   instant, and the ledger net quantity at submit equals the net now.

Pure — stdlib plus the stdlib-only ROB-671 classifier. No DB, broker, network
or clock: the caller injects every fact and ``now``. Nothing here can place,
modify or cancel an order.
"""

from __future__ import annotations

import dataclasses
import datetime
import re
from collections.abc import Callable
from decimal import Decimal

from app.services.brokers.kis.live_order_expiry import (
    SESSION_REGULAR,
    classify_kr_accept_session,
    kr_day_order_expiry,
)

__all__ = [
    "CONDITIONS",
    "EXPIRED_INFERENCE_CAVEAT",
    "EXPIRED_INFERENCE_VOID_REASON",
    "INFERENCE_RULE_ID",
    "FillFacts",
    "InferenceDecision",
    "KisOrderLedgerFacts",
    "LeftoverRungFacts",
    "ReconcileRunFacts",
    "classify_leftover_rung",
    "inference_deadline",
    "is_expired_inference_reason",
    "resolve_accept_at",
]

KST = datetime.timezone(datetime.timedelta(hours=9))

INFERENCE_RULE_ID = "kis_regular_day_leftover_expired_inference"
# Stored in ``order_proposal_rungs.void_reason``. The ``expired_`` prefix keeps
# the closed void-reason group ``cancelled_or_expired``; the rest of the string
# is what distinguishes it from broker-confirmed expiry (which carries no
# void_reason or a broker-derived one).
EXPIRED_INFERENCE_VOID_REASON = (
    "expired_inference:kis_regular_day_order_no_broker_original"
)
EXPIRED_INFERENCE_CAVEAT = "no_broker_original"

COND_KIS_LIVE_RESTING_BUY = "kis_live_resting_buy_rung"
COND_ORDER_LEDGER_ROW = "kis_order_ledger_row_open_and_owned"
COND_DAY_ORDER = "day_order"
COND_REGULAR_SESSION = "regular_session_accept"
COND_DAY_CLOSE_PASSED = "day_close_passed"
COND_LEDGER_COVERAGE = "execution_ledger_covers_order_day"
COND_NO_FILL = "no_fill_in_execution_ledger"
COND_HOLDING_UNCHANGED = "holding_quantity_unchanged"

_OPEN_LEDGER_STATUSES = frozenset({"accepted", "pending"})
_DAY_ORDER_TYPES = frozenset({"limit", "market"})
# Venues a live KIS domestic order can report for a regular-session order.
# ``None`` (not echoed) is allowed: the send path only ever routes SOR or KRX.
_REGULAR_VENUES = frozenset({"KRX", "SOR"})
_REGULAR_CLOSE_FLOOR = datetime.time(hour=15, minute=30)
_ORDER_TIME = re.compile(r"[0-9]{6}|[0-9]{4}", re.ASCII)


def is_expired_inference_reason(void_reason: object) -> bool:
    """True iff a rung's ``void_reason`` is this module's inference marker."""
    return void_reason == EXPIRED_INFERENCE_VOID_REASON


@dataclasses.dataclass(frozen=True)
class KisOrderLedgerFacts:
    """One ``review.kis_live_order_ledger`` row already attributed to the rung."""

    ledger_id: int
    order_no: str | None
    status: str
    order_type: str | None
    side: str
    symbol: str
    trade_date: datetime.datetime | None
    order_time: str | None
    broker_exchange: str | None
    filled_qty: Decimal | None


@dataclasses.dataclass(frozen=True)
class FillFacts:
    """One ``review.execution_ledger`` KIS live KR fill of the rung's symbol."""

    broker_order_id: str
    side: str
    quantity: Decimal
    filled_at: datetime.datetime
    source: str


@dataclasses.dataclass(frozen=True)
class ReconcileRunFacts:
    """One successful, committed KIS execution-ledger reconcile run."""

    window_start: datetime.datetime
    window_end: datetime.datetime
    finished_at: datetime.datetime


@dataclasses.dataclass(frozen=True)
class LeftoverRungFacts:
    """Everything the rule reads for one rung. ``None`` means unreadable."""

    proposal_id: str
    rung_id: int
    rung_index: int
    rung_state: str
    side: str
    symbol: str
    market: str
    account_mode: str
    group_order_type: str | None
    broker_order_id: str | None
    rung_filled_qty: Decimal | None
    order_ledger_rows: tuple[KisOrderLedgerFacts, ...] | None
    order_ledger_conflict: str | None
    session_bounds: tuple[datetime.datetime, datetime.datetime] | None
    symbol_fills: tuple[FillFacts, ...] | None
    reconcile_runs: tuple[ReconcileRunFacts, ...] | None
    unsettled_regular_buy_downgrade: bool = False


@dataclasses.dataclass(frozen=True)
class InferenceDecision:
    facts: LeftoverRungFacts
    eligible: bool
    failed_conditions: tuple[str, ...]
    accept_at: datetime.datetime | None
    deadline: datetime.datetime | None
    deadline_reason: str | None
    ledger_id: int | None
    observed_at: datetime.datetime

    def as_row(self) -> dict[str, object]:
        return {
            "rule": INFERENCE_RULE_ID,
            "proposal_id": self.facts.proposal_id,
            "rung_id": self.facts.rung_id,
            "rung_index": self.facts.rung_index,
            "rung_state": self.facts.rung_state,
            "symbol": self.facts.symbol,
            "account_mode": self.facts.account_mode,
            "broker_order_id": self.facts.broker_order_id,
            "order_ledger_id": self.ledger_id,
            "eligible": self.eligible,
            "failed_conditions": list(self.failed_conditions),
            "accept_at": self.accept_at.isoformat() if self.accept_at else None,
            "inferred_expiry_after": (
                self.deadline.isoformat() if self.deadline else None
            ),
            "deadline_reason": self.deadline_reason,
            "caveat": EXPIRED_INFERENCE_CAVEAT if self.eligible else None,
            "observed_at": self.observed_at.isoformat(),
        }


def _aware(value: datetime.datetime) -> datetime.datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=KST)


def _norm_order_no(value: str | None) -> str:
    raw = str(value or "").strip()
    return raw.lstrip("0") or raw


def _single_row(facts: LeftoverRungFacts) -> KisOrderLedgerFacts | None:
    rows = facts.order_ledger_rows
    if rows is None or len(rows) != 1 or facts.order_ledger_conflict is not None:
        return None
    return rows[0]


def resolve_accept_at(row: KisOrderLedgerFacts) -> datetime.datetime | None:
    """Broker accept instant: the send-day KST date plus the broker ``ord_tmd``.

    ``None`` when either half is missing or malformed — an unknown accept time
    is an unknown session, never a regular one.
    """
    if row.trade_date is None or row.order_time is None:
        return None
    raw = str(row.order_time).strip()
    # Exact ASCII HHMMSS (or HHMM) only. Stripping separators or accepting
    # non-ASCII digits would turn a garbled value into a plausible time.
    if _ORDER_TIME.fullmatch(raw) is None:
        return None
    digits = (raw + "00")[:6]
    try:
        clock = datetime.time(
            hour=int(digits[0:2]), minute=int(digits[2:4]), second=int(digits[4:6])
        )
    except ValueError:
        return None
    day = _aware(row.trade_date).astimezone(KST).date()
    return datetime.datetime.combine(day, clock, tzinfo=KST)


def inference_deadline(
    *,
    accept_at: datetime.datetime,
    side: str,
    session_bounds: tuple[datetime.datetime, datetime.datetime] | None,
    broker_exchange: str | None,
    unsettled_regular_buy_downgrade: bool,
) -> tuple[datetime.datetime | None, str | None]:
    """The instant after which the order is inferred dead, and why.

    The latest of three clocks, so the inference is never earlier than any
    expiry this repository already believes: submit-day 15:30 KST, the calendar
    close of that session (late-close days), and the ROB-671 conservative
    expected expiry.
    """
    if session_bounds is None:
        return None, None
    local = _aware(accept_at).astimezone(KST)
    floor = datetime.datetime.combine(local.date(), _REGULAR_CLOSE_FLOOR, tzinfo=KST)
    close = _aware(session_bounds[1]).astimezone(KST)
    venue = (broker_exchange or "").strip().upper()
    expiry_iso, expiry_reason = kr_day_order_expiry(
        accepted_at=local,
        side=side,
        accept_session=SESSION_REGULAR,
        unsettled_regular_buy_downgrade=unsettled_regular_buy_downgrade,
        nxt_tradable=False if venue == "KRX" else None,
    )
    if expiry_iso is None:
        return None, None
    expected = _aware(datetime.datetime.fromisoformat(expiry_iso)).astimezone(KST)
    candidates = [
        (floor, "submit_day_15_30_kst"),
        (close, "calendar_session_close"),
        (expected, expiry_reason),
    ]
    deadline, reason = max(candidates, key=lambda item: item[0])
    return deadline, reason


# --- one predicate per condition ---------------------------------------------
# Each takes the same context and returns True only when the condition HOLDS.
# ``tests/services/order_proposals/test_kis_leftover_inference_mutants.py``
# counts these functions on disk and mutates each one.


@dataclasses.dataclass(frozen=True)
class _Context:
    facts: LeftoverRungFacts
    row: KisOrderLedgerFacts | None
    accept_at: datetime.datetime | None
    deadline: datetime.datetime | None
    now: datetime.datetime


def _check_kis_live_resting_buy_rung(ctx: _Context) -> bool:
    facts = ctx.facts
    return (
        facts.account_mode == "kis_live"
        and facts.market == "equity_kr"
        and facts.side == "buy"
        and facts.rung_state == "resting"
        and bool(_norm_order_no(facts.broker_order_id))
    )


def _check_kis_order_ledger_row_open_and_owned(ctx: _Context) -> bool:
    row = ctx.row
    if row is None:
        return False
    return (
        _norm_order_no(row.order_no) != ""
        and _norm_order_no(row.order_no) == _norm_order_no(ctx.facts.broker_order_id)
        and row.symbol == ctx.facts.symbol
        and row.side == "buy"
        and row.status.strip().lower() in _OPEN_LEDGER_STATUSES
    )


def _check_day_order(ctx: _Context) -> bool:
    row = ctx.row
    if row is None:
        return False
    ledger_type = (row.order_type or "").strip().lower()
    group_type = (ctx.facts.group_order_type or "").strip().lower()
    return ledger_type in _DAY_ORDER_TYPES and group_type in _DAY_ORDER_TYPES


def _check_regular_session_accept(ctx: _Context) -> bool:
    row = ctx.row
    bounds = ctx.facts.session_bounds
    if row is None or ctx.accept_at is None or bounds is None:
        return False
    if row.trade_date is None:
        return False
    open_at, close_at = (_aware(bounds[0]), _aware(bounds[1]))
    recorded_at = _aware(row.trade_date)
    for instant in (ctx.accept_at, recorded_at):
        if not (open_at <= instant < close_at):
            return False
        if classify_kr_accept_session(instant.astimezone(KST)) != SESSION_REGULAR:
            return False
    venue = (row.broker_exchange or "").strip().upper()
    return venue == "" or venue in _REGULAR_VENUES


def _check_day_close_passed(ctx: _Context) -> bool:
    return ctx.deadline is not None and ctx.now > ctx.deadline


def _check_execution_ledger_covers_order_day(ctx: _Context) -> bool:
    runs = ctx.facts.reconcile_runs
    if runs is None or ctx.accept_at is None or ctx.deadline is None:
        return False
    return any(
        _aware(run.window_start) <= ctx.accept_at
        and _aware(run.window_end) >= ctx.deadline
        and _aware(run.finished_at) > ctx.deadline
        for run in runs
    )


def _check_no_fill_in_execution_ledger(ctx: _Context) -> bool:
    facts = ctx.facts
    if facts.symbol_fills is None:
        return False
    if facts.rung_filled_qty is not None and facts.rung_filled_qty != 0:
        return False
    row = ctx.row
    if row is not None:
        if row.status.strip().lower() == "partial":
            return False
        if row.filled_qty is not None and row.filled_qty != 0:
            return False
    target = _norm_order_no(facts.broker_order_id)
    if not target:
        return False
    return not any(
        _norm_order_no(fill.broker_order_id) == target for fill in facts.symbol_fills
    )


def _signed(fill: FillFacts) -> Decimal:
    return fill.quantity if fill.side == "buy" else -fill.quantity


def _check_holding_quantity_unchanged(ctx: _Context) -> bool:
    fills = ctx.facts.symbol_fills
    if not fills or ctx.accept_at is None:
        # No ledger rows at all is an unknown holding, not an unchanged one.
        return False
    if any(_aware(fill.filled_at) >= ctx.accept_at for fill in fills):
        return False
    at_submit = sum(
        (_signed(f) for f in fills if _aware(f.filled_at) < ctx.accept_at),
        Decimal(0),
    )
    now_qty = sum((_signed(f) for f in fills), Decimal(0))
    return at_submit == now_qty


CONDITIONS: tuple[tuple[str, Callable[[_Context], bool]], ...] = (
    (COND_KIS_LIVE_RESTING_BUY, _check_kis_live_resting_buy_rung),
    (COND_ORDER_LEDGER_ROW, _check_kis_order_ledger_row_open_and_owned),
    (COND_DAY_ORDER, _check_day_order),
    (COND_REGULAR_SESSION, _check_regular_session_accept),
    (COND_DAY_CLOSE_PASSED, _check_day_close_passed),
    (COND_LEDGER_COVERAGE, _check_execution_ledger_covers_order_day),
    (COND_NO_FILL, _check_no_fill_in_execution_ledger),
    (COND_HOLDING_UNCHANGED, _check_holding_quantity_unchanged),
)


def classify_leftover_rung(
    facts: LeftoverRungFacts, *, now: datetime.datetime
) -> InferenceDecision:
    """Evaluate every condition and report all failures, never just the first.

    ``eligible`` is True only when the failure list is empty.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    row = _single_row(facts)
    accept_at = resolve_accept_at(row) if row is not None else None
    deadline: datetime.datetime | None = None
    deadline_reason: str | None = None
    if row is not None and accept_at is not None:
        deadline, deadline_reason = inference_deadline(
            accept_at=accept_at,
            side=facts.side,
            session_bounds=facts.session_bounds,
            broker_exchange=row.broker_exchange,
            unsettled_regular_buy_downgrade=facts.unsettled_regular_buy_downgrade,
        )
    ctx = _Context(
        facts=facts, row=row, accept_at=accept_at, deadline=deadline, now=now
    )
    failed = tuple(name for name, check in CONDITIONS if not check(ctx))
    return InferenceDecision(
        facts=facts,
        eligible=not failed,
        failed_conditions=failed,
        accept_at=accept_at,
        deadline=deadline,
        deadline_reason=deadline_reason,
        ledger_id=row.ledger_id if row is not None else None,
        observed_at=now,
    )

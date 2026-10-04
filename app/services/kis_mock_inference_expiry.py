"""#1250 — #1112 ``expired[inference]`` for four stale kis_mock ledger rows.

Operator decision (hk #706 comment 1092, 2026-10-05, ``Q-46``): close kis_mock
ledger rows 80, 66, 64 and 63 — July/August shadow pending DAY buys that the
#881 Q-46 tool cannot close because no zero fill was ever recorded for them —
with the #1112 inference rule, skipping the strategy match. This is a one-off
lever, not a general one:

* the id allowlist below is baked in and a batch must be exactly that set;
* the decision reference must be exactly ``Q-46``;
* the audit table carries the same allowlist as a DB CHECK.

The #1112 rule (``app/services/order_proposals/kis_leftover_inference.py``) is
written for ``kis_live`` proposal rungs. Each condition is translated to the
evidence a kis_mock ledger row actually has; the translation is one predicate
per condition so the mutant test can count and break each one from disk:

1. ``kis_mock_accepted_buy_row`` — a ``kis_mock``/``kis`` KR cash-equity BUY
   whose stored broker response is a positive accept (``rt_cd`` 0, ``odno`` =
   order number, ``ord_tmd`` = order time) and that is not a synthetic
   scalping row. (#1112 1+2: the order exists and is ours.)
2. ``kis_mock_row_open`` — lifecycle still ``accepted``/``pending``. A terminal
   row belongs to whichever path closed it, never to inference.
3. ``day_order`` — limit with a positive price (ORD_DVSN 00) or market with a
   zero price (01), positive whole quantity.
4. ``regular_session_accept`` — the recorded send instant and the broker
   ``ord_tmd`` instant are both inside the XKRX regular session of a confirmed
   trading day and the ROB-671 classifier agrees it is ``regular``.
5. ``day_close_passed`` — ``now`` is after the #1112 deadline (the latest of
   submit-day 15:30 KST, the calendar close and the ROB-671 conservative
   expiry), computed by the #1112 function itself.
6. ``reconcile_coverage`` — see ``WAIVED_CONDITIONS``.
7. ``no_fill_recorded_for_order`` — no fill evidence for this order in any fill
   source the ledgers have: ``review.execution_ledger`` kis/mock rows for the
   order number (any source, quarantined rows included), the row's own fill
   reason codes / attributed quantity, and kis_mock rows sharing its
   correlation id.
8. ``holding_quantity_unchanged`` — the holding at send is known
   (``holdings_baseline_qty`` recorded) and no fill of the symbol from any order
   is recorded at or after the accept instant (kis_mock ledger rows with fill
   evidence, execution_ledger kis/mock rows).

Strategy match is skipped by the decision and is the only condition listed as
waived besides those named in ``WAIVED_CONDITIONS``; it is recorded in every
audit row. Pure: stdlib plus the stdlib-only #1112 / ROB-671 helpers. No DB,
broker, network or clock — the caller injects facts and ``now``.
"""

from __future__ import annotations

import dataclasses
import datetime
import re
import unicodedata
from collections.abc import Callable, Iterable, Mapping
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from app.services.brokers.kis.live_order_expiry import (
    SESSION_REGULAR,
    classify_kr_accept_session,
)
from app.services.order_proposals.kis_leftover_inference import (
    EXPIRED_INFERENCE_CAVEAT,
    EXPIRED_INFERENCE_VOID_REASON,
    KST,
    KisOrderLedgerFacts,
    inference_deadline,
    resolve_accept_at,
)

__all__ = [
    "ALLOWED_LEDGER_IDS",
    "CONDITIONS",
    "EXPIRY_BASIS",
    "INFERENCE_CAVEAT",
    "INFERENCE_REASON_CODE",
    "REQUIRED_DECISION_REF",
    "RULE_ID",
    "RULE_VERSION",
    "WAIVED_CONDITIONS",
    "BatchStatus",
    "accept_instant",
    "ExecFillFacts",
    "InferenceInputError",
    "MockLedgerRowFacts",
    "MockSiblingFacts",
    "RowDecision",
    "RowEvidence",
    "classify_row",
    "closed_detail",
    "decide_batch",
    "is_closed_by_this_rule",
    "parse_ids",
    "validate_decision_ref",
    "validate_text",
]

#: The four rows named by the operator decision. Nothing else is ever touched.
ALLOWED_LEDGER_IDS: frozenset[int] = frozenset({63, 64, 66, 80})
REQUIRED_DECISION_REF = "Q-46"
RULE_ID = "kis_mock_regular_day_leftover_expired_inference_q46"
RULE_VERSION = "kis_mock_expired_inference_t1250_v1"
#: The exact #1112 marker and caveat, so an inferred close never reads as a
#: broker-confirmed one anywhere the #1112 vocabulary is understood.
INFERENCE_REASON_CODE = EXPIRED_INFERENCE_VOID_REASON
INFERENCE_CAVEAT = EXPIRED_INFERENCE_CAVEAT
EXPIRY_BASIS = "inference"
#: Conditions of the #1112 rule that the Q-46 decision explicitly waives.
WAIVED_CONDITIONS: tuple[str, ...] = ("strategy_match",)

MAX_REASON_CHARS = 500
MAX_ACTOR_CHARS = 100
_ID_TOKEN = re.compile(r"[1-9][0-9]{0,18}", re.ASCII)
_ORDER_NO = re.compile(r"[0-9]{1,20}", re.ASCII)
_SYMBOL = re.compile(r"[0-9]{6}", re.ASCII)
_ORDER_TIME = re.compile(r"[0-9]{6}", re.ASCII)
_OPEN_STATES = frozenset({"accepted", "pending"})
_FILLED_STATES = frozenset({"fill", "reconciled"})
#: kis_mock holdings-reconciler reason codes that mean holdings moved or a fill
#: was attributed (``kis_mock_holdings_reconciler``). ``attribution_unconfirmed``
#: and ``holdings_mismatch`` are included: both mean the symbol's holding moved.
_FILL_REASON_CODES = frozenset(
    {
        "fill_detected",
        "partial_fill_detected",
        "position_reconciled",
        "holdings_mismatch",
        "attribution_unconfirmed",
    }
)

BatchStatus = Literal["eligible", "refused", "noop", "committed"]


class InferenceInputError(ValueError):
    """Operator input (ids, decision ref, reason, actor) is not acceptable."""


# --------------------------------------------------------------------- input


def parse_ids(values: Iterable[str]) -> tuple[int, ...]:
    """Exactly the allowlisted ids, each once, as plain decimals.

    Ranges, signs, whitespace, non-ASCII digits, duplicates, any id outside
    ``ALLOWED_LEDGER_IDS`` and any subset of it are refused before a database
    is opened.
    """
    ids: list[int] = []
    for value in values:
        if not isinstance(value, str):
            raise InferenceInputError("--ids values must be strings")
        for token in value.split(","):
            if not _ID_TOKEN.fullmatch(token):
                raise InferenceInputError(
                    f"--ids token {token!r} is not an exact positive decimal id"
                )
            number = int(token)
            if number not in ALLOWED_LEDGER_IDS:
                raise InferenceInputError(
                    f"--ids {number} is outside the Q-46 allowlist "
                    f"{sorted(ALLOWED_LEDGER_IDS)}"
                )
            if number in ids:
                raise InferenceInputError(f"--ids repeats id {number}")
            ids.append(number)
    if set(ids) != ALLOWED_LEDGER_IDS:
        raise InferenceInputError(
            f"--ids must name exactly {sorted(ALLOWED_LEDGER_IDS)}"
        )
    return tuple(ids)


def validate_decision_ref(value: Any) -> str:
    if value != REQUIRED_DECISION_REF:
        raise InferenceInputError(
            f"--decision-ref must be exactly {REQUIRED_DECISION_REF}"
        )
    return REQUIRED_DECISION_REF


def validate_text(name: str, value: Any, *, max_chars: int) -> str:
    """A required, bounded, single-line operator string (reason / actor)."""
    if not isinstance(value, str):
        raise InferenceInputError(f"{name} is required")
    text = value.strip()
    if not text:
        raise InferenceInputError(f"{name} must not be blank")
    if len(text) > max_chars:
        raise InferenceInputError(f"{name} exceeds {max_chars} characters")
    if any(unicodedata.category(ch) in {"Cc", "Cf"} for ch in text):
        raise InferenceInputError(f"{name} must not contain control characters")
    return text


# --------------------------------------------------------------------- facts


@dataclasses.dataclass(frozen=True)
class MockLedgerRowFacts:
    """One ``review.kis_mock_order_ledger`` row, copied attribute for attribute."""

    ledger_id: int
    lifecycle_state: str | None
    status: str | None
    account_mode: str | None
    broker: str | None
    instrument_type: str | None
    currency: str | None
    side: str | None
    symbol: str | None
    order_type: str | None
    quantity: Decimal | None
    price: Decimal | None
    order_no: str | None
    order_time: str | None
    trade_date: datetime.datetime | None
    response_code: str | None
    raw_response: Mapping[str, Any] | None
    scalping_role: str | None
    correlation_id: str | None
    last_reconcile_detail: Mapping[str, Any] | None
    holdings_baseline_qty: Decimal | None
    reconciled_at: datetime.datetime | None = None
    strategy: str | None = None


@dataclasses.dataclass(frozen=True)
class ExecFillFacts:
    """One ``review.execution_ledger`` kis/mock row (quarantined included)."""

    ledger_id: int
    broker_order_id: str
    symbol: str
    filled_at: datetime.datetime
    source: str
    quarantined: bool


@dataclasses.dataclass(frozen=True)
class MockSiblingFacts:
    """Another kis_mock ledger row (same symbol or same correlation id)."""

    ledger_id: int
    symbol: str | None
    correlation_id: str | None
    lifecycle_state: str | None
    last_reconcile_detail: Mapping[str, Any] | None
    trade_date: datetime.datetime | None
    reconciled_at: datetime.datetime | None


@dataclasses.dataclass(frozen=True)
class RowEvidence:
    """Everything the rule reads for one id. ``None`` means unreadable."""

    ledger_id: int
    row: MockLedgerRowFacts | None
    session_bounds: tuple[datetime.datetime, datetime.datetime] | None
    order_exec_fills: tuple[ExecFillFacts, ...] | None
    symbol_exec_fills: tuple[ExecFillFacts, ...] | None
    symbol_rows: tuple[MockSiblingFacts, ...] | None
    correlation_rows: tuple[MockSiblingFacts, ...] | None
    audit_recorded: bool = False


# ------------------------------------------------------------------- helpers


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def _aware(value: datetime.datetime) -> datetime.datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=KST)


def _norm_order_no(value: Any) -> str:
    raw = str(value or "").strip()
    return raw.lstrip("0") or raw


def _detail_has_fill_evidence(detail: Mapping[str, Any] | None) -> bool:
    """True when a kis_mock reconcile detail records (or may record) a fill.

    An unparseable ``attributed_fill_qty`` counts as a fill: unknown is never
    zero.
    """
    if detail is None:
        return False
    if not isinstance(detail, Mapping):
        return True
    if detail.get("reason_code") in _FILL_REASON_CODES:
        return True
    if "attributed_fill_qty" in detail:
        attributed = _decimal(detail.get("attributed_fill_qty"))
        if attributed is None or attributed != 0:
            return True
    return False


def _sibling_has_fill_evidence(row: MockSiblingFacts) -> bool:
    return row.lifecycle_state in _FILLED_STATES or _detail_has_fill_evidence(
        row.last_reconcile_detail
    )


def _ledger_facts(row: MockLedgerRowFacts) -> KisOrderLedgerFacts:
    """The #1112 order-ledger shape, so its accept/deadline helpers are reused."""
    return KisOrderLedgerFacts(
        ledger_id=row.ledger_id,
        order_no=row.order_no,
        status=str(row.status or ""),
        order_type=row.order_type,
        side=str(row.side or ""),
        symbol=str(row.symbol or ""),
        trade_date=row.trade_date,
        order_time=row.order_time,
        # KIS mock domestic orders are KRX orders; the stored response echoes no
        # venue, which #1112 treats as the conservative (latest) expiry.
        broker_exchange=None,
        filled_qty=None,
    )


def accept_instant(row: MockLedgerRowFacts) -> datetime.datetime | None:
    """Broker accept instant (send-day KST date + ``ord_tmd``), via #1112."""
    return resolve_accept_at(_ledger_facts(row))


def is_closed_by_this_rule(row: MockLedgerRowFacts | None) -> bool:
    """True iff the row is ``expired`` with this rule's marker and decision ref."""
    if row is None or row.lifecycle_state != "expired":
        return False
    detail = row.last_reconcile_detail
    return (
        isinstance(detail, Mapping)
        and detail.get("reason_code") == INFERENCE_REASON_CODE
        and detail.get("inference_rule") == RULE_ID
        and detail.get("operator_decision_ref") == REQUIRED_DECISION_REF
    )


# --------------------------------------------------------- one per condition
# Each takes the same context and returns True only when the condition HOLDS.
# ``tests/services/test_kis_mock_inference_expiry_mutants.py`` counts these
# functions on disk and mutates each one.


@dataclasses.dataclass(frozen=True)
class _Context:
    evidence: RowEvidence
    row: MockLedgerRowFacts | None
    accept_at: datetime.datetime | None
    deadline: datetime.datetime | None
    now: datetime.datetime


def _check_kis_mock_accepted_buy_row(ctx: _Context) -> bool:
    row = ctx.row
    if row is None:
        return False
    raw = row.raw_response
    order_no = row.order_no
    return (
        row.account_mode == "kis_mock"
        and row.broker == "kis"
        and row.instrument_type == "equity_kr"
        and row.currency == "KRW"
        and row.side == "buy"
        and row.status == "accepted"
        and row.scalping_role is None
        and isinstance(row.symbol, str)
        and _SYMBOL.fullmatch(row.symbol) is not None
        and isinstance(order_no, str)
        and _ORDER_NO.fullmatch(order_no) is not None
        and row.response_code == "0"
        and isinstance(raw, Mapping)
        and raw.get("rt_cd") == "0"
        and raw.get("odno") == order_no
        and isinstance(raw.get("ord_tmd"), str)
        and _ORDER_TIME.fullmatch(raw["ord_tmd"]) is not None
        and raw.get("ord_tmd") == row.order_time
    )


def _check_kis_mock_row_open(ctx: _Context) -> bool:
    row = ctx.row
    return row is not None and row.lifecycle_state in _OPEN_STATES


def _check_day_order(ctx: _Context) -> bool:
    row = ctx.row
    if row is None:
        return False
    qty = _decimal(row.quantity)
    price = _decimal(row.price)
    if qty is None or qty <= 0 or qty != qty.to_integral_value() or price is None:
        return False
    return (row.order_type == "limit" and price > 0) or (
        row.order_type == "market" and price == 0
    )


def _check_regular_session_accept(ctx: _Context) -> bool:
    row = ctx.row
    bounds = ctx.evidence.session_bounds
    if row is None or ctx.accept_at is None or bounds is None:
        return False
    if row.trade_date is None:
        return False
    open_at, close_at = _aware(bounds[0]), _aware(bounds[1])
    for instant in (ctx.accept_at, _aware(row.trade_date)):
        if not (open_at <= instant < close_at):
            return False
        if classify_kr_accept_session(instant.astimezone(KST)) != SESSION_REGULAR:
            return False
    return True


def _check_day_close_passed(ctx: _Context) -> bool:
    return ctx.deadline is not None and ctx.now > ctx.deadline


def _check_no_fill_recorded_for_order(ctx: _Context) -> bool:
    row = ctx.row
    evidence = ctx.evidence
    if row is None or row.lifecycle_state in _FILLED_STATES:
        return False
    if _detail_has_fill_evidence(row.last_reconcile_detail):
        return False
    if evidence.order_exec_fills is None or evidence.correlation_rows is None:
        return False
    target = _norm_order_no(row.order_no)
    if not target:
        return False
    if any(
        _norm_order_no(f.broker_order_id) == target for f in evidence.order_exec_fills
    ):
        return False
    return not any(
        sibling.ledger_id != row.ledger_id and _sibling_has_fill_evidence(sibling)
        for sibling in evidence.correlation_rows
    )


def _sibling_fill_may_postdate(
    sibling: MockSiblingFacts, accept_at: datetime.datetime
) -> bool:
    """A sibling's fill is provably before ``accept_at`` only when it was both
    placed and reconciled before it; anything else may have moved the holding."""
    if sibling.trade_date is None or sibling.reconciled_at is None:
        return True
    return not (
        _aware(sibling.trade_date) < accept_at
        and _aware(sibling.reconciled_at) < accept_at
    )


def _check_holding_quantity_unchanged(ctx: _Context) -> bool:
    row = ctx.row
    evidence = ctx.evidence
    if row is None or ctx.accept_at is None:
        return False
    if _decimal(row.holdings_baseline_qty) is None:
        # No recorded holding at send is an unknown holding, not an unchanged one.
        return False
    if evidence.symbol_exec_fills is None or evidence.symbol_rows is None:
        return False
    if any(_aware(f.filled_at) >= ctx.accept_at for f in evidence.symbol_exec_fills):
        return False
    return not any(
        sibling.ledger_id != row.ledger_id
        and _sibling_has_fill_evidence(sibling)
        and _sibling_fill_may_postdate(sibling, ctx.accept_at)
        for sibling in evidence.symbol_rows
    )


CONDITIONS: tuple[tuple[str, Callable[[_Context], bool]], ...] = (
    ("kis_mock_accepted_buy_row", _check_kis_mock_accepted_buy_row),
    ("kis_mock_row_open", _check_kis_mock_row_open),
    ("day_order", _check_day_order),
    ("regular_session_accept", _check_regular_session_accept),
    ("day_close_passed", _check_day_close_passed),
    ("no_fill_recorded_for_order", _check_no_fill_recorded_for_order),
    ("holding_quantity_unchanged", _check_holding_quantity_unchanged),
)


# ------------------------------------------------------------------ decision


RowVerdict = Literal["eligible", "refused", "already_closed"]


@dataclasses.dataclass(frozen=True)
class RowDecision:
    ledger_id: int
    verdict: RowVerdict
    failed_conditions: tuple[str, ...]
    accept_at: datetime.datetime | None
    deadline: datetime.datetime | None
    deadline_reason: str | None
    before_state: str | None
    snapshot: dict[str, Any]

    @property
    def eligible(self) -> bool:
        return self.verdict == "eligible"

    def as_dict(self) -> dict[str, Any]:
        return {
            "ledger_id": self.ledger_id,
            "verdict": self.verdict,
            "eligible": self.eligible,
            "failed_conditions": list(self.failed_conditions),
            "waived_conditions": list(WAIVED_CONDITIONS),
            "accept_at": self.accept_at.isoformat() if self.accept_at else None,
            "inferred_expiry_after": (
                self.deadline.isoformat() if self.deadline else None
            ),
            "deadline_reason": self.deadline_reason,
            "before_state": self.before_state,
            "row": self.snapshot,
        }


def _snapshot(row: MockLedgerRowFacts | None) -> dict[str, Any]:
    if row is None:
        return {}
    return {
        "lifecycle_state": row.lifecycle_state,
        "status": row.status,
        "account_mode": row.account_mode,
        "symbol": row.symbol,
        "side": row.side,
        "order_type": row.order_type,
        "quantity": None if row.quantity is None else format(row.quantity, "f"),
        "price": None if row.price is None else format(row.price, "f"),
        "order_no": row.order_no,
        "order_time": row.order_time,
        "trade_date": row.trade_date.isoformat() if row.trade_date else None,
        "holdings_baseline_qty": (
            None
            if row.holdings_baseline_qty is None
            else format(row.holdings_baseline_qty, "f")
        ),
        "strategy_recorded": row.strategy,
        "last_reconcile_detail": (
            dict(row.last_reconcile_detail)
            if isinstance(row.last_reconcile_detail, Mapping)
            else row.last_reconcile_detail
        ),
    }


def classify_row(evidence: RowEvidence, *, now: datetime.datetime) -> RowDecision:
    """Evaluate every condition and report all failures, never just the first."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    row = evidence.row
    snapshot = _snapshot(row)
    before = row.lifecycle_state if row is not None else None
    if row is not None and is_closed_by_this_rule(row) and evidence.audit_recorded:
        return RowDecision(
            evidence.ledger_id, "already_closed", (), None, None, None, before, snapshot
        )
    accept_at = accept_instant(row) if row is not None else None
    deadline: datetime.datetime | None = None
    deadline_reason: str | None = None
    if row is not None and accept_at is not None:
        deadline, deadline_reason = inference_deadline(
            accept_at=accept_at,
            side="buy",
            session_bounds=evidence.session_bounds,
            broker_exchange=None,
            unsettled_regular_buy_downgrade=False,
        )
    ctx = _Context(
        evidence=evidence, row=row, accept_at=accept_at, deadline=deadline, now=now
    )
    failed = tuple(name for name, check in CONDITIONS if not check(ctx))
    if row is None:
        failed = ("row_missing", *failed)
    return RowDecision(
        evidence.ledger_id,
        "refused" if failed else "eligible",
        failed,
        accept_at,
        deadline,
        deadline_reason,
        before,
        snapshot,
    )


def decide_batch(
    ids: tuple[int, ...], decisions: tuple[RowDecision, ...]
) -> BatchStatus:
    """All already closed → noop; all eligible → eligible; anything else refused."""
    if set(ids) != ALLOWED_LEDGER_IDS or len(decisions) != len(ids):
        return "refused"
    if all(d.verdict == "already_closed" for d in decisions):
        return "noop"
    if all(d.verdict == "eligible" for d in decisions):
        return "eligible"
    return "refused"


def closed_detail(
    decision: RowDecision,
    *,
    decision_ref: str,
    reason: str,
    actor: str,
    batch_id: str,
    closed_at: datetime.datetime,
) -> dict[str, Any]:
    """The ``last_reconcile_detail`` written on a closed row."""
    return {
        "reason_code": INFERENCE_REASON_CODE,
        "expiry_basis": EXPIRY_BASIS,
        "expiry_caveat": INFERENCE_CAVEAT,
        "inference_rule": RULE_ID,
        "rule_version": RULE_VERSION,
        "operator_decision_ref": decision_ref,
        "waived_conditions": list(WAIVED_CONDITIONS),
        "accept_at": decision.accept_at.isoformat() if decision.accept_at else None,
        "inferred_expiry_after": (
            decision.deadline.isoformat() if decision.deadline else None
        ),
        "deadline_reason": decision.deadline_reason,
        "evidence_scope": "db_ledgers_and_xkrx_no_broker_read",
        "reason": reason,
        "actor": actor,
        "batch_id": batch_id,
        "closed_at": closed_at.isoformat(),
        "prior_reconcile_detail": decision.snapshot.get("last_reconcile_detail"),
    }

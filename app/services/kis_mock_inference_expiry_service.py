"""#1250 — I/O layer for the Q-46 kis_mock ``expired[inference]`` close.

Splits from ``kis_mock_inference_expiry`` (pure rule) the same way #1112 does:

* ``kis_mock_inference_expiry.classify_row`` — pure per-row decision.
* ``preview_inference_expiry`` / ``commit_inference_expiry`` — read facts,
  decide the batch, and (commit only) write.

Every fact is a committed DB row: ``review.kis_mock_order_ledger`` (the row,
same-symbol rows and same-correlation rows), ``review.execution_ledger``
(kis/mock rows, quarantined ones included — a quarantined row still refuses,
which is the safe direction) and the audit table, plus the XKRX session
calendar. Nothing here contacts a broker or reads a live ledger. The only
writes are ``KISMockLifecycleService.close_rows_by_q46_inference`` and the
append-only audit insert, both inside one transaction that first locks and
re-classifies the four rows.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.execution_ledger import ExecutionLedger
from app.models.review import KISMockInferenceExpiryEvent, KISMockOrderLedger
from app.services.kis_mock_inference_expiry import (
    ALLOWED_LEDGER_IDS,
    KST,
    MAX_ACTOR_CHARS,
    MAX_REASON_CHARS,
    RULE_VERSION,
    WAIVED_CONDITIONS,
    BatchStatus,
    ExecFillFacts,
    InferenceRecheckRefused,
    MockLedgerRowFacts,
    MockSiblingFacts,
    RowDecision,
    RowEvidence,
    accept_instant,
    classify_row,
    closed_detail,
    decide_batch,
    exact_ids,
    validate_decision_ref,
    validate_text,
)
from app.services.kis_mock_lifecycle_service import KISMockLifecycleService
from app.services.market_events.session_calendar import regular_session_bounds

__all__ = [
    "InferenceBatchResult",
    "InferenceConflictError",
    "commit_inference_expiry",
    "preview_inference_expiry",
    "verify_locked_batch",
]


class InferenceConflictError(RuntimeError):
    """The guarded write did not touch exactly the verified rows."""


def _utcnow() -> datetime.datetime:
    """The only clock. Not a parameter, so no caller can move the deadline."""
    return datetime.datetime.now(datetime.UTC)


@dataclass(frozen=True, slots=True)
class InferenceBatchResult:
    status: BatchStatus
    ids: tuple[int, ...]
    rows: tuple[RowDecision, ...]
    observed_at: datetime.datetime
    batch_id: uuid.UUID | None = None
    changed: int = 0

    @property
    def refused_ids(self) -> list[int]:
        return [row.ledger_id for row in self.rows if row.verdict == "refused"]

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "requested_ids": list(self.ids),
            "refused_ids": self.refused_ids,
            "waived_conditions": list(WAIVED_CONDITIONS),
            "rule_version": RULE_VERSION,
            "observed_at": self.observed_at.isoformat(),
            "rows": [row.as_dict() for row in self.rows],
            "batch_id": None if self.batch_id is None else str(self.batch_id),
            "changed": self.changed,
        }


# ------------------------------------------------------------------- collect


def _decimal(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _row_facts(row: KISMockOrderLedger) -> MockLedgerRowFacts:
    instrument = getattr(row.instrument_type, "value", row.instrument_type)
    return MockLedgerRowFacts(
        ledger_id=int(row.id),
        lifecycle_state=row.lifecycle_state,
        status=row.status,
        account_mode=row.account_mode,
        broker=row.broker,
        instrument_type=None if instrument is None else str(instrument),
        currency=row.currency,
        side=row.side,
        symbol=row.symbol,
        order_type=row.order_type,
        quantity=_decimal(row.quantity),
        price=_decimal(row.price),
        order_no=row.order_no,
        order_time=row.order_time,
        trade_date=row.trade_date,
        response_code=row.response_code,
        raw_response=row.raw_response if isinstance(row.raw_response, dict) else None,
        scalping_role=row.scalping_role,
        correlation_id=row.correlation_id,
        last_reconcile_detail=row.last_reconcile_detail,
        holdings_baseline_qty=_decimal(row.holdings_baseline_qty),
        reconciled_at=row.reconciled_at,
        strategy=row.strategy,
    )


def _sibling(row: KISMockOrderLedger) -> MockSiblingFacts:
    return MockSiblingFacts(
        ledger_id=int(row.id),
        symbol=row.symbol,
        correlation_id=row.correlation_id,
        lifecycle_state=row.lifecycle_state,
        last_reconcile_detail=row.last_reconcile_detail,
        trade_date=row.trade_date,
        reconciled_at=row.reconciled_at,
    )


def _exec_fill(row: ExecutionLedger) -> ExecFillFacts:
    return ExecFillFacts(
        ledger_id=int(row.id),
        broker_order_id=str(row.broker_order_id),
        symbol=str(row.symbol),
        filled_at=row.filled_at,
        source=str(row.source),
        quarantined=row.quarantined_at is not None,
    )


async def _mock_exec_rows_for_order(
    session: AsyncSession, order_no: str
) -> tuple[ExecFillFacts, ...]:
    """Every kis/mock execution-ledger row for this order, quarantined included."""
    normalized = order_no.strip().lstrip("0") or order_no.strip()
    rows = (
        (
            await session.execute(
                select(ExecutionLedger)
                .where(ExecutionLedger.broker == "kis")
                .where(ExecutionLedger.account_mode == "mock")
                .where(
                    or_(
                        ExecutionLedger.broker_order_id == order_no,
                        func.ltrim(ExecutionLedger.broker_order_id, "0") == normalized,
                    )
                )
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    return tuple(_exec_fill(row) for row in rows)


async def _mock_exec_rows_for_symbol(
    session: AsyncSession, symbol: str
) -> tuple[ExecFillFacts, ...]:
    """Every kis/mock execution-ledger row of the symbol, quarantined included."""
    rows = (
        (
            await session.execute(
                select(ExecutionLedger)
                .where(ExecutionLedger.broker == "kis")
                .where(ExecutionLedger.account_mode == "mock")
                .where(ExecutionLedger.symbol == symbol)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    return tuple(_exec_fill(row) for row in rows)


async def _mock_rows_where(
    session: AsyncSession, *, ledger_id: int, column: Any, value: Any
) -> tuple[MockSiblingFacts, ...]:
    rows = (
        (
            await session.execute(
                select(KISMockOrderLedger)
                .where(column == value)
                .where(KISMockOrderLedger.id != ledger_id)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    return tuple(_sibling(row) for row in rows)


async def _gather(
    session: AsyncSession,
    facts: MockLedgerRowFacts,
    *,
    audit_recorded: bool,
) -> RowEvidence:
    """Every fill/holding source for one row, read now (READ COMMITTED)."""
    ledger_id = facts.ledger_id
    accept_at = accept_instant(facts)
    bounds = (
        regular_session_bounds("kr", accept_at.astimezone(KST).date())
        if accept_at is not None
        else None
    )
    order_fills = (
        await _mock_exec_rows_for_order(session, facts.order_no)
        if isinstance(facts.order_no, str) and facts.order_no.strip()
        else None
    )
    symbol_fills = symbol_rows = None
    if isinstance(facts.symbol, str) and facts.symbol:
        symbol_fills = await _mock_exec_rows_for_symbol(session, facts.symbol)
        symbol_rows = await _mock_rows_where(
            session,
            ledger_id=ledger_id,
            column=KISMockOrderLedger.symbol,
            value=facts.symbol,
        )
    correlation_rows: tuple[MockSiblingFacts, ...] = ()
    if facts.correlation_id:
        correlation_rows = await _mock_rows_where(
            session,
            ledger_id=ledger_id,
            column=KISMockOrderLedger.correlation_id,
            value=facts.correlation_id,
        )
    return RowEvidence(
        ledger_id=ledger_id,
        row=facts,
        session_bounds=bounds,
        order_exec_fills=order_fills,
        symbol_exec_fills=symbol_fills,
        symbol_rows=symbol_rows,
        correlation_rows=correlation_rows,
        audit_recorded=audit_recorded,
    )


async def _load_evidence(
    session: AsyncSession, ids: Sequence[int], *, for_update: bool
) -> tuple[RowEvidence, ...]:
    ids = exact_ids(ids)
    stmt = (
        select(KISMockOrderLedger)
        .where(KISMockOrderLedger.id.in_(sorted(ids)))
        .order_by(KISMockOrderLedger.id)
        .execution_options(populate_existing=True)
    )
    if for_update:
        stmt = stmt.with_for_update()
    with session.no_autoflush:
        found = {int(r.id): r for r in (await session.execute(stmt)).scalars().all()}
    audited = set(
        (
            await session.execute(
                select(KISMockInferenceExpiryEvent.ledger_id).where(
                    KISMockInferenceExpiryEvent.ledger_id.in_(sorted(ids))
                )
            )
        )
        .scalars()
        .all()
    )
    out: list[RowEvidence] = []
    for ledger_id in ids:
        model = found.get(ledger_id)
        if model is None:
            out.append(RowEvidence(ledger_id, None, None, None, None, None, None))
            continue
        out.append(
            await _gather(
                session, _row_facts(model), audit_recorded=ledger_id in audited
            )
        )
    return tuple(out)


async def _decide(
    session: AsyncSession,
    ids: Sequence[int],
    *,
    for_update: bool,
    now: datetime.datetime,
) -> tuple[BatchStatus, tuple[RowEvidence, ...], tuple[RowDecision, ...]]:
    evidence = await _load_evidence(session, ids, for_update=for_update)
    decisions = tuple(classify_row(item, now=now) for item in evidence)
    return decide_batch(tuple(ids), decisions), evidence, decisions


async def verify_locked_batch(
    session: AsyncSession, ids: Sequence[int]
) -> tuple[RowDecision, ...]:
    """Lock the four rows and require the whole batch eligible right now.

    Used by the write chokepoint itself
    (``KISMockLifecycleService.close_rows_by_q46_inference``) so the writer
    can never close a row the rule refuses, whoever calls it.
    """
    status, _evidence, decisions = await _decide(
        session, ids, for_update=True, now=_utcnow()
    )
    if status != "eligible":
        raise InferenceRecheckRefused(decisions)
    return decisions


async def _recheck_after_update(
    session: AsyncSession,
    evidence: tuple[RowEvidence, ...],
    *,
    now: datetime.datetime,
) -> tuple[str, tuple[RowDecision, ...]]:
    """Re-read every fill source after the guarded UPDATEs, before commit.

    The kis_mock rows are locked, but a fill writer does not take those locks:
    a fill committed between the evidence read and the UPDATE would otherwise
    be ignored. Under READ COMMITTED each statement here sees every fill
    committed before it starts. A fill committed after these reads (before
    the audit insert or COMMIT) is caught by the deferred COMMIT-time trigger
    ``trg_kis_mock_inference_requires_audit``, which re-runs the order,
    symbol and sibling fill checks in SQL while COMMIT is processed.
    Classification reuses the PRE-update row facts (the rows are ``expired`` in
    this transaction by now).
    """
    fresh = tuple(
        [
            await _gather(session, item.row, audit_recorded=False)
            for item in evidence
            if item.row is not None
        ]
    )
    decisions = tuple(classify_row(item, now=now) for item in fresh)
    status = decide_batch(tuple(item.ledger_id for item in fresh), decisions)
    return status, decisions


# ---------------------------------------------------------------- operations


async def preview_inference_expiry(
    session: AsyncSession, ids: Sequence[int], *, decision_ref: str
) -> InferenceBatchResult:
    """Read-only verdicts for the four rows. Writes nothing; ends in rollback."""
    validate_decision_ref(decision_ref)
    ids = exact_ids(ids)
    now = _utcnow()
    try:
        status, _evidence, decisions = await _decide(
            session, ids, for_update=False, now=now
        )
    finally:
        await session.rollback()
    return InferenceBatchResult(status, tuple(ids), decisions, now)


async def commit_inference_expiry(
    session: AsyncSession,
    ids: Sequence[int],
    *,
    decision_ref: str,
    reason: str,
    actor: str,
) -> InferenceBatchResult:
    """Close exactly the four rows in one transaction, or change nothing.

    Order inside the one transaction: lock and classify the four rows; the
    write chokepoint re-verifies under the same locks, then runs the guarded
    per-row UPDATEs (exactly four); every fill source is re-read fresh and the
    batch re-classified on the pre-update facts; four audit rows are inserted
    (the DB refuses an audit row whose ledger row is not closed by the same
    batch, and refuses at COMMIT a closed row without its audit row); commit.
    A refused, no-op or failed batch rolls back without writing.
    """
    ref = validate_decision_ref(decision_ref)
    ids = exact_ids(ids)
    reason_text = validate_text("reason", reason, max_chars=MAX_REASON_CHARS)
    actor_text = validate_text("actor", actor, max_chars=MAX_ACTOR_CHARS)
    now = _utcnow()
    try:
        status, evidence, decisions = await _decide(
            session, ids, for_update=True, now=now
        )
        if status != "eligible":
            await session.rollback()
            return InferenceBatchResult(status, tuple(ids), decisions, now)

        batch_id = uuid.uuid4()
        details = {
            d.ledger_id: closed_detail(
                d,
                decision_ref=ref,
                reason=reason_text,
                actor=actor_text,
                batch_id=str(batch_id),
                closed_at=now,
            )
            for d in decisions
        }
        try:
            changed = await KISMockLifecycleService(
                session
            ).close_rows_by_q46_inference(details=details, closed_at=now)
        except InferenceRecheckRefused as refused:
            await session.rollback()
            return InferenceBatchResult("refused", tuple(ids), refused.decisions, now)
        if changed != len(ALLOWED_LEDGER_IDS):
            raise InferenceConflictError(
                f"guarded update touched {changed} rows, "
                f"expected {len(ALLOWED_LEDGER_IDS)}"
            )
        recheck_status, recheck = await _recheck_after_update(
            session, evidence, now=now
        )
        if recheck_status != "eligible":
            await session.rollback()
            return InferenceBatchResult("refused", tuple(ids), recheck, now)
        session.add_all(
            [
                KISMockInferenceExpiryEvent(
                    batch_id=batch_id,
                    ledger_id=d.ledger_id,
                    action="expire_inference",
                    operator_decision_ref=ref,
                    rule_version=RULE_VERSION,
                    reason=reason_text,
                    actor=actor_text,
                    before_state=str(d.before_state),
                    after_state="expired",
                    evidence={**d.as_dict(), "closed_detail": details[d.ledger_id]},
                )
                for d in decisions
            ]
        )
        await session.flush()
        try:
            await session.commit()
        except IntegrityError:
            # The COMMIT-time trigger (trg_kis_mock_inference_requires_audit)
            # refused the close — the last fill gate. Report it like any other
            # refusal when a fresh read confirms the batch is no longer
            # eligible; otherwise surface the error.
            await session.rollback()
            fresh_status, _fresh_evidence, fresh = await _decide(
                session, ids, for_update=False, now=now
            )
            await session.rollback()
            if fresh_status == "refused":
                return InferenceBatchResult("refused", tuple(ids), fresh, now)
            raise
    except Exception:
        await session.rollback()
        raise
    return InferenceBatchResult(
        "committed", tuple(ids), decisions, now, batch_id, changed
    )

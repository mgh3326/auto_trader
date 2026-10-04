"""KIS mock order lifecycle service (ROB-102).

Pure record-keeping. Must not import or call broker mutation services,
KIS live execution, watch alerts, order intents, scheduler, fill
notification, or trade journal code. All writes are atomic per call.

Lifecycle vocabulary follows ROB-100 (`app.schemas.execution_contracts`).
Fine-grained reasoning is stored in `last_reconcile_detail.reason_code`.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.execution_ledger import ExecutionLedger
from app.models.review import KISMockOrderLedger
from app.schemas.execution_contracts import (
    ORDER_LIFECYCLE_STATES,
    TERMINAL_LIFECYCLE_STATES,
    OrderLifecycleState,
)
from app.services.kis_mock_terminal_expiry import (
    RULE_VERSION,
    classify_row,
    validate_request,
)
from app.services.market_events.session_calendar import trading_session_status

# These are the lifecycle states the reconciler may read from. A ``fill`` with
# durable full-fill evidence is excluded separately in ``list_open_orders``:
# current holdings can later fall after an opposite-side close, so they cannot
# safely re-prove or invalidate that completed leg (ROB-1019).
OPEN_LIFECYCLE_STATES: frozenset[str] = frozenset({"accepted", "pending", "fill"})
_CONCLUDED_FILL_REASON_CODES: frozenset[str] = frozenset({"fill_detected"})


def _today_kst() -> date:
    return datetime.now(timezone(timedelta(hours=9))).date()


class LedgerNotFoundError(Exception):
    """Raised when a ledger row with the given id does not exist."""


class ExpiredLifecycleConflict(ValueError):
    """A fresh locked row is expired; generic reconciliation must skip it."""


class KISMockLifecycleService:
    """Pure record-keeping service for KIS mock order lifecycle."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def list_open_orders(
        self,
        *,
        limit: int = 100,
        symbol: str | None = None,
        instrument_type: str | None = None,
        side: str | None = None,
        ledger_ids: list[int] | None = None,
    ) -> list[KISMockOrderLedger]:
        if limit < 1:
            raise ValueError("limit must be >= 1")
        fill_reason = KISMockOrderLedger.last_reconcile_detail["reason_code"].astext
        stmt = select(KISMockOrderLedger).where(
            or_(
                KISMockOrderLedger.lifecycle_state.in_(
                    OPEN_LIFECYCLE_STATES - {"fill"}
                ),
                and_(
                    KISMockOrderLedger.lifecycle_state == "fill",
                    or_(
                        fill_reason.is_(None),
                        fill_reason.notin_(_CONCLUDED_FILL_REASON_CODES),
                    ),
                ),
            )
        )
        if symbol:
            stmt = stmt.where(KISMockOrderLedger.symbol == symbol)
        if instrument_type:
            stmt = stmt.where(KISMockOrderLedger.instrument_type == instrument_type)
        if side:
            stmt = stmt.where(KISMockOrderLedger.side == side)
        if ledger_ids:
            stmt = stmt.where(KISMockOrderLedger.id.in_(ledger_ids))
        stmt = stmt.order_by(
            KISMockOrderLedger.trade_date.asc(),
            KISMockOrderLedger.id.asc(),
        ).limit(limit)
        result = await self._db.execute(stmt)
        return list(result.scalars().all())

    async def existing_ledger_ids(self, ledger_ids: list[int]) -> set[int]:
        """Return the subset of ``ledger_ids`` that exist in the ledger at
        all (any lifecycle state) — used to explicitly reject a
        reconciliation scope naming a nonexistent id rather than silently
        processing whatever subset does exist (ROB-1007)."""
        if not ledger_ids:
            return set()
        stmt = select(KISMockOrderLedger.id).where(
            KISMockOrderLedger.id.in_(ledger_ids)
        )
        result = await self._db.execute(stmt)
        return set(result.scalars().all())

    async def get_by_order_no(self, *, order_no: str) -> KISMockOrderLedger | None:
        """Look up a single ledger row by broker order number.

        Used by cancel/modify so KIS mock never depends on the unsupported
        TTTC8036R pending-orders inquiry.
        """
        stmt = select(KISMockOrderLedger).where(KISMockOrderLedger.order_no == order_no)
        result = await self._db.execute(stmt)
        return result.scalar_one_or_none()

    async def _has_local_fill_row(self, row: KISMockOrderLedger) -> bool:
        """Check both durable local fill surfaces, conservatively by order id.

        The mock holdings reconciler writes fill state on the mock order ledger.
        The generic execution ledger may also contain a mock KIS websocket fill.
        An unrelated same-number row makes us refuse, which is the safe result.
        """
        order_no = row.order_no or ""
        normalized = order_no.lstrip("0") or "0"
        execution = (
            select(ExecutionLedger.id)
            .where(
                ExecutionLedger.broker == "kis",
                ExecutionLedger.account_mode == "mock",
                or_(
                    ExecutionLedger.broker_order_id == order_no,
                    func.ltrim(ExecutionLedger.broker_order_id, "0") == normalized,
                ),
            )
            .limit(1)
        )
        if (await self._db.execute(execution)).scalar_one_or_none() is not None:
            return True
        if not row.correlation_id:
            return False
        sibling = (
            select(KISMockOrderLedger.id)
            .where(
                KISMockOrderLedger.id != row.id,
                KISMockOrderLedger.correlation_id == row.correlation_id,
                KISMockOrderLedger.lifecycle_state.in_(("fill", "reconciled")),
            )
            .limit(1)
        )
        return (await self._db.execute(sibling)).scalar_one_or_none() is not None

    async def expire_legacy_day_orders(
        self,
        *,
        ledger_ids: list[int],
        operator_decision_ref: str,
        expected_strategy: str,
        min_sessions: int,
        dry_run: bool = True,
        confirm: bool = False,
    ) -> list[dict[str, Any]]:
        """Audited Q-46 expiry, with eligibility rechecked under the row lock.

        This method is the only new writer. It does no broker I/O. A second
        invocation on an expired row rolls back the read transaction without
        touching the row, audit detail, or attempt counter.
        """
        invalid = validate_request(ledger_ids, operator_decision_ref, expected_strategy)
        if invalid is not None:
            raise ValueError(invalid)
        if type(min_sessions) is not int or not 2 <= min_sessions <= 20:
            raise ValueError("min_sessions_invalid")
        if not dry_run and confirm is not True:
            raise ValueError("confirm_required")
        today = _today_kst()
        decision_ref = operator_decision_ref.strip()
        out: list[dict[str, Any]] = []
        for index, ledger_id in enumerate(ledger_ids):
            try:
                result = await self._expire_one_legacy_day_order(
                    ledger_id=ledger_id,
                    decision_ref=decision_ref,
                    expected_strategy=expected_strategy,
                    min_sessions=min_sessions,
                    today=today,
                    dry_run=dry_run,
                )
            except Exception:  # noqa: BLE001 - preserve earlier committed row results
                # A failed commit may have an uncertain outcome. Never claim the
                # current row stayed unchanged, and never erase earlier results.
                try:
                    await self._db.rollback()
                except Exception:  # noqa: BLE001 - connection may already be lost
                    pass
                out.append(
                    {
                        "ledger_id": ledger_id,
                        "before_status": None,
                        "after_status": None,
                        "decision": "error",
                        "reason_code": (
                            "row_review_unavailable"
                            if dry_run
                            else "write_outcome_unknown"
                        ),
                        "evidence": {"scope": "row_local_and_xkrx_no_broker_read"},
                        "rule_version": RULE_VERSION,
                        "operator_decision_ref": decision_ref,
                    }
                )
                for unprocessed_id in ledger_ids[index + 1 :]:
                    out.append(
                        {
                            "ledger_id": unprocessed_id,
                            "before_status": None,
                            "after_status": None,
                            "decision": "not_processed",
                            "reason_code": "prior_row_error",
                            "evidence": {"scope": "row_local_and_xkrx_no_broker_read"},
                            "rule_version": RULE_VERSION,
                            "operator_decision_ref": decision_ref,
                        }
                    )
                break
            else:
                out.append(result)
        return out

    async def _expire_one_legacy_day_order(
        self,
        *,
        ledger_id: int,
        decision_ref: str,
        expected_strategy: str,
        min_sessions: int,
        today: date,
        dry_run: bool,
    ) -> dict[str, Any]:
        """Classify one row and commit only after the locked eligibility check."""
        stmt = (
            select(KISMockOrderLedger)
            .where(KISMockOrderLedger.id == ledger_id)
            .execution_options(populate_existing=True)
        )
        if not dry_run:
            stmt = stmt.with_for_update()
        with self._db.no_autoflush:
            row = (await self._db.execute(stmt)).scalar_one_or_none()
        if row is None:
            await self._db.rollback()
            return {
                "ledger_id": ledger_id,
                "before_status": None,
                "after_status": None,
                "decision": "refused",
                "reason_code": "row_missing",
                "evidence": {"scope": "row_local_and_xkrx_no_broker_read"},
                "rule_version": RULE_VERSION,
                "operator_decision_ref": decision_ref,
            }
        before = row.lifecycle_state
        reason, evidence = classify_row(
            row,
            expected_strategy=expected_strategy,
            today=today,
            min_sessions=min_sessions,
            calendar=trading_session_status,
        )
        if reason == "eligible":
            if await self._has_local_fill_row(row):
                reason = "fill_row_present"
            else:
                evidence["no_local_fill_row"] = True
        decision = "refused"
        after = before
        if reason == "eligible":
            if dry_run:
                decision = "would_expire"
            else:
                # This row remains locked from the service's own read and
                # complete eligibility check. The generic transition API
                # must never be able to bypass this Q-46 classification.
                row.lifecycle_state = "expired"
                row.reconcile_attempts = (row.reconcile_attempts or 0) + 1
                row.last_reconcile_detail = {
                    "reason_code": "operator_legacy_day_expired",
                    "rule_version": RULE_VERSION,
                    "operator_decision_ref": decision_ref,
                    "evidence_scope": "row_local_and_xkrx_no_broker_read",
                    "age_sessions": evidence["age_sessions"],
                }
                row.reconciled_at = datetime.now(tz=UTC)
                await self._db.commit()
                decision = "expired"
                after = "expired"
        if decision != "expired":
            await self._db.rollback()
        return {
            "ledger_id": ledger_id,
            "before_status": before,
            "after_status": after,
            "decision": decision,
            "reason_code": reason,
            "evidence": evidence,
            "rule_version": RULE_VERSION,
            "operator_decision_ref": decision_ref,
        }

    async def close_rows_by_q46_inference(
        self,
        *,
        details: dict[int, dict[str, Any]],
        closed_at: datetime,
    ) -> int:
        """#1250 guarded write for the Q-46 ``expired[inference]`` close.

        Called only by ``kis_mock_inference_expiry_service.commit_inference_expiry``
        after it has locked and re-classified the rows in the same transaction.
        Each UPDATE is itself guarded on the id allowlist and on the row still
        being ``accepted``/``pending``; the caller compares the returned count
        with the batch size and owns the commit/rollback. Never commits.
        """
        from app.services.kis_mock_inference_expiry import ALLOWED_LEDGER_IDS

        if set(details) != ALLOWED_LEDGER_IDS:
            raise ValueError("q46_inference_ids_outside_allowlist")
        changed = 0
        for ledger_id, detail in sorted(details.items()):
            result = await self._db.execute(
                update(KISMockOrderLedger)
                .where(
                    KISMockOrderLedger.id == ledger_id,
                    KISMockOrderLedger.id.in_(sorted(ALLOWED_LEDGER_IDS)),
                    KISMockOrderLedger.account_mode == "kis_mock",
                    KISMockOrderLedger.lifecycle_state.in_(("accepted", "pending")),
                )
                .values(
                    lifecycle_state="expired",
                    reconcile_attempts=KISMockOrderLedger.reconcile_attempts + 1,
                    last_reconcile_detail=detail,
                    reconciled_at=closed_at,
                )
                .execution_options(synchronize_session=False)
            )
            changed += int(result.rowcount or 0)
        return changed

    async def update_order_terms(
        self,
        *,
        ledger_id: int,
        price: Decimal | None = None,
        quantity: Decimal | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        """Reflect a broker-confirmed modify on the ledger row."""
        row = await self._db.get(KISMockOrderLedger, ledger_id)
        if row is None:
            raise LedgerNotFoundError(str(ledger_id))
        if price is not None:
            row.price = price
        if quantity is not None:
            row.quantity = quantity
        if detail is not None:
            row.last_reconcile_detail = {
                **(row.last_reconcile_detail or {}),
                **detail,
            }
        await self._db.commit()

    async def record_holdings_baseline(
        self,
        *,
        ledger_id: int,
        baseline_qty: Decimal,
    ) -> None:
        row = await self._db.get(KISMockOrderLedger, ledger_id)
        if row is None:
            raise LedgerNotFoundError(str(ledger_id))
        row.holdings_baseline_qty = baseline_qty
        await self._db.commit()

    async def apply_lifecycle_transition(
        self,
        *,
        ledger_id: int,
        next_state: OrderLifecycleState,
        reason_code: str,
        detail: dict[str, Any],
        dry_run: bool,
    ) -> dict[str, Any]:
        if next_state not in ORDER_LIFECYCLE_STATES:
            raise ValueError(f"unknown lifecycle state: {next_state!r}")
        if next_state == "expired":
            raise ValueError("expired_requires_day_classification")

        stmt = (
            select(KISMockOrderLedger)
            .where(KISMockOrderLedger.id == ledger_id)
            .execution_options(populate_existing=True)
        )
        if not dry_run:
            stmt = stmt.with_for_update()
        # The identity map may still hold a pre-expiry pending object from the
        # holdings fetch. Suppress an autoflush of that stale object and load
        # the current database row before deciding whether a write is allowed.
        with self._db.no_autoflush:
            row = (await self._db.execute(stmt)).scalar_one_or_none()
        if row is None:
            raise LedgerNotFoundError(str(ledger_id))

        prior_state = row.lifecycle_state
        if prior_state == "expired":
            raise ExpiredLifecycleConflict("expired_terminal_immutable")
        would_change = prior_state != next_state

        if dry_run:
            return {
                "ledger_id": ledger_id,
                "prior_state": prior_state,
                "next_state": next_state,
                "reason_code": reason_code,
                "would_change": would_change,
                "applied": False,
                "dry_run": True,
            }

        if not would_change:
            row.reconcile_attempts = (row.reconcile_attempts or 0) + 1
            row.last_reconcile_detail = {"reason_code": reason_code, **detail}
            await self._db.commit()
            return {
                "ledger_id": ledger_id,
                "prior_state": prior_state,
                "next_state": next_state,
                "reason_code": reason_code,
                "applied": True,
                "would_change": False,
                "dry_run": False,
            }

        row.lifecycle_state = next_state
        row.reconcile_attempts = (row.reconcile_attempts or 0) + 1
        row.last_reconcile_detail = {"reason_code": reason_code, **detail}
        if next_state in TERMINAL_LIFECYCLE_STATES:
            row.reconciled_at = datetime.now(tz=UTC)
        await self._db.commit()

        return {
            "ledger_id": ledger_id,
            "prior_state": prior_state,
            "next_state": next_state,
            "reason_code": reason_code,
            "applied": True,
            "would_change": True,
            "dry_run": False,
        }


__all__ = [
    "KISMockLifecycleService",
    "LedgerNotFoundError",
    "ExpiredLifecycleConflict",
    "OPEN_LIFECYCLE_STATES",
]

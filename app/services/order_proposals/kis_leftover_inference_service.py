"""#1112 — I/O layer for the ``expired[inference]`` rule.

Splits from ``kis_leftover_inference`` (pure rule) so the conditions can be unit-
and mutation-tested without a database:

* ``kis_leftover_inference.classify_leftover_rung`` — pure decision.
* ``KisLeftoverInferenceService``                    — gathers facts, applies.

Every fact is a committed DB row: ``review.kis_live_order_ledger`` (send-time
order record), ``review.execution_ledger`` (fills, #963) and
``review.execution_ledger_reconcile_runs`` (coverage), plus the XKRX session
calendar. Nothing here contacts a broker, and nothing here can create, modify
or cancel an order: the only write is the rung transition performed by
``OrderProposalsService.expire_resting_rung_by_inference`` under the group row
lock, after the facts are re-read and re-classified under that lock.
"""

from __future__ import annotations

import datetime
import logging
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.execution_ledger import ExecutionLedger, ExecutionLedgerReconcileRun
from app.models.order_proposals import OrderProposal, OrderProposalRung
from app.models.review import KISLiveOrderLedger
from app.services.market_events.session_calendar import regular_session_bounds
from app.services.order_proposals.kis_leftover_inference import (
    KST,
    FillFacts,
    InferenceDecision,
    KisOrderLedgerFacts,
    LeftoverRungFacts,
    ReconcileRunFacts,
    classify_leftover_rung,
    resolve_accept_at,
)

logger = logging.getLogger(__name__)

__all__ = ["KisLeftoverInferenceService", "is_inference_candidate"]

_BROKER_EXCHANGE_KEYS = ("EXCG_ID_DVSN_CD", "excg_id_dvsn_cd", "exg_id_dvsn_cd")


def is_inference_candidate(group: OrderProposal, rung: OrderProposalRung) -> bool:
    """The only rung shape the rule is ever evaluated for."""
    return (
        group.account_mode == "kis_live"
        and group.market == "equity_kr"
        and rung.side == "buy"
        and rung.state == "resting"
    )


def _broker_exchange(raw_response: Any) -> str | None:
    """Broker-echoed venue, read factually from the stored send response."""
    if not isinstance(raw_response, dict):
        return None
    output = raw_response.get("output")
    for source in (raw_response, output if isinstance(output, dict) else {}):
        for key in _BROKER_EXCHANGE_KEYS:
            value = source.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
    return None


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


class KisLeftoverInferenceService:
    """Evidence gatherer + applier for the #1112 inference rule."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        unsettled_regular_buy_downgrade: bool = False,
    ) -> None:
        self._session = session
        self._downgrade = unsettled_regular_buy_downgrade

    # ------------------------------------------------------------- collect

    async def _owned_ledger_rows(
        self, group: OrderProposal, rung: OrderProposalRung
    ) -> tuple[tuple[KisOrderLedgerFacts, ...], str | None]:
        """Order-ledger rows unambiguously owned by this rung, plus any conflict.

        Same ownership rule ROB-1284 uses: a row matched on one key but owned
        by another rung (or ambiguous) is a conflict, never evidence.
        """
        from app.services.order_proposals.errors import OrderProposalError
        from app.services.order_proposals.service import OrderProposalsService

        keys: list[tuple[Any, str]] = []
        if rung.broker_order_id:
            keys.append((KISLiveOrderLedger.order_no, rung.broker_order_id))
        if rung.idempotency_key:
            keys.append((KISLiveOrderLedger.idempotency_key, rung.idempotency_key))
        if rung.correlation_id:
            keys.append((KISLiveOrderLedger.correlation_id, rung.correlation_id))
        seen: dict[int, KISLiveOrderLedger] = {}
        for column, value in keys:
            rows = (
                (
                    await self._session.execute(
                        select(KISLiveOrderLedger)
                        .where(column == value)
                        .execution_options(populate_existing=True)
                    )
                )
                .scalars()
                .all()
            )
            for row in rows:
                seen.setdefault(int(row.id), row)

        service = OrderProposalsService(self._session)
        owned: list[KisOrderLedgerFacts] = []
        conflict: str | None = None
        for row in seen.values():
            try:
                owner = await service.find_unambiguous_evidence_rung_id(
                    correlation_id=row.correlation_id,
                    broker_order_id=row.order_no,
                    idempotency_key=row.idempotency_key,
                    account_mode="kis_live",
                    symbol=group.symbol,
                    market=group.market,
                )
            except OrderProposalError as exc:
                conflict = conflict or (str(exc) or exc.__class__.__name__)
                continue
            if owner != rung.id:
                conflict = conflict or "order_ledger_row_owned_by_other_rung"
                continue
            owned.append(
                KisOrderLedgerFacts(
                    ledger_id=int(row.id),
                    order_no=row.order_no,
                    status=str(row.status),
                    order_type=row.order_type,
                    side=str(row.side),
                    symbol=str(row.symbol),
                    trade_date=row.trade_date,
                    order_time=row.order_time,
                    broker_exchange=_broker_exchange(row.raw_response),
                    filled_qty=_decimal(row.filled_qty),
                )
            )
        return tuple(owned), conflict

    async def _symbol_fills(self, symbol: str) -> tuple[FillFacts, ...]:
        rows = (
            (
                await self._session.execute(
                    select(ExecutionLedger)
                    .where(ExecutionLedger.broker == "kis")
                    .where(ExecutionLedger.account_mode == "live")
                    .where(ExecutionLedger.symbol == symbol)
                    .order_by(ExecutionLedger.filled_at.asc(), ExecutionLedger.id.asc())
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .all()
        )
        return tuple(
            FillFacts(
                broker_order_id=str(row.broker_order_id),
                side=str(row.side),
                quantity=Decimal(row.filled_qty),
                filled_at=row.filled_at,
                source=str(row.source),
            )
            for row in rows
        )

    async def _covering_runs(
        self, accept_at: datetime.datetime
    ) -> tuple[ReconcileRunFacts, ...]:
        """Successful committed KIS runs whose window starts at/before accept."""
        rows = (
            (
                await self._session.execute(
                    select(ExecutionLedgerReconcileRun)
                    .where(ExecutionLedgerReconcileRun.broker == "kis")
                    .where(ExecutionLedgerReconcileRun.dry_run.is_(False))
                    .where(ExecutionLedgerReconcileRun.error_summary.is_(None))
                    .where(ExecutionLedgerReconcileRun.finished_at.is_not(None))
                    .where(ExecutionLedgerReconcileRun.window_start <= accept_at)
                    .where(ExecutionLedgerReconcileRun.window_end >= accept_at)
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .all()
        )
        return tuple(
            ReconcileRunFacts(
                window_start=row.window_start,
                window_end=row.window_end,
                finished_at=row.finished_at,
            )
            for row in rows
            if row.finished_at is not None
        )

    async def load_facts(
        self, group: OrderProposal, rung: OrderProposalRung
    ) -> LeftoverRungFacts:
        """Read every fact for one rung. A failed read becomes ``None``."""
        order_rows: tuple[KisOrderLedgerFacts, ...] | None
        conflict: str | None
        try:
            order_rows, conflict = await self._owned_ledger_rows(group, rung)
        except Exception:  # noqa: BLE001 - an unreadable ledger is not evidence
            logger.warning(
                "#1112 order-ledger read failed rung_id=%s", rung.id, exc_info=True
            )
            order_rows, conflict = None, "order_ledger_read_failed"

        accept_at: datetime.datetime | None = None
        if order_rows is not None and len(order_rows) == 1:
            accept_at = resolve_accept_at(order_rows[0])

        session_bounds = None
        runs: tuple[ReconcileRunFacts, ...] | None = ()
        if accept_at is not None:
            session_bounds = regular_session_bounds(
                "kr", accept_at.astimezone(KST).date()
            )
            try:
                runs = await self._covering_runs(accept_at)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "#1112 reconcile-run read failed rung_id=%s",
                    rung.id,
                    exc_info=True,
                )
                runs = None

        fills: tuple[FillFacts, ...] | None
        try:
            fills = await self._symbol_fills(group.symbol)
        except Exception:  # noqa: BLE001
            logger.warning(
                "#1112 execution-ledger read failed rung_id=%s", rung.id, exc_info=True
            )
            fills = None

        return LeftoverRungFacts(
            proposal_id=str(group.proposal_id),
            rung_id=int(rung.id),
            rung_index=int(rung.rung_index),
            rung_state=str(rung.state),
            side=str(rung.side),
            symbol=str(group.symbol),
            market=str(group.market),
            account_mode=str(group.account_mode),
            group_order_type=group.order_type,
            broker_order_id=rung.broker_order_id,
            rung_filled_qty=_decimal(rung.filled_qty),
            order_ledger_rows=order_rows,
            order_ledger_conflict=conflict,
            session_bounds=session_bounds,
            symbol_fills=fills,
            reconcile_runs=runs,
            unsettled_regular_buy_downgrade=self._downgrade,
        )

    async def evaluate(
        self,
        group: OrderProposal,
        rung: OrderProposalRung,
        *,
        now: datetime.datetime,
    ) -> InferenceDecision:
        return classify_leftover_rung(await self.load_facts(group, rung), now=now)

    async def _candidates(self) -> list[tuple[OrderProposal, OrderProposalRung]]:
        from app.services.order_proposals.service import OrderProposalsService

        pairs = await OrderProposalsService(
            self._session
        ).list_evidence_accepting_rungs()
        return [(g, r) for g, r in pairs if is_inference_candidate(g, r)]

    # ---------------------------------------------------------------- plan

    async def plan(self, *, now: datetime.datetime) -> list[InferenceDecision]:
        """Read-only classification of every candidate rung. Never mutates."""
        return [
            await self.evaluate(group, rung, now=now)
            for group, rung in await self._candidates()
        ]

    # --------------------------------------------------------------- apply

    async def apply(self, *, now: datetime.datetime) -> dict[str, Any]:
        """Close every rung that is STILL eligible under its group row lock.

        Idempotent: a closed rung is no longer ``resting`` and is not a
        candidate on the next run. Each rung runs in its own savepoint; every
        fact read uses ``populate_existing`` so the locked re-check never sees
        a stale identity-map row. The caller owns the commit.
        """
        from app.services.order_proposals.service import OrderProposalsService

        service = OrderProposalsService(self._session)
        decisions = await self.plan(now=now)
        applied: list[dict[str, object]] = []
        failed = 0
        for decision in decisions:
            if not decision.eligible:
                continue
            facts = decision.facts

            async def _still_eligible(
                group: OrderProposal, rung: OrderProposalRung
            ) -> bool:
                if not is_inference_candidate(group, rung):
                    return False
                fresh = await self.evaluate(group, rung, now=now)
                return fresh.eligible

            try:
                # One savepoint per rung: a failure rolls back only this rung
                # and cannot abort the transaction holding earlier closures.
                async with self._session.begin_nested():
                    rung = await service.expire_resting_rung_by_inference(
                        uuid.UUID(facts.proposal_id),
                        facts.rung_index,
                        now=now,
                        still_eligible=_still_eligible,
                    )
            except Exception as exc:  # noqa: BLE001 - surface, never swallow
                logger.error(
                    "#1112 inference transition failed rung_id=%s: %s",
                    facts.rung_id,
                    exc,
                )
                failed += 1
                continue
            if rung is not None:
                applied.append(decision.as_row())
        return {
            "candidates": len(decisions),
            "eligible": sum(1 for d in decisions if d.eligible),
            "applied": len(applied),
            "failed": failed,
            "applied_rows": applied,
            "blocked_rows": [d.as_row() for d in decisions if not d.eligible],
        }

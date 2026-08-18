"""ROB-1284 — I/O layer for the phantom-resting rung convergence sweep.

Splits cleanly from ``resting_sweep`` (pure classification) so the safety rules
can be unit- and mutation-tested without a database:

* ``resting_sweep.classify_rung``   — pure decision, no I/O, injected clock.
* ``RestingRungSweepService``       — gathers candidates + evidence, applies.

Nothing here contacts a broker.  Evidence is read from the already-committed
``*_order_ledger`` tables, which the ROB-395/407 reconcile kernel populates from
broker responses only.

``dry_run`` is the default on every entry point, and ``dry_run=False`` alone is
still not enough: ``apply`` additionally requires ``confirm=True``.
"""

from __future__ import annotations

import datetime
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.review import (
    KISLiveOrderLedger,
    LiveOrderLedger,
    TossLiveOrderLedger,
)
from app.services.order_proposals.resting_sweep import (
    LedgerEvidence,
    RungCandidate,
    RungVerdict,
    SweepDecision,
    classify_rung,
    summarize,
)

logger = logging.getLogger(__name__)

__all__ = ["RestingRungSweepService", "SweepNotConfirmed"]


class SweepNotConfirmed(RuntimeError):
    """Raised when a write was requested without the explicit operator confirm."""


# (model, table label, broker-order-id column, client-key column or None)
_LEDGER_SOURCES: tuple[tuple[Any, str, Any, Any], ...] = (
    (
        KISLiveOrderLedger,
        "review.kis_live_order_ledger",
        KISLiveOrderLedger.order_no,
        KISLiveOrderLedger.idempotency_key,
    ),
    (
        TossLiveOrderLedger,
        "review.toss_live_order_ledger",
        TossLiveOrderLedger.broker_order_id,
        TossLiveOrderLedger.client_order_id,
    ),
    (
        LiveOrderLedger,
        "review.live_order_ledger",
        LiveOrderLedger.order_no,
        LiveOrderLedger.idempotency_key,
    ),
)


class RestingRungSweepService:
    """Rung-driven, untruncated, evidence-first convergence sweep."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ---------------------------------------------------------------- collect

    async def collect_candidates(self) -> list[RungCandidate]:
        """Every rung in a broker-live state. No limit, no time window."""
        from app.services.order_proposals.service import OrderProposalsService

        pairs = await OrderProposalsService(
            self._session
        ).list_evidence_accepting_rungs()
        return [
            RungCandidate(
                proposal_id=str(group.proposal_id),
                rung_id=rung.id,
                rung_index=rung.rung_index,
                state=rung.state,
                side=rung.side,
                symbol=group.symbol,
                market=group.market,
                account_mode=group.account_mode,
                broker_order_id=rung.broker_order_id,
                idempotency_key=rung.idempotency_key,
                correlation_id=rung.correlation_id,
                quantity=rung.quantity,
                limit_price=rung.limit_price,
                updated_at=rung.updated_at,
            )
            for group, rung in pairs
        ]

    async def fetch_evidence(
        self, candidate: RungCandidate
    ) -> tuple[LedgerEvidence, ...]:
        """Collect every committed ledger row matching this rung's keys.

        All matches across all three ledgers are returned -- deliberately not
        "the first one".  Disagreement between two rows is a CONFLICT the
        operator must see, and a first-match-wins read would hide it.
        """
        found: list[LedgerEvidence] = []
        for model, label, order_col, client_col in _LEDGER_SOURCES:
            keys: list[tuple[Any, str, str]] = []
            if candidate.broker_order_id:
                keys.append((order_col, candidate.broker_order_id, "broker_order_id"))
            if candidate.idempotency_key and client_col is not None:
                keys.append((client_col, candidate.idempotency_key, "idempotency_key"))
            if candidate.correlation_id:
                keys.append(
                    (
                        model.correlation_id,
                        candidate.correlation_id,
                        "correlation_id",
                    )
                )
            for column, value, match_key in keys:
                rows = (
                    (await self._session.execute(select(model).where(column == value)))
                    .scalars()
                    .all()
                )
                for row in rows:
                    if any(
                        e.ledger_table == label and e.ledger_id == row.id for e in found
                    ):
                        continue
                    found.append(
                        LedgerEvidence(
                            ledger_table=label,
                            ledger_id=row.id,
                            status=row.status,
                            match_key=match_key,
                            broker_order_id=str(getattr(row, order_col.key, None) or "")
                            or None,
                            idempotency_key=(
                                getattr(row, client_col.key, None)
                                if client_col is not None
                                else None
                            ),
                            correlation_id=getattr(row, "correlation_id", None),
                            filled_qty=getattr(row, "filled_qty", None),
                            reconciled_at=getattr(row, "reconciled_at", None),
                            updated_at=getattr(row, "updated_at", None),
                        )
                    )
        return tuple(found)

    async def verify_ownership(
        self, candidate: RungCandidate, evidence: tuple[LedgerEvidence, ...]
    ) -> tuple[tuple[LedgerEvidence, ...], str | None]:
        """Keep only ledger rows that unambiguously belong to THIS rung.

        Matching on any single key is not enough. A ledger row can carry an
        ``order_no`` belonging to one rung and a ``correlation_id`` belonging to
        another (the ROB-900 conflict case); reading it as evidence for either
        one silently re-attributes a fill across proposals.

        So ownership is delegated to the existing, proven disambiguator —
        ``find_unambiguous_evidence_rung_id`` — which requires every *nonempty*
        resolving key set to intersect on a single rung. Anything it refuses to
        resolve, or resolves to a different rung, is dropped from the evidence
        set and reported as a conflict. It is never treated as evidence here.
        """
        from app.services.order_proposals.errors import OrderProposalError
        from app.services.order_proposals.service import OrderProposalsService

        if not evidence:
            return (), None
        service = OrderProposalsService(self._session)
        owned: list[LedgerEvidence] = []
        conflict: str | None = None
        for row in evidence:
            try:
                rung_id = await service.find_unambiguous_evidence_rung_id(
                    correlation_id=row.correlation_id,
                    broker_order_id=row.broker_order_id,
                    idempotency_key=row.idempotency_key,
                    account_mode=candidate.account_mode,
                    symbol=candidate.symbol,
                    market=candidate.market,
                )
            except OrderProposalError as exc:
                # e.g. proposal_evidence_conflict / _ambiguous / broker_id_duplicate
                conflict = conflict or (str(exc) or exc.__class__.__name__)
                continue
            if rung_id == candidate.rung_id:
                owned.append(row)
            elif rung_id is not None:
                conflict = conflict or "evidence_owned_by_other_rung"
        return tuple(owned), conflict

    # ------------------------------------------------------------------ plan

    async def plan(self, *, now: datetime.datetime) -> list[SweepDecision]:
        """Read-only classification of every candidate. Never mutates."""
        decisions: list[SweepDecision] = []
        for candidate in await self.collect_candidates():
            try:
                evidence = await self.fetch_evidence(candidate)
            except Exception as exc:  # noqa: BLE001 - a read failure is not evidence
                # Fail-closed and LOUD: an unreadable ledger must never be
                # mistaken for "no terminal evidence, leave it resting" in a
                # way the operator cannot see.
                logger.error(
                    "ROB-1284 evidence read failed rung_id=%s proposal_id=%s: %s",
                    candidate.rung_id,
                    candidate.proposal_id,
                    exc,
                )
                decisions.append(
                    SweepDecision(
                        candidate=candidate,
                        verdict=RungVerdict.CONFLICT,
                        reason_code="evidence_read_failed",
                        observed_at=now,
                    )
                )
                continue
            evidence, conflict = await self.verify_ownership(candidate, evidence)
            if conflict is not None and not evidence:
                # Evidence exists but cannot be attributed to this rung. That is
                # a conflict for a human, never a transition.
                decisions.append(
                    SweepDecision(
                        candidate=candidate,
                        verdict=RungVerdict.CONFLICT,
                        reason_code=conflict,
                        observed_at=now,
                    )
                )
                continue
            decisions.append(classify_rung(candidate, evidence, observed_at=now))
        return decisions

    async def report(self, *, now: datetime.datetime) -> dict[str, Any]:
        """Full dry-run payload: summary + every row's classification basis."""
        decisions = await self.plan(now=now)
        return {
            "dry_run": True,
            "truncated": False,
            "summary": summarize(decisions),
            "rows": [d.as_row() for d in decisions],
        }

    # ----------------------------------------------------------------- apply

    async def apply(
        self,
        *,
        now: datetime.datetime,
        dry_run: bool = True,
        confirm: bool = False,
    ) -> dict[str, Any]:
        """Transition only the ``TRANSITION`` rungs. Double-gated.

        ``dry_run=True`` (the default) plans and returns without writing.
        ``dry_run=False`` additionally requires ``confirm=True``; a caller that
        merely flips ``dry_run`` gets ``SweepNotConfirmed``, not a write.
        """
        decisions = await self.plan(now=now)
        payload: dict[str, Any] = {
            "dry_run": dry_run,
            "truncated": False,
            "summary": summarize(decisions),
            "rows": [d.as_row() for d in decisions],
            "applied": 0,
            "failed": 0,
        }
        if dry_run:
            return payload
        if not confirm:
            raise SweepNotConfirmed(
                "ROB-1284 sweep write requires confirm=True in addition to "
                "dry_run=False"
            )

        from app.services.order_proposals.service import OrderProposalsService

        service = OrderProposalsService(self._session)
        for decision in decisions:
            if decision.verdict is not RungVerdict.TRANSITION:
                continue
            candidate = decision.candidate
            try:
                rung = await service.record_fill_evidence_for_rung(
                    rung_id=candidate.rung_id,
                    correlation_id=candidate.correlation_id,
                    broker_order_id=candidate.broker_order_id,
                    idempotency_key=candidate.idempotency_key,
                    # cancelled/expired carry no fill quantity, preserving a
                    # partial booked before the terminal evidence arrived.
                    filled_qty=None
                    if decision.target_state in {"cancelled", "expired"}
                    else next(
                        (
                            e.filled_qty
                            for e in decision.evidence
                            if e.filled_qty is not None
                        ),
                        None,
                    ),
                    terminal_state=decision.target_state,  # type: ignore[arg-type]
                    now=now,
                    account_mode=candidate.account_mode,
                    symbol=candidate.symbol,
                    market=candidate.market,
                )
            except Exception as exc:  # noqa: BLE001 - surface, never swallow
                logger.error(
                    "ROB-1284 sweep transition failed rung_id=%s proposal_id=%s "
                    "target=%s: %s",
                    candidate.rung_id,
                    candidate.proposal_id,
                    decision.target_state,
                    exc,
                )
                payload["failed"] += 1
                continue
            if rung is not None:
                payload["applied"] += 1
        return payload

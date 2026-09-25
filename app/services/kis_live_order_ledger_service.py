"""Terminal KIS ledger candidates for proposal projection repair."""

from __future__ import annotations

from typing import Any

from sqlalchemy import and_, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.order_proposals import OrderProposal, OrderProposalRung
from app.models.review import KISLiveOrderLedger

_PROPOSAL_EVIDENCE_ACCEPTING_STATES = (
    "acked",
    "resting",
    "partially_filled",
    "unverified",
)

# ROB-719 gap D — the pre-pass used to apply ``LIMIT`` to the joined scan
# before the per-row eligibility check, so a dense prefix of ineligible rows
# (rungs already terminal, or evidence-key anomalies) could occupy every slot
# on every pass and starve real candidates behind them.  The scan now filters
# on accepting rung state in SQL (a row whose only matching rungs are terminal
# can never be a candidate — ``_terminal_projection_match`` would reject it
# anyway) and keyset-pages by ledger id until ``limit`` candidates are found
# or ``_TERMINAL_SCAN_ROW_CAP_FACTOR * limit`` joined rows were examined.
# The bound keeps one pass finite; the report's ``scan`` block states how far
# it got.
_TERMINAL_SCAN_ROW_CAP_FACTOR = 10


class KISLiveOrderLedgerService:
    """Read-only KIS terminal candidate lookup, symmetric with Toss repair."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def _terminal_candidate_page(
        self,
        *,
        symbol: str | None,
        order_id: str | None,
        after_id: int,
        limit: int,
    ) -> list[KISLiveOrderLedger]:
        """One keyset page of terminal ledger rows joined to accepting rungs.

        ``_terminal_projection_match`` still re-verifies each row — the SQL
        rung-state predicate is only a pre-filter so ``LIMIT`` applies after
        the cheap eligibility criterion, never before it.
        """
        evidence_match = or_(
            and_(
                KISLiveOrderLedger.correlation_id.is_not(None),
                KISLiveOrderLedger.correlation_id == OrderProposalRung.correlation_id,
            ),
            and_(
                KISLiveOrderLedger.order_no.is_not(None),
                KISLiveOrderLedger.order_no == OrderProposalRung.broker_order_id,
            ),
        )
        stmt = (
            select(KISLiveOrderLedger)
            .join(OrderProposalRung, evidence_match)
            .join(OrderProposal, OrderProposalRung.proposal_pk == OrderProposal.id)
            .where(
                KISLiveOrderLedger.status.in_(
                    ("filled", "cancelled", "expired", "rejected")
                ),
                OrderProposal.account_mode == "kis_live",
                OrderProposal.symbol == KISLiveOrderLedger.symbol,
                OrderProposal.market == KISLiveOrderLedger.instrument_type,
                OrderProposalRung.state.in_(_PROPOSAL_EVIDENCE_ACCEPTING_STATES),
                KISLiveOrderLedger.id > after_id,
            )
        )
        if symbol:
            stmt = stmt.where(KISLiveOrderLedger.symbol == symbol)
        if order_id:
            stmt = stmt.where(KISLiveOrderLedger.order_no == order_id)
        stmt = stmt.order_by(KISLiveOrderLedger.id.asc()).limit(limit)
        return list((await self._db.execute(stmt)).unique().scalars().all())

    async def list_terminal_projection_candidates(
        self,
        *,
        symbol: str | None = None,
        order_id: str | None = None,
        limit: int = 100,
    ) -> tuple[list[KISLiveOrderLedger], dict[str, int], dict[str, Any]]:
        candidates: list[KISLiveOrderLedger] = []
        anomalies: dict[str, int] = {}
        cursor = 0
        scanned = 0
        exhausted = False
        scan_cap = limit * _TERMINAL_SCAN_ROW_CAP_FACTOR
        while len(candidates) < limit and scanned < scan_cap:
            page = await self._terminal_candidate_page(
                symbol=symbol,
                order_id=order_id,
                after_id=cursor,
                limit=limit,
            )
            if not page:
                exhausted = True
                break
            # ``page`` is unique()d ledger rows while the SQL LIMIT counts join
            # rows, so a short page does not prove exhaustion — only an empty
            # page does.  The cursor is id-keyset (the last unique id is the
            # max id seen), so continuing never re-examines a row.
            scanned += len(page)
            cursor = page[-1].id
            for row in page:
                if len(candidates) >= limit:
                    break
                accepted, reason = await self._terminal_projection_match(row)
                if accepted:
                    candidates.append(row)
                elif reason is not None:
                    anomalies[reason] = anomalies.get(reason, 0) + 1
        scan = {
            "scanned": scanned,
            "exhausted": exhausted,
            "scan_cap": scan_cap,
            "cap_reached": scanned >= scan_cap,
            "scan_order": "ledger id ASC keyset pages",
        }
        if not exhausted and scanned >= scan_cap:
            scan["note"] = (
                "scan_cap reached before exhausting the terminal scan — "
                "unreached rows remain. The cursor restarts at the first row "
                "every pass, so a prefix of unprojectable rows beyond the cap "
                "starves candidates on every pass; resolve the anomalies or "
                "raise limit"
            )
        for row in candidates:
            self._db.expunge(row)
        return candidates, anomalies, scan

    async def _terminal_projection_match(
        self, row: KISLiveOrderLedger
    ) -> tuple[bool, str | None]:
        broker_match = (
            OrderProposalRung.broker_order_id == row.order_no
            if row.order_no is not None
            else literal(False)
        )
        correlation_match = (
            OrderProposalRung.correlation_id == row.correlation_id
            if row.correlation_id is not None
            else literal(False)
        )
        idempotency_match = (
            OrderProposalRung.idempotency_key == row.idempotency_key
            if row.idempotency_key is not None
            else literal(False)
        )
        if (
            row.order_no is None
            and row.correlation_id is None
            and row.idempotency_key is None
        ):
            return False, None
        stmt = (
            select(
                OrderProposalRung.id,
                OrderProposalRung.state,
                broker_match.label("broker_match"),
                correlation_match.label("correlation_match"),
                idempotency_match.label("idempotency_match"),
            )
            .join(OrderProposal, OrderProposalRung.proposal_pk == OrderProposal.id)
            .where(
                or_(broker_match, correlation_match, idempotency_match),
                OrderProposal.account_mode == "kis_live",
                OrderProposal.symbol == row.symbol,
                OrderProposal.market == row.instrument_type,
            )
        )
        matches = list((await self._db.execute(stmt)).all())
        broker_ids = {match.id for match in matches if match.broker_match}
        correlation_ids = {match.id for match in matches if match.correlation_match}
        idempotency_ids = {match.id for match in matches if match.idempotency_match}
        evidence_sets = [
            ids for ids in (broker_ids, correlation_ids, idempotency_ids) if ids
        ]
        if not evidence_sets:
            return False, None
        intersection = set.intersection(*evidence_sets)
        if not intersection:
            return False, "proposal_evidence_conflict"
        if len(broker_ids) > 1:
            return False, "broker_id_duplicate"
        if not broker_ids and not idempotency_ids and len(correlation_ids) > 1:
            return False, "content_hash_only_ambiguous"
        if len(intersection) > 1:
            return False, "proposal_evidence_ambiguous"
        rung_id = next(iter(intersection))
        return next(match.state for match in matches if match.id == rung_id) in (
            _PROPOSAL_EVIDENCE_ACCEPTING_STATES
        ), None

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.trading import InstrumentType
from app.models.trading_decision import ProposalKind
from app.schemas.research_run_decision_session import (
    LiveRefreshSnapshot,
    ResearchRunDecisionSessionRequest,
    ResearchRunSelector,
)
from app.services import (
    nxt_classifier_service,
    pending_reconciliation_service,
    research_run_service,
    trading_decision_service,
)

if TYPE_CHECKING:
    from app.models.research_run import (
        ResearchRun,
        ResearchRunCandidate,
        ResearchRunPendingReconciliation,
    )
    from app.models.trading_decision import TradingDecisionSession


@dataclass(frozen=True)
class ResearchRunDecisionSessionResult:
    session: TradingDecisionSession
    research_run: ResearchRun
    refreshed_at: datetime
    proposal_count: int
    reconciliation_count: int
    warnings: tuple[str, ...]


class ResearchRunNotFound(Exception): ...


class EmptyResearchRunError(Exception): ...


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def _proposal_kind_from_candidate(candidate: ResearchRunCandidate) -> ProposalKind:
    payload_kind = candidate.payload.get("proposal_kind")
    if isinstance(payload_kind, str) and payload_kind in {
        kind.value for kind in ProposalKind
    }:
        return ProposalKind(payload_kind)

    kind_map = {
        "holding": ProposalKind.no_action,
        "pending_order": ProposalKind.other,
        "screener_hit": ProposalKind.other,
        "proposed": ProposalKind.other,
        "other": ProposalKind.other,
    }
    return kind_map.get(candidate.candidate_kind, ProposalKind.other)


def _proposal_side(candidate: ResearchRunCandidate) -> str:
    if candidate.side in {"buy", "sell", "none"}:
        return candidate.side
    return "none"


def _lookup_existing_reconciliation(
    candidate: ResearchRunCandidate,
    reconciliations_by_order_id: dict[str, ResearchRunPendingReconciliation],
) -> ResearchRunPendingReconciliation | None:
    if candidate.candidate_kind != "pending_order":
        return None
    order_id = candidate.payload.get("order_id")
    if not isinstance(order_id, str) or not order_id:
        return None
    return reconciliations_by_order_id.get(order_id)


def _pending_orders_by_id(
    snapshot: LiveRefreshSnapshot,
) -> dict[str, Any]:
    return {order.order_id: order for order in snapshot.pending_orders}


def _reconciliations_by_order_id(
    research_run: ResearchRun,
) -> dict[str, ResearchRunPendingReconciliation]:
    return {
        recon.order_id: recon
        for recon in research_run.reconciliations
        if recon.order_id
    }


def _build_market_context(
    snapshot: LiveRefreshSnapshot,
    *,
    symbol: str,
) -> pending_reconciliation_service.MarketContextInput:
    quote = snapshot.quote_by_symbol.get(symbol)
    orderbook = snapshot.orderbook_by_symbol.get(symbol)
    support_resistance = snapshot.support_resistance_by_symbol.get(symbol)
    kr_universe = snapshot.kr_universe_by_symbol.get(symbol)

    return pending_reconciliation_service.MarketContextInput(
        quote=(
            pending_reconciliation_service.QuoteContext(
                price=quote.price,
                as_of=quote.as_of,
            )
            if quote is not None
            else None
        ),
        orderbook=(
            pending_reconciliation_service.OrderbookContext(
                best_bid=(
                    pending_reconciliation_service.OrderbookLevelContext(
                        price=orderbook.best_bid.price,
                        quantity=orderbook.best_bid.quantity,
                    )
                    if orderbook is not None and orderbook.best_bid is not None
                    else None
                ),
                best_ask=(
                    pending_reconciliation_service.OrderbookLevelContext(
                        price=orderbook.best_ask.price,
                        quantity=orderbook.best_ask.quantity,
                    )
                    if orderbook is not None and orderbook.best_ask is not None
                    else None
                ),
                total_bid_qty=orderbook.total_bid_qty,
                total_ask_qty=orderbook.total_ask_qty,
            )
            if orderbook is not None
            else None
        ),
        support_resistance=(
            pending_reconciliation_service.SupportResistanceContext(
                nearest_support=(
                    pending_reconciliation_service.SupportResistanceLevel(
                        price=support_resistance.nearest_support.price,
                        distance_pct=support_resistance.nearest_support.distance_pct,
                    )
                    if support_resistance is not None
                    and support_resistance.nearest_support is not None
                    else None
                ),
                nearest_resistance=(
                    pending_reconciliation_service.SupportResistanceLevel(
                        price=support_resistance.nearest_resistance.price,
                        distance_pct=support_resistance.nearest_resistance.distance_pct,
                    )
                    if support_resistance is not None
                    and support_resistance.nearest_resistance is not None
                    else None
                ),
            )
            if support_resistance is not None
            else None
        ),
        kr_universe=(
            pending_reconciliation_service.KrUniverseContext(
                nxt_eligible=kr_universe.nxt_eligible,
                name=kr_universe.name,
                exchange=kr_universe.exchange,
            )
            if kr_universe is not None
            else None
        ),
    )


def _build_pending_order_input(
    candidate: ResearchRunCandidate,
    *,
    live_pending_order: Any | None,
    existing_reconciliation: ResearchRunPendingReconciliation | None,
) -> pending_reconciliation_service.PendingOrderInput:
    order_id = candidate.payload.get("order_id")
    if not isinstance(order_id, str) or not order_id:
        order_id = (
            existing_reconciliation.order_id
            if existing_reconciliation is not None
            else f"candidate-{candidate.id}"
        )

    live_side = getattr(live_pending_order, "side", None)
    recon_side = (
        existing_reconciliation.side if existing_reconciliation is not None else None
    )
    candidate_side = candidate.side if candidate.side in {"buy", "sell"} else None
    side = live_side or recon_side or candidate_side or "buy"

    live_market = getattr(live_pending_order, "market", None)
    recon_market = (
        existing_reconciliation.market if existing_reconciliation is not None else None
    )
    market = live_market or recon_market or candidate.payload.get("market") or "kr"

    ordered_price = (
        getattr(live_pending_order, "ordered_price", None)
        or candidate.proposed_price
        or Decimal("0")
    )
    ordered_qty = (
        getattr(live_pending_order, "ordered_qty", None)
        or candidate.proposed_qty
        or Decimal("1")
    )
    remaining_qty = getattr(live_pending_order, "remaining_qty", None) or ordered_qty
    currency = getattr(live_pending_order, "currency", None) or candidate.currency
    ordered_at = getattr(live_pending_order, "ordered_at", None)

    return pending_reconciliation_service.PendingOrderInput(
        order_id=order_id,
        symbol=candidate.symbol,
        market=str(market),
        side=str(side),
        ordered_price=ordered_price,
        ordered_qty=ordered_qty,
        remaining_qty=remaining_qty,
        currency=currency,
        ordered_at=ordered_at,
    )


def _build_nxt_item(
    candidate: ResearchRunCandidate,
    *,
    context: pending_reconciliation_service.MarketContextInput,
    pending_order: pending_reconciliation_service.PendingOrderInput | None,
    holdings_qty: Decimal | None,
    as_of: datetime,
) -> nxt_classifier_service.NxtClassifierItem | None:
    if candidate.instrument_type != InstrumentType.equity_kr:
        return None

    if candidate.candidate_kind == "pending_order" and pending_order is not None:
        return nxt_classifier_service.classify_nxt_pending_order(
            pending_order,
            context,
            now=as_of,
        )

    if candidate.candidate_kind == "holding":
        return nxt_classifier_service.classify_nxt_holding(
            nxt_classifier_service.NxtHoldingInput(
                holding_id=str(candidate.id),
                symbol=candidate.symbol,
                quantity=holdings_qty or candidate.proposed_qty or Decimal("0"),
                currency=candidate.currency,
            ),
            context,
        )

    if candidate.proposed_price is not None and candidate.side in {"buy", "sell"}:
        return nxt_classifier_service.classify_nxt_candidate(
            nxt_classifier_service.NxtCandidateInput(
                candidate_id=str(candidate.id),
                symbol=candidate.symbol,
                side=candidate.side,
                proposed_price=candidate.proposed_price,
                proposed_qty=candidate.proposed_qty,
                currency=candidate.currency,
            ),
            context,
            now=as_of,
        )

    return None


def _proposal_payload(
    *,
    candidate: ResearchRunCandidate,
    research_run: ResearchRun,
    snapshot: LiveRefreshSnapshot,
    existing_reconciliation: ResearchRunPendingReconciliation | None,
    live_reconciliation: pending_reconciliation_service.PendingReconciliationItem
    | None,
    nxt_item: nxt_classifier_service.NxtClassifierItem | None,
    pending_order: pending_reconciliation_service.PendingOrderInput | None,
    context: pending_reconciliation_service.MarketContextInput,
) -> dict[str, Any]:
    live_quote = snapshot.quote_by_symbol.get(candidate.symbol)
    nxt_eligible = (
        context.kr_universe.nxt_eligible if context.kr_universe is not None else None
    )
    venue_eligibility = {
        "nxt": nxt_eligible,
        "regular": True
        if research_run.market_scope in {"kr", "us", "crypto"}
        else None,
    }

    payload = {
        "advisory_only": True,
        "execution_allowed": False,
        "research_run_id": str(research_run.run_uuid),
        "research_run_candidate_id": candidate.id,
        "refreshed_at": snapshot.refreshed_at,
        "candidate_kind": candidate.candidate_kind,
        "pending_order_id": pending_order.order_id
        if pending_order is not None
        else None,
        "reconciliation_status": (
            live_reconciliation.classification
            if live_reconciliation is not None
            else None
        ),
        "reconciliation_summary": (
            ",".join(live_reconciliation.reasons)
            if live_reconciliation is not None
            else None
        ),
        "nxt_classification": nxt_item.classification if nxt_item is not None else None,
        "nxt_summary": nxt_item.summary if nxt_item is not None else None,
        "nxt_eligible": nxt_eligible,
        "venue_eligibility": venue_eligibility,
        "live_quote": (
            {
                "price": live_quote.price,
                "as_of": live_quote.as_of,
            }
            if live_quote is not None
            else None
        ),
        "decision_support": (
            live_reconciliation.decision_support
            if live_reconciliation is not None
            else (nxt_item.decision_support if nxt_item is not None else {})
        ),
        "source_freshness": candidate.source_freshness,
        "warnings": list(
            dict.fromkeys(
                [
                    *candidate.warnings,
                    *snapshot.warnings,
                    *(
                        list(existing_reconciliation.warnings)
                        if existing_reconciliation is not None
                        else []
                    ),
                    *(
                        list(live_reconciliation.warnings)
                        if live_reconciliation is not None
                        else []
                    ),
                    *(list(nxt_item.warnings) if nxt_item is not None else []),
                ]
            )
        ),
    }
    return _json_safe(payload)


async def resolve_research_run(
    db: AsyncSession, *, user_id: int, selector: ResearchRunSelector
) -> ResearchRun:
    if selector.run_uuid is not None:
        research_run = await research_run_service.get_research_run_by_uuid(
            db,
            run_uuid=selector.run_uuid,
            user_id=user_id,
        )
    else:
        research_run = await research_run_service.get_latest_research_run(
            db,
            user_id=user_id,
            market_scope=selector.market_scope,
            stage=selector.stage,
            strategy_name=selector.strategy_name,
            status=selector.status,
        )

    if research_run is None:
        raise ResearchRunNotFound("Research run not found")
    return research_run


async def create_decision_session_from_research_run(
    db: AsyncSession,
    *,
    user_id: int,
    research_run: ResearchRun,
    snapshot: LiveRefreshSnapshot,
    request: ResearchRunDecisionSessionRequest,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> ResearchRunDecisionSessionResult:
    if request.include_tradingagents is True:
        raise NotImplementedError("include_tradingagents=True is not implemented")
    if not research_run.candidates:
        raise EmptyResearchRunError("Research run has no candidates")

    generated_at = request.generated_at or now()
    session = await trading_decision_service.create_decision_session(
        db,
        user_id=user_id,
        source_profile=research_run.source_profile,
        strategy_name=research_run.strategy_name,
        market_scope=research_run.market_scope,
        market_brief={},
        generated_at=generated_at,
        notes=request.notes,
    )

    pending_orders_by_id = _pending_orders_by_id(snapshot)
    reconciliations_by_order_id = _reconciliations_by_order_id(research_run)

    proposals: list[trading_decision_service.ProposalCreate] = []
    warnings: list[str] = list(snapshot.warnings)
    reconciliation_count = 0
    reconciliation_summary: dict[str, int] = {}
    nxt_summary: dict[str, int] = {}

    for candidate in sorted(research_run.candidates, key=lambda item: item.id):
        existing_reconciliation = _lookup_existing_reconciliation(
            candidate,
            reconciliations_by_order_id,
        )
        context = _build_market_context(snapshot, symbol=candidate.symbol)

        pending_order: pending_reconciliation_service.PendingOrderInput | None = None
        live_reconciliation: (
            pending_reconciliation_service.PendingReconciliationItem | None
        ) = None

        if candidate.candidate_kind == "pending_order":
            order_id = candidate.payload.get("order_id")
            if (
                isinstance(order_id, str)
                and order_id
                and order_id not in pending_orders_by_id
            ):
                warnings.append(f"missing_pending_order:{order_id}")

            pending_order = _build_pending_order_input(
                candidate,
                live_pending_order=(
                    pending_orders_by_id.get(order_id)
                    if isinstance(order_id, str)
                    else None
                ),
                existing_reconciliation=existing_reconciliation,
            )
            live_reconciliation = (
                pending_reconciliation_service.reconcile_pending_order(
                    pending_order,
                    context,
                    now=snapshot.refreshed_at,
                )
            )
            reconciliation_count += 1
            reconciliation_summary[live_reconciliation.classification] = (
                reconciliation_summary.get(live_reconciliation.classification, 0) + 1
            )
            warnings.extend(live_reconciliation.warnings)

        nxt_item = _build_nxt_item(
            candidate,
            context=context,
            pending_order=pending_order,
            holdings_qty=snapshot.holdings_by_symbol.get(candidate.symbol),
            as_of=snapshot.refreshed_at,
        )
        if nxt_item is not None:
            nxt_summary[nxt_item.classification] = (
                nxt_summary.get(nxt_item.classification, 0) + 1
            )
            warnings.extend(nxt_item.warnings)

        payload = _proposal_payload(
            candidate=candidate,
            research_run=research_run,
            snapshot=snapshot,
            existing_reconciliation=existing_reconciliation,
            live_reconciliation=live_reconciliation,
            nxt_item=nxt_item,
            pending_order=pending_order,
            context=context,
        )

        proposals.append(
            trading_decision_service.ProposalCreate(
                symbol=candidate.symbol,
                instrument_type=InstrumentType(candidate.instrument_type),
                proposal_kind=_proposal_kind_from_candidate(candidate),
                side=_proposal_side(candidate),
                original_quantity=candidate.proposed_qty,
                original_price=candidate.proposed_price,
                original_currency=candidate.currency,
                original_rationale=candidate.rationale,
                original_payload=payload,
            )
        )

    created_proposals = await trading_decision_service.add_decision_proposals(
        db,
        session_id=session.id,
        proposals=proposals,
    )

    session.market_brief = _json_safe(
        {
            "advisory_only": True,
            "execution_allowed": False,
            "research_run_uuid": str(research_run.run_uuid),
            "refreshed_at": snapshot.refreshed_at,
            "counts": {
                "candidates": len(research_run.candidates),
                "proposals": len(created_proposals),
                "pending_order_candidates": sum(
                    1
                    for candidate in research_run.candidates
                    if candidate.candidate_kind == "pending_order"
                ),
                "reconciliations": reconciliation_count,
            },
            "reconciliation_summary": reconciliation_summary,
            "nxt_summary": nxt_summary,
            "snapshot_warnings": list(snapshot.warnings),
            "source_warnings": list(research_run.source_warnings),
        }
    )
    await db.flush()
    await db.refresh(session)

    return ResearchRunDecisionSessionResult(
        session=session,
        research_run=research_run,
        refreshed_at=snapshot.refreshed_at,
        proposal_count=len(created_proposals),
        reconciliation_count=reconciliation_count,
        warnings=tuple(dict.fromkeys(warnings)),
    )


__all__ = [
    "ResearchRunDecisionSessionResult",
    "ResearchRunNotFound",
    "EmptyResearchRunError",
    "resolve_research_run",
    "create_decision_session_from_research_run",
]

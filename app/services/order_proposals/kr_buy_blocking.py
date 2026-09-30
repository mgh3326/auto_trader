"""#1112 — the 7-D "one active KR buy per symbol" blocking count, with causes.

The live standing buy ladder (operator prompt 7-D) refuses a new rung for a
symbol while that symbol has a non-terminal buy proposal. Before #1112 a
session could only see *that* a symbol was blocked; a stale row made it an
open question. This read model answers *which row* blocks and *by which rule*,
and lists the rows the night sweep / inference rule already cleared, so a
session can say "blocked by stale proposal X" or "cleared by the night sweep
at T" instead.

Scope: order-proposal rows only (component 1 of the 7-D rule). Broker-side open
orders (Toss order history, KIS ``open_buy_evidence``) remain separate inputs
and are not counted here — this report never claims their absence.

The count is every group with a non-terminal lifecycle or any non-terminal
rung (a superseded group can keep a broker-live buy): nothing is excluded at
read time. A row stops counting only when the night sweep or the inference rule has
actually written it terminal. For a KIS ``resting`` rung the report runs the
#1112 inference rule read-only and shows every failed condition, or that the
rung is eligible and waiting for the sweep to close it. Read-only; no broker.
"""

from __future__ import annotations

import datetime
from collections import defaultdict
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.order_proposals import state_machine as sm
from app.services.order_proposals.kis_leftover_inference import (
    EXPIRED_INFERENCE_CAVEAT,
    EXPIRED_INFERENCE_VOID_REASON,
)
from app.services.order_proposals.kis_leftover_inference_service import (
    KisLeftoverInferenceService,
    is_inference_candidate,
)
from app.services.order_proposals.night_sweep import (
    NIGHT_SWEEP_RUNG_STATES,
    NIGHT_SWEEP_VOID_REASON,
)

__all__ = [
    "CLEARED_BASIS_INFERENCE",
    "CLEARED_BASIS_NIGHT_SWEEP",
    "RULE_BROKER_LIVE_RUNG",
    "RULE_KIS_INFERENCE_NOT_MET",
    "RULE_KIS_INFERENCE_PENDING_SWEEP",
    "RULE_NONTERMINAL_PROPOSAL",
    "RULE_STALE_PROPOSAL",
    "build_kr_buy_blocking_report",
]

MARKET = "equity_kr"
SIDE = "buy"
CLEARED_LOOKBACK = datetime.timedelta(days=7)
NIGHT_SWEEP_TASK = "order_proposal.night_sweep"

# Closed blocking-rule vocabulary. Every blocking row carries exactly one.
RULE_NONTERMINAL_PROPOSAL = "nonterminal_buy_proposal"
RULE_STALE_PROPOSAL = "stale_proposal_past_valid_until"
RULE_KIS_INFERENCE_NOT_MET = "kis_resting_rung_inference_conditions_not_met"
RULE_KIS_INFERENCE_PENDING_SWEEP = "kis_resting_rung_inference_eligible_pending_sweep"
RULE_BROKER_LIVE_RUNG = "broker_live_rung_awaiting_broker_evidence"

CLEARED_BASIS_NIGHT_SWEEP = "valid_until_night_sweep"
CLEARED_BASIS_INFERENCE = "expired_inference"

_CLEARED_BASIS_BY_REASON = {
    NIGHT_SWEEP_VOID_REASON: CLEARED_BASIS_NIGHT_SWEEP,
    EXPIRED_INFERENCE_VOID_REASON: CLEARED_BASIS_INFERENCE,
}


def _iso(value: datetime.datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _base_item(group: Any, rung: Any) -> dict[str, Any]:
    return {
        "proposal_id": str(group.proposal_id),
        "rung_id": int(rung.id),
        "rung_index": int(rung.rung_index),
        "rung_state": rung.state,
        "lifecycle_state": group.lifecycle_state,
        "account_mode": group.account_mode,
        "strategy": group.strategy,
        "broker_order_id": rung.broker_order_id,
        "valid_until": _iso(group.valid_until),
        "created_at": _iso(group.created_at),
    }


async def _blocking_items(
    group: Any,
    rungs: list[Any],
    *,
    now: datetime.datetime,
    inference: KisLeftoverInferenceService,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    past_valid_until = group.valid_until is not None and group.valid_until <= now
    live_rungs = [rung for rung in rungs if not sm.is_terminal(rung.state)]
    # Mirrors sweep_expired: ANY rung outside the night scope (terminal ones
    # included) makes the whole group skipped, so it is not sweep-eligible.
    sweepable = group.lifecycle_state == "proposed" and all(
        rung.state in NIGHT_SWEEP_RUNG_STATES for rung in rungs
    )
    for rung in live_rungs:
        item = _base_item(group, rung)
        if is_inference_candidate(group, rung):
            decision = await inference.evaluate(group, rung, now=now)
            item["rule"] = (
                RULE_KIS_INFERENCE_PENDING_SWEEP
                if decision.eligible
                else RULE_KIS_INFERENCE_NOT_MET
            )
            item["inference"] = decision.as_row()
            if decision.eligible:
                item["cleared_by"] = NIGHT_SWEEP_TASK
        elif rung.state in sm.EVIDENCE_ACCEPTING_RUNG_STATES or rung.state == (
            "submitting"
        ):
            item["rule"] = RULE_BROKER_LIVE_RUNG
        elif past_valid_until and group.lifecycle_state == "proposed":
            item["rule"] = RULE_STALE_PROPOSAL
            item["sweep_eligible"] = sweepable
            item["cleared_by"] = NIGHT_SWEEP_TASK if sweepable else None
        else:
            item["rule"] = RULE_NONTERMINAL_PROPOSAL
            item["past_valid_until"] = past_valid_until
        items.append(item)
    return items


async def build_kr_buy_blocking_report(
    session: AsyncSession,
    *,
    now: datetime.datetime,
    symbol: str | None = None,
    unsettled_regular_buy_downgrade: bool = False,
) -> dict[str, Any]:
    """Per-symbol blocking rows (row id + rule) and recently cleared rows."""
    from app.services.order_proposals.service import OrderProposalsService

    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    service = OrderProposalsService(session)
    inference = KisLeftoverInferenceService(
        session, unsettled_regular_buy_downgrade=unsettled_regular_buy_downgrade
    )
    blocking: dict[str, list[dict[str, Any]]] = defaultdict(list)
    cleared: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for group, rungs in await service.list_active_side_groups(
        market=MARKET, side=SIDE, symbol=symbol
    ):
        blocking[group.symbol].extend(
            await _blocking_items(group, rungs, now=now, inference=inference)
        )

    for group, rung in await service.list_rungs_by_void_reasons(
        market=MARKET,
        side=SIDE,
        void_reasons=frozenset(_CLEARED_BASIS_BY_REASON),
        since=now - CLEARED_LOOKBACK,
        symbol=symbol,
    ):
        basis = _CLEARED_BASIS_BY_REASON[rung.void_reason]
        entry = _base_item(group, rung)
        entry.update(
            {
                "basis": basis,
                "void_reason": rung.void_reason,
                "cleared_at": _iso(rung.updated_at),
                "caveat": (
                    EXPIRED_INFERENCE_CAVEAT
                    if basis == CLEARED_BASIS_INFERENCE
                    else None
                ),
            }
        )
        cleared[group.symbol].append(entry)

    symbols = sorted(set(blocking) | set(cleared))
    rows = [
        {
            "symbol": sym,
            "blocked": bool(blocking.get(sym)),
            "blocking_count": len(blocking.get(sym, [])),
            "blocking": blocking.get(sym, []),
            "cleared": cleared.get(sym, []),
        }
        for sym in symbols
    ]
    return {
        "state": "known",
        "scope": "order_proposals_only",
        "market": MARKET,
        "side": SIDE,
        "as_of": now.isoformat(),
        "blocked_symbol_count": sum(1 for row in rows if row["blocked"]),
        "cleared_lookback_days": CLEARED_LOOKBACK.days,
        "note": (
            "Counts non-terminal buy proposal rows only. Broker-side open "
            "orders (Toss order history, KIS open_buy_evidence) are separate "
            "inputs and are not proven absent here."
        ),
        "symbols": rows,
    }

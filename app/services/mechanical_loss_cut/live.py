"""Turn a recorded decision into a ``loss_cut`` proposal (ROB-1460, default off).

Reached only when ``MECHANICAL_LOSS_CUT_LIVE_ENABLED`` is true. A proposal still
goes through ``dispatch_proposal``: fresh preview, per-order and daily caps,
limit-order rule and the mechanical gate. Whatever fails those gets the human
card, as every other loss-cut does.

The decision row and the proposal commit in one transaction before dispatch, so
a crash can leave a proposal unsent but cannot send it twice.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.core.timezone import now_kst
from app.mcp_server.caller_identity import caller_agent_id_var
from app.services.mechanical_loss_cut.gate import (
    MECHANICAL_STRATEGY,
    POLICY_MARKET,
    live_switch_refusal,
    regular_session_open,
)
from app.services.mechanical_loss_cut.job import WrittenDecision
from app.services.mechanical_loss_cut.rules import (
    CutDecision,
    text,
    with_fresh_limit,
)
from app.services.mechanical_loss_cut.store import MechanicalLossCutStore
from app.services.order_proposals.dispatch import (
    dispatch_proposal,
    open_auto_digest_round,
)
from app.services.order_proposals.service import OrderProposalsService, RungInput
from app.services.trade_journal.trade_retrospective_service import save_retrospective
from app.services.trading_policy_service import load_trading_policy

logger = logging.getLogger(__name__)

_RETROSPECTIVE_ACCOUNT_MODE = {"upbit": "upbit_live"}

FreshPriceFn = Callable[[str, str], Awaitable[float | None]]
DispatchFn = Callable[..., Awaitable[Any]]


async def _default_fresh_price(symbol: str, market: str) -> float | None:
    from app.mcp_server.tooling.order_validation import _get_current_price_for_order

    return await _get_current_price_for_order(symbol, market)


def _broker_account_id(account_mode: str) -> str | None:
    if account_mode == "kis_live":
        return settings.kis_account_no
    if account_mode == "toss_live" and settings.toss_api_account_seq is not None:
        return str(settings.toss_api_account_seq)
    return None


def _thesis(decision: CutDecision) -> str:
    return (
        f"Mechanical loss-cut: close {text(decision.close)} below the recorded "
        f"stop {text(decision.stop_price)} (plan v{decision.plan_version}), "
        f"{text(decision.unrealized_pct)}% from average cost."
    )


async def _create_proposal(
    decision: CutDecision,
    *,
    per_order_cap: Decimal,
    now: datetime,
    session_factory: Any,
) -> Any:
    """Write retrospective, proposal and live decision together, or nothing."""
    async with session_factory() as session:
        _status, retrospective = await save_retrospective(
            session,
            symbol=decision.key.symbol,
            instrument_type=decision.key.market,
            account_mode=_RETROSPECTIVE_ACCOUNT_MODE.get(
                decision.key.account_mode, decision.key.account_mode
            ),
            outcome="unfilled",
            side="sell",
            market=POLICY_MARKET[decision.key.market],
            strategy_key=MECHANICAL_STRATEGY,
            correlation_id=f"{MECHANICAL_STRATEGY}:{decision.trigger_key}"
            f":{decision.key.account_mode}:{decision.key.market}:{decision.key.symbol}",
            rationale=_thesis(decision),
            trigger_type="stop_loss",
            next_actions=[
                {"action": "review_mechanical_loss_cut", "owner": "operator"}
            ],
            actor=f"internal:{MECHANICAL_STRATEGY}",
        )
        service = OrderProposalsService(session)
        group = await service.create_proposal(
            symbol=decision.key.symbol,
            market=decision.key.market,
            account_mode=decision.key.account_mode,
            side="sell",
            order_type="limit",
            proposer=MECHANICAL_STRATEGY,
            rungs=[
                RungInput(
                    0,
                    "sell",
                    decision.intended_quantity,
                    decision.intended_limit_price,
                    None,
                )
            ],
            thesis=_thesis(decision),
            strategy=MECHANICAL_STRATEGY,
            broker_account_id=_broker_account_id(decision.key.account_mode),
            exit_intent="loss_cut",
            exit_reason="stop_loss",
            retrospective_id=retrospective.id,
            approval_issue_id=f"{MECHANICAL_STRATEGY}:{decision.trigger_key}",
            source_asof={
                MECHANICAL_STRATEGY: {
                    "plan_id": decision.plan_id,
                    "plan_version": decision.plan_version,
                    "trigger_key": decision.trigger_key,
                }
            },
            now=now,
        )
        row_id = await MechanicalLossCutStore(session).insert_decision(
            decision,
            mode="live",
            per_order_cap=per_order_cap,
            evaluated_at=now,
            proposal_id=group.proposal_id,
        )
        if row_id is None:
            # This trigger already has a proposal. Keep nothing from this attempt.
            await session.rollback()
            return None
        await session.commit()
        return group.proposal_id


async def emit_live_decisions(
    written: list[WrittenDecision],
    now: datetime,
    *,
    session_factory: Any = AsyncSessionLocal,
    fresh_price_fn: FreshPriceFn = _default_fresh_price,
    dispatch_fn: DispatchFn = dispatch_proposal,
    notifier: Any | None = None,
) -> dict[str, Any]:
    """Emit one proposal per decision that clears the job-side checks."""
    policy = load_trading_policy()
    outcomes: Counter[str] = Counter()
    proposal_ids: list[str] = []
    if notifier is None:
        from app.monitoring.trade_notifier import get_trade_notifier

        notifier = get_trade_notifier()
    submit_agent_id = settings.ORDER_PROPOSALS_SUBMIT_AGENT_ID.strip() or None
    identity = caller_agent_id_var.set(submit_agent_id)
    try:
        async with open_auto_digest_round(
            notifier=notifier, service_factory=session_factory
        ):
            for item in written:
                decision = item.decision
                policy_market = POLICY_MARKET[decision.key.market]
                params = policy.mechanical_loss_cut.markets[policy_market]
                refusal = live_switch_refusal(
                    market=decision.key.market,
                    account_mode=decision.key.account_mode,
                )
                if refusal is not None:
                    outcomes[refusal] += 1
                    continue
                if params.thresholds_confirmed is not True:
                    outcomes["thresholds_unconfirmed"] += 1
                    continue
                if decision.backfilled and params.live_include_backfilled is not True:
                    outcomes["backfilled_plan"] += 1
                    continue
                if not regular_session_open(decision.key.market, now):
                    outcomes["session_closed"] += 1
                    continue
                try:
                    raw_price = await fresh_price_fn(
                        decision.key.symbol, decision.key.market
                    )
                except Exception as exc:  # noqa: BLE001 - no price, no proposal
                    logger.warning(
                        "mechanical_loss_cut.fresh_price_failed",
                        extra={"exception_type": type(exc).__name__},
                    )
                    raw_price = None
                if raw_price is None or raw_price <= 0:
                    outcomes["fresh_price_unavailable"] += 1
                    continue
                fresh_price = Decimal(str(raw_price))
                if fresh_price >= decision.stop_price:
                    outcomes["price_recovered"] += 1
                    continue
                priced = with_fresh_limit(decision, price=fresh_price, params=params)
                per_order_cap = Decimal(
                    str(
                        policy.order_proposals.auto_approve.per_order_cap[policy_market]
                    )
                )
                proposal_id = await _create_proposal(
                    priced,
                    per_order_cap=per_order_cap,
                    now=now,
                    session_factory=session_factory,
                )
                if proposal_id is None:
                    outcomes["already_emitted"] += 1
                    continue
                proposal_ids.append(str(proposal_id))
                await dispatch_fn(
                    proposal_id,
                    notifier=notifier,
                    now=now,
                    now_fn=now_kst,
                    service_factory=session_factory,
                )
                outcomes["proposal_dispatched"] += 1
    finally:
        caller_agent_id_var.reset(identity)
    return {"outcomes": dict(sorted(outcomes.items())), "proposal_ids": proposal_ids}

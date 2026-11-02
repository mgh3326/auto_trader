"""Turn a recorded decision into a ``loss_cut`` proposal (ROB-1460, default off).

Reached only when ``MECHANICAL_LOSS_CUT_LIVE_ENABLED`` is true. A proposal still
goes through ``dispatch_proposal``: fresh preview, per-order and daily caps,
limit-order rule and the mechanical gate. Whatever fails those gets the human
card, as every other loss-cut does.

The decision row and the proposal commit in one transaction before dispatch, so
a crash can leave a proposal unsent but cannot send it twice.

Two recoveries, on purpose different:

- No proposal yet (market closed, no fresh price, a failed write): nothing reached
  a broker, so the job offers the trigger again on its next run.
- A proposal whose dispatch failed or raised: that is not proof the broker was not
  reached, so it is never dispatched again. A later run only sends the human card.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.core.timezone import now_kst
from app.mcp_server.caller_identity import caller_agent_id_var
from app.services.mechanical_loss_cut.gate import (
    MECHANICAL_STRATEGY,
    POLICY_MARKET,
    live_emission_refusal,
    regular_session_open,
)
from app.services.mechanical_loss_cut.job import WrittenDecision
from app.services.mechanical_loss_cut.rules import (
    CutDecision,
    text,
    with_fresh_limit,
)
from app.services.mechanical_loss_cut.store import MechanicalLossCutStore
from app.services.order_proposals.alerts import send_approval_dispatch_alert
from app.services.order_proposals.approval_window import ApprovalWindowDecision
from app.services.order_proposals.dispatch import (
    approval_window_failure_code,
    dispatch_proposal,
    open_auto_digest_round,
    send_proposal_for_approval,
)
from app.services.order_proposals.dispatch_contract import ApprovalDispatchState
from app.services.order_proposals.service import OrderProposalsService, RungInput
from app.services.trade_journal.trade_retrospective_service import save_retrospective
from app.services.trading_policy_service import load_trading_policy

logger = logging.getLogger(__name__)

_RETROSPECTIVE_ACCOUNT_MODE = {"upbit": "upbit_live"}

FreshPriceFn = Callable[[str, str], Awaitable[float | None]]
DispatchFn = Callable[..., Awaitable[Any]]

# Long enough that another run's dispatch has committed or died.
_UNDELIVERED_GRACE = timedelta(minutes=15)
_DELIVERED_STATES = frozenset(
    {ApprovalDispatchState.PENDING, ApprovalDispatchState.SENT_CURRENT}
)


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


def _undelivered_code(result: Any) -> str | None:
    """Why no card or auto-submit came of this dispatch, or ``None`` if one did."""
    if isinstance(result, ApprovalWindowDecision):
        return None if result.allowed else approval_window_failure_code(result)
    state = getattr(result, "state", None)
    if state in _DELIVERED_STATES:
        return None
    return getattr(result, "failure_code", None) or "approval_dispatch_failed"


async def _alert_undelivered(
    proposal_id: Any, *, code: str, now: datetime, session_factory: Any
) -> None:
    try:
        await send_approval_dispatch_alert(
            proposal_id,
            dispatch_state="failed",
            dispatch_failure_code=code,
            now=now,
            service_factory=session_factory,
        )
    except Exception as exc:  # noqa: BLE001 - the next run still sends the card
        logger.error(
            "mechanical_loss_cut.dispatch_alert_failed",
            extra={"exception_type": type(exc).__name__},
        )


async def _undelivered_proposal_ids(session_factory: Any, *, before: datetime) -> list:
    async with session_factory() as session:
        return await MechanicalLossCutStore(session).undelivered_live_proposals(
            before=before
        )


async def _recover_undelivered_cards(
    now: datetime,
    *,
    session_factory: Any,
    notifier: Any,
    outcomes: Counter[str],
) -> None:
    """Send the human card for a proposal an earlier run failed to deliver.

    Only ``send_proposal_for_approval``, which cannot reach a broker. It skips
    ``validate_proposal_redispatch`` because that refuses a sell limit below the
    market, and every mechanical limit is. Approving the card re-validates.
    """
    try:
        proposal_ids = await _undelivered_proposal_ids(
            session_factory, before=now - _UNDELIVERED_GRACE
        )
    except Exception as exc:  # noqa: BLE001 - new triggers can still go out
        logger.error(
            "mechanical_loss_cut.undelivered_read_failed",
            extra={"exception_type": type(exc).__name__},
        )
        outcomes["card_recovery_failed"] += 1
        return
    for proposal_id in proposal_ids:
        try:
            result = await send_proposal_for_approval(
                proposal_id,
                notifier=notifier,
                now=now,
                now_fn=now_kst,
                service_factory=session_factory,
                redispatch=True,
            )
            code = _undelivered_code(result)
        except Exception as exc:  # noqa: BLE001 - one card must not stop the rest
            logger.error(
                "mechanical_loss_cut.card_recovery_failed",
                extra={
                    "proposal_id": str(proposal_id),
                    "exception_type": type(exc).__name__,
                },
            )
            code = "approval_redispatch_internal_error"
        outcomes["card_recovered" if code is None else "card_recovery_failed"] += 1


async def emit_live_decisions(
    written: list[WrittenDecision],
    now: datetime,
    *,
    session_factory: Any = AsyncSessionLocal,
    fresh_price_fn: FreshPriceFn = _default_fresh_price,
    dispatch_fn: DispatchFn = dispatch_proposal,
    notifier: Any | None = None,
) -> dict[str, Any]:
    """Recover undelivered cards, then emit one proposal per ready decision.

    No outcome here ends the trigger except a committed live row, so a refusal or
    a failed write is simply offered again by the next run. One lot's failure
    never stops the lots after it.
    """
    policy = load_trading_policy()
    outcomes: Counter[str] = Counter()
    proposal_ids: list[str] = []
    if notifier is None:
        from app.monitoring.trade_notifier import get_trade_notifier

        notifier = get_trade_notifier()
    submit_agent_id = settings.ORDER_PROPOSALS_SUBMIT_AGENT_ID.strip() or None
    identity = caller_agent_id_var.set(submit_agent_id)
    try:
        # Before emitting, so that this run's own new proposals are not seen.
        await _recover_undelivered_cards(
            now,
            session_factory=session_factory,
            notifier=notifier,
            outcomes=outcomes,
        )
        async with open_auto_digest_round(
            notifier=notifier, service_factory=session_factory
        ):
            for item in written:
                decision = item.decision
                policy_market = POLICY_MARKET[decision.key.market]
                params = policy.mechanical_loss_cut.markets[policy_market]
                refusal = live_emission_refusal(
                    market=decision.key.market,
                    account_mode=decision.key.account_mode,
                    backfilled=decision.backfilled,
                    params=params,
                )
                if refusal is not None:
                    outcomes[refusal] += 1
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
                try:
                    proposal_id = await _create_proposal(
                        priced,
                        per_order_cap=per_order_cap,
                        now=now,
                        session_factory=session_factory,
                    )
                except Exception as exc:  # noqa: BLE001 - nothing was written
                    logger.error(
                        "mechanical_loss_cut.proposal_create_failed",
                        extra={"exception_type": type(exc).__name__},
                    )
                    outcomes["proposal_create_failed"] += 1
                    continue
                if proposal_id is None:
                    outcomes["already_emitted"] += 1
                    continue
                proposal_ids.append(str(proposal_id))
                try:
                    result = await dispatch_fn(
                        proposal_id,
                        notifier=notifier,
                        now=now,
                        now_fn=now_kst,
                        service_factory=session_factory,
                    )
                    code = _undelivered_code(result)
                except Exception as exc:  # noqa: BLE001 - the proposal is durable
                    logger.error(
                        "mechanical_loss_cut.dispatch_failed",
                        extra={
                            "proposal_id": str(proposal_id),
                            "exception_type": type(exc).__name__,
                        },
                    )
                    code = "approval_dispatch_internal_error"
                if code is None:
                    outcomes["proposal_dispatched"] += 1
                    continue
                outcomes["dispatch_undelivered"] += 1
                await _alert_undelivered(
                    proposal_id,
                    code=code,
                    now=now,
                    session_factory=session_factory,
                )
    finally:
        caller_agent_id_var.reset(identity)
    return {"outcomes": dict(sorted(outcomes.items())), "proposal_ids": proposal_ids}

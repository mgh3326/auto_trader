"""Record the evidence accompanying a new live proposal."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.symbol import to_db_symbol
from app.core.timezone import KST
from app.models.screener_pick_log import ScreenerPickLog
from app.services.decision_evidence import (
    DecisionEvidence,
    DecisionEvidenceService,
    DecisionRung,
    ReferencePrice,
    _check_decimals,
)
from app.services.mechanical_loss_cut.rules import parse_exit_plan_input


def _price(value: str | None) -> Decimal | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("decision_price_text must be a decimal string")
    try:
        price = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("decision_price_text must be a decimal string") from exc
    if not price.is_finite() or price <= 0:
        raise ValueError("decision_price_text must be positive and finite")
    return price


async def record_proposal_decision(
    session: AsyncSession,
    *,
    symbol: str,
    market: str,
    account_mode: str,
    side: str,
    action: str,
    proposer: str,
    strategy: str | None,
    sleeve_id: int | None,
    rungs: list[Any],
    source_asof: dict[str, Any],
    candidate_ref: dict[str, Any] | None,
    decision_price_text: str | None,
    now: datetime,
) -> UUID | None:
    if (account_mode, market) not in {
        ("kis_live", "equity_kr"),
        ("kis_live", "equity_us"),
        ("toss_live", "equity_kr"),
        ("toss_live", "equity_us"),
        ("upbit", "crypto"),
    }:
        return None
    canonical = symbol if market == "crypto" else to_db_symbol(symbol)
    pick_market = {"equity_kr": "kr", "equity_us": "us", "crypto": "crypto"}[market]
    query = select(ScreenerPickLog).where(
        ScreenerPickLog.symbol == canonical, ScreenerPickLog.market == pick_market
    )
    call_id = None
    basis = None
    if candidate_ref is not None:
        if (
            type(candidate_ref) is not dict
            or set(candidate_ref) != {"call_id", "symbol"}
            or candidate_ref["symbol"] != canonical
        ):
            raise ValueError("candidate_ref must contain call_id and matching symbol")
        call_id = UUID(str(candidate_ref["call_id"]))
        basis = "recorded"
        query = query.where(ScreenerPickLog.call_id == call_id)
    elif side == "buy" and action == "place":
        start = datetime.combine(now.astimezone(KST).date(), time.min, tzinfo=KST)
        query = query.where(
            ScreenerPickLog.admission == "admitted",
            ScreenerPickLog.recorded_at >= start,
            ScreenerPickLog.recorded_at <= now,
            ScreenerPickLog.recorded_at < start + timedelta(days=1),
        )
    pick = None
    if candidate_ref is not None or (side == "buy" and action == "place"):
        query = query.order_by(
            ScreenerPickLog.recorded_at.desc(), ScreenerPickLog.id.desc()
        ).limit(1)
        pick = (await session.execute(query)).scalar_one_or_none()
    if candidate_ref is not None and pick is None:
        raise ValueError("candidate_ref does not identify a candidate")
    if candidate_ref is None and pick is not None:
        call_id, basis = pick.call_id, "inference"
    prices = [
        ReferencePrice(kind="limit", price=r.limit_price, rung_index=r.rung_index)
        for r in rungs
        if r.limit_price is not None
    ]
    if (quote := _price(decision_price_text)) is not None:
        prices.append(ReferencePrice(kind="quote_at_decision", price=quote, asof=now))
    if pick is not None and pick.decision_price_text is not None:
        try:
            candidate_price = _price(pick.decision_price_text)
            _check_decimals(candidate_price)
        except ValueError:
            candidate_price = None
        if candidate_price is not None:
            prices.append(
                ReferencePrice(
                    kind="candidate_price", price=candidate_price, asof=pick.recorded_at
                )
            )
    exit_plan = source_asof.get("exit_plan")
    plan = parse_exit_plan_input(exit_plan) if exit_plan is not None else None
    evidence = DecisionEvidence(
        decision_kind="proposal",
        decided_by=proposer,
        market=cast(Any, market),
        symbol=canonical,
        decided_at=now,
        account_mode=cast(Any, account_mode),
        side=cast(Any, side),
        strategy=strategy,
        sleeve_id=sleeve_id,
        candidate_call_id=call_id,
        candidate_symbol=canonical if call_id else None,
        candidate_link_basis=cast(Any, basis),
        currency="USD" if market == "equity_us" else "KRW",
        stop_price=plan.stop_price if plan else None,
        stop_basis=plan.stop_basis if plan else None,
        stop_source="exit_plan" if plan else None,
    )
    decision_rungs = [
        DecisionRung(
            rung_index=r.rung_index,
            side=r.side,
            planned_quantity=r.quantity,
            planned_notional=r.notional,
            limit_price=r.limit_price,
        )
        for r in rungs
    ]
    return await DecisionEvidenceService(session).record(
        evidence, decision_rungs, prices
    )

"""Evaluate every held lot and record what would be sold (ROB-1460).

🔴 This module never imports or calls a broker client or an order function. Live
emission is a separate module that the caller hands in, and only when the live
switch is on. Default: nothing runs, nothing is sent.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.services.mechanical_loss_cut import positions
from app.services.mechanical_loss_cut.gate import POLICY_MARKET
from app.services.mechanical_loss_cut.rules import (
    Bar,
    CutDecision,
    ExitPlan,
    HeldLot,
    LotKey,
    PlanDraft,
    PriorDecision,
    Skip,
    backfill_plan,
    evaluate_lot,
    is_parking_instrument,
    text,
)
from app.services.mechanical_loss_cut.store import MechanicalLossCutStore
from app.services.trading_policy_service import load_trading_policy

logger = logging.getLogger(__name__)

ALL_MARKETS = ("equity_kr", "equity_us", "crypto")
_MIN_BARS = 30

LiveEmitter = Callable[[list["WrittenDecision"], datetime], Awaitable[dict[str, Any]]]


class EvaluationPorts(Protocol):
    """Everything the job reads or writes. Database only."""

    async def held_lots(self, markets: Sequence[str]) -> list[HeldLot]: ...

    async def latest_plans(self, keys: list[LotKey]) -> dict[LotKey, ExitPlan]: ...

    async def bars(
        self, market: str, symbols: list[str], count: int, now: datetime
    ) -> dict[str, tuple[list[Bar], bool]]: ...

    async def open_sell_quantities(
        self, keys: list[LotKey]
    ) -> dict[LotKey, Decimal]: ...

    async def protected_quantities(
        self, keys: list[LotKey]
    ) -> dict[LotKey, Decimal]: ...

    async def prior_decisions(
        self, keys: list[LotKey]
    ) -> dict[LotKey, PriorDecision]: ...

    async def append_backfill(self, draft: PlanDraft) -> ExitPlan: ...

    async def insert_shadow_decision(
        self, decision: CutDecision, *, per_order_cap: Decimal, evaluated_at: datetime
    ) -> int | None: ...

    async def commit(self) -> None: ...


class DatabasePorts:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._store = MechanicalLossCutStore(session)

    async def held_lots(self, markets: Sequence[str]) -> list[HeldLot]:
        return await positions.load_held_lots(self._session, markets=markets)

    async def latest_plans(self, keys: list[LotKey]) -> dict[LotKey, ExitPlan]:
        return await self._store.latest_plans(keys)

    async def bars(
        self, market: str, symbols: list[str], count: int, now: datetime
    ) -> dict[str, tuple[list[Bar], bool]]:
        return await positions.load_bars(
            self._session, market=market, symbols=symbols, count=count, now=now
        )

    async def open_sell_quantities(self, keys: list[LotKey]) -> dict[LotKey, Decimal]:
        return await positions.load_open_sell_quantities(self._session, keys)

    async def protected_quantities(self, keys: list[LotKey]) -> dict[LotKey, Decimal]:
        return await positions.load_protected_quantities(self._session, keys)

    async def prior_decisions(self, keys: list[LotKey]) -> dict[LotKey, PriorDecision]:
        return await self._store.prior_decisions(keys, mode="shadow")

    async def append_backfill(self, draft: PlanDraft) -> ExitPlan:
        await self._store.lock_lot(draft.key)
        return await self._store.append_plan(draft)

    async def insert_shadow_decision(
        self, decision: CutDecision, *, per_order_cap: Decimal, evaluated_at: datetime
    ) -> int | None:
        return await self._store.insert_decision(
            decision,
            mode="shadow",
            per_order_cap=per_order_cap,
            evaluated_at=evaluated_at,
        )

    async def commit(self) -> None:
        await self._session.commit()


@dataclass(frozen=True, slots=True)
class WrittenDecision:
    decision: CutDecision
    shadow_id: int


def _bar_count(params: Any) -> int:
    trigger = params.trigger
    return (
        max(
            _MIN_BARS,
            trigger.rising_lookback_sessions,
            trigger.n_day_low_sessions,
            trigger.trailing_lookback_sessions,
            params.default_stop.support_lookback_sessions,
            params.default_stop.atr_sessions,
            params.cooldown_sessions,
        )
        + 2
    )


async def evaluate_all_lots(
    ports: EvaluationPorts,
    *,
    now: datetime,
    markets: Sequence[str] = ALL_MARKETS,
    commit: bool = True,
) -> dict[str, Any]:
    """One pass over held lots. Returns counts and every newly written decision."""
    policy = load_trading_policy()
    auto_caps = policy.order_proposals.auto_approve.per_order_cap
    lots = [lot for lot in await ports.held_lots(markets) if lot.key.market in markets]
    keys = [lot.key for lot in lots]
    plans = await ports.latest_plans(keys)
    open_sells: dict[LotKey, Decimal] | None
    protected: dict[LotKey, Decimal] | None
    # A read that fails is unknown, and an unknown quantity never cuts.
    try:
        open_sells = await ports.open_sell_quantities(keys)
    except Exception as exc:  # noqa: BLE001 - closes the gate for every lot
        logger.warning(
            "mechanical_loss_cut.open_sell_read_failed",
            extra={"exception_type": type(exc).__name__},
        )
        open_sells = None
    try:
        protected = await ports.protected_quantities(keys)
    except Exception as exc:  # noqa: BLE001 - closes the gate for every lot
        logger.warning(
            "mechanical_loss_cut.protected_read_failed",
            extra={"exception_type": type(exc).__name__},
        )
        protected = None
    prior = await ports.prior_decisions(keys)

    written: list[WrittenDecision] = []
    skipped: Counter[str] = Counter()
    already_recorded = 0
    backfilled_plans = 0
    for market in markets:
        market_lots = [lot for lot in lots if lot.key.market == market]
        if not market_lots:
            continue
        policy_market = POLICY_MARKET[market]
        params = policy.mechanical_loss_cut.markets[policy_market]
        per_order_cap = Decimal(str(auto_caps[policy_market]))
        loaded = await ports.bars(
            market,
            sorted({lot.key.symbol for lot in market_lots}),
            _bar_count(params),
            now,
        )
        for lot in market_lots:
            bars, fresh = loaded.get(lot.key.symbol.upper(), ([], False))
            plan = plans.get(lot.key)
            if plan is None and not is_parking_instrument(lot.key.symbol, market):
                plan = await ports.append_backfill(
                    backfill_plan(lot=lot, params=params, bars=bars)
                )
                backfilled_plans += 1
            verdict = evaluate_lot(
                lot=lot,
                plan=plan,
                bars=bars,
                params=params,
                price_fresh=fresh,
                open_sell_quantity=(
                    open_sells.get(lot.key) if open_sells is not None else None
                ),
                protected_quantity=(
                    protected.get(lot.key) if protected is not None else None
                ),
                prior_decision=prior.get(lot.key),
            )
            if isinstance(verdict, Skip):
                skipped[verdict.reason] += 1
                continue
            row_id = await ports.insert_shadow_decision(
                verdict, per_order_cap=per_order_cap, evaluated_at=now
            )
            if row_id is None:
                already_recorded += 1
            else:
                written.append(WrittenDecision(verdict, row_id))
    if commit:
        await ports.commit()
    return {
        "lots_evaluated": len(lots),
        "plans_backfilled": backfilled_plans,
        "decisions_written": len(written),
        "already_recorded": already_recorded,
        "skipped": dict(sorted(skipped.items())),
        "decisions": written,
    }


def decision_preview(item: WrittenDecision) -> dict[str, Any]:
    """A printable view of one written decision."""
    decision = item.decision
    return {
        "id": item.shadow_id,
        "account_mode": decision.key.account_mode,
        "market": decision.key.market,
        "symbol": decision.key.symbol,
        "plan_version": decision.plan_version,
        "backfilled": decision.backfilled,
        "trigger_family": decision.trigger_family,
        "trigger_key": decision.trigger_key,
        "session": decision.session.isoformat(),
        "close": text(decision.close),
        "stop": text(decision.stop_price),
        "average_cost": text(decision.average_cost),
        "unrealized_pct": text(decision.unrealized_pct),
        "intended_quantity": text(decision.intended_quantity),
        "intended_limit": text(decision.intended_limit_price),
        "currency": decision.currency,
    }


async def run_mechanical_loss_cut(
    *,
    now: datetime,
    markets: Sequence[str] = ALL_MARKETS,
    commit: bool = True,
    session_factory: Any = AsyncSessionLocal,
    live_emitter: LiveEmitter | None = None,
) -> dict[str, Any]:
    """The job body. Off by default, and a dry run writes nothing."""
    if settings.MECHANICAL_LOSS_CUT_SHADOW_ENABLED is not True:
        return {"status": "disabled"}
    async with session_factory() as session:
        result = await evaluate_all_lots(
            DatabasePorts(session), now=now, markets=markets, commit=commit
        )
        if not commit:
            await session.rollback()
    written: list[WrittenDecision] = result.pop("decisions")
    result["status"] = "ok" if commit else "dry_run"
    result["decisions"] = [decision_preview(item) for item in written]
    if commit and written and settings.MECHANICAL_LOSS_CUT_LIVE_ENABLED is True:
        emitter = live_emitter
        if emitter is None:
            # Only this branch can load the order path. A test holds it shut.
            from app.services.mechanical_loss_cut.live import emit_live_decisions

            emitter = emit_live_decisions
        result["live"] = await emitter(written, now)
    return result

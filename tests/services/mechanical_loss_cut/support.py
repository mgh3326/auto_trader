"""Fixtures shared by the mechanical loss-cut tests. No database, no broker."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from app.services.mechanical_loss_cut.rules import (
    AddLevel,
    Bar,
    CutDecision,
    ExitPlan,
    HeldLot,
    LotKey,
    PlanDraft,
    PriorDecision,
)
from app.services.trading_policy_service import load_trading_policy

KEY = LotKey("kis_live", "equity_kr", "035720")
SESSION = date(2026, 10, 30)


def params(market: str = "kr") -> Any:
    return load_trading_policy().mechanical_loss_cut.markets[market]


def bars_from_closes(closes: Sequence[int | str], *, end: date = SESSION) -> list[Bar]:
    """Daily bars ending at ``end``, with an intraday range so none look frozen."""
    count = len(closes)
    bars: list[Bar] = []
    for offset, close in enumerate(closes):
        value = Decimal(str(close))
        bars.append(
            Bar(
                session=end - timedelta(days=count - 1 - offset),
                high=value * Decimal("1.01"),
                low=value * Decimal("0.99"),
                close=value,
                volume=Decimal("1000"),
                open=value,
            )
        )
    return bars


def held(
    *,
    key: LotKey = KEY,
    quantity: str = "10",
    average_cost: str = "100000",
    currency: str = "KRW",
) -> HeldLot:
    return HeldLot(
        key=key,
        quantity=Decimal(quantity),
        average_cost=Decimal(average_cost),
        currency=currency,
    )


def plan(
    *,
    key: LotKey = KEY,
    stop: str = "85000",
    version: int = 1,
    backfilled: bool = False,
    ladder: tuple[AddLevel, ...] = (),
    max_adds: int = 0,
    adds_used: int = 0,
    proposal_id: Any = None,
    plan_id: int = 7,
) -> ExitPlan:
    return ExitPlan(
        key=key,
        event="backfill" if backfilled else "entry",
        stop_price=Decimal(stop),
        stop_basis="support",
        stop_reason="60-session low",
        reference_price=Decimal("100000"),
        average_cost=Decimal("100000"),
        quantity=Decimal("10"),
        add_ladder=ladder,
        max_adds=max_adds,
        adds_used=adds_used,
        add_verdict=None,
        backfilled=backfilled,
        proposal_id=proposal_id,
        id=plan_id,
        version=version,
    )


class FakePorts:
    """In-memory ports. Holds the same once-per-trigger rule as the DB constraint."""

    def __init__(
        self,
        *,
        lots: list[HeldLot],
        plans: dict[LotKey, ExitPlan] | None = None,
        bars: dict[str, tuple[list[Bar], bool]] | None = None,
        open_sells: dict[LotKey, Decimal] | None = None,
        protected: dict[LotKey, Decimal] | None = None,
        open_sell_error: Exception | None = None,
    ) -> None:
        self.lots = lots
        self.plans = dict(plans or {})
        self.bar_map = bars or {}
        self.open_sells = open_sells or {}
        self.protected = protected or {}
        self.open_sell_error = open_sell_error
        self.rows: list[dict[str, Any]] = []
        self.backfills: list[PlanDraft] = []
        self.commits = 0

    async def held_lots(self, markets: Sequence[str]) -> list[HeldLot]:
        return list(self.lots)

    async def latest_plans(self, keys: list[LotKey]) -> dict[LotKey, ExitPlan]:
        return {key: self.plans[key] for key in keys if key in self.plans}

    async def bars(
        self, market: str, symbols: list[str], count: int, now: datetime
    ) -> dict[str, tuple[list[Bar], bool]]:
        return {symbol: self.bar_map.get(symbol, ([], False)) for symbol in symbols}

    async def open_sell_quantities(self, keys: list[LotKey]) -> dict[LotKey, Decimal]:
        if self.open_sell_error is not None:
            raise self.open_sell_error
        return {key: self.open_sells.get(key, Decimal("0")) for key in keys}

    async def protected_quantities(self, keys: list[LotKey]) -> dict[LotKey, Decimal]:
        return {key: self.protected.get(key, Decimal("0")) for key in keys}

    async def prior_decisions(self, keys: list[LotKey]) -> dict[LotKey, PriorDecision]:
        latest: dict[LotKey, PriorDecision] = {}
        for row in self.rows:
            decision = row["decision"]
            previous = latest.get(decision.key)
            same = (
                previous is not None
                and previous.plan_version == decision.plan_version
                and previous.stop_price == decision.stop_price
            )
            latest[decision.key] = PriorDecision(
                session=decision.session,
                plan_version=decision.plan_version,
                stop_price=decision.stop_price,
                cuts_on_this_stop=(previous.cuts_on_this_stop + 1) if same else 1,
            )
        return latest

    async def append_backfill(self, draft: PlanDraft) -> ExitPlan:
        self.backfills.append(draft)
        written = ExitPlan(
            key=draft.key,
            event=draft.event,
            stop_price=draft.stop_price,
            stop_basis=draft.stop_basis,
            stop_reason=draft.stop_reason,
            reference_price=draft.reference_price,
            average_cost=draft.average_cost,
            quantity=draft.quantity,
            add_ladder=draft.add_ladder,
            max_adds=draft.max_adds,
            adds_used=draft.adds_used,
            add_verdict=draft.add_verdict,
            backfilled=draft.backfilled,
            id=1000 + len(self.backfills),
            version=1,
        )
        self.plans[draft.key] = written
        return written

    async def insert_shadow_decision(
        self, decision: CutDecision, *, per_order_cap: Decimal, evaluated_at: datetime
    ) -> int | None:
        identity = (decision.key, decision.trigger_key)
        if any(
            (row["decision"].key, row["decision"].trigger_key) == identity
            for row in self.rows
        ):
            return None
        self.rows.append({"decision": decision, "per_order_cap": per_order_cap})
        return len(self.rows)

    async def commit(self) -> None:
        self.commits += 1

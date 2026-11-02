"""The only writer of ``review.lot_exit_plans`` and the decisions table (ROB-1460).

Reads and writes the database only. No broker package is imported here.
"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.mechanical_loss_cut import LotExitPlan, MechanicalLossCutDecision
from app.services.mechanical_loss_cut.rules import (
    AddLevel,
    CutDecision,
    ExitPlan,
    LotKey,
    PlanDraft,
)
from app.services.trading_policy_service import policy_version_stamp


def _ladder_json(ladder: tuple[AddLevel, ...]) -> list[dict[str, str]]:
    return [
        {"price": str(level.price), "quantity": str(level.quantity)} for level in ladder
    ]


def _to_plan(row: LotExitPlan) -> ExitPlan:
    return ExitPlan(
        key=LotKey(row.account_mode, row.market, row.symbol),
        event=row.event,  # type: ignore[arg-type]
        stop_price=row.stop_price,
        stop_basis=row.stop_basis,  # type: ignore[arg-type]
        stop_reason=row.stop_reason,
        reference_price=row.reference_price,
        average_cost=row.average_cost,
        quantity=row.quantity,
        add_ladder=tuple(
            AddLevel(Decimal(item["price"]), Decimal(item["quantity"]))
            for item in row.add_ladder or []
        ),
        max_adds=row.max_adds,
        adds_used=row.adds_used,
        add_verdict=row.add_verdict,
        backfilled=row.backfilled,
        proposal_id=row.proposal_id,
        rung_index=row.rung_index,
        id=row.id,
        version=row.version,
        created_at=row.created_at,
    )


class MechanicalLossCutStore:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def latest_plan(self, key: LotKey) -> ExitPlan | None:
        row = (
            await self._session.execute(
                select(LotExitPlan)
                .where(
                    LotExitPlan.account_mode == key.account_mode,
                    LotExitPlan.market == key.market,
                    LotExitPlan.symbol == key.symbol,
                )
                .order_by(LotExitPlan.version.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        return _to_plan(row) if row is not None else None

    async def latest_plans(self, keys: list[LotKey]) -> dict[LotKey, ExitPlan]:
        plans: dict[LotKey, ExitPlan] = {}
        for key in keys:
            plan = await self.latest_plan(key)
            if plan is not None:
                plans[key] = plan
        return plans

    async def lock_lot(self, key: LotKey) -> None:
        """Serialize version assignment for one lot until the transaction ends."""
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": f"lot_exit_plan:{key.account_mode}:{key.market}:{key.symbol}"},
        )

    async def append_plan(self, draft: PlanDraft) -> ExitPlan:
        """Write the next version. The caller must hold ``lock_lot``."""
        current = (
            await self._session.execute(
                select(func.max(LotExitPlan.version)).where(
                    LotExitPlan.account_mode == draft.key.account_mode,
                    LotExitPlan.market == draft.key.market,
                    LotExitPlan.symbol == draft.key.symbol,
                )
            )
        ).scalar_one()
        stamp = policy_version_stamp()
        row = LotExitPlan(
            account_mode=draft.key.account_mode,
            market=draft.key.market,
            symbol=draft.key.symbol,
            version=(current or 0) + 1,
            event=draft.event,
            stop_price=draft.stop_price,
            stop_basis=draft.stop_basis,
            stop_reason=draft.stop_reason,
            reference_price=draft.reference_price,
            average_cost=draft.average_cost,
            quantity=draft.quantity,
            add_ladder=_ladder_json(draft.add_ladder),
            max_adds=draft.max_adds,
            adds_used=draft.adds_used,
            add_verdict=draft.add_verdict,
            backfilled=draft.backfilled,
            proposal_id=draft.proposal_id,
            rung_index=draft.rung_index,
            policy_version=stamp["version"],
            policy_content_hash=stamp["content_hash"],
        )
        self._session.add(row)
        await self._session.flush()
        return _to_plan(row)

    async def insert_decision(
        self,
        decision: CutDecision,
        *,
        mode: str,
        per_order_cap: Decimal,
        evaluated_at: datetime,
        proposal_id: uuid.UUID | None = None,
    ) -> int | None:
        """Insert once per trigger. ``None`` means this trigger already fired."""
        stamp = policy_version_stamp()
        statement = (
            insert(MechanicalLossCutDecision)
            .values(
                mode=mode,
                account_mode=decision.key.account_mode,
                market=decision.key.market,
                symbol=decision.key.symbol,
                plan_id=decision.plan_id,
                plan_version=decision.plan_version,
                backfilled=decision.backfilled,
                trigger_family=decision.trigger_family,
                trigger_key=decision.trigger_key,
                session=decision.session,
                close_price=decision.close,
                stop_price=decision.stop_price,
                average_cost=decision.average_cost,
                unrealized_pct=decision.unrealized_pct,
                held_quantity=decision.held_quantity,
                open_sell_quantity=decision.open_sell_quantity,
                protected_quantity=decision.protected_quantity,
                intended_quantity=decision.intended_quantity,
                intended_limit_price=decision.intended_limit_price,
                intended_notional=decision.intended_notional,
                currency=decision.currency,
                per_order_cap=per_order_cap,
                within_per_order_cap=decision.intended_notional <= per_order_cap,
                details=json.loads(json.dumps(decision.details)),
                proposal_id=proposal_id,
                policy_version=stamp["version"],
                policy_content_hash=stamp["content_hash"],
                evaluated_at=evaluated_at,
            )
            .on_conflict_do_nothing(
                constraint="uq_mechanical_loss_cut_decisions_trigger"
            )
            .returning(MechanicalLossCutDecision.id)
        )
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def last_decision_sessions(
        self, keys: list[LotKey], *, mode: str
    ) -> dict[LotKey, date]:
        if not keys:
            return {}
        rows = await self._session.execute(
            select(
                MechanicalLossCutDecision.account_mode,
                MechanicalLossCutDecision.market,
                MechanicalLossCutDecision.symbol,
                func.max(MechanicalLossCutDecision.session),
            )
            .where(
                MechanicalLossCutDecision.mode == mode,
                MechanicalLossCutDecision.symbol.in_({key.symbol for key in keys}),
            )
            .group_by(
                MechanicalLossCutDecision.account_mode,
                MechanicalLossCutDecision.market,
                MechanicalLossCutDecision.symbol,
            )
        )
        return {LotKey(row[0], row[1], row[2]): row[3] for row in rows.all()}

    async def live_decision_for_proposal(
        self, proposal_id: uuid.UUID
    ) -> MechanicalLossCutDecision | None:
        return (
            await self._session.execute(
                select(MechanicalLossCutDecision).where(
                    MechanicalLossCutDecision.mode == "live",
                    MechanicalLossCutDecision.proposal_id == proposal_id,
                )
            )
        ).scalar_one_or_none()

    async def decisions_for_session(
        self, session_date: date, *, mode: str = "shadow"
    ) -> list[MechanicalLossCutDecision]:
        return list(
            (
                await self._session.execute(
                    select(MechanicalLossCutDecision)
                    .where(
                        MechanicalLossCutDecision.session == session_date,
                        MechanicalLossCutDecision.mode == mode,
                    )
                    .order_by(MechanicalLossCutDecision.id)
                )
            )
            .scalars()
            .all()
        )


def summarize_decisions(rows: list[Any], *, session_date: date) -> dict[str, Any]:
    """The daily summary. Backfilled plans are always counted apart."""
    planned = [row for row in rows if not row.backfilled]
    backfilled = [row for row in rows if row.backfilled]

    def bucket(items: list[Any]) -> dict[str, Any]:
        by_market: dict[str, dict[str, Any]] = {}
        for row in items:
            entry = by_market.setdefault(
                row.market,
                {
                    "count": 0,
                    "notional": Decimal("0"),
                    "currency": row.currency,
                    "over_per_order_cap": 0,
                    "symbols": [],
                },
            )
            entry["count"] += 1
            entry["notional"] += Decimal(row.intended_notional)
            entry["over_per_order_cap"] += 0 if row.within_per_order_cap else 1
            entry["symbols"].append(row.symbol)
        for entry in by_market.values():
            entry["notional"] = str(entry["notional"])
        return {"count": len(items), "by_market": by_market}

    return {
        "session": session_date.isoformat(),
        "planned": bucket(planned),
        "backfilled": bucket(backfilled),
    }

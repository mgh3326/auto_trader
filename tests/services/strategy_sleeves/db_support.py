"""Seeding helpers for the ROB-1470 DB tests (run-owned test database only)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import sqlalchemy as sa

from app.models.execution_ledger import ExecutionLedger
from app.models.order_proposals import OrderProposal, OrderProposalRung
from app.models.review import KISLiveOrderLedger
from app.models.strategy_sleeves import StrategySleeve, StrategySleeveVersion
from app.models.trading import InstrumentType
from app.services.order_proposals import OrderProposalsService
from app.services.order_proposals.service import RungInput
from app.services.strategy_sleeves import store

#: A complete test pre-registration (a live sleeve requires one).
PREREG: dict[str, Any] = {
    "metric": "cost-net result at H minus baseline",
    "min_effect_pct": "1",
    "horizon_sessions": "20",
    "max_duration_days": "90",
}


class SleeveSeed:
    """Creates uniquely named rows and removes every one of them afterwards."""

    def __init__(self, db) -> None:
        self.db = db
        self.sleeve_ids: list[int] = []
        self.proposal_pks: list[int] = []
        self.order_ids: list[str] = []
        self.acked_at: dict[str, datetime] = {}
        self.send_order_nos: list[str] = []

    async def sleeve(
        self,
        *,
        state: str = "live",
        budget_cap: str = "1000",
        loss_limit: str = "100",
        account_mode: str = "kis_live",
        market: str = "equity_kr",
        rule_id: str = "rob1458.kr_cut_minus20",
    ) -> StrategySleeve:
        sleeve, _version = await store.create_sleeve(
            self.db,
            sleeve_key=f"test-{uuid.uuid4().hex[:12]}",
            rule_id=rule_id,
            account_mode=account_mode,
            market=market,
            state=state,
            budget_cap=budget_cap,
            loss_limit=loss_limit,
            reason="test fixture",
            actor="test",
            **PREREG,
        )
        await self.db.commit()
        self.sleeve_ids.append(sleeve.id)
        return sleeve

    async def proposal(
        self,
        *,
        sleeve_id: int | None,
        symbol: str,
        quantity: str = "10",
        limit_price: str = "100",
        side: str = "buy",
        account_mode: str = "kis_live",
        market: str = "equity_kr",
        supersedes: uuid.UUID | None = None,
        commit: bool = True,
    ) -> OrderProposal:
        group = await OrderProposalsService(self.db).create_proposal(
            symbol=symbol,
            market=market,
            account_mode=account_mode,
            side=side,
            order_type="limit",
            proposer="p",
            thesis="sleeve test thesis",
            rungs=[RungInput(0, side, Decimal(quantity), Decimal(limit_price), None)],
            sleeve_id=sleeve_id,
            supersedes_proposal_id=supersedes,
        )
        if commit:
            await self.db.commit()
        self.proposal_pks.append(group.id)
        return group

    async def record_send(
        self, group: OrderProposal, order_id: str, *, at: datetime
    ) -> None:
        """Test seam: what a real KIS send leaves behind, without a broker call.

        The rung goes to ``acked`` with the order number and an idempotency key,
        and the send ledger gets the row a real send writes (same number and
        key). That row dates the rung's order. The caller commits.
        """
        idem = f"idem-{order_id}"
        rung = (
            await self.db.execute(
                sa.select(OrderProposalRung).where(
                    OrderProposalRung.proposal_pk == group.id
                )
            )
        ).scalar_one()
        await self.db.execute(
            sa.update(OrderProposalRung)
            .where(OrderProposalRung.id == rung.id)
            .values(state="acked", broker_order_id=order_id, idempotency_key=idem)
        )
        self.db.add(
            KISLiveOrderLedger(
                trade_date=at,
                symbol=str(group.symbol),
                instrument_type="equity_kr",
                side=str(rung.side),
                order_type="limit",
                quantity=rung.quantity,
                price=rung.limit_price,
                currency="KRW",
                order_no=order_id,
                account_mode="kis_live",
                broker="kis",
                status="accepted",
                lifecycle_state="accepted",
                idempotency_key=idem,
            )
        )
        self.send_order_nos.append(order_id)

    async def mark_acked(self, group: OrderProposal, order_id: str) -> None:
        """Put the rung at the broker (see ``record_send``) and commit."""
        now = datetime.now(UTC)
        await self.record_send(group, order_id, at=now)
        await self.db.commit()
        self.order_ids.append(order_id)
        self.acked_at[order_id] = now

    async def fill(
        self,
        *,
        order_id: str,
        symbol: str,
        side: str = "buy",
        quantity: str = "10",
        price: str = "100",
        filled_at: datetime | None = None,
        source: str = "reconciler",
    ) -> int:
        row = ExecutionLedger(
            broker="kis",
            account_mode="live",
            venue="krx",
            instrument_type=InstrumentType.equity_kr,
            symbol=symbol,
            raw_symbol=symbol,
            side=side,
            broker_order_id=order_id,
            fill_seq=0,
            filled_qty=Decimal(quantity),
            filled_price=Decimal(price),
            filled_notional=Decimal(quantity) * Decimal(price),
            # Just after the send by default: the same KIS trading day.
            filled_at=filled_at
            or self.acked_at.get(order_id, datetime.now(UTC)) + timedelta(seconds=1),
            currency="KRW",
            source=source,
        )
        self.db.add(row)
        await self.db.commit()
        self.order_ids.append(order_id)
        return int(row.id)

    async def cleanup(self) -> None:
        await self.db.rollback()
        # Sleeve rows are immutable for every role; teardown is the one place
        # they must go, so triggers are off for this transaction only.
        await self.db.execute(sa.text("SET LOCAL session_replication_role = replica"))
        if self.order_ids:
            await self.db.execute(
                sa.delete(ExecutionLedger).where(
                    ExecutionLedger.broker_order_id.in_(self.order_ids)
                )
            )
        if self.send_order_nos:
            await self.db.execute(
                sa.delete(KISLiveOrderLedger).where(
                    KISLiveOrderLedger.order_no.in_(self.send_order_nos)
                )
            )
        if self.proposal_pks or self.sleeve_ids:
            await self.db.execute(
                sa.delete(OrderProposal).where(
                    sa.or_(
                        OrderProposal.id.in_(self.proposal_pks or [-1]),
                        OrderProposal.sleeve_id.in_(self.sleeve_ids or [-1]),
                    )
                )
            )
        if self.sleeve_ids:
            await self.db.execute(
                sa.delete(StrategySleeveVersion).where(
                    StrategySleeveVersion.sleeve_id.in_(self.sleeve_ids)
                )
            )
            await self.db.execute(
                sa.delete(StrategySleeve).where(StrategySleeve.id.in_(self.sleeve_ids))
            )
        await self.db.commit()


def unique_symbol() -> str:
    return f"S{uuid.uuid4().hex[:5].upper()}"


def unique_order_id() -> str:
    return f"9{uuid.uuid4().int % 10**9:09d}"


async def raw(db, statement: str, **params: Any) -> None:
    await db.execute(sa.text(statement), params)

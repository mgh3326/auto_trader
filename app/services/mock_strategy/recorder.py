"""Only writer of review.mock_strategy_orders; no broker imports or calls."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert

from app.models.mock_strategy import OPEN_STATUSES, STATUSES, VENUES, MockStrategyOrder


@dataclass(frozen=True)
class OrderIntent:
    strategy_id: str
    venue: str
    account_ref: str
    episode_id: str
    symbol: str
    side: str
    qty: Decimal | None
    notional: Decimal | None
    client_order_id: str
    exit_due_at: datetime | None
    meta: dict

    def __post_init__(self) -> None:
        for key, bound in (
            ("strategy_id", 128),
            ("account_ref", 128),
            ("episode_id", 128),
            ("symbol", 64),
            ("client_order_id", 128),
        ):
            value = getattr(self, key)
            if not isinstance(value, str) or not value.strip() or len(value) > bound:
                raise ValueError(f"invalid {key}")
        if self.venue not in VENUES or self.side not in {
            "buy",
            "sell",
            "short",
            "cover",
        }:
            raise ValueError("invalid paper/demo venue or side")
        if self.qty is None and self.notional is None:
            raise ValueError("size required")
        for value in (self.qty, self.notional):
            if value is not None and (
                not isinstance(value, Decimal) or not value.is_finite() or value <= 0
            ):
                raise ValueError("size must be a positive finite Decimal")
        if self.exit_due_at is not None and self.exit_due_at.utcoffset() is None:
            raise ValueError("exit_due_at must be timezone aware")


def record_alpaca_absence(row: MockStrategyOrder) -> bool:
    if (
        row.venue != "alpaca_paper"
        or row.status not in {"sending", "uncertain"}
        or row.broker_order_id is not None
        or row.opened_at is not None
        or row.entry_price is not None
        or row.exit_price is not None
    ):
        return False
    try:
        if Decimal(row.meta.get("filled_qty", "0")) != 0:
            return False
    except (InvalidOperation, TypeError, ValueError):
        return False
    row.status = "rejected"
    row.meta = {
        **row.meta,
        "filled_qty": "0",
        "broker_terminal": True,
        "no_order_proven": True,
        "broker_status": "broker_confirmed_absent",
    }
    return True


class MockStrategyRecorder:
    def __init__(self, session_factory=None):
        if session_factory is None:
            from app.core.db import AsyncSessionLocal

            session_factory = AsyncSessionLocal
        self._sessions = session_factory

    @asynccontextmanager
    async def account_lock(self, *, venue: str, account_ref: str):
        """Serialize this consumer's episode book on the netted paper account.

        Other manual clients are outside this lock; fresh position checks at
        send remain necessary. This is not a broker fencing token.
        """
        key = int.from_bytes(
            hashlib.sha256(f"mock-strategy:{venue}:{account_ref}".encode()).digest()[
                :8
            ],
            "big",
            signed=True,
        )
        async with self._sessions() as session, session.begin():
            locked = await session.scalar(
                text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": key}
            )
            if not locked:
                raise ValueError("paper strategy account already in use")
            # Commit/rollback releases the lock on the owning connection,
            # including cancellation, with no session-level unlock to leak.
            yield

    async def record(self, intent: OrderIntent) -> MockStrategyOrder:
        return (await self.record_many([intent]))[0]

    async def record_many(
        self, intents: Sequence[OrderIntent]
    ) -> list[MockStrategyOrder]:
        """Commit the entire plan or nothing; a conflicting reused ID is an error."""
        by_id = {}
        async with self._sessions() as session:
            async with session.begin():
                for intent in sorted(intents, key=lambda item: item.client_order_id):
                    values = asdict(intent)
                    await session.execute(
                        insert(MockStrategyOrder)
                        .values(**values, status="planned")
                        .on_conflict_do_nothing(index_elements=["client_order_id"])
                    )
                    row = (
                        await session.scalars(
                            select(MockStrategyOrder).where(
                                MockStrategyOrder.client_order_id
                                == intent.client_order_id
                            )
                        )
                    ).one()
                    # meta may acquire evidence after sending; original intent keys
                    # remain immutable and are compared on every idempotent replay.
                    for key, value in values.items():
                        actual = getattr(row, key)
                        if key == "meta":
                            if any(actual.get(k) != v for k, v in value.items()):
                                raise ValueError("client_order_id intent conflict")
                        elif actual != value:
                            raise ValueError("client_order_id intent conflict")
                    by_id[intent.client_order_id] = row
                # Detach unique, loaded rows before commit can expire them,
                # including factories with expire_on_commit=True.
                for row in by_id.values():
                    session.expunge(row)
        return [by_id[intent.client_order_id] for intent in intents]

    async def get(self, client_order_id: str) -> MockStrategyOrder | None:
        async with self._sessions() as session:
            return (
                await session.scalars(
                    select(MockStrategyOrder).where(
                        MockStrategyOrder.client_order_id == client_order_id
                    )
                )
            ).one_or_none()

    async def claim(self, client_order_id: str) -> bool:
        """The committed planned→sending CAS is the sole permission to POST."""
        async with self._sessions() as session, session.begin():
            result = await session.execute(
                update(MockStrategyOrder)
                .where(
                    MockStrategyOrder.client_order_id == client_order_id,
                    MockStrategyOrder.status == "planned",
                )
                .values(status="sending")
                .returning(MockStrategyOrder.id)
            )
            return result.scalar_one_or_none() is not None

    async def record_settings_event(self, client_order_id: str, event: dict) -> None:
        async with self._sessions() as session, session.begin():
            row = (
                await session.scalars(
                    select(MockStrategyOrder)
                    .where(MockStrategyOrder.client_order_id == client_order_id)
                    .with_for_update()
                )
            ).one()
            if row.status != "planned":
                raise ValueError("settings audit requires planned entry")
            row.meta = {
                **row.meta,
                "settings_events": [*row.meta.get("settings_events", []), event],
            }
            await session.flush()

    async def record_symbol_change(self, client_order_id: str, event: dict) -> None:
        """Journal an operator-declared ticker change on a still-planned exit leg.

        The symbol column stays immutable; the broker symbol is ``event["to"]``.
        A different mapping than one already journaled is an error.
        """
        async with self._sessions() as session, session.begin():
            row = (
                await session.scalars(
                    select(MockStrategyOrder)
                    .where(MockStrategyOrder.client_order_id == client_order_id)
                    .with_for_update()
                )
            ).one()
            if row.status != "planned" or row.side not in {"sell", "cover"}:
                raise ValueError("symbol change requires a planned exit leg")
            if event.get("from") != row.symbol:
                raise ValueError("symbol change must start from the journaled symbol")
            existing = row.meta.get("symbol_change")
            if existing is not None:
                if (existing.get("from"), existing.get("to")) != (
                    event["from"],
                    event["to"],
                ):
                    raise ValueError("symbol change conflicts with journaled mapping")
                return
            row.meta = {**row.meta, "symbol_change": event}
            await session.flush()

    async def confirm_alpaca_absence(
        self, client_order_id: str, *, account_ref: str
    ) -> MockStrategyOrder:
        """Record exact paper lookup absence only while its send is unresolved."""
        async with self._sessions() as session, session.begin():
            row = (
                await session.scalars(
                    select(MockStrategyOrder)
                    .where(
                        MockStrategyOrder.client_order_id == client_order_id,
                        MockStrategyOrder.venue == "alpaca_paper",
                        MockStrategyOrder.account_ref == account_ref,
                    )
                    .with_for_update()
                )
            ).one()
            if record_alpaca_absence(row):
                await session.flush()
            session.expunge(row)
            return row

    async def update(
        self,
        client_order_id: str,
        *,
        status: str,
        broker_order_id: str | None = None,
        opened_at: datetime | None = None,
        closed_at: datetime | None = None,
        entry_price: Decimal | None = None,
        exit_price: Decimal | None = None,
        realized_pnl: Decimal | None = None,
        meta: dict | None = None,
    ) -> MockStrategyOrder:
        if status not in STATUSES or status == "planned":
            raise ValueError("invalid outcome status")
        async with self._sessions() as session, session.begin():
            row = (
                await session.scalars(
                    select(MockStrategyOrder)
                    .where(MockStrategyOrder.client_order_id == client_order_id)
                    .with_for_update()
                )
            ).one()
            if (
                row.status in {"closed", "canceled", "rejected"}
                and row.status != status
            ):
                raise ValueError("terminal order cannot reopen")
            if status == "sending" and row.status != "sending":
                raise ValueError("use claim for sending")
            if broker_order_id is not None and row.broker_order_id not in {
                None,
                broker_order_id,
            }:
                raise ValueError("broker order binding conflict")
            row.status = status
            for key, value in (
                ("broker_order_id", broker_order_id),
                ("opened_at", opened_at),
                ("closed_at", closed_at),
                ("entry_price", entry_price),
                ("exit_price", exit_price),
                ("realized_pnl", realized_pnl),
            ):
                if value is not None and (key != "opened_at" or row.opened_at is None):
                    setattr(row, key, value)
            row.meta = {**row.meta, **(meta or {})}
            await session.flush()
            session.expunge(row)
            return row

    async def list_open(
        self,
        *,
        venue: str,
        account_ref: str | None = None,
        strategy_id: str | None = None,
    ) -> list[MockStrategyOrder]:
        return await self._list(venue, account_ref, strategy_id, open_only=True)

    async def list_due(
        self,
        *,
        venue: str,
        asof: datetime,
        account_ref: str | None = None,
        strategy_id: str | None = None,
    ) -> list[MockStrategyOrder]:
        return await self._list(
            venue, account_ref, strategy_id, open_only=True, due=asof
        )

    async def list_orders(
        self,
        *,
        venue: str,
        account_ref: str | None = None,
        strategy_id: str | None = None,
    ) -> list[MockStrategyOrder]:
        return await self._list(venue, account_ref, strategy_id)

    async def _list(
        self,
        venue: str,
        account: str | None,
        strategy: str | None,
        *,
        open_only: bool = False,
        due: datetime | None = None,
    ) -> list[MockStrategyOrder]:
        query = select(MockStrategyOrder).where(MockStrategyOrder.venue == venue)
        if account is not None:
            query = query.where(MockStrategyOrder.account_ref == account)
        if strategy is not None:
            query = query.where(MockStrategyOrder.strategy_id == strategy)
        if open_only:
            query = query.where(MockStrategyOrder.status.in_(OPEN_STATUSES))
        if due is not None:
            if due.utcoffset() is None:
                raise ValueError("asof must be timezone aware")
            query = query.where(
                MockStrategyOrder.side.in_(("buy", "short")),
                MockStrategyOrder.exit_due_at <= due,
            )
        async with self._sessions() as session:
            return list(
                (await session.scalars(query.order_by(MockStrategyOrder.id))).all()
            )

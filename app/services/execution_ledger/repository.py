"""Repository write/read primitives for the broker execution ledger (ROB-211)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import Select, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.execution_ledger import ExecutionLedger, ExecutionLedgerReconcileRun
from app.schemas.execution_ledger import ExecutionLedgerUpsert, ReconcileRunRecord

UpsertStatus = Literal["inserted", "updated", "unchanged"]

COMPARE_COLUMNS = (
    "account_mode",
    "venue",
    "instrument_type",
    "symbol",
    "raw_symbol",
    "side",
    "filled_qty",
    "filled_price",
    "filled_notional",
    "fee_amount",
    "fee_currency",
    "filled_at",
    "currency",
    "correlation_id",
    "source",
    "source_run_id",
    "raw_payload_json",
)


def _model_payload(fill: ExecutionLedgerUpsert) -> dict:
    data = fill.model_dump()
    if data.get("instrument_type") is not None:
        data["instrument_type"] = str(data["instrument_type"])
    return data


def _values_equal(current: Any, expected: Any) -> bool:
    if isinstance(current, Decimal) or isinstance(expected, Decimal):
        if current is None or expected is None:
            return current is expected
        return Decimal(str(current)) == Decimal(str(expected))
    if isinstance(current, datetime) and isinstance(expected, datetime):
        current_cmp = current if current.tzinfo else current.replace(tzinfo=UTC)
        expected_cmp = expected if expected.tzinfo else expected.replace(tzinfo=UTC)
        return current_cmp.astimezone(UTC) == expected_cmp.astimezone(UTC)
    return current == expected


def _values_differ(row: ExecutionLedger, fill: ExecutionLedgerUpsert) -> bool:
    for column in COMPARE_COLUMNS:
        expected = getattr(fill, column)
        current = getattr(row, column)
        if not _values_equal(current, expected):
            return True
    return False


class ExecutionLedgerRepository:
    """The only write surface for review.execution_ledger."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_by_key(
        self,
        broker: str,
        account_mode: str,
        venue: str,
        broker_order_id: str,
        fill_seq: int,
    ) -> ExecutionLedger | None:
        result = await self.db.execute(
            select(ExecutionLedger).where(
                ExecutionLedger.broker == broker,
                ExecutionLedger.account_mode == account_mode,
                ExecutionLedger.venue == venue,
                ExecutionLedger.broker_order_id == broker_order_id,
                ExecutionLedger.fill_seq == fill_seq,
            )
        )
        return result.scalar_one_or_none()

    async def classify_fill(self, fill: ExecutionLedgerUpsert) -> UpsertStatus:
        existing = await self.get_by_key(
            fill.broker,
            fill.account_mode,
            fill.venue,
            fill.broker_order_id,
            fill.fill_seq,
        )
        if existing is None:
            return "inserted"
        return "updated" if _values_differ(existing, fill) else "unchanged"

    async def upsert_fill(
        self, fill: ExecutionLedgerUpsert
    ) -> tuple[UpsertStatus, int]:
        """Insert or update one fill by the broker idempotency key."""
        status = await self.classify_fill(fill)
        if status == "unchanged":
            existing = await self.get_by_key(
                fill.broker,
                fill.account_mode,
                fill.venue,
                fill.broker_order_id,
                fill.fill_seq,
            )
            return "unchanged", int(existing.id) if existing else 0

        payload = _model_payload(fill)
        stmt = insert(ExecutionLedger).values(**payload)
        update_payload = {
            key: getattr(stmt.excluded, key)
            for key in payload
            if key
            not in {"broker", "account_mode", "venue", "broker_order_id", "fill_seq"}
        }
        update_payload["updated_at"] = datetime.now(UTC)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_execution_ledger_fill",
            set_=update_payload,
        ).returning(ExecutionLedger.id)
        result = await self.db.execute(stmt)
        row_id = int(result.scalar_one())
        return status, row_id

    def record_run(self, run: ReconcileRunRecord) -> None:
        self.db.add(ExecutionLedgerReconcileRun(**run.model_dump()))

    async def latest_run_per_broker(self) -> dict[str, ReconcileRunRecord]:
        latest_started = (
            select(
                ExecutionLedgerReconcileRun.broker,
                func.max(ExecutionLedgerReconcileRun.started_at).label("started_at"),
            )
            .where(ExecutionLedgerReconcileRun.error_summary.is_(None))
            .group_by(ExecutionLedgerReconcileRun.broker)
            .subquery()
        )
        rows = await self.db.execute(
            select(ExecutionLedgerReconcileRun).join(
                latest_started,
                (ExecutionLedgerReconcileRun.broker == latest_started.c.broker)
                & (
                    ExecutionLedgerReconcileRun.started_at
                    == latest_started.c.started_at
                ),
            )
        )
        return {
            row.broker: ReconcileRunRecord.model_validate(row)
            for row in rows.scalars().all()
        }

    @staticmethod
    def apply_market_filter(stmt: Select, market: str | None) -> Select:
        if market == "kr":
            return stmt.where(ExecutionLedger.instrument_type == "equity_kr")
        if market == "us":
            return stmt.where(ExecutionLedger.instrument_type == "equity_us")
        if market == "crypto":
            return stmt.where(ExecutionLedger.instrument_type == "crypto")
        return stmt

    async def net_quantity_by_match_key_since(
        self, *, cutover: datetime
    ) -> dict[tuple[str, str, str, str, str, str], Decimal]:
        from sqlalchemy import case
        signed_qty = case(
            (ExecutionLedger.side == "buy", ExecutionLedger.filled_qty),
            else_=-ExecutionLedger.filled_qty,
        )
        rows = await self.db.execute(
            select(
                ExecutionLedger.broker,
                ExecutionLedger.account_mode,
                ExecutionLedger.venue,
                ExecutionLedger.instrument_type,
                ExecutionLedger.symbol,
                ExecutionLedger.currency,
                func.coalesce(func.sum(signed_qty), 0),
            )
            .where(ExecutionLedger.filled_at >= cutover)
            .where(ExecutionLedger.source != "manual_import")
            .group_by(
                ExecutionLedger.broker,
                ExecutionLedger.account_mode,
                ExecutionLedger.venue,
                ExecutionLedger.instrument_type,
                ExecutionLedger.symbol,
                ExecutionLedger.currency,
            )
        )
        return {
            (broker, account_mode, venue, str(instrument_type), symbol, currency): Decimal(str(net_qty))
            for broker, account_mode, venue, instrument_type, symbol, currency, net_qty in rows.all()
        }

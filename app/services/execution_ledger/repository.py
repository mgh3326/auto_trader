"""Repository write/read primitives for the broker execution ledger (ROB-211)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import Select, and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.execution_ledger import (
    ExecutionLedger,
    ExecutionLedgerQuarantineEvent,
    ExecutionLedgerReconcileRun,
    execution_ledger_in_effect,
)
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

    async def has_fill_for_order(
        self,
        *,
        broker: str,
        account_mode: str,
        venue: str,
        broker_order_id: str,
    ) -> bool:
        """Return whether durable fill evidence exists for one broker order."""
        result = await self.db.execute(
            select(ExecutionLedger.id)
            .where(
                ExecutionLedger.broker == broker,
                ExecutionLedger.account_mode == account_mode,
                ExecutionLedger.venue == venue,
                ExecutionLedger.broker_order_id == broker_order_id,
                execution_ledger_in_effect(),
            )
            .limit(1)
        )
        return result.scalar_one_or_none() is not None

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

    async def rows_by_ids(
        self, ids: list[int], *, for_update: bool
    ) -> dict[int, ExecutionLedger]:
        """Exact-id read for the #1175 quarantine tool (sees quarantined rows)."""
        stmt = (
            select(ExecutionLedger)
            .where(ExecutionLedger.id.in_(ids))
            .order_by(ExecutionLedger.id.asc())
            .execution_options(populate_existing=True)
        )
        if for_update:
            stmt = stmt.with_for_update()
        rows = (await self.db.execute(stmt)).scalars().all()
        return {int(row.id): row for row in rows}

    async def mark_quarantined(
        self, ids: list[int], *, at: datetime, reason: str, actor: str
    ) -> int:
        """#1175: the one UPDATE of ledger rows. Returns the touched row count.

        Guarded so it can only ever set the three quarantine columns on a
        still-unquarantined KIS websocket row; the DB CHECKs and permanence
        trigger enforce the same independently.
        """
        result = await self.db.execute(
            update(ExecutionLedger)
            .where(ExecutionLedger.id.in_(ids))
            .where(ExecutionLedger.quarantined_at.is_(None))
            .where(ExecutionLedger.source == "websocket")
            .where(ExecutionLedger.broker == "kis")
            .values(
                quarantined_at=at,
                quarantine_reason=reason,
                quarantined_by=actor,
            )
            .execution_options(synchronize_session=False)
        )
        return int(getattr(result, "rowcount", -1))

    async def append_quarantine_events(self, events: list[dict[str, Any]]) -> None:
        """#1175: append-only audit rows, one per quarantined ledger row."""
        await self.db.execute(insert(ExecutionLedgerQuarantineEvent), events)

    def record_run(self, run: ReconcileRunRecord) -> None:
        self.db.add(ExecutionLedgerReconcileRun(**run.model_dump()))

    async def latest_run_per_broker(self) -> dict[str, ReconcileRunRecord]:
        # Dry-run audit rows are persisted for observability but commit no
        # fills, so they must not make ledger freshness look "fresh".
        latest_started = (
            select(
                ExecutionLedgerReconcileRun.broker,
                func.max(ExecutionLedgerReconcileRun.started_at).label("started_at"),
            )
            .where(ExecutionLedgerReconcileRun.error_summary.is_(None))
            .where(ExecutionLedgerReconcileRun.dry_run.is_(False))
            .group_by(ExecutionLedgerReconcileRun.broker)
            .subquery()
        )
        rows = await self.db.execute(
            select(ExecutionLedgerReconcileRun)
            .join(
                latest_started,
                (ExecutionLedgerReconcileRun.broker == latest_started.c.broker)
                & (
                    ExecutionLedgerReconcileRun.started_at
                    == latest_started.c.started_at
                ),
            )
            .where(ExecutionLedgerReconcileRun.dry_run.is_(False))
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

    async def list_recent_fills_for_triage(
        self,
        *,
        after_id: int | None = None,
        market: str | None = None,
        side: str | None = None,
        source: str | None = "websocket",
        broker: str | None = None,
        account_mode: str | None = None,
        limit: int = 50,
    ) -> list[ExecutionLedger]:
        """Return fills newer than ``after_id`` for fill-event auto-triage (ROB-755).

        Defaults to ``source='websocket'`` so triagers don't accidentally ingest
        reconciler/manual_import backfills; pass ``source=None`` explicitly to
        override and read every source. ``limit`` is clamped to the [1, 500]
        range to keep pollers safe against bad input.
        """
        stmt = select(ExecutionLedger).where(execution_ledger_in_effect())
        if after_id is not None:
            stmt = stmt.where(ExecutionLedger.id > after_id)
        if side is not None:
            stmt = stmt.where(ExecutionLedger.side == side)
        if source is not None:
            stmt = stmt.where(ExecutionLedger.source == source)
        if broker is not None:
            stmt = stmt.where(ExecutionLedger.broker == broker)
        if account_mode is not None:
            stmt = stmt.where(ExecutionLedger.account_mode == account_mode)
        stmt = self.apply_market_filter(stmt, market)
        clamped_limit = max(1, min(int(limit), 500))
        stmt = stmt.order_by(ExecutionLedger.id.asc()).limit(clamped_limit)
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def max_ledger_id(self) -> int:
        """Return the current ledger high-water mark without reading fills."""
        result = await self.db.execute(select(func.max(ExecutionLedger.id)))
        value = result.scalar_one()
        return int(value or 0)

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
            .where(execution_ledger_in_effect())
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
            (
                broker,
                account_mode,
                venue,
                str(instrument_type),
                symbol,
                currency,
            ): Decimal(str(net_qty))
            for broker, account_mode, venue, instrument_type, symbol, currency, net_qty in rows.all()
        }

    async def position_before_fill(
        self,
        *,
        broker: str,
        account_mode: str,
        venue: str,
        instrument_type: Any,
        symbol: str,
        currency: str,
        filled_at: datetime,
        ledger_id: int,
    ) -> tuple[Decimal, int]:
        """Return ``(qty_before, rows_before)`` strictly before one fill.

        This is the position-fact read for the fill-handoff kick filter
        (task #825): the net signed quantity over every ledger row sharing the
        fill's exact match key, ordered strictly before it by
        ``(filled_at, id)``.  All sources count — unlike
        ``net_quantity_by_match_key_since`` this includes ``manual_import``
        opening-lot seeds because they carry the pre-ledger position, and the
        cutover/source filters are deliberately absent.  ``rows_before`` is
        the coverage witness: zero rows means the ledger has never seen the
        key, so the caller must treat the pre-fill position as unproven rather
        than flat.
        """
        from sqlalchemy import case

        signed_qty = case(
            (ExecutionLedger.side == "buy", ExecutionLedger.filled_qty),
            else_=-ExecutionLedger.filled_qty,
        )
        before_boundary = or_(
            ExecutionLedger.filled_at < filled_at,
            and_(
                ExecutionLedger.filled_at == filled_at,
                ExecutionLedger.id < ledger_id,
            ),
        )
        result = await self.db.execute(
            select(
                func.coalesce(func.sum(signed_qty), 0),
                func.count(ExecutionLedger.id),
            ).where(
                ExecutionLedger.broker == broker,
                ExecutionLedger.account_mode == account_mode,
                ExecutionLedger.venue == venue,
                ExecutionLedger.instrument_type == instrument_type,
                ExecutionLedger.symbol == symbol,
                ExecutionLedger.currency == currency,
                before_boundary,
                execution_ledger_in_effect(),
            )
        )
        qty_before, rows_before = result.one()
        return Decimal(str(qty_before)), int(rows_before)

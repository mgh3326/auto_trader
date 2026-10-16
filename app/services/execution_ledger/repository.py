"""Repository write/read primitives for the broker execution ledger (ROB-211)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
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
from app.services.execution_ledger.supersede import (
    AUTHORITATIVE_SOURCES,
    PROVISIONAL_SOURCE,
    normalize_order_id,
    supersede_provisional,
    terminal_cover,
)

logger = logging.getLogger(__name__)

UpsertStatus = Literal["inserted", "updated", "unchanged"]

#: ROB-1379: the widest gap between a KIS order's stamp (the reconciler books
#: ``ord_dt``/``ord_tmd``) and a fill of it. A day order lives inside one KST
#: date / one US trading date, and the order identity carries that date, so two
#: days of look-back always reaches the authoritative row of an in-window fill.
SUPERSEDE_LOOKBACK = timedelta(days=2)

#: Columns a quantity reader needs to sum rows and apply the ROB-1379
#: supersede rule (``supersede.order_identity`` reads these attributes).
_QUANTITY_COLUMNS = (
    ExecutionLedger.id,
    ExecutionLedger.broker,
    ExecutionLedger.account_mode,
    ExecutionLedger.venue,
    ExecutionLedger.instrument_type,
    ExecutionLedger.symbol,
    ExecutionLedger.currency,
    ExecutionLedger.side,
    ExecutionLedger.broker_order_id,
    ExecutionLedger.filled_at,
    ExecutionLedger.filled_qty,
    ExecutionLedger.source,
)


def _aware_utc(when: datetime) -> datetime:
    return when if when.tzinfo else when.replace(tzinfo=UTC)


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

    async def terminal_authoritative_cover(
        self, fill: ExecutionLedgerUpsert
    ) -> ExecutionLedger | None:
        """ROB-1379: the reconciler row that already books a websocket fill.

        Candidates share the fill's broker, account mode, instrument, side and
        leading-zero-normalized order id; ``supersede.terminal_cover`` then
        demands the same full order identity (symbol, currency, trading day)
        and broker proof that the order can fill no further.
        """
        if fill.source != PROVISIONAL_SOURCE:
            return None
        order_id = normalize_order_id(fill.broker_order_id)
        result = await self.db.execute(
            select(ExecutionLedger).where(
                ExecutionLedger.broker == fill.broker,
                ExecutionLedger.account_mode == fill.account_mode,
                ExecutionLedger.instrument_type == str(fill.instrument_type),
                ExecutionLedger.side == fill.side,
                func.ltrim(ExecutionLedger.broker_order_id, "0")
                == order_id.lstrip("0"),
                ExecutionLedger.source.in_(sorted(AUTHORITATIVE_SOURCES)),
                execution_ledger_in_effect(),
            )
        )
        return terminal_cover(fill, result.scalars().all())

    async def upsert_fill(
        self, fill: ExecutionLedgerUpsert
    ) -> tuple[UpsertStatus, int]:
        """Insert or update one fill by the broker idempotency key.

        ROB-1379: a *new* websocket fill (no row under its own key) whose order
        the reconciler already booked as terminal is not inserted a second
        time under a different ``fill_seq``; it answers ``unchanged`` with the
        authoritative row id. A replay of an existing key keeps its own row.
        """
        status = await self.classify_fill(fill)
        if status == "inserted" and fill.source == PROVISIONAL_SOURCE:
            cover = await self.terminal_authoritative_cover(fill)
            if cover is not None:
                logger.info(
                    "Websocket fill already booked by a terminal reconciler row; "
                    "not inserted: broker=%s order_id=%s fill_seq=%s row_id=%s",
                    fill.broker,
                    fill.broker_order_id,
                    fill.fill_seq,
                    cover.id,
                )
                return "unchanged", int(cover.id)
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
        still-unquarantined KIS websocket row; the DB CHECKs and the terminal
        trigger enforce the same independently. Once set, the DB refuses any
        further change to or removal of the row.
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
        """Signed net quantity per match key over non-seed rows since ``cutover``.

        ROB-1379: a websocket row whose order an authoritative row books is a
        duplicate of that row and is not added (``supersede``). Authority is
        read from ``SUPERSEDE_LOOKBACK`` before the cutover so a reconciler row
        stamped at order time still covers a websocket fill stamped later.
        """
        rows = (
            await self.db.execute(
                select(*_QUANTITY_COLUMNS).where(
                    ExecutionLedger.filled_at >= cutover - SUPERSEDE_LOOKBACK,
                    execution_ledger_in_effect(),
                )
            )
        ).all()
        since = _aware_utc(cutover)
        in_window = [
            row
            for row in rows
            if _aware_utc(row.filled_at) >= since and row.source != "manual_import"
        ]
        counted, _superseded = supersede_provisional(in_window, authority=rows)
        net: dict[tuple[str, str, str, str, str, str], Decimal] = {}
        for row in counted:
            key = (
                row.broker,
                row.account_mode,
                row.venue,
                str(row.instrument_type),
                row.symbol,
                row.currency,
            )
            qty = Decimal(str(row.filled_qty))
            net[key] = net.get(key, Decimal("0")) + (qty if row.side == "buy" else -qty)
        return net

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

        ROB-1379: a websocket row whose order an authoritative row before the
        boundary books is the same execution and is not added to
        ``qty_before`` (it still counts in ``rows_before``: the key was seen).
        """
        before_boundary = or_(
            ExecutionLedger.filled_at < filled_at,
            and_(
                ExecutionLedger.filled_at == filled_at,
                ExecutionLedger.id < ledger_id,
            ),
        )
        rows = (
            await self.db.execute(
                select(*_QUANTITY_COLUMNS).where(
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
        ).all()
        counted, _superseded = supersede_provisional(rows)
        qty_before = Decimal("0")
        for row in counted:
            qty = Decimal(str(row.filled_qty))
            qty_before += qty if row.side == "buy" else -qty
        return qty_before, len(rows)

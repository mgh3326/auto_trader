"""Market events repository — only place that writes the three market_events tables (ROB-128).

Idempotency strategy:
* When `source_event_id` is provided, upsert keyed by (source, category, market, source_event_id).
* Otherwise upsert keyed by (source, category, market, symbol, event_date, fiscal_year, fiscal_quarter).
Both keys are enforced by partial unique indexes (see migration).

Values are upserted by (event_id, metric_name, period).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.market_events import (
    MarketEvent,
    MarketEventIngestionPartition,
    MarketEventValue,
)
from app.services.alpaca_paper_ledger_service import _redact_sensitive_keys


class MarketEventsRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def upsert_event_with_values(
        self,
        event_data: dict[str, Any],
        values: list[dict[str, Any]],
    ) -> MarketEvent:
        payload = dict(event_data)
        if payload.get("raw_payload_json") is not None:
            payload["raw_payload_json"] = _redact_sensitive_keys(payload["raw_payload_json"])

        natural_keys = (
            "source",
            "category",
            "market",
            "symbol",
            "event_date",
            "fiscal_year",
            "fiscal_quarter",
        )
        update_columns = {
            k: payload.get(k)
            for k in (
                "country",
                "company_name",
                "title",
                "release_time_utc",
                "release_time_local",
                "source_timezone",
                "time_hint",
                "importance",
                "status",
                "source_url",
                "raw_payload_json",
                "fetched_at",
            )
            if k in payload
        }

        if payload.get("source_event_id"):
            stmt = (
                pg_insert(MarketEvent.__table__)
                .values(**payload)
                .on_conflict_do_update(
                    index_elements=["source", "category", "market", "source_event_id"],
                    index_where=MarketEvent.__table__.c.source_event_id.isnot(None),
                    set_=update_columns,
                )
                .returning(MarketEvent.__table__.c.id)
            )
        else:
            stmt = (
                pg_insert(MarketEvent.__table__)
                .values(**payload)
                .on_conflict_do_update(
                    index_elements=list(natural_keys),
                    index_where=MarketEvent.__table__.c.source_event_id.is_(None),
                    set_=update_columns,
                )
                .returning(MarketEvent.__table__.c.id)
            )

        result = await self.db.execute(stmt)
        event_id = result.scalar_one()

        for value in values:
            await self._upsert_value(event_id, value)

        await self.db.flush()
        event = (
            await self.db.execute(
                select(MarketEvent).where(MarketEvent.id == event_id)
            )
        ).scalar_one()
        return event

    async def _upsert_value(self, event_id: int, value: dict[str, Any]) -> None:
        payload = {**value, "event_id": event_id}
        update_columns = {
            k: payload.get(k)
            for k in (
                "actual",
                "forecast",
                "previous",
                "revised_previous",
                "unit",
                "surprise",
                "surprise_pct",
                "released_at",
            )
            if k in payload
        }
        stmt = (
            pg_insert(MarketEventValue.__table__)
            .values(**payload)
            .on_conflict_do_update(
                constraint="uq_market_event_values_event_metric_period",
                set_=update_columns,
            )
        )
        await self.db.execute(stmt)

    # -- partition state ----------------------------------------------------

    async def get_or_create_partition(
        self,
        *,
        source: str,
        category: str,
        market: str,
        partition_date: date,
    ) -> MarketEventIngestionPartition:
        existing = (
            await self.db.execute(
                select(MarketEventIngestionPartition).where(
                    MarketEventIngestionPartition.source == source,
                    MarketEventIngestionPartition.category == category,
                    MarketEventIngestionPartition.market == market,
                    MarketEventIngestionPartition.partition_date == partition_date,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing

        row = MarketEventIngestionPartition(
            source=source,
            category=category,
            market=market,
            partition_date=partition_date,
            status="pending",
        )
        self.db.add(row)
        await self.db.flush()
        return row

    async def mark_partition_running(
        self, partition: MarketEventIngestionPartition
    ) -> None:
        partition.status = "running"
        partition.started_at = datetime.now(UTC)
        partition.last_error = None
        await self.db.flush()

    async def mark_partition_succeeded(
        self,
        partition: MarketEventIngestionPartition,
        *,
        event_count: int,
    ) -> None:
        partition.status = "succeeded"
        partition.event_count = event_count
        partition.finished_at = datetime.now(UTC)
        partition.last_error = None
        await self.db.flush()

    async def mark_partition_failed(
        self,
        partition: MarketEventIngestionPartition,
        *,
        error: str,
    ) -> None:
        partition.status = "failed"
        partition.finished_at = datetime.now(UTC)
        partition.last_error = error[:2000]
        partition.retry_count = (partition.retry_count or 0) + 1
        await self.db.flush()

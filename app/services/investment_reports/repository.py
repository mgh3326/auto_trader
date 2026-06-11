"""ROB-265 — Thin SQLAlchemy DAO over the five investment_* tables.

The repository contains no business logic. Validation, idempotency
composition, and status transitions live in the business services
(``ingestion``, ``decisions``, ``watch_activation``, ``query_service``).

Pattern matches ``app/services/watch_order_intent_service.py`` —
class-based with an injected ``AsyncSession``. The repository flushes
when it has to update an attached row but never commits; callers own
the transaction boundary.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.investment_reports import (
    InvestmentReport,
    InvestmentReportItem,
    InvestmentReportItemDecision,
    InvestmentReportNewsCitation,
    InvestmentReportNewsFetchRun,
    InvestmentWatchAlert,
    InvestmentWatchEvent,
)


class InvestmentReportsRepository:
    """DAO over investment_reports / items / decisions / alerts / events."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ------------------------------------------------------------------
    # Reports
    # ------------------------------------------------------------------
    async def insert_report(self, **fields: Any) -> InvestmentReport:
        row = InvestmentReport(**fields)
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return row

    async def get_report_by_id(self, report_id: int) -> InvestmentReport | None:
        return await self._session.scalar(
            sa.select(InvestmentReport).where(InvestmentReport.id == report_id)
        )

    async def get_report_by_uuid(self, report_uuid: UUID) -> InvestmentReport | None:
        return await self._session.scalar(
            sa.select(InvestmentReport).where(
                InvestmentReport.report_uuid == report_uuid
            )
        )

    async def get_report_by_uuid_for_update(
        self, report_uuid: UUID
    ) -> InvestmentReport | None:
        return await self._session.scalar(
            sa.select(InvestmentReport)
            .where(InvestmentReport.report_uuid == report_uuid)
            .with_for_update()
        )

    async def get_report_by_idempotency_key(
        self, idempotency_key: str
    ) -> InvestmentReport | None:
        return await self._session.scalar(
            sa.select(InvestmentReport).where(
                InvestmentReport.idempotency_key == idempotency_key
            )
        )

    async def list_reports(
        self,
        *,
        market: str | None = None,
        market_session: str | None = None,
        account_scope: str | None = None,
        status: str | None = None,
        report_type: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[InvestmentReport]:
        stmt = sa.select(InvestmentReport).order_by(
            InvestmentReport.created_at.desc(), InvestmentReport.id.desc()
        )
        stmt = self._apply_report_filters(
            stmt,
            market=market,
            market_session=market_session,
            account_scope=account_scope,
            status=status,
            report_type=report_type,
        )
        if offset:
            stmt = stmt.offset(offset)
        stmt = stmt.limit(limit)
        result = await self._session.scalars(stmt)
        return list(result.all())

    async def latest_report(
        self,
        *,
        market: str | None = None,
        market_session: str | None = None,
        account_scope: str | None = None,
        status: str | None = None,
        report_type: str | None = None,
    ) -> InvestmentReport | None:
        stmt = sa.select(InvestmentReport).order_by(
            InvestmentReport.created_at.desc(), InvestmentReport.id.desc()
        )
        stmt = self._apply_report_filters(
            stmt,
            market=market,
            market_session=market_session,
            account_scope=account_scope,
            status=status,
            report_type=report_type,
        )
        return await self._session.scalar(stmt.limit(1))

    async def update_report(self, report_id: int, **fields: Any) -> None:
        """ROB-352 — update an existing report row in place (overwrite path).

        Keeps ``report_uuid`` / ``idempotency_key`` stable. The caller owns the
        transaction; this flushes but never commits.
        """
        if not fields:
            return
        await self._session.execute(
            sa.update(InvestmentReport)
            .where(InvestmentReport.id == report_id)
            .values(**fields)
        )
        await self._session.flush()

    @staticmethod
    def _apply_report_filters(
        stmt: sa.Select,
        *,
        market: str | None,
        market_session: str | None,
        account_scope: str | None,
        status: str | None,
        report_type: str | None,
    ) -> sa.Select:
        if market is not None:
            stmt = stmt.where(InvestmentReport.market == market)
        if market_session is not None:
            stmt = stmt.where(InvestmentReport.market_session == market_session)
        if account_scope is not None:
            stmt = stmt.where(InvestmentReport.account_scope == account_scope)
        if status is not None:
            stmt = stmt.where(InvestmentReport.status == status)
        if report_type is not None:
            stmt = stmt.where(InvestmentReport.report_type == report_type)
        return stmt

    # ------------------------------------------------------------------
    # Items
    # ------------------------------------------------------------------
    async def insert_item(self, **fields: Any) -> InvestmentReportItem:
        row = InvestmentReportItem(**fields)
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return row

    async def get_item_by_uuid(self, item_uuid: UUID) -> InvestmentReportItem | None:
        return await self._session.scalar(
            sa.select(InvestmentReportItem).where(
                InvestmentReportItem.item_uuid == item_uuid
            )
        )

    async def get_item_by_idempotency_key(
        self, idempotency_key: str
    ) -> InvestmentReportItem | None:
        return await self._session.scalar(
            sa.select(InvestmentReportItem).where(
                InvestmentReportItem.idempotency_key == idempotency_key
            )
        )

    async def find_item_by_report_client_key(
        self, report_id: int, client_item_key: str
    ) -> InvestmentReportItem | None:
        items = await self.list_items_for_report(report_id)
        for item in items:
            metadata = (
                item.item_metadata if isinstance(item.item_metadata, dict) else {}
            )
            if metadata.get("client_item_key") == client_item_key:
                return item
        return None

    async def list_items_for_report(self, report_id: int) -> list[InvestmentReportItem]:
        result = await self._session.scalars(
            sa.select(InvestmentReportItem)
            .where(InvestmentReportItem.report_id == report_id)
            .order_by(InvestmentReportItem.created_at.asc())
        )
        return list(result.all())

    async def update_item_status(self, item_id: int, status: str) -> None:
        await self._session.execute(
            sa.update(InvestmentReportItem)
            .where(InvestmentReportItem.id == item_id)
            .values(status=status)
        )

    async def update_item_watch_condition(
        self,
        item_id: int,
        watch_condition: dict | None,
        valid_until: datetime | None,
    ) -> None:
        """ROB-393 — persist a watch_condition / valid_until injected at
        activation time onto a review-watch item. Only non-None values are
        written; a None field is left unchanged. Flushes but never commits
        (caller owns the transaction)."""
        values: dict[str, Any] = {}
        if watch_condition is not None:
            values["watch_condition"] = watch_condition
        if valid_until is not None:
            values["valid_until"] = valid_until
        if not values:
            return
        await self._session.execute(
            sa.update(InvestmentReportItem)
            .where(InvestmentReportItem.id == item_id)
            .values(**values)
        )

    async def update_item_watch_recommendation(
        self, item_id: int, watch_recommendation: dict
    ) -> None:
        """ROB-337 — persist the advisory watch_recommendation JSONB onto an
        item. Flushes but never commits (caller owns the transaction)."""
        await self._session.execute(
            sa.update(InvestmentReportItem)
            .where(InvestmentReportItem.id == item_id)
            .values(watch_recommendation=watch_recommendation)
        )

    async def delete_items_for_report(self, report_id: int) -> None:
        """ROB-352 — remove every item of one report (overwrite path).

        Used only by the ingestion service's explicit-overwrite branch. The
        caller owns the transaction; this flushes but never commits.
        """
        await self._session.execute(
            sa.delete(InvestmentReportItem).where(
                InvestmentReportItem.report_id == report_id
            )
        )
        await self._session.flush()

    # ------------------------------------------------------------------
    # Decisions
    # ------------------------------------------------------------------
    async def insert_decision(self, **fields: Any) -> InvestmentReportItemDecision:
        row = InvestmentReportItemDecision(**fields)
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return row

    async def get_decision_by_idempotency_key(
        self, idempotency_key: str
    ) -> InvestmentReportItemDecision | None:
        return await self._session.scalar(
            sa.select(InvestmentReportItemDecision).where(
                InvestmentReportItemDecision.idempotency_key == idempotency_key
            )
        )

    async def list_decisions_for_item(
        self, item_id: int
    ) -> list[InvestmentReportItemDecision]:
        result = await self._session.scalars(
            sa.select(InvestmentReportItemDecision)
            .where(InvestmentReportItemDecision.item_id == item_id)
            .order_by(InvestmentReportItemDecision.created_at.asc())
        )
        return list(result.all())

    # ------------------------------------------------------------------
    # Alerts
    # ------------------------------------------------------------------
    async def insert_alert(self, **fields: Any) -> InvestmentWatchAlert:
        row = InvestmentWatchAlert(**fields)
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return row

    async def update_alert_metadata(self, alert_id: int, metadata: dict) -> None:
        """ROB-337 — replace an alert's alert_metadata JSONB (caller merges).

        Used by the validity review job to persist a ``last_review`` block.
        Does NOT touch status / threshold / valid_until. Flushes via the
        caller's transaction; never commits."""
        await self._session.execute(
            sa.update(InvestmentWatchAlert)
            .where(InvestmentWatchAlert.id == alert_id)
            .values(alert_metadata=metadata)
        )

    async def get_alert_by_idempotency_key(
        self, idempotency_key: str
    ) -> InvestmentWatchAlert | None:
        return await self._session.scalar(
            sa.select(InvestmentWatchAlert).where(
                InvestmentWatchAlert.idempotency_key == idempotency_key
            )
        )

    async def list_active_alerts(
        self,
        *,
        market: str | None = None,
        symbol: str | None = None,
        valid_at: datetime | None = None,
        include_expired_status_rows: bool = False,
        limit: int = 100,
    ) -> list[InvestmentWatchAlert]:
        capped_limit = max(1, min(int(limit), 250))
        stmt = sa.select(InvestmentWatchAlert).where(
            InvestmentWatchAlert.status == "active"
        )
        if market is not None:
            stmt = stmt.where(InvestmentWatchAlert.market == market)
        if symbol is not None:
            stmt = stmt.where(InvestmentWatchAlert.symbol == symbol)
        if valid_at is not None and not include_expired_status_rows:
            stmt = stmt.where(InvestmentWatchAlert.valid_until > valid_at)
        stmt = stmt.order_by(InvestmentWatchAlert.activated_at.desc()).limit(
            capped_limit
        )
        result = await self._session.scalars(stmt)
        return list(result.all())

    async def list_alerts_for_source_reports(
        self, source_report_uuids: list[UUID], *, status: str | None = None
    ) -> list[InvestmentWatchAlert]:
        if not source_report_uuids:
            return []
        stmt = sa.select(InvestmentWatchAlert).where(
            InvestmentWatchAlert.source_report_uuid.in_(source_report_uuids)
        )
        if status is not None:
            stmt = stmt.where(InvestmentWatchAlert.status == status)
        stmt = stmt.order_by(InvestmentWatchAlert.activated_at.desc())
        result = await self._session.scalars(stmt)
        return list(result.all())

    async def update_alert_status(self, alert_id: int, status: str) -> None:
        await self._session.execute(
            sa.update(InvestmentWatchAlert)
            .where(InvestmentWatchAlert.id == alert_id)
            .values(status=status)
        )

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------
    async def insert_event(self, **fields: Any) -> InvestmentWatchEvent:
        row = InvestmentWatchEvent(**fields)
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return row

    async def get_event_by_idempotency_key(
        self, idempotency_key: str
    ) -> InvestmentWatchEvent | None:
        return await self._session.scalar(
            sa.select(InvestmentWatchEvent).where(
                InvestmentWatchEvent.idempotency_key == idempotency_key
            )
        )

    async def update_event_follow_up(
        self, event_id: int, *, follow_up_report_item_id: int
    ) -> None:
        """ROB-405 Slice E — link a watch event to its follow-up report item."""
        await self._session.execute(
            sa.update(InvestmentWatchEvent)
            .where(InvestmentWatchEvent.id == event_id)
            .values(follow_up_report_item_id=follow_up_report_item_id)
        )

    async def update_event_delivery(
        self,
        event_id: int,
        *,
        delivery_status: str,
        delivery_reason: str | None = None,
        delivered_at: datetime | None = None,
    ) -> None:
        """Record the outcome of a Hermes delivery attempt.

        Increments ``delivery_attempts`` atomically so concurrent scanner
        runs cannot lose count.
        """
        await self._session.execute(
            sa.update(InvestmentWatchEvent)
            .where(InvestmentWatchEvent.id == event_id)
            .values(
                delivery_status=delivery_status,
                delivery_reason=delivery_reason,
                delivered_at=delivered_at,
                delivery_attempts=InvestmentWatchEvent.delivery_attempts + 1,
            )
        )

    async def list_events_for_alert(
        self, alert_id: int, *, limit: int = 50
    ) -> list[InvestmentWatchEvent]:
        result = await self._session.scalars(
            sa.select(InvestmentWatchEvent)
            .where(InvestmentWatchEvent.alert_id == alert_id)
            .order_by(InvestmentWatchEvent.created_at.desc())
            .limit(limit)
        )
        return list(result.all())

    async def list_events_for_source_reports(
        self,
        source_report_uuids: list[UUID],
        *,
        since: datetime | None = None,
        limit: int = 50,
    ) -> list[InvestmentWatchEvent]:
        if not source_report_uuids:
            return []
        stmt = sa.select(InvestmentWatchEvent).where(
            InvestmentWatchEvent.source_report_uuid.in_(source_report_uuids)
        )
        if since is not None:
            stmt = stmt.where(InvestmentWatchEvent.created_at >= since)
        stmt = stmt.order_by(InvestmentWatchEvent.created_at.desc()).limit(limit)
        result = await self._session.scalars(stmt)
        return list(result.all())

    async def list_decisions_for_items(
        self,
        item_ids: list[int],
        *,
        limit: int = 100,
    ) -> list[InvestmentReportItemDecision]:
        if not item_ids:
            return []
        result = await self._session.scalars(
            sa.select(InvestmentReportItemDecision)
            .where(InvestmentReportItemDecision.item_id.in_(item_ids))
            .order_by(InvestmentReportItemDecision.created_at.desc())
            .limit(limit)
        )
        return list(result.all())

    async def list_items_for_report_ordered_by_id(
        self, report_id: int
    ) -> list[InvestmentReportItem]:
        """Insertion-order items (id.asc()). Use for composition-index mapping —
        ``created_at`` ties (single-transaction inserts share ``now()``) make
        the created_at-ordered query non-deterministic for this purpose."""
        result = await self._session.scalars(
            sa.select(InvestmentReportItem)
            .where(InvestmentReportItem.report_id == report_id)
            .order_by(InvestmentReportItem.id.asc())
        )
        return list(result.all())

    async def insert_news_fetch_run(
        self, **fields: Any
    ) -> InvestmentReportNewsFetchRun:
        row = InvestmentReportNewsFetchRun(**fields)
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return row

    async def insert_news_citation(self, **fields: Any) -> InvestmentReportNewsCitation:
        row = InvestmentReportNewsCitation(**fields)
        self._session.add(row)
        await self._session.flush()
        return row

    async def list_news_citations_for_report(
        self, report_uuid: UUID
    ) -> list[InvestmentReportNewsCitation]:
        result = await self._session.scalars(
            sa.select(InvestmentReportNewsCitation)
            .where(InvestmentReportNewsCitation.report_uuid == report_uuid)
            .order_by(InvestmentReportNewsCitation.id.asc())
        )
        return list(result.all())

    async def merge_report_unavailable_sources(
        self, report_id: int, extra: dict[str, Any]
    ) -> None:
        row = await self._session.get(InvestmentReport, report_id)
        if row is None:
            return
        merged = {**(row.unavailable_sources or {}), **extra}
        row.unavailable_sources = merged
        await self._session.flush()

"""Sole writer for research_reports / research_report_ingestion_runs (ROB-140).

Idempotency:
* Reports upsert on `dedup_key`. Returns True on insert, False on skip.
* Runs upsert on `run_uuid`. Returns the row.

Mutation policy: this is the ONLY module allowed to write these tables.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.research_reports import (
    ResearchReport,
    ResearchReportIngestionRun,
)


class ResearchReportsRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def upsert_report(self, payload: dict[str, Any]) -> bool:
        dedup_key = payload["dedup_key"]
        existing = (
            await self.db.execute(
                select(ResearchReport).where(ResearchReport.dedup_key == dedup_key)
            )
        ).scalar_one_or_none()
        if existing is not None:
            content_fields = (
                "report_type",
                "source",
                "source_report_id",
                "title",
                "category",
                "analyst",
                "published_at_text",
                "published_date",
                "published_at",
                "summary_text",
                "detail_url",
                "detail_title",
                "detail_subtitle",
                "detail_excerpt",
                "pdf_url",
                "pdf_filename",
                "pdf_sha256",
                "pdf_size_bytes",
                "pdf_page_count",
                "pdf_text_length",
                "symbol_candidates",
                "raw_text_policy",
                "attribution_publisher",
                "attribution_copyright_notice",
                "attribution_full_text_exported",
                "attribution_pdf_body_exported",
            )
            changed = any(
                getattr(existing, name) != payload.get(name) for name in content_fields
            )
            if changed:
                for name in content_fields:
                    setattr(existing, name, payload.get(name))
                existing.revised_at = datetime.now(UTC)
                await self.db.flush()
            return False
        payload.setdefault("first_received_at", datetime.now(UTC))
        row = ResearchReport(**payload)
        self.db.add(row)
        await self.db.flush()
        return True

    async def known_source_ids(self, *, source: str, category: str) -> set[str]:
        """Return provider IDs already stored for one source category."""
        result = await self.db.execute(
            select(ResearchReport.source_report_id).where(
                ResearchReport.source == source,
                ResearchReport.category == category,
                ResearchReport.source_report_id.is_not(None),
            )
        )
        return {str(value) for value in result.scalars().all()}

    async def get_or_create_ingestion_run(
        self,
        *,
        run_uuid: str,
        payload_version: str,
        source: str,
        started_at: datetime | None,
        finished_at: datetime | None,
        exported_at: datetime | None,
        report_count: int | None,
        errors: list | dict | None,
        flags: list | dict | None,
        copyright_notice: str | None,
    ) -> ResearchReportIngestionRun:
        existing = (
            await self.db.execute(
                select(ResearchReportIngestionRun).where(
                    ResearchReportIngestionRun.run_uuid == run_uuid
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing

        row = ResearchReportIngestionRun(
            run_uuid=run_uuid,
            payload_version=payload_version,
            source=source,
            started_at=started_at,
            finished_at=finished_at,
            exported_at=exported_at,
            report_count=report_count or 0,
            errors=errors,
            flags=flags,
            copyright_notice=copyright_notice,
        )
        self.db.add(row)
        await self.db.flush()
        return row

    async def update_run_counts(
        self,
        run: ResearchReportIngestionRun,
        *,
        inserted_count: int,
        skipped_count: int,
    ) -> None:
        run.inserted_count = inserted_count
        run.skipped_count = skipped_count
        await self.db.flush()

    async def mark_run_finished(
        self,
        run: ResearchReportIngestionRun,
        *,
        finished_at: datetime,
        errors: list | dict | None = None,
    ) -> None:
        run.finished_at = finished_at
        if errors is not None:
            run.errors = errors
        await self.db.flush()

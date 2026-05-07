"""Sole writer for research_reports / research_report_ingestion_runs (ROB-140).

Idempotency:
* Reports upsert on `dedup_key`. Returns True on insert, False on skip.
* Runs upsert on `run_uuid`. Returns the row.

Mutation policy: this is the ONLY module allowed to write these tables.
"""

from __future__ import annotations

from datetime import datetime
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
            return False
        row = ResearchReport(**payload)
        self.db.add(row)
        await self.db.flush()
        return True

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

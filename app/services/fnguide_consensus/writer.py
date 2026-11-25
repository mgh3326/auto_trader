"""The only writer of ``fnguide_*`` tables (hk 6508). Insert-only.

A snapshot is keyed by (symbol, page, observed_date). The snapshot insert uses
``ON CONFLICT DO NOTHING ... RETURNING id``; when it returns nothing the same
day's snapshot already exists and no child row is inserted, so a same-day rerun
writes zero rows. Each page is one unit of work: the caller owns the transaction
and commits per page.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.fnguide import FnguideReport, FnguideSnapshot, FnguideValue
from app.services.fnguide_consensus.parser import ParsedPage

_CHUNK = 500


@dataclass(frozen=True)
class WriteOutcome:
    symbol: str
    page: str
    observed_date: str
    inserted: bool
    value_rows: int
    report_rows: int


def snapshot_values(parsed: ParsedPage) -> dict[str, object]:
    return {
        "symbol": parsed.symbol,
        "page": parsed.page,
        "observed_date": parsed.observed_date,
        "fetched_at": parsed.fetched_at,
        "source_url": parsed.source_url,
        "page_title": parsed.page_title,
        "company_name": parsed.company_name,
        "status": parsed.status,
        "reason": parsed.reason,
        "headers": parsed.headers,
        "raw_embedded": parsed.raw_embedded,
        "raw_reason": parsed.raw_reason,
        "value_count": parsed.value_count,
        "report_count": parsed.report_count,
        "skipped_items": parsed.skipped_items,
        "unparsed_blocks": parsed.unparsed_blocks,
        "duplicate_keys": parsed.duplicate_keys,
    }


class FnguideSnapshotWriter:
    """Single writer. Construct with the caller's session; never commits by itself."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def write_page(self, parsed: ParsedPage) -> WriteOutcome:
        stmt = (
            pg_insert(FnguideSnapshot)
            .values(**snapshot_values(parsed))
            .on_conflict_do_nothing(constraint="uq_fnguide_snapshots_symbol_page_date")
            .returning(FnguideSnapshot.id)
        )
        snapshot_id = (await self._session.execute(stmt)).scalar_one_or_none()
        outcome = WriteOutcome(
            symbol=parsed.symbol,
            page=parsed.page,
            observed_date=parsed.observed_date.isoformat(),
            inserted=snapshot_id is not None,
            value_rows=0,
            report_rows=0,
        )
        if snapshot_id is None:
            return outcome

        value_payload = [
            {
                "snapshot_id": snapshot_id,
                "section": row.section,
                "item_name": row.item_name,
                "item_code": row.item_code,
                "item_qualifier": row.item_qualifier,
                "column_key": row.column_key,
                "column_label": row.column_label,
                "value_text": row.value_text,
                "value_reason": row.value_reason,
                "unit": row.unit,
                "digit": row.digit,
            }
            for row in parsed.values
        ]
        for start in range(0, len(value_payload), _CHUNK):
            chunk = value_payload[start : start + _CHUNK]
            await self._session.execute(
                pg_insert(FnguideValue)
                .values(chunk)
                .on_conflict_do_nothing(constraint="uq_fnguide_values_natural")
            )

        report_payload = [
            {
                "snapshot_id": snapshot_id,
                "symbol": parsed.symbol,
                "observed_date": parsed.observed_date,
                "report_hash": row.report_hash,
                "report_date": row.report_date,
                "broker": row.broker,
                "analyst": row.analyst,
                "title": row.title,
                "source_report_id": row.source_report_id,
            }
            for row in parsed.reports
        ]
        for start in range(0, len(report_payload), _CHUNK):
            chunk = report_payload[start : start + _CHUNK]
            await self._session.execute(
                pg_insert(FnguideReport)
                .values(chunk)
                .on_conflict_do_nothing(constraint="uq_fnguide_reports_natural")
            )

        return WriteOutcome(
            symbol=parsed.symbol,
            page=parsed.page,
            observed_date=parsed.observed_date.isoformat(),
            inserted=True,
            value_rows=len(value_payload),
            report_rows=len(report_payload),
        )


__all__ = ["FnguideSnapshotWriter", "WriteOutcome", "snapshot_values"]

"""Service layer for ROB-637 analysis artifact persistence."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from app.core.timezone import KST, now_kst
from app.models.analysis_artifact import AnalysisArtifact
from app.schemas.analysis_artifact import (
    AnalysisArtifactKindLiteral,
    AnalysisArtifactReadinessLiteral,
    AnalysisArtifactSave,
)
from app.schemas.investment_reports import MarketLiteral

# Per-kind default freshness horizon in KST calendar days from as_of. Every kind
# has a concrete horizon so an omitted valid_until never yields a never-stale
# artifact (NULL=never-stale problem, ROB-648). Price/screen-derived kinds
# expire at the end of the as_of trading day; session summaries and briefings
# carry to the end of the next day so a morning-after review still sees them.
_DEFAULT_TTL_DAYS_BY_KIND: dict[str, int] = {
    "screening_ranking": 0,
    "profit_taking_verdicts": 0,
    "support_resistance_map": 0,
    "flow_assessment": 0,
    "candidate_pool": 0,
    "session_summary": 1,
    "briefing": 1,
}
_DEFAULT_TTL_DAYS_FALLBACK = 0


def compute_content_hash(payload: dict[str, Any] | None) -> str:
    """Canonical sha256 over the payload JSON.

    Sorted keys + ``ensure_ascii=False`` make the digest stable across
    re-serialization, so an identical payload hashes equal and drives the
    ``action='unchanged'`` no-op (ROB-648; mirrors
    ``symbol_report_ingest.content_hash``).
    """
    blob = json.dumps(payload or {}, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def default_valid_until(kind: str, as_of: datetime) -> datetime:
    """Per-kind default ``valid_until`` when the caller omits it (ROB-648).

    End-of-KST-day (23:59:59) of ``as_of`` plus the per-kind horizon. A lenient
    proxy for '장마감' that sidesteps per-market close times and the
    created-after-close edge (end-of-day is always >= a same-day ``as_of``).
    """
    as_of_kst = as_of.astimezone(KST) if as_of.tzinfo else as_of.replace(tzinfo=KST)
    horizon = _DEFAULT_TTL_DAYS_BY_KIND.get(kind, _DEFAULT_TTL_DAYS_FALLBACK)
    day = (as_of_kst + timedelta(days=horizon)).date()
    return datetime(day.year, day.month, day.day, 23, 59, 59, tzinfo=KST)


class AnalysisArtifactService:
    """Writer and filtered reader for persisted analysis artifacts."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save(self, entry: AnalysisArtifactSave) -> tuple[AnalysisArtifact, str]:
        """Persist one artifact and return ``(row, action)``.

        Without ``correlation_id`` every call appends a new row (``created``).
        With ``correlation_id`` the call is idempotent (ROB-474
        trade_retrospectives pattern): the server hashes the canonical payload
        and compares to the stored row —

        * identical payload → no write, ``version`` preserved (``unchanged``);
        * changed payload → in-place update with ``version`` bumped (``updated``);
        * no prior row → insert at ``version`` 1 (``created``).

        ``valid_until`` defaults to a per-kind TTL when omitted so no artifact is
        ever never-stale, and ``content_hash`` is always server-computed (ROB-648).
        """
        content_hash = compute_content_hash(entry.payload)
        valid_until = (
            entry.valid_until
            if entry.valid_until is not None
            else default_valid_until(entry.kind, entry.as_of)
        )
        values: dict[str, Any] = {
            "market": entry.market,
            "kind": entry.kind,
            "title": entry.title,
            "symbols": entry.symbols,
            "payload": entry.payload,
            "as_of": entry.as_of,
            "valid_until": valid_until,
            "created_by": entry.created_by,
            "session_label": entry.session_label,
            "correlation_id": entry.correlation_id,
            "account_scope": entry.account_scope,
            "readiness_label": entry.readiness_label,
            "content_hash": content_hash,
            "version": 1,
        }
        if entry.correlation_id is None:
            row = AnalysisArtifact(**values)
            self._session.add(row)
            await self._session.flush()
            await self._session.refresh(row)
            return row, "created"

        existing = await self._session.scalar(
            sa.select(AnalysisArtifact).where(
                AnalysisArtifact.correlation_id == entry.correlation_id
            )
        )
        if existing is not None and existing.content_hash == content_hash:
            # Identical canonical payload → no-op. version and content stay put
            # (dedup: same content = reuse, not churn).
            return existing, "unchanged"

        # Changed content bumps in place; a legacy row with NULL content_hash
        # (pre-migration) also lands here and gets its hash backfilled.
        values["version"] = (existing.version + 1) if existing is not None else 1
        stmt = (
            pg_insert(AnalysisArtifact)
            .values(**values)
            .on_conflict_do_update(
                # index_elements (not constraint=) so both a UNIQUE
                # constraint (fresh create_all) and a plain unique index
                # (patched-in on pre-existing DBs) satisfy the arbiter.
                index_elements=[AnalysisArtifact.correlation_id],
                set_={
                    key: value
                    for key, value in values.items()
                    if key != "correlation_id"
                },
            )
            .returning(AnalysisArtifact)
        )
        result = await self._session.scalars(
            stmt,
            execution_options={"populate_existing": True},
        )
        row = result.one()
        return row, "updated" if existing is not None else "created"

    async def list_artifacts(
        self,
        *,
        market: MarketLiteral | None = None,
        kind: AnalysisArtifactKindLiteral | None = None,
        symbol: str | None = None,
        since: datetime | None = None,
        include_stale: bool = False,
        limit: int = 20,
        correlation_id: str | None = None,
        account_scope: str | None = None,
        readiness_label: AnalysisArtifactReadinessLiteral | None = None,
    ) -> list[AnalysisArtifact]:
        """Query artifacts with filters, newest ``as_of`` first."""
        capped_limit = max(1, min(int(limit), 100))
        stmt = (
            sa.select(AnalysisArtifact)
            .options(defer(AnalysisArtifact.payload))
            .order_by(
                AnalysisArtifact.as_of.desc(),
                AnalysisArtifact.id.desc(),
            )
        )
        if market is not None:
            stmt = stmt.where(AnalysisArtifact.market == market)
        if kind is not None:
            stmt = stmt.where(AnalysisArtifact.kind == kind)
        if symbol:
            from app.core.symbol import to_db_symbol

            normalized_symbol = to_db_symbol(symbol.strip())
            stmt = stmt.where(
                AnalysisArtifact.symbols.op("@>")(
                    sa.text(":symbol").bindparams(
                        sa.bindparam("symbol", value=[normalized_symbol]),
                    )
                )
            )
        if since is not None:
            stmt = stmt.where(AnalysisArtifact.as_of >= since)
        if correlation_id is not None:
            stmt = stmt.where(AnalysisArtifact.correlation_id == correlation_id)
        if account_scope is not None:
            stmt = stmt.where(AnalysisArtifact.account_scope == account_scope)
        if readiness_label is not None:
            stmt = stmt.where(AnalysisArtifact.readiness_label == readiness_label)
        if not include_stale:
            now = now_kst()
            stmt = stmt.where(
                (AnalysisArtifact.valid_until.is_(None))
                | (AnalysisArtifact.valid_until >= now)
            )
        result = await self._session.scalars(stmt.limit(capped_limit))
        return list(result.all())

    async def fresh_artifacts_for_symbols(
        self,
        *,
        symbols: list[str],
        market: MarketLiteral | None = None,
        limit: int = 50,
    ) -> list[AnalysisArtifact]:
        """Non-stale artifacts overlapping any of ``symbols``, newest first.

        Powers the ``analyze_stock_batch`` ``fresh_artifact_exists`` hint
        (ROB-648) — a soft advisory, never a gate. Uses the Postgres ``&&``
        array-overlap operator so one query covers the whole batch.
        """
        from app.core.symbol import to_db_symbol

        normalized = sorted(
            {to_db_symbol(s.strip()) for s in symbols if s and s.strip()}
        )
        if not normalized:
            return []
        now = now_kst()
        stmt = (
            sa.select(AnalysisArtifact)
            .options(defer(AnalysisArtifact.payload))
            .where(
                AnalysisArtifact.symbols.op("&&")(
                    sa.text(":fresh_symbols").bindparams(
                        sa.bindparam("fresh_symbols", value=normalized),
                    )
                ),
                (AnalysisArtifact.valid_until.is_(None))
                | (AnalysisArtifact.valid_until >= now),
            )
            .order_by(AnalysisArtifact.as_of.desc(), AnalysisArtifact.id.desc())
        )
        if market is not None:
            stmt = stmt.where(AnalysisArtifact.market == market)
        result = await self._session.scalars(stmt.limit(max(1, min(int(limit), 100))))
        return list(result.all())

    async def get(
        self,
        artifact_id: int | str,
    ) -> AnalysisArtifact | None:
        """Return a single artifact by id or artifact_uuid, or None."""
        if isinstance(artifact_id, str):
            try:
                numeric_id = int(artifact_id)
                stmt = sa.select(AnalysisArtifact).where(
                    AnalysisArtifact.id == numeric_id
                )
            except ValueError:
                from uuid import UUID

                try:
                    parsed_uuid = UUID(artifact_id)
                except ValueError:
                    return None
                stmt = sa.select(AnalysisArtifact).where(
                    AnalysisArtifact.artifact_uuid == parsed_uuid
                )
        else:
            stmt = sa.select(AnalysisArtifact).where(AnalysisArtifact.id == artifact_id)
        result = await self._session.scalars(stmt)
        return result.first()


# Re-exported for callers that want a stable UTC "now" for as_of defaults.
def utc_now() -> datetime:
    """Return a timezone-aware UTC now (matches DB TIMESTAMPTZ semantics)."""
    return datetime.now(tz=UTC)

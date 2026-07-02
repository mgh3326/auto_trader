"""Service layer for ROB-637 analysis artifact persistence."""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import now_kst
from app.models.analysis_artifact import AnalysisArtifact
from app.schemas.analysis_artifact import (
    AnalysisArtifactKindLiteral,
    AnalysisArtifactSave,
)
from app.schemas.investment_reports import MarketLiteral


class AnalysisArtifactService:
    """Writer and filtered reader for persisted analysis artifacts."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save(self, entry: AnalysisArtifactSave) -> AnalysisArtifact:
        """Insert a single artifact row and return the refreshed row."""
        row = AnalysisArtifact(
            market=entry.market,
            kind=entry.kind,
            title=entry.title,
            symbols=entry.symbols,
            payload=entry.payload,
            as_of=entry.as_of,
            valid_until=entry.valid_until,
            created_by=entry.created_by,
            session_label=entry.session_label,
        )
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return row

    async def list_artifacts(
        self,
        *,
        market: MarketLiteral | None = None,
        kind: AnalysisArtifactKindLiteral | None = None,
        symbol: str | None = None,
        since: datetime | None = None,
        include_stale: bool = False,
        limit: int = 20,
    ) -> list[AnalysisArtifact]:
        """Query artifacts with filters, newest ``as_of`` first."""
        capped_limit = max(1, min(int(limit), 100))
        stmt = sa.select(AnalysisArtifact).order_by(
            AnalysisArtifact.as_of.desc(),
            AnalysisArtifact.id.desc(),
        )
        if market is not None:
            stmt = stmt.where(AnalysisArtifact.market == market)
        if kind is not None:
            stmt = stmt.where(AnalysisArtifact.kind == kind)
        if symbol:
            stmt = stmt.where(
                AnalysisArtifact.symbols.op("@>")(
                    sa.text(":symbol").bindparams(
                        sa.bindparam("symbol", value=[symbol]),
                    )
                )
            )
        if since is not None:
            stmt = stmt.where(AnalysisArtifact.as_of >= since)
        if not include_stale:
            now = now_kst()
            stmt = stmt.where(
                (AnalysisArtifact.valid_until.is_(None))
                | (AnalysisArtifact.valid_until >= now)
            )
        result = await self._session.scalars(stmt.limit(capped_limit))
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
            stmt = sa.select(AnalysisArtifact).where(
                AnalysisArtifact.id == artifact_id
            )
        result = await self._session.scalars(stmt)
        return result.first()


# Re-exported for callers that want a stable UTC "now" for as_of defaults.
def utc_now() -> datetime:
    """Return a timezone-aware UTC now (matches DB TIMESTAMPTZ semantics)."""
    return datetime.now(tz=UTC)

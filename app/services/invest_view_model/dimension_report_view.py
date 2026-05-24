"""Read-only Korean view-model for dimension reports (ROB-306)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.investment_dimension_reports import InvestmentDimensionReport

_STANCE_KO = {"bullish": "강세", "neutral": "중립", "bearish": "약세"}


async def build_dimension_reports_view(
    session: AsyncSession, *, run_uuid: uuid.UUID, dimension: str | None
) -> dict[str, Any]:
    stmt = select(InvestmentDimensionReport).where(
        InvestmentDimensionReport.run_uuid == run_uuid
    )
    if dimension is not None:
        stmt = stmt.where(InvestmentDimensionReport.dimension == dimension)
    stmt = stmt.order_by(
        InvestmentDimensionReport.dimension,
        InvestmentDimensionReport.artifact_version.desc(),
    )
    rows = list((await session.execute(stmt)).scalars().all())
    return {
        "runUuid": str(run_uuid),
        "dimension": dimension,
        "reports": [
            {
                "dimension": r.dimension,
                "market": r.market,
                "symbol": r.symbol,
                "stance": r.stance,
                "stanceLabel": _STANCE_KO.get(r.stance or "", "-"),
                "confidence": r.confidence,
                "confidenceLabel": f"{r.confidence}%"
                if r.confidence is not None
                else "-",
                "reportText": r.report_text,
                "keyFindings": r.key_findings or [],
                "signals": r.signals or {},
                "freshness": r.freshness_summary or {},
                "artifactVersion": r.artifact_version,
            }
            for r in rows
        ],
    }

"""ROB-715 — item→forecast/retrospective exact-join batch loaders.

Given report-item UUIDs, return each item's own ``trade_forecasts`` and
``trade_retrospectives`` rows (exact join on the ``report_item_uuid`` Text
column), projected for the /invest audit surface. One batched query per table;
items with no rows are absent from the returned dict. Read-only — no broker,
order, watch, or order-intent mutation is reachable. This module deliberately
does NOT import ``app.services.decision_history`` (ROB-717 ownership).
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.review import TradeForecast, TradeRetrospective
from app.schemas.investment_reports import (
    ForecastLinkResponse,
    RetrospectiveLinkResponse,
)


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None


def _project_forecast(row: TradeForecast) -> ForecastLinkResponse:
    target = row.forecast_target or {}
    price = target.get("target_price")
    return ForecastLinkResponse(
        forecast_id=str(row.forecast_id),
        status=row.status,
        outcome=row.outcome,
        review_date=_iso(row.review_date),
        direction=target.get("direction"),
        target_price=float(price) if price is not None else None,
        probability=float(row.probability),
        brier_score=(float(row.brier_score) if row.brier_score is not None else None),
        resolution_source=row.resolution_source,
        correlation_id=row.correlation_id,
    )


def _project_retrospective(row: TradeRetrospective) -> RetrospectiveLinkResponse:
    return RetrospectiveLinkResponse(
        retrospective_id=row.id,
        outcome=row.outcome,
        lesson=row.lesson,
        result_summary=row.result_summary,
        root_cause_class=row.root_cause_class,
        trigger_type=row.trigger_type,
        pnl_pct=float(row.pnl_pct) if row.pnl_pct is not None else None,
        created_at=_iso(row.created_at),
        correlation_id=row.correlation_id,
    )


async def list_forecasts_for_item_uuids(
    db: AsyncSession, item_uuids: Sequence[UUID]
) -> dict[str, list[ForecastLinkResponse]]:
    keys = [str(u) for u in item_uuids]
    grouped: dict[str, list[ForecastLinkResponse]] = {}
    if not keys:
        return grouped
    rows = (
        (
            await db.execute(
                select(TradeForecast)
                .where(TradeForecast.report_item_uuid.in_(keys))
                .order_by(TradeForecast.id.desc())
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        grouped.setdefault(row.report_item_uuid, []).append(_project_forecast(row))
    return grouped


async def list_retrospectives_for_item_uuids(
    db: AsyncSession, item_uuids: Sequence[UUID]
) -> dict[str, list[RetrospectiveLinkResponse]]:
    keys = [str(u) for u in item_uuids]
    grouped: dict[str, list[RetrospectiveLinkResponse]] = {}
    if not keys:
        return grouped
    rows = (
        (
            await db.execute(
                select(TradeRetrospective)
                .where(TradeRetrospective.report_item_uuid.in_(keys))
                .order_by(TradeRetrospective.id.desc())
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        grouped.setdefault(row.report_item_uuid, []).append(_project_retrospective(row))
    return grouped

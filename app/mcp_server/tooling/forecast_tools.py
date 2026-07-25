# app/mcp_server/tooling/forecast_tools.py
"""ROB-650 — MCP tools for the resolvable forecast ledger."""

from __future__ import annotations

import logging
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import AsyncSessionLocal
from app.services.trade_journal.forecast_service import (
    ForecastValidationError,
    build_forecast_calibration_aggregate,
    list_due_forecasts,
    list_due_quarantined_forecasts,
    list_forecasts,
    resolve_forecast,
    save_forecast,
    serialize_forecast,
)

logger = logging.getLogger(__name__)


def _session_factory() -> async_sessionmaker[AsyncSession]:
    return cast(async_sessionmaker[AsyncSession], cast(object, AsyncSessionLocal))


async def forecast_save(
    created_by: str,
    symbol: str,
    instrument_type: str,
    forecast_target: dict,
    probability: float,
    review_date: str,
    forecast_id: str | None = None,
    horizon: str | None = None,
    probability_range_low: float | None = None,
    probability_range_high: float | None = None,
    evidence_ids: list | None = None,
    contrary_evidence: str | None = None,
    forecast_start_date: str | None = None,
    resolution_source: str | None = None,
    session_label: str | None = None,
    model_label: str | None = None,
    policy_version: str | None = None,
    artifact_uuid: str | None = None,
    journal_id: int | None = None,
    report_uuid: str | None = None,
    report_item_uuid: str | None = None,
    correlation_id: str | None = None,
) -> dict[str, Any]:
    symbol = (symbol or "").strip()
    if not symbol:
        return {"success": False, "error": "symbol is required"}
    try:
        async with _session_factory()() as db:
            action, row = await save_forecast(
                db,
                created_by=created_by,
                symbol=symbol,
                instrument_type=instrument_type,
                forecast_target=forecast_target,
                probability=probability,
                review_date=review_date,
                forecast_id=forecast_id,
                horizon=horizon,
                probability_range_low=probability_range_low,
                probability_range_high=probability_range_high,
                evidence_ids=evidence_ids,
                contrary_evidence=contrary_evidence,
                forecast_start_date=forecast_start_date,
                resolution_source=resolution_source,
                session_label=session_label,
                model_label=model_label,
                policy_version=policy_version,
                artifact_uuid=artifact_uuid,
                journal_id=journal_id,
                report_uuid=report_uuid,
                report_item_uuid=report_item_uuid,
                correlation_id=correlation_id,
            )
            await db.commit()
            await db.refresh(row)
            return {
                "success": True,
                "action": action,
                "data": serialize_forecast(row),
            }
    except ForecastValidationError as exc:
        return {"success": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        logger.exception("forecast_save failed")
        return {"success": False, "error": f"forecast_save failed: {exc}"}


async def forecast_resolve(
    forecast_id: str | None = None,
    dry_run: bool = True,
    manual_outcome: bool | None = None,
    manual_observed_value: float | None = None,
    manual_evidence: Any | None = None,
    limit: int = 25,
    backfill_missing: bool = True,
) -> dict[str, Any]:

    persist = not dry_run
    try:
        async with _session_factory()() as db:
            if forecast_id:
                result = await resolve_forecast(
                    db,
                    forecast_id=forecast_id,
                    persist=persist,
                    manual_outcome=manual_outcome,
                    manual_observed_value=manual_observed_value,
                    manual_evidence=manual_evidence,
                    backfill_missing=backfill_missing,
                )

                if persist and result.get("changed"):
                    await db.commit()
                return {"success": True, "dry_run": dry_run, "mode": "single", **result}

            # Batch: resolve every due (review_date reached) open forecast.
            if manual_outcome is not None or manual_evidence is not None:
                return {
                    "success": False,
                    "error": "manual resolution requires an explicit forecast_id",
                }
            quarantined = await list_due_quarantined_forecasts(db, limit=limit)
            due = await list_due_forecasts(db, limit=limit)
            results: list[dict[str, Any]] = []
            changed_any = False
            for row in [*quarantined, *due]:
                r = await resolve_forecast(
                    db,
                    forecast_id=row.forecast_id,
                    persist=persist,
                    backfill_missing=backfill_missing,
                )

                changed_any = changed_any or bool(r.get("changed"))
                item: dict[str, Any] = {
                    "forecast_id": str(row.forecast_id),
                    "symbol": row.symbol,
                    "status": r["status"],
                    "changed": bool(r.get("changed")),
                    "auto_close": bool(r.get("auto_close")),
                    "computed": r.get("computed"),
                    "reason": r.get("reason"),
                }
                if r.get("resolution_evidence") is not None:
                    item["resolution_evidence"] = r["resolution_evidence"]
                results.append(item)
            if persist and changed_any:
                await db.commit()
            by_status: dict[str, int] = {}
            for r in results:
                by_status[r["status"]] = by_status.get(r["status"], 0) + 1
            return {
                "success": True,
                "dry_run": dry_run,
                "mode": "due_batch",
                "due_count": len(due),
                "quarantined_count": len(quarantined),
                "by_status": by_status,
                "results": results,
            }
    except ForecastValidationError as exc:
        return {"success": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        logger.exception("forecast_resolve failed")
        return {"success": False, "error": f"forecast_resolve failed: {exc}"}


async def get_forecasts(
    status: str | None = None,
    symbol: str | None = None,
    created_by: str | None = None,
    correlation_id: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    try:
        async with _session_factory()() as db:
            result = await list_forecasts(
                db,
                status=status,
                symbol=symbol,
                created_by=created_by,
                correlation_id=correlation_id,
                limit=limit,
            )
        return {"success": True, **result}
    except Exception as exc:  # noqa: BLE001
        logger.exception("get_forecasts failed")
        return {"success": False, "error": f"get_forecasts failed: {exc}"}


async def get_forecast_calibration(
    group_by: str = "created_by",
    created_by: str | None = None,
    symbol: str | None = None,
    instrument_type: str | None = None,
    days: int | None = None,
) -> dict[str, Any]:
    try:
        async with _session_factory()() as db:
            result = await build_forecast_calibration_aggregate(
                db,
                group_by=group_by,
                created_by=created_by,
                symbol=symbol,
                instrument_type=instrument_type,
                days=days,
            )
        return {"success": True, **result}
    except Exception as exc:  # noqa: BLE001
        logger.exception("get_forecast_calibration failed")
        return {
            "success": False,
            "error": f"get_forecast_calibration failed: {exc}",
        }

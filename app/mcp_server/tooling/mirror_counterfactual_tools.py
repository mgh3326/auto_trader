from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID

from app.core.db import AsyncSessionLocal
from app.services.trade_journal.mirror_counterfactual import execute_mirror_for_report


async def kis_mock_mirror_execute_report(
    report_uuid: str,
    dry_run: bool = True,
    min_rung_quantity: float = 1.0,
    confirm: bool = False,
) -> dict[str, Any]:
    try:
        rid = UUID(str(report_uuid))
    except ValueError:
        return {
            "success": False,
            "error": "invalid_report_uuid",
            "report_uuid": report_uuid,
        }

    if not dry_run and not confirm:
        return {
            "success": False,
            "status": "blocked",
            "error": "dry_run=False requires confirm=True for mirror execution",
            "error_code": "mirror_confirm_required",
            "report_uuid": report_uuid,
        }

    async with AsyncSessionLocal() as db:
        try:
            result = await execute_mirror_for_report(
                db,
                report_uuid=rid,
                dry_run=dry_run,
                min_rung_quantity=Decimal(str(min_rung_quantity)),
            )
            if not dry_run and result.get("success") is True:
                await db.commit()
            else:
                await db.rollback()
            return result
        except ValueError as exc:
            return {"success": False, "error": str(exc), "report_uuid": report_uuid}

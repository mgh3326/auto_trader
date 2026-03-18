"""n8n scan API endpoints — strategy scan and crash detection."""

from __future__ import annotations

import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.core.timezone import now_kst
from app.jobs.daily_scan import DailyScanner
from app.schemas.n8n_scan import (
    N8nCrashScanDetails,
    N8nCrashScanResponse,
    N8nStrategyScanDetails,
    N8nStrategyScanResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/n8n/scan", tags=["n8n-scan"])


@router.get("/strategy", response_model=N8nStrategyScanResponse)
async def strategy_scan() -> N8nStrategyScanResponse | JSONResponse:
    """Run crypto strategy scan (overbought/oversold/SMA20/F&G)."""
    as_of = now_kst().replace(microsecond=0).isoformat()
    scanner = DailyScanner(alert_mode="none")
    try:
        result = await scanner.run_strategy_scan()
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to run strategy scan")
        payload = N8nStrategyScanResponse(
            success=False,
            as_of=as_of,
            errors=[{"error": str(exc)}],
        )
        return JSONResponse(status_code=500, content=payload.model_dump())
    finally:
        await scanner.close()

    if result.get("skipped"):
        return N8nStrategyScanResponse(
            success=True,
            as_of=as_of,
            alerts_sent=0,
            message=f"Skipped: {result.get('reason', 'unknown')}",
        )

    details_raw = result.get("details", {})
    return N8nStrategyScanResponse(
        success=True,
        as_of=as_of,
        alerts_sent=result.get("alerts_sent", 0),
        message=result.get("message", ""),
        details=N8nStrategyScanDetails(**details_raw),
    )


@router.get("/crash", response_model=N8nCrashScanResponse)
async def crash_scan() -> N8nCrashScanResponse | JSONResponse:
    """Run crash detection scan (rapid price movements)."""
    as_of = now_kst().replace(microsecond=0).isoformat()
    scanner = DailyScanner(alert_mode="none")
    try:
        result = await scanner.run_crash_detection()
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to run crash detection scan")
        payload = N8nCrashScanResponse(
            success=False,
            as_of=as_of,
            errors=[{"error": str(exc)}],
        )
        return JSONResponse(status_code=500, content=payload.model_dump())
    finally:
        await scanner.close()

    if result.get("skipped"):
        return N8nCrashScanResponse(
            success=True,
            as_of=as_of,
            alerts_sent=0,
            message=f"Skipped: {result.get('reason', 'unknown')}",
        )

    details_raw = result.get("details", {})
    return N8nCrashScanResponse(
        success=True,
        as_of=as_of,
        alerts_sent=result.get("alerts_sent", 0),
        message=result.get("message", ""),
        details=N8nCrashScanDetails(**details_raw),
    )

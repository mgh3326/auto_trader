"""Phase 2 — Prefect wrapper for the daily demo scalping review + benchmark.

Importable only; NO deployment registered here. Recurrence lives in
robin-prefect-automations (paused-by-default). Default-OFF via
``settings.binance_demo_scalping_review_flow_enabled`` (enforced in the job).
All logic is in app/jobs/binance_demo_scalping_review.py (prefect-free).
"""

from __future__ import annotations

from typing import Any

from prefect import flow, task

from app.jobs.binance_demo_scalping_review import run_demo_scalping_review_refresh


@task(name="binance_demo_scalping_review")
async def binance_demo_scalping_review_task() -> dict[str, Any]:
    return await run_demo_scalping_review_refresh()


@flow(name="binance_demo_scalping_review")
async def binance_demo_scalping_review_flow() -> dict[str, Any]:
    """Daily review + buy&hold benchmark; deployment registration deferred."""
    return await binance_demo_scalping_review_task()

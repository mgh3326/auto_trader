"""ROB-26 — Taskiq cron tasks for read-only research-run refresh.

Each task is a thin wrapper around run_research_run_refresh(); all
guard logic (disabled, no-operator, outside-hours, no-run) lives in
the orchestrator, not here.
"""

from __future__ import annotations

from app.core.taskiq_broker import broker
from app.jobs.research_run_refresh_runner import run_research_run_refresh

_KST = "Asia/Seoul"


@broker.task(
    task_name="research_run.kr_preopen_refresh",
    schedule=[{"cron": "10 8 * * 1-5", "cron_offset": _KST}],
)
async def kr_preopen_research_refresh() -> dict:
    return await run_research_run_refresh(stage="preopen", market_scope="kr")


@broker.task(
    task_name="research_run.kr_regular_open_refresh",
    schedule=[{"cron": "3 9 * * 1-5", "cron_offset": _KST}],
)
async def kr_regular_open_live_refresh() -> dict:
    return await run_research_run_refresh(stage="preopen", market_scope="kr")


@broker.task(
    task_name="research_run.nxt_aftermarket_refresh_1545",
    schedule=[{"cron": "45 15 * * 1-5", "cron_offset": _KST}],
)
async def nxt_aftermarket_refresh_1545() -> dict:
    return await run_research_run_refresh(stage="nxt_aftermarket", market_scope="kr")


@broker.task(
    task_name="research_run.nxt_aftermarket_refresh_1630",
    schedule=[{"cron": "30 16 * * 1-5", "cron_offset": _KST}],
)
async def nxt_aftermarket_refresh_1630() -> dict:
    return await run_research_run_refresh(stage="nxt_aftermarket", market_scope="kr")


@broker.task(
    task_name="research_run.nxt_aftermarket_refresh_1730",
    schedule=[{"cron": "30 17 * * 1-5", "cron_offset": _KST}],
)
async def nxt_aftermarket_refresh_1730() -> dict:
    return await run_research_run_refresh(stage="nxt_aftermarket", market_scope="kr")


@broker.task(
    task_name="research_run.nxt_aftermarket_refresh_1830",
    schedule=[{"cron": "30 18 * * 1-5", "cron_offset": _KST}],
)
async def nxt_aftermarket_refresh_1830() -> dict:
    return await run_research_run_refresh(stage="nxt_aftermarket", market_scope="kr")


@broker.task(
    task_name="research_run.nxt_aftermarket_refresh_1930",
    schedule=[{"cron": "30 19 * * 1-5", "cron_offset": _KST}],
)
async def nxt_aftermarket_refresh_1930() -> dict:
    return await run_research_run_refresh(stage="nxt_aftermarket", market_scope="kr")


@broker.task(
    task_name="research_run.nxt_final_check_1955",
    schedule=[{"cron": "55 19 * * 1-5", "cron_offset": _KST}],
)
async def nxt_final_check_1955() -> dict:
    return await run_research_run_refresh(stage="nxt_aftermarket", market_scope="kr")

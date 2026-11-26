"""Default-off KR plan-only MCP surface."""

from datetime import UTC, date, datetime
from typing import Any

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.services.daily_plan import store
from app.services.trading_policy_service import load_trading_policy, policy_content_hash


async def daily_plan_write_version(session_date: date, request: dict[str, Any]) -> dict:
    """Validate and atomically publish one KR session plan version."""
    if not settings.daily_plan_enabled:
        return {"error_code": "daily_plan_disabled"}
    try:
        async with AsyncSessionLocal() as db, db.begin():
            return await store.write_version(
                db,
                day=session_date,
                request=request,
                now=datetime.now(UTC),
                policy=load_trading_policy(),
                policy_hash=policy_content_hash(),
            )
    except (KeyError, TypeError):
        return {"error_code": "invalid_plan_request"}
    except ValueError as error:
        code = str(error)
        return {
            "error_code": code
            if code.isascii() and code.isidentifier()
            else "invalid_plan_request"
        }


async def daily_plan_get(session_date: date, seq: int | None = None) -> dict:
    """Read the current or selected immutable publication folded by row key."""
    if not settings.daily_plan_enabled:
        return {"error_code": "daily_plan_disabled"}
    async with AsyncSessionLocal() as db:
        plan = await store.get_plan(
            db,
            day=session_date,
            seq=seq,
            now=datetime.now(UTC),
            max_carries=load_trading_policy().daily_plan.carry_max_sessions,
        )
    return plan if plan is not None else {"error_code": "plan_not_found"}


async def daily_plan_list(queue_for: date | None = None) -> dict:
    """List KR plan heads and the requested premarket reevaluation queue."""
    if not settings.daily_plan_enabled:
        return {"error_code": "daily_plan_disabled"}
    async with AsyncSessionLocal() as db:
        return await store.list_plans(
            db,
            now=datetime.now(UTC),
            max_carries=load_trading_policy().daily_plan.carry_max_sessions,
            queue_for=queue_for,
        )


async def daily_plan_confirm_links(session_date: date, seq: int) -> dict:
    """Resolve deterministic forecast IDs against committed declined decisions."""
    if not settings.daily_plan_enabled:
        return {"error_code": "daily_plan_disabled"}
    try:
        async with AsyncSessionLocal() as db, db.begin():
            return await store.confirm_links(
                db, day=session_date, seq=seq, now=datetime.now(UTC)
            )
    except ValueError:
        return {"error_code": "version_not_found"}


def register_daily_plan_tools(mcp: Any) -> None:
    mcp.tool()(daily_plan_write_version)
    mcp.tool()(daily_plan_confirm_links)
    mcp.tool()(daily_plan_get)
    mcp.tool()(daily_plan_list)

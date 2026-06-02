"""ROB-405 Slice D — read-only MCP tool: mock loop per-cycle retrospective."""

from __future__ import annotations

from typing import Any
from typing import cast as typing_cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import AsyncSessionLocal
from app.core.timezone import now_kst
from app.services.trade_journal.mock_loop_retrospective_service import (
    build_mock_loop_retrospective,
)

MOCK_LOOP_RETRO_TOOL_NAMES: set[str] = {"get_mock_loop_retrospective"}


def _session_factory() -> async_sessionmaker[AsyncSession]:
    return typing_cast(
        async_sessionmaker[AsyncSession], typing_cast(object, AsyncSessionLocal)
    )


def register_mock_loop_retro_tools(mcp: Any) -> None:
    @mcp.tool(
        name="get_mock_loop_retrospective",
        description=(
            "Per-cycle (KST date) retrospective for the mock autonomous loop: "
            "armed/triggered/filled/PnL/hit-miss + verdict + counterfactual "
            "aggregates over a KST date range. Read-only, mock accounts only."
        ),
    )
    async def get_mock_loop_retrospective(
        kst_date_from: str | None = None,
        kst_date_to: str | None = None,
        market: str | None = None,
    ) -> dict[str, Any]:
        today = now_kst().date().isoformat()
        date_from = kst_date_from or today
        date_to = kst_date_to or date_from
        async with _session_factory()() as db:
            cycles = await build_mock_loop_retrospective(
                db, kst_date_from=date_from, kst_date_to=date_to, market=market
            )
        return {
            "success": True,
            "kst_date_from": date_from,
            "kst_date_to": date_to,
            "market": market,
            "cycles": cycles,
        }


__all__ = ["MOCK_LOOP_RETRO_TOOL_NAMES", "register_mock_loop_retro_tools"]

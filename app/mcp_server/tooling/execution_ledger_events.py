"""ROB-755 — execution_ledger_fill_events_list_recent MCP read tool.

운영자 poller / 수동 조회용 read-only 툴.
``ExecutionLedgerRepository.list_recent_fills_for_triage`` 위에 얇은 래퍼를 얹고,
CLI (``scripts/list_recent_fill_events.py``)와 동일한 20-키 sanitized 출력 셰이프를
노출한다. raw_payload_json은 절대 출력에 포함되지 않는다 (보안 제약).

허용 source 값: ``websocket`` / ``reconciler`` / ``manual_import`` / ``None``
(``None`` = 모든 source). 그 외 값은 DB 도달 전에 ``invalid_source``로 거부.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from app.core.db import AsyncSessionLocal
from app.services.execution_ledger.fill_event_sanitizer import sanitize_fill
from app.services.execution_ledger.repository import ExecutionLedgerRepository

if TYPE_CHECKING:
    from fastmcp import FastMCP

VALID_SOURCES: frozenset[str | None] = frozenset(
    {None, "websocket", "reconciler", "manual_import"}
)
logger = logging.getLogger(__name__)


async def execution_ledger_fill_events_list_recent_impl(
    after_id: int | None = None,
    market: str | None = None,
    side: str | None = None,
    source: str | None = "websocket",
    broker: str | None = None,
    account_mode: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """최근 체결(fill) 이벤트 목록 조회 (운영자 poller/수동 조회용, read-only).

    Args:
        after_id: 워터마크 — 이 ID보다 큰 행만 반환. None이면 최근부터.
        market: kr|us|crypto 필터 (선택). ``None``이면 모든 market.
        side: buy|sell 필터 (선택). ``None``이면 모든 side.
        source: websocket|reconciler|manual_import|None. 기본 ``websocket`` —
            triager가 reconciler/manual_import backfill을 실수로 흡입하지 않도록.
            ``None`` 명시 시 모든 source.
        broker: kis|upbit 필터 (선택).
        account_mode: live|mock 필터 (선택).
        limit: 최대 row 수 (1..500, repo 단에서 clamp).

    Returns:
        ``{"success": True, "count": int, "fills": [sanitized_fill, ...]}`` 또는
        ``{"success": False, "error": str}`` (DB 오류 시). raw_payload_json은
        fills 안에 절대 포함되지 않는다.
    """
    if source not in VALID_SOURCES:
        # DB 도달 전 차단 — 잘못된 source는 운영자 poller 루프에서 즉시 reject되어야 한다.
        return {"success": False, "error": "invalid_source"}

    try:
        async with AsyncSessionLocal() as db:
            repo = ExecutionLedgerRepository(db)
            rows = await repo.list_recent_fills_for_triage(
                after_id=after_id,
                market=market,
                side=side,
                source=source,
                broker=broker,
                account_mode=account_mode,
                limit=limit,
            )
        return {
            "success": True,
            "count": len(rows),
            "fills": [sanitize_fill(row) for row in rows],
        }
    except Exception:  # noqa: BLE001 — 운영자/agent 표면은 모든 예외를 JSON으로 감싼다
        logger.exception("execution_ledger_fill_events_list_recent failed")
        return {"success": False, "error": "internal_error"}


def register_execution_ledger_event_tools(mcp: FastMCP) -> None:
    """Register read-only execution ledger fill event MCP tools (ROB-755)."""
    _ = mcp.tool(
        name="execution_ledger_fill_events_list_recent",
        description=(
            "최근 체결(fill) 이벤트 목록(운영자 poller/수동 조회용). "
            "after_id 워터마크 + market/side/source/broker/account_mode 필터 + "
            "limit(1..500). source 기본값 websocket. raw_payload 미노출. "
            "Read-only. 브로커/주문 mutation 없음."
        ),
    )(execution_ledger_fill_events_list_recent_impl)


__all__ = [
    "VALID_SOURCES",
    "execution_ledger_fill_events_list_recent_impl",
    "register_execution_ledger_event_tools",
]

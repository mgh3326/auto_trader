"""MCP tools for ROB-516 operator session context handoff."""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from app.core.db import AsyncSessionLocal
from app.schemas.session_context import (
    SessionContextAppendEntry,
    SessionContextAppendResponse,
    SessionContextRecentRequest,
    SessionContextRecentResponse,
    SessionContextResponse,
)
from app.services.session_context import SessionContextService


def _validation_error(exc: ValidationError) -> dict[str, Any]:
    return {
        "success": False,
        "error": "invalid_request",
        "detail": exc.errors(),
    }


async def session_context_append(
    entries: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Append one or more operator session context entries."""
    if not entries:
        return {
            "success": False,
            "error": "empty_entries",
            "hint": "Pass one or more session context entries.",
        }
    try:
        validated = [
            SessionContextAppendEntry.model_validate(entry) for entry in entries
        ]
    except ValidationError as exc:
        return _validation_error(exc)

    async with AsyncSessionLocal() as db:
        service = SessionContextService(db)
        rows = await service.append_entries(validated)
        await db.commit()
        response = SessionContextAppendResponse(
            count=len(rows),
            entries=[SessionContextResponse.model_validate(row) for row in rows],
        )
    return response.model_dump(mode="json")


async def session_context_get_recent(
    market: str | None = None,
    account_scope: str | None = None,
    kst_date_from: str | None = None,
    entry_type: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Return recent operator session context entries, newest first."""
    try:
        request = SessionContextRecentRequest.model_validate(
            {
                "market": market,
                "account_scope": account_scope,
                "kst_date_from": kst_date_from,
                "entry_type": entry_type,
                "limit": limit,
            }
        )
    except ValidationError as exc:
        return _validation_error(exc)

    async with AsyncSessionLocal() as db:
        service = SessionContextService(db)
        rows = await service.get_recent(
            market=request.market,
            account_scope=request.account_scope,
            kst_date_from=request.kst_date_from,
            entry_type=request.entry_type,
            limit=request.limit,
        )
        response = SessionContextRecentResponse(
            count=len(rows),
            filters=request,
            entries=[SessionContextResponse.model_validate(row) for row in rows],
        )
    return response.model_dump(mode="json")

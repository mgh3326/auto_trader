"""Pure URL helper for the Trading Decision Workspace SPA shell."""

from __future__ import annotations

from urllib.parse import quote
from uuid import UUID


def build_trading_decision_session_url(base_url: str, session_uuid: UUID) -> str:
    base = base_url.rstrip("/")
    return f"{base}/trading/decisions/sessions/{quote(str(session_uuid), safe='')}"


def resolve_trading_decision_base_url(
    *, configured: str | None, request_base_url: str
) -> str:
    if configured is not None and configured.strip():
        return configured.strip()
    return request_base_url

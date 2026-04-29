"""ROB-26 end-to-end smoke test — no-op skipped path, no DB writes."""
from __future__ import annotations

import re
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from app.jobs.research_run_refresh_runner import run_research_run_refresh

_SECRET_PATTERN = re.compile(r"(?i)(secret|token|password|sk-)")


class _FakeSession:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1

    async def close(self):
        pass


@asynccontextmanager
async def _fake_factory():
    session = _FakeSession()
    try:
        yield session
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_smoke_skipped_no_research_run(monkeypatch):
    from app.core.config import settings
    from app.services import research_run_decision_session_service as svc

    monkeypatch.setattr(settings, "research_run_refresh_enabled", True, raising=False)
    monkeypatch.setattr(settings, "research_run_refresh_user_id", 999, raising=False)
    monkeypatch.setattr(
        settings, "research_run_refresh_market_hours_only", False, raising=False
    )
    monkeypatch.setattr(
        svc,
        "resolve_research_run",
        AsyncMock(side_effect=svc.ResearchRunNotFound("smoke")),
    )

    captured: list[_FakeSession] = []

    @asynccontextmanager
    async def _capturing_factory():
        session = _FakeSession()
        captured.append(session)
        try:
            yield session
        finally:
            await session.close()

    result = await run_research_run_refresh(
        stage="preopen",
        market_scope="kr",
        db_factory=_capturing_factory,
    )

    # 1. Summary shape is correct
    assert result == {
        "status": "skipped",
        "reason": "no_research_run",
        "stage": "preopen",
        "market_scope": "kr",
        "research_run_uuid": None,
        "session_uuid": None,
        "proposal_count": 0,
        "reconciliation_count": 0,
        "refreshed_at": None,
        "warnings": [],
    }

    # 2. No DB commit on skip path
    assert captured[0].commits == 0

    # 3. No secret-shaped values in summary
    for value in result.values():
        if isinstance(value, str):
            assert not _SECRET_PATTERN.search(value), (
                f"Secret-shaped value in summary: {value!r}"
            )

"""ROB-26 orchestrator unit tests."""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.jobs.research_run_refresh_runner import _within_window, run_research_run_refresh


def test_preopen_window_includes_0810_weekday():
    # 2026-04-29 is a Wednesday
    assert _within_window(stage="preopen", now=datetime(2026, 4, 29, 8, 10)) is True


def test_preopen_window_excludes_weekend():
    # 2026-05-02 is a Saturday
    assert _within_window(stage="preopen", now=datetime(2026, 5, 2, 8, 10)) is False


def test_preopen_window_excludes_after_0930():
    assert _within_window(stage="preopen", now=datetime(2026, 4, 29, 9, 31)) is False


def test_nxt_window_includes_1545_and_1955():
    assert _within_window(stage="nxt_aftermarket", now=datetime(2026, 4, 29, 15, 45)) is True
    assert _within_window(stage="nxt_aftermarket", now=datetime(2026, 4, 29, 19, 55)) is True


def test_nxt_window_excludes_after_2030():
    assert _within_window(stage="nxt_aftermarket", now=datetime(2026, 4, 29, 20, 31)) is False


# ── Skip-path helpers ──────────────────────────────────────────────────────


class _FakeSession:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1

    async def close(self):
        self.closed = True


@asynccontextmanager
async def _fake_factory():
    session = _FakeSession()
    try:
        yield session
    finally:
        await session.close()


# ── Skip path tests ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_disabled_short_circuits(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "research_run_refresh_enabled", False, raising=False)
    monkeypatch.setattr(settings, "research_run_refresh_user_id", 123, raising=False)
    result = await run_research_run_refresh(
        stage="preopen", market_scope="kr", db_factory=_fake_factory
    )
    assert result["status"] == "disabled"
    assert result["reason"] == "research_run_refresh_disabled"


@pytest.mark.asyncio
async def test_no_operator_user_skips(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "research_run_refresh_enabled", True, raising=False)
    monkeypatch.setattr(settings, "research_run_refresh_user_id", None, raising=False)
    result = await run_research_run_refresh(
        stage="preopen", market_scope="kr", db_factory=_fake_factory
    )
    assert result["status"] == "skipped"
    assert result["reason"] == "no_operator_user_configured"


@pytest.mark.asyncio
async def test_outside_hours_skips(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "research_run_refresh_enabled", True, raising=False)
    monkeypatch.setattr(settings, "research_run_refresh_user_id", 1, raising=False)
    monkeypatch.setattr(settings, "research_run_refresh_market_hours_only", True, raising=False)
    # Saturday 08:10 — outside window because weekend
    result = await run_research_run_refresh(
        stage="preopen",
        market_scope="kr",
        db_factory=_fake_factory,
        now_local=lambda: datetime(2026, 5, 2, 8, 10),
    )
    assert result["status"] == "skipped"
    assert result["reason"] == "outside_trading_hours"


@pytest.mark.asyncio
async def test_no_run_returns_skipped(monkeypatch):
    from app.core.config import settings
    from app.services import research_run_decision_session_service as svc

    monkeypatch.setattr(settings, "research_run_refresh_enabled", True, raising=False)
    monkeypatch.setattr(settings, "research_run_refresh_user_id", 1, raising=False)
    monkeypatch.setattr(settings, "research_run_refresh_market_hours_only", False, raising=False)
    monkeypatch.setattr(
        svc,
        "resolve_research_run",
        AsyncMock(side_effect=svc.ResearchRunNotFound("none")),
    )
    result = await run_research_run_refresh(
        stage="preopen",
        market_scope="kr",
        db_factory=_fake_factory,
    )
    assert result["status"] == "skipped"
    assert result["reason"] == "no_research_run"


@pytest.mark.asyncio
async def test_happy_path_completed(monkeypatch):
    from uuid import uuid4

    from app.core.config import settings
    from app.services import (
        research_run_decision_session_service as svc,
        research_run_live_refresh_service as lrs,
    )

    monkeypatch.setattr(settings, "research_run_refresh_enabled", True, raising=False)
    monkeypatch.setattr(settings, "research_run_refresh_user_id", 1, raising=False)
    monkeypatch.setattr(settings, "research_run_refresh_market_hours_only", False, raising=False)

    run_uuid = uuid4()
    session_uuid = uuid4()

    mock_run = MagicMock()
    mock_run.run_uuid = run_uuid

    mock_snapshot = MagicMock()

    mock_result = MagicMock()
    mock_result.research_run = mock_run
    mock_result.session.session_uuid = session_uuid
    mock_result.proposal_count = 2
    mock_result.reconciliation_count = 1
    mock_result.refreshed_at = datetime(2026, 4, 29, 8, 10, tzinfo=UTC)
    mock_result.warnings = ("warn1",)

    monkeypatch.setattr(svc, "resolve_research_run", AsyncMock(return_value=mock_run))
    monkeypatch.setattr(lrs, "build_live_refresh_snapshot", AsyncMock(return_value=mock_snapshot))
    monkeypatch.setattr(
        svc, "create_decision_session_from_research_run", AsyncMock(return_value=mock_result)
    )

    captured_session: list[_FakeSession] = []

    @asynccontextmanager
    async def _capturing_factory():
        session = _FakeSession()
        captured_session.append(session)
        try:
            yield session
        finally:
            await session.close()

    result = await run_research_run_refresh(
        stage="preopen",
        market_scope="kr",
        db_factory=_capturing_factory,
    )

    assert result["status"] == "completed"
    assert result["proposal_count"] == 2
    assert result["session_uuid"] == str(session_uuid)
    assert captured_session[0].commits == 1

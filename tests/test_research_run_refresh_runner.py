"""ROB-26 orchestrator unit tests."""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from app.jobs.research_run_refresh_runner import _within_window


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

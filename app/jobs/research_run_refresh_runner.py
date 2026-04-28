"""ROB-26 — Read-only research-run refresh orchestrator.

Wraps the ROB-25 live-refresh / decision-session pipeline with deterministic,
read-only summary semantics. Never raises on operational skip conditions;
always returns a structured dict summary.

Scheduler-agnostic: no Taskiq imports here, so this function can be wrapped
by any scheduler (Taskiq cron tasks in app/tasks/research_run_refresh_tasks.py,
or a future Prefect @flow in a separate package).
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal, TypedDict

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.timezone import now_kst

logger = logging.getLogger(__name__)

StageLiteral = Literal["preopen", "nxt_aftermarket"]
MarketScopeLiteral = Literal["kr"]
StatusLiteral = Literal["completed", "disabled", "skipped", "error"]


class ResearchRunRefreshSummary(TypedDict, total=False):
    status: StatusLiteral
    reason: str
    stage: str
    market_scope: str
    research_run_uuid: str | None
    session_uuid: str | None
    proposal_count: int
    reconciliation_count: int
    refreshed_at: str | None
    warnings: list[str]


_KR_PREOPEN_WINDOW = ((8, 0), (9, 30))
_KR_NXT_WINDOW = ((15, 30), (20, 30))


def _within_window(*, stage: StageLiteral, now: datetime) -> bool:
    """Return True if `now` falls within the allowed trading window for `stage`."""
    weekday = now.weekday()  # Mon=0..Sun=6
    if weekday >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    if stage == "preopen":
        start = _KR_PREOPEN_WINDOW[0][0] * 60 + _KR_PREOPEN_WINDOW[0][1]
        end = _KR_PREOPEN_WINDOW[1][0] * 60 + _KR_PREOPEN_WINDOW[1][1]
    elif stage == "nxt_aftermarket":
        start = _KR_NXT_WINDOW[0][0] * 60 + _KR_NXT_WINDOW[0][1]
        end = _KR_NXT_WINDOW[1][0] * 60 + _KR_NXT_WINDOW[1][1]
    else:
        return False
    return start <= minutes <= end

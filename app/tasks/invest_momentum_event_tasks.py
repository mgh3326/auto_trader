"""TaskIQ wrappers for Naver momentum/theme event snapshot collection.

Recurring activation is deliberately double-gated:
- ``invest_momentum_events_scheduler_enabled`` registers the schedule.
- ``invest_momentum_events_commit_enabled`` allows DB writes.

Without the commit gate, scheduled/manual calls run as dry-runs and return a warning.
"""

from __future__ import annotations

from typing import Any

from app.core.config import settings
from app.core.taskiq_broker import broker
from app.jobs.invest_momentum_events import (
    NaverMomentumBuildRequest,
    run_naver_momentum_build,
)

_KST = "Asia/Seoul"


def _csv_tuple(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.split(",") if part.strip())


def _scheduled_naver_momentum_labels() -> list[dict[str, str]]:
    if not settings.invest_momentum_events_scheduler_enabled:
        return []
    return [
        {
            "cron": settings.invest_momentum_events_scheduler_cron,
            "cron_offset": _KST,
        }
    ]


async def _run_build_dict(
    *,
    trade_types: list[str] | None = None,
    order_types: list[str] | None = None,
    page_size: int | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    result = await run_naver_momentum_build(
        NaverMomentumBuildRequest(
            trade_types=tuple(
                trade_types
                or _csv_tuple(settings.invest_momentum_events_scheduler_trade_types)
            ),
            order_types=tuple(
                order_types
                or _csv_tuple(settings.invest_momentum_events_scheduler_order_types)
            ),
            page_size=page_size or settings.invest_momentum_events_scheduler_page_size,
            commit=commit,
        )
    )
    return {
        "momentumRows": result.momentum_rows,
        "themeRows": result.theme_rows,
        "committed": result.committed,
        "countsBySurface": result.counts_by_surface,
        "warnings": list(result.warnings),
        "samples": list(result.samples),
    }


@broker.task(task_name="invest_momentum_events.build")
async def build_invest_momentum_events(
    trade_types: list[str] | None = None,
    order_types: list[str] | None = None,
    page_size: int | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    """Manual TaskIQ entrypoint, dry-run by default."""
    return await _run_build_dict(
        trade_types=trade_types,
        order_types=order_types,
        page_size=page_size,
        commit=commit,
    )


@broker.task(
    task_name="invest_momentum_events.build_recurring",
    schedule=_scheduled_naver_momentum_labels(),
)
async def build_invest_momentum_events_recurring() -> dict[str, Any]:
    """Recurring KRX/NXT momentum collection.

    The scheduler calls with commit=True, but actual writes still require
    ``invest_momentum_events_commit_enabled=True`` in settings.
    """
    return await _run_build_dict(
        trade_types=list(
            _csv_tuple(settings.invest_momentum_events_scheduler_trade_types)
        ),
        order_types=list(
            _csv_tuple(settings.invest_momentum_events_scheduler_order_types)
        ),
        page_size=settings.invest_momentum_events_scheduler_page_size,
        commit=True,
    )

"""Mechanical loss-cut evaluation lever (ROB-1460).

Shipped scheduleless: this task has no ``schedule`` label and must never be
given one without a separate operator approval (CLAUDE.md hard rule 6). The
registration to request is in docs/runbooks/mechanical-loss-cut.md.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from app.core.taskiq_broker import broker
from app.services.mechanical_loss_cut.job import run_mechanical_loss_cut

logger = logging.getLogger(__name__)


@broker.task(task_name="mechanical_loss_cut.evaluate")
async def mechanical_loss_cut_evaluate_task() -> dict[str, Any]:
    try:
        return await run_mechanical_loss_cut(now=datetime.now(UTC))
    except Exception as exc:  # pragma: no cover - defensive logging path
        logger.error("mechanical loss-cut evaluation failed: %s", type(exc).__name__)
        return {"status": "failed", "error": type(exc).__name__}

"""Protected-position reconcile lever (#943).

Shipped scheduleless: this task has no ``schedule`` label and must never be
given one without a separate operator approval (CLAUDE.md hard rule 6).  It is
the manual counterpart of the fill hooks: it lowers every active declared P
that exceeds the fresh broker holding, and it is a no-op while
``protected_position_auto_follow_enabled`` is off.
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.taskiq_broker import broker
from app.services.protected_position_auto_follow import reconcile_declared_positions

logger = logging.getLogger(__name__)


@broker.task(task_name="protected_positions.auto_follow_reconcile")
async def protected_positions_auto_follow_reconcile_task(
    account_scope: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    try:
        return await reconcile_declared_positions(
            account_scope=account_scope, dry_run=dry_run
        )
    except Exception as exc:  # pragma: no cover - defensive logging path
        logger.error(
            "protected position reconcile lever failed: %s", type(exc).__name__
        )
        return {"status": "failed", "error": type(exc).__name__}

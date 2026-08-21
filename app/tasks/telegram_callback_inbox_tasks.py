"""W5 TaskIQ surface for the durable Telegram callback inbox.

Two tasks, both inert until an operator arms them:

``order_proposals.telegram_callback_job``
    processes one job. The Redis argument is a job UUID and the Redis result
    is that UUID plus an allowlisted status -- never a callback payload, a
    nonce, a chat/user/message id, or an exception string.

``order_proposals.telegram_callback_recovery``
    the safety net for a lost kick. Ships **scheduleless**: the ``schedule``
    label is computed at import and is ``[]`` while the gate is off, so
    registering it is an operator action plus a process restart, never a
    side effect of deploying this code.

Neither task opts into ``retry_on_error``. The broker installs
``SmartRetryMiddleware`` with ``default_retry_label=False``, so opting in
would put a *second*, uncoordinated retry authority in front of an
order-adjacent callback. The inbox's own state machine is the only one.
"""

from __future__ import annotations

from typing import Any

from app.core.config import settings
from app.core.taskiq_broker import broker
from app.services.order_proposals.callback_inbox.recovery import recover_callback_jobs
from app.services.order_proposals.callback_inbox.worker import process_callback_job


def recovery_schedule_labels() -> list[dict[str, str]]:
    """The cron label, or none at all while the gate is off."""
    if not settings.ORDER_PROPOSALS_TELEGRAM_CALLBACK_RECOVERY_SCHEDULE_ENABLED:
        return []
    return [
        {
            "cron": settings.ORDER_PROPOSALS_TELEGRAM_CALLBACK_RECOVERY_CRON,
            "cron_offset": "Asia/Seoul",
        }
    ]


@broker.task(task_name="order_proposals.telegram_callback_job")
async def run_telegram_callback_job(job_id: str) -> dict[str, str]:
    """Process one durable callback job.

    The gate is checked before anything else, so a disabled deployment does
    not open a database session, let alone reach the approval machinery.
    """
    if not settings.ORDER_PROPOSALS_TELEGRAM_CALLBACK_WORKER_ENABLED:
        return {"status": "disabled", "job_id": str(job_id)}
    result = await process_callback_job(job_id)
    # Re-project deliberately: only these two keys may reach the result
    # backend, whatever a future refactor decides to return internally.
    return {"status": str(result["status"]), "job_id": str(result["job_id"])}


@broker.task(
    task_name="order_proposals.telegram_callback_recovery",
    schedule=recovery_schedule_labels(),
)
async def recover_telegram_callback_jobs() -> dict[str, Any]:
    """Sweep for work a lost Redis kick left behind.

    Subordinate to the worker gate as well as its own: this task *executes*
    handlers, so arming the schedule alone must not start running
    order-adjacent callbacks.
    """
    if not settings.ORDER_PROPOSALS_TELEGRAM_CALLBACK_RECOVERY_SCHEDULE_ENABLED:
        return {"status": "disabled"}
    if not settings.ORDER_PROPOSALS_TELEGRAM_CALLBACK_WORKER_ENABLED:
        return {"status": "worker_disabled"}
    return await recover_callback_jobs()


__all__ = [
    "recover_telegram_callback_jobs",
    "recovery_schedule_labels",
    "run_telegram_callback_job",
]

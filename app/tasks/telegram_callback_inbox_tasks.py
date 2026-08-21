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
from app.services.order_proposals.callback_inbox.contracts import (
    RECOVERY_SCAN_LIMIT,
    recovery_scan_cap,
)
from app.services.order_proposals.callback_inbox.recovery import recover_callback_jobs
from app.services.order_proposals.callback_inbox.result_boundary import (
    canonical_job_id,
    disabled_job_result,
    error_job_result,
    invalid_job_id_result,
    project_job_result,
    project_recovery_report,
    recovery_error_result,
)
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
async def run_telegram_callback_job(job_id: object) -> dict[str, str]:
    """Process one durable callback job.

    Invalid wire values are rejected before the gate, so neither gate branch
    can reflect arbitrary TaskIQ input or open durable-inbox authority.
    """
    canonical_job_id_value = canonical_job_id(job_id)
    if canonical_job_id_value is None:
        return invalid_job_id_result()
    if not settings.ORDER_PROPOSALS_TELEGRAM_CALLBACK_WORKER_ENABLED:
        return disabled_job_result(canonical_job_id_value)
    try:
        result = await process_callback_job(canonical_job_id_value)
        projected = project_job_result(result, job_id=canonical_job_id_value)
    except Exception:  # noqa: BLE001 - TaskIQ result boundary; BaseException propagates
        return error_job_result(canonical_job_id_value)
    return projected or error_job_result(canonical_job_id_value)


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
    try:
        report = await recover_callback_jobs()
        projected = project_recovery_report(
            report,
            execution_limit=RECOVERY_SCAN_LIMIT,
            scan_cap=recovery_scan_cap(RECOVERY_SCAN_LIMIT),
        )
    except Exception:  # noqa: BLE001 - TaskIQ result boundary; BaseException propagates
        return recovery_error_result()
    return projected or recovery_error_result()


__all__ = [
    "recover_telegram_callback_jobs",
    "recovery_schedule_labels",
    "run_telegram_callback_job",
]

"""W5 TaskIQ surface for the durable Telegram callback inbox.

Two tasks, both inert until an operator arms them:

``order_proposals.telegram_callback_job``
    processes one job. The W5 application payload contains one canonical job
    UUID and the closed result projection contains only its allowlisted status
    fields -- never a callback payload, a nonce, a chat/user/message id, or an
    exception string. The producer wire shape is tested independently.

``order_proposals.telegram_callback_recovery``
    the safety net for a lost kick. Ships **scheduleless**: the ``schedule``
    label is computed at import and is ``[]`` while the gate is off, so
    registering it is an operator action plus a process restart, never a
    side effect of deploying this code.

    The task body reduces exact control exceptions to private category-only
    signals. The final W5 post-execute boundary raises a fresh safe exact
    control after Receiver's task-exception catch but before SmartRetry
    post-processing and result save; retry/save see nothing and no Receiver
    error log is emitted. Neither task opts into ``retry_on_error``. The broker installs
``SmartRetryMiddleware`` with ``default_retry_label=False``, so opting in
would put a *second*, uncoordinated retry authority in front of an
order-adjacent callback. The inbox's own state machine is the only one.
"""

from __future__ import annotations

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
from app.services.order_proposals.callback_inbox.taskiq_receiver_boundary import (
    CALLBACK_JOB_TASK_NAME,
    CALLBACK_RECOVERY_TASK_NAME,
    control_signal_for_exception,
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


@broker.task(task_name=CALLBACK_JOB_TASK_NAME)
async def run_telegram_callback_job(
    *wire_args: object, **wire_kwargs: object
) -> object:
    """Process one durable callback job.

    The exact envelope is one positional canonical UUID string and no keyword
    fields.  Validation precedes every gate and worker authority.
    """
    canonical_job_id_value = _canonical_job_wire(wire_args, wire_kwargs)
    if canonical_job_id_value is None:
        return invalid_job_id_result()
    if not settings.ORDER_PROPOSALS_TELEGRAM_CALLBACK_WORKER_ENABLED:
        return disabled_job_result(canonical_job_id_value)
    try:
        result = await process_callback_job(canonical_job_id_value)
        projected = project_job_result(result, job_id=canonical_job_id_value)
    except BaseException as exc:  # noqa: BLE001 - closed TaskIQ receiver boundary
        control_signal = control_signal_for_exception(exc)
        if control_signal is not None:
            return control_signal
        return error_job_result(canonical_job_id_value)
    return projected or error_job_result(canonical_job_id_value)


@broker.task(
    task_name=CALLBACK_RECOVERY_TASK_NAME,
    schedule=recovery_schedule_labels(),
)
async def recover_telegram_callback_jobs(
    *wire_args: object, **wire_kwargs: object
) -> object:
    """Sweep for work a lost Redis kick left behind.

    Subordinate to the worker gate as well as its own: this task *executes*
    handlers, so arming the schedule alone must not start running
    order-adjacent callbacks.  Its wire envelope is exactly empty.
    """
    if wire_args or wire_kwargs:
        return recovery_error_result()
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
    except BaseException as exc:  # noqa: BLE001 - closed TaskIQ receiver boundary
        control_signal = control_signal_for_exception(exc)
        if control_signal is not None:
            return control_signal
        return recovery_error_result()
    return projected or recovery_error_result()


def _canonical_job_wire(
    wire_args: tuple[object, ...], wire_kwargs: dict[str, object]
) -> str | None:
    """Validate the consumer's untrusted variadic envelope without coercion."""
    if len(wire_args) != 1 or wire_kwargs:
        return None
    return canonical_job_id(wire_args[0])


__all__ = [
    "recover_telegram_callback_jobs",
    "recovery_schedule_labels",
    "run_telegram_callback_job",
]

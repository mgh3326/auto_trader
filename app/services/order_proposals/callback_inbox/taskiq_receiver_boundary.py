"""Closed TaskIQ Receiver envelope boundary for the durable callback inbox.

TaskIQ logs a decoded message before middleware ``pre_execute`` hooks run.
The formatter therefore removes untrusted W5 envelope authority immediately
after a successful decode; the middleware repeats that same operation directly
before task execution.  The boundary is deliberately cold: it has no settings,
broker, worker, recovery, model, or database imports.
"""

from __future__ import annotations

import asyncio
from typing import Any

from taskiq.abc.formatter import TaskiqFormatter
from taskiq.abc.middleware import TaskiqMiddleware
from taskiq.message import BrokerMessage, TaskiqMessage
from taskiq.result import TaskiqResult

from app.services.order_proposals.callback_inbox.result_boundary import canonical_job_id

CALLBACK_JOB_TASK_NAME = "order_proposals.telegram_callback_job"
CALLBACK_RECOVERY_TASK_NAME = "order_proposals.telegram_callback_recovery"

_INVALID_JOB_WIRE_MARKER = "w5-invalid-job-envelope"
_INVALID_RECOVERY_WIRE_MARKER = "w5-invalid-recovery-envelope"


class _W5ControlSignal:
    """Private category-only handoff from a task body to its middleware."""

    __slots__ = ("kind",)

    def __init__(self, kind: str) -> None:
        self.kind = kind


_CANCELLED_SIGNAL = _W5ControlSignal("cancelled")
_KEYBOARD_INTERRUPT_SIGNAL = _W5ControlSignal("keyboard_interrupt")
_SYSTEM_EXIT_SIGNAL = _W5ControlSignal("system_exit")


def control_signal_for_exception(exc: BaseException) -> _W5ControlSignal | None:
    """Classify only exact process-control exceptions without retaining ``exc``."""
    if type(exc) is asyncio.CancelledError:
        return _CANCELLED_SIGNAL
    if type(exc) is KeyboardInterrupt:
        return _KEYBOARD_INTERRUPT_SIGNAL
    if type(exc) is SystemExit:
        return _SYSTEM_EXIT_SIGNAL
    return None


def sanitize_w5_receiver_message(message: TaskiqMessage) -> TaskiqMessage:
    """Drop W5 wire authority while preserving every non-W5 message verbatim."""
    task_name = message.task_name
    if type(task_name) is not str:
        return message
    if task_name == CALLBACK_JOB_TASK_NAME:
        _sanitize_job_message(message)
    elif task_name == CALLBACK_RECOVERY_TASK_NAME:
        _sanitize_recovery_message(message)
    return message


def _sanitize_job_message(message: TaskiqMessage) -> None:
    """Retain only the documented single canonical job UUID envelope."""
    message.labels = {}
    message.labels_types = None
    canonical = _canonical_job_wire(message.args, message.kwargs)
    if canonical is None:
        message.args = [_INVALID_JOB_WIRE_MARKER]
    else:
        message.args = [canonical]
    message.kwargs = {}


def _sanitize_recovery_message(message: TaskiqMessage) -> None:
    """Retain only the documented empty recovery envelope."""
    message.labels = {}
    message.labels_types = None
    if (
        type(message.args) is list
        and not message.args
        and _empty_exact_dict(message.kwargs)
    ):
        message.args = []
    else:
        message.args = [_INVALID_RECOVERY_WIRE_MARKER]
    message.kwargs = {}


def _canonical_job_wire(args: object, kwargs: object) -> str | None:
    """Accept one exact canonical UUID string without examining other shapes."""
    if type(args) is not list or len(args) != 1 or not _empty_exact_dict(kwargs):
        return None
    return canonical_job_id(args[0])


def _empty_exact_dict(value: object) -> bool:
    """Recognize only an empty built-in dict without invoking foreign hooks."""
    return type(value) is dict and not value


class W5ReceiverBoundaryFormatter(TaskiqFormatter):
    """Delegate writes and sanitize decoded W5 messages before Receiver logging."""

    def __init__(self, formatter: TaskiqFormatter) -> None:
        self._formatter = formatter

    def dumps(self, message: TaskiqMessage) -> BrokerMessage:
        """Keep producer-side serialization exactly delegated to TaskIQ."""
        return self._formatter.dumps(message)

    def loads(self, message: bytes) -> TaskiqMessage:
        """Decode once, then sanitize only the two durable-inbox task names."""
        return sanitize_w5_receiver_message(self._formatter.loads(message))


class W5ReceiverBoundaryMiddleware(TaskiqMiddleware):
    """Repeat W5 sanitization and turn private signals into safe callback exits."""

    def pre_execute(self, message: TaskiqMessage) -> TaskiqMessage:
        """Close any in-process mutation between formatter decode and execution."""
        return sanitize_w5_receiver_message(message)

    def post_execute(self, message: TaskiqMessage, result: TaskiqResult[Any]) -> None:
        """Raise fresh exact control outside ``run_task`` and before result save."""
        if type(message.task_name) is not str or message.task_name not in {
            CALLBACK_JOB_TASK_NAME,
            CALLBACK_RECOVERY_TASK_NAME,
        }:
            return
        signal = result.return_value
        # The task body kept exact controls out of on_error/SmartRetry. Receiver
        # now calls reverse post_execute before result_backend.set_result, so no
        # control signal can be saved or reach an ack-capable broker's
        # WHEN_SAVED acknowledgement stage as a completed result.
        if signal is _CANCELLED_SIGNAL:
            # Cancellation intentionally remains scoped to this callback.  The
            # durable DB markers and recovery own its final classification;
            # hostile task input must never gain process-wide shutdown power.
            raise asyncio.CancelledError()
        if signal is _KEYBOARD_INTERRUPT_SIGNAL:
            raise KeyboardInterrupt()
        if signal is _SYSTEM_EXIT_SIGNAL:
            raise SystemExit(1)


__all__ = [
    "CALLBACK_JOB_TASK_NAME",
    "CALLBACK_RECOVERY_TASK_NAME",
    "W5ReceiverBoundaryFormatter",
    "W5ReceiverBoundaryMiddleware",
    "control_signal_for_exception",
    "sanitize_w5_receiver_message",
]

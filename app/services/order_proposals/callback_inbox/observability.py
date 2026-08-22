"""Safe telemetry for the durable callback worker.

Everything the worker reports about a job goes through here, so there is one
place to check when asking "can a nonce reach Sentry". The answer is
structural rather than careful: the builders below accept only the fields
listed in :data:`SAFE_SPAN_KEYS`, and the one free-ish value (``outcome``) is
projected to a closed category by :func:`normalize_outcome` before it can be
attached.

ROB-1305's whole-event scrubber still runs downstream; this is the layer that
means it never has anything to scrub.
"""

from __future__ import annotations

import contextlib
import logging
import uuid
from collections.abc import Iterator
from typing import Any

from app.services.order_proposals.callback_inbox.contracts import (
    clamp_attempt_count,
    normalize_outcome,
)

logger = logging.getLogger(__name__)

WORKER_TRANSACTION_NAME = "order_proposals.telegram_callback_job"
WORKER_TRANSACTION_OP = "queue.task.taskiq"

#: The complete set of keys that may be attached to a worker span or log line.
#: No chat id, user id, message id, callback-query id, nonce or binding field
#: appears here, and none may be added without failing the data-minimisation
#: suite.
SAFE_SPAN_KEYS: frozenset[str] = frozenset(
    {
        "callback_job.id",
        "callback_job.state",
        "callback_job.attempt",
        "callback_job.queue_delay_seconds",
        "callback_job.outcome",
        "callback_job.error_class",
    }
)


def build_worker_span_data(
    *,
    job_id: uuid.UUID,
    state: str,
    attempt_count: int,
    queue_delay_seconds: float | None,
    outcome: object,
    error_class: str | None,
) -> dict[str, Any]:
    """Build the only payload the worker ever attaches to a span."""
    data: dict[str, Any] = {
        "callback_job.id": str(job_id),
        "callback_job.state": str(state),
        # Keep telemetry in the same fixed protocol range as persisted
        # terminal rows.  This avoids ever exporting a legacy poison count
        # even if a future caller bypasses the worker's terminal branch.
        "callback_job.attempt": clamp_attempt_count(attempt_count),
    }
    if queue_delay_seconds is not None:
        data["callback_job.queue_delay_seconds"] = round(float(queue_delay_seconds), 3)
    label = normalize_outcome(outcome)
    if label is not None:
        data["callback_job.outcome"] = label
    if error_class is not None:
        data["callback_job.error_class"] = str(error_class)
    return data


def log_job_event(
    event: str,
    *,
    job_id: uuid.UUID,
    state: str,
    attempt_count: int,
    queue_delay_seconds: float | None = None,
    outcome: object = None,
    error_class: str | None = None,
) -> None:
    """Log a job transition using the same allowlist as the span."""
    logger.info(
        event,
        extra=build_worker_span_data(
            job_id=job_id,
            state=state,
            attempt_count=attempt_count,
            queue_delay_seconds=queue_delay_seconds,
            outcome=outcome,
            error_class=error_class,
        ),
    )


@contextlib.contextmanager
def worker_transaction(job_id: uuid.UUID) -> Iterator[Any]:
    """Own a Sentry transaction for the worker, or degrade to a no-op.

    The Telegram and broker spans the webhook used to emit inline belong under
    this transaction once the durable path is armed. Sentry being absent or
    misbehaving must never fail a job, so every interaction is suppressed.
    """
    try:
        import sentry_sdk

        manager = sentry_sdk.start_transaction(
            op=WORKER_TRANSACTION_OP, name=WORKER_TRANSACTION_NAME
        )
        transaction = manager.__enter__()
    except Exception:  # noqa: BLE001 - telemetry must never break a job
        yield None
        return

    with contextlib.suppress(Exception):
        transaction.set_tag("callback_job.id", str(job_id))
    try:
        yield transaction
    except BaseException as caller_error:
        # Sentry teardown may observe the failure, but it may neither replace
        # nor suppress the worker's original exception or control signal.
        with contextlib.suppress(Exception):
            manager.__exit__(
                type(caller_error), caller_error, caller_error.__traceback__
            )
        raise
    else:
        # A telemetry-only teardown failure cannot turn a successful job into
        # a retry or dead letter.
        with contextlib.suppress(Exception):
            manager.__exit__(None, None, None)


def annotate(span: Any, data: dict[str, Any]) -> None:
    """Attach allowlisted data to a span, tolerating any SDK shape."""
    if span is None:
        return
    for key, value in data.items():
        if key not in SAFE_SPAN_KEYS:
            continue
        with contextlib.suppress(Exception):
            span.set_data(key, value)


__all__ = [
    "SAFE_SPAN_KEYS",
    "WORKER_TRANSACTION_NAME",
    "WORKER_TRANSACTION_OP",
    "annotate",
    "build_worker_span_data",
    "log_job_event",
    "worker_transaction",
]

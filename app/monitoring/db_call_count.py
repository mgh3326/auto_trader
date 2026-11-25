"""Per-transaction DB statement counter for Sentry transactions (hk 6378).

Each ``before_cursor_execute`` increments one integer keyed by the active
Sentry transaction's ``(trace_id, span_id)``. :func:`attach_db_call_count_measurement`
copies the finished transaction's count onto its event as ``db.call_count``
and drops the key, independent of the Sentry span cap.

Keying off the transaction object (not the task's context) is what makes
this work for every surface:

- MCP tool calls: the tool body runs in the session task while the ``/mcp``
  transaction finishes in the request task, but the SDK's MCP integration
  re-applies the request's isolation scope in the handler, so
  ``get_current_span().containing_transaction`` is the same object.
- Statements with no active transaction (sweeps before a job transaction
  starts, unsampled requests that never produce an event) are attributed to
  nothing and never leak into the next transaction.

Cost is one dict update per statement. No statement text, parameters, or
connection details are read or stored. Every entry point is fail-open: any
error in the counter never affects the statement or the transaction.
Unfinished transactions are bounded by evicting the oldest key.
"""

from __future__ import annotations

import threading
from typing import Any

import sentry_sdk

DB_CALL_COUNT_MEASUREMENT = "db.call_count"
_counts: dict[tuple[str, str], int] = {}

# Bounds entries for transactions whose events never reach finalize
# (unsampled, probes). One entry is two short strings plus an int.
_MAX_TRACKED_TRANSACTIONS = 1024

_counts: dict[tuple[str, str], int] = {}
_lock = threading.Lock()


def _active_transaction_key() -> tuple[str, str] | None:
    """Key of the calling task's active Sentry transaction, if any."""
    try:
        span = sentry_sdk.get_current_span()
    except Exception:  # noqa: BLE001 - telemetry must never raise
        return None
    if span is None:
        return None
    try:
        transaction = span.containing_transaction
    except Exception:  # noqa: BLE001 - telemetry must never raise
        return None
    if transaction is None:
        return None
    try:
        trace_id = transaction.trace_id
        span_id = transaction.span_id
    except Exception:  # noqa: BLE001 - telemetry must never raise
        return None
    if (
        not isinstance(trace_id, str)
        or not isinstance(span_id, str)
        or not trace_id
        or not span_id
    ):
        return None
    return (trace_id, span_id)


def before_cursor_execute(
    conn: Any,
    cursor: Any,
    statement: Any,
    parameters: Any,
    context: Any,
    executemany: Any,
) -> None:
    """SQLAlchemy ``before_cursor_execute`` listener.

    Counts one statement against the active Sentry transaction. Argument
    values are never read or stored; with no active transaction the
    statement is ignored; any internal error is swallowed so the statement
    always proceeds.
    """
    del conn, cursor, statement, parameters, context, executemany
    try:
        key = _active_transaction_key()
        if key is None:
            return
        with _lock:
            if key not in _counts and len(_counts) >= _MAX_TRACKED_TRANSACTIONS:
                _counts.pop(next(iter(_counts)))
            _counts[key] = _counts.get(key, 0) + 1
    except Exception:  # noqa: BLE001 - telemetry must never break a statement
        return


def attach_db_call_count_measurement(event: dict[str, Any]) -> None:
    """Attach the finished transaction's count as ``db.call_count``.

    The transaction is identified from the event's trace context and its
    key is dropped, so sequential transactions never accumulate. No
    measurement is added when the transaction has no counted statements.
    Never raises and never alters other event fields.
    """
    try:
        contexts = event.get("contexts")
        trace = contexts.get("trace") if isinstance(contexts, dict) else None
        trace_id = trace.get("trace_id") if isinstance(trace, dict) else None
        span_id = trace.get("span_id") if isinstance(trace, dict) else None
        if (
            not isinstance(trace_id, str)
            or not isinstance(span_id, str)
            or not trace_id
            or not span_id
        ):
            return
        with _lock:
            count = _counts.pop((trace_id, span_id), 0)
        if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
            return
        measurements = event.get("measurements")
        if measurements is None:
            measurements = {}
            event["measurements"] = measurements
        if isinstance(measurements, dict):
            measurements[DB_CALL_COUNT_MEASUREMENT] = {"value": count}
    except Exception:  # noqa: BLE001 - telemetry must never break a transaction
        return

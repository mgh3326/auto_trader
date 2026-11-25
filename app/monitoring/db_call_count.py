"""Per-request DB statement counter for Sentry transactions (hk 6378).

A SQLAlchemy ``before_cursor_execute`` listener increments one integer in a
:class:`contextvars.ContextVar`, so concurrent transactions (separate asyncio
tasks) keep separate counts. :func:`attach_db_call_count_measurement` copies
the count onto the finished transaction event as ``db.call_count``,
independent of the Sentry span cap.

Cost is one integer increment per statement. No statement text, parameters,
or connection details are stored. Every entry point is fail-open: any error
in the counter never affects the statement or the transaction.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any

DB_CALL_COUNT_MEASUREMENT = "db.call_count"

_count: ContextVar[int] = ContextVar("sentry_db_call_count", default=0)


def get_db_call_count() -> int:
    """Return the current request's statement count, 0 on any error."""
    try:
        value = _count.get()
    except Exception:  # noqa: BLE001 - telemetry must never raise
        return 0
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def reset_db_call_count() -> None:
    """Reset the current request's count. Never raises."""
    try:
        _count.set(0)
    except Exception:  # noqa: BLE001 - telemetry must never raise
        return


def _increment() -> None:
    _count.set(get_db_call_count() + 1)


def before_cursor_execute(
    conn: Any,
    cursor: Any,
    statement: Any,
    parameters: Any,
    context: Any,
    executemany: Any,
) -> None:
    """SQLAlchemy ``before_cursor_execute`` listener.

    Counts one statement. Argument values are never read or stored, and any
     internal error is swallowed so the statement always proceeds.
    """
    del conn, cursor, statement, parameters, context, executemany
    try:
        _increment()
    except Exception:  # noqa: BLE001 - telemetry must never break a statement
        return


def attach_db_call_count_measurement(event: dict[str, Any]) -> None:
    """Attach the current count as ``db.call_count`` measurement, then reset.

    No measurement is added when the count is 0 (no transaction tracked in
    this context). The reset keeps sequential transactions in one context
    from accumulating. Never raises and never alters other event fields.
    """
    try:
        count = get_db_call_count()
    except Exception:  # noqa: BLE001 - telemetry must never raise
        return
    try:
        reset_db_call_count()
        if count <= 0:
            return
        measurements = event.get("measurements")
        if measurements is None:
            measurements = {}
            event["measurements"] = measurements
        if isinstance(measurements, dict):
            measurements[DB_CALL_COUNT_MEASUREMENT] = {"value": count}
    except Exception:  # noqa: BLE001 - telemetry must never break a transaction
        return

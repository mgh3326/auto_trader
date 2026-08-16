"""Bounded durable audit events for proposal submit failures.

The submit boundary can return rich provider payloads, but proposal provenance
must retain only stable operational facts.  This module owns the narrow JSONB
projection used by the service writer and Telegram-facing submit classifier.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

SUBMIT_FAILURES_KEY = "submit_failures"

_MAX_FAILURES = 16
_KNOWN_REASON_CODES = frozenset(
    {
        "kis_gateway_throttle_not_delivered",
        "kis_gateway_throttle_delivery_ambiguous",
    }
)
_KNOWN_BROKER_CODES = frozenset({"EGW00201", "EGW00215"})


def _safe_rung_index(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value < 10000 else None


def _safe_timestamp(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 80:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.isoformat() if parsed.tzinfo is not None else None


def _safe_bool_or_none(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _safe_post_attempts(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value == 1 else None


def _safe_failure(
    value: Any,
    *,
    rung_index: int | None = None,
    occurred_at: str | None = None,
) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    safe_rung_index = (
        rung_index
        if rung_index is not None
        else _safe_rung_index(value.get("rung_index"))
    )
    reason_code = value.get("reason_code")
    broker_message_code = value.get("broker_message_code")
    http_status = value.get("http_status")
    post_attempts = _safe_post_attempts(value.get("post_attempts"))
    if (
        safe_rung_index is None
        or reason_code not in _KNOWN_REASON_CODES
        or broker_message_code not in _KNOWN_BROKER_CODES
        or not isinstance(http_status, int)
        or not 200 <= http_status < 300
        or post_attempts is None
    ):
        return None
    if value.get("broker_order_id") is not None:
        # This event family represents only responses without a broker order
        # number. Any other shape must remain outside this audit projection.
        return None
    return {
        "occurred_at": occurred_at
        if occurred_at is not None
        else _safe_timestamp(value.get("occurred_at")),
        "stage": "submit",
        "rung_index": safe_rung_index,
        "reason_code": reason_code,
        "broker_message_code": broker_message_code,
        "http_status": http_status,
        "broker_order_id": None,
        "ledger_entry_present": _safe_bool_or_none(value.get("ledger_entry_present")),
        "idempotency_key_present": value.get("idempotency_key_present") is True,
        "intent_reserved": value.get("intent_reserved") is True,
        "post_attempts": post_attempts,
    }


def project_submit_failures(source_asof: Any) -> list[dict[str, Any]]:
    """Return only valid bounded submit-failure records from proposal JSONB."""
    if not isinstance(source_asof, Mapping):
        return []
    raw_events = source_asof.get(SUBMIT_FAILURES_KEY)
    if not isinstance(raw_events, Sequence) or isinstance(raw_events, (str, bytes)):
        return []
    events: list[dict[str, Any]] = []
    for raw in raw_events[-_MAX_FAILURES:]:
        safe = _safe_failure(raw)
        if safe is None or safe["occurred_at"] is None:
            continue
        events.append(safe)
    return events


def append_submit_failure(
    source_asof: Any,
    *,
    rung_index: int,
    failure: Mapping[str, Any],
    now: datetime,
) -> dict[str, Any]:
    """Append one validated submit failure without retaining raw provider text."""
    source = dict(source_asof) if isinstance(source_asof, Mapping) else {}
    safe = _safe_failure(
        failure,
        rung_index=_safe_rung_index(rung_index),
        occurred_at=now.isoformat(),
    )
    if safe is None:
        return source
    source[SUBMIT_FAILURES_KEY] = [
        *project_submit_failures(source),
        safe,
    ][-_MAX_FAILURES:]
    return source


__all__ = [
    "SUBMIT_FAILURES_KEY",
    "append_submit_failure",
    "project_submit_failures",
]

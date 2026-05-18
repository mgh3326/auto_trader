"""Deterministic idempotency-key composers for ROB-265 investment reports.

All composers return a colon-joined, lowercase-where-applicable string.
``_`` is the slot for ``None`` so a missing field never collides with a
real value. The canonical watch-condition hash is sha256 of a JSON dump
with sorted keys, so logically equivalent payloads produce the same hash
regardless of dict insertion order.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

__all__ = [
    "report_key",
    "item_key",
    "watch_activation_key",
    "watch_event_key",
    "canonical_watch_condition_hash",
]

_NONE_SLOT = "_"


def _slot(value: Any) -> str:
    if value is None:
        return _NONE_SLOT
    return str(value).strip().lower()


def report_key(
    *,
    report_type: str,
    market: str,
    market_session: str | None,
    kst_date: str,
    generator_version: str,
) -> str:
    """Stable key for one generator pass producing one report."""
    return ":".join(
        [
            "report",
            _slot(report_type),
            _slot(market),
            _slot(market_session),
            _slot(kst_date),
            _slot(generator_version),
        ]
    )


def item_key(
    *,
    report_uuid: str,
    item_kind: str,
    symbol: str | None,
    side: str | None,
    intent: str,
    watch_condition: dict | None,
) -> str:
    """Stable key per (report, kind, symbol, side, intent, condition)."""
    condition_hash = (
        canonical_watch_condition_hash(watch_condition)
        if watch_condition is not None
        else _NONE_SLOT
    )
    return ":".join(
        [
            "item",
            _slot(report_uuid),
            _slot(item_kind),
            _slot(symbol),
            _slot(side),
            _slot(intent),
            condition_hash,
        ]
    )


def watch_activation_key(*, source_item_uuid: str) -> str:
    """One activation per approved item."""
    return ":".join(["activation", _slot(source_item_uuid)])


def watch_event_key(
    *, alert_uuid: str, kst_date: str, threshold_key: str
) -> str:
    """One event per (alert, day, threshold)."""
    return ":".join(
        [
            "event",
            _slot(alert_uuid),
            _slot(kst_date),
            _slot(threshold_key),
        ]
    )


def canonical_watch_condition_hash(payload: dict) -> str:
    """sha256 of sort_keys JSON dump.

    Returns the first 16 hex chars — enough collision-resistance for an
    idempotency-key slot and short enough to stay readable in logs.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]

"""Deterministic idempotency-key composers for ROB-265 investment reports.

All composers return a colon-joined, lowercase-where-applicable string.
``_`` is the slot for ``None`` so a missing field never collides with a
real value. The canonical watch-condition hash is sha256 over a
sort_keys JSON dump.

Plan-2 hardening:
* ``report_key`` includes ``account_scope`` + ``execution_mode`` so a
  ``kis_live`` vs ``kis_mock`` report on the same date/session/generator
  does not collide.
* ``item_key`` includes ``client_item_key`` — the caller-supplied stable
  identifier — so duplicate natural-key items (multiple risks, or
  two same-symbol/same-intent action items) get distinct keys.
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
    "direct_watch_key",
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
    account_scope: str | None,
    execution_mode: str,
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
            _slot(account_scope),
            _slot(execution_mode),
            _slot(kst_date),
            _slot(generator_version),
        ]
    )


def kst_date_from_report_key(key: str | None) -> str | None:
    """Recover the kst_date slot from a :func:`report_key` string.

    Inverse of ``report_key`` — kept here so the two evolve together. Returns
    ``None`` if the key is missing or doesn't match the expected 8-slot layout
    (``report:type:market:session:scope:mode:kst_date:version``).
    """
    if not key:
        return None
    parts = key.split(":")
    if len(parts) != 8 or parts[0] != "report":
        return None
    slot = parts[6]
    return None if slot == _NONE_SLOT else slot


def item_key(
    *,
    report_uuid: str,
    client_item_key: str,
    item_kind: str,
    symbol: str | None,
    side: str | None,
    intent: str,
    watch_condition: dict | None,
) -> str:
    """Stable key per (report, client_item_key, kind, symbol, side, intent, condition).

    ``client_item_key`` is the disambiguator the caller supplies so two
    items that happen to share the same natural fields (e.g. two risks,
    or two scoped buys on the same symbol) still produce distinct keys.
    """
    condition_hash = (
        canonical_watch_condition_hash(watch_condition)
        if watch_condition is not None
        else _NONE_SLOT
    )
    return ":".join(
        [
            "item",
            _slot(report_uuid),
            _slot(client_item_key),
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


def watch_event_key(*, alert_uuid: str, kst_date: str, threshold_key: str) -> str:
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


def direct_watch_key(
    *,
    created_by: str,
    market: str,
    symbol: str,
    intent: str,
    valid_until: str,
    watch_condition: dict,
) -> str:
    """Stable key for direct, report-flow-independent watch creation."""
    return ":".join(
        [
            "direct-watch",
            _slot(created_by),
            _slot(market),
            _slot(symbol),
            _slot(intent),
            _slot(valid_until),
            canonical_watch_condition_hash(watch_condition),
        ]
    )

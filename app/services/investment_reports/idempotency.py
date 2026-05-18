"""Deterministic idempotency-key composers for ROB-265 investment reports.

Keys are colon-joined, lowercase where applicable, and use ``_`` for
``None`` slots so that a missing field never collides with another value.
The canonical watch-condition hash is sha256 over a sorted-key JSON dump.

Composers (filled in during Task 8):
* ``report_key``
* ``item_key``
* ``watch_activation_key``
* ``watch_event_key``
* ``canonical_watch_condition_hash``
"""

from __future__ import annotations

__all__ = [
    "report_key",
    "item_key",
    "watch_activation_key",
    "watch_event_key",
    "canonical_watch_condition_hash",
]

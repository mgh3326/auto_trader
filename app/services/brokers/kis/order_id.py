"""ROB-843 — canonical broker order-id normalization for KIS order results.

Shared by the domestic and overseas KIS mock result boundary so a blank or
whitespace-only ``odno`` is never mistaken for an accepted broker order. Pure,
stdlib-only, no broker/DB/network imports.
"""

from __future__ import annotations

from typing import Any


def normalize_broker_order_id(value: Any) -> str | None:
    """Return a stripped non-empty order id, else ``None``.

    * ``str`` → stripped; empty/whitespace-only → ``None``.
    * ``int`` → decimal string (but ``bool`` is rejected as malformed).
    * ``None`` / any other type (dict, list, float, …) → ``None`` (malformed).

    Used so ``accepted`` requires a *valid* broker order id, not merely a
    truthy one (``"   "`` was previously accepted).
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        value = str(value)
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None

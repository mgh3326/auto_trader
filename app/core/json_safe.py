"""JSON-safety helpers shared across snapshot/report payload writers.

Houses :func:`sanitize_non_finite`, relocated here from
``app/schemas/validated_run_card.py`` (ROB-329) so non-schema callers — e.g.
``financial_fundamentals_snapshots`` (ROB-426) — can reuse it without depending
on a report-citation schema module. ``validated_run_card`` re-exports it for
back-compat.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any


def sanitize_non_finite(value: Any) -> Any:
    """Recursively replace non-finite floats (``inf``/``-inf``/``nan``) with
    ``None`` so the result is strict-JSON / Postgres-jsonb / JS-JSON.parse
    safe. Returns a new structure; the input is not mutated. Booleans and
    integers are left untouched (``bool`` is intentionally not treated as a
    float here)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return {k: sanitize_non_finite(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize_non_finite(v) for v in value]
    return value

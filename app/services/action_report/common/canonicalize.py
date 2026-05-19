# app/services/action_report/common/canonicalize.py
"""Canonical payload hashing for snapshot dedup (ROB-269 Phase 1).

Normalization rules (locked in pre-plan Decision 3):
1. Keys lexicographically sorted at every level.
2. Top-level ``source_timestamps`` block is excluded.
3. ISO-8601 timestamp strings are truncated to second precision.
4. Floats are formatted to 9-digit fixed precision.
5. List order is preserved (not sorted) — order is meaningful for ordered series.
6. None values are kept (explicit null != absent key).

Returns a 64-char SHA-256 hex digest.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

_ISO_SUBSECOND_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\.\d+(.*)$"
)


def _truncate_iso_subsecond(value: str) -> str:
    m = _ISO_SUBSECOND_RE.match(value)
    if m is None:
        return value
    return f"{m.group(1)}{m.group(2)}"


def _normalize(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {k: _normalize(value[k]) for k in sorted(value.keys())}
    if isinstance(value, list):
        return [_normalize(v) for v in value]
    if isinstance(value, float):
        return f"{value:.9f}"
    if isinstance(value, str):
        return _truncate_iso_subsecond(value)
    return value


def canonical_payload_hash(payload: Mapping[str, Any]) -> str:
    """Return SHA-256 hex of canonicalized payload (see module docstring)."""
    stripped = {k: payload[k] for k in payload if k != "source_timestamps"}
    normalized = _normalize(stripped)
    encoded = json.dumps(normalized, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()

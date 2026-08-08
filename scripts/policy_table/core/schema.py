"""Decimal-safe JSON serialization + hashing for policy_table.v1 artifacts.

Market-agnostic: no Upbit/KRX-specific code here. ``canonical_json_bytes``
is used both to compute ``policy_table_hash`` and to write the artifact, so
the hash always matches the bytes on disk, and two runs over identical
input produce byte-identical files (ROB-1230 acceptance #3).
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal, localcontext
from typing import Any

from research.kr_corpus.d3_engine.constants import DECIMAL_PRECISION, DECIMAL_ROUNDING

SCHEMA_VERSION = "policy_table.v1"

# Serialization-time display quantum. Internal computation stays at D3's
# full 50-digit precision (research.kr_corpus.d3_engine.constants); this
# only trims the division/sqrt tail noise (e.g. "...060124000...0001")
# before writing JSON. 8 places covers Upbit's smallest KRW tick (0.00001)
# with headroom and is applied uniformly rather than per-field.
_DISPLAY_QUANTUM = Decimal("0.00000001")


def to_jsonable(obj: Any) -> Any:
    """Recursively convert Decimal/date/tuple values into JSON-safe types."""

    if isinstance(obj, Decimal):
        if obj.is_finite():
            with localcontext() as context:
                context.prec = DECIMAL_PRECISION
                context.rounding = DECIMAL_ROUNDING
                obj = obj.quantize(_DISPLAY_QUANTUM, rounding=DECIMAL_ROUNDING)
        return format(obj, "f")
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {str(key): to_jsonable(value) for key, value in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(item) for item in obj]
    return obj


def canonical_json_bytes(payload: dict[str, Any]) -> bytes:
    """Deterministic (sorted-key, fixed-indent) UTF-8 JSON bytes."""

    jsonable = to_jsonable(payload)
    return (
        json.dumps(jsonable, sort_keys=True, ensure_ascii=False, indent=2).encode(
            "utf-8"
        )
        + b"\n"
    )


def compute_policy_table_hash(payload_without_hash: dict[str, Any]) -> str:
    digest = hashlib.sha256(canonical_json_bytes(payload_without_hash)).hexdigest()
    return f"sha256:{digest}"


def sha256_of_bytes(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


__all__ = [
    "SCHEMA_VERSION",
    "to_jsonable",
    "canonical_json_bytes",
    "compute_policy_table_hash",
    "sha256_of_bytes",
]

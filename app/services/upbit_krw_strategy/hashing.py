"""Canonical JSON and Decimal text shared by every hashed record."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal


def decimal_text(value: Decimal | None) -> str | None:
    """Plain exact text without exponent or trailing zeros (``5E+9`` -> ``5000000000``)."""
    if value is None:
        return None
    if not isinstance(value, Decimal) or not value.is_finite():
        raise TypeError("value must be a finite Decimal or None")
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


def canonical_json(payload: object) -> str:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

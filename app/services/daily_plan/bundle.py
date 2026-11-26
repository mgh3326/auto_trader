"""Canonical serialization for immutable daily-plan inputs."""

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

SCHEMA_VERSION = "daily-plan-bundle/v1"


def decimal_string(value: Decimal) -> str:
    """Render without exponent or context-dependent rounding."""
    if not value.is_finite():
        raise ValueError("non_finite_decimal")
    if not value:
        return "0"
    result = format(value, "f")
    return result.rstrip("0").rstrip(".") if "." in result else result


def _normalize(value: Any) -> Any:
    if isinstance(value, Decimal):
        return decimal_string(value)
    if isinstance(value, datetime):
        if value.utcoffset() is None:
            raise ValueError("unknown_time")
        return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    if isinstance(value, dict):
        if any(type(key) is not str for key in value):
            raise TypeError("bundle keys must be strings")
        return {key: _normalize(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_normalize(item) for item in value]
    if value is None or type(value) in (str, bool, int):
        return value
    raise TypeError("unsupported bundle value")


def canonical_bundle(body: dict[str, Any]) -> bytes:
    if body.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unknown_bundle_schema")
    normalized = _normalize(body)
    if "rows" in normalized:
        normalized["rows"] = sorted(
            normalized["rows"],
            key=lambda row: (row["row_kind"], row["market"], row["symbol"], row["account_ref"] or ""),
        )
    return json.dumps(
        normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def bundle_sha256(body: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_bundle(body)).hexdigest()

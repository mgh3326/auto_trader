"""ROB-945 (H5) -- canonical payload converter.

``research_contracts.canonical_hash.encode_canonical`` hashes plain
JSON-native Python values (``dict``/``list``/``tuple``/``str``/``int``/
``float``/``bool``/``None``) but raises ``TypeError`` on an arbitrary
object -- including H4's own frozen dataclasses (``SignalEvent``,
``TradeRecord``, ``WalkForwardResult``, ...). This module recursively
converts such a dataclass tree into the plain structure the canonical
authority already knows how to hash, WITHOUT inventing a second hashing
scheme: the actual byte-level canonicalization (key sorting, float hex
encoding, non-finite rejection) still happens entirely inside
``research_contracts.canonical_hash``.
"""

from __future__ import annotations

import dataclasses
import math
from typing import Any

# Repository-wide stable sentinel convention for non-finite floats in a
# JSON-hashed/rendered payload (matches
# ``rob944_walkforward._json_safe_float_or_sentinel`` byte-for-byte, kept as
# a local literal copy rather than an import to avoid this generic,
# domain-agnostic converter depending on one specific H4 module).
_NONFINITE_NAN = "nonfinite:nan"
_NONFINITE_POS_INF = "nonfinite:inf"
_NONFINITE_NEG_INF = "nonfinite:-inf"


def _sentinel_for_nonfinite_float(value: float) -> str:
    if math.isnan(value):
        return _NONFINITE_NAN
    return _NONFINITE_POS_INF if value > 0 else _NONFINITE_NEG_INF


def to_canonical_payload(value: Any) -> Any:
    """Recursively convert dataclasses/tuples/lists/dicts into a plain,
    JSON-native structure. Leaves (``str``/``int``/``bool``/``None``) pass
    through unchanged; ``float`` passes through unchanged IF finite, else is
    replaced by the repository's stable non-finite sentinel string (e.g.
    ``profit_factor=+Inf`` when gross_loss is zero is a legitimate, expected
    value here, never an error). Any other, unrecognized type raises
    ``TypeError`` rather than silently coercing it (e.g. via ``str()``),
    since a lossy fallback would let two different values collide onto the
    same hash.
    """
    if isinstance(value, float):
        return value if math.isfinite(value) else _sentinel_for_nonfinite_float(value)
    if value is None or isinstance(value, str | int | bool):
        return value
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            f.name: to_canonical_payload(getattr(value, f.name))
            for f in dataclasses.fields(value)
        }
    if isinstance(value, dict):
        return {key: to_canonical_payload(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [to_canonical_payload(item) for item in value]
    raise TypeError(
        f"to_canonical_payload: unsupported type {type(value).__name__!r} -- "
        "add explicit, non-lossy handling rather than falling back to str()"
    )

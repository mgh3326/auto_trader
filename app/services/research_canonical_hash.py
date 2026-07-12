"""ROB-846 — canonical SHA-256 identity helpers for the strategy experiment registry.

Deterministic, order-independent hashing so a strategy version's identity
(strategy/code/params/dataset/PIT/frozen-config/policy/benchmark/cost/MDD) can
be pinned once and reproduced exactly.

Intentionally stdlib-only. This module must never import broker/order/fill
surfaces (see ``tests/services/research/test_no_broker_import_guard.py``).
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime
from decimal import Decimal
from typing import Any

__all__ = [
    "IDENTITY_COMPONENTS",
    "canonical_json",
    "canonical_sha256",
    "compute_identity_hashes",
    "derive_experiment_id",
    "to_jsonable",
]

# Ordered identity components. Each maps to a ``<name>_hash`` column on
# ``research.strategy_experiments``. The order is stable and part of the
# public contract — appending is safe; reordering/removing is not.
IDENTITY_COMPONENTS: tuple[str, ...] = (
    "strategy",
    "code",
    "params",
    "dataset_manifest",
    "universe",
    "pit",
    "frozen_config",
    "policy",
    "benchmark",
    "cost",
    "mdd",
)


def to_jsonable(value: Any) -> Any:
    """Recursively convert an identity payload into a JSON-safe structure.

    This is the single canonical representation used for BOTH hashing and DB
    (JSONB) persistence, so a value that was hashed can be stored, read back,
    and re-hashed to the identical digest. The result contains only
    ``dict``/``list``/``str``/``int``/``float``/``bool``/``None`` and therefore
    always serialises to Postgres JSONB.

    Type mapping and fail-closed rules:

    * ``Decimal`` → ``"__decimal__:<canonical str>"`` (never a lossy float);
      non-finite Decimals (NaN/Inf) are rejected.
    * ``float`` → passed through, but NaN/±Inf are rejected (JSONB has no
      representation for them).
    * ``datetime``/``date`` → ``"__datetime__:<ISO-8601>"``.
    * ``dict`` → keys MUST be ``str``. A non-string key (e.g. ``1`` vs ``"1"``)
      is rejected rather than coerced with ``str(key)``, which would collapse
      distinct identities onto the same manifest/hash.
    * ``set``/``frozenset`` → list ordered by each member's canonical JSON (not
      the members' Python natural order, which is undefined across types).
      Members that collide to the same canonical form are rejected as
      ambiguous.

    Raises ``ValueError``/``TypeError`` for any value that cannot be represented
    as a collision-free, JSON-safe canonical form.
    """
    if value is None or isinstance(value, str | bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"non-finite float is not JSON/JSONB safe: {value!r}")
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError(f"non-finite Decimal is not JSON/JSONB safe: {value!r}")
        return f"__decimal__:{value!s}"
    if isinstance(value, datetime | date):
        return f"__datetime__:{value.isoformat()}"
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(
                    "identity dict keys must be str to stay collision-free; got "
                    f"{type(key).__name__} key {key!r}"
                )
            result[key] = to_jsonable(item)
        return result
    if isinstance(value, list | tuple):
        return [to_jsonable(item) for item in value]
    if isinstance(value, frozenset | set):
        keyed = sorted(
            ((canonical_json(to_jsonable(item)), to_jsonable(item)) for item in value),
            key=lambda pair: pair[0],
        )
        for earlier, later in zip(keyed, keyed[1:], strict=False):
            if earlier[0] == later[0]:
                raise ValueError(
                    "ambiguous set: members collide to the same canonical form "
                    f"{later[0]!r}"
                )
        return [member for _, member in keyed]
    raise TypeError(f"Unhashable identity value of type {type(value).__name__!r}")


def canonical_json(payload: Any) -> str:
    """Canonical JSON text: sorted keys, compact separators, UTF-8 safe.

    Operates on the json-safe form (:func:`to_jsonable`) so the exact bytes
    that are hashed equal the bytes derived from the persisted JSONB manifest.
    ``allow_nan=False`` is the last line of defence — ``to_jsonable`` already
    rejects non-finite numbers, but this guarantees no ``NaN``/``Infinity``
    token can ever reach the wire even via an unforeseen path.
    """
    return json.dumps(
        to_jsonable(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def canonical_sha256(payload: Any) -> str:
    """Lowercase 64-hex SHA-256 of the canonical JSON of ``payload``."""
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def compute_identity_hashes(components: dict[str, Any]) -> dict[str, str]:
    """Return ``{"<component>_hash": sha256}`` for every identity component.

    A missing component is hashed as ``null`` so the mapping is always complete
    and deterministic; callers that require full identity enforce presence at
    the schema layer.
    """
    return {
        f"{name}_hash": canonical_sha256(components.get(name))
        for name in IDENTITY_COMPONENTS
    }


def derive_experiment_id(
    strategy_key: str,
    strategy_version: str,
    component_hashes: dict[str, str],
) -> str:
    """Derive the immutable canonical experiment identity.

    The identity is a function of the strategy key/version plus every component
    hash. Any change to code, params, dataset, config, policy, cost, benchmark
    or MDD definition yields a new experiment_id (a new lineage version),
    leaving prior rows' hashes untouched.
    """
    identity = {
        "strategy_key": strategy_key,
        "strategy_version": strategy_version,
        "component_hashes": {
            f"{name}_hash": component_hashes[f"{name}_hash"]
            for name in IDENTITY_COMPONENTS
        },
    }
    return canonical_sha256(identity)

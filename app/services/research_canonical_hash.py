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
from datetime import date, datetime
from decimal import Decimal
from typing import Any

__all__ = [
    "IDENTITY_COMPONENTS",
    "canonical_json",
    "canonical_sha256",
    "compute_identity_hashes",
    "derive_experiment_id",
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


def _default(value: Any) -> Any:
    """Deterministic fallback for non-JSON-native identity payloads.

    Decimals serialize as their canonical string form (never a lossy float),
    and datetimes/dates as ISO-8601. This keeps the hash stable across
    processes and Python builds.
    """
    if isinstance(value, Decimal):
        return f"__decimal__:{value!s}"
    if isinstance(value, datetime | date):
        return f"__datetime__:{value.isoformat()}"
    if isinstance(value, frozenset | set):
        return sorted(
            _default(v) if isinstance(v, Decimal | datetime | date) else v
            for v in value
        )
    raise TypeError(f"Unhashable identity value of type {type(value).__name__!r}")


def canonical_json(payload: Any) -> str:
    """Canonical JSON text: sorted keys, compact separators, UTF-8 safe."""
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=_default,
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

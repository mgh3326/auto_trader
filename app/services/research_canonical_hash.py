"""ROB-846 — canonical identity helpers for the strategy experiment registry.

The identity of a strategy version must have ONE canonical, collision-free,
JSON-safe representation used for both hashing and JSONB persistence, so a
version can be reproduced exactly and two genuinely different identities can
never share an ``experiment_id``.

Design: every value — leaf AND container, including plain strings — is encoded
into a **closed, typed canonical AST** made only of JSON-native types. Each node
is a 2-element ``[tag, payload]`` list, e.g. ``["decimal", "1.0"]``,
``["str", "x"]``, ``["list", [...]]``, ``["tuple", [...]]``, ``["set", [...]]``,
``["dict", [[key, node], ...]]``. Because raw ``list``/``dict`` inputs are
themselves wrapped in a typed node and their members are recursively encoded, a
user cannot forge a tag: a raw string ``"__decimal__:1.0"`` encodes to
``["str", "__decimal__:1.0"]`` which can never equal ``["decimal", "1.0"]``, and
a ``list`` (``["list", ...]``) can never equal a ``tuple``/``set``.

Two entry points are kept deliberately separate (ROB-846 review): one encodes a
RAW Python value into an AST, the other hashes an ALREADY-encoded AST. A
persisted JSONB manifest is hashed directly and never re-encoded, so a
round-trip reproduces identical digests.

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
    "canonical_ast_json",
    "canonical_json",
    "canonical_sha256",
    "compute_identity_hashes",
    "compute_identity_hashes_from_ast",
    "derive_experiment_id",
    "encode_canonical",
    "encode_manifest",
    "hash_canonical_ast",
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

# The null AST node — the canonical encoding of ``None`` (also the default used
# when a persisted manifest is missing a component).
_NULL_NODE: list[Any] = ["null", None]


def encode_canonical(value: Any) -> list[Any]:
    """Encode a raw identity value into a closed, forgery-proof typed AST.

    Returns a JSON-native ``[tag, payload]`` node. Fail-closed rules:

    * ``float`` NaN/±Inf and non-finite ``Decimal`` are rejected — JSONB cannot
      represent them.
    * ``dict`` keys must be ``str`` (a non-string key would otherwise need a
      lossy ``str()`` coercion that collapses distinct identities).
    * ``set``/``frozenset`` members are ordered by their canonical AST bytes
      (never Python natural order, which is undefined across types); members
      that encode to the same node are rejected as ambiguous.
    * Any unsupported type raises ``TypeError`` (rejected at the schema boundary,
      before any DB work).
    """
    if value is None:
        return ["null", None]
    # bool is a subclass of int — handle it first.
    if isinstance(value, bool):
        return ["bool", value]
    if isinstance(value, int):
        return ["int", value]
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"non-finite float is not JSON/JSONB safe: {value!r}")
        return ["float", value]
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError(f"non-finite Decimal is not JSON/JSONB safe: {value!r}")
        return ["decimal", str(value)]
    # datetime is a subclass of date — handle it first.
    if isinstance(value, datetime):
        return ["datetime", value.isoformat()]
    if isinstance(value, date):
        return ["date", value.isoformat()]
    if isinstance(value, str):
        return ["str", value]
    if isinstance(value, dict):
        entries: list[list[Any]] = []
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(
                    "identity dict keys must be str to stay collision-free; got "
                    f"{type(key).__name__} key {key!r}"
                )
            entries.append([key, encode_canonical(item)])
        entries.sort(key=lambda pair: pair[0])
        return ["dict", entries]
    if isinstance(value, tuple):
        return ["tuple", [encode_canonical(item) for item in value]]
    if isinstance(value, list):
        return ["list", [encode_canonical(item) for item in value]]
    if isinstance(value, frozenset | set):
        members = sorted(
            (encode_canonical(item) for item in value), key=canonical_ast_json
        )
        for earlier, later in zip(members, members[1:], strict=False):
            if canonical_ast_json(earlier) == canonical_ast_json(later):
                raise ValueError(
                    "ambiguous set: members encode to the same canonical node "
                    f"{canonical_ast_json(later)!r}"
                )
        return ["set", members]
    raise TypeError(f"unsupported identity value of type {type(value).__name__!r}")


def canonical_ast_json(ast: Any) -> str:
    """Deterministic JSON text of an ALREADY-encoded canonical AST.

    The AST contains only JSON-native types produced by :func:`encode_canonical`.
    A top-level manifest mapping (component name → AST) may also be passed, so
    ``sort_keys=True`` normalises those keys; ``allow_nan=False`` is a final
    guard against any stray non-finite number reaching the wire.
    """
    return json.dumps(
        ast,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def hash_canonical_ast(ast: Any) -> str:
    """Lowercase 64-hex SHA-256 of an already-encoded canonical AST."""
    return hashlib.sha256(canonical_ast_json(ast).encode("utf-8")).hexdigest()


def canonical_json(payload: Any) -> str:
    """Canonical JSON text of a RAW value (encode then serialise)."""
    return canonical_ast_json(encode_canonical(payload))


def canonical_sha256(payload: Any) -> str:
    """Lowercase 64-hex SHA-256 of a RAW value's canonical AST."""
    return hash_canonical_ast(encode_canonical(payload))


def encode_manifest(components: dict[str, Any]) -> dict[str, Any]:
    """Encode identity components into their persisted AST (name → AST node)."""
    return {
        name: encode_canonical(components.get(name)) for name in IDENTITY_COMPONENTS
    }


def compute_identity_hashes(components: dict[str, Any]) -> dict[str, str]:
    """Per-component SHA-256 from RAW components (encode then hash)."""
    return {
        f"{name}_hash": canonical_sha256(components.get(name))
        for name in IDENTITY_COMPONENTS
    }


def compute_identity_hashes_from_ast(manifest: dict[str, Any]) -> dict[str, str]:
    """Per-component SHA-256 from a PERSISTED AST manifest (hash directly).

    The manifest maps component name → already-encoded AST node. It is hashed
    without re-encoding, so a JSONB round-trip reproduces identical digests —
    this is the separate hashing entry point required by ROB-846 review.
    """
    return {
        f"{name}_hash": hash_canonical_ast(manifest.get(name, _NULL_NODE))
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

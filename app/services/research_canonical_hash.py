"""Backward-compatible ROB-846 canonical-hash API.

The stdlib-only implementation lives in the small wheel-packaged
``research_contracts`` boundary so isolated research and the registry cannot
drift to different hashes.
"""

import math
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from research_contracts.canonical_hash import (
    IDENTITY_COMPONENTS,
    canonical_ast_json,
    canonical_json,
    canonical_sha256,
    compute_identity_hashes,
    compute_identity_hashes_from_ast,
    derive_experiment_id,
    encode_canonical,
    encode_manifest,
    hash_canonical_ast,
)

__all__ = [
    "IDENTITY_COMPONENTS",
    "canonical_ast_json",
    "canonical_json",
    "canonical_sha256",
    "compute_identity_hashes",
    "compute_identity_hashes_from_ast",
    "decode_canonical",
    "decode_manifest",
    "derive_experiment_id",
    "encode_canonical",
    "encode_manifest",
    "hash_canonical_ast",
]


def decode_canonical(ast: Any) -> Any:
    """Strictly decode one closed typed AST node from persisted JSONB."""
    if type(ast) is not list or len(ast) != 2:
        raise ValueError("canonical AST node must be an exact 2-element list")
    tag, payload = ast
    if type(tag) is not str:
        raise ValueError("canonical AST tag must be a built-in str")

    try:
        if tag == "null":
            if payload is not None:
                raise ValueError("null payload must be None")
            decoded: Any = None
        elif tag == "bool":
            if type(payload) is not bool:
                raise ValueError("bool payload must be a built-in bool")
            decoded = payload
        elif tag == "int":
            if type(payload) is not int:
                raise ValueError("int payload must be a built-in int")
            decoded = payload
        elif tag == "float":
            if type(payload) is not str:
                raise ValueError("float payload must be an exact hex string")
            decoded = float.fromhex(payload)
            if not math.isfinite(decoded):
                raise ValueError("float payload must be finite")
        elif tag == "decimal":
            if type(payload) is not str:
                raise ValueError("decimal payload must be a built-in str")
            decoded = Decimal(payload)
            if not decoded.is_finite():
                raise ValueError("decimal payload must be finite")
        elif tag == "datetime":
            if type(payload) is not str:
                raise ValueError("datetime payload must be a built-in str")
            decoded = datetime.fromisoformat(payload)
        elif tag == "date":
            if type(payload) is not str:
                raise ValueError("date payload must be a built-in str")
            decoded = date.fromisoformat(payload)
        elif tag == "str":
            if type(payload) is not str:
                raise ValueError("str payload must be a built-in str")
            decoded = payload
        elif tag == "dict":
            if type(payload) is not list:
                raise ValueError("dict payload must be a built-in list")
            decoded = {}
            prior_key: str | None = None
            for entry in payload:
                if type(entry) is not list or len(entry) != 2:
                    raise ValueError("dict entry must be an exact [key, node] list")
                key, child = entry
                if type(key) is not str:
                    raise ValueError("dict key must be a built-in str")
                if prior_key is not None and key <= prior_key:
                    raise ValueError("dict keys must be unique and strictly sorted")
                decoded[key] = decode_canonical(child)
                prior_key = key
        elif tag in {"list", "tuple", "set"}:
            if type(payload) is not list:
                raise ValueError(f"{tag} payload must be a built-in list")
            values = [decode_canonical(child) for child in payload]
            if tag == "list":
                decoded = values
            elif tag == "tuple":
                decoded = tuple(values)
            else:
                try:
                    decoded = frozenset(values)
                except TypeError as exc:
                    raise ValueError(
                        "set payload contains an unhashable member"
                    ) from exc
        else:
            raise ValueError(f"unknown canonical AST tag {tag!r}")
    except (InvalidOperation, OverflowError) as exc:
        raise ValueError(f"invalid {tag!r} canonical AST payload") from exc

    if canonical_ast_json(encode_canonical(decoded)) != canonical_ast_json(ast):
        raise ValueError(f"{tag!r} canonical AST node is not in canonical form")
    return decoded


def decode_manifest(manifest: Any) -> dict[str, Any]:
    """Decode an exact persisted identity manifest (all 11 components)."""
    if type(manifest) is not dict:
        raise ValueError("canonical identity manifest must be a built-in dict")
    expected = set(IDENTITY_COMPONENTS)
    actual = set(manifest)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ValueError(
            f"canonical identity manifest keys differ (missing={missing}, extra={extra})"
        )
    return {name: decode_canonical(manifest[name]) for name in IDENTITY_COMPONENTS}

"""Backward-compatible ROB-846 canonical-hash API.

The stdlib-only implementation lives in the small wheel-packaged
``research_contracts`` boundary so isolated research and the registry cannot
drift to different hashes.
"""

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
    "derive_experiment_id",
    "encode_canonical",
    "encode_manifest",
    "hash_canonical_ast",
]

"""Versioned EvaluationConfig canonical SHA-256 hash computation.

The hash is order-independent for mappings (``canonical_sha256`` sorts dict
keys) and order-dependent for sequences (tuples).  Changing any meaningful
field changes the hash.  Non-finite / ambiguous / unsupported values are
rejected by the Pydantic frozen contracts before reaching the hash.
"""

from __future__ import annotations

from app.services.paper_evaluation.contracts import EvaluationConfig


def compute_config_hash(config: EvaluationConfig) -> str:
    """Return the deterministic 64-hex SHA-256 of ``config``.

    This is the single entry point for config hashing.  It delegates to
    ``EvaluationConfig.config_hash()`` which calls ``canonical_sha256``
    over the canonical payload dict.
    """
    return config.config_hash()


__all__ = ["compute_config_hash"]

"""Deterministic correlation spine for the KIS mock runner."""

from __future__ import annotations

import hashlib


def kis_mock_runner_correlation_id(
    *,
    tag: str,
    candidate_id: str,
    contract_hash: str,
    strategy_id: str,
    decision_key: str,
) -> str:
    """Build the one ID carried unchanged to ledger and forecast attribution.

    ``decision_key`` is supplied by a future overlay (for example, a frozen
    session/signal identity).  B0 never fabricates one.
    """
    values = (tag, candidate_id, contract_hash, strategy_id, decision_key)
    if any(not value.strip() for value in values):
        raise ValueError("correlation inputs must all be non-blank")
    canonical = "|".join(values)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return f"kis-mock-runner:{tag}:{digest}"

"""Compatibility re-export of the wheel-packaged trial-evidence contract."""

from research_contracts.trial_evidence import (
    LEGACY_SCHEMA_VERSION,
    P_VALUE_METHOD,
    PRODUCER,
    PRODUCER_VERSION,
    SCHEMA_VERSION,
    SELECTION_SCHEMA_VERSION,
    SELECTION_SCORE_METHOD,
    SHARPE_METHOD,
    TrialEvidence,
    TrialEvidenceError,
    build_trial_evidence,
    parse_trial_evidence,
)

__all__ = [
    "LEGACY_SCHEMA_VERSION",
    "P_VALUE_METHOD",
    "PRODUCER",
    "PRODUCER_VERSION",
    "SCHEMA_VERSION",
    "SELECTION_SCHEMA_VERSION",
    "SELECTION_SCORE_METHOD",
    "SHARPE_METHOD",
    "TrialEvidence",
    "TrialEvidenceError",
    "build_trial_evidence",
    "parse_trial_evidence",
]

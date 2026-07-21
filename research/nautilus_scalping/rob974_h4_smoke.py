"""ROB-982 CP8/CP9 deterministic contract and production-identity smoke.

The bounded CP8 fixture proves the H1/H2/H3/H4 contracts without running a
year-long synthetic campaign.  CP9 additionally builds the real merged H6-A
production identity from raw source pins.  ROB-984 CP10 closed the delegated
fake-free full-scope run; this smoke records that downstream evidence receipt
without becoming part of H4's runner source bundle or recomputing its evidence.
"""

from __future__ import annotations

from rob974_h3_h2_adapter import verify_h2_contract
from rob974_h3_manifest import FROZEN_H3_ROSTER, validate_manifest
from rob974_h4_contracts import (
    ATTRIBUTION_SCHEMA_VERSION,
    H4SourcePins,
    attribution_contract,
    exact_h4_folds,
)
from rob974_h4_h6a_adapter import build_production_h4_plan
from rob974_h4_plan import build_fixture_plan

_DEFERRED_FAKE_FREE_FULL_SCOPE = "DEFERRED_TO_H6B_INTEGRATION_E2E"
_CLOSED_FAKE_FREE_PREFIX = "CLOSED_BY_ROB984_CP10:sha256:"
# Exact deterministic receipt from the ROB-984 R1 fake-free CP10 E2E.  This
# file is intentionally outside RUNNER_SOURCE_FILES.
ROB984_CP10_FAKE_FREE_EVIDENCE_SHA256 = (
    "6f60e868df5c1d27f19094b5146e22c70a69ba89550f18397c0926ebce2ec326"
)


def _fake_free_full_scope_marker(evidence_sha256: str | None) -> str:
    """Never emit CLOSED when the downstream CP10 evidence receipt is absent."""

    if evidence_sha256 is None:
        return _DEFERRED_FAKE_FREE_FULL_SCOPE
    if evidence_sha256 != ROB984_CP10_FAKE_FREE_EVIDENCE_SHA256:
        raise ValueError("ROB-984 CP10 fake-free evidence receipt differs")
    return _CLOSED_FAKE_FREE_PREFIX + evidence_sha256


def run_contract_fixture_smoke() -> dict[str, object]:
    """Pure integration audit; no empirical/corpus/DB/runtime execution."""
    validate_manifest(FROZEN_H3_ROSTER)
    verify_h2_contract()
    folds = exact_h4_folds()
    fixture_plan = build_fixture_plan(source_pins=H4SourcePins("1" * 64, "2" * 64))
    if len(folds) != 8 or len(fixture_plan.expected_attempt_ids) != 48:
        raise ValueError("H4 fixture smoke cardinality drift")
    production_plan = build_production_h4_plan()
    if len(production_plan.expected_attempt_ids) != 48:
        raise ValueError("H4/H6-A production identity cardinality drift")
    return {
        "contract_fixture_h4_smoke": "PASS",
        "actual_h1_integration": "PASS",
        "actual_h2_integration": "PASS",
        "actual_h3_integration": "PASS",
        "actual_h6a_integration": "PASS",
        "actual_h4_contract": "PASS",
        "actual_h4_contract_semantic": "typed_integration_not_empirical_closure",
        "attribution_schema_version": ATTRIBUTION_SCHEMA_VERSION,
        "contract_provenance": list(attribution_contract()["contract_provenance"]),
        "fake_free_full_scope": _fake_free_full_scope_marker(
            ROB984_CP10_FAKE_FREE_EVIDENCE_SHA256
        ),
        "fake_free_full_scope_evidence_sha256": (ROB984_CP10_FAKE_FREE_EVIDENCE_SHA256),
        "full_campaign_hash": production_plan.full_campaign_hash,
        "campaign_run_id": production_plan.campaign_run_id,
        "source_pins": production_plan.source_pins.as_dict(),
        "empirical_runs": 0,
        "real_corpus_campaign_runs": 0,
        "db_sessions": 0,
        "db_queries": 0,
        "db_writes": 0,
        "broker_or_pair_executor_calls": 0,
        "artifact_publish_calls": 0,
    }

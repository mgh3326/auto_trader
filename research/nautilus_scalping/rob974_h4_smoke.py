"""ROB-982 CP8/CP9 deterministic contract and production-identity smoke.

The bounded CP8 fixture proves the H1/H2/H3/H4 contracts without running a
year-long synthetic campaign.  CP9 additionally builds the real merged H6-A
production identity from raw source pins.  The expensive fake-free full-scope
run is deliberately delegated to H6-B's campaign-final integration E2E.
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
        "fake_free_full_scope": "DEFERRED_TO_H6B_INTEGRATION_E2E",
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

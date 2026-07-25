"""ROB-1005: actual H6-A retries produce a fail-closed H5 scorecard."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest
import rob974_h6a_accounting as h6a_accounting
import test_rob974_h5_cp6_canonical_json as cp6fx
import test_rob974_h5_cp7_markdown_and_smoke as cp7fx
from rob974_h4_h6a_adapter import build_production_h4_plan
from rob974_h5_canonical import build_canonical_scorecard, canonical_json_bytes
from rob974_h5_contracts import (
    H5InputError,
    fixture_h4_attribution_result,
    resolve_h6a_accounting_contract,
    validate_envelope_and_accounting,
)
from rob974_h5_markdown import render_markdown
from rob974_h5_s4 import compute_campaign_decision

_RETRY_REASON = "h6_accounting_has_retries"
_NOT_EVALUATED = "not_evaluated_h6a_accounting_incomplete"


def _actual_retry_report():
    plan = build_production_h4_plan()
    attempts = [
        h6a_accounting.AttemptAccountingRow(
            row_id=spec.row_id,
            experiment_id=spec.experiment_id,
            retry_index=0,
            status="completed",
            reason_code=None,
            fold_evidence_hash=hashlib.sha256(
                f"rob1005-primary:{spec.experiment_id}".encode()
            ).hexdigest(),
            run_identity=hashlib.sha256(
                f"rob1005-run:{plan.campaign_run_id}:{spec.experiment_id}".encode()
            ).hexdigest(),
        )
        for spec in plan.row_specs
    ]
    retried = plan.row_specs[0]
    attempts.append(
        h6a_accounting.AttemptAccountingRow(
            row_id=retried.row_id,
            experiment_id=retried.experiment_id,
            retry_index=1,
            status="completed",
            reason_code=None,
            fold_evidence_hash=hashlib.sha256(b"rob1005-retry-fold").hexdigest(),
            run_identity=hashlib.sha256(b"rob1005-retry-run").hexdigest(),
        )
    )
    report = h6a_accounting.build_combined_accounting(
        campaign_run_id=plan.campaign_run_id,
        canonical_row_ids=tuple(spec.row_id for spec in plan.row_specs),
        row_id_to_experiment_id={
            spec.row_id: spec.experiment_id for spec in plan.row_specs
        },
        registered_total=len(plan.row_specs),
        attempts=tuple(attempts),
    )
    assert report.accounting_complete is True
    assert report.all_primary_completed is True
    assert report.performance_usable is False
    assert report.primary_attempts == 48
    assert report.total_attempts == 49
    assert report.retry_attempts == 1
    assert sum(report.status_counts.values()) == 49
    return plan, report


def _degraded_scorecard():
    plan, report = _actual_retry_report()
    try:
        contract = resolve_h6a_accounting_contract(report)
    except H5InputError as exc:
        pytest.fail(f"actual retry report must degrade instead of hard-fail: {exc}")
    assert contract.actual_h6a_contract == "PASS"
    assert contract.seal is not None
    assert contract.seal.retry_attempts == 1
    assert contract.seal.performance_usable is False
    assert contract.seal.reason_codes == (_RETRY_REASON,)

    envelope = cp6fx._envelope(
        campaign_run_id=plan.campaign_run_id,
        h6a_trial_accounting_hash=report.trial_accounting_hash,
    )
    validation = validate_envelope_and_accounting(envelope, contract.seal)
    assert validation.ok is False
    assert validation.incomplete_reasons == (_RETRY_REASON,)

    s3_inputs = replace(
        cp7fx._s3_inputs_with_verdict(passing=True),
        direct_verdict="incomplete",
    )
    s4_inputs = replace(
        cp7fx._s4_inputs_with_verdict(passing=True),
        direct_verdict="incomplete",
    )
    campaign = compute_campaign_decision(
        s3_direct_verdict=s3_inputs.direct_verdict,
        s4_direct_verdict=s4_inputs.direct_verdict,
    )
    scorecard = build_canonical_scorecard(
        envelope=envelope,
        h6a_seal=report,
        envelope_ok=False,
        envelope_incomplete_reasons=validation.incomplete_reasons,
        h4_attribution=fixture_h4_attribution_result(),
        s3_inputs=s3_inputs,
        s4_inputs=s4_inputs,
        campaign_decision=campaign,
    )
    return report, scorecard


def test_actual_retry_report_materializes_degraded_incomplete_scorecard():
    report, scorecard = _degraded_scorecard()
    assert scorecard["h6a_accounting"]["actual_h6a_contract"] == "PASS"
    assert scorecard["h6a_accounting"]["retry_attempts"] == 1
    assert scorecard["h6a_accounting"]["performance_usable"] is False
    assert scorecard["h6a_accounting"]["reason_codes"] == [_RETRY_REASON]
    assert scorecard["envelope_validation"] == {
        "ok": False,
        "incomplete_reasons": [_RETRY_REASON],
    }
    for strategy in ("S3", "S4"):
        entry = scorecard["strategies"][strategy]
        assert entry["evaluation_state"] == _NOT_EVALUATED
        assert entry["evaluation_incomplete_reasons"] == [_RETRY_REASON]
        assert entry["common_gates"]["passed"] is None
        assert entry["falsification"]["passed"] is None
        assert entry["direct_verdict"] == "incomplete"
    assert scorecard["campaign_decision"]["campaign_decision"] == "incomplete"
    assert scorecard["campaign_decision"]["campaign_historical_verdict"] == (
        "incomplete"
    )
    assert scorecard["campaign_decision"]["demo_candidate"] is None
    assert (
        report.trial_accounting_hash
        == scorecard["lineage"]["h6a_trial_accounting_hash"]
    )

    canonical = canonical_json_bytes(scorecard)
    markdown = render_markdown(json.loads(canonical))
    assert b"evaluation_state: not_evaluated_h6a_accounting_incomplete" in markdown
    assert b"campaign_historical_verdict: incomplete" in markdown


def test_retry_cannot_masquerade_as_performance_usable_or_historical_pass():
    _plan, report = _actual_retry_report()
    with pytest.raises(H5InputError, match="performance_usable_forged_or_stale"):
        resolve_h6a_accounting_contract(replace(report, performance_usable=True))

    _report, scorecard = _degraded_scorecard()
    assert all(
        scorecard["strategies"][strategy]["direct_verdict"] != "historical_pass"
        for strategy in ("S3", "S4")
    )
    assert scorecard["campaign_decision"]["campaign_historical_verdict"] != (
        "historical_pass"
    )

"""ROB-945 (H5, Task 1A) -- RED tests for the exact frozen-campaign +
real H6 accounting/attempt-evidence sealing boundary.

Scope note: this task (1A) covers structural validation only -- the exact
nested ``AttemptEvidence``/``AttemptKey``/``ScenarioEvidence`` shape bound to
the real frozen 24 experiment IDs, the exact 12-field
``CampaignCompletenessReport``, and canonical-order hash stability.
``fold_evidence_hash``/``run_identity`` are validated here ONLY as opaque
well-formed lowercase-hex64 strings -- cross-binding their CONTENT against
the real H4 ``ConfigAttemptEvidenceSummary`` is Task 1B, not this file.
"""

from __future__ import annotations

import hashlib

import pytest
from rob944_frozen_campaign import build_production_frozen_campaign_envelope
from rob945_accounting_seal import (
    ACCOUNTING_INCOMPLETE_REASON,
    NOT_FROZEN_PRODUCTION_CAMPAIGN_REASON,
    PRIMARY_ATTEMPT_NOT_COMPLETED_REASON,
    RETRIES_PRESENT_REASON,
    ScorecardInputError,
    seal_trial_accounting,
)

from research_contracts.canonical_hash import canonical_sha256

_ENVELOPE = build_production_frozen_campaign_envelope()
FULL_CAMPAIGN_HASH = _ENVELOPE.full_campaign_hash()
FROZEN_EXPERIMENT_IDS = tuple(_ENVELOPE.to_dict()["experiment_ids"])
assert len(FROZEN_EXPERIMENT_IDS) == 24 and len(set(FROZEN_EXPERIMENT_IDS)) == 24


def _derive_run_id(full_campaign_hash: str) -> str:
    import base64

    digest_hex = canonical_sha256(
        {"full_campaign_hash": full_campaign_hash, "kind": "primary_run"}
    )
    suffix = (
        base64.urlsafe_b64encode(bytes.fromhex(digest_hex)).decode("ascii").rstrip("=")
    )
    return f"rob944-primary-{suffix}"


CAMPAIGN_RUN_ID = _derive_run_id(FULL_CAMPAIGN_HASH)


def _hex64(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _scenario_evidence_row(seed: str, scenario_name: str) -> dict:
    return {
        "scenario_name": scenario_name,
        "trade_count": 3,
        "artifact_hash": _hex64(f"{seed}-{scenario_name}"),
    }


def _attempt(
    experiment_id: str,
    *,
    retry_index: int = 0,
    status: str = "completed",
    reason_code: str | None = None,
    campaign_run_id: str = CAMPAIGN_RUN_ID,
) -> dict:
    seed = f"{experiment_id}:{retry_index}"
    return {
        "attempt_key": {
            "campaign_run_id": campaign_run_id,
            "experiment_id": experiment_id,
            "retry_index": retry_index,
        },
        "status": status,
        "reason_code": reason_code,
        "fold_evidence_hash": _hex64(f"fold:{seed}"),
        "run_identity": _hex64(f"run:{seed}"),
        "scenario_evidence": [
            _scenario_evidence_row(seed, name)
            for name in ("base", "primary_stress", "upward_stress")
        ],
    }


def _all_24_completed_attempts() -> list[dict]:
    return [
        _attempt(eid, retry_index=0, status="completed")
        for eid in FROZEN_EXPERIMENT_IDS
    ]


def _clean_report(**overrides) -> dict:
    report = {
        "campaign_run_id": CAMPAIGN_RUN_ID,
        "expected_total": 24,
        "actual_registrations": 24,
        "primary_attempts": 24,
        "total_attempts": 24,
        "retry_attempts": 0,
        "status_counts": {"completed": 24, "rejected": 0, "crashed": 0, "timeout": 0},
        "missing_experiment_ids": [],
        "extra_experiment_ids": [],
        "mismatch_experiment_ids": [],
        "duplicate_or_gap_experiment_ids": [],
        "verdict": "complete",
    }
    report.update(overrides)
    return report


def _seal(*, attempt_evidence=None, accounting_report=None, full_campaign_hash=None):
    return seal_trial_accounting(
        accounting_report=accounting_report
        if accounting_report is not None
        else _clean_report(),
        attempt_evidence=attempt_evidence
        if attempt_evidence is not None
        else _all_24_completed_attempts(),
        full_campaign_hash=full_campaign_hash
        if full_campaign_hash is not None
        else FULL_CAMPAIGN_HASH,
    )


# -- Case 1/2: real frozen campaign lineage, reject arbitrary self-consistent fakes --


def test_seals_the_real_production_frozen_campaign_hash():
    sealed = _seal()
    assert sealed.full_campaign_hash == FULL_CAMPAIGN_HASH
    assert sealed.campaign_run_id == CAMPAIGN_RUN_ID
    assert sealed.performance_usable is True
    assert sealed.accounting_complete is True
    assert sealed.all_primary_completed is True


def test_rejects_a_self_consistent_arbitrary_campaign_hash():
    fake_payload = {"not": "the-real-campaign", "nonce": 1}
    fake_hash = canonical_sha256(fake_payload)
    with pytest.raises(ScorecardInputError) as exc_info:
        _seal(full_campaign_hash=fake_hash)
    assert NOT_FROZEN_PRODUCTION_CAMPAIGN_REASON in str(exc_info.value)


def test_rejects_wrong_length_or_non_hex_full_campaign_hash():
    with pytest.raises(ScorecardInputError):
        _seal(full_campaign_hash="0" * 63)
    with pytest.raises(ScorecardInputError):
        _seal(full_campaign_hash="Z" * 64)


# -- Case 3: exact nested AttemptEvidence shape --


def test_accepts_the_exact_nested_attempt_evidence_shape():
    sealed = _seal()
    assert len(sealed.attempts) == 24
    first = sealed.attempts[0]
    assert first.experiment_id in FROZEN_EXPERIMENT_IDS
    assert len(first.scenario_evidence) == 3
    assert [s.scenario_name for s in first.scenario_evidence] == [
        "base",
        "primary_stress",
        "upward_stress",
    ]


def test_rejects_flattened_attempt_shape_missing_attempt_key():
    attempts = _all_24_completed_attempts()
    attempts[0] = {
        "experiment_id": FROZEN_EXPERIMENT_IDS[0],
        "retry_index": 0,
        "status": "completed",
    }
    with pytest.raises(ScorecardInputError):
        _seal(attempt_evidence=attempts)


def test_rejects_scenario_evidence_out_of_canonical_order():
    attempts = _all_24_completed_attempts()
    attempts[0]["scenario_evidence"] = list(reversed(attempts[0]["scenario_evidence"]))
    with pytest.raises(ScorecardInputError):
        _seal(attempt_evidence=attempts)


def test_rejects_scenario_evidence_with_wrong_count():
    attempts = _all_24_completed_attempts()
    attempts[0]["scenario_evidence"] = attempts[0]["scenario_evidence"][:2]
    with pytest.raises(ScorecardInputError):
        _seal(attempt_evidence=attempts)


def test_rejects_duplicate_scenario_name():
    attempts = _all_24_completed_attempts()
    attempts[0]["scenario_evidence"][2]["scenario_name"] = "base"
    with pytest.raises(ScorecardInputError):
        _seal(attempt_evidence=attempts)


# -- Case 4: every experiment ID must belong to the exact frozen 24 --


def test_rejects_an_experiment_id_not_in_the_frozen_24():
    attempts = _all_24_completed_attempts()
    attempts[0] = _attempt("totally-made-up-id-not-frozen", retry_index=0)
    with pytest.raises(ScorecardInputError):
        _seal(attempt_evidence=attempts)


def test_wrong_attempt_count_fails_closed():
    attempts = _all_24_completed_attempts()[:23]
    with pytest.raises(ScorecardInputError):
        _seal(
            attempt_evidence=attempts,
            accounting_report=_clean_report(total_attempts=23),
        )


# -- Case 5: retry_index strict-int / bool / float / negative / gap / duplicate --


def test_rejects_bool_retry_index():
    attempts = _all_24_completed_attempts()
    attempts[0]["attempt_key"]["retry_index"] = False
    with pytest.raises(ScorecardInputError):
        _seal(attempt_evidence=attempts)


def test_rejects_float_retry_index():
    attempts = _all_24_completed_attempts()
    attempts[0]["attempt_key"]["retry_index"] = 0.0
    with pytest.raises(ScorecardInputError):
        _seal(attempt_evidence=attempts)


def test_rejects_negative_retry_index():
    attempts = _all_24_completed_attempts()
    attempts[0]["attempt_key"]["retry_index"] = -1
    with pytest.raises(ScorecardInputError):
        _seal(attempt_evidence=attempts)


def test_a_contiguous_explicit_retry_forces_performance_usable_false_but_is_not_malformed():
    attempts = _all_24_completed_attempts()
    retry_row = _attempt(FROZEN_EXPERIMENT_IDS[0], retry_index=1, status="completed")
    attempts.append(retry_row)
    report = _clean_report(
        total_attempts=25,
        retry_attempts=1,
        status_counts={"completed": 25, "rejected": 0, "crashed": 0, "timeout": 0},
    )
    sealed = _seal(attempt_evidence=attempts, accounting_report=report)
    assert sealed.accounting_complete is True
    assert sealed.performance_usable is False
    assert RETRIES_PRESENT_REASON in sealed.reason_codes


def test_a_retry_gap_is_internally_inconsistent_and_raises():
    """A retry_index=2 row with no retry_index=1 row for the same experiment
    is a gap -- the report's own ``duplicate_or_gap_experiment_ids`` must
    reflect it; a report that claims a clean/empty list while a real gap
    exists in the supplied attempts is an internally inconsistent
    (malformed) input, not a silently-accepted well-formed incomplete one."""
    attempts = _all_24_completed_attempts()
    gap_row = _attempt(FROZEN_EXPERIMENT_IDS[0], retry_index=2, status="completed")
    attempts.append(gap_row)
    report = _clean_report(total_attempts=25, retry_attempts=1)
    with pytest.raises(ScorecardInputError):
        _seal(attempt_evidence=attempts, accounting_report=report)


# -- Case 6: exact closed statuses / H4 status-reason combinations --


def test_rejects_unknown_status():
    attempts = _all_24_completed_attempts()
    attempts[0]["status"] = "garbage_status_xyz"
    with pytest.raises(ScorecardInputError):
        _seal(attempt_evidence=attempts)


def test_rejects_completed_status_with_nonnull_reason_code():
    attempts = _all_24_completed_attempts()
    attempts[0]["reason_code"] = "child_execution_crashed"
    with pytest.raises(ScorecardInputError):
        _seal(attempt_evidence=attempts)


def test_accepts_the_two_valid_rejected_reason_codes():
    for reason in (
        "rejected:data_gap_in_position",
        "insufficient_train_evidence_all_folds",
    ):
        attempts = _all_24_completed_attempts()
        attempts[0] = _attempt(
            FROZEN_EXPERIMENT_IDS[0],
            retry_index=0,
            status="rejected",
            reason_code=reason,
        )
        report = _clean_report(
            status_counts={"completed": 23, "rejected": 1, "crashed": 0, "timeout": 0},
        )
        sealed = _seal(attempt_evidence=attempts, accounting_report=report)
        assert sealed.accounting_complete is True
        assert sealed.all_primary_completed is False
        assert sealed.performance_usable is False


def test_rejects_rejected_status_with_wrong_reason_code():
    attempts = _all_24_completed_attempts()
    attempts[0] = _attempt(
        FROZEN_EXPERIMENT_IDS[0],
        retry_index=0,
        status="rejected",
        reason_code="bogus_reason",
    )
    with pytest.raises(ScorecardInputError):
        _seal(attempt_evidence=attempts)


def test_accepts_both_crashed_reason_codes_and_the_timeout_reason_code():
    for status, reason in (
        ("crashed", "child_execution_crashed"),
        ("crashed", "global_corpus_load_failed"),
        ("timeout", "child_execution_timeout"),
    ):
        attempts = _all_24_completed_attempts()
        attempts[0] = _attempt(
            FROZEN_EXPERIMENT_IDS[0], retry_index=0, status=status, reason_code=reason
        )
        counts = {"completed": 23, "rejected": 0, "crashed": 0, "timeout": 0}
        counts[status] = 1
        report = _clean_report(status_counts=counts)
        sealed = _seal(attempt_evidence=attempts, accounting_report=report)
        assert sealed.accounting_complete is True
        assert PRIMARY_ATTEMPT_NOT_COMPLETED_REASON in sealed.reason_codes


# -- Case 7: hash-format validation --


def test_rejects_non_hex64_fold_evidence_hash():
    attempts = _all_24_completed_attempts()
    attempts[0]["fold_evidence_hash"] = "not-a-hash"
    with pytest.raises(ScorecardInputError):
        _seal(attempt_evidence=attempts)


def test_rejects_non_hex64_run_identity():
    attempts = _all_24_completed_attempts()
    attempts[0]["run_identity"] = "z" * 64
    with pytest.raises(ScorecardInputError):
        _seal(attempt_evidence=attempts)


def test_rejects_non_hex64_scenario_artifact_hash():
    attempts = _all_24_completed_attempts()
    attempts[0]["scenario_evidence"][0]["artifact_hash"] = "short"
    with pytest.raises(ScorecardInputError):
        _seal(attempt_evidence=attempts)


def test_rejects_negative_or_non_int_scenario_trade_count():
    attempts = _all_24_completed_attempts()
    attempts[0]["scenario_evidence"][0]["trade_count"] = -1
    with pytest.raises(ScorecardInputError):
        _seal(attempt_evidence=attempts)
    attempts2 = _all_24_completed_attempts()
    attempts2[0]["scenario_evidence"][0]["trade_count"] = True
    with pytest.raises(ScorecardInputError):
        _seal(attempt_evidence=attempts2)


# -- Case 9: exact 12-field CampaignCompletenessReport --


def test_rejects_report_with_extra_field():
    report = _clean_report(extra_unexpected_field="nope")
    with pytest.raises(ScorecardInputError):
        _seal(accounting_report=report)


def test_rejects_report_with_missing_field():
    report = _clean_report()
    del report["duplicate_or_gap_experiment_ids"]
    with pytest.raises(ScorecardInputError):
        _seal(accounting_report=report)


def test_rejects_report_whose_campaign_run_id_does_not_match_the_derived_one():
    report = _clean_report(campaign_run_id="rob944-primary-" + "z" * 43)
    with pytest.raises(ScorecardInputError):
        _seal(accounting_report=report)


def test_rejects_report_claiming_complete_while_status_counts_do_not_sum_to_total():
    report = _clean_report(
        status_counts={"completed": 20, "rejected": 0, "crashed": 0, "timeout": 0}
    )
    with pytest.raises(ScorecardInputError):
        _seal(accounting_report=report)


def test_rejects_status_counts_with_unknown_key():
    report = _clean_report(
        status_counts={
            "completed": 24,
            "rejected": 0,
            "crashed": 0,
            "timeout": 0,
            "bogus": 0,
        }
    )
    with pytest.raises(ScorecardInputError):
        _seal(accounting_report=report)


# -- Case 10: malformed vs well-formed-incomplete distinction --


def test_a_bare_verdict_complete_string_alone_is_not_sufficient():
    """The R1-flagged weakness: a caller cannot pass a near-empty/forged
    report and have it accepted merely because ``verdict == "complete"``."""
    with pytest.raises(ScorecardInputError):
        _seal(accounting_report={"verdict": "complete"})


def test_missing_primary_attempt_yields_well_formed_incomplete_not_a_raise():
    attempts = _all_24_completed_attempts()[:23]
    missing_id = FROZEN_EXPERIMENT_IDS[23]
    report = _clean_report(
        actual_registrations=23,
        primary_attempts=23,
        total_attempts=23,
        status_counts={"completed": 23, "rejected": 0, "crashed": 0, "timeout": 0},
        missing_experiment_ids=[missing_id],
        verdict="incomplete",
    )
    sealed = _seal(attempt_evidence=attempts, accounting_report=report)
    assert sealed.accounting_complete is False
    assert sealed.performance_usable is False
    assert ACCOUNTING_INCOMPLETE_REASON in sealed.reason_codes


def test_verdict_claim_contradicting_recomputed_completeness_raises():
    """A report claiming ``verdict=="complete"`` while independently
    recomputed evidence proves otherwise (or vice versa) is internally
    inconsistent -- malformed, not silently coerced either way."""
    attempts = _all_24_completed_attempts()[:23]
    report = _clean_report(
        actual_registrations=23,
        primary_attempts=23,
        total_attempts=23,
        status_counts={"completed": 23, "rejected": 0, "crashed": 0, "timeout": 0},
        missing_experiment_ids=[FROZEN_EXPERIMENT_IDS[23]],
        verdict="complete",  # contradicts the nonempty missing_experiment_ids
    )
    with pytest.raises(ScorecardInputError):
        _seal(attempt_evidence=attempts, accounting_report=report)


def test_accounting_complete_but_primary_attempt_not_completed_is_incomplete_not_fail():
    attempts = _all_24_completed_attempts()
    attempts[0] = _attempt(
        FROZEN_EXPERIMENT_IDS[0],
        retry_index=0,
        status="crashed",
        reason_code="child_execution_crashed",
    )
    report = _clean_report(
        status_counts={"completed": 23, "rejected": 0, "crashed": 1, "timeout": 0}
    )
    sealed = _seal(attempt_evidence=attempts, accounting_report=report)
    assert sealed.accounting_complete is True
    assert sealed.all_primary_completed is False
    assert sealed.performance_usable is False


# -- Case 11: canonical-order normalization + hash stability/mutation-sensitivity --


def test_reordered_attempt_input_yields_identical_hash():
    attempts = _all_24_completed_attempts()
    sealed1 = _seal(attempt_evidence=attempts)
    sealed2 = _seal(attempt_evidence=list(reversed(attempts)))
    assert sealed1.trial_accounting_hash == sealed2.trial_accounting_hash


def test_normalized_attempts_are_in_frozen_experiment_order():
    attempts = list(reversed(_all_24_completed_attempts()))
    sealed = _seal(attempt_evidence=attempts)
    assert tuple(a.experiment_id for a in sealed.attempts) == FROZEN_EXPERIMENT_IDS


@pytest.mark.parametrize(
    "mutate",
    [
        lambda a: (
            a.__setitem__("status", "rejected")
            or a.__setitem__("reason_code", "rejected:data_gap_in_position")
        ),
        lambda a: a["scenario_evidence"][0].__setitem__("trade_count", 999),
        lambda a: a["scenario_evidence"][1].__setitem__("artifact_hash", "b" * 64),
        lambda a: a.__setitem__("fold_evidence_hash", "c" * 64),
        lambda a: a.__setitem__("run_identity", "d" * 64),
    ],
)
def test_mutating_any_nested_evidence_field_changes_the_hash(mutate):
    attempts = _all_24_completed_attempts()
    baseline = _seal(attempt_evidence=attempts)
    mutated_attempts = _all_24_completed_attempts()
    mutate(mutated_attempts[0])
    counts = {"completed": 24, "rejected": 0, "crashed": 0, "timeout": 0}
    if mutated_attempts[0]["status"] != "completed":
        counts = {"completed": 23, "rejected": 1, "crashed": 0, "timeout": 0}
    report = _clean_report(status_counts=counts)
    mutated = _seal(attempt_evidence=mutated_attempts, accounting_report=report)
    assert baseline.trial_accounting_hash != mutated.trial_accounting_hash


def test_mutating_the_report_alone_changes_the_hash():
    """``mismatch_experiment_ids`` is the one report field this seal does
    not independently recompute from attempt evidence (no data available to
    do so -- see module docstring); mutating it alone -- with no attempt
    change -- still changes the trial_accounting_hash and correctly forces
    ``accounting_complete=False``."""
    baseline = _seal()
    mutated_report = _clean_report(
        mismatch_experiment_ids=[FROZEN_EXPERIMENT_IDS[0]], verdict="incomplete"
    )
    mutated = _seal(accounting_report=mutated_report)
    assert baseline.trial_accounting_hash != mutated.trial_accounting_hash
    assert mutated.accounting_complete is False
    assert mutated.performance_usable is False


def test_caller_owned_mutable_attempt_list_is_snapshotted():
    attempts = _all_24_completed_attempts()
    sealed = _seal(attempt_evidence=attempts)
    original_hash = sealed.trial_accounting_hash
    attempts[0]["scenario_evidence"][0]["trade_count"] = 999999
    assert sealed.trial_accounting_hash == original_hash

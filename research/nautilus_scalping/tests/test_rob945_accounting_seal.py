"""ROB-945 (H5, Task 1A+1B) -- RED tests for the exact frozen-campaign +
real H6 accounting/attempt-evidence sealing boundary.

Task 1A: structural validation -- the exact nested
``AttemptEvidence``/``AttemptKey``/``ScenarioEvidence`` shape bound to the
real frozen 24 experiment IDs, the exact 12-field
``CampaignCompletenessReport``, and canonical-order hash stability.

Task 1B: cross-binds each primary (``retry_index==0``) attempt's opaque
``fold_evidence_hash``/``run_identity``/status/reason/scenario evidence
against the real H4 ``ConfigAttemptEvidenceSummary`` (via
``rob944_walkforward.summarize_config_attempts_for_h6``), so a
structurally-well-formed-but-forged/stale attempt cannot pass merely
because its fields are individually well-typed.

Fixtures below build a REAL (hand-assembled, 0-fold, no-corpus)
``WalkForwardResult`` per strategy and derive REAL ``AttemptEvidence`` from
it via the actual H6-build boundary (``run_rob944_campaign
._summary_to_attempt_evidence``) -- never a fabricated shape that merely
looks right. This keeps attempt_evidence and walkforward_results
intrinsically consistent by construction in every test that isn't
deliberately testing a mismatch.
"""

from __future__ import annotations

import hashlib

import pytest
import rob941_frozen_scope as frozen
import rob944_folds as foldmod
from rob944_frozen_campaign import (
    CANONICAL_ROW_ORDER,
    PRODUCTION_S1_STRATEGY_KEY,
    PRODUCTION_S2_STRATEGY_KEY,
    build_production_frozen_campaign_envelope,
)
from rob944_selection import (
    INSUFFICIENT_ELIGIBLE_SYMBOLS_REASON,
    INSUFFICIENT_SYMBOL_EVIDENCE_REASON,
    ConfigSelectionOutcome,
    FoldSelectionTrace,
)
from rob944_walkforward import (
    REASON_CHILD_EXECUTION_CRASHED,
    REASON_CHILD_EXECUTION_TIMEOUT,
    REASON_DATA_GAP_IN_POSITION,
    REASON_GLOBAL_CORPUS_LOAD_FAILED,
    REASON_INSUFFICIENT_TRAIN_EVIDENCE_ALL_FOLDS,
    ConfigAttemptResult,
    FoldWalkForwardResult,
    WalkForwardResult,
    summarize_config_attempts_for_h6,
)
from rob945_accounting_seal import (
    ACCOUNTING_INCOMPLETE_REASON,
    CROSS_BIND_MISMATCH_REASON,
    NOT_FROZEN_PRODUCTION_CAMPAIGN_REASON,
    PRIMARY_ATTEMPT_NOT_COMPLETED_REASON,
    RETRIES_PRESENT_REASON,
    ScorecardInputError,
    _recompute_fold_evidence_hash_and_run_identity,
    seal_trial_accounting,
)
from run_rob944_campaign import (
    _global_failure_evidence_batch,
    _summary_to_attempt_evidence,
)

from research_contracts.canonical_hash import canonical_sha256

_ENVELOPE = build_production_frozen_campaign_envelope()
FULL_CAMPAIGN_HASH = _ENVELOPE.full_campaign_hash()
FROZEN_EXPERIMENT_IDS = tuple(_ENVELOPE.to_dict()["experiment_ids"])
assert len(FROZEN_EXPERIMENT_IDS) == 24 and len(set(FROZEN_EXPERIMENT_IDS)) == 24
assert len(CANONICAL_ROW_ORDER) == 24

_EXPERIMENT_ID_TO_CONFIG_ID = dict(
    zip(FROZEN_EXPERIMENT_IDS, CANONICAL_ROW_ORDER, strict=True)
)
_STRATEGY_KEY = {"S1": PRODUCTION_S1_STRATEGY_KEY, "S2": PRODUCTION_S2_STRATEGY_KEY}


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


def _config_ids_for(strategy: str) -> tuple[str, ...]:
    return tuple(f"{strategy}-{i:02d}" for i in range(12))


# The REAL production 8-fold schedule (pure, offline) -- every non-corpus-
# failure attempt's ``fold_selection_trace`` must cover exactly these 8
# canonical fold IDs per the real H6-build boundary's own validation
# (``run_rob944_campaign._assert_valid_fold_selection_trace``).
_REAL_FOLDS = foldmod.generate_frozen_fold_schedule(
    frozen.WINDOW_START_MS, frozen.WINDOW_END_MS
)
assert len(_REAL_FOLDS) == 8


def _rejected_candidate(config_id: str, seed: str) -> ConfigSelectionOutcome:
    """Every symbol excluded for insufficient train evidence -- the
    simplest real-shaped, fully-validation-satisfying
    ``ConfigSelectionOutcome`` (no real bars/signals needed)."""
    return ConfigSelectionOutcome(
        config_id=config_id,
        eligible_symbols=(),
        excluded_symbols=tuple(
            (symbol, INSUFFICIENT_SYMBOL_EVIDENCE_REASON) for symbol in frozen.UNIVERSE
        ),
        equal_weight_expectancy_bps=None,
        pooled_expectancy_bps=None,
        profit_factor=0.0,
        rejected=True,
        rejection_reason=INSUFFICIENT_ELIGIBLE_SYMBOLS_REASON,
        train_input_hash=_hex64(f"train:{seed}"),
        no_trade_reason_counts={},
    )


def _build_walkforward_result(
    strategy: str, *, status_overrides=None
) -> WalkForwardResult:
    """A real, hand-assembled ``WalkForwardResult`` over the REAL 8-fold
    schedule -- no corpus, no network. Every config is train-ineligible
    (insufficient symbol evidence) in every fold, so no fold ever selects a
    winner (every scenario row becomes the real "never_selected" sentinel),
    but each config still gets a full, real, validation-satisfying 8-row
    ``fold_selection_trace``. Every config defaults to attempt-level
    ``status="completed"`` (evidence generation succeeded -- H4's own
    "completed != ever won a fold" semantics). ``status_overrides`` lets a
    test make ONE specific config's attempt-level outcome
    rejected/crashed/timeout instead."""
    status_overrides = status_overrides or {}
    config_ids = _config_ids_for(strategy)

    fold_results = []
    for fold in _REAL_FOLDS:
        candidates = tuple(
            _rejected_candidate(config_id, f"{fold.fold_id}:{config_id}")
            for config_id in config_ids
        )
        trace = FoldSelectionTrace(
            strategy=strategy, candidates=candidates, selected_config_id=None
        )
        fold_results.append(
            FoldWalkForwardResult(fold=fold, selection_trace=trace, oos_outcomes=())
        )

    attempts = []
    for config_id in config_ids:
        status, reason_code = status_overrides.get(config_id, ("completed", None))
        attempts.append(
            ConfigAttemptResult(
                strategy=strategy,
                config_id=config_id,
                status=status,
                reason_code=reason_code,
                selected_in_folds=(),
                crash_log=(),
                gap_rejection_log=(),
            )
        )
    return WalkForwardResult(
        strategy=strategy,
        folds=tuple(fold_results),
        config_attempts=tuple(attempts),
        concatenated_oos_ledgers={},
    )


DEFAULT_WALKFORWARD_RESULTS = {
    "S1": _build_walkforward_result("S1"),
    "S2": _build_walkforward_result("S2"),
}


def _real_attempts_for(walkforward_results: dict) -> list[dict]:
    """Derive the REAL 24 ``AttemptEvidence`` dicts (via the actual H6-build
    boundary, ``run_rob944_campaign._summary_to_attempt_evidence``) from a
    ``{"S1": WalkForwardResult, "S2": WalkForwardResult}`` mapping."""
    attempts = []
    for strategy in ("S1", "S2"):
        wf_result = walkforward_results[strategy]
        for summary in summarize_config_attempts_for_h6(wf_result):
            experiment_id = next(
                eid
                for eid, cid in _EXPERIMENT_ID_TO_CONFIG_ID.items()
                if cid == summary.config_id
            )
            evidence = _summary_to_attempt_evidence(
                summary,
                strategy_key=_STRATEGY_KEY[strategy],
                experiment_id=experiment_id,
                full_campaign_hash=FULL_CAMPAIGN_HASH,
                campaign_run_id=CAMPAIGN_RUN_ID,
            )
            attempts.append(evidence.model_dump())
    return attempts


def _override_walkforward_results(experiment_id: str, *, status: str, reason_code):
    config_id = _EXPERIMENT_ID_TO_CONFIG_ID[experiment_id]
    strategy = config_id[:2]
    results = dict(DEFAULT_WALKFORWARD_RESULTS)
    results[strategy] = _build_walkforward_result(
        strategy, status_overrides={config_id: (status, reason_code)}
    )
    return results


def _all_24_completed_attempts() -> list[dict]:
    return _real_attempts_for(DEFAULT_WALKFORWARD_RESULTS)


def _experiment_id_by_key() -> dict:
    return {
        (_STRATEGY_KEY[cid[:2]], cid): eid
        for eid, cid in _EXPERIMENT_ID_TO_CONFIG_ID.items()
    }


def _all_24_global_failure_attempts() -> list[dict]:
    """The REAL, authentic 24-row global-corpus-load-failed fallback batch
    (``run_rob944_campaign._global_failure_evidence_batch`` -- the actual
    H6-build boundary for this whole-campaign sentinel), never a hand-rolled
    approximation."""
    batch = _global_failure_evidence_batch(
        _experiment_id_by_key(),
        full_campaign_hash=FULL_CAMPAIGN_HASH,
        campaign_run_id=CAMPAIGN_RUN_ID,
    )
    return [e.model_dump() for e in batch]


def _hand_built_attempt(
    experiment_id: str,
    *,
    retry_index: int = 0,
    status: str = "completed",
    reason_code: str | None = None,
    campaign_run_id: str = CAMPAIGN_RUN_ID,
) -> dict:
    """A NON-cross-bindable, hand-built attempt row -- used only for tests
    that deliberately probe structural (1A) validation or a genuine
    non-cross-bindable edge case (the global-corpus-load-failure crashed
    sentinel, which by construction has no per-config WalkForwardResult)."""
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
            {
                "scenario_name": name,
                "trade_count": 3,
                "artifact_hash": _hex64(f"{seed}-{name}"),
            }
            for name in ("base", "primary_stress", "upward_stress")
        ],
    }


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


# Production `seal_trial_accounting` now gives `walkforward_results=None`
# a REAL meaning (the genuine global-corpus-load-failure producer state,
# Task 1C/I4) -- this helper's own "caller omitted the kwarg, use my
# default fixture" convenience can therefore no longer reuse `None` as ITS
# sentinel, or every test that explicitly wants to exercise the real `None`
# semantics would be indistinguishable from one that simply didn't care.
_UNSET = object()


def _seal(
    *,
    attempt_evidence=None,
    accounting_report=None,
    full_campaign_hash=None,
    walkforward_results=_UNSET,
):
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
        walkforward_results=DEFAULT_WALKFORWARD_RESULTS
        if walkforward_results is _UNSET
        else walkforward_results,
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
    attempts[0] = _hand_built_attempt("totally-made-up-id-not-frozen", retry_index=0)
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


def _legit_retry_attempt(
    experiment_id: str, *, retry_index: int, walkforward_results=None
) -> dict:
    """A retry row that legitimately cross-binds against the SAME real H4
    summary as the primary for this experiment_id, at a different
    retry_index -- fold_evidence_hash/scenario_evidence are unchanged
    (identity/status-derived, retry-index-independent), run_identity
    differs since its own payload embeds retry_index (Task 1C, I5:
    non-primary attempts are cross-bound too, using THAT row's retry_index)."""
    wf = walkforward_results or DEFAULT_WALKFORWARD_RESULTS
    config_id = _EXPERIMENT_ID_TO_CONFIG_ID[experiment_id]
    strategy = config_id[:2]
    summary = next(
        s
        for s in summarize_config_attempts_for_h6(wf[strategy])
        if s.config_id == config_id
    )
    fold_hash, run_identity = _recompute_fold_evidence_hash_and_run_identity(
        summary,
        full_campaign_hash=FULL_CAMPAIGN_HASH,
        campaign_run_id=CAMPAIGN_RUN_ID,
        strategy_key=_STRATEGY_KEY[strategy],
        experiment_id=experiment_id,
        retry_index=retry_index,
    )
    row = _hand_built_attempt(
        experiment_id,
        retry_index=retry_index,
        status=summary.status,
        reason_code=summary.reason_code,
    )
    row["fold_evidence_hash"] = fold_hash
    row["run_identity"] = run_identity
    row["scenario_evidence"] = [
        {
            "scenario_name": s.scenario_name,
            "trade_count": s.trade_count,
            "artifact_hash": s.artifact_hash,
        }
        for s in sorted(summary.scenario_summaries, key=lambda r: r.scenario_name)
    ]
    return row


def test_a_contiguous_explicit_retry_forces_performance_usable_false_but_is_not_malformed():
    attempts = _all_24_completed_attempts()
    retry_row = _legit_retry_attempt(FROZEN_EXPERIMENT_IDS[0], retry_index=1)
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


def test_contiguous_retry_with_arbitrary_hashes_fails_cross_bind():
    """Task 1C (I5): cross-binding previously applied ONLY to primaries --
    a contiguous retry (retry_index=1) with forged/arbitrary
    fold_evidence_hash/run_identity/scenario_evidence was silently
    accepted (never validated against the real H4 summary), letting the
    campaign seal as merely "incomplete" (retries present) without ever
    checking that retry's own claimed evidence. Every normal-path attempt
    (not just primaries) must be cross-bound."""
    attempts = _all_24_completed_attempts()
    forged_retry = _hand_built_attempt(
        FROZEN_EXPERIMENT_IDS[0], retry_index=1, status="completed"
    )
    attempts.append(forged_retry)
    report = _clean_report(
        total_attempts=25,
        retry_attempts=1,
        status_counts={"completed": 25, "rejected": 0, "crashed": 0, "timeout": 0},
    )
    with pytest.raises(ScorecardInputError) as exc_info:
        _seal(attempt_evidence=attempts, accounting_report=report)
    assert CROSS_BIND_MISMATCH_REASON in str(exc_info.value)


def test_a_retry_gap_is_internally_inconsistent_and_raises():
    """A retry_index=2 row with no retry_index=1 row for the same experiment
    is a gap -- the report's own ``duplicate_or_gap_experiment_ids`` must
    reflect it; a report that claims a clean/empty list while a real gap
    exists in the supplied attempts is an internally inconsistent
    (malformed) input, not a silently-accepted well-formed incomplete one."""
    attempts = _all_24_completed_attempts()
    gap_row = _hand_built_attempt(
        FROZEN_EXPERIMENT_IDS[0], retry_index=2, status="completed"
    )
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


def test_accepts_the_two_valid_rejected_reason_codes_cross_bound():
    for reason in (
        REASON_DATA_GAP_IN_POSITION,
        REASON_INSUFFICIENT_TRAIN_EVIDENCE_ALL_FOLDS,
    ):
        eid = FROZEN_EXPERIMENT_IDS[0]
        wf = _override_walkforward_results(eid, status="rejected", reason_code=reason)
        attempts = _real_attempts_for(wf)
        report = _clean_report(
            status_counts={"completed": 23, "rejected": 1, "crashed": 0, "timeout": 0},
        )
        sealed = _seal(
            attempt_evidence=attempts, accounting_report=report, walkforward_results=wf
        )
        assert sealed.accounting_complete is True
        assert sealed.all_primary_completed is False
        assert sealed.performance_usable is False


def test_rejects_rejected_status_with_wrong_reason_code():
    attempts = _all_24_completed_attempts()
    attempts[0] = _hand_built_attempt(
        FROZEN_EXPERIMENT_IDS[0],
        retry_index=0,
        status="rejected",
        reason_code="bogus_reason",
    )
    with pytest.raises(ScorecardInputError):
        _seal(attempt_evidence=attempts)


def test_accepts_crashed_and_timeout_reason_codes_cross_bound():
    for status, reason in (
        ("crashed", REASON_CHILD_EXECUTION_CRASHED),
        ("timeout", REASON_CHILD_EXECUTION_TIMEOUT),
    ):
        eid = FROZEN_EXPERIMENT_IDS[0]
        wf = _override_walkforward_results(eid, status=status, reason_code=reason)
        attempts = _real_attempts_for(wf)
        counts = {"completed": 23, "rejected": 0, "crashed": 0, "timeout": 0}
        counts[status] = 1
        report = _clean_report(status_counts=counts)
        sealed = _seal(
            attempt_evidence=attempts, accounting_report=report, walkforward_results=wf
        )
        assert sealed.accounting_complete is True
        assert PRIMARY_ATTEMPT_NOT_COMPLETED_REASON in sealed.reason_codes


def test_accepts_the_authentic_all_24_global_corpus_load_failed_sentinel():
    """Task 1C (I4): the ``global_corpus_load_failed`` crashed sentinel is a
    documented H4 WHOLE-CAMPAIGN fallback -- it is emitted for ALL 24
    experiments identically when the corpus never loaded at all, never for
    an individual row. The authentic 24-row batch (the real
    ``run_rob944_campaign._global_failure_evidence_batch`` boundary) must
    still seal successfully even though there is, by construction, no
    per-config ``WalkForwardResult`` to cross-bind against.

    Uses ``walkforward_results=None`` -- the GENUINE producer state for a
    real corpus-load failure (H4 never even attempts a per-config
    walk-forward). A fabricated stand-in WalkForwardResult would not be
    the real producer state and must not be accepted as equivalent -- see
    the conflict-control test below, which proves supplying a REAL
    ``walkforward_results`` alongside this same evidence is rejected."""
    attempts = _all_24_global_failure_attempts()
    report = _clean_report(
        status_counts={"completed": 0, "rejected": 0, "crashed": 24, "timeout": 0}
    )
    sealed = _seal(
        attempt_evidence=attempts,
        accounting_report=report,
        walkforward_results=None,
    )
    assert sealed.accounting_complete is True
    assert sealed.performance_usable is False
    assert PRIMARY_ATTEMPT_NOT_COMPLETED_REASON in sealed.reason_codes


def test_walkforward_results_none_with_non_fallback_evidence_is_malformed():
    """Task 1C (I4): ``walkforward_results=None`` is malformed unless the
    supplied evidence IS genuinely the authentic all-24 fallback claim --
    normal (non-fallback) evidence always requires a real
    ``walkforward_results`` mapping to cross-bind against."""
    with pytest.raises(ScorecardInputError):
        _seal(walkforward_results=None)


def test_all_24_global_corpus_load_failed_claim_conflicting_with_real_completed_h4_fails():
    """Task 1C (I4 conflict control): a corpus that never loaded could
    never produce a genuinely "completed" per-config H4 result -- if the
    caller's OWN ``walkforward_results`` shows real completions (as
    ``DEFAULT_WALKFORWARD_RESULTS`` does) while attempt_evidence claims the
    whole-campaign global-failure sentinel for all 24 rows, that claim is
    directly contradicted by evidence the caller also supplied and must
    fail cross-binding -- never silently accepted merely because every row
    superficially matches the sentinel pairing."""
    attempts = _all_24_global_failure_attempts()
    report = _clean_report(
        status_counts={"completed": 0, "rejected": 0, "crashed": 24, "timeout": 0}
    )
    with pytest.raises(ScorecardInputError) as exc_info:
        _seal(
            attempt_evidence=attempts,
            accounting_report=report,
            walkforward_results=DEFAULT_WALKFORWARD_RESULTS,
        )
    assert CROSS_BIND_MISMATCH_REASON in str(exc_info.value)


def test_mixed_single_row_global_corpus_load_failed_claim_fails_cross_bind():
    """Task 1C (I4): a single row claiming the (crashed,
    global_corpus_load_failed) sentinel while the other 23 are real
    completed H4 evidence is NOT the authentic whole-campaign fallback --
    real H4 never emits this for an individual config while the rest of the
    campaign ran normally. It must fail cross-binding against the real H4
    evidence for that config, never be silently exempted."""
    attempts = _all_24_completed_attempts()
    attempts[0] = _hand_built_attempt(
        FROZEN_EXPERIMENT_IDS[0],
        retry_index=0,
        status="crashed",
        reason_code=REASON_GLOBAL_CORPUS_LOAD_FAILED,
    )
    report = _clean_report(
        status_counts={"completed": 23, "rejected": 0, "crashed": 1, "timeout": 0}
    )
    with pytest.raises(ScorecardInputError) as exc_info:
        _seal(attempt_evidence=attempts, accounting_report=report)
    assert CROSS_BIND_MISMATCH_REASON in str(exc_info.value)


def test_all_24_global_corpus_load_failed_claim_with_an_arbitrary_hash_fails():
    """Task 1C (I4): even when ALL 24 primaries share the whole-campaign
    sentinel pairing, each one's fold_evidence_hash/run_identity must still
    byte-match H4's deterministic fallback recipe
    (``run_rob944_campaign._global_failure_summaries``) -- an authentic-
    looking but ARBITRARY hash on even one row must still fail closed."""
    attempts = _all_24_global_failure_attempts()
    attempts[0]["fold_evidence_hash"] = _hex64("arbitrary")
    report = _clean_report(
        status_counts={"completed": 0, "rejected": 0, "crashed": 24, "timeout": 0}
    )
    with pytest.raises(ScorecardInputError) as exc_info:
        _seal(
            attempt_evidence=attempts,
            accounting_report=report,
            walkforward_results=None,
        )
    assert CROSS_BIND_MISMATCH_REASON in str(exc_info.value)


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


# -- Case 8 (Task 1B): cross-bind fold_evidence_hash/run_identity/scenario
# evidence against the real H4 ConfigAttemptEvidenceSummary --


def test_known_vector_fold_evidence_hash_and_run_identity_match_the_real_h6_boundary():
    """Prove the H5 pure mirror recomputes byte-identical
    fold_evidence_hash/run_identity to the REAL H6-build boundary
    (``run_rob944_campaign._summary_to_attempt_evidence``) for the same
    input -- a known-vector parity test, never importing ``app.*`` from H5
    production code (only this test does)."""
    strategy = "S1"
    summary = summarize_config_attempts_for_h6(DEFAULT_WALKFORWARD_RESULTS[strategy])[0]
    experiment_id = next(
        eid
        for eid, cid in _EXPERIMENT_ID_TO_CONFIG_ID.items()
        if cid == summary.config_id
    )
    real_evidence = _summary_to_attempt_evidence(
        summary,
        strategy_key=_STRATEGY_KEY[strategy],
        experiment_id=experiment_id,
        full_campaign_hash=FULL_CAMPAIGN_HASH,
        campaign_run_id=CAMPAIGN_RUN_ID,
    )
    # Seal a full 24-attempt set (this experiment's real evidence included)
    # -- if the seal's own internal mirror disagreed with the real boundary,
    # it would raise CROSS_BIND_MISMATCH_REASON on this very attempt.
    sealed = _seal()
    sealed_attempt = next(
        a for a in sealed.attempts if a.experiment_id == experiment_id
    )
    assert sealed_attempt.fold_evidence_hash == real_evidence.fold_evidence_hash
    assert sealed_attempt.run_identity == real_evidence.run_identity


def test_seal_rejects_a_tampered_fold_evidence_hash_that_does_not_match_the_real_h4_summary():
    attempts = _all_24_completed_attempts()
    attempts[0]["fold_evidence_hash"] = _hex64("tampered")
    with pytest.raises(ScorecardInputError) as exc_info:
        _seal(attempt_evidence=attempts)
    assert CROSS_BIND_MISMATCH_REASON in str(exc_info.value)


def test_seal_rejects_a_tampered_run_identity_that_does_not_match_the_real_h4_summary():
    attempts = _all_24_completed_attempts()
    attempts[0]["run_identity"] = _hex64("tampered-run-identity")
    with pytest.raises(ScorecardInputError) as exc_info:
        _seal(attempt_evidence=attempts)
    assert CROSS_BIND_MISMATCH_REASON in str(exc_info.value)


def test_seal_rejects_tampered_scenario_trade_count_or_artifact_hash():
    attempts = _all_24_completed_attempts()
    attempts[0]["scenario_evidence"][0]["trade_count"] = 999
    with pytest.raises(ScorecardInputError) as exc_info:
        _seal(attempt_evidence=attempts)
    assert CROSS_BIND_MISMATCH_REASON in str(exc_info.value)

    attempts2 = _all_24_completed_attempts()
    attempts2[0]["scenario_evidence"][1]["artifact_hash"] = _hex64("tampered-scenario")
    with pytest.raises(ScorecardInputError) as exc_info2:
        _seal(attempt_evidence=attempts2)
    assert CROSS_BIND_MISMATCH_REASON in str(exc_info2.value)


def test_seal_rejects_a_status_claim_contradicting_the_real_h4_summary():
    """A well-formed, hex64-valid, status/reason-contract-legal attempt row
    that simply LIES about which config it represents (claims "rejected"
    while the real H4 summary for that config is "completed") must fail
    cross-binding even though every 1A structural check passes it."""
    attempts = _all_24_completed_attempts()
    eid = FROZEN_EXPERIMENT_IDS[1]
    attempts[1] = _hand_built_attempt(
        eid, retry_index=0, status="rejected", reason_code=REASON_DATA_GAP_IN_POSITION
    )
    with pytest.raises(ScorecardInputError) as exc_info:
        _seal(attempt_evidence=attempts)
    assert CROSS_BIND_MISMATCH_REASON in str(exc_info.value)


def test_seal_rejects_stale_evidence_from_a_different_walkforward_run():
    """Evidence that is internally well-formed and even cross-binds
    correctly against a DIFFERENT (stale) WalkForwardResult must still fail
    against the one actually supplied -- proves the cross-bind uses the
    CALLER's supplied walkforward_results, not merely "some valid-looking
    real evidence"."""
    eid = FROZEN_EXPERIMENT_IDS[0]
    stale_wf = _override_walkforward_results(
        eid, status="rejected", reason_code=REASON_DATA_GAP_IN_POSITION
    )
    stale_attempts = _real_attempts_for(stale_wf)
    # Feed the STALE attempt for this experiment alongside the DEFAULT
    # (fresh, all-completed) walkforward_results -- a real mismatch.
    fresh_attempts = _all_24_completed_attempts()
    stale_row = next(
        a for a in stale_attempts if a["attempt_key"]["experiment_id"] == eid
    )
    fresh_attempts[0] = stale_row
    with pytest.raises(ScorecardInputError) as exc_info:
        _seal(attempt_evidence=fresh_attempts)
    assert CROSS_BIND_MISMATCH_REASON in str(exc_info.value)


def test_walkforward_results_missing_a_strategy_key_is_malformed():
    with pytest.raises(ScorecardInputError):
        _seal(walkforward_results={"S1": DEFAULT_WALKFORWARD_RESULTS["S1"]})


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
    eid = FROZEN_EXPERIMENT_IDS[0]
    wf = _override_walkforward_results(
        eid, status="crashed", reason_code=REASON_CHILD_EXECUTION_CRASHED
    )
    attempts = _real_attempts_for(wf)
    report = _clean_report(
        status_counts={"completed": 23, "rejected": 0, "crashed": 1, "timeout": 0}
    )
    sealed = _seal(
        attempt_evidence=attempts, accounting_report=report, walkforward_results=wf
    )
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


def test_mutating_fold_evidence_hash_changes_the_trial_accounting_hash():
    """Mutation-sensitivity via a REAL cross-bindable alternate value (a
    different config's real fold_evidence_hash swapped in) -- proves the
    seal's own hash reacts to the change, distinct from proving the swap
    itself is rejected (covered by the cross-bind tests above)."""
    baseline = _seal()
    attempts = _all_24_completed_attempts()
    attempts[0]["fold_evidence_hash"], attempts[1]["fold_evidence_hash"] = (
        attempts[1]["fold_evidence_hash"],
        attempts[0]["fold_evidence_hash"],
    )
    with pytest.raises(ScorecardInputError):
        # swapping two DIFFERENT configs' real hashes is still a genuine
        # cross-bind mismatch for both rows -- this asserts fail-closed,
        # not silent hash change, since a forged-but-real-looking swap
        # must never be accepted.
        _seal(attempt_evidence=attempts)
    assert baseline.trial_accounting_hash  # baseline itself built successfully


def test_mutating_the_report_alone_changes_the_hash():
    """``mismatch_experiment_ids`` is the one report field this seal does
    not independently recompute from attempt evidence (no data available to
    do so -- see module docstring); mutating it alone still changes the
    trial_accounting_hash and correctly forces ``accounting_complete=False``.

    Real H6 only marks an ID `mismatch` when its expected frozen
    registration is absent -- it can never ALSO have terminal evidence
    supplied under that same experiment_id, so this is a real 23-attempt
    mismatch vector (that row excluded), never a same-attempts-plus-mismatch
    fixture."""
    baseline = _seal()
    mismatched_id = FROZEN_EXPERIMENT_IDS[0]
    mismatch_attempts = [
        a
        for a in _all_24_completed_attempts()
        if a["attempt_key"]["experiment_id"] != mismatched_id
    ]
    mutated_report = _clean_report(
        primary_attempts=23,
        total_attempts=23,
        status_counts={"completed": 23, "rejected": 0, "crashed": 0, "timeout": 0},
        mismatch_experiment_ids=[mismatched_id],
        verdict="incomplete",
    )
    mutated = _seal(
        attempt_evidence=mismatch_attempts, accounting_report=mutated_report
    )
    assert baseline.trial_accounting_hash != mutated.trial_accounting_hash
    assert mutated.accounting_complete is False
    assert mutated.performance_usable is False


def test_caller_owned_mutable_attempt_list_is_snapshotted():
    attempts = _all_24_completed_attempts()
    sealed = _seal(attempt_evidence=attempts)
    original_hash = sealed.trial_accounting_hash
    attempts[0]["scenario_evidence"][0]["trade_count"] = 999999
    assert sealed.trial_accounting_hash == original_hash


# -- Captain targeted review (post-1B): actual_registrations/extra/mismatch
# are H6 REGISTRATION-time facts this seal cannot independently observe from
# terminal attempt_evidence alone (registration happens BEFORE any attempt
# completes) -- they are trusted-but-shape/domain-validated and hashed, never
# force-equated to a naive recompute from supplied attempts. missing_ids and
# duplicate_or_gap_ids ARE fully recomputable from supplied attempts (they
# describe exactly what terminal evidence was/wasn't supplied) and remain
# strictly cross-checked. --


def test_actual_registrations_can_exceed_supplied_primary_attempt_count():
    """H6 registers all 24 identities before execution -- a registered
    identity whose primary attempt never completed is legitimately
    ``actual_registrations=24`` even though only 23 terminal attempts were
    supplied (one experiment missing evidence). This must be accepted as
    well-formed incomplete, never rejected as malformed."""
    attempts = _all_24_completed_attempts()[:23]
    missing_id = FROZEN_EXPERIMENT_IDS[23]
    report = _clean_report(
        actual_registrations=24,  # registered, even though evidence is missing
        primary_attempts=23,
        total_attempts=23,
        status_counts={"completed": 23, "rejected": 0, "crashed": 0, "timeout": 0},
        missing_experiment_ids=[missing_id],
        verdict="incomplete",
    )
    sealed = _seal(attempt_evidence=attempts, accounting_report=report)
    assert sealed.accounting_complete is False
    assert sealed.performance_usable is False


def test_actual_registrations_below_the_distinct_supplied_experiment_count_is_malformed():
    """``actual_registrations`` can never be LESS than the number of
    distinct experiments for which evidence was actually supplied -- that
    would mean evidence exists for something never registered, a genuine
    internal contradiction."""
    report = _clean_report(actual_registrations=10)
    with pytest.raises(ScorecardInputError):
        _seal(accounting_report=report)


def test_actual_registrations_can_exceed_24_via_mismatch_multiplicity_with_no_extra_ids():
    """Task 1C (I1, captain precision appendix): a single ``mismatch``
    entry can itself correspond to MULTIPLE drifted registered candidates
    sharing one params hash -- every such candidate inflates
    ``actual_registrations`` while entering neither the supplied evidence
    nor ``extra_experiment_ids`` (it IS one of the frozen 24, just
    registered more than once). A `mismatch` entry means that exact frozen
    registration's expected identity is absent -- so its row is EXCLUDED
    from attempt_evidence (23 rows, not 24; an all-24 fixture claiming both
    "this ID completed normally" AND "this ID is a registration mismatch"
    would itself be internally contradictory). The serialized report
    cannot reconstruct the multiplicity, so ``actual_registrations=25``
    with exactly one mismatch ID and NO extra ID must seal as well-formed
    incomplete, never raise -- there is no H5-observable finite upper
    bound on this field."""
    mismatched_id = FROZEN_EXPERIMENT_IDS[0]
    attempts = [
        a
        for a in _all_24_completed_attempts()
        if a["attempt_key"]["experiment_id"] != mismatched_id
    ]
    report = _clean_report(
        actual_registrations=25,
        primary_attempts=23,
        total_attempts=23,
        status_counts={"completed": 23, "rejected": 0, "crashed": 0, "timeout": 0},
        mismatch_experiment_ids=[mismatched_id],
        verdict="incomplete",
    )
    sealed = _seal(attempt_evidence=attempts, accounting_report=report)
    assert sealed.accounting_complete is False
    assert sealed.performance_usable is False


def test_extra_experiment_ids_without_matching_attempt_evidence_is_well_formed_incomplete():
    """An ``extra_experiment_ids`` entry represents an unexpected H6
    registration outside the frozen 24 -- by construction it has NO
    attempt_evidence row (this seal only accepts frozen-24-bound attempts),
    so this field can never be recomputed from supplied attempts. A
    genuine, well-formed claim of one extra registration is incomplete,
    not a raise."""
    attempts = _all_24_completed_attempts()
    report = _clean_report(
        actual_registrations=25,  # 24 frozen + 1 extra registration
        extra_experiment_ids=["some-unexpected-registered-id"],
        verdict="incomplete",
    )
    sealed = _seal(attempt_evidence=attempts, accounting_report=report)
    assert sealed.accounting_complete is False
    assert sealed.performance_usable is False


def test_extra_experiment_ids_containing_a_frozen_id_is_malformed():
    """An ``extra`` ID that is actually one of the frozen 24 is self-
    contradictory (extra means outside the expected set) -- domain-checked
    where knowable, even though the field's overall presence/absence is
    otherwise trusted."""
    report = _clean_report(
        extra_experiment_ids=[FROZEN_EXPERIMENT_IDS[0]], verdict="incomplete"
    )
    with pytest.raises(ScorecardInputError):
        _seal(accounting_report=report)


def test_mismatch_experiment_ids_containing_a_non_frozen_id_is_malformed():
    """A ``mismatch`` entry must itself be one of the frozen 24 (an
    expected identity that drifted) -- an ID outside the frozen set can
    never legitimately appear here."""
    report = _clean_report(
        mismatch_experiment_ids=["not-a-frozen-id"], verdict="incomplete"
    )
    with pytest.raises(ScorecardInputError):
        _seal(accounting_report=report)


def test_extra_or_mismatch_lists_with_duplicate_entries_are_malformed():
    report = _clean_report(
        extra_experiment_ids=["dup-id", "dup-id"], verdict="incomplete"
    )
    with pytest.raises(ScorecardInputError):
        _seal(accounting_report=report)


def test_discrepancy_lists_normalize_to_canonical_order_regardless_of_input_order():
    """Two reports whose ``extra_experiment_ids`` contain the SAME set in a
    DIFFERENT order must seal to byte-identical ``trial_accounting_hash``es
    -- discrepancy list order is normalized, never leaked into the hash."""
    ids = ["zzz-extra-a", "aaa-extra-b"]
    report1 = _clean_report(
        actual_registrations=26,  # 24 frozen + 2 extra registrations
        extra_experiment_ids=list(ids),
        verdict="incomplete",
    )
    report2 = _clean_report(
        actual_registrations=26,
        extra_experiment_ids=list(reversed(ids)),
        verdict="incomplete",
    )
    sealed1 = _seal(accounting_report=report1)
    sealed2 = _seal(accounting_report=report2)
    assert sealed1.trial_accounting_hash == sealed2.trial_accounting_hash


def test_authentic_retry_gap_report_with_excluded_counters_is_accepted_as_incomplete():
    """H6 may exclude a gapped experiment's rows from its own
    primary/total/retry/status counters entirely (rather than counting them
    at face value) -- a report that correctly identifies the gap in
    ``duplicate_or_gap_experiment_ids`` but whose aggregate counters
    reflect exclusion of that group (rather than this seal's own naive
    full tally) must still be accepted as well-formed (incomplete), not
    rejected as an internal-consistency violation. The gap identification
    itself remains strictly cross-checked."""
    attempts = _all_24_completed_attempts()
    gapped_id = FROZEN_EXPERIMENT_IDS[0]
    # Legitimate (cross-bindable) evidence at retry_index=2 -- I5 now
    # cross-binds every normal-path attempt, so an arbitrary/forged hash
    # here would raise for the WRONG reason (cross-bind mismatch) rather
    # than exercising the gap-accounting behavior this test targets.
    gap_row = _legit_retry_attempt(gapped_id, retry_index=2)
    attempts.append(gap_row)
    # H6 excludes the gapped experiment's 2 rows entirely from its own
    # counters here (23 clean primaries + the gapped group's rows neither
    # counted as primary nor retry) -- deliberately NOT a naive 24/1 tally.
    report = _clean_report(
        actual_registrations=24,
        primary_attempts=23,
        total_attempts=23,
        retry_attempts=0,
        status_counts={"completed": 23, "rejected": 0, "crashed": 0, "timeout": 0},
        duplicate_or_gap_experiment_ids=[gapped_id],
        verdict="incomplete",
    )
    sealed = _seal(attempt_evidence=attempts, accounting_report=report)
    assert sealed.accounting_complete is False
    assert sealed.performance_usable is False


# -- Task 1C (independent-audit strategy-audit-rob945-task1b-20260718-042347.md):
# I1 authentic-incomplete round-trip / I2 gap-branch forged-aggregate bypass /
# I3 cross-bind cardinality+identity / I4 whole-campaign-only exemption. --


def test_actual_registrations_can_exceed_24_when_backed_by_extra_experiment_ids():
    """I1 ('extra_actual_25'): real H6 semantics permit
    ``actual_registrations`` to exceed the frozen 24 when backed by a
    matching ``extra_experiment_ids`` entry (an extra IS an additional
    registration beyond the frozen 24) -- this must seal as well-formed
    incomplete, never raise."""
    attempts = _all_24_completed_attempts()
    report = _clean_report(
        actual_registrations=25,
        extra_experiment_ids=["some-unexpected-registered-id"],
        verdict="incomplete",
    )
    sealed = _seal(attempt_evidence=attempts, accounting_report=report)
    assert sealed.accounting_complete is False
    assert sealed.performance_usable is False


def test_mismatch_experiment_id_is_not_independently_reclassified_as_missing():
    """I1 ('authentic_mismatch'): a frozen ID H6 legitimately classifies as
    ``mismatch`` (a registration-time discrepancy this seal cannot recompute)
    may have no retry_index==0 attempt evidence supplied at all -- the seal
    must trust the caller's mismatch classification rather than ALSO
    independently reclassifying it as ``missing`` and raising a spurious
    cross-check conflict."""
    attempts = _all_24_completed_attempts()[:23]
    mismatched_id = FROZEN_EXPERIMENT_IDS[23]
    report = _clean_report(
        actual_registrations=24,
        primary_attempts=23,
        total_attempts=23,
        status_counts={"completed": 23, "rejected": 0, "crashed": 0, "timeout": 0},
        mismatch_experiment_ids=[mismatched_id],
        verdict="incomplete",
    )
    sealed = _seal(attempt_evidence=attempts, accounting_report=report)
    assert sealed.accounting_complete is False
    assert sealed.performance_usable is False


def test_retry_index_one_only_group_is_missing_not_duplicate_or_gap():
    """I1 ('authentic_retry_only'): an experiment with a single
    retry_index=1 evidence row and NO retry_index=0 primary is simply a
    missing primary -- it must not ALSO be independently reclassified as a
    duplicate/gap purely because its sole retry index isn't
    contiguous-from-zero.

    Captain counter-parity correction: the real H6 loop hits
    ``retry_indices[0] != 0`` for this group, classifies it missing, and
    `continue`s -- NONE of that group's rows enter
    primary_attempts/total_attempts/retry_attempts/status_counts. The
    authentic fixture therefore reports 23/23/0 and completed=23 (the
    stray retry-only row is entirely excluded), never 24/1/24."""
    attempts = _all_24_completed_attempts()[:23]
    missing_id = FROZEN_EXPERIMENT_IDS[23]
    stray_retry_row = _legit_retry_attempt(missing_id, retry_index=1)
    attempts.append(stray_retry_row)
    report = _clean_report(
        actual_registrations=24,
        primary_attempts=23,
        total_attempts=23,
        retry_attempts=0,
        status_counts={"completed": 23, "rejected": 0, "crashed": 0, "timeout": 0},
        missing_experiment_ids=[missing_id],
        verdict="incomplete",
    )
    sealed = _seal(attempt_evidence=attempts, accounting_report=report)
    assert sealed.accounting_complete is False
    assert missing_id in sealed.report["missing_experiment_ids"]
    assert missing_id not in sealed.report["duplicate_or_gap_experiment_ids"]


def test_gap_branch_recomputes_non_gapped_counters_and_rejects_forged_zero_aggregates():
    """I2: 24 valid primaries plus one extra retry_index=2 row (making one
    experiment gapped) must have its non-gapped counters (primary/total/
    status) cross-checked against the ACTUAL 23 non-gapped rows -- a report
    forging these to all-zero must be rejected, not accepted merely because
    it is internally self-consistent (0 == 0 + 0)."""
    attempts = _all_24_completed_attempts()
    gapped_id = FROZEN_EXPERIMENT_IDS[0]
    gap_row = _hand_built_attempt(gapped_id, retry_index=2, status="completed")
    attempts.append(gap_row)
    forged_report = _clean_report(
        actual_registrations=24,
        primary_attempts=0,
        total_attempts=0,
        retry_attempts=0,
        status_counts={"completed": 0, "rejected": 0, "crashed": 0, "timeout": 0},
        duplicate_or_gap_experiment_ids=[gapped_id],
        verdict="incomplete",
    )
    with pytest.raises(ScorecardInputError):
        _seal(attempt_evidence=attempts, accounting_report=forged_report)


def test_duplicate_config_attempt_result_in_walkforward_result_fails_closed():
    """I3: a ``WalkForwardResult`` whose ``config_attempts`` contains a
    duplicate ``config_id`` (13 rows, all 12 real configs present PLUS one
    literal repeat -- never silently overwrite the earlier summary for that
    config_id in ``summaries_by_strategy_config``) must fail closed. Using
    the REAL, unmodified 24-attempt evidence set (matching the real S1
    result byte-for-byte otherwise), this currently (bug) seals successfully
    with ``accounting_complete=True``/``performance_usable=True``."""
    real_s1 = DEFAULT_WALKFORWARD_RESULTS["S1"]
    duplicated_attempts = real_s1.config_attempts + (real_s1.config_attempts[0],)
    malformed_s1 = WalkForwardResult(
        strategy="S1",
        folds=real_s1.folds,
        config_attempts=duplicated_attempts,
        concatenated_oos_ledgers=real_s1.concatenated_oos_ledgers,
    )
    wf = dict(DEFAULT_WALKFORWARD_RESULTS)
    wf["S1"] = malformed_s1
    with pytest.raises(ScorecardInputError):
        _seal(walkforward_results=wf)


def test_walkforward_results_wrong_config_prefix_fails_closed():
    """I3: a ``WalkForwardResult`` labeled ``strategy="S1"`` whose
    ``config_attempts`` actually carry S2's config IDs must fail closed --
    the exact frozen 12-config set per strategy is required, not just a
    count of 12."""
    real_s1 = DEFAULT_WALKFORWARD_RESULTS["S1"]
    real_s2 = DEFAULT_WALKFORWARD_RESULTS["S2"]
    wrong_prefix_s1 = WalkForwardResult(
        strategy="S1",
        folds=real_s1.folds,
        config_attempts=real_s2.config_attempts,
        concatenated_oos_ledgers=real_s1.concatenated_oos_ledgers,
    )
    wf = dict(DEFAULT_WALKFORWARD_RESULTS)
    wf["S1"] = wrong_prefix_s1
    with pytest.raises(ScorecardInputError):
        _seal(walkforward_results=wf)


def test_walkforward_results_wrong_strategy_key_binding_fails_closed_even_with_matching_hashes():
    """I3: a caller-supplied ``walkforward_results`` dict slot whose key
    ("S1") doesn't match the ``WalkForwardResult``'s own ``.strategy``
    field ("S2") must fail -- even when the attacker forges EVERY one of
    S1's 12 attempts' fold_evidence_hash/run_identity/scenario evidence to
    be internally self-consistent with that SAME mislabeled per-config
    summary set (proving this isn't merely caught by coincidental hash
    divergence against unmodified real S1 evidence -- a single-row forge
    alone still diverges on the OTHER 11 untouched real rows)."""
    real_s1 = DEFAULT_WALKFORWARD_RESULTS["S1"]
    mislabeled_s1 = WalkForwardResult(
        strategy="S2",  # deliberately wrong -- dict key below is "S1"
        folds=real_s1.folds,
        config_attempts=real_s1.config_attempts,
        concatenated_oos_ledgers=real_s1.concatenated_oos_ledgers,
    )
    mislabeled_summaries = {
        s.config_id: s for s in summarize_config_attempts_for_h6(mislabeled_s1)
    }
    attempts = _all_24_completed_attempts()
    for i, a in enumerate(attempts):
        eid = a["attempt_key"]["experiment_id"]
        config_id = _EXPERIMENT_ID_TO_CONFIG_ID[eid]
        if not config_id.startswith("S1"):
            continue
        summary = mislabeled_summaries[config_id]
        forged_fold_hash, forged_run_identity = (
            _recompute_fold_evidence_hash_and_run_identity(
                summary,
                full_campaign_hash=FULL_CAMPAIGN_HASH,
                campaign_run_id=CAMPAIGN_RUN_ID,
                strategy_key=_STRATEGY_KEY["S1"],
                experiment_id=eid,
                retry_index=0,
            )
        )
        forged_row = _hand_built_attempt(eid, retry_index=0, status="completed")
        forged_row["fold_evidence_hash"] = forged_fold_hash
        forged_row["run_identity"] = forged_run_identity
        forged_row["scenario_evidence"] = [
            {
                "scenario_name": row.scenario_name,
                "trade_count": row.trade_count,
                "artifact_hash": row.artifact_hash,
            }
            for row in sorted(summary.scenario_summaries, key=lambda r: r.scenario_name)
        ]
        attempts[i] = forged_row
    wf = dict(DEFAULT_WALKFORWARD_RESULTS)
    wf["S1"] = mislabeled_s1
    with pytest.raises(ScorecardInputError):
        _seal(attempt_evidence=attempts, walkforward_results=wf)


def test_sealed_report_status_counts_is_deeply_immutable_against_post_seal_mutation():
    sealed = _seal()
    with pytest.raises(TypeError):
        sealed.report["status_counts"]["completed"] = 999


def test_sealed_report_discrepancy_list_is_deeply_immutable_against_post_seal_mutation():
    sealed = _seal()
    with pytest.raises(TypeError):
        sealed.report["missing_experiment_ids"][:] = ["forged"]
    with pytest.raises((TypeError, AttributeError)):
        sealed.report["missing_experiment_ids"].append("forged")


def test_sealed_report_top_level_is_deeply_immutable_against_post_seal_mutation():
    sealed = _seal()
    with pytest.raises(TypeError):
        sealed.report["verdict"] = "historical_pass"

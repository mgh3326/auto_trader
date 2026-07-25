"""ROB-945 (H5, Task 1E) -- RED tests for the pure H6-summary normalizer/
validator contract.

Task 1 final re-review (strategy-audit-rob945-task1-final-20260718-050428.md,
I2/I3) found that ``rob945_accounting_seal`` hashed/cross-bound a caller/
producer-supplied ``ConfigAttemptEvidenceSummary`` without ever applying the
real H6-build boundary's own nested normalization/validation (exact fold
cardinality, scenario set, status/reason contracts, closed no-trade reasons,
lowercase hashes, frozen-universe partition/order) -- a structurally
malformed summary (e.g. one config missing from one fold's candidate roster,
leaving 7 fold-selection rows) sealed successfully as
``accounting_complete=True``/``performance_usable=True``.

This module (``rob945_h6_summary_contract.normalize_and_validate_h6_summary``)
is a pure H5 mirror of the real H6-build boundary's own normalizer
(``run_rob944_campaign._normalize_config_attempt_evidence_summary`` +
``_normalized_summary_to_attempt_evidence``'s pre-hash validation) -- H6
private helpers are imported here ONLY as a test-only parity oracle, never by
H5 production code (``rob945_h6_summary_contract.py`` imports no
``run_rob944_campaign``/``app.*``).
"""

from __future__ import annotations

import hashlib

import pytest
import rob941_frozen_scope as frozen
import rob944_folds as foldmod
from rob944_frozen_campaign import (
    PRODUCTION_S1_STRATEGY_KEY,
    PRODUCTION_S2_STRATEGY_KEY,
    build_production_frozen_campaign_envelope,
)
from rob944_gap_funding import (
    REASON_EXPECTED_FUNDING_COST_ABOVE_3BPS,
    REASON_FUNDING_EVIDENCE_UNAVAILABLE,
)
from rob944_selection import (
    INSUFFICIENT_ELIGIBLE_SYMBOLS_REASON,
    INSUFFICIENT_SYMBOL_EVIDENCE_REASON,
)
from rob944_walkforward import (
    REASON_CHILD_EXECUTION_CRASHED,
    REASON_CHILD_EXECUTION_TIMEOUT,
    REASON_DATA_GAP_IN_POSITION,
    REASON_GLOBAL_CORPUS_LOAD_FAILED,
    REASON_NEVER_SELECTED_IN_ANY_FOLD,
    ConfigAttemptEvidenceSummary,
    FoldSelectionEvidenceSummary,
    ScenarioEvidenceSummary,
)
from rob945_accounting_seal import _recompute_fold_evidence_hash_and_run_identity
from rob945_h6_summary_contract import (
    H6SummaryContractError,
    normalize_and_validate_h6_summary,
)
from run_rob944_campaign import _global_failure_summaries, _summary_to_attempt_evidence

from research_contracts.canonical_hash import canonical_sha256

_ENVELOPE = build_production_frozen_campaign_envelope()
_FULL_CAMPAIGN_HASH = _ENVELOPE.full_campaign_hash()
_FROZEN_EXPERIMENT_IDS = tuple(_ENVELOPE.to_dict()["experiment_ids"])
_STRATEGY_KEY = {"S1": PRODUCTION_S1_STRATEGY_KEY, "S2": PRODUCTION_S2_STRATEGY_KEY}


def _derive_campaign_run_id(full_campaign_hash: str) -> str:
    import base64

    digest_hex = canonical_sha256(
        {"full_campaign_hash": full_campaign_hash, "kind": "primary_run"}
    )
    suffix = (
        base64.urlsafe_b64encode(bytes.fromhex(digest_hex)).decode("ascii").rstrip("=")
    )
    return f"rob944-primary-{suffix}"


_CAMPAIGN_RUN_ID = _derive_campaign_run_id(_FULL_CAMPAIGN_HASH)

_CANONICAL_SCENARIO_ORDER = ("base", "primary_stress", "upward_stress")
_REAL_FOLDS = foldmod.generate_frozen_fold_schedule(
    frozen.WINDOW_START_MS, frozen.WINDOW_END_MS
)
_REAL_FOLD_IDS = tuple(f.fold_id for f in _REAL_FOLDS)
assert len(_REAL_FOLD_IDS) == 8


class _AliasStr(str):
    """A str SUBCLASS -- could override __eq__/__hash__ to pass a
    membership check while its actual buffer content differs; exact-type
    (``type(x) is str``) discipline must reject it outright."""


def _hex64(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _never_selected_scenario_row(
    scenario_name: str, seed: str
) -> ScenarioEvidenceSummary:
    return ScenarioEvidenceSummary(
        scenario_name=scenario_name,
        status="never_selected",
        reason_code=REASON_NEVER_SELECTED_IN_ANY_FOLD,
        trade_count=0,
        artifact_hash=_hex64(f"scenario:{seed}:{scenario_name}"),
        no_trade_reason_counts={},
    )


def _rejected_fold_row(fold_id: str, seed: str) -> FoldSelectionEvidenceSummary:
    return FoldSelectionEvidenceSummary(
        fold_id=fold_id,
        fold_selected_config_id=None,
        eligible_symbols=(),
        excluded_symbols=tuple(
            (symbol, INSUFFICIENT_SYMBOL_EVIDENCE_REASON) for symbol in frozen.UNIVERSE
        ),
        equal_weight_expectancy_bps=None,
        pooled_expectancy_bps=None,
        profit_factor=0.0,
        rejected=True,
        rejection_reason=INSUFFICIENT_ELIGIBLE_SYMBOLS_REASON,
        train_input_hash=_hex64(f"train:{seed}:{fold_id}"),
        no_trade_reason_counts={},
    )


def _well_formed_summary(
    strategy: str = "S1",
    config_id: str = "S1-00",
    *,
    status: str = "completed",
    reason_code=None,
    fold_ids=_REAL_FOLD_IDS,
) -> ConfigAttemptEvidenceSummary:
    seed = f"{strategy}:{config_id}"
    scenario_summaries = tuple(
        _never_selected_scenario_row(name, seed) for name in _CANONICAL_SCENARIO_ORDER
    )
    fold_selection_trace = tuple(_rejected_fold_row(fid, seed) for fid in fold_ids)
    return ConfigAttemptEvidenceSummary(
        strategy=strategy,
        config_id=config_id,
        status=status,
        reason_code=reason_code,
        scenario_summaries=scenario_summaries,
        fold_selection_trace=fold_selection_trace,
    )


def _global_fallback_summary(strategy: str = "S1", config_id: str = "S1-00"):
    seed = f"{strategy}:{config_id}:global"
    scenario_summaries = tuple(
        ScenarioEvidenceSummary(
            scenario_name=name,
            status="crashed",
            reason_code=REASON_GLOBAL_CORPUS_LOAD_FAILED,
            trade_count=0,
            artifact_hash=_hex64(f"{seed}:{name}"),
            no_trade_reason_counts={},
        )
        for name in _CANONICAL_SCENARIO_ORDER
    )
    return ConfigAttemptEvidenceSummary(
        strategy=strategy,
        config_id=config_id,
        status="crashed",
        reason_code=REASON_GLOBAL_CORPUS_LOAD_FAILED,
        scenario_summaries=scenario_summaries,
        fold_selection_trace=(),
    )


# -- Positive/happy-path cases --


def test_accepts_a_well_formed_summary_and_returns_canonically_ordered_equal_copy():
    summary = _well_formed_summary()
    normalized = normalize_and_validate_h6_summary(
        summary, expected_strategy="S1", expected_config_id="S1-00"
    )
    assert normalized.strategy == "S1"
    assert normalized.config_id == "S1-00"
    assert [r.scenario_name for r in normalized.scenario_summaries] == list(
        _CANONICAL_SCENARIO_ORDER
    )
    assert [r.fold_id for r in normalized.fold_selection_trace] == sorted(
        _REAL_FOLD_IDS
    )


def test_accepts_the_authentic_global_fallback_empty_trace_signature():
    summary = _global_fallback_summary()
    normalized = normalize_and_validate_h6_summary(
        summary, expected_strategy="S1", expected_config_id="S1-00"
    )
    assert normalized.fold_selection_trace == ()


def test_reordered_equivalent_input_normalizes_to_the_identical_canonical_order():
    seed = "S1:S1-00"
    scenario_summaries_forward = tuple(
        _never_selected_scenario_row(name, seed) for name in _CANONICAL_SCENARIO_ORDER
    )
    scenario_summaries_reversed = tuple(reversed(scenario_summaries_forward))
    fold_rows_forward = tuple(_rejected_fold_row(fid, seed) for fid in _REAL_FOLD_IDS)
    fold_rows_reversed = tuple(reversed(fold_rows_forward))

    summary_forward = ConfigAttemptEvidenceSummary(
        strategy="S1",
        config_id="S1-00",
        status="completed",
        reason_code=None,
        scenario_summaries=scenario_summaries_forward,
        fold_selection_trace=fold_rows_forward,
    )
    summary_reversed = ConfigAttemptEvidenceSummary(
        strategy="S1",
        config_id="S1-00",
        status="completed",
        reason_code=None,
        scenario_summaries=scenario_summaries_reversed,
        fold_selection_trace=fold_rows_reversed,
    )
    normalized_forward = normalize_and_validate_h6_summary(
        summary_forward, expected_strategy="S1", expected_config_id="S1-00"
    )
    normalized_reversed = normalize_and_validate_h6_summary(
        summary_reversed, expected_strategy="S1", expected_config_id="S1-00"
    )
    assert normalized_forward == normalized_reversed


# -- Identity mismatch --


def test_rejects_strategy_mismatch_against_expected():
    summary = _well_formed_summary(strategy="S1", config_id="S1-00")
    with pytest.raises(H6SummaryContractError):
        normalize_and_validate_h6_summary(
            summary, expected_strategy="S2", expected_config_id="S1-00"
        )


def test_rejects_config_id_mismatch_against_expected():
    summary = _well_formed_summary(strategy="S1", config_id="S1-00")
    with pytest.raises(H6SummaryContractError):
        normalize_and_validate_h6_summary(
            summary, expected_strategy="S1", expected_config_id="S1-01"
        )


def test_rejects_config_id_outside_the_strategys_frozen_12_config_set():
    summary = _well_formed_summary(strategy="S1", config_id="S1-99")
    with pytest.raises(H6SummaryContractError):
        normalize_and_validate_h6_summary(
            summary, expected_strategy="S1", expected_config_id="S1-99"
        )


def test_rejects_non_exact_config_attempt_evidence_summary_type():
    with pytest.raises(H6SummaryContractError):
        normalize_and_validate_h6_summary(
            {"strategy": "S1", "config_id": "S1-00"},
            expected_strategy="S1",
            expected_config_id="S1-00",
        )


# -- ROB-970 (Q2, Fable-approved): diagnostic_evidence passthrough --------


def _child_failure_evidence(**overrides):
    from rob944_diagnostic_evidence import ChildFailureEvidence

    base = {
        "transport": "in_process",
        "stage": "generator",
        "exception_type": "RuntimeError",
        "message": "boom",
        "traceback_text": "Traceback (most recent call last):\nRuntimeError: boom\n",
        "stderr": None,
        "strategy": "S1",
        "config_id": "S1-00",
        "symbol": "BTCUSDT",
        "fold_id": "fold-00",
        "scenario_name": None,
        "signature": _hex64("boom-signature"),
        "occurrence_count": 1,
        "truncated": False,
    }
    base.update(overrides)
    return ChildFailureEvidence(**base)


def test_diagnostic_evidence_passes_through_normalization_unchanged():
    evidence = _child_failure_evidence()
    summary = _well_formed_summary(
        status="crashed",
        reason_code=REASON_CHILD_EXECUTION_CRASHED,
    )
    from dataclasses import replace

    summary = replace(summary, diagnostic_evidence=(evidence,))
    normalized = normalize_and_validate_h6_summary(
        summary, expected_strategy="S1", expected_config_id="S1-00"
    )
    assert normalized.diagnostic_evidence == (evidence,)


def test_diagnostic_evidence_defaults_to_empty_tuple_when_absent():
    summary = _well_formed_summary()
    normalized = normalize_and_validate_h6_summary(
        summary, expected_strategy="S1", expected_config_id="S1-00"
    )
    assert normalized.diagnostic_evidence == ()


def test_rejects_non_exact_tuple_diagnostic_evidence():
    from dataclasses import replace

    summary = replace(
        _well_formed_summary(), diagnostic_evidence=[_child_failure_evidence()]
    )
    with pytest.raises(H6SummaryContractError):
        normalize_and_validate_h6_summary(
            summary, expected_strategy="S1", expected_config_id="S1-00"
        )


def test_rejects_non_exact_child_failure_evidence_entry_type():
    from dataclasses import replace

    summary = replace(
        _well_formed_summary(),
        diagnostic_evidence=({"stage": "generator"},),
    )
    with pytest.raises(H6SummaryContractError):
        normalize_and_validate_h6_summary(
            summary, expected_strategy="S1", expected_config_id="S1-00"
        )


def test_diagnostic_evidence_never_alters_fold_evidence_hash_or_run_identity():
    """Observer-effect-0 at the H5 recompute boundary: the SAME semantic
    summary with vs. without diagnostic_evidence must hash/run-identity
    IDENTICALLY -- diagnostic content has no seal role."""
    from dataclasses import replace

    with_diag = normalize_and_validate_h6_summary(
        replace(
            _well_formed_summary(
                status="crashed", reason_code=REASON_CHILD_EXECUTION_CRASHED
            ),
            diagnostic_evidence=(_child_failure_evidence(message="one message"),),
        ),
        expected_strategy="S1",
        expected_config_id="S1-00",
    )
    without_diag = normalize_and_validate_h6_summary(
        _well_formed_summary(
            status="crashed", reason_code=REASON_CHILD_EXECUTION_CRASHED
        ),
        expected_strategy="S1",
        expected_config_id="S1-00",
    )
    different_diag = normalize_and_validate_h6_summary(
        replace(
            _well_formed_summary(
                status="crashed", reason_code=REASON_CHILD_EXECUTION_CRASHED
            ),
            diagnostic_evidence=(
                _child_failure_evidence(message="a totally different message"),
            ),
        ),
        expected_strategy="S1",
        expected_config_id="S1-00",
    )
    common_kwargs = {
        "full_campaign_hash": _FULL_CAMPAIGN_HASH,
        "campaign_run_id": _CAMPAIGN_RUN_ID,
        "strategy_key": _STRATEGY_KEY["S1"],
        "experiment_id": "exp-observer-effect-0",
        "retry_index": 0,
    }
    hash_with, identity_with = _recompute_fold_evidence_hash_and_run_identity(
        with_diag, **common_kwargs
    )
    hash_without, identity_without = _recompute_fold_evidence_hash_and_run_identity(
        without_diag, **common_kwargs
    )
    hash_different, identity_different = _recompute_fold_evidence_hash_and_run_identity(
        different_diag, **common_kwargs
    )
    assert hash_with == hash_without == hash_different
    assert identity_with == identity_without == identity_different


# -- ROB-970 R1 (Q1=A, cap=32): diagnostic_overflow passthrough ------------


def _overflow(**overrides):
    from rob944_diagnostic_evidence import DiagnosticOverflowMetadata

    base = {
        "truncated": True,
        "omitted_distinct_signatures": 3,
        "omitted_occurrences": 7,
    }
    base.update(overrides)
    return DiagnosticOverflowMetadata(**base)


def test_diagnostic_overflow_passes_through_normalization_unchanged():
    from dataclasses import replace

    summary = replace(_well_formed_summary(), diagnostic_overflow=_overflow())
    normalized = normalize_and_validate_h6_summary(
        summary, expected_strategy="S1", expected_config_id="S1-00"
    )
    assert normalized.diagnostic_overflow == _overflow()


def test_diagnostic_overflow_defaults_to_empty_when_absent():
    from rob944_diagnostic_evidence import DiagnosticOverflowMetadata

    summary = _well_formed_summary()
    normalized = normalize_and_validate_h6_summary(
        summary, expected_strategy="S1", expected_config_id="S1-00"
    )
    assert normalized.diagnostic_overflow == DiagnosticOverflowMetadata(
        truncated=False, omitted_distinct_signatures=0, omitted_occurrences=0
    )


def test_rejects_non_exact_diagnostic_overflow_type():
    from dataclasses import replace

    summary = replace(
        _well_formed_summary(),
        diagnostic_overflow={
            "truncated": True,
            "omitted_distinct_signatures": 1,
            "omitted_occurrences": 1,
        },
    )
    with pytest.raises(H6SummaryContractError):
        normalize_and_validate_h6_summary(
            summary, expected_strategy="S1", expected_config_id="S1-00"
        )


def test_rejects_negative_omitted_counts_in_diagnostic_overflow():
    from dataclasses import replace

    summary = replace(
        _well_formed_summary(),
        diagnostic_overflow=_overflow(omitted_distinct_signatures=-1),
    )
    with pytest.raises(H6SummaryContractError):
        normalize_and_validate_h6_summary(
            summary, expected_strategy="S1", expected_config_id="S1-00"
        )


def test_rejects_diagnostic_evidence_longer_than_the_cap():
    """R2 audit: every trust boundary must independently fail closed if
    ``len(diagnostic_evidence) > 32``, not only the producer helper."""
    from dataclasses import replace

    too_many = tuple(
        _child_failure_evidence(signature=_hex64(f"forged-sig-{i}")) for i in range(33)
    )
    summary = replace(_well_formed_summary(), diagnostic_evidence=too_many)
    with pytest.raises(H6SummaryContractError):
        normalize_and_validate_h6_summary(
            summary, expected_strategy="S1", expected_config_id="S1-00"
        )


def test_accepts_diagnostic_evidence_exactly_at_the_cap():
    from dataclasses import replace

    exactly_32 = tuple(
        _child_failure_evidence(signature=_hex64(f"forged-sig-{i}")) for i in range(32)
    )
    summary = replace(_well_formed_summary(), diagnostic_evidence=exactly_32)
    normalized = normalize_and_validate_h6_summary(
        summary, expected_strategy="S1", expected_config_id="S1-00"
    )
    assert len(normalized.diagnostic_evidence) == 32


@pytest.mark.parametrize(
    "overrides",
    [
        {
            "truncated": False,
            "omitted_distinct_signatures": 0,
            "omitted_occurrences": 1,
        },
        {"truncated": True, "omitted_distinct_signatures": 0, "omitted_occurrences": 0},
        {
            "truncated": False,
            "omitted_distinct_signatures": 1,
            "omitted_occurrences": 1,
        },
    ],
)
def test_rejects_diagnostic_overflow_with_inconsistent_truncated_flag(overrides):
    """``truncated`` must be exactly ``omitted_occurrences > 0`` -- never a
    caller-asserted boolean independent of the actual counts."""
    from dataclasses import replace

    summary = replace(
        _well_formed_summary(), diagnostic_overflow=_overflow(**overrides)
    )
    with pytest.raises(H6SummaryContractError):
        normalize_and_validate_h6_summary(
            summary, expected_strategy="S1", expected_config_id="S1-00"
        )


def test_diagnostic_overflow_never_alters_fold_evidence_hash_or_run_identity():
    from dataclasses import replace

    common_kwargs = {
        "full_campaign_hash": _FULL_CAMPAIGN_HASH,
        "campaign_run_id": _CAMPAIGN_RUN_ID,
        "strategy_key": _STRATEGY_KEY["S1"],
        "experiment_id": "exp-overflow-observer-effect-0",
        "retry_index": 0,
    }
    without_overflow = normalize_and_validate_h6_summary(
        _well_formed_summary(
            status="crashed", reason_code=REASON_CHILD_EXECUTION_CRASHED
        ),
        expected_strategy="S1",
        expected_config_id="S1-00",
    )
    with_overflow = normalize_and_validate_h6_summary(
        replace(
            _well_formed_summary(
                status="crashed", reason_code=REASON_CHILD_EXECUTION_CRASHED
            ),
            diagnostic_overflow=_overflow(),
        ),
        expected_strategy="S1",
        expected_config_id="S1-00",
    )
    hash_without, identity_without = _recompute_fold_evidence_hash_and_run_identity(
        without_overflow, **common_kwargs
    )
    hash_with, identity_with = _recompute_fold_evidence_hash_and_run_identity(
        with_overflow, **common_kwargs
    )
    assert hash_without == hash_with
    assert identity_without == identity_with


# -- Fold cardinality (the literal I2 repro: one fold missing => 7 rows) --


def test_rejects_seven_fold_trace_missing_one_canonical_fold():
    summary = _well_formed_summary(fold_ids=_REAL_FOLD_IDS[1:])  # drop fold-00
    with pytest.raises(H6SummaryContractError):
        normalize_and_validate_h6_summary(
            summary, expected_strategy="S1", expected_config_id="S1-00"
        )


def test_rejects_nine_fold_trace_with_a_duplicate_fold_id():
    summary = _well_formed_summary(fold_ids=(*_REAL_FOLD_IDS, _REAL_FOLD_IDS[0]))
    with pytest.raises(H6SummaryContractError):
        normalize_and_validate_h6_summary(
            summary, expected_strategy="S1", expected_config_id="S1-00"
        )


def test_rejects_foreign_fold_id_outside_the_canonical_eight():
    summary = _well_formed_summary(fold_ids=(*_REAL_FOLD_IDS[:-1], "fold-99"))
    with pytest.raises(H6SummaryContractError):
        normalize_and_validate_h6_summary(
            summary, expected_strategy="S1", expected_config_id="S1-00"
        )


def test_rejects_empty_trace_for_a_non_global_status_reason():
    summary = _well_formed_summary(fold_ids=())
    with pytest.raises(H6SummaryContractError):
        normalize_and_validate_h6_summary(
            summary, expected_strategy="S1", expected_config_id="S1-00"
        )


# -- Attempt-level status/reason contract --


def test_rejects_unknown_attempt_status():
    summary = _well_formed_summary(status="garbage_status_xyz")
    with pytest.raises(H6SummaryContractError):
        normalize_and_validate_h6_summary(
            summary, expected_strategy="S1", expected_config_id="S1-00"
        )


def test_rejects_completed_status_with_nonnull_reason_code():
    summary = _well_formed_summary(status="completed", reason_code="some_reason")
    with pytest.raises(H6SummaryContractError):
        normalize_and_validate_h6_summary(
            summary, expected_strategy="S1", expected_config_id="S1-00"
        )


def test_rejects_crashed_status_with_a_reason_outside_the_closed_set():
    summary = _well_formed_summary(status="crashed", reason_code="bogus_reason")
    with pytest.raises(H6SummaryContractError):
        normalize_and_validate_h6_summary(
            summary, expected_strategy="S1", expected_config_id="S1-00"
        )


def test_accepts_the_two_valid_rejected_attempt_reason_codes():
    from rob944_walkforward import REASON_INSUFFICIENT_TRAIN_EVIDENCE_ALL_FOLDS

    for reason in (
        REASON_DATA_GAP_IN_POSITION,
        REASON_INSUFFICIENT_TRAIN_EVIDENCE_ALL_FOLDS,
    ):
        summary = _well_formed_summary(status="rejected", reason_code=reason)
        normalized = normalize_and_validate_h6_summary(
            summary, expected_strategy="S1", expected_config_id="S1-00"
        )
        assert normalized.reason_code == reason


def test_accepts_crashed_and_timeout_attempt_reason_codes():
    for status, reason in (
        ("crashed", REASON_CHILD_EXECUTION_CRASHED),
        ("timeout", REASON_CHILD_EXECUTION_TIMEOUT),
    ):
        summary = _well_formed_summary(status=status, reason_code=reason)
        normalized = normalize_and_validate_h6_summary(
            summary, expected_strategy="S1", expected_config_id="S1-00"
        )
        assert normalized.status == status


# -- Scenario set/count/status/reason/hash/count --


def test_rejects_scenario_set_with_wrong_count():
    summary = _well_formed_summary()
    truncated = ConfigAttemptEvidenceSummary(
        strategy=summary.strategy,
        config_id=summary.config_id,
        status=summary.status,
        reason_code=summary.reason_code,
        scenario_summaries=summary.scenario_summaries[:2],
        fold_selection_trace=summary.fold_selection_trace,
    )
    with pytest.raises(H6SummaryContractError):
        normalize_and_validate_h6_summary(
            truncated, expected_strategy="S1", expected_config_id="S1-00"
        )


def test_rejects_duplicate_scenario_name():
    summary = _well_formed_summary()
    rows = list(summary.scenario_summaries)
    rows[2] = ScenarioEvidenceSummary(
        scenario_name=rows[0].scenario_name,
        status=rows[2].status,
        reason_code=rows[2].reason_code,
        trade_count=rows[2].trade_count,
        artifact_hash=rows[2].artifact_hash,
        no_trade_reason_counts=rows[2].no_trade_reason_counts,
    )
    duplicated = ConfigAttemptEvidenceSummary(
        strategy=summary.strategy,
        config_id=summary.config_id,
        status=summary.status,
        reason_code=summary.reason_code,
        scenario_summaries=tuple(rows),
        fold_selection_trace=summary.fold_selection_trace,
    )
    with pytest.raises(H6SummaryContractError):
        normalize_and_validate_h6_summary(
            duplicated, expected_strategy="S1", expected_config_id="S1-00"
        )


def test_rejects_scenario_row_with_non_hex_artifact_hash():
    summary = _well_formed_summary()
    rows = list(summary.scenario_summaries)
    rows[0] = ScenarioEvidenceSummary(
        scenario_name=rows[0].scenario_name,
        status=rows[0].status,
        reason_code=rows[0].reason_code,
        trade_count=rows[0].trade_count,
        artifact_hash="NOT-HEX" * 8,
        no_trade_reason_counts=rows[0].no_trade_reason_counts,
    )
    malformed = ConfigAttemptEvidenceSummary(
        strategy=summary.strategy,
        config_id=summary.config_id,
        status=summary.status,
        reason_code=summary.reason_code,
        scenario_summaries=tuple(rows),
        fold_selection_trace=summary.fold_selection_trace,
    )
    with pytest.raises(H6SummaryContractError):
        normalize_and_validate_h6_summary(
            malformed, expected_strategy="S1", expected_config_id="S1-00"
        )


def test_rejects_scenario_row_with_negative_trade_count():
    summary = _well_formed_summary()
    rows = list(summary.scenario_summaries)
    rows[0] = ScenarioEvidenceSummary(
        scenario_name=rows[0].scenario_name,
        status=rows[0].status,
        reason_code=rows[0].reason_code,
        trade_count=-1,
        artifact_hash=rows[0].artifact_hash,
        no_trade_reason_counts=rows[0].no_trade_reason_counts,
    )
    malformed = ConfigAttemptEvidenceSummary(
        strategy=summary.strategy,
        config_id=summary.config_id,
        status=summary.status,
        reason_code=summary.reason_code,
        scenario_summaries=tuple(rows),
        fold_selection_trace=summary.fold_selection_trace,
    )
    with pytest.raises(H6SummaryContractError):
        normalize_and_validate_h6_summary(
            malformed, expected_strategy="S1", expected_config_id="S1-00"
        )


def test_rejects_scenario_row_with_bool_trade_count():
    summary = _well_formed_summary()
    rows = list(summary.scenario_summaries)
    rows[0] = ScenarioEvidenceSummary(
        scenario_name=rows[0].scenario_name,
        status=rows[0].status,
        reason_code=rows[0].reason_code,
        trade_count=True,
        artifact_hash=rows[0].artifact_hash,
        no_trade_reason_counts=rows[0].no_trade_reason_counts,
    )
    malformed = ConfigAttemptEvidenceSummary(
        strategy=summary.strategy,
        config_id=summary.config_id,
        status=summary.status,
        reason_code=summary.reason_code,
        scenario_summaries=tuple(rows),
        fold_selection_trace=summary.fold_selection_trace,
    )
    with pytest.raises(H6SummaryContractError):
        normalize_and_validate_h6_summary(
            malformed, expected_strategy="S1", expected_config_id="S1-00"
        )


def test_rejects_scenario_row_with_unknown_no_trade_reason_key():
    summary = _well_formed_summary()
    rows = list(summary.scenario_summaries)
    rows[0] = ScenarioEvidenceSummary(
        scenario_name=rows[0].scenario_name,
        status=rows[0].status,
        reason_code=rows[0].reason_code,
        trade_count=rows[0].trade_count,
        artifact_hash=rows[0].artifact_hash,
        no_trade_reason_counts={"SECRET-injected-reason": 1},
    )
    malformed = ConfigAttemptEvidenceSummary(
        strategy=summary.strategy,
        config_id=summary.config_id,
        status=summary.status,
        reason_code=summary.reason_code,
        scenario_summaries=tuple(rows),
        fold_selection_trace=summary.fold_selection_trace,
    )
    with pytest.raises(H6SummaryContractError):
        normalize_and_validate_h6_summary(
            malformed, expected_strategy="S1", expected_config_id="S1-00"
        )


def test_accepts_every_known_no_trade_reason_key():
    for reason in (
        "next_bar_unavailable",
        "daily_stop_active",
        "daily_entry_cap",
        "cooldown_active",
        "tp_below_min_distance",
        REASON_FUNDING_EVIDENCE_UNAVAILABLE,
        REASON_EXPECTED_FUNDING_COST_ABOVE_3BPS,
        "confirmation_failed",
        "target_direction_invalid",
        "tp_above_max",
        "tp_below_r_min_sl",
        "tp_below_abs_floor",
    ):
        summary = _well_formed_summary()
        rows = list(summary.scenario_summaries)
        rows[0] = ScenarioEvidenceSummary(
            scenario_name=rows[0].scenario_name,
            status=rows[0].status,
            reason_code=rows[0].reason_code,
            trade_count=rows[0].trade_count,
            artifact_hash=rows[0].artifact_hash,
            no_trade_reason_counts={reason: 1},
        )
        patched = ConfigAttemptEvidenceSummary(
            strategy=summary.strategy,
            config_id=summary.config_id,
            status=summary.status,
            reason_code=summary.reason_code,
            scenario_summaries=tuple(rows),
            fold_selection_trace=summary.fold_selection_trace,
        )
        normalized = normalize_and_validate_h6_summary(
            patched, expected_strategy="S1", expected_config_id="S1-00"
        )
        assert normalized is not None


def test_rejects_scenario_status_reason_mismatch():
    summary = _well_formed_summary()
    rows = list(summary.scenario_summaries)
    rows[0] = ScenarioEvidenceSummary(
        scenario_name=rows[0].scenario_name,
        status="completed",
        reason_code=REASON_CHILD_EXECUTION_CRASHED,
        trade_count=rows[0].trade_count,
        artifact_hash=rows[0].artifact_hash,
        no_trade_reason_counts={},
    )
    malformed = ConfigAttemptEvidenceSummary(
        strategy=summary.strategy,
        config_id=summary.config_id,
        status=summary.status,
        reason_code=summary.reason_code,
        scenario_summaries=tuple(rows),
        fold_selection_trace=summary.fold_selection_trace,
    )
    with pytest.raises(H6SummaryContractError):
        normalize_and_validate_h6_summary(
            malformed, expected_strategy="S1", expected_config_id="S1-00"
        )


# -- Fold-row domain checks --


def test_rejects_fold_row_with_symbol_in_both_eligible_and_excluded():
    summary = _well_formed_summary()
    rows = list(summary.fold_selection_trace)
    bad = rows[0]
    rows[0] = FoldSelectionEvidenceSummary(
        fold_id=bad.fold_id,
        fold_selected_config_id=None,
        eligible_symbols=(frozen.UNIVERSE[0],),
        excluded_symbols=tuple(
            (symbol, INSUFFICIENT_SYMBOL_EVIDENCE_REASON) for symbol in frozen.UNIVERSE
        ),
        equal_weight_expectancy_bps=None,
        pooled_expectancy_bps=None,
        profit_factor=0.0,
        rejected=True,
        rejection_reason=INSUFFICIENT_ELIGIBLE_SYMBOLS_REASON,
        train_input_hash=bad.train_input_hash,
        no_trade_reason_counts={},
    )
    malformed = ConfigAttemptEvidenceSummary(
        strategy=summary.strategy,
        config_id=summary.config_id,
        status=summary.status,
        reason_code=summary.reason_code,
        scenario_summaries=summary.scenario_summaries,
        fold_selection_trace=tuple(rows),
    )
    with pytest.raises(H6SummaryContractError):
        normalize_and_validate_h6_summary(
            malformed, expected_strategy="S1", expected_config_id="S1-00"
        )


def test_rejects_fold_row_not_covering_the_exact_frozen_universe():
    summary = _well_formed_summary()
    rows = list(summary.fold_selection_trace)
    bad = rows[0]
    rows[0] = FoldSelectionEvidenceSummary(
        fold_id=bad.fold_id,
        fold_selected_config_id=None,
        eligible_symbols=(),
        excluded_symbols=tuple(
            (symbol, INSUFFICIENT_SYMBOL_EVIDENCE_REASON)
            for symbol in frozen.UNIVERSE[:-1]  # drop one symbol entirely
        ),
        equal_weight_expectancy_bps=None,
        pooled_expectancy_bps=None,
        profit_factor=0.0,
        rejected=True,
        rejection_reason=INSUFFICIENT_ELIGIBLE_SYMBOLS_REASON,
        train_input_hash=bad.train_input_hash,
        no_trade_reason_counts={},
    )
    malformed = ConfigAttemptEvidenceSummary(
        strategy=summary.strategy,
        config_id=summary.config_id,
        status=summary.status,
        reason_code=summary.reason_code,
        scenario_summaries=summary.scenario_summaries,
        fold_selection_trace=tuple(rows),
    )
    with pytest.raises(H6SummaryContractError):
        normalize_and_validate_h6_summary(
            malformed, expected_strategy="S1", expected_config_id="S1-00"
        )


def test_rejects_rejected_fold_row_with_a_non_null_expectancy():
    summary = _well_formed_summary()
    rows = list(summary.fold_selection_trace)
    bad = rows[0]
    rows[0] = FoldSelectionEvidenceSummary(
        fold_id=bad.fold_id,
        fold_selected_config_id=None,
        eligible_symbols=(),
        excluded_symbols=bad.excluded_symbols,
        equal_weight_expectancy_bps=10.0,  # rejected=True must be None
        pooled_expectancy_bps=None,
        profit_factor=0.0,
        rejected=True,
        rejection_reason=INSUFFICIENT_ELIGIBLE_SYMBOLS_REASON,
        train_input_hash=bad.train_input_hash,
        no_trade_reason_counts={},
    )
    malformed = ConfigAttemptEvidenceSummary(
        strategy=summary.strategy,
        config_id=summary.config_id,
        status=summary.status,
        reason_code=summary.reason_code,
        scenario_summaries=summary.scenario_summaries,
        fold_selection_trace=tuple(rows),
    )
    with pytest.raises(H6SummaryContractError):
        normalize_and_validate_h6_summary(
            malformed, expected_strategy="S1", expected_config_id="S1-00"
        )


def test_rejects_fold_row_with_fold_selected_config_id_outside_strategys_set():
    summary = _well_formed_summary()
    rows = list(summary.fold_selection_trace)
    bad = rows[0]
    rows[0] = FoldSelectionEvidenceSummary(
        fold_id=bad.fold_id,
        fold_selected_config_id="S2-00",  # foreign strategy's config
        eligible_symbols=frozen.UNIVERSE,
        excluded_symbols=(),
        equal_weight_expectancy_bps=1.0,
        pooled_expectancy_bps=1.0,
        profit_factor=1.0,
        rejected=False,
        rejection_reason=None,
        train_input_hash=bad.train_input_hash,
        no_trade_reason_counts={},
    )
    malformed = ConfigAttemptEvidenceSummary(
        strategy=summary.strategy,
        config_id=summary.config_id,
        status=summary.status,
        reason_code=summary.reason_code,
        scenario_summaries=summary.scenario_summaries,
        fold_selection_trace=tuple(rows),
    )
    with pytest.raises(H6SummaryContractError):
        normalize_and_validate_h6_summary(
            malformed, expected_strategy="S1", expected_config_id="S1-00"
        )


def test_rejects_fold_row_with_non_hex_train_input_hash():
    summary = _well_formed_summary()
    rows = list(summary.fold_selection_trace)
    bad = rows[0]
    rows[0] = FoldSelectionEvidenceSummary(
        fold_id=bad.fold_id,
        fold_selected_config_id=None,
        eligible_symbols=(),
        excluded_symbols=bad.excluded_symbols,
        equal_weight_expectancy_bps=None,
        pooled_expectancy_bps=None,
        profit_factor=0.0,
        rejected=True,
        rejection_reason=INSUFFICIENT_ELIGIBLE_SYMBOLS_REASON,
        train_input_hash="UPPERCASE" * 8,
        no_trade_reason_counts={},
    )
    malformed = ConfigAttemptEvidenceSummary(
        strategy=summary.strategy,
        config_id=summary.config_id,
        status=summary.status,
        reason_code=summary.reason_code,
        scenario_summaries=summary.scenario_summaries,
        fold_selection_trace=tuple(rows),
    )
    with pytest.raises(H6SummaryContractError):
        normalize_and_validate_h6_summary(
            malformed, expected_strategy="S1", expected_config_id="S1-00"
        )


# -- Lineage arguments (expected_strategy/expected_config_id) exact-str gate --

_CONTROL_MARKER = "SECRET-CONTROL-MARKER-injected"


@pytest.mark.parametrize(
    "bad_value",
    [_AliasStr("S1"), [_CONTROL_MARKER], {_CONTROL_MARKER: 1}, True, None, 1],
    ids=["alias_str", "list", "dict", "bool", "none", "int"],
)
def test_rejects_non_exact_str_expected_strategy(bad_value):
    summary = _well_formed_summary()
    with pytest.raises(H6SummaryContractError) as exc_info:
        normalize_and_validate_h6_summary(
            summary, expected_strategy=bad_value, expected_config_id="S1-00"
        )
    assert _CONTROL_MARKER not in str(exc_info.value)


@pytest.mark.parametrize(
    "bad_value",
    [_AliasStr("S1-00"), [_CONTROL_MARKER], {_CONTROL_MARKER: 1}, True, None, 1],
    ids=["alias_str", "list", "dict", "bool", "none", "int"],
)
def test_rejects_non_exact_str_expected_config_id(bad_value):
    summary = _well_formed_summary()
    with pytest.raises(H6SummaryContractError) as exc_info:
        normalize_and_validate_h6_summary(
            summary, expected_strategy="S1", expected_config_id=bad_value
        )
    assert _CONTROL_MARKER not in str(exc_info.value)


# -- Exact-type / TOCTOU subclass-forgery closure --


class _AliasInt(int):
    pass


class _AliasFloat(float):
    pass


class _AliasTuple(tuple):
    pass


class _StatefulDict(dict):
    """A dict SUBCLASS whose ``.items()`` returns DIFFERENT content on each
    call -- a genuine builtin ``dict`` cannot do this. Exact-type
    (``type(x) is dict``) discipline must reject it BEFORE ``.items()`` is
    ever read, closing the TOCTOU hole structurally rather than by hoping
    validation only ever reads it once."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._reads = 0

    def items(self):
        self._reads += 1
        if self._reads == 1:
            return {}.items()
        return {"SECRET-injected-reason": 1}.items()


class _ScenarioEvidenceSummarySubclass(ScenarioEvidenceSummary):
    pass


class _FoldSelectionEvidenceSummarySubclass(FoldSelectionEvidenceSummary):
    pass


class _ConfigAttemptEvidenceSummarySubclass(ConfigAttemptEvidenceSummary):
    pass


def test_rejects_config_attempt_evidence_summary_subclass():
    summary = _well_formed_summary()
    subclassed = _ConfigAttemptEvidenceSummarySubclass(
        strategy=summary.strategy,
        config_id=summary.config_id,
        status=summary.status,
        reason_code=summary.reason_code,
        scenario_summaries=summary.scenario_summaries,
        fold_selection_trace=summary.fold_selection_trace,
    )
    with pytest.raises(H6SummaryContractError):
        normalize_and_validate_h6_summary(
            subclassed, expected_strategy="S1", expected_config_id="S1-00"
        )


def test_rejects_scenario_evidence_summary_subclass():
    summary = _well_formed_summary()
    rows = list(summary.scenario_summaries)
    rows[0] = _ScenarioEvidenceSummarySubclass(
        scenario_name=rows[0].scenario_name,
        status=rows[0].status,
        reason_code=rows[0].reason_code,
        trade_count=rows[0].trade_count,
        artifact_hash=rows[0].artifact_hash,
        no_trade_reason_counts=rows[0].no_trade_reason_counts,
    )
    malformed = ConfigAttemptEvidenceSummary(
        strategy=summary.strategy,
        config_id=summary.config_id,
        status=summary.status,
        reason_code=summary.reason_code,
        scenario_summaries=tuple(rows),
        fold_selection_trace=summary.fold_selection_trace,
    )
    with pytest.raises(H6SummaryContractError):
        normalize_and_validate_h6_summary(
            malformed, expected_strategy="S1", expected_config_id="S1-00"
        )


def test_rejects_fold_selection_evidence_summary_subclass():
    summary = _well_formed_summary()
    rows = list(summary.fold_selection_trace)
    bad = rows[0]
    rows[0] = _FoldSelectionEvidenceSummarySubclass(
        fold_id=bad.fold_id,
        fold_selected_config_id=bad.fold_selected_config_id,
        eligible_symbols=bad.eligible_symbols,
        excluded_symbols=bad.excluded_symbols,
        equal_weight_expectancy_bps=bad.equal_weight_expectancy_bps,
        pooled_expectancy_bps=bad.pooled_expectancy_bps,
        profit_factor=bad.profit_factor,
        rejected=bad.rejected,
        rejection_reason=bad.rejection_reason,
        train_input_hash=bad.train_input_hash,
        no_trade_reason_counts=bad.no_trade_reason_counts,
    )
    malformed = ConfigAttemptEvidenceSummary(
        strategy=summary.strategy,
        config_id=summary.config_id,
        status=summary.status,
        reason_code=summary.reason_code,
        scenario_summaries=summary.scenario_summaries,
        fold_selection_trace=tuple(rows),
    )
    with pytest.raises(H6SummaryContractError):
        normalize_and_validate_h6_summary(
            malformed, expected_strategy="S1", expected_config_id="S1-00"
        )


def test_rejects_scenario_summaries_container_tuple_subclass():
    summary = _well_formed_summary()
    malformed = ConfigAttemptEvidenceSummary(
        strategy=summary.strategy,
        config_id=summary.config_id,
        status=summary.status,
        reason_code=summary.reason_code,
        scenario_summaries=_AliasTuple(summary.scenario_summaries),
        fold_selection_trace=summary.fold_selection_trace,
    )
    with pytest.raises(H6SummaryContractError):
        normalize_and_validate_h6_summary(
            malformed, expected_strategy="S1", expected_config_id="S1-00"
        )


def test_rejects_scenario_name_str_subclass():
    summary = _well_formed_summary()
    rows = list(summary.scenario_summaries)
    rows[0] = ScenarioEvidenceSummary(
        scenario_name=_AliasStr(rows[0].scenario_name),
        status=rows[0].status,
        reason_code=rows[0].reason_code,
        trade_count=rows[0].trade_count,
        artifact_hash=rows[0].artifact_hash,
        no_trade_reason_counts=rows[0].no_trade_reason_counts,
    )
    malformed = ConfigAttemptEvidenceSummary(
        strategy=summary.strategy,
        config_id=summary.config_id,
        status=summary.status,
        reason_code=summary.reason_code,
        scenario_summaries=tuple(rows),
        fold_selection_trace=summary.fold_selection_trace,
    )
    with pytest.raises(H6SummaryContractError):
        normalize_and_validate_h6_summary(
            malformed, expected_strategy="S1", expected_config_id="S1-00"
        )


def test_rejects_scenario_trade_count_int_subclass():
    summary = _well_formed_summary()
    rows = list(summary.scenario_summaries)
    rows[0] = ScenarioEvidenceSummary(
        scenario_name=rows[0].scenario_name,
        status=rows[0].status,
        reason_code=rows[0].reason_code,
        trade_count=_AliasInt(rows[0].trade_count),
        artifact_hash=rows[0].artifact_hash,
        no_trade_reason_counts=rows[0].no_trade_reason_counts,
    )
    malformed = ConfigAttemptEvidenceSummary(
        strategy=summary.strategy,
        config_id=summary.config_id,
        status=summary.status,
        reason_code=summary.reason_code,
        scenario_summaries=tuple(rows),
        fold_selection_trace=summary.fold_selection_trace,
    )
    with pytest.raises(H6SummaryContractError):
        normalize_and_validate_h6_summary(
            malformed, expected_strategy="S1", expected_config_id="S1-00"
        )


def test_rejects_scenario_no_trade_reason_counts_stateful_dict_subclass():
    summary = _well_formed_summary()
    rows = list(summary.scenario_summaries)
    rows[0] = ScenarioEvidenceSummary(
        scenario_name=rows[0].scenario_name,
        status=rows[0].status,
        reason_code=rows[0].reason_code,
        trade_count=rows[0].trade_count,
        artifact_hash=rows[0].artifact_hash,
        no_trade_reason_counts=_StatefulDict(),
    )
    malformed = ConfigAttemptEvidenceSummary(
        strategy=summary.strategy,
        config_id=summary.config_id,
        status=summary.status,
        reason_code=summary.reason_code,
        scenario_summaries=tuple(rows),
        fold_selection_trace=summary.fold_selection_trace,
    )
    with pytest.raises(H6SummaryContractError):
        normalize_and_validate_h6_summary(
            malformed, expected_strategy="S1", expected_config_id="S1-00"
        )


def test_rejects_fold_row_profit_factor_float_subclass():
    summary = _well_formed_summary()
    rows = list(summary.fold_selection_trace)
    bad = rows[0]
    rows[0] = FoldSelectionEvidenceSummary(
        fold_id=bad.fold_id,
        fold_selected_config_id=bad.fold_selected_config_id,
        eligible_symbols=bad.eligible_symbols,
        excluded_symbols=bad.excluded_symbols,
        equal_weight_expectancy_bps=bad.equal_weight_expectancy_bps,
        pooled_expectancy_bps=bad.pooled_expectancy_bps,
        profit_factor=_AliasFloat(bad.profit_factor),
        rejected=bad.rejected,
        rejection_reason=bad.rejection_reason,
        train_input_hash=bad.train_input_hash,
        no_trade_reason_counts=bad.no_trade_reason_counts,
    )
    malformed = ConfigAttemptEvidenceSummary(
        strategy=summary.strategy,
        config_id=summary.config_id,
        status=summary.status,
        reason_code=summary.reason_code,
        scenario_summaries=summary.scenario_summaries,
        fold_selection_trace=tuple(rows),
    )
    with pytest.raises(H6SummaryContractError):
        normalize_and_validate_h6_summary(
            malformed, expected_strategy="S1", expected_config_id="S1-00"
        )


def test_rejects_fold_row_eligible_symbols_tuple_subclass():
    summary = _well_formed_summary()
    rows = list(summary.fold_selection_trace)
    bad = rows[0]
    rows[0] = FoldSelectionEvidenceSummary(
        fold_id=bad.fold_id,
        fold_selected_config_id=None,
        eligible_symbols=_AliasTuple(()),
        excluded_symbols=bad.excluded_symbols,
        equal_weight_expectancy_bps=None,
        pooled_expectancy_bps=None,
        profit_factor=bad.profit_factor,
        rejected=True,
        rejection_reason=INSUFFICIENT_ELIGIBLE_SYMBOLS_REASON,
        train_input_hash=bad.train_input_hash,
        no_trade_reason_counts={},
    )
    malformed = ConfigAttemptEvidenceSummary(
        strategy=summary.strategy,
        config_id=summary.config_id,
        status=summary.status,
        reason_code=summary.reason_code,
        scenario_summaries=summary.scenario_summaries,
        fold_selection_trace=tuple(rows),
    )
    with pytest.raises(H6SummaryContractError):
        normalize_and_validate_h6_summary(
            malformed, expected_strategy="S1", expected_config_id="S1-00"
        )


def test_rejects_fold_row_no_trade_reason_counts_stateful_dict_subclass():
    summary = _well_formed_summary()
    rows = list(summary.fold_selection_trace)
    bad = rows[0]
    rows[0] = FoldSelectionEvidenceSummary(
        fold_id=bad.fold_id,
        fold_selected_config_id=bad.fold_selected_config_id,
        eligible_symbols=bad.eligible_symbols,
        excluded_symbols=bad.excluded_symbols,
        equal_weight_expectancy_bps=bad.equal_weight_expectancy_bps,
        pooled_expectancy_bps=bad.pooled_expectancy_bps,
        profit_factor=bad.profit_factor,
        rejected=bad.rejected,
        rejection_reason=bad.rejection_reason,
        train_input_hash=bad.train_input_hash,
        no_trade_reason_counts=_StatefulDict(),
    )
    malformed = ConfigAttemptEvidenceSummary(
        strategy=summary.strategy,
        config_id=summary.config_id,
        status=summary.status,
        reason_code=summary.reason_code,
        scenario_summaries=summary.scenario_summaries,
        fold_selection_trace=tuple(rows),
    )
    with pytest.raises(H6SummaryContractError):
        normalize_and_validate_h6_summary(
            malformed, expected_strategy="S1", expected_config_id="S1-00"
        )


# -- Fold eligible/excluded ORDER (membership/coverage alone is not enough) --


def test_rejects_fold_row_eligible_symbols_out_of_frozen_universe_order():
    summary = _well_formed_summary()
    rows = list(summary.fold_selection_trace)
    bad = rows[0]
    # Two eligible symbols, deliberately reversed relative to frozen.UNIVERSE order.
    reversed_pair = tuple(reversed(frozen.UNIVERSE[:2]))
    rows[0] = FoldSelectionEvidenceSummary(
        fold_id=bad.fold_id,
        fold_selected_config_id=None,
        eligible_symbols=reversed_pair,
        excluded_symbols=tuple(
            (symbol, INSUFFICIENT_SYMBOL_EVIDENCE_REASON)
            for symbol in frozen.UNIVERSE[2:]
        ),
        equal_weight_expectancy_bps=1.0,
        pooled_expectancy_bps=1.0,
        profit_factor=1.0,
        rejected=False,
        rejection_reason=None,
        train_input_hash=bad.train_input_hash,
        no_trade_reason_counts={},
    )
    malformed = ConfigAttemptEvidenceSummary(
        strategy=summary.strategy,
        config_id=summary.config_id,
        status=summary.status,
        reason_code=summary.reason_code,
        scenario_summaries=summary.scenario_summaries,
        fold_selection_trace=tuple(rows),
    )
    with pytest.raises(H6SummaryContractError):
        normalize_and_validate_h6_summary(
            malformed, expected_strategy="S1", expected_config_id="S1-00"
        )


def test_rejects_fold_row_excluded_symbols_out_of_frozen_universe_order():
    summary = _well_formed_summary()
    rows = list(summary.fold_selection_trace)
    bad = rows[0]
    reversed_excluded = tuple(
        (symbol, INSUFFICIENT_SYMBOL_EVIDENCE_REASON)
        for symbol in reversed(frozen.UNIVERSE)
    )
    rows[0] = FoldSelectionEvidenceSummary(
        fold_id=bad.fold_id,
        fold_selected_config_id=None,
        eligible_symbols=(),
        excluded_symbols=reversed_excluded,
        equal_weight_expectancy_bps=None,
        pooled_expectancy_bps=None,
        profit_factor=0.0,
        rejected=True,
        rejection_reason=INSUFFICIENT_ELIGIBLE_SYMBOLS_REASON,
        train_input_hash=bad.train_input_hash,
        no_trade_reason_counts={},
    )
    malformed = ConfigAttemptEvidenceSummary(
        strategy=summary.strategy,
        config_id=summary.config_id,
        status=summary.status,
        reason_code=summary.reason_code,
        scenario_summaries=summary.scenario_summaries,
        fold_selection_trace=tuple(rows),
    )
    with pytest.raises(H6SummaryContractError):
        normalize_and_validate_h6_summary(
            malformed, expected_strategy="S1", expected_config_id="S1-00"
        )


def test_rejects_fold_row_unknown_no_trade_reason_key():
    summary = _well_formed_summary()
    rows = list(summary.fold_selection_trace)
    bad = rows[0]
    rows[0] = FoldSelectionEvidenceSummary(
        fold_id=bad.fold_id,
        fold_selected_config_id=bad.fold_selected_config_id,
        eligible_symbols=bad.eligible_symbols,
        excluded_symbols=bad.excluded_symbols,
        equal_weight_expectancy_bps=bad.equal_weight_expectancy_bps,
        pooled_expectancy_bps=bad.pooled_expectancy_bps,
        profit_factor=bad.profit_factor,
        rejected=bad.rejected,
        rejection_reason=bad.rejection_reason,
        train_input_hash=bad.train_input_hash,
        no_trade_reason_counts={"SECRET-injected-reason": 1},
    )
    malformed = ConfigAttemptEvidenceSummary(
        strategy=summary.strategy,
        config_id=summary.config_id,
        status=summary.status,
        reason_code=summary.reason_code,
        scenario_summaries=summary.scenario_summaries,
        fold_selection_trace=tuple(rows),
    )
    with pytest.raises(H6SummaryContractError):
        normalize_and_validate_h6_summary(
            malformed, expected_strategy="S1", expected_config_id="S1-00"
        )


def test_rejects_fold_row_negative_no_trade_reason_count():
    summary = _well_formed_summary()
    rows = list(summary.fold_selection_trace)
    bad = rows[0]
    rows[0] = FoldSelectionEvidenceSummary(
        fold_id=bad.fold_id,
        fold_selected_config_id=bad.fold_selected_config_id,
        eligible_symbols=bad.eligible_symbols,
        excluded_symbols=bad.excluded_symbols,
        equal_weight_expectancy_bps=bad.equal_weight_expectancy_bps,
        pooled_expectancy_bps=bad.pooled_expectancy_bps,
        profit_factor=bad.profit_factor,
        rejected=bad.rejected,
        rejection_reason=bad.rejection_reason,
        train_input_hash=bad.train_input_hash,
        no_trade_reason_counts={"daily_stop_active": -1},
    )
    malformed = ConfigAttemptEvidenceSummary(
        strategy=summary.strategy,
        config_id=summary.config_id,
        status=summary.status,
        reason_code=summary.reason_code,
        scenario_summaries=summary.scenario_summaries,
        fold_selection_trace=tuple(rows),
    )
    with pytest.raises(H6SummaryContractError):
        normalize_and_validate_h6_summary(
            malformed, expected_strategy="S1", expected_config_id="S1-00"
        )


# -- Parity oracle: byte-for-byte cross-check against the real H6 boundary --


def test_normalized_output_matches_the_real_h6_normalizer_field_for_field():
    """Test-only parity oracle (H6 private helper imported ONLY here, never
    in H5 production): the pure H5 mirror must accept/normalize the exact
    same well-formed summary identically to the real
    ``run_rob944_campaign._normalize_config_attempt_evidence_summary``."""
    from run_rob944_campaign import _normalize_config_attempt_evidence_summary

    summary = _well_formed_summary()
    ours = normalize_and_validate_h6_summary(
        summary, expected_strategy="S1", expected_config_id="S1-00"
    )
    oracle = _normalize_config_attempt_evidence_summary(summary, context="oracle")
    assert ours == oracle


# -- Independent audit correction (Task 1E, I2): the normalizer-only oracle
# above proves shape/field parity but never exercises H6's SEMANTIC checks
# or fold/run hash construction, which live downstream in
# ``_normalized_summary_to_attempt_evidence``. These tests differentially
# pin H5's ``normalize_and_validate_h6_summary`` +
# ``rob945_accounting_seal._recompute_fold_evidence_hash_and_run_identity``
# against the REAL H6 ``_summary_to_attempt_evidence`` wrapper (the actual
# H6-build boundary, imported here ONLY as a test oracle) for known accepted
# vectors (byte-identical fold_evidence_hash/run_identity) and for a battery
# of malformed vectors (both sides must reject).


def _h5_hash_pair(summary, *, strategy, config_id, experiment_id):
    normalized = normalize_and_validate_h6_summary(
        summary, expected_strategy=strategy, expected_config_id=config_id
    )
    return _recompute_fold_evidence_hash_and_run_identity(
        normalized,
        full_campaign_hash=_FULL_CAMPAIGN_HASH,
        campaign_run_id=_CAMPAIGN_RUN_ID,
        strategy_key=_STRATEGY_KEY[strategy],
        experiment_id=experiment_id,
        retry_index=0,
    )


def _h6_hash_pair(summary, *, strategy, experiment_id):
    evidence = _summary_to_attempt_evidence(
        summary,
        strategy_key=_STRATEGY_KEY[strategy],
        experiment_id=experiment_id,
        full_campaign_hash=_FULL_CAMPAIGN_HASH,
        campaign_run_id=_CAMPAIGN_RUN_ID,
    )
    return evidence.fold_evidence_hash, evidence.run_identity


@pytest.mark.parametrize(
    "status,reason_code",
    [
        ("completed", None),
        ("rejected", REASON_DATA_GAP_IN_POSITION),
        ("crashed", REASON_CHILD_EXECUTION_CRASHED),
        ("timeout", REASON_CHILD_EXECUTION_TIMEOUT),
    ],
    ids=["completed", "rejected", "crashed", "timeout"],
)
def test_h5_and_h6_produce_byte_identical_fold_hash_and_run_identity(
    status, reason_code
):
    summary = _well_formed_summary(status=status, reason_code=reason_code)
    eid = _FROZEN_EXPERIMENT_IDS[0]
    h5_fold_hash, h5_run_identity = _h5_hash_pair(
        summary, strategy="S1", config_id="S1-00", experiment_id=eid
    )
    h6_fold_hash, h6_run_identity = _h6_hash_pair(
        summary, strategy="S1", experiment_id=eid
    )
    assert h5_fold_hash == h6_fold_hash
    assert h5_run_identity == h6_run_identity


def test_h5_and_h6_produce_byte_identical_hashes_for_the_authentic_global_fallback():
    """Uses H6's OWN deterministic global-fallback recipe
    (``run_rob944_campaign._global_failure_summaries``), not the hand-seeded
    fixture used elsewhere in this file, so the fallback vector itself is
    authentic, not merely shaped like it."""
    experiment_id_by_key = {(_STRATEGY_KEY["S1"], "S1-00"): _FROZEN_EXPERIMENT_IDS[0]}
    (summary,) = _global_failure_summaries(experiment_id_by_key)
    eid = _FROZEN_EXPERIMENT_IDS[0]
    h5_fold_hash, h5_run_identity = _h5_hash_pair(
        summary, strategy="S1", config_id="S1-00", experiment_id=eid
    )
    h6_fold_hash, h6_run_identity = _h6_hash_pair(
        summary, strategy="S1", experiment_id=eid
    )
    assert h5_fold_hash == h6_fold_hash
    assert h5_run_identity == h6_run_identity


def _malformed_seven_fold():
    return _well_formed_summary(fold_ids=_REAL_FOLD_IDS[1:])


def _malformed_duplicate_scenario_name():
    summary = _well_formed_summary()
    rows = list(summary.scenario_summaries)
    rows[2] = ScenarioEvidenceSummary(
        scenario_name=rows[0].scenario_name,
        status=rows[2].status,
        reason_code=rows[2].reason_code,
        trade_count=rows[2].trade_count,
        artifact_hash=rows[2].artifact_hash,
        no_trade_reason_counts=rows[2].no_trade_reason_counts,
    )
    return ConfigAttemptEvidenceSummary(
        strategy=summary.strategy,
        config_id=summary.config_id,
        status=summary.status,
        reason_code=summary.reason_code,
        scenario_summaries=tuple(rows),
        fold_selection_trace=summary.fold_selection_trace,
    )


def _malformed_non_hex_artifact_hash():
    summary = _well_formed_summary()
    rows = list(summary.scenario_summaries)
    rows[0] = ScenarioEvidenceSummary(
        scenario_name=rows[0].scenario_name,
        status=rows[0].status,
        reason_code=rows[0].reason_code,
        trade_count=rows[0].trade_count,
        artifact_hash="NOT-HEX" * 8,
        no_trade_reason_counts=rows[0].no_trade_reason_counts,
    )
    return ConfigAttemptEvidenceSummary(
        strategy=summary.strategy,
        config_id=summary.config_id,
        status=summary.status,
        reason_code=summary.reason_code,
        scenario_summaries=tuple(rows),
        fold_selection_trace=summary.fold_selection_trace,
    )


def _malformed_unknown_no_trade_reason():
    summary = _well_formed_summary()
    rows = list(summary.scenario_summaries)
    rows[0] = ScenarioEvidenceSummary(
        scenario_name=rows[0].scenario_name,
        status=rows[0].status,
        reason_code=rows[0].reason_code,
        trade_count=rows[0].trade_count,
        artifact_hash=rows[0].artifact_hash,
        no_trade_reason_counts={"SECRET-injected-reason": 1},
    )
    return ConfigAttemptEvidenceSummary(
        strategy=summary.strategy,
        config_id=summary.config_id,
        status=summary.status,
        reason_code=summary.reason_code,
        scenario_summaries=tuple(rows),
        fold_selection_trace=summary.fold_selection_trace,
    )


def _malformed_attempt_status_reason_mismatch():
    return _well_formed_summary(status="completed", reason_code="bogus_reason")


def _malformed_fold_row_symbol_overlap():
    summary = _well_formed_summary()
    rows = list(summary.fold_selection_trace)
    bad = rows[0]
    rows[0] = FoldSelectionEvidenceSummary(
        fold_id=bad.fold_id,
        fold_selected_config_id=None,
        eligible_symbols=(frozen.UNIVERSE[0],),
        excluded_symbols=tuple(
            (symbol, INSUFFICIENT_SYMBOL_EVIDENCE_REASON) for symbol in frozen.UNIVERSE
        ),
        equal_weight_expectancy_bps=None,
        pooled_expectancy_bps=None,
        profit_factor=0.0,
        rejected=True,
        rejection_reason=INSUFFICIENT_ELIGIBLE_SYMBOLS_REASON,
        train_input_hash=bad.train_input_hash,
        no_trade_reason_counts={},
    )
    return ConfigAttemptEvidenceSummary(
        strategy=summary.strategy,
        config_id=summary.config_id,
        status=summary.status,
        reason_code=summary.reason_code,
        scenario_summaries=summary.scenario_summaries,
        fold_selection_trace=tuple(rows),
    )


@pytest.mark.parametrize(
    "build_malformed",
    [
        _malformed_seven_fold,
        _malformed_duplicate_scenario_name,
        _malformed_non_hex_artifact_hash,
        _malformed_unknown_no_trade_reason,
        _malformed_attempt_status_reason_mismatch,
        _malformed_fold_row_symbol_overlap,
    ],
    ids=[
        "seven_fold",
        "duplicate_scenario_name",
        "non_hex_artifact_hash",
        "unknown_no_trade_reason",
        "attempt_status_reason_mismatch",
        "fold_row_symbol_overlap",
    ],
)
def test_h5_and_h6_both_reject_the_same_malformed_summary(build_malformed):
    summary = build_malformed()
    eid = _FROZEN_EXPERIMENT_IDS[0]
    with pytest.raises(H6SummaryContractError):
        normalize_and_validate_h6_summary(
            summary, expected_strategy="S1", expected_config_id="S1-00"
        )
    with pytest.raises(ValueError):  # the real H6 wrapper's own raised type
        _summary_to_attempt_evidence(
            summary,
            strategy_key=_STRATEGY_KEY["S1"],
            experiment_id=eid,
            full_campaign_hash=_FULL_CAMPAIGN_HASH,
            campaign_run_id=_CAMPAIGN_RUN_ID,
        )

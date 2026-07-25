"""ROB-983 (H5, CP2) -- selected-OOS dual evidence and PBO prerequisites.

Unique generator evidence (scenario-independent, H3-canonical candidate
identity) is kept strictly separate from each of the three path invocation
evidence rows (base13/primary_stress17/upward_stress22). Every path's
accepted-input hash/count must trace back to the SAME unique accepted set --
never a scenario sum/intersection/first-path reconstruction, and never a
tripled count. PBO is required, independently evaluated, 24x365/slices=4,
reference-only (it can never flip a verdict) -- missing/placeholder/invalid
PBO makes the campaign incomplete, never a raise-on-missing (a caller may
legitimately not have it yet).
"""

from __future__ import annotations

import pytest
from rob974_h5_contracts import H5InputError
from rob974_h5_dual_evidence import (
    PBO_CONFIG_COUNT,
    PBO_DAY_COUNT,
    PBO_SCENARIO_NAME,
    PBO_SLICES,
    PathInvocationEvidence,
    PboEvidence,
    UniqueGeneratorEvidence,
    cross_check_dual_evidence,
    validate_pbo_evidence,
)


def _unique(**overrides) -> UniqueGeneratorEvidence:
    base = {
        "strategy": "S3",
        "config_id": "S3-00",
        "fold_id": "fold-00",
        "phase": "selected_oos",
        "evaluated_decision_units": 20,
        "no_signal": 5,
        "no_signal_reason_histogram": {"momentum": 5},
        "accepted": 10,
        "rejected": 5,
        "accepted_input_hash": "a" * 64,
        "rejection_reason_histogram": {
            "next_bar_unavailable": 3,
            "tp_below_r_min_sl": 2,
        },
    }
    base.update(overrides)
    return UniqueGeneratorEvidence(**base)


def _path(
    path_scenario: str, unique: UniqueGeneratorEvidence, **overrides
) -> PathInvocationEvidence:
    base = {
        "strategy": unique.strategy,
        "config_id": unique.config_id,
        "fold_id": unique.fold_id,
        "path_scenario": path_scenario,
        "unique_evidence_hash": unique.accepted_input_hash,
        "unique_evidence_accepted_count": unique.accepted,
        "engine_input_hash": "b" * 64,
        "engine_input_count": unique.accepted,
        "no_trade_reason_counts": {"insufficient_oos_exit_horizon": 1},
        "ledger_status": "completed",
        "trade_count": unique.accepted - 1,
        "artifact_hash": "c" * 64,
    }
    base.update(overrides)
    return PathInvocationEvidence(**base)


def _three_paths(unique: UniqueGeneratorEvidence) -> dict[str, PathInvocationEvidence]:
    return {
        name: _path(name, unique)
        for name in ("base13", "primary_stress17", "upward_stress22")
    }


class TestUniqueGeneratorEvidenceInvariants:
    def test_phase_and_no_signal_histogram_are_observable(self):
        unique = _unique()
        assert unique.phase == "selected_oos"
        assert unique.evaluated_decision_units == 20
        assert unique.no_signal_reason_histogram == {"momentum": 5}

    def test_no_signal_histogram_cannot_be_empty_when_no_signal_is_nonzero(self):
        with pytest.raises(
            H5InputError,
            match="unique_evidence_no_signal_histogram_subtotal_mismatch",
        ):
            _unique(no_signal_reason_histogram={})

    def test_evaluated_units_partition_is_enforced(self):
        with pytest.raises(
            H5InputError, match="unique_evidence_evaluated_partition_mismatch"
        ):
            _unique(evaluated_decision_units=19)

    def test_candidate_equals_accepted_plus_rejected(self):
        unique = _unique()
        assert unique.candidate == unique.accepted + unique.rejected == 15

    def test_reason_histogram_subtotal_equals_rejected(self):
        with pytest.raises(H5InputError):
            _unique(
                rejection_reason_histogram={"next_bar_unavailable": 1}
            )  # sums to 1, not 5

    def test_negative_accepted_rejected(self):
        with pytest.raises(H5InputError):
            _unique(accepted=-1)

    def test_bad_hash_format_rejected(self):
        with pytest.raises(H5InputError):
            _unique(accepted_input_hash="not-hex")

    def test_zero_rejection_with_empty_histogram_is_valid(self):
        unique = _unique(
            evaluated_decision_units=15,
            rejected=0,
            rejection_reason_histogram={},
        )
        assert unique.rejected == 0
        assert unique.rejection_reason_histogram == {}

    def test_nonzero_rejection_cannot_be_hidden_as_empty_histogram(self):
        with pytest.raises(H5InputError):
            _unique(rejected=5, rejection_reason_histogram={})


class TestPathAgreesWithUniqueSource:
    def test_valid_three_paths_cross_check_passes(self):
        unique = _unique()
        paths = _three_paths(unique)
        cross_check_dual_evidence(unique, paths)  # must not raise

    def test_train_accepted_counts_cannot_be_bound_to_selected_oos_paths(self):
        unique = _unique(phase="train")
        with pytest.raises(
            H5InputError,
            match="dual_evidence_path_requires_selected_oos_unique_phase",
        ):
            cross_check_dual_evidence(unique, _three_paths(unique))

    def test_missing_path_rejected(self):
        unique = _unique()
        paths = _three_paths(unique)
        del paths["upward_stress22"]
        with pytest.raises(H5InputError):
            cross_check_dual_evidence(unique, paths)

    def test_path_referencing_wrong_unique_hash_rejected(self):
        unique = _unique()
        paths = _three_paths(unique)
        paths["base13"] = _path("base13", unique, unique_evidence_hash="f" * 64)
        with pytest.raises(H5InputError):
            cross_check_dual_evidence(unique, paths)

    def test_path_tripling_accepted_count_rejected(self):
        # Simulates the "tripled unique counts" mutant: a path claims the
        # accepted count is 3x the real unique-accepted value (as if
        # summed across all three scenario paths).
        unique = _unique()
        paths = _three_paths(unique)
        paths["primary_stress17"] = _path(
            "primary_stress17",
            unique,
            unique_evidence_accepted_count=unique.accepted * 3,
        )
        with pytest.raises(H5InputError):
            cross_check_dual_evidence(unique, paths)

    def test_first_path_reconstruction_mutant_rejected(self):
        # A caller must not substitute "whatever the first path happened to
        # see" as if it were the canonical unique accepted count.
        unique = _unique()
        paths = _three_paths(unique)
        wrong_first = dict(paths)
        wrong_first["base13"] = _path(
            "base13", unique, unique_evidence_accepted_count=unique.accepted - 1
        )
        with pytest.raises(H5InputError):
            cross_check_dual_evidence(unique, wrong_first)

    def test_intersection_reconstruction_mutant_rejected(self):
        unique = _unique()
        paths = _three_paths(unique)
        # Pretend the caller computed an intersection-derived accepted
        # count smaller than the true unique accepted set.
        paths["upward_stress22"] = _path(
            "upward_stress22",
            unique,
            unique_evidence_accepted_count=unique.accepted // 2,
        )
        with pytest.raises(H5InputError):
            cross_check_dual_evidence(unique, paths)

    def test_missing_zero_default_engine_hash_rejected(self):
        unique = _unique()
        with pytest.raises(H5InputError):
            _path("base13", unique, engine_input_hash="")

    def test_nonzero_path_rejection_reason_stays_nonzero(self):
        unique = _unique()
        paths = _three_paths(unique)
        paths["base13"] = _path(
            "base13",
            unique,
            no_trade_reason_counts={"expected_funding_cost_above_3bps": 2},
        )
        cross_check_dual_evidence(unique, paths)  # must not raise
        assert paths["base13"].no_trade_reason_counts == {
            "expected_funding_cost_above_3bps": 2
        }

    def test_positive_accepted_zero_trade_requires_complete_reason_accounting(self):
        unique = _unique()
        paths = _three_paths(unique)
        paths["base13"] = _path(
            "base13",
            unique,
            trade_count=0,
            no_trade_reason_counts={},
        )
        with pytest.raises(
            H5InputError,
            match="dual_evidence_zero_trade_reason_subtotal_mismatch",
        ):
            cross_check_dual_evidence(unique, paths)

    def test_positive_accepted_zero_trade_reason_total_must_equal_accepted(self):
        unique = _unique()
        paths = _three_paths(unique)
        paths["base13"] = _path(
            "base13",
            unique,
            trade_count=0,
            no_trade_reason_counts={
                "funding_evidence_unavailable": 4,
                "next_tick_unavailable": 6,
            },
        )
        cross_check_dual_evidence(unique, paths)

    def test_wrong_strategy_or_config_binding_rejected(self):
        unique = _unique()
        paths = _three_paths(unique)
        paths["base13"] = _path("base13", unique, config_id="S3-01")
        with pytest.raises(H5InputError):
            cross_check_dual_evidence(unique, paths)


class TestPboPrerequisites:
    def _pbo(self, **overrides) -> PboEvidence:
        base = {
            "strategy": "S3",
            "config_count": PBO_CONFIG_COUNT,
            "day_count": PBO_DAY_COUNT,
            "slices": PBO_SLICES,
            "scenario_name": PBO_SCENARIO_NAME,
            "value": 0.42,
            "reason_codes": (),
            "source_hash": "a" * 64,
            "input_hash": "b" * 64,
            "artifact_hash": "c" * 64,
        }
        base.update(overrides)
        return PboEvidence(**base)

    def test_valid_pbo_evidence_passes(self):
        result = validate_pbo_evidence(self._pbo())
        assert result.ok is True

    def test_23_configs_rejected(self):
        with pytest.raises(H5InputError):
            self._pbo(config_count=23)

    def test_25_configs_rejected(self):
        with pytest.raises(H5InputError):
            self._pbo(config_count=25)

    def test_364_days_rejected(self):
        with pytest.raises(H5InputError):
            self._pbo(day_count=364)

    def test_366_days_rejected(self):
        with pytest.raises(H5InputError):
            self._pbo(day_count=366)

    def test_wrong_slices_rejected(self):
        with pytest.raises(H5InputError):
            self._pbo(slices=5)

    def test_wrong_scenario_rejected(self):
        with pytest.raises(H5InputError):
            self._pbo(scenario_name="base13")

    def test_missing_pbo_evidence_is_incomplete_not_a_raise(self):
        result = validate_pbo_evidence(None)
        assert result.ok is False
        assert "missing_pbo_evidence" in result.incomplete_reasons

    def test_evaluator_failure_reason_code_is_incomplete(self):
        pbo = self._pbo(value=None, reason_codes=("evaluator_failed",))
        result = validate_pbo_evidence(pbo)
        assert result.ok is False

    def test_extreme_pbo_value_never_flips_a_verdict(self):
        # PboEvidence exposes no verdict-shaped field at all -- there is
        # nothing for a caller to branch a pass/fail decision on besides
        # the (reference-only) value/reason_codes fields, and neither this
        # module nor any gate-evaluation module ever imports the value into
        # a hard gate.
        overfit = self._pbo(value=0.99)
        clean = self._pbo(value=0.01)
        assert validate_pbo_evidence(overfit).ok == validate_pbo_evidence(clean).ok
        assert not hasattr(overfit, "verdict")
        assert not hasattr(clean, "verdict")

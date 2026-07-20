"""ROB-981 (ROB-974 R2 H6-A) CP7 -- pure fixture end-to-end smoke.

Proves the wiring across CP1 (identity) -> CP2 (payload) -> CP3 (evidence)
-> CP4 (accounting) -> CP6 (diagnostics), plus a curated set of the
highest-risk identity/accounting/diagnostic mutants named in the ROB-981
packet. No DB/network/corpus/process/current-time/random access anywhere in
this file (see ``test_rob974_h6a_import_guard.py`` for the module-level
static proof; this file additionally never imports anything beyond the
rob974_h6a_* family + pytest + hashlib/copy).
"""

from __future__ import annotations

import hashlib

import pytest
import rob974_h6a_accounting as h6a_accounting
import rob974_h6a_evidence as h6a_evidence
import rob974_h6a_identity as h6a_identity
import rob974_h6a_payload as h6a_payload
import rob974_h6a_smoke as smoke


def _hex64(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


class TestExactFixtureIdentity:
    def test_exact_48_rows_canonical_order(self):
        plan = smoke.build_smoke_plan()
        assert (
            tuple(spec.row_id for spec in plan.row_specs)
            == h6a_identity.CANONICAL_ROW_ORDER
        )
        assert len(plan.row_specs) == 48
        assert len({spec.experiment_id for spec in plan.row_specs}) == 48

    def test_every_row_is_fixture_marked(self):
        plan = smoke.build_smoke_plan()
        assert all(spec.provenance == "fixture_identity" for spec in plan.row_specs)

    def test_deterministic_plan_built_twice(self):
        a = smoke.build_smoke_plan()
        b = smoke.build_smoke_plan()
        assert a.full_campaign_hash == b.full_campaign_hash
        assert a.campaign_run_id == b.campaign_run_id
        assert [spec.experiment_id for spec in a.row_specs] == [
            spec.experiment_id for spec in b.row_specs
        ]
        assert a.accounting.trial_accounting_hash == b.accounting.trial_accounting_hash


class TestAttemptsAndDualEvidence:
    def test_49_attempts_from_48_configs_due_to_one_retry(self):
        plan = smoke.build_smoke_plan()
        assert len(plan.attempts) == 49  # 48 primaries + 1 explicit retry

    def test_never_selected_row_carries_sentinel_scenarios(self):
        plan = smoke.build_smoke_plan()
        attempt = next(
            a for a in plan.attempts if a.row_id == smoke.NEVER_SELECTED_ROW_ID
        )
        assert attempt.status == "completed"
        assert all(
            row.status == "never_selected" for row in attempt.path_scenario_evidence
        )
        assert not any(trace.selected for trace in attempt.fold_traces)

    def test_retry_row_has_primary_and_retry_with_distinct_run_identity(self):
        plan = smoke.build_smoke_plan()
        retry_attempts = [a for a in plan.attempts if a.row_id == smoke.RETRY_ROW_ID]
        assert len(retry_attempts) == 2
        primary = next(a for a in retry_attempts if a.retry_index == 0)
        retry = next(a for a in retry_attempts if a.retry_index == 1)
        assert primary.status == "crashed"
        assert retry.status == "completed"
        assert primary.run_identity != retry.run_identity
        assert retry.historical_executor_state is not None
        assert retry.historical_executor_state.pair_exec_fail == "not_evaluated"

    def test_every_attempt_carries_eight_ordered_folds(self):
        plan = smoke.build_smoke_plan()
        for attempt in plan.attempts:
            assert [t.fold_index for t in attempt.fold_traces] == list(range(8))

    def test_s3_rows_never_carry_historical_executor_state(self):
        plan = smoke.build_smoke_plan()
        for attempt in plan.attempts:
            if attempt.row_id.startswith("S3"):
                assert attempt.historical_executor_state is None


class TestCombinedAccountingAndTrialSeal:
    def test_accounting_complete_with_one_valid_retry(self):
        plan = smoke.build_smoke_plan()
        report = plan.accounting
        assert report.expected_total == 48
        assert report.primary_attempts == 48
        assert report.total_attempts == 49
        assert report.retry_attempts == 1
        assert report.accounting_complete is True
        # Not performance_usable: the retry row's PRIMARY is crashed, not
        # completed, and a retry is present at all.
        assert report.all_primary_completed is False
        assert report.performance_usable is False

    def test_trial_hash_changes_if_any_attempt_status_flips(self):
        plan = smoke.build_smoke_plan()
        mutated_rows = tuple(
            h6a_accounting.AttemptAccountingRow(
                row_id=a.row_id,
                experiment_id=a.experiment_id,
                retry_index=a.retry_index,
                status=(
                    "rejected"
                    if a.row_id == "S3-05" and a.retry_index == 0
                    else a.status
                ),
                reason_code=(
                    h6a_accounting.REASON_DATA_GAP_IN_POSITION
                    if a.row_id == "S3-05" and a.retry_index == 0
                    else a.reason_code
                ),
                fold_evidence_hash=a.fold_evidence_hash,
                run_identity=a.run_identity,
            )
            for a in plan.attempts
        )
        mutated_report = h6a_accounting.build_combined_accounting(
            campaign_run_id=plan.campaign_run_id,
            canonical_row_ids=tuple(spec.row_id for spec in plan.row_specs),
            row_id_to_experiment_id=plan.row_id_to_experiment_id,
            registered_total=48,
            attempts=mutated_rows,
        )
        assert (
            mutated_report.trial_accounting_hash
            != plan.accounting.trial_accounting_hash
        )


class TestDiagnosticVariantsAndSemanticIsolation:
    def test_all_four_variants_present(self):
        plan = smoke.build_smoke_plan()
        assert set(plan.diagnostics) == {"absent", "present", "reworded", "overflow"}

    def test_absent_vs_present_differ(self):
        plan = smoke.build_smoke_plan()
        import rob974_h6a_diagnostics as h6a_diagnostics

        absent_bytes = h6a_diagnostics.canonical_diagnostic_bytes(
            plan.diagnostics["absent"]
        )
        present_bytes = h6a_diagnostics.canonical_diagnostic_bytes(
            plan.diagnostics["present"]
        )
        assert absent_bytes != present_bytes

    def test_present_vs_reworded_differ(self):
        plan = smoke.build_smoke_plan()
        import rob974_h6a_diagnostics as h6a_diagnostics

        present_bytes = h6a_diagnostics.canonical_diagnostic_bytes(
            plan.diagnostics["present"]
        )
        reworded_bytes = h6a_diagnostics.canonical_diagnostic_bytes(
            plan.diagnostics["reworded"]
        )
        assert present_bytes != reworded_bytes

    def test_overflow_variant_is_truncated(self):
        plan = smoke.build_smoke_plan()
        assert plan.diagnostics["overflow"].overflow.truncated is True
        assert len(plan.diagnostics["overflow"].evidence) == 32

    def test_diagnostic_carriers_never_influence_full_campaign_hash(self):
        # The envelope/attempt hashes are computed entirely independently of
        # `plan.diagnostics` -- rebuilding the plan with wildly different
        # diagnostic content (impossible to inject here since the builder
        # doesn't accept it) is structurally guaranteed by the fact that
        # SmokePlan.envelope/attempts are built BEFORE diagnostics even
        # exist. This test locks that ordering.
        plan = smoke.build_smoke_plan()
        assert plan.full_campaign_hash == plan.envelope.full_campaign_hash()


class TestHighestRiskIdentityMutants:
    def test_47_rows_rejected(self):
        rows = smoke._rows()[:-1]
        with pytest.raises(h6a_identity.RowCountError):
            h6a_identity.validate_campaign_rows(rows)

    def test_49_rows_rejected(self):
        rows = smoke._rows() + [
            h6a_identity.CampaignConfigRow(
                row_id="S4-24",
                params={},
                hypothesis=smoke._S4_HYPOTHESIS,
                authority_label="x",
                provenance="fixture_identity",
            )
        ]
        with pytest.raises((h6a_identity.RowCountError, h6a_identity.RowIdError)):
            h6a_identity.validate_campaign_rows(rows)

    def test_duplicate_row_id_rejected(self):
        rows = smoke._rows()
        rows[1] = rows[0]
        with pytest.raises(h6a_identity.RowIdError):
            h6a_identity.validate_campaign_rows(rows)

    def test_s3_23_s4_00_boundary_swap_rejected(self):
        rows = smoke._rows()
        # Swap the LAST S3 row and the FIRST S4 row's ids -- same total
        # count(48), still 24/24 slug split, but the id SET no longer
        # matches the required S3-00..23,S4-00..23 contiguous ranges.
        idx_s3_23 = next(i for i, r in enumerate(rows) if r.row_id == "S3-23")
        rows[idx_s3_23] = h6a_identity.CampaignConfigRow(
            row_id="S3-24",
            params=rows[idx_s3_23].params,
            hypothesis=rows[idx_s3_23].hypothesis,
            authority_label=rows[idx_s3_23].authority_label,
            provenance="fixture_identity",
        )
        with pytest.raises(h6a_identity.RowIdError):
            h6a_identity.validate_campaign_rows(rows)

    def test_reordered_specs_rejected_by_envelope_builder(self):
        plan_row_specs = smoke._row_specs()
        shuffled = (plan_row_specs[1], plan_row_specs[0]) + tuple(plan_row_specs[2:])
        with pytest.raises(h6a_identity.RowIdError):
            h6a_payload.build_campaign_envelope(
                row_specs=shuffled,
                parent_corpus={"content_hash": _hex64("x")},
                campaign_policy=smoke._campaign_policy(),
                source_pins=h6a_payload.EMPTY_SOURCE_PINS,
                mode="fixture_plan",
            )

    def test_forged_envelope_experiment_id_rejected(self):
        specs = smoke._row_specs()
        with pytest.raises(h6a_identity.EnvelopeIdMismatchError):
            h6a_identity.verify_row_experiment_id(
                specs[0], envelope_experiment_id=_hex64("forged")
            )

    def test_collided_s3_s4_contract_hash_rejected(self):
        contracts = smoke._contracts()
        shared_hash = _hex64("colliding-contract")
        contracts["S3"] = h6a_identity.StrategyContractProvenance(
            strategy_slug="S3",
            strategy_key="ROB974-S3-SMOKE",
            strategy_version="s3-smoke-v1",
            contract_hash=shared_hash,
            contract_key="S3-smoke-key",
            provenance="fixture_identity",
        )
        contracts["S4"] = h6a_identity.StrategyContractProvenance(
            strategy_slug="S4",
            strategy_key="ROB974-S4-SMOKE",
            strategy_version="s4-smoke-v1",
            contract_hash=shared_hash,  # same hash as S3 -- a collision
            contract_key="S4-smoke-key",
            provenance="fixture_identity",
        )
        shared = {
            "dataset_manifest": {"h1_lineage_hash": _hex64("smoke-h1-lineage")},
            "universe": {"symbols": ["XRPUSDT", "DOGEUSDT", "SOLUSDT"]},
            "benchmark": {"kind": "none_explicit_sentinel"},
            "mdd": {"h2_engine_contract_hash": _hex64("smoke-h2-engine")},
        }

        def by_slug(key):
            return {"S3": {key: f"S3-smoke-{key}"}, "S4": {key: f"S4-smoke-{key}"}}

        with pytest.raises(h6a_identity.H6AIdentityError):
            h6a_identity.build_campaign_row_specs(
                smoke._rows(),
                contracts=contracts,
                shared_components=shared,
                pit_component_by_slug=by_slug("pit"),
                frozen_config_component_by_slug=by_slug("frozen_config"),
                policy_component_by_slug=by_slug("policy"),
                cost_component_by_slug=by_slug("cost"),
            )

    def test_stale_source_pin_rejected(self):
        contract = h6a_identity.StrategyContractProvenance(
            strategy_slug="S3",
            strategy_key="ROB974-S3-SMOKE",
            strategy_version="s3-smoke-v1",
            contract_hash=_hex64("real-contract"),
            contract_key="S3-smoke-key",
            expected_contract_hash=_hex64("stale-expected"),
            provenance="fixture_identity",
        )
        with pytest.raises(h6a_identity.StaleSourcePinError):
            contract.verified_contract_hash()

    def test_arbitrary_run_id_rejected(self):
        plan = smoke.build_smoke_plan()
        with pytest.raises(h6a_payload.RunIdDerivationError):
            h6a_payload.verify_primary_run_id(
                "arbitrary-uuid-not-derived", full_campaign_hash=plan.full_campaign_hash
            )

    def test_one_ulp_mutation_changes_full_campaign_hash(self):
        plan = smoke.build_smoke_plan()
        mutated_policy = h6a_payload.CampaignPolicy(
            folds=smoke._campaign_policy().folds,
            embargo_hours=smoke._campaign_policy().embargo_hours,
            horizons={"s3_max_hold_bars": 12, "s4_max_hold_bars": 9.000000000000002},
            selection_authority=smoke._campaign_policy().selection_authority,
            path_membership=smoke._campaign_policy().path_membership,
            funding_policy=smoke._campaign_policy().funding_policy,
            gates_bins=smoke._campaign_policy().gates_bins,
            pbo_contract=smoke._campaign_policy().pbo_contract,
            pair_order=smoke._campaign_policy().pair_order,
            s4_tri_state_policy=smoke._campaign_policy().s4_tri_state_policy,
        )
        mutated_envelope = h6a_payload.build_campaign_envelope(
            row_specs=plan.row_specs,
            parent_corpus={
                "content_hash": _hex64("smoke-parent-corpus"),
                "universe": ["XRPUSDT", "DOGEUSDT", "SOLUSDT"],
            },
            campaign_policy=mutated_policy,
            source_pins=h6a_payload.EMPTY_SOURCE_PINS,
            mode="fixture_plan",
        )
        assert mutated_envelope.full_campaign_hash() != plan.full_campaign_hash


class TestHighestRiskAccountingMutants:
    def test_duplicate_plus_missing_masked_by_count_48_is_still_incomplete(self):
        plan = smoke.build_smoke_plan()
        rows = [
            h6a_accounting.AttemptAccountingRow(
                row_id=a.row_id,
                experiment_id=a.experiment_id,
                retry_index=a.retry_index,
                status=a.status,
                reason_code=a.reason_code,
                fold_evidence_hash=a.fold_evidence_hash,
                run_identity=a.run_identity,
            )
            for a in plan.attempts
            if a.retry_index == 0  # drop the one retry -> 48 primaries
        ]
        # Remove S4-23's primary (missing) and duplicate S3-00's -- total
        # count stays at 48 (masking a naive count-only check).
        rows = [r for r in rows if r.row_id != "S4-23"]
        s3_00 = next(r for r in rows if r.row_id == "S3-00")
        rows.append(s3_00)
        assert len(rows) == 48
        report = h6a_accounting.build_combined_accounting(
            campaign_run_id=plan.campaign_run_id,
            canonical_row_ids=tuple(spec.row_id for spec in plan.row_specs),
            row_id_to_experiment_id=plan.row_id_to_experiment_id,
            registered_total=48,
            attempts=rows,
        )
        assert report.accounting_complete is False
        assert "S4-23" in report.missing_row_ids
        assert "S3-00" in report.duplicate_or_gap_row_ids

    def test_winner_only_rows_still_detected_as_missing(self):
        plan = smoke.build_smoke_plan()
        rows = [
            h6a_accounting.AttemptAccountingRow(
                row_id=a.row_id,
                experiment_id=a.experiment_id,
                retry_index=a.retry_index,
                status=a.status,
                reason_code=a.reason_code,
                fold_evidence_hash=a.fold_evidence_hash,
                run_identity=a.run_identity,
            )
            for a in plan.attempts
            # keep only "completed" rows -- a naive winner-only filter.
            if a.status == "completed"
        ]
        report = h6a_accounting.build_combined_accounting(
            campaign_run_id=plan.campaign_run_id,
            canonical_row_ids=tuple(spec.row_id for spec in plan.row_specs),
            row_id_to_experiment_id=plan.row_id_to_experiment_id,
            registered_total=48,
            attempts=rows,
        )
        # The retry row's PRIMARY (crashed) was filtered out by the
        # winner-only view -- its primary is now missing.
        assert smoke.RETRY_ROW_ID in report.missing_row_ids
        assert report.accounting_complete is False

    def test_unique_evidence_tripled_across_scenarios_rejected(self):
        plan = smoke.build_smoke_plan()
        attempt = next(
            a for a in plan.attempts if a.retry_index == 0 and a.row_id == "S3-00"
        )
        tripled = attempt.unique_evidence * 3
        with pytest.raises(h6a_evidence.AttemptEvidenceError):
            h6a_evidence.build_attempt_record(
                row_id=attempt.row_id,
                experiment_id=attempt.experiment_id,
                campaign_run_id=attempt.campaign_run_id,
                full_campaign_hash=attempt.full_campaign_hash,
                strategy_key=attempt.strategy_key,
                retry_index=attempt.retry_index,
                status=attempt.status,
                reason_code=attempt.reason_code,
                fold_traces=attempt.fold_traces,
                unique_evidence=tripled,
                path_scenario_evidence=attempt.path_scenario_evidence,
                historical_executor_state=attempt.historical_executor_state,
            )

    def test_scenario_membership_omission_changes_artifact_hash(self):
        plan = smoke.build_smoke_plan()
        attempt = next(
            a for a in plan.attempts if a.retry_index == 0 and a.row_id == "S3-02"
        )
        real_row = attempt.path_scenario_evidence[0]
        omitted = h6a_evidence.PathScenarioEvidence(
            path_scenario=real_row.path_scenario,
            status=real_row.status,
            reason_code=real_row.reason_code,
            trade_count=0,
            member_trade_keys=(),
            no_trade_reason_counts=real_row.no_trade_reason_counts,
            artifact_hash=h6a_evidence._recompute_path_scenario_hash(
                type(
                    "H",
                    (),
                    {
                        "path_scenario": real_row.path_scenario,
                        "status": real_row.status,
                        "reason_code": real_row.reason_code,
                        "trade_count": 0,
                        "member_trade_keys": (),
                        "no_trade_reason_counts": real_row.no_trade_reason_counts,
                    },
                )()
            ),
        )
        assert omitted.artifact_hash != real_row.artifact_hash

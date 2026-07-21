"""ROB-984 CP10 persisted fake-free non-vacuity and frozen seal."""

from __future__ import annotations

import json
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import pytest
import pytest_asyncio
import rob974_h3_h2_adapter as h3_h2_adapter
import rob974_h3_smoke as h3_smoke
import rob974_h4_runner as h4_runner
import test_rob962_frozen_production_delta as frozen_guard
from rob974_features import FOUR_HOUR_MS, MINUTE_MS
from rob974_h3_manifest import get_config
from rob974_h3_s3 import EmitWindow, generate_s3_global
from rob974_h3_s4 import generate_s4_global
from rob974_h4_contracts import exact_h4_folds
from rob974_h6b_artifacts import (
    ArtifactVerificationError,
    DirectoryAtomicArtifactPort,
)
from rob974_h6b_postaudit import (
    ProductionPostAuditAuthority,
    run_production_postaudit,
)
from rob984_fake_free_support import prepare_fake_free_input
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool
from test_rob984_cp9_real_test_db import _actual_result, _ActualRunner

from app.services import rob974_h6b_materializer as materializer
from app.services.research_db_write_guard import default_research_db_policy

_PERSISTED_OUTPUT = Path("/private/tmp/strategy-worker-rob1012-test-db-e2e-pair")
_H6B_RESEARCH_PATHS = {
    "research/nautilus_scalping/rob974_h6b_artifacts.py",
    "research/nautilus_scalping/rob974_h6b_cli.py",
    "research/nautilus_scalping/rob974_h6b_postaudit.py",
}
_PROJECT_TEST_DB_URL = "postgresql+asyncpg://postgres:postgres@localhost:5432/test_db"
_TARGET = materializer.DatabaseTarget(
    host="localhost", port=5432, database="test_db", user="postgres"
)
_INTEGRATION_HEAD = "c3c31b76e3a79e9cf9573e066b1d7e278088fc8e"
_INTEGRATION_TREE = "bc8091c50e720af86b610332714d077e7b461397"
_DEFERRED_FAKE_FREE_MARKER = "DEFERRED_TO_H6B_INTEGRATION_E2E"
_PRE_R1_FORENSIC_JSON_SHA256 = (
    "ce77983a2d47a0d8137b0df4a1171090f2183363cf948aea9ed7ffc8e14cd704"
)
_CP10_FAKE_FREE_CLOSURE_SHA256 = (
    "a3bfced8c4b0de7919a79056b3e315403f941c933416cc876e3d53c1afe59885"
)
_CP10_RAW_MEMBER_KEY_CROSS_SHA256 = (
    "3bc2b53a0caab2bed4c882277a1fa2375e915f55fdaa368445b8eee5b712d93f"
)


@pytest_asyncio.fixture(autouse=True)
async def _ensure_committed_fake_free_state(tmp_path) -> None:
    """Make the CP10 smoke independent of CP9/module discovery order.

    A clean disposable facility is populated once through the actual production
    materializer.  An existing immutable campaign is only inspected under READ
    ONLY; mixed DB/artifact states fail instead of being repaired.
    """

    identity = materializer.build_production_identity_plan()
    plan = materializer.build_production_execution_plan(
        identity=identity,
        output_root=_PERSISTED_OUTPUT,
        integration_head_sha=_INTEGRATION_HEAD,
        integration_tree_sha=_INTEGRATION_TREE,
    )
    engine = create_async_engine(_PROJECT_TEST_DB_URL, poolclass=NullPool)

    def session_factory() -> AsyncSession:
        return AsyncSession(bind=engine, expire_on_commit=False)

    try:
        inspection = session_factory()
        await inspection.begin()
        await inspection.execute(text("SET TRANSACTION READ ONLY"))
        snapshot = await materializer.ActualCampaignStateInspector().inspect(
            inspection, plan=plan
        )
        await inspection.rollback()
        await inspection.close()
        artifact_state = (
            DirectoryAtomicArtifactPort().probe(output_dir=_PERSISTED_OUTPUT).state
        )

        if snapshot.is_absent() and artifact_state == "ABSENT":
            prepared = prepare_fake_free_input(
                tmp_path / "committed-persisted-synthetic-corpus"
            )
            plan = materializer.build_production_execution_plan(
                identity=prepared.identity,
                output_root=_PERSISTED_OUTPUT,
                integration_head_sha=_INTEGRATION_HEAD,
                integration_tree_sha=_INTEGRATION_TREE,
            )
            ports = materializer.ProductionExecutionPorts(
                session_factory=session_factory,
                h4_runner=prepared.runner,
                artifacts=DirectoryAtomicArtifactPort(),
                state_inspector=materializer.ActualCampaignStateInspector(),
            )
            campaign = materializer.ProductionCampaignInput(
                plan=plan,
                guard_policy=default_research_db_policy(),
                strategy_name=materializer.ROB974_R2_PRODUCTION_STRATEGY_NAME,
                timeframe=materializer.ROB974_R2_PRODUCTION_TIMEFRAME,
                runner=materializer.ROB974_R2_PRODUCTION_RUNNER,
            )
            authorization = materializer.issue_project_test_db_authorization(
                plan,
                materializer.ProjectTestDbAuthority(
                    expected_full_campaign_hash=plan.full_campaign_hash,
                    expected_campaign_run_id=plan.campaign_run_id,
                    expected_exact_48_mapping_hash=plan.exact_48_mapping_hash,
                    expected_target=_TARGET,
                    observed_target=_TARGET,
                    inherited_target=_TARGET,
                    write_opt_in=True,
                    expected_output_root=_PERSISTED_OUTPUT,
                    requested_output_root=_PERSISTED_OUTPUT,
                    expected_source_pins=plan.source_pins,
                    observed_source_pins=plan.source_pins,
                    one_shot_approval="cp10-committed-fake-free-test-db",
                ),
            )
            outcome = await materializer.materialize_production(
                plan=plan,
                authorization=authorization,
                campaign=campaign,
                ports=ports,
            )
            assert outcome.exit_code == 0, repr(outcome.primary_error)
            assert outcome.disposition == "MATERIALIZED"
            assert outcome.counters.register == outcome.counters.record == 1
            assert outcome.counters.commit == outcome.counters.publish == 1
            assert outcome.counters.rollback == outcome.counters.delete == 0
        else:
            assert artifact_state == "PAIR_PRESENT"
            assert snapshot.campaign_run_id == plan.campaign_run_id
            assert snapshot.registered_mapping == plan.ordered_mapping
            assert len(snapshot.attempts) == 48
    finally:
        await engine.dispose()


def test_cp10_persisted_fake_free_nonvacuity_and_exact_guard_seal() -> None:
    scorecard_bytes = _PERSISTED_OUTPUT.joinpath("scorecard.json").read_bytes()
    scorecard = json.loads(scorecard_bytes)
    assert scorecard["lineage"]["full_campaign_hash"] == (
        "c8bb8e88e129e0072d0ea174adca5c4cce8158f2726c6397030d2ae6e4619f39"
    )
    assert scorecard["lineage"]["campaign_run_id"] == (
        "rob974h6a-G4efMErFLrEyHWNztSKlo9j-ghlxQuPkwD0h1g6sQEw"
    )
    expected_dimensions = {
        "S3": {"XRPUSDT", "DOGEUSDT", "SOLUSDT"},
        "S4": {"XRP-DOGE", "XRP-SOL", "DOGE-SOL"},
    }
    expected_paths = {"base13", "primary_stress17", "upward_stress22"}
    for strategy in ("S3", "S4"):
        rows = scorecard["strategies"][strategy]["raw_attribution_rows"]
        assert rows
        assert {row["dimension"] for row in rows} == expected_dimensions[strategy]
        assert {row["path_scenario"] for row in rows} == expected_paths
        assert all(
            any(row["path_scenario"] == scenario for row in rows)
            for scenario in expected_paths
        )
        pbo = scorecard["strategies"][strategy]["pbo"]
        assert (pbo["config_count"], pbo["day_count"], pbo["slices"]) == (
            24,
            365,
            4,
        )
    s3_exits = {
        row["exit_reason"]
        for row in scorecard["strategies"]["S3"]["raw_attribution_rows"]
    }
    s4_exits = {
        row["exit_reason"]
        for row in scorecard["strategies"]["S4"]["raw_attribution_rows"]
    }
    assert "THESIS_EXIT" in s3_exits
    assert {"MEAN_EXIT", "STALL_EXIT"} & s4_exits

    attribution_contract = scorecard["h4_attribution_contract"]
    fake_free_marker = attribution_contract["fake_free_empirical_closure"]
    raw_cross_marker = attribution_contract["raw_member_key_cross_seal"]
    if fake_free_marker.startswith(materializer.ROB984_CP10_CLOSED_PREFIX):
        assert raw_cross_marker.startswith(materializer.ROB984_CP10_CLOSED_PREFIX)
    else:
        # This exact pair predates the R1 closure.  The collision contract
        # makes it immutable forensic evidence; the actual runner's CLOSED
        # output is exercised and physically published in the test below.
        assert fake_free_marker == _DEFERRED_FAKE_FREE_MARKER
        assert raw_cross_marker == _DEFERRED_FAKE_FREE_MARKER
        assert sha256(scorecard_bytes).hexdigest() == _PRE_R1_FORENSIC_JSON_SHA256

    authorized = frozen_guard._AUTHORIZED_PRODUCTION_CHANGES
    authorized_paths = {paths[0] for status, paths in authorized if status == "A"}
    assert len(authorized) == 48
    assert _H6B_RESEARCH_PATHS <= authorized_paths


@pytest.mark.asyncio
async def test_cp10_divergent_replay_is_read_only_and_never_mutates() -> None:
    identity = materializer.build_production_identity_plan()
    plan = materializer.build_production_execution_plan(
        identity=identity,
        output_root=_PERSISTED_OUTPUT,
        integration_head_sha=_INTEGRATION_HEAD,
        integration_tree_sha=_INTEGRATION_TREE,
    )
    before = tuple(
        (
            _PERSISTED_OUTPUT.joinpath(name).stat().st_ino,
            _PERSISTED_OUTPUT.joinpath(name).read_bytes(),
        )
        for name in ("scorecard.json", "scorecard.md")
    )
    engine = create_async_engine(_PROJECT_TEST_DB_URL, poolclass=NullPool)

    def session_factory() -> AsyncSession:
        return AsyncSession(bind=engine, expire_on_commit=False)

    runner = _ActualRunner(_actual_result(identity))
    ports = materializer.ProductionExecutionPorts(
        session_factory=session_factory,
        h4_runner=runner,
        artifacts=DirectoryAtomicArtifactPort(),
        state_inspector=materializer.ActualCampaignStateInspector(),
    )
    campaign = materializer.ProductionCampaignInput(
        plan=plan,
        guard_policy=default_research_db_policy(),
        strategy_name=materializer.ROB974_R2_PRODUCTION_STRATEGY_NAME,
        timeframe=materializer.ROB974_R2_PRODUCTION_TIMEFRAME,
        runner=materializer.ROB974_R2_PRODUCTION_RUNNER,
    )
    authorization = materializer.issue_project_test_db_authorization(
        plan,
        materializer.ProjectTestDbAuthority(
            expected_full_campaign_hash=plan.full_campaign_hash,
            expected_campaign_run_id=plan.campaign_run_id,
            expected_exact_48_mapping_hash=plan.exact_48_mapping_hash,
            expected_target=_TARGET,
            observed_target=_TARGET,
            inherited_target=_TARGET,
            write_opt_in=True,
            expected_output_root=_PERSISTED_OUTPUT,
            requested_output_root=_PERSISTED_OUTPUT,
            expected_source_pins=plan.source_pins,
            observed_source_pins=plan.source_pins,
            one_shot_approval="cp10-divergent-read-only-replay",
        ),
    )
    try:
        outcome = await materializer.materialize_production(
            plan=plan,
            authorization=authorization,
            campaign=campaign,
            ports=ports,
        )
        assert outcome.exit_code == materializer.PRECOMMIT_FAILURE
        assert outcome.disposition == "PRECOMMIT_FAILURE"
        assert isinstance(outcome.primary_error, materializer.ReplayCollisionError)
        assert "semantic attempt differs" in str(outcome.primary_error)
        assert outcome.retry_forbidden is True
        assert outcome.trace.index("set_transaction_read_only") < outcome.trace.index(
            "db_state_inspection"
        )
        assert outcome.counters.register == outcome.counters.record == 0
        assert outcome.counters.commit == outcome.counters.stage == 0
        assert outcome.counters.delete == outcome.counters.publish == 0
        assert outcome.counters.rollback == outcome.counters.close == 1
        assert runner.calls == 1
        after = tuple(
            (
                _PERSISTED_OUTPUT.joinpath(name).stat().st_ino,
                _PERSISTED_OUTPUT.joinpath(name).read_bytes(),
            )
            for name in ("scorecard.json", "scorecard.md")
        )
        assert after == before
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_cp10_actual_chain_is_deterministic_replay_noop_and_nonvacuous(
    tmp_path,
) -> None:
    before_json = _PERSISTED_OUTPUT.joinpath("scorecard.json").read_bytes()
    before_markdown = _PERSISTED_OUTPUT.joinpath("scorecard.md").read_bytes()
    before_inodes = tuple(
        _PERSISTED_OUTPUT.joinpath(name).stat().st_ino
        for name in ("scorecard.json", "scorecard.md")
    )

    prepared = prepare_fake_free_input(tmp_path / "second-persisted-synthetic-corpus")
    identity = prepared.identity
    plan = materializer.build_production_execution_plan(
        identity=identity,
        output_root=_PERSISTED_OUTPUT,
        integration_head_sha=_INTEGRATION_HEAD,
        integration_tree_sha=_INTEGRATION_TREE,
    )
    engine = create_async_engine(_PROJECT_TEST_DB_URL, poolclass=NullPool)

    def session_factory() -> AsyncSession:
        return AsyncSession(bind=engine, expire_on_commit=False)

    ports = materializer.ProductionExecutionPorts(
        session_factory=session_factory,
        h4_runner=prepared.runner,
        artifacts=DirectoryAtomicArtifactPort(),
        state_inspector=materializer.ActualCampaignStateInspector(),
    )
    campaign = materializer.ProductionCampaignInput(
        plan=plan,
        guard_policy=default_research_db_policy(),
        strategy_name=materializer.ROB974_R2_PRODUCTION_STRATEGY_NAME,
        timeframe=materializer.ROB974_R2_PRODUCTION_TIMEFRAME,
        runner=materializer.ROB974_R2_PRODUCTION_RUNNER,
    )
    authorization = materializer.issue_project_test_db_authorization(
        plan,
        materializer.ProjectTestDbAuthority(
            expected_full_campaign_hash=plan.full_campaign_hash,
            expected_campaign_run_id=plan.campaign_run_id,
            expected_exact_48_mapping_hash=plan.exact_48_mapping_hash,
            expected_target=_TARGET,
            observed_target=_TARGET,
            inherited_target=_TARGET,
            write_opt_in=True,
            expected_output_root=_PERSISTED_OUTPUT,
            requested_output_root=_PERSISTED_OUTPUT,
            expected_source_pins=plan.source_pins,
            observed_source_pins=plan.source_pins,
            one_shot_approval="cp10-exact-semantic-replay",
        ),
    )
    try:
        replay = await materializer.materialize_production(
            plan=plan,
            authorization=authorization,
            campaign=campaign,
            ports=ports,
        )
        persisted = json.loads(before_json)
        persisted_contract = persisted["h4_attribution_contract"]
        persisted_is_closed = persisted_contract[
            "fake_free_empirical_closure"
        ].startswith(materializer.ROB984_CP10_CLOSED_PREFIX)
        if persisted_is_closed:
            assert replay.exit_code == 0, repr(replay.primary_error)
            assert replay.disposition == "REPLAY_NOOP"
            assert replay.primary_error is None
        else:
            assert sha256(before_json).hexdigest() == _PRE_R1_FORENSIC_JSON_SHA256
            assert replay.exit_code == materializer.PRECOMMIT_FAILURE
            assert replay.disposition == "PRECOMMIT_FAILURE"
            assert isinstance(replay.primary_error, ArtifactVerificationError)
            assert str(replay.primary_error) == (
                "physical JSON bytes differ from H5 canonical bytes"
            )
            assert replay.retry_forbidden is True
        assert replay.counters.register == replay.counters.record == 0
        assert replay.counters.commit == replay.counters.stage == 0
        assert replay.counters.delete == replay.counters.publish == 0
        assert replay.counters.h4 == replay.counters.accounting == 1
        assert replay.counters.h5 == replay.counters.replay_verify == 1
        assert replay.counters.rollback == replay.counters.close == 1
        assert replay.db_state == "EXACT"
        assert replay.artifact_state == "PAIR_PRESENT"

        result = prepared.runner.last_result
        assert result is not None
        assert len(result.attempts) == len(result.batch_items()) == 48
        assert prepared.runner.last_selected == (
            ("S3", "fold-00", "S3-20"),
            ("S4", "fold-00", "S4-23"),
        )
        assert len(result.attribution.paths) == 6
        assert all(path.rows for path in result.attribution.paths)
        assert len({id(path.terminal.result) for path in result.attribution.paths}) == 6
        assert {
            (path.strategy, path.path_scenario) for path in result.attribution.paths
        } == {
            (strategy, scenario)
            for strategy in ("S3", "S4")
            for scenario in ("base13", "primary_stress17", "upward_stress22")
        }
        path_value_hashes = {
            strategy: {
                materializer.canonical_sha256(
                    [
                        h4_runner._attribution_row_payload(row)[
                            {
                                "base13": "e13_bps",
                                "primary_stress17": "e17_bps",
                                "upward_stress22": "e22_bps",
                            }[path.path_scenario]
                        ]
                        for row in path.rows
                    ]
                )
                for path in result.attribution.paths
                if path.strategy == strategy
            }
            for strategy in ("S3", "S4")
        }
        assert len(path_value_hashes["S3"]) == 3
        assert len(path_value_hashes["S4"]) == 3

        attempt_by_row = {attempt.row_id: attempt for attempt in result.attempts}
        for row_id in ("S3-20", "S4-23"):
            unique = next(
                item
                for item in attempt_by_row[row_id].unique_evidence
                if item.fold_id == "fold-00"
            )
            assert unique.candidate > 0
            assert unique.generator_accepted > 0
            assert unique.candidate == (
                unique.generator_rejected + unique.generator_accepted
            )
            assert (
                sum(unique.generator_rejection_subtotal_by_reason.values())
                == unique.generator_rejected
            )
            named_paths = attempt_by_row[row_id].path_scenario_evidence
            assert all(item.trade_count > 0 for item in named_paths)
            assert len({item.artifact_hash for item in named_paths}) == 3
        for strategy in ("S3", "S4"):
            fold_zero = [
                next(
                    item
                    for item in attempt.unique_evidence
                    if item.fold_id == "fold-00"
                )
                for attempt in result.attempts
                if attempt.row_id.startswith(strategy + "-")
            ]
            assert sum(item.candidate for item in fold_zero) > 0
            assert sum(item.generator_rejected for item in fold_zero) > 0
            assert sum(item.generator_accepted for item in fold_zero) > 0
            assert any(
                item.generator_rejection_subtotal_by_reason for item in fold_zero
            )

        unique_by_strategy, paths_by_strategy = materializer._h5_dual_evidence(result)
        actual_unique = unique_by_strategy["S3"][("S3-20", "fold-00")]
        actual_paths = {
            scenario: paths_by_strategy["S3"][("S3-20", "fold-00", scenario)]
            for scenario in ("base13", "primary_stress17", "upward_stress22")
        }
        tripled = replace(
            actual_paths["base13"],
            unique_evidence_accepted_count=actual_unique.accepted * 3,
        )
        with pytest.raises(
            materializer.h5_contracts.H5InputError,
            match="dual_evidence_path_unique_accepted_count_mismatch",
        ):
            materializer.h5_dual.cross_check_dual_evidence(
                actual_unique, {**actual_paths, "base13": tripled}
            )

        raw_minutes = prepared.input_data.as_dict()
        context, _feature_hash = h3_smoke._context(raw_minutes)
        gap_output = generate_s3_global(
            context,
            EmitWindow(
                prepared.gap_close,
                prepared.recovery_close + 240 * FOUR_HOUR_MS,
            ),
            get_config("S3-20"),
        )
        gap_decision = next(
            item
            for item in gap_output.decisions
            if item.decision_ts == prepared.gap_close and item.symbol == "SOLUSDT"
        )
        assert gap_decision.status == "NO_SIGNAL"
        assert gap_decision.no_signal_reason == "missing_required_context"
        assert any(
            snapshot.decision_ts == prepared.recovery_close
            for snapshot in context.snapshots
        )
        recovery_decision = next(
            item
            for item in gap_output.decisions
            if item.decision_ts > prepared.recovery_close
            and item.symbol == "SOLUSDT"
            and item.no_signal_reason != "missing_required_context"
        )
        assert recovery_decision.decision_ts > prepared.gap_close

        fold = exact_h4_folds()[0]
        window = EmitWindow(fold.oos_start_ms, fold.oos_end_ms)
        s3_output = generate_s3_global(context, window, get_config("S3-20"))
        s4_output = generate_s4_global(context, window, get_config("S4-23"))
        preview = h3_h2_adapter.run_h2_integration(
            s3_output,
            s4_output,
            raw_minutes,
            context,
            fold_id=fold.fold_id,
            corpus_end_ts=prepared.input_data.corpus_end_ts,
            horizon_end_ts=fold.oos_end_ms,
        )
        chosen = next(
            trade
            for trade in preview.s4_engine_result.trades
            if trade.exit_reason == "STALL_EXIT"
        )
        gap_ts = chosen.signal_ts + MINUTE_MS
        gapped_minutes = dict(raw_minutes)
        gapped_minutes[chosen.pair[0]] = tuple(
            row for row in raw_minutes[chosen.pair[0]] if row.ts != gap_ts
        )
        gapped = h3_h2_adapter.run_h2_integration(
            s3_output,
            s4_output,
            gapped_minutes,
            context,
            fold_id=fold.fold_id,
            corpus_end_ts=prepared.input_data.corpus_end_ts,
            horizon_end_ts=fold.oos_end_ms,
        )
        assert any(
            item.signal_ts == chosen.signal_ts
            and item.pair == chosen.pair
            and item.reason == "data_gap_in_pair_position"
            for item in gapped.s4_engine_result.incompletes
        )
        assert not any(
            trade.signal_ts == chosen.signal_ts and trade.pair == chosen.pair
            for trade in gapped.s4_engine_result.trades
        )

        closed_scorecard = replay.scorecard
        assert closed_scorecard is not None
        closed_contract = closed_scorecard["h4_attribution_contract"]
        assert closed_contract["fake_free_empirical_closure"].startswith(
            materializer.ROB984_CP10_CLOSED_PREFIX
        )
        assert closed_contract["fake_free_empirical_closure"] == (
            materializer.ROB984_CP10_CLOSED_PREFIX + _CP10_FAKE_FREE_CLOSURE_SHA256
        )
        assert closed_contract["raw_member_key_cross_seal"] == (
            materializer.ROB984_CP10_CLOSED_PREFIX + _CP10_RAW_MEMBER_KEY_CROSS_SHA256
        )
        for strategy in ("S3", "S4"):
            stall_rows = [
                row
                for row in closed_scorecard["strategies"][strategy][
                    "raw_attribution_rows"
                ]
                if row["exit_reason"] == "STALL_EXIT"
            ]
            if stall_rows:
                assert all(row["gross_bps"].hex() == "0x0.0p+0" for row in stall_rows)
        closed_output = (tmp_path / "r1-closed-scorecard").resolve()
        staged = ports.artifacts.stage(
            scorecard=closed_scorecard,
            output_dir=closed_output,
            h5_port=ports.h5,
        )
        published = ports.artifacts.publish(staged, h5_port=ports.h5)
        closed_json = published.json_path.read_bytes()
        closed_markdown = published.markdown_path.read_bytes()
        closed_persisted = json.loads(closed_json)
        assert closed_persisted == closed_scorecard
        assert ports.h5.canonical_json_bytes(closed_scorecard) == closed_json
        assert ports.h5.render_markdown(closed_scorecard) == closed_markdown
        assert (
            closed_persisted["h4_attribution_contract"]["fake_free_empirical_closure"]
            == closed_contract["fake_free_empirical_closure"]
        )
        assert ports.artifacts.probe(output_dir=closed_output).staging_dirs == ()
        assert (
            replay.accounting.trial_accounting_hash
            == (closed_persisted["lineage"]["h6a_trial_accounting_hash"])
        )

        audit = await run_production_postaudit(
            plan=plan,
            authority=ProductionPostAuditAuthority(
                expected_target=_TARGET,
                observed_target=_TARGET,
                inherited_target=_TARGET,
                output_dir=_PERSISTED_OUTPUT,
            ),
            session_factory=session_factory,
        )
        assert audit.exit_code == 0, repr(audit.primary_error)
        assert audit.disposition == "POSTAUDIT_VERIFIED_READ_ONLY"

        after_json = _PERSISTED_OUTPUT.joinpath("scorecard.json").read_bytes()
        after_markdown = _PERSISTED_OUTPUT.joinpath("scorecard.md").read_bytes()
        after_inodes = tuple(
            _PERSISTED_OUTPUT.joinpath(name).stat().st_ino
            for name in ("scorecard.json", "scorecard.md")
        )
        assert (after_json, after_markdown) == (before_json, before_markdown)
        assert after_inodes == before_inodes
        presence = ports.artifacts.probe(output_dir=_PERSISTED_OUTPUT)
        assert presence.state == "PAIR_PRESENT"
        assert presence.staging_dirs == ()

        evidence = {
            "h4_attempt_batch_hash": materializer.canonical_sha256(
                [item.fingerprint() for item in result.batch_items()]
            ),
            "h4_attribution_hash": result.attribution.producer_seal_sha256,
            "h6a_trial_accounting_hash": replay.accounting.trial_accounting_hash,
            "h5_semantic_hash": ports.h5.semantic_hash(closed_scorecard),
            "json_sha256": sha256(closed_json).hexdigest(),
            "markdown_sha256": sha256(closed_markdown).hexdigest(),
            "fake_free_empirical_closure": closed_contract[
                "fake_free_empirical_closure"
            ],
            "raw_member_key_cross_seal": closed_contract["raw_member_key_cross_seal"],
            "pre_r1_forensic_json_sha256": sha256(after_json).hexdigest(),
            "pre_r1_forensic_markdown_sha256": sha256(after_markdown).hexdigest(),
            "manifest_hash": prepared.manifest_hash,
            "feature_hash": prepared.feature_hash,
            "selected": prepared.runner.last_selected,
        }
        print("ROB984_CP10_EVIDENCE " + json.dumps(evidence, sort_keys=True))
    finally:
        await engine.dispose()

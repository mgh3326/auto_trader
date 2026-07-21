"""ROB-984 CP8 actual H4 -> H6-A -> H5 composition coverage."""

from __future__ import annotations

import json
import math
from dataclasses import replace
from pathlib import Path

import pytest
import test_rob1001_h45_attribution as h4fx
from rob945_pbo_grid import FROZEN_DAY_KEYS
from rob974_h4_pbo import compute_h4_full_window_pbo
from rob974_h4_runner import (
    H4AttributionError,
    bind_s3_attribution_path,
    bind_s4_attribution_path,
    build_actual_attribution_envelope,
    build_tercile_authority,
)
from rob974_h6a_evidence import (
    FOLD_COUNT,
    PATH_SCENARIOS,
    FoldSelectionTrace,
    HistoricalExecutorState,
    PathScenarioEvidence,
    UniqueGeneratorEvidence,
    _recompute_path_scenario_hash,
    _recompute_unique_evidence_hash,
    build_attempt_record,
)
from rob974_h6b_artifacts import (
    DirectoryAtomicArtifactPort,
    read_persisted_scorecard_pair,
)
from rob974_h6b_cli import build_production_identity_plan

from app.services import rob974_h6b_materializer as materializer
from app.services.research_canonical_hash import compute_identity_hashes
from app.services.research_db_write_guard import ResearchDbPolicy, ResearchDbTarget
from tests.services.research.test_rob984_cp3_transaction_coordinator import (
    SessionSpy,
    _RegisteredRow,
    _StoredRow,
)

_INTEGRATION_HEAD = "c3c31b76e3a79e9cf9573e066b1d7e278088fc8e"
_INTEGRATION_TREE = "bc8091c50e720af86b610332714d077e7b461397"


class _Holder:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def _unique(row_id: str) -> tuple[UniqueGeneratorEvidence, ...]:
    result = []
    for index in range(FOLD_COUNT):
        kwargs = {
            "fold_id": f"fold-{index:02d}",
            "candidate_identity_hash": materializer.canonical_sha256(
                {"row_id": row_id, "fold": index, "surface": "candidate"}
            ),
            "evaluated_decision_units": 3,
            "no_signal": 1,
            "candidate": 2,
            "generator_rejected": 1,
            "generator_accepted": 1,
            "generator_rejection_subtotal_by_reason": {
                "simultaneous_candidate_arbitration_loser": 1
            },
        }
        result.append(
            UniqueGeneratorEvidence(
                **kwargs,
                content_hash=_recompute_unique_evidence_hash(_Holder(**kwargs)),
            )
        )
    return tuple(result)


def _trace(
    unique: tuple[UniqueGeneratorEvidence, ...], *, selected_fold: str | None
) -> tuple[FoldSelectionTrace, ...]:
    result = []
    for index, evidence in enumerate(unique):
        selected = evidence.fold_id == selected_fold
        result.append(
            FoldSelectionTrace(
                fold_id=evidence.fold_id,
                fold_index=index,
                selected=selected,
                eligible_symbols_or_pairs=("XRPUSDT",),
                excluded_symbols_or_pairs=(),
                accepted_input_hash=evidence.content_hash if selected else None,
                rejection_reason=None if selected else "lost_train_selection",
                no_trade_reason_counts={},
            )
        )
    return tuple(result)


def _path_evidence(
    *, path_scenario: str, member_keys: tuple[str, ...], selected: bool
) -> PathScenarioEvidence:
    kwargs = {
        "path_scenario": path_scenario,
        "status": "completed" if selected else "never_selected",
        "reason_code": None,
        "trade_count": len(member_keys),
        "member_trade_keys": member_keys,
        "no_trade_reason_counts": {},
    }
    return PathScenarioEvidence(
        **kwargs,
        artifact_hash=_recompute_path_scenario_hash(_Holder(**kwargs)),
    )


def _actual_attribution(identity):
    authority = build_tercile_authority(
        fold_id="fold-00",
        train_start_ms=0,
        train_end_ms=h4fx._SIGNAL_TS,
        snapshots=(
            h4fx._snapshot(0, m=9.0, M=0.0),
            h4fx._snapshot(60_000, m=9.0, M=0.01),
            h4fx._snapshot(120_000, m=9.0, M=0.03),
        ),
    )
    s3_candidate = h4fx._s3_candidate()
    s4_candidate = h4fx._s4_candidate()
    s3_terminal = h4fx._s3_terminal(s3_candidate)
    s4_terminal = h4fx._s4_terminal(s4_candidate)
    paths = []
    for scenario in PATH_SCENARIOS:
        paths.append(
            bind_s3_attribution_path(
                row_spec=identity._h4_plan.row_specs[0],
                fold_id="fold-00",
                path_scenario=scenario,
                candidates=(s3_candidate,),
                terminal=s3_terminal,
                corpus_end_ts=h4fx._CORPUS_END_TS,
                horizon_end_ts=None,
                decision_snapshots=(h4fx._snapshot(h4fx._SIGNAL_TS, m=-0.4, M=0.02),),
                tercile_authority=authority,
            )
        )
        paths.append(
            bind_s4_attribution_path(
                row_spec=identity._h4_plan.row_specs[24],
                fold_id="fold-00",
                path_scenario=scenario,
                candidates=(s4_candidate,),
                terminal=s4_terminal,
                corpus_end_ts=h4fx._CORPUS_END_TS,
                horizon_end_ts=None,
                decision_snapshots=(h4fx._snapshot(h4fx._SIGNAL_TS, m=-0.4, M=0.04),),
            )
        )
    return build_actual_attribution_envelope(
        plan=identity._h4_plan,
        paths=tuple(paths),
        tercile_authorities=(authority,),
    )


def _actual_attempts(identity, attribution):
    raw_keys = {}
    for path in attribution.paths:
        raw_keys[(path.lineage.row_id, path.path_scenario)] = tuple(
            materializer.build_h4_member_trade_key(row) for row in path.rows
        )
    attempts = []
    for spec in identity._h4_plan.row_specs:
        selected = spec.row_id in ("S3-00", "S4-00")
        unique = _unique(spec.row_id)
        paths = tuple(
            _path_evidence(
                path_scenario=scenario,
                member_keys=raw_keys.get((spec.row_id, scenario), ()),
                selected=selected,
            )
            for scenario in PATH_SCENARIOS
        )
        attempts.append(
            build_attempt_record(
                row_id=spec.row_id,
                experiment_id=spec.experiment_id,
                campaign_run_id=identity.campaign_run_id,
                full_campaign_hash=identity.full_campaign_hash,
                strategy_key=spec.strategy_key,
                retry_index=0,
                status="completed",
                reason_code=None,
                fold_traces=_trace(
                    unique, selected_fold="fold-00" if selected else None
                ),
                unique_evidence=unique,
                path_scenario_evidence=paths,
                historical_executor_state=(
                    HistoricalExecutorState() if spec.row_id.startswith("S4-") else None
                ),
            )
        )
    return tuple(attempts)


def _pbo(strategy: str):
    grid = {
        f"{strategy}-{config:02d}": {
            day: float(((index + config * 7) % 29) - 14) + config * 0.01
            for index, day in enumerate(FROZEN_DAY_KEYS)
        }
        for config in range(24)
    }
    return compute_h4_full_window_pbo(strategy=strategy, daily_gross_bps_by_config=grid)


def _composition(tmp_path: Path):
    identity = build_production_identity_plan()
    plan = materializer.build_production_execution_plan(
        identity=identity,
        output_root=(tmp_path / "scorecard").resolve(),
        integration_head_sha=_INTEGRATION_HEAD,
        integration_tree_sha=_INTEGRATION_TREE,
    )
    attribution = _actual_attribution(identity)
    result = materializer.ActualH4CampaignResult(
        identity=identity,
        attempts=_actual_attempts(identity, attribution),
        attribution=attribution,
        pbo=(_pbo("S3"), _pbo("S4")),
    )
    batch = result.batch_items()
    accounting = materializer.ActualMergedH6AAccounting().reconstruct(
        plan=plan, registered_total=48, attempts=batch
    )
    h5 = materializer.ActualMergedH5Composition()
    scorecard = h5.build_scorecard(plan=plan, h4_result=result, accounting=accounting)
    return plan, result, accounting, h5, scorecard


def test_actual_h4_h6a_h5_composition_is_typed_deterministic_and_canonical(tmp_path):
    plan, result, accounting, h5, scorecard = _composition(tmp_path)
    assert len(result.attempts) == len(result.batch_items()) == 48
    assert accounting.registered_total == accounting.primary_attempts == 48
    assert accounting.total_attempts == 48
    assert accounting.retry_attempts == 0
    assert accounting.accounting_complete is True
    assert scorecard["lineage"]["full_campaign_hash"] == plan.full_campaign_hash
    assert scorecard["lineage"]["campaign_run_id"] == plan.campaign_run_id
    assert scorecard["lineage"]["h6a_trial_accounting_hash"] == (
        accounting.trial_accounting_hash
    )
    assert scorecard["h6a_accounting"]["actual_h6a_contract"] == "PASS"
    assert scorecard["h4_attribution_contract"]["actual_h4_contract"] == "PASS"
    assert scorecard["h4_attribution_contract"]["typed_path_cross_check"] == "PASS"
    canonical = h5.canonical_json_bytes(scorecard)
    assert h5.semantic_hash(
        scorecard
    ) == materializer.h5_canonical.hash_canonical_bytes(canonical)
    assert h5.render_markdown(json.loads(canonical)).endswith(b"\n")
    assert h5.canonical_json_bytes(scorecard) == canonical


def test_raw_member_key_and_one_ulp_mutants_fail_before_scorecard():
    identity = build_production_identity_plan()
    attribution = _actual_attribution(identity)
    attempts = _actual_attempts(identity, attribution)
    first = attempts[0]
    first_path = first.path_scenario_evidence[0]
    forged_path = _path_evidence(
        path_scenario=first_path.path_scenario,
        member_keys=("f" * 64,),
        selected=True,
    )
    forged_attempt = build_attempt_record(
        row_id=first.row_id,
        experiment_id=first.experiment_id,
        campaign_run_id=first.campaign_run_id,
        full_campaign_hash=first.full_campaign_hash,
        strategy_key=first.strategy_key,
        retry_index=first.retry_index,
        status=first.status,
        reason_code=first.reason_code,
        fold_traces=first.fold_traces,
        unique_evidence=first.unique_evidence,
        path_scenario_evidence=(
            forged_path,
            *first.path_scenario_evidence[1:],
        ),
    )
    with pytest.raises(materializer.H6BPlanError, match="raw member keys"):
        materializer.ActualH4CampaignResult(
            identity=identity,
            attempts=(
                forged_attempt,
                *attempts[1:],
            ),
            attribution=attribution,
            pbo=(_pbo("S3"), _pbo("S4")),
        )

    path = attribution.paths[0]
    row = path.rows[0]
    with pytest.raises(H4AttributionError, match="S3 attribution value drift"):
        replace(row, market_return=math.nextafter(row.market_return, math.inf))


def test_actual_h5_rejects_nonproduction_plan(tmp_path):
    _plan, result, accounting, h5, _scorecard = _composition(tmp_path)
    fixture = materializer.ContractFixturePlan
    assert fixture is not materializer.ProductionExecutionPlan
    with pytest.raises(materializer.H6BPreflightRefused):
        h5.build_scorecard(
            plan=build_production_identity_plan(),
            h4_result=result,
            accounting=accounting,
        )


@pytest.mark.asyncio
async def test_actual_materializer_owns_transaction_and_publishes_h5_pair(tmp_path):
    identity = build_production_identity_plan()
    output = (tmp_path / "actual-scorecard").resolve()
    plan = materializer.build_production_execution_plan(
        identity=identity,
        output_root=output,
        integration_head_sha=_INTEGRATION_HEAD,
        integration_tree_sha=_INTEGRATION_TREE,
    )
    attribution = _actual_attribution(identity)
    result = materializer.ActualH4CampaignResult(
        identity=identity,
        attempts=_actual_attempts(identity, attribution),
        attribution=attribution,
        pbo=(_pbo("S3"), _pbo("S4")),
    )

    class ActualRunner:
        provenance = "actual_merged_h4"
        calls = 0

        async def run(self, received):
            assert received is identity
            self.calls += 1
            return result

    class AbsentInspector:
        provenance = "actual_read_only_campaign_state"
        calls = 0

        async def inspect(self, session, *, plan):
            del session
            assert plan is campaign.plan
            self.calls += 1
            return materializer.CampaignDbSnapshot(
                campaign_run_id=None, registered_mapping=(), attempts=()
            )

    session = SessionSpy()
    register_calls = 0
    record_calls = 0

    async def register(session_view, *, specs, guard_opt_in_enabled, guard_policy):
        nonlocal register_calls
        del session_view, guard_opt_in_enabled, guard_policy
        register_calls += 1
        starting_pk = 1 if register_calls == 1 else 25
        rows = []
        mapping = dict(plan.ordered_mapping)
        for primary_key, spec in enumerate(specs, start=starting_pk):
            rows.append(
                _RegisteredRow(
                    primary_key=primary_key,
                    experiment_id=mapping[spec.params["row_id"]],
                    strategy_key=spec.strategy_key,
                    strategy_version=spec.strategy_version,
                    **compute_identity_hashes(spec.components()),
                )
            )
        return rows

    async def find_existing(session_view, *, experiment_pk, idempotency_key):
        del session_view, experiment_pk, idempotency_key
        return None

    async def record(session_view, *, experiment_id, request):
        nonlocal record_calls
        del session_view, experiment_id
        record_calls += 1
        return _StoredRow(request.raw_payload["h6a_evidence_fingerprint"])

    campaign = materializer.ProductionCampaignInput(
        plan=plan,
        guard_policy=ResearchDbPolicy.of(
            ResearchDbTarget(host="localhost", database_name="test_db")
        ),
    )
    runner = ActualRunner()
    inspector = AbsentInspector()
    ports = materializer.ProductionExecutionPorts(
        session_factory=lambda: session,
        h4_runner=runner,
        artifacts=DirectoryAtomicArtifactPort(),
        state_inspector=inspector,
        register_experiments_fn=register,
        find_existing_trial_fn=find_existing,
        record_trial_fn=record,
    )
    target = materializer.DatabaseTarget(
        host="db-authority.invalid",
        port=5432,
        database="rob974_db",
        user="rob974_runner_test",
    )
    authorization = materializer.issue_run_authorization(
        plan,
        materializer.RunAuthority(
            expected_full_campaign_hash=plan.full_campaign_hash,
            expected_campaign_run_id=plan.campaign_run_id,
            expected_exact_48_mapping_hash=plan.exact_48_mapping_hash,
            approved_target=target,
            observed_target=target,
            inherited_target=None,
            write_opt_in=True,
            expected_output_root=output,
            requested_output_root=output,
            expected_source_pins=plan.source_pins,
            observed_source_pins=plan.source_pins,
            one_shot_approval="cp8-call-spy-no-db",
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
    assert outcome.trace == (
        "preflight",
        "artifact_probe",
        "session_factory",
        "begin",
        "db_state_inspection",
        "h6a_register",
        "h4_attempts",
        "h6a_record",
        "h6a_accounting",
        "h5_scorecard",
        "artifact_stage",
        "db_commit",
        "artifact_publish",
        "session_close",
    )
    assert session.calls == ["begin", "commit", "close"]
    assert register_calls == 2
    assert record_calls == 48
    assert runner.calls == inspector.calls == 1
    assert outcome.counters.register == outcome.counters.record == 1
    assert outcome.counters.commit == outcome.counters.publish == 1
    assert outcome.counters.rollback == outcome.counters.delete == 0
    assert outcome.scorecard["h4_attribution_contract"]["actual_h4_contract"] == (
        "PASS"
    )
    assert outcome.scorecard["h6a_accounting"]["actual_h6a_contract"] == "PASS"
    assert sorted(path.name for path in output.iterdir()) == [
        "scorecard.json",
        "scorecard.md",
    ]
    persisted = read_persisted_scorecard_pair(output_dir=output, h5_port=ports.h5)
    assert persisted.parsed_scorecard == outcome.scorecard
    assert (
        persisted.canonical_json_bytes == output.joinpath("scorecard.json").read_bytes()
    )
    assert persisted.markdown_bytes == output.joinpath("scorecard.md").read_bytes()
    assert persisted.semantic_hash == ports.h5.semantic_hash(outcome.scorecard)
    common = persisted.parsed_scorecard["strategies"]["S3"]["common_gates"]
    assert common["e0_bps"] == 10.0
    assert common["observed_win_rate"] == 0.0
    assert common["weighted_pbe"] == 0.468
    assert common["win_margin"] == -0.468
    assert common["pf17"] == 0.0
    assert common["pooled_e17_bps"] == -7.0
    s4_executor = persisted.parsed_scorecard["strategies"]["S4"]["pair_executor_state"]
    assert s4_executor == {
        "volatility_percentile": None,
        "volatility_percentile_provenance": "not_defined_for_s4",
        "pair_executor_state": "not_evaluated",
        "order_count": None,
        "residual_count": None,
        "pair_exec_fail_count": None,
        "readiness": "historical_screen_only",
        "demo_eligible": False,
    }

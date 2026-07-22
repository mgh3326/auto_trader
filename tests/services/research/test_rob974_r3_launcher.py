"""ROB-974 R3 CP7 launcher safety and production-wiring contracts."""

from __future__ import annotations

import importlib.util
import io
import json
from dataclasses import replace
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT = _ROOT / "scripts" / "run_rob974_r3_campaign.py"


def _launcher() -> ModuleType:
    spec = importlib.util.spec_from_file_location("rob974_r3_launcher", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("argv", [(), ("--plan",)])
def test_default_and_plan_are_zero_effect_paths(
    argv: tuple[str, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    launcher = _launcher()
    stdout = io.StringIO()
    stderr = io.StringIO()

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("zero-effect path crossed an effect boundary")

    for name in (
        "_install_runtime_paths",
        "_execute_schema_guard",
        "_execute_refrozen_run",
        "_load_exact_real_input",
    ):
        monkeypatch.setattr(launcher, name, forbidden)

    assert launcher.run_cli(argv, stdout=stdout, stderr=stderr, environ={}) == 0
    payload = json.loads(stdout.getvalue())
    assert payload["default_state"] == "DISABLED"
    assert payload["run_requested"] is False
    assert payload["refreeze"]["status"] == "CP8_PENDING_FINAL_REFREEZE"
    assert payload["identity"]["attempts"] == 12
    assert payload["identity"]["folds"] == 8
    assert payload["target"]["database"] == "rob974_r3_db"
    assert payload["target"]["output_root_template"].endswith(
        "rob974-r3-<full-hash-prefix>-v1"
    )
    assert all(value == 0 for value in payload["effects"].values())
    assert stderr.getvalue() == ""


def test_launcher_approved_database_literal_is_r3_only() -> None:
    source = _SCRIPT.read_text(encoding="utf-8")
    assert 'APPROVED_DB = ("localhost", 5432, "rob974_r3_db", "postgres")' in source
    assert '"rob974_db"' not in source
    assert 'ResearchDbTarget(host="localhost", database_name="rob974_r3_db")' in source
    assert '"database": "rob974_r3_db"' in source
    assert "current_database()" in source


@pytest.mark.asyncio
async def test_schema_probe_sets_read_only_before_first_inspection_and_rolls_back() -> (
    None
):
    launcher = _launcher()

    class _Result:
        def one(self) -> tuple[object, ...]:
            return (
                "rob974_r3_db",
                "postgres",
                "on",
                "2.26.3",
                True,
                "research.strategy_experiments",
                "research.backtest_runs",
                64,
                "20260722_rob1023_widen_runner",
            )

    class _Session:
        def __init__(self) -> None:
            self.statements: list[str] = []
            self.begins = 0
            self.rollbacks = 0
            self.closes = 0

        async def begin(self) -> None:
            self.begins += 1

        async def execute(self, statement: object) -> _Result:
            self.statements.append(str(statement))
            return _Result()

        def in_transaction(self) -> bool:
            return True

        async def rollback(self) -> None:
            self.rollbacks += 1

        async def close(self) -> None:
            self.closes += 1

    session = _Session()
    snapshot = await launcher._read_only_schema_session(session)
    assert snapshot.database == "rob974_r3_db"
    assert session.begins == 1
    assert session.statements[0].strip() == "SET TRANSACTION READ ONLY"
    assert "current_database()" in session.statements[1]
    assert "information_schema.columns" in session.statements[1]
    assert len(session.statements) == 2
    assert session.rollbacks == 1
    assert session.closes == 1


def test_schema_guard_missing_dsn_refuses_without_runtime_effects() -> None:
    launcher = _launcher()
    stdout = io.StringIO()
    stderr = io.StringIO()
    assert (
        launcher.run_cli(
            (launcher.SCHEMA_GUARD_ONLY_ARGUMENT,),
            stdout=stdout,
            stderr=stderr,
            environ={},
        )
        == launcher.AUTHORITY_OR_PREFLIGHT_REFUSED
    )
    assert stdout.getvalue() == ""
    assert stderr.getvalue() == (
        "AUTHORITY_OR_PREFLIGHT_REFUSED DATABASE_URL_ENV_ABSENT\n"
    )


def test_database_url_target_mutation_is_refused() -> None:
    launcher = _launcher()
    with pytest.raises(launcher.LaunchRefused, match="DATABASE_URL_TARGET_MISMATCH"):
        launcher._resolve_database_target(
            "postgresql+asyncpg://postgres@localhost:5432/rob974_db"
        )


def test_cp8_pending_guard_precedes_every_mutation_or_corpus_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = _launcher()
    calls: list[str] = []

    def forbidden(*_args: object, **_kwargs: object) -> None:
        calls.append("effect")
        raise AssertionError("CP8 pending guard did not stop first")

    for name in (
        "_require_exact_static_gates",
        "_load_exact_real_input",
        "_create_write_engine",
        "_publish_artifact_pair",
    ):
        monkeypatch.setattr(launcher, name, forbidden)

    with pytest.raises(launcher.LaunchRefused, match="CP8_PENDING_FINAL_REFREEZE"):
        launcher._execute_refrozen_run(
            object(), environ={}, stdout=io.StringIO(), stderr=io.StringIO()
        )
    assert calls == []


def test_production_identity_uses_real_exact_12_rows_and_eight_folds() -> None:
    launcher = _launcher()
    plan = launcher._build_candidate_production_plan()
    launcher._validate_production_plan_shape(plan)
    assert tuple(row_id for row_id, _ in plan.ordered_mapping) == (
        "S3-R3-00",
        "S3-R3-01",
        "S3-R3-02",
        "S4-R3-00",
        "S4-R3-01",
        "S4-R3-02",
        "S4-R3-03",
        "S4-R3-04",
        "S4-R3-05",
        "S4-R3-06",
        "S4-R3-07",
        "S4-R3-08",
    )
    assert tuple(fold.fold_id for fold in plan.folds) == tuple(
        f"fold-{index:02d}" for index in range(8)
    )
    assert len(plan.full_campaign_hash) == 64
    assert len(plan.exact_12_mapping_hash) == 64
    assert all(len(experiment_id) == 64 for _, experiment_id in plan.ordered_mapping)
    assert plan.campaign_run_id.startswith("rob974r3-")


def test_state_matrix_allows_only_fresh_run_or_exact_replay() -> None:
    launcher = _launcher()
    absent = launcher.PreflightState(
        database_state="ABSENT",
        artifact_state="ABSENT",
        registered_rows=0,
        attempt_rows=0,
        foreign_rows=0,
        retry_rows=0,
        stale_staging_rows=0,
        staging_artifacts=0,
        artifact_pair_valid=False,
        artifact_semantic_match=False,
    )
    present = launcher.PreflightState(
        database_state="PRESENT",
        artifact_state="PAIR_PRESENT",
        registered_rows=12,
        attempt_rows=12,
        foreign_rows=0,
        retry_rows=0,
        stale_staging_rows=0,
        staging_artifacts=0,
        artifact_pair_valid=True,
        artifact_semantic_match=True,
    )
    assert launcher._preflight_disposition(absent) == "RUN_ONCE"
    assert launcher._preflight_disposition(present) == "REPLAY_NOOP"


@pytest.mark.asyncio
async def test_coarse_asymmetry_stops_before_corpus_or_engine_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = _launcher()
    plan = launcher._build_candidate_production_plan()
    calls: list[str] = []

    class _Engine:
        async def dispose(self) -> None:
            calls.append("dispose")

    async def coarse_probe(**_kwargs: object) -> object:
        calls.append("coarse_probe")
        return launcher.DatabaseProjection(
            state=launcher.PreflightState(
                database_state="ABSENT",
                artifact_state="PAIR_PRESENT",
                registered_rows=0,
                attempt_rows=0,
                foreign_rows=0,
                retry_rows=0,
                stale_staging_rows=0,
                staging_artifacts=0,
                artifact_pair_valid=False,
                artifact_semantic_match=False,
            ),
            persisted_snapshot=object(),
        )

    def forbidden(name: str):
        def fail(*_args: object, **_kwargs: object) -> None:
            calls.append(name)
            raise AssertionError(f"{name} crossed the coarse refusal boundary")

        return fail

    monkeypatch.setattr(
        launcher,
        "_require_exact_static_gates",
        lambda *_args, **_kwargs: (
            Path("manifest.json"),
            Path("corpus"),
            Path("output"),
            "postgresql+asyncpg://postgres@localhost:5432/rob974_r3_db",
        ),
    )
    monkeypatch.setattr(launcher, "_install_runtime_paths", lambda: None)
    monkeypatch.setattr(launcher, "_build_candidate_production_plan", lambda: plan)
    monkeypatch.setattr(launcher, "_require_plan_matches_refreeze", lambda *_a: None)
    monkeypatch.setattr(launcher, "_registration_specs", lambda _plan: ((), ()))
    monkeypatch.setattr(launcher, "_create_read_only_engine", lambda _url: _Engine())
    monkeypatch.setattr(launcher, "_read_only_coarse_state_session", coarse_probe)
    monkeypatch.setattr(launcher, "_load_exact_real_input", forbidden("corpus"))
    monkeypatch.setattr(launcher, "_compute_actual_campaign", forbidden("compute"))

    import sqlalchemy.ext.asyncio

    monkeypatch.setattr(
        sqlalchemy.ext.asyncio,
        "AsyncSession",
        lambda **_kwargs: object(),
    )
    with pytest.raises(
        launcher.LaunchRefused,
        match="COARSE_DATABASE_ARTIFACT_ASYMMETRY_OR_PARTIAL",
    ):
        await launcher._run_after_refreeze_preflight(
            SimpleNamespace(),
            pins=object(),
            environ={},
            stdout=io.StringIO(),
            stderr=io.StringIO(),
        )
    assert calls == ["coarse_probe", "dispose"]


@pytest.mark.parametrize(
    "updates",
    [
        {"registered_rows": 11},
        {"registered_rows": 13},
        {"attempt_rows": 11},
        {"foreign_rows": 1},
        {"retry_rows": 1},
        {"stale_staging_rows": 1},
        {"staging_artifacts": 1},
        {"artifact_state": "PARTIAL"},
        {"artifact_pair_valid": False},
        {"artifact_semantic_match": False},
        {"database_state": "ABSENT"},
    ],
)
def test_partial_foreign_staging_and_semantic_replay_mutants_fail_closed(
    updates: dict[str, object],
) -> None:
    launcher = _launcher()
    values: dict[str, object] = {
        "database_state": "PRESENT",
        "artifact_state": "PAIR_PRESENT",
        "registered_rows": 12,
        "attempt_rows": 12,
        "foreign_rows": 0,
        "retry_rows": 0,
        "stale_staging_rows": 0,
        "staging_artifacts": 0,
        "artifact_pair_valid": True,
        "artifact_semantic_match": True,
    }
    values.update(updates)
    state = launcher.PreflightState(**values)
    with pytest.raises(launcher.LaunchRefused):
        launcher._preflight_disposition(state)


def test_second_run_exact_replay_is_write_free(monkeypatch: pytest.MonkeyPatch) -> None:
    launcher = _launcher()
    state = launcher.PreflightState(
        database_state="PRESENT",
        artifact_state="PAIR_PRESENT",
        registered_rows=12,
        attempt_rows=12,
        foreign_rows=0,
        retry_rows=0,
        stale_staging_rows=0,
        staging_artifacts=0,
        artifact_pair_valid=True,
        artifact_semantic_match=True,
    )

    def forbidden() -> None:
        raise AssertionError("exact replay attempted a write or retry")

    result = launcher._dispatch_preflight_disposition(
        state=state,
        run_once=forbidden,
        replay_noop=lambda: "REPLAY_NOOP",
    )
    assert result == "REPLAY_NOOP"


def _exact_replay_fixture(launcher: ModuleType) -> tuple[object, object, tuple]:
    from app.services.rob974_r3_h6a_bridge import R3AttemptBatchItem
    from app.services.rob974_r3_materializer import issue_r3_materialization_contract
    from research_contracts.canonical_hash import canonical_sha256

    plan = launcher._build_candidate_production_plan()
    specs = launcher._registration_specs(plan)
    contract = issue_r3_materialization_contract(
        full_campaign_hash=plan.full_campaign_hash,
        campaign_run_id=plan.campaign_run_id,
        s3_specs=specs[0],
        s4_specs=specs[1],
        row_id_to_experiment_id=dict(plan.ordered_mapping),
    )
    attempts = tuple(
        R3AttemptBatchItem(
            row_id=row_id,
            experiment_id=experiment_id,
            retry_index=0,
            status="completed",
            reason_code=None,
            fold_evidence_hash=canonical_sha256([row_id, "folds"]),
            run_identity=canonical_sha256([row_id, "run"]),
            evidence_payload={"row_id": row_id, "fold_count": 8},
        )
        for row_id, experiment_id in plan.ordered_mapping
    )
    computed = SimpleNamespace(
        registration_specs=specs,
        materialization_contract=contract,
        attempts=attempts,
        accounting=SimpleNamespace(
            status_counts={
                "completed": 12,
                "rejected": 0,
                "crashed": 0,
                "timeout": 0,
            }
        ),
    )
    return plan, computed, attempts


def test_exact_replay_uses_persisted_snapshot_and_semantic_attempt_fingerprints() -> (
    None
):
    launcher = _launcher()
    from app.services.rob974_r3_materializer import (
        R3PersistedSnapshot,
        R3ReplayCollisionError,
    )

    plan, computed, attempts = _exact_replay_fixture(launcher)
    snapshot = R3PersistedSnapshot(
        campaign_run_id=plan.campaign_run_id,
        registered_mapping=plan.ordered_mapping,
        attempts=attempts,
        status_counts=tuple(computed.accounting.status_counts.items()),
    )
    projection = launcher.DatabaseProjection(
        state=launcher.PreflightState(
            database_state="PRESENT",
            artifact_state="PAIR_PRESENT",
            registered_rows=12,
            attempt_rows=12,
            foreign_rows=0,
            retry_rows=0,
            stale_staging_rows=0,
            staging_artifacts=0,
            artifact_pair_valid=True,
            artifact_semantic_match=True,
        ),
        persisted_snapshot=snapshot,
    )
    seal = launcher._validate_exact_replay(projection=projection, computed=computed)
    assert seal.registered_total == seal.primary_attempts == 12
    assert seal.retry_attempts == 0

    drifted = replace(
        attempts[0], evidence_payload={"row_id": attempts[0].row_id, "fold_count": 7}
    )
    with pytest.raises(R3ReplayCollisionError):
        launcher._validate_exact_replay(
            projection=replace(
                projection,
                persisted_snapshot=replace(snapshot, attempts=(drifted, *attempts[1:])),
            ),
            computed=computed,
        )


def test_raw_database_projection_reconstructs_exact_issued_snapshot() -> None:
    launcher = _launcher()
    plan, computed, attempts = _exact_replay_fixture(launcher)
    expected = launcher._expected_registration_projection(plan=plan, computed=computed)
    experiment_rows = tuple(
        {"id": index, **expected[row_id]}
        for index, (row_id, _experiment_id) in enumerate(plan.ordered_mapping, start=1)
    )
    attempt_rows = []
    for index, item in enumerate(attempts, start=1):
        raw_payload = {
            "r3_h6a_evidence_fingerprint": item.fingerprint(),
            "full_campaign_hash": plan.full_campaign_hash,
            "campaign_run_id": plan.campaign_run_id,
            "exact_12_mapping_hash": plan.exact_12_mapping_hash,
            "row_id": item.row_id,
            "experiment_id": item.experiment_id,
            "retry_index": 0,
            "status": item.status,
            "reason_code": item.reason_code,
            "fold_evidence_hash": item.fold_evidence_hash,
            "run_identity": item.run_identity,
            "evidence_payload": {"row_id": item.row_id, "fold_count": 8},
        }
        attempt_rows.append(
            {
                "id": index,
                "strategy_experiment_id": index,
                "trial_idempotency_key": item.idempotency_key(plan.campaign_run_id),
                "trial_status": item.status,
                "runner": launcher.R3_RUNNER,
                "raw_payload": raw_payload,
            }
        )
    projection = launcher._project_database_rows(
        plan=plan,
        computed=computed,
        experiment_rows=experiment_rows,
        attempt_rows=tuple(attempt_rows),
        artifact_state="PAIR_PRESENT",
        staging_artifacts=0,
        artifact_pair_valid=True,
        artifact_semantic_match=True,
    )
    assert launcher._preflight_disposition(projection.state) == "REPLAY_NOOP"
    launcher._validate_exact_replay(projection=projection, computed=computed)


def test_terminal_incomplete_is_rejected_and_completed_mutation_fails_replay() -> None:
    launcher = _launcher()
    from dataclasses import dataclass

    from app.services.rob974_r3_h6a_bridge import R3AttemptBatchItem
    from app.services.rob974_r3_materializer import (
        R3PersistedSnapshot,
        R3ReplayCollisionError,
        issue_r3_materialization_contract,
        validate_r3_persisted_snapshot,
    )

    @dataclass(frozen=True)
    class Report:
        phase: str

    plan = launcher._build_candidate_production_plan()
    empty_paths = [
        {
            "path_scenario": scenario,
            "terminal_incomplete_rows": [],
        }
        for scenario in launcher.PATH_SCENARIOS
    ]
    cell_evidence = {
        row_id: [
            {
                "phase": phase,
                "fold_id": fold.fold_id,
                "accepted_decision_units": 0,
                "path_evidence": [dict(path) for path in empty_paths],
            }
            for phase in ("TRAIN", "OOS")
            for fold in plan.folds
        ]
        for row_id, _experiment_id in plan.ordered_mapping
    }
    for path in cell_evidence["S3-R3-00"][8]["path_evidence"]:
        path["terminal_incomplete_rows"] = [
            {
                "symbol": "XRPUSDT",
                "signal_ts": plan.folds[0].oos_start_ms,
                "reason": "fold_horizon_rejected",
            }
        ]
    reports = {
        (row_id, phase): Report(phase)
        for row_id, _experiment_id in plan.ordered_mapping
        for phase in ("TRAIN", "OOS")
    }
    attempts = launcher._build_attempts(
        plan=plan, cell_evidence=cell_evidence, gate_reports=reports
    )
    assert attempts[0].status == "rejected"
    assert attempts[0].reason_code == "rejected:data_gap_in_position"
    assert all(item.status == "completed" for item in attempts[1:])

    specs = launcher._registration_specs(plan)
    contract = issue_r3_materialization_contract(
        full_campaign_hash=plan.full_campaign_hash,
        campaign_run_id=plan.campaign_run_id,
        s3_specs=specs[0],
        s4_specs=specs[1],
        row_id_to_experiment_id=dict(plan.ordered_mapping),
    )
    first = attempts[0]
    forged_completed = R3AttemptBatchItem(
        row_id=first.row_id,
        experiment_id=first.experiment_id,
        retry_index=0,
        status="completed",
        reason_code=None,
        fold_evidence_hash=first.fold_evidence_hash,
        run_identity=first.run_identity,
        evidence_payload=first.evidence_payload,
    )
    persisted = (forged_completed, *attempts[1:])
    snapshot = R3PersistedSnapshot(
        campaign_run_id=plan.campaign_run_id,
        registered_mapping=plan.ordered_mapping,
        attempts=persisted,
        status_counts=(
            ("completed", 12),
            ("rejected", 0),
            ("crashed", 0),
            ("timeout", 0),
        ),
    )
    with pytest.raises(R3ReplayCollisionError):
        validate_r3_persisted_snapshot(
            contract=contract,
            snapshot=snapshot,
            recomputed_attempts=attempts,
        )


def test_output_root_is_canonical_full_hash_prefix_v1() -> None:
    launcher = _launcher()
    full_hash = "43f4ce12a4fda7791702e8fd2071ba8e8c854b7f2ca68d1ac7a186f6e8fa12d9"
    assert launcher._output_root_for(full_hash) == Path(
        "/Users/mgh3326/work/herdr-artifacts/rob974-r3-43f4ce12-v1"
    )


def test_launcher_wires_stable_m4_scorecard_and_markdown_seams() -> None:
    source = _SCRIPT.read_text(encoding="utf-8")
    for name in (
        "build_r3_scorecard",
        "canonical_r3_json_bytes",
        "hash_r3_canonical_bytes",
        "build_r3_artifact_pair",
        "verify_r3_artifact_pair",
        "issue_r3_fold_scenario_attribution",
        "issue_r3_all_cell_oos_ledger",
        "R3CellOOSInput",
        "R3FoldOOSInput",
        "render_r3_markdown",
        "verify_r3_markdown_semantic_binding",
    ):
        assert name in source
    assert "rob974.r3.h5.scorecard.v1" in source


def test_read_only_engine_enforces_connection_default_read_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = _launcher()
    observed: dict[str, object] = {}

    def fake_create(url: str, **kwargs: object) -> object:
        observed.update(url=url, **kwargs)
        return object()

    monkeypatch.setattr("sqlalchemy.ext.asyncio.create_async_engine", fake_create)
    launcher._create_read_only_engine(
        "postgresql+asyncpg://postgres@localhost:5432/rob974_r3_db"
    )
    assert observed["connect_args"] == {
        "server_settings": {"default_transaction_read_only": "on"}
    }


def _pins_for(plan: object, launcher: ModuleType) -> object:
    return launcher.FinalRefreezePins(
        status="CP8_REFROZEN",
        approved_integration_head_sha="1" * 40,
        approved_integration_tree_sha="2" * 40,
        full_campaign_hash=plan.full_campaign_hash,
        campaign_run_id=plan.campaign_run_id,
        exact_12_mapping_hash=plan.exact_12_mapping_hash,
        feature_source_sha256=plan.source_pins.feature_source_sha256,
        engine_source_sha256=plan.source_pins.engine_source_sha256,
        runner_source_sha256=plan.source_pins.runner_source_sha256,
        pbo_implementation_sha256=plan.source_pins.pbo_implementation_sha256,
    )


@pytest.mark.parametrize(
    "field",
    [
        "full_campaign_hash",
        "campaign_run_id",
        "exact_12_mapping_hash",
        "feature_source_sha256",
        "engine_source_sha256",
        "runner_source_sha256",
        "pbo_implementation_sha256",
    ],
)
def test_derived_plan_must_match_every_cp8_identity_pin(field: str) -> None:
    launcher = _launcher()
    plan = launcher._build_candidate_production_plan()
    pins = _pins_for(plan, launcher)
    launcher._require_plan_matches_refreeze(plan, pins)
    replacement = "rob974r3-mutant" if field == "campaign_run_id" else "f" * 64
    with pytest.raises(
        launcher.LaunchRefused, match="DERIVED_PLAN_DIFFERS_FROM_CP8_REFREEZE"
    ):
        launcher._require_plan_matches_refreeze(
            plan, replace(pins, **{field: replacement})
        )


@pytest.mark.parametrize(
    ("commit_count", "changed_paths"),
    [
        ("2", "scripts/run_rob974_r3_campaign.py"),
        (
            "1",
            "scripts/run_rob974_r3_campaign.py\n"
            "research/nautilus_scalping/rob974_r3_plan.py",
        ),
        ("0", ""),
    ],
)
def test_cp8_must_be_one_launcher_only_descendant_commit(
    commit_count: str,
    changed_paths: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = _launcher()
    plan = launcher._build_candidate_production_plan()
    pins = _pins_for(plan, launcher)

    def fake_git(*arguments: str) -> str:
        if arguments[:2] == ("rev-list", "--count"):
            return commit_count
        if arguments[:2] == ("diff", "--name-only"):
            return changed_paths
        raise AssertionError(arguments)

    monkeypatch.setattr(launcher, "_git", fake_git)
    with pytest.raises(
        launcher.LaunchRefused,
        match="CP8_DESCENDANT_NOT_SINGLE_LAUNCHER_ONLY_COMMIT",
    ):
        launcher._require_single_cp8_launcher_descendant(pins)


def test_cp8_single_launcher_only_descendant_commit_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = _launcher()
    pins = _pins_for(launcher._build_candidate_production_plan(), launcher)

    def fake_git(*arguments: str) -> str:
        return (
            "1"
            if arguments[:2] == ("rev-list", "--count")
            else "scripts/run_rob974_r3_campaign.py"
        )

    monkeypatch.setattr(launcher, "_git", fake_git)
    launcher._require_single_cp8_launcher_descendant(pins)


def test_s3_generator_uses_exact_gate_source_and_existing_arbitration() -> None:
    launcher = _launcher()
    import rob974_h3_s3 as s3
    from rob974_features import SYMBOLS
    from rob974_r3_manifest import get_r3_config

    config = get_r3_config("S3-R3-02")
    decision_ts = 1_751_328_000_000

    def metrics(symbol: str) -> object:
        return s3.S3Metrics(
            config_id=config.config_id,
            decision_ts=decision_ts,
            symbol=symbol,
            R=0.10,
            ER=0.50,
            S=0.10,
            Qplus=0.50,
            Qminus=-0.10,
            close=101.0,
            previous_close=100.0,
            prior_l_high=102.0,
            prior_l_low=90.0,
            atr20=0.6,
            A=0.006,
            vwap12=100.0,
            vwap24=99.0,
            percentile_30d=50.0,
            range24=0.03,
            market_return_24h=0.01,
            current_market_return_4h=0.005,
            bplus=3,
            bminus=0,
        )

    source = SimpleNamespace(
        units=tuple(
            s3.S3FormulaUnit(decision_ts, symbol, metrics(symbol)) for symbol in SYMBOLS
        )
    )
    output = launcher._generate_r3_s3(source=source, config=config)
    assert len(output.decisions) == 3
    assert len(output.accepted) == 1
    assert len(output.rejected) == 2
    assert output.accepted[0].config_id == "S3-R3-02"


def test_s4_generator_preserves_low_z_source_rebound_order_and_arbitration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = _launcher()
    import rob974_h3_s3 as s3
    import rob974_h3_s4 as s4
    import rob974_r3_gate_adapter as gate_adapter
    from rob974_h4_contracts import exact_h4_folds
    from rob974_r3_h3_adapter import R3S4GateObservation
    from rob974_r3_manifest import get_r3_config

    config = get_r3_config("S4-R3-08")
    fold = exact_h4_folds()[0]
    decision_ts = fold.train_start_ms
    symbols = {
        "XRP-DOGE": ("XRPUSDT", "DOGEUSDT"),
        "XRP-SOL": ("XRPUSDT", "SOLUSDT"),
        "DOGE-SOL": ("DOGEUSDT", "SOLUSDT"),
    }
    distance = {"XRP-DOGE": 0.018, "XRP-SOL": 0.017, "DOGE-SOL": 0.016}

    def estimate(pair: str, *, config_id: str) -> object:
        symbol_a, symbol_b = symbols[pair]
        d_fraction = distance[pair]
        return s4.S4Estimate(
            config_id=config_id,
            decision_ts=decision_ts,
            pair=pair,
            symbol_a=symbol_a,
            symbol_b=symbol_b,
            beta_a=1.0,
            beta_b=1.0,
            beta_a_first=1.0,
            beta_a_second=1.0,
            beta_b_first=1.0,
            beta_b_second=1.0,
            weight_a=0.5,
            weight_b=0.5,
            spread=-d_fraction,
            mu=0.0,
            mad=0.01,
            effective_mad_scale=0.014826,
            z=-0.60,
            prior_beta_a=1.0,
            prior_beta_b=1.0,
            prior_weight_a=0.5,
            prior_weight_b=0.5,
            prior_mu=0.0,
            prior_mad=0.01,
            prior_effective_mad_scale=0.014826,
            z_prior=-0.80,
            D_fraction=d_fraction,
            D_bps=d_fraction * 10_000.0,
            rho=0.70,
            phi=0.75,
            half_life_4h_bars=2.409420839653209,
            beta_stability=0.10,
            sigma_pair=0.0,
            pair_return_fraction=0.001,
            pair_return_bps=10.0,
            current_market_return_4h=0.005,
        )

    def observe(
        _feature_context: object,
        observed_config: object,
        observed_ts: int,
        pair: str,
    ) -> object:
        assert observed_config is config
        assert observed_ts == decision_ts
        rebound = estimate(pair, config_id=config.config_id)
        return gate_adapter.R3S4ObservationOutcome(
            decision_ts,
            pair,
            R3S4GateObservation.from_estimate(rebound),
            None,
        )

    def reestimate(
        _feature_context: object,
        anchor: object,
        observed_ts: int,
        pair: str,
    ) -> object:
        assert anchor.config_id == "S4-02"
        assert observed_ts == decision_ts
        return s4.S4EstimationOutcome(
            estimate(pair, config_id=anchor.config_id),
            None,
        )

    monkeypatch.setattr(s3, "expected_decision_closes", lambda _window: (decision_ts,))
    monkeypatch.setattr(gate_adapter, "observe_r3_s4_pair", observe)
    monkeypatch.setattr(s4, "estimate_s4_pair", reestimate)

    source = gate_adapter.build_r3_s4_fold_source(
        feature_context=object(),
        fold=fold,
        phase="TRAIN",
        config=config,
    )
    output = launcher._generate_r3_s4(
        feature_context=object(),
        source=source,
        config=config,
    )

    assert tuple(unit.pair for unit in source.units) == s4.PAIR_ORDER
    assert tuple(decision.pair for decision in output.decisions) == s4.PAIR_ORDER
    assert output.config_id == config.config_id
    assert len(output.accepted) == 1
    assert len(output.rejected) == 2
    winner = output.accepted[0]
    assert winner.pair == "XRP-DOGE"
    assert winner.config_id == config.config_id
    assert winner.observed_z.hex() == (-0.60).hex()
    assert winner.observed_z.hex() == source.units[0].observation.z_current.hex()
    assert all(item.candidate.config_id == config.config_id for item in output.rejected)
    assert all(
        item.reason == "simultaneous_pair_arbitration_loser" for item in output.rejected
    )


def test_funding_is_prepared_once_before_exact_three_fresh_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = _launcher()
    from rob974_h2_dtos import S3EngineResult
    from rob974_h4_adapter import (
        SealedS3Terminal,
        seal_s3_engine_input,
        seal_s3_engine_output,
    )
    from rob974_h4_contracts import exact_h4_folds
    from rob974_r3_manifest import get_r3_config

    fold = exact_h4_folds()[0]
    config = get_r3_config("S3-R3-02")
    calls: list[str] = []

    def prepare(**_kwargs: object) -> tuple[tuple[object, ...], tuple[object, ...]]:
        calls.append("funding")
        return (), ()

    def invoke(**kwargs: object) -> object:
        calls.append("engine")
        result = S3EngineResult((), (), ())
        return SealedS3Terminal(
            result,
            seal_s3_engine_input(
                (),
                corpus_end_ts=kwargs["input_data"].corpus_end_ts,
                horizon_end_ts=kwargs["horizon_end_ts"],
            ),
            seal_s3_engine_output(result),
        )

    monkeypatch.setattr(launcher, "_prepare_s3_funding_once", prepare)
    monkeypatch.setattr(launcher, "_invoke_s3_path", invoke)
    sources, evidence = launcher._execute_exact_three_paths(
        config=config,
        fold=fold,
        phase="OOS",
        generator_output=SimpleNamespace(accepted=()),
        input_data=SimpleNamespace(
            corpus_end_ts=fold.oos_end_ms,
            funding_sidecars=(),
        ),
        surface=SimpleNamespace(
            minute_index={}, close_feature_index={}, pair_close_index={}
        ),
    )
    assert calls == ["funding", "engine", "engine", "engine"]
    assert tuple(name for name, _source in sources) == launcher.PATH_SCENARIOS
    assert sources[1][0] == launcher.PRIMARY_STRESS_SCENARIO
    assert tuple(row["path_scenario"] for row in evidence) == launcher.PATH_SCENARIOS


@pytest.mark.asyncio
async def test_stage_commit_publish_order_and_precommit_rollback() -> None:
    launcher = _launcher()
    calls: list[str] = []

    class Session:
        active = True

        async def commit(self) -> None:
            calls.append("commit")
            self.active = False

        def in_transaction(self) -> bool:
            return self.active

        async def rollback(self) -> None:
            calls.append("rollback")
            self.active = False

    async def persist() -> str:
        calls.append("persist")
        return "persisted"

    result = await launcher._coordinate_stage_commit_publish(
        session=Session(),
        persist=persist,
        stage=lambda: calls.append("stage") or "staged",
        publish=lambda _staged: calls.append("publish") or "published",
    )
    assert result == ("persisted", "staged", "published")
    assert calls == ["persist", "stage", "commit", "publish"]

    calls.clear()
    with pytest.raises(launcher.PrecommitCampaignFailure):
        await launcher._coordinate_stage_commit_publish(
            session=Session(),
            persist=persist,
            stage=lambda: (_ for _ in ()).throw(OSError("stage fault")),
            publish=lambda _staged: calls.append("publish"),
        )
    assert calls == ["persist", "rollback"]


@pytest.mark.asyncio
async def test_postcommit_publish_failure_never_rolls_back_or_retries() -> None:
    launcher = _launcher()
    calls: list[str] = []

    class Session:
        active = True

        async def commit(self) -> None:
            calls.append("commit")
            self.active = False

        def in_transaction(self) -> bool:
            return self.active

        async def rollback(self) -> None:
            calls.append("rollback")

    async def persist() -> str:
        calls.append("persist")
        return "persisted"

    def fail_publish(_staged: object) -> object:
        calls.append("publish")
        raise OSError("rename fault")

    with pytest.raises(launcher.PostcommitPublishFailure):
        await launcher._coordinate_stage_commit_publish(
            session=Session(),
            persist=persist,
            stage=lambda: calls.append("stage") or "staged",
            publish=fail_publish,
        )
    assert calls == ["persist", "stage", "commit", "publish"]


@pytest.mark.asyncio
async def test_fresh_postcommit_read_only_audit_requires_exact_semantic_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = _launcher()
    calls: list[str] = []

    class _Engine:
        async def dispose(self) -> None:
            calls.append("dispose")

    exact_state = launcher.PreflightState(
        database_state="PRESENT",
        artifact_state="PAIR_PRESENT",
        registered_rows=12,
        attempt_rows=12,
        foreign_rows=0,
        retry_rows=0,
        stale_staging_rows=0,
        staging_artifacts=0,
        artifact_pair_valid=True,
        artifact_semantic_match=True,
    )

    async def read_state(**_kwargs: object) -> object:
        calls.append("read_only_projection")
        return launcher.DatabaseProjection(exact_state, object())

    def validate(**_kwargs: object) -> str:
        calls.append("persisted_snapshot_validation")
        return "sealed"

    monkeypatch.setattr(
        launcher,
        "_create_read_only_engine",
        lambda _url: calls.append("create_read_only_engine") or _Engine(),
    )
    monkeypatch.setattr(launcher, "_read_only_campaign_state_session", read_state)
    monkeypatch.setattr(launcher, "_validate_exact_replay", validate)

    import sqlalchemy.ext.asyncio

    monkeypatch.setattr(
        sqlalchemy.ext.asyncio,
        "AsyncSession",
        lambda **_kwargs: calls.append("fresh_session") or object(),
    )
    result = await launcher._postcommit_read_only_audit(
        database_url="postgresql+asyncpg://postgres@localhost:5432/rob974_r3_db",
        plan=object(),
        computed=object(),
        artifacts=object(),
        output_root=Path("output"),
    )
    assert result == "sealed"
    assert calls == [
        "create_read_only_engine",
        "fresh_session",
        "read_only_projection",
        "persisted_snapshot_validation",
        "dispose",
    ]


@pytest.mark.asyncio
async def test_postcommit_audit_failure_is_distinct_and_never_validates_partial_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = _launcher()
    calls: list[str] = []

    class _Engine:
        async def dispose(self) -> None:
            calls.append("dispose")

    invalid_state = launcher.PreflightState(
        database_state="PRESENT",
        artifact_state="PAIR_PRESENT",
        registered_rows=12,
        attempt_rows=12,
        foreign_rows=0,
        retry_rows=0,
        stale_staging_rows=0,
        staging_artifacts=0,
        artifact_pair_valid=False,
        artifact_semantic_match=False,
    )

    async def read_state(**_kwargs: object) -> object:
        calls.append("read_only_projection")
        return launcher.DatabaseProjection(invalid_state, object())

    monkeypatch.setattr(launcher, "_create_read_only_engine", lambda _url: _Engine())
    monkeypatch.setattr(launcher, "_read_only_campaign_state_session", read_state)
    monkeypatch.setattr(
        launcher,
        "_validate_exact_replay",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("partial state reached semantic replay validation")
        ),
    )

    import sqlalchemy.ext.asyncio

    monkeypatch.setattr(sqlalchemy.ext.asyncio, "AsyncSession", lambda **_kw: object())
    with pytest.raises(
        launcher.PostcommitAuditFailure,
        match="POSTCOMMIT_READ_ONLY_AUDIT_FAILED_RETRY_FORBIDDEN",
    ):
        await launcher._postcommit_read_only_audit(
            database_url=("postgresql+asyncpg://postgres@localhost:5432/rob974_r3_db"),
            plan=object(),
            computed=object(),
            artifacts=object(),
            output_root=Path("output"),
        )
    assert calls == ["read_only_projection", "dispose"]


def test_postcommit_audit_failure_has_dedicated_exit_and_forensic_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = _launcher()

    class _Parser:
        def parse_args(self, _argv: object) -> object:
            values = dict.fromkeys(launcher._REQUIRED, "set")
            return SimpleNamespace(run=True, **values)

    def fail(*_args: object, **_kwargs: object) -> None:
        raise launcher.PostcommitAuditFailure("committed state audit failed")

    monkeypatch.setattr(launcher, "_parser", _Parser)
    monkeypatch.setattr(launcher, "_execute_refrozen_run", fail)
    stdout = io.StringIO()
    stderr = io.StringIO()
    assert (
        launcher.run_cli(("--run",), stdout=stdout, stderr=stderr, environ={})
        == launcher.POSTCOMMIT_AUDIT_FAILURE
    )
    assert stdout.getvalue() == ""
    assert stderr.getvalue() == (
        "POSTCOMMIT_AUDIT_FAILURE commit_and_publish_confirmed "
        "retry_forbidden_manual_database_artifact_forensics_required\n"
    )


def test_launcher_calls_actual_m4_api_when_integrated() -> None:
    pytest.importorskip("rob974_r3_scorecard")
    launcher = _launcher()
    from rob941_funding_sidecar import FundingSidecar
    from rob974_h2_dtos import S3EngineResult
    from rob974_h3_manifest import SYMBOLS
    from rob974_h4_adapter import (
        SealedS3Terminal,
        seal_s3_engine_input,
        seal_s3_engine_output,
    )
    from rob974_h4_contracts import exact_h4_folds
    from rob974_r3_evidence_context import issue_r3_production_evidence_context
    from rob974_r3_manifest import get_r3_config
    from rob974_r3_relaxation_h2_adapter import R3H2CellFoldInput

    api = launcher._load_m4_api()
    plan = launcher._build_candidate_production_plan()
    context = issue_r3_production_evidence_context(plan)
    config = get_r3_config("S3-R3-02")
    fold = exact_h4_folds()[0]
    result = S3EngineResult((), (), ())
    source = R3H2CellFoldInput(
        config=config,
        fold_id=fold.fold_id,
        h3_candidates=(),
        engine_intents=(),
        corpus_end_ts=fold.oos_end_ms,
        horizon_end_ts=fold.oos_end_ms,
        terminal=SealedS3Terminal(
            result,
            seal_s3_engine_input(
                (),
                corpus_end_ts=fold.oos_end_ms,
                horizon_end_ts=fold.oos_end_ms,
            ),
            seal_s3_engine_output(result),
        ),
    )
    receipts = tuple(
        api.issue_r3_fold_scenario_attribution(
            path_scenario=scenario,
            source=source,
            decision_snapshots=(),
            funding_sidecars=tuple(
                FundingSidecar.from_rows(symbol, ()) for symbol in SYMBOLS
            ),
        )
        for scenario in launcher.PATH_SCENARIOS
    )
    fold_input = api.R3FoldOOSInput(scenario_attributions=receipts)
    assert fold_input.primary.source is source
    assert context.campaign_identity_sha256 == plan.full_campaign_hash


def test_launcher_has_no_broker_order_or_fill_import_surface() -> None:
    source = _SCRIPT.read_text(encoding="utf-8")
    imports = "\n".join(
        line.strip().lower()
        for line in source.splitlines()
        if line.startswith(("import ", "from "))
    )
    assert "taskiq" not in imports
    assert "broker" not in imports
    assert "order" not in imports
    assert "fill" not in imports

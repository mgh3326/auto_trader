"""ROB-974 R3 CP7 launcher safety and production-wiring contracts."""

from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
from types import ModuleType

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
        "issue_r3_all_cell_oos_ledger",
        "R3CellOOSLedger",
        "render_r3_markdown",
        "verify_r3_markdown_semantic_binding",
    ):
        assert name in source
    assert "rob974.r3.h5.scorecard.v1" in source


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

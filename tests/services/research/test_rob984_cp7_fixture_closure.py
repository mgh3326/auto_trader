"""ROB-984 CP7 deterministic contract-fixture dependency-stop closure."""

from __future__ import annotations

import ast
import importlib.util
import json
from dataclasses import replace
from pathlib import Path

import pytest
import rob974_h6b_artifacts
import rob974_h6b_cli
import rob974_h6b_postaudit

from app.services import rob974_h6b_materializer as materializer
from tests.services.research.test_rob984_cp3_transaction_coordinator import Fixture
from tests.services.research.test_rob984_cp5_postaudit import AuditFixture

_ROOT = Path(__file__).resolve().parents[3]
_EXPECTED_RESEARCH_PRODUCTION = {
    "rob974_h6b_artifacts.py",
    "rob974_h6b_cli.py",
    "rob974_h6b_postaudit.py",
}


class _AbsentStateInspector:
    provenance = "contract_fixture"

    def __init__(self):
        self.calls = 0

    async def inspect(self, session, *, plan):
        del session, plan
        self.calls += 1
        return materializer.CampaignDbSnapshot(
            campaign_run_id=None,
            registered_mapping=(),
            attempts=(),
        )


async def _materialize_fixture(root: Path):
    root.mkdir()
    fixture = Fixture(root)
    inspector = _AbsentStateInspector()
    ports = replace(fixture.ports, state_inspector=inspector)
    outcome = await materializer.materialize_or_replay_contract_fixture(
        plan=fixture.plan,
        authorization=fixture.authorize(),
        campaign=fixture.campaign,
        ports=ports,
        output_dir=fixture.output,
    )
    closure = materializer.build_contract_fixture_closure_evidence(
        plan=fixture.plan, outcome=outcome
    )
    return fixture, inspector, outcome, closure


def _canonical_bytes(value):
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode()


def test_cp7_fixture_closure_evidence_surface_exists() -> None:
    assert materializer.build_contract_fixture_closure_evidence


@pytest.mark.asyncio
async def test_two_independent_fixture_materializations_are_byte_identical_and_nonvacuous(
    tmp_path,
):
    first = await _materialize_fixture(tmp_path / "run-a")
    second = await _materialize_fixture(tmp_path / "run-b")
    fixture_a, inspector_a, outcome_a, closure_a = first
    fixture_b, inspector_b, outcome_b, closure_b = second

    assert inspector_a.calls == inspector_b.calls == 1
    assert closure_a == closure_b
    assert _canonical_bytes(closure_a.to_payload()) == _canonical_bytes(
        closure_b.to_payload()
    )
    assert outcome_a.scorecard == outcome_b.scorecard
    assert outcome_a.accounting == outcome_b.accounting
    assert outcome_a.scorecard["attempts"] == 48
    assert outcome_a.scorecard["registered_total"] == 48
    assert outcome_a.scorecard["trial_accounting_hash"]
    assert outcome_a.scorecard["semantic_verdict"] == "NOT_EVALUATED"
    assert outcome_a.accounting.primary_attempts == 48
    assert outcome_a.accounting.total_attempts == 48
    assert outcome_a.accounting.retry_attempts == 0

    for name in ("scorecard.json", "scorecard.md"):
        bytes_a = fixture_a.output.joinpath(name).read_bytes()
        bytes_b = fixture_b.output.joinpath(name).read_bytes()
        assert bytes_a == bytes_b
        assert bytes_a
    assert sorted(path.name for path in fixture_a.output.iterdir()) == [
        "scorecard.json",
        "scorecard.md",
    ]
    assert not tuple(fixture_a.output.parent.glob(".materialized-pair.staging-*"))


@pytest.mark.asyncio
async def test_fixture_closure_labels_and_safety_counters_are_exact_zero(tmp_path):
    fixture, _inspector, outcome, closure = await _materialize_fixture(tmp_path / "run")
    payload = closure.to_payload()
    assert payload["actual_h4_contract"] == "NOT_EVALUATED"
    assert payload["actual_h5_contract"] == "NOT_EVALUATED"
    assert payload["production_identity"] == "DEFERRED_UNTIL_H4_SOURCE_PINS"
    assert payload["launchability"] == "NOT_LAUNCHABLE_CONTRACT_FIXTURE"
    assert set(payload["safety_counters"].values()) == {0}
    assert outcome.counters.db_inspect == outcome.counters.artifact_probe == 1
    assert outcome.counters.register == outcome.counters.record == 1
    assert outcome.counters.commit == outcome.counters.publish == 1
    assert outcome.counters.rollback == outcome.counters.delete == 0
    assert fixture.session_factory_calls == 1


@pytest.mark.asyncio
async def test_fixture_closure_rejects_type_coercion_and_semantic_mutants(tmp_path):
    fixture, _inspector, outcome, closure = await _materialize_fixture(tmp_path / "run")
    scorecard = dict(outcome.scorecard)
    scorecard["semantic_verdict"] = "PASS"
    mutants = (
        replace(outcome, disposition="REPLAY_NOOP"),
        replace(
            outcome,
            counters=replace(outcome.counters, register=True),
        ),
        replace(
            outcome,
            accounting=replace(outcome.accounting, registered_total=48.0),
        ),
        replace(outcome, scorecard=scorecard),
    )
    for mutant in mutants:
        with pytest.raises(materializer.H6BPlanError):
            materializer.build_contract_fixture_closure_evidence(
                plan=fixture.plan,
                outcome=mutant,
            )

    with pytest.raises(materializer.H6BPlanError):
        replace(closure, empirical_runs=False)


@pytest.mark.asyncio
async def test_fixture_closure_never_exposes_full_hash_or_run_id_claim(tmp_path):
    fixture, _inspector, _outcome, closure = await _materialize_fixture(
        tmp_path / "run"
    )
    closure_bytes = _canonical_bytes(closure.to_payload())
    plan_bytes_a = rob974_h6b_cli.render_plan_bytes(fixture.plan)
    plan_bytes_b = rob974_h6b_cli.render_plan_bytes(
        rob974_h6b_cli.build_contract_fixture_plan()
    )
    assert plan_bytes_a == plan_bytes_b
    assert fixture.plan._fixture_campaign_hash.encode() not in closure_bytes
    assert fixture.plan._fixture_run_id.encode() not in closure_bytes
    assert fixture.plan._fixture_campaign_hash.encode() not in plan_bytes_a
    assert fixture.plan._fixture_run_id.encode() not in plan_bytes_a
    assert b"NOT_LAUNCHABLE_CONTRACT_FIXTURE" in plan_bytes_a


@pytest.mark.asyncio
async def test_fixture_postaudit_is_deterministic_first_statement_read_only(tmp_path):
    first_root = tmp_path / "audit-a"
    second_root = tmp_path / "audit-b"
    first_root.mkdir()
    second_root.mkdir()
    first = AuditFixture(first_root)
    second = AuditFixture(second_root)
    first.write_pair()
    second.write_pair()
    outcome_a = await first.run()
    outcome_b = await second.run()
    assert outcome_a.exit_code == outcome_b.exit_code == 0
    assert outcome_a.seal == outcome_b.seal
    assert outcome_a.persisted_pair.canonical_json_bytes == (
        outcome_b.persisted_pair.canonical_json_bytes
    )
    assert outcome_a.persisted_pair.markdown_bytes == (
        outcome_b.persisted_pair.markdown_bytes
    )
    assert first.session.events[:3] == [
        "begin",
        "execute:SET TRANSACTION READ ONLY",
        "execute:SELECT canonical_raw_rows",
    ]
    assert outcome_a.counters.commit == outcome_a.counters.mutation == 0
    assert outcome_a.seal.experiments == outcome_a.seal.trials == 48
    assert outcome_a.seal.strategy_counts == (("S3", 24), ("S4", 24))


def test_exact_four_production_files_exist_without_symlinks_or_fifth_scope():
    research_files = {
        path.name
        for path in (_ROOT / "research/nautilus_scalping").glob("rob974_h6b_*.py")
    }
    assert research_files == _EXPECTED_RESEARCH_PRODUCTION
    production_paths = [
        _ROOT / "app/services/rob974_h6b_materializer.py",
        *[
            _ROOT / "research/nautilus_scalping" / name
            for name in sorted(_EXPECTED_RESEARCH_PRODUCTION)
        ],
    ]
    assert all(path.is_file() and not path.is_symlink() for path in production_paths)


def test_production_imports_use_only_actual_merged_h4_h5_and_no_forbidden_runtime_surface():
    paths = (
        _ROOT / "app/services/rob974_h6b_materializer.py",
        _ROOT / "research/nautilus_scalping/rob974_h6b_artifacts.py",
        _ROOT / "research/nautilus_scalping/rob974_h6b_cli.py",
        _ROOT / "research/nautilus_scalping/rob974_h6b_postaudit.py",
    )
    forbidden_import_roots = {
        "aiohttp",
        "httpx",
        "requests",
        "socket",
        "subprocess",
        "taskiq",
    }
    h4_h5_imports: set[str] = set()
    for path in paths:
        tree = ast.parse(path.read_text())
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.append(node.module)
        assert not {name.split(".", 1)[0] for name in imports} & forbidden_import_roots
        h4_h5_imports.update(
            name for name in imports if name.startswith(("rob974_h4", "rob974_h5"))
        )
        assert h4_h5_imports == {
            "rob974_h4_adapter",
            "rob974_h4_contracts",
            "rob974_h4_h6a_adapter",
            "rob974_h4_pbo",
            "rob974_h4_runner",
            "rob974_h4_selection",
            "rob974_h5_canonical",
            "rob974_h5_contracts",
            "rob974_h5_dual_evidence",
            "rob974_h5_gates",
            "rob974_h5_markdown",
            "rob974_h5_s3",
            "rob974_h5_s4",
        }


def test_transaction_and_sql_ast_ownership_is_closed():
    artifacts_source = (
        _ROOT / "research/nautilus_scalping/rob974_h6b_artifacts.py"
    ).read_text()
    cli_source = (_ROOT / "research/nautilus_scalping/rob974_h6b_cli.py").read_text()
    audit_source = (
        _ROOT / "research/nautilus_scalping/rob974_h6b_postaudit.py"
    ).read_text()
    for source in (artifacts_source, cli_source):
        tree = ast.parse(source)
        attrs = {
            node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
        }
        assert not attrs & {"begin", "commit", "rollback"}
    audit_tree = ast.parse(audit_source)
    audit_attrs = {
        node.attr for node in ast.walk(audit_tree) if isinstance(node, ast.Attribute)
    }
    assert "commit" not in audit_attrs
    assert {"begin", "rollback", "close"} <= audit_attrs
    upper = audit_source.upper()
    for forbidden in (
        "SELECT COUNT",
        "GROUP BY",
        "DELETE FROM",
        "TRUNCATE ",
        "CREATE TABLE",
        "ALTER TABLE",
        "DROP TABLE",
    ):
        assert forbidden not in upper


def test_guard_is_exact_48_after_cp10_literal_h6b_seal():
    guard = (
        _ROOT
        / "research/nautilus_scalping/tests/test_rob962_frozen_production_delta.py"
    )
    tree = ast.parse(guard.read_text())
    assignment = next(
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name)
            and target.id == "_AUTHORIZED_PRODUCTION_CHANGES"
            for target in node.targets
        )
    )
    authorized_changes = ast.literal_eval(assignment.value)
    assert len(authorized_changes) == 48
    h6b = {
        path
        for status, paths in authorized_changes
        for path in paths
        if status == "A" and "rob974_h6b_" in path
    }
    assert h6b == {
        "research/nautilus_scalping/rob974_h6b_artifacts.py",
        "research/nautilus_scalping/rob974_h6b_cli.py",
        "research/nautilus_scalping/rob974_h6b_postaudit.py",
    }


def test_import_origins_and_actual_dependency_origins_are_this_worktree():
    modules = (
        materializer,
        rob974_h6b_artifacts,
        rob974_h6b_cli,
        rob974_h6b_postaudit,
    )
    assert all(
        Path(module.__file__).resolve().is_relative_to(_ROOT) for module in modules
    )
    for dependency in ("rob974_h4_contracts", "rob974_h5_contracts"):
        spec = importlib.util.find_spec(dependency)
        assert spec is not None
        assert spec.origin is not None
        assert Path(spec.origin).resolve().is_relative_to(_ROOT)

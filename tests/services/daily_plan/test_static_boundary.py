"""Keep plan writes on the plan-only side of the service boundary."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
FORBIDDEN = (
    "app.services.brokers",
    "app.services.order_proposals",
    "app.services.mandate",
    "app.services.investment_watch",
    "app.services.execution_intent_gateway",
    "app.services.mechanical_loss_cut.job",
    "app.services.mechanical_loss_cut.store",
    "app.mcp_server",
    "app.tasks",
)

#: Read-only retro readers allowed to import the plan tables for SELECTs.
#: Each value names the test that proves the module issues no writes; the
#: write rule itself is unchanged — ``store.py`` stays the only writer, and
#: any other importer still fails unless it is declared here with a proof.
READ_ONLY_PLAN_TABLE_READERS = {
    "app/services/retro_v1/plan_source.py": (
        "test_guards.py::test_no_retro_module_calls_a_write_or_transaction_ending_method"
    ),
}


def _imports(module):
    for node in ast.walk(ast.parse(module.read_text())):
        if isinstance(node, ast.ImportFrom) and node.module:
            yield node.module
        elif isinstance(node, ast.Import):
            yield from (name.name for name in node.names)


def test_plan_service_cannot_reach_order_or_watch_writers():
    pending = list((ROOT / "app/services/daily_plan").glob("*.py"))
    checked = set()
    while pending:
        module = pending.pop()
        if module in checked:
            continue
        checked.add(module)
        for name in _imports(module):
            # The evaluator uses the pure parking-symbol constants. Follow this
            # module too so a later import of a writer still fails the guard.
            pure_helpers = {
                "app.services.order_proposals.parking_allowlist",
                "app.mcp_server.tick_size",
            }
            if name not in pure_helpers:
                assert not name.startswith(FORBIDDEN), (module.name, name)
            if name.startswith("app.services.") or name in pure_helpers:
                candidate = ROOT / (name.replace(".", "/") + ".py")
                if candidate.is_file():
                    pending.append(candidate)
    assert ROOT / "app/services/mechanical_loss_cut/rules.py" in checked


def test_only_plan_service_imports_plan_tables_for_writes():
    model_users = []
    for path in (ROOT / "app").rglob("*.py"):
        if "daily_plan" not in path.read_text():
            continue
        if "app.models.daily_plan" in _imports(path):
            model_users.append(path.relative_to(ROOT).as_posix())
    assert sorted(model_users) == sorted(
        ["app/services/daily_plan/store.py", *READ_ONLY_PLAN_TABLE_READERS]
    )


def test_every_declared_plan_table_reader_has_a_no_writes_proof():
    guards = (ROOT / "tests/services/retro_v1/test_guards.py").read_text()
    for reader, proof in READ_ONLY_PLAN_TABLE_READERS.items():
        module_name, test_name = proof.split("::", 1)
        assert module_name == "test_guards.py", proof
        assert f"def {test_name}(" in guards, proof
        # The proof scans ``app/services/retro_v1/*.py`` non-recursively, so a
        # reader declared here must live directly in that package.
        assert Path(reader).parent == Path("app/services/retro_v1"), reader

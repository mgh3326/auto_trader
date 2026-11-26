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
    assert model_users == ["app/services/daily_plan/store.py"]

from __future__ import annotations

import ast
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
SHADOW_DIR = REPO_ROOT / "app" / "services" / "buy_gate_ab_shadow"
MCP_FILES = (
    REPO_ROOT / "app" / "mcp_server" / "tooling" / "buy_gate_ab_shadow.py",
    REPO_ROOT / "app" / "mcp_server" / "tooling" / "buy_gate_ab_shadow_registration.py",
)
SHADOW_IMPORT_ALLOWED_FILES = MCP_FILES + (
    REPO_ROOT / "app" / "mcp_server" / "tooling" / "analysis_registration.py",
)
LIVE_PATH_FILES = (
    REPO_ROOT / "app" / "mcp_server" / "tooling" / "order_proposal_tools.py",
    REPO_ROOT / "app" / "mcp_server" / "tooling" / "order_execution.py",
    REPO_ROOT / "app" / "mcp_server" / "tooling" / "orders_registration.py",
    REPO_ROOT
    / "app"
    / "mcp_server"
    / "tooling"
    / "support_reserve_net_consumer_tool.py",
    REPO_ROOT / "app" / "mcp_server" / "tooling" / "buy_candidate_fanout.py",
    REPO_ROOT / "app" / "services" / "support_reserve_net_consumer.py",
)
FORBIDDEN_IMPORT_PREFIXES = (
    "app.core.database",
    "app.models",
    "app.services.brokers",
    "app.services.downside_watch",
    "app.services.investment_",
    "app.mcp_server.tooling.orders_",
    "app.mcp_server.tooling.order_proposal",
    "app.mcp_server.tooling.order_execution",
    "app.mcp_server.tooling.investment_",
    "app.services.support_reserve_net_consumer",
    "app.services.trading_policy_service",
    "app.core.taskiq_broker",
    "app.tasks",
    "app.flows",
    "sqlalchemy",
)
FORBIDDEN_NAME_SUBSTRINGS = (
    "place_order",
    "submit_order",
    "order_proposal",
    "watch_create",
    "taskiq",
    "prefect",
)


def _python_files() -> list[Path]:
    return sorted(SHADOW_DIR.glob("*.py")) + list(MCP_FILES)


def _imported_module_names(tree: ast.Module) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
            modules.update(f"{node.module}.{alias.name}" for alias in node.names)
    return modules


def test_shadow_package_has_no_broker_proposal_policy_or_scheduler_imports() -> None:
    for path in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        modules = _imported_module_names(tree)
        for module in modules:
            for prefix in FORBIDDEN_IMPORT_PREFIXES:
                assert not module.startswith(prefix), f"{path.name} imports {module!r}"
            lowered = module.lower()
            for needle in FORBIDDEN_NAME_SUBSTRINGS:
                assert needle not in lowered, f"{path.name} imports {module!r}"


def test_live_order_and_gate_modules_do_not_import_the_shadow_package() -> None:
    for path in REPO_ROOT.joinpath("app").rglob("*.py"):
        if path in SHADOW_IMPORT_ALLOWED_FILES or path.is_relative_to(SHADOW_DIR):
            continue
        assert path.is_file(), path
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        modules = _imported_module_names(tree)
        for module in modules:
            assert "buy_gate_ab_shadow" not in module, (
                f"{path.name} imports shadow module {module!r}"
            )


def test_live_policy_support_gate_wording_is_untouched() -> None:
    policy = yaml.safe_load(
        (REPO_ROOT / "config" / "trading_policy.yaml").read_text(encoding="utf-8")
    )
    screen = policy["thresholds"]["screen.support_within_pct"]
    assert screen["value"] == 8
    assert "strong support" in screen["semantics"]
    exception = policy["crash_day"]["actions"]["new_entry_hold_exception"]
    assert exception["requires"]["support_quality"] == "required"
    assert exception["requires"]["price_zone"] == "strong_support"
    assert exception["requires"]["gate_relaxation"] == "none"
    reserve = policy["decision_rules"]["buy.support_reserve_net"]
    assert reserve["support_strength_min"] == "moderate"


def test_no_scheduler_or_flow_files_were_added() -> None:
    stray = [
        path
        for directory in (
            REPO_ROOT / "app" / "tasks",
            REPO_ROOT / "app" / "flows",
        )
        for path in directory.rglob("*1301*")
    ]
    assert stray == []

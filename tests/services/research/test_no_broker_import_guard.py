"""ROB-846 AC#6 / ROB-946 §7 — the experiment registry and the ROB-946
campaign bridge must not import broker/order/fill/scheduler surfaces.

The immutable strategy experiment registry (and the ROB-946 app-side campaign
bridge + DB write guard built on top of it) is deterministic research
bookkeeping in the ``research`` schema only. It must never reach a broker,
order, fill ledger, or scheduler — neither to import one nor (transitively)
to write/schedule one. This static guard scans those modules and fails if a
forbidden surface is imported.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]

# The ROB-846 registry surface.
GUARDED_FILES: tuple[pathlib.Path, ...] = (
    REPO_ROOT / "app" / "services" / "strategy_experiment_registry.py",
    REPO_ROOT / "app" / "services" / "research_canonical_hash.py",
    REPO_ROOT / "app" / "services" / "research_offline_gate_service.py",
    REPO_ROOT / "app" / "models" / "research_backtest.py",
    REPO_ROOT / "app" / "schemas" / "research_backtest.py",
    # ROB-946 (H6) — app-side campaign bridge + DB write guard + schemas.
    REPO_ROOT / "app" / "services" / "research_campaign_bridge.py",
    REPO_ROOT / "app" / "services" / "research_db_write_guard.py",
    REPO_ROOT / "app" / "schemas" / "research_campaign_bridge.py",
    # ROB-944 (H4) — thin --run preflight/registration controller.
    REPO_ROOT / "app" / "services" / "rob944_campaign_controller.py",
    # ROB-981 (ROB-974 R2 H6-A) — exact-48 registration/attempt-batch bridge.
    REPO_ROOT / "app" / "services" / "rob974_h6a_bridge.py",
    *sorted((REPO_ROOT / "research_contracts").glob("*.py")),
)

# Module import prefixes that indicate a broker/order/fill surface.
FORBIDDEN_MODULE_PREFIXES: tuple[str, ...] = (
    "app.services.brokers",
    "app.services.kis",
    "app.services.upbit",
    "app.services.order_service",
    "app.services.execution_event",
    "app.services.fill_notification",
    "app.services.alpaca_paper_ledger_service",
    "app.services.toss_live_order_ledger_service",
    "app.models.paper_trading",
    "app.models.order_proposals",
    "app.models.review",
    "app.mcp_server.tooling.orders",
    "app.monitoring.trade_notifier",
)

# Substrings that indicate an order/fill ledger or scheduler regardless of
# package path.
FORBIDDEN_MODULE_SUBSTRINGS: tuple[str, ...] = (
    "order_ledger",
    "_ledger",
    "broker",
    "order_intent",
    "trade_notifier",
    # ROB-946 §7 — no scheduler wiring from research bookkeeping.
    "taskiq",
    "celery",
    "prefect",
    "apscheduler",
)


def _imported_modules(tree: ast.AST) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
            for alias in node.names:
                modules.add(f"{node.module}.{alias.name}")
    return modules


def _is_forbidden(module: str) -> bool:
    if any(
        module == p or module.startswith(p + ".") for p in FORBIDDEN_MODULE_PREFIXES
    ):
        return True
    return any(token in module for token in FORBIDDEN_MODULE_SUBSTRINGS)


def test_registry_files_exist() -> None:
    missing = [str(path) for path in GUARDED_FILES if not path.exists()]
    assert not missing, f"guarded registry files missing: {missing}"


@pytest.mark.parametrize("path", GUARDED_FILES, ids=lambda p: p.name)
def test_registry_module_has_no_broker_or_ledger_import(path: pathlib.Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    offending = sorted(m for m in _imported_modules(tree) if _is_forbidden(m))
    assert not offending, (
        f"{path.relative_to(REPO_ROOT)} imports forbidden broker/order/fill "
        f"surface(s): {offending}"
    )

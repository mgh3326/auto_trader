"""Frozen actual-registration inventories, initially 14 profiles / 228 tools.

The JSON is reviewed data, never recalculated from runtime allowlists during
tests. Update only the changed profile when an intentional surface change lands.
The initial commit retains the full audit baseline in Git history.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from app.mcp_server.profiles import McpProfile
from tests.mcp_server._registration_recorder import collect_profile_tools

pytestmark = pytest.mark.unit
SNAPSHOT_PATH = Path(__file__).with_name("profile_tool_snapshot.json")


@pytest.mark.parametrize("gates_enabled", [True, False], ids=["gates-on", "gates-off"])
def test_profile_tool_snapshot(
    monkeypatch: pytest.MonkeyPatch, gates_enabled: bool
) -> None:
    assert SNAPSHOT_PATH.is_file(), "profile tool snapshot is missing"
    snapshot = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    key = "gates_enabled" if gates_enabled else "gates_disabled"
    expected = snapshot[key]
    actual = collect_profile_tools(monkeypatch, gates_enabled=gates_enabled)
    profiles = {profile.value for profile in McpProfile}
    assert set(expected) == profiles, "snapshot must cover every MCP profile exactly"
    assert set(actual) == profiles, "registrar inventory omitted an MCP profile"
    for profile in sorted(profiles):
        assert expected[profile] == sorted(set(expected[profile])), (
            f"{profile}: snapshot must contain sorted, unique tool names"
        )
        missing = sorted(set(expected[profile]) - set(actual[profile]))
        added = sorted(set(actual[profile]) - set(expected[profile]))
        assert actual[profile] == expected[profile], (
            f"{profile} ({key}): MCP registration snapshot changed; "
            f"missing={missing}, added={added}"
        )


# Explicit PROMPT defaults: lane conflicts and mixed regression contracts.
_RETAINED_DEAD_TOOL_NAMES = {
    "get_dividends",
    "get_financials",
    "get_insider_transactions",
    "get_investor_trends",
    "get_market_reports",
    "get_sector_peers",
    "get_short_interest",
    "get_trading_scoreboard",
    "get_user_setting",
    "research_summary_get",
    "set_user_setting",
    "stage_analysis_get",
    "update_manual_holdings",
    "paper_cancel_pending_order",
    "investment_report_prepare_intraday_context",
    "investment_watch_void",
    "investment_watch_expire",
    "investment_report_set_status",
    "order_proposal_expire_sweep",
    "analyze_portfolio",
    "delete_paper_account",
    "sweep_expired_watches",
    "reset_paper_account",
    "investment_report_create_from_hermes_composition",
    "investment_report_update",
    "update_trade_journal",
    "investment_report_add_items",
    "alpaca_paper_reconcile_orders",
    "get_toss_ai_signal",
    "get_retrospective_aggregate",
    "investment_report_decide_item",
    "save_trade_journal",
    "create_paper_account",
    "list_active_journals",
    "order_proposal_redispatch",
    "investment_report_delta_get",
    "alpaca_paper_automated_preview_order",
    "investment_report_list",
    "investment_report_activate_watch",
    "investment_watch_recommend",
    "investment_report_context_get",
    "get_analyst_consensus",
    "get_toss_buy_balance",
    "save_position_intake_retrospective",
}

_DELETED_MODULE_PATHS = (
    "app/mcp_server/tooling/analysis_bundle_handlers.py",
    "app/mcp_server/tooling/paper_analytics_registration.py",
    "app/mcp_server/tooling/paper_execution_registration.py",
    "app/mcp_server/tooling/paper_journal_bridge.py",
    "app/mcp_server/tooling/paper_journal_registration.py",
    "app/mcp_server/tooling/paper_validation_registration.py",
)


def test_remaining_surface_matches_audit_and_reviewed_exceptions(monkeypatch):
    root = Path(__file__).resolve().parents[2]
    audit = (root / "docs/mcp-tool-usage-audit-20260903.md").read_text()
    table = audit.split("## Complete classification\n", 1)[1].split("\n## ", 1)[0]
    expected = {profile.value: set() for profile in McpProfile}
    for line in table.splitlines():
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) != 10 or cells[4] not in {"A", "B", "C", "D", "U"}:
            continue
        tool, profiles, classification = cells[0], cells[1].split(", "), cells[4]
        if classification == "D" and tool not in _RETAINED_DEAD_TOOL_NAMES:
            continue
        for profile in profiles:
            if profile in expected:
                expected[profile].add(tool)
    actual = collect_profile_tools(monkeypatch, gates_enabled=True)
    assert {profile: set(names) for profile, names in actual.items()} == expected, (
        "surface must preserve A/B/C/U and only the reviewed D exceptions"
    )


def test_orphaned_modules_and_runtime_imports_stay_absent():
    root = Path(__file__).resolve().parents[2]
    existing = [path for path in _DELETED_MODULE_PATHS if (root / path).exists()]
    assert not existing, f"deleted MCP handler modules returned: {existing}"
    deleted = {
        path.removesuffix(".py").replace("/", ".") for path in _DELETED_MODULE_PATHS
    }
    violations = []
    for path in (root / "app").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
            imports = []
            if isinstance(node, ast.Import):
                imports = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports = [node.module] + [
                    f"{node.module}.{alias.name}" for alias in node.names
                ]
            for module in imports:
                if module in deleted:
                    violations.append(
                        f"{path.relative_to(root)}:{node.lineno}: {module}"
                    )
    assert not violations, f"deleted MCP handler import restored: {violations}"

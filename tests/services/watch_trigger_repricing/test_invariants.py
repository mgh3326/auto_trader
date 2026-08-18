"""ROB-1286 §6 / AC7 — the invariants, enforced statically.

This flow's whole risk is that it manufactures proposals faster than a
human does, and a proposal can reach the §40/51차 auto-approve lane. So
the guarantee cannot be "we didn't mean to touch the approval gates"; it
has to be that the package structurally cannot.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
PACKAGE = REPO_ROOT / "app" / "services" / "watch_trigger_repricing"
FLOW = REPO_ROOT / "app" / "flows" / "watch_trigger_repricing_flow.py"

PACKAGE_FILES = sorted(PACKAGE.glob("*.py"))
ALL_FILES = PACKAGE_FILES + [FLOW]

# Anything that could submit, approve, cancel or size a real order, or
# mutate a watch. Substring match on the dotted import path.
FORBIDDEN_IMPORT_FRAGMENTS = (
    "app.services.brokers",
    "app.services.order_proposals",
    "app.services.kis_trading_service",
    "app.services.upbit",
    "app.mcp_server.tooling.orders",
    "app.mcp_server.tooling.order_proposal_tools",
    "app.services.investment_reports.repository",
    "app.services.trade_journal",
)

# The approval machinery's own switches. Naming any of them here -- even
# to read one -- would mean this package participates in that decision.
FORBIDDEN_SETTING_NAMES = (
    "ORDER_PROPOSALS_AUTO_APPROVE",
    "ORDER_PROPOSALS_AUTO_APPROVE_MODE",
    "ORDER_PROPOSALS_TOSS_LIVE_VETO_ENABLED",
    "ORDER_PROPOSALS_TELEGRAM_ENABLED",
    "ORDER_PROPOSALS_ENABLED",
)


def _trees() -> list[tuple[pathlib.Path, ast.Module]]:
    return [(p, ast.parse(p.read_text())) for p in ALL_FILES]


def test_package_files_are_present() -> None:
    assert {p.name for p in PACKAGE_FILES} == {
        "__init__.py",
        "claims.py",
        "consumption.py",
        "gate.py",
        "orchestrator.py",
        "selection.py",
        "spawn.py",
    }


def test_no_broker_order_or_approval_imports() -> None:
    offenders: list[str] = []
    for path, tree in _trees():
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                if any(f in name for f in FORBIDDEN_IMPORT_FRAGMENTS):
                    offenders.append(f"{path.name}:{node.lineno} {name}")
    assert offenders == []


def test_approval_gate_settings_are_never_referenced() -> None:
    """Includes string literals, so a getattr() bypass is caught too."""
    offenders: list[str] = []
    for path, tree in _trees():
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_SETTING_NAMES:
                offenders.append(f"{path.name}:{node.lineno} attr {node.attr}")
            elif isinstance(node, ast.Name) and node.id in FORBIDDEN_SETTING_NAMES:
                offenders.append(f"{path.name}:{node.lineno} name {node.id}")
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value in FORBIDDEN_SETTING_NAMES:
                    offenders.append(f"{path.name}:{node.lineno} literal {node.value}")
    assert offenders == []


def test_execution_boundary_is_order_proposal_create_and_no_further() -> None:
    """The named boundary, and nothing downstream of it, appears anywhere."""
    from app.services.watch_trigger_repricing.spawn import EXECUTION_BOUNDARY

    assert EXECUTION_BOUNDARY == "order_proposal_create"

    beyond = (
        "order_proposal_approve",
        "order_proposal_submit",
        "place_order",
        "submit_order",
        "kis_live_place_order",
        "toss_place_order",
        "record_auto_approval",
        "revalidate_and_submit",
    )
    offenders = [
        f"{p.name}: {token}"
        for p in ALL_FILES
        for token in beyond
        if token in p.read_text()
    ]
    assert offenders == []


def test_market_scope_stays_kr() -> None:
    """ROB-1286: US/crypto expansion is explicitly a separate issue."""
    from app.services.watch_trigger_repricing.claims import InMemoryClaimStore
    from app.services.watch_trigger_repricing.selection import select_candidates

    from .conftest import INCIDENT_TICK, make_event

    result = select_candidates(
        [
            make_event(event_uuid="evt-us", symbol="AAPL", market="us"),
            make_event(event_uuid="evt-crypto", symbol="KRW-BTC", market="crypto"),
            make_event(event_uuid="evt-kr", symbol="005930", market="kr"),
        ],
        store=InMemoryClaimStore(),
        now=INCIDENT_TICK,
    )

    assert [e.event_uuid for e in result.selected] == ["evt-kr"]
    assert {s.reason for s in result.skipped} == {"market_out_of_scope"}


def test_flow_registers_no_deployment_or_schedule() -> None:
    """The invariant is one new recurring job, registered by an operator."""
    source = FLOW.read_text()
    for token in (
        "Deployment",
        ".serve(",
        ".deploy(",
        "CronSchedule",
        "IntervalSchedule",
        "cron=",
        "interval=",
        "schedule=",
    ):
        assert token not in source, f"{token} found in {FLOW.name}"


def test_flow_is_a_shell_over_the_gated_entrypoint() -> None:
    """Logic in the flow file would be logic no test can import."""
    tree = ast.parse(FLOW.read_text())
    functions = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
    ]
    assert [f.name for f in functions] == ["watch_trigger_repricing_flow"]


def test_this_change_adds_exactly_one_flow() -> None:
    """No second recurring job smuggled in alongside.

    Filesystem-based on purpose: a ``git diff origin/main...HEAD`` check
    passes locally and fails on CI's shallow clone, which has bitten this
    program before.
    """
    flows_dir = REPO_ROOT / "app" / "flows"
    referencing = [
        p.name
        for p in sorted(flows_dir.glob("*.py"))
        if "watch_trigger_repricing" in p.read_text()
    ]
    assert referencing == ["watch_trigger_repricing_flow.py"]


def test_no_migration_defines_a_consumption_marker() -> None:
    """§6: a consumption-marking column is approval-gated, not assumed."""
    versions = REPO_ROOT / "alembic" / "versions"
    offenders = [
        p.name
        for p in versions.glob("*.py")
        if "watch_trigger_repricing" in p.read_text()
        or "watch_event_repricing_claim" in p.read_text()
    ]
    assert offenders == []


def test_settings_gate_defaults_off() -> None:
    from app.core.config import Settings

    assert Settings.model_fields["WATCH_TRIGGER_REPRICING_ENABLED"].default is False

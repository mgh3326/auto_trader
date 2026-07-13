from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.services.paper_validation.contracts import (
    HypothesisDraftInput,
    PostmortemReviewInput,
    TransitionRequest,
)

REPO = Path(__file__).resolve().parents[3]
REGISTRATION = REPO / "app/mcp_server/tooling/paper_validation_registration.py"
HANDLERS = REPO / "app/mcp_server/tooling/paper_validation_handlers.py"
SOURCES = [
    *sorted((REPO / "app/services/paper_validation").glob("*.py")),
    REGISTRATION,
    *([HANDLERS] if HANDLERS.exists() else []),
]
FORBIDDEN_IMPORT_FRAGMENTS = {
    "app.services.order_proposals",
    "app.services.kis_trading_service",
    "app.services.paper_trading_service",
    "app.services.alpaca_paper_order_application",
    "app.services.order_send_intent_service",
    "app.services.brokers.alpaca",
    "app.services.brokers.binance",
    "app.services.brokers.kiwoom",
    "app.services.brokers.paper",
    "app.services.brokers.toss",
    "app.services.brokers.upbit",
    "app.mcp_server.tooling.alpaca_paper_orders",
    "app.mcp_server.tooling.order_execution",
    "app.mcp_server.tooling.order_proposal_tools",
    "app.mcp_server.tooling.orders",
    "app.mcp_server.tooling.paper_order_handler",
    "canonical_market_snapshot",
}
FORBIDDEN_CALLS = {
    "submit",
    "submit_order",
    "execute_order",
    "place_order",
    "place_buy_order",
    "place_sell_order",
    "place_market_buy_order",
    "place_market_sell_order",
    "place_limit_order",
    "modify_order",
    "cancel_order",
    "create_order_proposal",
    "approve_order_proposal",
    "mutate_order_proposal",
}
FORBIDDEN_ROB849_TYPES = {
    "CanonicalMarketSnapshot",
    "ConcreteExperimentProvenanceVerifier",
}


def _qualified_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _qualified_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def test_validation_boundary_has_no_live_proposal_or_broker_mutation_imports() -> None:
    violations: list[str] = []
    for path in SOURCES:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
            for module in modules:
                if any(fragment in module for fragment in FORBIDDEN_IMPORT_FRAGMENTS):
                    violations.append(f"{path.name}:{node.lineno}: import {module}")
    assert violations == []


def test_validation_boundary_never_calls_order_or_proposal_mutations() -> None:
    violations: list[str] = []
    for path in SOURCES:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _qualified_name(node.func).rsplit(".", maxsplit=1)[-1]
            if name in FORBIDDEN_CALLS:
                violations.append(f"{path.name}:{node.lineno}: call {name}")
    assert violations == []


def test_rob849_concrete_types_and_llm_gate_payloads_are_absent() -> None:
    source = "\n".join(path.read_text(encoding="utf-8") for path in SOURCES)

    assert FORBIDDEN_ROB849_TYPES.isdisjoint(source.split())
    assert "active_strategy_payload" not in source
    assert "gate_results" not in source
    for caller_payload in (
        HypothesisDraftInput,
        PostmortemReviewInput,
        TransitionRequest,
    ):
        assert "resolved_negative_class_count" not in caller_payload.model_fields


def test_migration_descends_from_starting_head_and_defines_db_triggers() -> None:
    source = (REPO / "alembic/versions/20260713_rob848_paper_validation.py").read_text(
        encoding="utf-8"
    )

    assert 'revision = "20260713_rob848_paper_validation"' in source
    assert 'down_revision = "20260713_rob866_manual_alerts"' in source
    assert "reject_paper_validation_audit_mutation" in source
    assert "validate_paper_validation_experiment_identity" in source


def test_migration_check_names_bypass_double_prefix_convention() -> None:
    path = REPO / "alembic/versions/20260713_rob848_paper_validation.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    check_names: list[ast.AST] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if _qualified_name(node.func) != "sa.CheckConstraint":
            continue
        check_names.extend(
            keyword.value for keyword in node.keywords if keyword.arg == "name"
        )

    assert check_names
    assert all(
        isinstance(name, ast.Call) and _qualified_name(name.func) == "op.f"
        for name in check_names
    )


def test_registration_keeps_application_logic_in_handlers_module() -> None:
    assert HANDLERS.is_file(), "paper validation handler module is missing"

    registration = ast.parse(
        REGISTRATION.read_text(encoding="utf-8"), filename=str(REGISTRATION)
    )
    class_names = {
        node.name for node in registration.body if isinstance(node, ast.ClassDef)
    }
    imports = {
        node.module
        for node in registration.body
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }

    assert class_names == set()
    assert "app.mcp_server.tooling.paper_validation_handlers" in imports


@pytest.mark.parametrize(
    "reason_code",
    [
        "promotion_confirmation_required",
        "promotion_gate_blocked",
        "calibration_gate_blocked",
        "authorization_identity_mismatch",
        "order_state_not_authorized",
    ],
)
def test_closed_failure_vocabulary_lists_every_runtime_reason(
    reason_code: str,
) -> None:
    spec = (
        REPO / "docs/superpowers/specs/2026-07-13-rob-848-paper-validation-design.md"
    ).read_text(encoding="utf-8")

    assert f"`{reason_code}`" in spec

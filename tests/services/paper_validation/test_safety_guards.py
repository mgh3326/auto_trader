from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SOURCES = [
    *sorted((REPO / "app/services/paper_validation").glob("*.py")),
    REPO / "app/mcp_server/tooling/paper_validation_registration.py",
]
FORBIDDEN_IMPORT_FRAGMENTS = {
    "app.services.order_proposals",
    "app.services.brokers.alpaca",
    "app.services.brokers.binance",
    "app.services.brokers.paper",
    "app.mcp_server.tooling.orders",
    "canonical_market_snapshot",
}
FORBIDDEN_CALLS = {
    "submit_order",
    "place_order",
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
    assert "resolved_negative_class" not in source

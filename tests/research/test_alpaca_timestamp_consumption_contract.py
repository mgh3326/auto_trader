"""Static guard for the legacy Alpaca ledger timestamp contract.

Coverage boundary: this AST guard detects statically visible arithmetic,
non-identity comparisons, latency/SLA/clock-like helper calls, and
latency-like assignments involving ``submitted_at`` or ``canceled_at``.
It intentionally does not parse SQL, evaluate reflection/dynamic attribute
access, inspect runtime values, or claim to make every indirect use
structurally impossible. State checks and serialization are allowed because
the columns remain legacy fields used for compatibility and lifecycle reads.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = REPOSITORY_ROOT / "app"
TARGET_FIELDS = frozenset({"submitted_at", "canceled_at"})
SUSPICIOUS_NAME = re.compile(
    r"(?:latency|sla|clock|elapsed|duration|queue|receipt|t0|age|lag|skew|"
    r"delta|timeout|compare|comparison|difference|diff)",
    re.IGNORECASE,
)


def _attribute_is_target(node: ast.AST) -> bool:
    return isinstance(node, ast.Attribute) and node.attr in TARGET_FIELDS


def _call_name(node: ast.Call) -> str:
    function = node.func
    if isinstance(function, ast.Name):
        return function.id
    if isinstance(function, ast.Attribute):
        return function.attr
    return ""


def _target_name(node: ast.Assign | ast.AnnAssign) -> str:
    target = node.targets[0] if isinstance(node, ast.Assign) else node.target
    if isinstance(target, ast.Name):
        return target.id
    return ""


def find_forbidden_consumption(source: str, *, filename: str = "<source>") -> list[str]:
    tree = ast.parse(source, filename=filename)
    parents = {
        id(child): parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }
    violations: list[str] = []

    for node in ast.walk(tree):
        if not _attribute_is_target(node):
            continue
        current: ast.AST | None = node
        while current is not None:
            parent = parents.get(id(current))
            if isinstance(parent, ast.BinOp):
                violations.append(
                    f"{filename}:{node.lineno}: {node.attr} used in timestamp arithmetic"
                )
                break
            if isinstance(parent, ast.Compare) and not all(
                isinstance(op, (ast.Is, ast.IsNot)) for op in parent.ops
            ):
                violations.append(
                    f"{filename}:{node.lineno}: {node.attr} used in clock comparison"
                )
                break
            if isinstance(parent, ast.Call) and SUSPICIOUS_NAME.search(
                _call_name(parent)
            ):
                violations.append(
                    f"{filename}:{node.lineno}: {node.attr} passed to clock-like helper"
                )
                break
            if isinstance(
                parent, (ast.Assign, ast.AnnAssign)
            ) and SUSPICIOUS_NAME.search(_target_name(parent)):
                violations.append(
                    f"{filename}:{node.lineno}: {node.attr} assigned to clock-like name"
                )
                break
            current = parent
    return violations


def find_repository_violations(paths: Iterable[Path]) -> list[str]:
    violations: list[str] = []
    for path in sorted(paths):
        violations.extend(
            find_forbidden_consumption(
                path.read_text(encoding="utf-8"), filename=str(path)
            )
        )
    return violations


def test_app_has_no_forbidden_legacy_timestamp_consumption() -> None:
    violations = find_repository_violations(APP_ROOT.rglob("*.py"))
    assert not violations, (
        "forbidden legacy Alpaca timestamp consumption found:\n" + "\n".join(violations)
    )


@pytest.mark.parametrize(
    "source",
    (
        "latency = row.submitted_at - row.created_at",
        "if row.canceled_at >= deadline: pass",
        "measure_queue_latency(row.submitted_at)",
        "clock_delta = row.canceled_at",
    ),
)
def test_guard_rejects_new_clock_consumers(source: str) -> None:
    assert find_forbidden_consumption(source, filename="new_consumer.py")


def test_guard_allows_legacy_state_and_serialization_reads() -> None:
    source = """
if row.submitted_at is not None:
    payload[\"submitted_at\"] = row.submitted_at.isoformat()
if row.canceled_at is None:
    payload[\"canceled_at\"] = None
"""
    assert find_forbidden_consumption(source) == []


__all__ = ["find_forbidden_consumption", "find_repository_violations"]

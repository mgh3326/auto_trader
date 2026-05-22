"""ROB-298 — Static import guard.

No production code in ``app/`` may import from
``app.services.brokers.binance.testnet`` or ``app.services.scalping``
(both deleted in ROB-298 PR 1). Tests under ``tests/`` may not either
(no stale dead imports).

Scripts under ``scripts/`` are checked separately.
"""

from __future__ import annotations

import ast
import pathlib

_BANNED_PREFIXES = (
    "app.services.brokers.binance.testnet",
    "app.services.scalping",
)


def _scan(roots: list[pathlib.Path]) -> list[str]:
    offenders: list[str] = []
    for root in roots:
        for py in root.rglob("*.py"):
            try:
                tree = ast.parse(py.read_text())
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    if node.module and any(
                        node.module.startswith(p) for p in _BANNED_PREFIXES
                    ):
                        offenders.append(f"{py}: from {node.module} import ...")
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        if any(alias.name.startswith(p) for p in _BANNED_PREFIXES):
                            offenders.append(f"{py}: import {alias.name}")
    return offenders


def test_no_testnet_imports_in_app() -> None:
    offenders = _scan([pathlib.Path("app")])
    assert not offenders, (
        "ROB-298 forbids imports from "
        f"{_BANNED_PREFIXES}. Offenders:\n" + "\n".join(offenders)
    )


def test_no_testnet_imports_in_scripts() -> None:
    offenders = _scan([pathlib.Path("scripts")])
    assert not offenders, (
        "ROB-298 forbids imports from "
        f"{_BANNED_PREFIXES}. Offenders:\n" + "\n".join(offenders)
    )


def test_no_testnet_imports_in_tests() -> None:
    offenders = _scan([pathlib.Path("tests")])
    assert not offenders, (
        "ROB-298 forbids imports from "
        f"{_BANNED_PREFIXES}. Offenders:\n" + "\n".join(offenders)
    )

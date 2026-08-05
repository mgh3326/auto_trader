from __future__ import annotations

import ast
from datetime import date
from pathlib import Path

import pytest

from research.crypto_stage_b.contracts import (
    CryptoStageBRunContract,
    ExplorationWindowError,
    VenueCostLiteral,
)
from research.crypto_stage_b.tests.conftest import candidate, cost


def test_cost_literals_are_explicit_and_exactly_frozen() -> None:
    assert cost("upbit_krw").round_trip_bp == 30
    assert cost("upbit_krw").sensitivity_round_trip_bp == 70
    assert cost("binance_usdt_spot").round_trip_bp == 40
    assert cost("binance_usdt_spot").sensitivity_round_trip_bp == 80
    with pytest.raises(ValueError, match="does not inject defaults"):
        VenueCostLiteral(
            venue="upbit_krw",
            fee_bp_per_side=5,
            slippage_bp_per_side=11,
            sensitivity_slippage_bp_per_side=30,
        )


def test_contract_refuses_holdout_intersection_before_source_reads() -> None:
    with pytest.raises(ExplorationWindowError, match="holdout"):
        CryptoStageBRunContract(
            candidate=candidate("CR-SPOT-ETR-01"),
            venue="upbit_krw",
            exploration_start=date(2024, 12, 30),
            exploration_end=date(2025, 1, 1),
            cost=cost("upbit_krw"),
        )


def test_new_crypto_module_has_no_kr_shadow_or_runtime_import_path() -> None:
    root = Path(__file__).parents[1]
    forbidden_prefixes = (
        "app",
        "research.kr_corpus",
        "research.three_market_shadow",
    )
    violations: list[str] = []
    for path in sorted(root.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports = [name.name for name in node.names]
            elif isinstance(node, ast.ImportFrom):
                imports = [node.module or ""]
            else:
                continue
            for imported in imports:
                if imported.startswith(forbidden_prefixes):
                    violations.append(f"{path.name}:{imported}")
    assert violations == []

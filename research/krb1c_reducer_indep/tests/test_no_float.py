"""§6 evidence: the reducer contains no float / Decimal arithmetic at all.

Two independent guards:

1. A static AST scan of every module in the package (excluding this test
   directory) for float literals, ``float(...)`` / ``complex(...)`` calls,
   ``Decimal`` usage, ``math`` / ``decimal`` / ``numpy`` imports, and the true
   division operator applied outside of ``Fraction`` construction is *allowed*
   (Fraction / Fraction stays exact) but ``//`` on floats cannot arise because
   no float can exist in the first place.

2. A dynamic scan asserting that every value produced by a full reduction is
   an ``int`` or a ``Fraction`` — never a ``float``.
"""

from __future__ import annotations

import ast
import pathlib
from fractions import Fraction

import pytest

PACKAGE_DIR = pathlib.Path(__file__).resolve().parent.parent

FORBIDDEN_MODULES = {"math", "decimal", "numpy", "cmath", "statistics"}
FORBIDDEN_NAMES = {"float", "complex", "Decimal"}


def package_sources() -> list[pathlib.Path]:
    return sorted(
        path
        for path in PACKAGE_DIR.rglob("*.py")
        if "tests" not in path.relative_to(PACKAGE_DIR).parts
    )


def test_package_has_sources() -> None:
    assert package_sources(), "no sources found — guard would vacuously pass"


@pytest.mark.parametrize("path", package_sources(), ids=lambda p: p.name)
def test_source_has_no_float_or_decimal(path: pathlib.Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    problems: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, (float, complex)):
            problems.append(
                f"{path.name}:{node.lineno} float/complex literal {node.value!r}"
            )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in FORBIDDEN_MODULES:
                    problems.append(f"{path.name}:{node.lineno} import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root in FORBIDDEN_MODULES:
                problems.append(
                    f"{path.name}:{node.lineno} from {node.module} import ..."
                )
            for alias in node.names:
                if alias.name in FORBIDDEN_NAMES:
                    problems.append(f"{path.name}:{node.lineno} imports {alias.name}")
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            # `float` inside an isinstance-style guard would still be a Name;
            # the package uses none, so any occurrence is reported.
            problems.append(f"{path.name}:{node.lineno} name {node.id!r}")
        elif isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_NAMES:
            problems.append(f"{path.name}:{node.lineno} attribute {node.attr!r}")

    assert not problems, "float/Decimal usage detected:\n" + "\n".join(problems)


def test_reduction_values_are_all_exact() -> None:
    from research.krb1c_reducer_indep.cli import TABLES
    from research.krb1c_reducer_indep.reducer import reduce_c_stress_cap
    from research.krb1c_reducer_indep.sealed_input import sealed_cost_inputs

    result = reduce_c_stress_cap(TABLES, sealed_cost_inputs())

    exact = (int, Fraction)
    assert isinstance(result.c_raw, Fraction)
    assert isinstance(result.c_stress_cap, Fraction)
    assert isinstance(result.c_stress_cap_bp, int)
    assert not isinstance(result.c_stress_cap_bp, bool)

    for market_result in result.markets.values():
        assert isinstance(market_result.c_raw, Fraction)
        assert isinstance(market_result.witness_price, int)
        for row in market_result.candidates:
            for value in (
                row.price,
                row.tick_at_price,
                row.rho_entry,
                row.exit_witness_q,
                row.tick_at_exit_witness,
                row.rho_exit,
                row.entry_multiplier,
                row.exit_multiplier_cap,
                row.c,
            ):
                assert isinstance(value, exact), f"{value!r} is not exact"
                assert not isinstance(value, bool)
        for check in market_result.target_checks:
            for value in (check.target, check.lhs, check.rhs):
                assert isinstance(value, exact), f"{value!r} is not exact"

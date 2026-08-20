"""JIT funding disposition — deposit amount is the shortfall, never the declaration."""

from __future__ import annotations

import ast
from decimal import Decimal
from pathlib import Path

import pytest

from app.services.funding_advisory.jit import (
    build_jit_funding,
    declared_cover,
)

ROOT = Path(__file__).resolve().parents[3]
JIT_MODULE = ROOT / "app/services/funding_advisory/jit.py"


def jit(*, shortfall: str, declared: str, gap: str = "90000") -> dict:
    return build_jit_funding(
        shortfall=Decimal(shortfall),
        operational_gap=Decimal(gap),
        currency="KRW",
        declared_total=Decimal(declared),
    )


@pytest.mark.unit
def test_deposit_amount_is_the_candidate_shortfall_not_the_declared_total() -> None:
    result = jit(shortfall="60000", declared="640000")

    condition = result["condition"]
    assert condition["deposit_amount"] == "60000"
    assert condition["deposit_amount_basis"] == "candidate_shortfall"
    assert condition["declared_total_disclosure_only"] == "640000"


@pytest.mark.unit
@pytest.mark.parametrize(
    "declared", ["0", "1", "59999", "60000", "60001", "640000", "999999999999"]
)
def test_deposit_amount_is_invariant_under_every_declared_total(declared: str) -> None:
    """A larger or smaller declaration can never move X."""

    baseline = jit(shortfall="60000", declared="0")["condition"]["deposit_amount"]

    assert (
        jit(shortfall="60000", declared=declared)["condition"]["deposit_amount"]
        == baseline
        == "60000"
    )


@pytest.mark.unit
def test_operational_gap_is_disclosed_separately_and_is_not_the_deposit_amount() -> (
    None
):
    condition = jit(shortfall="60000", declared="640000", gap="90000")["condition"]

    assert condition["deposit_amount"] == "60000"
    assert condition["operational_gap_amount"] == "90000"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("shortfall", "declared", "expected"),
    [
        ("60000", "640000", "sufficient"),
        ("60000", "60000", "sufficient"),
        ("60000", "59999", "partial"),
        ("60000", "1", "partial"),
        ("60000", "0", "none"),
    ],
)
def test_declared_cover_is_display_classification_only(
    shortfall: str, declared: str, expected: str
) -> None:
    assert (
        declared_cover(shortfall=Decimal(shortfall), declared_total=Decimal(declared))
        == expected
    )
    assert (
        jit(shortfall=shortfall, declared=declared)["condition"]["declared_cover"]
        == expected
    )


@pytest.mark.unit
def test_cash_short_candidate_is_deferred_with_condition_and_never_rejected() -> None:
    result = jit(shortfall="60000", declared="640000")

    assert result["disposition"] == "deferred_with_condition"
    assert result["rejected_for_insufficient_cash"] is False
    assert result["next_step"] == "operator_deposit_then_reevaluate"
    assert result["condition"]["kind"] == "operator_deposit_to_target_account"
    assert (
        result["condition"]["satisfied_by"]
        == "target_broker_buying_power_reobservation"
    )


@pytest.mark.unit
def test_declared_cash_is_never_counted_toward_buying_power() -> None:
    for declared in ("0", "640000"):
        result = jit(shortfall="60000", declared=declared)
        assert result["declared_cash_counted_toward_buying_power"] is False
        assert result["declared_cash_is_display_evidence_only"] is True


@pytest.mark.unit
def test_no_shortfall_is_fundable_now_with_no_condition_and_no_proposal() -> None:
    result = jit(shortfall="0", declared="640000", gap="0")

    assert result["disposition"] == "fundable_now"
    assert result["condition"] is None
    assert result["next_step"] == "existing_proposal_creation_and_approval_path"
    assert result["creates_proposal"] is False
    assert result["executes_money_movement"] is False


@pytest.mark.unit
def test_jit_module_is_pure_and_imports_no_db_broker_or_order_surface() -> None:
    tree = ast.parse(JIT_MODULE.read_text(encoding="utf-8"), filename=str(JIT_MODULE))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)

    assert modules == {
        "__future__",
        "decimal",
        "typing",
        "app.schemas.funding_advisory",
    }

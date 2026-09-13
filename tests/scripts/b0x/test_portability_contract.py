from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from decimal import Decimal

import pytest

from scripts.b0x import portability_contract as contract
from scripts.b0x.portability_contract import (
    EXPECTED_FIELD_CLASSES,
    EXPECTED_GUARDS,
    EXPECTED_SOURCE_DIGESTS,
    load_projection,
    validate_projection,
)

pytestmark = pytest.mark.unit


def test_reviewed_operator_projection_matches_production_invariants() -> None:
    projection = load_projection()
    assert validate_projection(projection) == ()
    assert set(projection["field_comparison"]) == EXPECTED_FIELD_CLASSES
    assert projection["portability_guards"] == EXPECTED_GUARDS


@pytest.mark.parametrize("field_class", tuple(sorted(EXPECTED_FIELD_CLASSES)))
def test_every_load_bearing_field_class_result_mutant_is_rejected(
    field_class: str,
) -> None:
    projection = deepcopy(load_projection())
    projection["field_comparison"][field_class]["result"] = "mutated"
    assert f"field comparison not matched: {field_class}" in validate_projection(
        projection
    )


@pytest.mark.parametrize("field_class", tuple(sorted(EXPECTED_FIELD_CLASSES)))
def test_every_load_bearing_field_class_conflict_mutant_is_rejected(
    field_class: str,
) -> None:
    projection = deepcopy(load_projection())
    projection["field_comparison"][field_class]["conflicts"] = ["redacted-conflict"]
    assert f"field comparison has conflicts: {field_class}" in validate_projection(
        projection
    )


@pytest.mark.parametrize("field", tuple(EXPECTED_SOURCE_DIGESTS))
def test_every_reviewed_source_digest_mutant_is_rejected(field: str) -> None:
    projection = deepcopy(load_projection())
    projection["source_digests"][field] = "mutated"
    assert f"reviewed source digest drift: {field}" in validate_projection(projection)


@pytest.mark.parametrize("field", tuple(EXPECTED_GUARDS))
def test_every_top_level_authority_guard_mutant_is_rejected(field: str) -> None:
    projection = deepcopy(load_projection())
    projection["portability_guards"][field] = "mutated"
    assert f"portability guard drift: {field}" in validate_projection(projection)


def test_production_envelope_constant_mutant_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    widened = replace(
        contract.CRYPTO_SIDECAR_ENVELOPE,
        per_order_notional=(
            contract.CRYPTO_SIDECAR_ENVELOPE.per_order_notional + Decimal("1")
        ),
    )
    monkeypatch.setattr(contract, "CRYPTO_SIDECAR_ENVELOPE", widened)
    assert "public production envelope constants drifted" in validate_projection(
        load_projection()
    )


def test_unreviewed_fields_and_malformed_results_cannot_hide() -> None:
    projection = deepcopy(load_projection())
    projection["field_comparison"]["unreviewed"] = {
        "result": "matched",
        "conflicts": [],
    }
    assert "operator field-class set differs" in validate_projection(projection)

    projection = deepcopy(load_projection())
    field_class = next(iter(EXPECTED_FIELD_CLASSES))
    projection["field_comparison"][field_class]["unreviewed"] = True
    assert f"field comparison shape differs: {field_class}" in validate_projection(
        projection
    )

    projection = deepcopy(load_projection())
    projection["source_digests"]["unreviewed"] = "silent"
    assert "reviewed source digest field set differs" in validate_projection(projection)

    projection = deepcopy(load_projection())
    projection["portability_guards"]["unreviewed"] = True
    assert "portability guard field set differs" in validate_projection(projection)

    projection = deepcopy(load_projection())
    projection["unreviewed"] = "silent"
    assert "operator projection top-level field set differs" in validate_projection(
        projection
    )

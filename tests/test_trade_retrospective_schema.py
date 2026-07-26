# tests/test_trade_retrospective_schema.py
"""ROB-647 — pure-unit validation of postmortem JSONB pydantic contracts."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.trade_retrospective import (
    VALID_ROOT_CAUSE_CLASSES,
    VALID_TRIGGER_TYPES,
    IntendedVsHappened,
    NextAction,
)

pytestmark = pytest.mark.unit


def test_trigger_type_taxonomy_is_closed_and_stable():
    assert "expired" in VALID_TRIGGER_TYPES
    assert "fill" in VALID_TRIGGER_TYPES
    assert "policy_violation" in VALID_TRIGGER_TYPES
    assert "missed_opportunity" in VALID_TRIGGER_TYPES
    # expired must NOT be an outcome — it only survives as a trigger_type.
    assert VALID_ROOT_CAUSE_CLASSES == {
        "user_input",
        "analysis",
        "policy",
        "execution",
        "harness",
    }


def test_intended_vs_happened_requires_a_signal():
    with pytest.raises(ValidationError):
        IntendedVsHappened.model_validate({})
    with pytest.raises(ValidationError):
        IntendedVsHappened.model_validate({"deviations": []})


def test_intended_vs_happened_summary_only_ok():
    m = IntendedVsHappened.model_validate({"summary": "entered late"})
    assert m.summary == "entered late"
    assert m.deviations == []


def test_intended_vs_happened_deviation_structure():
    m = IntendedVsHappened.model_validate(
        {
            "deviations": [
                {
                    "dimension": "price",
                    "planned": 100.0,
                    "actual": 103.5,
                    "delta": 3.5,
                    "unit": "USD",
                }
            ]
        }
    )
    assert m.deviations[0].dimension == "price"
    assert m.deviations[0].delta == 3.5


def test_intended_vs_happened_rejects_unknown_key():
    with pytest.raises(ValidationError):
        IntendedVsHappened.model_validate({"summary": "x", "bogus": 1})


def test_intended_vs_happened_rejects_empty_dimension():
    with pytest.raises(ValidationError):
        IntendedVsHappened.model_validate({"deviations": [{"dimension": "  "}]})


def test_next_action_requires_action():
    with pytest.raises(ValidationError):
        NextAction.model_validate({"owner": "claude"})
    with pytest.raises(ValidationError):
        NextAction.model_validate({"action": "   "})


def test_next_action_status_constrained():
    with pytest.raises(ValidationError):
        NextAction.model_validate({"action": "do", "status": "wip"})
    m = NextAction.model_validate(
        {"action": "raise issue", "issue_id": "ROB-999", "status": "open"}
    )
    assert m.issue_id == "ROB-999"


def test_next_action_preserves_legacy_extension_key():
    model = NextAction.model_validate(
        {"action": "do", "legacy_context": {"source": "pre-cutover"}}
    )

    assert model.model_dump()["legacy_context"] == {"source": "pre-cutover"}

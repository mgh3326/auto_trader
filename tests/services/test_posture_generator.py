import datetime as dt
from pathlib import Path

import pytest

from app.services.posture_generator import (
    POSTURE_STATES,
    PostureDisabledError,
    PostureGeneratorInput,
    PostureState,
    generate_posture,
)
from app.services.trading_policy_service import (
    load_trading_policy,
    policy_content_hash,
)

_REPLAY = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "posture"
    / "rob1106_20260727_replay.json"
)


def _replay() -> PostureGeneratorInput:
    return PostureGeneratorInput.model_validate_json(
        _REPLAY.read_text(encoding="utf-8")
    )


def _enabled_policy():
    return load_trading_policy().posture.model_copy(update={"enabled": True})


def test_posture_state_machine_has_exactly_five_states():
    assert [state.value for state in POSTURE_STATES] == [
        "RESTING",
        "CONDITIONAL",
        "ARMED_DEFERRED",
        "DISARMED",
        "EXPIRED_REARMABLE",
    ]


def test_generator_cannot_bypass_default_off_gate():
    policy = load_trading_policy()

    with pytest.raises(PostureDisabledError, match="posture.enabled=false"):
        generate_posture(
            _replay(),
            posture_policy=policy.posture,
            policy_version=policy.version,
            policy_content_hash=policy_content_hash(),
        )


def test_rob1106_20260727_replay_matches_canonical_coverage():
    policy = load_trading_policy()

    result = generate_posture(
        _replay(),
        posture_policy=_enabled_policy(),
        policy_version=policy.version,
        policy_content_hash=policy_content_hash(),
        generated_at=dt.datetime(2026, 7, 28, tzinfo=dt.UTC),
    )

    assert result.coverage.input_holding_count == 15
    assert result.coverage.mapped_holding_count == 15
    assert result.coverage.unmapped_holding_count == 0
    assert result.coverage.coverage_ratio == 1.0
    assert result.coverage.state_counts == {
        PostureState.RESTING: 2,
        PostureState.CONDITIONAL: 6,
        PostureState.ARMED_DEFERRED: 1,
        PostureState.DISARMED: 6,
        PostureState.EXPIRED_REARMABLE: 0,
    }
    assert result.unmapped_holdings == []
    assert result.safety.orders_created == 0
    assert result.safety.proposals_created == 0
    assert result.safety.broker_mutations == 0


def test_unmapped_holding_is_exposed_instead_of_forced_into_a_state():
    policy = load_trading_policy()
    snapshot = _replay().model_copy(deep=True)
    snapshot.policy_contexts = [
        row for row in snapshot.policy_contexts if row.holding_id != "toss-036460"
    ]

    result = generate_posture(
        snapshot,
        posture_policy=_enabled_policy(),
        policy_version=policy.version,
        policy_content_hash=policy_content_hash(),
    )

    assert result.coverage.input_holding_count == 15
    assert result.coverage.mapped_holding_count == 14
    assert result.coverage.unmapped_holding_count == 1
    assert result.coverage.coverage_ratio == 14 / 15
    assert sum(result.coverage.state_counts.values()) == 14
    assert [row.holding_id for row in result.unmapped_holdings] == ["toss-036460"]
    assert result.unmapped_holdings[0].reason == "missing_policy_context"


def test_expired_rearmable_is_a_state_not_a_sixth_overlay_state():
    policy = load_trading_policy()
    snapshot = _replay().model_copy(deep=True)
    context = next(
        row for row in snapshot.policy_contexts if row.holding_id == "toss-052690"
    )
    context.level_status = "expired"
    context.rearm_allowed = True

    result = generate_posture(
        snapshot,
        posture_policy=_enabled_policy(),
        policy_version=policy.version,
        policy_content_hash=policy_content_hash(),
    )

    assigned = next(
        row for row in result.assignments if row.holding_id == "toss-052690"
    )
    assert assigned.state is PostureState.EXPIRED_REARMABLE
    assert set(result.coverage.state_counts) == set(POSTURE_STATES)


def test_unknown_overlay_role_surfaces_as_state_definition_gap():
    policy = load_trading_policy()
    snapshot = _replay().model_copy(deep=True)
    context = next(
        row for row in snapshot.policy_contexts if row.holding_id == "toss-052690"
    )
    context.level_role = "CATALYST_GAP_CAPTURE_V1"

    result = generate_posture(
        snapshot,
        posture_policy=_enabled_policy(),
        policy_version=policy.version,
        policy_content_hash=policy_content_hash(),
    )

    gap = next(
        row for row in result.unmapped_holdings if row.holding_id == "toss-052690"
    )
    assert gap.reason == (
        "state_definition_gap:unknown_level_role:CATALYST_GAP_CAPTURE_V1"
    )
    assert "CATALYST_GAP_CAPTURE_V1" not in {state.value for state in POSTURE_STATES}


def test_generator_output_has_no_order_or_proposal_surface():
    policy = load_trading_policy()
    result = generate_posture(
        _replay(),
        posture_policy=_enabled_policy(),
        policy_version=policy.version,
        policy_content_hash=policy_content_hash(),
    )

    fields = set(type(result.assignments[0]).model_fields)
    assert not any("order" in field or "proposal" in field for field in fields)
    assert result.safety.model_dump() == {
        "orders_created": 0,
        "proposals_created": 0,
        "broker_mutations": 0,
    }

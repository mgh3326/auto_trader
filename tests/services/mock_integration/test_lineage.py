"""ROB-1261 deterministic intent/plan/attempt lineage tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from app.schemas.execution_contracts import DecisionIntent, ExecutionPlan, OrderAttempt
from app.services.brokers.client_order_ids import (
    ALPACA_PAPER_CLIENT_ORDER_ID_MAX_LENGTH,
    BINANCE_SPOT_DEMO_CLIENT_ORDER_ID_MAX_LENGTH,
    BROKER_CLIENT_ID_CONSTRAINT_VIOLATION,
    BROKER_CLIENT_ID_TARGET_PLAN_BROKERS,
    BROKER_CLIENT_ORDER_ID_CONSTRAINTS,
    TOSS_CLIENT_ORDER_ID_MAX_LENGTH,
    BrokerClientIdTarget,
    BrokerClientOrderIdConstraintViolation,
    assert_broker_client_order_id,
)
from app.services.mock_integration import lineage
from app.services.mock_integration.lineage import (
    BROKER_CLIENT_ID_TARGET_MISMATCH,
    BROKER_ORDER_ID_CONFLICT,
    BrokerClientIdTargetMismatch,
    BrokerOrderIdConflict,
    CallerOwnedIdRejected,
    DecisionIntentDraft,
    ExecutionPlanDraft,
    HashVersionUpgradeRequired,
    LineagePersistenceUnavailable,
    LineageReasonCode,
    MockLineageFactory,
    OrderAttemptDraft,
    canonical_bytes,
    normalize_datetime,
    require_lineage_persistence_port,
)


def _intent_draft(**overrides: object) -> DecisionIntentDraft:
    values: dict[str, object] = {
        "policy_version": "policy-v1",
        "policy_version_hash": "a" * 12,
        "decision_timestamp": datetime(2026, 8, 15, 9, 0, tzinfo=UTC),
        "market_data_cutoff": datetime(2026, 8, 15, 8, 59, tzinfo=UTC),
        "symbol": "BRK.B",
        "side": "buy",
        "target_notional": Decimal("1"),
        "target_notional_currency": "USD",
        "limit_policy": {"order_type": "limit"},
        "expiry_policy": {"kind": "day"},
        "rationale": "한국어 canonical fixture",
    }
    values.update(overrides)
    return DecisionIntentDraft(**values)


def _plan_draft(**overrides: object) -> ExecutionPlanDraft:
    values: dict[str, object] = {
        "lane_id": "us.alpaca.paper.default",
        "broker": "alpaca",
        "account_profile": "default-paper",
        "account_mode": "alpaca_paper",
        "normalized_symbol": "BRK.B",
        "quantity": Decimal("1"),
        "limit_price": Decimal("1"),
        "quote_currency": "USD",
        "tick_rounding": {"increment": "0.01"},
        "session": "regular",
        "time_in_force": "day",
        "min_order_validation": {"quote_required": True},
        "risk_caps": {"max_notional": "1"},
    }
    values.update(overrides)
    return ExecutionPlanDraft(**values)


def _attempt_draft(**overrides: object) -> OrderAttemptDraft:
    values: dict[str, object] = {
        "cycle_id": "cycle-20260815-1",
        "attempt_seq": 1,
        "lane_prefix": "alpaca",
        "broker_client_id_target": BrokerClientIdTarget.ALPACA_PAPER,
    }
    values.update(overrides)
    return OrderAttemptDraft(**values)


def _issued_intent_and_plan(
    *,
    intent_overrides: dict[str, object] | None = None,
    plan_overrides: dict[str, object] | None = None,
) -> tuple[MockLineageFactory, DecisionIntent, ExecutionPlan]:
    factory = MockLineageFactory()
    intent = factory.create_decision_intent(_intent_draft(**(intent_overrides or {})))
    plan = factory.create_execution_plan(intent, _plan_draft(**(plan_overrides or {})))
    return factory, intent, plan


def test_factory_rejects_caller_owned_identifier_fields() -> None:
    factory = MockLineageFactory()
    draft = _intent_draft()

    with pytest.raises(ValidationError, match="decision_intent_id"):
        DecisionIntentDraft(
            **draft.model_dump(mode="python"),
            decision_intent_id="caller-owned",
        )
    with pytest.raises(TypeError, match="decision_intent_id"):
        factory.create_decision_intent(draft, decision_intent_id="caller-owned")

    forged = DecisionIntent(
        decision_intent_id="caller-owned",
        **draft.model_dump(mode="python"),
    )
    with pytest.raises(CallerOwnedIdRejected, match="server-generated"):
        factory.create_execution_plan(forged, _plan_draft())
    with pytest.raises(ValidationError, match="server-generated"):
        lineage.LineageEnvelope(decision_intent=forged)


def test_every_server_factory_draft_forbids_caller_owned_ids() -> None:
    with pytest.raises(ValidationError, match="execution_plan_id"):
        ExecutionPlanDraft(
            **_plan_draft().model_dump(mode="python"),
            execution_plan_id="caller-owned",
        )
    with pytest.raises(ValidationError, match="decision_intent_id"):
        ExecutionPlanDraft(
            **_plan_draft().model_dump(mode="python"),
            decision_intent_id="caller-owned",
        )
    with pytest.raises(ValidationError, match="order_attempt_id"):
        OrderAttemptDraft(
            **_attempt_draft().model_dump(mode="python"),
            order_attempt_id="caller-owned",
        )
    with pytest.raises(ValidationError, match="idempotency_key"):
        OrderAttemptDraft(
            **_attempt_draft().model_dump(mode="python"),
            idempotency_key="caller-owned",
        )
    with pytest.raises(ValidationError, match="broker_client_order_id"):
        OrderAttemptDraft(
            **_attempt_draft().model_dump(mode="python"),
            broker_client_order_id="caller-owned",
        )


def test_server_factory_generates_a_full_internal_id_and_immutable_envelope() -> None:
    factory = MockLineageFactory()
    envelope = factory.create_intent_envelope(_intent_draft())

    prefix, digest = envelope.decision_intent.decision_intent_id.split(":", 1)
    assert prefix == "mock-intent-v1"
    assert len(digest) == 64
    with pytest.raises(ValidationError):
        envelope.decision_intent = envelope.decision_intent


def test_canonical_json_is_sorted_utf8_and_round_trips() -> None:
    _, intent, _ = _issued_intent_and_plan()

    canonical = canonical_bytes(intent)

    assert b", " not in canonical
    assert b"\\u" not in canonical
    assert "한국어".encode() in canonical
    assert json.loads(canonical)["decision_intent_id"] == intent.decision_intent_id
    assert DecisionIntent.model_validate_json(canonical) == intent


def test_datetime_golden_values_produce_the_same_intent_hash_for_one_instant() -> None:
    factory = MockLineageFactory()
    kst = ZoneInfo("Asia/Seoul")
    eastern_time = ZoneInfo("America/New_York")
    representations = (
        (
            datetime(2026, 8, 15, 9, 0, tzinfo=UTC),
            datetime(2026, 8, 15, 8, 59, tzinfo=UTC),
        ),
        (
            datetime(2026, 8, 15, 18, 0, tzinfo=kst),
            datetime(2026, 8, 15, 17, 59, tzinfo=kst),
        ),
        (
            datetime(2026, 8, 15, 5, 0, tzinfo=eastern_time),
            datetime(2026, 8, 15, 4, 59, tzinfo=eastern_time),
        ),
    )

    intent_ids = [
        factory.create_decision_intent(
            _intent_draft(
                decision_timestamp=decision_timestamp,
                market_data_cutoff=market_data_cutoff,
            )
        ).decision_intent_id
        for decision_timestamp, market_data_cutoff in representations
    ]
    different_instant_id = factory.create_decision_intent(
        _intent_draft(
            decision_timestamp=datetime(2026, 8, 15, 9, 1, tzinfo=UTC),
            market_data_cutoff=datetime(2026, 8, 15, 9, 0, tzinfo=UTC),
        )
    ).decision_intent_id

    assert intent_ids[0] == intent_ids[1] == intent_ids[2]
    assert different_instant_id != intent_ids[0]


def test_naive_datetime_is_rejected_before_hashing() -> None:
    naive = datetime(2026, 8, 15, 9, 0)

    with pytest.raises(ValueError, match="timezone-aware"):
        normalize_datetime(naive)
    with pytest.raises(ValidationError, match="timezone-aware"):
        MockLineageFactory().create_decision_intent(
            _intent_draft(decision_timestamp=naive)
        )


@pytest.mark.parametrize("value", [Decimal("1"), Decimal("1.0"), Decimal("1.000")])
def test_decimal_golden_values_produce_the_same_intent_hash(value: Decimal) -> None:
    factory = MockLineageFactory()
    intent = factory.create_decision_intent(_intent_draft(target_notional=value))

    assert intent.decision_intent_id == (
        "mock-intent-v1:"
        "6b58bec1ac89e262cda8a301a0c01e33432f26e80d5dce8c0a7401c6a5f4a1ea"
    )


@pytest.mark.parametrize("value", [Decimal("1"), Decimal("1.0"), Decimal("1.000")])
def test_decimal_golden_values_produce_the_same_plan_hash(value: Decimal) -> None:
    factory = MockLineageFactory()
    intent = factory.create_decision_intent(_intent_draft())
    plan = factory.create_execution_plan(
        intent,
        _plan_draft(quantity=value, limit_price=value),
    )

    assert plan.execution_plan_id == (
        "mock-plan-v1:b9ac4cfe41a996714d5969efda2c222d16c42579b42900853ac1c88160960077"
    )


def test_v1_hash_allowlists_are_exact_and_field_additions_require_v2() -> None:
    assert lineage._INTENT_V1_HASH_FIELDS == (
        "policy_version",
        "policy_version_hash",
        "decision_timestamp",
        "market_data_cutoff",
        "symbol",
        "side",
        "target_notional",
        "target_notional_currency",
        "limit_policy",
        "expiry_policy",
        "rationale",
    )
    assert lineage._PLAN_V1_HASH_FIELDS == (
        "decision_intent_id",
        "lane_id",
        "broker",
        "account_profile",
        "account_mode",
        "normalized_symbol",
        "quantity",
        "limit_price",
        "quote_currency",
        "tick_rounding",
        "session",
        "time_in_force",
        "min_order_validation",
        "risk_caps",
    )

    class IntentWithNewField(DecisionIntent):
        new_hash_input: str

    class PlanWithNewField(ExecutionPlan):
        new_hash_input: str

    _, intent, plan = _issued_intent_and_plan()
    extended_intent = IntentWithNewField(
        **intent.model_dump(mode="python"),
        new_hash_input="v2-required",
    )
    extended_plan = PlanWithNewField(
        **plan.model_dump(mode="python"),
        new_hash_input="v2-required",
    )

    with pytest.raises(HashVersionUpgradeRequired, match="mock-intent-v2"):
        lineage.derive_intent_v1_id(extended_intent)
    with pytest.raises(HashVersionUpgradeRequired, match="mock-plan-v2"):
        lineage.derive_plan_v1_id(extended_plan)


def test_attempt_sequence_changes_only_the_attempt_identity() -> None:
    factory, intent, plan = _issued_intent_and_plan()
    envelope = lineage.LineageEnvelope(
        decision_intent=intent,
        execution_plan=plan,
    )
    first = factory.create_order_attempt(envelope, _attempt_draft(attempt_seq=1))
    retry = factory.create_order_attempt(envelope, _attempt_draft(attempt_seq=2))

    assert first.order_attempt_id != retry.order_attempt_id
    assert first.idempotency_key == retry.idempotency_key
    assert first.broker_client_order_id == retry.broker_client_order_id
    assert ":1" not in first.idempotency_key
    assert ":2" not in retry.idempotency_key


@pytest.mark.parametrize(
    ("field_name", "replacement"),
    [
        ("order_attempt_id", "caller-owned"),
        ("idempotency_key", "caller-owned"),
        ("broker_client_order_id", "caller-owned"),
    ],
)
def test_attempt_envelope_rejects_caller_owned_identifiers(
    field_name: str, replacement: str
) -> None:
    factory, intent, plan = _issued_intent_and_plan()
    plan_envelope = lineage.LineageEnvelope(
        decision_intent=intent,
        execution_plan=plan,
    )
    draft = _attempt_draft()
    issued = factory.create_order_attempt(plan_envelope, draft)
    forged = issued.model_copy(update={field_name: replacement})

    with pytest.raises(ValidationError, match="server-generated"):
        lineage.LineageEnvelope(
            decision_intent=intent,
            execution_plan=plan,
            order_attempt=forged,
            attempt_seq=draft.attempt_seq,
            lane_prefix=draft.lane_prefix,
            broker_client_id_target=draft.broker_client_id_target,
        )


def test_factory_attempt_envelope_carries_only_derived_correlation() -> None:
    factory, intent, plan = _issued_intent_and_plan()
    plan_envelope = lineage.LineageEnvelope(
        decision_intent=intent,
        execution_plan=plan,
    )
    draft = _attempt_draft()

    envelope = factory.create_attempt_envelope(plan_envelope, draft)

    assert envelope.order_attempt is not None
    assert envelope.attempt_seq == draft.attempt_seq
    assert envelope.lane_prefix == draft.lane_prefix
    assert envelope.broker_client_id_target == draft.broker_client_id_target


def test_order_attempt_draft_requires_a_complete_broker_client_id_pair() -> None:
    required_values = _attempt_draft().model_dump(mode="python")
    del required_values["lane_prefix"]
    del required_values["broker_client_id_target"]

    with pytest.raises(ValidationError, match="lane_prefix"):
        OrderAttemptDraft(**required_values)
    with pytest.raises(ValidationError, match="both set or both None"):
        _attempt_draft(lane_prefix="kis", broker_client_id_target=None)
    with pytest.raises(ValidationError, match="both set or both None"):
        _attempt_draft(
            lane_prefix=None,
            broker_client_id_target=BrokerClientIdTarget.TOSS,
        )

    assert (
        _attempt_draft(
            lane_prefix=None,
            broker_client_id_target=None,
        ).lane_prefix
        is None
    )
    assert _attempt_draft().broker_client_id_target is BrokerClientIdTarget.ALPACA_PAPER


def test_broker_client_id_target_remains_the_three_confirmed_targets() -> None:
    assert tuple(BrokerClientIdTarget) == (
        BrokerClientIdTarget.TOSS,
        BrokerClientIdTarget.BINANCE_SPOT_DEMO,
        BrokerClientIdTarget.ALPACA_PAPER,
    )


def test_broker_client_id_target_plan_broker_map_is_read_only() -> None:
    with pytest.raises(TypeError):
        BROKER_CLIENT_ID_TARGET_PLAN_BROKERS[BrokerClientIdTarget.TOSS] = "mutated"  # type: ignore[index]


def test_broker_client_order_id_constraints_map_rejects_in_place_mutation() -> None:
    target = BrokerClientIdTarget.TOSS
    original_constraint = BROKER_CLIENT_ORDER_ID_CONSTRAINTS[target]

    with pytest.raises(TypeError):
        BROKER_CLIENT_ORDER_ID_CONSTRAINTS[target] = original_constraint  # type: ignore[index]
    with pytest.raises(TypeError):
        del BROKER_CLIENT_ORDER_ID_CONSTRAINTS[target]  # type: ignore[index]

    assert BROKER_CLIENT_ORDER_ID_CONSTRAINTS[target] is original_constraint


@pytest.mark.parametrize(
    ("plan_broker", "target", "matches"),
    [
        pytest.param("toss", BrokerClientIdTarget.TOSS, True, id="toss-matches"),
        pytest.param(
            "binance",
            BrokerClientIdTarget.BINANCE_SPOT_DEMO,
            True,
            id="binance-spot-demo-matches-binance",
        ),
        pytest.param(
            "alpaca",
            BrokerClientIdTarget.ALPACA_PAPER,
            True,
            id="alpaca-matches",
        ),
        pytest.param("toss", BrokerClientIdTarget.BINANCE_SPOT_DEMO, False),
        pytest.param("toss", BrokerClientIdTarget.ALPACA_PAPER, False),
        pytest.param("binance", BrokerClientIdTarget.TOSS, False),
        pytest.param("binance", BrokerClientIdTarget.ALPACA_PAPER, False),
        pytest.param("alpaca", BrokerClientIdTarget.TOSS, False),
        pytest.param("alpaca", BrokerClientIdTarget.BINANCE_SPOT_DEMO, False),
    ],
)
def test_factory_enforces_broker_client_id_target_plan_broker_matrix(
    plan_broker: str,
    target: BrokerClientIdTarget,
    matches: bool,
) -> None:
    factory, intent, plan = _issued_intent_and_plan(
        plan_overrides={"broker": plan_broker}
    )
    plan_envelope = lineage.LineageEnvelope(
        decision_intent=intent,
        execution_plan=plan,
    )

    if matches:
        attempt = factory.create_order_attempt(
            plan_envelope,
            _attempt_draft(lane_prefix="lane", broker_client_id_target=target),
        )
        assert attempt.broker_client_order_id is not None
    else:
        with pytest.raises(BrokerClientIdTargetMismatch) as error:
            factory.create_order_attempt(
                plan_envelope,
                _attempt_draft(lane_prefix="lane", broker_client_id_target=target),
            )
        assert error.value.reason_code == BROKER_CLIENT_ID_TARGET_MISMATCH
        assert str(error.value) == "broker_client_id_target_mismatch"


def test_factory_rejects_kis_plan_with_alpaca_target() -> None:
    factory, intent, plan = _issued_intent_and_plan(plan_overrides={"broker": "kis"})
    plan_envelope = lineage.LineageEnvelope(
        decision_intent=intent,
        execution_plan=plan,
    )

    with pytest.raises(BrokerClientIdTargetMismatch) as error:
        factory.create_order_attempt(
            plan_envelope,
            _attempt_draft(
                lane_prefix="lane",
                broker_client_id_target=BrokerClientIdTarget.ALPACA_PAPER,
            ),
        )

    assert error.value.reason_code == "broker_client_id_target_mismatch"


@pytest.mark.parametrize("broker", ("toss", "alpaca", "binance"))
def test_factory_rejects_a_none_pair_for_each_confirmed_broker(broker: str) -> None:
    factory, intent, plan = _issued_intent_and_plan(plan_overrides={"broker": broker})
    plan_envelope = lineage.LineageEnvelope(
        decision_intent=intent,
        execution_plan=plan,
    )

    with pytest.raises(BrokerClientIdTargetMismatch) as error:
        factory.create_order_attempt(
            plan_envelope,
            _attempt_draft(lane_prefix=None, broker_client_id_target=None),
        )

    assert error.value.reason_code == BROKER_CLIENT_ID_TARGET_MISMATCH


def test_factory_allows_a_none_pair_for_an_unconfirmed_broker() -> None:
    factory, intent, plan = _issued_intent_and_plan(
        plan_overrides={"broker": "unknown-broker"}
    )
    envelope = factory.create_attempt_envelope(
        lineage.LineageEnvelope(
            decision_intent=intent,
            execution_plan=plan,
        ),
        _attempt_draft(lane_prefix=None, broker_client_id_target=None),
    )

    assert envelope.order_attempt is not None
    assert envelope.order_attempt.broker_client_order_id is None


@pytest.mark.parametrize("broker", ("kis", "kiwoom"))
@pytest.mark.parametrize("target", tuple(BrokerClientIdTarget))
def test_factory_rejects_a_confirmed_target_for_kr_brokers(
    broker: str,
    target: BrokerClientIdTarget,
) -> None:
    factory, intent, plan = _issued_intent_and_plan(plan_overrides={"broker": broker})
    plan_envelope = lineage.LineageEnvelope(
        decision_intent=intent,
        execution_plan=plan,
    )

    with pytest.raises(BrokerClientIdTargetMismatch) as error:
        factory.create_order_attempt(
            plan_envelope,
            _attempt_draft(lane_prefix="lane", broker_client_id_target=target),
        )

    assert error.value.reason_code == BROKER_CLIENT_ID_TARGET_MISMATCH


def test_target_mismatch_is_rejected_before_broker_client_id_derivation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, intent, plan = _issued_intent_and_plan(plan_overrides={"broker": "kis"})
    plan_envelope = lineage.LineageEnvelope(
        decision_intent=intent,
        execution_plan=plan,
    )

    def broker_id_derivation_must_not_run(**_: object) -> str:
        raise AssertionError("broker client ID derivation ran after a target mismatch")

    monkeypatch.setattr(
        lineage,
        "derive_broker_client_order_id",
        broker_id_derivation_must_not_run,
    )

    with pytest.raises(BrokerClientIdTargetMismatch) as error:
        factory.create_order_attempt(
            plan_envelope,
            _attempt_draft(
                lane_prefix="lane",
                broker_client_id_target=BrokerClientIdTarget.ALPACA_PAPER,
            ),
        )

    assert error.value.reason_code == "broker_client_id_target_mismatch"


def _none_pair_envelope(
    *, broker: str = "kis"
) -> tuple[MockLineageFactory, lineage.LineageEnvelope]:
    factory, intent, plan = _issued_intent_and_plan(plan_overrides={"broker": broker})
    return factory, factory.create_attempt_envelope(
        lineage.LineageEnvelope(
            decision_intent=intent,
            execution_plan=plan,
        ),
        _attempt_draft(lane_prefix=None, broker_client_id_target=None),
    )


def _self_consistent_attempt(
    plan: ExecutionPlan,
    *,
    lane_prefix: str | None,
    target: BrokerClientIdTarget | None,
) -> OrderAttempt:
    cycle_id = "forged-cycle-1"
    attempt_seq = 1
    broker_client_order_id: str | None = None
    if lane_prefix is not None:
        assert target is not None
        broker_client_order_id = lineage.derive_broker_client_order_id(
            execution_plan_id=plan.execution_plan_id,
            cycle_id=cycle_id,
            lane_prefix=lane_prefix,
            target=target,
        )
    return OrderAttempt(
        order_attempt_id=lineage.derive_attempt_v1_id(
            execution_plan_id=plan.execution_plan_id,
            cycle_id=cycle_id,
            attempt_seq=attempt_seq,
        ),
        execution_plan_id=plan.execution_plan_id,
        cycle_id=cycle_id,
        idempotency_key=lineage.derive_idempotency_key(
            execution_plan_id=plan.execution_plan_id,
            cycle_id=cycle_id,
        ),
        broker_client_order_id=broker_client_order_id,
        broker_order_id=None,
    )


@pytest.mark.parametrize("broker", ("kis", "kiwoom"))
def test_none_pair_preserves_internal_correlation_and_round_trips(broker: str) -> None:
    _, envelope = _none_pair_envelope(broker=broker)
    assert envelope.execution_plan is not None
    assert envelope.execution_plan.broker == broker
    assert envelope.order_attempt is not None
    attempt = envelope.order_attempt

    assert attempt.order_attempt_id.startswith("mock-attempt-v1:")
    assert attempt.idempotency_key.startswith("mock-idempotency-v1:")
    assert attempt.broker_client_order_id is None
    assert attempt.broker_order_id is None
    canonical = canonical_bytes(attempt)
    round_tripped = OrderAttempt.model_validate_json(canonical)
    assert round_tripped == attempt
    assert canonical_bytes(round_tripped) == canonical


@pytest.mark.parametrize("broker", ("kis", "kiwoom"))
def test_acknowledge_order_attempt_sets_a_broker_order_id_once(broker: str) -> None:
    factory, envelope = _none_pair_envelope(broker=broker)

    acknowledged = factory.acknowledge_order_attempt(envelope, "  id-1  ")

    assert acknowledged is not envelope
    assert acknowledged.order_attempt is not None
    assert acknowledged.order_attempt.broker_order_id == "id-1"
    assert acknowledged.order_attempt.broker_client_order_id is None


def test_acknowledge_order_attempt_replays_the_same_broker_order_id_as_a_no_op() -> (
    None
):
    factory, envelope = _none_pair_envelope()
    acknowledged = factory.acknowledge_order_attempt(envelope, "  id-1  ")

    replayed = factory.acknowledge_order_attempt(acknowledged, "id-1")

    assert replayed is acknowledged


def test_acknowledge_order_attempt_revalidates_same_id_replay_before_returning() -> (
    None
):
    factory, envelope = _none_pair_envelope()
    acknowledged = factory.acknowledge_order_attempt(envelope, "id-1")
    assert acknowledged.execution_plan is not None
    assert acknowledged.order_attempt is not None

    forged_attempt_seq = acknowledged.model_copy(update={"attempt_seq": 2})
    forged_idempotency_key = acknowledged.model_copy(
        update={
            "order_attempt": acknowledged.order_attempt.model_copy(
                update={"idempotency_key": "caller-owned"}
            )
        }
    )
    forged_plan_correlation = acknowledged.model_copy(
        update={
            "execution_plan": acknowledged.execution_plan.model_copy(
                update={"decision_intent_id": "caller-owned"}
            )
        }
    )

    for forged_envelope in (
        forged_attempt_seq,
        forged_idempotency_key,
        forged_plan_correlation,
    ):
        with pytest.raises(ValidationError):
            factory.acknowledge_order_attempt(forged_envelope, "id-1")


def test_acknowledge_order_attempt_rejects_a_conflicting_broker_order_id() -> None:
    factory, envelope = _none_pair_envelope()
    acknowledged = factory.acknowledge_order_attempt(envelope, "  id-1  ")

    with pytest.raises(BrokerOrderIdConflict) as error:
        factory.acknowledge_order_attempt(acknowledged, "  id-2  ")

    assert error.value.reason_code == BROKER_ORDER_ID_CONFLICT
    assert str(error.value) == "broker_order_id_conflict"


@pytest.mark.parametrize("broker_order_id", ("", "   "))
def test_acknowledge_order_attempt_rejects_an_empty_normalized_broker_order_id(
    broker_order_id: str,
) -> None:
    factory, envelope = _none_pair_envelope()

    with pytest.raises(ValueError, match="non-empty"):
        factory.acknowledge_order_attempt(envelope, broker_order_id)


def test_acknowledge_order_attempt_accepts_a_confirmed_client_id_pair() -> None:
    factory, intent, plan = _issued_intent_and_plan()
    envelope = factory.create_attempt_envelope(
        lineage.LineageEnvelope(decision_intent=intent, execution_plan=plan),
        _attempt_draft(),
    )

    acknowledged = factory.acknowledge_order_attempt(envelope, "broker-ack-1")

    assert acknowledged.order_attempt is not None
    assert acknowledged.order_attempt.broker_order_id == "broker-ack-1"


def test_common_lineage_source_has_no_native_ack_field_parser() -> None:
    source = Path(lineage.__file__).read_text(encoding="utf-8")

    assert "od" + "no" not in source
    assert "order_" + "no" not in source


def test_acknowledge_order_attempt_rejects_a_model_copy_normalization_bypass() -> None:
    factory, envelope = _none_pair_envelope()
    assert envelope.order_attempt is not None
    forged_attempt = envelope.order_attempt.model_copy(
        update={"broker_order_id": "  id-1  "}
    )

    with pytest.raises(ValidationError, match="strip-normalized"):
        lineage.LineageEnvelope(
            decision_intent=envelope.decision_intent,
            execution_plan=envelope.execution_plan,
            order_attempt=forged_attempt,
            attempt_seq=envelope.attempt_seq,
            lane_prefix=envelope.lane_prefix,
            broker_client_id_target=envelope.broker_client_id_target,
        )

    unvalidated_envelope = lineage.LineageEnvelope.model_construct(
        decision_intent=envelope.decision_intent,
        execution_plan=envelope.execution_plan,
        order_attempt=forged_attempt,
        attempt_seq=envelope.attempt_seq,
        lane_prefix=envelope.lane_prefix,
        broker_client_id_target=envelope.broker_client_id_target,
    )
    with pytest.raises(ValidationError, match="strip-normalized"):
        factory.acknowledge_order_attempt(unvalidated_envelope, "id-1")


@pytest.mark.parametrize("broker", ("toss", "alpaca", "binance"))
def test_envelope_rejects_a_self_consistent_none_pair_for_confirmed_brokers(
    broker: str,
) -> None:
    _, intent, plan = _issued_intent_and_plan(plan_overrides={"broker": broker})

    with pytest.raises(ValidationError, match=BROKER_CLIENT_ID_TARGET_MISMATCH):
        lineage.LineageEnvelope(
            decision_intent=intent,
            execution_plan=plan,
            order_attempt=_self_consistent_attempt(
                plan,
                lane_prefix=None,
                target=None,
            ),
            attempt_seq=1,
            lane_prefix=None,
            broker_client_id_target=None,
        )


def test_envelope_rejects_a_self_consistent_mismatched_broker_target() -> None:
    _, intent, plan = _issued_intent_and_plan(plan_overrides={"broker": "kis"})

    with pytest.raises(ValidationError, match=BROKER_CLIENT_ID_TARGET_MISMATCH):
        lineage.LineageEnvelope(
            decision_intent=intent,
            execution_plan=plan,
            order_attempt=_self_consistent_attempt(
                plan,
                lane_prefix="lane",
                target=BrokerClientIdTarget.ALPACA_PAPER,
            ),
            attempt_seq=1,
            lane_prefix="lane",
            broker_client_id_target=BrokerClientIdTarget.ALPACA_PAPER,
        )


def test_acknowledge_order_attempt_rejects_an_unvalidated_confirmed_none_pair() -> None:
    factory, intent, plan = _issued_intent_and_plan(plan_overrides={"broker": "alpaca"})
    unvalidated_envelope = lineage.LineageEnvelope.model_construct(
        decision_intent=intent,
        execution_plan=plan,
        order_attempt=_self_consistent_attempt(
            plan,
            lane_prefix=None,
            target=None,
        ),
        attempt_seq=1,
        lane_prefix=None,
        broker_client_id_target=None,
    )

    with pytest.raises(ValidationError, match=BROKER_CLIENT_ID_TARGET_MISMATCH):
        factory.acknowledge_order_attempt(unvalidated_envelope, "broker-ack-1")


def test_none_pair_rejects_a_broker_client_order_id_value() -> None:
    _, envelope = _none_pair_envelope()
    assert envelope.order_attempt is not None
    forged = envelope.order_attempt.model_copy(
        update={"broker_client_order_id": "must-not-be-present"}
    )

    with pytest.raises(ValidationError, match="broker_client_order_id"):
        lineage.LineageEnvelope(
            decision_intent=envelope.decision_intent,
            execution_plan=envelope.execution_plan,
            order_attempt=forged,
            attempt_seq=envelope.attempt_seq,
            lane_prefix=None,
            broker_client_id_target=None,
        )


def test_broker_client_id_is_shorter_than_the_internal_digest() -> None:
    factory, intent, plan = _issued_intent_and_plan(plan_overrides={"broker": "toss"})
    envelope = lineage.LineageEnvelope(
        decision_intent=intent,
        execution_plan=plan,
    )
    attempt = factory.create_order_attempt(
        envelope,
        _attempt_draft(
            broker_client_id_target=BrokerClientIdTarget.TOSS,
            lane_prefix="tosprop",
        ),
    )

    assert attempt.broker_client_order_id is not None
    attempt_digest = attempt.order_attempt_id.split(":", 1)[1]
    assert len(attempt_digest) == 64
    internal_digest = attempt.idempotency_key.split(":", 1)[1]
    assert len(internal_digest) == 64
    assert attempt.broker_client_order_id == f"tosprop-{internal_digest[:24]}"
    assert len(attempt.broker_client_order_id) <= TOSS_CLIENT_ORDER_ID_MAX_LENGTH
    assert attempt_digest not in attempt.broker_client_order_id
    assert internal_digest not in attempt.broker_client_order_id


def test_generated_broker_client_id_fails_closed_when_it_exceeds_target_limit() -> None:
    factory, intent, plan = _issued_intent_and_plan(
        plan_overrides={"broker": "binance"}
    )
    envelope = lineage.LineageEnvelope(
        decision_intent=intent,
        execution_plan=plan,
    )

    with pytest.raises(BrokerClientOrderIdConstraintViolation) as error:
        factory.create_order_attempt(
            envelope,
            _attempt_draft(
                lane_prefix="x" * 12,
                broker_client_id_target=BrokerClientIdTarget.BINANCE_SPOT_DEMO,
            ),
        )

    assert error.value.reason_code == BROKER_CLIENT_ID_CONSTRAINT_VIOLATION


@pytest.mark.parametrize(
    ("target", "maximum"),
    [
        (BrokerClientIdTarget.TOSS, TOSS_CLIENT_ORDER_ID_MAX_LENGTH),
        (
            BrokerClientIdTarget.BINANCE_SPOT_DEMO,
            BINANCE_SPOT_DEMO_CLIENT_ORDER_ID_MAX_LENGTH,
        ),
        (BrokerClientIdTarget.ALPACA_PAPER, ALPACA_PAPER_CLIENT_ORDER_ID_MAX_LENGTH),
    ],
)
def test_broker_client_id_constraints_fail_closed(
    target: BrokerClientIdTarget, maximum: int
) -> None:
    assert_broker_client_order_id(target=target, client_order_id="lane-0123456789")

    with pytest.raises(BrokerClientOrderIdConstraintViolation) as length_error:
        assert_broker_client_order_id(
            target=target, client_order_id="a" * (maximum + 1)
        )
    assert length_error.value.reason_code == BROKER_CLIENT_ID_CONSTRAINT_VIOLATION

    with pytest.raises(BrokerClientOrderIdConstraintViolation) as charset_error:
        assert_broker_client_order_id(target=target, client_order_id="unsafe.id")
    assert charset_error.value.reason_code == BROKER_CLIENT_ID_CONSTRAINT_VIOLATION


def test_reason_code_dictionary_contains_persistence_broker_and_d5_codes() -> None:
    assert lineage.LINEAGE_REASON_CODES == {
        "lineage_persistence_unavailable",
        "currency_conversion_not_authorized",
        "lane_quote_currency_mismatch",
        "broker_client_id_constraint_violation",
        "broker_client_id_target_mismatch",
        "broker_order_id_conflict",
    }
    assert (
        LineageReasonCode.BROKER_CLIENT_ID_CONSTRAINT_VIOLATION.value
        == BROKER_CLIENT_ID_CONSTRAINT_VIOLATION
    )
    assert LineageReasonCode.BROKER_ORDER_ID_CONFLICT.value == BROKER_ORDER_ID_CONFLICT


def test_persistence_requires_a_future_owned_port_without_implementing_storage() -> (
    None
):
    with pytest.raises(LineagePersistenceUnavailable) as unavailable:
        require_lineage_persistence_port(None)
    assert unavailable.value.reason_code == "lineage_persistence_unavailable"

    class FuturePort:
        async def persist(self, envelope, /) -> None:  # type: ignore[no-untyped-def]
            del envelope

    port = FuturePort()
    assert require_lineage_persistence_port(port) is port


def test_currency_mismatch_fails_before_plan_issuance() -> None:
    factory = MockLineageFactory()
    intent = factory.create_decision_intent(
        _intent_draft(target_notional_currency="USD")
    )

    with pytest.raises(ValueError) as error:
        factory.create_execution_plan(intent, _plan_draft(quote_currency="KRW"))
    assert str(error.value) == "currency_conversion_not_authorized"

"""Acceptance 2 and 3: gateway refusals, single forward, cancel under kill."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest

from app.schemas.execution_intent import AmendChild, CancelChild, SubmitChild
from app.services.execution_intent_gateway.decisions import (
    ChildState,
    IntentSnapshot,
    Refusal,
)
from app.services.execution_intent_gateway.gateway import ForwardResult, IntentGateway
from app.services.kis_mock_runner.control import KillMode
from tests.services.execution_intent_gateway.support import (
    ENABLED,
    NOW,
    FakeBroker,
    FakeKillSwitch,
    MemoryGatewayStore,
    intent_snapshot,
)


class Harness:
    def __init__(
        self,
        intent: IntentSnapshot | None = None,
        *,
        environment: dict[str, str] | None = None,
        kill_mode: KillMode = KillMode.ACTIVE,
        now: Any = NOW,
    ) -> None:
        self.events: list[str] = []
        self.intent = intent or intent_snapshot()
        self.store = MemoryGatewayStore(self.intent, events=self.events)
        self.broker = FakeBroker(events=self.events)
        self.kill = FakeKillSwitch(kill_mode)
        self.now = now
        self.gateway = IntentGateway(
            store=self.store,
            forwarders={"kis_mock": self.broker},
            kill_switch=self.kill,
            environment=ENABLED if environment is None else environment,
            clock=lambda: self.now,
        )

    def submit(self, key: str, quantity: int = 1, price: int = 70_000) -> SubmitChild:
        return SubmitChild(
            action="submit",
            intent_id=self.intent.intent_id,
            idempotency_key=key,
            quantity=quantity,
            limit_price_krw=price,
        )

    def amend(
        self, key: str, target: str, quantity: int = 1, price: int = 70_000
    ) -> AmendChild:
        return AmendChild(
            action="amend",
            intent_id=self.intent.intent_id,
            idempotency_key=key,
            target_idempotency_key=target,
            quantity=quantity,
            limit_price_krw=price,
        )

    def cancel(self, key: str, target: str) -> CancelChild:
        return CancelChild(
            action="cancel",
            intent_id=self.intent.intent_id,
            idempotency_key=key,
            target_idempotency_key=target,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "environment",
    [
        {},
        {"EXECUTION_INTENT_GATEWAY_ENABLED": "false"},
        {"EXECUTION_INTENT_GATEWAY_ENABLED": "1"},
        {"EXECUTION_INTENT_GATEWAY_ENABLED": "TRUE"},
        {"EXECUTION_INTENT_GATEWAY_ENABLED": " true "},
    ],
)
async def test_disabled_gate_refuses_every_action_before_any_state(
    environment: dict[str, str],
) -> None:
    h = Harness(environment=environment)
    for request in (
        h.submit("child-0001"),
        h.amend("child-0002", "child-0001"),
        h.cancel("child-0003", "child-0001"),
    ):
        outcome = await h.gateway.handle(request, confirm=True)
        assert (outcome.status, outcome.reason) == (
            "refused",
            Refusal.GATEWAY_DISABLED.value,
        )
    assert h.broker.calls == []
    assert h.store.children == []
    assert h.kill.reads == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("confirm", [False, 1, "true", None])
async def test_missing_per_call_confirm_is_refused(confirm: Any) -> None:
    h = Harness()
    outcome = await h.gateway.handle(h.submit("child-0001"), confirm=confirm)
    assert outcome.reason == Refusal.CONFIRM_REQUIRED.value
    assert h.broker.calls == []
    assert h.store.children == []


@pytest.mark.asyncio
async def test_default_call_does_not_send() -> None:
    h = Harness()
    outcome = await h.gateway.handle(h.submit("child-0001"))
    assert outcome.reason == Refusal.CONFIRM_REQUIRED.value
    assert h.broker.calls == []


def test_gateway_cannot_be_built_with_a_non_mock_forwarder() -> None:
    with pytest.raises(ValueError, match="non-mock"):
        IntentGateway(
            store=MemoryGatewayStore(),
            forwarders={"kis_live": FakeBroker()},
            kill_switch=FakeKillSwitch(),
            environment=ENABLED,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("account", ["kis_live", "toss_live", "upbit", "alpaca_paper"])
async def test_stored_non_mock_account_is_refused(account: str) -> None:
    h = Harness(intent_snapshot(account=account))
    for request in (h.submit("child-0001"), h.cancel("child-0002", "child-0001")):
        outcome = await h.gateway.handle(request, confirm=True)
        assert outcome.reason == Refusal.ACCOUNT_NOT_MOCK.value
    assert h.broker.calls == []


@pytest.mark.asyncio
async def test_mock_account_without_a_forwarder_is_refused() -> None:
    intent = intent_snapshot()
    gateway = IntentGateway(
        store=MemoryGatewayStore(intent),
        forwarders={},
        kill_switch=FakeKillSwitch(),
        environment=ENABLED,
        clock=lambda: NOW,
    )
    request = SubmitChild(
        action="submit",
        intent_id=intent.intent_id,
        idempotency_key="child-0001",
        quantity=1,
        limit_price_krw=70_000,
    )
    outcome = await gateway.handle(request, confirm=True)
    assert outcome.reason == Refusal.FORWARDER_UNAVAILABLE.value


@pytest.mark.asyncio
async def test_unknown_intent_is_refused() -> None:
    h = Harness()
    request = SubmitChild(
        action="submit",
        intent_id=uuid4(),
        idempotency_key="child-0001",
        quantity=1,
        limit_price_krw=70_000,
    )
    outcome = await h.gateway.handle(request, confirm=True)
    assert outcome.reason == Refusal.INTENT_NOT_FOUND.value
    assert h.broker.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("offset", [timedelta(0), timedelta(seconds=-1)])
async def test_expired_authorization_is_refused(offset: timedelta) -> None:
    h = Harness(intent_snapshot(expires_at=NOW + offset))
    outcome = await h.gateway.handle(h.submit("child-0001"), confirm=True)
    assert outcome.reason == Refusal.AUTHORIZATION_EXPIRED.value
    assert h.broker.calls == []


@pytest.mark.asyncio
async def test_authorization_still_valid_just_before_expiry() -> None:
    h = Harness(intent_snapshot(expires_at=NOW + timedelta(microseconds=1)))
    outcome = await h.gateway.handle(h.submit("child-0001"), confirm=True)
    assert outcome.status == "accepted"


@pytest.mark.asyncio
async def test_amend_after_expiry_is_refused_but_cancel_is_forwarded() -> None:
    h = Harness()
    await h.gateway.handle(h.submit("child-0001"), confirm=True)
    h.now = h.intent.expires_at
    amended = await h.gateway.handle(h.amend("child-0002", "child-0001"), confirm=True)
    assert amended.reason == Refusal.AUTHORIZATION_EXPIRED.value
    cancelled = await h.gateway.handle(
        h.cancel("child-0003", "child-0001"), confirm=True
    )
    assert cancelled.status == "accepted"
    assert h.broker.calls == [("submit", "child-0001"), ("cancel", "1001")]


@pytest.mark.asyncio
async def test_single_child_over_intent_quantity_cap_is_refused() -> None:
    h = Harness()
    outcome = await h.gateway.handle(h.submit("child-0001", quantity=11), confirm=True)
    assert outcome.reason == Refusal.QUANTITY_CAP_EXCEEDED.value
    assert h.broker.calls == []


@pytest.mark.asyncio
async def test_single_child_over_intent_notional_cap_is_refused() -> None:
    h = Harness(intent_snapshot(max_notional_krw=69_999))
    outcome = await h.gateway.handle(h.submit("child-0001"), confirm=True)
    assert outcome.reason == Refusal.NOTIONAL_CAP_EXCEEDED.value
    assert h.broker.calls == []


@pytest.mark.asyncio
async def test_child_over_locked_envelope_cap_is_refused() -> None:
    h = Harness(
        intent_snapshot(
            max_quantity=100,
            max_position_after=100,
            max_notional_krw=50_000_000,
        )
    )
    outcome = await h.gateway.handle(h.submit("child-0001", quantity=72), confirm=True)
    assert outcome.reason == Refusal.LOCKED_ENVELOPE_CAP_EXCEEDED.value
    assert h.broker.calls == []


@pytest.mark.asyncio
async def test_caps_bound_the_sum_of_children_exactly() -> None:
    h = Harness()
    first = await h.gateway.handle(h.submit("child-0001", quantity=4), confirm=True)
    second = await h.gateway.handle(h.submit("child-0002", quantity=6), confirm=True)
    over = await h.gateway.handle(h.submit("child-0003", quantity=1), confirm=True)
    assert (first.status, second.status) == ("accepted", "accepted")
    assert over.reason == Refusal.QUANTITY_CAP_EXCEEDED.value
    assert len(h.broker.calls) == 2


@pytest.mark.asyncio
async def test_notional_cap_bounds_the_sum_of_children() -> None:
    h = Harness(intent_snapshot(max_notional_krw=140_000))
    assert (await h.gateway.handle(h.submit("child-0001"), confirm=True)).status == (
        "accepted"
    )
    assert (await h.gateway.handle(h.submit("child-0002"), confirm=True)).status == (
        "accepted"
    )
    over = await h.gateway.handle(h.submit("child-0003"), confirm=True)
    assert over.reason == Refusal.NOTIONAL_CAP_EXCEEDED.value


@pytest.mark.asyncio
async def test_refused_children_do_not_spend_the_caps() -> None:
    h = Harness()
    await h.gateway.handle(h.submit("child-0001", quantity=11), confirm=True)
    outcome = await h.gateway.handle(h.submit("child-0002", quantity=10), confirm=True)
    assert outcome.status == "accepted"


@pytest.mark.asyncio
async def test_cancel_does_not_free_the_caps_without_fill_evidence() -> None:
    h = Harness()
    await h.gateway.handle(h.submit("child-0001", quantity=10), confirm=True)
    await h.gateway.handle(h.cancel("child-0002", "child-0001"), confirm=True)
    over = await h.gateway.handle(h.submit("child-0003", quantity=1), confirm=True)
    assert over.reason == Refusal.QUANTITY_CAP_EXCEEDED.value


@pytest.mark.asyncio
async def test_amend_cannot_raise_the_order_past_a_cap() -> None:
    h = Harness()
    await h.gateway.handle(h.submit("child-0001", quantity=5), confirm=True)
    await h.gateway.handle(h.submit("child-0002", quantity=5), confirm=True)
    over = await h.gateway.handle(
        h.amend("child-0003", "child-0001", quantity=6), confirm=True
    )
    assert over.reason == Refusal.QUANTITY_CAP_EXCEEDED.value


@pytest.mark.asyncio
async def test_amend_counts_the_order_once_at_its_high_water_mark() -> None:
    h = Harness()
    await h.gateway.handle(h.submit("child-0001", quantity=8), confirm=True)
    down = await h.gateway.handle(
        h.amend("child-0002", "child-0001", quantity=2), confirm=True
    )
    assert down.status == "accepted"
    # The order may still be at 8 if the amend did not apply, so only 2 are left.
    assert (
        await h.gateway.handle(h.submit("child-0003", quantity=3), confirm=True)
    ).reason == Refusal.QUANTITY_CAP_EXCEEDED.value
    assert (
        await h.gateway.handle(h.submit("child-0004", quantity=2), confirm=True)
    ).status == "accepted"


@pytest.mark.asyncio
async def test_amend_chain_keeps_one_order_exposure_and_targets_the_latest_id() -> None:
    h = Harness()
    await h.gateway.handle(h.submit("child-0001", quantity=5), confirm=True)
    await h.gateway.handle(
        h.amend("child-0002", "child-0001", quantity=5), confirm=True
    )
    await h.gateway.handle(
        h.amend("child-0003", "child-0002", quantity=5), confirm=True
    )
    await h.gateway.handle(h.submit("child-0004", quantity=5), confirm=True)
    assert h.broker.calls == [
        ("submit", "child-0001"),
        ("amend", "1001"),
        ("amend", "1002"),
        ("submit", "child-0004"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("side", "price", "reason"),
    [
        ("buy", 70_700, None),
        ("buy", 70_701, Refusal.PRICE_OUTSIDE_BOUND.value),
        ("sell", 69_300, None),
        ("sell", 69_299, Refusal.PRICE_OUTSIDE_BOUND.value),
    ],
)
async def test_child_price_stays_inside_the_limit_and_slippage_bound(
    side: str, price: int, reason: str | None
) -> None:
    h = Harness(
        intent_snapshot(
            side=side,
            position_at_authorization=10,
            min_position_after=0,
            max_position_after=20,
        )
    )
    outcome = await h.gateway.handle(h.submit("child-0001", price=price), confirm=True)
    assert outcome.reason == reason


@pytest.mark.asyncio
async def test_amend_price_stays_inside_the_bound() -> None:
    h = Harness()
    await h.gateway.handle(h.submit("child-0001"), confirm=True)
    outcome = await h.gateway.handle(
        h.amend("child-0002", "child-0001", price=70_701), confirm=True
    )
    assert outcome.reason == Refusal.PRICE_OUTSIDE_BOUND.value


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overrides", "quantity"),
    [
        (
            {
                "side": "buy",
                "position_at_authorization": 5,
                "max_position_after": 6,
            },
            2,
        ),
        (
            {
                "side": "sell",
                "position_at_authorization": 1,
                "max_position_after": 10,
            },
            2,
        ),
    ],
)
async def test_inventory_constraint_is_enforced(
    overrides: dict[str, Any], quantity: int
) -> None:
    h = Harness(intent_snapshot(**overrides))
    outcome = await h.gateway.handle(
        h.submit("child-0001", quantity=quantity), confirm=True
    )
    assert outcome.reason == Refusal.INVENTORY_CONSTRAINT_EXCEEDED.value
    assert h.broker.calls == []


@pytest.mark.asyncio
async def test_duplicate_key_forwards_exactly_once_and_returns_the_first_result() -> (
    None
):
    h = Harness()
    request = h.submit("child-0001")
    first = await h.gateway.handle(request, confirm=True)
    second = await h.gateway.handle(request, confirm=True)
    third = await h.gateway.handle(request, confirm=True)
    assert h.broker.calls == [("submit", "child-0001")]
    assert first.replayed is False
    for replay in (second, third):
        assert replay.replayed is True
        assert replay.status == first.status
        assert replay.broker_order_id == first.broker_order_id
        assert replay.child_id == first.child_id
        assert replay.result == first.result


@pytest.mark.asyncio
async def test_duplicate_cancel_forwards_exactly_once() -> None:
    h = Harness()
    await h.gateway.handle(h.submit("child-0001"), confirm=True)
    cancel = h.cancel("child-0002", "child-0001")
    await h.gateway.handle(cancel, confirm=True)
    replay = await h.gateway.handle(cancel, confirm=True)
    assert replay.replayed is True
    assert [call for call in h.broker.calls if call[0] == "cancel"] == [
        ("cancel", "1001")
    ]


@pytest.mark.asyncio
async def test_same_key_with_different_payload_is_a_conflict_and_never_sends() -> None:
    h = Harness()
    await h.gateway.handle(h.submit("child-0001", quantity=1), confirm=True)
    outcome = await h.gateway.handle(h.submit("child-0001", quantity=2), confirm=True)
    assert outcome.status == "conflict"
    assert h.broker.calls == [("submit", "child-0001")]


@pytest.mark.asyncio
async def test_replay_does_not_read_as_a_new_decision_after_a_refusal() -> None:
    h = Harness(kill_mode=KillMode.ENTRY_HALT)
    request = h.submit("child-0001")
    refused = await h.gateway.handle(request, confirm=True)
    h.kill.mode = KillMode.ACTIVE
    replay = await h.gateway.handle(request, confirm=True)
    assert refused.reason == Refusal.SUBMISSIONS_KILLED.value
    assert (replay.status, replay.reason, replay.replayed) == (
        "refused",
        Refusal.SUBMISSIONS_KILLED.value,
        True,
    )
    assert h.broker.calls == []


@pytest.mark.asyncio
async def test_journal_row_is_written_before_the_forward() -> None:
    h = Harness()
    await h.gateway.handle(h.submit("child-0001"), confirm=True)
    assert h.events == [
        "journal:sending:child-0001",
        "forward:submit:child-0001",
        "finish:accepted:child-0001",
    ]


@pytest.mark.asyncio
async def test_forward_exception_is_uncertain_and_is_never_resent() -> None:
    h = Harness()
    h.broker.raises = RuntimeError("secret-bearing transport detail")
    request = h.submit("child-0001")
    first = await h.gateway.handle(request, confirm=True)
    h.broker.raises = None
    replay = await h.gateway.handle(request, confirm=True)
    assert first.status == "uncertain"
    assert first.result == {"error_class": "RuntimeError"}
    assert (replay.status, replay.replayed) == ("uncertain", True)
    assert h.broker.calls == [("submit", "child-0001")]


@pytest.mark.asyncio
async def test_broker_failure_result_is_uncertain_not_a_clean_rejection() -> None:
    h = Harness()
    h.broker.accepted = False
    outcome = await h.gateway.handle(h.submit("child-0001"), confirm=True)
    assert outcome.status == "uncertain"


@pytest.mark.asyncio
async def test_cancelled_task_leaves_the_row_sending_and_no_resend_happens() -> None:
    h = Harness()
    h.broker.raises = asyncio.CancelledError()
    request = h.submit("child-0001")
    with pytest.raises(asyncio.CancelledError):
        await h.gateway.handle(request, confirm=True)
    assert h.store.child("child-0001").state is ChildState.SENDING
    h.broker.raises = None
    replay = await h.gateway.handle(request, confirm=True)
    assert (replay.status, replay.replayed) == ("sending", True)
    assert len(h.broker.calls) == 1


@pytest.mark.asyncio
async def test_outcome_write_failure_stays_sending_and_never_resends() -> None:
    h = Harness()
    h.store.finish_error = RuntimeError("db down")
    request = h.submit("child-0001")
    outcome = await h.gateway.handle(request, confirm=True)
    assert outcome.status == "sending"
    assert outcome.outcome_not_recorded is True
    assert outcome.broker_order_id == "1001"
    h.store.finish_error = None
    replay = await h.gateway.handle(request, confirm=True)
    assert (replay.status, replay.replayed) == ("sending", True)
    assert len(h.broker.calls) == 1


@pytest.mark.asyncio
async def test_result_that_cannot_be_journaled_still_records_an_outcome() -> None:
    h = Harness()

    async def submit(intent: Any, request: Any) -> Any:
        return ForwardResult(True, "1001", {"success": True, "bad": float("nan")})

    h.broker.submit = submit  # type: ignore[method-assign]
    outcome = await h.gateway.handle(h.submit("child-0001"), confirm=True)
    assert outcome.status == "accepted"
    assert outcome.result == {"unserializable": True, "success": True}


@pytest.mark.asyncio
async def test_accepted_submit_without_an_order_id_is_uncertain() -> None:
    h = Harness()

    async def submit(intent: Any, request: Any) -> Any:
        return ForwardResult(True, None, {"success": True})

    h.broker.submit = submit  # type: ignore[method-assign]
    outcome = await h.gateway.handle(h.submit("child-0001"), confirm=True)
    assert outcome.status == "uncertain"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [KillMode.ENTRY_HALT, KillMode.GLOBAL_FREEZE])
async def test_cancel_is_forwarded_while_submissions_are_killed(mode: KillMode) -> None:
    h = Harness()
    await h.gateway.handle(h.submit("child-0001"), confirm=True)
    h.kill.mode = mode

    submit = await h.gateway.handle(h.submit("child-0002"), confirm=True)
    amend = await h.gateway.handle(h.amend("child-0003", "child-0001"), confirm=True)
    cancel = await h.gateway.handle(h.cancel("child-0004", "child-0001"), confirm=True)

    assert submit.reason == Refusal.SUBMISSIONS_KILLED.value
    assert amend.reason == Refusal.SUBMISSIONS_KILLED.value
    assert cancel.status == "accepted"
    assert h.broker.calls == [("submit", "child-0001"), ("cancel", "1001")]


@pytest.mark.asyncio
async def test_unreadable_kill_state_blocks_submit_but_not_cancel() -> None:
    h = Harness()
    await h.gateway.handle(h.submit("child-0001"), confirm=True)
    h.kill.read_error = RuntimeError("control table unavailable")

    submit = await h.gateway.handle(h.submit("child-0002"), confirm=True)
    cancel = await h.gateway.handle(h.cancel("child-0003", "child-0001"), confirm=True)

    assert submit.reason == Refusal.SUBMISSIONS_KILLED.value
    assert cancel.status == "accepted"


@pytest.mark.asyncio
async def test_cancel_refuses_a_target_outside_this_intent_journal() -> None:
    h = Harness()
    outcome = await h.gateway.handle(h.cancel("child-0002", "child-0001"), confirm=True)
    assert outcome.reason == Refusal.TARGET_NOT_FOUND.value
    assert h.broker.calls == []


@pytest.mark.asyncio
async def test_target_without_a_broker_order_id_cannot_be_acted_on() -> None:
    h = Harness()
    h.broker.raises = RuntimeError("timeout")
    await h.gateway.handle(h.submit("child-0001"), confirm=True)
    h.broker.raises = None
    amend = await h.gateway.handle(h.amend("child-0002", "child-0001"), confirm=True)
    cancel = await h.gateway.handle(h.cancel("child-0003", "child-0001"), confirm=True)
    assert amend.reason == Refusal.TARGET_NOT_WORKING.value
    assert cancel.reason == Refusal.TARGET_NOT_WORKING.value
    assert len(h.broker.calls) == 1


@pytest.mark.asyncio
async def test_cancel_cannot_target_a_cancel_or_a_replaced_order() -> None:
    h = Harness()
    await h.gateway.handle(h.submit("child-0001"), confirm=True)
    await h.gateway.handle(h.amend("child-0002", "child-0001"), confirm=True)
    replaced = await h.gateway.handle(
        h.cancel("child-0003", "child-0001"), confirm=True
    )
    assert replaced.reason == Refusal.TARGET_SUPERSEDED.value
    await h.gateway.handle(h.cancel("child-0004", "child-0002"), confirm=True)
    of_cancel = await h.gateway.handle(
        h.cancel("child-0005", "child-0004"), confirm=True
    )
    assert of_cancel.reason == Refusal.TARGET_NOT_FOUND.value


@pytest.mark.asyncio
async def test_amend_of_a_superseded_order_is_refused() -> None:
    h = Harness()
    await h.gateway.handle(h.submit("child-0001"), confirm=True)
    await h.gateway.handle(h.amend("child-0002", "child-0001"), confirm=True)
    outcome = await h.gateway.handle(h.amend("child-0003", "child-0001"), confirm=True)
    assert outcome.reason == Refusal.TARGET_SUPERSEDED.value


@pytest.mark.asyncio
async def test_cancel_may_be_retried_with_a_new_key_after_an_unknown_outcome() -> None:
    h = Harness()
    await h.gateway.handle(h.submit("child-0001"), confirm=True)
    h.broker.raises = RuntimeError("timeout")
    first = await h.gateway.handle(h.cancel("child-0002", "child-0001"), confirm=True)
    h.broker.raises = None
    second = await h.gateway.handle(h.cancel("child-0003", "child-0001"), confirm=True)
    assert (first.status, second.status) == ("uncertain", "accepted")


@pytest.mark.asyncio
async def test_amend_cannot_target_a_cancel() -> None:
    h = Harness()
    await h.gateway.handle(h.submit("child-0001"), confirm=True)
    await h.gateway.handle(h.cancel("child-0002", "child-0001"), confirm=True)
    outcome = await h.gateway.handle(h.amend("child-0003", "child-0002"), confirm=True)
    assert outcome.reason == Refusal.TARGET_NOT_FOUND.value
    assert len(h.broker.calls) == 2


@pytest.mark.asyncio
async def test_sell_cannot_take_the_position_below_the_inventory_floor() -> None:
    h = Harness(
        intent_snapshot(
            side="sell",
            position_at_authorization=5,
            min_position_after=3,
            max_position_after=5,
        )
    )
    allowed = await h.gateway.handle(h.submit("child-0001", quantity=2), confirm=True)
    below = await h.gateway.handle(h.submit("child-0002", quantity=1), confirm=True)
    assert allowed.status == "accepted"
    assert below.reason == Refusal.INVENTORY_CONSTRAINT_EXCEEDED.value


@pytest.mark.asyncio
async def test_children_of_another_intent_do_not_spend_this_intent_caps() -> None:
    h = Harness()
    other = intent_snapshot()
    h.store.intents[other.intent_id] = other
    await h.gateway.handle(h.submit("child-0001", quantity=10), confirm=True)
    outcome = await h.gateway.handle(
        SubmitChild(
            action="submit",
            intent_id=other.intent_id,
            idempotency_key="child-0002",
            quantity=10,
            limit_price_krw=70_000,
        ),
        confirm=True,
    )
    assert outcome.status == "accepted"

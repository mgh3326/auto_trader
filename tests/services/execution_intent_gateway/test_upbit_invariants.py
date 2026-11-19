"""hk 5441 — the T3 invariant table of ``docs/design/5438-go-execution-path.md`` §2.

One failing-direction test per row I1..I10, named ``test_i<n>_*``: each sets up
the state the invariant forbids and asserts that nothing is forwarded. The rows
of §2.2-§2.6 that are pure rules (B7, B9, B11, B12, B17, S2, S4, S5 b, S6) follow.

Rows whose enforcement is a lock or a trigger have their DB test in
``test_upbit_db.py``; this file proves the decision.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from app.services.execution_intent_gateway.decisions import ChildState, Refusal
from app.services.execution_intent_gateway.forward_result import ForwardResult
from app.services.execution_intent_gateway.upbit_decisions import (
    SleeveView,
    check_before_send,
    check_storable,
    decide_upbit_child,
    evaluate_operator_exit,
    evaluate_shadow_sleeve,
)
from app.services.execution_intent_gateway.upbit_gateway import UpbitGatewayPath
from app.services.kis_mock_runner.control import KillMode
from app.services.strategy_sleeves.book import SleeveOrder
from tests.services.execution_intent_gateway.support import FakeKillSwitch
from tests.services.execution_intent_gateway.upbit_support import (
    ACCOUNT_REF,
    LIVE_ENV,
    NOW,
    FakeLiveForwarder,
    FakeShadowForwarder,
    MemoryUpbitStore,
    book,
    child_row,
    facts,
    held_book,
    mandate,
    operator_exit,
    reads,
    sell_intent,
    submit,
    symbol_view,
    upbit_intent,
    version,
)
from tests.services.execution_intent_gateway.upbit_support import (
    cancel as cancel_child,
)

pytestmark = pytest.mark.unit


def _path(
    store: MemoryUpbitStore,
    *,
    live: FakeLiveForwarder | None = None,
    kill: KillMode = KillMode.ACTIVE,
    environment: dict[str, str] | None = None,
    avg_price: Decimal | None = Decimal("2500"),
    cooldown: bool = False,
) -> tuple[UpbitGatewayPath, FakeShadowForwarder, FakeLiveForwarder]:
    shadow = FakeShadowForwarder()
    live = live or FakeLiveForwarder()

    async def avg_reader(_symbol: str) -> Decimal | None:
        return avg_price

    async def cooldown_reader(_symbol: str) -> bool:
        return cooldown

    path = UpbitGatewayPath(
        store=store,
        kill_switch=FakeKillSwitch(kill),
        shadow_forwarder=shadow,
        live_forwarder=live,
        avg_price_reader=avg_reader,
        cooldown_reader=cooldown_reader,
        environment=LIVE_ENV if environment is None else environment,
        clock=lambda: NOW,
    )
    return path, shadow, live


def _decide(intent, request, *, children=(), **overrides):
    return decide_upbit_child(
        request,
        intent=intent,
        children=list(children),
        facts=overrides.pop("facts", facts()),
        reads=overrides.pop("reads", reads()),
    )


# --------------------------------------------------------------------- I1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("the_mandate", "refusal"),
    [
        (None, Refusal.MANDATE_MISSING),
        (mandate(revoked=True), Refusal.MANDATE_REVOKED),
        (mandate(expires_at=NOW), Refusal.MANDATE_EXPIRED),
        (mandate(mandate_id=UUID(int=2)), Refusal.MANDATE_MISSING),
        (mandate(mode="shadow"), Refusal.MANDATE_SCOPE_MISMATCH),
        (mandate(sleeve_id=8), Refusal.MANDATE_SCOPE_MISMATCH),
        (
            mandate(allowed_symbols=frozenset({"KRW-BTC"})),
            Refusal.MANDATE_SCOPE_MISMATCH,
        ),
    ],
)
async def test_i1_no_child_is_forwarded_without_a_valid_mandate(
    the_mandate, refusal
) -> None:
    intent = upbit_intent()
    store = MemoryUpbitStore(intent, facts=facts(mandate=the_mandate))
    path, shadow, live = _path(store)

    outcome = await path.handle(submit(intent))

    assert (outcome.status, outcome.reason) == ("refused", refusal.value)
    assert store.child("child-0001").state is ChildState.REFUSED
    assert live.calls == [] and shadow.calls == []


def test_i1_an_intent_is_not_storable_without_a_valid_mandate() -> None:
    for the_mandate in (None, mandate(revoked=True), mandate(expires_at=NOW)):
        decision = check_storable(
            upbit_intent(), facts=facts(mandate=the_mandate), reads=reads()
        )
        assert decision.refusal is not None


# --------------------------------------------------------------------- I2


@pytest.mark.asyncio
async def test_i2_the_intent_caps_bound_everything_it_ever_sent() -> None:
    intent = upbit_intent(max_quantity_decimal=Decimal("15"))
    store = MemoryUpbitStore(intent, facts=facts())
    path, _shadow, live = _path(store)

    assert (await path.handle(submit(intent, "child-0001"))).status == "accepted"
    over = await path.handle(submit(intent, "child-0002"))

    assert over.reason == Refusal.QUANTITY_CAP_EXCEEDED.value
    assert live.calls == [("submit", "child-0001")]


def test_i2_notional_price_bound_and_expiry_refuse() -> None:
    intent = upbit_intent(max_notional_krw=20_000)
    assert _decide(intent, submit(intent)).refusal is Refusal.NOTIONAL_CAP_EXCEEDED
    # A buy may pay at most limit x (1 + 30bp) = 3009.
    assert (
        _decide(upbit_intent(), submit(intent, limit_price_krw=3_010)).refusal
        is Refusal.PRICE_OUTSIDE_BOUND
    )
    expired = facts(now=intent.expires_at)
    assert (
        _decide(upbit_intent(), submit(intent), facts=expired).refusal
        is Refusal.AUTHORIZATION_EXPIRED
    )


def test_i2_a_cancelled_order_still_counts_against_the_caps() -> None:
    intent = upbit_intent(max_quantity_decimal=Decimal("15"))
    sent = child_row("child-0001", quantity="10")
    cancelled = child_row(
        "child-0002",
        action="cancel",
        quantity=None,
        price=None,
        target="child-0001",
        child_id=2,
    )
    decision = _decide(intent, submit(intent, "child-0003"), children=[sent, cancelled])
    assert decision.refusal is Refusal.QUANTITY_CAP_EXCEEDED


# --------------------------------------------------------------------- I3


@pytest.mark.parametrize(
    "overrides",
    [
        {"max_notional_krw": 480_001},
        {"max_slippage_bps": 31},
        {"expires_at": NOW + timedelta(hours=12, seconds=1)},
        {"expires_at": NOW + timedelta(days=31)},
        {"strategy_code_sha": "b" * 40},
        {"symbol": "KRW-DOGE"},
    ],
)
def test_i3_an_intent_wider_than_its_mandate_is_not_storable(overrides) -> None:
    decision = check_storable(upbit_intent(**overrides), facts=facts(), reads=reads())
    assert decision.refusal is Refusal.MANDATE_SCOPE_MISMATCH


# --------------------------------------------------------------------- I4


def test_i4_a_buy_over_the_sleeve_budget_is_refused() -> None:
    tight = SleeveView(version=version(budget_cap=Decimal("20000")), book=book())
    intent = upbit_intent()
    assert (
        _decide(intent, submit(intent), facts=facts(sleeve=tight)).refusal
        is Refusal.SLEEVE_BUDGET_EXCEEDED
    )
    assert (
        check_storable(intent, facts=facts(sleeve=tight), reads=reads()).refusal
        is Refusal.SLEEVE_BUDGET_EXCEEDED
    )


def test_i4_budget_reserved_by_other_open_intents_counts() -> None:
    sleeve = SleeveView(
        version=version(budget_cap=Decimal("500000")),
        book=book(),
        reserved_buy_notional=Decimal("480000"),
    )
    intent = upbit_intent()
    assert (
        _decide(intent, submit(intent), facts=facts(sleeve=sleeve)).refusal
        is Refusal.SLEEVE_BUDGET_EXCEEDED
    )


def test_i4_unmeasured_used_is_a_refusal_not_zero() -> None:
    unpriced = SleeveOrder(
        proposal_pk=0,
        rung_id=-9,
        symbol="KRW-BTC",
        side="buy",
        state="resting",
        quantity=Decimal("1"),
        limit_price=None,
        notional=None,
        rung_filled_qty=None,
    )
    sleeve = SleeveView(version=version(), book=book(orders=[unpriced]))
    intent = upbit_intent()
    assert (
        _decide(intent, submit(intent), facts=facts(sleeve=sleeve)).refusal
        is Refusal.SLEEVE_USED_UNMEASURED
    )


def test_i4_more_open_buy_intents_than_the_mandate_allows_is_not_storable() -> None:
    decision = check_storable(
        upbit_intent(), facts=facts(open_buy_intents=4), reads=reads()
    )
    assert decision.refusal is Refusal.MAX_OPEN_INTENTS_EXCEEDED


# --------------------------------------------------------------------- I5


@pytest.mark.asyncio
async def test_i5_one_key_sends_once_and_replays() -> None:
    intent = upbit_intent()
    store = MemoryUpbitStore(intent, facts=facts())
    path, _shadow, live = _path(store)

    first = await path.handle(submit(intent))
    again = await path.handle(submit(intent))

    assert first.status == "accepted" and again.replayed is True
    assert again.broker_order_id == first.broker_order_id
    assert live.calls == [("submit", "child-0001")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result",
    [
        ForwardResult(accepted=False, broker_order_id=None, result={"success": False}),
        ForwardResult(accepted=True, broker_order_id=None, result={"success": True}),
        RuntimeError("socket closed"),
    ],
)
async def test_i5_an_unknown_outcome_is_never_sent_again(result) -> None:
    intent = upbit_intent()
    store = MemoryUpbitStore(intent, facts=facts())
    path, _shadow, live = _path(store, live=FakeLiveForwarder(result))

    first = await path.handle(submit(intent))
    again = await path.handle(submit(intent))

    assert first.status == "uncertain" and again.status == "uncertain"
    assert again.replayed is True
    assert live.calls == [("submit", "child-0001")]


@pytest.mark.asyncio
async def test_i5_a_lost_outcome_record_stays_sending_and_is_not_resent() -> None:
    intent = upbit_intent()
    store = MemoryUpbitStore(intent, facts=facts())
    store.finish_fails = True
    path, _shadow, live = _path(store)

    first = await path.handle(submit(intent))
    again = await path.handle(submit(intent))

    assert first.status == "sending" and first.outcome_not_recorded is True
    assert again.status == "sending" and again.replayed is True
    assert live.calls == [("submit", "child-0001")]


@pytest.mark.asyncio
async def test_i5_the_journal_row_commits_before_the_forward() -> None:
    intent = upbit_intent()
    store = MemoryUpbitStore(intent, facts=facts())

    class Recording(FakeLiveForwarder):
        async def submit(self, intent, request, *, child_id):
            store.events.append("forward")
            return await super().submit(intent, request, child_id=child_id)

    path, _shadow, _live = _path(store, live=Recording())
    await path.handle(submit(intent))

    assert store.events == [
        "journal:child-0001:sending",
        "forward",
        "finish:child-0001:accepted",
    ]


# --------------------------------------------------------------------- I6


@pytest.mark.asyncio
async def test_i6_a_shadow_intent_never_reaches_the_live_forwarder() -> None:
    intent = upbit_intent(execution_mode="shadow")
    shadow_facts = facts(
        mandate=mandate(mode="shadow"),
        sleeve=SleeveView(version=version("shadow"), book=book()),
    )
    store = MemoryUpbitStore(intent, facts=shadow_facts)
    path, shadow, live = _path(store)

    outcome = await path.handle(submit(intent))

    assert outcome.status == "shadowed"
    assert outcome.broker_order_id is None
    assert store.child("child-0001").state is ChildState.SHADOWED
    assert shadow.calls == ["child-0001"] and live.calls == []


@pytest.mark.asyncio
async def test_i6_a_shadow_row_records_no_broker_order_whatever_was_returned() -> None:
    intent = upbit_intent(execution_mode="shadow")
    shadow_facts = facts(
        mandate=mandate(mode="shadow"),
        sleeve=SleeveView(version=version("shadow"), book=book()),
    )
    store = MemoryUpbitStore(intent, facts=shadow_facts)
    path, shadow, _live = _path(store)

    async def lying_submit(_intent, _request):
        return ForwardResult(
            accepted=True, broker_order_id="uuid-x", result={}, shadowed=True
        )

    shadow.submit = lying_submit  # type: ignore[method-assign]
    outcome = await path.handle(submit(intent))

    assert outcome.status == "shadowed" and outcome.broker_order_id is None
    assert store.child("child-0001").broker_order_id is None


def test_i6_a_shadow_intent_under_a_live_mandate_is_refused() -> None:
    intent = upbit_intent(execution_mode="shadow")
    decision = _decide(
        intent,
        submit(intent),
        facts=facts(sleeve=SleeveView(version=version("shadow"), book=book())),
    )
    assert decision.refusal is Refusal.MANDATE_SCOPE_MISMATCH


# --------------------------------------------------------------------- I7


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [KillMode.GLOBAL_FREEZE, KillMode.ENTRY_HALT])
async def test_i7_a_killed_account_refuses_a_submit_and_allows_a_cancel(mode) -> None:
    intent = upbit_intent()
    store = MemoryUpbitStore(intent, facts=facts())
    path, _shadow, live = _path(store)
    assert (await path.handle(submit(intent, "child-0001"))).status == "accepted"

    killed, _shadow2, _live2 = _path(store, live=live, kill=mode)
    refused = await killed.handle(submit(intent, "child-0002"))
    cancelled = await killed.handle(cancel_child(intent, "child-0001"))

    assert refused.reason == Refusal.SUBMISSIONS_KILLED.value
    assert cancelled.status == "accepted"
    assert live.calls == [("submit", "child-0001"), ("cancel", "child-0009")]


@pytest.mark.asyncio
async def test_i7_the_live_switch_off_calls_no_upbit_order_function() -> None:
    intent = upbit_intent()
    store = MemoryUpbitStore(intent, facts=facts())
    path, _shadow, live = _path(store, environment={})

    outcome = await path.handle(submit(intent))

    assert outcome.reason == Refusal.UPBIT_LIVE_DISABLED.value
    assert store.children == [] and live.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["", "TRUE", "True", "1", "yes", " true"])
async def test_i7_the_live_switch_is_exactly_true(value) -> None:
    intent = upbit_intent()
    store = MemoryUpbitStore(intent, facts=facts())
    path, _shadow, live = _path(
        store, environment={"EXECUTION_INTENT_GATEWAY_UPBIT_LIVE_ENABLED": value}
    )
    assert (await path.handle(submit(intent))).reason == "upbit_live_disabled"
    assert live.calls == []


@pytest.mark.asyncio
async def test_i7_a_revoked_mandate_stops_the_next_submit_but_not_a_cancel() -> None:
    intent = upbit_intent()
    store = MemoryUpbitStore(intent, facts=facts())
    path, _shadow, live = _path(store)
    assert (await path.handle(submit(intent, "child-0001"))).status == "accepted"

    store.facts = facts(mandate=mandate(revoked=True))
    refused = await path.handle(submit(intent, "child-0002"))
    cancelled = await path.handle(cancel_child(intent, "child-0001"))

    assert refused.reason == Refusal.MANDATE_REVOKED.value
    assert cancelled.status == "accepted"


# --------------------------------------------------------------------- I8


def test_i8_a_sleeve_at_its_loss_limit_refuses_a_buy_and_asks_for_the_stop() -> None:
    losing = held_book(quantity="100", price="3000")
    marked_down = SleeveView(
        version=version(loss_limit=Decimal("100000")),
        book=book(
            orders=[
                SleeveOrder(
                    proposal_pk=0,
                    rung_id=-1,
                    symbol="KRW-XRP",
                    side="buy",
                    state="filled",
                    quantity=Decimal("100"),
                    limit_price=Decimal("3000"),
                    notional=None,
                    rung_filled_qty=Decimal("100"),
                )
            ],
            fills=losing and _fills_of(losing),
            marks={"KRW-XRP": Decimal("1500")},
        ),
    )
    intent = upbit_intent(symbol="KRW-BTC", limit_price_krw=150_000_000)
    request = submit(intent, quantity_decimal="0.001", limit_price_krw=150_000_000)

    decision = _decide(intent, request, facts=facts(sleeve=marked_down))

    assert decision.refusal is Refusal.SLEEVE_LOSS_LIMIT_REACHED
    assert decision.stop_sleeve is not None and decision.stop_sleeve.sleeve_id == 7


def _fills_of(_book):
    from app.services.strategy_sleeves.book import SleeveFill

    return [
        SleeveFill(
            ledger_id=1,
            rung_id=-1,
            symbol="KRW-XRP",
            side="buy",
            quantity=Decimal("100"),
            price=Decimal("3000"),
            fee=Decimal("0"),
            filled_at=NOW,
        )
    ]


@pytest.mark.parametrize("state", ["stopped", "paused"])
def test_i8_a_stopped_sleeve_takes_no_new_intent_and_no_submit(state) -> None:
    stopped = facts(sleeve=SleeveView(version=version(state), book=book()))
    intent = upbit_intent()
    assert (
        check_storable(intent, facts=stopped, reads=reads()).refusal
        is Refusal.SLEEVE_NOT_LIVE
    )
    assert (
        _decide(intent, submit(intent), facts=stopped).refusal
        is Refusal.SLEEVE_NOT_LIVE
    )


# --------------------------------------------------------------------- I9


def test_i9_a_sell_above_proven_holdings_is_refused() -> None:
    intent = sell_intent()
    short = facts(
        sleeve=SleeveView(version=version(), book=held_book()),
        symbol=symbol_view(holdings=Decimal("5")),
    )
    request = submit(intent, quantity_decimal="10", limit_price_krw=3_000)

    assert _decide(intent, request, facts=short).refusal is (
        Refusal.INVENTORY_CONSTRAINT_EXCEEDED
    )
    assert (
        check_storable(intent, facts=short, reads=reads()).refusal
        is Refusal.INVENTORY_CONSTRAINT_EXCEEDED
    )


def test_i9_unknown_holdings_refuse_a_sell() -> None:
    intent = sell_intent()
    unknown = facts(
        sleeve=SleeveView(version=version(), book=held_book()),
        symbol=symbol_view(holdings=None),
    )
    assert (
        _decide(intent, submit(intent), facts=unknown).refusal
        is Refusal.SLEEVE_HOLDINGS_UNMEASURED
    )


def test_i9_an_intent_cannot_leave_a_negative_position() -> None:
    intent = sell_intent(
        max_quantity_decimal=Decimal("100"),
        position_at_authorization_decimal=Decimal("5"),
    )
    holding = facts(
        sleeve=SleeveView(version=version(), book=held_book()),
        symbol=symbol_view(holdings=Decimal("100")),
    )
    assert (
        _decide(intent, submit(intent, quantity_decimal="10"), facts=holding).refusal
        is Refusal.INVENTORY_CONSTRAINT_EXCEEDED
    )


# --------------------------------------------------------------------- I10


def _holding_facts(**overrides):
    return facts(
        sleeve=SleeveView(version=version(), book=held_book()),
        symbol=symbol_view(holdings=Decimal("100")),
        **overrides,
    )


@pytest.mark.asyncio
async def test_i10_a_sell_below_the_loss_sell_floor_is_not_forwarded() -> None:
    # Floor = average buy price x 1.01 = 3030; the sell asks 3000.
    intent = sell_intent()
    store = MemoryUpbitStore(intent, facts=_holding_facts())
    path, _shadow, live = _path(store, avg_price=Decimal("3000"))

    outcome = await path.handle(submit(intent, limit_price_krw=3_000))

    assert outcome.reason == Refusal.LOSS_SELL_REQUIRES_CARD.value
    assert live.calls == []
    # S5 (a): the live refusal opens the exit handoff with the refused row.
    assert store.handoffs_opened == [(intent.intent_id, "loss_sell_requires_card")]


def test_i10_the_floor_is_exactly_average_times_101_percent() -> None:
    intent = sell_intent(limit_price_krw=3_030)
    at_floor = submit(intent, limit_price_krw=3_030)
    below = submit(intent, limit_price_krw=3_029)
    avg = reads(avg_buy_price=Decimal("3000"))

    assert _decide(intent, at_floor, facts=_holding_facts(), reads=avg).refusal is None
    assert (
        _decide(intent, below, facts=_holding_facts(), reads=avg).refusal
        is Refusal.LOSS_SELL_REQUIRES_CARD
    )


@pytest.mark.asyncio
async def test_i10_a_buy_in_cooldown_is_not_forwarded() -> None:
    intent = upbit_intent()
    store = MemoryUpbitStore(intent, facts=facts())
    path, _shadow, live = _path(store, cooldown=True)

    outcome = await path.handle(submit(intent))

    assert outcome.reason == Refusal.COOLDOWN_ACTIVE.value and live.calls == []


@pytest.mark.asyncio
async def test_i10_an_unreadable_cooldown_blocks_the_buy() -> None:
    intent = upbit_intent()
    store = MemoryUpbitStore(intent, facts=facts())
    path, _shadow, live = _path(store)

    async def broken(_symbol: str) -> bool:
        raise RuntimeError("redis down")

    path._cooldown_reader = broken  # type: ignore[assignment]
    assert (await path.handle(submit(intent))).reason == "cooldown_active"
    assert live.calls == []


# ------------------------------------------------------------ B7, B9, B11, B12


def test_b7_a_sell_has_no_notional_cap() -> None:
    # 100 x 9,000,000 = 900,000,000 KRW, far above the mandate's 480,000.
    intent = sell_intent(max_notional_krw=5_000, limit_price_krw=9_000_000)
    request = submit(intent, quantity_decimal="100", limit_price_krw=9_000_000)
    high = reads(avg_buy_price=Decimal("3000"))
    assert _decide(intent, request, facts=_holding_facts(), reads=high).refusal is None
    assert check_storable(intent, facts=_holding_facts(), reads=high).refusal is None


def test_b7_the_same_cap_refuses_a_buy() -> None:
    # 10 x 3,000 = 30,000 KRW against a 5,000 KRW intent cap.
    intent = upbit_intent(max_notional_krw=5_000)
    assert _decide(intent, submit(intent)).refusal is Refusal.NOTIONAL_CAP_EXCEEDED


@pytest.mark.parametrize("avg", [None, Decimal("0"), Decimal("-1"), Decimal("NaN")])
async def test_b9_an_unproven_average_price_means_no_automatic_sell(avg) -> None:
    intent = sell_intent()
    store = MemoryUpbitStore(intent, facts=_holding_facts())
    path, _shadow, live = _path(store, avg_price=avg)

    outcome = await path.handle(submit(intent))

    assert outcome.reason == Refusal.AVG_PRICE_UNPROVEN.value
    assert live.calls == [] and store.handoffs_opened == []


@pytest.mark.asyncio
async def test_b9_a_failed_average_price_read_is_unproven() -> None:
    intent = sell_intent()
    store = MemoryUpbitStore(intent, facts=_holding_facts())
    path, _shadow, live = _path(store)

    async def broken(_symbol: str) -> Decimal | None:
        raise RuntimeError("upbit unreachable")

    path._avg_price_reader = broken  # type: ignore[assignment]
    assert (await path.handle(submit(intent))).reason == "avg_price_unproven"
    assert live.calls == []


def test_b11_another_code_sha_is_refused_at_store_and_at_reservation() -> None:
    intent = upbit_intent(strategy_code_sha="c" * 40)
    assert (
        check_storable(intent, facts=facts(), reads=reads()).refusal
        is Refusal.MANDATE_SCOPE_MISMATCH
    )
    assert _decide(intent, submit(intent)).refusal is Refusal.MANDATE_SCOPE_MISMATCH


@pytest.mark.parametrize("configured", [None, "", "upbit:upbit:" + "c" * 64])
def test_b12_another_account_is_refused_before_the_send(configured) -> None:
    refusal = check_before_send(
        upbit_intent(),
        quantity=Decimal("10"),
        facts=facts(),
        reads=reads(),
        configured_account_ref=configured,
    )
    assert refusal is Refusal.ACCOUNT_MISMATCH


def test_b12_the_same_account_passes_the_last_look() -> None:
    assert (
        check_before_send(
            upbit_intent(),
            quantity=Decimal("10"),
            facts=facts(),
            reads=reads(),
            configured_account_ref=ACCOUNT_REF,
        )
        is None
    )


@pytest.mark.parametrize(
    ("changed", "refusal"),
    [
        ({"mandate": mandate(revoked=True)}, Refusal.MANDATE_REVOKED),
        ({"mandate": mandate(expires_at=NOW)}, Refusal.MANDATE_EXPIRED),
        ({"symbol": symbol_view(handoff_active=True)}, Refusal.EXIT_HANDOFF_ACTIVE),
        (
            {"sleeve": SleeveView(version=version("stopped"), book=book())},
            Refusal.SLEEVE_NOT_LIVE,
        ),
    ],
)
def test_the_last_look_sees_what_changed_after_the_reservation(
    changed, refusal
) -> None:
    assert (
        check_before_send(
            upbit_intent(),
            quantity=Decimal("10"),
            facts=facts(**changed),
            reads=reads(),
            configured_account_ref=ACCOUNT_REF,
        )
        is refusal
    )


def test_the_last_look_refuses_under_a_kill_or_with_the_switch_off() -> None:
    for the_reads, refusal in (
        (reads(kill_mode=KillMode.GLOBAL_FREEZE), Refusal.SUBMISSIONS_KILLED),
        (reads(live_enabled=False), Refusal.UPBIT_LIVE_DISABLED),
    ):
        assert (
            check_before_send(
                upbit_intent(),
                quantity=Decimal("10"),
                facts=facts(),
                reads=the_reads,
                configured_account_ref=ACCOUNT_REF,
            )
            is refusal
        )


# ------------------------------------------------------------ B17, B21 (shadow)


def test_b17_a_shadow_sleeve_is_judged_from_its_own_book() -> None:
    assert (
        evaluate_shadow_sleeve(
            version=version("live"),
            book=book(),
            order_buy_notional=Decimal("1"),
            places_buy=True,
        )
        is Refusal.SLEEVE_NOT_SHADOW
    )
    assert (
        evaluate_shadow_sleeve(
            version=version("shadow", budget_cap=Decimal("10")),
            book=book(),
            order_buy_notional=Decimal("11"),
            places_buy=True,
        )
        is Refusal.SLEEVE_BUDGET_EXCEEDED
    )
    assert (
        evaluate_shadow_sleeve(
            version=version("shadow"),
            book=None,
            order_buy_notional=Decimal("1"),
            places_buy=True,
        )
        is Refusal.SLEEVE_UNREADABLE
    )


@pytest.mark.asyncio
async def test_b21_a_shadow_loss_sell_refusal_opens_no_handoff() -> None:
    intent = sell_intent(execution_mode="shadow")
    shadow_facts = facts(
        mandate=mandate(mode="shadow"),
        sleeve=SleeveView(version=version("shadow"), book=held_book()),
        symbol=symbol_view(holdings=Decimal("100")),
    )
    store = MemoryUpbitStore(intent, facts=shadow_facts)
    path, shadow, live = _path(store, avg_price=Decimal("3000"))

    outcome = await path.handle(submit(intent, limit_price_krw=3_000))

    assert outcome.reason == Refusal.LOSS_SELL_REQUIRES_CARD.value
    assert store.handoffs_opened == [] and shadow.calls == [] and live.calls == []


# ------------------------------------------------------------ S2, S4, S5 (b), B5


def test_s2_a_second_open_sell_intent_on_the_symbol_is_refused() -> None:
    other = uuid4()
    blocked = facts(
        sleeve=SleeveView(version=version(), book=held_book()),
        symbol=symbol_view(
            holdings=Decimal("100"), open_sell_intents=frozenset({other})
        ),
    )
    intent = sell_intent()
    assert (
        check_storable(intent, facts=blocked, reads=reads()).refusal
        is Refusal.OPEN_SELL_INTENT_EXISTS
    )
    assert (
        _decide(intent, submit(intent), facts=blocked).refusal
        is Refusal.OPEN_SELL_INTENT_EXISTS
    )


def test_s2_the_intent_itself_does_not_block_its_own_child() -> None:
    intent = sell_intent()
    own = facts(
        sleeve=SleeveView(version=version(), book=held_book()),
        symbol=symbol_view(
            holdings=Decimal("100"), open_sell_intents=frozenset({intent.intent_id})
        ),
    )
    assert _decide(intent, submit(intent), facts=own).refusal is None


@pytest.mark.parametrize(
    ("mode", "state", "refusal"),
    [
        ("live", "shadow", Refusal.SLEEVE_NOT_LIVE),
        ("live", "paused", Refusal.SLEEVE_NOT_LIVE),
        ("shadow", "live", Refusal.SLEEVE_NOT_SHADOW),
    ],
)
def test_s4_the_sleeve_state_must_match_the_execution_mode(
    mode, state, refusal
) -> None:
    intent = upbit_intent(execution_mode=mode)
    the_facts = facts(
        mandate=mandate(mode=mode),
        sleeve=SleeveView(version=version(state), book=book()),
    )
    assert _decide(intent, submit(intent), facts=the_facts).refusal is refusal


@pytest.mark.parametrize("side", ["buy", "sell"])
def test_s5_an_open_handoff_refuses_buys_and_sells_on_the_symbol(side) -> None:
    intent = upbit_intent() if side == "buy" else sell_intent()
    handed_off = facts(
        sleeve=SleeveView(version=version(), book=held_book()),
        symbol=symbol_view(holdings=Decimal("100"), handoff_active=True),
    )
    assert (
        check_storable(intent, facts=handed_off, reads=reads()).refusal
        is Refusal.EXIT_HANDOFF_ACTIVE
    )
    assert (
        _decide(intent, submit(intent), facts=handed_off).refusal
        is Refusal.EXIT_HANDOFF_ACTIVE
    )


def test_s5_a_cancel_passes_while_the_symbol_is_handed_off() -> None:
    intent = upbit_intent()
    handed_off = facts(symbol=symbol_view(handoff_active=True))
    decision = decide_upbit_child(
        cancel_child(intent, "child-0001"),
        intent=intent,
        children=[child_row("child-0001")],
        facts=handed_off,
        reads=reads(kill_mode=None),
    )
    assert decision.refusal is None and decision.target_broker_order_id == "uuid-1"


def test_b5_an_unresolved_order_keeps_the_symbol_reserved() -> None:
    blocked = facts(symbol=symbol_view(unresolved_prior_order=True))
    intent = upbit_intent()
    assert (
        check_storable(intent, facts=blocked, reads=reads()).refusal
        is Refusal.UNRESOLVED_PRIOR_ORDER
    )
    assert (
        _decide(intent, submit(intent), facts=blocked).refusal
        is Refusal.UNRESOLVED_PRIOR_ORDER
    )


# ------------------------------------------------------------ S6 operator exit


def _exit_intent(**overrides):
    values = {
        "origin": "operator_exit",
        "operator_exit_id": uuid4(),
        "expires_at": NOW + timedelta(minutes=20),
    }
    return sell_intent(**{**values, **overrides})


def _exit_facts(intent, **overrides):
    values = {
        "operator_exit": operator_exit(intent),
        "sleeve": SleeveView(version=version("stopped"), book=held_book()),
        "symbol": symbol_view(holdings=Decimal("100")),
    }
    return facts(**{**values, **overrides})


@pytest.mark.parametrize("state", ["paused", "stopped", "shadow", "live"])
def test_s6_an_operator_exit_sells_whatever_the_sleeve_state(state) -> None:
    intent = _exit_intent()
    the_facts = _exit_facts(
        intent, sleeve=SleeveView(version=version(state), book=held_book())
    )
    assert check_storable(intent, facts=the_facts, reads=reads()).refusal is None
    assert _decide(intent, submit(intent), facts=the_facts).refusal is None
    assert (
        check_before_send(
            intent,
            quantity=Decimal("10"),
            facts=the_facts,
            reads=reads(),
            configured_account_ref=ACCOUNT_REF,
        )
        is None
    )


@pytest.mark.parametrize(
    "the_mandate",
    [mandate(revoked=True), mandate(expires_at=NOW - timedelta(days=1))],
)
def test_s6_a_revoked_or_expired_mandate_does_not_strand_the_position(
    the_mandate,
) -> None:
    intent = _exit_intent()
    the_facts = _exit_facts(intent, mandate=the_mandate)
    assert check_storable(intent, facts=the_facts, reads=reads()).refusal is None
    assert _decide(intent, submit(intent), facts=the_facts).refusal is None
    assert (
        check_before_send(
            intent,
            quantity=Decimal("10"),
            facts=the_facts,
            reads=reads(),
            configured_account_ref=ACCOUNT_REF,
        )
        is None
    )


def test_s6_an_operator_exit_is_never_a_buy() -> None:
    intent = _exit_intent(side="buy")
    the_facts = _exit_facts(intent)
    assert (
        evaluate_operator_exit(
            intent, quantity=Decimal("1"), facts=the_facts, reads=reads()
        )
        is Refusal.OPERATOR_EXIT_INVALID
    )


@pytest.mark.parametrize(
    "exit_overrides",
    [
        {"expires_at": NOW},
        {"max_quantity": Decimal("5")},
        {"symbol": "KRW-BTC"},
        {"mandate_id": UUID(int=9)},
    ],
)
def test_s6_an_expired_or_narrower_exit_row_refuses(exit_overrides) -> None:
    intent = _exit_intent()
    the_facts = _exit_facts(
        intent, operator_exit=operator_exit(intent, **exit_overrides)
    )
    assert (
        check_storable(intent, facts=the_facts, reads=reads()).refusal
        is Refusal.OPERATOR_EXIT_INVALID
    )


def test_s6_an_exit_without_its_row_refuses() -> None:
    intent = _exit_intent()
    assert (
        check_storable(
            intent, facts=_exit_facts(intent, operator_exit=None), reads=reads()
        ).refusal
        is Refusal.OPERATOR_EXIT_INVALID
    )


@pytest.mark.parametrize(
    ("overrides", "the_reads", "refusal"),
    [
        (
            {"symbol": symbol_view(holdings=Decimal("100"), handoff_active=True)},
            reads(),
            Refusal.EXIT_HANDOFF_ACTIVE,
        ),
        (
            {"symbol": symbol_view(holdings=Decimal("5"))},
            reads(),
            Refusal.INVENTORY_CONSTRAINT_EXCEEDED,
        ),
        (
            {"symbol": symbol_view(holdings=None)},
            reads(),
            Refusal.SLEEVE_HOLDINGS_UNMEASURED,
        ),
        ({}, reads(kill_mode=KillMode.GLOBAL_FREEZE), Refusal.SUBMISSIONS_KILLED),
    ],
)
def test_s6_what_protects_the_account_still_applies(
    overrides, the_reads, refusal
) -> None:
    intent = _exit_intent()
    the_facts = _exit_facts(intent, **overrides)
    assert (
        evaluate_operator_exit(
            intent, quantity=Decimal("10"), facts=the_facts, reads=the_reads
        )
        is refusal
    )


def test_s6_an_exit_below_the_floor_goes_to_the_handoff_too() -> None:
    intent = _exit_intent()
    decision = _decide(
        intent,
        submit(intent, limit_price_krw=3_000),
        facts=_exit_facts(intent),
        reads=reads(avg_buy_price=Decimal("3000")),
    )
    assert decision.refusal is Refusal.LOSS_SELL_REQUIRES_CARD
    assert decision.open_handoff is True


# ------------------------------------------------------------ shape (§3.3, §3.4)


def test_an_off_tick_price_is_refused() -> None:
    # 10,000-50,000 KRW moves in 10 KRW steps.
    intent = upbit_intent(limit_price_krw=20_000)
    request = submit(intent, quantity_decimal="1", limit_price_krw=20_005)
    assert _decide(intent, request).refusal is Refusal.PRICE_OFF_TICK


def test_a_sub_krw_tick_range_is_refused() -> None:
    intent = upbit_intent(limit_price_krw=50)
    request = submit(intent, quantity_decimal="200", limit_price_krw=50)
    assert _decide(intent, request).refusal is Refusal.PRICE_TICK_UNSUPPORTED


def test_a_key_too_long_for_an_upbit_identifier_leaves_no_row() -> None:
    intent = upbit_intent()
    decision = _decide(intent, submit(intent, "k" * 41))
    assert decision.refusal is Refusal.CHILD_SHAPE_INVALID_FOR_ACCOUNT
    assert decision.journal is False

"""hk 5441 — a go sleeve's intent orders: resolution, the shadow book and the
sleeve views the gateway reads (design §1.4, §3.6, B3, B5, B18, S2, I4).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from app.services.execution_intent_gateway.upbit_store import sleeve_symbol_views
from app.services.strategy_sleeves.book import SleeveFill, compute_sleeve_book
from app.services.strategy_sleeves.intent_orders import (
    ChildOrderEvidence,
    IntentChildOrder,
    build_child_orders,
    child_order_key,
    live_orders,
    shadow_book_inputs,
    unresolved_orders,
)
from app.services.strategy_sleeves.store import mark_children_with_unbooked_fills

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 20, 5, 20, tzinfo=UTC)
INTENT = uuid4()


def order(
    child_id: int = 1,
    *,
    state: str = "accepted",
    side: str = "buy",
    symbol: str = "KRW-XRP",
    quantity: str = "10",
    price: str = "3000",
    uuid: str | None = "uuid-1",
    evidence: tuple[str, str, str] | None = None,
    intent_id: UUID = INTENT,
    at: datetime = NOW,
    mode: str | None = None,
) -> IntentChildOrder:
    return IntentChildOrder(
        child_id=child_id,
        intent_id=intent_id,
        idempotency_key=f"child-{child_id:04d}",
        symbol=symbol,
        side=side,
        execution_mode=mode or ("shadow" if state == "shadowed" else "live"),
        decision_ref="decision-1",
        state=state,
        quantity=Decimal(quantity),
        limit_price=Decimal(price),
        broker_order_id=uuid,
        intent_created_at=NOW,
        created_at=at,
        finished_at=at,
        evidence=(
            None
            if evidence is None
            else ChildOrderEvidence(
                order_uuid=evidence[0],
                broker_state=evidence[1],
                executed_volume=Decimal(evidence[2]),
                remaining_volume=None,
                observed_at=NOW,
            )
        ),
    )


def test_a_child_order_key_never_collides_with_a_rung_id() -> None:
    assert child_order_key(7) == -7 and child_order_key(7) < 0


@pytest.mark.parametrize("state", ["accepted", "uncertain", "sending"])
def test_an_order_is_unresolved_until_the_broker_proves_its_end(state: str) -> None:
    assert order(state=state).resolved is False
    assert order(state=state).filled_quantity is None
    assert order(state=state, evidence=("uuid-1", "wait", "3")).resolved is False


@pytest.mark.parametrize("broker_state", ["done", "cancel"])
def test_a_final_broker_state_resolves_with_the_traded_quantity(broker_state) -> None:
    ended = order(evidence=("uuid-1", broker_state, "4"))
    assert ended.resolved is True and ended.filled_quantity == Decimal("4")
    book_order = ended.as_sleeve_order()
    assert book_order.state == "cancelled" and book_order.rung_filled_qty == Decimal(
        "4"
    )
    assert order(evidence=("uuid-1", "done", "10")).as_sleeve_order().state == "filled"


def test_b3_an_unrecorded_send_is_bound_by_the_identifier_evidence() -> None:
    found = order(state="sending", uuid=None, evidence=("uuid-found", "cancel", "2"))
    assert found.order_uuid == "uuid-found" and found.resolved is True


def test_b3_an_unbound_send_makes_the_book_unmeasured() -> None:
    unbound = order(state="sending", uuid=None)
    book_order = unbound.as_sleeve_order()
    assert unbound.order_uuid is None and book_order.fill_unproven is True
    book = compute_sleeve_book(orders=[book_order], fills=[], marks={})
    assert book.used is None and book.net_pnl is None
    assert book.unproven_orders == (child_order_key(1),)


def test_two_different_uuids_bind_nothing() -> None:
    torn = order(evidence=("uuid-other", "done", "10"))
    assert torn.order_uuid is None and torn.resolved is False
    assert torn.as_sleeve_order().fill_unproven is True


def test_a_resting_order_with_a_uuid_is_measured_as_open() -> None:
    resting = order().as_sleeve_order()
    assert resting.state == "resting" and resting.fill_unproven is False
    book = compute_sleeve_book(orders=[resting], fills=[], marks={})
    assert book.used == Decimal("30000") and book.open_order_notional == Decimal(
        "30000"
    )


def test_b5_unresolved_orders_are_per_symbol_and_ignore_shadow() -> None:
    orders = [
        order(1),
        order(2, symbol="KRW-BTC", evidence=("uuid-1", "done", "10")),
        order(3, state="shadowed", uuid=None),
    ]
    assert [o.child_id for o in unresolved_orders(orders, symbol="KRW-XRP")] == [1]
    assert unresolved_orders(orders, symbol="KRW-BTC") == []


def test_b18_the_shadow_position_is_the_fifo_remainder() -> None:
    orders = [
        order(1, state="shadowed", uuid=None, quantity="10", price="3000"),
        order(
            2,
            state="shadowed",
            uuid=None,
            quantity="10",
            price="3300",
            at=NOW + timedelta(hours=1),
        ),
        order(
            3,
            state="shadowed",
            uuid=None,
            side="sell",
            quantity="10",
            price="3600",
            at=NOW + timedelta(hours=2),
        ),
        order(4),  # a live order is not part of the shadow book
    ]
    book_orders, fills = shadow_book_inputs(orders)
    book = compute_sleeve_book(orders=book_orders, fills=fills, marks={})
    [position] = book.positions
    # The first lot (3000) was sold; the 3300 lot is what is still held.
    assert (position.quantity, position.cost_basis) == (Decimal("10"), Decimal("33000"))
    assert book.realized_pnl == Decimal("6000")
    assert len(book_orders) == 3


def test_build_child_orders_keeps_only_orders() -> None:
    def child(child_id: int, action: str, state: str, quantity: str | None):
        return SimpleNamespace(
            id=child_id,
            action=action,
            state=state,
            idempotency_key=f"k{child_id}",
            quantity_decimal=None if quantity is None else Decimal(quantity),
            limit_price_krw=3000 if quantity else None,
            broker_order_id=None,
            created_at=NOW,
            finished_at=NOW,
        )

    intent = SimpleNamespace(
        id=INTENT,
        symbol="KRW-XRP",
        side="buy",
        execution_mode="live",
        decision_ref="d",
        created_at=NOW,
    )
    rows = [
        (intent, child(1, "submit", "accepted", "10")),
        (intent, child(2, "submit", "refused", "10")),
        (intent, child(3, "submit", "not_sent", "10")),
        (intent, child(4, "cancel", "accepted", None)),
        (intent, child(5, "submit", "shadowed", "10")),
    ]
    assert [o.child_id for o in build_child_orders(rows, [])] == [1, 5]


# ------------------------------------------------------------ gateway views


def _intent_row(side: str, *, symbol: str = "KRW-XRP", expires_in: int = 3600, **extra):
    values = {
        "id": uuid4(),
        "side": side,
        "symbol": symbol,
        "origin": "strategy",
        "expires_at": NOW + timedelta(seconds=expires_in),
        "max_notional_krw": 480_000,
    }
    return SimpleNamespace(**{**values, **extra})


def _judged(side: str = "buy"):
    return SimpleNamespace(
        intent_id=uuid4(), symbol="KRW-XRP", side=side, execution_mode="live"
    )


def _views(intents, orders, *, book=None, judged=None):
    return sleeve_symbol_views(
        intent=judged or _judged(),
        intents=intents,
        orders=orders,
        book=book
        if book is not None
        else compute_sleeve_book(orders=[], fills=[], marks={}),
        handoff_active=False,
        now=NOW,
    )


def test_s2_a_stored_unexpired_sell_intent_is_open() -> None:
    stored = _intent_row("sell")
    expired = _intent_row("sell", expires_in=-1)
    other_symbol = _intent_row("sell", symbol="KRW-BTC")
    symbol, _buys, _reserved = _views([stored, expired, other_symbol], [])
    assert symbol.open_sell_intents == frozenset({stored.id})


def test_s2_an_expired_sell_intent_with_an_unresolved_order_is_still_open() -> None:
    expired = _intent_row("sell", expires_in=-1)
    symbol, _b, _r = _views([expired], [order(side="sell", intent_id=expired.id)])
    assert symbol.open_sell_intents == frozenset({expired.id})
    assert symbol.unresolved_prior_order is True and symbol.holdings is None


def test_s2_a_sell_intent_whose_orders_all_ended_is_done() -> None:
    done = _intent_row("sell")
    ended = order(side="sell", intent_id=done.id, evidence=("uuid-1", "done", "10"))
    symbol, _b, _r = _views([done], [ended])
    assert symbol.open_sell_intents == frozenset()
    assert symbol.unresolved_prior_order is False


def test_i4_other_open_buy_intents_reserve_what_they_have_not_sent() -> None:
    untouched = _intent_row("buy")
    partly_sent = _intent_row("buy", symbol="KRW-BTC")
    expired = _intent_row("buy", expires_in=-1)
    sent = order(
        2, intent_id=partly_sent.id, symbol="KRW-BTC", quantity="10", price="3000"
    )
    _symbol, open_buys, reserved = _views([untouched, partly_sent, expired], [sent])
    assert open_buys == 2
    assert reserved == Decimal("480000") + Decimal("450000")


def test_the_judged_intent_does_not_reserve_against_itself() -> None:
    judged = _judged()
    own = _intent_row("buy", id=judged.intent_id)
    _symbol, open_buys, reserved = _views([own], [], judged=judged)
    assert (open_buys, reserved) == (0, Decimal("0"))


def test_holdings_are_unknown_when_the_book_is_unreadable_or_unproven() -> None:
    unproven = compute_sleeve_book(
        orders=[order(state="sending", uuid=None).as_sleeve_order()], fills=[], marks={}
    )
    for book in (None, unproven):
        symbol, _b, _r = sleeve_symbol_views(
            intent=_judged("sell"),
            intents=[],
            orders=[],
            book=book,
            handoff_active=False,
            now=NOW,
        )
        assert symbol.holdings is None


# ------------------------------------------------------------ card sleeves unchanged


class _EmptyResult:
    def all(self) -> list:
        return []

    def scalars(self) -> _EmptyResult:
        return self


class _CountingSession:
    def __init__(self) -> None:
        self.statements: list[str] = []

    async def execute(self, statement, *_args, **_kwargs):
        self.statements.append(str(statement))
        return _EmptyResult()


@pytest.mark.asyncio
async def test_a_card_sleeve_reads_nothing_of_the_second_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``card`` sleeve's attribution is the one query it always was: no intent
    child and no handoff is read, so its result cannot change (design §1.4).
    """
    from app.services.strategy_sleeves import store

    async def must_not_run(*_args, **_kwargs):
        raise AssertionError("a card sleeve must not read intent orders")

    monkeypatch.setattr(store, "load_intent_child_orders", must_not_run)
    monkeypatch.setattr(store, "_mark_unmeasured_handoff_cards", must_not_run)
    session = _CountingSession()
    sleeve = SimpleNamespace(
        id=1,
        account_mode="upbit",
        market="crypto",
        currency="KRW",
        execution_path="card",
    )

    inputs = await store.load_sleeve_inputs(session, sleeve)  # type: ignore[arg-type]

    assert inputs.orders == () and inputs.fills == ()
    assert len(session.statements) == 1
    assert "order_proposal_rungs" in session.statements[0]
    assert "execution_child_requests" not in session.statements[0]


@pytest.mark.asyncio
async def test_a_go_sleeve_reads_its_intent_orders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.strategy_sleeves import store

    async def orders(_session, _sleeve_id):
        return [
            order(evidence=("uuid-1", "done", "10")),
            order(2, state="shadowed", uuid=None),
        ]

    async def unchanged(_session, _sleeve, existing):
        return existing

    monkeypatch.setattr(store, "load_intent_child_orders", orders)
    monkeypatch.setattr(store, "_mark_unmeasured_handoff_cards", unchanged)
    session = _CountingSession()
    sleeve = SimpleNamespace(
        id=1,
        account_mode="upbit",
        market="crypto",
        currency="KRW",
        execution_path="go",
    )

    inputs = await store.load_sleeve_inputs(session, sleeve)  # type: ignore[arg-type]

    # The live order is in the book; the shadow one never is.
    assert [o.rung_id for o in inputs.orders] == [child_order_key(1)]
    assert inputs.orders[0].state == "filled"


# ------------------------------------------------- review round 1 (PR 2486)


def test_a_shadow_child_still_sending_is_shadow_and_is_counted_in_the_book() -> None:
    """Between the reservation and the recorded ``shadowed`` outcome the child
    is ``sending``. It must be in the book then, or its notional is in neither
    the intent's reservation nor the book and two buys overrun the budget."""
    pending = order(1, state="sending", mode="shadow", uuid=None, quantity="20")

    assert pending.is_shadow is True and pending.resolved is True
    # Never a live order: it blocks nothing and nobody looks it up at the broker.
    assert unresolved_orders([pending], symbol="KRW-XRP") == []
    assert live_orders([pending]) == []
    book_orders, fills = shadow_book_inputs([pending])
    book = compute_sleeve_book(orders=book_orders, fills=fills, marks={})
    assert book.held_cost_basis == Decimal("60000")


def test_a_live_child_still_sending_is_never_shadow() -> None:
    pending = order(1, state="sending", uuid=None)
    assert pending.is_shadow is False and pending.resolved is False
    assert unresolved_orders([pending], symbol="KRW-XRP") == [pending]
    assert shadow_book_inputs([pending]) == ([], [])


def _fill(child_id: int, quantity: str, side: str = "sell") -> SleeveFill:
    return SleeveFill(
        ledger_id=child_id * 10,
        rung_id=child_order_key(child_id),
        symbol="KRW-XRP",
        side=side,
        quantity=Decimal(quantity),
        price=Decimal("3000"),
        fee=Decimal("0"),
        filled_at=NOW,
    )


def test_a_broker_final_child_with_unbooked_fills_makes_holdings_unknown() -> None:
    """The broker says the sell traded 4; the ledger has booked none of it.

    The lots still show the 10 the sleeve held, so the next sell could take 10
    while the sleeve owns 6 -- on a shared account, somebody else's coins.
    """
    bought = order(1, side="buy", evidence=("uuid-1", "done", "10"))
    sold = order(2, side="sell", uuid="uuid-2", evidence=("uuid-2", "cancel", "4"))
    orders = [bought.as_sleeve_order(), sold.as_sleeve_order()]
    booked_buy = [_fill(1, "10", side="buy")]

    lagging = mark_children_with_unbooked_fills(orders, [bought, sold], booked_buy)
    book = compute_sleeve_book(orders=lagging, fills=booked_buy, marks={})
    assert [o.fill_unproven for o in lagging] == [False, True]
    assert book.unproven_orders == (child_order_key(2),)
    assert book.net_pnl is None and book.used is None

    # A partly booked sell is still unproven; the full amount proves it.
    partly = [*booked_buy, _fill(2, "3")]
    assert mark_children_with_unbooked_fills(orders, [bought, sold], partly)[
        1
    ].fill_unproven
    caught_up = [*booked_buy, _fill(2, "4")]
    proven = mark_children_with_unbooked_fills(orders, [bought, sold], caught_up)
    assert [o.fill_unproven for o in proven] == [False, False]
    assert compute_sleeve_book(orders=proven, fills=caught_up, marks={}).positions[
        0
    ].quantity == Decimal("6")


def test_an_order_the_broker_has_not_ended_is_not_marked_by_the_fill_check() -> None:
    resting = order(3, side="sell", uuid="uuid-3")
    orders = [resting.as_sleeve_order()]
    assert mark_children_with_unbooked_fills(orders, [resting], []) == orders


def test_a_shadow_fill_keeps_its_place_in_the_fifo_when_it_is_recorded_late() -> None:
    """PR 2486 review: a shadow buy whose outcome write failed is ``sending``,
    a later shadow sell of that position is recorded first, and then the buy is
    finished by its key. The buy's virtual fill must not move behind the sell."""
    t0 = NOW
    sell_at = NOW + timedelta(minutes=1)
    recorded_late = NOW + timedelta(minutes=5)

    def book_of(buy: IntentChildOrder):
        sell = order(2, state="shadowed", side="sell", uuid=None, at=sell_at)
        orders, fills = shadow_book_inputs([buy, sell])
        return compute_sleeve_book(orders=orders, fills=fills, marks={})

    pending = order(1, state="sending", mode="shadow", uuid=None, at=t0)
    finished = IntentChildOrder(
        **{
            **{f: getattr(pending, f) for f in pending.__dataclass_fields__},
            "state": "shadowed",
            "finished_at": recorded_late,
        }
    )

    for buy in (pending, finished):
        book = book_of(buy)
        assert book.positions == ()
        assert book.unmatched_sell_quantity == Decimal("0")
        assert book.realized_pnl == Decimal("0")


def test_shadow_fills_fold_in_the_order_they_were_reserved_not_started() -> None:
    """PR 2486 review (65wJD5, 65wMjK): ``created_at`` is the start of the
    reserving transaction, which reads before it takes the sleeve lock. Buy B
    starts first and waits; buy A takes the lock and commits first at 3,000; B
    then commits at 3,300 with the *earlier* ``created_at``. The journal id is
    handed out under the lock, so A is child 1 and B is child 2. A later sell of
    one lot must consume A's lot, leaving B's 3,300 lot as what is still held."""
    b_started = NOW
    a_started = NOW + timedelta(seconds=1)
    first_reserved = order(1, state="shadowed", uuid=None, price="3000", at=a_started)
    second_reserved = order(2, state="shadowed", uuid=None, price="3300", at=b_started)
    sell = order(
        3,
        state="shadowed",
        side="sell",
        uuid=None,
        price="3100",
        at=NOW + timedelta(minutes=1),
    )

    orders, fills = shadow_book_inputs([first_reserved, second_reserved, sell])
    book = compute_sleeve_book(orders=orders, fills=fills, marks={})

    assert [(p.symbol, p.quantity, p.cost_basis) for p in book.positions] == [
        ("KRW-XRP", Decimal("10"), Decimal("33000"))
    ]
    assert book.realized_pnl == Decimal("1000")
    # What the budget gate reads: the cost of what is still held.
    assert book.used == Decimal("33000")


def test_a_ledger_fill_keeps_its_ledger_time_in_the_fifo() -> None:
    """The rank is for virtual fills only: ledger fills still fold by their
    ledger time, then their ledger id, exactly as before."""

    def fill(ledger_id: int, price: str, at: datetime, side: str = "buy") -> SleeveFill:
        return SleeveFill(
            ledger_id=ledger_id,
            rung_id=ledger_id,
            symbol="KRW-XRP",
            side=side,
            quantity=Decimal("10"),
            price=Decimal(price),
            fee=Decimal("0"),
            filled_at=at,
        )

    later_id_earlier_time = fill(9, "3000", NOW)
    earlier_id_later_time = fill(1, "3300", NOW + timedelta(seconds=1))
    sell = fill(10, "3100", NOW + timedelta(minutes=1), side="sell")
    book = compute_sleeve_book(
        orders=[], fills=[earlier_id_later_time, later_id_earlier_time, sell], marks={}
    )

    assert [(p.quantity, p.cost_basis) for p in book.positions] == [
        (Decimal("10"), Decimal("33000"))
    ]

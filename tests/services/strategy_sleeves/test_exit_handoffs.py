"""hk 5441 — exit handoffs: what a claimed card proves (design S5, O1-O4).

The pure proofs are tested here. The state machine, the claim's conditional
update, the send fence and the settle under its locks run against the database
in ``tests/services/execution_intent_gateway/test_upbit_db.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.services.execution_intent_gateway.decisions import Refusal
from app.services.execution_intent_gateway.upbit_decisions import (
    SleeveView,
    decide_upbit_child,
)
from app.services.strategy_sleeves.book import SleeveOrder, compute_sleeve_book
from app.services.strategy_sleeves.exit_handoffs import (
    UNREADABLE_ORDER,
    BrokerOrderReading,
    card_proven_unsent,
    sent_card_problem,
)
from app.services.strategy_sleeves.gate import evaluate_sleeve_gate
from tests.services.execution_intent_gateway.upbit_support import (
    facts,
    reads,
    submit,
    symbol_view,
    upbit_intent,
    version,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 20, 5, 20, tzinfo=UTC)


def handoff(attempts: int = 0, proven_unsent: int = 0):
    return SimpleNamespace(
        send_attempts=attempts, send_attempts_proven_unsent=proven_unsent
    )


def group(state: str = "expired", *, nonce: str | None = None, used_at=None):
    return SimpleNamespace(
        lifecycle_state=state, approval_nonce=nonce, approval_nonce_used_at=used_at
    )


def rung(state: str = "expired", *, order_id=None, key=None, filled=None):
    return SimpleNamespace(
        state=state, broker_order_id=order_id, idempotency_key=key, filled_qty=filled
    )


# ------------------------------------------------------------ O1


@pytest.mark.parametrize(
    ("the_group", "the_rung"),
    [
        (group("expired"), rung("expired")),
        (group("rejected", nonce="n", used_at=NOW), rung("rejected")),
        (group("voided", nonce=None, used_at=NOW), rung("voided")),
        (group("superseded", nonce="n", used_at=NOW), rung("superseded")),
    ],
)
def test_o1_a_card_that_ended_before_any_send_is_proven_unsent(
    the_group, the_rung
) -> None:
    assert card_proven_unsent(handoff(), the_group, [the_rung]) is True


def test_o1_a_pre_send_block_that_was_recorded_still_proves_unsent() -> None:
    # One attempt was counted and the pre-send hook provably stopped it.
    assert card_proven_unsent(
        handoff(1, 1), group("rejected", used_at=NOW), [rung("rejected")]
    )


@pytest.mark.parametrize(
    ("the_handoff", "the_group", "the_rung"),
    [
        # An approval started and nothing proves it did not post (O1: the core
        # was entered; internal_error and a W5 post-core dead letter look like this).
        (handoff(1, 0), group("expired"), rung("expired")),
        (handoff(2, 1), group("rejected", used_at=NOW), rung("rejected")),
        # The broker named an order, or an identifier was recorded.
        (
            handoff(),
            group("rejected", used_at=NOW),
            rung("rejected", order_id="uuid-1"),
        ),
        (handoff(), group("expired"), rung("expired", key="oprop-abc")),
        (handoff(), group("expired"), rung("expired", filled=Decimal("1"))),
        # States that are, or may be, at the broker.
        (handoff(), group("terminal"), rung("cancelled")),
        (handoff(), group("terminal"), rung("filled")),
        (handoff(), group("submitted"), rung("resting")),
        (handoff(), group("expired"), rung("voided_local_stale")),
    ],
)
def test_o1_anything_else_is_not_proven_unsent(
    the_handoff, the_group, the_rung
) -> None:
    assert card_proven_unsent(the_handoff, the_group, [the_rung]) is False


def test_o1_a_card_with_no_rung_proves_nothing() -> None:
    assert card_proven_unsent(handoff(), group("expired"), []) is False


# ------------------------------------------------------------ O3


@pytest.mark.parametrize("state", ["proposed", "approved", "partially_submitted"])
def test_o3_a_card_that_can_still_be_approved_is_not_returned(state) -> None:
    """A W5 pre-core dead letter ends the callback job only. The proposal stays
    ``proposed`` with a live nonce, so the handoff must stay claimed: returning
    it would let a second card exist while the first can still be approved.
    """
    live_card = group(state, nonce="nonce-1", used_at=None)
    assert card_proven_unsent(handoff(), live_card, [rung("pending_approval")]) is False


def test_o3_a_terminal_card_with_a_live_nonce_is_not_returned() -> None:
    # The no-send end and the dead nonce must both be on the row.
    still_armed = group("expired", nonce="nonce-1", used_at=None)
    assert card_proven_unsent(handoff(), still_armed, [rung("expired")]) is False


# ------------------------------------------------------------ O2, O4


def reading(
    volume: str | None = "4", *, state: str = "cancel", uuid: str | None = "uuid-1"
):
    return BrokerOrderReading(uuid, state, None if volume is None else Decimal(volume))


def test_o2_equal_broker_and_ledger_totals_prove_the_card() -> None:
    assert (
        sent_card_problem(
            order_uuid="uuid-1", reading=reading("4"), ledger_attributed=Decimal("4")
        )
        is None
    )
    assert (
        sent_card_problem(
            order_uuid="uuid-1",
            reading=reading("0", state="cancel"),
            ledger_attributed=Decimal("0"),
        )
        is None
    )


def test_o2_a_ledger_total_below_the_broker_total_is_not_proven() -> None:
    assert (
        sent_card_problem(
            order_uuid="uuid-1", reading=reading("4"), ledger_attributed=Decimal("3")
        )
        == "ledger_total_differs_from_broker"
    )


@pytest.mark.parametrize(
    ("the_reading", "attributed", "problem"),
    [
        (None, Decimal("4"), "broker_reading_unavailable"),
        (UNREADABLE_ORDER, Decimal("4"), "broker_reading_unavailable"),
        (reading("4", state="wait"), Decimal("4"), "broker_reading_unavailable"),
        (reading(None), Decimal("4"), "broker_reading_unavailable"),
        (reading("4", uuid="uuid-other"), Decimal("4"), "broker_reading_unavailable"),
        (reading("4"), None, "ledger_unreadable"),
    ],
)
def test_o4_an_unread_value_is_never_treated_as_zero(
    the_reading, attributed, problem
) -> None:
    assert (
        sent_card_problem(
            order_uuid="uuid-1", reading=the_reading, ledger_attributed=attributed
        )
        == problem
    )


def test_o4_a_card_whose_order_is_not_named_is_not_proven() -> None:
    assert (
        sent_card_problem(
            order_uuid=None, reading=reading("0"), ledger_attributed=Decimal("0")
        )
        == "card_order_not_named"
    )


def _book_with_unmeasured_card():
    card = SleeveOrder(
        proposal_pk=11,
        rung_id=101,
        symbol="KRW-XRP",
        side="sell",
        state="partially_filled",
        quantity=Decimal("100"),
        limit_price=Decimal("2900"),
        notional=None,
        rung_filled_qty=None,
        fill_unproven=True,
    )
    return compute_sleeve_book(orders=[card], fills=[], marks={})


def test_o4_an_unmeasured_card_makes_the_whole_sleeve_unmeasured() -> None:
    book = _book_with_unmeasured_card()
    assert book.net_pnl is None and book.used is None and book.loss is None
    assert book.unproven_orders == (101,)
    verdict = evaluate_sleeve_gate(
        version=version(),
        book=book,
        order_buy_notional=Decimal("30000"),
        places_buy=True,
    )
    assert verdict.allowed is False
    assert (
        verdict.reason is not None and verdict.reason.value == "sleeve_pnl_unmeasured"
    )


def test_o4_ledger_lag_and_a_failed_broker_read_refuse_a_buy_on_another_symbol() -> (
    None
):
    """The card sold KRW-XRP; the ledger has not booked it and the broker read
    failed. The settle keeps the handoff claimed and marks it unmeasured, the
    loader marks the card's order, and a buy of KRW-BTC is refused.
    """
    assert (
        sent_card_problem(
            order_uuid="uuid-1",
            reading=UNREADABLE_ORDER,
            ledger_attributed=Decimal("0"),
        )
        is not None
    )
    intent = upbit_intent(symbol="KRW-BTC", limit_price_krw=150_000_000)
    other_symbol_buy = submit(
        intent, quantity_decimal="0.001", limit_price_krw=150_000_000
    )
    decision = decide_upbit_child(
        other_symbol_buy,
        intent=intent,
        children=[],
        facts=facts(
            sleeve=SleeveView(version=version(), book=_book_with_unmeasured_card()),
            symbol=symbol_view(),
        ),
        reads=reads(),
    )
    assert decision.refusal is Refusal.SLEEVE_PNL_UNMEASURED


def test_o1_an_unsent_card_needs_no_broker_reading_at_all() -> None:
    # O1 comes first: a proven-unsent card returns without any reading (O2).
    assert card_proven_unsent(handoff(), group("expired"), [rung("expired")]) is True

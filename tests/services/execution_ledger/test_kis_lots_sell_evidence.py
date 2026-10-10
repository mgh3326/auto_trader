"""Task #1087 — sell-side ledger evidence for KIS live KR lots (pure, no DB, no broker).

``open_sell_evidence`` is the twin of ``open_buy_evidence``,
``same_day_buy_evidence`` the twin of ``same_day_sell_evidence``, and
``sellable_by_ledger`` is the known lot net minus own open sell orders today,
clamped to ``[0, broker quantity]``. Unknown is never rendered as known.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.services.execution_ledger import kis_lots
from app.services.execution_ledger.kis_lots import (
    BLOCK_BUY_EVIDENCE_UNKNOWN,
    BLOCK_OPEN_SELL_EVIDENCE_UNKNOWN,
    BLOCK_OWN_OPEN_SELL,
    BLOCK_SAME_DAY_BUY_FILL_IN_LEDGER,
    BLOCK_SAME_DAY_SELL_FILL_UNPROVEN,
    SELLABLE_UNKNOWN_LEDGER_STATE,
    SELLABLE_UNKNOWN_OPEN_SELL_EVIDENCE,
    SELLABLE_UNKNOWN_OPEN_SELL_QUANTITY,
    UNKNOWN_LEDGER_STALE,
    UNKNOWN_NO_RECONCILE_RUN,
    UNKNOWN_ORDER_LEDGER_READ_FAILED,
    Freshness,
    LedgerFill,
    OrderRow,
    build_symbol_block,
    compute_freshness,
    unknown_block,
)

pytestmark = pytest.mark.unit

D = Decimal
# 2026-09-29 14:30 KST (a 14:30 KR regular-session rep).
NOW = datetime(2026, 9, 29, 5, 30, tzinfo=UTC)
FRESH = Freshness("fresh", NOW - timedelta(minutes=20), 20.0)
EARLIER = NOW - timedelta(days=10)
YESTERDAY = NOW - timedelta(days=1)
TODAY_MORNING = NOW - timedelta(hours=4)  # 10:30 KST
TOMORROW = NOW + timedelta(hours=12)  # 02:30 KST next day (writer clock skew)
# KST midnight that starts NOW's KST day: 2026-09-29 00:00 KST == 09-28 15:00 UTC.
KST_MIDNIGHT = datetime(2026, 9, 28, 15, 0, tzinfo=UTC)


def fill(
    fid: int,
    side: str,
    qty: str,
    when: datetime,
    *,
    source: str = "reconciler",
    order: str | None = None,
    price: str = "100000",
) -> LedgerFill:
    return LedgerFill(
        id=fid,
        source=source,
        side=side,
        quantity=D(qty),
        price=D(price),
        filled_at=when,
        broker_order_id=order or f"000{fid}",
    )


def order(
    oid: int,
    status: str,
    when: datetime,
    *,
    side: str = "sell",
    qty: str | None = "5",
    order_no: str | None = None,
) -> OrderRow:
    return OrderRow(
        id=oid,
        order_no=order_no or f"000{oid}",
        status=status,
        quantity=None if qty is None else D(qty),
        price=D("1000"),
        trade_date=when,
        side=side,
    )


HOLD_12 = [fill(1, "buy", "12", EARLIER)]


def block(
    fills=HOLD_12,
    *,
    reference: str | None = "12",
    orders=(),
    freshness: Freshness = FRESH,
    now: datetime = NOW,
):
    return build_symbol_block(
        symbol="196170",
        reference_quantity=None if reference is None else D(reference),
        current_price=None,
        fills=fills,
        orders=orders,
        freshness=freshness,
        now=now,
    )


def ose(out):
    return out["open_sell_evidence"]


def sbe(out):
    return out["same_day_buy_evidence"]


# ---------------------------------------------------------------------------
# open_sell_evidence: shape and the clean case
# ---------------------------------------------------------------------------
def test_clean_symbol_is_known_non_blocking_and_fully_sellable() -> None:
    out = block()
    evidence = ose(out)
    assert evidence == {
        "state": "known",
        "unknown_reasons": [],
        "blocking": False,
        "blocking_reasons": [],
        "kis_live_order_ledger_open_sells": [],
        "own_open_sell_order_quantity": "0",
        "same_day_sell_fills_unproven_complete": [],
        "presumed_dead_prior_day_sells": [],
        "scope": "orders_known_to_auto_trader_only",
        "external_orders_verifiable": False,
    }
    assert sbe(out) == {
        "state": "known",
        "unknown_reasons": [],
        "blocking": False,
        "blocking_reasons": [],
        "fills": [],
        "scope": "orders_known_to_auto_trader_only",
    }
    assert out["sellable_by_ledger"] == "12"
    assert out["sellable_by_ledger_basis"] == {
        "state": "known",
        "unknown_reasons": [],
        "method": "ledger_net_minus_own_open_sell_orders_today",
        "ledger_net_quantity": "12",
        "own_open_sell_order_quantity": "0",
        "reference_quantity": "12",
        "clamped": False,
    }


def test_private_keys_never_leak_into_the_public_block() -> None:
    out = block(orders=[order(1, "accepted", TODAY_MORNING)])
    assert not [k for k in ose(out) if k.startswith("_")]
    json.dumps(out)


# ---------------------------------------------------------------------------
# blocking reason 1: own_nonterminal_sell_order_today
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "status", ["accepted", "pending", "partial", "unknown", "anomaly", "brand_new"]
)
def test_same_day_nonterminal_own_sell_blocks(status: str) -> None:
    out = block(orders=[order(1, status, TODAY_MORNING, qty="5")])
    evidence = ose(out)
    assert evidence["state"] == "known"
    assert evidence["blocking"] is True
    assert evidence["blocking_reasons"] == [BLOCK_OWN_OPEN_SELL]
    assert [o["status"] for o in evidence["kis_live_order_ledger_open_sells"]] == [
        status
    ]
    assert evidence["own_open_sell_order_quantity"] == "5"
    assert out["sellable_by_ledger"] == "7"


@pytest.mark.parametrize("status", ["filled", "cancelled", "expired", "rejected"])
def test_same_day_terminal_own_sell_does_not_block_or_reserve(status: str) -> None:
    out = block(orders=[order(1, status, TODAY_MORNING)])
    assert ose(out)["blocking"] is False
    assert ose(out)["own_open_sell_order_quantity"] == "0"
    assert out["sellable_by_ledger"] == "12"


def test_open_buy_rows_are_not_open_sells_and_open_sells_are_not_open_buys() -> None:
    out = block(
        orders=[
            order(1, "accepted", TODAY_MORNING, side="buy"),
            order(2, "accepted", TODAY_MORNING, side="sell"),
        ]
    )
    assert [o["order_no"] for o in ose(out)["kis_live_order_ledger_open_sells"]] == [
        "0002"
    ]
    assert [
        o["order_no"]
        for o in out["open_buy_evidence"]["kis_live_order_ledger_open_buys"]
    ] == ["0001"]


def test_prior_day_nonterminal_sell_is_reported_but_neither_blocks_nor_reserves() -> (
    None
):
    out = block(orders=[order(1, "accepted", YESTERDAY)])
    evidence = ose(out)
    assert evidence["blocking"] is False
    assert [o["status"] for o in evidence["presumed_dead_prior_day_sells"]] == [
        "accepted"
    ]
    assert evidence["own_open_sell_order_quantity"] == "0"
    assert out["sellable_by_ledger"] == "12"


def test_future_dated_nonterminal_sell_row_blocks_as_today() -> None:
    out = block(orders=[order(1, "accepted", TOMORROW)])
    assert ose(out)["blocking_reasons"] == [BLOCK_OWN_OPEN_SELL]
    assert ose(out)["presumed_dead_prior_day_sells"] == []


# ---------------------------------------------------------------------------
# blocking reason 2: same_day_sell_fill_order_not_proven_complete
# ---------------------------------------------------------------------------
def test_same_day_sell_fill_without_order_row_blocks() -> None:
    out = block(
        [*HOLD_12, fill(2, "sell", "2", TODAY_MORNING, order="0000321")],
        reference="10",
    )
    evidence = ose(out)
    assert evidence["state"] == "known"
    assert evidence["blocking_reasons"] == [BLOCK_SAME_DAY_SELL_FILL_UNPROVEN]
    assert [
        f["broker_order_id"] for f in evidence["same_day_sell_fills_unproven_complete"]
    ] == ["0000321"]
    # the fill is inside the reconciled net; no own order is still open
    assert out["sellable_by_ledger"] == "10"


def test_same_day_sell_fill_of_an_order_proven_filled_does_not_block() -> None:
    out = block(
        [*HOLD_12, fill(2, "sell", "2", TODAY_MORNING, order="0000321")],
        reference="10",
        orders=[order(1, "filled", TODAY_MORNING, order_no="321")],
    )
    assert ose(out)["blocking"] is False
    assert ose(out)["same_day_sell_fills_unproven_complete"] == []


def test_a_filled_buy_order_row_never_proves_a_sell_fill_complete() -> None:
    out = block(
        [*HOLD_12, fill(2, "sell", "2", TODAY_MORNING, order="0000321")],
        reference="10",
        orders=[order(1, "filled", TODAY_MORNING, side="buy", order_no="321")],
    )
    assert ose(out)["blocking_reasons"] == [BLOCK_SAME_DAY_SELL_FILL_UNPROVEN]


def test_partial_sell_order_blocks_on_both_reasons_and_reserves_its_quantity() -> None:
    out = block(
        [*HOLD_12, fill(2, "sell", "2", TODAY_MORNING, order="0000321")],
        reference="10",
        orders=[order(1, "partial", TODAY_MORNING, order_no="321", qty="5")],
    )
    evidence = ose(out)
    assert evidence["blocking_reasons"] == [
        BLOCK_OWN_OPEN_SELL,
        BLOCK_SAME_DAY_SELL_FILL_UNPROVEN,
    ]
    # the full order quantity is reserved (conservative: 10 - 5, not 10 - 3)
    assert out["sellable_by_ledger"] == "5"


def test_same_day_provisional_sell_fill_blocks_and_never_reaches_the_quantity() -> None:
    out = block(
        [*HOLD_12, fill(2, "sell", "2", TODAY_MORNING, source="websocket", order="9")],
        reference="10",
    )
    evidence = ose(out)
    assert evidence["blocking_reasons"] == [BLOCK_SAME_DAY_SELL_FILL_UNPROVEN]
    assert [
        f["provisional"] for f in evidence["same_day_sell_fills_unproven_complete"]
    ] == [True]
    # websocket rows never touch net: lots unknown => sellable unknown
    assert out["ledger_state"] == "unknown"
    assert out["sellable_by_ledger"] is None
    assert (
        SELLABLE_UNKNOWN_LEDGER_STATE
        in out["sellable_by_ledger_basis"]["unknown_reasons"]
    )


def test_websocket_twin_of_a_reconciled_sell_is_listed_once() -> None:
    out = block(
        [
            *HOLD_12,
            fill(2, "sell", "2", TODAY_MORNING, order="0000500"),
            fill(3, "sell", "2", TODAY_MORNING, source="websocket", order="500"),
        ],
        reference="10",
    )
    hits = ose(out)["same_day_sell_fills_unproven_complete"]
    assert [h["source"] for h in hits] == ["reconciler"]
    assert out["sellable_by_ledger"] == "10"


def test_prior_day_sell_fill_does_not_block_and_future_dated_one_does() -> None:
    prior = block([*HOLD_12, fill(2, "sell", "2", YESTERDAY)], reference="10")
    assert ose(prior)["blocking"] is False
    future = block([*HOLD_12, fill(2, "sell", "2", TOMORROW)], reference="10")
    assert ose(future)["blocking_reasons"] == [BLOCK_SAME_DAY_SELL_FILL_UNPROVEN]


def test_seed_rows_are_never_sell_fills() -> None:
    out = block(
        [fill(1, "buy", "12", TODAY_MORNING, source="manual_import", order="SEED-1")],
    )
    assert ose(out)["blocking"] is False
    assert sbe(out)["blocking"] is False


# ---------------------------------------------------------------------------
# blocking reason 3: open_sell_evidence_unknown (no run / stale / read failed)
# ---------------------------------------------------------------------------
def test_order_ledger_read_failure_makes_open_sell_evidence_unknown() -> None:
    out = block(orders=None)
    evidence = ose(out)
    assert evidence["state"] == "unknown"
    assert evidence["unknown_reasons"] == [UNKNOWN_ORDER_LEDGER_READ_FAILED]
    assert evidence["blocking_reasons"] == [BLOCK_OPEN_SELL_EVIDENCE_UNKNOWN]
    assert evidence["own_open_sell_order_quantity"] is None
    # lots stay known, but the sellable quantity cannot be
    assert out["ledger_state"] == "known"
    assert out["sellable_by_ledger"] is None
    assert out["sellable_by_ledger_basis"]["state"] == "unknown"
    assert out["sellable_by_ledger_basis"]["unknown_reasons"] == [
        SELLABLE_UNKNOWN_OPEN_SELL_EVIDENCE
    ]
    # the same-day buy view does not read the order ledger
    assert sbe(out)["state"] == "known"


def test_no_reconcile_run_makes_every_sell_view_unknown() -> None:
    out = block(freshness=Freshness("missing", None, None))
    assert ose(out)["unknown_reasons"] == [UNKNOWN_NO_RECONCILE_RUN]
    assert ose(out)["blocking_reasons"] == [BLOCK_OPEN_SELL_EVIDENCE_UNKNOWN]
    assert sbe(out)["unknown_reasons"] == [UNKNOWN_NO_RECONCILE_RUN]
    assert sbe(out)["blocking_reasons"] == [BLOCK_BUY_EVIDENCE_UNKNOWN]
    assert out["sellable_by_ledger"] is None
    assert out["sellable_by_ledger_basis"]["unknown_reasons"] == [
        SELLABLE_UNKNOWN_LEDGER_STATE,
        SELLABLE_UNKNOWN_OPEN_SELL_EVIDENCE,
    ]


def test_every_unknown_reason_combines_with_the_concrete_reasons() -> None:
    out = block(
        [*HOLD_12, fill(2, "sell", "2", TODAY_MORNING, order="0000321")],
        reference="10",
        orders=None,
        freshness=compute_freshness(NOW - timedelta(minutes=91), NOW),
    )
    evidence = ose(out)
    assert evidence["unknown_reasons"] == [
        UNKNOWN_LEDGER_STALE,
        UNKNOWN_ORDER_LEDGER_READ_FAILED,
    ]
    assert evidence["blocking_reasons"] == [
        BLOCK_OPEN_SELL_EVIDENCE_UNKNOWN,
        BLOCK_SAME_DAY_SELL_FILL_UNPROVEN,
    ]


# ---------------------------------------------------------------------------
# the 90-minute staleness edge, for every blocking reason
# ---------------------------------------------------------------------------
AT_CAP = compute_freshness(NOW - timedelta(minutes=90), NOW)
OVER_CAP = compute_freshness(NOW - timedelta(minutes=90, seconds=1), NOW)


def test_edge_fixtures_straddle_the_cap() -> None:
    assert (AT_CAP.state, OVER_CAP.state) == ("fresh", "stale")


@pytest.mark.parametrize(
    ("scenario", "concrete"),
    [
        ("clean", []),
        ("own_open_sell", [BLOCK_OWN_OPEN_SELL]),
        ("unproven_sell_fill", [BLOCK_SAME_DAY_SELL_FILL_UNPROVEN]),
    ],
)
def test_open_sell_reasons_at_and_over_the_90_minute_edge(
    scenario: str, concrete: list[str]
) -> None:
    fills = list(HOLD_12)
    orders: list[OrderRow] = []
    reference = "12"
    if scenario == "own_open_sell":
        orders = [order(1, "accepted", TODAY_MORNING)]
    elif scenario == "unproven_sell_fill":
        fills.append(fill(2, "sell", "2", TODAY_MORNING, order="0000321"))
        reference = "10"

    at_cap = block(fills, reference=reference, orders=orders, freshness=AT_CAP)
    assert ose(at_cap)["state"] == "known"
    assert ose(at_cap)["blocking_reasons"] == concrete
    assert ose(at_cap)["blocking"] is bool(concrete)
    assert at_cap["sellable_by_ledger"] is not None

    over = block(fills, reference=reference, orders=orders, freshness=OVER_CAP)
    assert ose(over)["state"] == "unknown"
    assert ose(over)["unknown_reasons"] == [UNKNOWN_LEDGER_STALE]
    assert ose(over)["blocking_reasons"] == [
        BLOCK_OPEN_SELL_EVIDENCE_UNKNOWN,
        *concrete,
    ]
    assert ose(over)["blocking"] is True
    assert ose(over)["own_open_sell_order_quantity"] is None
    assert over["sellable_by_ledger"] is None


@pytest.mark.parametrize("with_buy", [False, True])
def test_same_day_buy_reasons_at_and_over_the_90_minute_edge(with_buy: bool) -> None:
    fills = list(HOLD_12)
    reference = "12"
    concrete: list[str] = []
    if with_buy:
        fills = [fill(1, "buy", "10", EARLIER), fill(2, "buy", "2", TODAY_MORNING)]
        concrete = [BLOCK_SAME_DAY_BUY_FILL_IN_LEDGER]
    at_cap = sbe(block(fills, reference=reference, freshness=AT_CAP))
    assert (at_cap["state"], at_cap["blocking_reasons"]) == ("known", concrete)
    over = sbe(block(fills, reference=reference, freshness=OVER_CAP))
    assert over["state"] == "unknown"
    assert over["blocking_reasons"] == [BLOCK_BUY_EVIDENCE_UNKNOWN, *concrete]


# ---------------------------------------------------------------------------
# across KST midnight (KST, not UTC)
# ---------------------------------------------------------------------------
JUST_BEFORE_MIDNIGHT = KST_MIDNIGHT - timedelta(seconds=1)  # 09-28 23:59:59 KST


def test_sell_order_rows_split_exactly_at_kst_midnight() -> None:
    today = block(orders=[order(1, "accepted", KST_MIDNIGHT)])
    prior = block(orders=[order(2, "accepted", JUST_BEFORE_MIDNIGHT)])
    assert ose(today)["blocking_reasons"] == [BLOCK_OWN_OPEN_SELL]
    assert ose(prior)["blocking"] is False
    assert len(ose(prior)["presumed_dead_prior_day_sells"]) == 1
    assert prior["sellable_by_ledger"] == "12"


def test_sell_and_buy_fills_split_exactly_at_kst_midnight() -> None:
    fills_today = [
        fill(1, "buy", "10", EARLIER),
        fill(2, "buy", "4", KST_MIDNIGHT, order="0000401"),
        fill(3, "sell", "2", KST_MIDNIGHT, order="0000402"),
    ]
    today = block(fills_today, reference="12")
    assert ose(today)["blocking_reasons"] == [BLOCK_SAME_DAY_SELL_FILL_UNPROVEN]
    assert sbe(today)["blocking_reasons"] == [BLOCK_SAME_DAY_BUY_FILL_IN_LEDGER]

    fills_prior = [
        fill(1, "buy", "10", EARLIER),
        fill(2, "buy", "4", JUST_BEFORE_MIDNIGHT, order="0000401"),
        fill(3, "sell", "2", JUST_BEFORE_MIDNIGHT, order="0000402"),
    ]
    prior = block(fills_prior, reference="12")
    assert ose(prior)["blocking"] is False
    assert sbe(prior)["blocking"] is False


def test_utc_date_does_not_decide_the_kst_day() -> None:
    # NOW = 00:30 KST on 09-29 (still 09-28 in UTC). A row from 23:30 KST on
    # 09-28 shares the UTC date with NOW but is the previous KST day.
    now = datetime(2026, 9, 28, 15, 30, tzinfo=UTC)
    fresh = Freshness("fresh", now - timedelta(minutes=10), 10.0)
    out = block(
        [fill(1, "buy", "12", EARLIER), fill(2, "buy", "1", now - timedelta(hours=1))],
        reference="13",
        orders=[order(1, "accepted", now - timedelta(hours=1))],
        freshness=fresh,
        now=now,
    )
    assert ose(out)["blocking"] is False
    assert sbe(out)["blocking"] is False
    assert out["sellable_by_ledger"] == "13"


# ---------------------------------------------------------------------------
# same_day_buy_evidence
# ---------------------------------------------------------------------------
def test_same_day_reconciled_buy_fill_blocks_and_is_listed() -> None:
    out = block(
        [fill(1, "buy", "10", EARLIER), fill(2, "buy", "2", TODAY_MORNING, order="77")],
        reference="12",
    )
    evidence = sbe(out)
    assert evidence["state"] == "known"
    assert evidence["blocking_reasons"] == [BLOCK_SAME_DAY_BUY_FILL_IN_LEDGER]
    assert [
        (f["broker_order_id"], f["side"], f["provisional"]) for f in evidence["fills"]
    ] == [("77", "buy", False)]
    # a proven-filled buy order does not clear the chain view: it is a fill, not an order
    proven = block(
        [fill(1, "buy", "10", EARLIER), fill(2, "buy", "2", TODAY_MORNING, order="77")],
        reference="12",
        orders=[order(1, "filled", TODAY_MORNING, side="buy", order_no="77")],
    )
    assert sbe(proven)["blocking"] is True
    # the sell side of the same symbol is clean
    assert ose(out)["blocking"] is False


def test_same_day_provisional_buy_fill_blocks_and_is_flagged_once() -> None:
    out = block(
        [
            fill(1, "buy", "10", EARLIER),
            fill(2, "buy", "2", TODAY_MORNING, order="0000088"),
            fill(3, "buy", "2", TODAY_MORNING, source="websocket", order="88"),
            fill(4, "buy", "1", TODAY_MORNING, source="websocket", order="99"),
        ],
        reference="12",
    )
    fills = sbe(out)["fills"]
    assert [(f["broker_order_id"], f["provisional"]) for f in fills] == [
        ("0000088", False),
        ("99", True),
    ]


def test_same_day_sells_and_prior_day_buys_do_not_block_the_buy_view() -> None:
    out = block(
        [
            fill(1, "buy", "12", YESTERDAY),
            fill(2, "sell", "2", TODAY_MORNING),
        ],
        reference="10",
    )
    assert sbe(out)["blocking"] is False
    assert sbe(out)["fills"] == []


# ---------------------------------------------------------------------------
# sellable_by_ledger: never negative, never above the broker quantity
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("open_qtys", "expected", "clamped"),
    [
        ([], "12", False),
        (["5"], "7", False),
        (["5", "7"], "0", False),
        (["5", "8"], "0", True),  # over-reserved: clamp at zero, never negative
        (["100"], "0", True),
        (["0.5"], "11.5", False),
    ],
)
def test_sellable_is_net_minus_open_sells_clamped_at_zero(
    open_qtys: list[str], expected: str, clamped: bool
) -> None:
    orders = [
        order(i + 1, "accepted", TODAY_MORNING, qty=q) for i, q in enumerate(open_qtys)
    ]
    out = block(orders=orders)
    assert out["sellable_by_ledger"] == expected
    assert out["sellable_by_ledger_basis"]["clamped"] is clamped
    assert D(out["sellable_by_ledger"]) >= 0
    assert D(out["sellable_by_ledger"]) <= D(out["reference_quantity"])


def test_open_sell_without_a_quantity_makes_sellable_unknown() -> None:
    out = block(orders=[order(1, "accepted", TODAY_MORNING, qty=None)])
    assert ose(out)["state"] == "known"
    assert ose(out)["own_open_sell_order_quantity"] is None
    assert out["sellable_by_ledger"] is None
    assert out["sellable_by_ledger_basis"]["unknown_reasons"] == [
        SELLABLE_UNKNOWN_OPEN_SELL_QUANTITY
    ]


def test_negative_open_sell_quantity_is_unknown_not_a_credit() -> None:
    out = block(orders=[order(1, "accepted", TODAY_MORNING, qty="-5")])
    assert out["sellable_by_ledger"] is None


@pytest.mark.parametrize(
    ("fills", "reference"),
    [
        ([], "12"),  # no ledger rows
        (HOLD_12, "13"),  # net disagrees with the broker
        (HOLD_12, None),  # broker quantity unavailable
        ([fill(1, "buy", "12", EARLIER, source="websocket")], "12"),  # provisional only
        ([fill(1, "sell", "3", EARLIER)], "12"),  # oversold history gap
    ],
)
def test_unknown_ledger_never_yields_a_sellable_quantity(fills, reference) -> None:
    out = block(fills, reference=reference)
    assert out["ledger_state"] == "unknown"
    assert out["sellable_by_ledger"] is None
    basis = out["sellable_by_ledger_basis"]
    assert basis["state"] == "unknown"
    assert SELLABLE_UNKNOWN_LEDGER_STATE in basis["unknown_reasons"]
    assert basis["ledger_net_quantity"] is None


def test_provisional_rows_never_raise_the_sellable_quantity() -> None:
    # A websocket buy the reconciler has not seen yet: broker 14, ledger 12.
    out = block(
        [*HOLD_12, fill(2, "buy", "2", TODAY_MORNING, source="websocket", order="5")],
        reference="14",
    )
    assert out["ledger_state"] == "unknown"
    assert out["sellable_by_ledger"] is None


@pytest.mark.parametrize("seed", range(40))
def test_sellable_bounds_hold_for_generated_ledgers(seed: int) -> None:
    import random

    rng = random.Random(seed)
    fills: list[LedgerFill] = []
    net = D("0")
    for i in range(rng.randint(1, 6)):
        side = "buy" if net == 0 or rng.random() < 0.6 else "sell"
        qty = D(rng.randint(1, 9)) if side == "buy" else D(rng.randint(1, int(net)))
        net += qty if side == "buy" else -qty
        when = rng.choice([EARLIER, YESTERDAY, TODAY_MORNING])
        fills.append(fill(i + 1, side, str(qty), when, order=f"9{seed}{i}"))
    orders = [
        order(
            j + 1,
            rng.choice(["accepted", "partial", "filled", "cancelled"]),
            rng.choice([YESTERDAY, TODAY_MORNING]),
            side=rng.choice(["buy", "sell"]),
            qty=str(rng.randint(0, 15)),
        )
        for j in range(rng.randint(0, 4))
    ]
    reference = str(net + rng.choice([0, 0, 0, 1]))
    out = block(fills, reference=reference, orders=orders)
    value = out["sellable_by_ledger"]
    if out["ledger_state"] != "known":
        assert value is None
        return
    assert value is not None
    assert D("0") <= D(value) <= D(reference)
    assert D(value) <= D(out["net_quantity"])


# ---------------------------------------------------------------------------
# the fail-closed unknown block
# ---------------------------------------------------------------------------
def test_unknown_block_carries_blocking_unknown_sell_side_fields() -> None:
    out = unknown_block("196170", kis_lots.UNKNOWN_LOAD_FAILED)
    assert ose(out)["state"] == "unknown"
    assert ose(out)["blocking"] is True
    assert ose(out)["blocking_reasons"] == [BLOCK_OPEN_SELL_EVIDENCE_UNKNOWN]
    assert ose(out)["external_orders_verifiable"] is False
    assert sbe(out)["state"] == "unknown"
    assert sbe(out)["blocking"] is True
    assert sbe(out)["blocking_reasons"] == [BLOCK_BUY_EVIDENCE_UNKNOWN]
    assert out["sellable_by_ledger"] is None
    assert out["sellable_by_ledger_basis"]["state"] == "unknown"
    json.dumps(out)


def test_unknown_block_and_projected_block_share_the_same_keys() -> None:
    projected = block()
    failed = unknown_block("196170", kis_lots.UNKNOWN_LOAD_FAILED)
    assert set(projected) == set(failed)
    for key in (
        "open_sell_evidence",
        "same_day_buy_evidence",
        "sellable_by_ledger_basis",
    ):
        assert set(projected[key]) == set(failed[key]), key


def test_every_new_blocking_reason_constant_is_exercised_here() -> None:
    import inspect
    import sys

    source = inspect.getsource(sys.modules[__name__])
    for name in (
        "BLOCK_OWN_OPEN_SELL",
        "BLOCK_SAME_DAY_SELL_FILL_UNPROVEN",
        "BLOCK_OPEN_SELL_EVIDENCE_UNKNOWN",
        "BLOCK_SAME_DAY_BUY_FILL_IN_LEDGER",
        "BLOCK_BUY_EVIDENCE_UNKNOWN",
    ):
        assert source.count(name) >= 3, name
    assert kis_lots.BLOCK_OWN_OPEN_SELL == "own_nonterminal_sell_order_today"
    assert (
        kis_lots.BLOCK_SAME_DAY_SELL_FILL_UNPROVEN
        == "same_day_sell_fill_order_not_proven_complete"
    )
    assert kis_lots.BLOCK_OPEN_SELL_EVIDENCE_UNKNOWN == "open_sell_evidence_unknown"


# ---------------------------------------------------------------------------
# round 1 (tester BLOCKER): only SEED-* manual_import rows are opening
# snapshots; any other manual_import row is an actual authoritative fill and
# must count as same-day evidence in all four same-day views.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("when", [KST_MIDNIGHT, TODAY_MORNING])
def test_nonseed_manual_import_buy_today_blocks_both_buy_views(when) -> None:
    out = block(
        [
            fill(1, "buy", "12", EARLIER),
            fill(2, "buy", "3", when, source="manual_import", order="MANUAL-FIX-1"),
        ],
        reference="15",
        freshness=AT_CAP,
    )
    assert out["ledger_state"] == "known", out["unknown_reasons"]
    assert sbe(out)["blocking_reasons"] == [BLOCK_SAME_DAY_BUY_FILL_IN_LEDGER]
    assert [f["broker_order_id"] for f in sbe(out)["fills"]] == ["MANUAL-FIX-1"]
    # the #963 buy view: an actual buy fill whose order is not proven complete
    assert out["open_buy_evidence"]["blocking_reasons"] == [
        kis_lots.BLOCK_SAME_DAY_FILL
    ]


@pytest.mark.parametrize("when", [KST_MIDNIGHT, TODAY_MORNING])
def test_nonseed_manual_import_sell_today_blocks_both_sell_views(when) -> None:
    out = block(
        [
            fill(1, "buy", "12", EARLIER),
            fill(2, "sell", "2", when, source="manual_import", order="MANUAL-FIX-2"),
        ],
        reference="10",
        freshness=AT_CAP,
    )
    assert out["ledger_state"] == "known", out["unknown_reasons"]
    assert ose(out)["blocking_reasons"] == [BLOCK_SAME_DAY_SELL_FILL_UNPROVEN]
    assert [
        f["broker_order_id"] for f in ose(out)["same_day_sell_fills_unproven_complete"]
    ] == ["MANUAL-FIX-2"]
    # the #963 sell view for buys
    assert out["same_day_sell_evidence"]["blocking_reasons"] == [
        kis_lots.BLOCK_SAME_DAY_SELL_FILL
    ]


def test_seed_prefixed_manual_import_today_is_still_not_same_day_evidence() -> None:
    out = block(
        [
            fill(
                1,
                "buy",
                "12",
                KST_MIDNIGHT,
                source="manual_import",
                order="SEED-20260929-kis-krx-196170",
            )
        ],
    )
    for key in ("open_buy_evidence", "same_day_buy_evidence"):
        assert out[key]["blocking"] is False, key


def test_nonseed_manual_import_prior_day_does_not_block() -> None:
    out = block(
        [
            fill(1, "buy", "12", EARLIER),
            fill(
                2, "buy", "3", JUST_BEFORE_MIDNIGHT, source="manual_import", order="M1"
            ),
            fill(
                3, "sell", "2", JUST_BEFORE_MIDNIGHT, source="manual_import", order="M2"
            ),
        ],
        reference="13",
    )
    assert ose(out)["blocking"] is False
    assert sbe(out)["blocking"] is False
    assert out["sellable_by_ledger"] == "13"

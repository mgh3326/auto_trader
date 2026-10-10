"""Task #963 — pure projection tests for KIS live KR ledger lots (no DB, no broker)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.services.execution_ledger import kis_lots
from app.services.execution_ledger.kis_lots import (
    BLOCK_EVIDENCE_UNKNOWN,
    BLOCK_OWN_OPEN_BUY,
    BLOCK_SAME_DAY_FILL,
    BLOCK_SAME_DAY_SELL_FILL,
    BLOCK_SELL_EVIDENCE_UNKNOWN,
    FRESH_MAX_MINUTES,
    UNKNOWN_HISTORY_GAP,
    UNKNOWN_LEDGER_STALE,
    UNKNOWN_NO_LEDGER_ROWS,
    UNKNOWN_NO_RECONCILE_RUN,
    UNKNOWN_ONLY_PROVISIONAL_ROWS,
    UNKNOWN_ORDER_LEDGER_READ_FAILED,
    UNKNOWN_PROVISIONAL_PENDING,
    UNKNOWN_QTY_MISMATCH,
    UNKNOWN_REFERENCE_MISSING,
    Freshness,
    LedgerFill,
    OrderRow,
    build_symbol_block,
    compute_freshness,
    to_decimal,
    unknown_block,
)

pytestmark = pytest.mark.unit

# 2026-09-29 14:30 KST (a 14:30 KR regular-session rep).
NOW = datetime(2026, 9, 29, 5, 30, tzinfo=UTC)
FRESH = Freshness("fresh", NOW - timedelta(minutes=20), 20.0)
D = Decimal


def fill(
    fid: int,
    side: str,
    qty: str,
    price: str,
    when: datetime,
    *,
    source: str = "reconciler",
    order: str | None = None,
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
    order_no: str | None = None,
    *,
    side: str = "buy",
) -> OrderRow:
    return OrderRow(
        id=oid,
        order_no=order_no or f"000{oid}",
        status=status,
        quantity=D("5"),
        price=D("1000"),
        trade_date=when,
        side=side,
    )


def block(
    fills,
    *,
    reference: str | None = "12",
    orders=(),
    freshness: Freshness = FRESH,
    current_price: str | None = None,
):
    return build_symbol_block(
        symbol="196170",
        reference_quantity=None if reference is None else D(reference),
        current_price=None if current_price is None else D(current_price),
        fills=fills,
        orders=orders,
        freshness=freshness,
        now=NOW,
    )


LONG_AGO = NOW - timedelta(days=30)
SEED_AT = NOW - timedelta(days=20)
EARLIER = NOW - timedelta(days=10)
YESTERDAY = NOW - timedelta(days=1)
TODAY_MORNING = NOW - timedelta(hours=4)  # 10:30 KST, same KST day


# ---------------------------------------------------------------------------
# lots
# ---------------------------------------------------------------------------
def test_known_fifo_lots_seed_then_buy_then_partial_sell() -> None:
    fills = [
        fill(1, "buy", "10", "100000", SEED_AT, source="manual_import", order="SEED-1"),
        fill(2, "buy", "5", "90000", EARLIER),
        fill(3, "sell", "3", "110000", YESTERDAY),
    ]
    out = block(fills, reference="12", current_price="81000")

    assert out["ledger_state"] == "known"
    assert out["unknown_reasons"] == []
    assert out["cost_method"] == "fifo_remaining_lots_from_ledger"
    assert "NOT the broker moving-average" in out["cost_basis_note"]
    lots = out["lots"]
    assert [(lot["origin"], lot["quantity"], lot["unit_cost"]) for lot in lots] == [
        ("opening_seed", "7", "100000.0000"),
        ("fill", "5", "90000.0000"),
    ]
    # (7*100000 + 5*90000) / 12
    assert out["weighted_avg_cost"] == "95833.3333"
    assert out["net_quantity"] == "12"
    assert out["quantity_reconciles"] is True
    assert lots[0]["unrealized_pnl_pct"] == "-19.00"
    assert lots[1]["unrealized_pnl_pct"] == "-10.00"
    assert out["weighted_unrealized_pnl_pct"] == "-15.48"
    assert out["freshness"]["state"] == "fresh"
    assert out["freshness"]["fresh_max_minutes"] == FRESH_MAX_MINUTES
    assert out["as_of"] == FRESH.last_reconcile_finished_at.isoformat()


def test_pnl_is_none_without_a_usable_current_price() -> None:
    fills = [fill(1, "buy", "12", "100000", EARLIER)]
    out = block(fills, current_price=None)
    assert out["ledger_state"] == "known"
    assert out["lots"][0]["unrealized_pnl_pct"] is None
    assert out["weighted_unrealized_pnl_pct"] is None


def test_fifo_is_ordered_by_time_not_input_order() -> None:
    fills = [
        fill(3, "sell", "4", "120000", YESTERDAY),
        fill(2, "buy", "10", "90000", EARLIER),
        fill(1, "buy", "6", "100000", SEED_AT, source="manual_import", order="SEED-1"),
    ]
    out = block(fills, reference="12")
    # sell consumes the oldest lot (seed, 6 @100000) first, then 4... i.e. 4 of the seed.
    assert [lot["quantity"] for lot in out["lots"]] == ["2", "10"]


# ---------------------------------------------------------------------------
# unknown is never an empty list
# ---------------------------------------------------------------------------
def test_empty_ledger_is_unknown_never_no_lots() -> None:
    out = block([], reference="12")
    assert out["ledger_state"] == "unknown"
    assert UNKNOWN_NO_LEDGER_ROWS in out["unknown_reasons"]
    assert out["lots"] is None
    assert out["net_quantity"] is None
    assert out["weighted_avg_cost"] is None


def test_partial_history_sells_exceed_buys_is_unknown() -> None:
    fills = [
        fill(1, "buy", "3", "100000", EARLIER),
        fill(2, "sell", "5", "110000", YESTERDAY),
    ]
    out = block(fills, reference="10")
    assert out["ledger_state"] == "unknown"
    assert UNKNOWN_HISTORY_GAP in out["unknown_reasons"]
    assert out["lots"] is None
    assert out["diagnostics"]["oversold_quantity"] == "2"


def test_quantity_mismatch_with_broker_reference_is_unknown() -> None:
    out = block([fill(1, "buy", "10", "100000", EARLIER)], reference="12")
    assert out["ledger_state"] == "unknown"
    assert out["unknown_reasons"] == [UNKNOWN_QTY_MISMATCH]
    assert out["quantity_reconciles"] is False
    assert out["lots"] is None
    assert out["diagnostics"]["ledger_net_quantity"] == "10"


@pytest.mark.parametrize("reference", [None, "0", "-3"])
def test_missing_or_nonpositive_reference_is_unknown(reference: str | None) -> None:
    out = block([fill(1, "buy", "10", "100000", EARLIER)], reference=reference)
    assert out["ledger_state"] == "unknown"
    assert UNKNOWN_REFERENCE_MISSING in out["unknown_reasons"]
    assert out["quantity_reconciles"] is None
    assert out["lots"] is None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"reference": "12"},
        {"reference": None},
        {"reference": "3", "freshness": Freshness("missing", None, None)},
    ],
)
def test_unknown_blocks_never_carry_an_empty_lots_list(kwargs) -> None:
    out = block([], **kwargs)
    assert out["ledger_state"] == "unknown"
    assert out["lots"] is None  # never []


# ---------------------------------------------------------------------------
# provisional websocket rows are never truth
# ---------------------------------------------------------------------------
def test_websocket_only_rows_are_never_lots() -> None:
    fills = [fill(1, "buy", "12", "100000", TODAY_MORNING, source="websocket")]
    out = block(fills, reference="12")
    assert out["ledger_state"] == "unknown"
    assert UNKNOWN_ONLY_PROVISIONAL_ROWS in out["unknown_reasons"]
    # The broker-quantity cross-check also fires (ledger net 0 != broker 12).
    assert UNKNOWN_QTY_MISMATCH in out["unknown_reasons"]
    assert out["lots"] is None
    (excluded,) = out["provisional_rows_excluded"]
    assert excluded["provisional"] is True
    assert excluded["source"] == "websocket"


def test_provisional_rows_pending_reconcile_stay_unknown_with_reason() -> None:
    fills = [
        fill(1, "buy", "10", "100000", EARLIER),
        fill(2, "buy", "2", "99000", TODAY_MORNING, source="websocket", order="0009"),
    ]
    out = block(fills, reference="12")
    assert out["ledger_state"] == "unknown"
    assert out["unknown_reasons"] == [
        UNKNOWN_QTY_MISMATCH,
        UNKNOWN_PROVISIONAL_PENDING,
    ]
    assert out["lots"] is None  # authoritative 10 != broker 12; provisional not truth
    assert out["diagnostics"]["provisional_net_quantity"] == "2"
    assert len(out["provisional_rows_excluded"]) == 1


def test_websocket_duplicate_of_reconciled_order_is_superseded_not_double_counted() -> (
    None
):
    fills = [
        fill(1, "buy", "12", "100000", EARLIER, order="0000123"),
        fill(2, "buy", "12", "100000", EARLIER, source="websocket", order="123"),
    ]
    out = block(fills, reference="12")
    assert out["ledger_state"] == "known"
    assert out["provisional_rows_excluded"] == []
    assert out["diagnostics"]["superseded_websocket_duplicates"] == 1
    assert out["diagnostics"]["provisional_row_count"] == 0


# ---------------------------------------------------------------------------
# freshness
# ---------------------------------------------------------------------------
def test_freshness_thresholds_are_inclusive_at_the_cap() -> None:
    assert FRESH_MAX_MINUTES == 90
    at_cap = compute_freshness(NOW - timedelta(minutes=90), NOW)
    over = compute_freshness(NOW - timedelta(minutes=90, seconds=1), NOW)
    assert (at_cap.state, over.state) == ("fresh", "stale")
    assert compute_freshness(None, NOW).state == "missing"
    # A finish time in the future cannot be verified as recent.
    assert compute_freshness(NOW + timedelta(minutes=5), NOW).state == "missing"
    # Naive timestamps are treated as UTC.
    naive = compute_freshness((NOW - timedelta(minutes=10)).replace(tzinfo=None), NOW)
    assert naive.state == "fresh"


def test_stale_ledger_is_never_read_as_fresh() -> None:
    stale = compute_freshness(NOW - timedelta(minutes=91), NOW)
    out = block([fill(1, "buy", "12", "100000", EARLIER)], freshness=stale)
    assert out["ledger_state"] == "unknown"
    assert out["unknown_reasons"] == [UNKNOWN_LEDGER_STALE]
    assert out["lots"] is None
    assert out["freshness"]["state"] == "stale"
    assert out["freshness"]["lag_minutes"] == 91.0
    # Stale evidence also blocks new buys.
    evidence = out["open_buy_evidence"]
    assert evidence["state"] == "unknown"
    assert evidence["blocking"] is True


def test_no_reconcile_run_is_unknown() -> None:
    out = block(
        [fill(1, "buy", "12", "100000", EARLIER)],
        freshness=Freshness("missing", None, None),
    )
    assert UNKNOWN_NO_RECONCILE_RUN in out["unknown_reasons"]
    assert out["as_of"] is None
    assert out["lots"] is None


# ---------------------------------------------------------------------------
# S2: own non-terminal buy rows in kis_live_order_ledger
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "status", ["accepted", "pending", "partial", "unknown", "anomaly", "brand_new"]
)
def test_same_day_nonterminal_own_buy_blocks(status: str) -> None:
    out = block(
        [fill(1, "buy", "12", "100000", EARLIER)],
        orders=[order(1, status, TODAY_MORNING)],
    )
    evidence = out["open_buy_evidence"]
    assert evidence["state"] == "known"
    assert evidence["blocking"] is True
    assert evidence["blocking_reasons"] == [BLOCK_OWN_OPEN_BUY]
    assert [o["status"] for o in evidence["kis_live_order_ledger_open_buys"]] == [
        status
    ]


@pytest.mark.parametrize("status", ["filled", "cancelled", "expired", "rejected"])
def test_same_day_terminal_own_buy_does_not_block(status: str) -> None:
    out = block(
        [fill(1, "buy", "12", "100000", EARLIER)],
        orders=[order(1, status, TODAY_MORNING)],
    )
    evidence = out["open_buy_evidence"]
    assert evidence["blocking"] is False
    assert evidence["blocking_reasons"] == []


def test_prior_day_nonterminal_row_is_reported_but_not_blocking() -> None:
    out = block(
        [fill(1, "buy", "12", "100000", EARLIER)],
        orders=[order(1, "accepted", YESTERDAY)],
    )
    evidence = out["open_buy_evidence"]
    assert evidence["blocking"] is False
    assert [o["status"] for o in evidence["presumed_dead_prior_day_buys"]] == [
        "accepted"
    ]
    assert evidence["kis_live_order_ledger_open_buys"] == []


def test_kst_day_boundary_uses_kst_not_utc() -> None:
    # 2026-09-28 15:30 UTC == 2026-09-29 00:30 KST -> same KST day as NOW.
    just_after_kst_midnight = datetime(2026, 9, 28, 15, 30, tzinfo=UTC)
    # 2026-09-28 14:59 UTC == 2026-09-28 23:59 KST -> previous KST day.
    just_before_kst_midnight = datetime(2026, 9, 28, 14, 59, tzinfo=UTC)
    base = [fill(1, "buy", "12", "100000", EARLIER)]
    today = block(base, orders=[order(1, "accepted", just_after_kst_midnight)])
    prior = block(base, orders=[order(2, "accepted", just_before_kst_midnight)])
    assert today["open_buy_evidence"]["blocking"] is True
    assert prior["open_buy_evidence"]["blocking"] is False


# ---------------------------------------------------------------------------
# S3: same-day buy fills whose order is not proven complete
# ---------------------------------------------------------------------------
def test_same_day_buy_fill_without_order_row_blocks_as_possible_external_order() -> (
    None
):
    out = block(
        [
            fill(1, "buy", "10", "100000", EARLIER),
            fill(2, "buy", "2", "99000", TODAY_MORNING, order="7777"),
        ],
        reference="12",
    )
    evidence = out["open_buy_evidence"]
    assert evidence["blocking"] is True
    assert evidence["blocking_reasons"] == [BLOCK_SAME_DAY_FILL]
    (hit,) = evidence["same_day_buy_fills_unproven_complete"]
    assert hit["broker_order_id"] == "7777"
    assert hit["provisional"] is False


def test_same_day_provisional_buy_fill_blocks_and_is_flagged() -> None:
    out = block(
        [
            fill(1, "buy", "12", "100000", EARLIER),
            fill(2, "buy", "2", "99000", TODAY_MORNING, source="websocket"),
        ],
    )
    (hit,) = out["open_buy_evidence"]["same_day_buy_fills_unproven_complete"]
    assert hit["provisional"] is True
    assert out["open_buy_evidence"]["blocking"] is True


def test_same_day_fill_of_order_proven_filled_by_order_ledger_does_not_block() -> None:
    out = block(
        [
            fill(1, "buy", "10", "100000", EARLIER),
            fill(2, "buy", "2", "99000", TODAY_MORNING, order="0000555"),
        ],
        orders=[order(1, "filled", TODAY_MORNING, order_no="555")],
    )
    evidence = out["open_buy_evidence"]
    assert evidence["blocking"] is False
    assert evidence["same_day_buy_fills_unproven_complete"] == []


def test_same_day_fill_with_partial_order_row_blocks_via_s2() -> None:
    out = block(
        [fill(2, "buy", "2", "99000", TODAY_MORNING, order="555")],
        orders=[order(1, "partial", TODAY_MORNING, order_no="555")],
        reference="2",
    )
    evidence = out["open_buy_evidence"]
    assert BLOCK_OWN_OPEN_BUY in evidence["blocking_reasons"]
    assert BLOCK_SAME_DAY_FILL in evidence["blocking_reasons"]


def test_seeded_opening_lot_dated_today_is_not_a_same_day_buy() -> None:
    out = block(
        [
            fill(
                1,
                "buy",
                "12",
                "100000",
                TODAY_MORNING,
                source="manual_import",
                order="SEED-20260929-kis-krx-196170",
            )
        ]
    )
    assert out["ledger_state"] == "known"
    assert out["open_buy_evidence"]["blocking"] is False


def test_sells_and_prior_day_buy_fills_do_not_block() -> None:
    out = block(
        [
            fill(1, "buy", "15", "100000", EARLIER),
            fill(2, "buy", "1", "99000", YESTERDAY),
            fill(3, "sell", "4", "101000", TODAY_MORNING),
        ],
        reference="12",
    )
    assert out["ledger_state"] == "known"
    assert out["open_buy_evidence"]["blocking"] is False


def test_websocket_duplicate_of_same_day_reconciled_fill_counts_once() -> None:
    out = block(
        [
            fill(1, "buy", "12", "100000", TODAY_MORNING, order="0000900"),
            fill(
                2, "buy", "12", "100000", TODAY_MORNING, source="websocket", order="900"
            ),
        ]
    )
    hits = out["open_buy_evidence"]["same_day_buy_fills_unproven_complete"]
    assert len(hits) == 1
    assert hits[0]["source"] == "reconciler"


# ---------------------------------------------------------------------------
# evidence is unknown => blocking (fail-closed)
# ---------------------------------------------------------------------------
def test_order_ledger_read_failure_makes_open_buy_evidence_unknown_and_blocking() -> (
    None
):
    out = block([fill(1, "buy", "12", "100000", EARLIER)], orders=None)
    evidence = out["open_buy_evidence"]
    assert evidence["state"] == "unknown"
    assert UNKNOWN_ORDER_LEDGER_READ_FAILED in evidence["unknown_reasons"]
    assert evidence["blocking"] is True
    assert BLOCK_EVIDENCE_UNKNOWN in evidence["blocking_reasons"]
    # Lots are independent of the order-ledger read and stay usable.
    assert out["ledger_state"] == "known"


def test_external_orders_are_always_declared_unverifiable() -> None:
    clean = block([fill(1, "buy", "12", "100000", EARLIER)])
    assert clean["open_buy_evidence"]["blocking"] is False
    assert clean["open_buy_evidence"]["external_orders_verifiable"] is False
    assert clean["open_buy_evidence"]["scope"] == "orders_known_to_auto_trader_only"
    assert (
        unknown_block("196170", "x")["open_buy_evidence"]["external_orders_verifiable"]
        is False
    )


def test_unknown_block_shape_is_fail_closed() -> None:
    out = unknown_block("196170", kis_lots.UNKNOWN_LOAD_FAILED)
    assert out["ledger_state"] == "unknown"
    assert out["unknown_reasons"] == ["ledger_read_failed"]
    assert out["lots"] is None
    assert out["open_buy_evidence"]["blocking"] is True
    assert out["open_buy_evidence"]["state"] == "unknown"


def test_to_decimal_handles_floats_and_junk() -> None:
    assert to_decimal(12.0) == D("12")
    assert to_decimal("3.5") == D("3.5")
    for junk in (None, "", "abc", float("nan"), float("inf")):
        assert to_decimal(junk) is None


def test_module_has_no_broker_import_or_write_verbs() -> None:
    import inspect

    source = inspect.getsource(kis_lots)
    assert "app.services.brokers" not in source
    assert "KISClient" not in source
    for verb in ("db.add(", "delete(", "update(", "insert(", "commit(", "flush("):
        assert verb not in source


def test_blocks_are_json_serializable() -> None:
    import json

    fills = [
        fill(1, "buy", "10", "100000", SEED_AT, source="manual_import", order="SEED-1"),
        fill(2, "buy", "2", "99000", TODAY_MORNING, source="websocket", order="9"),
    ]
    json.dumps(block(fills, reference="10", current_price="81000"))
    json.dumps(block([], reference=None, orders=None))
    json.dumps(unknown_block("196170", kis_lots.UNKNOWN_LOAD_FAILED))


# ---------------------------------------------------------------------------
# #935 Part B: 06-10..09-14 the same fill exists as a reconciler row AND a
# websocket row (different fill_seq). Never double count; a fill that exists
# only as a websocket row must end unknown through the broker-quantity
# cross-check, never as a lower lot count.
# ---------------------------------------------------------------------------
def test_935_same_fill_as_reconciler_and_websocket_is_not_double_counted() -> None:
    fills = [
        fill(1, "buy", "10", "100000", SEED_AT, source="manual_import", order="SEED-1"),
        fill(2, "buy", "2", "99000", EARLIER, order="0000777"),
        # websocket twin of order 777: different row id (and, in the ledger,
        # a different fill_seq), same fill, zero-padding drift on the order id.
        fill(3, "buy", "2", "99000", EARLIER, source="websocket", order="777"),
    ]
    out = block(fills, reference="12")
    assert out["ledger_state"] == "known", out["unknown_reasons"]
    assert sum(D(lot["quantity"]) for lot in out["lots"]) == D("12")
    assert out["net_quantity"] == "12"
    assert out["diagnostics"]["superseded_websocket_duplicates"] == 1
    assert out["diagnostics"]["provisional_row_count"] == 0
    assert out["diagnostics"]["provisional_net_quantity"] == "0"
    assert out["provisional_rows_excluded"] == []


def test_935_split_websocket_partials_of_a_reconciled_order_are_all_superseded() -> (
    None
):
    fills = [
        fill(1, "buy", "10", "100000", SEED_AT, source="manual_import", order="SEED-1"),
        fill(2, "buy", "5", "99000", EARLIER, order="0000800"),
        fill(3, "buy", "2", "99000", EARLIER, source="websocket", order="800"),
        fill(4, "buy", "3", "99000", EARLIER, source="websocket", order="0000800"),
    ]
    out = block(fills, reference="15")
    assert out["ledger_state"] == "known", out["unknown_reasons"]
    assert out["net_quantity"] == "15"
    assert out["diagnostics"]["superseded_websocket_duplicates"] == 2


def test_935_websocket_only_fill_is_unknown_never_a_lower_lot_count() -> None:
    fills = [
        fill(1, "buy", "10", "100000", SEED_AT, source="manual_import", order="SEED-1"),
        fill(2, "buy", "2", "99000", EARLIER, source="websocket", order="0000900"),
    ]
    out = block(fills, reference="12")  # broker holds seed 10 + the websocket-only 2
    assert out["ledger_state"] == "unknown"
    assert UNKNOWN_QTY_MISMATCH in out["unknown_reasons"]
    assert out["quantity_reconciles"] is False
    # No lot list at all — in particular not a 10-share list under a 12-share holding.
    assert out["lots"] is None
    assert out["net_quantity"] is None
    assert out["weighted_avg_cost"] is None
    assert out["diagnostics"]["ledger_net_quantity"] == "10"
    assert out["diagnostics"]["provisional_net_quantity"] == "2"
    (excluded,) = out["provisional_rows_excluded"]
    assert excluded["provisional"] is True


def test_935_symbol_with_only_websocket_rows_is_unknown_via_the_cross_check() -> None:
    fills = [
        fill(1, "buy", "7", "50000", EARLIER, source="websocket", order="1"),
        fill(2, "buy", "5", "51000", YESTERDAY, source="websocket", order="2"),
    ]
    out = block(fills, reference="12")
    assert out["ledger_state"] == "unknown"
    assert UNKNOWN_QTY_MISMATCH in out["unknown_reasons"]
    assert UNKNOWN_ONLY_PROVISIONAL_ROWS in out["unknown_reasons"]
    assert out["lots"] is None
    assert out["net_quantity"] is None


def test_935_unmatched_websocket_row_next_to_a_reconciled_fill_does_not_inflate() -> (
    None
):
    fills = [
        fill(1, "buy", "12", "100000", EARLIER, order="0001"),
        # Same shares under an order id no authoritative row carries: it stays
        # provisional (listed), and must not add to lots or net.
        fill(2, "buy", "12", "100000", EARLIER, source="websocket", order="9999"),
    ]
    out = block(fills, reference="12")
    assert out["ledger_state"] == "known", out["unknown_reasons"]
    assert sum(D(lot["quantity"]) for lot in out["lots"]) == D("12")
    assert out["net_quantity"] == "12"
    assert len(out["provisional_rows_excluded"]) == 1
    assert out["diagnostics"]["provisional_net_quantity"] == "12"
    # The broker quantity does not include a phantom second 12, so counting the
    # websocket row would have made the cross-check fail: it is not counted.
    assert out["quantity_reconciles"] is True


# ---------------------------------------------------------------------------
# F5 (round 1): rows dated later than today are clock skew => fail closed
# ---------------------------------------------------------------------------
def test_future_dated_nonterminal_own_buy_row_blocks() -> None:
    tomorrow = NOW + timedelta(hours=12)  # 02:30 KST on the next day
    out = block(
        [fill(1, "buy", "12", "100000", EARLIER)],
        orders=[order(1, "accepted", tomorrow)],
    )
    evidence = out["open_buy_evidence"]
    assert evidence["blocking"] is True
    assert evidence["blocking_reasons"] == [BLOCK_OWN_OPEN_BUY]
    assert evidence["presumed_dead_prior_day_buys"] == []


def test_future_dated_buy_fill_blocks() -> None:
    tomorrow = NOW + timedelta(hours=12)
    out = block(
        [
            fill(1, "buy", "10", "100000", EARLIER),
            fill(2, "buy", "2", "99000", tomorrow, order="4242"),
        ],
        reference="12",
    )
    assert out["open_buy_evidence"]["blocking"] is True
    assert out["open_buy_evidence"]["blocking_reasons"] == [BLOCK_SAME_DAY_FILL]


# ---------------------------------------------------------------------------
# strategy-lab 796/798: same-day sell visibility for the chain / wash check
# ---------------------------------------------------------------------------
def sell_ev(out):
    return out["same_day_sell_evidence"]


def test_same_day_reconciled_sell_fill_blocks_and_is_listed() -> None:
    out = block(
        [
            fill(1, "buy", "12", "100000", EARLIER),
            fill(2, "sell", "2", "101000", TODAY_MORNING, order="0000321"),
        ],
        reference="10",
    )
    assert out["ledger_state"] == "known", out["unknown_reasons"]
    assert out["net_quantity"] == "10"
    evidence = sell_ev(out)
    assert evidence["state"] == "known"
    assert evidence["blocking"] is True
    assert evidence["blocking_reasons"] == [BLOCK_SAME_DAY_SELL_FILL]
    (hit,) = evidence["fills"]
    assert (hit["broker_order_id"], hit["side"], hit["provisional"]) == (
        "0000321",
        "sell",
        False,
    )
    # the buy side stays clean: a sell is not an open buy
    assert out["open_buy_evidence"]["blocking"] is False


def test_same_day_provisional_sell_fill_blocks_and_is_flagged() -> None:
    out = block(
        [
            fill(1, "buy", "12", "100000", EARLIER),
            fill(2, "sell", "2", "101000", TODAY_MORNING, source="websocket"),
        ],
        reference="10",  # the broker already reflects the websocket-only sell
    )
    evidence = sell_ev(out)
    assert evidence["blocking"] is True
    assert [f["provisional"] for f in evidence["fills"]] == [True]
    # websocket rows never touch lots or net: the lots are unknown, not 10 shares
    assert out["ledger_state"] == "unknown"
    assert UNKNOWN_QTY_MISMATCH in out["unknown_reasons"]
    assert out["lots"] is None
    assert out["diagnostics"]["ledger_net_quantity"] == "12"


def test_websocket_twin_of_a_reconciled_sell_is_listed_once() -> None:
    out = block(
        [
            fill(1, "buy", "12", "100000", EARLIER),
            fill(2, "sell", "2", "101000", TODAY_MORNING, order="0000500"),
            fill(
                3, "sell", "2", "101000", TODAY_MORNING, source="websocket", order="500"
            ),
        ],
        reference="10",
    )
    assert len(sell_ev(out)["fills"]) == 1
    assert sell_ev(out)["fills"][0]["source"] == "reconciler"


def test_prior_day_sell_and_same_day_buys_do_not_block_the_sell_evidence() -> None:
    out = block(
        [
            fill(1, "buy", "12", "100000", EARLIER),
            fill(2, "sell", "1", "101000", YESTERDAY),
            fill(3, "buy", "1", "99000", TODAY_MORNING, order="0000600"),
        ],
        reference="12",
        orders=[order(1, "filled", TODAY_MORNING, order_no="600")],
    )
    evidence = sell_ev(out)
    assert evidence["state"] == "known"
    assert evidence["blocking"] is False
    assert evidence["fills"] == []


def test_sell_evidence_is_unknown_and_blocking_when_the_ledger_is_not_fresh() -> None:
    for freshness, reason in (
        (compute_freshness(NOW - timedelta(minutes=91), NOW), UNKNOWN_LEDGER_STALE),
        (Freshness("missing", None, None), UNKNOWN_NO_RECONCILE_RUN),
    ):
        evidence = sell_ev(
            block([fill(1, "buy", "12", "100000", EARLIER)], freshness=freshness)
        )
        assert evidence["state"] == "unknown"
        assert evidence["unknown_reasons"] == [reason]
        assert evidence["blocking"] is True
        assert evidence["blocking_reasons"] == [BLOCK_SELL_EVIDENCE_UNKNOWN]


def test_future_dated_sell_fill_blocks() -> None:
    tomorrow = NOW + timedelta(hours=12)
    out = block(
        [
            fill(1, "buy", "12", "100000", EARLIER),
            fill(2, "sell", "2", "101000", tomorrow, order="777"),
        ],
        reference="10",
    )
    assert sell_ev(out)["blocking"] is True


def test_unknown_block_carries_blocking_sell_evidence() -> None:
    evidence = sell_ev(unknown_block("196170", kis_lots.UNKNOWN_LOAD_FAILED))
    assert evidence["state"] == "unknown"
    assert evidence["blocking"] is True
    assert evidence["fills"] == []

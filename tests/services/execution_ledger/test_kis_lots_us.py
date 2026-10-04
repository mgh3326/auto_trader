"""Task #1173 — KIS live US lot block, pure projection (no DB, no broker).

A1 known block (FIFO lots, freshness, caveat) and mismatch -> unknown.
A2 phantom (websocket accept-notice) rows never count toward lots, net,
   reconciliation or sellable; they may only add blocking. Quarantined rows
   never reach this function (SQL filter, see test_kis_lots_us_db.py).
A3 the #1087 sell-side fields are present for US, fail closed the same way.
A4 the KR block is byte-identical to the pre-#1173 golden.
Also: the US trading-date boundary and the US venue spellings.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from app.services.execution_ledger import kis_lots
from app.services.execution_ledger.kis_lots import (
    BLOCK_OWN_OPEN_SELL,
    BLOCK_SAME_DAY_BUY_FILL_IN_LEDGER,
    BLOCK_SAME_DAY_FILL,
    BLOCK_SAME_DAY_SELL_FILL,
    UNKNOWN_LEDGER_STALE,
    UNKNOWN_LOAD_FAILED,
    UNKNOWN_PROVISIONAL_PENDING,
    UNKNOWN_QTY_MISMATCH,
    UNKNOWN_UNRECOGNIZED_VENUE,
    US_VENUES,
    Freshness,
    LedgerFill,
    OrderRow,
    build_symbol_block,
    unknown_block,
    us_trading_day_window,
)
from tests.services.execution_ledger.kis_lots_kr_golden_support import (
    GOLDEN_PATH,
    render,
)

pytestmark = pytest.mark.unit

# 2026-10-01 10:00 EDT = 23:00 KST: inside the US regular session.
NOW = datetime(2026, 10, 1, 14, 0, tzinfo=UTC)
US_DAY_START = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)  # 2026-09-30 20:00 EDT
SESSION_FILL = datetime(2026, 10, 1, 13, 45, tzinfo=UTC)  # 09:45 EDT
SEED_AT = datetime(2026, 9, 1, tzinfo=UTC)
FRESH = Freshness("fresh", NOW - timedelta(minutes=12), 12.0)

US_ONLY_KEYS = {
    "market",
    "currency",
    "accepted_venues",
    "trading_day_basis",
    "trading_day_start",
    "order_ledger_sources",
}


def fill(
    fid: int,
    side: str,
    qty: str,
    price: str,
    at: datetime,
    *,
    source: str = "reconciler",
    order: str | None = None,
    venue: str = "NASD",
) -> LedgerFill:
    return LedgerFill(
        id=fid,
        source=source,
        side=side,
        quantity=Decimal(qty),
        price=Decimal(price),
        filled_at=at,
        broker_order_id=order or f"00{fid:06d}",
        venue=venue,
    )


def order(
    oid: int,
    side: str,
    status: str,
    at: datetime,
    *,
    qty: str | None = "1",
    order_no: str | None = None,
) -> OrderRow:
    return OrderRow(
        id=oid,
        order_no=order_no or f"{oid:010d}",
        status=status,
        quantity=None if qty is None else Decimal(qty),
        price=Decimal("400"),
        trade_date=at,
        side=side,
    )


def us_block(
    fills: list[LedgerFill],
    *,
    reference: str | None = "8",
    orders: list[OrderRow] | None = None,
    freshness: Freshness = FRESH,
    now: datetime = NOW,
    current_price: str | None = "420",
) -> dict[str, Any]:
    return build_symbol_block(
        symbol="MSFT",
        reference_quantity=None if reference is None else Decimal(reference),
        current_price=None if current_price is None else Decimal(current_price),
        fills=fills,
        orders=[] if orders is None else orders,
        freshness=freshness,
        now=now,
        market="us",
    )


def msft_history() -> list[LedgerFill]:
    """Seed 5 @ 380 (NASD), buy 4 @ 400 (NASD), sell 1 @ 410 (NASD) -> net 8."""
    return [
        fill(
            1,
            "buy",
            "5",
            "380",
            SEED_AT,
            source="manual_import",
            order="SEED-20260901-MSFT",
        ),
        fill(2, "buy", "4", "400", NOW - timedelta(days=6)),
        fill(3, "sell", "1", "410", NOW - timedelta(days=3)),
    ]


# ------------------------------------------------------------------ A1


def test_a1_known_block_has_fifo_lots_freshness_and_caveat() -> None:
    block = us_block(msft_history())

    assert block["ledger_state"] == "known", block["unknown_reasons"]
    assert block["unknown_reasons"] == []
    assert block["quantity_reconciles"] is True
    assert block["net_quantity"] == "8"
    # FIFO: the sell consumed the oldest lot (the opening seed) first.
    assert [
        (lot["quantity"], lot["unit_cost"], lot["origin"]) for lot in block["lots"]
    ] == [
        ("4", "380.0000", "opening_seed"),
        ("4", "400.0000", "fill"),
    ]
    assert block["weighted_avg_cost"] == "390.0000"
    assert block["freshness"]["state"] == "fresh"
    assert block["freshness"]["fresh_max_minutes"] == kis_lots.FRESH_MAX_MINUTES
    assert block["as_of"] == FRESH.last_reconcile_finished_at.isoformat()
    # #963 caveat, same wording on every evidence view and the order scope.
    for key in ("open_buy_evidence", "open_sell_evidence"):
        assert block[key]["external_orders_verifiable"] is False
        assert block[key]["scope"] == "orders_known_to_auto_trader_only"
    for key in ("same_day_sell_evidence", "same_day_buy_evidence"):
        assert block[key]["scope"] == "orders_known_to_auto_trader_only"
    assert block["sellable_by_ledger"] == "8"
    assert block["market"] == "us"
    assert block["currency"] == "USD"
    assert block["accepted_venues"] == ["AMEX", "NASD", "NYSE"]
    assert block["trading_day_start"] == US_DAY_START.isoformat()
    assert block["order_ledger_sources"] == [
        "review.live_order_ledger",
        "review.kis_live_order_ledger",
    ]
    assert block["diagnostics"]["unrecognized_venue_rows"] == []


def test_a1_every_us_exchange_code_counts() -> None:
    fills = [
        fill(1, "buy", "1", "100", NOW - timedelta(days=4), venue="NASD"),
        fill(2, "buy", "1", "100", NOW - timedelta(days=3), venue="NYSE"),
        fill(3, "buy", "1", "100", NOW - timedelta(days=2), venue="AMEX"),
    ]
    block = us_block(fills, reference="3")
    assert block["ledger_state"] == "known", block["unknown_reasons"]
    assert block["net_quantity"] == "3"
    assert US_VENUES == frozenset({"NASD", "NYSE", "AMEX"})


@pytest.mark.parametrize("reference", ["7", "9", "8.5"])
def test_a1_quantity_mismatch_is_unknown_never_known(reference: str) -> None:
    block = us_block(msft_history(), reference=reference)
    assert block["ledger_state"] == "unknown"
    assert UNKNOWN_QTY_MISMATCH in block["unknown_reasons"]
    assert block["quantity_reconciles"] is False
    assert block["lots"] is None
    assert block["net_quantity"] is None
    assert block["sellable_by_ledger"] is None
    assert block["diagnostics"]["ledger_net_quantity"] == "8"


def test_a1_stale_reconcile_is_unknown() -> None:
    stale = Freshness("stale", NOW - timedelta(hours=3), 180.0)
    block = us_block(msft_history(), freshness=stale)
    assert block["ledger_state"] == "unknown"
    assert block["unknown_reasons"] == [UNKNOWN_LEDGER_STALE]
    assert block["open_sell_evidence"]["blocking"] is True
    assert block["same_day_buy_evidence"]["blocking"] is True


# ------------------------------------------------------- venue spellings


@pytest.mark.parametrize("venue", ["NASDAQ", "NAS", "NYS", "AMS", "krx", "", "SEHK"])
def test_unrecognized_venue_is_unknown_even_when_counting_it_would_reconcile(
    venue: str,
) -> None:
    fills = [
        *msft_history(),
        fill(9, "buy", "2", "405", NOW - timedelta(days=1), venue=venue),
    ]
    # 8 + 2 = 10: counting the mislabelled row would reconcile with the broker.
    block = us_block(fills, reference="10")
    assert block["ledger_state"] == "unknown"
    assert UNKNOWN_UNRECOGNIZED_VENUE in block["unknown_reasons"]
    assert block["lots"] is None
    assert block["sellable_by_ledger"] is None
    [row] = block["diagnostics"]["unrecognized_venue_rows"]
    assert row["broker_order_id"] == "00000009"
    assert row["venue"] == venue


def test_unrecognized_venue_is_unknown_even_when_the_rest_reconciles() -> None:
    fills = [
        *msft_history(),
        fill(9, "buy", "2", "405", NOW - timedelta(days=1), venue="NASDAQ"),
    ]
    # The remaining rows (net 8) match the broker; the off-venue row still
    # makes the block unknown instead of vanishing.
    block = us_block(fills, reference="8")
    assert block["ledger_state"] == "unknown"
    assert block["unknown_reasons"] == [UNKNOWN_UNRECOGNIZED_VENUE]


def test_venue_case_and_whitespace_are_normalized() -> None:
    fills = [fill(1, "buy", "2", "100", NOW - timedelta(days=2), venue=" nyse ")]
    block = us_block(fills, reference="2")
    assert block["ledger_state"] == "known", block["unknown_reasons"]


def test_websocket_row_venue_is_not_checked_and_never_counted() -> None:
    # The websocket tap falls back to venue "krx" when the frame has none; the
    # row is provisional either way and never reaches lots.
    fills = [
        *msft_history(),
        fill(9, "buy", "2", "405", SESSION_FILL, source="websocket", venue="krx"),
    ]
    block = us_block(fills, reference="8")
    assert block["unknown_reasons"] == []
    assert block["diagnostics"]["unrecognized_venue_rows"] == []
    assert [r["broker_order_id"] for r in block["provisional_rows_excluded"]] == [
        "00000009"
    ]


# ------------------------------------------------------------------ A2


def test_a2_phantom_websocket_buy_never_makes_a_lot_or_reconciles() -> None:
    phantom = fill(9, "buy", "2", "400", SESSION_FILL, source="websocket")
    # Broker says 10 = 8 + phantom 2: the phantom must not be what reconciles.
    block = us_block([*msft_history(), phantom], reference="10")
    assert block["ledger_state"] == "unknown"
    assert block["unknown_reasons"] == [
        UNKNOWN_QTY_MISMATCH,
        UNKNOWN_PROVISIONAL_PENDING,
    ]
    assert block["lots"] is None
    assert block["sellable_by_ledger"] is None
    assert block["diagnostics"]["ledger_net_quantity"] == "8"
    assert block["diagnostics"]["provisional_net_quantity"] == "2"


def test_a2_phantom_never_changes_lots_net_or_sellable_but_may_block() -> None:
    clean = us_block(msft_history(), reference="8")
    phantoms = [
        fill(9, "buy", "2", "400", SESSION_FILL, source="websocket"),
        fill(10, "sell", "3", "410", SESSION_FILL, source="websocket"),
    ]
    dirty = us_block([*msft_history(), *phantoms], reference="8")
    assert dirty["ledger_state"] == "known"
    for key in ("lots", "net_quantity", "weighted_avg_cost", "sellable_by_ledger"):
        assert dirty[key] == clean[key], key
    # Only blocking can grow: an accept notice still names an order of today.
    assert (
        BLOCK_SAME_DAY_BUY_FILL_IN_LEDGER
        in dirty["same_day_buy_evidence"]["blocking_reasons"]
    )
    assert (
        BLOCK_SAME_DAY_SELL_FILL in dirty["same_day_sell_evidence"]["blocking_reasons"]
    )
    assert BLOCK_SAME_DAY_FILL in dirty["open_buy_evidence"]["blocking_reasons"]
    assert clean["same_day_buy_evidence"]["blocking"] is False
    assert clean["open_buy_evidence"]["blocking"] is False


def test_a2_phantom_never_raises_sellable_above_the_authoritative_net() -> None:
    # A phantom websocket SELL superseding nothing must not lower the ledger
    # lots either: the projection ignores it entirely.
    phantom_sell = fill(9, "sell", "8", "410", SESSION_FILL, source="websocket")
    block = us_block([*msft_history(), phantom_sell], reference="8")
    assert block["net_quantity"] == "8"
    assert block["sellable_by_ledger"] == "8"
    assert block["same_day_sell_evidence"]["blocking"] is True


# ------------------------------------------------------------------ A3


def test_a3_sell_side_evidence_for_us_with_own_open_sell() -> None:
    orders = [order(1, "sell", "accepted", SESSION_FILL, qty="3")]
    block = us_block(msft_history(), orders=orders)
    open_sell = block["open_sell_evidence"]
    assert open_sell["state"] == "known"
    assert open_sell["blocking_reasons"] == [BLOCK_OWN_OPEN_SELL]
    assert open_sell["own_open_sell_order_quantity"] == "3"
    assert [o["order_no"] for o in open_sell["kis_live_order_ledger_open_sells"]] == [
        "0000000001"
    ]
    assert block["sellable_by_ledger"] == "5"
    assert block["sellable_by_ledger_basis"]["state"] == "known"
    assert block["same_day_buy_evidence"]["state"] == "known"
    assert block["same_day_buy_evidence"]["blocking"] is False


def test_a3_unreadable_order_ledger_fails_closed() -> None:
    block = build_symbol_block(
        symbol="MSFT",
        reference_quantity=Decimal("8"),
        current_price=Decimal("420"),
        fills=msft_history(),
        orders=None,
        freshness=FRESH,
        now=NOW,
        market="us",
    )
    assert block["ledger_state"] == "known"
    for key in ("open_buy_evidence", "open_sell_evidence"):
        assert block[key]["state"] == "unknown"
        assert block[key]["blocking"] is True
    assert block["sellable_by_ledger"] is None


def test_a3_us_unknown_block_fails_closed_like_kr_plus_us_keys() -> None:
    kr = unknown_block("MSFT", UNKNOWN_LOAD_FAILED)
    us = unknown_block("MSFT", UNKNOWN_LOAD_FAILED, market="us")
    assert set(us) - set(kr) == US_ONLY_KEYS
    for key in kr:
        assert us[key] == kr[key], key
    for key in (
        "open_buy_evidence",
        "same_day_sell_evidence",
        "open_sell_evidence",
        "same_day_buy_evidence",
    ):
        assert us[key]["state"] == "unknown"
        assert us[key]["blocking"] is True
    assert us["sellable_by_ledger"] is None
    assert us["market"] == "us"
    assert us["trading_day_start"] is None


def test_a3_us_block_has_every_kr_field() -> None:
    kr = build_symbol_block(
        symbol="196170",
        reference_quantity=Decimal("8"),
        current_price=Decimal("420"),
        fills=msft_history(),
        orders=[],
        freshness=FRESH,
        now=NOW,
    )
    us = us_block(msft_history())
    assert set(us) - set(kr) == US_ONLY_KEYS
    assert set(kr) <= set(us)
    for key in (
        "open_buy_evidence",
        "same_day_sell_evidence",
        "open_sell_evidence",
        "same_day_buy_evidence",
        "sellable_by_ledger_basis",
        "freshness",
    ):
        assert set(us[key]) == set(kr[key]), key
    assert set(us["diagnostics"]) - set(kr["diagnostics"]) == {
        "unrecognized_venue_rows"
    }


# ------------------------------------------------------- US trading date


@pytest.mark.parametrize(
    ("now", "start", "end"),
    [
        # EDT: 10:00 ET Oct 1 -> [Sep 30 20:00, Oct 1 20:00) ET
        (
            NOW,
            datetime(2026, 10, 1, 0, 0, tzinfo=UTC),
            datetime(2026, 10, 2, 0, 0, tzinfo=UTC),
        ),
        # exactly at the rollover instant: next US date
        (
            datetime(2026, 10, 2, 0, 0, tzinfo=UTC),
            datetime(2026, 10, 2, 0, 0, tzinfo=UTC),
            datetime(2026, 10, 3, 0, 0, tzinfo=UTC),
        ),
        # one microsecond before the rollover: still the old date
        (
            datetime(2026, 10, 1, 23, 59, 59, 999999, tzinfo=UTC),
            datetime(2026, 10, 1, 0, 0, tzinfo=UTC),
            datetime(2026, 10, 2, 0, 0, tzinfo=UTC),
        ),
        # EST: 07:00 ET Dec 1 -> [Nov 30 20:00, Dec 1 20:00) EST
        (
            datetime(2026, 12, 1, 12, 0, tzinfo=UTC),
            datetime(2026, 12, 1, 1, 0, tzinfo=UTC),
            datetime(2026, 12, 2, 1, 0, tzinfo=UTC),
        ),
        # DST end (2026-11-01): 25-hour US date
        (
            datetime(2026, 11, 1, 12, 0, tzinfo=UTC),
            datetime(2026, 11, 1, 0, 0, tzinfo=UTC),
            datetime(2026, 11, 2, 1, 0, tzinfo=UTC),
        ),
        # KIS daytime session, 10:30 KST Oct 2 = 21:30 EDT Oct 1 -> US date Oct 2
        (
            datetime(2026, 10, 2, 1, 30, tzinfo=UTC),
            datetime(2026, 10, 2, 0, 0, tzinfo=UTC),
            datetime(2026, 10, 3, 0, 0, tzinfo=UTC),
        ),
    ],
)
def test_us_trading_day_window(now: datetime, start: datetime, end: datetime) -> None:
    assert us_trading_day_window(now) == (start, end)


def test_us_today_is_the_us_trading_date_not_the_kst_day() -> None:
    # 01:30 KST Oct 2 = 12:30 EDT Oct 1: the 09:45 EDT fill is from KST
    # "yesterday" but from the current US trading date, so it blocks.
    after_kst_midnight = datetime(2026, 10, 1, 16, 30, tzinfo=UTC)
    fresh = Freshness("fresh", after_kst_midnight - timedelta(minutes=5), 5.0)
    fills = [*msft_history(), fill(9, "buy", "1", "401", SESSION_FILL)]
    block = us_block(fills, reference="9", now=after_kst_midnight, freshness=fresh)
    assert (
        BLOCK_SAME_DAY_BUY_FILL_IN_LEDGER
        in block["same_day_buy_evidence"]["blocking_reasons"]
    )
    kr_view = build_symbol_block(
        symbol="X",
        reference_quantity=Decimal("9"),
        current_price=None,
        fills=fills,
        orders=[],
        freshness=fresh,
        now=after_kst_midnight,
    )
    assert kr_view["same_day_buy_evidence"]["blocking"] is False


def test_open_order_from_the_current_us_date_blocks_after_kst_midnight() -> None:
    after_kst_midnight = datetime(2026, 10, 1, 16, 30, tzinfo=UTC)
    fresh = Freshness("fresh", after_kst_midnight - timedelta(minutes=5), 5.0)
    orders = [
        order(1, "sell", "accepted", SESSION_FILL, qty="2"),
        order(2, "buy", "pending", SESSION_FILL, qty="1"),
        # previous US date (Sep 30 regular session): presumed dead, reported only
        order(3, "sell", "accepted", SESSION_FILL - timedelta(days=1), qty="9"),
    ]
    block = us_block(
        msft_history(), orders=orders, now=after_kst_midnight, freshness=fresh
    )
    assert block["open_sell_evidence"]["blocking_reasons"] == [BLOCK_OWN_OPEN_SELL]
    assert block["open_sell_evidence"]["own_open_sell_order_quantity"] == "2"
    assert [
        o["order_no"]
        for o in block["open_sell_evidence"]["presumed_dead_prior_day_sells"]
    ] == ["0000000003"]
    assert block["open_buy_evidence"]["blocking"] is True
    assert block["sellable_by_ledger"] == "6"


def test_future_dated_us_row_is_treated_as_today() -> None:
    fills = [*msft_history(), fill(9, "sell", "1", "430", NOW + timedelta(days=2))]
    block = us_block(fills, reference="7")
    assert block["same_day_sell_evidence"]["blocking"] is True


# ------------------------------------------------------------------ A4


def test_a4_kr_blocks_are_byte_identical_to_the_pre_change_golden() -> None:
    assert render(kis_lots) == GOLDEN_PATH.read_text(encoding="utf-8")


def test_a4_kr_blocks_never_carry_us_keys() -> None:
    golden = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    for name, block in golden.items():
        assert not US_ONLY_KEYS & set(block), name

"""A4: ladder approach/touch/fill transitions, dedupe, null-order semantics."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.services.quotes_consumer.ladder import APPROACH_BAND, LadderTracker
from app.services.quotes_consumer.types import OwnFill, QuoteTick, RungAnchor

pytestmark = pytest.mark.unit

T0 = datetime(2026, 9, 30, 13, 0, 0, tzinfo=UTC)


def _rung(**over) -> RungAnchor:
    base = {
        "ledger_name": "toss_live_order_ledger",
        "ledger_id": 501,
        "symbol": "005930",
        "market": "kr",
        "side": "buy",
        "anchor_price": Decimal("70000"),
        "broker_order_id": "T-501",
        "client_order_id": "cli-501",
        "correlation_id": None,
        "received_at": T0 - timedelta(minutes=5),
        "died_at": None,
        "nxt_tradable": None,
    }
    base.update(over)
    return RungAnchor(**base)


def _trade(
    symbol: str, price: str, *, ts: datetime = T0, entry: str = "1-2"
) -> QuoteTick:
    return QuoteTick(
        entry_id=entry,
        symbol=symbol,
        source_symbol=symbol,
        ts=ts,
        session="krx_regular",
        market="kr",
        kind="trade",
        price=Decimal(price),
        bid1=None,
        bid_qty=None,
        ask1=None,
        ask_qty=None,
    )


def _book(symbol: str, bid: str, ask: str, *, ts: datetime = T0) -> QuoteTick:
    return QuoteTick(
        entry_id="1-1",
        symbol=symbol,
        source_symbol=symbol,
        ts=ts,
        session="krx_regular",
        market="kr",
        kind="orderbook",
        price=None,
        bid1=Decimal(bid),
        bid_qty=Decimal("10"),
        ask1=Decimal(ask),
        ask_qty=Decimal("10"),
    )


def test_approach_records_once_within_half_pct() -> None:
    tr = LadderTracker()
    rung = _rung()
    tr.prime([rung])
    # 70399 vs anchor 70000: |Δ| = 0.57% — outside the 0.5% band.
    assert tr.on_trade_tick(_trade("005930", "70399"), [rung]) == []
    # 70350: exactly 0.5% — inside the band, approach fires once.
    rows = tr.on_trade_tick(_trade("005930", "70350"), [rung])
    assert len(rows) == 1
    row = rows[0]
    assert row.event_type == "approach"
    assert row.event_price == Decimal("70350")
    assert row.anchor_price == Decimal("70000")
    assert row.session == "krx_regular"
    # Still inside the band — the near edge already fired.
    assert tr.on_trade_tick(_trade("005930", "70340"), [rung]) == []


def test_approach_re_arms_after_leaving_the_band() -> None:
    tr = LadderTracker()
    rung = _rung()
    tr.prime([rung])
    first = tr.on_trade_tick(_trade("005930", "70350", ts=T0), [rung])
    assert len(first) == 1
    # Leave the band (far), then re-enter — a *new* approach event fires.
    assert (
        tr.on_trade_tick(
            _trade("005930", "71000", ts=T0 + timedelta(seconds=1)), [rung]
        )
        == []
    )
    second = tr.on_trade_tick(
        _trade("005930", "70340", ts=T0 + timedelta(seconds=2)), [rung]
    )
    assert len(second) == 1
    assert second[0].event_type == "approach"
    assert second[0].dedupe_key != first[0].dedupe_key


def test_touch_records_once_on_crossing_trade_price() -> None:
    tr = LadderTracker()
    rung = _rung()
    tr.prime([rung])
    tr.observe_orderbook(_book("005930", "70000", "70050"))
    rows = tr.on_trade_tick(_trade("005930", "70300"), [rung])
    assert [r.event_type for r in rows] == ["approach"]
    rows = tr.on_trade_tick(_trade("005930", "70000"), [rung])
    assert [r.event_type for r in rows] == ["touch"]
    assert rows[0].event_price == Decimal("70000")
    # The book snapshot is captured as detail, never as the event price.
    assert rows[0].detail == {"bid1": "70000", "ask1": "70050"}
    # Touched is terminal — later ticks record nothing more.
    assert tr.on_trade_tick(_trade("005930", "69900"), [rung]) == []


def test_sell_rung_touches_on_trade_at_or_above_anchor() -> None:
    tr = LadderTracker()
    rung = _rung(side="sell")
    tr.prime([rung])
    rows = tr.on_trade_tick(_trade("005930", "69900"), [rung])
    assert [r.event_type for r in rows] == ["approach"]  # 0.14% band
    rows = tr.on_trade_tick(_trade("005930", "70000"), [rung])
    assert [r.event_type for r in rows] == ["touch"]


def test_orderbook_tick_never_emits_events() -> None:
    tr = LadderTracker()
    rung = _rung()
    tr.prime([rung])
    tr.observe_orderbook(_book("005930", "69999", "70000"))
    assert tr.on_trade_tick(_book("005930", "69999", "70000"), [rung]) == []


def test_terminal_fill_requires_broker_evidence_fields() -> None:
    """A4 + contract: never guess a fill — both price and ts are required."""
    tr = LadderTracker()
    rung = _rung()
    tr.prime([rung])
    assert (
        tr.on_terminal(
            rung, status="sent", reconciled_at=T0, avg_fill_price=Decimal("1")
        )
        is None
    )
    assert (
        tr.on_terminal(rung, status="filled", reconciled_at=T0, avg_fill_price=None)
        is None
    )
    assert (
        tr.on_terminal(
            rung,
            status="filled",
            reconciled_at=None,
            avg_fill_price=Decimal("70100"),
        )
        is None
    )


def test_terminal_fill_records_with_evidence_and_null_session() -> None:
    tr = LadderTracker()
    rung = _rung()
    tr.prime([rung])
    row = tr.on_terminal(
        rung,
        status="filled",
        reconciled_at=T0 + timedelta(minutes=3),
        avg_fill_price=Decimal("70050"),
    )
    assert row is not None
    assert row.event_type == "fill"
    assert row.event_price == Decimal("70050")
    assert row.event_ts == T0 + timedelta(minutes=3)
    assert row.died_at == T0 + timedelta(minutes=3)
    assert row.session is None  # ledger carries no session — stays null


def test_own_fill_records_from_ledger_evidence() -> None:
    tr = LadderTracker()
    rung = _rung()
    tr.prime([rung])
    fill = OwnFill(
        ledger_id=77,
        symbol="005930",
        market="kr",
        side="buy",
        price=Decimal("70000"),
        qty=Decimal("3"),
        filled_at=T0,
        broker_order_id="T-501",
        broker="toss",
    )
    row = tr.on_fill(rung, fill)
    assert row is not None
    assert row.event_type == "fill"
    assert row.event_price == Decimal("70000")
    assert row.event_ts == T0
    assert row.fill_ledger_id == 77
    # Redelivery of the same ledger row → no second fill event.
    assert tr.on_fill(rung, fill) is None


def test_events_carry_order_refs_and_unknown_fields_stay_null() -> None:
    tr = LadderTracker()
    rung = _rung(
        broker_order_id=None,
        client_order_id=None,
        correlation_id=None,
        received_at=None,
        nxt_tradable=None,
        died_at=T0,
    )
    tr.prime([rung])
    rows = tr.on_trade_tick(_trade("005930", "70300"), [rung])
    assert len(rows) == 1
    row = rows[0]
    assert row.order_ledger == "toss_live_order_ledger"
    assert row.order_ledger_id == 501
    assert row.broker_order_id is None
    assert row.client_order_id is None
    assert row.received_at is None
    assert row.nxt_tradable is None
    assert row.died_at == T0  # known fields pass through verbatim


def test_each_event_dedupes_per_rung_and_event() -> None:
    tr = LadderTracker()
    rung = _rung()
    tr.prime([rung])
    keys = set()
    ticks = [
        _trade("005930", "70300", ts=T0),
        _trade("005930", "70200", ts=T0 + timedelta(seconds=1)),
        _trade("005930", "70000", ts=T0 + timedelta(seconds=2)),
        _trade("005930", "70300", ts=T0 + timedelta(seconds=3)),
    ]
    for tick in ticks:
        for row in tr.on_trade_tick(tick, [rung]):
            assert row.dedupe_key not in keys
            keys.add(row.dedupe_key)
    # 1 approach + 1 touch only — the far/touched states emit nothing.
    assert len(keys) == 2
    assert APPROACH_BAND == Decimal("0.005")


def test_symbol_mismatch_emits_nothing() -> None:
    tr = LadderTracker()
    rung = _rung()
    tr.prime([rung])
    assert tr.on_trade_tick(_trade("000660", "70000"), [rung]) == []

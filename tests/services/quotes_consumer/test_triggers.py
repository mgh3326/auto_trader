"""A1 threshold edges + A2 shadow suppression for the spike evaluator."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal

import pytest

from app.services.quotes_consumer.triggers import (
    KICK_DAILY_CAP,
    HoldingsView,
    TriggerEvaluator,
)
from app.services.quotes_consumer.types import OwnFill, QuoteTick

pytestmark = pytest.mark.unit

T0 = datetime(2026, 9, 30, 12, 0, 0, tzinfo=UTC)  # 21:00 KST — leaves
# room for three 61-minute-spaced kicks inside one KST day.


def _tick(
    symbol: str,
    price: str,
    *,
    ts: datetime = T0,
    session: str = "krx_regular",
    kind: str = "trade",
    entry: str = "1-0",
) -> QuoteTick:
    market: Literal["kr", "us"] = "kr" if session.startswith(("nxt", "krx")) else "us"
    return QuoteTick(
        entry_id=entry,
        symbol=symbol,
        source_symbol=symbol,
        ts=ts,
        session=session,
        market=market,
        kind=kind,  # type: ignore[arg-type]
        price=Decimal(price) if kind == "trade" else None,
        bid1=None,
        bid_qty=None,
        ask1=None,
        ask_qty=None,
    )


def _holdings(*, held: dict[str, str], core: frozenset[str]) -> HoldingsView:
    return HoldingsView(held=held, core=core)


# ---------------------------------------------------------------------------
# A1: threshold edges — fires exactly at the threshold, not one tick before
# ---------------------------------------------------------------------------
def test_holding_core_fires_exactly_at_5pct() -> None:
    ev = TriggerEvaluator()
    holdings = _holdings(held={"005930": "kr"}, core=frozenset({"005930"}))
    prev = {"005930": Decimal("100000")}

    just_under = ev.evaluate_tick(
        _tick("005930", "104999"), holdings, prev
    )  # +4.999% — one tick before
    assert just_under == []

    at_edge = ev.evaluate_tick(_tick("005930", "105000"), holdings, prev)
    assert len(at_edge) == 1
    row = at_edge[0]
    assert row.outcome == "fired"
    assert row.trigger_type == "holding_spike"
    assert row.reference_price == Decimal("100000")
    assert row.current_price == Decimal("105000")
    assert row.session == "krx_regular"
    assert row.window == "day"
    assert row.detail["is_core"] is True


def test_holding_core_negative_edge() -> None:
    ev = TriggerEvaluator()
    holdings = _holdings(held={"005930": "kr"}, core=frozenset({"005930"}))
    prev = {"005930": Decimal("100000")}

    assert ev.evaluate_tick(_tick("005930", "95001"), holdings, prev) == []
    fired = ev.evaluate_tick(_tick("005930", "95000"), holdings, prev)
    assert len(fired) == 1
    assert fired[0].trigger_type == "holding_spike"


def test_holding_other_needs_7pct() -> None:
    ev = TriggerEvaluator()
    holdings = _holdings(held={"000660": "kr"}, core=frozenset())
    prev = {"000660": Decimal("100000")}

    assert (
        ev.evaluate_tick(_tick("000660", "105000"), holdings, prev) == []
    )  # +5% is not enough for non-core
    assert ev.evaluate_tick(_tick("000660", "106999"), holdings, prev) == []
    fired = ev.evaluate_tick(_tick("000660", "107000"), holdings, prev)
    assert len(fired) == 1
    assert fired[0].detail["is_core"] is False


def test_holding_fires_once_per_breach_episode_and_rearms() -> None:
    ev = TriggerEvaluator()
    holdings = _holdings(held={"005930": "kr"}, core=frozenset({"005930"}))
    prev = {"005930": Decimal("100000")}
    t = T0

    first = ev.evaluate_tick(_tick("005930", "106000", ts=t), holdings, prev)
    assert len(first) == 1
    # Still above threshold — the edge already fired.
    assert (
        ev.evaluate_tick(
            _tick("005930", "106500", ts=t + timedelta(seconds=1)),
            holdings,
            prev,
        )
        == []
    )
    # Re-arm after falling back under, then a new breach fires again.
    assert (
        ev.evaluate_tick(
            _tick("005930", "102000", ts=t + timedelta(seconds=2)),
            holdings,
            prev,
        )
        == []
    )
    second = ev.evaluate_tick(
        _tick("005930", "106000", ts=t + timedelta(seconds=3)), holdings, prev
    )
    assert len(second) == 1
    assert second[0].dedupe_key != first[0].dedupe_key


def test_unheld_symbol_never_fires_holding_trigger() -> None:
    ev = TriggerEvaluator()
    holdings = _holdings(held={}, core=frozenset())
    assert ev.evaluate_tick(_tick("999999", "100"), holdings, {}) == []


def test_vi_proxy_fires_at_3pct_within_60s() -> None:
    ev = TriggerEvaluator()
    holdings = _holdings(held={}, core=frozenset())
    t0 = T0
    # Warm-up tick — no 60s-old reference yet.
    assert ev.evaluate_tick(_tick("005930", "100000", ts=t0), holdings, {}) == []
    # 59s later: still no tick at or before now-60s.
    assert (
        ev.evaluate_tick(
            _tick("005930", "103000", ts=t0 + timedelta(seconds=59)),
            holdings,
            {},
        )
        == []
    )
    # t+60s: reference = the t0 tick. +2.9% does not fire.
    assert (
        ev.evaluate_tick(
            _tick("005930", "102900", ts=t0 + timedelta(seconds=60)),
            holdings,
            {},
        )
        == []
    )
    # t+61s vs reference(t0+1s is absent → t0 tick still the ref): +3.0% fires.
    fired = ev.evaluate_tick(
        _tick("005930", "103000", ts=t0 + timedelta(seconds=61)),
        holdings,
        {},
    )
    assert len(fired) == 1
    row = fired[0]
    assert row.trigger_type == "vi_proxy"
    assert row.window == "60s"
    assert row.reference_price == Decimal("100000")
    assert row.session == "krx_regular"


def test_vi_proxy_ignores_us_ticks() -> None:
    ev = TriggerEvaluator()
    holdings = _holdings(held={}, core=frozenset())
    ev.evaluate_tick(_tick("AAPL", "100", ts=T0, session="us_regular"), holdings, {})
    fired = ev.evaluate_tick(
        _tick("AAPL", "103.5", ts=T0 + timedelta(seconds=61), session="us_regular"),
        holdings,
        {},
    )
    assert fired == []


def test_orderbook_tick_drives_no_price_trigger() -> None:
    ev = TriggerEvaluator()
    holdings = _holdings(held={"005930": "kr"}, core=frozenset({"005930"}))
    book = QuoteTick(
        entry_id="2-1",
        symbol="005930",
        source_symbol="005930",
        ts=T0,
        session="krx_regular",
        market="kr",
        kind="orderbook",
        price=None,
        bid1=Decimal("71400"),
        bid_qty=Decimal("5"),
        ask1=Decimal("71600"),
        ask_qty=Decimal("3"),
    )
    assert ev.evaluate_tick(book, holdings, {"005930": Decimal("70000")}) == []


# ---------------------------------------------------------------------------
# own_fill — one firing per new ledger row
# ---------------------------------------------------------------------------
def test_own_fill_fires_once_per_ledger_row() -> None:
    ev = TriggerEvaluator()
    fills = [
        OwnFill(
            ledger_id=41,
            symbol="005930",
            market="kr",
            side="buy",
            price=Decimal("71500"),
            qty=Decimal("3"),
            filled_at=T0,
            broker_order_id="KIS-1",
            broker="kis",
        )
    ]
    rows = ev.evaluate_own_fills(fills)
    assert len(rows) == 1
    row = rows[0]
    assert row.trigger_type == "own_fill"
    assert row.window == "fill"
    assert row.dedupe_key == "ownfill:41"
    assert row.session is None


# ---------------------------------------------------------------------------
# A2: shadow suppression — cap 2/day + 60-minute cooldown, never a kick call
# ---------------------------------------------------------------------------
def _fire_symbols(ev: TriggerEvaluator, symbols: list[str], ts: datetime):
    holdings = _holdings(held=dict.fromkeys(symbols, "kr"), core=frozenset(symbols))
    prev = {s: Decimal("100000") for s in symbols}
    rows = []
    for s in symbols:
        rows.extend(ev.evaluate_tick(_tick(s, "106000", ts=ts), holdings, prev))
    return rows


def test_same_minute_firings_collapse_via_cooldown() -> None:
    """The gate's real arithmetic: one kick sets a 60-minute cooldown,
    so simultaneous firings cannot all would-kick."""
    ev = TriggerEvaluator()
    rows = _fire_symbols(ev, ["AAA", "BBB"], T0)
    assert [r.would_kick for r in rows] == [True, False]
    assert rows[1].suppress_reason == "cooldown"


def test_would_kick_cap_two_per_day() -> None:
    ev = TriggerEvaluator()
    first = _fire_symbols(ev, ["AAA"], T0)
    second = _fire_symbols(ev, ["BBB"], T0 + timedelta(minutes=61))
    third = _fire_symbols(ev, ["CCC"], T0 + timedelta(minutes=122))
    rows = first + second + third
    assert [r.would_kick for r in rows] == [True, True, False]
    assert rows[2].suppress_reason == "daily_cap"
    assert rows[0].daily_would_kick_count == 1
    assert rows[1].daily_would_kick_count == 2
    assert KICK_DAILY_CAP == 2


def test_would_kick_cooldown_60_minutes() -> None:
    ev = TriggerEvaluator()
    first = _fire_symbols(ev, ["AAA"], T0)
    assert first[0].would_kick is True

    early = _fire_symbols(ev, ["BBB"], T0 + timedelta(minutes=30))
    assert early[0].would_kick is False
    assert early[0].suppress_reason == "cooldown"

    later = _fire_symbols(ev, ["CCC"], T0 + timedelta(minutes=61))
    assert later[0].would_kick is True
    assert later[0].daily_would_kick_count == 2


def test_capped_markets_are_independent() -> None:
    ev = TriggerEvaluator()
    kr = _fire_symbols(ev, ["AAA"], T0)
    us_holdings = _holdings(held={"AAPL": "us"}, core=frozenset())
    us = ev.evaluate_tick(
        _tick("AAPL", "108", ts=T0, session="us_regular"),
        us_holdings,
        {"AAPL": Decimal("100")},
    )
    assert kr[0].would_kick is True
    assert us[0].would_kick is True  # separate market budget


def test_gate_reseed_continues_the_shadow_budget() -> None:
    ev = TriggerEvaluator()
    ev.gate.seed("kr", "2026-09-30", 2, T0)
    rows = _fire_symbols(ev, ["AAA"], T0 + timedelta(minutes=90))
    assert rows[0].would_kick is False
    assert rows[0].suppress_reason == "daily_cap"


def test_evaluator_never_calls_a_session_kick(monkeypatch) -> None:
    """A2: the real trigger path runs under a booby-trapped kick seam.

    If any firing secretly constructed FillHandoffRunner or called kick_task
    the sentinel records it and the assertion below fails. The evaluator has
    no kick injection point, so a mutant that adds one must trip this.
    """
    called: list[str] = []

    class _ExplodingKick:
        def __init__(self, *a, **k):
            called.append("FillHandoffRunner")

    async def _exploding_task(*a, **k):
        called.append("kick_task")
        raise AssertionError("session-kick path invoked")

    monkeypatch.setattr(
        "app.services.fill_event_handoff.service.FillHandoffRunner",
        _ExplodingKick,
    )
    monkeypatch.setattr("app.services.ops_task_kick.service.kick_task", _exploding_task)
    ev = TriggerEvaluator()
    rows = _fire_symbols(ev, ["AAA", "BBB"], T0)
    assert len(rows) == 2
    assert called == []


# ---------------------------------------------------------------------------
# not_evaluable records
# ---------------------------------------------------------------------------
def test_index_trigger_is_not_evaluable_once_per_day() -> None:
    ev = TriggerEvaluator()
    row = ev.index_status(T0)
    assert row is not None
    assert row.outcome == "not_evaluable"
    assert row.trigger_type == "index_spike"
    assert row.not_evaluable_reason == "index_level_unavailable"
    assert row.would_kick is False
    # Same KST day → deduped.
    assert ev.index_status(T0 + timedelta(hours=1)) is None


def test_held_symbol_without_prev_close_is_not_evaluable() -> None:
    ev = TriggerEvaluator()
    holdings = _holdings(held={"005930": "kr"}, core=frozenset({"005930"}))
    rows = ev.evaluate_tick(_tick("005930", "106000"), holdings, {})
    assert len(rows) == 1
    assert rows[0].outcome == "not_evaluable"
    assert rows[0].not_evaluable_reason == "previous_close_unavailable"
    # Once per symbol/day.
    assert ev.evaluate_tick(_tick("005930", "107000"), holdings, {}) == []

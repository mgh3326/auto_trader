"""#1112 — the ``expired[inference]`` rule, one condition at a time (AC A2/A3).

Every test starts from ``_facts()`` — a KIS regular-session DAY buy rung that
satisfies ALL conditions — and breaks exactly one thing. The baseline must be
eligible; every single-break variant must keep blocking and name the broken
condition (and only that condition when the break is isolated).
"""

from __future__ import annotations

import dataclasses
import datetime
from decimal import Decimal

import pytest

from app.models.rung_reason_vocabulary import RUNG_VOID_REASON_CANCELLED_OR_EXPIRED
from app.services.order_proposals import kis_leftover_inference as rule
from app.services.order_proposals.kis_leftover_inference import (
    EXPIRED_INFERENCE_CAVEAT,
    EXPIRED_INFERENCE_VOID_REASON,
    FillFacts,
    KisOrderLedgerFacts,
    LeftoverRungFacts,
    ReconcileRunFacts,
    classify_leftover_rung,
    is_expired_inference_reason,
)
from app.services.order_proposals.rung_reason import classify_rung_void_reason

pytestmark = pytest.mark.unit

KST = datetime.timezone(datetime.timedelta(hours=9))
DAY = datetime.date(2026, 9, 29)


def kst(day: datetime.date, hh: int, mm: int = 0, ss: int = 0) -> datetime.datetime:
    return datetime.datetime(day.year, day.month, day.day, hh, mm, ss, tzinfo=KST)


OPEN = kst(DAY, 9)
CLOSE = kst(DAY, 15, 30)
ACCEPT = kst(DAY, 10, 0, 0)
# Conservative ROB-671 expiry for a regular-session SOR buy: NXT close 20:00.
DEADLINE = kst(DAY, 20)
NOW = kst(DAY + datetime.timedelta(days=1), 7)
ORDER_NO = "0012345678"


def _row(**overrides: object) -> KisOrderLedgerFacts:
    base = KisOrderLedgerFacts(
        ledger_id=41,
        order_no=ORDER_NO,
        status="accepted",
        order_type="limit",
        side="buy",
        symbol="005880",
        trade_date=kst(DAY, 10, 0, 1),
        order_time="100000",
        broker_exchange=None,
        filled_qty=None,
    )
    return dataclasses.replace(base, **overrides)  # type: ignore[arg-type]


def _seed_fill(**overrides: object) -> FillFacts:
    base = FillFacts(
        broker_order_id="SEED-005880",
        side="buy",
        quantity=Decimal("10"),
        filled_at=kst(DAY - datetime.timedelta(days=20), 10),
        source="manual_import",
    )
    return dataclasses.replace(base, **overrides)  # type: ignore[arg-type]


def _run(**overrides: object) -> ReconcileRunFacts:
    base = ReconcileRunFacts(
        window_start=kst(DAY - datetime.timedelta(days=1), 20, 30),
        window_end=kst(DAY, 20, 30),
        finished_at=kst(DAY, 20, 31),
    )
    return dataclasses.replace(base, **overrides)  # type: ignore[arg-type]


def _facts(**overrides: object) -> LeftoverRungFacts:
    base = LeftoverRungFacts(
        proposal_id="00000000-0000-0000-0000-000000001112",
        rung_id=7,
        rung_index=0,
        rung_state="resting",
        side="buy",
        symbol="005880",
        market="equity_kr",
        account_mode="kis_live",
        group_order_type="limit",
        broker_order_id=ORDER_NO,
        rung_filled_qty=None,
        order_ledger_rows=(_row(),),
        order_ledger_conflict=None,
        session_bounds=(OPEN, CLOSE),
        symbol_fills=(_seed_fill(),),
        reconcile_runs=(_run(),),
        unsettled_regular_buy_downgrade=False,
    )
    return dataclasses.replace(base, **overrides)  # type: ignore[arg-type]


def _verdict(facts: LeftoverRungFacts, now: datetime.datetime = NOW):
    return classify_leftover_rung(facts, now=now)


def test_all_conditions_hold_is_eligible_and_carries_the_caveat():
    decision = _verdict(_facts())
    assert decision.eligible is True
    assert decision.failed_conditions == ()
    assert decision.accept_at == ACCEPT
    assert decision.deadline == DEADLINE
    assert decision.deadline_reason == "regular_buy_conservative_20_00"
    row = decision.as_row()
    assert row["caveat"] == EXPIRED_INFERENCE_CAVEAT
    assert row["rule"] == rule.INFERENCE_RULE_ID
    assert row["order_ledger_id"] == 41


# --- A2: each condition fails alone -----------------------------------------

ISOLATED_BREAKS: dict[str, dict[str, object]] = {
    # KIS: a Toss rung is never inferred.
    rule.COND_KIS_LIVE_RESTING_BUY: {"account_mode": "toss_live"},
    # Owned, open order-ledger row: the row is already broker-terminal.
    rule.COND_ORDER_LEDGER_ROW: {"order_ledger_rows": (_row(status="cancelled"),)},
    # DAY: a non-DAY order type.
    rule.COND_DAY_ORDER: {"order_ledger_rows": (_row(order_type="ioc_limit"),)},
    # Regular session: broker-reported NXT venue.
    rule.COND_REGULAR_SESSION: {"order_ledger_rows": (_row(broker_exchange="NXT"),)},
    # 15:30/close passed: evaluated before the conservative deadline.
    rule.COND_DAY_CLOSE_PASSED: {},
    # Coverage: the only reconcile run finished before the deadline.
    rule.COND_LEDGER_COVERAGE: {"reconcile_runs": (_run(finished_at=kst(DAY, 19)),)},
    # No fill: the rung already booked a partial.
    rule.COND_NO_FILL: {"rung_filled_qty": Decimal("1")},
    # Holding unchanged: another order's sell filled after accept.
    rule.COND_HOLDING_UNCHANGED: {
        "symbol_fills": (
            _seed_fill(),
            _seed_fill(
                broker_order_id="0099999999",
                side="sell",
                quantity=Decimal("2"),
                filled_at=kst(DAY, 11),
                source="reconciler",
            ),
        )
    },
}


def test_every_condition_has_an_isolated_break():
    assert set(ISOLATED_BREAKS) == {name for name, _ in rule.CONDITIONS}


@pytest.mark.parametrize("condition", sorted(ISOLATED_BREAKS))
def test_only_that_condition_fails_and_the_row_still_blocks(condition: str):
    now = NOW
    if condition == rule.COND_DAY_CLOSE_PASSED:
        now = kst(DAY, 16, 30)  # the 16:30 sweep: SOR buy may live to 20:00
    decision = _verdict(_facts(**ISOLATED_BREAKS[condition]), now)
    assert decision.eligible is False
    assert decision.failed_conditions == (condition,)
    assert decision.as_row()["caveat"] is None


# --- fail-closed shapes the brief names explicitly -------------------------


@pytest.mark.parametrize(
    ("label", "overrides", "expected"),
    [
        ("non_day_group", {"group_order_type": "stop_limit"}, rule.COND_DAY_ORDER),
        (
            "missing_order_type",
            {"order_ledger_rows": (_row(order_type=None),)},
            rule.COND_DAY_ORDER,
        ),
        (
            "nxt_after_hours_accept",
            {
                "order_ledger_rows": (
                    _row(order_time="170000", trade_date=kst(DAY, 17, 0, 1)),
                )
            },
            rule.COND_REGULAR_SESSION,
        ),
        (
            "premarket_accept",
            {
                "order_ledger_rows": (
                    _row(order_time="082000", trade_date=kst(DAY, 8, 20, 1)),
                )
            },
            rule.COND_REGULAR_SESSION,
        ),
        (
            "gap_15_30_to_16_00_accept",
            {
                "order_ledger_rows": (
                    _row(order_time="153500", trade_date=kst(DAY, 15, 35, 1)),
                )
            },
            rule.COND_REGULAR_SESSION,
        ),
        (
            "sent_inside_but_recorded_after_close",
            {
                "order_ledger_rows": (
                    _row(order_time="152959", trade_date=kst(DAY, 15, 30, 1)),
                )
            },
            rule.COND_REGULAR_SESSION,
        ),
        (
            "unknown_venue",
            {"order_ledger_rows": (_row(broker_exchange="XYZ"),)},
            rule.COND_REGULAR_SESSION,
        ),
        (
            "partial_status",
            {"order_ledger_rows": (_row(status="partial"),)},
            rule.COND_ORDER_LEDGER_ROW,
        ),
        (
            "ledger_partial_qty",
            {"order_ledger_rows": (_row(filled_qty=Decimal("1")),)},
            rule.COND_NO_FILL,
        ),
        (
            "fill_of_this_order_in_ledger",
            {
                "symbol_fills": (
                    _seed_fill(),
                    _seed_fill(
                        broker_order_id="12345678",  # leading zeros normalised
                        filled_at=kst(DAY, 13),
                        source="websocket",
                        quantity=Decimal("1"),
                    ),
                )
            },
            rule.COND_NO_FILL,
        ),
        ("fills_unreadable", {"symbol_fills": None}, rule.COND_NO_FILL),
        (
            "no_ledger_rows_for_symbol",
            {"symbol_fills": ()},
            rule.COND_HOLDING_UNCHANGED,
        ),
        ("runs_unreadable", {"reconcile_runs": None}, rule.COND_LEDGER_COVERAGE),
        ("no_runs", {"reconcile_runs": ()}, rule.COND_LEDGER_COVERAGE),
        (
            "run_window_starts_after_accept",
            {"reconcile_runs": (_run(window_start=kst(DAY, 10, 30)),)},
            rule.COND_LEDGER_COVERAGE,
        ),
        (
            "run_window_ends_before_deadline",
            {"reconcile_runs": (_run(window_end=kst(DAY, 19, 59)),)},
            rule.COND_LEDGER_COVERAGE,
        ),
        (
            "holiday_or_unknown_calendar",
            {"session_bounds": None},
            rule.COND_REGULAR_SESSION,
        ),
        (
            "order_ledger_unreadable",
            {"order_ledger_rows": None},
            rule.COND_ORDER_LEDGER_ROW,
        ),
        ("no_order_ledger_row", {"order_ledger_rows": ()}, rule.COND_ORDER_LEDGER_ROW),
        (
            "two_order_ledger_rows",
            {"order_ledger_rows": (_row(), _row(ledger_id=42))},
            rule.COND_ORDER_LEDGER_ROW,
        ),
        (
            "ownership_conflict",
            {"order_ledger_conflict": "broker_id_duplicate"},
            rule.COND_ORDER_LEDGER_ROW,
        ),
        (
            "order_no_mismatch",
            {"order_ledger_rows": (_row(order_no="0000000001"),)},
            rule.COND_ORDER_LEDGER_ROW,
        ),
        (
            "sell_ledger_row",
            {"order_ledger_rows": (_row(side="sell"),)},
            rule.COND_ORDER_LEDGER_ROW,
        ),
        ("acked_not_resting", {"rung_state": "acked"}, rule.COND_KIS_LIVE_RESTING_BUY),
        (
            "partially_filled_rung",
            {"rung_state": "partially_filled"},
            rule.COND_KIS_LIVE_RESTING_BUY,
        ),
        (
            "unverified_rung",
            {"rung_state": "unverified"},
            rule.COND_KIS_LIVE_RESTING_BUY,
        ),
        ("sell_rung", {"side": "sell"}, rule.COND_KIS_LIVE_RESTING_BUY),
        ("kis_mock", {"account_mode": "kis_mock"}, rule.COND_KIS_LIVE_RESTING_BUY),
        ("us_market", {"market": "equity_us"}, rule.COND_KIS_LIVE_RESTING_BUY),
        (
            "no_broker_order_id",
            {"broker_order_id": None},
            rule.COND_KIS_LIVE_RESTING_BUY,
        ),
    ],
)
def test_fail_closed_shapes_keep_blocking(label, overrides, expected):
    decision = _verdict(_facts(**overrides))
    assert decision.eligible is False, label
    assert expected in decision.failed_conditions, (label, decision.failed_conditions)


@pytest.mark.parametrize("order_time", [None, "", "10", "1000000", "2561xx", "256100"])
def test_unknown_accept_time_is_unknown_session(order_time):
    decision = _verdict(_facts(order_ledger_rows=(_row(order_time=order_time),)))
    assert decision.eligible is False
    assert decision.accept_at is None
    assert rule.COND_REGULAR_SESSION in decision.failed_conditions
    assert rule.COND_DAY_CLOSE_PASSED in decision.failed_conditions


def test_missing_trade_date_is_unknown_session():
    decision = _verdict(_facts(order_ledger_rows=(_row(trade_date=None),)))
    assert decision.eligible is False
    assert rule.COND_REGULAR_SESSION in decision.failed_conditions


def test_quantity_change_that_nets_to_zero_still_blocks():
    fills = (
        _seed_fill(),
        _seed_fill(broker_order_id="A1", filled_at=kst(DAY, 11), quantity=Decimal("1")),
        _seed_fill(
            broker_order_id="A2",
            side="sell",
            filled_at=kst(DAY, 12),
            quantity=Decimal("1"),
        ),
    )
    decision = _verdict(_facts(symbol_fills=fills))
    assert decision.failed_conditions == (rule.COND_HOLDING_UNCHANGED,)


def test_fill_exactly_at_accept_instant_counts_as_after_submit():
    fills = (_seed_fill(), _seed_fill(broker_order_id="A1", filled_at=ACCEPT))
    decision = _verdict(_facts(symbol_fills=fills))
    assert rule.COND_HOLDING_UNCHANGED in decision.failed_conditions


# --- the deadline: timezone and 15:30 edges -------------------------------


def test_deadline_is_strict_at_the_boundary():
    at = _verdict(_facts(), DEADLINE)
    assert at.failed_conditions == (rule.COND_DAY_CLOSE_PASSED,)
    after = _verdict(_facts(), DEADLINE + datetime.timedelta(microseconds=1))
    assert after.eligible is True


def test_utc_now_is_compared_as_an_instant_not_a_wall_clock():
    # 11:00 UTC == 20:00 KST: still AT the deadline, so it must block.
    utc_now = datetime.datetime(2026, 9, 29, 11, 0, tzinfo=datetime.UTC)
    assert _verdict(_facts(), utc_now).failed_conditions == (
        rule.COND_DAY_CLOSE_PASSED,
    )
    later = datetime.datetime(2026, 9, 29, 11, 0, 1, tzinfo=datetime.UTC)
    assert _verdict(_facts(), later).eligible is True


def test_naive_now_is_refused():
    with pytest.raises(ValueError):
        _verdict(_facts(), datetime.datetime(2026, 9, 30, 7))


def test_sor_regular_buy_is_not_inferred_at_the_16_30_sweep():
    decision = _verdict(_facts(), kst(DAY, 16, 30))
    assert decision.failed_conditions == (rule.COND_DAY_CLOSE_PASSED,)
    assert decision.deadline == DEADLINE


def test_krx_venue_with_downgrade_flag_uses_15_30_but_never_earlier():
    facts = _facts(
        order_ledger_rows=(_row(broker_exchange="KRX"),),
        reconcile_runs=(_run(window_end=kst(DAY, 16), finished_at=kst(DAY, 16, 1)),),
    )
    decision = _verdict(facts, kst(DAY, 16, 30))
    assert decision.deadline == CLOSE
    assert decision.eligible is True
    at_close = _verdict(facts, CLOSE)
    assert rule.COND_DAY_CLOSE_PASSED in at_close.failed_conditions
    before = _verdict(facts, kst(DAY, 15, 29, 59))
    assert rule.COND_DAY_CLOSE_PASSED in before.failed_conditions


def test_downgrade_flag_moves_sor_buy_deadline_to_15_30():
    facts = _facts(
        unsettled_regular_buy_downgrade=True,
        reconcile_runs=(_run(window_end=kst(DAY, 16), finished_at=kst(DAY, 16, 1)),),
    )
    decision = _verdict(facts, kst(DAY, 16, 30))
    assert decision.deadline == CLOSE
    assert decision.deadline_reason in {
        "submit_day_15_30_kst",
        "calendar_session_close",
        "regular_buy_unsettled_15_30",
    }
    assert decision.eligible is True


def test_late_close_day_pushes_the_deadline_past_15_30():
    late_close = kst(DAY, 16, 30)
    facts = _facts(
        session_bounds=(kst(DAY, 10), late_close),
        order_ledger_rows=(
            _row(
                broker_exchange="KRX",
                order_time="160000",
                trade_date=kst(DAY, 16, 0, 1),
            ),
        ),
        reconcile_runs=(_run(window_end=kst(DAY, 17), finished_at=kst(DAY, 17, 1)),),
    )
    # 16:00 is inside the calendar session but the ROB-671 window says "off":
    # an unconfirmable regular accept is not inferred.
    assert (
        rule.COND_REGULAR_SESSION in _verdict(facts, kst(DAY, 17, 30)).failed_conditions
    )
    in_window = dataclasses.replace(
        facts,
        order_ledger_rows=(
            _row(
                broker_exchange="KRX",
                order_time="110000",
                trade_date=kst(DAY, 11, 0, 1),
            ),
        ),
    )
    decision = _verdict(in_window, kst(DAY, 16, 20))
    assert decision.deadline == late_close
    assert rule.COND_DAY_CLOSE_PASSED in decision.failed_conditions
    assert _verdict(in_window, kst(DAY, 17, 30)).eligible is True


def test_late_open_day_accept_before_open_is_not_regular():
    facts = _facts(
        session_bounds=(kst(DAY, 10), CLOSE),
        order_ledger_rows=(_row(order_time="093000", trade_date=kst(DAY, 9, 30, 1)),),
    )
    assert rule.COND_REGULAR_SESSION in _verdict(facts).failed_conditions


# --- A3: the marker ---------------------------------------------------------


def test_marker_is_distinct_from_broker_confirmed_expiry_but_same_group():
    assert is_expired_inference_reason(EXPIRED_INFERENCE_VOID_REASON)
    for broker_reason in (
        None,
        "expired",
        "order_expired",
        "expired_valid_until_night_sweep",
    ):
        assert not is_expired_inference_reason(broker_reason)
    assert (
        classify_rung_void_reason(EXPIRED_INFERENCE_VOID_REASON)
        == RUNG_VOID_REASON_CANCELLED_OR_EXPIRED
    )

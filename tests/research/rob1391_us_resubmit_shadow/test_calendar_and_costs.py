"""Session arithmetic (D0/D1-D3/D+20, holidays, early closes) and costs."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from research.rob1391_us_resubmit_shadow.costs import (
    CostModel,
    FeeSchedule,
    FeeScheduleError,
)
from research.rob1391_us_resubmit_shadow.us_calendar import ET
from tests.research.rob1391_us_resubmit_shadow.helpers import CAL, FEE_SCHEDULE, SEAL_AT

pytestmark = pytest.mark.unit


def test_window_is_twenty_sessions_skipping_thanksgiving():
    start = CAL.first_session_opening_after(SEAL_AT)
    window = CAL.window(start, 20)
    assert start == date(2025, 11, 14)
    assert len(window) == 20
    assert date(2025, 11, 27) not in window  # holiday
    assert window[-1] == date(2025, 12, 12)


def test_seal_during_session_starts_next_session():
    during = datetime(2025, 11, 14, 15, 0, tzinfo=UTC)  # 10:00 ET
    assert CAL.first_session_opening_after(during) == date(2025, 11, 17)


def test_resubmit_and_eval_offsets_skip_holidays():
    d0 = date(2025, 11, 25)
    assert [CAL.offset(d0, k) for k in (1, 2, 3)] == [
        date(2025, 11, 26),
        date(2025, 11, 28),
        date(2025, 12, 1),
    ]
    assert CAL.offset(d0, 20) == date(2025, 12, 24)  # early close, before Christmas


def test_early_close_recorded_in_et_and_utc():
    s = CAL.session(date(2025, 11, 28))
    assert s.early_close and s.close_et.hour == 13
    assert s.close_utc == datetime(2025, 11, 28, 18, 0, tzinfo=UTC)
    assert s.as_record()["close_et"].endswith("-05:00")


def test_working_session_of_overnight_and_intraday_orders():
    assert CAL.working_session(datetime(2025, 11, 17, 3, 0, tzinfo=UTC)) == date(
        2025, 11, 17
    )
    assert CAL.working_session(datetime(2025, 11, 17, 20, 59, tzinfo=UTC)) == date(
        2025, 11, 17
    )
    assert CAL.working_session(datetime(2025, 11, 17, 21, 0, tzinfo=UTC)) == date(
        2025, 11, 18
    )
    # Wednesday before Thanksgiving, after the close -> Friday (early close)
    assert CAL.working_session(datetime(2025, 11, 26, 22, 0, tzinfo=UTC)) == date(
        2025, 11, 28
    )


def test_gap_counts_sessions_not_days():
    assert CAL.gap(date(2025, 11, 21), date(2025, 12, 1)) == 5  # Thanksgiving skipped


def test_dst_is_respected():
    assert CAL.session(date(2025, 11, 3)).open_utc.hour == 14  # after DST end
    assert CAL.session(date(2025, 10, 31)).open_utc.hour == 13
    assert CAL.session(date(2025, 10, 31)).open_et.tzinfo == ET


def schedule():
    return FeeSchedule.parse(json.loads(json.dumps(FEE_SCHEDULE)))


def test_fee_schedule_is_closed_and_validated():
    bad = dict(FEE_SCHEDULE)
    bad["extra"] = "1"
    with pytest.raises(FeeScheduleError):
        FeeSchedule.parse(bad)
    bad = json.loads(json.dumps(FEE_SCHEDULE))
    bad["by_source"]["live_order_ledger"]["commission_rate"] = (
        0.0025  # float, not string
    )
    with pytest.raises(FeeScheduleError):
        FeeSchedule.parse(bad)
    bad = json.loads(json.dumps(FEE_SCHEDULE))
    bad["fx"] = "krw_conversion"
    with pytest.raises(FeeScheduleError):
        FeeSchedule.parse(bad)


def test_costs_buy_and_sell():
    m = CostModel(
        schedule=schedule(), source="toss_live_order_ledger", slippage_bp=Decimal("10")
    )
    assert m.buy_cost(Decimal("1000")) == Decimal("1.000") + Decimal("1.000")
    sell = m.sell_cost(Decimal("1000"), 10)
    assert sell == Decimal("1.000") + Decimal("0.0278") + Decimal("0.00166") + Decimal(
        "1.000"
    )


def test_missing_fee_schedule_is_slippage_only_and_flagged():
    m = CostModel(
        schedule=None, source="toss_live_order_ledger", slippage_bp=Decimal("10")
    )
    assert not m.fees_known
    assert m.buy_cost(Decimal("1000")) == Decimal("1.000")


def test_affordable_shares_includes_costs_and_floors():
    m = CostModel(
        schedule=schedule(), source="toss_live_order_ledger", slippage_bp=Decimal("10")
    )
    # 3 x 100 = 300 + 0.6 cost > 300.5 -> only 2 affordable
    assert (
        m.affordable_shares(
            price=Decimal("100"), remaining_budget=Decimal("300.5"), include_cost=True
        )
        == 2
    )
    assert (
        m.affordable_shares(
            price=Decimal("100"), remaining_budget=Decimal("300.6"), include_cost=True
        )
        == 3
    )
    assert (
        m.affordable_shares(
            price=Decimal("100"), remaining_budget=Decimal("300.5"), include_cost=False
        )
        == 3
    )
    assert (
        m.affordable_shares(
            price=Decimal("100"), remaining_budget=Decimal("99.99"), include_cost=False
        )
        == 0
    )

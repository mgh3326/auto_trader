"""Acceptance 1: the contract's bracket reproduces the ROB-1383 Q2 bracket.

ROB-1383 Q2 (results/q2.md): on the five real filled LIMIT BUYs, the optimistic
assumption reproduced 5/5 fills and the conservative one 0/5, because every
real fill printed exactly at the limit (a touch, never a trade-through).

The real orders are private (the repo is public; ROB-1383 never commits row
data), so this fixture is a deterministic mirror of that comparison group: two
KR and three US filled BUY limits, each filled at its limit inside the bar
that touched it, covering the three shapes the legacy code distinguishes --
order and fill inside one hour (conservative window empty), a whole hour
inside the life whose low equals the limit (window present, no trade-through),
and an order resting from before the open / across two sessions.

The legacy ``q2_q3_orders.run`` runs unchanged on the fixture; the contract's
``OptimisticTouch`` / ``ConservativeThrough`` must agree with it order by order
and must fill at the real fill price.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from research.eval_contract.contract import Side
from research.eval_contract.fill_models import (
    ConservativeThrough,
    FillContext,
    OptimisticTouch,
)
from research.rob1383_buy_drought import q2_q3_orders
from research.rob1383_buy_drought.core import tick_size
from research.rob1383_buy_drought.inputs import TossOrder

KST = ZoneInfo("Asia/Seoul")
ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")
SESSIONS = 25  # leaves day+5 / day+20 closes for the legacy return columns


def _ts(day: date, hh: int, mm: int, tz: ZoneInfo) -> pd.Timestamp:
    return pd.Timestamp(datetime(day.year, day.month, day.day, hh, mm, tzinfo=tz))


def _weekdays(start: date, n: int) -> list[date]:
    days, d = [], start
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


# (start hour, start minute) of each hourly bar and the session window.
KR_HOURS = [(9, 0), (10, 0), (11, 0), (12, 0), (13, 0), (14, 0), (15, 0)]
US_HOURS = [(9, 30), (10, 30), (11, 30), (12, 30), (13, 30), (14, 30), (15, 30)]
SESSION = {"kr": ((9, 0), (15, 30), KST), "us": ((9, 30), (16, 0), ET)}


def _build(
    market: str, days: list[date], base: float, lows: dict[tuple[date, int], float]
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Flat bars at ``base`` with chosen hourly lows on given (day, hour idx)."""

    (oh, om), (ch, cm), tz = SESSION[market]
    starts = KR_HOURS if market == "kr" else US_HOURS
    daily, hourly = [], []
    for day in days:
        open_ts, close_ts = _ts(day, oh, om, tz), _ts(day, ch, cm, tz)
        day_lows = []
        for i, (h, m) in enumerate(starts):
            start = _ts(day, h, m, tz)
            end = min(start + pd.Timedelta(hours=1), close_ts)
            low = lows.get((day, i), base * 0.995)
            day_lows.append(low)
            hourly.append(
                {
                    "session_date": day,
                    "start_ts": start.tz_convert(UTC),
                    "end_ts": end.tz_convert(UTC),
                    "open": base,
                    "high": base * 1.01,
                    "low": low,
                    "close": base,
                    "volume": 1000.0,
                }
            )
        daily.append(
            {
                "session_date": day,
                "open_ts": open_ts.tz_convert(UTC),
                "close_ts": close_ts.tz_convert(UTC),
                "open": base,
                "high": base * 1.01,
                "low": min(day_lows),
                "close": base,
                "volume": 7000.0,
            }
        )
    return pd.DataFrame(daily), pd.DataFrame(hourly)


@dataclass
class FakeStore:
    """Duck-typed ``PriceStore``: bars, hourly, missing, split_events."""

    daily: dict[tuple[str, str], pd.DataFrame] = field(default_factory=dict)
    hours: dict[tuple[str, str], pd.DataFrame] = field(default_factory=dict)
    missing: dict[tuple[str, str], str] = field(default_factory=dict)
    split_events: dict[tuple[str, str], list[str]] = field(default_factory=dict)

    def bars(self, market: str, symbol: str) -> pd.DataFrame | None:
        return self.daily.get((market, symbol))

    def hourly(self, market: str, symbol: str) -> pd.DataFrame | None:
        return self.hours.get((market, symbol))


def _order(
    oid: str, market: str, symbol: str, limit: float, at: datetime, filled: datetime
) -> TossOrder:
    return TossOrder(
        order_id=oid,
        market=market,
        symbol=symbol,
        side="BUY",
        status="FILLED",
        order_type="LIMIT",
        price=limit,
        quantity=3.0,
        ordered_at=at,
        canceled_at=None,
        filled_qty=3.0,
        avg_fill=limit,  # every real Q2 fill printed at the limit
        filled_at=filled,
    )


def _fixture() -> tuple[list[TossOrder], FakeStore]:
    kr_days = _weekdays(date(2026, 9, 1), SESSIONS)
    us_days = _weekdays(date(2026, 9, 1), SESSIONS)
    d0, d1 = kr_days[2], kr_days[3]
    u0, u1 = us_days[2], us_days[3]
    store = FakeStore()
    orders = []

    # KR-1: placed 10:20, filled 10:40 inside the 10:00 hour whose low = limit.
    store.daily[("kr", "KRA")], store.hours[("kr", "KRA")] = _build(
        "kr", kr_days, 58_500.0, {(d0, 1): 57_700.0}
    )
    orders.append(
        _order(
            "kr-1",
            "kr",
            "KRA",
            57_700.0,
            _ts(d0, 10, 20, KST).to_pydatetime(),
            _ts(d0, 10, 40, KST).to_pydatetime(),
        )
    )
    # KR-2: placed 09:05, filled 11:30; the 10:00 hour lies wholly inside the
    # life and its low equals the limit -- a touch, not a trade-through.
    store.daily[("kr", "KRB")], store.hours[("kr", "KRB")] = _build(
        "kr", kr_days, 9_600.0, {(d1, 0): 9_500.0, (d1, 1): 9_450.0, (d1, 2): 9_470.0}
    )
    orders.append(
        _order(
            "kr-2",
            "kr",
            "KRB",
            9_450.0,
            _ts(d1, 9, 5, KST).to_pydatetime(),
            _ts(d1, 11, 30, KST).to_pydatetime(),
        )
    )
    # US-1: placed 10:45 ET, filled 11:10 ET inside the 10:30 hour.
    store.daily[("us", "USA")], store.hours[("us", "USA")] = _build(
        "us", us_days, 67.10, {(u0, 1): 66.38}
    )
    orders.append(
        _order(
            "us-1",
            "us",
            "USA",
            66.38,
            _ts(u0, 10, 45, ET).to_pydatetime(),
            _ts(u0, 11, 10, ET).to_pydatetime(),
        )
    )
    # US-2: placed 08:00 ET before the open, filled 10:00 ET in the first hour.
    store.daily[("us", "USB")], store.hours[("us", "USB")] = _build(
        "us", us_days, 120.40, {(u1, 0): 119.85}
    )
    orders.append(
        _order(
            "us-2",
            "us",
            "USB",
            119.85,
            _ts(u1, 8, 0, ET).to_pydatetime(),
            _ts(u1, 10, 0, ET).to_pydatetime(),
        )
    )
    # US-3: placed 15:00 ET on one session, filled 09:50 ET the next; the last
    # whole hour of day one stays above the limit.
    store.daily[("us", "USC")], store.hours[("us", "USC")] = _build(
        "us",
        us_days,
        31.20,
        {(u0, 5): 31.05, (u0, 6): 30.95, (u1, 0): 30.90},
    )
    orders.append(
        _order(
            "us-3",
            "us",
            "USC",
            30.90,
            _ts(u0, 15, 0, ET).to_pydatetime(),
            _ts(u1, 9, 50, ET).to_pydatetime(),
        )
    )
    return orders, store


def _dec(value: float | None) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _contexts(orders: list[TossOrder], store: FakeStore) -> dict[str, FillContext]:
    """Contract contexts built from the legacy lifetime-low windows."""

    out = {}
    for o in orders:
        bars, hourly = store.bars(o.market, o.symbol), store.hourly(o.market, o.symbol)
        assert bars is not None
        pos = q2_q3_orders.order_session_positions(bars, o)
        low_in, _ = q2_q3_orders.lifetime_low(bars, hourly, pos, o, inclusive=False)
        low_ov, _ = q2_q3_orders.lifetime_low(bars, hourly, pos, o, inclusive=True)
        assert o.price is not None
        out[o.order_id] = FillContext(
            side=Side.BUY,
            limit=Decimal(str(o.price)),
            quantity=Decimal(str(o.quantity)),
            tick=Decimal(str(tick_size(o.market, o.price))),
            optimistic_extreme=_dec(low_ov),
            conservative_extreme=_dec(low_in),
            first_price=None,  # Q2 has no gap leg
        )
    return out


def test_legacy_q2_bracket_on_the_fixture_is_5_of_5_and_0_of_5() -> None:
    orders, store = _fixture()
    detail = q2_q3_orders.run(orders, store)

    scored = detail[detail["group"] == "filled_comparison"]
    assert len(scored) == 5
    assert scored["missing"].isna().all() and scored["q2_missing"].isna().all()
    assert int(scored["actual_anchor_100__optimistic__any_fill"].sum()) == 5
    assert int(scored["actual_anchor_100__conservative__any_fill"].sum()) == 0


def test_contract_bracket_reproduces_q2_order_by_order() -> None:
    orders, store = _fixture()
    detail = q2_q3_orders.run(orders, store).set_index("order_id")
    contexts = _contexts(orders, store)

    optimistic = {k: OptimisticTouch().fill(c) for k, c in contexts.items()}
    conservative = {k: ConservativeThrough().fill(c) for k, c in contexts.items()}

    assert sum(o.any_fill for o in optimistic.values()) == 5
    assert sum(o.any_fill for o in conservative.values()) == 0
    for o in orders:
        legacy = detail.loc[o.order_id]
        assert optimistic[o.order_id].any_fill is bool(
            legacy["actual_anchor_100__optimistic__any_fill"]
        )
        assert conservative[o.order_id].any_fill is bool(
            legacy["actual_anchor_100__conservative__any_fill"]
        )
        # The optimistic end reproduces the real fill, price and quantity.
        assert optimistic[o.order_id].price == Decimal(str(o.avg_fill))
        assert optimistic[o.order_id].filled_qty == Decimal(str(o.filled_qty))


def test_conservative_fails_both_ways_the_fixture_covers() -> None:
    """Empty inside-window (kr-1, us-1, us-2) and low == limit (kr-2, us-3)."""

    orders, store = _fixture()
    contexts = _contexts(orders, store)
    empty = {k for k, c in contexts.items() if c.conservative_extreme is None}
    at_limit = {
        k
        for k, c in contexts.items()
        if c.conservative_extreme is not None and c.conservative_extreme >= c.limit
    }
    assert empty == {"kr-1", "us-1", "us-2"}
    assert at_limit == {"kr-2", "us-3"}


@pytest.mark.parametrize("order_id", ["kr-2", "us-3"])
def test_one_tick_lower_low_flips_the_conservative_end(order_id: str) -> None:
    """Mutant guard: the 0/5 is the trade-through rule, not a dead model."""

    orders, store = _fixture()
    ctx = _contexts(orders, store)[order_id]
    assert ctx.conservative_extreme is not None
    through = FillContext(
        side=ctx.side,
        limit=ctx.limit,
        quantity=ctx.quantity,
        tick=ctx.tick,
        optimistic_extreme=ctx.optimistic_extreme,
        conservative_extreme=ctx.limit - ctx.tick,
    )
    assert ConservativeThrough().fill(ctx).any_fill is False
    assert ConservativeThrough().fill(through).price == ctx.limit

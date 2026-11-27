"""``session_observation`` (hk 6421): state rules, windows, reader and contract."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from app.services.kis_futures_mock import session_calendar as real_futures
from app.services.kis_futures_mock.session_calendar import (
    second_thursday as real_second_thursday,
)
from app.services.research_candles.session_segment import (
    UNKNOWN,
    futures_session_date,
    session_segment_of,
)
from app.services.retro_v1 import RetroReadError
from app.services.retro_v1 import session_observation as so
from app.services.retro_v1 import session_windows as windows
from app.services.retro_v1.daily_bars import is_first_receipt_batch
from tests.services.retro_v1 import session_observation_support as support
from tests.services.retro_v1.support import operational_error, validate

pytestmark = pytest.mark.unit

DAY = support.DAY
BAR = timedelta(minutes=1)
SCHEMA = "SessionObservationResponse"


# --------------------------------------------------------------------------
# windows are the stored labeller's windows
# --------------------------------------------------------------------------
def _minutes(start: datetime, hours: int) -> list[datetime]:
    return [start + timedelta(minutes=i) for i in range(hours * 60)]


def _containing(found: dict[str, windows.Window], minute: datetime) -> str:
    inside = [
        name
        for name, window in found.items()
        if window.start is not None
        and window.end is not None
        and window.start <= minute < window.end
    ]
    assert len(inside) <= 1
    return inside[0] if inside else UNKNOWN


@pytest.mark.parametrize("venue", sorted(windows.KR_SESSIONS))
def test_kr_windows_equal_the_stored_labels_for_every_minute(venue: str) -> None:
    found = {
        session: windows.session_window("kr", session, DAY)
        for session, _start, _end in windows.KR_SESSIONS[venue]
    }
    start = datetime(2026, 11, 25, 0, 0, tzinfo=support.KST).astimezone(UTC)
    for minute in _minutes(start, 24):
        assert _containing(found, minute) == session_segment_of("kr", venue, minute)


@pytest.mark.parametrize("day", [date(2026, 11, 25), date(2026, 11, 27)])
def test_us_windows_equal_the_stored_labels_incl_the_early_close(day: date) -> None:
    found = {
        session: windows.session_window("us", session, day)
        for session in windows.US_EXCHANGE_SESSIONS
    }
    if day == date(2026, 11, 27):  # the day after Thanksgiving closes 13:00 ET
        assert found["US_REGULAR"].end == datetime(2026, 11, 27, 18, 0, tzinfo=UTC)
    # The exchange's extended session ends at 17:00 ET on an early close day
    # (hk 7002: Nasdaq ETA #2026-69, NYSE holidays page), so the reader's
    # after-market window is narrower than the stored label after that close.
    cut = datetime(2026, 11, 27, 22, 0, tzinfo=UTC)
    start = datetime(day.year, day.month, day.day, 0, 0, tzinfo=support.ET)
    for minute in _minutes(start.astimezone(UTC), 24):
        if minute >= cut:
            assert _containing(found, minute) == UNKNOWN
            assert day == date(2026, 11, 27)
            continue
        assert _containing(found, minute) == session_segment_of("us", "NASD", minute)
    if day == date(2026, 11, 27):
        after = datetime(2026, 11, 27, 17, 30, tzinfo=support.ET).astimezone(UTC)
        assert session_segment_of("us", "NASD", after) == "US_POST"
        assert _containing(found, after) == UNKNOWN


@pytest.mark.parametrize(
    "day",
    [date(2026, 11, 25), date(2026, 11, 30)],  # a Monday: the night began Friday
)
def test_futures_windows_equal_the_stored_labels_and_session_date(day: date) -> None:
    found = {
        session: windows.session_window("futures", session, day)
        for session in windows.FUTURES_SESSIONS
    }
    for session, window in found.items():
        assert window.start is not None and window.end is not None, session
        for minute in _minutes(window.start - timedelta(hours=1), 14):
            inside = window.start <= minute < window.end
            label = session_segment_of("futures", "KRX", minute)
            assert (label == session and futures_session_date(minute) == day) is inside


def test_us_day_market_has_no_stored_hours_only_a_first_possible_start() -> None:
    window = windows.session_window("us", "US_DAY", DAY)
    assert (window.start, window.end) == (None, None)
    assert window.earliest_start == datetime(2026, 11, 25, 1, 0, tzinfo=UTC)


def test_previous_session_skips_holidays_and_refuses_an_unknown_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert windows.previous_session_date("kr", DAY) == date(2026, 11, 24)
    assert windows.previous_session_date("us", date(2026, 11, 27)) == DAY
    monkeypatch.setattr(windows, "trading_session_status", lambda *_: "unknown")
    assert windows.previous_session_date("kr", DAY) is None


def test_contract_expiry_uses_the_second_thursday_of_its_month() -> None:
    for year in (2026, 2027):
        for month in range(1, 13):
            assert windows.second_thursday(year, month) == real_second_thursday(
                year, month
            )
    assert not windows.contract_expired("MF200.2611", date(2026, 11, 12))
    assert windows.contract_expired("MF200.2611", date(2026, 11, 13))
    assert not windows.contract_expired("F200.2612", DAY)


#: Second Thursday of the December 2026 contract (hk 7002).
LAST_TRADE_DAY = date(2026, 12, 10)
#: The day after Thanksgiving: the regular session closes 13:00 ET.
EARLY_CLOSE_DAY = date(2026, 11, 27)


def test_the_futures_day_session_ends_at_1520_on_the_last_trading_day() -> None:
    last = windows.session_window("futures", "FUT_DAY", LAST_TRADE_DAY, "MF200.2612")
    earlier = windows.session_window(
        "futures", "FUT_DAY", date(2026, 12, 9), "MF200.2612"
    )
    next_month = windows.session_window(
        "futures", "FUT_DAY", LAST_TRADE_DAY, "MF200.2703"
    )
    unknown_symbol = windows.session_window("futures", "FUT_DAY", LAST_TRADE_DAY)
    assert last.end == datetime(2026, 12, 10, 6, 20, tzinfo=UTC)  # 15:20 KST
    assert earlier.end == datetime(2026, 12, 9, 6, 45, tzinfo=UTC)  # 15:45 KST
    assert next_month.end == datetime(2026, 12, 10, 6, 45, tzinfo=UTC)
    assert unknown_symbol.end == next_month.end
    assert last.start == next_month.start == unknown_symbol.start


def test_the_copied_futures_rule_matches_the_order_calendar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert windows.FUTURES_DAY == (
        real_futures.REGULAR_OPEN,
        real_futures.REGULAR_CLOSE,
    )
    assert windows.FUTURES_LAST_TRADE_DAY_CLOSE == real_futures.LAST_TRADE_DAY_CLOSE
    assert real_futures.last_trade_date("2026-12") == LAST_TRADE_DAY
    assert windows.last_trade_date("2026-12") == LAST_TRADE_DAY
    assert (
        real_futures.session_close_for(LAST_TRADE_DAY, "2026-12")
        == windows.futures_day_close("MF200.2612", LAST_TRADE_DAY)
        == real_futures.LAST_TRADE_DAY_CLOSE
    )
    assert (
        windows.futures_day_close("MF200.2612", date(2026, 12, 9))
        == real_futures.REGULAR_CLOSE
    )
    # The rule source answers only inside its hand-maintained range; the copy
    # reads the shared exchange calendar, so it also answers before that range.
    assert real_futures.last_trade_date("2026-11") is None
    assert windows.last_trade_date("2026-11") == date(2026, 11, 12)
    # A closed second Thursday moves both rules to the same earlier day.
    thursday = real_second_thursday(2026, 12)
    monkeypatch.setattr(
        real_futures, "day_state", lambda d: "closed" if d == thursday else "open"
    )
    monkeypatch.setattr(
        windows,
        "trading_session_status",
        lambda _market, d: "closed" if d == thursday else "open",
    )
    assert real_futures.last_trade_date("2026-12") == thursday - timedelta(days=1)
    assert windows.last_trade_date("2026-12") == thursday - timedelta(days=1)
    assert (
        windows.futures_day_close("MF200.2612", thursday - timedelta(days=1))
        == real_futures.LAST_TRADE_DAY_CLOSE
    )


def test_us_post_ends_at_17_et_on_the_confirmed_early_close() -> None:
    early = windows.session_window("us", "US_POST", EARLY_CLOSE_DAY)
    normal = windows.session_window("us", "US_POST", DAY)
    assert early.start == datetime(2026, 11, 27, 18, 0, tzinfo=UTC)  # 13:00 ET
    assert early.end == datetime(2026, 11, 27, 22, 0, tzinfo=UTC)  # 17:00 ET
    assert normal.end == datetime(2026, 11, 26, 1, 0, tzinfo=UTC)  # 20:00 ET


# --------------------------------------------------------------------------
# one rule per state
# --------------------------------------------------------------------------
OPEN = windows.session_window("kr", "KRX_REGULAR", DAY)
NOW = support.AS_OF.astimezone(UTC)


def bars(
    last: datetime, *, lag: int = 50, count: int = 10, trades: int | None = None
) -> so.SessionBars:
    row = support.agg(
        "005930",
        "KRX",
        "KRX_REGULAR",
        last.astimezone(UTC),
        lag=lag,
        bars=count,
        trade_bars=trades,
    )
    values = {
        k: v
        for k, v in row.items()
        if k not in {"symbol", "venue", "session_segment", "sources"}
    }
    return so.SessionBars(sources=("KIS",), **values)


def state(**kw: Any) -> tuple[str | None, str]:
    values: dict[str, Any] = {
        "status": "open",
        "window": OPEN,
        "bars": None,
        "bars_read": True,
        "collection": "expected",
        "as_of": NOW,
        "max_lag_seconds": 600,
    }
    values.update(kw)
    return so.derive_state(**values)


def test_a_fresh_trade_bar_is_observed() -> None:
    assert state(bars=bars(support.kst(10, 58))) == ("observed", "trade_bars_received")


@pytest.mark.parametrize("count", [1, 2])
def test_volume_zero_bars_are_no_trades_never_a_trade(count: int) -> None:
    found = bars(support.kst(10, 58), count=count, trades=0)
    assert state(bars=found) == ("no_trades", "only_zero_volume_bars")


def test_missing_rows_in_an_open_session_are_not_collected() -> None:
    assert OPEN.start is not None and NOW < OPEN.end  # type: ignore[operator]
    assert state() == ("not_collected", "no_stored_bars")


def test_missing_rows_in_an_ended_session_are_not_collected() -> None:
    assert state(as_of=NOW + timedelta(days=1)) == (
        "not_collected",
        "no_stored_bars",
    )


def test_a_receive_lag_over_the_threshold_is_delayed() -> None:
    at = support.kst(10, 40)
    assert state(bars=bars(at, lag=601)) == ("delayed", "receive_lag_over_threshold")
    # exactly the threshold is not over it
    fresh = support.kst(10, 58)
    assert state(bars=bars(fresh, lag=30), max_lag_seconds=60)[0] == "observed"
    assert state(bars=bars(at, lag=61), max_lag_seconds=60) == (
        "delayed",
        "receive_lag_over_threshold",
    )
    assert state(bars=bars(support.kst(10, 58), lag=60), max_lag_seconds=60)[0] == (
        "observed"
    )


def test_a_lagging_volume_zero_bar_is_delayed_not_no_trades() -> None:
    found = bars(support.kst(10, 58), lag=700, trades=0)
    assert state(bars=found) == ("delayed", "receive_lag_over_threshold")


@pytest.mark.parametrize("trades", [None, 0])
def test_an_old_newest_bar_is_delayed_while_the_session_is_open(
    trades: int | None,
) -> None:
    # newest bar 10:48 ends 10:49: 660 s old at 11:00
    assert state(bars=bars(support.kst(10, 48), trades=trades)) == (
        "delayed",
        "latest_bar_older_than_threshold",
    )
    assert state(bars=bars(support.kst(10, 49), trades=trades))[0] != "delayed"


def test_an_ended_session_is_judged_against_its_end_not_against_now() -> None:
    later = NOW + timedelta(days=2)
    complete = bars(support.kst(15, 29))
    assert state(bars=complete, as_of=later) == ("observed", "trade_bars_received")
    cut = bars(support.kst(12, 0))
    assert state(bars=cut, as_of=later) == (
        "delayed",
        "latest_bar_older_than_threshold",
    )


def test_collection_after_the_last_trading_day_close_is_not_delayed() -> None:
    window = windows.session_window("futures", "FUT_DAY", LAST_TRADE_DAY, "MF200.2612")
    after = datetime(2026, 12, 10, 15, 32, tzinfo=support.KST)
    # The newest bar starts 15:19 and closes at the 15:20 session end.
    found = bars(datetime(2026, 12, 10, 15, 19, tzinfo=support.KST))
    assert state(bars=found, window=window, as_of=after) == (
        "observed",
        "trade_bars_received",
    )
    assert so.phase_of(window, "open", after) == "ended"
    # Judged against the regular 15:45 close the same bar is 12 minutes old.
    uncut = windows.Window(
        window.start, datetime(2026, 12, 10, 15, 45, tzinfo=support.KST)
    )
    assert state(bars=found, window=uncut, as_of=after) == (
        "delayed",
        "latest_bar_older_than_threshold",
    )


def test_collection_after_the_early_close_is_not_delayed() -> None:
    window = windows.session_window("us", "US_POST", EARLY_CLOSE_DAY)
    after = datetime(2026, 11, 27, 17, 30, tzinfo=support.ET)
    # The newest after-market bar starts 16:59 and closes at the 17:00 end.
    found = bars(datetime(2026, 11, 27, 16, 59, tzinfo=support.ET))
    assert state(bars=found, window=window, as_of=after) == (
        "observed",
        "trade_bars_received",
    )
    assert so.phase_of(window, "open", after) == "ended"
    # Judged against 20:00 ET the same bar would be half an hour old.
    uncut = windows.Window(
        window.start, datetime(2026, 11, 27, 20, 0, tzinfo=support.ET)
    )
    assert state(bars=found, window=uncut, as_of=after) == (
        "delayed",
        "latest_bar_older_than_threshold",
    )


def test_unknown_hours_apply_the_lag_rule_only() -> None:
    unknown = windows.session_window("us", "US_DAY", DAY)
    old = bars(support.et(21, 0, day=24))
    assert state(bars=old, window=unknown)[0] == "observed"
    assert state(window=unknown) == (None, "window_unknown")
    assert state(window=unknown, as_of=unknown.earliest_start - BAR) == (
        "closed",
        "session_not_started",
    )


@pytest.mark.parametrize(
    ("kw", "expected"),
    [
        ({"status": "closed"}, ("closed", "not_trading_day")),
        ({"status": "unknown"}, (None, "calendar_unknown")),
        ({"collection": "not_eligible"}, ("closed", "symbol_not_nxt_eligible")),
        ({"collection": "eligibility_unknown"}, (None, "eligibility_unknown")),
        ({"collection": "universe_unread"}, (None, "source_unavailable")),
        (
            {"collection": "unsupported"},
            ("not_collected", "collection_unsupported"),
        ),
        (
            {"collection": "symbol_not_in_universe"},
            ("not_collected", "symbol_not_in_universe"),
        ),
        ({"bars_read": False}, (None, "source_unavailable")),
        (
            {"as_of": OPEN.start - BAR},
            ("closed", "session_not_started"),
        ),
        (
            {"as_of": OPEN.start + timedelta(seconds=659)},
            (None, "awaiting_first_receipt"),
        ),
        (
            {"as_of": OPEN.start + timedelta(seconds=660)},
            ("not_collected", "no_stored_bars"),
        ),
    ],
)
def test_rules_without_stored_bars(
    kw: dict[str, Any], expected: tuple[str | None, str]
) -> None:
    assert state(**kw) == expected


def test_stored_bars_outrank_eligibility_and_calendar() -> None:
    found = bars(support.kst(10, 58))
    for collection in ("not_eligible", "unsupported", "eligibility_unknown"):
        assert state(bars=found, collection=collection)[0] == "observed"


# --------------------------------------------------------------------------
# reference close and symbols
# --------------------------------------------------------------------------
def test_reference_prefers_the_official_close_then_the_one_daily_row() -> None:
    official = [support.close_row("005930", "171300", "krx_openapi")]
    daily = [support.close_row("005930", "170900", "kis")]
    assert so.pick_reference(official, daily) == so.Reference(
        Decimal("171300"), "krx_official", "krx_openapi", None
    )
    no_close = [{"symbol": "005930", "close": None, "source": "krx_openapi"}]
    assert so.pick_reference(no_close, daily).basis == "stored_daily"
    assert so.pick_reference([], []).reason == "no_stored_close"
    assert so.pick_reference([], daily * 2).reason == "source_mixed"
    zero = [support.close_row("005930", "0", "kis")]
    assert so.pick_reference([], zero).reason == "no_stored_close"


def test_an_unread_source_never_falls_back_to_a_weaker_reference() -> None:
    daily = [support.close_row("005930", "170900", "kis")]
    assert so.pick_reference(None, daily).reason == "source_unavailable"
    assert so.pick_reference([], None).reason == "source_unavailable"


def test_symbols_are_split_normalized_and_assigned_a_market() -> None:
    found = so.parse_symbols("005930, BRK-B,spy,MF200.2612,005930", None)
    assert [(s.market, s.symbol) for s in found] == [
        ("kr", "005930"),
        ("us", "BRK.B"),
        ("us", "SPY"),
        ("futures", "MF200.2612"),
    ]
    assert [s.market for s in so.parse_symbols("BRK.B", "us")] == ["us"]


@pytest.mark.parametrize(
    ("value", "market"),
    [
        ("", None),
        ("005930,", None),
        ("KRW-BTC", None),
        ("SPY", "kr"),
        ("005930", "us"),
        ("005930", "futures"),
        ("MF200.2612", "us"),
        (",".join(["SPY"] * 51), None),
    ],
)
def test_symbols_outside_the_scope_are_refused(value: str, market: str | None) -> None:
    with pytest.raises(RetroReadError) as err:
        so.parse_symbols(value, market)
    assert err.value.code == "validation_error"


# --------------------------------------------------------------------------
# reader
# --------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def frozen_now(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(so, "_now", lambda: support.AS_OF.astimezone(UTC))


def index(response: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    return {(r["symbol"], r["session"]): r for r in response["data"]}


async def read(session: Any, **kw: Any) -> dict[str, Any]:
    kw.setdefault("date", DAY)
    kw.setdefault("limit", 500)
    response = await so.read_session_observation(session, **kw)
    validate(response, SCHEMA)
    return response


@pytest.mark.asyncio
async def test_default_population_is_the_fixed_list_movers_and_stored_contracts() -> (
    None
):
    response = await read(support.scenario())
    origins = {(r["market"], r["symbol"]): r["origin"] for r in response["data"]}
    assert origins == {
        ("kr", "005930"): "fixed",  # also ranked: stays fixed
        ("kr", "000660"): "fixed",
        ("kr", "278470"): "fixed",
        ("kr", "035420"): "mover",
        ("us", "SPY"): "fixed",
        ("us", "QQQ"): "fixed",
        ("us", "SMH"): "fixed",
        ("us", "DELL"): "mover",
        ("us", "NOPE"): "mover",
        ("futures", "MF200.2612"): "stored_contract",  # 2611 is past its month
    }
    # 4 KR x 5 sessions, 5 US x (3 exchange sessions + day market), 1 x 2 futures
    assert response["total"] == len(response["data"]) == 20 + 20 + 2
    assert response["rule_parameters"] == {
        "session_date": "2026-11-25",
        "max_lag_seconds": 600,
        "movers_per_market": 10,
    }
    assert response["unknowns"] == []


@pytest.mark.asyncio
async def test_every_state_is_reported_with_its_rule() -> None:
    rows = index(await read(support.scenario()))
    expected = {
        ("005930", "KRX_REGULAR"): ("observed", "trade_bars_received", "open"),
        ("005930", "NXT_PRE"): ("observed", "trade_bars_received", "ended"),
        ("278470", "NXT_PRE"): ("no_trades", "only_zero_volume_bars", "ended"),
        ("278470", "KRX_REGULAR"): (
            "delayed",
            "latest_bar_older_than_threshold",
            "open",
        ),
        ("000660", "NXT_OVERLAP"): (
            "delayed",
            "receive_lag_over_threshold",
            "open",
        ),
        ("000660", "KRX_REGULAR"): ("not_collected", "no_stored_bars", "open"),
        ("000660", "NXT_PRE"): ("not_collected", "no_stored_bars", "ended"),
        ("005930", "KRX_AFTER"): ("closed", "session_not_started", "before_open"),
        ("035420", "NXT_PRE"): ("closed", "symbol_not_nxt_eligible", "ended"),
        ("SPY", "US_PRE"): ("closed", "session_not_started", "before_open"),
        ("SPY", "US_DAY"): (None, "window_unknown", "unknown"),
        ("NOPE", "US_REGULAR"): ("closed", "session_not_started", "before_open"),
        ("MF200.2612", "FUT_DAY"): ("observed", "trade_bars_received", "open"),
        ("MF200.2612", "FUT_NIGHT"): (
            "not_collected",
            "collection_unsupported",
            "ended",
        ),
    }
    actual = {
        key: (rows[key]["state"], rows[key]["reason"], rows[key]["phase"])
        for key in expected
    }
    assert actual == expected


@pytest.mark.asyncio
async def test_a_row_carries_source_times_lag_and_reference() -> None:
    response = await read(support.scenario())
    rows = index(response)
    row = rows[("005930", "KRX_REGULAR")]
    assert row == {
        "market": "kr",
        "symbol": "005930",
        "origin": "fixed",
        "venue": "KRX",
        "session": "KRX_REGULAR",
        "session_start": "2026-11-25T00:00:00Z",
        "session_end": "2026-11-25T06:30:00Z",
        "phase": "open",
        "state": "observed",
        "reason": "trade_bars_received",
        "source": "KIS",
        "sources": ["KIS"],
        "stored_bar_count": 119,
        "trade_bar_count": 119,
        "last_bar_at": "2026-11-25T01:58:00Z",
        "last_trade_bar_at": "2026-11-25T01:58:00Z",
        "last_received_at": "2026-11-25T01:59:50Z",
        "receive_lag_seconds": 50,
        "reference_close_price": "171300",
        "reference_close_date": "2026-11-24",
        "reference_close_basis": "krx_official",
        "reference_close_source": "krx_openapi",
        "reference_close_reason": None,
        "currency": "KRW",
    }
    zero = rows[("278470", "NXT_PRE")]
    assert zero["trade_bar_count"] == 0 and zero["stored_bar_count"] == 2
    assert zero["last_trade_bar_at"] is None and zero["last_bar_at"] is not None
    assert zero["reference_close_reason"] == "no_stored_close"
    assert zero["reference_close_date"] == "2026-11-24"
    assert rows[("000660", "NXT_OVERLAP")]["receive_lag_seconds"] == 900
    assert rows[("000660", "KRX_REGULAR")]["reference_close_basis"] == "stored_daily"
    assert rows[("035420", "KRX_REGULAR")]["reference_close_reason"] == "source_mixed"
    spy = rows[("SPY", "US_REGULAR")]
    assert (spy["venue"], spy["reference_close_price"], spy["currency"]) == (
        "AMEX",
        "601.2500",
        "USD",
    )
    assert rows[("SPY", "US_DAY")]["venue"] == "TOSS_INTEGRATED"
    assert rows[("NOPE", "US_REGULAR")]["venue"] is None
    future = rows[("MF200.2612", "FUT_DAY")]
    assert future["reference_close_reason"] == "unsupported"
    assert future["reference_close_date"] is None and future["currency"] is None
    # The newest first receipt read (000660's late 10:44 bar) is the data cutoff.
    assert response["data_as_of"] == "2026-11-25T02:00:00Z"


@pytest.mark.asyncio
async def test_us_sessions_of_an_ended_day_and_the_unresolved_symbol() -> None:
    later = datetime(2026, 11, 26, 3, 0, tzinfo=UTC)
    us_bars = [
        support.agg("SPY", "AMEX", "US_REGULAR", support.et(15, 59)),
        support.agg(
            "SPY",
            "TOSS_INTEGRATED",
            "US_DAY",
            support.et(1, 0),
            source="TOSS",
        ),
        support.agg(
            "SPY",
            "TOSS_INTEGRATED",
            "US_PRE",
            support.et(9, 29),
            source="TOSS",
        ),
        # stored under another exchange than today's universe row
        support.agg("QQQ", "NYSE", "US_REGULAR", support.et(15, 59)),
    ]
    session = support.scenario(**{support.US_BARS: us_bars})
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(so, "_now", lambda: later)
        rows = (await read(session, market="us"))["data"]
    found = {(r["symbol"], r["venue"], r["session"]): r for r in rows}
    assert found[("SPY", "AMEX", "US_REGULAR")]["state"] == "observed"
    assert found[("SPY", "AMEX", "US_PRE")]["state"] == "not_collected"
    assert found[("SPY", "TOSS_INTEGRATED", "US_DAY")]["state"] == "observed"
    assert found[("SPY", "TOSS_INTEGRATED", "US_PRE")]["source"] == "TOSS"
    assert found[("QQQ", "NYSE", "US_REGULAR")]["state"] == "observed"
    assert ("QQQ", "NASD", "US_REGULAR") not in found
    assert found[("NOPE", None, "US_REGULAR")]["reason"] == "symbol_not_in_universe"
    assert found[("SMH", "TOSS_INTEGRATED", "US_DAY")]["reason"] == "window_unknown"


@pytest.mark.asyncio
async def test_kis_day_bars_add_no_phantom_listing_venue_slots() -> None:
    """hk 7037: KIS 주간거래 is not a listing exchange. Stored KIS_DAY bars must not
    turn into NASD/AMEX-style expected slots for the regular sessions."""
    later = datetime(2026, 11, 26, 3, 0, tzinfo=UTC)
    us_bars = [
        support.agg(
            "SPY",
            "KIS_DAY",
            "US_DAY",
            support.et(1, 0),
            source="KIS",
        ),
    ]
    session = support.scenario(**{support.US_BARS: us_bars})
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(so, "_now", lambda: later)
        rows = (await read(session, market="us"))["data"]
    found = {(r["symbol"], r["venue"], r["session"]): r for r in rows}
    assert found[("SPY", "KIS_DAY", "US_DAY")]["state"] == "observed"
    assert ("SPY", "KIS_DAY", "US_REGULAR") not in found
    assert ("SPY", "KIS_DAY", "US_PRE") not in found
    # The listing venue keeps its expected regular slots, as with Toss-only rows.
    assert found[("SPY", "AMEX", "US_REGULAR")]["state"] == "not_collected"


@pytest.mark.asyncio
async def test_the_early_close_day_row_reports_the_exchange_end() -> None:
    later = datetime(2026, 11, 27, 17, 30, tzinfo=support.ET)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(so, "_now", lambda: later)
        rows = index(
            await read(support.scenario(), date=EARLY_CLOSE_DAY, market="us", movers=0)
        )
    post = rows[("SPY", "US_POST")]
    assert (post["session_end"], post["phase"]) == ("2026-11-27T22:00:00Z", "ended")
    assert post["state"] == "not_collected" and post["reason"] == "no_stored_bars"


@pytest.mark.asyncio
async def test_a_last_trading_day_row_uses_the_contract_close() -> None:
    later = datetime(2026, 12, 10, 15, 32, tzinfo=support.KST)
    session = support.scenario(
        **{
            support.FUT_BARS: [
                support.agg(
                    "MF200.2612",
                    "KRX",
                    "FUT_DAY",
                    datetime(2026, 12, 10, 15, 19, tzinfo=support.KST),
                )
            ]
        }
    )
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(so, "_now", lambda: later)
        rows = index(await read(session, date=LAST_TRADE_DAY, symbols="MF200.2612"))
    day = rows[("MF200.2612", "FUT_DAY")]
    assert (day["session_end"], day["phase"]) == ("2026-12-10T06:20:00Z", "ended")
    assert (day["state"], day["reason"]) == ("observed", "trade_bars_received")


@pytest.mark.asyncio
async def test_a_holiday_is_closed_for_every_session_without_hours() -> None:
    response = await read(
        support.scenario(), date=date(2026, 11, 26), market="us", movers=0
    )
    assert {(r["state"], r["reason"], r["phase"]) for r in response["data"]} == {
        ("closed", "not_trading_day", "not_trading_day")
    }
    assert {r["session_start"] for r in response["data"]} == {None}


@pytest.mark.asyncio
async def test_requested_symbols_replace_the_default_population() -> None:
    session = support.scenario()
    response = await read(session, symbols="278470,MF200.2612")
    assert {(r["symbol"], r["origin"]) for r in response["data"]} == {
        ("278470", "requested"),
        ("MF200.2612", "requested"),
    }
    assert response["total"] == 7
    assert response["rule_parameters"]["movers_per_market"] == 0
    assert not any(support.MOVERS in s for s in session.statements)
    assert not any(support.CONTRACTS in s for s in session.statements)
    assert not any(support.US_BARS in s for s in session.statements)


@pytest.mark.asyncio
async def test_movers_zero_reads_no_mover_log() -> None:
    session = support.scenario()
    response = await read(session, market="kr", movers=0)
    assert {r["symbol"] for r in response["data"]} == set(so.FIXED_SYMBOLS["kr"])
    assert not any(support.MOVERS in s for s in session.statements)


@pytest.mark.asyncio
async def test_the_mover_cap_counts_only_symbols_outside_the_fixed_list() -> None:
    response = await read(support.scenario(), market="kr", movers=1)
    movers = {r["symbol"] for r in response["data"] if r["origin"] == "mover"}
    assert movers == {"035420"}


@pytest.mark.asyncio
async def test_empty_mover_log_and_no_stored_contract_are_said_not_hidden() -> None:
    session = support.scenario(**{support.MOVERS: [], support.CONTRACTS: []})
    response = await read(session)
    assert {(u["field"], u["reason"]) for u in response["unknowns"]} == {
        ("population.movers_kr", "missing_data"),
        ("population.movers_us", "missing_data"),
        ("population.futures", "missing_data"),
    }
    assert not any(r["market"] == "futures" for r in response["data"])


@pytest.mark.asyncio
async def test_a_failed_bar_read_is_unknown_never_not_collected() -> None:
    session = support.scenario(**{support.KR_BARS: operational_error()})
    response = await read(session)
    kr = [r for r in response["data"] if r["market"] == "kr"]
    assert {(r["state"], r["reason"]) for r in kr} == {(None, "source_unavailable")}
    assert {r["stored_bar_count"] for r in kr} == {None}
    assert {r["sources"] for r in kr} == {None}
    assert response["total"] is None
    fields = {u["field"] for u in response["unknowns"]}
    assert {"source.minute_bars_kr", "total"} <= fields
    # the other markets are unaffected
    assert index(response)[("MF200.2612", "FUT_DAY")]["state"] == "observed"
    assert "raw driver text" not in str(response)


@pytest.mark.asyncio
async def test_a_failed_universe_read_does_not_claim_eligibility() -> None:
    session = support.scenario(**{support.KR_UNIVERSE: operational_error()})
    rows = index(await read(session, market="kr"))
    assert rows[("035420", "NXT_PRE")]["reason"] == "source_unavailable"
    # stored bars are still evidence, and KRX does not need eligibility
    assert rows[("005930", "NXT_PRE")]["state"] == "observed"
    assert rows[("000660", "KRX_REGULAR")]["state"] == "not_collected"


@pytest.mark.asyncio
async def test_a_failed_official_read_does_not_fall_back_to_the_daily_close() -> None:
    session = support.scenario(**{support.OFFICIAL: operational_error()})
    response = await read(session, market="kr")
    assert {r["reference_close_reason"] for r in response["data"]} == {
        "source_unavailable"
    }
    assert {r["reference_close_price"] for r in response["data"]} == {None}


@pytest.mark.asyncio
async def test_a_failed_mover_read_leaves_the_total_unknown() -> None:
    session = support.scenario(**{support.MOVERS: operational_error()})
    response = await read(session, market="kr")
    assert response["total"] is None
    assert {r["origin"] for r in response["data"]} == {"fixed"}
    assert {"field": "source.movers_kr", "reason": "upstream_error"} in response[
        "unknowns"
    ]


@pytest.mark.asyncio
async def test_pagination_keeps_the_whole_population_total() -> None:
    first = await read(support.scenario(), limit=5)
    assert (len(first["data"]), first["total"], first["truncated"]) == (5, 42, True)
    second = await read(support.scenario(), limit=5, cursor=first["next_cursor"])
    assert second["data"][0] != first["data"][0]
    with pytest.raises(RetroReadError):
        await read(support.scenario(), max_lag_seconds=60, cursor=first["next_cursor"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kw",
    [
        {"date": "25-11-2026"},
        {"market": "crypto"},
        {"movers": -1},
        {"movers": 51},
        {"movers": True},
        {"max_lag_seconds": 59},
        {"max_lag_seconds": 3601},
        {"symbols": "SPY", "movers": 1},
        {"limit": 0},
    ],
)
async def test_invalid_requests_are_refused_before_any_statement(
    kw: dict[str, Any],
) -> None:
    session = support.scenario()
    with pytest.raises(RetroReadError) as err:
        await so.read_session_observation(session, **{"date": DAY, **kw})
    assert err.value.code == "validation_error"
    assert session.statements == []


@pytest.mark.asyncio
async def test_bar_queries_read_first_receipts_with_bound_parameters_only() -> None:
    session = support.scenario()
    await read(session)
    bar_sql = [s for s in session.statements if " AS b\n" in s]
    assert len(bar_sql) == 3
    for sql in bar_sql:
        assert "position('backfill' in lower(b.batch_id)) = 0" in sql
        assert "left(lower(b.batch_id), 8) <> 'promote-'" in sql
        assert "FILTER (WHERE volume > 0)" in sql
        assert "005930" not in sql and "SPY" not in sql
    # the SQL rule is the same one the bar reader applies in Python
    assert not is_first_receipt_batch("x-backfill-1")
    assert not is_first_receipt_batch("promote-1")
    assert is_first_receipt_batch("session-minutes-kr-1")


@pytest.mark.parametrize("market", so.MARKET_ORDER)
def test_the_chunk_bounds_contain_every_minute_of_the_session_date(
    market: str,
) -> None:
    # Mondays and the day after a holiday have the earliest night/extended bars.
    for day in (DAY, date(2026, 11, 30), date(2026, 2, 19)):
        lo, hi = so._time_bounds(market, day)
        for session in windows.SESSION_ORDER:
            if market == "futures" and session.startswith("FUT"):
                window = windows.session_window(market, session, day)
            elif market == "us" and session.startswith("US"):
                window = windows.session_window(market, session, day)
            elif market == "kr" and session.startswith(("KRX", "NXT")):
                window = windows.session_window(market, session, day)
            else:
                continue
            start = window.start or window.earliest_start
            assert start is not None and lo <= start, (day, session)
            assert window.end is None or window.end <= hi, (day, session)

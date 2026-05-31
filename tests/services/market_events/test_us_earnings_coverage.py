"""Tests for the US earnings coverage measurement (ROB-371).

Unit tests cover the pure ``aggregate_coverage`` §5 logic (no DB). One
integration test exercises the DB-backed ``measure`` wiring against a seeded
``us_candles_1d``.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from app.services.market_events.earnings_decision_time import (
    label_earnings_decision_time,
)
from app.services.market_events.session_calendar import trading_sessions_in_range
from app.services.market_events.us_earnings_coverage import (
    MIN_WINDOW_COVERAGE,
    aggregate_coverage,
)

# A clean mid-month trading day; the -5..+20 calendar window has no edge holidays.
_EVENT_DATE = date(2025, 7, 15)


def _expected_sessions(event_date: date) -> list[date]:
    return trading_sessions_in_range(
        "us", event_date - timedelta(days=5), event_date + timedelta(days=20)
    )


def _decision_window(event_date: date, time_hint: str | None) -> set[date]:
    """Expected -5..+20 sessions anchored on the lookahead-safe decision session
    (the window a backtest would actually use)."""
    label = label_earnings_decision_time(event_date, time_hint, "us")
    d = label.decision_session
    assert d is not None
    return set(
        trading_sessions_in_range("us", d - timedelta(days=5), d + timedelta(days=20))
    )


# --------------------------------------------------------------------------- #
# Pure aggregate_coverage unit tests
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_full_coverage_single_event():
    expected = set(_expected_sessions(_EVENT_DATE))
    m = aggregate_coverage(
        events=[("AAPL", _EVENT_DATE, "before_open")],
        total_released=1,
        window_present={("AAPL", _EVENT_DATE): (set(expected), set(expected))},
        benchmark_present={"SPY": set(expected)},
        delisted_symbols=set(),
        delisted_recoverable=0,
        session_calendar_present=True,
    )
    assert m.realized_events == 1
    assert m.eligible_events == 1
    assert m.events_with_bars_present == 1
    assert m.events_with_zero_bars == 0
    assert m.joinable_events == 1
    assert m.joinable_symbols == 1
    assert m.window_coverage_p50 == pytest.approx(1.0)
    assert m.intraday_excluded_events == 0
    assert m.benchmark_coverage == pytest.approx(1.0)
    assert m.tradability_coverage == pytest.approx(1.0)
    assert m.date_only_ratio == 1.0
    assert m.dup_ambiguous_ratio == 0.0


@pytest.mark.unit
def test_zero_bars_event_counts_as_zero_not_joinable():
    m = aggregate_coverage(
        events=[("ZZZZ", _EVENT_DATE, "after_close")],
        total_released=1,
        window_present={},  # no bars
        benchmark_present={},
        delisted_symbols=set(),
        delisted_recoverable=0,
        session_calendar_present=True,
    )
    assert m.realized_events == 1
    assert m.events_with_bars_present == 0
    assert m.events_with_zero_bars == 1
    assert m.joinable_events == 0
    assert m.joinable_symbols == 0
    assert m.window_coverage_p50 == pytest.approx(0.0)


@pytest.mark.unit
def test_intraday_event_is_excluded_not_measured():
    # ROB-378: a during_market event is counted in intraday_excluded_events but
    # excluded from the eligible population — never measured, joined, or counted
    # as having bars (even though full bars are supplied here).
    expected = set(_expected_sessions(_EVENT_DATE))
    m = aggregate_coverage(
        events=[("INTC", _EVENT_DATE, "during_market")],
        total_released=1,
        window_present={("INTC", _EVENT_DATE): (set(expected), set(expected))},
        benchmark_present={"SPY": set(expected)},
        delisted_symbols=set(),
        delisted_recoverable=0,
        session_calendar_present=True,
    )
    assert m.intraday_excluded_events == 1
    assert m.realized_events == 1
    assert m.eligible_events == 0
    assert m.events_with_bars_present == 0
    assert m.joinable_events == 0
    assert m.joinable_symbols == 0
    assert m.benchmark_coverage == pytest.approx(0.0)


@pytest.mark.unit
def test_intraday_mixed_with_eligible_excludes_only_intraday():
    # One eligible BMO event (full coverage) + one intraday event. The intraday
    # event is excluded from every join-quality count; the eligible one is
    # measured normally.
    expected = set(_expected_sessions(_EVENT_DATE))
    m = aggregate_coverage(
        events=[
            ("AAPL", _EVENT_DATE, "before_open"),
            ("INTC", _EVENT_DATE, "during_market"),
        ],
        total_released=2,
        window_present={
            ("AAPL", _EVENT_DATE): (set(expected), set(expected)),
            ("INTC", _EVENT_DATE): (set(expected), set(expected)),
        },
        benchmark_present={"SPY": set(expected)},
        delisted_symbols=set(),
        delisted_recoverable=0,
        session_calendar_present=True,
    )
    assert m.realized_events == 2
    assert m.intraday_excluded_events == 1
    assert m.eligible_events == 1
    assert m.joinable_events == 1
    assert m.joinable_symbols == 1
    # benchmark denominator is the eligible population (1), fully covered.
    assert m.benchmark_coverage == pytest.approx(1.0)


@pytest.mark.unit
def test_null_symbol_rows_drive_dup_ratio():
    # 9 real events + 1 NULL-symbol row dropped upstream -> total_released=10.
    expected = set(_expected_sessions(_EVENT_DATE))
    events = [(f"SYM{i}", _EVENT_DATE, "before_open") for i in range(9)]
    window_present = {
        (s, _EVENT_DATE): (set(expected), set(expected)) for s, _, _ in events
    }
    m = aggregate_coverage(
        events=events,
        total_released=10,
        window_present=window_present,
        benchmark_present={"SPY": set(expected)},
        delisted_symbols=set(),
        delisted_recoverable=0,
        session_calendar_present=True,
    )
    assert m.realized_events == 9
    assert m.dup_ambiguous_ratio == pytest.approx(0.1)


@pytest.mark.unit
def test_partial_window_below_threshold_not_joinable_but_has_bars():
    expected = _expected_sessions(_EVENT_DATE)
    # Present only the first ~70% of expected sessions -> coverage < 0.90.
    cutoff = int(len(expected) * 0.7)
    present = set(expected[:cutoff])
    m = aggregate_coverage(
        events=[("AAPL", _EVENT_DATE, "before_open")],
        total_released=1,
        window_present={("AAPL", _EVENT_DATE): (present, present)},
        benchmark_present={"SPY": set(expected)},
        delisted_symbols=set(),
        delisted_recoverable=0,
        session_calendar_present=True,
    )
    assert m.events_with_bars_present == 1
    assert m.joinable_events == 0
    assert m.window_coverage_p50 < MIN_WINDOW_COVERAGE


@pytest.mark.unit
def test_tradability_excludes_joinable_symbol_with_no_volume():
    expected = set(_expected_sessions(_EVENT_DATE))
    m = aggregate_coverage(
        events=[("AAPL", _EVENT_DATE, "before_open")],
        total_released=1,
        # full price coverage but ZERO volume bars
        window_present={("AAPL", _EVENT_DATE): (set(expected), set())},
        benchmark_present={"SPY": set(expected)},
        delisted_symbols=set(),
        delisted_recoverable=0,
        session_calendar_present=True,
    )
    assert m.joinable_symbols == 1
    assert m.tradability_coverage == pytest.approx(0.0)


@pytest.mark.unit
def test_benchmark_below_threshold_is_not_covered():
    expected = _expected_sessions(_EVENT_DATE)
    half = set(expected[: len(expected) // 2])  # ~50% benchmark coverage
    m = aggregate_coverage(
        events=[("AAPL", _EVENT_DATE, "before_open")],
        total_released=1,
        window_present={("AAPL", _EVENT_DATE): (set(expected), set(expected))},
        benchmark_present={"SPY": half},
        delisted_symbols=set(),
        delisted_recoverable=0,
        session_calendar_present=True,
    )
    assert m.benchmark_coverage == pytest.approx(0.0)


@pytest.mark.unit
def test_delisted_events_counted():
    expected = set(_expected_sessions(_EVENT_DATE))
    m = aggregate_coverage(
        events=[("DEAD", _EVENT_DATE, "before_open")],
        total_released=1,
        window_present={("DEAD", _EVENT_DATE): (set(expected), set(expected))},
        benchmark_present={"SPY": set(expected)},
        delisted_symbols={"DEAD"},
        delisted_recoverable=1,
        session_calendar_present=True,
    )
    assert m.delisted_events == 1
    assert m.delisted_recoverable == 1


@pytest.mark.unit
def test_unknown_time_ratio_measured():
    expected = set(_expected_sessions(_EVENT_DATE))
    events = [
        ("AAA", _EVENT_DATE, "before_open"),
        ("BBB", _EVENT_DATE, None),
        ("CCC", _EVENT_DATE, "unknown"),
        ("DDD", _EVENT_DATE, "after_close"),
    ]
    window_present = {
        (s, _EVENT_DATE): (set(expected), set(expected)) for s, _, _ in events
    }
    m = aggregate_coverage(
        events=events,
        total_released=4,
        window_present=window_present,
        benchmark_present={"SPY": set(expected)},
        delisted_symbols=set(),
        delisted_recoverable=0,
        session_calendar_present=True,
    )
    # BBB (None) + CCC (unknown) are the two non-BMO/AMC/intraday -> 2/4.
    assert m.unknown_time_ratio == pytest.approx(0.5)


# --------------------------------------------------------------------------- #
# Blocker 2: window must be anchored on the lookahead-safe decision session
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_amc_window_anchored_on_next_session_not_event_date():
    # AMC release: the tradable reaction window starts at the NEXT session, so the
    # coverage window must be anchored there. Bars present exactly for the
    # decision-session window -> coverage 1.0. (If the code anchored on event_date,
    # the shifted window would intersect at <1.0, so p50==1.0 is a sharp proof.)
    label = label_earnings_decision_time(_EVENT_DATE, "after_close", "us")
    assert label.decision_session is not None
    assert label.decision_session != _EVENT_DATE  # AMC shifts forward
    win = _decision_window(_EVENT_DATE, "after_close")
    m = aggregate_coverage(
        events=[("MSFT", _EVENT_DATE, "after_close")],
        total_released=1,
        window_present={("MSFT", _EVENT_DATE): (win, win)},
        benchmark_present={"SPY": win},
        delisted_symbols=set(),
        delisted_recoverable=0,
        session_calendar_present=True,
    )
    assert m.joinable_events == 1
    assert m.joinable_symbols == 1
    assert m.window_coverage_p50 == pytest.approx(1.0)
    assert m.benchmark_coverage == pytest.approx(1.0)
    assert m.unmappable_events == 0


@pytest.mark.unit
def test_event_date_anchored_bars_undercover_an_amc_event():
    # Conversely: bars covering only the raw event_date window do NOT fully cover
    # an AMC event's decision-session window -> coverage strictly < 1.0. This
    # fails if the code (incorrectly) anchored on event_date.
    event_win = set(_expected_sessions(_EVENT_DATE))
    m = aggregate_coverage(
        events=[("MSFT", _EVENT_DATE, "after_close")],
        total_released=1,
        window_present={("MSFT", _EVENT_DATE): (event_win, event_win)},
        benchmark_present={"SPY": event_win},
        delisted_symbols=set(),
        delisted_recoverable=0,
        session_calendar_present=True,
    )
    assert m.window_coverage_p50 < 1.0


@pytest.mark.unit
def test_unknown_window_anchored_on_next_session():
    label = label_earnings_decision_time(_EVENT_DATE, "unknown", "us")
    assert label.decision_session != _EVENT_DATE
    win = _decision_window(_EVENT_DATE, "unknown")
    m = aggregate_coverage(
        events=[("GOOG", _EVENT_DATE, "unknown")],
        total_released=1,
        window_present={("GOOG", _EVENT_DATE): (win, win)},
        benchmark_present={"SPY": win},
        delisted_symbols=set(),
        delisted_recoverable=0,
        session_calendar_present=True,
    )
    assert m.joinable_events == 1
    assert m.window_coverage_p50 == pytest.approx(1.0)


@pytest.mark.unit
def test_unmappable_event_counted_and_not_joinable():
    # Out-of-calendar-range event -> no lookahead-safe decision session -> fail
    # closed: counted explicitly, never treated as a valid event_date window.
    m = aggregate_coverage(
        events=[("FUTR", date(2100, 1, 15), "before_open")],
        total_released=1,
        window_present={},
        benchmark_present={},
        delisted_symbols=set(),
        delisted_recoverable=0,
        session_calendar_present=True,
    )
    assert m.unmappable_events == 1
    assert m.joinable_events == 0
    assert m.realized_events == 1
    assert m.window_coverage_p50 == pytest.approx(0.0)


# --------------------------------------------------------------------------- #
# Integration test: DB-backed measure() against a seeded us_candles_1d
# --------------------------------------------------------------------------- #
_US_CANDLES_DDL = """
CREATE TABLE IF NOT EXISTS public.us_candles_1d (
    time TIMESTAMPTZ NOT NULL,
    symbol TEXT NOT NULL,
    exchange TEXT NOT NULL,
    open NUMERIC NOT NULL,
    high NUMERIC NOT NULL,
    low NUMERIC NOT NULL,
    close NUMERIC NOT NULL,
    adj_close NUMERIC,
    volume NUMERIC NOT NULL,
    value NUMERIC NOT NULL,
    source TEXT NOT NULL,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_us_candles_1d_exchange CHECK (exchange IN ('NASD', 'NYSE', 'AMEX')),
    CONSTRAINT uq_us_candles_1d_time_symbol_exchange UNIQUE (time, symbol, exchange)
)
"""


async def _clean_finnhub_us_earnings(db_session, *, from_date, to_date):
    """Isolate this test's slice — the persistent test DB is not truncated
    between tests, so prior released US earnings events would inflate counts."""
    from sqlalchemy import text

    await db_session.execute(
        text(
            "DELETE FROM market_events WHERE source='finnhub' AND "
            "category='earnings' AND market='us' AND "
            "event_date >= :from_date AND event_date <= :to_date"
        ),
        {"from_date": from_date, "to_date": to_date},
    )


async def _seed_released_event(db_session, *, symbol, event_date, time_hint):
    from app.models.market_events import MarketEvent

    db_session.add(
        MarketEvent(
            source="finnhub",
            category="earnings",
            market="us",
            symbol=symbol,
            event_date=event_date,
            status="released",
            time_hint=time_hint,
        )
    )


async def _seed_full_window_bars(db_session, *, symbol, event_date):
    from app.services.daily_candles.repository import (
        DailyCandleRow,
        DailyCandlesRepository,
        MarketKey,
    )

    repo = DailyCandlesRepository(session=db_session)
    sessions = trading_sessions_in_range(
        "us", event_date - timedelta(days=5), event_date + timedelta(days=20)
    )
    rows = [
        DailyCandleRow(
            time_utc=datetime.combine(s, datetime.min.time(), tzinfo=UTC),
            symbol=symbol,
            partition="NYSE",
            open=100.0,
            high=102.0,
            low=99.0,
            close=101.0,
            adj_close=101.0,
            volume=1_000_000.0,
            value=101_000_000.0,
            source="kis",
        )
        for s in sessions
    ]
    await repo.upsert_rows(market=MarketKey.US, rows=rows)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_measure_counts_released_event_with_full_window(db_session):
    from sqlalchemy import text

    from app.services.market_events.us_earnings_coverage import (
        UsEarningsCoverageService,
    )

    await db_session.execute(text(_US_CANDLES_DDL))
    await _clean_finnhub_us_earnings(
        db_session, from_date=date(2025, 7, 1), to_date=date(2025, 7, 31)
    )
    await _seed_released_event(
        db_session, symbol="AAPL", event_date=_EVENT_DATE, time_hint="before_open"
    )
    await _seed_full_window_bars(db_session, symbol="AAPL", event_date=_EVENT_DATE)
    await db_session.commit()

    svc = UsEarningsCoverageService(db_session)
    m = await svc.measure(
        from_date=date(2025, 7, 1),
        to_date=date(2025, 7, 31),
        today=date(2025, 8, 31),
    )
    assert m.realized_events == 1
    assert m.eligible_events == 1
    assert m.events_with_bars_present == 1
    assert m.events_with_zero_bars == 0
    assert m.window_coverage_p50 >= MIN_WINDOW_COVERAGE
    assert m.joinable_symbols == 1
    assert m.intraday_excluded_events == 0
    assert m.date_only_ratio == 1.0
    assert m.session_calendar_present is True


@pytest.mark.integration
@pytest.mark.asyncio
async def test_measure_event_without_bars_counts_zero(db_session):
    from sqlalchemy import text

    from app.services.market_events.us_earnings_coverage import (
        UsEarningsCoverageService,
    )

    await db_session.execute(text(_US_CANDLES_DDL))
    await _clean_finnhub_us_earnings(
        db_session, from_date=date(2025, 7, 1), to_date=date(2025, 7, 31)
    )
    await _seed_released_event(
        db_session, symbol="NOBARS", event_date=_EVENT_DATE, time_hint="after_close"
    )
    await db_session.commit()

    svc = UsEarningsCoverageService(db_session)
    m = await svc.measure(
        from_date=date(2025, 7, 1),
        to_date=date(2025, 7, 31),
        today=date(2025, 8, 31),
    )
    assert m.realized_events == 1
    assert m.events_with_zero_bars == 1
    assert m.events_with_bars_present == 0

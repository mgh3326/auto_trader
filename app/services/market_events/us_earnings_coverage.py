"""Read-only US earnings event -> daily-bar join-coverage measurement (ROB-371).

Measures, for realized Finnhub US earnings events, how well the event ->
``-5..+20d`` daily window joins against ``us_candles_1d`` (KIS primary + Yahoo
fallback), plus survivorship and benchmark coverage. Emits a counts-only
:class:`CoverageMeasurement` (no raw bars / symbols escape) for the §5 gate.

The §5 logic lives in the pure :func:`aggregate_coverage` (unit-tested, no DB);
:class:`UsEarningsCoverageService` is the thin DB-backed wiring that fetches the
inputs and delegates. All reads are read-only; the only network call is the
opt-in delisted-recoverability probe (Yahoo fallback), which performs no DB
write and never raises.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, time, timedelta
from statistics import median

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.market_events import MarketEvent
from app.models.us_symbol_universe import USSymbolUniverse
from app.services.daily_candles.repository import DailyCandlesRepository
from app.services.market_events.coverage_gate import CoverageMeasurement
from app.services.market_events.earnings_decision_time import (
    label_earnings_decision_time,
)
from app.services.market_events.session_calendar import (
    Market,
    trading_sessions_in_range,
)

logger = logging.getLogger(__name__)

# US benchmark set: SPY + GICS sector SPDRs. The symbols are an explicit
# hard-coded list (NOT resolved from the common-stock universe — ETFs are
# is_common_stock=False/NULL and may be absent from it). Their bars are READ from
# the pre-materialized us_candles_1d store below (the probe never live-fetches bar
# data during the gate), so each benchmark symbol must already be backfilled.
BENCHMARK_SYMBOLS: tuple[str, ...] = (
    "SPY",
    "XLK",
    "XLF",
    "XLE",
    "XLV",
    "XLI",
    "XLY",
    "XLP",
    "XLU",
    "XLB",
    "XLRE",
    "XLC",
)

WINDOW_LOOKBACK_DAYS = 5
WINDOW_LOOKAHEAD_DAYS = 20
MIN_WINDOW_COVERAGE = 0.90

_BMO = "before_open"
_AMC = "after_close"
_INTRADAY = "during_market"

# UTC-deterministic per-session bar presence. DATE(timestamptz) would convert in
# the DB session timezone; (time AT TIME ZONE 'UTC')::date pins it to UTC.
_WINDOW_SQL = text(
    """
    SELECT (time AT TIME ZONE 'UTC')::date AS bar_date,
           MAX(CASE WHEN volume > 0 THEN 1 ELSE 0 END) AS has_volume
    FROM us_candles_1d
    WHERE symbol = :symbol
      AND time >= :start_ts
      AND time < :end_ts
    GROUP BY (time AT TIME ZONE 'UTC')::date
    """
)


def _window_bounds(start: date, end: date) -> tuple[datetime, datetime]:
    """Half-open UTC bracket ``[start 00:00, end+1 00:00)`` for ``[start, end]``."""
    start_ts = datetime.combine(start, time.min, tzinfo=UTC)
    end_ts = datetime.combine(end + timedelta(days=1), time.min, tzinfo=UTC)
    return start_ts, end_ts


def aggregate_coverage(
    *,
    events: list[tuple[str, date, str | None]],
    total_released: int,
    window_present: dict[tuple[str, date], tuple[set[date], set[date]]],
    benchmark_present: dict[str, set[date]],
    delisted_symbols: set[str],
    delisted_recoverable: int,
    session_calendar_present: bool,
    market: Market = "us",
) -> CoverageMeasurement:
    """Pure §5 aggregation. ``window_present`` maps (symbol, event_date) ->
    (present bar-dates, volume>0 bar-dates), where the caller fetched those bars
    around the event's **lookahead-safe decision session**; ``benchmark_present``
    maps each benchmark symbol -> its present bar-dates over the full span.
    Expected sessions are the -5..+20 window around the decision session (the
    next tradable session for AMC/unknown), so date-only earnings are never
    treated as intraday-tradable on event_date. Unmappable events are counted and
    fail closed.

    Intraday (``during_market``) events are **excluded** from the eligible
    daily-granularity population (ROB-378): they are counted in
    ``intraday_excluded_events`` but never measured, joined, or placed in the
    coverage/benchmark denominator. ``eligible_events`` = realized minus
    intraday-excluded; all join-quality counts are over the eligible set. The
    labeling logic is untouched, so lookahead safety is preserved."""
    realized_events = len(events)
    null_symbol_count = max(total_released - realized_events, 0)
    dup_ambiguous_ratio = (
        null_symbol_count / total_released if total_released > 0 else 0.0
    )

    labels = [label_earnings_decision_time(ed, th, market) for (_s, ed, th) in events]
    intraday_excluded_events = sum(1 for lbl in labels if lbl.is_intraday_rejected)
    eligible_events = realized_events - intraday_excluded_events
    unmappable_events = sum(1 for lbl in labels if lbl.decision_session is None)
    unknown_count = sum(
        1 for (_s, _ed, th) in events if th not in (_BMO, _AMC, _INTRADAY)
    )
    unknown_time_ratio = unknown_count / realized_events if realized_events else 0.0
    # Finnhub equity earnings carry no release_time_utc -> date-only by design.
    date_only_ratio = 1.0 if realized_events else 0.0

    # Windows are anchored on the lookahead-safe DECISION SESSION (the first bar a
    # backtest could trade on the news), not the raw event_date — for AMC/unknown
    # this is the next tradable session. Cached per decision session.
    expected_cache: dict[date, set[date]] = {}

    def _expected(decision_session: date) -> set[date]:
        cached = expected_cache.get(decision_session)
        if cached is None:
            cached = set(
                trading_sessions_in_range(
                    market,
                    decision_session - timedelta(days=WINDOW_LOOKBACK_DAYS),
                    decision_session + timedelta(days=WINDOW_LOOKAHEAD_DAYS),
                )
            )
            expected_cache[decision_session] = cached
        return cached

    coverages: list[float] = []
    events_with_bars_present = 0
    joinable_events = 0
    joinable_symbols: set[str] = set()
    symbol_has_volume: dict[str, bool] = {}

    for (sym, ed, _th), label in zip(events, labels, strict=True):
        if label.is_intraday_rejected:
            # Excluded from the eligible daily-granularity population (ROB-378):
            # intraday timing cannot be pinned to a lookahead-safe daily bar, so
            # it is counted in intraday_excluded_events but never measured,
            # joined, or placed in the coverage denominator.
            continue
        decision = label.decision_session
        if decision is None:
            # Unmappable: no lookahead-safe window -> fail closed (0.0 coverage,
            # never joinable; never silently use the raw event_date window).
            coverages.append(0.0)
            continue
        expected = _expected(decision)
        present, volume = window_present.get((sym, ed), (set(), set()))
        if present:
            events_with_bars_present += 1
        coverage = len(expected & present) / len(expected) if expected else 0.0
        coverages.append(coverage)
        if coverage >= MIN_WINDOW_COVERAGE:
            joinable_events += 1
            joinable_symbols.add(sym)
        has_vol = bool(volume & expected)
        symbol_has_volume[sym] = symbol_has_volume.get(sym, False) or has_vol

    events_with_zero_bars = eligible_events - events_with_bars_present
    window_coverage_p50 = median(coverages) if coverages else 0.0

    if joinable_symbols:
        tradable = sum(1 for s in joinable_symbols if symbol_has_volume.get(s))
        tradability_coverage = tradable / len(joinable_symbols)
    else:
        tradability_coverage = 0.0

    benchmark_covered = 0
    for (_sym, _ed, _th), label in zip(events, labels, strict=True):
        if label.is_intraday_rejected:
            # Excluded from the eligible population (ROB-378), so also excluded
            # from the benchmark-coverage denominator below.
            continue
        decision = label.decision_session
        if decision is None:
            continue
        expected = _expected(decision)
        if not expected:
            continue
        for bdates in benchmark_present.values():
            if len(expected & bdates) / len(expected) >= MIN_WINDOW_COVERAGE:
                benchmark_covered += 1
                break
    benchmark_coverage = benchmark_covered / eligible_events if eligible_events else 0.0

    delisted_events = sum(1 for (sym, _ed, _th) in events if sym in delisted_symbols)

    return CoverageMeasurement(
        realized_events=realized_events,
        eligible_events=eligible_events,
        events_with_bars_present=events_with_bars_present,
        events_with_zero_bars=events_with_zero_bars,
        joinable_events=joinable_events,
        joinable_symbols=len(joinable_symbols),
        window_coverage_p50=round(window_coverage_p50, 4),
        date_only_ratio=date_only_ratio,
        unknown_time_ratio=round(unknown_time_ratio, 4),
        intraday_excluded_events=intraday_excluded_events,
        dup_ambiguous_ratio=round(dup_ambiguous_ratio, 4),
        tradability_coverage=round(tradability_coverage, 4),
        benchmark_coverage=round(benchmark_coverage, 4),
        delisted_events=delisted_events,
        delisted_recoverable=delisted_recoverable,
        session_calendar_present=session_calendar_present,
        unmappable_events=unmappable_events,
    )


class UsEarningsCoverageService:
    """Read-only coverage measurement against ``us_candles_1d``."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db
        self._repo = DailyCandlesRepository(session=db)

    async def measure(
        self,
        *,
        from_date: date,
        to_date: date,
        today: date | None = None,
        measure_delisted_recoverability: bool = False,
        delisted_sample: int = 10,
    ) -> CoverageMeasurement:
        today = today or datetime.now(UTC).date()

        rows = (
            await self._db.execute(
                select(
                    MarketEvent.symbol,
                    MarketEvent.event_date,
                    MarketEvent.time_hint,
                ).where(
                    MarketEvent.source == "finnhub",
                    MarketEvent.category == "earnings",
                    MarketEvent.market == "us",
                    MarketEvent.status == "released",
                    MarketEvent.event_date >= from_date,
                    MarketEvent.event_date <= to_date,
                    MarketEvent.event_date <= today,
                )
            )
        ).all()
        total_released = len(rows)
        events: list[tuple[str, date, str | None]] = [
            (r.symbol, r.event_date, r.time_hint) for r in rows if r.symbol
        ]

        # Fetch each event's bars around its lookahead-safe DECISION SESSION (the
        # next tradable session for AMC/unknown), matching aggregate_coverage's
        # window anchor. Unmappable events (no decision session) get no query.
        window_present: dict[tuple[str, date], tuple[set[date], set[date]]] = {}
        for sym, ed, th in events:
            decision = label_earnings_decision_time(ed, th, "us").decision_session
            if decision is None:
                continue
            window_present[(sym, ed)] = await self._present_bar_dates(
                sym,
                decision - timedelta(days=WINDOW_LOOKBACK_DAYS),
                decision + timedelta(days=WINDOW_LOOKAHEAD_DAYS),
            )

        # Benchmark span covers the latest decision-anchored window. Decision
        # sessions can sit a few sessions past to_date (AMC near the boundary), so
        # extend the span end by an extra buffer beyond the +20d lookahead.
        span_start = from_date - timedelta(days=WINDOW_LOOKBACK_DAYS)
        span_end = to_date + timedelta(days=WINDOW_LOOKAHEAD_DAYS + 10)
        benchmark_present: dict[str, set[date]] = {}
        for bsym in BENCHMARK_SYMBOLS:
            present, _vol = await self._present_bar_dates(bsym, span_start, span_end)
            benchmark_present[bsym] = present

        symbols = {sym for (sym, _ed, _th) in events}
        delisted_symbols = await self._delisted_symbols(symbols)

        delisted_recoverable = 0
        if measure_delisted_recoverability and delisted_symbols:
            delisted_recoverable = await self._measure_delisted_recoverability(
                sorted(delisted_symbols)[:delisted_sample]
            )

        session_calendar_present = bool(
            trading_sessions_in_range("us", date(2025, 7, 1), date(2025, 7, 8))
        )

        return aggregate_coverage(
            events=events,
            total_released=total_released,
            window_present=window_present,
            benchmark_present=benchmark_present,
            delisted_symbols=delisted_symbols,
            delisted_recoverable=delisted_recoverable,
            session_calendar_present=session_calendar_present,
        )

    async def _present_bar_dates(
        self, symbol: str, start: date, end: date
    ) -> tuple[set[date], set[date]]:
        start_ts, end_ts = _window_bounds(start, end)
        result = await self._db.execute(
            _WINDOW_SQL, {"symbol": symbol, "start_ts": start_ts, "end_ts": end_ts}
        )
        present: set[date] = set()
        volume_dates: set[date] = set()
        for bar_date, has_volume in result:
            present.add(bar_date)
            if has_volume:
                volume_dates.add(bar_date)
        return present, volume_dates

    async def _delisted_symbols(self, symbols: set[str]) -> set[str]:
        if not symbols:
            return set()
        rows = (
            (
                await self._db.execute(
                    select(USSymbolUniverse.symbol).where(
                        USSymbolUniverse.symbol.in_(symbols),
                        USSymbolUniverse.is_active.is_(False),
                    )
                )
            )
            .scalars()
            .all()
        )
        return set(rows)

    async def _measure_delisted_recoverability(self, sample: list[str]) -> int:
        """Bounded, read-only Yahoo probe. Network errors -> non-recoverable."""
        from app.services.daily_candles.yahoo_us_fallback import (
            fetch_us_daily_yahoo_fallback,
        )

        recovered = 0
        for sym in sample:
            try:
                rows = await fetch_us_daily_yahoo_fallback(symbol=sym, n=30)
            except Exception:  # noqa: BLE001 - network/quota -> treat as no recovery
                logger.warning("delisted recoverability probe failed for %s", sym)
                rows = []
            if rows:
                recovered += 1
        return recovered

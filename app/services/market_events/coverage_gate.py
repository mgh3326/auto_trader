"""ROB-367 §5 deterministic coverage gate for US earnings (ROB-371).

Pure classifier: a :class:`CoverageMeasurement` (counts/ratios only, no raw
bars) in, a :class:`GateResult` (overall PASS/FAIL + per-criterion breakdown)
out. No I/O, no DB.

The verdict explicitly distinguishes three FAIL shapes so an unbuilt dev store
is never mis-reported as a data-quality problem:
* ``realized_events == 0``                          -> "no earnings in range"
* events present but every window empty             -> "coverage not materialized"
* a §5 threshold genuinely missed                   -> "thresholds not met"

The §5 criteria mirror ``docs/runbooks/rob-367-event-driven-equity-data-feasibility.md``
section 5. ``date_only_ratio`` / ``unknown_time_ratio`` are recorded for
transparency but do NOT independently gate: per §5, any ratio is accepted for
equities once intraday labeling is forbidden, which the ``no_intraday_labeling``
criterion enforces directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CoverageMeasurement:
    """Counts-only coverage measurement. Every field is a scalar — no symbol or
    bar-date collections — so serializing it can never leak raw data."""

    realized_events: int
    events_with_bars_present: int
    events_with_zero_bars: int
    joinable_events: int
    joinable_symbols: int
    window_coverage_p50: float
    date_only_ratio: float
    unknown_time_ratio: float
    intraday_labeled_events: int
    dup_ambiguous_ratio: float
    tradability_coverage: float
    benchmark_coverage: float
    delisted_events: int
    delisted_recoverable: int
    session_calendar_present: bool


@dataclass(frozen=True)
class Section5Thresholds:
    min_realized_events: int = 500
    min_joinable_symbols: int = 200
    min_joinable_event_ratio: float = 0.90
    max_dup_ambiguous: float = 0.01
    min_tradability: float = 0.90
    min_benchmark: float = 0.90
    require_session_calendar: bool = True


@dataclass(frozen=True)
class GateCriterion:
    name: str
    observed: float | int | bool
    threshold: float | int | bool
    passed: bool
    note: str = ""


@dataclass(frozen=True)
class GateResult:
    passed: bool
    verdict: str
    criteria: list[GateCriterion] = field(default_factory=list)


def _ratio(numer: int, denom: int) -> float:
    return numer / denom if denom > 0 else 0.0


def evaluate_section5_gate(m: CoverageMeasurement, t: Section5Thresholds) -> GateResult:
    joinable_event_ratio = _ratio(m.joinable_events, m.realized_events)
    no_events = m.realized_events == 0
    not_materialized = m.realized_events > 0 and m.events_with_bars_present == 0

    criteria = [
        GateCriterion(
            "min_realized_events",
            m.realized_events,
            t.min_realized_events,
            m.realized_events >= t.min_realized_events,
        ),
        GateCriterion(
            "min_joinable_symbols",
            m.joinable_symbols,
            t.min_joinable_symbols,
            m.joinable_symbols >= t.min_joinable_symbols,
        ),
        GateCriterion(
            "min_joinable_event_ratio",
            round(joinable_event_ratio, 4),
            t.min_joinable_event_ratio,
            joinable_event_ratio >= t.min_joinable_event_ratio,
            note="fraction of realized events with >=90% window join coverage",
        ),
        GateCriterion(
            "no_intraday_labeling",
            m.intraday_labeled_events,
            0,
            m.intraday_labeled_events == 0,
            note="intraday labeling is forbidden (ROB-367 boundary)",
        ),
        GateCriterion(
            "max_dup_ambiguous",
            m.dup_ambiguous_ratio,
            t.max_dup_ambiguous,
            m.dup_ambiguous_ratio <= t.max_dup_ambiguous,
            note="US Finnhub: NULL-symbol ratio (dedup enforced by unique index)",
        ),
        GateCriterion(
            "min_tradability",
            m.tradability_coverage,
            t.min_tradability,
            m.tradability_coverage >= t.min_tradability,
            note="fraction of joinable symbols with >=1 volume>0 bar",
        ),
        GateCriterion(
            "min_benchmark",
            m.benchmark_coverage,
            t.min_benchmark,
            m.benchmark_coverage >= t.min_benchmark,
            note="fraction of events with >=1 benchmark ETF window coverage >=90%",
        ),
        GateCriterion(
            "session_calendar_present",
            m.session_calendar_present,
            t.require_session_calendar,
            (m.session_calendar_present or not t.require_session_calendar),
        ),
    ]

    passed = all(c.passed for c in criteria) and not not_materialized and not no_events

    # NOTE: verdict keywords PASS / FAIL are machine-parsed by operators and log
    # dashboards; do not refactor the prefixes without updating the runbook.
    if no_events:
        verdict = (
            "FAIL — no earnings events found in the date range. Extend the "
            "window or wait for events to release; this is not a quality failure."
        )
    elif not_materialized:
        verdict = (
            "FAIL — coverage not materialized: "
            f"{m.realized_events} realized events but 0 have daily bars. "
            "If backfill is pending, materialize the window with "
            "scripts/backfill_daily_candles.py --market us against a dev DB and "
            "re-probe. Otherwise some symbols (delisted / penny) may lack "
            f"history (delisted events={m.delisted_events}, "
            f"Yahoo-recoverable={m.delisted_recoverable}). "
            "This is a build gap, not a join-quality failure."
        )
    elif passed:
        verdict = (
            "PASS — §5 thresholds met; a bounded US event-response backtest "
            "issue MAY be opened. (This issue does NOT open it.)"
        )
    else:
        missing = ", ".join(c.name for c in criteria if not c.passed)
        verdict = f"FAIL — §5 thresholds not met: {missing}."

    return GateResult(passed=passed, verdict=verdict, criteria=criteria)

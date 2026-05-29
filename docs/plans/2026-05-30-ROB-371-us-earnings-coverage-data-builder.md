# ROB-371 — US Earnings Event+Price Coverage Data-Builder — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a fail-closed NYSE session+holiday calendar, a lookahead-safe date-only→decision-session labeler for Finnhub earnings, and an operator-run read-only coverage probe that measures the `us_candles_1d` event→`-5d..+20d` join against the ROB-367 §5 thresholds and emits a deterministic PASS/FAIL artifact. **No strategy, no backtest, no signal.**

**Architecture:** Reusable deterministic primitives live in `app/services/market_events/` (session calendar, decision-time labeler, coverage-measurement service, pure §5 gate classifier). The operator shell is `scripts/probe_us_earnings_coverage.py` — read-only by default, with a separately double-gated `--backfill-window` mode (dev DB only). Artifacts land under `AUTO_TRADER_RESEARCH_ARTIFACT_ROOT` (env) or a gitignored `research/event_coverage/results/` fallback; counts-only, never raw bars.

**Tech Stack:** Python 3.13, `exchange_calendars` (XNYS/XKRX — **already a dependency**, `>=4.7,<5.0`), SQLAlchemy async (`AsyncSessionLocal`), `app/services/daily_candles` (`DailyCandlesRepository`, `DailyCandleSyncService`, Yahoo fallback), `app/services/market_events` (`MarketEventsQueryService`), pytest (`@pytest.mark.unit` / `@pytest.mark.integration`).

**Decisions locked (from exploration `wf_f0a949b2-9b1`):**
- D1. **Holiday calendar = extend `exchange_calendars`** (XNYS/XKRX), lazy-import inside functions, mirror `app/tasks/invest_screener_snapshot_tasks.py::is_market_session_today`. **No new dependency, no hardcoded holiday table.**
- D2. **Fail-closed = out-of-calendar-range / library error → "not a trading session"**, and the labeler flags such events `unmappable` (never silently picks a wrong session).
- D3. **Date-range candle reads use custom SQL via `repository.session`** — `DailyCandlesRepository.fetch_recent` is count-based, there is no date-range read method.
- D4. **Survivorship**: symbols are never deleted, delisting = `is_active=False` (row kept); `is_common_stock` NULL = unclassified (conservatively excluded). Delisted-bar recoverability is **measured via Yahoo fallback**, not assumed.
- D5. **Benchmark symbols are fetched directly** (SPY + GICS sector SPDRs), NOT via the universe — ETFs are `is_common_stock=False/NULL` and may be absent from the KIS-sourced universe.
- D6. **Probe default path is strictly read-only.** Backfill is a separate `--backfill-window --confirm` double-gate that writes `us_candles_1d` via `DailyCandleSyncService.sync_one` (count-based `horizon_bars`); the artifact records whether backfill ran. Operators point `DATABASE_URL` at a dev DB; the script prints a loud DEV-DB warning and never targets prod by itself.
- D7. **FALSE-FAIL guard**: the probe reports `events_with_bars_present` vs `events_with_zero_bars` so "0 coverage because nothing materialized yet" is never reported as a real join failure. The gate verdict states which.
- D8. **Artifact location** = dedicated `research/event_coverage/artifact_paths.py` (mirrors the scalping module's env contract but its own `results/` fallback, to avoid coupling earnings coverage to `nautilus_scalping`). New `.gitignore` rule keeps the fallback out of git.

**Out of scope / safety (verbatim from issue):** no strategy/backtest/sweep/signal; no broker/order/watch/order-intent/approval/trade-journal mutation; no scheduler/TaskIQ/Prefect/cron; no prod DB write; no secrets/raw-data committed. This issue does **not** open a backtest issue — it only emits a PASS/FAIL verdict.

---

## File Structure

**New — app primitives (reusable, tested):**
- `app/services/market_events/session_calendar.py` — fail-closed XNYS/XKRX session calendar (`is_trading_session`, `next_trading_session`, `previous_trading_session`, `trading_sessions_in_range`).
- `app/services/market_events/earnings_decision_time.py` — `label_earnings_decision_time(event_date, time_hint, market) -> EarningsDecisionLabel`; date-only→lookahead-safe decision session; intraday rejected.
- `app/services/market_events/us_earnings_coverage.py` — read-only `UsEarningsCoverageService.measure(...) -> CoverageMeasurement` (counts only).
- `app/services/market_events/coverage_gate.py` — pure `evaluate_section5_gate(measurement, thresholds) -> GateResult`.

**New — research artifact location:**
- `research/event_coverage/__init__.py`
- `research/event_coverage/artifact_paths.py` — `event_coverage_artifact_root()`, `coverage_artifact_path(*parts)`.

**New — operator CLI:**
- `scripts/probe_us_earnings_coverage.py` — read-only probe + opt-in delisted-recoverability + double-gated backfill.

**Modified:**
- `app/services/market_events/expected_sources.py` — `expected_sources_for_date` becomes holiday-aware via `session_calendar` (closes the `:11-14` follow-up).
- `.gitignore` — add `research/event_coverage/results/`.

**New — docs:**
- `docs/runbooks/rob-371-us-earnings-coverage-probe.md` — operator runbook (how to RUN, flags, dev-DB safety, verdict interpretation).

**New — tests:**
- `tests/services/market_events/test_session_calendar.py` (unit)
- `tests/services/market_events/test_earnings_decision_time.py` (unit)
- `tests/services/market_events/test_coverage_gate.py` (unit)
- `tests/services/market_events/test_us_earnings_coverage.py` (integration, DB-seeded)
- `tests/services/test_market_events_expected_sources.py` (MODIFY — add holiday cases)
- `tests/research/test_event_coverage_artifact_paths.py` (unit)
- `tests/scripts/test_probe_us_earnings_coverage_cli.py` (unit — `--help`/dry-run run without secrets; verdict exit codes)

---

## PR slicing

- **PR1 (foundation):** Tasks 1–3 — session calendar, `expected_sources` refactor, decision-time labeler, artifact_paths + gitignore. Pure/near-pure, fully unit-tested. Self-contained.
- **PR2 (probe + gate):** Tasks 4–7 — coverage measurement service, §5 gate, CLI, runbook, integration tests. Depends on PR1.

Each PR ends green on `ruff check app/ tests/` + targeted pytest + (pre-merge) the full Test workflow.

---

## Task 1: Fail-closed NYSE/KRX session calendar

**Files:**
- Create: `app/services/market_events/session_calendar.py`
- Test: `tests/services/market_events/test_session_calendar.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/services/market_events/test_session_calendar.py
from __future__ import annotations

from datetime import date

import pytest

from app.services.market_events.session_calendar import (
    is_trading_session,
    next_trading_session,
    previous_trading_session,
    trading_sessions_in_range,
)


@pytest.mark.unit
def test_weekend_is_not_a_session():
    assert is_trading_session("us", date(2026, 5, 9)) is False   # Saturday
    assert is_trading_session("us", date(2026, 5, 10)) is False  # Sunday


@pytest.mark.unit
def test_us_holiday_is_not_a_session():
    # 2025-07-04 Independence Day (observed) — XNYS closed.
    assert is_trading_session("us", date(2025, 7, 4)) is False
    # 2025-12-25 Christmas — XNYS closed.
    assert is_trading_session("us", date(2025, 12, 25)) is False


@pytest.mark.unit
def test_us_regular_weekday_is_a_session():
    assert is_trading_session("us", date(2025, 7, 7)) is True  # Monday, open


@pytest.mark.unit
def test_kr_holiday_is_not_a_session():
    # 2025-01-01 New Year — XKRX closed.
    assert is_trading_session("kr", date(2025, 1, 1)) is False


@pytest.mark.unit
def test_out_of_range_date_fails_closed():
    # Far future beyond the calendar's precomputed range -> not a session,
    # never an exception.
    assert is_trading_session("us", date(2100, 1, 4)) is False


@pytest.mark.unit
def test_next_and_previous_skip_holiday_and_weekend():
    # Thu 2025-07-03 session; Fri 2025-07-04 holiday; next session Mon 2025-07-07.
    assert next_trading_session("us", date(2025, 7, 3)) == date(2025, 7, 7)
    assert previous_trading_session("us", date(2025, 7, 7)) == date(2025, 7, 3)


@pytest.mark.unit
def test_next_trading_session_unresolvable_returns_none():
    assert next_trading_session("us", date(2100, 1, 1)) is None


@pytest.mark.unit
def test_trading_sessions_in_range_excludes_weekends_holidays():
    sessions = trading_sessions_in_range("us", date(2025, 7, 1), date(2025, 7, 8))
    # Jul 1,2,3 (Tue-Thu), 4 holiday, 5-6 weekend, 7,8 (Mon-Tue)
    assert date(2025, 7, 4) not in sessions
    assert date(2025, 7, 5) not in sessions
    assert date(2025, 7, 3) in sessions
    assert date(2025, 7, 7) in sessions
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/services/market_events/test_session_calendar.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

```python
# app/services/market_events/session_calendar.py
"""Fail-closed XNYS / XKRX session+holiday calendar (ROB-371).

Wraps :mod:`exchange_calendars` (already a project dependency, used by
``invest_screener_snapshot_tasks`` and ``invest_screener_snapshots.freshness``).
``exchange_calendars`` is imported lazily so importing this module stays cheap.

Fail-closed contract (ROB-367 §5 / ROB-371 D2): any date the calendar cannot
classify — out of its precomputed range, or any library error — is treated as
**not a trading session**. Lookahead-safe labeling must never leak across a
session it could not positively confirm is open.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Literal

Market = Literal["us", "kr"]

_CALENDAR_NAME: dict[str, str] = {"us": "XNYS", "kr": "XKRX"}

# Bounded search horizon for next/previous session lookups. 10 calendar days
# covers the worst-case US holiday gap (Thanksgiving Wed -> following Mon);
# we use a generous 16 to also span KR lunar-holiday clusters.
_SESSION_SEARCH_DAYS = 16


def _calendar(market: Market):
    import exchange_calendars as xcals

    try:
        return xcals.get_calendar(_CALENDAR_NAME[market])
    except KeyError as exc:  # unknown market key -> programmer error
        raise ValueError(f"unsupported market {market!r}") from exc


def is_trading_session(market: Market, day: date) -> bool:
    """True iff ``day`` is a trading session on the market's exchange.

    Fail-closed: out-of-range dates and any calendar error return ``False``.
    """
    import pandas as pd

    cal = _calendar(market)
    try:
        return bool(cal.is_session(pd.Timestamp(day)))
    except Exception:  # noqa: BLE001 - fail-closed on any calendar error
        return False


def next_trading_session(market: Market, day: date) -> date | None:
    """First trading session strictly after ``day`` within a bounded horizon.

    Returns ``None`` if none can be confirmed (fail-closed / out of range).
    """
    for offset in range(1, _SESSION_SEARCH_DAYS + 1):
        candidate = day + timedelta(days=offset)
        if is_trading_session(market, candidate):
            return candidate
    return None


def previous_trading_session(market: Market, day: date) -> date | None:
    """Last trading session strictly before ``day`` within a bounded horizon."""
    for offset in range(1, _SESSION_SEARCH_DAYS + 1):
        candidate = day - timedelta(days=offset)
        if is_trading_session(market, candidate):
            return candidate
    return None


def trading_sessions_in_range(market: Market, start: date, end: date) -> list[date]:
    """Trading-session dates in the inclusive ``[start, end]`` range.

    Empty when the range is out of the calendar's bounds (fail-closed).
    """
    import pandas as pd

    if end < start:
        return []
    cal = _calendar(market)
    try:
        sessions = cal.sessions_in_range(pd.Timestamp(start), pd.Timestamp(end))
    except Exception:  # noqa: BLE001 - fail-closed
        return []
    return [ts.date() for ts in sessions]
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/services/market_events/test_session_calendar.py -v`
Expected: PASS (all 8).

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check app/services/market_events/session_calendar.py tests/services/market_events/test_session_calendar.py
git add app/services/market_events/session_calendar.py tests/services/market_events/test_session_calendar.py
git commit -m "feat(ROB-371): fail-closed XNYS/XKRX session calendar"
```

---

## Task 2: Make `expected_sources_for_date` holiday-aware (close the `:11-14` follow-up)

**Files:**
- Modify: `app/services/market_events/expected_sources.py:41-54`
- Modify: `tests/services/test_market_events_expected_sources.py`

- [ ] **Step 1: Add failing holiday tests**

```python
# tests/services/test_market_events_expected_sources.py  (append)
@pytest.mark.unit
def test_us_holiday_drops_finnhub_us_but_keeps_global():
    # 2025-07-04 Independence Day — XNYS closed, XKRX open.
    triples = expected_sources_for_date(date(2025, 7, 4))
    assert ("finnhub", "earnings", "us") not in triples
    assert ("forexfactory", "economic", "global") in triples


@pytest.mark.unit
def test_kr_holiday_drops_kr_sources():
    # 2025-01-01 New Year — XKRX closed.
    triples = expected_sources_for_date(date(2025, 1, 1))
    assert ("dart", "disclosure", "kr") not in triples
    assert ("wisefn", "earnings", "kr") not in triples


@pytest.mark.unit
def test_regular_weekday_keeps_all_session_sources():
    # 2025-07-07 Monday — both exchanges open.
    triples = expected_sources_for_date(date(2025, 7, 7))
    assert ("finnhub", "earnings", "us") in triples
    assert ("dart", "disclosure", "kr") in triples
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/services/test_market_events_expected_sources.py -v`
Expected: FAIL — current code only checks weekends, so US holiday still includes finnhub/us.

- [ ] **Step 3: Refactor `expected_sources_for_date`**

Replace lines 41-54 with:

```python
def expected_sources_for_date(target_date: date) -> frozenset[tuple[str, str, str]]:
    """Return the subset of EXPECTED_SOURCES expected to have non-empty data on `target_date`.

    Session-aware (ROB-371): US sources (finnhub) are gated on the NYSE (XNYS)
    trading calendar and KR sources (dart, wisefn) on the KRX (XKRX) calendar —
    both weekend- and holiday-aware via :mod:`app.services.market_events.session_calendar`.
    On an exchange holiday those sources are simply "not expected" (no false
    "missing" signal). ForexFactory publishes a weekly XML and is expected every
    day. Fail-closed: a day the calendar cannot confirm open is treated as
    closed, so we never claim a source is expected on a non-session day.
    """
    from app.services.market_events.session_calendar import is_trading_session

    triples: set[tuple[str, str, str]] = {("forexfactory", "economic", "global")}
    if is_trading_session("us", target_date):
        triples.add(("finnhub", "earnings", "us"))
    if is_trading_session("kr", target_date):
        triples.add(("dart", "disclosure", "kr"))
        triples.add(("wisefn", "earnings", "kr"))
    return frozenset(triples)
```

Also update the module docstring lines 11-15: replace the "We do not yet model KRX/NYSE observed holidays …" paragraph with a note that holidays are now modeled via `session_calendar` (XNYS/XKRX) and the follow-up is closed by ROB-371.

- [ ] **Step 4: Run to verify pass (and no regressions in this file)**

Run: `uv run pytest tests/services/test_market_events_expected_sources.py -v`
Expected: PASS (existing weekend tests + new holiday tests).

- [ ] **Step 5: Regression sweep on freshness consumers**

Run: `uv run pytest tests/services -k "freshness or expected_sources or calendar_coverage" -v`
Expected: PASS. If any test pins a specific holiday date as "expected", update it to reflect the now-correct holiday-aware behavior (document the change in the commit body).

- [ ] **Step 6: Lint + commit**

```bash
uv run ruff check app/services/market_events/expected_sources.py tests/services/test_market_events_expected_sources.py
git add app/services/market_events/expected_sources.py tests/services/test_market_events_expected_sources.py
git commit -m "fix(ROB-371): expected_sources is XNYS/XKRX holiday-aware (closes expected_sources :11-14 follow-up)"
```

---

## Task 3: Lookahead-safe date-only → decision-session labeler

**Files:**
- Create: `app/services/market_events/earnings_decision_time.py`
- Test: `tests/services/market_events/test_earnings_decision_time.py`

**Semantics (lookahead-safe, daily granularity; intraday forbidden):**
- `before_open` (BMO): news public before the open on `event_date`. First tradable reaction = `event_date`'s own session (if a trading day, else the next session). `anchor="next_open"`.
- `after_close` (AMC): news public after the close on `event_date`. First tradable reaction = the **next** trading session. `anchor="next_close"`.
- `during_market` (intraday): pinning a lookahead-safe open/close bar is impossible at daily granularity → `anchor="whole_day_uncertain"`, reaction session = next trading session (a clean full session strictly after the announcement), `is_lookahead_safe=True` but `is_intraday_rejected=True`.
- `unknown` / `None`: `anchor="whole_day_uncertain"`, reaction session = `event_date` session if trading else next; `is_lookahead_safe=True`.
- Any case where the calendar cannot resolve a session within bounds → `anchor="unmappable"`, `decision_session=None`, `is_lookahead_safe=False`.

- [ ] **Step 1: Write failing tests**

```python
# tests/services/market_events/test_earnings_decision_time.py
from __future__ import annotations

from datetime import date

import pytest

from app.services.market_events.earnings_decision_time import (
    label_earnings_decision_time,
)


@pytest.mark.unit
def test_bmo_on_trading_day_reacts_same_session_next_open():
    # 2025-07-07 Monday session.
    label = label_earnings_decision_time(date(2025, 7, 7), "before_open")
    assert label.decision_session == date(2025, 7, 7)
    assert label.anchor == "next_open"
    assert label.is_lookahead_safe is True


@pytest.mark.unit
def test_amc_reacts_next_session_next_close():
    # 2025-07-07 Mon AMC -> next session Tue 2025-07-08.
    label = label_earnings_decision_time(date(2025, 7, 7), "after_close")
    assert label.decision_session == date(2025, 7, 8)
    assert label.anchor == "next_close"
    assert label.is_lookahead_safe is True


@pytest.mark.unit
def test_amc_before_holiday_skips_to_next_open_session():
    # 2025-07-03 Thu AMC; 07-04 holiday, 05-06 weekend -> next session 07-07 Mon.
    label = label_earnings_decision_time(date(2025, 7, 3), "after_close")
    assert label.decision_session == date(2025, 7, 7)


@pytest.mark.unit
def test_bmo_on_holiday_moves_to_next_session():
    # BMO labeled on a holiday -> first actual session is the next one.
    label = label_earnings_decision_time(date(2025, 7, 4), "before_open")
    assert label.decision_session == date(2025, 7, 7)
    assert label.anchor == "next_open"


@pytest.mark.unit
def test_during_market_is_intraday_rejected_whole_day():
    label = label_earnings_decision_time(date(2025, 7, 7), "during_market")
    assert label.anchor == "whole_day_uncertain"
    assert label.is_intraday_rejected is True
    assert label.decision_session == date(2025, 7, 8)  # next clean full session


@pytest.mark.unit
def test_unknown_time_is_whole_day_uncertain():
    label = label_earnings_decision_time(date(2025, 7, 7), "unknown")
    assert label.anchor == "whole_day_uncertain"
    assert label.is_lookahead_safe is True
    label_none = label_earnings_decision_time(date(2025, 7, 7), None)
    assert label_none.anchor == "whole_day_uncertain"


@pytest.mark.unit
def test_out_of_range_event_is_unmappable():
    label = label_earnings_decision_time(date(2100, 1, 1), "before_open")
    assert label.anchor == "unmappable"
    assert label.decision_session is None
    assert label.is_lookahead_safe is False
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/services/market_events/test_earnings_decision_time.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

```python
# app/services/market_events/earnings_decision_time.py
"""Lookahead-safe date-only -> decision-session labeling for earnings (ROB-371).

Finnhub equity earnings are 100% date-only (``release_time_utc`` is always NULL;
only a BMO/AMC ``time_hint`` is available). To study event reactions without
lookahead bias we map each event to the first daily bar that could legitimately
trade on the news, at **daily granularity only** — intraday labeling is
forbidden (ROB-367 hard boundary).

Anchors:
* ``next_open``            — BMO: react on the event session's open.
* ``next_close``           — AMC: react on the next session's close.
* ``whole_day_uncertain``  — intraday/unknown timing; treat the next clean full
                             session as the reaction window (no open/close pin).
* ``unmappable``           — calendar could not confirm a session (fail-closed).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal

from app.services.market_events.session_calendar import (
    Market,
    is_trading_session,
    next_trading_session,
)

Anchor = Literal["next_open", "next_close", "whole_day_uncertain", "unmappable"]

# Finnhub time_hint values (taxonomy.TIME_HINTS).
_BMO = "before_open"
_AMC = "after_close"
_INTRADAY = "during_market"


@dataclass(frozen=True)
class EarningsDecisionLabel:
    event_date: date
    time_hint: str | None
    decision_session: date | None
    anchor: Anchor
    is_lookahead_safe: bool
    is_intraday_rejected: bool


def _event_or_next_session(market: Market, event_date: date) -> date | None:
    if is_trading_session(market, event_date):
        return event_date
    return next_trading_session(market, event_date)


def label_earnings_decision_time(
    event_date: date,
    time_hint: str | None,
    market: Market = "us",
) -> EarningsDecisionLabel:
    """Map a date-only earnings event to a lookahead-safe decision session."""

    def _unmappable() -> EarningsDecisionLabel:
        return EarningsDecisionLabel(
            event_date=event_date,
            time_hint=time_hint,
            decision_session=None,
            anchor="unmappable",
            is_lookahead_safe=False,
            is_intraday_rejected=False,
        )

    if time_hint == _BMO:
        session = _event_or_next_session(market, event_date)
        if session is None:
            return _unmappable()
        return EarningsDecisionLabel(
            event_date, time_hint, session, "next_open", True, False
        )

    if time_hint == _AMC:
        session = next_trading_session(market, event_date)
        if session is None:
            return _unmappable()
        return EarningsDecisionLabel(
            event_date, time_hint, session, "next_close", True, False
        )

    if time_hint == _INTRADAY:
        # Intraday timing cannot be pinned to a lookahead-safe daily bar; use the
        # next clean full session as the reaction window.
        session = next_trading_session(market, event_date)
        if session is None:
            return _unmappable()
        return EarningsDecisionLabel(
            event_date, time_hint, session, "whole_day_uncertain", True, True
        )

    # unknown / None -> whole-day uncertainty on the event session (or next).
    session = _event_or_next_session(market, event_date)
    if session is None:
        return _unmappable()
    return EarningsDecisionLabel(
        event_date, time_hint, session, "whole_day_uncertain", True, False
    )
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/services/market_events/test_earnings_decision_time.py -v`
Expected: PASS (all 7).

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check app/services/market_events/earnings_decision_time.py tests/services/market_events/test_earnings_decision_time.py
git add app/services/market_events/earnings_decision_time.py tests/services/market_events/test_earnings_decision_time.py
git commit -m "feat(ROB-371): lookahead-safe date-only earnings decision-session labeler"
```

---

## Task 3b: Research artifact location + gitignore

**Files:**
- Create: `research/event_coverage/__init__.py` (empty)
- Create: `research/event_coverage/artifact_paths.py`
- Modify: `.gitignore`
- Test: `tests/research/test_event_coverage_artifact_paths.py`

- [ ] **Step 1: Write failing test**

```python
# tests/research/test_event_coverage_artifact_paths.py
from __future__ import annotations

from pathlib import Path

import pytest

from research.event_coverage.artifact_paths import (
    ENV_VAR,
    coverage_artifact_path,
    event_coverage_artifact_root,
)


@pytest.mark.unit
def test_root_uses_env_when_set(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "/tmp/research-root")
    assert event_coverage_artifact_root() == Path("/tmp/research-root")


@pytest.mark.unit
def test_root_falls_back_to_repo_results_when_unset(monkeypatch):
    monkeypatch.delenv(ENV_VAR, raising=False)
    root = event_coverage_artifact_root()
    assert root.name == "results"
    assert "event_coverage" in str(root)


@pytest.mark.unit
def test_blank_env_falls_back(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "   ")
    assert event_coverage_artifact_root().name == "results"


@pytest.mark.unit
def test_coverage_artifact_path_joins_parts(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "/tmp/r")
    p = coverage_artifact_path("us_earnings_coverage.json")
    assert p == Path("/tmp/r/event_coverage/us_earnings_coverage.json")
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/research/test_event_coverage_artifact_paths.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

```python
# research/event_coverage/artifact_paths.py
"""ROB-371 — artifact location for the US earnings coverage probe.

Shares the ``AUTO_TRADER_RESEARCH_ARTIFACT_ROOT`` env contract with the rest of
the research tooling but keeps its own ``results/`` fallback so earnings
coverage output is never mixed into the ``nautilus_scalping`` namespace. Read via
plain ``os.environ`` only — never imports app Settings (research-only boundary).

The fallback ``research/event_coverage/results/`` is gitignored: coverage
artifacts (even counts-only) must not be committed.
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_VAR = "AUTO_TRADER_RESEARCH_ARTIFACT_ROOT"


def event_coverage_artifact_root() -> Path:
    """Env root if set (non-blank), else repo-internal gitignored ``results/``."""
    raw = os.environ.get(ENV_VAR)
    if raw is not None and raw.strip():
        return Path(raw.strip())
    return Path(__file__).resolve().parent / "results"


def coverage_artifact_path(*parts: str) -> Path:
    """``<root>/event_coverage/<*parts>``."""
    return event_coverage_artifact_root().joinpath("event_coverage", *parts)
```

```python
# research/event_coverage/__init__.py
# (empty package marker)
```

Append to `.gitignore`:

```
# ROB-371 US earnings coverage probe artifacts (counts-only, never committed)
research/event_coverage/results/
```

- [ ] **Step 4: Run to verify pass + gitignore confirmation**

Run: `uv run pytest tests/research/test_event_coverage_artifact_paths.py -v`
Expected: PASS (4).
Run: `git check-ignore research/event_coverage/results/x.json`
Expected: prints the path (ignored).

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check research/event_coverage tests/research
git add research/event_coverage .gitignore tests/research/test_event_coverage_artifact_paths.py
git commit -m "feat(ROB-371): event-coverage research artifact root (gitignored)"
```

> **PR1 boundary** — open PR with Tasks 1, 2, 3, 3b. Verify full `ruff check app/ tests/` + `uv run pytest tests/services/market_events tests/research tests/services/test_market_events_expected_sources.py` green, confirm CI Test workflow green before merge.

---

## Task 4: §5 threshold gate (pure classifier)

**Files:**
- Create: `app/services/market_events/coverage_gate.py`
- Test: `tests/services/market_events/test_coverage_gate.py`

The gate consumes a `CoverageMeasurement` (defined here as a frozen dataclass so the gate is independently testable; Task 5's service returns the same type).

- [ ] **Step 1: Write failing tests**

```python
# tests/services/market_events/test_coverage_gate.py
from __future__ import annotations

import pytest

from app.services.market_events.coverage_gate import (
    CoverageMeasurement,
    Section5Thresholds,
    evaluate_section5_gate,
)


def _passing_measurement(**overrides) -> CoverageMeasurement:
    base = dict(
        realized_events=600,
        events_with_bars_present=590,
        events_with_zero_bars=10,
        joinable_symbols=250,
        window_coverage_p50=0.98,
        date_only_ratio=1.0,
        unknown_time_ratio=0.05,
        intraday_labeled_events=0,
        dup_ambiguous_ratio=0.0,
        tradability_coverage=0.95,
        benchmark_coverage=0.97,
        delisted_events=40,
        delisted_recoverable=38,
        session_calendar_present=True,
    )
    base.update(overrides)
    return CoverageMeasurement(**base)


@pytest.mark.unit
def test_full_coverage_passes():
    result = evaluate_section5_gate(_passing_measurement(), Section5Thresholds())
    assert result.passed is True
    assert result.verdict.startswith("PASS")
    assert all(c.passed for c in result.criteria)


@pytest.mark.unit
def test_too_few_events_fails_that_criterion():
    result = evaluate_section5_gate(
        _passing_measurement(realized_events=120), Section5Thresholds()
    )
    assert result.passed is False
    failed = [c.name for c in result.criteria if not c.passed]
    assert "min_realized_events" in failed


@pytest.mark.unit
def test_intraday_labeled_events_hard_fail():
    # Any intraday-labeled event violates the "intraday forbidden" boundary.
    result = evaluate_section5_gate(
        _passing_measurement(intraday_labeled_events=3), Section5Thresholds()
    )
    assert result.passed is False
    assert any(c.name == "no_intraday_labeling" and not c.passed for c in result.criteria)


@pytest.mark.unit
def test_zero_bars_everywhere_reports_not_materialized_not_join_failure():
    # FALSE-FAIL guard: all events have zero bars -> verdict must say
    # "coverage not materialized", not "join failed".
    m = _passing_measurement(events_with_bars_present=0, events_with_zero_bars=600)
    result = evaluate_section5_gate(m, Section5Thresholds())
    assert result.passed is False
    assert "not materialized" in result.verdict.lower()


@pytest.mark.unit
def test_missing_session_calendar_fails():
    result = evaluate_section5_gate(
        _passing_measurement(session_calendar_present=False), Section5Thresholds()
    )
    assert result.passed is False
    assert any(c.name == "session_calendar_present" and not c.passed for c in result.criteria)


@pytest.mark.unit
def test_dup_ratio_above_one_percent_fails():
    result = evaluate_section5_gate(
        _passing_measurement(dup_ambiguous_ratio=0.02), Section5Thresholds()
    )
    assert result.passed is False
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/services/market_events/test_coverage_gate.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

```python
# app/services/market_events/coverage_gate.py
"""ROB-367 §5 deterministic coverage gate for US earnings (ROB-371).

Pure classifier: a :class:`CoverageMeasurement` (counts only, no raw bars) in,
a :class:`GateResult` (overall PASS/FAIL + per-criterion breakdown) out. No I/O,
no DB. The verdict explicitly distinguishes "coverage not materialized yet"
(events present but zero bars) from a genuine join failure, so an unbuilt dev
store is never mis-reported as a data-quality FAIL (ROB-371 D7).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CoverageMeasurement:
    realized_events: int
    events_with_bars_present: int
    events_with_zero_bars: int
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
    min_window_coverage: float = 0.90
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


def evaluate_section5_gate(
    m: CoverageMeasurement, t: Section5Thresholds
) -> GateResult:
    not_materialized = (
        m.realized_events > 0 and m.events_with_bars_present == 0
    )

    criteria = [
        GateCriterion(
            "min_realized_events", m.realized_events, t.min_realized_events,
            m.realized_events >= t.min_realized_events,
        ),
        GateCriterion(
            "min_joinable_symbols", m.joinable_symbols, t.min_joinable_symbols,
            m.joinable_symbols >= t.min_joinable_symbols,
        ),
        GateCriterion(
            "min_window_coverage", m.window_coverage_p50, t.min_window_coverage,
            m.window_coverage_p50 >= t.min_window_coverage,
            note="median per-event window join coverage",
        ),
        GateCriterion(
            "no_intraday_labeling", m.intraday_labeled_events, 0,
            m.intraday_labeled_events == 0,
            note="intraday labeling is forbidden (ROB-367 boundary)",
        ),
        GateCriterion(
            "max_dup_ambiguous", m.dup_ambiguous_ratio, t.max_dup_ambiguous,
            m.dup_ambiguous_ratio <= t.max_dup_ambiguous,
        ),
        GateCriterion(
            "min_tradability", m.tradability_coverage, t.min_tradability,
            m.tradability_coverage >= t.min_tradability,
        ),
        GateCriterion(
            "min_benchmark", m.benchmark_coverage, t.min_benchmark,
            m.benchmark_coverage >= t.min_benchmark,
        ),
        GateCriterion(
            "session_calendar_present", m.session_calendar_present,
            t.require_session_calendar,
            (m.session_calendar_present or not t.require_session_calendar),
        ),
    ]

    passed = all(c.passed for c in criteria) and not not_materialized

    if not_materialized:
        verdict = (
            "FAIL — coverage not materialized: "
            f"{m.realized_events} realized events but 0 have daily bars. "
            "Run --backfill-window against a dev DB, then re-probe. "
            "This is a build gap, not a join-quality failure."
        )
    elif passed:
        verdict = (
            "PASS — §5 thresholds met; a bounded US event-response backtest "
            "issue may be opened. (This issue does NOT open it.)"
        )
    else:
        missing = ", ".join(c.name for c in criteria if not c.passed)
        verdict = f"FAIL — §5 thresholds not met: {missing}."

    return GateResult(passed=passed, verdict=verdict, criteria=criteria)
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/services/market_events/test_coverage_gate.py -v`
Expected: PASS (6).

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check app/services/market_events/coverage_gate.py tests/services/market_events/test_coverage_gate.py
git add app/services/market_events/coverage_gate.py tests/services/market_events/test_coverage_gate.py
git commit -m "feat(ROB-371): pure §5 coverage threshold gate"
```

---

## Task 5: Read-only coverage measurement service

**Files:**
- Create: `app/services/market_events/us_earnings_coverage.py`
- Test: `tests/services/market_events/test_us_earnings_coverage.py`

**Behavior:** Given `(from_date, to_date)`, the service:
1. Loads realized US earnings events via `MarketEventsQueryService.list_for_range(category="earnings", market="us", source="finnhub")`, filtered in-memory to `status == "released"` and `event_date <= today` and `symbol is not None`.
2. Labels each via `label_earnings_decision_time` (collects `intraday_labeled_events`, `unknown_time_ratio`; `date_only_ratio` is 1.0 by construction — `release_time_utc` always NULL — and is asserted, not assumed).
3. For each event, enumerates expected sessions in `[event_date-5 .. event_date+20]` (calendar days bracket) via `trading_sessions_in_range("us", ...)` and counts present distinct bar-dates in `us_candles_1d` (custom SQL via `repository.session`). `window_coverage = present / expected`; event is joinable if `>= min_window_coverage`. Tracks `events_with_bars_present` / `events_with_zero_bars`.
4. Tradability: fraction of joinable symbols whose bars have `volume > 0`.
5. Survivorship: counts active vs delisted (`is_active=False`) symbols among event symbols; for a bounded sample of delisted symbols (size `delisted_sample`, only when `measure_delisted_recoverability=True`), attempts `fetch_us_daily_yahoo_fallback` (read-only network) and counts recoveries.
6. Benchmark: for `BENCHMARK_SYMBOLS`, measures window coverage over the union event-window range; `benchmark_coverage` = fraction of events whose window is fully spanned by SPY bars present.
7. Returns `CoverageMeasurement` (the Task 4 dataclass).

**SQL helper (date-window read; no date-range method exists — D3):**

```python
from sqlalchemy import text

_WINDOW_SQL = text(
    """
    SELECT DISTINCT (time AT TIME ZONE 'UTC')::date AS bar_date,
           CASE WHEN volume > 0 THEN 1 ELSE 0 END AS has_volume
    FROM us_candles_1d
    WHERE symbol = :symbol
      AND time >= :start_ts
      AND time < :end_ts
    """
)
```

(Exact column/timezone handling to be confirmed against `us_candles_1d` during Step 3 by reading `daily_candles/repository.py` `fetch_recent` SQL — mirror its `time` column usage and UTC convention.)

- [ ] **Step 1: Write failing integration test (DB-seeded)**

```python
# tests/services/market_events/test_us_earnings_coverage.py
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from app.services.market_events.coverage_gate import CoverageMeasurement
from app.services.market_events.us_earnings_coverage import UsEarningsCoverageService


@pytest.mark.integration
async def test_measure_counts_released_events_and_window_coverage(db_session):
    # Seed: one released BMO earnings event for AAPL on a trading day, with a
    # full -5..+20 session window of us_candles_1d bars (volume>0).
    await _seed_released_event(db_session, symbol="AAPL", event_date=date(2025, 7, 7),
                               time_hint="before_open")
    await _seed_full_window_bars(db_session, symbol="AAPL", event_date=date(2025, 7, 7))
    await db_session.commit()

    svc = UsEarningsCoverageService(db_session)
    m = await svc.measure(from_date=date(2025, 6, 1), to_date=date(2025, 7, 31))

    assert isinstance(m, CoverageMeasurement)
    assert m.realized_events == 1
    assert m.events_with_bars_present == 1
    assert m.events_with_zero_bars == 0
    assert m.window_coverage_p50 >= 0.90
    assert m.intraday_labeled_events == 0
    assert m.session_calendar_present is True
    assert m.date_only_ratio == 1.0


@pytest.mark.integration
async def test_event_without_bars_counts_as_zero_bars(db_session):
    await _seed_released_event(db_session, symbol="ZZZZ", event_date=date(2025, 7, 7),
                               time_hint="after_close")
    await db_session.commit()
    svc = UsEarningsCoverageService(db_session)
    m = await svc.measure(from_date=date(2025, 6, 1), to_date=date(2025, 7, 31))
    assert m.realized_events == 1
    assert m.events_with_zero_bars == 1
    assert m.events_with_bars_present == 0
```

(Helpers `_seed_released_event` / `_seed_full_window_bars` insert via `MarketEventsRepository.upsert_event_with_values` and `DailyCandlesRepository.upsert_rows` respectively — written in Step 1 using the exact signatures from exploration. Reuse existing conftest `db_session` fixture; check `tests/conftest.py` for its name and adapt.)

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/services/market_events/test_us_earnings_coverage.py -v`
Expected: FAIL — module not found (or fixture mismatch — fix fixture name first).

- [ ] **Step 3: Implement `UsEarningsCoverageService`**

Implement per the Behavior list above. Key points:
- Constructor: `def __init__(self, db: AsyncSession): self._db = db`.
- Use `MarketEventsQueryService(self._db).list_for_range(from_date, to_date, category="earnings", market="us", source="finnhub")` then filter `status=="released"`, `symbol`, `event_date <= date.today()`.
- Window read via `self._db.execute(_WINDOW_SQL, {...})`; `start_ts`/`end_ts` are UTC datetimes bracketing `[event_date-5d, event_date+20d+1d)`.
- Expected sessions via `trading_sessions_in_range("us", event_date - timedelta(days=5), event_date + timedelta(days=20))`.
- `session_calendar_present = True` (the calendar module is importable & returns sessions) — verify by asserting a known session resolves.
- Delisted recoverability via `fetch_us_daily_yahoo_fallback(symbol=sym, n=...)` only when `measure_delisted_recoverability=True`; bounded by `delisted_sample`. Network errors counted as non-recoverable, never raise.
- Benchmark via the same window SQL for each of `BENCHMARK_SYMBOLS`.
- `BENCHMARK_SYMBOLS = ("SPY", "XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB", "XLRE", "XLC")`.
- Return `CoverageMeasurement(...)`.

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/services/market_events/test_us_earnings_coverage.py -v`
Expected: PASS (2).

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check app/services/market_events/us_earnings_coverage.py tests/services/market_events/test_us_earnings_coverage.py
git add app/services/market_events/us_earnings_coverage.py tests/services/market_events/test_us_earnings_coverage.py
git commit -m "feat(ROB-371): read-only us_candles_1d earnings join-coverage measurement"
```

---

## Task 6: Operator CLI — `scripts/probe_us_earnings_coverage.py`

**Files:**
- Create: `scripts/probe_us_earnings_coverage.py`
- Test: `tests/scripts/test_probe_us_earnings_coverage_cli.py`

**Flags:**
- `--from-date` / `--to-date` (ISO; default last 365d ending today).
- `--run` (operator gate; without it → dry-run prints intended actions, no DB, no secrets).
- `--out` (write counts-only artifact to `coverage_artifact_path("us_earnings_coverage.json")`).
- `--measure-delisted-recoverability` + `--delisted-sample N` (opt-in Yahoo network probe).
- `--backfill-window` + `--confirm` (DOUBLE-gate; writes `us_candles_1d` via `DailyCandleSyncService`; prints DEV-DB warning; refuses without `--confirm`).
- Exit: `0` PASS, `1` FAIL (thresholds), `2` error/crash.

- [ ] **Step 1: Write failing CLI tests**

```python
# tests/scripts/test_probe_us_earnings_coverage_cli.py
from __future__ import annotations

from datetime import date

import pytest

from scripts.probe_us_earnings_coverage import parse_args


@pytest.mark.unit
def test_dry_run_is_default():
    args = parse_args(["--from-date", "2025-01-01", "--to-date", "2025-12-31"])
    assert args.run is False
    assert args.dry_run is True


@pytest.mark.unit
def test_backfill_requires_confirm():
    args = parse_args(["--run", "--backfill-window"])
    assert args.backfill_window is True
    assert args.confirm is False  # caller must enforce refusal


@pytest.mark.unit
def test_help_runs_without_secrets(capsys):
    # --help must not import Settings / require env. SystemExit(0) expected.
    with pytest.raises(SystemExit) as exc:
        parse_args(["--help"])
    assert exc.value.code == 0
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/scripts/test_probe_us_earnings_coverage_cli.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement CLI** (lazy imports inside `run`; module-level imports limited to stdlib + `app.core.cli`/`app.core.db` which are import-safe). Structure mirrors `scripts/diagnose_calendar_coverage.py` + the exploration skeleton:
  - `parse_args` sets `args.dry_run = not args.run`.
  - Dry-run: log intended actions, return 0.
  - `--run` without backfill: open `AsyncSessionLocal`, call `UsEarningsCoverageService.measure(...)`, then `evaluate_section5_gate`, print counts-only JSON + verdict, optionally `--out` write, return `0 if result.passed else 1`.
  - `--backfill-window`: refuse unless `--confirm`; print DEV-DB warning; iterate event symbols + benchmarks calling `DailyCandleSyncService.sync_one(target=SyncTarget(MarketKey.US, symbol, exchange), horizon_bars=...)`; record `backfill_performed=True` in artifact; then measure.
  - Wrap body in try/except → return 2 on crash.

- [ ] **Step 4: Run to verify pass + manual dry-run smoke (no secrets)**

Run: `uv run pytest tests/scripts/test_probe_us_earnings_coverage_cli.py -v`
Expected: PASS (3).
Run: `uv run python -m scripts.probe_us_earnings_coverage --from-date 2025-01-01 --to-date 2025-12-31`
Expected: prints a `[DRY-RUN]` line, exit 0, no DB access, no secret required.

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check scripts/probe_us_earnings_coverage.py tests/scripts/test_probe_us_earnings_coverage_cli.py
git add scripts/probe_us_earnings_coverage.py tests/scripts/test_probe_us_earnings_coverage_cli.py
git commit -m "feat(ROB-371): operator-gated read-only US earnings coverage probe CLI"
```

---

## Task 7: Runbook + verdict scaffold

**Files:**
- Create: `docs/runbooks/rob-371-us-earnings-coverage-probe.md`

- [ ] **Step 1: Write runbook** documenting: purpose; the §5 thresholds; how to run dry-run vs `--run`; the DEV-DB-only `--backfill-window --confirm` procedure (loud warning, never prod); artifact location + gitignore note; verdict interpretation (PASS → a bounded backtest issue *may* be opened, FAIL → what coverage is missing); and the safety-evidence checklist (no mutation, no scheduler, no prod write, no raw-data commit). Mirror the structure of `docs/runbooks/market-events-ingestion.md`.

- [ ] **Step 2: Commit**

```bash
git add docs/runbooks/rob-371-us-earnings-coverage-probe.md
git commit -m "docs(ROB-371): US earnings coverage probe runbook"
```

> **PR2 boundary** — open PR with Tasks 4, 5, 6, 7. Verify full `ruff check app/ tests/` + `uv run pytest tests/services/market_events tests/scripts` green (skip integration if no DB locally; note it), confirm CI Test workflow green before merge.

---

## Verdict / RUN expectation

The committed deliverable is the **mechanism + a deterministic gate**. The actual PASS/FAIL **verdict requires an operator RUN** against a populated dev DB (Finnhub earnings ingested + `us_candles_1d` materialized for the window) — and possibly a `--backfill-window` step. On a host without that data/network, the probe correctly emits **FAIL — coverage not materialized** (the D7 FALSE-FAIL guard), which is an honest, non-fabricated result, not a bug. The operator RUN + final verdict (and any subsequent backtest-issue decision) is gated and recorded in Linear, consistent with prior ROB research-run issues.

## Self-Review (run after drafting; fix inline)

1. **Spec coverage:** AC1 fail-closed calendar → Task 1+2. AC2 lookahead-safe labeling + intraday rejected → Task 3 (+ gate `no_intraday_labeling`). AC3 `-5..+20d` join coverage counts → Task 5. AC4 survivorship + delisted recoverability measured → Task 5. AC5 §5 artifact → Task 4+6. AC6 verdict PASS/FAIL, no backtest opened → Task 4 verdict + this section. AC7 safety evidence → runbook Task 7 + read-only/double-gate design. ✅ all mapped.
2. **Placeholder scan:** the only deferred detail is the exact `us_candles_1d` window SQL column/timezone, explicitly flagged in Task 5 Step 3 to be confirmed against `daily_candles/repository.py` at implementation — acceptable (verify-then-write), not a silent TODO.
3. **Type consistency:** `CoverageMeasurement` defined once (Task 4), returned by Task 5, consumed by gate + CLI. `EarningsDecisionLabel.anchor` values match the gate's `no_intraday_labeling` source (`intraday_labeled_events` counts `is_intraday_rejected`). `Market`/`Anchor` literals consistent across calendar + labeler. ✅

---

## Critique resolutions (applied — from `wf_7b6fd7f4-51d`, 4-lens adversarial review)

**BLOCKERS (must fix; baked into implementation):**
- **B1 — lookahead leak on `unknown` time_hint.** `unknown`/`None` must NOT anchor on `event_date` (it could have been AMC → that day's close is history before the news). Map `unknown`/`None` → `next_trading_session(event_date)` (conservative worst-case AMC), `anchor="whole_day_uncertain"`. Update Task 3 unknown branch + tests.
- **B2 — backfill prod-DB guard.** `--backfill-window` refuses (exit 2) if `DATABASE_URL` matches a prod denylist (`prod`, `production`, `release`) — checked BEFORE `--confirm`, so `--confirm` cannot bypass. Add `_assert_dev_database_url()` + unit test on a fake prod URL.
- **B3 — counts-only artifact is schema-enforced, not just gitignored.** `CoverageMeasurement` is a frozen dataclass of only scalar count/ratio/bool fields; the CLI serializes `dataclasses.asdict(measurement)` + gate scalars. Add a test asserting the emitted artifact JSON has NO list/dict values (no symbol/date arrays can leak).
- **B4 — integration tests need `@pytest.mark.integration` + commit-before-measure.** Fixture name `db_session` confirmed (conftest.py:407, pytest_asyncio). New test dirs need `__init__.py` (`tests/services/market_events/`, `tests/research/`).
- **B5 — window SQL.** `us_candles_1d.time` is `TIMESTAMPTZ`; exchange CHECK is `('NASD','NYSE','AMEX')` (seed partition = `"NYSE"`, NOT `"NASDAQ"`). Use UTC-deterministic extraction `(time AT TIME ZONE 'UTC')::date` (NOT `DATE(time)`, which is session-TZ dependent). Count coverage as `|present_dates ∩ expected_sessions| / |expected_sessions|`.

**§5 fidelity (MAJOR — restructure gate + measurement):**
- **F1 — `joinable_event_ratio`.** Runbook §5 "join coverage ≥90% of selected events" ⇒ gate on `joinable_event_ratio = events_joinable / realized_events ≥ 0.90` (event joinable iff its window coverage ≥ 0.90). Keep `window_coverage_p50` (median) as a recorded diagnostic only. `joinable_symbols` = distinct symbols with ≥1 joinable event (gate ≥200).
- **F2 — benchmark.** `benchmark_coverage = fraction of realized events for which ≥1 benchmark symbol has window coverage ≥0.90` (mirror per-event window logic — NOT "fully spanned").
- **F3 — tradability.** `tradability_coverage = fraction of joinable symbols with ≥1 `volume>0` bar across their event windows`.
- **F4 — dup/ambiguous.** US Finnhub: `dup_ambiguous_ratio = events_with_null_symbol / realized_events` (measured before null-filtering; natural-key unique index makes true dupes ~0). Documented as the carry-forward of the DART NULL-symbol metric.
- **F5 — date_only/unknown ratios recorded, not gated** (≤100% always accepted once intraday is forbidden — enforced by `no_intraday_labeling`). Add gate docstring noting this.

**Correctness/robustness (MAJOR/MINOR — applied):**
- **C1 — session search horizon** bumped `_SESSION_SEARCH_DAYS` 16 → **32** (covers worst-case KR lunar cluster ~10d + buffer; XNYS max ~4d). Documented in comment.
- **C2 — fail-closed catch narrowed** from bare `Exception` to `(ValueError, KeyError)` for `is_session`/`sessions_in_range` (xcals/pandas raise `ValueError`/`OutOfBoundsDatetime⊂ValueError` for out-of-range). Verified empirically by the far-future/far-past tests; widened only if a test proves a different type escapes.
- **C3 — gate verdict cases**: (a) `realized_events==0` → distinct "no events in range" verdict; (b) not-materialized verdict enriched with delisted recovery context; (c) verdict-keyword test (`PASS`/`FAIL` machine-parsed; comment warns against refactor).
- **C4 — intraday collector explicit**: `intraday_labeled_events = sum(1 for l in labels if l.is_intraday_rejected)`; service asserts `==0` only as a recorded count (gate enforces the hard-fail).
- **C5 — extra labeler tests**: BMO-on-weekend→Mon, AMC-across-holiday+weekend, far-past(1900)→unmappable, AMC anchor explicitly bound to `next_close` (+ docstring: "next session's CLOSE, not open").
- **C6 — expected_sources regression sweep**: run freshness/coverage-matrix tests; document the semantics change (holiday partitions become *expected-absent*, not *missing*) in the commit body. (No brittle "one get_calendar call" perf test — library memoizes; lazy import is amortized.)
- **C7 — Yahoo fallback read-only**: confirm `fetch_us_daily_yahoo_fallback` performs no DB write (pure fetch per exploration); document; network error → counted non-recoverable, never raises.
- **C8 — dry-run secrets test**: `parse_args(...)` runs with `DATABASE_URL` unset; all app/Settings imports deferred inside `run()`.

**Rejected/Deferred:** brittle perf-assertion test (C6 rationale); extracting a separate read-only Yahoo wrapper (C7 — already pure-read). The reviewer's "diagnose_calendar_coverage.py does not exist" is incorrect — it exists and is the structural mirror.

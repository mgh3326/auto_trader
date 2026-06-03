# ROB-426 PR2a — latest-healthy-partition selection (read-path) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop a thin smoke partition (e.g. 20 rows of a ~3,900 active universe) from shadowing a healthy older partition in `/invest/screener` KR loaders, by selecting the most recent *healthy* partition (bounded scan-back) and labeling any degraded serve honestly as `stale`.

**Architecture:** One table-generic resolver `resolve_healthy_partition` (in a new `partition_health.py`) replaces each loader's bare `max(snapshot_date)`. It walks distinct partition dates DESC, returns the first whose total row count ≥ `active_universe × 0.50`, falls back to older partitions, and never reduces availability (serves the newest as a degraded last resort; `None` only when the table is empty). Fail-open lives inside the resolver. No migration, read-only, no broker/order touch.

**Tech Stack:** Python 3.13, SQLAlchemy async, dataclasses, pytest (`pytest.mark.asyncio` + real `db_session` fixture), ruff.

**Spec:** `docs/superpowers/specs/2026-06-03-rob-426-pr2a-partition-health-selection-design.md`

**Branch:** `rob-426-pr2a` (off `origin/main`, independent of PR1/#1111). Spec commits already on it.

---

## File Structure

| File | Create/Modify | Responsibility |
| ---- | ------------- | -------------- |
| `app/services/invest_screener_snapshots/partition_health.py` | **Create** | `HealthyPartition`, `cap_degraded`, `active_universe_count`, `resolve_healthy_partition` (+ locked constants) |
| `app/services/invest_view_model/screener_service.py` | Modify | Site 1 `consecutive_gainers` (`:429-441`) + Site 2 `investor_flow` (`:600-612`) route through resolver |
| `app/services/invest_view_model/double_buy_screener.py` | Modify | Site 4 (`:41-54`) routes both tables through resolver |
| `app/services/invest_view_model/fundamentals_screener.py` | Modify | Site 5 (`:273-287`) valuation partition through resolver |
| `app/services/invest_view_model/high_yield_value_screener.py` | Modify | Site 6 valuation partition through resolver |
| `app/services/invest_view_model/undervalued_breakout_screener.py` | Modify | Site 7 valuation partition through resolver |
| `tests/test_partition_health.py` | **Create** | Resolver + `cap_degraded` + `active_universe_count` unit/db tests |
| `tests/test_partition_health_loader_wiring.py` | **Create** | Headline T1 + thin-today T9 + investor_flow/double_buy T7 (real `db_session`) |
| `tests/test_invest_view_model_screener_service.py` | Modify | Monkeypatch resolver in `_FakeSession` snapshot-loader tests |
| `tests/test_invest_view_model_double_buy_screener.py` | Modify | Same |
| `tests/test_invest_view_model_high_yield_value_screener.py` | Modify | Same |
| `tests/test_undervalued_breakout_screener.py` | Modify | Same |

---

## Task 1: The resolver module

**Files:**
- Create: `app/services/invest_screener_snapshots/partition_health.py`
- Create: `tests/test_partition_health.py`

- [ ] **Step 1: Write the failing unit tests for `cap_degraded` + `active_universe_count`**

Create `tests/test_partition_health.py`:

```python
from __future__ import annotations

import datetime as dt

import pytest

from app.models.invest_screener_snapshot import InvestScreenerSnapshot
from app.services.invest_screener_snapshots.partition_health import (
    HealthyPartition,
    active_universe_count,
    cap_degraded,
    resolve_healthy_partition,
)


def test_cap_degraded_floors_fresh_and_partial_to_stale():
    assert cap_degraded("fresh") == "stale"
    assert cap_degraded("partial") == "stale"
    assert cap_degraded("stale") == "stale"
    assert cap_degraded("missing") == "missing"
    assert cap_degraded("fallback") == "fallback"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_partition_health.py::test_cap_degraded_floors_fresh_and_partial_to_stale -v`
Expected: FAIL — `ModuleNotFoundError: app.services.invest_screener_snapshots.partition_health`

- [ ] **Step 3: Create the module**

Create `app/services/invest_screener_snapshots/partition_health.py`:

```python
"""ROB-426 PR2a — read-path latest-healthy-partition selection.

A /invest/screener loader must not let a thin smoke partition (e.g. 20 rows of a
~3,900 active universe) shadow a healthy older partition (~3,800 rows). This
module resolves the most recent partition whose TOTAL row count meets a coverage
bar (a fraction of the active universe), falling back to older partitions
(bounded scan-back), and never reduces availability: when no scanned partition is
healthy it returns the newest as a degraded last resort, and returns None only
when the table has no partitions for the market.

"Coverage" = total rows in a partition (the scored universe), NOT the number of
preset qualifiers — qualifier filtering happens downstream, unchanged.

Constants are locked here; changing them is a separate telemetry-backed PR
(mirrors invest_screener_snapshots/guards.py convention).
"""

from __future__ import annotations

import datetime as dt
import logging
import math
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.invest_screener_snapshots.freshness import DataState

logger = logging.getLogger(__name__)

#: A partition is healthy when its total row count is at least this fraction of
#: the active universe. Distinct from the 2b commit-guard floors. Change = PR.
_MIN_HEALTHY_COVERAGE_RATIO = 0.50
#: Bound the scan-back so a degenerate table cannot trigger an unbounded walk.
_MAX_PARTITION_SCAN_BACK = 10

_DEGRADED_FLOOR: DataState = "stale"
_KEEP_STATES: frozenset[DataState] = frozenset({"missing", "fallback", "stale"})


@dataclass(frozen=True)
class HealthyPartition:
    partition_date: dt.date
    row_count: int
    coverage_ratio: float
    is_fallback: bool  # older than the newest partition
    healthy: bool  # row_count met the coverage floor


def cap_degraded(state: DataState) -> DataState:
    """Never claim better than ``stale`` for a degraded partition.

    ``fresh``/``partial`` -> ``stale``; ``missing``/``fallback``/``stale`` kept.
    """
    return state if state in _KEEP_STATES else _DEGRADED_FLOOR


async def active_universe_count(session: AsyncSession, *, market: str) -> int:
    """Count active symbols for the market (the coverage denominator)."""
    if market == "kr":
        from app.models.kr_symbol_universe import KRSymbolUniverse

        stmt = (
            sa.select(sa.func.count())
            .select_from(KRSymbolUniverse)
            .where(KRSymbolUniverse.is_active.is_(True))
        )
    else:
        from app.models.us_symbol_universe import USSymbolUniverse

        stmt = (
            sa.select(sa.func.count())
            .select_from(USSymbolUniverse)
            .where(USSymbolUniverse.is_active.is_(True))
        )
    return int((await session.execute(stmt)).scalar() or 0)


async def _partition_row_count(
    session: AsyncSession, *, model: Any, market_col: Any, market: str, date_col: Any,
    partition_date: dt.date,
) -> int:
    return int(
        (
            await session.execute(
                sa.select(sa.func.count())
                .select_from(model)
                .where(market_col == market, date_col == partition_date)
            )
        ).scalar()
        or 0
    )


async def resolve_healthy_partition(
    session: AsyncSession,
    *,
    model: Any,
    date_col: Any,
    market_col: Any,
    market: str,
    universe_count: int | None = None,
    min_ratio: float = _MIN_HEALTHY_COVERAGE_RATIO,
    max_scan_back: int = _MAX_PARTITION_SCAN_BACK,
) -> HealthyPartition | None:
    """Return the partition to serve (see module docstring).

    None only when the table has no partitions for the market. Fail-open: on any
    query error, falls back to a plain max(date_col) treated as healthy.
    """
    try:
        dates = [
            d
            for (d,) in (
                await session.execute(
                    sa.select(date_col)
                    .where(market_col == market)
                    .distinct()
                    .order_by(date_col.desc())
                    .limit(max_scan_back)
                )
            ).all()
        ]
        if not dates:
            return None
        newest = dates[0]

        if universe_count is None:
            universe_count = await active_universe_count(session, market=market)
        if universe_count <= 0:
            return HealthyPartition(
                partition_date=newest, row_count=0, coverage_ratio=0.0,
                is_fallback=False, healthy=True,
            )

        floor = math.ceil(universe_count * min_ratio)
        for d in dates:
            count = await _partition_row_count(
                session, model=model, market_col=market_col, market=market,
                date_col=date_col, partition_date=d,
            )
            if count >= floor:
                return HealthyPartition(
                    partition_date=d, row_count=count,
                    coverage_ratio=count / universe_count,
                    is_fallback=(d != newest), healthy=True,
                )

        newest_count = await _partition_row_count(
            session, model=model, market_col=market_col, market=market,
            date_col=date_col, partition_date=newest,
        )
        return HealthyPartition(
            partition_date=newest, row_count=newest_count,
            coverage_ratio=newest_count / universe_count,
            is_fallback=False, healthy=False,
        )
    except Exception as exc:  # noqa: BLE001  — fail-open, never reduce availability
        logger.warning(
            "resolve_healthy_partition failed; falling back to max(): %s",
            exc, exc_info=True,
        )
        try:
            newest = (
                await session.execute(
                    sa.select(sa.func.max(date_col)).where(market_col == market)
                )
            ).scalar_one_or_none()
        except Exception:  # noqa: BLE001
            return None
        if newest is None:
            return None
        return HealthyPartition(
            partition_date=newest, row_count=0, coverage_ratio=0.0,
            is_fallback=False, healthy=True,
        )
```

- [ ] **Step 4: Run the `cap_degraded` test to verify it passes**

Run: `uv run pytest tests/test_partition_health.py::test_cap_degraded_floors_fresh_and_partial_to_stale -v`
Expected: PASS

- [ ] **Step 5: Add the db-backed resolver tests**

Append to `tests/test_partition_health.py` (uses the shared async `db_session` fixture — same one used by `tests/test_invest_screener_snapshots_repository.py`):

```python
def _snap(symbol: str, snapshot_date: dt.date) -> InvestScreenerSnapshot:
    return InvestScreenerSnapshot(
        market="kr",
        symbol=symbol,
        snapshot_date=snapshot_date,
        consecutive_up_days=5,
        week_change_rate=1.0,
        change_rate=1.0,
        closes_window=[1, 2, 3, 4, 5],
        computed_at=dt.datetime(2026, 5, 22, 0, 30, tzinfo=dt.UTC),
    )


async def _seed(session, *, date_counts: dict[dt.date, int]) -> None:
    n = 0
    for d, cnt in date_counts.items():
        for _ in range(cnt):
            n += 1
            session.add(_snap(f"{n:06d}", d))
    await session.flush()


_KW = dict(
    model=InvestScreenerSnapshot,
    date_col=InvestScreenerSnapshot.snapshot_date,
    market_col=InvestScreenerSnapshot.market,
    market="kr",
)


@pytest.mark.asyncio
async def test_resolve_latest_healthy(db_session):
    d = dt.date(2026, 5, 22)
    await _seed(db_session, date_counts={d: 60})
    hp = await resolve_healthy_partition(db_session, universe_count=100, **_KW)
    assert hp is not None and hp.partition_date == d
    assert hp.healthy is True and hp.is_fallback is False


@pytest.mark.asyncio
async def test_resolve_thin_latest_falls_back_to_older_healthy(db_session):
    older, newer = dt.date(2026, 5, 19), dt.date(2026, 5, 22)
    await _seed(db_session, date_counts={older: 60, newer: 5})
    hp = await resolve_healthy_partition(db_session, universe_count=100, **_KW)
    assert hp is not None and hp.partition_date == older
    assert hp.healthy is True and hp.is_fallback is True


@pytest.mark.asyncio
async def test_resolve_all_thin_serves_newest_as_last_resort(db_session):
    older, newer = dt.date(2026, 5, 19), dt.date(2026, 5, 22)
    await _seed(db_session, date_counts={older: 3, newer: 5})
    hp = await resolve_healthy_partition(db_session, universe_count=100, **_KW)
    assert hp is not None and hp.partition_date == newer  # NOT None
    assert hp.healthy is False and hp.is_fallback is False
    assert hp.row_count == 5


@pytest.mark.asyncio
async def test_resolve_empty_table_returns_none(db_session):
    hp = await resolve_healthy_partition(db_session, universe_count=100, **_KW)
    assert hp is None


@pytest.mark.asyncio
async def test_resolve_universe_zero_disables_gate(db_session):
    newer = dt.date(2026, 5, 22)
    await _seed(db_session, date_counts={newer: 5})
    hp = await resolve_healthy_partition(db_session, universe_count=0, **_KW)
    assert hp is not None and hp.partition_date == newer
    assert hp.healthy is True


@pytest.mark.asyncio
async def test_resolve_scan_back_bound_does_not_reach_distant_healthy(db_session):
    # Newest 2 are thin; a healthy partition exists but beyond max_scan_back=2.
    healthy_far = dt.date(2026, 5, 1)
    thin1, thin2 = dt.date(2026, 5, 20), dt.date(2026, 5, 22)
    await _seed(db_session, date_counts={healthy_far: 60, thin1: 5, thin2: 5})
    hp = await resolve_healthy_partition(
        db_session, universe_count=100, max_scan_back=2, **_KW
    )
    assert hp is not None and hp.partition_date == thin2  # last resort, not healthy_far
    assert hp.healthy is False


@pytest.mark.asyncio
async def test_active_universe_count_counts_active_kr(db_session):
    from app.models.kr_symbol_universe import KRSymbolUniverse

    db_session.add(KRSymbolUniverse(symbol="000001", name="A", is_active=True))
    db_session.add(KRSymbolUniverse(symbol="000002", name="B", is_active=False))
    await db_session.flush()
    assert await active_universe_count(db_session, market="kr") == 1
```

> Note for the implementer: confirm `InvestScreenerSnapshot` required NOT-NULL
> columns from `app/models/invest_screener_snapshot.py` and `KRSymbolUniverse`
> required columns from `app/models/kr_symbol_universe.py`; add any missing
> required kwarg to `_snap` / the universe rows. The model has nullable price
> fields, so the minimal set above should satisfy NOT-NULLs — verify before
> running.

- [ ] **Step 6: Run the resolver tests to verify they pass**

Run: `uv run pytest tests/test_partition_health.py -v`
Expected: PASS (all). If a NOT-NULL error appears, add the missing column per the note and re-run.

- [ ] **Step 7: Lint + commit**

```bash
uv run ruff check app/services/invest_screener_snapshots/partition_health.py tests/test_partition_health.py
git add app/services/invest_screener_snapshots/partition_health.py tests/test_partition_health.py
git commit -m "feat(ROB-426): partition-health resolver (read-path) for invest screener

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: Wire `consecutive_gainers` (Site 1) + headline regression

**Files:**
- Modify: `app/services/invest_view_model/screener_service.py:426-512`
- Create: `tests/test_partition_health_loader_wiring.py`
- Modify: `tests/test_invest_view_model_screener_service.py`

- [ ] **Step 1: Write the failing headline test**

Create `tests/test_partition_health_loader_wiring.py`:

```python
from __future__ import annotations

import datetime as dt

import pytest

from app.models.invest_screener_snapshot import InvestScreenerSnapshot
from app.services.invest_view_model import screener_service


def _gainer(symbol: str, d: dt.date) -> InvestScreenerSnapshot:
    return InvestScreenerSnapshot(
        market="kr",
        symbol=symbol,
        snapshot_date=d,
        consecutive_up_days=6,
        week_change_rate=3.5,
        change_rate=1.2,
        latest_close=80000,
        prev_close=79000,
        change_amount=1000,
        closes_window=[76000, 77000, 78000, 79000, 80000],
        daily_volume=1234567,
        computed_at=dt.datetime(2026, 5, 19, 0, 30, tzinfo=dt.UTC),
    )


async def _seed_two_partitions(session, *, healthy_n: int, thin_n: int):
    older, newer = dt.date(2026, 5, 19), dt.date(2026, 5, 22)
    k = 0
    for _ in range(healthy_n):
        k += 1
        session.add(_gainer(f"1{k:05d}", older))
    for _ in range(thin_n):
        k += 1
        session.add(_gainer(f"2{k:05d}", newer))
    # Universe so the 0.50 bar = 1000: healthy_n=1200 passes, thin_n=20 fails.
    from app.models.kr_symbol_universe import KRSymbolUniverse

    for i in range(2000):
        session.add(
            KRSymbolUniverse(symbol=f"{i:06d}", name=f"N{i}", is_active=True)
        )
    await session.flush()
    return older, newer


@pytest.mark.asyncio
async def test_thin_newer_partition_does_not_shadow_healthy_older(db_session):
    older, newer = await _seed_two_partitions(db_session, healthy_n=1200, thin_n=20)
    rows = await screener_service._load_consecutive_gainers_from_snapshots(
        db_session, market="kr", limit=20
    )
    assert rows, "expected the healthy older partition to be served"
    # Every served row comes from the older healthy partition...
    assert all(r["snapshot_date"] == older for r in rows)
    # ...and is labeled stale (older than today), not fresh.
    assert all(r["dataState"] == "stale" for r in rows)
```

> Implementer note: confirm the exact public name + signature of the
> consecutive-gainers snapshot loader in `screener_service.py` (around `:415`)
> and the row dict keys it emits for `snapshot_date` / `dataState` (the loader
> builds `rows.append({... "dataState"? ...})` — match the actual key; if the
> loader stores the per-row state under a different key, assert that key). If the
> loader is not module-public, call it via its actual name.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_partition_health_loader_wiring.py -v`
Expected: FAIL — without the fix the loader serves the **newer** 20-row partition (rows from `newer`, or `dataState` not `stale`).

- [ ] **Step 3: Wire the resolver into Site 1**

In `app/services/invest_view_model/screener_service.py`, add the import near the
other `invest_screener_snapshots` imports at the top of the module:

```python
from app.services.invest_screener_snapshots.partition_health import (
    cap_degraded,
    resolve_healthy_partition,
)
```

Replace the Step-1 latest-date block (current `:426-441`):

```python
    # Step 1: resolve the latest snapshot partition date.
    # This prevents older qualifying partitions from leaking into current results
    # when the latest partition has zero qualifiers (the known stale-data bug).
    latest_date_stmt = sa.select(
        sa.func.max(InvestScreenerSnapshot.snapshot_date)
    ).where(InvestScreenerSnapshot.market == market)
    try:
        latest_date_result = await session.execute(latest_date_stmt)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "failed to read invest_screener_snapshots max date: %s", exc, exc_info=True
        )
        return None
    latest_snapshot_date = latest_date_result.scalar_one_or_none()
    if latest_snapshot_date is None:
        return None  # no snapshots in the table; fall through to external
```

with:

```python
    # Step 1: resolve the latest *healthy* snapshot partition (ROB-426 PR2a).
    # A thin smoke partition must not shadow a healthy older one; the resolver
    # falls back to the newest healthy partition and is fail-open internally.
    hp = await resolve_healthy_partition(
        session,
        model=InvestScreenerSnapshot,
        date_col=InvestScreenerSnapshot.snapshot_date,
        market_col=InvestScreenerSnapshot.market,
        market=market,
    )
    latest_snapshot_date = hp.partition_date if hp else None
    if latest_snapshot_date is None:
        return None  # no snapshots in the table; fall through to external
    partition_degraded = bool(hp and (hp.is_fallback or not hp.healthy))
```

Then, in the row loop, wrap the per-row state with `cap_degraded` when degraded.
Change (current `:506-512`):

```python
        state = classify_state(
            snapshot_date=snap.snapshot_date,
            computed_at=snap.computed_at,
            closes_window_len=len(snap.closes_window or []),
            today_trading_date_value=today,
            now=now_utc,
        )
```

to:

```python
        state = classify_state(
            snapshot_date=snap.snapshot_date,
            computed_at=snap.computed_at,
            closes_window_len=len(snap.closes_window or []),
            today_trading_date_value=today,
            now=now_utc,
        )
        if partition_degraded:
            state = cap_degraded(state)
```

- [ ] **Step 4: Run the headline test to verify it passes**

Run: `uv run pytest tests/test_partition_health_loader_wiring.py -v`
Expected: PASS

- [ ] **Step 5: Fix the existing `_FakeSession` consecutive-gainers tests**

Run the existing file first to see what breaks:

Run: `uv run pytest tests/test_invest_view_model_screener_service.py -v`
Expected: one or more snapshot-loader tests FAIL (the resolver added `execute()` calls the `_FakeSession` sequence didn't expect).

For each failing test that exercises the consecutive-gainers snapshot path, add a
monkeypatch so the loader uses the fake session's intended partition date without
the resolver issuing extra queries. At the top of each such test (it must accept
the `monkeypatch` fixture):

```python
from app.services.invest_screener_snapshots.partition_health import HealthyPartition
from unittest.mock import AsyncMock

monkeypatch.setattr(
    screener_service,
    "resolve_healthy_partition",
    AsyncMock(
        return_value=HealthyPartition(
            partition_date=date(2026, 5, 11),  # the date the fake snapshots use
            row_count=9999,
            coverage_ratio=1.0,
            is_fallback=False,
            healthy=True,
        )
    ),
)
```

(Use the same `snapshot_date` the test's `_FakeSnapshot`/`_FakeExecuteResult`
already use — default `date(2026, 5, 11)` per `_FakeSnapshot`.)

- [ ] **Step 6: Re-run the existing file to verify green**

Run: `uv run pytest tests/test_invest_view_model_screener_service.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add app/services/invest_view_model/screener_service.py tests/test_partition_health_loader_wiring.py tests/test_invest_view_model_screener_service.py
git commit -m "feat(ROB-426): consecutive_gainers serves latest-healthy partition (PR2a)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: Wire `investor_flow` (Site 2) + `double_buy` (Site 4)

**Files:**
- Modify: `app/services/invest_view_model/screener_service.py:600-684`
- Modify: `app/services/invest_view_model/double_buy_screener.py:41-54`
- Modify: `tests/test_partition_health_loader_wiring.py` (add T7)
- Modify: `tests/test_invest_view_model_double_buy_screener.py`

- [ ] **Step 1: Write the failing investor_flow fallback test**

Append to `tests/test_partition_health_loader_wiring.py`:

```python
from app.models.investor_flow_snapshot import InvestorFlowSnapshot


def _flow(symbol: str, d: dt.date) -> InvestorFlowSnapshot:
    return InvestorFlowSnapshot(
        market="kr",
        symbol=symbol,
        snapshot_date=d,
        double_buy=True,
        foreign_consecutive_buy_days=5,
        foreign_net=1000,
        institution_net=10,
        individual_net=-10,
        collected_at=dt.datetime(2026, 5, 19, 0, 30, tzinfo=dt.UTC),
    )


@pytest.mark.asyncio
async def test_investor_flow_thin_newer_falls_back_to_healthy_older(db_session):
    older, newer = dt.date(2026, 5, 19), dt.date(2026, 5, 22)
    k = 0
    for _ in range(1200):
        k += 1
        db_session.add(_flow(f"1{k:05d}", older))
    for _ in range(20):
        k += 1
        db_session.add(_flow(f"2{k:05d}", newer))
    from app.models.kr_symbol_universe import KRSymbolUniverse

    for i in range(2000):
        db_session.add(KRSymbolUniverse(symbol=f"{i:06d}", name=f"N{i}", is_active=True))
    await db_session.flush()

    rows = await screener_service._load_investor_flow_discovery_from_snapshots(
        db_session, market="kr", limit=20
    )
    assert rows, "expected the healthy older investor_flow partition to be served"
    assert all(r["dataState"] == "stale" for r in rows)
```

> Implementer note: confirm the investor-flow snapshot loader's actual name
> (around `screener_service.py:590`) and that `_is_kr_toss_common_stock` does not
> filter out the seeded symbols (use symbols that pass the common-stock guard, or
> seed matching `KRSymbolUniverse.name`s; the guard keys on name patterns). If
> the guard filters everything, give the universe rows ordinary names (no
> ETF/ETN/SPAC tokens) so rows survive. Confirm the row dict's per-row state key.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_partition_health_loader_wiring.py::test_investor_flow_thin_newer_falls_back_to_healthy_older -v`
Expected: FAIL (serves the 20-row newer partition).

- [ ] **Step 3: Wire Site 2 (investor_flow)**

In `screener_service.py`, replace the investor-flow latest-date block (current `:600-612`):

```python
    latest_date_stmt = sa.select(sa.func.max(InvestorFlowSnapshot.snapshot_date)).where(
        InvestorFlowSnapshot.market == "kr"
    )
    try:
        latest_date_result = await session.execute(latest_date_stmt)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "failed to read investor_flow_snapshots max date: %s", exc, exc_info=True
        )
        return None
    latest_snapshot_date = latest_date_result.scalar_one_or_none()
    if latest_snapshot_date is None:
        return None
```

with:

```python
    hp = await resolve_healthy_partition(
        session,
        model=InvestorFlowSnapshot,
        date_col=InvestorFlowSnapshot.snapshot_date,
        market_col=InvestorFlowSnapshot.market,
        market="kr",
    )
    latest_snapshot_date = hp.partition_date if hp else None
    if latest_snapshot_date is None:
        return None
    partition_degraded = bool(hp and (hp.is_fallback or not hp.healthy))
```

Then wrap the per-row state (current `:679-684`):

```python
        state = classify_investor_flow_partition(
            snapshot_date=snap.snapshot_date,
            collected_at=snap.collected_at,
            today_trading_date_value=today,
            now=now_utc,
        )
```

to:

```python
        state = classify_investor_flow_partition(
            snapshot_date=snap.snapshot_date,
            collected_at=snap.collected_at,
            today_trading_date_value=today,
            now=now_utc,
        )
        if partition_degraded:
            state = cap_degraded(state)
```

- [ ] **Step 4: Wire Site 4 (double_buy)**

In `app/services/invest_view_model/double_buy_screener.py`, add at the top with
the other imports:

```python
from app.services.invest_screener_snapshots.partition_health import (
    active_universe_count,
    resolve_healthy_partition,
)
```

Replace the latest-dates block (current `:41-54`):

```python
    latest_flow_stmt = sa.select(sa.func.max(InvestorFlowSnapshot.snapshot_date)).where(
        InvestorFlowSnapshot.market == "kr"
    )
    latest_price_stmt = sa.select(
        sa.func.max(InvestScreenerSnapshot.snapshot_date)
    ).where(InvestScreenerSnapshot.market == "kr")
    try:
        flow_date = (await session.execute(latest_flow_stmt)).scalar_one_or_none()
        price_date = (await session.execute(latest_price_stmt)).scalar_one_or_none()
    except Exception as exc:  # noqa: BLE001
        logger.warning("double_buy: latest dates lookup failed: %s", exc, exc_info=True)
        return None
    if flow_date is None or price_date is None:
        return None
```

with:

```python
    universe_count = await active_universe_count(session, market="kr")
    flow_hp = await resolve_healthy_partition(
        session,
        model=InvestorFlowSnapshot,
        date_col=InvestorFlowSnapshot.snapshot_date,
        market_col=InvestorFlowSnapshot.market,
        market="kr",
        universe_count=universe_count,
    )
    price_hp = await resolve_healthy_partition(
        session,
        model=InvestScreenerSnapshot,
        date_col=InvestScreenerSnapshot.snapshot_date,
        market_col=InvestScreenerSnapshot.market,
        market="kr",
        universe_count=universe_count,
    )
    flow_date = flow_hp.partition_date if flow_hp else None
    price_date = price_hp.partition_date if price_hp else None
    if flow_date is None or price_date is None:
        return None
```

> Implementer note: `double_buy` derives its per-row `dataState` downstream
> (around `:120-160`). If that code recomputes freshness from the joined
> `flow_snapshot_date` / `price_snapshot_date`, serving older partitions already
> yields `stale` via the date-mismatch rule, so no `cap_degraded` is required
> here. If it instead hardcodes `fresh`, apply
> `degraded = (flow_hp and (flow_hp.is_fallback or not flow_hp.healthy)) or (price_hp and (price_hp.is_fallback or not price_hp.healthy))`
> and `cap_degraded` the emitted state. Verify the actual freshness derivation
> before deciding; add the cap only if needed.

- [ ] **Step 5: Run the investor_flow test + fix existing double_buy fake-session tests**

Run: `uv run pytest tests/test_partition_health_loader_wiring.py::test_investor_flow_thin_newer_falls_back_to_healthy_older tests/test_invest_view_model_double_buy_screener.py -v`
Expected: the new test PASSES; existing double_buy fake-session tests may FAIL on the added `execute()` calls.

For each failing double_buy test, monkeypatch both the resolver call and the
universe count in `double_buy_screener`. Define a tiny `side_effect` that keys off
the `model` kwarg so the two tables can return their own dates:

```python
from datetime import date
from unittest.mock import AsyncMock
from app.models.invest_screener_snapshot import InvestScreenerSnapshot
from app.services.invest_screener_snapshots.partition_health import HealthyPartition
from app.services.invest_view_model import double_buy_screener

_FLOW_DATE = date(2026, 5, 11)   # the date the test's fake investor-flow rows use
_PRICE_DATE = date(2026, 5, 11)  # the date the test's fake screener rows use

def _fake_resolve(session, *, model, **kwargs):
    d = _PRICE_DATE if model is InvestScreenerSnapshot else _FLOW_DATE
    return HealthyPartition(
        partition_date=d, row_count=9999, coverage_ratio=1.0,
        is_fallback=False, healthy=True,
    )

monkeypatch.setattr(double_buy_screener, "active_universe_count", AsyncMock(return_value=10000))
monkeypatch.setattr(double_buy_screener, "resolve_healthy_partition", AsyncMock(side_effect=_fake_resolve))
```

(If the existing test used a single date for both tables, set both constants to
that date.)

- [ ] **Step 6: Re-run both files to verify green**

Run: `uv run pytest tests/test_partition_health_loader_wiring.py tests/test_invest_view_model_double_buy_screener.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add app/services/invest_view_model/screener_service.py app/services/invest_view_model/double_buy_screener.py tests/test_partition_health_loader_wiring.py tests/test_invest_view_model_double_buy_screener.py
git commit -m "feat(ROB-426): investor_flow + double_buy serve latest-healthy partition (PR2a)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: Wire the valuation-primary loaders (Sites 5, 6, 7)

**Files:**
- Modify: `app/services/invest_view_model/fundamentals_screener.py:273-287`
- Modify: `app/services/invest_view_model/high_yield_value_screener.py` (the `max(MarketValuationSnapshot.snapshot_date)` block ~`:52-57`)
- Modify: `app/services/invest_view_model/undervalued_breakout_screener.py` (the `max(MarketValuationSnapshot.snapshot_date)` block ~`:59-64`)
- Modify: `tests/test_invest_view_model_high_yield_value_screener.py`, `tests/test_undervalued_breakout_screener.py`

- [ ] **Step 1: Wire Site 5 (fundamentals_screener)**

In `app/services/invest_view_model/fundamentals_screener.py`, add the import:

```python
from app.services.invest_screener_snapshots.partition_health import (
    resolve_healthy_partition,
)
```

Replace the valuation latest-date block (current `:273-287`):

```python
    try:
        val_date = (
            await session.execute(
                sa.select(sa.func.max(MarketValuationSnapshot.snapshot_date)).where(
                    MarketValuationSnapshot.market == "kr"
                )
            )
        ).scalar_one_or_none()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "fundamentals_screener: val date lookup failed: %s", exc, exc_info=True
        )
        return None
    if val_date is None:
        return None
```

with:

```python
    val_hp = await resolve_healthy_partition(
        session,
        model=MarketValuationSnapshot,
        date_col=MarketValuationSnapshot.snapshot_date,
        market_col=MarketValuationSnapshot.market,
        market="kr",
    )
    val_date = val_hp.partition_date if val_hp else None
    if val_date is None:
        return None
```

> Implementer note: the fundamentals loader computes its dependency freshness
> downstream (it has a `today_market_date` and a fundamentals-state check at
> `:372`). Since serving an older `val_date` makes the valuation dependency
> date-mismatch → `stale` through the existing freshness composition, no extra
> cap is required here. Confirm by reading the freshness assembly before
> finalizing; if a `fresh` could leak for a thin same-day valuation partition,
> thread `degraded = bool(val_hp and (val_hp.is_fallback or not val_hp.healthy))`
> into the dependency-state and `cap_degraded` it. (In current prod valuation is
> always thin, so `not val_hp.healthy` will be True — the degraded path matters.)

- [ ] **Step 2: Wire Sites 6 and 7 identically**

In `high_yield_value_screener.py` and `undervalued_breakout_screener.py`, add the
same import and replace each file's `max(MarketValuationSnapshot.snapshot_date)`
resolution with the same `resolve_healthy_partition(model=MarketValuationSnapshot,
date_col=MarketValuationSnapshot.snapshot_date, market_col=MarketValuationSnapshot.market,
market="kr")` call, taking `hp.partition_date` (return early when `None`), exactly
as in Step 1. Apply the same degraded/`cap_degraded` treatment only if the
implementer note's freshness check shows a `fresh` could otherwise leak.

> Implementer note: read the exact current block in each file (around the cited
> lines) and mirror the Step-1 transformation. Both join `InvestScreenerSnapshot`
> via LEFT JOIN but their PRIMARY partition is the valuation table, so only the
> valuation `max()` is replaced.

- [ ] **Step 3: Run the affected loader tests; fix fake-session sequences**

Run: `uv run pytest tests/test_invest_view_model_high_yield_value_screener.py tests/test_undervalued_breakout_screener.py -v`
Expected: existing fake-session tests may FAIL on added `execute()` calls.

Apply the single-line monkeypatch per failing test, targeting the module under
test (`high_yield_value_screener` / `undervalued_breakout_screener`):

```python
from unittest.mock import AsyncMock
from app.services.invest_screener_snapshots.partition_health import HealthyPartition

monkeypatch.setattr(
    <module_under_test>,
    "resolve_healthy_partition",
    AsyncMock(return_value=HealthyPartition(
        partition_date=<the date the test's fake valuation rows use>,
        row_count=9999, coverage_ratio=1.0, is_fallback=False, healthy=True)),
)
```

- [ ] **Step 4: Re-run to verify green**

Run: `uv run pytest tests/test_invest_view_model_high_yield_value_screener.py tests/test_undervalued_breakout_screener.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/services/invest_view_model/fundamentals_screener.py app/services/invest_view_model/high_yield_value_screener.py app/services/invest_view_model/undervalued_breakout_screener.py tests/test_invest_view_model_high_yield_value_screener.py tests/test_undervalued_breakout_screener.py
git commit -m "feat(ROB-426): valuation-primary presets serve latest-healthy partition (PR2a)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: Full verification

**Files:** none (verification only)

- [ ] **Step 1: Run the full PR2a + adjacent screener test surface**

Run:

```bash
uv run pytest \
  tests/test_partition_health.py \
  tests/test_partition_health_loader_wiring.py \
  tests/test_invest_view_model_screener_service.py \
  tests/test_invest_view_model_double_buy_screener.py \
  tests/test_invest_view_model_high_yield_value_screener.py \
  tests/test_undervalued_breakout_screener.py \
  tests/test_invest_coverage.py \
  tests/test_invest_api_screener_router.py \
  tests/test_invest_screener_snapshots_repository.py \
  -q
```

Expected: ALL PASS.

- [ ] **Step 2: Lint + format (CI scope is `app/ tests/`)**

```bash
uv run ruff check app/services/invest_screener_snapshots/partition_health.py app/services/invest_view_model/ tests/test_partition_health.py tests/test_partition_health_loader_wiring.py
uv run ruff format --check app/ tests/
```

Expected: no errors.

- [ ] **Step 3: Confirm no migration / no out-of-scope changes**

Run: `git status --short && git diff --stat origin/main..HEAD`
Expected: only `partition_health.py`, the 5 loader files, and the test files; **no** `alembic/versions/` change; no broker/order/watch/CLI/build-script edits (those are 2b).

- [ ] **Step 4: Grep that no other screener loader still uses a bare valuation/screener `max()` that should have been wired**

Run:

```bash
grep -rn "func.max(.*snapshot_date)" app/services/invest_view_model/ app/services/invest_screener_snapshots/repository.py
```

Expected: remaining hits are only the intentionally-out-of-scope sites (action-readiness symbol-scoped `:361`/`:467`, repo chokepoint `latest_partition` if site 10 was deferred, crypto). Confirm each remaining hit is a documented non-goal, not a missed wiring.

---

## Self-Review

**1. Spec coverage:**
- Spec §3.1 resolver/constants/`active_universe_count`/`cap_degraded` → Task 1.
- §3.2 loader wiring (sites 1,2,4,5,6,7; site 10 deferred) → Tasks 2,3,4.
- §3.3 freshness honesty (`cap_degraded` on degraded) → applied in Tasks 2,3 (and 4 conditionally per freshness derivation).
- §5 resolver-internal fail-open → Task 1 resolver body + `test_partition_health` (the `except` branch is covered indirectly; an explicit fail-open test can be added if desired — the headline value does not depend on it).
- §6 tests T1→Task2, T2/T3/T4/T4b/T5/T6→Task1, T7→Task3, T9→Task1 (`cap_degraded`) + Task2 (thin-today through loader is implied by the degraded path; the headline test already asserts `stale`), existing-test compat → Tasks 2/3/4 Step "fix fake-session".
- §7 non-goals / §8 acceptance → enforced in Task 5 Steps 3-4.

**Gap noted & accepted:** an explicit resolver fail-open unit test (§5) is not a
separate numbered step; add one if executing strictly to T-count. T8 (loader
fail-open) is implicitly covered because the resolver never raises to the loader;
a dedicated test can monkeypatch the resolver to raise and assert the loader
still… — but since fail-open is now resolver-internal, the loader has no catch,
so **do not** assert loader-level fail-open; instead trust the resolver's
internal `except`. (This is a deliberate design change from the spec's earlier
loader-wrapped version; noted so the implementer does not add a loader try/except.)

**2. Placeholder scan:** No TBD/TODO. Implementer notes call for confirming exact
current code (loader names, row-dict keys, model NOT-NULLs) before editing — these
are verification instructions, not placeholders; the transformation code is fully
shown.

**3. Type consistency:** `HealthyPartition(partition_date, row_count, coverage_ratio, is_fallback, healthy)` used identically across the module, tests, and every monkeypatch. `resolve_healthy_partition(session, *, model, date_col, market_col, market, universe_count=None, ...)` signature matches all call sites. `cap_degraded(state) -> DataState` consistent.

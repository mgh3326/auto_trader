# ROB-311 Fundamentals Dimension Evidence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a deterministic, DB-only per-symbol Fundamentals evidence bundle (`fundamentals_evidence.py`, reading `market_valuation_snapshots` + `stock_info` sector) and wire it into the Hermes context export, so Hermes can write a Fundamentals dimension report — reusing the generic ROB-306/308 dimension contract (no new table/endpoint/migration).

**Architecture:** Mirror `news_evidence`/`market_evidence`. A new repo method `latest_for_symbols` returns the latest valuation row per symbol; `build_fundamentals_evidence` joins it with `stock_info` sector into a JSON-able per-symbol bundle; the context exporter attaches it under `dimension_evidence["fundamentals"]` (best-effort, kr/us). **DB-only — no live KIS/Yahoo fetch.** `market_valuation_snapshots` is empty until ingestion is enabled (operator gate); the assembler degrades to `unavailable`.

**Tech Stack:** Python 3.13, SQLAlchemy async, Pydantic v2, pytest (`db_session`), `uv`.

**Spec:** `docs/superpowers/specs/2026-05-25-invest-reports-fundamentals-dimension-design.md` · **Linear:** ROB-311 · **Branch:** `rob-311`

**Conventions:** `uv run pytest ... -v`; commit trailer `Co-Authored-By: Paperclip <noreply@paperclip.ing>`. Mirror targets: `app/services/investment_dimensions/news_evidence.py`, `tests/services/investment_dimensions/test_news_evidence.py`; the `dimension_evidence["news"]` block at `app/services/investment_stages/hermes_context.py` ~line 127-134. Repo: `app/services/market_valuation_snapshots/repository.py` (`MarketValuationSnapshotsRepository`, `MarketValuationSnapshot` cols `market/symbol/snapshot_date/source/per/pbr/roe/dividend_yield/market_cap/high_52w/low_52w/computed_at`). Sector: `app/services/stock_info_service.py` `StockInfoService(db).get_stock_info_by_symbol(symbol) -> StockInfo|None` (`.sector` nullable).

---

## File Structure
- Modify: `app/services/market_valuation_snapshots/repository.py` — add `latest_for_symbols`.
- Create: `app/services/investment_dimensions/fundamentals_evidence.py`
- Create: `tests/services/investment_dimensions/test_fundamentals_evidence.py`
- Modify: `app/services/investment_stages/hermes_context.py` — add `dimension_evidence["fundamentals"]`.
- Test: `tests/services/investment_stages/test_hermes_context_fundamentals_dimension.py`

---

## Task 1: `latest_for_symbols` repository method

**Files:**
- Modify: `app/services/market_valuation_snapshots/repository.py`
- Test: `tests/services/market_valuation_snapshots/test_repository_latest_for_symbols.py`

Returns the latest-`snapshot_date` row per symbol for a market (across sources). Source precedence on tie: newest `snapshot_date`, then `computed_at` desc — deterministic via `DISTINCT ON (symbol)` ordered by `symbol, snapshot_date DESC, computed_at DESC`.

- [ ] **Step 1: Write the failing test**

```python
import datetime as dt
from decimal import Decimal

import pytest

from app.models.market_valuation_snapshot import MarketValuationSnapshot
from app.services.market_valuation_snapshots.repository import (
    MarketValuationSnapshotsRepository,
)


async def _clear(db_session):
    from sqlalchemy import text
    await db_session.execute(text("DELETE FROM market_valuation_snapshots"))
    await db_session.commit()


def _row(symbol, *, snapshot_date, per, source="yahoo", market="us"):
    return MarketValuationSnapshot(
        market=market, symbol=symbol, snapshot_date=snapshot_date, source=source,
        per=Decimal(per), pbr=Decimal("1.2"), roe=Decimal("0.15"),
        dividend_yield=Decimal("0.02"), market_cap=Decimal("1000000"),
        high_52w=Decimal("200"), low_52w=Decimal("100"),
    )


@pytest.mark.asyncio
async def test_latest_for_symbols_returns_newest_per_symbol(db_session):
    await _clear(db_session)
    db_session.add_all([
        _row("AAPL", snapshot_date=dt.date(2026, 5, 20), per="10"),
        _row("AAPL", snapshot_date=dt.date(2026, 5, 23), per="12"),  # newest
        _row("MSFT", snapshot_date=dt.date(2026, 5, 23), per="30"),
    ])
    await db_session.commit()

    repo = MarketValuationSnapshotsRepository(db_session)
    rows = await repo.latest_for_symbols(market="us", symbols={"AAPL", "MSFT", "TSLA"})
    by_symbol = {r.symbol: r for r in rows}
    assert set(by_symbol) == {"AAPL", "MSFT"}  # TSLA absent
    assert by_symbol["AAPL"].per == Decimal("12")  # newest snapshot_date


@pytest.mark.asyncio
async def test_latest_for_symbols_empty_input(db_session):
    repo = MarketValuationSnapshotsRepository(db_session)
    assert await repo.latest_for_symbols(market="us", symbols=set()) == []
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/services/market_valuation_snapshots/test_repository_latest_for_symbols.py -v`
Expected: FAIL — `AttributeError: ... 'latest_for_symbols'`.

- [ ] **Step 3: Implement** — add to `MarketValuationSnapshotsRepository` (uses `Set` import + existing `select`):

```python
    async def latest_for_symbols(
        self, *, market: str, symbols: "set[str]"
    ) -> list[MarketValuationSnapshot]:
        if not symbols:
            return []
        norm_market = market.strip().lower()
        norm_symbols = {s.strip().upper() for s in symbols}
        stmt = (
            select(MarketValuationSnapshot)
            .where(
                MarketValuationSnapshot.market == norm_market,
                MarketValuationSnapshot.symbol.in_(norm_symbols),
            )
            .order_by(
                MarketValuationSnapshot.symbol.asc(),
                MarketValuationSnapshot.snapshot_date.desc(),
                MarketValuationSnapshot.computed_at.desc(),
            )
            .distinct(MarketValuationSnapshot.symbol)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())
```

(`.distinct(col)` renders Postgres `DISTINCT ON (symbol)`; the leading `ORDER BY symbol, snapshot_date DESC, computed_at DESC` makes it pick the newest per symbol deterministically.)

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/services/market_valuation_snapshots/test_repository_latest_for_symbols.py -v`
Expected: PASS (2 cases).

- [ ] **Step 5: Commit**

```bash
git add app/services/market_valuation_snapshots/repository.py tests/services/market_valuation_snapshots/test_repository_latest_for_symbols.py
git commit -m "feat(rob-311): valuation repo latest_for_symbols (DISTINCT ON newest per symbol)

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 2: Fundamentals evidence assembler

**Files:**
- Create: `app/services/investment_dimensions/fundamentals_evidence.py`
- Test: `tests/services/investment_dimensions/test_fundamentals_evidence.py`

- [ ] **Step 1: Write the failing test**

```python
import datetime as dt
from decimal import Decimal

import pytest

from app.models.analysis import StockInfo
from app.models.market_valuation_snapshot import MarketValuationSnapshot
from app.services.market_valuation_snapshots.repository import (
    MarketValuationSnapshotsRepository,
)
from app.services.stock_info_service import StockInfoService
from app.services.investment_dimensions.fundamentals_evidence import (
    build_fundamentals_evidence,
)


async def _clear(db_session):
    from sqlalchemy import text
    await db_session.execute(text("DELETE FROM market_valuation_snapshots"))
    await db_session.execute(text("DELETE FROM stock_info WHERE symbol IN ('AAPL','MSFT')"))
    await db_session.commit()


@pytest.mark.asyncio
async def test_build_fundamentals_evidence_covered(db_session):
    await _clear(db_session)
    db_session.add(MarketValuationSnapshot(
        market="us", symbol="AAPL", snapshot_date=dt.date(2026, 5, 23), source="yahoo",
        per=Decimal("28.5"), pbr=Decimal("45"), roe=Decimal("1.5"),
        dividend_yield=Decimal("0.005"), market_cap=Decimal("3000000000000"),
        high_52w=Decimal("260"), low_52w=Decimal("164"),
    ))
    db_session.add(StockInfo(symbol="AAPL", name="Apple", instrument_type="equity_us",
                             sector="Technology", is_active=True))
    await db_session.commit()

    bundle = await build_fundamentals_evidence(
        MarketValuationSnapshotsRepository(db_session), StockInfoService(db_session),
        market="us", symbols={"AAPL", "MSFT"},
        now=dt.datetime(2026, 5, 24, tzinfo=dt.UTC),
    )
    assert bundle["market"] == "us"
    assert bundle["data_health"] == {"requested": 2, "covered": 1}
    assert bundle["covered_count"] == 1
    row = bundle["per_symbol"][0]
    assert row["symbol"] == "AAPL"
    assert row["sector"] == "Technology"
    assert row["per"] == 28.5
    assert row["dividend_yield"] == 0.005
    assert bundle["freshness"]["status"] in {"fresh", "stale"}
    assert bundle["freshness"]["latest_snapshot_date"] == "2026-05-23"


@pytest.mark.asyncio
async def test_build_fundamentals_evidence_empty_is_unavailable(db_session):
    await _clear(db_session)
    bundle = await build_fundamentals_evidence(
        MarketValuationSnapshotsRepository(db_session), StockInfoService(db_session),
        market="us", symbols={"AAPL"},
        now=dt.datetime(2026, 5, 24, tzinfo=dt.UTC),
    )
    assert bundle["covered_count"] == 0
    assert bundle["per_symbol"] == []
    assert bundle["freshness"]["status"] == "unavailable"
    assert bundle["data_health"] == {"requested": 1, "covered": 0}
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/services/investment_dimensions/test_fundamentals_evidence.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement** `app/services/investment_dimensions/fundamentals_evidence.py`:

```python
"""Deterministic Fundamentals dimension evidence bundle (ROB-311).

Assembles per-symbol valuation (PER/PBR/ROE/dividend/market_cap/52w from
``market_valuation_snapshots``) + sector (``stock_info``) into a market+symbol
bundle, mirroring ``market_evidence``/``news_evidence``. DB-ONLY — never calls a
live broker API. ``market_valuation_snapshots`` is empty until ingestion is
enabled (operator gate); this degrades to ``unavailable``.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Set
from decimal import Decimal
from typing import Any

from app.services.market_valuation_snapshots.repository import (
    MarketValuationSnapshotsRepository,
)
from app.services.stock_info_service import StockInfoService

FRESH_WINDOW_DAYS = 7


def _f(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


async def build_fundamentals_evidence(
    valuation_repo: MarketValuationSnapshotsRepository,
    stock_info_service: StockInfoService,
    *,
    market: str,
    symbols: Set[str],
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    now_dt = now or dt.datetime.now(tz=dt.UTC)
    requested = len(symbols)
    rows = await valuation_repo.latest_for_symbols(market=market, symbols=set(symbols))

    per_symbol: list[dict[str, Any]] = []
    latest_date: dt.date | None = None
    for row in rows:
        info = await stock_info_service.get_stock_info_by_symbol(row.symbol)
        per_symbol.append(
            {
                "symbol": row.symbol,
                "sector": getattr(info, "sector", None),
                "per": _f(row.per),
                "pbr": _f(row.pbr),
                "roe": _f(row.roe),
                "dividend_yield": _f(row.dividend_yield),
                "market_cap": _f(row.market_cap),
                "high_52w": _f(row.high_52w),
                "low_52w": _f(row.low_52w),
            }
        )
        if latest_date is None or row.snapshot_date > latest_date:
            latest_date = row.snapshot_date

    if not per_symbol:
        status = "unavailable"
    elif (
        latest_date is not None
        and latest_date >= (now_dt.date() - dt.timedelta(days=FRESH_WINDOW_DAYS))
    ):
        status = "fresh"
    else:
        status = "stale"

    return {
        "market": market,
        "per_symbol": per_symbol,
        "covered_count": len(per_symbol),
        "freshness": {
            "status": status,
            "latest_snapshot_date": latest_date.isoformat() if latest_date else None,
        },
        "data_health": {"requested": requested, "covered": len(per_symbol)},
    }
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/services/investment_dimensions/test_fundamentals_evidence.py -v`
Expected: PASS (2 cases).

- [ ] **Step 5: Commit**

```bash
git add app/services/investment_dimensions/fundamentals_evidence.py tests/services/investment_dimensions/test_fundamentals_evidence.py
git commit -m "feat(rob-311): deterministic Fundamentals dimension evidence (DB-only)

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 3: Wire Fundamentals evidence into the Hermes context export

**Files:**
- Modify: `app/services/investment_stages/hermes_context.py`
- Test: `tests/services/investment_stages/test_hermes_context_fundamentals_dimension.py`

`symbols` = portfolio holdings (gathered from `snapshots_by_kind["portfolio"]`) ∪ market-dimension top movers (from the `dimension_evidence["market"]` dict if it was built successfully this call).

- [ ] **Step 1: Write the failing test** — mirror `test_hermes_context_news_dimension.py` (`grep -rl "dimension_evidence\[.news.\]\|test_hermes_context_news" tests/`). Seed a `market_valuation_snapshots` row + matching `stock_info` for a symbol that is held (in the portfolio snapshot) for a `kr` (or `us`) bundle, build the context, and assert:

```python
assert "fundamentals" in payload.dimension_evidence
fund = payload.dimension_evidence["fundamentals"]
assert fund["market"] in ("kr", "us")
assert fund["data_health"]["requested"] >= 1
# the seeded held symbol appears (covered) when its valuation row exists
assert any(r["symbol"] == SEEDED_SYMBOL for r in fund["per_symbol"])
```

Copy the bundle/run/portfolio-snapshot setup verbatim from the news/market context test; add the valuation + stock_info rows for the held symbol before `export`.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/services/investment_stages/test_hermes_context_fundamentals_dimension.py -v`
Expected: FAIL — `"fundamentals" not in dimension_evidence`.

- [ ] **Step 3: Implement** — in `hermes_context.py`, add imports near the news import:

```python
from app.services.investment_dimensions.fundamentals_evidence import (
    build_fundamentals_evidence,
)
from app.services.market_valuation_snapshots.repository import (
    MarketValuationSnapshotsRepository,
)
from app.services.stock_info_service import StockInfoService
```

Inside the `if bundle.market in ("kr", "us"):` block, immediately after the `dimension_evidence["news"]` try/except, add:

```python
            try:
                fundamentals_symbols: set[str] = set()
                for snap in snapshots_by_kind.get("portfolio", []):
                    for h in (snap.payload_json or {}).get("holdings", []):
                        ticker = h.get("ticker")
                        if ticker:
                            fundamentals_symbols.add(ticker)
                market_dim = dimension_evidence.get("market")
                if isinstance(market_dim, dict):
                    for mover in market_dim.get("top_movers", []):
                        sym = mover.get("symbol")
                        if sym:
                            fundamentals_symbols.add(sym)
                fundamentals_evidence = await build_fundamentals_evidence(
                    MarketValuationSnapshotsRepository(self._session),
                    StockInfoService(self._session),
                    market=bundle.market,
                    symbols=fundamentals_symbols,
                )
                dimension_evidence["fundamentals"] = fundamentals_evidence
            except Exception as exc:  # noqa: BLE001 — best-effort, like market/news
                _logger.exception(
                    "Failed to build fundamentals evidence for context export"
                )
                dimension_evidence["fundamentals"] = {"unavailable": str(exc)}
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/services/investment_stages/test_hermes_context_fundamentals_dimension.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/investment_stages/hermes_context.py tests/services/investment_stages/test_hermes_context_fundamentals_dimension.py
git commit -m "feat(rob-311): attach Fundamentals evidence to Hermes context export

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 4: Verification

- [ ] **Step 1:** `uv run pytest tests/services/investment_dimensions/ tests/services/investment_stages/ tests/services/market_valuation_snapshots/ -q` → all pass (Fundamentals + existing Market/News/dimension/valuation).
- [ ] **Step 2:** ROB-287 guard: `uv run pytest tests/services/action_report/snapshot_backed/test_no_internal_llm_imports.py -q` → pass. Also confirm the assembler imports no broker client: `grep -nE "kis|yahoo|broker|fetch_fundamental" app/services/investment_dimensions/fundamentals_evidence.py` → no matches.
- [ ] **Step 3:** `make lint` → clean.
- [ ] **Step 4:** broad regression: `uv run pytest tests/ -k "hermes or dimension or valuation or fundamental" -q` → green.
- [ ] **Step 5:** Open PR. Handoff: branch, PR URL, tests; note `market_valuation_snapshots` ingestion enablement remains an operator gate (data deferred), earnings deferred, and the Fundamentals report prose is produced by Hermes via the existing `/hermes/dimension-reports` (dimension="fundamentals").

---

## Self-Review (against spec)

**Spec coverage:**
- F1 `build_fundamentals_evidence` (per-symbol valuation+sector, DB-only, soft-fail, coverage) → Task 2. ✓
- F1b `latest_for_symbols` (newest per symbol, deterministic, empty→[]) → Task 1. ✓
- F2 context wiring `dimension_evidence["fundamentals"]` (kr/us, holdings ∪ top_movers, soft-fail) → Task 3. ✓
- F3 tests (covered/empty/partial assembler + context export) → Tasks 1-3. ✓
- Boundaries: no live broker (Task 4 grep guard + DB-only assembler); no LLM (Task 4 guard); no table/endpoint/migration (none added); ingestion deferred (none enabled); per-symbol only. ✓

**Placeholder scan:** Task 3 Step 1 says "mirror the news context test, grep to find it" — explicit instruction against a named pattern, not deferred work. Assembler + repo (Tasks 1-2) are complete code.

**Type consistency:** `latest_for_symbols(*, market, symbols: set[str]) -> list[MarketValuationSnapshot]` (Task 1) ↔ called in Task 2. `build_fundamentals_evidence(valuation_repo, stock_info_service, *, market, symbols, now)` identical in Task 2 (def+tests) and Task 3 (call). Return keys `{market, per_symbol, covered_count, freshness:{status, latest_snapshot_date}, data_health:{requested, covered}}` asserted consistently. `_f` Decimal→float helper used for all numeric fields.
```

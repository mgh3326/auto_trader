# ROB-422 PR2a — Fundamentals Screener Infra + 돈잘버는회사 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire PR1's `financial_fundamentals_snapshots` read-model into `/invest/screener` for the first time by building shared fundamentals-screening infra and lighting up one Toss-parity preset end-to-end: 돈잘버는회사 (`profitable_company` = `gross_margin_ttm` ≥ 20% AND ROE ≥ 15%).

**Architecture:** Mirror `high_yield_value_screener.py` (the valuation-snapshot preset loader precedent). A new `fundamentals_screener.py` loader takes a candidate universe from the latest `market_valuation_snapshots` partition (ROE filter), bulk-reads `financial_fundamentals_snapshots` per candidate, runs the pure PIT-gated `derive_fundamentals_metrics(report_date=today)`, applies a per-preset threshold spec, and returns rows + valuation/fundamentals partition metadata. `screener_service` dispatches `profitable_company` to it (snapshot-only, no generic fallback) and surfaces a new `'fundamentals'` freshness dependency. Aggregates are derived live, never stored. `derive.py`'s known latent streak/dividend-empty items are closed first (derive becomes wired here).

**Tech Stack:** Python 3.13, SQLAlchemy 2.x async, Pydantic v2, pytest. No migration (PR1 table reused). No new dependency.

**Spec:** `docs/superpowers/specs/2026-06-02-rob-422-pr2a-fundamentals-screener-money-maker-design.md`

---

## File Structure

**Create:**
- `app/services/invest_view_model/fundamentals_screener.py` — `FundamentalsPresetSpec`, `FundamentalsScreenResult`, pure `evaluate_fundamentals_candidates(...)`, DB orchestration `load_fundamentals_preset_from_snapshots(...)`, `PROFITABLE_COMPANY_SPEC`.
- Tests: `tests/test_financial_fundamentals_derive.py` (extend), `tests/test_financial_fundamentals_snapshots_repository.py` (extend), `tests/test_fundamentals_screener.py`, `tests/test_screener_service_profitable_company.py`, `tests/test_screener_presets_profitable_company.py`.

**Modify:**
- `app/services/financial_fundamentals_snapshots/derive.py` — §10.1 fixes (streak year-contiguity guard; dividend streaks → `unavailable` on 0 visible periods).
- `app/services/financial_fundamentals_snapshots/repository.py` — add `latest_periods_for_symbols`.
- `app/services/invest_view_model/screener_presets.py` — add `profitable_company` preset + `_KR_ONLY_PRESET_IDS`.
- `app/services/invest_view_model/screener_service.py` — dispatch + snapshot-only guard + `primary_source` + `'fundamentals'` dependency.
- `app/schemas/invest_screener.py` — extend the freshness dependency `kind` to allow `'fundamentals'` (only if it is a constrained Literal — verify in Task 5).
- `docs/invest-screener-toss-parity-matrix.md` — mark 돈잘버는회사 row implemented.

**Conventions (verified in-repo):**
- Loader returns `None` when no valuation partition exists → caller sets `dataState=missing`, never falls through to the generic provider (mirror `high_yield_value` guard at `screener_service.py:1582-1587`).
- Per-row valuation freshness via `_screener_snapshot_state` key (fresh/stale). Fundamentals freshness is a separate dependency.
- `_is_kr_toss_common_stock(sym, name)` filters ETFs/preferred (reuse, imported inside the function to avoid circular import — see `high_yield_value_screener.py:130-133`).
- `today_trading_date("kr", now=...)` gives the KR trading date used as `report_date`.

---

## Task 1: Close derive §10.1 latent items (streak year-gap guard + dividend-empty)

**Files:**
- Modify: `app/services/financial_fundamentals_snapshots/derive.py`
- Test: `tests/test_financial_fundamentals_derive.py`

- [ ] **Step 1: Write the failing tests (append to the file)**

```python
def test_earnings_increase_streak_breaks_on_fiscal_year_gap():
    # 2021,2022 present then 2024 (2023 row absent) — the gap means the run ending
    # at 2024 has no contiguous prior year → streak 0 (NOT a fabricated 2).
    periods = [
        _annual(2021, revenue="1000", net_income="100", filing_date=dt.date(2022, 3, 20)),
        _annual(2022, revenue="1100", net_income="120", filing_date=dt.date(2023, 3, 20)),
        _annual(2024, revenue="1600", net_income="200", filing_date=dt.date(2025, 3, 20)),
    ]
    d = derive_fundamentals_metrics(periods, report_date=dt.date(2025, 6, 1))
    assert d.earnings_increase_streak_years.value == 0


def test_dividend_streaks_unavailable_when_no_visible_periods():
    # report_date before every filing → 0 visible annual rows → dividend streaks
    # must be 'unavailable' (missing != zero; never (ok, 0)).
    d = derive_fundamentals_metrics(_periods(), report_date=dt.date(2020, 1, 1))
    assert d.dividend_paid_streak_years.state == "unavailable"
    assert d.dividend_growth_streak_years.state == "unavailable"
    assert d.earnings_increase_streak_years.state == "unavailable"


def test_dividend_paid_streak_breaks_on_fiscal_year_gap():
    # 2024 dividend present but 2023 dividend missing (None) → only 2024 counts.
    periods = _periods()
    periods[2] = _annual(2023, revenue="1300", net_income="150",
                         filing_date=dt.date(2024, 3, 20), dps=None, payout_ratio=None)
    d = derive_fundamentals_metrics(periods, report_date=dt.date(2025, 6, 1))
    assert d.dividend_paid_streak_years.value == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_financial_fundamentals_derive.py -k "fiscal_year_gap or unavailable_when_no_visible" -v`
Expected: FAIL (current code counts list-adjacency / returns `(ok, 0)` on empty).

- [ ] **Step 3: Replace the three streak functions + their call sites in `derive.py`**

Replace `_increase_streak`, `_dividend_paid_streak`, `_dividend_growth_streak` with year-contiguous versions that take the annual period objects:

```python
def _increase_streak(annual: list) -> MetricResult:
    # Consecutive YoY net-income increases ending at the most recent year.
    # A fiscal-year gap (non-contiguous year) or a None value breaks the run.
    if len(annual) < 2:
        return MetricResult(value=0, state="partial", note="insufficient history")
    streak = 0
    for i in range(len(annual) - 1, 0, -1):
        cur, prev = annual[i], annual[i - 1]
        if cur.net_income is None or prev.net_income is None:
            break
        if cur.period_end_date.year != prev.period_end_date.year + 1:
            break  # fiscal-year gap → not consecutive
        if cur.net_income > prev.net_income:
            streak += 1
        else:
            break
    return MetricResult(value=streak, state="ok")


def _dividend_paid_streak(annual: list) -> MetricResult:
    # Consecutive years with DPS > 0 ending at the most recent year. A None DPS
    # (no filing) or a fiscal-year gap breaks the run (missing != zero).
    streak = 0
    prev_year: int | None = None
    for p in reversed(annual):
        if p.dividend_per_share is None:
            break
        if prev_year is not None and p.period_end_date.year != prev_year - 1:
            break  # fiscal-year gap
        if p.dividend_per_share > 0:
            streak += 1
            prev_year = p.period_end_date.year
        else:
            break
    return MetricResult(value=streak, state="ok")


def _dividend_growth_streak(annual: list) -> MetricResult:
    # Consecutive YoY DPS increases ending at the most recent year. A non-contiguous
    # year (incl. one created by a None-DPS year) breaks the run.
    dps_rows = [p for p in annual if p.dividend_per_share is not None]
    if len(dps_rows) < 2:
        return MetricResult(value=0, state="partial", note="insufficient dividend history")
    streak = 0
    for i in range(len(dps_rows) - 1, 0, -1):
        cur, prev = dps_rows[i], dps_rows[i - 1]
        if cur.period_end_date.year != prev.period_end_date.year + 1:
            break
        if cur.dividend_per_share > prev.dividend_per_share:
            streak += 1
        else:
            break
    return MetricResult(
        value=streak, state="ok", note="DPS is split/par-value unadjusted (DART raw)"
    )
```

Then update the call sites in `derive_fundamentals_metrics` so streaks take `annual` and are guarded on empty:

```python
    return FundamentalsDerivation(
        report_date=report_date,
        payout_ratio=_payout_ratio(annual),
        gross_margin_ttm=_gross_margin_ttm(annual, quarterly),
        revenue_growth_3y_avg=_growth_3y_avg(revenues) if revenues else _UNAVAILABLE,
        earnings_growth_3y_avg=_growth_3y_avg(net_incomes)
        if net_incomes
        else _UNAVAILABLE,
        earnings_growth_qoq=_earnings_growth_qoq(quarterly),
        earnings_increase_streak_years=_increase_streak(annual) if annual else _UNAVAILABLE,
        dividend_paid_streak_years=_dividend_paid_streak(annual) if annual else _UNAVAILABLE,
        dividend_growth_streak_years=_dividend_growth_streak(annual) if annual else _UNAVAILABLE,
    )
```

(`revenues`/`net_incomes` local lists stay for `_growth_3y_avg`; `_increase_streak` no longer takes `net_incomes`.)

- [ ] **Step 4: Run the full derive test file**

Run: `uv run pytest tests/test_financial_fundamentals_derive.py -v`
Expected: PASS — the 7 pre-existing tests (incl. `test_earnings_increase_streak_counts_consecutive` which uses 4 contiguous years → value 3) plus the 3 new tests.

- [ ] **Step 5: Commit**

```bash
git add app/services/financial_fundamentals_snapshots/derive.py tests/test_financial_fundamentals_derive.py
git commit -m "fix(ROB-422): derive streak fiscal-year-gap guard + dividend-empty→unavailable (PR2a §10.1)"
```

---

## Task 2: Bulk repository read — `latest_periods_for_symbols`

**Files:**
- Modify: `app/services/financial_fundamentals_snapshots/repository.py`
- Test: `tests/test_financial_fundamentals_snapshots_repository.py`

- [ ] **Step 1: Write the failing integration test (append)**

```python
@pytest.mark.integration
@pytest.mark.asyncio
async def test_latest_periods_for_symbols_groups_by_symbol(db_session):
    repo = FinancialFundamentalsSnapshotsRepository(db_session)
    await repo.upsert(
        [
            _row("2023A", 100, filing_date=dt.date(2024, 3, 20)),
            _row("2024A", 200, filing_date=dt.date(2025, 3, 20)),
        ]
    )
    # a second symbol
    other = _row("2024A", 50, filing_date=dt.date(2025, 3, 20))
    other_dict = other.model_dump()
    other_dict["symbol"] = "000660"
    await repo.upsert([FinancialFundamentalsUpsert(**other_dict)])
    await db_session.commit()

    grouped = await repo.latest_periods_for_symbols(market="kr", symbols=["005930", "000660", "999999"])
    assert set(grouped) == {"005930", "000660"}  # missing symbol absent, not error
    assert [r.fiscal_period for r in grouped["005930"]] == ["2023A", "2024A"]  # asc
    assert len(grouped["000660"]) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_financial_fundamentals_snapshots_repository.py::test_latest_periods_for_symbols_groups_by_symbol -v`
Expected: FAIL with `AttributeError: ... has no attribute 'latest_periods_for_symbols'`.

- [ ] **Step 3: Add the method to `FinancialFundamentalsSnapshotsRepository`**

```python
    async def latest_periods_for_symbols(
        self,
        *,
        market: str,
        symbols: Iterable[str],
        period_type: str | None = None,
    ) -> dict[str, list[FinancialFundamentalsSnapshot]]:
        """symbol -> period_end_date-ascending rows. One query (no N+1).

        Missing symbols are simply absent from the returned dict (no error).
        """
        norm_market = market.strip().lower()
        norm_symbols = {s.strip().upper() for s in symbols if s.strip()}
        if not norm_symbols:
            return {}
        stmt = select(FinancialFundamentalsSnapshot).where(
            FinancialFundamentalsSnapshot.market == norm_market,
            FinancialFundamentalsSnapshot.symbol.in_(norm_symbols),
        )
        if period_type is not None:
            stmt = stmt.where(FinancialFundamentalsSnapshot.period_type == period_type)
        stmt = stmt.order_by(
            FinancialFundamentalsSnapshot.symbol.asc(),
            FinancialFundamentalsSnapshot.period_end_date.asc(),
            FinancialFundamentalsSnapshot.fiscal_period.asc(),
        )
        result = await self._session.execute(stmt)
        grouped: dict[str, list[FinancialFundamentalsSnapshot]] = {}
        for row in result.scalars().all():
            grouped.setdefault(row.symbol, []).append(row)
        return grouped
```

(`Iterable` is already imported in `repository.py`; confirm the import line includes it.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_financial_fundamentals_snapshots_repository.py -v`
Expected: PASS (existing 2 + new 1).

- [ ] **Step 5: Commit**

```bash
git add app/services/financial_fundamentals_snapshots/repository.py tests/test_financial_fundamentals_snapshots_repository.py
git commit -m "feat(ROB-422): bulk latest_periods_for_symbols repository read (PR2a)"
```

---

## Task 3: Fundamentals screener loader (pure core + DB orchestration)

**Files:**
- Create: `app/services/invest_view_model/fundamentals_screener.py`
- Test: `tests/test_fundamentals_screener.py`

- [ ] **Step 1: Write the failing tests for the pure evaluation core**

```python
# tests/test_fundamentals_screener.py
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from app.services.financial_fundamentals_snapshots.derive import FundamentalPeriod
from app.services.invest_view_model.fundamentals_screener import (
    PROFITABLE_COMPANY_SPEC,
    evaluate_fundamentals_candidates,
)


def _period(year: int, *, revenue, cost_of_sales, filing_date) -> FundamentalPeriod:
    return FundamentalPeriod(
        fiscal_period=f"{year}A",
        period_type="annual",
        period_end_date=dt.date(year, 12, 31),
        filing_date=filing_date,
        revenue=Decimal(revenue),
        net_income=Decimal("100"),
        cost_of_sales=Decimal(cost_of_sales),
        discrete_revenue=Decimal(revenue),
        discrete_net_income=Decimal("100"),
    )


def test_includes_symbol_meeting_roe_and_gross_margin():
    valuation_rows = [{"symbol": "005930", "roe": 20.0, "per": 8.0, "pbr": 1.2, "market_cap": 5e11}]
    periods = {"005930": [_period(2024, revenue="1000", cost_of_sales="700", filing_date=dt.date(2025, 3, 20))]}
    rows, excluded = evaluate_fundamentals_candidates(
        valuation_rows=valuation_rows, periods_by_symbol=periods,
        spec=PROFITABLE_COMPANY_SPEC, report_date=dt.date(2025, 6, 1),
        limit=20, name_map={"005930": "삼성전자"},
    )
    # gross margin = (1000-700)/1000 = 0.30 >= 0.20, roe 20 >= 15 → included
    assert [r["symbol"] for r in rows] == ["005930"]
    assert rows[0]["gross_margin_ttm"] == 0.30


def test_excludes_when_gross_margin_below_threshold():
    valuation_rows = [{"symbol": "005930", "roe": 20.0, "per": 8.0, "pbr": 1.2, "market_cap": 5e11}]
    periods = {"005930": [_period(2024, revenue="1000", cost_of_sales="900", filing_date=dt.date(2025, 3, 20))]}  # margin 0.10
    rows, excluded = evaluate_fundamentals_candidates(
        valuation_rows=valuation_rows, periods_by_symbol=periods,
        spec=PROFITABLE_COMPANY_SPEC, report_date=dt.date(2025, 6, 1), limit=20, name_map={},
    )
    assert rows == []
    assert excluded[0]["symbol"] == "005930" and "gross_margin" in excluded[0]["reason"]


def test_excludes_when_fundamentals_unavailable_never_silent_pass():
    valuation_rows = [{"symbol": "005930", "roe": 20.0, "per": 8.0, "pbr": 1.2, "market_cap": 5e11}]
    rows, excluded = evaluate_fundamentals_candidates(
        valuation_rows=valuation_rows, periods_by_symbol={},  # no fundamentals
        spec=PROFITABLE_COMPANY_SPEC, report_date=dt.date(2025, 6, 1), limit=20, name_map={},
    )
    assert rows == []
    assert excluded[0]["reason"] == "gross_margin_ttm unavailable"


def test_pit_gate_excludes_unfiled_period():
    valuation_rows = [{"symbol": "005930", "roe": 20.0, "per": 8.0, "pbr": 1.2, "market_cap": 5e11}]
    periods = {"005930": [_period(2024, revenue="1000", cost_of_sales="700", filing_date=dt.date(2025, 3, 20))]}
    rows, excluded = evaluate_fundamentals_candidates(
        valuation_rows=valuation_rows, periods_by_symbol=periods,
        spec=PROFITABLE_COMPANY_SPEC, report_date=dt.date(2025, 1, 1),  # before filing
        limit=20, name_map={},
    )
    assert rows == []  # period not yet filed as of report_date → unavailable → excluded
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_fundamentals_screener.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write `fundamentals_screener.py`**

```python
"""Read-only loader for fundamentals-backed Toss-parity screener presets (ROB-422 PR2a).

Candidate universe comes from the latest market_valuation_snapshots partition
(valuation conditions, e.g. ROE). Each candidate's financial_fundamentals_snapshots
periods are run through the pure PIT-gated derive_fundamentals_metrics(report_date=today),
and the preset's fundamentals thresholds are applied. A metric whose state is not
'ok' excludes the candidate (never a silent pass). When the fundamentals table has
no rows (operator backfill pending), the result is empty with a 'missing' fundamentals
dependency state — honest, never fabricated.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.financial_fundamentals_snapshot import FinancialFundamentalsSnapshot
from app.models.kr_symbol_universe import KRSymbolUniverse
from app.models.market_valuation_snapshot import MarketValuationSnapshot
from app.services.financial_fundamentals_snapshots.derive import (
    FundamentalPeriod,
    derive_fundamentals_metrics,
)
from app.services.financial_fundamentals_snapshots.repository import (
    FinancialFundamentalsSnapshotsRepository,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FundamentalsPresetSpec:
    preset_id: str
    min_roe: Decimal | None = None              # valuation.roe, percent (e.g. 15)
    min_gross_margin_ttm: Decimal | None = None  # derive ratio (e.g. 0.20)
    sort_by: str = "roe"                          # 'roe' | 'gross_margin_ttm'
    # PR2b extension slots (unused in PR2a):
    # min_revenue_growth_3y_avg / min_earnings_growth_3y_avg / min_payout_ratio /
    # min_dividend_yield / min_earnings_increase_streak_years / ... / max_per / max_pbr


PROFITABLE_COMPANY_SPEC = FundamentalsPresetSpec(
    preset_id="profitable_company",
    min_roe=Decimal("15"),
    min_gross_margin_ttm=Decimal("0.20"),
    sort_by="roe",
)


@dataclass(frozen=True)
class FundamentalsScreenResult:
    rows: list[dict[str, Any]]
    valuation_partition_date: dt.date | None
    fundamentals_partition_date: dt.date | None
    fundamentals_collected_at: dt.datetime | None
    fundamentals_state: str  # 'fresh' | 'stale' | 'missing'
    excluded: list[dict[str, Any]] = field(default_factory=list)


def _to_period(row: FinancialFundamentalsSnapshot) -> FundamentalPeriod:
    return FundamentalPeriod(
        fiscal_period=row.fiscal_period,
        period_type=row.period_type,
        period_end_date=row.period_end_date,
        filing_date=row.filing_date,
        revenue=row.revenue,
        net_income=row.net_income,
        gross_profit=row.gross_profit,
        cost_of_sales=row.cost_of_sales,
        discrete_revenue=row.discrete_revenue,
        discrete_net_income=row.discrete_net_income,
        payout_ratio=row.payout_ratio,
        dividend_per_share=row.dividend_per_share,
        roe=row.roe,
    )


def evaluate_fundamentals_candidates(
    *,
    valuation_rows: list[dict[str, Any]],
    periods_by_symbol: dict[str, list[FundamentalPeriod]],
    spec: FundamentalsPresetSpec,
    report_date: dt.date,
    limit: int,
    name_map: dict[str, str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Pure: apply the preset spec to candidates. Returns (included_rows, excluded)."""
    included: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for v in valuation_rows:
        symbol = v["symbol"]
        periods = periods_by_symbol.get(symbol, [])
        derivation = derive_fundamentals_metrics(periods, report_date=report_date)
        if spec.min_gross_margin_ttm is not None:
            gm = derivation.gross_margin_ttm
            if gm.state != "ok" or gm.value is None:
                excluded.append({"symbol": symbol, "reason": "gross_margin_ttm unavailable"})
                continue
            if Decimal(str(gm.value)) < spec.min_gross_margin_ttm:
                excluded.append({"symbol": symbol, "reason": "gross_margin_ttm below threshold"})
                continue
        gm_value = (
            float(derivation.gross_margin_ttm.value)
            if derivation.gross_margin_ttm.value is not None
            else None
        )
        included.append(
            {
                "symbol": symbol,
                "market": "kr",
                "name": name_map.get(symbol),
                "roe": float(v["roe"]) if v.get("roe") is not None else None,
                "per": float(v["per"]) if v.get("per") is not None else None,
                "pbr": float(v["pbr"]) if v.get("pbr") is not None else None,
                "market_cap": float(v["market_cap"]) if v.get("market_cap") is not None else None,
                "gross_margin_ttm": gm_value,
                "_screener_snapshot_state": v.get("_screener_snapshot_state", "fresh"),
            }
        )
    sort_key = "roe" if spec.sort_by == "roe" else "gross_margin_ttm"
    included.sort(key=lambda r: (r.get(sort_key) is None, -(r.get(sort_key) or 0.0), r["symbol"]))
    return included[:limit], excluded


async def load_fundamentals_preset_from_snapshots(
    session: AsyncSession | None,
    *,
    market: str,
    spec: FundamentalsPresetSpec,
    limit: int = 20,
    now: Any = None,
) -> FundamentalsScreenResult | None:
    """None when no valuation partition exists (caller → dataState=missing)."""
    if session is None or market != "kr":
        return None
    from datetime import UTC, datetime

    from app.services.invest_screener_snapshots.freshness import today_trading_date
    from app.services.invest_view_model.screener_service import _is_kr_toss_common_stock

    now_dt = now() if callable(now) else datetime.now(UTC)
    today_market_date = today_trading_date("kr", now=now_dt)

    try:
        val_date = (
            await session.execute(
                sa.select(sa.func.max(MarketValuationSnapshot.snapshot_date)).where(
                    MarketValuationSnapshot.market == "kr"
                )
            )
        ).scalar_one_or_none()
    except Exception as exc:  # noqa: BLE001
        logger.warning("fundamentals_screener: val date lookup failed: %s", exc, exc_info=True)
        return None
    if val_date is None:
        return None

    cand_stmt = sa.select(
        MarketValuationSnapshot.symbol,
        MarketValuationSnapshot.roe,
        MarketValuationSnapshot.per,
        MarketValuationSnapshot.pbr,
        MarketValuationSnapshot.market_cap,
    ).where(
        MarketValuationSnapshot.market == "kr",
        MarketValuationSnapshot.snapshot_date == val_date,
    )
    if spec.min_roe is not None:
        cand_stmt = cand_stmt.where(MarketValuationSnapshot.roe >= spec.min_roe)
    cand_stmt = cand_stmt.order_by(MarketValuationSnapshot.roe.desc().nullslast()).limit(
        max(limit * 6, limit + 60)
    )
    cand_mappings = list((await session.execute(cand_stmt)).mappings().all())

    val_state = "fresh" if val_date == today_market_date else "stale"
    symbols = [m["symbol"] for m in cand_mappings]

    name_map: dict[str, str] = {}
    if symbols:
        names = await session.execute(
            sa.select(KRSymbolUniverse.symbol, KRSymbolUniverse.name).where(
                KRSymbolUniverse.symbol.in_(symbols), KRSymbolUniverse.is_active.is_(True)
            )
        )
        name_map = {r.symbol: r.name for r in names.all()}

    # common-stock filter (drop ETF/preferred) before fundamentals work
    valuation_rows = [
        {**dict(m), "_screener_snapshot_state": val_state}
        for m in cand_mappings
        if _is_kr_toss_common_stock(m["symbol"], name_map.get(m["symbol"]))
    ]

    repo = FinancialFundamentalsSnapshotsRepository(session)
    period_rows = await repo.latest_periods_for_symbols(
        market="kr", symbols=[v["symbol"] for v in valuation_rows]
    )
    periods_by_symbol = {sym: [_to_period(r) for r in rows] for sym, rows in period_rows.items()}

    # fundamentals partition metadata + state (missing when nothing backfilled)
    fund_date = None
    fund_collected: dt.datetime | None = None
    if period_rows:
        all_rows = [r for rows in period_rows.values() for r in rows]
        fund_date = max((r.period_end_date for r in all_rows), default=None)
        fund_collected = max((r.source_collected_at for r in all_rows), default=None)
    fundamentals_state = "missing" if not period_rows else "fresh"

    included, excluded = evaluate_fundamentals_candidates(
        valuation_rows=valuation_rows,
        periods_by_symbol=periods_by_symbol,
        spec=spec,
        report_date=today_market_date,
        limit=limit,
        name_map=name_map,
    )
    for r in included:
        r["snapshot_date"] = val_date
    return FundamentalsScreenResult(
        rows=included,
        valuation_partition_date=val_date,
        fundamentals_partition_date=fund_date,
        fundamentals_collected_at=fund_collected,
        fundamentals_state=fundamentals_state,
        excluded=excluded,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_fundamentals_screener.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add app/services/invest_view_model/fundamentals_screener.py tests/test_fundamentals_screener.py
git commit -m "feat(ROB-422): fundamentals_screener loader + PROFITABLE_COMPANY_SPEC (PR2a)"
```

---

## Task 4: `profitable_company` preset catalog entry

**Files:**
- Modify: `app/services/invest_view_model/screener_presets.py`
- Test: `tests/test_screener_presets_profitable_company.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_screener_presets_profitable_company.py
from __future__ import annotations

from app.services.invest_view_model.screener_presets import (
    _KR_ONLY_PRESET_IDS,
    build_screener_presets,
)


def test_profitable_company_preset_present_and_full_parity():
    presets = {p.id: p for p in build_screener_presets(market="kr")}
    assert "profitable_company" in presets
    p = presets["profitable_company"]
    assert p.name == "돈 잘버는 회사"
    assert p.presetOrigin == "toss_parity"
    assert p.parityStatus == "full"
    chip_labels = {c.label for c in p.filterChips}
    assert {"매출총이익률", "ROE"} <= chip_labels
    assert "profitable_company" in _KR_ONLY_PRESET_IDS
```

> **Verify first:** the exact public builder is `build_screener_presets(market=...)` — confirm the function name/signature in `screener_presets.py` (the grounding referenced `SCREENER_PRESETS` list + a builder). If the builder differs, adjust the test's import accordingly before Step 2.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_screener_presets_profitable_company.py -v`
Expected: FAIL (`profitable_company` not in presets).

- [ ] **Step 3: Add the preset to `screener_presets.py`**

Add to the `SCREENER_PRESETS` list (next to `high_yield_value`), following the exact `ScreenerPreset` shape used there:

```python
    ScreenerPreset(
        id="profitable_company",
        name="돈 잘버는 회사",
        description="매출총이익률(TTM)과 ROE가 모두 높은 고수익성 기업",
        badges=["국내"],
        filterChips=[
            ScreenerFilterChip(label="국내"),
            ScreenerFilterChip(label="매출총이익률", detail="TTM 20% 이상"),
            ScreenerFilterChip(label="ROE", detail="15% 이상"),
            ScreenerFilterChip(label="데이터", detail="지연 스냅샷 기반"),
        ],
        metricLabel="ROE",
        market="kr",
        presetOrigin="toss_parity",
        parityStatus="full",
    ),
```

Add `"profitable_company"` to the `_KR_ONLY_PRESET_IDS` set (line ~27).

> Note: `profitable_company` is snapshot-only and does NOT go through the generic provider, so it needs NO `_SCREENING_FILTERS` entry (its thresholds live in `PROFITABLE_COMPANY_SPEC`). Confirm no test asserts every preset has a `_SCREENING_FILTERS` key; if one does, add a snapshot-only exemption mirroring how `high_yield_value` is handled (it has an entry but is snapshot-only — match whichever pattern the repo uses for high_yield_value to avoid a drift test failure).

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_screener_presets_profitable_company.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/invest_view_model/screener_presets.py tests/test_screener_presets_profitable_company.py
git commit -m "feat(ROB-422): profitable_company preset catalog entry (toss_parity/full)"
```

---

## Task 5: Wire dispatch + snapshot-only guard + primary_source + fundamentals dependency

**Files:**
- Modify: `app/services/invest_view_model/screener_service.py`
- Modify: `app/schemas/invest_screener.py` (only if the dependency `kind` is a constrained Literal)
- Test: `tests/test_screener_service_profitable_company.py`

- [ ] **Step 1: Verify the dependency `kind` type**

Run: `grep -n "kind" app/schemas/invest_screener.py | grep -i "depend\|Literal\|investor_flow"`
If the `ScreenerFreshnessDependency.kind` field is a `Literal[...]` that lists only `"investor_flow"`, add `"fundamentals"` to that Literal. If it is a free `str`, no schema change is needed. Note the outcome before proceeding.

- [ ] **Step 2: Write the failing test**

```python
# tests/test_screener_service_profitable_company.py
from __future__ import annotations

import datetime as dt

import pytest

from app.services.invest_view_model import screener_service
from app.services.invest_view_model.fundamentals_screener import FundamentalsScreenResult


class _StubScreening:
    async def list_screening(self, **kwargs):  # must never be called for this preset
        raise AssertionError("profitable_company must be snapshot-only")


@pytest.mark.asyncio
async def test_profitable_company_uses_fundamentals_loader_and_is_snapshot_only(monkeypatch):
    async def _fake_loader(session, *, market, spec, limit, now):
        return FundamentalsScreenResult(
            rows=[{"symbol": "005930", "market": "kr", "name": "삼성전자",
                   "roe": 20.0, "gross_margin_ttm": 0.31, "snapshot_date": dt.date(2026, 6, 2),
                   "_screener_snapshot_state": "fresh"}],
            valuation_partition_date=dt.date(2026, 6, 2),
            fundamentals_partition_date=dt.date(2025, 12, 31),
            fundamentals_collected_at=dt.datetime(2026, 6, 2, tzinfo=dt.UTC),
            fundamentals_state="fresh",
        )

    monkeypatch.setattr(
        "app.services.invest_view_model.fundamentals_screener.load_fundamentals_preset_from_snapshots",
        _fake_loader,
    )
    result = await screener_service.build_screener_results(
        preset_id="profitable_company", market="kr",
        session=object(), screening_service=_StubScreening(),
    )
    assert [row.symbol for row in result.results] == ["005930"]
    assert result.freshness.primary.source == "market_valuation_snapshots"
    dep_kinds = {d.kind for d in result.freshness.dependencies}
    assert "fundamentals" in dep_kinds


@pytest.mark.asyncio
async def test_profitable_company_missing_when_loader_returns_none(monkeypatch):
    async def _none_loader(session, *, market, spec, limit, now):
        return None

    monkeypatch.setattr(
        "app.services.invest_view_model.fundamentals_screener.load_fundamentals_preset_from_snapshots",
        _none_loader,
    )
    result = await screener_service.build_screener_results(
        preset_id="profitable_company", market="kr",
        session=object(), screening_service=_StubScreening(),
    )
    assert result.results == []
    assert result.freshness.overallState == "missing"
```

> **Verify first:** confirm `build_screener_results`'s exact signature/return type (the grounding cites it; match the real parameter names — `preset_id`, `market`/`requested_market`, `session`, `screening_service`, `now`). Adjust the call + the result accessors (`result.results`, `result.freshness.primary.source`, `result.freshness.dependencies`, `result.freshness.overallState`) to the real shapes before Step 3. Mirror an existing screener_service test (e.g. the high_yield_value / investor_flow service test) for the precise harness.

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/test_screener_service_profitable_company.py -v`
Expected: FAIL (no `profitable_company` dispatch branch yet).

- [ ] **Step 4: Add the dispatch branch (after the `high_yield_value` block, ~line 1550)**

```python
        elif preset_id == "profitable_company":
            from app.services.invest_view_model.fundamentals_screener import (
                PROFITABLE_COMPANY_SPEC,
                load_fundamentals_preset_from_snapshots,
            )

            _fundamentals_screen_result = await load_fundamentals_preset_from_snapshots(
                session,
                market=requested_market,
                spec=PROFITABLE_COMPANY_SPEC,
                limit=int(filters.get("limit") or _SNAPSHOT_FIRST_LIMIT),
                now=now,
            )
            if _fundamentals_screen_result is not None:
                _snapshot_check_result = _fundamentals_screen_result.rows
                _snapshot_load_result = _SnapshotLoadResult(
                    rows=_fundamentals_screen_result.rows,
                    partition_date=_fundamentals_screen_result.valuation_partition_date,
                )
            _snapshot_empty_warning = (
                "최신 밸류에이션/재무 스냅샷에서 돈 잘버는 회사 조건"
                "(매출총이익률 TTM 20%↑·ROE 15%↑)에 맞는 종목이 없습니다."
            )
```

Initialize `_fundamentals_screen_result: FundamentalsScreenResult | None = None` near the other snapshot locals (where `_snapshot_load_result` is initialized, ~before line 1502). Add the import of `FundamentalsScreenResult` for the annotation at the top of the function or use `Any`-free local init `_fundamentals_screen_result = None`.

- [ ] **Step 5: Add the snapshot-only guard (after the high_yield_value guard, ~line 1587)**

```python
    if preset_id == "profitable_company" and _snapshot_check_result is None:
        # snapshot-only; the generic provider has neither a gross-margin nor a
        # fundamentals filter and could half-apply the rule — never fall through.
        _snapshot_check_result = []
        _snapshot_state_override = "missing"
        _snapshot_empty_warning = (
            "밸류에이션/재무 스냅샷이 아직 적재되지 않아 돈 잘버는 회사 후보를 표시할 수 없습니다."
        )
```

- [ ] **Step 6: Add `primary_source` (in the elif chain, after the high_yield_value branch ~line 1678)**

```python
        elif preset_id == "profitable_company":
            primary_source = "market_valuation_snapshots"
```

- [ ] **Step 7: Append the fundamentals dependency (after the investor_flow dependency block, ~line 1757)**

```python
    if (
        requested_market == "kr"
        and _fundamentals_screen_result is not None
    ):
        dependency_specs.append(
            {
                "kind": "fundamentals",
                "snapshot_date": _fundamentals_screen_result.fundamentals_partition_date,
                "collected_at": _fundamentals_screen_result.fundamentals_collected_at,
                "data_state": _fundamentals_screen_result.fundamentals_state,
                "source": "financial_fundamentals_snapshots",
            }
        )
```

- [ ] **Step 8: Run test to verify it passes**

Run: `uv run pytest tests/test_screener_service_profitable_company.py -v`
Expected: PASS (2 tests). If `overallState` is not `missing` in the second test, confirm `_snapshot_state_override="missing"` propagates into `_aggregated_data_state` for the no-valuation-partition path (it should via the same path high_yield_value uses).

- [ ] **Step 9: Commit**

```bash
git add app/services/invest_view_model/screener_service.py app/schemas/invest_screener.py tests/test_screener_service_profitable_company.py
git commit -m "feat(ROB-422): wire profitable_company dispatch + fundamentals freshness dependency (PR2a)"
```

---

## Task 6: Parity matrix doc update

**Files:**
- Modify: `docs/invest-screener-toss-parity-matrix.md`

- [ ] **Step 1: Update the 돈잘버는회사 row**

Change the 돈 잘버는 회사 (#5) matrix row from `missing / —` to `full / profitable_company` and update its note to: implemented in ROB-422 PR2a via `gross_margin_ttm`(financial_fundamentals_snapshots) + `roe`(market_valuation_snapshots); data is operator-backfill-gated (empty → dataState=missing until backfill). Leave the other 7 in-scope presets' rows unchanged (PR2b/c).

- [ ] **Step 2: Commit**

```bash
git add docs/invest-screener-toss-parity-matrix.md
git commit -m "docs(ROB-422): parity matrix — 돈잘버는회사 full (profitable_company, PR2a)"
```

---

## Task 7: Verification + lint + regression

**Files:** none (verification only)

- [ ] **Step 1: Run all PR2a-touched tests**

Run: `uv run pytest tests/test_financial_fundamentals_derive.py tests/test_financial_fundamentals_snapshots_repository.py tests/test_fundamentals_screener.py tests/test_screener_presets_profitable_company.py tests/test_screener_service_profitable_company.py -v`
Expected: all PASS.

- [ ] **Step 2: Regression — existing screener + fundamentals suites**

Run: `uv run pytest tests/test_invest_view_model_high_yield_value_screener.py tests/test_financial_fundamentals_builder_parse.py tests/test_financial_fundamentals_builder_orchestration.py tests/test_financial_fundamentals_job.py tests/test_invest_coverage_valuation.py -v`
Expected: all PASS (full-3 presets + valuation path unaffected).

- [ ] **Step 3: Lint + format (both)**

Run: `uv run ruff check app/ tests/ && uv run ruff format --check app/services/invest_view_model/fundamentals_screener.py app/services/financial_fundamentals_snapshots/derive.py app/services/financial_fundamentals_snapshots/repository.py tests/test_fundamentals_screener.py tests/test_screener_service_profitable_company.py tests/test_screener_presets_profitable_company.py`
Expected: clean. (If format fails: `uv run ruff format <files>` then re-commit.)

- [ ] **Step 4: Type check**

Run: `uv run ty check app/services/invest_view_model/fundamentals_screener.py`
Expected: clean (or only pre-existing repo-wide noise).

- [ ] **Step 5: Commit any fixups**

```bash
git add -A
git commit -m "chore(ROB-422): PR2a lint/format fixups" || echo "nothing to commit"
```

---

## Self-Review (completed by plan author)

**Spec coverage (spec §-by-§):**
- §3 derive §10.1 fixes (streak year-gap guard, dividend-empty→unavailable, coverage) → Task 1. ✓
- §4 bulk `latest_periods_for_symbols` → Task 2. ✓
- §5 `fundamentals_screener` loader (candidate universe via valuation ROE, bulk fundamentals, derive report_date=today, predicate, unavailable→exclude+note, empty→missing, partition metadata return) → Task 3. ✓
- §6 data_state mapping + `'fundamentals'` dependency → Task 5 (Steps 1, 7). ✓
- §7 preset wiring (catalog entry full/toss_parity + dispatch + snapshot-only guard + primary_source + doc) → Tasks 4, 5, 6. ✓
- §8 tests → every task TDD; PIT gate, unavailable-exclude, empty-missing all covered (Tasks 1, 3, 5). ✓
- §9 operational reality (backfill-empty→missing) → Task 3 (`fundamentals_state="missing"`) + Task 5 Step 8 assertion. ✓
- §2 safety: read-only, no migration (no migration task), KR-only (loader `market != "kr"` → None), derive→screener direction (loader imports derive; derive imports nothing from screener). ✓

**Placeholder scan:** every code step has complete code. Three steps carry an explicit *verify-first* note (Task 4 Step 1 builder name; Task 5 Step 1 dependency-kind Literal; Task 5 Step 2 `build_screener_results` signature) — these are bounded verifications against real code, each with the concrete grep/action and a fallback, not placeholders. They exist because the exact public function name/return-shape must be matched to the live `screener_service.py`/`screener_presets.py` rather than guessed.

**Type/name consistency:** `FundamentalsPresetSpec`, `PROFITABLE_COMPANY_SPEC`, `FundamentalsScreenResult`, `evaluate_fundamentals_candidates`, `load_fundamentals_preset_from_snapshots`, `latest_periods_for_symbols`, `_to_period`, `_fundamentals_screen_result` used consistently across Tasks 2, 3, 5. The dependency dict keys (`kind/snapshot_date/collected_at/data_state/source`) match the investor_flow template at `screener_service.py:1749-1757`. `gross_margin_ttm` is a ratio (0.20 threshold); `roe` is percent (15). `_SnapshotLoadResult(rows, partition_date)` reused for primary valuation metadata (Task 5 Step 4) matches its definition at `screener_service.py:109-118`. ✓

# US Market Valuation Bulk Sourcing Transition Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Transition US market valuation snapshots from per-symbol Yahoo calls to tvscreener bulk data to prevent `getaddrinfo` thread exhaustion.

**Architecture:** Introduce a dedicated US bulk provider using `tvscreener`, update the builder to prioritize this bulk path for US 'all-symbols' runs, and strictly avoid Yahoo fan-out in the bulk path.

**Tech Stack:** Python 3.13+, tvscreener, SQLAlchemy, asyncio.

---

### Task 1: Model Migration (source enum)

**Files:**
- Modify: `app/models/market_valuation_snapshot.py`

- [ ] **Step 1: Update source CHECK constraint**
Add `'tvscreener'` to the allowed source list in `__table_args__`.

```python
        CheckConstraint(
            "source IN ('naver_finance', 'yahoo', 'toss_openapi', 'tvscreener')",
            name="ck_market_valuation_snapshots_source",
        ),
```

- [ ] **Step 2: Generate and run Alembic migration**
Run: `uv run alembic revision --autogenerate -m "Add tvscreener to market_valuation_snapshots source enum"`
Run: `uv run alembic upgrade head`

- [ ] **Step 3: Commit**
```bash
git add app/models/market_valuation_snapshot.py alembic/versions/
git commit -m "db: allow tvscreener as a source for market_valuation_snapshots"
```

---

### Task 2: US TvScreener Provider

**Files:**
- Create: `app/services/market_valuation_snapshots/us_provider.py`
- Modify: `app/services/invest_kr_fundamentals_snapshots/provider.py` (optional: extract common field mapping logic if needed, but for now isolation is preferred)

- [ ] **Step 1: Implement `TvScreenerUsValuationProvider`**
Create the file with the following bulk fetch logic:

```python
from __future__ import annotations
import logging
from typing import Any
from app.services.tvscreener_service import TvScreenerService, _import_tvscreener

logger = logging.getLogger(__name__)
_FULL_UNIVERSE_FETCH_CAP = 12_000

_US_STOCK_FIELD_SPECS = (
    ("symbol", ("ACTIVE_SYMBOL", "SYMBOL")),
    ("market_cap", ("MARKET_CAPITALIZATION", "MARKET_CAP_BASIC")),
    ("per", ("PRICE_TO_EARNINGS_RATIO_TTM", "PRICE_TO_EARNINGS_TTM")),
    ("pbr", ("PRICE_TO_BOOK_FQ", "PRICE_TO_BOOK_MRQ", "PRICE_BOOK_CURRENT")),
    ("dividend_yield", ("DIVIDENDS_YIELD", "DIVIDEND_YIELD_FORWARD")),
    ("roe", ("RETURN_ON_EQUITY_TTM",)),
    ("high_52w", ("WEEK_HIGH_52",)),
    ("low_52w", ("WEEK_LOW_52",)),
    ("high_52w_date", ("PRICE_52_WEEK_HIGH_DATE",)),
)

class TvScreenerUsValuationProvider:
    async def fetch_rows(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        is_full = limit is None or limit <= 0
        query_limit = _FULL_UNIVERSE_FETCH_CAP if is_full else limit
        tvs = _import_tvscreener()
        market = tvs.Market
        sf = tvs.StockField
        
        columns = [getattr(sf, candidates[0]) for _, candidates in _US_STOCK_FIELD_SPECS]
        service = TvScreenerService()
        df = await service.query_stock_screener(
            columns=columns,
            markets=[market.AMERICA],
            limit=query_limit,
        )
        if df is None or df.empty:
            return []
        return [row.to_dict() for _, row in df.iterrows()]
```

- [ ] **Step 2: Commit**
```bash
git add app/services/market_valuation_snapshots/us_provider.py
git commit -m "feat: add TvScreenerUsValuationProvider for US bulk sourcing"
```

---

### Task 3: Builder Refactoring (Bulk Path)

**Files:**
- Modify: `app/services/market_valuation_snapshots/builder.py`

- [ ] **Step 1: Implement `build_valuation_snapshots_bulk_for_us`**
Add the new bulk building function that maps provider rows to `MarketValuationSnapshotUpsert`.

```python
from app.services.market_valuation_snapshots.us_provider import TvScreenerUsValuationProvider

async def build_valuation_snapshots_bulk_for_us(
    *, snapshot_date: dt.date, limit: int | None = None
) -> MarketValuationBuildResult:
    provider = TvScreenerUsValuationProvider()
    rows = await provider.fetch_rows(limit=limit)
    payloads = []
    for row in rows:
        symbol = row.get("symbol", "").split(":")[-1] # Strip exchange prefix
        if not symbol: continue
        payloads.append(
            MarketValuationSnapshotUpsert(
                market="us",
                symbol=symbol,
                snapshot_date=snapshot_date,
                source="tvscreener",
                per=_to_decimal(row.get("price_earnings_ttm")),
                pbr=_to_decimal(row.get("price_book_ratio")),
                roe=_to_decimal(row.get("return_on_equity")),
                dividend_yield=_to_decimal(row.get("dividends_yield")),
                market_cap=_to_decimal(row.get("market_cap_basic")),
                high_52w=_to_decimal(row.get("price_52_week_high")),
                low_52w=_to_decimal(row.get("price_52_week_low")),
                high_52w_date=_to_date(row.get("price_52_week_high_date")),
                raw_payload=row,
            )
        )
    return MarketValuationBuildResult(payloads=tuple(payloads))
```

- [ ] **Step 2: Update `build_valuation_snapshots_for_market`**
If `market=="us"` and a specific flag (or detection of bulk intent) is present, route to the bulk function.

- [ ] **Step 3: Commit**
```bash
git add app/services/market_valuation_snapshots/builder.py
git commit -m "feat: implement bulk builder for US market valuation"
```

---

### Task 4: Job Orchestration & Verification

**Files:**
- Modify: `app/jobs/market_valuation_snapshots.py`
- Modify: `scripts/build_market_valuation_snapshots.py`

- [ ] **Step 1: Update `run_market_valuation_snapshot_build`**
Detect `request.all_symbols and market == "us"` and call `build_valuation_snapshots_bulk_for_us`.
Ensure DB commit slicing using `request.batch_size` is maintained.

- [ ] **Step 2: Add coverage probe to script**
Implement a check in `scripts/build_market_valuation_snapshots.py` to print coverage stats after a US bulk run.

- [ ] **Step 3: Dry run verification**
Run: `uv run scripts/build_market_valuation_snapshots.py --market us --all --limit 100` (Dry run)
Verify that sourcing shows `tvscreener` and coverage is reported.

- [ ] **Step 4: Commit**
```bash
git add app/jobs/market_valuation_snapshots.py scripts/build_market_valuation_snapshots.py
git commit -m "feat: route US bulk runs to tvscreener and add coverage probe"
```

# ROB-575 Investor Flow Snapshot Enrichment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix false stale investor-flow cards by using the latest completed KR trading session, then enrich `investor_flow_snapshots` with persisted price, volume, and foreign-holding fields so `/invest/stocks/kr/{symbol}` can render real daily table values.

**Architecture:** Ship this as two PRs. PR1 is migration-free and changes only investor-flow freshness plus the operator runbook. PR2 is an additive nullable-column migration that wires already-parsed Naver fields through the ORM, repository, builder, stock-detail view model, and frontend regression tests.

**Tech Stack:** Python 3.13, FastAPI service layer, SQLAlchemy 2, Alembic, Pydantic v2, pytest, React 19, TypeScript, Vitest, `uv`.

---

## Decisions

- PR split: yes. PR1 is migration 0 stale fix. PR2 is migration 1 field enrichment.
- Persist `change_rate` percent only, not absolute `change`. Store `close` too. Absolute price change can be derived later from adjacent closes.
- Keep schedule and commit gates default-off in code. Activation is operator env flip plus manual backfill, documented in the runbook.
- PR2 labels: `high_risk_change`, `needs_stronger_model_review`, `hold_for_final_review`. Context for reviewers: additive nullable 5-column migration, no destructive data change, no broker/order/live-trading boundary.
- Units: `change_rate` and `foreign_holding_rate` are stored as percent points. Examples: `change_rate=2.5` means `+2.5%`; `foreign_holding_rate=47.73` means `47.73%`. Frontend `fmtPercent` already prints percent points and does not divide by 100.

## File Structure

### PR1 Files

- **Modify** `app/services/invest_view_model/investor_flow_service.py` - resolve default investor-flow `as_of` from the previous confirmed KR trading session instead of calendar today.
- **Modify** `tests/test_investor_flow_service.py` - add regression coverage for weekend or holiday stale false positives.
- **Modify** `docs/runbooks/invest-screener-snapshots.md` - add operator activation and rollback instructions for investor-flow schedule and commit gates.

### PR2 Files

- **Create** `alembic/versions/2026_06_15_rob575_investor_flow_snapshot_market_fields.py` - additive nullable fields on `investor_flow_snapshots`.
- **Modify** `app/models/investor_flow_snapshot.py` - ORM columns for close, change rate, volume, and foreign holdings.
- **Modify** `app/services/investor_flow_snapshots/repository.py` - upsert DTO accepts new fields and conflict update persists them.
- **Modify** `app/services/investor_flow_snapshots/builder.py` - maps Naver `close`, `change_pct`, `volume`, `foreign_holding_shares`, and `foreign_holding_rate` into upsert payloads.
- **Modify** `app/services/invest_view_model/stock_detail_service.py` - daily rows and period summary use persisted values and remove stale "storage not loaded" labels.
- **Modify** `tests/test_investor_flow_snapshots_repository.py` - repository round-trip coverage for new fields.
- **Modify** `tests/test_investor_flow_snapshot_builder.py` - builder mapping and unit coverage.
- **Modify** `tests/test_stock_detail_investor_flow_provider.py` - stock-detail API view-model coverage for table and summary fields.
- **Modify** `frontend/invest/src/__tests__/StockDetailPage.test.tsx` - frontend unit rendering coverage for percent units and no ready-soon label.
- **Modify** `docs/runbooks/invest-screener-snapshots.md` - PR2 backfill and verification commands.

---

## PR1 - Stale False Positive Fix

### Task 1: Use Previous KR Trading Session For Default Investor-Flow Freshness

**Files:**
- Modify: `tests/test_investor_flow_service.py`
- Modify: `app/services/invest_view_model/investor_flow_service.py`

- [ ] **Step 1: Write failing service tests**

Add these imports near the top of `tests/test_investor_flow_service.py`:

```python
from app.services.invest_view_model import investor_flow_service as flow_service
```

Append these tests after `test_build_investor_flow_cards_marks_missing_symbol`:

```python
def test_resolve_investor_flow_as_of_defaults_to_previous_kr_session(monkeypatch):
    calls: list[tuple[str, dt.date]] = []

    def fake_previous_session(market: str, day: dt.date) -> dt.date | None:
        calls.append((market, day))
        return dt.date(2026, 6, 12)

    monkeypatch.setattr(
        flow_service, "previous_trading_session", fake_previous_session
    )

    now = dt.datetime(2026, 6, 15, 0, 5, tzinfo=dt.UTC)  # Mon 09:05 KST

    assert flow_service._resolve_investor_flow_as_of(None, now=now) == dt.date(
        2026, 6, 12
    )
    assert calls == [("kr", dt.date(2026, 6, 15))]


def test_resolve_investor_flow_as_of_keeps_explicit_effective_date(monkeypatch):
    def fail_if_called(market: str, day: dt.date) -> dt.date | None:
        raise AssertionError(f"unexpected calendar lookup: {market} {day}")

    monkeypatch.setattr(flow_service, "previous_trading_session", fail_if_called)

    assert flow_service._resolve_investor_flow_as_of(
        dt.date(2026, 5, 11)
    ) == dt.date(2026, 5, 11)


@pytest.mark.asyncio
async def test_build_investor_flow_cards_uses_previous_kr_session_by_default(
    db_session, monkeypatch
):
    repo = InvestorFlowSnapshotsRepository(db_session)
    await repo.upsert(
        InvestorFlowSnapshotUpsert(
            market="kr",
            symbol="900198",
            snapshot_date=dt.date(2026, 6, 12),
            foreign_net=10,
            institution_net=20,
            individual_net=-30,
            source="naver_finance",
            collected_at=dt.datetime(2026, 6, 12, 7, 0, tzinfo=dt.UTC),
        )
    )
    await db_session.commit()

    monkeypatch.setattr(
        flow_service,
        "_resolve_investor_flow_as_of",
        lambda as_of=None, *, now=None: dt.date(2026, 6, 12),
    )

    response = await flow_service.build_investor_flow_cards(
        db=db_session,
        symbols=["900198"],
        market="kr",
        max_stale_days=1,
    )

    assert response.asOf == dt.date(2026, 6, 12)
    assert response.dataState == "fresh"
    assert response.items[0].dataState == "fresh"
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
uv run pytest tests/test_investor_flow_service.py::test_resolve_investor_flow_as_of_defaults_to_previous_kr_session tests/test_investor_flow_service.py::test_resolve_investor_flow_as_of_keeps_explicit_effective_date tests/test_investor_flow_service.py::test_build_investor_flow_cards_uses_previous_kr_session_by_default -v
```

Expected: first two tests fail with `AttributeError: module ... has no attribute '_resolve_investor_flow_as_of'`.

- [ ] **Step 3: Implement default `as_of` resolver**

In `app/services/invest_view_model/investor_flow_service.py`, add imports after `import datetime as dt`:

```python
from zoneinfo import ZoneInfo

from app.services.market_events.session_calendar import previous_trading_session
```

Add this module constant and helper after `_normalize_symbol`:

```python
_KST = ZoneInfo("Asia/Seoul")


def _resolve_investor_flow_as_of(
    as_of: dt.date | None = None, *, now: dt.datetime | None = None
) -> dt.date:
    """Effective KR investor-flow snapshot date.

    Naver daily investor-flow rows are loaded next morning for the previous KR
    session. When callers do not pass an explicit effective date, compare stored
    snapshots against the previous confirmed XKRX trading session, not calendar
    today. This removes weekend and holiday false stale banners.
    """
    if as_of is not None:
        return as_of
    moment = now or dt.datetime.now(dt.UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.UTC)
    today_kst = moment.astimezone(_KST).date()
    previous = previous_trading_session("kr", today_kst)
    return previous or today_kst
```

Replace both occurrences of:

```python
today = as_of or dt.date.today()
```

with:

```python
today = _resolve_investor_flow_as_of(as_of)
```

- [ ] **Step 4: Run focused tests**

Run:

```bash
uv run pytest tests/test_investor_flow_service.py -v
```

Expected: PASS.

- [ ] **Step 5: Run related freshness tests**

Run:

```bash
uv run pytest tests/test_market_session.py tests/services/market_events/test_session_calendar.py tests/test_investor_flow_service.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit PR1 code**

Run:

```bash
git add app/services/invest_view_model/investor_flow_service.py tests/test_investor_flow_service.py
git commit -m "fix: use kr session baseline for investor flow freshness"
```

### Task 2: Document Investor-Flow Schedule Activation Without Enabling Defaults

**Files:**
- Modify: `docs/runbooks/invest-screener-snapshots.md`

- [ ] **Step 1: Add runbook section**

Append this section after the US market valuation section in `docs/runbooks/invest-screener-snapshots.md`:

````markdown
## 12. KR investor-flow snapshots activation (ROB-575)

### Overview
`investor_flow_snapshots` backs `/invest/api/investor-flow`, `/invest/stocks/kr/{symbol}` investor-flow cards, and snapshot-backed report context. Code defaults stay paused by design:

- `INVESTOR_FLOW_SCHEDULE_ENABLED=false` means the TaskIQ cron is not registered.
- `INVESTOR_FLOW_SNAPSHOTS_COMMIT_ENABLED=false` means scheduled execution is dry-run only.

### Manual dry-run

```bash
uv run python -m scripts.build_investor_flow_snapshots --market kr --all --days 20
```

Check that `snapshotsBuilt` is non-zero, warnings are bounded, and `snapshotDateDistribution` includes the previous KR trading session.

### Manual backfill after approval

```bash
uv run python -m scripts.build_investor_flow_snapshots --market kr --all --days 20 --commit
```

### Scheduled activation after approval

Set both env vars in the worker/scheduler runtime environment:

```bash
INVESTOR_FLOW_SCHEDULE_ENABLED=true
INVESTOR_FLOW_SNAPSHOTS_COMMIT_ENABLED=true
```

The registered TaskIQ cron remains `30 8 * * 1-5` KST and is holiday-gated. It targets the previous KR trading session because Naver daily investor-flow rows finalize the next morning.

### Verification

```bash
uv run python -m scripts.build_investor_flow_snapshots --market kr --all --days 20
uv run pytest tests/test_investor_flow_service.py tests/test_investor_flow_snapshot_tasks.py tests/test_snapshot_schedulers_rob438.py -v
```

For a known KR symbol, confirm the investor-flow card no longer shows a stale banner on weekends or holidays when the latest stored snapshot equals the previous KR trading session.

### Rollback

Set both env vars back to false and restart the scheduler/worker runtime:

```bash
INVESTOR_FLOW_SCHEDULE_ENABLED=false
INVESTOR_FLOW_SNAPSHOTS_COMMIT_ENABLED=false
```

This stops cron registration and DB writes. Existing rows remain read-only.
````

- [ ] **Step 2: Verify runbook text**

Run:

```bash
rg -n "KR investor-flow snapshots activation|INVESTOR_FLOW_SCHEDULE_ENABLED|INVESTOR_FLOW_SNAPSHOTS_COMMIT_ENABLED" docs/runbooks/invest-screener-snapshots.md
```

Expected: all three strings are found in the new section.

- [ ] **Step 3: Commit PR1 runbook**

Run:

```bash
git add docs/runbooks/invest-screener-snapshots.md
git commit -m "docs: add investor flow snapshot activation runbook"
```

---

## PR2 - Additive Snapshot Field Enrichment

### Task 3: Add Nullable Columns To ORM, Migration, And Repository

**Files:**
- Create: `alembic/versions/2026_06_15_rob575_investor_flow_snapshot_market_fields.py`
- Modify: `app/models/investor_flow_snapshot.py`
- Modify: `app/services/investor_flow_snapshots/repository.py`
- Modify: `tests/test_investor_flow_snapshots_repository.py`

- [ ] **Step 1: Write failing repository test**

In `tests/test_investor_flow_snapshots_repository.py`, add this import after `import datetime as dt`:

```python
from decimal import Decimal
```

In `test_upsert_and_latest_by_symbols_returns_fresh_snapshot`, add these fields to the `InvestorFlowSnapshotUpsert(...)` call:

```python
            close=Decimal("75000"),
            change_rate=Decimal("2.5"),
            volume=15_118_684,
            foreign_holding_shares=2_790_424_635,
            foreign_holding_rate=Decimal("47.73"),
```

Add these assertions after `assert row.individual_net == -1_500_000`:

```python
    assert row.close == Decimal("75000")
    assert row.change_rate == Decimal("2.5")
    assert row.volume == 15_118_684
    assert row.foreign_holding_shares == 2_790_424_635
    assert row.foreign_holding_rate == Decimal("47.73")
```

- [ ] **Step 2: Run repository test to verify failure**

Run:

```bash
uv run pytest tests/test_investor_flow_snapshots_repository.py::test_upsert_and_latest_by_symbols_returns_fresh_snapshot -v
```

Expected: FAIL with Pydantic `extra_forbidden` for the new upsert fields.

- [ ] **Step 3: Add ORM columns**

In `app/models/investor_flow_snapshot.py`, add `Numeric` to the SQLAlchemy imports:

```python
    Numeric,
```

Add these columns after `snapshot_date`:

```python
    close: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    change_rate: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)
    volume: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    foreign_holding_shares: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    foreign_holding_rate: Mapped[Decimal | None] = mapped_column(
        Numeric(10, 4), nullable=True
    )
```

Add this import at the top:

```python
from decimal import Decimal
```

- [ ] **Step 4: Add repository DTO fields**

In `app/services/investor_flow_snapshots/repository.py`, add this import:

```python
from decimal import Decimal
```

Add these fields to `InvestorFlowSnapshotUpsert` after `snapshot_date`:

```python
    close: Decimal | None = None
    change_rate: Decimal | None = None
    volume: int | None = None
    foreign_holding_shares: int | None = None
    foreign_holding_rate: Decimal | None = None
```

The existing `_with_derived_flags()` and `on_conflict_do_update()` already include all `model_dump(exclude_none=True)` keys except conflict keys, so no extra repository logic is needed.

- [ ] **Step 5: Create Alembic migration**

Create `alembic/versions/2026_06_15_rob575_investor_flow_snapshot_market_fields.py`:

```python
"""Add market fields to investor flow snapshots for ROB-575.

Revision ID: 20260615_rob575
Revises: 20260615_rob568_us_fx_pnl
Create Date: 2026-06-15
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260615_rob575"
down_revision = "20260615_rob568_us_fx_pnl"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "investor_flow_snapshots",
        sa.Column("close", sa.Numeric(20, 6), nullable=True),
    )
    op.add_column(
        "investor_flow_snapshots",
        sa.Column("change_rate", sa.Numeric(10, 4), nullable=True),
    )
    op.add_column(
        "investor_flow_snapshots",
        sa.Column("volume", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "investor_flow_snapshots",
        sa.Column("foreign_holding_shares", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "investor_flow_snapshots",
        sa.Column("foreign_holding_rate", sa.Numeric(10, 4), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("investor_flow_snapshots", "foreign_holding_rate")
    op.drop_column("investor_flow_snapshots", "foreign_holding_shares")
    op.drop_column("investor_flow_snapshots", "volume")
    op.drop_column("investor_flow_snapshots", "change_rate")
    op.drop_column("investor_flow_snapshots", "close")
```

- [ ] **Step 6: Run migration and repository tests**

Run:

```bash
uv run alembic upgrade head
uv run pytest tests/test_investor_flow_snapshots_repository.py -v
```

Expected: Alembic upgrade succeeds and repository tests pass.

- [ ] **Step 7: Commit PR2 persistence foundation**

Run:

```bash
git add alembic/versions/2026_06_15_rob575_investor_flow_snapshot_market_fields.py app/models/investor_flow_snapshot.py app/services/investor_flow_snapshots/repository.py tests/test_investor_flow_snapshots_repository.py
git commit -m "feat: add investor flow snapshot market fields"
```

### Task 4: Map Existing Naver Fields Into Snapshot Payloads

**Files:**
- Modify: `tests/test_investor_flow_snapshot_builder.py`
- Modify: `app/services/investor_flow_snapshots/builder.py`

- [ ] **Step 1: Write failing builder assertions**

In `tests/test_investor_flow_snapshot_builder.py`, add:

```python
from decimal import Decimal
```

Replace the first `900301` fixture row in `_fake_fetcher` with:

```python
            {
                "date": "2026-05-12",
                "close": 75000,
                "change_pct": 2.5,
                "volume": 15_118_684,
                "foreign_net": 300,
                "institutional_net": 200,
                "foreign_holding_shares": 2_790_424_635,
                "foreign_holding_rate": 47.73,
            },
```

Add these assertions after `assert newest.individual_net == -500`:

```python
    assert newest.close == Decimal("75000")
    assert newest.change_rate == Decimal("2.5")
    assert newest.volume == 15_118_684
    assert newest.foreign_holding_shares == 2_790_424_635
    assert newest.foreign_holding_rate == Decimal("47.73")
```

- [ ] **Step 2: Run builder test to verify failure**

Run:

```bash
uv run pytest tests/test_investor_flow_snapshot_builder.py::test_build_investor_flow_snapshots_derives_streaks_ranks_and_individual -v
```

Expected: FAIL because `InvestorFlowSnapshotUpsert` payloads do not yet carry the new values.

- [ ] **Step 3: Add decimal parser and field mapping**

In `app/services/investor_flow_snapshots/builder.py`, add this import:

```python
from decimal import Decimal, InvalidOperation
```

Add this helper after `_int_or_none`:

```python
def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
```

In the `InvestorFlowSnapshotUpsert(...)` call inside `build_symbol`, add these fields after `snapshot_date=snapshot_date`:

```python
                    close=_decimal_or_none(row.get("close")),
                    change_rate=_decimal_or_none(row.get("change_pct")),
                    volume=_int_or_none(row.get("volume")),
                    foreign_holding_shares=_int_or_none(
                        row.get("foreign_holding_shares")
                    ),
                    foreign_holding_rate=_decimal_or_none(
                        row.get("foreign_holding_rate")
                    ),
```

- [ ] **Step 4: Run builder tests**

Run:

```bash
uv run pytest tests/test_investor_flow_snapshot_builder.py tests/test_naver_finance.py::TestFetchInvestorTrends -v
```

Expected: PASS. `TestFetchInvestorTrends` confirms upstream Naver returns `foreign_holding_rate=47.73` as percent points.

- [ ] **Step 5: Commit PR2 builder mapping**

Run:

```bash
git add app/services/investor_flow_snapshots/builder.py tests/test_investor_flow_snapshot_builder.py
git commit -m "feat: persist naver investor flow market fields"
```

### Task 5: Wire Stock Detail Rows And Summaries

**Files:**
- Modify: `tests/test_stock_detail_investor_flow_provider.py`
- Modify: `app/services/invest_view_model/stock_detail_service.py`

- [ ] **Step 1: Add failing stock-detail row mapping test**

Add these imports in `tests/test_stock_detail_investor_flow_provider.py`:

```python
from datetime import date
from decimal import Decimal
```

Append this test before `test_kr_detail_default_investor_flow_includes_daily_rows`:

```python
def test_daily_row_from_snapshot_carries_persisted_market_fields():
    from app.services.invest_view_model.stock_detail_service import (
        _daily_row_from_snapshot,
    )

    row = SimpleNamespace(
        snapshot_date=date(2026, 5, 13),
        collected_at=None,
        source="naver_finance",
        close=Decimal("75000"),
        change_rate=Decimal("2.5"),
        volume=15_118_684,
        foreign_net=20_859,
        foreign_holding_shares=2_790_424_635,
        foreign_holding_rate=Decimal("47.73"),
        institution_net=-12_931,
        individual_net=125_586,
        double_buy=False,
        double_sell=False,
    )

    out = _daily_row_from_snapshot(row)

    assert out.close == 75000.0
    assert out.changeRate == 2.5
    assert out.volume == 15_118_684
    assert out.foreignHoldingShares == 2_790_424_635
    assert out.foreignHoldingRate == 47.73
```

- [ ] **Step 2: Update summary test fixture**

In `test_kr_detail_default_investor_flow_includes_daily_rows`, add these values to the first `StockDetailInvestorFlowDailyRow(...)`:

```python
                    close=75000,
                    changeRate=2.5,
                    volume=15_118_684,
                    foreignHoldingShares=2_790_424_635,
                    foreignHoldingRate=47.73,
```

Add these values to the second `StockDetailInvestorFlowDailyRow(...)`:

```python
                    close=73500,
                    changeRate=-1.2,
                    volume=10_000_000,
                    foreignHoldingShares=2_790_400_000,
                    foreignHoldingRate=47.71,
```

Replace these old assertions:

```python
    assert response.investorFlow.dailyRows[0].close is None
    assert response.investorFlow.periodSummary.foreignNetToVolumeRatio is None
```

with:

```python
    assert response.investorFlow.dailyRows[0].close == 75000
    assert response.investorFlow.dailyRows[0].changeRate == 2.5
    assert response.investorFlow.dailyRows[0].volume == 15_118_684
    assert response.investorFlow.dailyRows[0].foreignHoldingRate == 47.73
    assert response.investorFlow.periodSummary.foreignNetToVolumeRatio == pytest.approx(
        21299 / 25_118_684
    )
    assert response.investorFlow.periodSummary.foreignHoldingSharesChange == 24_635
    assert response.investorFlow.periodSummary.foreignHoldingRateChange == pytest.approx(
        0.02
    )
```

Replace this assertion:

```python
    assert "거래량" in " ".join(response.investorFlow.unavailableLabels)
```

with:

```python
    assert response.investorFlow.unavailableLabels == []
    assert response.investorFlow.periodSummary.unavailableLabels == []
```

- [ ] **Step 3: Run stock-detail tests to verify failure**

Run:

```bash
uv run pytest tests/test_stock_detail_investor_flow_provider.py::test_daily_row_from_snapshot_carries_persisted_market_fields tests/test_stock_detail_investor_flow_provider.py::test_kr_detail_default_investor_flow_includes_daily_rows -v
```

Expected: FAIL because `_daily_row_from_snapshot` does not map the new fields and summary labels remain non-empty.

- [ ] **Step 4: Implement stock-detail row mapping and summary changes**

In `app/services/invest_view_model/stock_detail_service.py`, replace `_daily_row_from_snapshot` with:

```python
def _daily_row_from_snapshot(row: Any) -> StockDetailInvestorFlowDailyRow:
    return StockDetailInvestorFlowDailyRow(
        snapshotDate=row.snapshot_date.isoformat(),
        collectedAt=row.collected_at,
        source=row.source,
        close=_float_or_none(getattr(row, "close", None)),
        changeRate=_float_or_none(getattr(row, "change_rate", None)),
        volume=getattr(row, "volume", None),
        foreignNet=row.foreign_net,
        foreignHoldingShares=getattr(row, "foreign_holding_shares", None),
        foreignHoldingRate=_float_or_none(
            getattr(row, "foreign_holding_rate", None)
        ),
        institutionNet=row.institution_net,
        individualNet=row.individual_net,
        doubleBuy=row.double_buy,
        doubleSell=row.double_sell,
    )
```

Add this helper after `_sum_known`:

```python
def _newest_minus_oldest_known_int(values: list[int | None]) -> int | None:
    known = [value for value in values if value is not None]
    if len(known) < 2:
        return None
    return known[0] - known[-1]


def _newest_minus_oldest_known_float(values: list[float | None]) -> float | None:
    known = [value for value in values if value is not None]
    if len(known) < 2:
        return None
    return known[0] - known[-1]
```

In `_build_period_summary`, replace:

```python
    unavailable = [
        "종가/등락률/거래량은 investor_flow_snapshots 저장소에 아직 없어 일별 표에서 준비중으로 표시됩니다.",
        "외국인 보유주수/보유율은 investor_flow_snapshots 저장소에 아직 없어 변화율을 계산하지 않습니다.",
    ]
```

with:

```python
    unavailable: list[str] = []
```

Replace:

```python
        foreignHoldingSharesChange=None,
        foreignHoldingRateChange=None,
```

with:

```python
        foreignHoldingSharesChange=_newest_minus_oldest_known_int(
            [row.foreignHoldingShares for row in daily_rows]
        ),
        foreignHoldingRateChange=_newest_minus_oldest_known_float(
            [row.foreignHoldingRate for row in daily_rows]
        ),
```

Replace `_INVESTOR_FLOW_UNAVAILABLE_LABELS` with:

```python
_INVESTOR_FLOW_UNAVAILABLE_LABELS: list[str] = []
```

In `_build_buyer_decomposition`, replace the note:

```python
        note="급등일 여부는 종가/등락률 저장 전까지 판별하지 않고, 최신 수급 행의 매수 주체 분해만 표시합니다.",
```

with:

```python
        note="최신 수급 행 기준입니다.",
```

- [ ] **Step 5: Run stock-detail tests**

Run:

```bash
uv run pytest tests/test_stock_detail_investor_flow_provider.py tests/test_invest_stock_detail_schemas.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit PR2 stock-detail wiring**

Run:

```bash
git add app/services/invest_view_model/stock_detail_service.py tests/test_stock_detail_investor_flow_provider.py
git commit -m "feat: show persisted investor flow market fields"
```

### Task 6: Verify Frontend Percent Units And Remove Ready-Soon Label

**Files:**
- Modify: `frontend/invest/src/__tests__/StockDetailPage.test.tsx`

- [ ] **Step 1: Update frontend fixture**

In the investor-flow fixture inside `StockDetailPage.test.tsx`, replace the first daily row values:

```ts
          close: null,
          changeRate: null,
          volume: null,
          foreignNet: 20859,
          foreignHoldingShares: null,
          foreignHoldingRate: null,
```

with:

```ts
          close: 75000,
          changeRate: 2.5,
          volume: 15118684,
          foreignNet: 20859,
          foreignHoldingShares: 2790424635,
          foreignHoldingRate: 47.73,
```

Replace the second daily row values:

```ts
          close: null,
          changeRate: null,
          volume: null,
          foreignNet: 440,
          foreignHoldingShares: null,
          foreignHoldingRate: null,
```

with:

```ts
          close: 73500,
          changeRate: -1.2,
          volume: 10000000,
          foreignNet: 440,
          foreignHoldingShares: 2790400000,
          foreignHoldingRate: 47.71,
```

Replace period summary fields:

```ts
        foreignNetToVolumeRatio: null,
        foreignHoldingSharesChange: null,
        foreignHoldingRateChange: null,
        unavailableLabels: ["거래량 저장 전까지 계산 불가"],
```

with:

```ts
        foreignNetToVolumeRatio: 21299 / 25118684,
        foreignHoldingSharesChange: 24635,
        foreignHoldingRateChange: 0.02,
        unavailableLabels: [],
```

Replace:

```ts
      unavailableLabels: ["외국인 순매수/거래량 강도: 거래량 저장 전까지 계산 불가"],
```

with:

```ts
      unavailableLabels: [],
```

- [ ] **Step 2: Add unit display assertions**

In the same test, add these assertions after `expect(card).toHaveTextContent("2026-05-12");`:

```ts
  expect(card).toHaveTextContent("75,000");
  expect(card).toHaveTextContent("+2.5%");
  expect(card).toHaveTextContent("15,118,684");
  expect(card).toHaveTextContent("2,790,424,635주 / 47.73%");
  expect(card).not.toHaveTextContent("준비중 지표");
```

- [ ] **Step 3: Run frontend test**

Run:

```bash
cd frontend/invest && npm test -- StockDetailPage.test.tsx
```

Expected: PASS. This confirms `changeRate=2.5` renders as `+2.5%` and `foreignHoldingRate=47.73` renders as `47.73%`, with no divide-by-100 mismatch.

- [ ] **Step 4: Commit PR2 frontend coverage**

Run:

```bash
git add frontend/invest/src/__tests__/StockDetailPage.test.tsx
git commit -m "test: cover investor flow market field rendering"
```

### Task 7: PR2 Backfill Runbook And Strong-Review Marker

**Files:**
- Modify: `docs/runbooks/invest-screener-snapshots.md`

- [ ] **Step 1: Extend ROB-575 runbook section**

In the ROB-575 section added in PR1, add this subsection before `### Verification`:

````markdown
### Market-field backfill after PR2 migration

PR2 adds nullable columns:

- `close`
- `change_rate`
- `volume`
- `foreign_holding_shares`
- `foreign_holding_rate`

The migration is additive and nullable. It does not delete or rewrite existing rows, but existing rows remain null for the new fields until the operator runs a commit backfill.

After `uv run alembic upgrade head` and approval:

```bash
uv run python -m scripts.build_investor_flow_snapshots --market kr --all --days 20 --commit
```

Read-only verification:

```bash
uv run pytest tests/test_investor_flow_snapshots_repository.py tests/test_investor_flow_snapshot_builder.py tests/test_stock_detail_investor_flow_provider.py -v
cd frontend/invest && npm test -- StockDetailPage.test.tsx
```

For a known KR symbol, confirm the investor-flow daily table shows numeric close, percent change, volume, and foreign holding fields. `change_rate` and `foreign_holding_rate` are percent points, so `2.5` renders as `2.5%` and `47.73` renders as `47.73%`.
````

- [ ] **Step 2: Add PR2 Linear label note**

When opening PR2, include this exact note in the PR body or Linear issue comment:

```markdown
Applying high_risk_change + needs_stronger_model_review + hold_for_final_review for ROB-575 PR2 because it adds an Alembic migration. The migration is additive nullable-only: five columns on `investor_flow_snapshots`, no data deletion, no live order path, no broker mutation. Holding merge until stronger-model/CTO review clears schema and rollback assumptions.
```

- [ ] **Step 3: Run full PR2 verification**

Run:

```bash
uv run pytest tests/test_investor_flow_snapshots_repository.py tests/test_investor_flow_snapshot_builder.py tests/test_stock_detail_investor_flow_provider.py tests/test_investor_flow_service.py -v
cd frontend/invest && npm test -- StockDetailPage.test.tsx
uv run alembic upgrade head
```

Expected: all tests pass and Alembic upgrade completes.

- [ ] **Step 4: Commit PR2 docs**

Run:

```bash
git add docs/runbooks/invest-screener-snapshots.md
git commit -m "docs: add investor flow market field backfill"
```

---

## Final Verification

- [ ] **PR1 final check**

Run:

```bash
uv run pytest tests/test_investor_flow_service.py tests/test_market_session.py tests/services/market_events/test_session_calendar.py tests/test_investor_flow_snapshot_tasks.py tests/test_snapshot_schedulers_rob438.py -v
```

Expected: PASS.

- [ ] **PR2 final check**

Run:

```bash
uv run alembic upgrade head
uv run pytest tests/test_investor_flow_snapshots_repository.py tests/test_investor_flow_snapshot_builder.py tests/test_stock_detail_investor_flow_provider.py tests/test_invest_stock_detail_schemas.py tests/test_investor_flow_service.py -v
cd frontend/invest && npm test -- StockDetailPage.test.tsx
```

Expected: PASS.

- [ ] **Manual dry-run evidence**

Run:

```bash
uv run python -m scripts.build_investor_flow_snapshots --market kr --symbol 005930 --days 3
```

Expected: command exits successfully with no DB writes. The sample payload should include the recent snapshot dates and no errors. After PR2 implementation, inspect the dry-run payload path or log sample to confirm Naver-derived close, change rate, volume, and foreign-holding fields are non-null for symbols where Naver returned those columns.

---

## Self-Review

- Spec coverage: PR1 covers stale false positive and runbook-only schedule activation. PR2 covers 5 persisted fields, Naver builder mapping, stock-detail row and summary wiring, frontend display, percent-unit verification, and backfill docs.
- Placeholder scan: no deferred implementation markers are used.
- Type consistency: Python uses snake_case DB fields (`change_rate`, `foreign_holding_rate`), Pydantic stock-detail schemas expose camelCase transport fields (`changeRate`, `foreignHoldingRate`), and frontend types already match the transport names.

# ROB-422 PR2c-2 — 저평가탈출-Toss (undervalued_breakout) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the Toss "저평가 탈출" preset (`undervalued_breakout` = PER 0~10 + PBR 0~1 + 신고가 근접) as a valuation-only snapshot screener — no fundamentals dependency — that works live off the already-collected `market_valuation_snapshots` + `invest_screener_snapshots`.

**Architecture:** Mirror `high_yield_value_screener.py` (valuation-snapshot loader precedent). A new `undervalued_breakout_screener.py` filters the latest valuation partition by PER/PBR, OUTER-joins the latest price snapshot for `latest_close`, and keeps only candidates near their 52-week high (`close >= high_52w * 0.95`); pure helpers make the 신고가 logic unit-testable. A new `screener_service.py` dispatch branch routes it (snapshot-only, primary_source=market_valuation_snapshots, NO fundamentals dependency). Catalog adds the preset. Read-only, no migration.

**Tech Stack:** Python 3.13, SQLAlchemy 2.x async, Pydantic v2, pytest. No new dependency.

**Spec:** `docs/superpowers/specs/2026-06-02-rob-422-pr2c2-undervalued-breakout-design.md`

**Resolved (was verify-first):** `naver_finance/valuation.py:90-94` extracts "52주 최고" → `high_52w`, and the builder stores it (`market_valuation_snapshots.builder:74`). So `high_52w` is populated for KR; the preset works live (no operator backfill).

---

## File Structure

**Create:**
- `app/services/invest_view_model/undervalued_breakout_screener.py` — `_near_high_proximity`/`_passes_near_high` pure helpers + `load_undervalued_breakout_from_snapshots` DB loader + `_NEAR_HIGH_RATIO`/`_MAX_PER`/`_MAX_PBR` constants.
- Tests: `tests/test_undervalued_breakout_screener.py`, `tests/test_screener_presets_pr2c2.py`.

**Modify:**
- `app/services/invest_view_model/screener_service.py` — dispatch branch + snapshot-only guard + primary_source + `_METRIC_FIELD` entry.
- `app/services/invest_view_model/screener_presets.py` — catalog entry + `_KR_ONLY_PRESET_IDS`.
- `docs/invest-screener-toss-parity-matrix.md` — #6 row → full.
- `tests/test_screener_service_profitable_company.py` — dispatch test (no fundamentals dependency).

**No `fundamentals_screener` change; no migration.**

---

## Task 1: Loader (pure 신고가 helpers + DB orchestration)

**Files:**
- Create: `app/services/invest_view_model/undervalued_breakout_screener.py`
- Test: `tests/test_undervalued_breakout_screener.py`

- [ ] **Step 1: Write the failing tests (pure helpers + integration loader)**

```python
# tests/test_undervalued_breakout_screener.py
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
import sqlalchemy as sa

from app.models.invest_screener_snapshot import InvestScreenerSnapshot
from app.models.kr_symbol_universe import KRSymbolUniverse
from app.models.market_valuation_snapshot import MarketValuationSnapshot
from app.services.invest_view_model.undervalued_breakout_screener import (
    _near_high_proximity,
    _passes_near_high,
    load_undervalued_breakout_from_snapshots,
)


def test_near_high_proximity_and_pass():
    # within 5% of 52w high → passes
    assert _near_high_proximity(Decimal("95"), Decimal("100")) == Decimal("0.95")
    assert _passes_near_high(Decimal("95"), Decimal("100"), Decimal("0.95")) is True
    # 10% below high → fails
    assert _passes_near_high(Decimal("90"), Decimal("100"), Decimal("0.95")) is False
    # NULL close or high → fail-closed (cannot judge 신고가)
    assert _near_high_proximity(None, Decimal("100")) is None
    assert _passes_near_high(None, Decimal("100"), Decimal("0.95")) is False
    assert _passes_near_high(Decimal("95"), None, Decimal("0.95")) is False
    # close above 52w high (new high) → passes
    assert _passes_near_high(Decimal("105"), Decimal("100"), Decimal("0.95")) is True


@pytest.mark.integration
@pytest.mark.asyncio
async def test_loader_filters_per_pbr_and_near_high(db_session):
    vd = dt.date(2026, 6, 2)
    syms = ["907001", "907002", "907003", "907004"]
    await db_session.execute(
        sa.delete(MarketValuationSnapshot).where(MarketValuationSnapshot.symbol.in_(syms))
    )
    await db_session.execute(
        sa.delete(InvestScreenerSnapshot).where(InvestScreenerSnapshot.symbol.in_(syms))
    )
    await db_session.commit()
    # 907001: per 8, pbr 0.8, close 96/high 100 → near high (0.96) → INCLUDED
    # 907002: per 8, pbr 0.8, close 80/high 100 → 0.80 < 0.95 → excluded (not near high)
    # 907003: per 20 (> 10) → excluded at SQL candidate stage
    # 907004: per 8, pbr 0.8, NO price row → close NULL → fail-closed excluded
    db_session.add_all([
        MarketValuationSnapshot(market="kr", symbol="907001", snapshot_date=vd, source="naver_finance",
            per=Decimal("8"), pbr=Decimal("0.8"), high_52w=Decimal("100"), market_cap=Decimal("5e11")),
        MarketValuationSnapshot(market="kr", symbol="907002", snapshot_date=vd, source="naver_finance",
            per=Decimal("8"), pbr=Decimal("0.8"), high_52w=Decimal("100"), market_cap=Decimal("4e11")),
        MarketValuationSnapshot(market="kr", symbol="907003", snapshot_date=vd, source="naver_finance",
            per=Decimal("20"), pbr=Decimal("0.8"), high_52w=Decimal("100"), market_cap=Decimal("3e11")),
        MarketValuationSnapshot(market="kr", symbol="907004", snapshot_date=vd, source="naver_finance",
            per=Decimal("8"), pbr=Decimal("0.8"), high_52w=Decimal("100"), market_cap=Decimal("2e11")),
    ])
    db_session.add_all([
        InvestScreenerSnapshot(market="kr", symbol="907001", snapshot_date=vd, latest_close=Decimal("96")),
        InvestScreenerSnapshot(market="kr", symbol="907002", snapshot_date=vd, latest_close=Decimal("80")),
        InvestScreenerSnapshot(market="kr", symbol="907003", snapshot_date=vd, latest_close=Decimal("99")),
    ])
    db_session.add_all([
        KRSymbolUniverse(symbol=s, name=f"종목{s}", is_active=True) for s in syms
    ])
    await db_session.commit()

    rows = await load_undervalued_breakout_from_snapshots(
        db_session, market="kr", limit=20, today_market_date=vd
    )
    assert rows is not None
    assert [r["symbol"] for r in rows] == ["907001"]  # only near-high cheap value survives


@pytest.mark.integration
@pytest.mark.asyncio
async def test_loader_returns_none_without_valuation_partition(db_session):
    rows = await load_undervalued_breakout_from_snapshots(
        db_session, market="us", limit=20, today_market_date=dt.date(2026, 6, 2)
    )
    assert rows is None  # non-KR short-circuits
```

> Confirm `InvestScreenerSnapshot`/`MarketValuationSnapshot`/`KRSymbolUniverse` kwargs against the models (add any NOT NULL columns the model requires — e.g. InvestScreenerSnapshot may need `computed_at` default or other fields; `db_session` create_all builds tables).

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_undervalued_breakout_screener.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write the loader**

```python
# app/services/invest_view_model/undervalued_breakout_screener.py
"""Read-only loader for the 저평가 탈출 (Toss undervalued-breakout parity) preset.

Toss rule: market = kr, 0 < per <= 10, 0 < pbr <= 1, and the price is near the
52-week high (close >= high_52w * 0.95). per/pbr/high_52w come from
market_valuation_snapshots (naver_finance); latest_close from invest_screener_snapshots.
Valuation-only — NO fundamentals dependency. NULL per/pbr/close/high_52w are excluded
(fail-closed; never fabricate a qualifier).
"""

from __future__ import annotations

import datetime as dt
import logging
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.invest_screener_snapshot import InvestScreenerSnapshot
from app.models.kr_symbol_universe import KRSymbolUniverse
from app.models.market_valuation_snapshot import MarketValuationSnapshot

logger = logging.getLogger(__name__)

_MAX_PER = Decimal("10")
_MAX_PBR = Decimal("1")
_NEAR_HIGH_RATIO = Decimal("0.95")  # close within 5% of (or above) the 52-week high


def _near_high_proximity(
    latest_close: Decimal | None, high_52w: Decimal | None
) -> Decimal | None:
    """close / high_52w; None when either is missing or high_52w <= 0."""
    if latest_close is None or high_52w is None or high_52w <= 0:
        return None
    return latest_close / high_52w


def _passes_near_high(
    latest_close: Decimal | None, high_52w: Decimal | None, ratio: Decimal
) -> bool:
    prox = _near_high_proximity(latest_close, high_52w)
    return prox is not None and prox >= ratio


async def load_undervalued_breakout_from_snapshots(
    session: AsyncSession | None,
    *,
    market: str,
    limit: int = 20,
    today_market_date: dt.date | None = None,
) -> list[dict[str, Any]] | None:
    """Toss-parity 저평가 탈출 rows, or None when no KR valuation partition exists."""
    if session is None or market != "kr":
        return None

    latest_val_stmt = sa.select(sa.func.max(MarketValuationSnapshot.snapshot_date)).where(
        MarketValuationSnapshot.market == "kr"
    )
    latest_price_stmt = sa.select(sa.func.max(InvestScreenerSnapshot.snapshot_date)).where(
        InvestScreenerSnapshot.market == "kr"
    )
    try:
        val_date = (await session.execute(latest_val_stmt)).scalar_one_or_none()
        price_date = (await session.execute(latest_price_stmt)).scalar_one_or_none()
    except Exception as exc:  # noqa: BLE001
        logger.warning("undervalued_breakout: date lookup failed: %s", exc, exc_info=True)
        return None
    if val_date is None:
        return None

    cand_stmt = (
        sa.select(
            MarketValuationSnapshot.symbol,
            MarketValuationSnapshot.per,
            MarketValuationSnapshot.pbr,
            MarketValuationSnapshot.high_52w,
            MarketValuationSnapshot.market_cap,
            InvestScreenerSnapshot.latest_close,
            InvestScreenerSnapshot.change_rate,
            InvestScreenerSnapshot.daily_volume,
        )
        .outerjoin(
            InvestScreenerSnapshot,
            sa.and_(
                InvestScreenerSnapshot.market == MarketValuationSnapshot.market,
                InvestScreenerSnapshot.symbol == MarketValuationSnapshot.symbol,
                InvestScreenerSnapshot.snapshot_date == price_date,
            ),
        )
        .where(
            MarketValuationSnapshot.market == "kr",
            MarketValuationSnapshot.snapshot_date == val_date,
            MarketValuationSnapshot.per > 0,
            MarketValuationSnapshot.per <= _MAX_PER,
            MarketValuationSnapshot.pbr > 0,
            MarketValuationSnapshot.pbr <= _MAX_PBR,
        )
        .order_by(
            MarketValuationSnapshot.per.asc().nullslast(),
            MarketValuationSnapshot.symbol.asc(),
            MarketValuationSnapshot.source.asc(),
        )
        .limit(max(limit * 6, limit + 60))
    )
    try:
        cand_rows = list((await session.execute(cand_stmt)).mappings().all())
    except Exception as exc:  # noqa: BLE001
        logger.warning("undervalued_breakout: candidate query failed: %s", exc, exc_info=True)
        return None

    symbols = [r["symbol"] for r in cand_rows]
    name_map: dict[str, str] = {}
    if symbols:
        try:
            names = await session.execute(
                sa.select(KRSymbolUniverse.symbol, KRSymbolUniverse.name).where(
                    KRSymbolUniverse.symbol.in_(symbols),
                    KRSymbolUniverse.is_active.is_(True),
                )
            )
            name_map = {row.symbol: row.name for row in names.all()}
        except Exception as exc:  # noqa: BLE001
            logger.warning("undervalued_breakout: name lookup failed: %s", exc, exc_info=True)

    from app.services.invest_view_model.screener_service import _is_kr_toss_common_stock

    if today_market_date is None:
        from datetime import UTC, datetime

        from app.services.invest_screener_snapshots.freshness import today_trading_date

        today_market_date = today_trading_date("kr", now=datetime.now(UTC))
    state = "fresh" if val_date == today_market_date else "stale"

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for r in cand_rows:
        sym = r["symbol"]
        if sym in seen:
            continue
        name = name_map.get(sym)
        if not _is_kr_toss_common_stock(sym, name):
            continue
        # 신고가 근접 (fail-closed on NULL close / high_52w)
        if not _passes_near_high(r["latest_close"], r["high_52w"], _NEAR_HIGH_RATIO):
            continue
        seen.add(sym)
        prox = _near_high_proximity(r["latest_close"], r["high_52w"])
        rows.append(
            {
                "symbol": sym,
                "market": "kr",
                "name": name,
                "latest_close": float(r["latest_close"]) if r["latest_close"] is not None else None,
                "change_rate": float(r["change_rate"]) if r["change_rate"] is not None else None,
                "volume": r["daily_volume"],
                "per": float(r["per"]) if r["per"] is not None else None,
                "pbr": float(r["pbr"]) if r["pbr"] is not None else None,
                "high_52w": float(r["high_52w"]) if r["high_52w"] is not None else None,
                "high_52w_proximity": float(prox) if prox is not None else None,
                "market_cap": float(r["market_cap"]) if r["market_cap"] is not None else None,
                "snapshot_date": val_date,
                "_screener_snapshot_state": state,
            }
        )

    # Rank by proximity to the 52-week high (desc), then cheapest PER (asc).
    rows.sort(
        key=lambda x: (
            x["high_52w_proximity"] is None,
            -(x["high_52w_proximity"] or 0.0),
            x["per"] if x["per"] is not None else float("inf"),
            x["symbol"],
        )
    )
    return rows[:limit]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_undervalued_breakout_screener.py -v`
Expected: PASS (pure helper test + 2 integration tests).

- [ ] **Step 5: Commit**

```bash
git add app/services/invest_view_model/undervalued_breakout_screener.py tests/test_undervalued_breakout_screener.py
git commit -m "feat(ROB-422): undervalued_breakout valuation-only loader (PER0~10+PBR0~1+신고가, PR2c-2)"
```

---

## Task 2: Dispatch + guard + primary_source + metric map

**Files:**
- Modify: `app/services/invest_view_model/screener_service.py`
- Test: `tests/test_screener_service_profitable_company.py`

- [ ] **Step 1: Write the failing test (append)**

```python
@pytest.mark.asyncio
async def test_undervalued_breakout_routes_snapshot_only_no_fundamentals_dependency(monkeypatch):
    async def _fake_loader(session, *, market, limit, today_market_date=None):
        return [{"symbol": "907001", "market": "kr", "name": "종목907001",
                 "per": 8.0, "pbr": 0.8, "high_52w": 100.0, "high_52w_proximity": 0.96,
                 "latest_close": 96.0, "snapshot_date": dt.date(2026, 6, 2),
                 "_screener_snapshot_state": "fresh"}]

    monkeypatch.setattr(
        "app.services.invest_view_model.undervalued_breakout_screener.load_undervalued_breakout_from_snapshots",
        _fake_loader,
    )
    result = await screener_service.build_screener_results(
        preset_id="undervalued_breakout", market="kr",
        session=object(), screening_service=_StubScreening(),
    )
    assert [r.symbol for r in result.results] == ["907001"]
    assert result.freshness.primary.source == "market_valuation_snapshots"
    # valuation-only: NO fundamentals dependency attached
    assert "fundamentals" not in {d.kind for d in result.freshness.dependencies}


@pytest.mark.asyncio
async def test_undervalued_breakout_missing_when_loader_none(monkeypatch):
    async def _none_loader(session, *, market, limit, today_market_date=None):
        return None

    monkeypatch.setattr(
        "app.services.invest_view_model.undervalued_breakout_screener.load_undervalued_breakout_from_snapshots",
        _none_loader,
    )
    result = await screener_service.build_screener_results(
        preset_id="undervalued_breakout", market="kr",
        session=object(), screening_service=_StubScreening(),
    )
    assert result.results == []
    assert result.freshness.overallState == "missing"
```

> Match `build_screener_results` call/accessors to the live signature (same harness as the existing tests in this file).

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_screener_service_profitable_company.py -k undervalued_breakout -v`
Expected: FAIL (no dispatch branch; `list_screening` would be hit or preset unhandled).

- [ ] **Step 3: Add the dispatch branch (after the high_yield_value block, before `elif preset_id in FUNDAMENTALS_PRESET_SPECS`)**

```python
        elif preset_id == "undervalued_breakout":
            from app.services.invest_view_model.undervalued_breakout_screener import (
                load_undervalued_breakout_from_snapshots,
            )

            _snapshot_check_result = await load_undervalued_breakout_from_snapshots(
                session,
                market=requested_market,
                limit=int(filters.get("limit") or _SNAPSHOT_FIRST_LIMIT),
            )
            _snapshot_empty_warning = (
                "최신 밸류에이션/시세 스냅샷에서 저평가 탈출 조건"
                "(PER 0~10·PBR 0~1·신고가 근접)에 맞는 종목이 없습니다."
            )
```

- [ ] **Step 4: Add the snapshot-only guard (after the high_yield_value guard, ~line 1610)**

```python
    if preset_id == "undervalued_breakout" and _snapshot_check_result is None:
        # snapshot-only; the generic provider has no 52-week-high proximity filter.
        _snapshot_check_result = []
        _snapshot_state_override = "missing"
        _snapshot_empty_warning = (
            "밸류에이션/시세 스냅샷이 아직 적재되지 않아 저평가 탈출 후보를 표시할 수 없습니다."
        )
```

- [ ] **Step 5: Add primary_source (in the elif chain, after the high_yield_value branch ~line 1712)**

```python
        elif preset_id == "undervalued_breakout":
            primary_source = "market_valuation_snapshots"
```

- [ ] **Step 6: Add the `_METRIC_FIELD` entry (~line 837, inside the dict)**

```python
    "undervalued_breakout": "high_52w_proximity",
```

- [ ] **Step 7: Run test to verify it passes**

Run: `uv run pytest tests/test_screener_service_profitable_company.py -k undervalued_breakout -v`
Expected: PASS (2 tests). If `overallState` not `missing`, confirm `_snapshot_state_override="missing"` propagation (same path as high_yield_value).

- [ ] **Step 8: Commit**

```bash
git add app/services/invest_view_model/screener_service.py tests/test_screener_service_profitable_company.py
git commit -m "feat(ROB-422): wire undervalued_breakout dispatch (snapshot-only, no fundamentals dep, PR2c-2)"
```

---

## Task 3: Catalog entry + KR-only

**Files:**
- Modify: `app/services/invest_view_model/screener_presets.py`
- Test: `tests/test_screener_presets_pr2c2.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_screener_presets_pr2c2.py
from __future__ import annotations

from app.services.invest_view_model.fundamentals_screener import (
    FUNDAMENTALS_PRESET_SPECS,
)
from app.services.invest_view_model.screener_presets import (
    _KR_ONLY_PRESET_IDS,
    build_screener_presets,
)


def test_undervalued_breakout_preset_full_toss_parity_kr_only():
    presets = {p.id: p for p in build_screener_presets(market="kr")}
    assert "undervalued_breakout" in presets
    p = presets["undervalued_breakout"]
    assert p.name == "저평가 탈출"
    assert p.presetOrigin == "toss_parity"
    assert p.parityStatus == "full"
    assert "undervalued_breakout" in _KR_ONLY_PRESET_IDS
    # valuation-only: NOT a fundamentals-registry preset
    assert "undervalued_breakout" not in FUNDAMENTALS_PRESET_SPECS
    chip_labels = {c.label for c in p.filterChips}
    assert {"PER", "PBR", "신고가"} <= chip_labels
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_screener_presets_pr2c2.py -v`
Expected: FAIL (preset absent).

- [ ] **Step 3: Add the catalog entry to `SCREENER_PRESETS`**

```python
    ScreenerPreset(
        id="undervalued_breakout",
        name="저평가 탈출",
        description="PER·PBR이 낮으면서 주가가 52주 고가에 근접한 저평가 탈출 종목 (지연 스냅샷 기반)",
        badges=["인기"],
        filterChips=[
            ScreenerFilterChip(label="국내", detail=None),
            ScreenerFilterChip(label="PER", detail="0~10"),
            ScreenerFilterChip(label="PBR", detail="0~1"),
            ScreenerFilterChip(label="신고가", detail="52주 고가 5% 이내"),
            ScreenerFilterChip(label="데이터", detail="지연 스냅샷 기반"),
        ],
        metricLabel="신고가 대비",
        market="kr",
        presetOrigin=_TOSS,
        parityStatus=_FULL,
    ),
```

- [ ] **Step 4: Add to `_KR_ONLY_PRESET_IDS`**

Add `"undervalued_breakout"` to the set.

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_screener_presets_pr2c2.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/services/invest_view_model/screener_presets.py tests/test_screener_presets_pr2c2.py
git commit -m "feat(ROB-422): undervalued_breakout catalog entry (toss_parity/full, PR2c-2)"
```

---

## Task 4: Parity matrix doc

**Files:**
- Modify: `docs/invest-screener-toss-parity-matrix.md`

- [ ] **Step 1: Update the #6 row**

Change 저평가 탈출(#6) from `missing (PR2c-2 예정)` to `full / undervalued_breakout` (note: ROB-422 PR2c-2, valuation-only, PER0~10+PBR0~1+신고가 근접 5%, works live off existing valuation+price snapshots). Recount: missing −1, full +1. (The reclassified `oversold_recovery` extra row is unchanged.)

- [ ] **Step 2: Commit**

```bash
git add docs/invest-screener-toss-parity-matrix.md
git commit -m "docs(ROB-422): parity matrix — 저평가 탈출 full (undervalued_breakout, PR2c-2)"
```

---

## Task 5: Verification + lint + regression

**Files:** none.

- [ ] **Step 1: PR2c-2 + screener module tests**

Run: `uv run pytest tests/test_undervalued_breakout_screener.py tests/test_screener_service_profitable_company.py tests/test_screener_presets_pr2c2.py tests/test_screener_presets_pr2c1.py tests/test_fundamentals_screener.py -v`
Expected: all PASS.

- [ ] **Step 2: Regression — high_yield_value + valuation + full-3**

Run: `uv run pytest tests/test_invest_view_model_high_yield_value_screener.py tests/test_invest_coverage_valuation.py -v`
Expected: PASS (high_yield_value uses the same valuation snapshot + price OUTER-join pattern; must be unaffected).

- [ ] **Step 3: Lint + format**

Run: `uv run ruff check app/ tests/ && uv run ruff format --check app/services/invest_view_model/undervalued_breakout_screener.py app/services/invest_view_model/screener_service.py app/services/invest_view_model/screener_presets.py tests/test_undervalued_breakout_screener.py tests/test_screener_presets_pr2c2.py`
Expected: clean (else `ruff format <files>` + recommit).

- [ ] **Step 4: Type check**

Run: `uv run ty check app/services/invest_view_model/undervalued_breakout_screener.py`
Expected: clean (or pre-existing repo-wide noise only).

- [ ] **Step 5: Commit any fixups**

```bash
git add -A
git commit -m "chore(ROB-422): PR2c-2 lint/format fixups" || echo "nothing to commit"
```

---

## Self-Review (completed by plan author)

**Spec coverage (spec §-by-§):**
- §1 undervalued_breakout = PER0~10 + PBR0~1 + 신고가(close≥high_52w×0.95), full/toss_parity, KR-only, snapshot-only, works live (high_52w populated) → Tasks 1 (loader), 3 (catalog). ✓
- §2 loader: candidate per/pbr SQL filter + OUTER-join close + 신고가 fail-closed + common-stock + dedup + proximity-desc sort + None contract + `_NEAR_HIGH_RATIO=0.95` constant → Task 1 (full code + tests). ✓
- §3 dispatch (new branch) + snapshot-only guard + primary_source + NO fundamentals dependency + `_METRIC_FIELD` entry → Task 2. ✓
- §4 catalog + `_KR_ONLY` + parity doc #6 full → Tasks 3, 4. ✓
- §5 tests → every task TDD: 신고가 include/exclude/NULL-fail-closed (pure + integration), per/pbr SQL filter, dedup/common-stock, None contract, dispatch (no fundamentals dep), catalog → Tasks 1, 2, 3. ✓
- §6 safety: read-only, no migration (no migration task), KR-only (added to `_KR_ONLY`), snapshot-only (guard, generic fallback blocked), no fundamentals/derive. ✓

**Placeholder scan:** every code step has complete code. Two verify-against-live notes (Task 1 Step 1 model kwargs; Task 2 Step 1 `build_screener_results` accessors) are bounded checks with fallbacks, not placeholders.

**Type/name consistency:** `_near_high_proximity`/`_passes_near_high`/`load_undervalued_breakout_from_snapshots`/`_NEAR_HIGH_RATIO`/`_MAX_PER`/`_MAX_PBR` defined in Task 1, used consistently. Loader returns `list[dict] | None` (mirrors high_yield_value); dispatch (Task 2) consumes it the same way as the high_yield_value branch (no `_SnapshotLoadResult`). `_METRIC_FIELD` key `undervalued_breakout` → `high_52w_proximity` matches the row field emitted by the loader (Task 1 row dict). Preset id `undervalued_breakout` identical across Tasks 1, 2, 3, 4. `primary_source="market_valuation_snapshots"` matches high_yield_value's value. Valuation-only confirmed: not added to `FUNDAMENTALS_PRESET_SPECS` (asserted in Tasks 2/3 tests). ✓

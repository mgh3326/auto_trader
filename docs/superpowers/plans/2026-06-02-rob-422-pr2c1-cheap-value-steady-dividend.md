# ROB-422 PR2c-1 — cheap_value·steady_dividend full + mismatch 재분류 + §10.1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Raise `cheap_value` and `steady_dividend` to full Toss parity by migrating them onto the PR2a/2b fundamentals screener (registry-dispatched, snapshot-only), reclassify the two mismatch presets (`oversold_recovery`, `growth_expectation`) as `auto_trader_original`, and close the PR2a §10.1 hardening items (symbol dedup defense + loader DB integration test + empty-fundamentals dependency test).

**Architecture:** Add two `FundamentalsPresetSpec` slots (`max_pbr` valuation filter; `min_dividend_paid_streak_years` derive check) and two specs (`CHEAP_VALUE_SPEC`, `STEADY_DIVIDEND_SPEC`) into `FUNDAMENTALS_PRESET_SPECS` — the PR2b registry dispatch then auto-routes them with **no `screener_service.py` change**. Catalog flips their `parityStatus` to full and the two mismatch presets to `auto_trader_original` (catalog-only; their RSI/market-cap generic-provider routing is unchanged). Read-only, no migration.

**Tech Stack:** Python 3.13, SQLAlchemy 2.x async, Pydantic v2, pytest. No new dependency.

**Spec:** `docs/superpowers/specs/2026-06-02-rob-422-pr2c1-cheap-value-steady-dividend-full-design.md`

**Conscious trade-off (spec §1):** cheap_value/steady_dividend currently return LIVE results via the generic tvscreener provider (partial). After migration they are snapshot-only → **empty + dataState=missing until operator fundamentals backfill**. User-approved (project honesty philosophy; consistent with all other fundamentals presets; auto-completes after backfill).

---

## File Structure

**Modify:**
- `app/services/invest_view_model/fundamentals_screener.py` — `FundamentalsPresetSpec` (`max_pbr`, `min_dividend_paid_streak_years`); `_DERIVE_CHECKS` + `_CARRIED_DERIVE_METRICS` (dividend_paid_streak); `CHEAP_VALUE_SPEC`/`STEADY_DIVIDEND_SPEC` + registry; loader `max_pbr` SQL filter + symbol dedup defense.
- `app/services/invest_view_model/screener_presets.py` — cheap_value/steady_dividend → full (+ chips, + `_KR_ONLY_PRESET_IDS`); oversold_recovery/growth_expectation → `_AT_OWN` + parityStatus None.
- `docs/invest-screener-toss-parity-matrix.md` — row updates + count recount.
- Tests: `tests/test_fundamentals_screener.py` (extend), `tests/test_screener_presets_pr2c1.py` (new), `tests/test_screener_service_profitable_company.py` (extend: Path B + cheap_value routing).

**No `screener_service.py` change; no migration.**

---

## Task 1: Spec slots + 2 SPECs + derive check (cheap_value, steady_dividend)

**Files:**
- Modify: `app/services/invest_view_model/fundamentals_screener.py`
- Test: `tests/test_fundamentals_screener.py`

- [ ] **Step 1: Write the failing tests (append)**

```python
def _div_paid_period(year, *, net_income, dps, payout_ratio, filing_date):
    return FundamentalPeriod(
        fiscal_period=f"{year}A", period_type="annual",
        period_end_date=dt.date(year, 12, 31), filing_date=filing_date,
        revenue=Decimal("1000"), net_income=Decimal(net_income),
        discrete_revenue=Decimal("1000"), discrete_net_income=Decimal(net_income),
        dividend_per_share=Decimal(dps), payout_ratio=Decimal(payout_ratio),
    )


def test_pr2c1_registry_has_cheap_value_and_steady_dividend():
    from app.services.invest_view_model.fundamentals_screener import (
        FUNDAMENTALS_PRESET_SPECS,
    )

    assert {"cheap_value", "steady_dividend"} <= set(FUNDAMENTALS_PRESET_SPECS)
    cv = FUNDAMENTALS_PRESET_SPECS["cheap_value"]
    assert cv.max_per == Decimal("15") and cv.max_pbr == Decimal("1.5")
    assert cv.min_earnings_growth_3y_avg == Decimal("0")
    sd = FUNDAMENTALS_PRESET_SPECS["steady_dividend"]
    assert sd.min_dividend_yield == Decimal("0.03") and sd.min_payout_ratio == Decimal("30")
    assert sd.min_dividend_paid_streak_years == 3 and sd.min_earnings_increase_streak_years == 3


def test_steady_dividend_includes_when_all_dividend_gates_met():
    from app.services.invest_view_model.fundamentals_screener import STEADY_DIVIDEND_SPEC

    # net income up 4y (increase streak 3); DPS > 0 each year (paid streak 3); payout latest 40.
    periods = {
        "005930": [
            _div_paid_period(2021 + i, net_income=str(ni), dps=str(d), payout_ratio="40",
                             filing_date=dt.date(2022 + i, 3, 20))
            for i, (ni, d) in enumerate([(100, 50), (120, 55), (150, 60), (200, 65)])
        ]
    }
    valuation_rows = [{"symbol": "005930", "roe": 9.0, "per": 8.0, "pbr": 1.0,
                       "market_cap": 5e11, "dividend_yield": 0.04}]
    rows, excluded = evaluate_fundamentals_candidates(
        valuation_rows=valuation_rows, periods_by_symbol=periods,
        spec=STEADY_DIVIDEND_SPEC, report_date=dt.date(2025, 6, 1), limit=20, name_map={},
    )
    assert [r["symbol"] for r in rows] == ["005930"]
    assert rows[0]["dividend_paid_streak_years"] == 3


def test_steady_dividend_excludes_when_dividend_paid_streak_below_threshold():
    from app.services.invest_view_model.fundamentals_screener import STEADY_DIVIDEND_SPEC

    # 2023 DPS = 0 → paid streak ending 2024 is only 1 (< 3) → excluded.
    periods = {
        "005930": [
            _div_paid_period(2021 + i, net_income=str(ni), dps=str(d), payout_ratio="40",
                             filing_date=dt.date(2022 + i, 3, 20))
            for i, (ni, d) in enumerate([(100, 50), (120, 55), (150, 0), (200, 65)])
        ]
    }
    valuation_rows = [{"symbol": "005930", "roe": 9.0, "per": 8.0, "pbr": 1.0,
                       "market_cap": 5e11, "dividend_yield": 0.04}]
    rows, excluded = evaluate_fundamentals_candidates(
        valuation_rows=valuation_rows, periods_by_symbol=periods,
        spec=STEADY_DIVIDEND_SPEC, report_date=dt.date(2025, 6, 1), limit=20, name_map={},
    )
    assert rows == []
    assert any("dividend_paid_streak_years" in e["reason"] for e in excluded)


def test_cheap_value_includes_when_earnings_growth_non_negative():
    from app.services.invest_view_model.fundamentals_screener import CHEAP_VALUE_SPEC

    # revenue flat-ish, net income non-decreasing → earnings_growth_3y_avg >= 0.
    periods = _four_growth_years("005930", [1000, 1010, 1020, 1030], [100, 100, 110, 120])
    valuation_rows = [{"symbol": "005930", "roe": 5.0, "per": 10.0, "pbr": 0.8,
                       "market_cap": 3e11, "dividend_yield": 0.01}]
    rows, _ = evaluate_fundamentals_candidates(
        valuation_rows=valuation_rows, periods_by_symbol=periods,
        spec=CHEAP_VALUE_SPEC, report_date=dt.date(2025, 6, 1), limit=20, name_map={},
    )
    assert [r["symbol"] for r in rows] == ["005930"]


def test_cheap_value_excludes_when_earnings_growth_negative():
    from app.services.invest_view_model.fundamentals_screener import CHEAP_VALUE_SPEC

    # net income declining → earnings_growth_3y_avg < 0 → excluded.
    periods = _four_growth_years("005930", [1000, 1010, 1020, 1030], [200, 180, 150, 120])
    valuation_rows = [{"symbol": "005930", "roe": 5.0, "per": 10.0, "pbr": 0.8,
                       "market_cap": 3e11, "dividend_yield": 0.01}]
    rows, excluded = evaluate_fundamentals_candidates(
        valuation_rows=valuation_rows, periods_by_symbol=periods,
        spec=CHEAP_VALUE_SPEC, report_date=dt.date(2025, 6, 1), limit=20, name_map={},
    )
    assert rows == []
    assert any("earnings_growth_3y_avg" in e["reason"] for e in excluded)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_fundamentals_screener.py -k "pr2c1 or steady_dividend_includes or steady_dividend_excludes or cheap_value_includes or cheap_value_excludes" -v`
Expected: FAIL (`max_pbr`/`min_dividend_paid_streak_years`/`CHEAP_VALUE_SPEC`/`STEADY_DIVIDEND_SPEC` undefined; dividend_paid_streak not checked).

- [ ] **Step 3: Add the two spec fields**

In `FundamentalsPresetSpec`, add after `max_per` (valuation group) and in the derive group:

```python
    max_pbr: Decimal | None = None              # 0 < pbr <= max_pbr
```
```python
    min_dividend_paid_streak_years: int | None = None       # years (3)
```

- [ ] **Step 4: Add the derive check + carried metric**

In `_DERIVE_CHECKS`, append:
```python
    ("min_dividend_paid_streak_years", "dividend_paid_streak_years"),
```
In `_CARRIED_DERIVE_METRICS`, append:
```python
    "dividend_paid_streak_years",
```

- [ ] **Step 5: Add the two SPECs + register**

Next to the other SPECs (before `FUNDAMENTALS_PRESET_SPECS`):

```python
CHEAP_VALUE_SPEC = FundamentalsPresetSpec(
    preset_id="cheap_value",
    max_per=Decimal("15"),
    max_pbr=Decimal("1.5"),
    min_earnings_growth_3y_avg=Decimal("0"),  # 3y-avg net income growth >= 0%
    sort_by="earnings_growth_3y_avg",
)

STEADY_DIVIDEND_SPEC = FundamentalsPresetSpec(
    preset_id="steady_dividend",
    min_dividend_yield=Decimal("0.03"),  # 3% (ratio; KR naver stores /100)
    min_payout_ratio=Decimal("30"),
    min_dividend_paid_streak_years=3,
    min_earnings_increase_streak_years=3,
    sort_by="dividend_yield",
)
```

In the `FUNDAMENTALS_PRESET_SPECS` dict comprehension tuple, add `CHEAP_VALUE_SPEC, STEADY_DIVIDEND_SPEC`:
```python
FUNDAMENTALS_PRESET_SPECS: dict[str, FundamentalsPresetSpec] = {
    s.preset_id: s
    for s in (
        PROFITABLE_COMPANY_SPEC,
        UNDERVALUED_GROWTH_SPEC,
        STABLE_GROWTH_SPEC,
        FUTURE_DIVIDEND_KING_SPEC,
        CHEAP_VALUE_SPEC,
        STEADY_DIVIDEND_SPEC,
    )
}
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_fundamentals_screener.py -v`
Expected: PASS — new tests + all pre-existing (PR2a/PR2b). The dividend_paid_streak check fires only when `min_dividend_paid_streak_years` is set (other presets leave it None → skipped, no regression).

- [ ] **Step 7: Commit**

```bash
git add app/services/invest_view_model/fundamentals_screener.py tests/test_fundamentals_screener.py
git commit -m "feat(ROB-422): max_pbr + dividend_paid_streak slots + cheap_value/steady_dividend SPECs (PR2c-1)"
```

---

## Task 2: Loader `max_pbr` SQL filter + symbol dedup defense

**Files:**
- Modify: `app/services/invest_view_model/fundamentals_screener.py`
- Test: `tests/test_fundamentals_screener.py` (integration, `db_session`)

- [ ] **Step 1: Write the failing integration tests (append)**

```python
@pytest.mark.integration
@pytest.mark.asyncio
async def test_loader_valuation_filter_max_pbr_excludes_high_pbr(db_session):
    import sqlalchemy as sa

    from app.models.market_valuation_snapshot import MarketValuationSnapshot
    from app.services.invest_view_model.fundamentals_screener import (
        CHEAP_VALUE_SPEC,
        load_fundamentals_preset_from_snapshots,
    )

    vd = dt.date(2026, 6, 2)
    await db_session.execute(
        sa.delete(MarketValuationSnapshot).where(
            MarketValuationSnapshot.symbol.in_(["906421", "906422"])
        )
    )
    await db_session.commit()
    db_session.add_all([
        MarketValuationSnapshot(market="kr", symbol="906421", snapshot_date=vd,
            source="naver_finance", per=Decimal("12"), pbr=Decimal("1.0"),
            roe=Decimal("8"), dividend_yield=Decimal("0.01"), market_cap=Decimal("500000000000")),
        MarketValuationSnapshot(market="kr", symbol="906422", snapshot_date=vd,
            source="naver_finance", per=Decimal("12"), pbr=Decimal("3.0"),  # PBR 3.0 > 1.5 → excluded
            roe=Decimal("8"), dividend_yield=Decimal("0.01"), market_cap=Decimal("400000000000")),
    ])
    await db_session.commit()

    result = await load_fundamentals_preset_from_snapshots(
        db_session, market="kr", spec=CHEAP_VALUE_SPEC, limit=20,
        now=lambda: dt.datetime(2026, 6, 2, tzinfo=dt.UTC),
    )
    assert result is not None
    excluded_symbols = {e["symbol"] for e in result.excluded}
    assert "906421" in excluded_symbols       # PBR 1.0 <= 1.5 → candidate (then no fundamentals)
    assert "906422" not in excluded_symbols   # PBR 3.0 filtered at SQL stage


@pytest.mark.integration
@pytest.mark.asyncio
async def test_loader_dedups_symbol_across_multiple_sources(db_session):
    import sqlalchemy as sa

    from app.models.market_valuation_snapshot import MarketValuationSnapshot
    from app.services.invest_view_model.fundamentals_screener import (
        CHEAP_VALUE_SPEC,
        load_fundamentals_preset_from_snapshots,
    )

    vd = dt.date(2026, 6, 2)
    await db_session.execute(
        sa.delete(MarketValuationSnapshot).where(MarketValuationSnapshot.symbol == "906431")
    )
    await db_session.commit()
    # Same KR symbol under two sources at the same date (defensive — KR is single-source today).
    db_session.add_all([
        MarketValuationSnapshot(market="kr", symbol="906431", snapshot_date=vd,
            source="naver_finance", per=Decimal("10"), pbr=Decimal("1.0"),
            roe=Decimal("8"), dividend_yield=Decimal("0.01"), market_cap=Decimal("500000000000")),
        MarketValuationSnapshot(market="kr", symbol="906431", snapshot_date=vd,
            source="yahoo", per=Decimal("10"), pbr=Decimal("1.0"),
            roe=Decimal("8"), dividend_yield=Decimal("0.01"), market_cap=Decimal("500000000000")),
    ])
    await db_session.commit()

    result = await load_fundamentals_preset_from_snapshots(
        db_session, market="kr", spec=CHEAP_VALUE_SPEC, limit=20,
        now=lambda: dt.datetime(2026, 6, 2, tzinfo=dt.UTC),
    )
    assert result is not None
    # 906431 reaches derive at most ONCE (deduped), not twice.
    assert [e["symbol"] for e in result.excluded].count("906431") == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_fundamentals_screener.py -k "max_pbr or dedups_symbol" -v`
Expected: FAIL (max_pbr filter absent; duplicate symbol appears twice in excluded).

- [ ] **Step 3: Add the `max_pbr` SQL filter**

In `load_fundamentals_preset_from_snapshots`, after the `max_per` block (`if spec.max_per is not None:`):

```python
    if spec.max_pbr is not None:
        cand_stmt = cand_stmt.where(
            MarketValuationSnapshot.pbr > 0,
            MarketValuationSnapshot.pbr <= spec.max_pbr,
        )
```

- [ ] **Step 4: Add symbol dedup defense**

Replace the `valuation_rows = [ ... ]` comprehension (the common-stock filter block) with a dedup loop:

```python
    # common-stock filter (drop ETF/preferred) + symbol dedup (defensive: KR is single-source
    # today, but a future second valuation source must not produce duplicate candidates).
    valuation_rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for m in cand_mappings:
        sym = m["symbol"]
        if sym in seen:
            continue
        if not _is_kr_toss_common_stock(sym, name_map.get(sym)):
            continue
        seen.add(sym)
        valuation_rows.append({**dict(m), "_screener_snapshot_state": val_state})
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_fundamentals_screener.py -k "max_pbr or dedups_symbol or max_per or dividend_yield" -v`
Expected: PASS (new max_pbr + dedup, and pre-existing max_per/dividend_yield integration tests unaffected).

- [ ] **Step 6: Commit**

```bash
git add app/services/invest_view_model/fundamentals_screener.py tests/test_fundamentals_screener.py
git commit -m "feat(ROB-422): loader max_pbr filter + symbol dedup defense (PR2c-1, §10.1)"
```

---

## Task 3: Catalog — cheap_value/steady_dividend full + mismatch 재분류

**Files:**
- Modify: `app/services/invest_view_model/screener_presets.py`
- Test: `tests/test_screener_presets_pr2c1.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_screener_presets_pr2c1.py
from __future__ import annotations

from app.services.invest_view_model.fundamentals_screener import (
    FUNDAMENTALS_PRESET_SPECS,
)
from app.services.invest_view_model.screener_presets import (
    _KR_ONLY_PRESET_IDS,
    build_screener_presets,
)


def test_cheap_value_and_steady_dividend_now_full_and_kr_only():
    presets = {p.id: p for p in build_screener_presets(market="kr")}
    for pid in ("cheap_value", "steady_dividend"):
        assert presets[pid].parityStatus == "full"
        assert presets[pid].parityNote is None
        assert pid in _KR_ONLY_PRESET_IDS
        assert pid in FUNDAMENTALS_PRESET_SPECS  # registry-routed (snapshot-only)


def test_mismatch_presets_reclassified_as_auto_trader_original():
    presets = {p.id: p for p in build_screener_presets(market="kr")}
    for pid in ("oversold_recovery", "growth_expectation"):
        assert presets[pid].presetOrigin == "auto_trader_original"
        assert presets[pid].parityStatus is None
        assert pid not in FUNDAMENTALS_PRESET_SPECS  # still generic-provider routed
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_screener_presets_pr2c1.py -v`
Expected: FAIL (cheap_value/steady_dividend still partial; mismatch presets still `_TOSS`/`_MISMATCH`).

- [ ] **Step 3: Update cheap_value entry**

Replace the `cheap_value` `ScreenerPreset(...)` block (currently parityStatus=_PARTIAL + parityNote) with:

```python
    ScreenerPreset(
        id="cheap_value",
        name="아직 저렴한 가치주",
        description="PER·PBR이 낮으면서 순이익이 역성장하지 않는 저평가 종목 (지연 스냅샷 기반)",
        badges=[],
        filterChips=[
            ScreenerFilterChip(label="국내", detail=None),
            ScreenerFilterChip(label="PER", detail="0~15"),
            ScreenerFilterChip(label="PBR", detail="0~1.5"),
            ScreenerFilterChip(label="순이익증가율", detail="3년평균 0% 이상"),
            ScreenerFilterChip(label="데이터", detail="지연 스냅샷 기반"),
        ],
        metricLabel="PER",
        market="kr",
        presetOrigin=_TOSS,
        parityStatus=_FULL,
    ),
```

- [ ] **Step 4: Update steady_dividend entry**

Replace the `steady_dividend` `ScreenerPreset(...)` block with:

```python
    ScreenerPreset(
        id="steady_dividend",
        name="꾸준한 배당주",
        description="배당수익률·배당성향이 높고 배당 연속지급·순이익 연속증가를 갖춘 종목 (지연 스냅샷 기반)",
        badges=["인기"],
        filterChips=[
            ScreenerFilterChip(label="국내", detail=None),
            ScreenerFilterChip(label="배당수익률", detail="3% 이상"),
            ScreenerFilterChip(label="배당성향", detail="30% 이상"),
            ScreenerFilterChip(label="배당", detail="연속지급 3년 이상"),
            ScreenerFilterChip(label="순이익", detail="연속증가 3년 이상"),
            ScreenerFilterChip(label="데이터", detail="지연 스냅샷 기반"),
        ],
        metricLabel="배당수익률",
        market="kr",
        presetOrigin=_TOSS,
        parityStatus=_FULL,
    ),
```

- [ ] **Step 5: Reclassify oversold_recovery + growth_expectation**

Replace the `oversold_recovery` block with (presetOrigin → `_AT_OWN`, parityStatus removed, note reframed):

```python
    ScreenerPreset(
        id="oversold_recovery",
        name="과매도 반등 (RSI)",
        description="RSI가 30 이하 과매도 구간에 들어온 종목 (auto_trader 자체 스크린)",
        badges=["인기"],
        filterChips=[
            ScreenerFilterChip(label="국내", detail=None),
            ScreenerFilterChip(label="RSI", detail="30 이하"),
        ],
        metricLabel="RSI",
        market="kr",
        presetOrigin=_AT_OWN,
        parityNote=(
            "auto_trader 자체 스크린(RSI ≤ 30 과매도 반등). "
            "Toss '저평가 탈출'(PER 0~10 + PBR 0~1 + 신고가)과는 별개이며, "
            "Toss 의미 프리셋은 별도(PR2c-2)로 추가 예정."
        ),
    ),
```

Replace the `growth_expectation` block with:

```python
    ScreenerPreset(
        id="growth_expectation",
        name="대형 모멘텀 (시총·등락률)",
        description="시가총액이 충분하고 등락률 상위인 대형 모멘텀 종목 (auto_trader 자체 스크린)",
        badges=[],
        filterChips=[
            ScreenerFilterChip(label="국내", detail=None),
            ScreenerFilterChip(label="시가총액", detail="1조 이상"),
            ScreenerFilterChip(label="주가등락률", detail="상위"),
        ],
        metricLabel="주가등락률",
        market="kr",
        presetOrigin=_AT_OWN,
        parityNote=(
            "auto_trader 자체 스크린(시가총액 ≥ 1조 + 등락률 상위). "
            "Toss '성장 기대주'(순이익 3년 성장 + 직전분기 순이익 성장)와는 별개이며, "
            "Toss 의미 프리셋은 분기 재무 수집 후 별도 이슈로 추가 예정."
        ),
    ),
```

- [ ] **Step 6: Add cheap_value/steady_dividend to `_KR_ONLY_PRESET_IDS`**

```python
_KR_ONLY_PRESET_IDS = {
    "investor_flow_momentum",
    "double_buy",
    "high_yield_value",
    "profitable_company",
    "undervalued_growth",
    "stable_growth",
    "future_dividend_king",
    "cheap_value",
    "steady_dividend",
}
```

> They are now KR-fundamentals snapshot presets; the loader returns `None` for non-KR markets, so they must not surface for US (where they would render a perpetual `missing`).

- [ ] **Step 7: Run test to verify it passes**

Run: `uv run pytest tests/test_screener_presets_pr2c1.py -v`
Expected: PASS (2 tests).

- [ ] **Step 8: Commit**

```bash
git add app/services/invest_view_model/screener_presets.py tests/test_screener_presets_pr2c1.py
git commit -m "feat(ROB-422): cheap_value/steady_dividend full + mismatch presets → auto_trader_original (PR2c-1)"
```

---

## Task 4: §10.1 — loader DB integration (full include/exclude) + Path B service test

**Files:**
- Test: `tests/test_fundamentals_screener.py` (extend), `tests/test_screener_service_profitable_company.py` (extend)

- [ ] **Step 1: Write the loader end-to-end integration test**

```python
# append to tests/test_fundamentals_screener.py
@pytest.mark.integration
@pytest.mark.asyncio
async def test_loader_end_to_end_includes_and_excludes_with_fundamentals(db_session):
    import sqlalchemy as sa

    from app.models.financial_fundamentals_snapshot import FinancialFundamentalsSnapshot
    from app.models.kr_symbol_universe import KRSymbolUniverse
    from app.models.market_valuation_snapshot import MarketValuationSnapshot
    from app.services.invest_view_model.fundamentals_screener import (
        CHEAP_VALUE_SPEC,
        load_fundamentals_preset_from_snapshots,
    )

    vd = dt.date(2026, 6, 2)
    syms = ["906441", "906442"]
    await db_session.execute(
        sa.delete(FinancialFundamentalsSnapshot).where(
            FinancialFundamentalsSnapshot.symbol.in_(syms)
        )
    )
    await db_session.execute(
        sa.delete(MarketValuationSnapshot).where(MarketValuationSnapshot.symbol.in_(syms))
    )
    await db_session.commit()
    # Both pass valuation (PER 12<=15, PBR 1.0<=1.5).
    db_session.add_all([
        MarketValuationSnapshot(market="kr", symbol=s, snapshot_date=vd, source="naver_finance",
            per=Decimal("12"), pbr=Decimal("1.0"), roe=Decimal("8"),
            dividend_yield=Decimal("0.01"), market_cap=Decimal("500000000000"))
        for s in syms
    ])
    # 906441 earnings growing (eg >= 0 → included); 906442 declining (eg < 0 → excluded).
    for s, nis in [("906441", [100, 110, 120, 130]), ("906442", [200, 180, 150, 120])]:
        for i, ni in enumerate(nis):
            db_session.add(FinancialFundamentalsSnapshot(
                market="kr", symbol=s, fiscal_period=f"{2021 + i}A", period_type="annual",
                period_end_date=dt.date(2021 + i, 12, 31), filing_date=dt.date(2022 + i, 3, 20),
                effective_at=dt.date(2022 + i, 3, 20), source="dart",
                source_collected_at=dt.datetime(2026, 6, 1, tzinfo=dt.UTC),
                revenue=Decimal("1000"), net_income=Decimal(ni), data_state="fresh"))
    db_session.add_all([
        KRSymbolUniverse(symbol=s, name=f"종목{s}", is_active=True) for s in syms
    ])
    await db_session.commit()

    result = await load_fundamentals_preset_from_snapshots(
        db_session, market="kr", spec=CHEAP_VALUE_SPEC, limit=20,
        now=lambda: dt.datetime(2026, 6, 2, tzinfo=dt.UTC),
    )
    assert result is not None
    assert [r["symbol"] for r in result.rows] == ["906441"]  # growing earnings included
    assert result.fundamentals_state == "fresh"  # fundamentals rows exist
    assert "906442" in {e["symbol"] for e in result.excluded}  # declining → excluded
```

> Confirm `KRSymbolUniverse(...)` and `FinancialFundamentalsSnapshot(...)` kwargs match the models (add any NOT NULL columns). `db_session` builds tables via `Base.metadata.create_all` (conftest.py:437); both models are registered in `app/models/__init__.py`.

- [ ] **Step 2: Write the Path B (empty fundamentals → missing dependency) service test**

```python
# append to tests/test_screener_service_profitable_company.py
@pytest.mark.asyncio
async def test_cheap_value_empty_fundamentals_surfaces_missing_dependency(monkeypatch):
    from app.services.invest_view_model.fundamentals_screener import FundamentalsScreenResult

    async def _empty_fundamentals_loader(session, *, market, spec, limit, now):
        # valuation partition exists, but no fundamentals rows backfilled (Path B).
        return FundamentalsScreenResult(
            rows=[], valuation_partition_date=dt.date(2026, 6, 2),
            fundamentals_partition_date=None, fundamentals_collected_at=None,
            fundamentals_state="missing",
        )

    monkeypatch.setattr(
        "app.services.invest_view_model.fundamentals_screener.load_fundamentals_preset_from_snapshots",
        _empty_fundamentals_loader,
    )
    result = await screener_service.build_screener_results(
        preset_id="cheap_value", market="kr",
        session=object(), screening_service=_StubScreening(),
    )
    assert result.results == []
    fundamentals_deps = [d for d in result.freshness.dependencies if d.kind == "fundamentals"]
    assert fundamentals_deps and fundamentals_deps[0].dataState == "missing"
```

> Match `build_screener_results` call/accessors + the dependency `dataState` field name to the live shapes (same harness as the existing tests in this file).

- [ ] **Step 3: Run tests to verify they fail then pass**

Run: `uv run pytest tests/test_fundamentals_screener.py::test_loader_end_to_end_includes_and_excludes_with_fundamentals tests/test_screener_service_profitable_company.py::test_cheap_value_empty_fundamentals_surfaces_missing_dependency -v`
Expected: PASS (the production code from Tasks 1-3 already supports both; these close the §10.1 coverage gaps). If the Path B test fails on the dependency assertion, confirm cheap_value is in `FUNDAMENTALS_PRESET_SPECS` (Task 1) so the dependency append fires.

- [ ] **Step 4: Commit**

```bash
git add tests/test_fundamentals_screener.py tests/test_screener_service_profitable_company.py
git commit -m "test(ROB-422): loader DB integration + Path B missing-dependency (PR2c-1, §10.1)"
```

---

## Task 5: Parity matrix doc

**Files:**
- Modify: `docs/invest-screener-toss-parity-matrix.md`

- [ ] **Step 1: Update rows + recount**

- 아직 저렴한 가치주(#3) → `full / cheap_value`; 꾸준한 배당주(#4) → `full / steady_dividend` (note: ROB-422 PR2c-1, snapshot-only, operator-backfill-gated).
- 저평가 탈출(#6) → `missing (저평가탈출-Toss = PR2c-2 예정)`; existing `oversold_recovery` (RSI) → `extra (auto_trader_original)`.
- 성장 기대주(#8) → `missing (qoq = 분기 수집 필요, 별도 이슈)`; existing `growth_expectation` (시총·등락률) → `extra (auto_trader_original)`.
- Recount the full/partial/mismatch/missing/extra summary: **mismatch → 0**, full +2, extra +2 (the two reclassified), missing reflects #6/#8 Toss presets pending.

- [ ] **Step 2: Commit**

```bash
git add docs/invest-screener-toss-parity-matrix.md
git commit -m "docs(ROB-422): parity matrix — cheap/steady full, mismatch→extra, #6/#8 missing (PR2c-1)"
```

---

## Task 6: Verification + lint + regression

**Files:** none.

- [ ] **Step 1: PR2c-1 + PR2a/2b module tests**

Run: `uv run pytest tests/test_fundamentals_screener.py tests/test_screener_service_profitable_company.py tests/test_screener_presets_pr2c1.py tests/test_screener_presets_pr2b.py tests/test_screener_presets_profitable_company.py tests/test_financial_fundamentals_derive.py -v`
Expected: all PASS.

- [ ] **Step 2: Regression — full-3 + valuation + high_yield_value**

Run: `uv run pytest tests/test_invest_view_model_high_yield_value_screener.py tests/test_invest_coverage_valuation.py -v`
Expected: PASS.

- [ ] **Step 3: Lint + format**

Run: `uv run ruff check app/ tests/ && uv run ruff format --check app/services/invest_view_model/fundamentals_screener.py app/services/invest_view_model/screener_presets.py tests/test_fundamentals_screener.py tests/test_screener_presets_pr2c1.py tests/test_screener_service_profitable_company.py`
Expected: clean (else `ruff format <files>` + recommit).

- [ ] **Step 4: Type check**

Run: `uv run ty check app/services/invest_view_model/fundamentals_screener.py`
Expected: clean (or pre-existing repo-wide noise only).

- [ ] **Step 5: Commit any fixups**

```bash
git add -A
git commit -m "chore(ROB-422): PR2c-1 lint/format fixups" || echo "nothing to commit"
```

---

## Self-Review (completed by plan author)

**Spec coverage (spec §-by-§):**
- §1 cheap_value full (max_per/max_pbr + earnings_growth_3y_avg≥0), steady_dividend full (dividend_yield 0.03 + payout 30 + dividend_paid_streak 3 + earnings_increase_streak 3) → Tasks 1 (specs), 3 (catalog). ✓
- §2 new slots max_pbr (valuation) + min_dividend_paid_streak_years (derive check + carry) → Task 1 (Steps 3-4), Task 2 (max_pbr SQL). ✓
- §3 registry add + parity full + KR-only + generic-provider exit (auto via registry) + sort desc reuse → Tasks 1 (registry), 3 (catalog + KR-only). ✓
- §4 mismatch 2 → auto_trader_original, parityStatus None, routing unchanged (not in registry) → Task 3 (Steps 5) + test (not-in-registry assertion). ✓
- §5 §10.1: dedup defense → Task 2 (Step 4 + test); loader DB integration → Task 4 (Step 1); Path B → Task 4 (Step 2). (fundamentals_state 'stale' = spec §5.4 optional, deferred — noted.) ✓
- §6 parity matrix doc → Task 5. ✓
- §7 tests → every task TDD: dividend_paid_streak below/include, cheap_value earnings≥0 include/exclude, max_pbr + dividend_yield SQL, registry+catalog, mismatch reclassify, dedup, loader e2e, Path B. ✓
- §8 safety: read-only, no migration (no migration task), KR-only (added to _KR_ONLY), snapshot-only (registry), no screener_service.py change. ✓

**Placeholder scan:** every code step has complete code. Two verify-against-live notes (Task 4 model kwargs; Task 4 build_screener_results accessors) are bounded checks with fallbacks, not placeholders. spec §5.4 'stale' classification is explicitly optional/deferred (not a silent gap).

**Type/name consistency:** `max_pbr`/`min_dividend_paid_streak_years` added to spec (Task 1 S3), consumed in `_DERIVE_CHECKS`/`_CARRIED_DERIVE_METRICS` (Task 1 S4) and loader SQL (Task 2 S3). `CHEAP_VALUE_SPEC`/`STEADY_DIVIDEND_SPEC` defined Task 1 S5, registered same step, asserted Tasks 1/3. `_TOSS`/`_AT_OWN`/`_FULL` are existing module constants (screener_presets.py:51 etc.); reclassify uses `_AT_OWN` + `parityStatus` omitted (None default). `dividend_paid_streak_years` derive attr exists on `FundamentalsDerivation` (PR1). cheap_value sort_by=`earnings_growth_3y_avg` and steady_dividend sort_by=`dividend_yield` are both carried on the row (earnings_growth via _CARRIED_DERIVE_METRICS; dividend_yield via the valuation copy). ✓

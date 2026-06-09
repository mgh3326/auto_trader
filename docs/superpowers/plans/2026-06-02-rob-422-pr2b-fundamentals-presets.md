# ROB-422 PR2b — 저평가성장주·안정성장주·미래의배당왕 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Light up the remaining three Toss "missing" presets — 저평가성장주 (`undervalued_growth`), 안정성장주 (`stable_growth`), 미래의배당왕 (`future_dividend_king`) — by generalizing PR2a's fundamentals screener (valuation SQL filters + a declarative derive-check list + a preset-spec registry) and adding three threshold configs.

**Architecture:** Extend `FundamentalsPresetSpec` with optional valuation/derive threshold fields; generalize `evaluate_fundamentals_candidates` to a declarative per-metric check loop (each `unavailable`/below-threshold → exclude + reason, never a silent pass) and to carry every checked metric on the output row; generalize the loader's valuation candidate query (max_per, min_dividend_yield) ordered by market_cap; replace the single `profitable_company` dispatch branch with a `FUNDAMENTALS_PRESET_SPECS` registry-driven branch (preserving profitable_company). All read-only; no migration (PR1 table reused).

**Tech Stack:** Python 3.13, SQLAlchemy 2.x async, Pydantic v2, pytest. No new dependency.

**Spec:** `docs/superpowers/specs/2026-06-02-rob-422-pr2b-fundamentals-presets-design.md`

**Resolved (was verify-first):** `market_valuation_snapshots.dividend_yield` is a **ratio** for KR — `naver_finance/valuation.py` stores `dividend_yield = dvr_val / 100` (lines 66, 151). So `min_dividend_yield = Decimal("0.01")` for 배당수익률 ≥1%. (`steady_dividend`'s `2.0` is the unrelated generic-provider path, not the valuation snapshot.)

---

## File Structure

**Modify:**
- `app/services/invest_view_model/fundamentals_screener.py` — extend `FundamentalsPresetSpec`; 3 new SPECs + `FUNDAMENTALS_PRESET_SPECS` registry; generalize `evaluate_fundamentals_candidates` (declarative checks + carry metrics + generalized sort); generalize loader valuation SQL (max_per/min_dividend_yield + market_cap ordering + dividend_yield SELECT + cap warning).
- `app/services/invest_view_model/screener_service.py` — registry-driven dispatch / snapshot-only guard / primary_source (lines 1552, 1614, 1711).
- `app/services/invest_view_model/screener_presets.py` — 3 `ScreenerPreset` entries + `_KR_ONLY_PRESET_IDS`.
- `docs/invest-screener-toss-parity-matrix.md` — mark the 3 rows implemented.
- Tests: `tests/test_fundamentals_screener.py` (extend), `tests/test_screener_presets_profitable_company.py` (extend or new `tests/test_screener_presets_pr2b.py`), `tests/test_screener_service_profitable_company.py` (extend for registry).

**No new files; no migration.**

---

## Task 1: Extend FundamentalsPresetSpec + 3 SPECs + registry

**Files:**
- Modify: `app/services/invest_view_model/fundamentals_screener.py`
- Test: `tests/test_fundamentals_screener.py`

- [ ] **Step 1: Write the failing test (append)**

```python
from app.services.invest_view_model.fundamentals_screener import (
    FUNDAMENTALS_PRESET_SPECS,
)


def test_registry_has_four_specs_with_expected_thresholds():
    assert set(FUNDAMENTALS_PRESET_SPECS) == {
        "profitable_company", "undervalued_growth", "stable_growth", "future_dividend_king",
    }
    ug = FUNDAMENTALS_PRESET_SPECS["undervalued_growth"]
    assert ug.max_per == Decimal("20") and ug.min_revenue_growth_3y_avg == Decimal("0.10")
    assert ug.min_earnings_growth_3y_avg == Decimal("0.20")
    sg = FUNDAMENTALS_PRESET_SPECS["stable_growth"]
    assert sg.min_roe == Decimal("15") and sg.min_earnings_growth_3y_avg == Decimal("0.10")
    assert sg.min_earnings_increase_streak_years == 3
    dk = FUNDAMENTALS_PRESET_SPECS["future_dividend_king"]
    assert dk.min_dividend_yield == Decimal("0.01") and dk.min_payout_ratio == Decimal("30")
    assert dk.min_dividend_growth_streak_years == 3 and dk.min_earnings_increase_streak_years == 3
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_fundamentals_screener.py::test_registry_has_four_specs_with_expected_thresholds -v`
Expected: FAIL (`FUNDAMENTALS_PRESET_SPECS` undefined).

- [ ] **Step 3: Replace the `FundamentalsPresetSpec` dataclass + add SPECs/registry**

Replace the existing `FundamentalsPresetSpec` and `PROFITABLE_COMPANY_SPEC` block with:

```python
@dataclass(frozen=True)
class FundamentalsPresetSpec:
    preset_id: str
    # valuation filters (applied in the SQL candidate query):
    min_roe: Decimal | None = None              # percent (e.g. 15)
    max_per: Decimal | None = None              # 0 < per <= max_per
    min_dividend_yield: Decimal | None = None   # ratio (e.g. 0.01 == 1%), KR naver stores /100
    # derive thresholds (applied in evaluate_fundamentals_candidates):
    min_gross_margin_ttm: Decimal | None = None         # ratio (0.20)
    min_revenue_growth_3y_avg: Decimal | None = None    # ratio (0.10)
    min_earnings_growth_3y_avg: Decimal | None = None   # ratio (0.10 / 0.20)
    min_earnings_increase_streak_years: int | None = None   # years (3)
    min_dividend_growth_streak_years: int | None = None     # years (3)
    min_payout_ratio: Decimal | None = None             # percent (30) — DART 현금배당성향%
    sort_by: str = "roe"  # any metric key carried on the output row


PROFITABLE_COMPANY_SPEC = FundamentalsPresetSpec(
    preset_id="profitable_company",
    min_roe=Decimal("15"),
    min_gross_margin_ttm=Decimal("0.20"),
    sort_by="roe",
)

UNDERVALUED_GROWTH_SPEC = FundamentalsPresetSpec(
    preset_id="undervalued_growth",
    max_per=Decimal("20"),
    min_revenue_growth_3y_avg=Decimal("0.10"),
    min_earnings_growth_3y_avg=Decimal("0.20"),
    sort_by="earnings_growth_3y_avg",
)

STABLE_GROWTH_SPEC = FundamentalsPresetSpec(
    preset_id="stable_growth",
    min_roe=Decimal("15"),
    min_earnings_growth_3y_avg=Decimal("0.10"),
    min_earnings_increase_streak_years=3,
    sort_by="roe",
)

FUTURE_DIVIDEND_KING_SPEC = FundamentalsPresetSpec(
    preset_id="future_dividend_king",
    min_dividend_yield=Decimal("0.01"),
    min_dividend_growth_streak_years=3,
    min_earnings_increase_streak_years=3,
    min_payout_ratio=Decimal("30"),
    sort_by="dividend_yield",
)

FUNDAMENTALS_PRESET_SPECS: dict[str, FundamentalsPresetSpec] = {
    s.preset_id: s
    for s in (
        PROFITABLE_COMPANY_SPEC,
        UNDERVALUED_GROWTH_SPEC,
        STABLE_GROWTH_SPEC,
        FUTURE_DIVIDEND_KING_SPEC,
    )
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_fundamentals_screener.py::test_registry_has_four_specs_with_expected_thresholds -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/invest_view_model/fundamentals_screener.py tests/test_fundamentals_screener.py
git commit -m "feat(ROB-422): FundamentalsPresetSpec extension + 3 SPECs + registry (PR2b)"
```

---

## Task 2: Generalize `evaluate_fundamentals_candidates` (declarative checks + carry metrics + sort)

**Files:**
- Modify: `app/services/invest_view_model/fundamentals_screener.py`
- Test: `tests/test_fundamentals_screener.py`

- [ ] **Step 1: Write the failing tests (append)**

```python
from app.services.invest_view_model.fundamentals_screener import (
    STABLE_GROWTH_SPEC,
    UNDERVALUED_GROWTH_SPEC,
)


def _growth_period(year, *, revenue, net_income, filing_date):
    return FundamentalPeriod(
        fiscal_period=f"{year}A", period_type="annual",
        period_end_date=dt.date(year, 12, 31), filing_date=filing_date,
        revenue=Decimal(revenue), net_income=Decimal(net_income),
        discrete_revenue=Decimal(revenue), discrete_net_income=Decimal(net_income),
    )


def _four_growth_years(symbol, revs, nis):
    # revs/nis are 4 ascending-year values (2021..2024); filed the following March.
    return {symbol: [
        _growth_period(2021 + i, revenue=str(revs[i]), net_income=str(nis[i]),
                       filing_date=dt.date(2022 + i, 3, 20))
        for i in range(4)
    ]}


def test_stable_growth_includes_when_growth_and_streak_met():
    # net income 100→120→150→200 (all increases → streak 3; 3y-avg growth well above 10%)
    valuation_rows = [{"symbol": "005930", "roe": 20.0, "per": 9.0, "pbr": 1.1,
                       "market_cap": 5e11, "dividend_yield": 0.02}]
    periods = _four_growth_years("005930", [1000, 1100, 1300, 1600], [100, 120, 150, 200])
    rows, excluded = evaluate_fundamentals_candidates(
        valuation_rows=valuation_rows, periods_by_symbol=periods,
        spec=STABLE_GROWTH_SPEC, report_date=dt.date(2025, 6, 1), limit=20, name_map={},
    )
    assert [r["symbol"] for r in rows] == ["005930"]
    assert rows[0]["earnings_increase_streak_years"] == 3
    assert rows[0]["earnings_growth_3y_avg"] is not None


def test_stable_growth_excludes_when_streak_below_threshold():
    # net income dips in 2023 → streak ending 2024 is only 1 (< 3) → excluded.
    valuation_rows = [{"symbol": "005930", "roe": 20.0, "per": 9.0, "pbr": 1.1,
                       "market_cap": 5e11, "dividend_yield": 0.02}]
    periods = _four_growth_years("005930", [1000, 1100, 1300, 1600], [100, 120, 90, 200])
    rows, excluded = evaluate_fundamentals_candidates(
        valuation_rows=valuation_rows, periods_by_symbol=periods,
        spec=STABLE_GROWTH_SPEC, report_date=dt.date(2025, 6, 1), limit=20, name_map={},
    )
    assert rows == []
    assert any("earnings_increase_streak_years" in e["reason"] for e in excluded)


def test_undervalued_growth_excludes_when_growth_metric_unavailable_never_silent():
    # Only 1 annual period → 3y-avg growth is 'partial'/'unavailable' → excluded, not passed.
    valuation_rows = [{"symbol": "005930", "roe": 8.0, "per": 12.0, "pbr": 0.9,
                       "market_cap": 3e11, "dividend_yield": 0.01}]
    periods = {"005930": [_growth_period(2024, revenue="1600", net_income="200",
                                          filing_date=dt.date(2025, 3, 20))]}
    rows, excluded = evaluate_fundamentals_candidates(
        valuation_rows=valuation_rows, periods_by_symbol=periods,
        spec=UNDERVALUED_GROWTH_SPEC, report_date=dt.date(2025, 6, 1), limit=20, name_map={},
    )
    assert rows == []
    assert excluded  # never silently included
```

> The existing PR2a tests (profitable_company gross_margin path, ranking) must keep passing — Step 3's generalization must remain backward-compatible for `min_gross_margin_ttm`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_fundamentals_screener.py -k "stable_growth or undervalued_growth" -v`
Expected: FAIL (generalization not present; new metrics not checked/carried).

- [ ] **Step 3: Replace `evaluate_fundamentals_candidates` with the generalized version**

```python
# (spec field, derivation attribute) — each non-None spec field is checked.
_DERIVE_CHECKS: tuple[tuple[str, str], ...] = (
    ("min_gross_margin_ttm", "gross_margin_ttm"),
    ("min_revenue_growth_3y_avg", "revenue_growth_3y_avg"),
    ("min_earnings_growth_3y_avg", "earnings_growth_3y_avg"),
    ("min_payout_ratio", "payout_ratio"),
    ("min_earnings_increase_streak_years", "earnings_increase_streak_years"),
    ("min_dividend_growth_streak_years", "dividend_growth_streak_years"),
)

_CARRIED_DERIVE_METRICS = (
    "gross_margin_ttm",
    "revenue_growth_3y_avg",
    "earnings_growth_3y_avg",
    "payout_ratio",
    "earnings_increase_streak_years",
    "dividend_growth_streak_years",
)


def _metric_float(m: Any) -> float | None:
    return float(m.value) if m is not None and m.value is not None else None


def evaluate_fundamentals_candidates(
    *,
    valuation_rows: list[dict[str, Any]],
    periods_by_symbol: dict[str, list[FundamentalPeriod]],
    spec: FundamentalsPresetSpec,
    report_date: dt.date,
    limit: int,
    name_map: dict[str, str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Pure: apply the preset spec to candidates. Returns (included_rows, excluded).

    Each active derive threshold (non-None spec field) must be 'ok' AND meet the
    threshold; state != 'ok' or value None excludes the candidate (never a silent pass).
    """
    included: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for v in valuation_rows:
        symbol = v["symbol"]
        derivation = derive_fundamentals_metrics(
            periods_by_symbol.get(symbol, []), report_date=report_date
        )
        rejected = False
        for spec_field, metric_attr in _DERIVE_CHECKS:
            threshold = getattr(spec, spec_field)
            if threshold is None:
                continue
            metric = getattr(derivation, metric_attr)
            if metric.state != "ok" or metric.value is None:
                excluded.append({"symbol": symbol, "reason": f"{metric_attr} unavailable"})
                rejected = True
                break
            if Decimal(str(metric.value)) < Decimal(str(threshold)):
                excluded.append({"symbol": symbol, "reason": f"{metric_attr} below threshold"})
                rejected = True
                break
        if rejected:
            continue
        row = {
            "symbol": symbol,
            "market": "kr",
            "name": name_map.get(symbol),
            "roe": float(v["roe"]) if v.get("roe") is not None else None,
            "per": float(v["per"]) if v.get("per") is not None else None,
            "pbr": float(v["pbr"]) if v.get("pbr") is not None else None,
            "market_cap": float(v["market_cap"]) if v.get("market_cap") is not None else None,
            "dividend_yield": float(v["dividend_yield"]) if v.get("dividend_yield") is not None else None,
            "_screener_snapshot_state": v.get("_screener_snapshot_state", "fresh"),
        }
        for metric_attr in _CARRIED_DERIVE_METRICS:
            row[metric_attr] = _metric_float(getattr(derivation, metric_attr))
        included.append(row)
    included.sort(
        key=lambda r: (r.get(spec.sort_by) is None, -(r.get(spec.sort_by) or 0.0), r["symbol"])
    )
    return included[:limit], excluded
```

- [ ] **Step 4: Run tests to verify they pass (incl. PR2a backward-compat)**

Run: `uv run pytest tests/test_fundamentals_screener.py -v`
Expected: PASS — new stable_growth/undervalued_growth tests + all pre-existing PR2a tests (profitable_company include/exclude/unavailable/PIT/ranking).

- [ ] **Step 5: Commit**

```bash
git add app/services/invest_view_model/fundamentals_screener.py tests/test_fundamentals_screener.py
git commit -m "feat(ROB-422): generalize evaluate_fundamentals_candidates to declarative derive checks (PR2b)"
```

---

## Task 3: Generalize the loader's valuation SQL (max_per, min_dividend_yield, market_cap ordering)

**Files:**
- Modify: `app/services/invest_view_model/fundamentals_screener.py`
- Test: `tests/test_fundamentals_screener.py` (integration, `db_session`)

- [ ] **Step 1: Write the failing integration test (append)**

```python
import pytest

from app.models.market_valuation_snapshot import MarketValuationSnapshot
from app.services.invest_view_model.fundamentals_screener import (
    load_fundamentals_preset_from_snapshots,
    UNDERVALUED_GROWTH_SPEC,
)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_loader_valuation_filter_max_per_excludes_high_per(db_session):
    vd = dt.date(2026, 6, 2)
    db_session.add_all([
        MarketValuationSnapshot(market="kr", symbol="000001", snapshot_date=vd,
                                source="naver_finance", per=15, roe=10, dividend_yield=0.01,
                                market_cap=5e11),
        MarketValuationSnapshot(market="kr", symbol="000002", snapshot_date=vd,
                                source="naver_finance", per=40, roe=10, dividend_yield=0.01,
                                market_cap=4e11),  # PER 40 > 20 → excluded from candidates
    ])
    await db_session.commit()

    result = await load_fundamentals_preset_from_snapshots(
        db_session, market="kr", spec=UNDERVALUED_GROWTH_SPEC, limit=20,
        now=lambda: dt.datetime(2026, 6, 2, tzinfo=dt.UTC),
    )
    assert result is not None
    # both lack fundamentals rows → both excluded from results, but candidate filtering
    # is observable via excluded list: only the PER<=20 symbol reaches derive (then excluded
    # for missing fundamentals); the PER>20 symbol never becomes a candidate.
    excluded_symbols = {e["symbol"] for e in result.excluded}
    assert "000001" in excluded_symbols
    assert "000002" not in excluded_symbols  # filtered at SQL candidate stage
    assert result.fundamentals_state == "missing"  # no fundamentals backfilled
```

> The exact `MarketValuationSnapshot(...)` kwargs must match the model columns (market/symbol/snapshot_date/source/per/pbr/roe/dividend_yield/market_cap...). Confirm against `app/models/market_valuation_snapshot.py` before running; add any NOT NULL columns the model requires.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_fundamentals_screener.py::test_loader_valuation_filter_max_per_excludes_high_per -v`
Expected: FAIL (loader applies only `min_roe`; `max_per`/`min_dividend_yield` not yet; `dividend_yield` not selected).

- [ ] **Step 3: Update the candidate query in `load_fundamentals_preset_from_snapshots`**

Replace the `cand_stmt` construction (the SELECT + `if spec.min_roe` + `order_by(roe).limit(...)` block) with:

```python
    cand_stmt = sa.select(
        MarketValuationSnapshot.symbol,
        MarketValuationSnapshot.roe,
        MarketValuationSnapshot.per,
        MarketValuationSnapshot.pbr,
        MarketValuationSnapshot.market_cap,
        MarketValuationSnapshot.dividend_yield,
    ).where(
        MarketValuationSnapshot.market == "kr",
        MarketValuationSnapshot.snapshot_date == val_date,
    )
    if spec.min_roe is not None:
        cand_stmt = cand_stmt.where(MarketValuationSnapshot.roe >= spec.min_roe)
    if spec.max_per is not None:
        cand_stmt = cand_stmt.where(
            MarketValuationSnapshot.per > 0,
            MarketValuationSnapshot.per <= spec.max_per,
        )
    if spec.min_dividend_yield is not None:
        cand_stmt = cand_stmt.where(
            MarketValuationSnapshot.dividend_yield >= spec.min_dividend_yield
        )
    # Cap the candidate universe by market_cap (prefer liquid names); ranking by the
    # preset's sort_by happens AFTER derive. Surface truncation honestly (no silent cap).
    _cand_cap = max(limit * 8, 200)
    cand_stmt = cand_stmt.order_by(
        MarketValuationSnapshot.market_cap.desc().nullslast()
    ).limit(_cand_cap)
    cand_mappings = list((await session.execute(cand_stmt)).mappings().all())
    if len(cand_mappings) >= _cand_cap:
        logger.warning(
            "fundamentals_screener: candidate universe capped at %d for preset=%s "
            "(some lower-market-cap candidates not evaluated)",
            _cand_cap,
            spec.preset_id,
        )
```

(The downstream `valuation_rows = [{**dict(m), "_screener_snapshot_state": val_state} ...]` already carries `dividend_yield` now that it is SELECTed.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_fundamentals_screener.py::test_loader_valuation_filter_max_per_excludes_high_per -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/invest_view_model/fundamentals_screener.py tests/test_fundamentals_screener.py
git commit -m "feat(ROB-422): loader valuation filters max_per/min_dividend_yield + market_cap cap (PR2b)"
```

---

## Task 4: Registry-driven dispatch in screener_service.py

**Files:**
- Modify: `app/services/invest_view_model/screener_service.py`
- Test: `tests/test_screener_service_profitable_company.py`

- [ ] **Step 1: Write the failing test (append)**

```python
@pytest.mark.asyncio
async def test_stable_growth_routes_to_fundamentals_loader(monkeypatch):
    captured = {}

    async def _fake_loader(session, *, market, spec, limit, now):
        captured["preset_id"] = spec.preset_id
        from app.services.invest_view_model.fundamentals_screener import (
            FundamentalsScreenResult,
        )
        return FundamentalsScreenResult(
            rows=[], valuation_partition_date=dt.date(2026, 6, 2),
            fundamentals_partition_date=None, fundamentals_collected_at=None,
            fundamentals_state="missing",
        )

    monkeypatch.setattr(
        "app.services.invest_view_model.fundamentals_screener.load_fundamentals_preset_from_snapshots",
        _fake_loader,
    )
    result = await screener_service.build_screener_results(
        preset_id="stable_growth", market="kr",
        session=object(), screening_service=_StubScreening(),
    )
    assert captured["preset_id"] == "stable_growth"  # registry routed the right spec
    assert result.freshness.primary.source == "market_valuation_snapshots"
    assert "fundamentals" in {d.kind for d in result.freshness.dependencies}
```

> Match `build_screener_results` call/return accessors to the live signature (same as the PR2a service test in this file).

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_screener_service_profitable_company.py::test_stable_growth_routes_to_fundamentals_loader -v`
Expected: FAIL (`stable_growth` not dispatched — only `profitable_company` is).

- [ ] **Step 3: Generalize the three dispatch sites**

(a) Dispatch branch — replace `elif preset_id == "profitable_company":` (line ~1552) condition and the `spec=PROFITABLE_COMPANY_SPEC` argument:

```python
        elif preset_id in FUNDAMENTALS_PRESET_SPECS:
            from app.services.invest_view_model.fundamentals_screener import (
                FUNDAMENTALS_PRESET_SPECS,
                load_fundamentals_preset_from_snapshots,
            )

            _fundamentals_screen_result = await load_fundamentals_preset_from_snapshots(
                session,
                market=requested_market,
                spec=FUNDAMENTALS_PRESET_SPECS[preset_id],
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
                "최신 밸류에이션/재무 스냅샷에서 해당 프리셋 조건에 맞는 종목이 없습니다."
            )
```

For the `elif preset_id in FUNDAMENTALS_PRESET_SPECS:` condition to reference the dict, add a module-level import at the top of `screener_service.py` (alongside other invest_view_model imports):
```python
from app.services.invest_view_model.fundamentals_screener import FUNDAMENTALS_PRESET_SPECS
```
(If a top-level import risks a circular import — `fundamentals_screener` imports `_is_kr_toss_common_stock` from `screener_service` inside its function, not at module load, so a top-level import here is safe. Verify by running the test; if a cycle appears, guard the membership check with a local import helper instead.)

(b) Snapshot-only guard — change line ~1614 from `if preset_id == "profitable_company" and ...` to:

```python
    if preset_id in FUNDAMENTALS_PRESET_SPECS and _snapshot_check_result is None:
        _snapshot_check_result = []
        _snapshot_state_override = "missing"
        _snapshot_empty_warning = (
            "밸류에이션/재무 스냅샷이 아직 적재되지 않아 해당 프리셋 후보를 표시할 수 없습니다."
        )
```

(c) primary_source — change line ~1711 from `elif preset_id == "profitable_company":` to:

```python
        elif preset_id in FUNDAMENTALS_PRESET_SPECS:
            primary_source = "market_valuation_snapshots"
```

(The fundamentals dependency append at line ~1795 already keys on `_fundamentals_screen_result is not None`, so it covers all registry presets with no change.)

- [ ] **Step 4: Run tests to verify they pass (incl. profitable_company regression)**

Run: `uv run pytest tests/test_screener_service_profitable_company.py -v`
Expected: PASS — new stable_growth routing test + the 2 pre-existing profitable_company tests (loader-used/snapshot-only + missing-when-None).

- [ ] **Step 5: Commit**

```bash
git add app/services/invest_view_model/screener_service.py tests/test_screener_service_profitable_company.py
git commit -m "feat(ROB-422): registry-driven fundamentals preset dispatch (PR2b)"
```

---

## Task 5: Add the 3 catalog entries

**Files:**
- Modify: `app/services/invest_view_model/screener_presets.py`
- Test: `tests/test_screener_presets_pr2b.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_screener_presets_pr2b.py
from __future__ import annotations

from app.services.invest_view_model.screener_presets import (
    _KR_ONLY_PRESET_IDS,
    build_screener_presets,
)


def test_three_pr2b_presets_present_full_parity_kr_only():
    presets = {p.id: p for p in build_screener_presets(market="kr")}
    for pid, name in [
        ("undervalued_growth", "저평가 성장주"),
        ("stable_growth", "안정 성장주"),
        ("future_dividend_king", "미래의 배당왕"),
    ]:
        assert pid in presets, pid
        assert presets[pid].name == name
        assert presets[pid].presetOrigin == "toss_parity"
        assert presets[pid].parityStatus == "full"
        assert pid in _KR_ONLY_PRESET_IDS
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_screener_presets_pr2b.py -v`
Expected: FAIL (presets absent).

- [ ] **Step 3: Add the 3 entries to `SCREENER_PRESETS` (next to `profitable_company`)**

```python
    ScreenerPreset(
        id="undervalued_growth",
        name="저평가 성장주",
        description="저평가(PER)면서 매출·순이익이 꾸준히 성장하는 기업",
        badges=["국내"],
        filterChips=[
            ScreenerFilterChip(label="국내"),
            ScreenerFilterChip(label="PER", detail="0~20"),
            ScreenerFilterChip(label="매출증가율", detail="3년평균 10% 이상"),
            ScreenerFilterChip(label="순이익증가율", detail="3년평균 20% 이상"),
            ScreenerFilterChip(label="데이터", detail="지연 스냅샷 기반"),
        ],
        metricLabel="순이익증가율",
        market="kr",
        presetOrigin="toss_parity",
        parityStatus="full",
    ),
    ScreenerPreset(
        id="stable_growth",
        name="안정 성장주",
        description="높은 ROE와 꾸준한 순이익 성장·연속증가를 갖춘 안정 성장 기업",
        badges=["국내"],
        filterChips=[
            ScreenerFilterChip(label="국내"),
            ScreenerFilterChip(label="ROE", detail="15% 이상"),
            ScreenerFilterChip(label="순이익증가율", detail="3년평균 10% 이상"),
            ScreenerFilterChip(label="순이익", detail="연속증가 3년 이상"),
            ScreenerFilterChip(label="데이터", detail="지연 스냅샷 기반"),
        ],
        metricLabel="ROE",
        market="kr",
        presetOrigin="toss_parity",
        parityStatus="full",
    ),
    ScreenerPreset(
        id="future_dividend_king",
        name="미래의 배당왕",
        description="배당을 꾸준히 늘리고 순이익도 연속 증가하는 미래 배당 성장 기업",
        badges=["국내"],
        filterChips=[
            ScreenerFilterChip(label="국내"),
            ScreenerFilterChip(label="배당수익률", detail="1% 이상"),
            ScreenerFilterChip(label="배당", detail="연속성장 3년 이상"),
            ScreenerFilterChip(label="순이익", detail="연속증가 3년 이상"),
            ScreenerFilterChip(label="배당성향", detail="30% 이상"),
            ScreenerFilterChip(label="데이터", detail="지연 스냅샷 기반"),
        ],
        metricLabel="배당수익률",
        market="kr",
        presetOrigin="toss_parity",
        parityStatus="full",
    ),
```

Update `_KR_ONLY_PRESET_IDS` (line 27) to include the 3 ids:
```python
_KR_ONLY_PRESET_IDS = {
    "investor_flow_momentum", "double_buy", "high_yield_value", "profitable_company",
    "undervalued_growth", "stable_growth", "future_dividend_king",
}
```

> These presets are snapshot-only (dispatched via `FUNDAMENTALS_PRESET_SPECS`), so they need NO `_SCREENING_FILTERS` entry — match exactly how `profitable_company` is handled (PR2a added none). If a drift test requires every preset to have a `_SCREENING_FILTERS` key, mirror profitable_company's resolution.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_screener_presets_pr2b.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/invest_view_model/screener_presets.py tests/test_screener_presets_pr2b.py
git commit -m "feat(ROB-422): 3 PR2b preset catalog entries (toss_parity/full)"
```

---

## Task 6: Parity matrix doc update

**Files:**
- Modify: `docs/invest-screener-toss-parity-matrix.md`

- [ ] **Step 1: Update the three rows**

Change 저평가 성장주 (#2) → `full / undervalued_growth`, 미래의 배당왕 (#7) → `full / future_dividend_king`, 안정 성장주 (#11) → `full / stable_growth`. Note each: implemented in ROB-422 PR2b via the named fundamentals metrics; data operator-backfill-gated (empty → dataState=missing). Leave the mismatch-2 / partial-2 rows for PR2c.

- [ ] **Step 2: Commit**

```bash
git add docs/invest-screener-toss-parity-matrix.md
git commit -m "docs(ROB-422): parity matrix — 저평가성장주·안정성장주·미래의배당왕 full (PR2b)"
```

---

## Task 7: Verification + lint + regression

**Files:** none.

- [ ] **Step 1: PR2b + PR2a + PR1 module tests**

Run: `uv run pytest tests/test_fundamentals_screener.py tests/test_screener_service_profitable_company.py tests/test_screener_presets_profitable_company.py tests/test_screener_presets_pr2b.py tests/test_financial_fundamentals_derive.py tests/test_financial_fundamentals_snapshots_repository.py -v`
Expected: all PASS.

- [ ] **Step 2: Regression — full-3 + valuation + high_yield_value**

Run: `uv run pytest tests/test_invest_view_model_high_yield_value_screener.py tests/test_invest_coverage_valuation.py -v`
Expected: PASS.

- [ ] **Step 3: Lint + format**

Run: `uv run ruff check app/ tests/ && uv run ruff format --check app/services/invest_view_model/fundamentals_screener.py app/services/invest_view_model/screener_service.py app/services/invest_view_model/screener_presets.py tests/test_fundamentals_screener.py tests/test_screener_presets_pr2b.py tests/test_screener_service_profitable_company.py`
Expected: clean (else `ruff format <files>` + recommit).

- [ ] **Step 4: Type check**

Run: `uv run ty check app/services/invest_view_model/fundamentals_screener.py`
Expected: clean (or pre-existing repo-wide noise only).

- [ ] **Step 5: Commit any fixups**

```bash
git add -A
git commit -m "chore(ROB-422): PR2b lint/format fixups" || echo "nothing to commit"
```

---

## Self-Review (completed by plan author)

**Spec coverage (spec §-by-§):**
- §1 three presets + criteria→metric/source → Tasks 1 (specs), 2 (derive checks), 3 (valuation filters), 5 (catalog). ✓
- §2 approach A: spec fields + valuation→SQL + derive→evaluate; units (growth ratio / payout percent / streak int / dividend_yield ratio 0.01) → Tasks 1, 2, 3. ✓
- §2.1 valuation SQL generalization + market_cap cap + coverage warning → Task 3. ✓
- §2.2 declarative derive checks + unavailable→exclude+note + carry metrics → Task 2. ✓
- §2.3 sort generalization (row carries sort metric; `spec.sort_by` direct key) → Task 2. ✓
- §3 dispatch registry (`FUNDAMENTALS_PRESET_SPECS`) → Tasks 1 (registry), 4 (3 dispatch sites). ✓
- §4 catalog entries + _KR_ONLY → Task 5. ✓
- §5 parity matrix doc → Task 6. ✓
- §6 dividend_yield unit RESOLVED to ratio 0.01 (plan header) — no longer open; growth-depth is a runtime/backfill note, not code. ✓
- §7 tests → every task TDD; below-threshold, unavailable-never-silent, streak, valuation filter, registry routing, catalog all covered. ✓
- §8 safety: read-only, no migration (no migration task), KR-only, snapshot-only (registry guard), derive→screener direction. ✓

**Placeholder scan:** every code step has complete code. Three verify-against-live notes (Task 3 Step 1 MarketValuationSnapshot kwargs; Task 4 Step 3 circular-import guard + build_screener_results signature; Task 5 Step 3 _SCREENING_FILTERS drift) are bounded checks with concrete fallbacks, not placeholders.

**Type/name consistency:** `FundamentalsPresetSpec` new fields (`max_per`, `min_dividend_yield`, `min_revenue_growth_3y_avg`, `min_earnings_growth_3y_avg`, `min_earnings_increase_streak_years`, `min_dividend_growth_streak_years`, `min_payout_ratio`) match between Task 1 (definition), Task 2 (`_DERIVE_CHECKS` consumes the derive ones), and Task 3 (loader consumes the valuation ones). `FUNDAMENTALS_PRESET_SPECS` defined in Task 1, consumed in Task 4. `_CARRIED_DERIVE_METRICS`/`_metric_float` used in Task 2; `sort_by` keys (`earnings_growth_3y_avg`, `dividend_yield`, `roe`) are all carried on the row. The 3 preset ids (`undervalued_growth`/`stable_growth`/`future_dividend_king`) are identical across Tasks 1, 4, 5, 6. ✓

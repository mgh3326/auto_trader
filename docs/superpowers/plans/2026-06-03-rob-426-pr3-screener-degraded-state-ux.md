# ROB-426 PR3 — `/invest/screener` Degraded-State UX Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface the partition-health intelligence PR2a/2b already compute as honest, distinct user-facing UX on `/invest/screener` — a structured degradation reason + coverage label, a fix for the healthy-but-empty `stale` mislabel, and a `market_cap` value for the three non-valuation KR presets.

**Architecture:** Thread a `degradation_reason` + `coverage_label` from each KR snapshot loader's `HealthyPartition` through `_SnapshotLoadResult` → `_build_freshness` → `ScreenerFreshnessPrimary` (Pydantic + TS, `extra="forbid"` so both move in lockstep). Add `market_cap` to `consecutive_gainers`/`investor_flow`/`double_buy` via a per-symbol valuation lookup dict (mirrors the existing `symbol_names` pattern; does not disturb the existing scalar queries). Frontend renders a reason-switched empty/degraded component (desktop only). `DataState` enum and `partition_health.py`/`guards.py`/`freshness.py` are unchanged.

**Tech Stack:** Python 3.13, SQLAlchemy async, Pydantic v2, FastAPI, pytest; React + TypeScript + Vitest (frontend `frontend/invest`).

**Spec:** `docs/superpowers/specs/2026-06-03-rob-426-pr3-screener-degraded-state-ux-design.md`

**Scope boundary (no silent caps):** Fine-grained `degradation_reason` + `coverage_label` + the `healthy_no_matches` fix + the `market_cap` join are applied to **`consecutive_gainers`, `investor_flow`, `double_buy`** only — the three non-valuation KR presets that read the price/flow partitions where the 20-row vs 3,800-row regression occurred. The value presets (`high_yield_value`, `undervalued_breakout`, fundamentals presets) already route healthy valuation partitions via PR2a and already display `market_cap`; their existing `cap_degraded → "stale"` labeling is retained unchanged in this PR (fine-grained reason for value presets = minor follow-up). Crypto path unchanged. US `market_cap` stays `-` (null at the Yahoo source — separate builder PR). Mobile screener page does not exist and is out of scope.

**Invariants:** migration 0; broker/order/watch/order-intent mutation 0; `app/services/invest_screener_snapshots/partition_health.py`, `.../guards.py`, `app/services/snapshot_commit_guard.py`, `app/services/invest_screener_snapshots/freshness.py` unchanged.

**Verify after every backend task:** `uv run ruff check app/ tests/` and `uv run ruff format --check app/ tests/` (CI scope is `app/ tests/` only). Frontend: `cd frontend/invest && npx vitest run <file>`.

---

## Reason taxonomy (reference for all tasks)

`degradationReason` values and their triggers:

| value | trigger | chip `dataState` |
|---|---|---|
| `snapshot_missing` | no partition; loader returned `None`; `_snapshot_state_override="missing"` | `missing` |
| `coverage_below_floor` | `hp.healthy is False` (thin newest) | `stale` + `coverageLabel` |
| `older_fallback` | `hp.is_fallback is True` (older healthy served) | `stale` |
| `healthy_no_matches` | partition healthy & non-fallback, but 0 qualifiers | partition's date-based state (**not** `stale`) |
| `live` | snapshot path not taken (`primary_kind="live"`) | existing |
| `None` | fresh partition with results | existing |

`coverageLabel` format: `"{rows:,} / {universe:,} ({pct:.1f}%)"` where `universe = round(hp.row_count / hp.coverage_ratio)` (derivable from `hp` alone; no extra query). `hp.row_count`/`hp.coverage_ratio` come from `HealthyPartition` (`app/services/invest_screener_snapshots/partition_health.py:43-50`).

---

## Task 1: Contract — add `degradationReason` + `coverageLabel` + `marketCapSource` (Pydantic + TS)

**Files:**
- Modify: `app/schemas/invest_screener.py:108-145`
- Modify: `frontend/invest/src/types/screener.ts:87-120`
- Test: `tests/test_invest_screener_schemas.py`

- [ ] **Step 1: Write the failing schema round-trip test**

Append to `tests/test_invest_screener_schemas.py`:

```python
def test_freshness_primary_accepts_degradation_reason_and_coverage_label():
    from app.schemas.invest_screener import ScreenerFreshnessPrimary

    primary = ScreenerFreshnessPrimary(
        kind="screener_snapshot",
        asOfLabel="2026.06.03 15:30 기준",
        dataState="stale",
        degradationReason="coverage_below_floor",
        coverageLabel="20 / 3,800 (0.5%)",
    )
    assert primary.degradationReason == "coverage_below_floor"
    assert primary.coverageLabel == "20 / 3,800 (0.5%)"
    # defaults remain optional/None
    bare = ScreenerFreshnessPrimary(
        kind="live", asOfLabel="x", dataState="missing"
    )
    assert bare.degradationReason is None
    assert bare.coverageLabel is None


def test_result_row_accepts_market_cap_source():
    from app.schemas.invest_screener import ScreenerResultRow

    row = ScreenerResultRow(
        rank=1,
        symbol="005930",
        market="kr",
        name="삼성전자",
        priceLabel="70,000원",
        changePctLabel="+1.0%",
        changeAmountLabel="+700원",
        changeDirection="up",
        category="-",
        marketCapLabel="418조원",
        volumeLabel="1,000,000",
        analystLabel="-",
        metricValueLabel="-",
        marketCapSource="fallback",
    )
    assert row.marketCapSource == "fallback"
    bare = ScreenerResultRow(
        rank=1, symbol="x", market="kr", name="x", priceLabel="-",
        changePctLabel="-", changeAmountLabel="-", changeDirection="flat",
        category="-", marketCapLabel="-", volumeLabel="-", analystLabel="-",
        metricValueLabel="-",
    )
    assert bare.marketCapSource is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_invest_screener_schemas.py::test_freshness_primary_accepts_degradation_reason_and_coverage_label tests/test_invest_screener_schemas.py::test_result_row_accepts_market_cap_source -v`
Expected: FAIL with `ValidationError` (`extra="forbid"` rejects `degradationReason`/`coverageLabel`/`marketCapSource`).

- [ ] **Step 3: Add the Pydantic fields**

In `app/schemas/invest_screener.py`, extend `ScreenerResultRow` (after line 129 `candidateContext`):

```python
    candidateContext: ScreenerCandidateContext | None = None
    # ROB-426 PR3: provenance of marketCapLabel for the non-valuation KR presets.
    marketCapSource: Literal["primary", "fallback"] | None = None
```

Extend `ScreenerFreshnessPrimary` (after line 145 `source`):

```python
    source: str | None = None
    # ROB-426 PR3: structured degraded-state context. dataState (the chip) stays
    # frozen; these carry the *why* and a coverage label for the thin-partition case.
    degradationReason: (
        Literal[
            "snapshot_missing",
            "coverage_below_floor",
            "older_fallback",
            "healthy_no_matches",
            "live",
        ]
        | None
    ) = None
    coverageLabel: str | None = None
```

- [ ] **Step 4: Add the TS fields**

In `frontend/invest/src/types/screener.ts`, extend `ScreenerResultRow` (after line 107 `candidateContext`):

```typescript
  candidateContext?: ScreenerCandidateContext | null;
  // ROB-426 PR3
  marketCapSource?: "primary" | "fallback" | null;
}
```

Add the reason type and extend `ScreenerFreshnessPrimary` (after line 119 `source`):

```typescript
export type ScreenerDegradationReason =
  | "snapshot_missing"
  | "coverage_below_floor"
  | "older_fallback"
  | "healthy_no_matches"
  | "live";

export interface ScreenerFreshnessPrimary {
  kind: "screener_snapshot" | "live" | "fallback";
  snapshotDate: string | null;
  computedAt: string | null;
  asOfLabel: string;
  dataState: ScreenerDataState;
  source: string | null;
  // ROB-426 PR3
  degradationReason?: ScreenerDegradationReason | null;
  coverageLabel?: string | null;
}
```

- [ ] **Step 5: Run tests + typecheck**

Run: `uv run pytest tests/test_invest_screener_schemas.py -v && cd frontend/invest && npx tsc --noEmit && cd -`
Expected: PASS; tsc clean.

- [ ] **Step 6: Commit**

```bash
git add app/schemas/invest_screener.py frontend/invest/src/types/screener.ts tests/test_invest_screener_schemas.py
git commit -m "feat(ROB-426): PR3 contract — degradationReason/coverageLabel/marketCapSource (Pydantic+TS)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: `_SnapshotLoadResult` fields + `_partition_degradation` helper

**Files:**
- Modify: `app/services/invest_view_model/screener_service.py:112-120` (dataclass)
- Modify: `app/services/invest_view_model/screener_service.py` (add helper near the dataclass)
- Test: `tests/test_invest_view_model_screener_service.py`

- [ ] **Step 1: Write the failing helper test**

Append to `tests/test_invest_view_model_screener_service.py`:

```python
def test_partition_degradation_maps_each_case():
    from app.services.invest_screener_snapshots.partition_health import HealthyPartition
    from app.services.invest_view_model.screener_service import _partition_degradation

    # no partition
    assert _partition_degradation(None, rows_empty=True) == ("snapshot_missing", None)

    # thin newest (not healthy): coverage_ratio 20/3800
    thin = HealthyPartition(
        partition_date=__import__("datetime").date(2026, 5, 22),
        row_count=20, coverage_ratio=20 / 3800, is_fallback=False, healthy=False,
    )
    reason, label = _partition_degradation(thin, rows_empty=False)
    assert reason == "coverage_below_floor"
    assert label == "20 / 3,800 (0.5%)"

    # older healthy fallback
    older = HealthyPartition(
        partition_date=__import__("datetime").date(2026, 5, 20),
        row_count=3800, coverage_ratio=1.0, is_fallback=True, healthy=True,
    )
    assert _partition_degradation(older, rows_empty=False) == ("older_fallback", None)

    # healthy newest, no qualifiers
    healthy = HealthyPartition(
        partition_date=__import__("datetime").date(2026, 6, 3),
        row_count=3800, coverage_ratio=1.0, is_fallback=False, healthy=True,
    )
    assert _partition_degradation(healthy, rows_empty=True) == ("healthy_no_matches", None)

    # healthy newest with rows -> no degradation
    assert _partition_degradation(healthy, rows_empty=False) == (None, None)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_invest_view_model_screener_service.py::test_partition_degradation_maps_each_case -v`
Expected: FAIL with `ImportError`/`AttributeError` (`_partition_degradation` not defined).

- [ ] **Step 3: Extend the dataclass + add the helper**

In `app/services/invest_view_model/screener_service.py`, replace the `_SnapshotLoadResult` dataclass (lines 112-120) with:

```python
@dataclass
class _SnapshotLoadResult:
    """ROB-277 follow-up: snapshot loaders must surface latest-partition metadata
    even when no rows qualified, so freshness.primary can still report the
    correct snapshot date. ROB-426 PR3 adds the degradation reason + coverage
    label derived from the served HealthyPartition."""

    rows: list[dict[str, Any]]
    partition_date: dt.date | None
    partition_computed_at: datetime | None = None
    # ROB-426 PR3
    degradation_reason: str | None = None
    coverage_label: str | None = None
```

Immediately after the dataclass (before `def _investor_flow_chip_for_item`), add:

```python
def _partition_degradation(
    hp: "HealthyPartition | None", *, rows_empty: bool
) -> tuple[str | None, str | None]:
    """Map a served HealthyPartition to (degradation_reason, coverage_label).

    The five HealthyPartition shapes from resolve_healthy_partition are mutually
    exclusive: (is_fallback=False, healthy=True) newest-healthy; (is_fallback=True,
    healthy=True) older-fallback; (is_fallback=False, healthy=False) thin-newest.
    """
    if hp is None:
        return ("snapshot_missing", None)
    if not hp.healthy:
        universe = round(hp.row_count / hp.coverage_ratio) if hp.coverage_ratio > 0 else 0
        label = (
            f"{hp.row_count:,} / {universe:,} ({hp.coverage_ratio * 100:.1f}%)"
            if universe > 0
            else None
        )
        return ("coverage_below_floor", label)
    if hp.is_fallback:
        return ("older_fallback", None)
    if rows_empty:
        return ("healthy_no_matches", None)
    return (None, None)
```

Add the import for the type hint at the top of the module (near the other `partition_health` imports if present, else under `TYPE_CHECKING`). Find the existing top-level imports block and add:

```python
from app.services.invest_screener_snapshots.partition_health import HealthyPartition
```

(If a circular import surfaces at module load, instead add `from app.services.invest_screener_snapshots.partition_health import HealthyPartition` inside a `if TYPE_CHECKING:` block and keep the runtime annotation as the string `"HealthyPartition | None"` already used above.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_invest_view_model_screener_service.py::test_partition_degradation_maps_each_case -v`
Expected: PASS.

- [ ] **Step 5: Lint + commit**

Run: `uv run ruff check app/ tests/ && uv run ruff format --check app/ tests/`

```bash
git add app/services/invest_view_model/screener_service.py tests/test_invest_view_model_screener_service.py
git commit -m "feat(ROB-426): PR3 _SnapshotLoadResult reason/coverage + _partition_degradation helper

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: `consecutive_gainers` loader — reason/coverage + market_cap join

**Files:**
- Modify: `app/services/invest_view_model/screener_service.py:412-558` (`_load_consecutive_gainers_from_snapshots`)
- Test: `tests/test_invest_view_model_screener_service.py`

The loader holds `hp` (line 433-439) and currently builds `candidate_symbols` for KR (line 482). Add a per-symbol `market_cap` lookup against the healthy valuation partition (mirrors the existing `symbol_names` lookup), then set the new `_SnapshotLoadResult` fields.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_invest_view_model_screener_service.py` (reuse the file's existing fake-session/builder helpers — check the top of the file for the in-repo `_FakeSession`/fixture pattern and follow it; the assertions below are loader-output level):

```python
@pytest.mark.asyncio
async def test_consecutive_gainers_thin_partition_sets_coverage_reason(monkeypatch):
    """When the served partition is thin (not healthy), the loader result carries
    degradation_reason='coverage_below_floor' and a coverage label."""
    import app.services.invest_view_model.screener_service as svc
    from app.services.invest_screener_snapshots.partition_health import HealthyPartition

    thin = HealthyPartition(
        partition_date=dt.date(2026, 5, 22), row_count=20,
        coverage_ratio=20 / 3800, is_fallback=False, healthy=False,
    )

    async def _fake_resolve(session, **kwargs):
        return thin

    monkeypatch.setattr(svc, "resolve_healthy_partition", _fake_resolve, raising=False)
    # ... build a session that returns at least one qualifying InvestScreenerSnapshot
    # row at partition_date 2026-05-22 (follow the existing fake-session helper in
    # this test module), then:
    result = await svc._load_consecutive_gainers_from_snapshots(
        session, market="kr", limit=10, now=lambda: dt.datetime(2026, 5, 22, 6, tzinfo=dt.UTC)
    )
    assert result is not None
    assert result.degradation_reason == "coverage_below_floor"
    assert result.coverage_label == "20 / 3,800 (0.5%)"
```

> **Implementer note:** `_load_consecutive_gainers_from_snapshots` imports `resolve_healthy_partition` *inside* the function (line 418-421). Monkeypatching `svc.resolve_healthy_partition` will not intercept a function-local import. Either (a) patch `app.services.invest_screener_snapshots.partition_health.resolve_healthy_partition`, or (b) drive it through a real `_FakeSession` that returns controlled `distinct dates` + row counts (preferred — mirror the existing tests in this module). Pick whichever the existing tests already use and stay consistent.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_invest_view_model_screener_service.py::test_consecutive_gainers_thin_partition_sets_coverage_reason -v`
Expected: FAIL (`result.degradation_reason` is `None`/attr missing path returns no reason yet).

- [ ] **Step 3: Add the market_cap lookup + reason fields**

In `_load_consecutive_gainers_from_snapshots`, the KR name lookup block ends at line 496. Immediately after it (still inside the `if market == "kr" and candidate_snaps:` scope is NOT required — do this in its own KR-guarded block), insert the valuation lookup:

```python
    # ROB-426 PR3: market_cap for this non-valuation preset — look up the healthy
    # KR valuation partition by symbol (mirrors the symbol-name lookup above; does
    # not disturb the InvestScreenerSnapshot scalar query).
    market_cap_map: dict[str, float] = {}
    market_cap_source: str | None = None
    if market == "kr" and candidate_snaps:
        from app.models.market_valuation_snapshot import MarketValuationSnapshot

        val_hp = await resolve_healthy_partition(
            session,
            model=MarketValuationSnapshot,
            date_col=MarketValuationSnapshot.snapshot_date,
            market_col=MarketValuationSnapshot.market,
            market="kr",
        )
        if val_hp is not None:
            market_cap_source = "fallback" if val_hp.is_fallback else "primary"
            try:
                _mc = await session.execute(
                    sa.select(
                        MarketValuationSnapshot.symbol,
                        MarketValuationSnapshot.market_cap,
                    ).where(
                        MarketValuationSnapshot.market == "kr",
                        MarketValuationSnapshot.snapshot_date == val_hp.partition_date,
                        MarketValuationSnapshot.symbol.in_(
                            [snap.symbol for snap in candidate_snaps]
                        ),
                    )
                )
                market_cap_map = {
                    r.symbol: float(r.market_cap)
                    for r in _mc.all()
                    if r.market_cap is not None
                }
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "consecutive_gainers: market_cap lookup failed: %s",
                    exc,
                    exc_info=True,
                )
```

In the per-row dict (lines 517-541), add two keys (after `"_screener_snapshot_state": state,`):

```python
                "_screener_snapshot_state": state,
                "market_cap": market_cap_map.get(snap.symbol),
                "_market_cap_source": (
                    market_cap_source if snap.symbol in market_cap_map else None
                ),
```

Replace the `return _SnapshotLoadResult(...)` (lines 554-558) with:

```python
    reason, coverage_label = _partition_degradation(hp, rows_empty=not rows)
    return _SnapshotLoadResult(
        rows=rows,
        partition_date=latest_snapshot_date,
        partition_computed_at=partition_computed_at,
        degradation_reason=reason,
        coverage_label=coverage_label,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_invest_view_model_screener_service.py -k "consecutive_gainers" -v`
Expected: PASS.

- [ ] **Step 5: Lint + commit**

Run: `uv run ruff check app/ tests/ && uv run ruff format --check app/ tests/`

```bash
git add app/services/invest_view_model/screener_service.py tests/test_invest_view_model_screener_service.py
git commit -m "feat(ROB-426): PR3 consecutive_gainers reason/coverage + KR market_cap join

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: `investor_flow` loader — reason/coverage + market_cap join

**Files:**
- Modify: `app/services/invest_view_model/screener_service.py:595-722` (`_load_investor_flow_discovery_from_snapshots`)
- Test: `tests/test_invest_view_model_screener_service.py`

This loader holds `hp` (line 608), builds `candidate_symbols` (line 653), and returns `_SnapshotLoadResult` (lines 718-722).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_invest_view_model_screener_service.py`:

```python
@pytest.mark.asyncio
async def test_investor_flow_older_fallback_sets_reason(monkeypatch):
    import app.services.invest_view_model.screener_service as svc
    from app.services.invest_screener_snapshots.partition_health import HealthyPartition

    older = HealthyPartition(
        partition_date=dt.date(2026, 5, 30), row_count=3800,
        coverage_ratio=1.0, is_fallback=True, healthy=True,
    )
    # Drive via _FakeSession returning the older partition + >=1 qualifying
    # InvestorFlowSnapshot row (follow the module's existing helper). Then:
    result = await svc._load_investor_flow_discovery_from_snapshots(
        session, market="kr", limit=10,
        now=lambda: dt.datetime(2026, 6, 3, 6, tzinfo=dt.UTC),
    )
    assert result is not None
    assert result.degradation_reason == "older_fallback"
    assert result.coverage_label is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_invest_view_model_screener_service.py::test_investor_flow_older_fallback_sets_reason -v`
Expected: FAIL (`degradation_reason` is `None`).

- [ ] **Step 3: Add the market_cap lookup + reason fields**

After the KR name lookup block (ends line 667), insert (same shape as Task 3, using this loader's `candidate_snaps`):

```python
    # ROB-426 PR3: market_cap via the healthy KR valuation partition.
    market_cap_map: dict[str, float] = {}
    market_cap_source: str | None = None
    if candidate_snaps:
        from app.models.market_valuation_snapshot import MarketValuationSnapshot

        val_hp = await resolve_healthy_partition(
            session,
            model=MarketValuationSnapshot,
            date_col=MarketValuationSnapshot.snapshot_date,
            market_col=MarketValuationSnapshot.market,
            market="kr",
        )
        if val_hp is not None:
            market_cap_source = "fallback" if val_hp.is_fallback else "primary"
            try:
                _mc = await session.execute(
                    sa.select(
                        MarketValuationSnapshot.symbol,
                        MarketValuationSnapshot.market_cap,
                    ).where(
                        MarketValuationSnapshot.market == "kr",
                        MarketValuationSnapshot.snapshot_date == val_hp.partition_date,
                        MarketValuationSnapshot.symbol.in_(
                            [snap.symbol for snap in candidate_snaps]
                        ),
                    )
                )
                market_cap_map = {
                    r.symbol: float(r.market_cap)
                    for r in _mc.all()
                    if r.market_cap is not None
                }
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "investor_flow: market_cap lookup failed: %s", exc, exc_info=True
                )
```

> **Implementer note:** `resolve_healthy_partition` is imported function-locally at line 603-606. Add `MarketValuationSnapshot` to that local import group or import as shown above; do not add a module-level import if it risks a cycle (this module is imported by `double_buy_screener`).

In the per-row dict (lines 693-708), add after `"_screener_snapshot_state": state,`:

```python
                "_screener_snapshot_state": state,
                "market_cap": market_cap_map.get(snap.symbol),
                "_market_cap_source": (
                    market_cap_source if snap.symbol in market_cap_map else None
                ),
```

Replace the `return _SnapshotLoadResult(...)` (lines 718-722) with:

```python
    reason, coverage_label = _partition_degradation(hp, rows_empty=not rows)
    return _SnapshotLoadResult(
        rows=rows,
        partition_date=latest_snapshot_date,
        partition_computed_at=partition_collected_at,  # investor_flow has no computed_at; use collected_at
        degradation_reason=reason,
        coverage_label=coverage_label,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_invest_view_model_screener_service.py -k "investor_flow" -v`
Expected: PASS.

- [ ] **Step 5: Lint + commit**

```bash
git add app/services/invest_view_model/screener_service.py tests/test_invest_view_model_screener_service.py
git commit -m "feat(ROB-426): PR3 investor_flow reason/coverage + KR market_cap join

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: `double_buy` loader — convert to `_SnapshotLoadResult` + reason/coverage + market_cap

**Files:**
- Modify: `app/services/invest_view_model/double_buy_screener.py` (whole loader)
- Modify: `app/services/invest_view_model/screener_service.py:1542-1554` (caller)
- Test: `tests/test_invest_view_model_double_buy_screener.py`

`load_double_buy_from_snapshots` currently returns `list | None`. Convert it to return `_SnapshotLoadResult | None` so it carries `partition_date` (even when empty) + reason/coverage like the other two loaders. It already computes `flow_hp`/`price_hp` (lines 47-69). Use the **worst** of the two for the reason (priority: missing > coverage_below_floor > older_fallback > healthy_no_matches).

- [ ] **Step 1: Write the failing test**

Check the existing `tests/test_invest_view_model_double_buy_screener.py` for how it asserts on the return value (today it indexes the returned list). Update those existing assertions to read `.rows` and add:

```python
@pytest.mark.asyncio
async def test_double_buy_returns_snapshot_load_result_with_reason(...):
    # Drive flow_hp healthy/non-fallback + price_hp healthy/non-fallback, but
    # candidate query yields 0 qualifiers -> healthy_no_matches.
    result = await load_double_buy_from_snapshots(session, market="kr", limit=10)
    assert result is not None
    assert result.rows == []
    assert result.partition_date is not None          # was unavailable before
    assert result.degradation_reason == "healthy_no_matches"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_invest_view_model_double_buy_screener.py -v`
Expected: FAIL — return is a `list`, has no `.rows`/`.degradation_reason`.

- [ ] **Step 3: Convert the loader return type**

In `app/services/invest_view_model/double_buy_screener.py`:

Add the import at the top (after line 24):

```python
from app.models.kr_symbol_universe import KRSymbolUniverse
from app.models.market_valuation_snapshot import MarketValuationSnapshot
```

Change the signature + docstring (lines 29-37) return type to `_SnapshotLoadResult | None`. Add the import of the dataclass + helper inside the function (the module-level import of `screener_service` would be circular — it already imports `_is_kr_toss_common_stock` function-locally at line 132). At the top of the function body, after the `if session is None or market != "kr": return None` guard, the existing function-local `partition_health` import stays. Add near the `_is_kr_toss_common_stock` import (line 130-134):

```python
    from app.services.invest_view_model.screener_service import (
        _SnapshotLoadResult,
        _is_kr_toss_common_stock,
        _partition_degradation,
    )
```

Add the market_cap lookup after the `name_map` block (after line 128), guarded on `symbols`:

```python
    market_cap_map: dict[str, float] = {}
    market_cap_source: str | None = None
    if symbols:
        from app.services.invest_screener_snapshots.partition_health import (
            resolve_healthy_partition as _resolve_val_hp,
        )

        val_hp = await _resolve_val_hp(
            session,
            model=MarketValuationSnapshot,
            date_col=MarketValuationSnapshot.snapshot_date,
            market_col=MarketValuationSnapshot.market,
            market="kr",
            universe_count=universe_count,
        )
        if val_hp is not None:
            market_cap_source = "fallback" if val_hp.is_fallback else "primary"
            try:
                _mc = await session.execute(
                    sa.select(
                        MarketValuationSnapshot.symbol,
                        MarketValuationSnapshot.market_cap,
                    ).where(
                        MarketValuationSnapshot.market == "kr",
                        MarketValuationSnapshot.snapshot_date == val_hp.partition_date,
                        MarketValuationSnapshot.symbol.in_(symbols),
                    )
                )
                market_cap_map = {
                    r.symbol: float(r.market_cap)
                    for r in _mc.all()
                    if r.market_cap is not None
                }
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "double_buy: market_cap lookup failed: %s", exc, exc_info=True
                )
```

In the per-row dict append (lines 160-187), add after `"_screener_snapshot_state": state,`:

```python
                "_screener_snapshot_state": state,
                "market_cap": market_cap_map.get(sym),
                "_market_cap_source": (
                    market_cap_source if sym in market_cap_map else None
                ),
```

Replace the final `return rows` (line 190) with a `_SnapshotLoadResult` carrying the worst-partition reason:

```python
    # ROB-426 PR3: reason from the worst of the two partitions. Priority order
    # snapshot_missing > coverage_below_floor > older_fallback > healthy_no_matches.
    _priority = {
        "snapshot_missing": 0,
        "coverage_below_floor": 1,
        "older_fallback": 2,
        "healthy_no_matches": 3,
        None: 4,
    }
    flow_reason, flow_cov = _partition_degradation(flow_hp, rows_empty=not rows)
    price_reason, price_cov = _partition_degradation(price_hp, rows_empty=not rows)
    if _priority[flow_reason] <= _priority[price_reason]:
        reason, coverage_label = flow_reason, flow_cov
    else:
        reason, coverage_label = price_reason, price_cov
    return _SnapshotLoadResult(
        rows=rows,
        partition_date=price_date,
        partition_computed_at=None,
        degradation_reason=reason,
        coverage_label=coverage_label,
    )
```

> Note: `flow_date`/`price_date` are guaranteed non-None here (the `if flow_date is None or price_date is None: return None` guard at lines 65-66 already returned). `flow_hp`/`price_hp` are non-None at this point for the same reason.

- [ ] **Step 4: Update the caller**

In `app/services/invest_view_model/screener_service.py`, replace the `double_buy` dispatch block (lines 1542-1554) with:

```python
        elif preset_id == "double_buy":
            from app.services.invest_view_model.double_buy_screener import (
                load_double_buy_from_snapshots,
            )

            _snapshot_load_result = await load_double_buy_from_snapshots(
                session,
                market=requested_market,
                limit=int(filters.get("limit") or _SNAPSHOT_FIRST_LIMIT),
            )
            if _snapshot_load_result is not None:
                _snapshot_check_result = _snapshot_load_result.rows
            _snapshot_empty_warning = (
                "최신 수급/시세 스냅샷에서 쌍끌이 매수 조건에 맞는 종목이 없습니다."
            )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_invest_view_model_double_buy_screener.py tests/test_screener_service.py -k "double_buy" -v`
Expected: PASS (including the updated `.rows` assertions).

- [ ] **Step 6: Lint + commit**

```bash
git add app/services/invest_view_model/double_buy_screener.py app/services/invest_view_model/screener_service.py tests/test_invest_view_model_double_buy_screener.py
git commit -m "feat(ROB-426): PR3 double_buy -> _SnapshotLoadResult + reason/coverage + market_cap

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 6: Thread reason/coverage through `_build_freshness`

**Files:**
- Modify: `app/services/invest_view_model/screener_service.py:1309-1429` (`_build_freshness`)
- Test: `tests/test_invest_view_model_screener_service.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_invest_view_model_screener_service.py`:

```python
def test_build_freshness_carries_degradation_reason_and_coverage():
    from app.services.invest_view_model.screener_service import _build_freshness

    fr = _build_freshness(
        raw_timestamp=None,
        cache_hit=True,
        market="kr",
        now=lambda: dt.datetime(2026, 6, 3, 6, tzinfo=dt.UTC),
        dataState="stale",
        primary_kind="screener_snapshot",
        primary_snapshot_date=dt.date(2026, 5, 22),
        primary_source="invest_screener_snapshots",
        primary_degradation_reason="coverage_below_floor",
        primary_coverage_label="20 / 3,800 (0.5%)",
    )
    assert fr.primary is not None
    assert fr.primary.degradationReason == "coverage_below_floor"
    assert fr.primary.coverageLabel == "20 / 3,800 (0.5%)"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_invest_view_model_screener_service.py::test_build_freshness_carries_degradation_reason_and_coverage -v`
Expected: FAIL — `_build_freshness` got unexpected keyword argument `primary_degradation_reason`.

- [ ] **Step 3: Add the params + pass to both `ScreenerFreshnessPrimary` constructions**

In `_build_freshness`, add to the keyword-only signature (after line 1321 `dependency_specs`):

```python
    dependency_specs: list[dict[str, Any]] | None = None,
    # ROB-426 PR3
    primary_degradation_reason: str | None = None,
    primary_coverage_label: str | None = None,
```

In the snapshot-kind `ScreenerFreshnessPrimary(...)` (lines 1411-1420), add the two fields:

```python
        primary = ScreenerFreshnessPrimary(
            kind="screener_snapshot",
            snapshotDate=primary_snapshot_date.isoformat(),
            computedAt=primary_computed_at.astimezone(UTC).isoformat()
            if primary_computed_at is not None
            else None,
            asOfLabel=as_of_label,
            dataState=dataState,  # type: ignore[arg-type]
            source=primary_source,
            degradationReason=primary_degradation_reason,  # type: ignore[arg-type]
            coverageLabel=primary_coverage_label,
        )
```

In the live/fallback `ScreenerFreshnessPrimary(...)` (lines 1422-1429), add the same two fields:

```python
        primary = ScreenerFreshnessPrimary(
            kind=primary_kind,
            snapshotDate=None,
            computedAt=None,
            asOfLabel=data_basis_kst.strftime("%Y.%m.%d %H:%M 기준"),
            dataState=dataState,  # type: ignore[arg-type]
            source=primary_source,
            degradationReason=primary_degradation_reason,  # type: ignore[arg-type]
            coverageLabel=primary_coverage_label,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_invest_view_model_screener_service.py::test_build_freshness_carries_degradation_reason_and_coverage -v`
Expected: PASS.

- [ ] **Step 5: Lint + commit**

```bash
git add app/services/invest_view_model/screener_service.py tests/test_invest_view_model_screener_service.py
git commit -m "feat(ROB-426): PR3 thread reason/coverage through _build_freshness

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 7: `build_screener_results` — compute reason/coverage, fix healthy_no_matches, wire marketCapSource

**Files:**
- Modify: `app/services/invest_view_model/screener_service.py:1701-1711` (aggregation/mislabel)
- Modify: `app/services/invest_view_model/screener_service.py:1733-1764` (primary_* computation)
- Modify: `app/services/invest_view_model/screener_service.py:1839-1850` (`_build_freshness` call)
- Modify: `app/services/invest_view_model/screener_service.py:1883-1924` (`ScreenerResultRow` construction)
- Test: `tests/test_screener_service.py` or `tests/test_invest_view_model_screener_service.py`

- [ ] **Step 1: Write the failing tests**

Add two tests (use the module's existing `build_screener_results` harness — `tests/test_screener_service.py` has fake screening-service + resolver fixtures; follow them):

```python
@pytest.mark.asyncio
async def test_healthy_no_matches_is_not_stale(...):
    """A healthy newest partition with 0 qualifiers must NOT be labeled stale;
    freshness.primary.degradationReason must be 'healthy_no_matches'."""
    # Arrange a consecutive_gainers run where the loader returns a
    # _SnapshotLoadResult(rows=[], partition_date=<today>, degradation_reason=
    # "healthy_no_matches"). Then:
    resp = await build_screener_results("consecutive_gainers", fake_svc, resolver,
                                        market="kr", now=..., session=session)
    assert resp.results == []
    assert resp.freshness.primary.degradationReason == "healthy_no_matches"
    assert resp.freshness.primary.dataState != "stale"


@pytest.mark.asyncio
async def test_market_cap_source_surfaced_on_row(...):
    """A consecutive_gainers row whose market_cap came from the valuation join
    carries marketCapSource."""
    # Arrange one qualifying row with row["_market_cap_source"]="primary". Then:
    resp = await build_screener_results("consecutive_gainers", fake_svc, resolver,
                                        market="kr", now=..., session=session)
    assert resp.results[0].marketCapSource == "primary"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_screener_service.py -k "healthy_no_matches or market_cap_source" -v`
Expected: FAIL (dataState is `"stale"`; `marketCapSource` is `None`).

- [ ] **Step 3a: Fix the healthy_no_matches mislabel**

Replace lines 1701-1711 (the `if _snapshot_was_checked and not rows:` block) with:

```python
    if _snapshot_was_checked and not rows:
        # Latest snapshot partition was found but had no qualifying rows.
        _load_reason = (
            _snapshot_load_result.degradation_reason
            if _snapshot_load_result is not None
            else None
        )
        if _load_reason == "healthy_no_matches":
            # ROB-426 PR3: the served partition is the newest *healthy* one — zero
            # qualifiers is a filter outcome, not staleness. Reflect the
            # partition's date-based freshness instead of flooring to "stale".
            from app.services.invest_screener_snapshots.freshness import (
                expected_baseline_date,
            )

            _baseline = expected_baseline_date(requested_market, now=now())
            _pd = _snapshot_load_result.partition_date
            _aggregated_data_state = "fresh" if _pd == _baseline else "stale"
        else:
            # missing / degraded / crypto override paths keep prior behavior.
            _aggregated_data_state = _snapshot_state_override or "stale"
    else:
        _row_states: list[str] = [
            str(r.get("_screener_snapshot_state") or "missing") for r in rows
        ]
        _aggregated_data_state = aggregate_states(_row_states)  # type: ignore[arg-type]
```

- [ ] **Step 3b: Compute primary_degradation_reason + coverage_label**

Find the end of the `primary_*` computation block (after line 1763 `primary_source = "screening_service"`). Insert immediately after that whole `if _snapshot_was_checked: ... else: ...` block (i.e., after line 1763):

```python
    # ROB-426 PR3: structured degradation reason for freshness.primary.
    primary_degradation_reason: str | None = None
    primary_coverage_label: str | None = None
    if _snapshot_load_result is not None and _snapshot_load_result.degradation_reason:
        primary_degradation_reason = _snapshot_load_result.degradation_reason
        primary_coverage_label = _snapshot_load_result.coverage_label
    elif _snapshot_state_override == "missing":
        primary_degradation_reason = "snapshot_missing"
    elif primary_kind == "live":
        primary_degradation_reason = "live"
```

- [ ] **Step 3c: Pass them into `_build_freshness`**

In the `_build_freshness(...)` call (lines 1839-1850), add the two kwargs after `dependency_specs=dependency_specs,`:

```python
        dependency_specs=dependency_specs,
        primary_degradation_reason=primary_degradation_reason,
        primary_coverage_label=primary_coverage_label,
    )
```

- [ ] **Step 3d: Wire marketCapSource onto the row**

In the `ScreenerResultRow(...)` construction (lines 1883-1924), add after `candidateContext=...` (line 1921-1923):

```python
                candidateContext=_crypto_candidate_context(row, preset_id)
                if market == "crypto"
                else None,
                marketCapSource=row.get("_market_cap_source"),
            )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_screener_service.py tests/test_invest_view_model_screener_service.py -v`
Expected: PASS.

- [ ] **Step 5: Run the full screener backend suite (regression)**

Run: `uv run pytest tests/test_screener_service.py tests/test_invest_view_model_screener_service.py tests/test_invest_view_model_double_buy_screener.py tests/test_screener_us_missing_warning.py tests/test_invest_api_screener_router.py tests/test_invest_screener_schemas.py -v`
Expected: PASS (no regression to existing freshness/US-warning/router assertions).

- [ ] **Step 6: Lint + commit**

```bash
git add app/services/invest_view_model/screener_service.py tests/
git commit -m "feat(ROB-426): PR3 wire reason/coverage + heal healthy_no_matches mislabel + marketCapSource

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 8: Frontend — `ScreenerEmptyState` component

**Files:**
- Create: `frontend/invest/src/desktop/screener/ScreenerEmptyState.tsx`
- Test: `frontend/invest/src/__tests__/ScreenerEmptyState.test.tsx`

- [ ] **Step 1: Write the failing test**

Create `frontend/invest/src/__tests__/ScreenerEmptyState.test.tsx`:

```tsx
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ScreenerEmptyState } from "../desktop/screener/ScreenerEmptyState";

describe("ScreenerEmptyState", () => {
  it("renders neutral copy for healthy_no_matches (not a warning)", () => {
    render(<ScreenerEmptyState reason="healthy_no_matches" coverageLabel={null} />);
    expect(screen.getByText(/조건에 맞는 종목이 없습니다/)).toBeInTheDocument();
  });

  it("renders the coverage label for coverage_below_floor", () => {
    render(
      <ScreenerEmptyState reason="coverage_below_floor" coverageLabel="20 / 3,800 (0.5%)" />,
    );
    expect(screen.getByText(/20 \/ 3,800 \(0\.5%\)/)).toBeInTheDocument();
  });

  it("renders snapshot_missing copy", () => {
    render(<ScreenerEmptyState reason="snapshot_missing" coverageLabel={null} />);
    expect(screen.getByText(/스냅샷.*준비/)).toBeInTheDocument();
  });

  it("falls back to a generic message when reason is null", () => {
    render(<ScreenerEmptyState reason={null} coverageLabel={null} />);
    expect(screen.getByText("표시할 종목이 없습니다.")).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend/invest && npx vitest run src/__tests__/ScreenerEmptyState.test.tsx`
Expected: FAIL — module `ScreenerEmptyState` not found.

- [ ] **Step 3: Create the component**

Create `frontend/invest/src/desktop/screener/ScreenerEmptyState.tsx`:

```tsx
import type { ScreenerDegradationReason } from "../../types/screener";

interface ScreenerEmptyStateProps {
  reason: ScreenerDegradationReason | null | undefined;
  coverageLabel: string | null | undefined;
}

const COPY: Record<ScreenerDegradationReason, { title: string; tone: "neutral" | "degraded" }> = {
  healthy_no_matches: { title: "조건에 맞는 종목이 없습니다.", tone: "neutral" },
  coverage_below_floor: { title: "오늘 스냅샷 커버리지가 얇어 일부만 표시됩니다.", tone: "degraded" },
  older_fallback: { title: "최신 스냅샷이 얇아 직전 영업일 스냅샷 기준으로 표시합니다.", tone: "degraded" },
  snapshot_missing: { title: "스크리너 스냅샷이 준비 중입니다.", tone: "degraded" },
  live: { title: "실시간 결과입니다 (스냅샷 아님).", tone: "neutral" },
};

export function ScreenerEmptyState({ reason, coverageLabel }: ScreenerEmptyStateProps) {
  const entry = reason ? COPY[reason] : null;
  if (!entry) {
    return <div className="screener-empty">표시할 종목이 없습니다.</div>;
  }
  return (
    <div className={`screener-empty screener-empty--${entry.tone}`} role="status">
      <p className="screener-empty__title">{entry.title}</p>
      {reason === "coverage_below_floor" && coverageLabel ? (
        <p className="screener-empty__coverage">스냅샷 적재: {coverageLabel}</p>
      ) : null}
    </div>
  );
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd frontend/invest && npx vitest run src/__tests__/ScreenerEmptyState.test.tsx`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add frontend/invest/src/desktop/screener/ScreenerEmptyState.tsx frontend/invest/src/__tests__/ScreenerEmptyState.test.tsx
git commit -m "feat(ROB-426): PR3 ScreenerEmptyState reason-switched component

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 9: Frontend — wire `ScreenerEmptyState` into the table + marketCapSource badge

**Files:**
- Modify: `frontend/invest/src/desktop/screener/ScreenerResultsTable.tsx:17-18` (empty state), `:92` (market_cap cell)
- Modify: `frontend/invest/src/pages/desktop/DesktopScreenerPage.tsx` (pass freshness.primary into the table or render the empty-state with it)
- Test: `frontend/invest/src/__tests__/DesktopScreenerPage.test.tsx`

- [ ] **Step 1: Read the current table + page wiring**

Read `frontend/invest/src/desktop/screener/ScreenerResultsTable.tsx` (full) and `frontend/invest/src/pages/desktop/DesktopScreenerPage.tsx:110-135` to confirm the prop names (`rows`, `freshness`) the table receives. The empty branch is at `ScreenerResultsTable.tsx:17-18`; the market_cap cell renders `r.marketCapLabel` at line 92.

- [ ] **Step 2: Write the failing test**

Add to `frontend/invest/src/__tests__/DesktopScreenerPage.test.tsx` a case that mounts the page with a results response whose `results: []` and `freshness.primary.degradationReason: "coverage_below_floor"`, `coverageLabel: "20 / 3,800 (0.5%)"`, and asserts the coverage text renders instead of the bare `"표시할 종목이 없습니다."`. Mirror the existing mock-response shape already used in this test file.

```tsx
it("renders the coverage degraded empty-state when the partition is thin", async () => {
  // ...mock fetchScreenerResults to resolve results: [] with
  // freshness.primary.degradationReason = "coverage_below_floor",
  // coverageLabel = "20 / 3,800 (0.5%)" (follow this file's existing mock helper)...
  render(<DesktopScreenerPage /* existing props */ />);
  expect(await screen.findByText(/20 \/ 3,800 \(0\.5%\)/)).toBeInTheDocument();
});
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd frontend/invest && npx vitest run src/__tests__/DesktopScreenerPage.test.tsx`
Expected: FAIL — coverage text not rendered (still static string).

- [ ] **Step 4: Wire the component + badge**

In `ScreenerResultsTable.tsx`: import `ScreenerEmptyState`, and accept the freshness primary (add to the component's props type a `degradationReason?: ScreenerDegradationReason | null` and `coverageLabel?: string | null`, OR a `freshness?: ScreenerFreshness` prop — match what the table already receives). Replace the empty branch (line 17-18):

```tsx
  if (rows.length === 0) {
    return (
      <ScreenerEmptyState
        reason={freshness?.primary?.degradationReason ?? null}
        coverageLabel={freshness?.primary?.coverageLabel ?? null}
      />
    );
  }
```

For the market_cap cell (line 92), render a quiet badge only on `fallback`:

```tsx
<td className="screener-cell screener-cell--market-cap">
  {r.marketCapLabel}
  {r.marketCapSource === "fallback" ? (
    <span className="screener-cell__cap-badge" title="직전 영업일 밸류에이션 스냅샷 기준">참고</span>
  ) : null}
</td>
```

In `DesktopScreenerPage.tsx`, ensure the `freshness` object is passed to `<ScreenerResultsTable .../>` (it likely already passes `freshness` to `ScreenerFreshnessLine`; add the same prop to the table if not already present).

- [ ] **Step 5: Run test to verify it passes**

Run: `cd frontend/invest && npx vitest run src/__tests__/DesktopScreenerPage.test.tsx`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add frontend/invest/src/desktop/screener/ScreenerResultsTable.tsx frontend/invest/src/pages/desktop/DesktopScreenerPage.tsx frontend/invest/src/__tests__/DesktopScreenerPage.test.tsx
git commit -m "feat(ROB-426): PR3 wire ScreenerEmptyState + marketCapSource badge into results table

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 10: Frontend — coverage label + reason in `ScreenerFreshnessLine`

**Files:**
- Modify: `frontend/invest/src/desktop/screener/ScreenerFreshnessLine.tsx`
- Test: `frontend/invest/src/__tests__/ScreenerFreshnessLine.test.tsx`

- [ ] **Step 1: Read the current component**

Read `frontend/invest/src/desktop/screener/ScreenerFreshnessLine.tsx` (full) to confirm where the data-basis line is rendered (synthesis: dual-line at lines 44-95; reads `freshness.primary`, `overallState`/`dataState`, `asOfLabel`). The chip class is keyed on `overall` (line 89).

- [ ] **Step 2: Write the failing test**

Add to `frontend/invest/src/__tests__/ScreenerFreshnessLine.test.tsx`:

```tsx
it("renders coverageLabel when the primary partition is coverage_below_floor", () => {
  render(
    <ScreenerFreshnessLine
      freshness={{
        fetchedAt: "2026-06-03T06:00:00Z",
        asOfLabel: "2026.05.22 15:30 기준",
        relativeLabel: "방금",
        cacheHit: true,
        source: "cached",
        dataState: "stale",
        primary: {
          kind: "screener_snapshot",
          snapshotDate: "2026-05-22",
          computedAt: null,
          asOfLabel: "2026.05.22 15:30 기준",
          dataState: "stale",
          source: "invest_screener_snapshots",
          degradationReason: "coverage_below_floor",
          coverageLabel: "20 / 3,800 (0.5%)",
        },
        dependencies: [],
        overallState: "stale",
      }}
    />,
  );
  expect(screen.getByText(/20 \/ 3,800 \(0\.5%\)/)).toBeInTheDocument();
});
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd frontend/invest && npx vitest run src/__tests__/ScreenerFreshnessLine.test.tsx`
Expected: FAIL — coverage label not rendered.

- [ ] **Step 4: Render the coverage label**

In `ScreenerFreshnessLine.tsx`, on the data-basis line (after the existing `asOfLabel`/lag rendering), add a coverage chip when present:

```tsx
{freshness.primary?.degradationReason === "coverage_below_floor" &&
freshness.primary?.coverageLabel ? (
  <span className="screener-freshness__coverage">
    스냅샷 적재 {freshness.primary.coverageLabel}
  </span>
) : null}
```

(Place it inside the existing data-line container so it sits beside the as-of/lag text; do not change the chip-class logic.)

- [ ] **Step 5: Run test to verify it passes**

Run: `cd frontend/invest && npx vitest run src/__tests__/ScreenerFreshnessLine.test.tsx`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add frontend/invest/src/desktop/screener/ScreenerFreshnessLine.tsx frontend/invest/src/__tests__/ScreenerFreshnessLine.test.tsx
git commit -m "feat(ROB-426): PR3 surface coverageLabel on the screener freshness line

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 11: Frontend — styles for empty-state / coverage / badge

**Files:**
- Modify: `frontend/invest/src/desktop/screener/screener.css`

- [ ] **Step 1: Read the existing relevant CSS**

Read `frontend/invest/src/desktop/screener/screener.css` and find the existing `.screener-empty` and freshness-chip rules to match naming/colors.

- [ ] **Step 2: Add styles (no test — visual only)**

Append to `screener.css`:

```css
.screener-empty { padding: 32px 16px; text-align: center; color: var(--text-secondary, #6b7280); }
.screener-empty--degraded .screener-empty__title { color: var(--warn-text, #92400e); }
.screener-empty--neutral .screener-empty__title { color: var(--text-secondary, #6b7280); }
.screener-empty__coverage { margin-top: 6px; font-size: 12px; color: var(--text-tertiary, #9ca3af); }
.screener-freshness__coverage { margin-left: 8px; font-size: 12px; color: var(--text-tertiary, #9ca3af); }
.screener-cell__cap-badge {
  margin-left: 6px; padding: 0 4px; font-size: 10px; border-radius: 4px;
  background: var(--surface-subtle, #f3f4f6); color: var(--text-tertiary, #9ca3af);
}
```

(Match the actual CSS-variable names already in `screener.css`; the fallbacks above are defensive.)

- [ ] **Step 3: Verify the frontend builds**

Run: `cd frontend/invest && npx tsc --noEmit && npx vitest run src/__tests__/ScreenerEmptyState.test.tsx src/__tests__/ScreenerFreshnessLine.test.tsx src/__tests__/DesktopScreenerPage.test.tsx`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add frontend/invest/src/desktop/screener/screener.css
git commit -m "feat(ROB-426): PR3 screener degraded-state / coverage / market-cap-badge styles

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 12: Full-suite regression + final verification

**Files:** none (verification only)

- [ ] **Step 1: Backend — screener + schema + router suites**

Run:
```bash
uv run pytest \
  tests/test_screener_service.py \
  tests/test_invest_view_model_screener_service.py \
  tests/test_invest_view_model_double_buy_screener.py \
  tests/test_undervalued_breakout_screener.py \
  tests/test_invest_view_model_high_yield_value_screener.py \
  tests/test_fundamentals_screener.py \
  tests/test_screener_us_missing_warning.py \
  tests/test_invest_api_screener_router.py \
  tests/test_invest_screener_schemas.py \
  tests/services/invest_screener_snapshots \
  -v
```
Expected: all PASS. (Value-preset suites confirm the documented scope boundary — they are unchanged.)

- [ ] **Step 2: Backend lint + format**

Run: `uv run ruff check app/ tests/ && uv run ruff format --check app/ tests/`
Expected: clean.

- [ ] **Step 3: Frontend — full screener test set + build**

Run: `cd frontend/invest && npx tsc --noEmit && npx vitest run src/__tests__/`
Expected: PASS.

- [ ] **Step 4: Confirm invariants**

Run:
```bash
git diff --stat origin/main...HEAD -- alembic/ | tail -1   # expect: no migration files
git diff origin/main...HEAD -- app/services/invest_screener_snapshots/partition_health.py app/services/invest_screener_snapshots/guards.py app/services/invest_screener_snapshots/freshness.py app/services/snapshot_commit_guard.py
```
Expected: empty diff for the four "unchanged" files; no `alembic/versions/*` additions.

- [ ] **Step 5: Push the branch (PR creation is operator-gated — only on request)**

Run: `git push -u origin rob-426-pr3`
(Do NOT open the PR unless the user asks.)

---

## Self-Review (completed during authoring)

**1. Spec coverage:**
- Unit A (contract degradationReason/coverageLabel/marketCapSource) → Task 1. ✓
- Unit B (`_SnapshotLoadResult` fields + helper + `_build_freshness` threading + central reason + healthy_no_matches fix) → Tasks 2, 6, 7. ✓
- Unit C (market_cap join on consecutive_gainers/investor_flow/double_buy) → Tasks 3, 4, 5 + row wiring in Task 7. ✓
- Unit D (frontend ScreenerEmptyState + coverageLabel + fallback badge + CSS) → Tasks 8, 9, 10, 11. ✓
- Unit E (tests) → woven TDD per task + Task 12 regression. ✓
- 4 empty-reasons table → encoded in `_partition_degradation` (Task 2) + reason taxonomy section. ✓

**2. Placeholder scan:** Frontend wiring steps (Tasks 9/10/11) intentionally instruct reading the current component to match existing prop/variable names before editing, because the exact prop signatures of `ScreenerResultsTable`/`ScreenerFreshnessLine` and the CSS-variable names were not captured verbatim in this plan — these are "read then match" steps, not unfilled placeholders, and each still gives the concrete code to add.

**3. Type consistency:** `degradation_reason`/`coverage_label` (snake, backend `_SnapshotLoadResult`) ↔ `degradationReason`/`coverageLabel` (camel, Pydantic/TS) used consistently. `_market_cap_source` (row dict key, snake) → `marketCapSource` (Pydantic/TS). Helper `_partition_degradation(hp, *, rows_empty)` signature identical across Tasks 2-5. `_build_freshness` new kwargs `primary_degradation_reason`/`primary_coverage_label` identical in Tasks 6 and 7. Reason enum values identical across Pydantic, TS, helper, and component COPY map.

**Scope boundary disclosed:** value presets (`high_yield_value`/`undervalued_breakout`/fundamentals) and crypto retain current degraded labeling (no fine-grained reason); documented at top and verified in Task 12 Step 1.

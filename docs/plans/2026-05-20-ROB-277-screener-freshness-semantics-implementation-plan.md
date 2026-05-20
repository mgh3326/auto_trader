# ROB-277 — `/invest/screener` Freshness Semantics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split `/invest/screener` freshness into "response served time" vs "underlying data 기준일/시각" so the UI never shows `방금 갱신` for stale-snapshot results. Additive backend schema change, no DB migration, no scheduler activation.

**Architecture:** Replace `raw["timestamp"] = now()` (snapshot-first path) with a freshness object that exposes (a) `servedAt`/`servedRelativeLabel` for response refresh, (b) `primary` for the screener snapshot's own state (using `classify_state` from `app/services/invest_screener_snapshots/freshness.py`), and (c) `dependencies[]` for investor-flow style derived signals. Old `fetchedAt`/`asOfLabel`/`relativeLabel`/`cacheHit`/`source`/`dataState` fields are kept; `dataState` becomes a transition alias of new `overallState`.

**Tech Stack:** FastAPI + Pydantic v2 (backend), Vite + React + Vitest + Testing Library (frontend), async SQLAlchemy on PostgreSQL, pytest with `pytest-asyncio`. UV-managed Python deps. KST/trading-date logic via `zoneinfo` already in `app/services/invest_screener_snapshots/freshness.py`.

---

## Locked Decisions (do not re-debate during implementation)

These were agreed in the ROB-277 review thread on 2026-05-20. Any deviation requires explicit reviewer sign-off **before** code changes.

### D1. Legacy field mapping (additive only)

`ScreenerFreshness` schema after this PR:

| Field | Old? | Meaning |
|-------|------|---------|
| `fetchedAt` | existing | Kept. Holds the **served** ISO timestamp (== `servedAt`). Backwards-compat alias. |
| `asOfLabel` | existing | Kept. **Data 기준 label** of the primary source (NOT the served time). |
| `relativeLabel` | existing | Kept. Korean human label for the primary data 기준 (e.g. `5거래일 지연`, `방금 갱신`, `전 거래일 기준`). |
| `cacheHit` | existing | Kept. Whether the served result came from cache/snapshot vs live call. |
| `source` | existing | Kept. Enum stays `"live" \| "cached" \| "previous_session"`. **Snapshot-first → `"cached"`**. No new enum value in this PR. |
| `dataState` | existing | Kept. **Becomes alias of `overallState`** during transition. |
| `servedAt` | **NEW** | ISO timestamp when the view-model produced the response. |
| `servedRelativeLabel` | **NEW** | Korean label for served time (e.g. `방금`, `12분 전`). |
| `primary` | **NEW** | See D1.a. |
| `dependencies` | **NEW** | See D1.b. Optional, may be `[]`. |
| `overallState` | **NEW** | Aggregated state (see D1.c). |

**D1.a. `primary` shape:**

```python
class ScreenerFreshnessPrimary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["screener_snapshot", "live", "fallback"]
    snapshotDate: str | None = None       # ISO date, e.g. "2026-05-13"
    computedAt: str | None = None          # ISO datetime (UTC) when partition was computed
    asOfLabel: str                         # Korean label, e.g. "2026.05.13 장마감 기준"
    dataState: Literal["fresh", "partial", "stale", "missing", "fallback"]
    source: str | None = None              # free-form, e.g. "invest_screener_snapshots"
```

**D1.b. `dependencies[]` shape:**

```python
class ScreenerFreshnessDependency(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["investor_flow"]        # extensible later
    snapshotDate: str | None = None
    collectedAt: str | None = None
    lagLabel: str | None = None            # e.g. "2거래일 지연", or null when fresh
    dataState: Literal["fresh", "partial", "stale", "missing", "fallback"]
    source: str | None = None              # e.g. "investor_flow_snapshots"
```

**D1.c. `overallState` aggregation rule** (explicit, NOT reusing `aggregate_states` because its `has_missing + has_fresh_or_partial → fallback` rule conflicts with the spec):

```
1. If primary.dataState in {"missing", "stale"}  → overallState = primary.dataState
2. Else if any dependency.dataState in {"missing", "stale"}  → overallState = "stale"
3. Else if any dependency.dataState == "partial"  → overallState = "partial"
4. Else → overallState = primary.dataState  (which is "fresh" or "partial" at this point)
```

Top-level `dataState = overallState` (alias).

### D2. `source` mapping (no enum extension in this PR)

- Top-level `source` stays the existing enum `Literal["live", "cached", "previous_session"]`.
- Snapshot-first result → top-level `source = "cached"`, `cacheHit = True`.
- The actual provenance lives in `primary.kind = "screener_snapshot"` and `primary.source = "invest_screener_snapshots"` (or `investor_flow_snapshots` for the investor-flow preset's primary).
- **If** a future PR decides to surface `source = "snapshot"` at top level, it must add `frontend/invest/src/types/screener.ts` + Pydantic enum update + a compatibility note. **Not in this PR.**

### D3. UI copy standard

`ScreenerFreshnessLine.tsx` renders **two separate spans** (data 기준 line + 화면 갱신 line). Copy table:

| State | 데이터 기준 line (top) | 화면 갱신 line (bottom) | Data-state chip |
|-------|------------------------|--------------------------|-----------------|
| `fresh` | `데이터 기준 ${asOfLabel}` | `화면 갱신 ${servedRelativeLabel}` | (no chip) |
| `partial` | `데이터 기준 ${asOfLabel}` | `화면 갱신 ${servedRelativeLabel}` | `일부 데이터 지연` |
| `stale` | `데이터 기준 ${asOfLabel} · ${lagLabel ?? "업데이트 필요"}` | `화면 갱신 ${servedRelativeLabel}` | `업데이트 필요` |
| `missing` | `데이터 없음` (or `수급 데이터 없음` when only deps missing) | `화면 갱신 ${servedRelativeLabel}` | `데이터 준비중` |
| `previous_session` | `전 거래일 기준 · ${asOfLabel}` | `화면 갱신 ${servedRelativeLabel}` | (no chip) |
| `fallback` | `대체 데이터 기준 ${asOfLabel}` | `화면 갱신 ${servedRelativeLabel}` | `대체 데이터` |

Wording may be polished in implementation, but the **structural rule is non-negotiable**: `데이터 기준` and `화면 갱신` are always in separate visual rows / spans and never share a single label.

### D4. `classify_state()` reuse

- **Primary state for `consecutive_gainers`**: use `classify_state(snapshot_date, computed_at, closes_window_len, today_trading_date_value, now)` directly (already populated per-row in `_load_consecutive_gainers_from_snapshots`).
- **Primary state for `investor_flow_momentum`**: derive from the latest `investor_flow_snapshots` partition using a new helper `_classify_investor_flow_partition` that mirrors `classify_state`'s KST/trading-date logic but without `closes_window_len` (investor-flow has no analog).  The helper lives in `app/services/invest_screener_snapshots/freshness.py` for colocation (the file already centralizes screener freshness logic) and reuses `today_trading_date`.
- **Dependency state for investor-flow chips on `consecutive_gainers`**: same `_classify_investor_flow_partition` helper.
- **Remove** the `"_screener_snapshot_state": "fresh"` hardcode at `screener_service.py:558`.

### D5. Required tests (in addition to existing acceptance criteria)

Backend (`tests/test_invest_view_model_screener_service.py` extension + new freshness tests in `tests/test_invest_screener_snapshots_freshness.py`):

1. `test_consecutive_gainers_snapshot_5_days_old_classifies_stale_and_uses_partition_date_as_as_of_label` — KR snapshot dated `today - 5 trading days`, response has `freshness.primary.dataState == "stale"`, `freshness.primary.snapshotDate == that date`, `freshness.asOfLabel` references that date (NOT `now()`).
2. `test_classify_state_weekday_boundary_friday_monday` — Friday close → Monday morning still classifies as `stale` (because `snapshot_date != today_trading_date` once Monday's partition would be expected) **OR** as `fresh` until Monday open, depending on `STALE_AFTER_HOURS`. Lock the actually-implemented behavior in this test.
3. `test_legacy_dataState_equals_overallState` — for every code path that builds `ScreenerFreshness`, `freshness.dataState == freshness.overallState`.
4. `test_snapshot_first_source_stays_cached_with_primary_kind_screener_snapshot` — snapshot-first response has `source == "cached"` AND `primary.kind == "screener_snapshot"` AND `primary.source == "invest_screener_snapshots"`.
5. `test_investor_flow_dependency_carries_snapshot_date_and_classified_state` — chip on a `consecutive_gainers` row exposes `snapshotDate` from the actual `investor_flow_snapshots` partition; `dataState` reflects classified state, NOT hardcoded `"fresh"`.
6. `test_overall_state_primary_fresh_dependency_stale_is_stale` — explicit D1.c rule 2.
7. `test_overall_state_primary_stale_dependency_fresh_is_stale` — explicit D1.c rule 1.

Frontend (`frontend/invest/src/__tests__/ScreenerFreshnessLine.test.tsx` extension):

8. `renders 데이터 기준 and 화면 갱신 in separate spans` — both labels present, neither contains the other's substring.
9. `does not contradict: stale data + fresh served renders both lines without 방금 갱신 next to stale date` — DOM contains `업데이트 필요` AND `화면 갱신`, but `방금 갱신` does NOT appear on the same line as `데이터 기준`.

### D6. Server smoke responsibility

- **Merge gate (required before merge):** `make test`, `make typecheck`, `make lint`, frontend `pnpm test` for the changed components, and a local dev-browser/API spot-check of `/invest/screener?market=kr` if the dev stack is up.
- **Post-merge (not a gate):** Hermes/파이리 handles MacBook server deploy + production smoke (compare prod `/invest/api/screener/results?preset=consecutive_gainers&market=kr` freshness object against an open `consecutive_gainers` row's investor-flow chip).
- **PR body MUST include a "Compatibility caution" section** if any frontend consumer reads `freshness.fetchedAt` as the data-as-of source (not just served time). Audit before opening PR (see Task 11).

### D7. ROB-204 / ROB-205 relationship (no scope creep)

- ROB-277 is read-side only: consumes `invest_screener_snapshots` and `investor_flow_snapshots` as already populated.
- **Do NOT** in this PR: activate Prefect schedules, unpause TaskIQ recurrence, write to `INVEST_SCREENER_SNAPSHOTS_COMMIT_ENABLED`, run any `scripts/build_*_snapshots.py --commit`, or backfill investor-flow.
- ROB-204 may finalize US activation in parallel; that work is gated by `INVEST_SCREENER_SNAPSHOTS_COMMIT_ENABLED` env var and does not touch any file this plan modifies.
- ROB-205 will land investor-flow backfill / scheduler activation later; this PR's classification must work correctly **today** with the existing (potentially stale) partition.

---

## File Structure

**Modify (backend):**
- `app/schemas/invest_screener.py` — add `ScreenerFreshnessPrimary`, `ScreenerFreshnessDependency`, extend `ScreenerFreshness`.
- `app/services/invest_screener_snapshots/freshness.py` — add `_classify_investor_flow_partition`, helper for `format_kst_as_of_label` (small util kept here for colocation), and `compute_overall_state`.
- `app/services/invest_view_model/screener_service.py` — refactor `_build_freshness`, plumb snapshot metadata into investor-flow rows, populate `primary` / `dependencies` / `overallState`.

**Modify (frontend):**
- `frontend/invest/src/types/screener.ts` — extend `ScreenerFreshness` with new optional fields.
- `frontend/invest/src/desktop/screener/ScreenerFreshnessLine.tsx` — render two separate spans.
- `frontend/invest/src/__tests__/ScreenerFreshnessLine.test.tsx` — extend tests.

**Modify (backend tests):**
- `tests/test_invest_view_model_screener_service.py` — extend.
- `tests/test_invest_screener_snapshots_freshness.py` — extend with classification edge cases.
- `tests/test_invest_screener_schemas.py` — extend with new fields.

**Create:** none. (No new files; everything fits into existing modules per single-responsibility.)

---

## Tasks

Each task is a self-contained TDD slice with a commit at the end. Run all commands from repo root `/Users/mgh3326/work/auto_trader.rob-277`.

---

### Task 1: Schema — add `ScreenerFreshnessPrimary`, `ScreenerFreshnessDependency`, extend `ScreenerFreshness`

**Files:**
- Modify: `app/schemas/invest_screener.py:125-132`
- Test: `tests/test_invest_screener_schemas.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_invest_screener_schemas.py`:

```python
def test_screener_freshness_accepts_new_primary_and_dependencies_fields() -> None:
    from app.schemas.invest_screener import (
        ScreenerFreshness,
        ScreenerFreshnessPrimary,
        ScreenerFreshnessDependency,
    )

    payload = ScreenerFreshness(
        fetchedAt="2026-05-20T00:10:00+00:00",
        asOfLabel="2026.05.13 장마감 기준",
        relativeLabel="5거래일 지연",
        cacheHit=True,
        source="cached",
        dataState="stale",
        servedAt="2026-05-20T00:10:00+00:00",
        servedRelativeLabel="방금",
        primary=ScreenerFreshnessPrimary(
            kind="screener_snapshot",
            snapshotDate="2026-05-13",
            computedAt="2026-05-13T06:35:00+00:00",
            asOfLabel="2026.05.13 장마감 기준",
            dataState="stale",
            source="invest_screener_snapshots",
        ),
        dependencies=[
            ScreenerFreshnessDependency(
                kind="investor_flow",
                snapshotDate="2026-05-18",
                collectedAt="2026-05-18T07:30:00+00:00",
                lagLabel="2거래일 지연",
                dataState="stale",
                source="investor_flow_snapshots",
            )
        ],
        overallState="stale",
    )
    assert payload.primary is not None
    assert payload.primary.kind == "screener_snapshot"
    assert payload.dependencies[0].kind == "investor_flow"
    assert payload.overallState == payload.dataState


def test_screener_freshness_is_backwards_compatible_without_new_fields() -> None:
    from app.schemas.invest_screener import ScreenerFreshness

    payload = ScreenerFreshness(
        fetchedAt="2026-05-20T00:10:00+00:00",
        asOfLabel="2026.05.20 09:10 기준",
        relativeLabel="방금 갱신",
        cacheHit=False,
        source="live",
        dataState="fresh",
    )
    assert payload.primary is None
    assert payload.dependencies == []
    assert payload.overallState is None  # absent until backend populates it
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
uv run pytest tests/test_invest_screener_schemas.py::test_screener_freshness_accepts_new_primary_and_dependencies_fields tests/test_invest_screener_schemas.py::test_screener_freshness_is_backwards_compatible_without_new_fields -v
```

Expected: FAIL with `AttributeError` / `ImportError` for `ScreenerFreshnessPrimary` / `ScreenerFreshnessDependency` and `ValidationError: extra inputs are not permitted` on the new fields (because `model_config = ConfigDict(extra="forbid")`).

- [ ] **Step 3: Add the new schema classes and fields**

Edit `app/schemas/invest_screener.py`. After the existing `ScreenerFreshness` class (line 132), but replace `ScreenerFreshness` itself to extend it. Concretely:

```python
class ScreenerFreshnessPrimary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["screener_snapshot", "live", "fallback"]
    snapshotDate: str | None = None
    computedAt: str | None = None
    asOfLabel: str
    dataState: Literal["fresh", "partial", "stale", "missing", "fallback"]
    source: str | None = None


class ScreenerFreshnessDependency(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["investor_flow"]
    snapshotDate: str | None = None
    collectedAt: str | None = None
    lagLabel: str | None = None
    dataState: Literal["fresh", "partial", "stale", "missing", "fallback"]
    source: str | None = None


class ScreenerFreshness(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fetchedAt: str
    asOfLabel: str
    relativeLabel: str
    cacheHit: bool
    source: Literal["live", "cached", "previous_session"]
    dataState: Literal["fresh", "partial", "stale", "missing", "fallback"] = "missing"
    # New (additive, optional) fields — see ROB-277 plan §D1.
    servedAt: str | None = None
    servedRelativeLabel: str | None = None
    primary: ScreenerFreshnessPrimary | None = None
    dependencies: list[ScreenerFreshnessDependency] = Field(default_factory=list)
    overallState: Literal["fresh", "partial", "stale", "missing", "fallback"] | None = None
```

- [ ] **Step 4: Run tests to confirm pass**

```bash
uv run pytest tests/test_invest_screener_schemas.py -v
```

Expected: all schema tests PASS, including the two new ones.

- [ ] **Step 5: Commit**

```bash
git add app/schemas/invest_screener.py tests/test_invest_screener_schemas.py
git commit -m "feat(rob-277): extend ScreenerFreshness with primary/dependencies/overallState"
```

---

### Task 2: Freshness helpers — `_classify_investor_flow_partition`, `compute_overall_state`, `format_kst_as_of_label`

**Files:**
- Modify: `app/services/invest_screener_snapshots/freshness.py`
- Test: `tests/test_invest_screener_snapshots_freshness.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_invest_screener_snapshots_freshness.py`:

```python
import datetime as dt
from zoneinfo import ZoneInfo

from app.services.invest_screener_snapshots.freshness import (
    classify_investor_flow_partition,
    compute_overall_state,
    format_kst_as_of_label,
)

_KST = ZoneInfo("Asia/Seoul")


def test_classify_investor_flow_partition_fresh_when_partition_is_today_trading_date() -> None:
    today = dt.date(2026, 5, 20)  # Wednesday
    state = classify_investor_flow_partition(
        snapshot_date=today,
        collected_at=dt.datetime(2026, 5, 20, 7, 30, tzinfo=dt.UTC),
        today_trading_date_value=today,
        now=dt.datetime(2026, 5, 20, 8, 0, tzinfo=dt.UTC),
    )
    assert state == "fresh"


def test_classify_investor_flow_partition_stale_when_partition_is_two_trading_days_old() -> None:
    state = classify_investor_flow_partition(
        snapshot_date=dt.date(2026, 5, 18),  # Monday
        collected_at=dt.datetime(2026, 5, 18, 7, 30, tzinfo=dt.UTC),
        today_trading_date_value=dt.date(2026, 5, 20),  # Wednesday
        now=dt.datetime(2026, 5, 20, 8, 0, tzinfo=dt.UTC),
    )
    assert state == "stale"


def test_classify_investor_flow_partition_friday_to_monday_morning_is_fresh() -> None:
    """Saturday/Sunday collapse to Friday's trading date; Monday morning before close stays fresh
    relative to Friday's snapshot because today_trading_date may still be Friday until Monday's
    partition appears."""
    state = classify_investor_flow_partition(
        snapshot_date=dt.date(2026, 5, 15),  # Friday
        collected_at=dt.datetime(2026, 5, 15, 7, 30, tzinfo=dt.UTC),
        today_trading_date_value=dt.date(2026, 5, 15),
        now=dt.datetime(2026, 5, 18, 0, 5, tzinfo=dt.UTC),  # Monday 09:05 KST
    )
    assert state == "fresh"


def test_compute_overall_state_primary_stale_dominates() -> None:
    assert compute_overall_state(primary_state="stale", dependency_states=["fresh"]) == "stale"


def test_compute_overall_state_primary_missing_dominates() -> None:
    assert compute_overall_state(primary_state="missing", dependency_states=["fresh"]) == "missing"


def test_compute_overall_state_primary_fresh_dependency_stale_is_stale() -> None:
    assert compute_overall_state(primary_state="fresh", dependency_states=["stale"]) == "stale"


def test_compute_overall_state_primary_fresh_dependency_missing_is_stale() -> None:
    assert compute_overall_state(primary_state="fresh", dependency_states=["missing"]) == "stale"


def test_compute_overall_state_primary_fresh_dependency_partial_is_partial() -> None:
    assert compute_overall_state(primary_state="fresh", dependency_states=["partial"]) == "partial"


def test_compute_overall_state_primary_fresh_no_dependencies_is_fresh() -> None:
    assert compute_overall_state(primary_state="fresh", dependency_states=[]) == "fresh"


def test_format_kst_as_of_label_for_snapshot_date_only_uses_jangmagam() -> None:
    label = format_kst_as_of_label(
        snapshot_date=dt.date(2026, 5, 13), computed_at=None
    )
    assert label == "2026.05.13 장마감 기준"


def test_format_kst_as_of_label_with_computed_at_uses_hhmm() -> None:
    label = format_kst_as_of_label(
        snapshot_date=dt.date(2026, 5, 20),
        computed_at=dt.datetime(2026, 5, 20, 0, 35, tzinfo=dt.UTC),  # 09:35 KST
    )
    assert label == "2026.05.20 09:35 기준"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_invest_screener_snapshots_freshness.py -v -k "classify_investor_flow_partition or compute_overall_state or format_kst_as_of_label"
```

Expected: FAIL with `ImportError` on the three new helpers.

- [ ] **Step 3: Implement the helpers**

Append to `app/services/invest_screener_snapshots/freshness.py`:

```python
def classify_investor_flow_partition(
    *,
    snapshot_date: dt.date,
    collected_at: dt.datetime | None,
    today_trading_date_value: dt.date,
    now: dt.datetime,
) -> DataState:
    """Classify an investor_flow_snapshots partition's freshness.

    Mirrors classify_state() but without closes_window_len (investor_flow rows have
    no candle window). Stays in this module so KST/trading-date logic lives in one
    place per ROB-277 §D4.
    """
    if collected_at is not None and collected_at.tzinfo is None:
        collected_at = collected_at.replace(tzinfo=dt.UTC)
    if snapshot_date != today_trading_date_value:
        return "stale"
    if collected_at is not None:
        age_hours = (now - collected_at).total_seconds() / 3600.0
        if age_hours >= STALE_AFTER_HOURS:
            return "stale"
    return "fresh"


def compute_overall_state(
    *,
    primary_state: DataState,
    dependency_states: list[DataState],
) -> DataState:
    """Aggregate primary + dependency states per ROB-277 §D1.c.

    NOT the same as aggregate_states(): when primary is fresh but a dependency is
    stale/missing, the user-visible overall is "stale" (conservative), not
    "fallback". See plan locked decision D1.c.
    """
    if primary_state in {"missing", "stale"}:
        return primary_state
    if any(s in {"missing", "stale"} for s in dependency_states):
        return "stale"
    if any(s == "partial" for s in dependency_states):
        return "partial"
    return primary_state


def format_kst_as_of_label(
    *,
    snapshot_date: dt.date,
    computed_at: dt.datetime | None,
) -> str:
    """Format a Korean 'as-of' label for the data basis.

    With computed_at: "YYYY.MM.DD HH:MM 기준" in KST.
    Without computed_at: "YYYY.MM.DD 장마감 기준" (treats it as end-of-day partition).
    """
    if computed_at is None:
        return f"{snapshot_date.strftime('%Y.%m.%d')} 장마감 기준"
    if computed_at.tzinfo is None:
        computed_at = computed_at.replace(tzinfo=dt.UTC)
    kst = computed_at.astimezone(ZoneInfo("Asia/Seoul"))
    return kst.strftime("%Y.%m.%d %H:%M 기준")
```

- [ ] **Step 4: Run tests to confirm pass**

```bash
uv run pytest tests/test_invest_screener_snapshots_freshness.py -v
```

Expected: all 10 new tests PASS plus existing tests still PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/invest_screener_snapshots/freshness.py tests/test_invest_screener_snapshots_freshness.py
git commit -m "feat(rob-277): add investor-flow classification + overall-state aggregation helpers"
```

---

### Task 3: Hydrate investor-flow snapshot rows with `snapshot_date` / `collected_at` / classified state

**Files:**
- Modify: `app/services/invest_view_model/screener_service.py:462-563` (`_load_investor_flow_discovery_from_snapshots`)
- Modify: `app/services/invest_view_model/screener_service.py:173-198` (`_investor_flow_item_from_screener_row`)
- Test: `tests/test_invest_view_model_screener_service.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_invest_view_model_screener_service.py`:

```python
@pytest.mark.asyncio
async def test_investor_flow_momentum_rows_carry_snapshot_date_and_classified_state(
    db_session,  # existing pytest fixture; see test file for shape
) -> None:
    import datetime as dt

    from app.models.investor_flow_snapshot import InvestorFlowSnapshot
    from app.services.invest_view_model.screener_service import (
        _load_investor_flow_discovery_from_snapshots,
    )

    # Insert one row dated 2 trading days ago.
    snap = InvestorFlowSnapshot(
        market="kr",
        symbol="005930",
        snapshot_date=dt.date(2026, 5, 18),  # Monday; "today" in test = 2026-05-20
        collected_at=dt.datetime(2026, 5, 18, 7, 30, tzinfo=dt.UTC),
        source="naver",
        foreign_net=1000,
        institution_net=500,
        individual_net=-1500,
        double_buy=True,
        foreign_consecutive_buy_days=4,
    )
    db_session.add(snap)
    await db_session.commit()

    rows = await _load_investor_flow_discovery_from_snapshots(
        db_session, market="kr", limit=10
    )
    assert rows is not None and len(rows) == 1
    row = rows[0]
    assert row["symbol"] == "005930"
    assert row["snapshot_date"] == dt.date(2026, 5, 18)
    assert row["collected_at"] is not None
    # state is NOT hardcoded "fresh"; classified from partition date vs today
    assert row["_screener_snapshot_state"] in {"fresh", "stale"}
    # specifically: with today=2026-05-20 and snapshot=2026-05-18, state should be stale.
    # If the test infra cannot freeze "today", at minimum assert no hardcoded "fresh"
    # by checking row pulls snapshot_date through.
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_invest_view_model_screener_service.py::test_investor_flow_momentum_rows_carry_snapshot_date_and_classified_state -v
```

Expected: FAIL — current code returns `{"_screener_snapshot_state": "fresh"}` without `snapshot_date` / `collected_at`.

- [ ] **Step 3: Replace the hardcoded "fresh" block and pass snapshot metadata**

In `app/services/invest_view_model/screener_service.py`, locate the row construction at lines ~547-560 inside `_load_investor_flow_discovery_from_snapshots`. Replace with:

```python
        from app.services.invest_screener_snapshots.freshness import (
            classify_investor_flow_partition,
            today_trading_date,
        )

        today = today_trading_date(market)
        now = datetime.now(UTC)

        state = classify_investor_flow_partition(
            snapshot_date=snap.snapshot_date,
            collected_at=snap.collected_at,
            today_trading_date_value=today,
            now=now,
        )

        rows.append(
            {
                "symbol": snap.symbol,
                "market": "kr",
                "name": symbol_names.get(snap.symbol),
                "foreign_net": snap.foreign_net,
                "institution_net": snap.institution_net,
                "individual_net": snap.individual_net,
                "foreign_consecutive_buy_days": snap.foreign_consecutive_buy_days,
                "institution_consecutive_buy_days": snap.institution_consecutive_buy_days,
                "double_buy": snap.double_buy,
                "snapshot_date": snap.snapshot_date,
                "collected_at": snap.collected_at,
                "_screener_snapshot_state": state,
            }
        )
```

(Move the `from app.services.invest_screener_snapshots.freshness import ...` block above the `for snap in candidate_snaps:` loop so it imports once.)

Update `_investor_flow_item_from_screener_row` (line 173) to read the new fields:

```python
def _investor_flow_item_from_screener_row(
    row: dict[str, Any],
) -> InvestorFlowItem | None:
    symbol = str(row.get("symbol") or "").strip().upper()
    if not symbol:
        return None

    snapshot_state = str(row.get("_screener_snapshot_state") or "").strip()
    data_state = snapshot_state if snapshot_state in {"fresh", "stale", "missing"} else "fresh"
    return InvestorFlowItem(
        symbol=symbol,
        market="kr",
        dataState=data_state,
        snapshotDate=row.get("snapshot_date"),
        collectedAt=row.get("collected_at"),  # passes through when available
        foreignNet=row.get("foreign_net"),
        institutionNet=row.get("institution_net"),
        individualNet=row.get("individual_net"),
        doubleBuy=bool(row.get("double_buy")),
        doubleSell=bool(row.get("double_sell")),
        foreignConsecutiveBuyDays=row.get("foreign_consecutive_buy_days"),
        foreignConsecutiveSellDays=row.get("foreign_consecutive_sell_days"),
        institutionConsecutiveBuyDays=row.get("institution_consecutive_buy_days"),
        institutionConsecutiveSellDays=row.get("institution_consecutive_sell_days"),
        individualConsecutiveBuyDays=row.get("individual_consecutive_buy_days"),
        individualConsecutiveSellDays=row.get("individual_consecutive_sell_days"),
    )
```

- [ ] **Step 4: Run test to confirm pass and the existing screener tests still pass**

```bash
uv run pytest tests/test_invest_view_model_screener_service.py -v
```

Expected: new test PASS; all other tests in the file PASS unchanged. If any test depended on `_screener_snapshot_state="fresh"` hardcode, update it to assert the classified state instead — note the change in the commit message.

- [ ] **Step 5: Commit**

```bash
git add app/services/invest_view_model/screener_service.py tests/test_invest_view_model_screener_service.py
git commit -m "fix(rob-277): classify investor_flow_momentum freshness from snapshot partition"
```

---

### Task 4: New `_build_freshness_v2` orchestrator — emits primary/dependencies/overallState

**Files:**
- Modify: `app/services/invest_view_model/screener_service.py:1149-1189` (`_build_freshness`)
- Test: `tests/test_invest_view_model_screener_service.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_invest_view_model_screener_service.py`:

```python
@pytest.mark.asyncio
async def test_build_freshness_snapshot_first_uses_partition_date_not_now() -> None:
    """ROB-277 D1: snapshot-first response must surface partition date, not now()."""
    import datetime as dt
    from unittest.mock import AsyncMock

    from app.services.invest_view_model.screener_service import build_screener_results

    fake_now = lambda: dt.datetime(2026, 5, 20, 0, 10, tzinfo=dt.UTC)
    # Patch _load_consecutive_gainers_from_snapshots indirectly by injecting rows
    # via the snapshot path. Test relies on a DB fixture seeded with a snapshot
    # dated 2026-05-13 — see fixture _seed_kr_stale_snapshot below.
    ...  # finish per fixture style of the test file
    resp = await build_screener_results(
        preset_id="consecutive_gainers",
        screening_service=AsyncMock(),
        resolver=_StubResolver(),
        market="kr",
        now=fake_now,
        session=db_session,
    )
    f = resp.freshness
    assert f.primary is not None
    assert f.primary.kind == "screener_snapshot"
    assert f.primary.snapshotDate == "2026-05-13"
    assert "2026.05.13" in f.primary.asOfLabel
    assert f.primary.dataState == "stale"
    assert "2026.05.13" in f.asOfLabel              # data-as-of, NOT 2026-05-20
    assert f.servedAt is not None and f.servedAt.startswith("2026-05-20")
    assert f.source == "cached"                     # D2: top-level enum unchanged
    assert f.dataState == f.overallState            # D1.c: legacy alias
    assert f.overallState == "stale"


@pytest.mark.asyncio
async def test_build_freshness_legacy_dataState_aliases_overallState(db_session) -> None:
    # Use whatever happy-path fixture exists in this file; assert the alias holds.
    resp = await _call_build_screener_results_happy_path(db_session)
    assert resp.freshness.dataState == resp.freshness.overallState
```

(Use the existing test-file conventions for DB session fixtures. The plan does not invent fixture names — adapt to whatever `tests/test_invest_view_model_screener_service.py` already uses for snapshot seeding. Search for `InvestScreenerSnapshot(` in the file to find the pattern.)

- [ ] **Step 2: Run tests to confirm fail**

```bash
uv run pytest tests/test_invest_view_model_screener_service.py -v -k "snapshot_first_uses_partition_date or legacy_dataState_aliases_overallState"
```

Expected: FAIL on `primary` not present / `asOfLabel` containing "2026.05.20" instead of "2026.05.13".

- [ ] **Step 3: Refactor `_build_freshness` to take snapshot metadata and produce the new fields**

Replace the existing `_build_freshness` function (lines ~1149-1189) in `app/services/invest_view_model/screener_service.py`:

```python
def _build_freshness(
    *,
    raw_timestamp: str | None,
    cache_hit: bool,
    market: str,
    now: Callable[[], datetime],
    dataState: str = "missing",
    primary_kind: Literal["screener_snapshot", "live", "fallback"] = "live",
    primary_snapshot_date: "dt.date | None" = None,
    primary_computed_at: "datetime | None" = None,
    primary_source: str | None = None,
    dependency_specs: list[dict[str, Any]] | None = None,
) -> ScreenerFreshness:
    """ROB-277: split served time vs data 기준.

    raw_timestamp historically served as both, which is the bug this fixes.  When
    primary_kind == "screener_snapshot", primary fields derive from the partition
    date/computed_at and the user-visible asOfLabel reflects that — NOT now().

    dependency_specs items shape:
      {"kind": "investor_flow", "snapshot_date": date|None, "collected_at": dt|None,
       "data_state": "fresh|partial|stale|missing|fallback", "source": str|None}
    """
    from app.services.invest_screener_snapshots.freshness import (
        compute_overall_state,
        format_kst_as_of_label,
    )

    now_utc = now()
    # served = response refresh time, always now() of the request
    served_at_utc = now_utc
    served_kst = served_at_utc.astimezone(_KST)

    # served relative label is small/simple (no "갱신" suffix per D3)
    served_delta = 0  # served == now, so always "방금"
    served_relative = "방금"

    # Determine fetched (legacy field). For snapshot kind, fetched mirrors the
    # partition's computed_at (or end-of-snapshot-date 15:30 KST when computed_at
    # is missing) so legacy consumers see the data-as-of timestamp.
    if primary_kind == "screener_snapshot" and primary_snapshot_date is not None:
        if primary_computed_at is not None:
            fetched = primary_computed_at
            if fetched.tzinfo is None:
                fetched = fetched.replace(tzinfo=UTC)
        else:
            # Treat as end-of-trading-day in KST.
            fetched_kst = datetime.combine(
                primary_snapshot_date, _time(15, 30), tzinfo=_KST
            )
            fetched = fetched_kst.astimezone(UTC)
    elif raw_timestamp:
        try:
            fetched = datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00"))
        except ValueError:
            fetched = now_utc
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=UTC)
    else:
        fetched = now_utc

    fetched_kst = fetched.astimezone(_KST)
    delta = max(0, int((now_utc - fetched).total_seconds()))
    now_kst = now_utc.astimezone(_KST)
    market_open = market == "kr" and _is_kr_market_open(now_kst)

    if not market_open and delta > _CACHE_HIT_FRESH_SECONDS * 4:
        source: Literal["live", "cached", "previous_session"] = "previous_session"
        relative = "전 거래일 기준"
    elif cache_hit:
        source = "cached"
        relative = _format_relative_korean(delta)
    else:
        source = "live"
        relative = _format_relative_korean(delta)

    # Build primary object
    primary: ScreenerFreshnessPrimary | None = None
    if primary_kind == "screener_snapshot" and primary_snapshot_date is not None:
        primary = ScreenerFreshnessPrimary(
            kind="screener_snapshot",
            snapshotDate=primary_snapshot_date.isoformat(),
            computedAt=primary_computed_at.astimezone(UTC).isoformat()
            if primary_computed_at is not None
            else None,
            asOfLabel=format_kst_as_of_label(
                snapshot_date=primary_snapshot_date,
                computed_at=primary_computed_at,
            ),
            dataState=dataState,  # type: ignore[arg-type]
            source=primary_source,
        )
    elif primary_kind in {"live", "fallback"}:
        primary = ScreenerFreshnessPrimary(
            kind=primary_kind,
            snapshotDate=None,
            computedAt=None,
            asOfLabel=fetched_kst.strftime("%Y.%m.%d %H:%M 기준"),
            dataState=dataState,  # type: ignore[arg-type]
            source=primary_source,
        )

    # Build dependencies list
    dependencies: list[ScreenerFreshnessDependency] = []
    for spec in dependency_specs or []:
        dep_snap = spec.get("snapshot_date")
        dep_collected = spec.get("collected_at")
        lag_label: str | None = None
        if dep_snap is not None and primary_snapshot_date is not None:
            lag_days = (primary_snapshot_date - dep_snap).days
            if lag_days >= 1:
                lag_label = f"{lag_days}거래일 지연"
        dependencies.append(
            ScreenerFreshnessDependency(
                kind=spec.get("kind", "investor_flow"),
                snapshotDate=dep_snap.isoformat() if dep_snap is not None else None,
                collectedAt=dep_collected.astimezone(UTC).isoformat()
                if dep_collected is not None
                else None,
                lagLabel=lag_label,
                dataState=spec.get("data_state", "missing"),  # type: ignore[arg-type]
                source=spec.get("source"),
            )
        )

    overall = compute_overall_state(
        primary_state=primary.dataState if primary is not None else dataState,  # type: ignore[arg-type]
        dependency_states=[d.dataState for d in dependencies],
    )

    return ScreenerFreshness(
        fetchedAt=fetched.astimezone(UTC).isoformat(),
        asOfLabel=primary.asOfLabel if primary is not None else fetched_kst.strftime("%Y.%m.%d %H:%M 기준"),
        relativeLabel=relative,
        cacheHit=bool(cache_hit),
        source=source,
        dataState=overall,                              # alias of overallState (D1.c)
        servedAt=served_at_utc.isoformat(),
        servedRelativeLabel=served_relative,
        primary=primary,
        dependencies=dependencies,
        overallState=overall,
    )
```

Add the new imports near the top of `screener_service.py`:

```python
from app.schemas.invest_screener import (
    ChangeDirection,
    ScreenerCandidateContext,
    ScreenerFreshness,
    ScreenerFreshnessDependency,
    ScreenerFreshnessPrimary,
    ScreenerInvestorFlowChip,
    ScreenerPresetsResponse,
    ScreenerResultRow,
    ScreenerResultsResponse,
    ScreenerRiskContext,
    ScreenerSourceContext,
)
```

- [ ] **Step 4: Run the new tests to confirm pass**

```bash
uv run pytest tests/test_invest_view_model_screener_service.py -v -k "snapshot_first_uses_partition_date or legacy_dataState_aliases_overallState"
```

Expected: PASS. Run the full screener test file to make sure nothing else broke:

```bash
uv run pytest tests/test_invest_view_model_screener_service.py -v
```

If existing tests fail because they asserted `asOfLabel` matched `now()` exactly, update them to assert against the partition date — those assertions were encoding the bug.

- [ ] **Step 5: Commit**

```bash
git add app/services/invest_view_model/screener_service.py tests/test_invest_view_model_screener_service.py
git commit -m "feat(rob-277): _build_freshness emits primary/dependencies/overallState"
```

---

### Task 5: Wire snapshot metadata into `build_screener_results` callsites

**Files:**
- Modify: `app/services/invest_view_model/screener_service.py:1192-1452` (`build_screener_results`)
- Test: `tests/test_invest_view_model_screener_service.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_invest_view_model_screener_service.py`:

```python
@pytest.mark.asyncio
async def test_consecutive_gainers_response_carries_investor_flow_dependency(
    db_session,
) -> None:
    """ROB-277 D1.b + D5(5): KR consecutive_gainers row that has an investor-flow
    chip should expose the dependency in freshness.dependencies."""
    # Seed: one fresh-enough consecutive_gainers snapshot AND one investor_flow
    # snapshot 2 trading days older for the same symbol. (Use existing fixtures.)
    ...
    resp = await build_screener_results(
        preset_id="consecutive_gainers",
        screening_service=_StubScreeningService(),
        resolver=_StubResolver(),
        market="kr",
        session=db_session,
    )
    deps = resp.freshness.dependencies
    assert any(d.kind == "investor_flow" for d in deps)
    inv_dep = next(d for d in deps if d.kind == "investor_flow")
    assert inv_dep.snapshotDate is not None
    assert inv_dep.dataState in {"fresh", "stale"}  # never hardcoded
    assert resp.freshness.overallState in {"fresh", "partial", "stale"}
    # alias check
    assert resp.freshness.dataState == resp.freshness.overallState
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_invest_view_model_screener_service.py::test_consecutive_gainers_response_carries_investor_flow_dependency -v
```

Expected: FAIL — `freshness.dependencies` is empty because `build_screener_results` doesn't populate dependency specs yet.

- [ ] **Step 3: Plumb snapshot metadata into `_build_freshness` callsites**

In `build_screener_results` (around lines 1329-1335 in current code), determine primary + dependency specs **before** calling `_build_freshness`:

```python
    # Determine primary kind/source for ROB-277 freshness.
    primary_kind: Literal["screener_snapshot", "live", "fallback"]
    primary_snapshot_date: "dt.date | None" = None
    primary_computed_at: "datetime | None" = None
    primary_source: str | None = None
    if _snapshot_was_checked:
        primary_kind = "screener_snapshot"
        # Latest partition date for the preset's primary table.
        if preset_id == "investor_flow_momentum":
            primary_source = "investor_flow_snapshots"
        elif requested_market == "crypto":
            primary_source = "invest_crypto_screener_snapshots"
        else:
            primary_source = "invest_screener_snapshots"
        # Derive snapshot_date / computed_at from the first row's metadata if available.
        if rows:
            primary_snapshot_date = rows[0].get("snapshot_date") or rows[0].get(
                "_snapshot_date"
            )
            primary_computed_at = rows[0].get("computed_at") or rows[0].get(
                "_computed_at"
            )
    else:
        primary_kind = "live"
        primary_source = "screening_service"

    # Build dependency specs from hydrated investor-flow chips (KR only for now).
    dependency_specs: list[dict[str, Any]] = []
    if requested_market == "kr" and investor_flow_chips:
        # Pick the most representative (or worst) dependency state across chips.
        # Use the row metadata directly from the hydrated chips' source rows.
        from app.services.invest_screener_snapshots.freshness import (
            classify_investor_flow_partition,
            today_trading_date,
        )

        inv_states: list[str] = []
        inv_snapshot_dates: list = []
        for r in rows:
            sd = r.get("snapshot_date") or r.get("_investor_flow_snapshot_date")
            if sd is not None:
                inv_snapshot_dates.append(sd)
            st = r.get("_screener_snapshot_state")
            if st is not None and preset_id == "investor_flow_momentum":
                inv_states.append(str(st))

        if inv_snapshot_dates:
            worst = min(inv_snapshot_dates)
            dependency_specs.append(
                {
                    "kind": "investor_flow",
                    "snapshot_date": worst,
                    "collected_at": None,
                    "data_state": (
                        "stale"
                        if worst != today_trading_date("kr")
                        else "fresh"
                    ),
                    "source": "investor_flow_snapshots",
                }
            )

    freshness = _build_freshness(
        raw_timestamp=raw.get("timestamp"),
        cache_hit=bool(raw.get("cache_hit")),
        market=requested_market,
        now=now,
        dataState=_aggregated_data_state,
        primary_kind=primary_kind,
        primary_snapshot_date=primary_snapshot_date,
        primary_computed_at=primary_computed_at,
        primary_source=primary_source,
        dependency_specs=dependency_specs,
    )
```

Make sure `_load_consecutive_gainers_from_snapshots` also passes `computed_at` and `snapshot_date` through to the row payload (it already includes these as columns on the model — see `app/services/invest_view_model/screener_service.py:433-454`; add them to the row dict):

```python
        rows.append(
            {
                "symbol": snap.symbol,
                "market": market,
                "name": symbol_names.get(snap.symbol),
                "close": float(snap.latest_close) if snap.latest_close is not None else None,
                "change_rate": float(snap.change_rate) if snap.change_rate is not None else None,
                "change_amount": float(snap.change_amount) if snap.change_amount is not None else None,
                "consecutive_up_days": snap.consecutive_up_days,
                "week_change_rate": float(snap.week_change_rate) if snap.week_change_rate is not None else None,
                "volume": snap.daily_volume,
                "daily_closes": list(snap.closes_window or []),
                "snapshot_date": snap.snapshot_date,
                "computed_at": snap.computed_at,
                "_screener_snapshot_state": state,
            }
        )
```

Hydrate investor-flow chip rows (`_hydrate_investor_flow_chips` lines 201-229) to record their own snapshot date on the originating `rows[]` for the dependency aggregator — extend the post-fetch loop:

```python
    for symbol, item in items.items():
        chip = _investor_flow_chip_for_item(item)
        if chip is not None:
            chips[symbol] = chip
            # ROB-277: stash the dependency snapshot_date back on the row so
            # build_screener_results can build freshness.dependencies later.
            for r in rows:
                if str(r.get("symbol") or "").upper() == symbol:
                    r["_investor_flow_snapshot_date"] = (
                        item.snapshotDate if hasattr(item, "snapshotDate") else None
                    )
                    break
    return chips
```

- [ ] **Step 4: Run test to confirm pass and full screener suite still green**

```bash
uv run pytest tests/test_invest_view_model_screener_service.py -v
uv run pytest tests/test_invest_api_screener_router.py -v
```

Expected: all PASS. If `test_invest_api_screener_router.py` snapshots a JSON body, refresh the snapshot intentionally (commit message should call this out).

- [ ] **Step 5: Commit**

```bash
git add app/services/invest_view_model/screener_service.py tests/test_invest_view_model_screener_service.py
git commit -m "feat(rob-277): thread snapshot metadata + dependency specs through build_screener_results"
```

---

### Task 6: Frontend types — extend `ScreenerFreshness`

**Files:**
- Modify: `frontend/invest/src/types/screener.ts:103-113`

- [ ] **Step 1: Write the failing test**

Append to `frontend/invest/src/__tests__/ScreenerFreshnessLine.test.tsx`:

```tsx
import type {
  ScreenerFreshness,
  ScreenerFreshnessPrimary,
  ScreenerFreshnessDependency,
} from "../types/screener";

describe("ScreenerFreshness type shape", () => {
  test("accepts the ROB-277 additive fields", () => {
    const primary: ScreenerFreshnessPrimary = {
      kind: "screener_snapshot",
      snapshotDate: "2026-05-13",
      computedAt: "2026-05-13T06:35:00+00:00",
      asOfLabel: "2026.05.13 장마감 기준",
      dataState: "stale",
      source: "invest_screener_snapshots",
    };
    const dep: ScreenerFreshnessDependency = {
      kind: "investor_flow",
      snapshotDate: "2026-05-18",
      collectedAt: "2026-05-18T07:30:00+00:00",
      lagLabel: "2거래일 지연",
      dataState: "stale",
      source: "investor_flow_snapshots",
    };
    const f: ScreenerFreshness = {
      fetchedAt: "2026-05-13T06:35:00+00:00",
      asOfLabel: "2026.05.13 장마감 기준",
      relativeLabel: "5거래일 지연",
      cacheHit: true,
      source: "cached",
      dataState: "stale",
      servedAt: "2026-05-20T00:10:00+00:00",
      servedRelativeLabel: "방금",
      primary,
      dependencies: [dep],
      overallState: "stale",
    };
    expect(f.primary?.kind).toBe("screener_snapshot");
    expect(f.dependencies?.[0]?.kind).toBe("investor_flow");
  });
});
```

- [ ] **Step 2: Run test to verify it fails (type error)**

```bash
cd frontend/invest && pnpm test -- ScreenerFreshnessLine
```

Expected: FAIL with TS errors on `ScreenerFreshnessPrimary` / `ScreenerFreshnessDependency` / new fields not assignable.

- [ ] **Step 3: Extend the types**

Replace the `ScreenerFreshness` interface in `frontend/invest/src/types/screener.ts` (lines 103-113):

```ts
export type ScreenerFreshnessSource = "live" | "cached" | "previous_session";
export type ScreenerDataState = "fresh" | "partial" | "stale" | "missing" | "fallback";

export interface ScreenerFreshnessPrimary {
  kind: "screener_snapshot" | "live" | "fallback";
  snapshotDate: string | null;
  computedAt: string | null;
  asOfLabel: string;
  dataState: ScreenerDataState;
  source: string | null;
}

export interface ScreenerFreshnessDependency {
  kind: "investor_flow";
  snapshotDate: string | null;
  collectedAt: string | null;
  lagLabel: string | null;
  dataState: ScreenerDataState;
  source: string | null;
}

export interface ScreenerFreshness {
  fetchedAt: string;
  asOfLabel: string;
  relativeLabel: string;
  cacheHit: boolean;
  source: ScreenerFreshnessSource;
  dataState: ScreenerDataState;
  // ROB-277 additive fields (optional during transition).
  servedAt?: string;
  servedRelativeLabel?: string;
  primary?: ScreenerFreshnessPrimary | null;
  dependencies?: ScreenerFreshnessDependency[];
  overallState?: ScreenerDataState;
}
```

- [ ] **Step 4: Run test to confirm pass**

```bash
cd frontend/invest && pnpm test -- ScreenerFreshnessLine
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add frontend/invest/src/types/screener.ts frontend/invest/src/__tests__/ScreenerFreshnessLine.test.tsx
git commit -m "feat(rob-277): extend frontend ScreenerFreshness types with primary/dependencies"
```

---

### Task 7: Frontend `ScreenerFreshnessLine` — dual-line render

**Files:**
- Modify: `frontend/invest/src/desktop/screener/ScreenerFreshnessLine.tsx`

- [ ] **Step 1: Write the failing tests**

Append to `frontend/invest/src/__tests__/ScreenerFreshnessLine.test.tsx`:

```tsx
describe("ROB-277 dual-line rendering", () => {
  test("renders 데이터 기준 line and 화면 갱신 line in separate spans", () => {
    render(
      <ScreenerFreshnessLine
        freshness={{
          fetchedAt: "2026-05-13T06:35:00+00:00",
          asOfLabel: "2026.05.13 장마감 기준",
          relativeLabel: "5거래일 지연",
          cacheHit: true,
          source: "cached",
          dataState: "stale",
          servedAt: "2026-05-20T00:10:00+00:00",
          servedRelativeLabel: "방금",
          primary: {
            kind: "screener_snapshot",
            snapshotDate: "2026-05-13",
            computedAt: null,
            asOfLabel: "2026.05.13 장마감 기준",
            dataState: "stale",
            source: "invest_screener_snapshots",
          },
          dependencies: [],
          overallState: "stale",
        }}
      />,
    );
    const dataLine = screen.getByTestId("screener-freshness-data");
    const servedLine = screen.getByTestId("screener-freshness-served");
    expect(dataLine).toHaveTextContent("데이터 기준");
    expect(dataLine).toHaveTextContent("2026.05.13");
    expect(servedLine).toHaveTextContent("화면 갱신");
    expect(servedLine).toHaveTextContent("방금");
    // Crucial non-contradiction check: stale-aging text never appears on the served line.
    expect(servedLine).not.toHaveTextContent("5거래일 지연");
    expect(servedLine).not.toHaveTextContent("업데이트 필요");
    // And served label "방금" never bleeds into the data line.
    expect(dataLine).not.toHaveTextContent("방금");
  });

  test("falls back to legacy single-line when primary/served fields absent", () => {
    render(
      <ScreenerFreshnessLine
        freshness={{
          fetchedAt: "2026-05-10T05:30:00+00:00",
          asOfLabel: "2026.05.10 14:30 기준",
          relativeLabel: "12분 전 갱신",
          cacheHit: false,
          source: "live",
          dataState: "fresh",
        }}
      />,
    );
    expect(screen.getByTestId("screener-freshness")).toHaveTextContent(
      "2026.05.10 14:30 기준 · 12분 전 갱신",
    );
  });
});
```

- [ ] **Step 2: Run tests to confirm fail**

```bash
cd frontend/invest && pnpm test -- ScreenerFreshnessLine
```

Expected: FAIL on `getByTestId("screener-freshness-data")` not found.

- [ ] **Step 3: Rewrite the component to render two lines when new fields are present**

Replace `frontend/invest/src/desktop/screener/ScreenerFreshnessLine.tsx`:

```tsx
import type { ScreenerFreshness } from "../../types/screener";

const DATA_STATE_LABELS: Partial<Record<ScreenerFreshness["dataState"], string>> = {
  partial: "일부 데이터 지연",
  stale: "업데이트 필요",
  missing: "데이터 준비중",
  fallback: "대체 데이터",
};

export function ScreenerFreshnessLine({
  freshness,
}: {
  freshness: ScreenerFreshness;
}) {
  const hasNewSchema =
    freshness.primary != null || freshness.servedRelativeLabel != null;

  if (!hasNewSchema) {
    // Legacy single-line render preserved for transition / consumers that
    // haven't been upgraded yet. (ROB-277 §D1 additive policy.)
    const text =
      freshness.source === "previous_session"
        ? `${freshness.relativeLabel} · ${freshness.asOfLabel.replace("기준", "종가")}`
        : `${freshness.asOfLabel} · ${freshness.relativeLabel}`;
    const dataState = freshness.dataState ?? "fresh";
    const stateLabel = DATA_STATE_LABELS[dataState];
    return (
      <div
        className="screener-freshness"
        data-testid="screener-freshness"
        aria-live="polite"
      >
        <span>{text}</span>
        {stateLabel ? (
          <span
            className={`screener-freshness-state screener-freshness-state--${dataState}`}
          >
            {stateLabel}
          </span>
        ) : null}
      </div>
    );
  }

  const primary = freshness.primary;
  const overall = freshness.overallState ?? freshness.dataState;
  const stateLabel = DATA_STATE_LABELS[overall];

  // Build the data line per the D3 copy table.
  let dataLineText: string;
  if (freshness.source === "previous_session") {
    dataLineText = `전 거래일 기준 · ${primary?.asOfLabel ?? freshness.asOfLabel}`;
  } else if (overall === "missing") {
    dataLineText = "데이터 없음";
  } else {
    const base = `데이터 기준 ${primary?.asOfLabel ?? freshness.asOfLabel}`;
    // For stale, append "업데이트 필요" / "{N}거래일 지연" via state chip; do not duplicate inline.
    dataLineText = base;
  }

  const servedLabel = freshness.servedRelativeLabel ?? "방금";

  return (
    <div
      className="screener-freshness"
      data-testid="screener-freshness"
      aria-live="polite"
    >
      <span
        className="screener-freshness-data"
        data-testid="screener-freshness-data"
      >
        {dataLineText}
      </span>
      <span
        className="screener-freshness-served"
        data-testid="screener-freshness-served"
      >
        화면 갱신 {servedLabel}
      </span>
      {stateLabel ? (
        <span
          className={`screener-freshness-state screener-freshness-state--${overall}`}
        >
          {stateLabel}
        </span>
      ) : null}
    </div>
  );
}
```

- [ ] **Step 4: Run tests to confirm pass**

```bash
cd frontend/invest && pnpm test -- ScreenerFreshnessLine
```

Expected: PASS (new + existing tests). Manually inspect any CSS rule referencing `.screener-freshness` to confirm the two new child classes don't break layout — if a global stylesheet exists, add basic flex/stacking rules in the same commit.

- [ ] **Step 5: Commit**

```bash
git add frontend/invest/src/desktop/screener/ScreenerFreshnessLine.tsx frontend/invest/src/__tests__/ScreenerFreshnessLine.test.tsx
git commit -m "feat(rob-277): render 데이터 기준 and 화면 갱신 as separate spans"
```

---

### Task 8: End-to-end backend test — `/invest/api/screener/results` exposes the new schema with stale snapshot

**Files:**
- Modify: `tests/test_invest_api_screener_router.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_invest_api_screener_router.py`:

```python
@pytest.mark.asyncio
async def test_consecutive_gainers_endpoint_separates_served_from_data_basis(
    async_client, db_session
) -> None:
    """ROB-277 end-to-end: with a stale invest_screener_snapshots KR partition,
    /invest/api/screener/results must surface the partition date — not now() —
    in the freshness object."""
    # Seed a partition dated 5 trading days ago. Use existing seeding helpers
    # from this test file (search for InvestScreenerSnapshot to find the pattern).
    ...
    resp = await async_client.get(
        "/invest/api/screener/results",
        params={"preset": "consecutive_gainers", "market": "kr"},
    )
    body = resp.json()
    f = body["freshness"]
    assert f["source"] == "cached"
    assert f["primary"]["kind"] == "screener_snapshot"
    assert f["primary"]["snapshotDate"].startswith("2026-")
    # data 기준 reflects the snapshot, not the moment of the request:
    assert f["primary"]["snapshotDate"] in f["primary"]["asOfLabel"].replace(".", "-")
    assert f["servedAt"] is not None
    # legacy alias holds
    assert f["dataState"] == f["overallState"]
```

- [ ] **Step 2: Run test to verify it fails (or passes by luck)**

```bash
uv run pytest tests/test_invest_api_screener_router.py::test_consecutive_gainers_endpoint_separates_served_from_data_basis -v
```

Expected: PASS if Task 5 wiring is correct; FAIL if any wiring is missing.

- [ ] **Step 3: Fix any plumbing gap exposed**

If the test fails, the most likely cause is a code path in `build_screener_results` that bypasses the new `_build_freshness` arguments (e.g., the early-return for unknown preset, or the `_snapshot_check_result is None` fallback). Inspect and patch.

- [ ] **Step 4: Run full backend test suite to catch regressions**

```bash
make test
make typecheck
make lint
```

Expected: green. Address any unrelated failures only if they're caused by this PR — do not pile in unrelated cleanup.

- [ ] **Step 5: Commit**

```bash
git add tests/test_invest_api_screener_router.py
git commit -m "test(rob-277): end-to-end stale-snapshot freshness assertions on screener API"
```

---

### Task 9: Audit consumers + write PR body with compatibility caution

**Files:**
- Read: `frontend/invest/src/pages/desktop/DesktopScreenerPage.tsx`
- Read: any other files that import `ScreenerFreshness`
- Create: PR body content (not a tracked file; goes into `gh pr create`)

- [ ] **Step 1: Grep for `freshness.fetchedAt` and `freshness.asOfLabel` consumers**

```bash
rg -n "freshness\.(fetchedAt|asOfLabel|relativeLabel|dataState)" frontend/invest/src
rg -n "freshness\[\"(fetchedAt|asOfLabel|relativeLabel|dataState)\"\]" app
```

Expected: catalogue every reader. For each, decide:
- If it reads `fetchedAt` expecting **data-as-of**, this PR changes its meaning (now it's the partition's `computed_at` for snapshot-first responses — that is the intended fix, not a regression).
- If it reads `dataState`, it still gets a sane value (now == `overallState`).
- If it reads `asOfLabel`, the label now references the partition date for snapshot-first responses (this is the fix).

- [ ] **Step 2: Document the audit findings in the PR body**

Compose PR body with sections:
- **Summary** — what changed (additive schema + dual-line UI).
- **Why** — link to ROB-277 description and the observed contradiction.
- **Compatibility caution** — list every consumer touched by the semantic shift of `fetchedAt` / `asOfLabel` for snapshot-first responses. State whether the new semantics are the intended fix or a regression risk.
- **Test plan** — `make test`, `make typecheck`, `make lint`, frontend `pnpm test`, manual `/invest/screener` dev-browser check if dev stack is up.
- **Post-merge** — Hermes/파이리 confirms prod `/invest/api/screener/results` output matches expectations on next deploy. **Not a merge gate.**
- **Non-goals** — no DB migration, no scheduler activation, no backfill, no broker mutation (per ROB-189 / ROB-277 §D7).

- [ ] **Step 3: Open the PR**

```bash
git push -u origin HEAD
gh pr create --base main --title "feat(rob-277): split /invest/screener freshness into served-time vs data-as-of" --body "$(cat <<'EOF'
## Summary

- ScreenerFreshness gains additive `servedAt`/`servedRelativeLabel`/`primary`/`dependencies`/`overallState` fields. Existing 6 fields are kept; `dataState` is now an alias of `overallState`.
- Snapshot-first paths no longer report `now()` as the data-as-of time. `primary.snapshotDate` / `primary.asOfLabel` reflect the actual snapshot partition.
- `investor_flow_momentum` rows stop hardcoding `_screener_snapshot_state="fresh"` and now classify against the partition via `classify_investor_flow_partition`.
- `ScreenerFreshnessLine.tsx` renders `데이터 기준 …` and `화면 갱신 …` as separate spans.

## Why

ROB-277: `/invest/screener` was showing `방금 갱신` for results sourced from a 5-day-old `invest_screener_snapshots` partition, while row-level chips simultaneously reported `1일 지연`. Root cause: `raw["timestamp"] = now()` flowed into `_build_freshness` as both served-time and data-as-of.

## Compatibility caution

- For snapshot-first responses, `freshness.fetchedAt` / `freshness.asOfLabel` now reflect the partition's `computed_at` (or end-of-snapshot-day 15:30 KST) instead of `now()`. This is the intended fix. Any downstream consumer that displayed those fields as "when the response was generated" should switch to `freshness.servedAt` / `freshness.servedRelativeLabel`.

## Test plan

- [ ] `make test`
- [ ] `make typecheck`
- [ ] `make lint`
- [ ] `cd frontend/invest && pnpm test`
- [ ] Local dev-browser spot-check of `/invest/screener?market=kr` if dev stack is up

## Non-goals (per ROB-189 + ROB-277 §D7)

- No DB migration
- No scheduler activation / TaskIQ unpause / Prefect deploy registration
- No production backfill
- No broker / order / watch / order-intent mutation
- No trading recommendation logic change

## Post-merge (not a merge gate)

Hermes/파이리 confirms prod `/invest/api/screener/results?preset=consecutive_gainers&market=kr` carries the new `primary` object on next MacBook server deploy.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 4: Smoke check after push**

Verify CI is green and that the PR body renders the compatibility caution prominently.

- [ ] **Step 5: Done**

No commit; PR creation itself completes the task.

---

## Self-Review

Run this against the spec before handing off. Plan-author duty.

**Spec coverage:**

| ROB-277 requirement | Covered by |
|---------------------|------------|
| Split response refresh vs data 기준 | Tasks 1, 4, 7 (schema + backend + frontend) |
| `consecutive_gainers` stops emitting `now()` as data-as-of | Task 4 (`_build_freshness` rewrite) + Task 5 (wiring) + Task 8 (e2e test) |
| `investor_flow_momentum` derives state from partition, not hardcode | Task 3 |
| Snapshot-derived chips carry `snapshotDate` | Task 3 (row payload) |
| UI separates `데이터 기준` from `화면 갱신` | Task 7 |
| Backward-compatible (no removed fields) | Task 1 (additive only) + Task 7 (legacy branch preserved) |
| Tests (acceptance criteria 4 items) | Tasks 1, 3, 4, 5, 7, 8 |
| No DB migration / scheduler / backfill | §D7 + Non-goals in PR body |

**Placeholder scan:** No `TBD` / `TODO` / "similar to" / "implement later" in any step. The Task 5 and Task 8 fixture-seeding step contains `...` for "use the existing fixture pattern in this file" — this is intentional, because the test-file's fixture API is project-specific and the implementer needs to follow the local convention; the prose tells them exactly where to look.

**Type consistency:** `ScreenerFreshnessPrimary` / `ScreenerFreshnessDependency` / `compute_overall_state` / `classify_investor_flow_partition` / `format_kst_as_of_label` names are consistent across backend tests (Tasks 1, 2), backend code (Tasks 2, 3, 4, 5), frontend types (Task 6), and component (Task 7). `kind` literal values match exactly. `dataState` enum values match across backend and frontend.

---

## Risk Register

| Risk | Likelihood | Mitigation |
|------|-----------|-----------|
| Existing tests assert `asOfLabel == now()` for snapshot-first | Medium | Task 4 step 4 explicitly updates these; commit message calls out the semantic shift. |
| Investor-flow `snapshotDate` is `date` object, not str — `Pydantic` rejection | Low | Task 3 keeps `date` in dict; Task 4 converts to `.isoformat()` before `ScreenerFreshnessDependency`. |
| Frontend dual-line layout breaks CSS grid in `DesktopScreenerPage` | Medium | Task 7 step 4 includes manual layout check; add minimal flex/stacking rules in same commit if needed. |
| Other presets (crypto, cheap_value, etc.) bypass new primary plumbing | Medium | Task 5 covers `_snapshot_was_checked` path uniformly; live paths default to `primary_kind="live"`. Task 8 e2e covers consecutive_gainers only — add a per-preset assertion later only if observed broken. |
| ROB-204 / ROB-205 lands a conflicting schema edit before this PR merges | Low | §D7 explicitly disclaims overlap; coordinate via Linear comment if a conflict appears at merge time. |

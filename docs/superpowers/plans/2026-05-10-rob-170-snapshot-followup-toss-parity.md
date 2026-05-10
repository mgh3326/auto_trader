# ROB-170 Follow-up — Production-Visible Snapshot Activation & Toss Parity Hardening

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the `/invest/screener` `consecutive_gainers` preset behave like Toss in production: snapshot-backed reads (so `freshness.dataState` is `fresh`/`partial`/`stale`/`fallback` rather than always `missing`), explicit `consecutive_up_days >= 5` and `week_change_rate >= 0` filters that drop ineligible rows even when upstream tvscreener returns them, and full-universe operator backfill workflow with read-only diagnostics.

**Architecture:**
- Wire the existing snapshot-hydration helper (`app/mcp_server/tooling/screening/enrichment._hydrate_from_snapshots`, ROB-170 PR #772) into the production `/invest/screener` request path. Today the FastAPI router passes a DB session to `build_screener_results` but the session never reaches `_enrich_consecutive_up_days`, so `_screener_snapshot_state` is never set and `aggregate_states` always returns `"missing"`. The fix threads the session through (or hydrates rows once before result-row formatting) so `dataState` is meaningful as soon as snapshots exist.
- Add the second Toss filter `min_week_change_rate=0.0` to `consecutive_gainers` so the chip "1주일 전 보다 · 0% 이상" actually drops rows whose week change is negative or unknown. The filter is implemented as an existing-pattern post-enrichment list filter (`_apply_min_week_change_rate`) symmetric to the streak filter, normalized in `normalize_screen_request`, threaded through `screener_service.list_screening` / `screen_stocks_impl`, and consulted from the snapshot first (no extra OHLCV fetch when the snapshot already populated `week_change_rate`).
- Make the operator-driven backfill CLI capable of full-universe coverage (currently `--limit` defaults to 20 and US ignores `is_active`). Add `--all` to iterate the full active universe in batches, align KR/US `is_active` filtering, and keep `--dry-run` as the default. Coverage diagnostics (`scripts/diagnose_invest_screener_snapshots`, `GET /invest/api/screener/snapshots/coverage`) are unchanged but become the gating evidence before any `--commit` against production.
- **No scheduler activation, no broker/order/watch mutations, no destructive DB writes** in this work. A production `--commit` only happens after explicit human approval following dry-run evidence; recurring fill remains a separate ticket.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy 2 async, Pydantic v2, pytest, Alembic. Frontend layer untouched (the existing `freshness.dataState` field already exists; the React badge wiring is a separate follow-up).

**Linear / Issue:** ROB-170 (snapshot follow-up).

**Branch:** `feature/ROB-170-snapshot-followup` (already created — verify with `git -C /Users/mgh3326/worktrees/auto_trader/rob-170-snapshot-followup branch --show-current`).

**Worktree:** `/Users/mgh3326/worktrees/auto_trader/rob-170-snapshot-followup` — implementer MUST work here. **Hard rule:** never edit `/Users/mgh3326/work/auto_trader` (production release checkout) or `~/auto_trader` (root) directly.

**Depends on (must already be merged):**
- ROB-168 — `min_consecutive_up_days` filter, `_enrich_consecutive_up_days(rows, market)`, `ScreenerFreshness` block. Already merged.
- ROB-170 first slice (commit `66efba66`) — `invest_screener_snapshots` table, repository, builder, CLIs, hydration helper, coverage endpoint. Already merged.
- ROB-170 fix (commit `8e65fb6d`, PR #772) — prefer-latest-snapshot logic in enrichment. Already merged.

**Out of scope (explicitly):**
- Recurring scheduler / Prefect / TaskIQ activation for the builder (separate ticket; requires its own approval after one or two days of operator-run smoke evidence).
- Other presets (`cheap_value`, `steady_dividend`, `oversold_recovery`, `high_volume_momentum`, `growth_expectation`).
- Crypto market — snapshots are KR + US equities only.
- React frontend changes (a `dataState` badge / "live" fallback messaging story is a separate UI follow-up; the API contract here is unchanged).
- Storing/exposing `weekChangeRate` as a display label in `ScreenerResultRow` — it's a filter-only signal in this slice (UI continues to render only `metricValueLabel`).

---

## Decision Log (locked before tasks begin)

### D1. Snapshot read path is wired at the view-model layer, not at the screening-service layer.

The simplest fix is to call `enrichment._enrich_consecutive_up_days(rows, market=market, session=session)` from `build_screener_results` AFTER `screening_service.list_screening` returns and BEFORE the per-row formatting loop. The screening service stays unchanged; we treat its output as the candidate pool and run a second snapshot-backed enrichment pass on the rows.

Rationale:
- `screening_service.list_screening` is reused by other callers (analysis MCP tools, refresh path) and threading `session` through it widens its coupling to FastAPI request scope.
- The existing `_hydrate_from_snapshots` helper is idempotent — calling it after `screen_stocks_impl` has already populated `consecutive_up_days` from on-demand OHLCV is a no-op for rows whose snapshot is missing (state="missing", no overwrite via `setdefault`) and a snapshot-source-of-truth refresh for rows whose snapshot is fresh/partial.
- This makes `_screener_snapshot_state` reach `aggregate_states`, so `dataState` becomes meaningful.

### D2. `week_change_rate >= 0` filter is enforced at the same post-enrichment layer as `min_consecutive_up_days`.

We add `min_week_change_rate: float | None = None` to:
- `normalize_screen_request` validation (must be ≥ -100.0, ≤ 1000.0; reject NaN/inf via existing `_to_optional_float`)
- `screener_service.list_screening` / `screener_service.refresh_screening` signatures + cache key
- `screen_stocks_impl` signature + filtering block (mirror of `_apply_min_consecutive_up_days` site)
- `screening_filters_for("consecutive_gainers", ...)` → adds `"min_week_change_rate": 0.0`

The filter helper `_apply_min_week_change_rate(rows, threshold)` drops rows where `week_change_rate is None or float(...) < threshold`. **Critical:** rows whose `week_change_rate` is `None` are dropped (consistent with `min_consecutive_up_days`'s "missing-is-not-eligible" rule) — Toss explicitly says "1주일 전 보다 · 0% 이상" which means we cannot vouch for a row whose week change we can't compute.

The filter consults already-hydrated `week_change_rate` from snapshots first (set by `_hydrate_from_snapshots` when state ∈ {fresh, partial}). When the snapshot is missing/stale, the existing on-demand OHLCV path in `_enrich_consecutive_up_days` already pulls 10 closes; we extend that helper to also compute `week_change_rate` from `closes[-5]` when available, mirroring `builder.derive_metrics`. Symbols whose 10-bar window has fewer than 5 closes get `week_change_rate=None` and are dropped by the filter.

### D3. CLI gains `--all` for full active-universe backfill; KR/US `is_active` aligned.

Today `_resolve_symbols`:
- KR path filters `is_active.is_(True)` (correct).
- US path does NOT filter `is_active` (bug — drift vs. coverage_service).
- Both paths use `--limit 20` default and have no "all" mode.

Changes:
- Add `--all` (mutually exclusive with `--limit` and `--symbol`). When set, iterate the full active universe in `--batch-size` chunks (default 200) with progress logging.
- Align US to filter `is_active.is_(True)`.
- Default `--limit` stays 20; default `--all` is False; default remains `--dry-run`.

### D4. `--commit` against production stays approval-gated.

Even after the CLI improvements land, no production commit happens automatically:
- Tasks 9–11 below run dry-run + coverage diagnostics and capture evidence.
- Task 12 is explicitly **GATED** — it only runs after a human reviewer responds with explicit "approved to commit" on the PR or in Linear, citing the dry-run evidence from Task 11.
- Implementer must NOT proactively run `--commit`. If the gating step is unclear at execution time, stop and ask.

### D5. `dataState` semantics after fix.

| Production state                              | Expected `freshness.dataState`              |
|-----------------------------------------------|---------------------------------------------|
| Table empty, on-demand fallback fills rows    | `missing` (matches today's response)        |
| All result-row symbols have fresh snapshots   | `fresh`                                     |
| Some result-row symbols missing, others fresh | `fallback`                                  |
| Snapshot rows exist but yesterday's date      | `stale`                                     |
| Snapshot exists but `closes_window` length 2-4| `partial` (week_change_rate not computable) |

After fix, an empty-table production response still reports `dataState="missing"` — that's correct and informative (operator knows backfill hasn't run). After successful backfill + filter wiring, the same response reports `fresh` or `partial` depending on closes_window completeness.

The `aggregate_states` function (`app/services/invest_screener_snapshots/freshness.py:61`) is already correct; we don't change it. We only ensure `_screener_snapshot_state` is set on every row before aggregation.

### D6. UI/API acceptance criteria (no React changes in this slice).

The contract is purely server-side:
1. `GET /invest/api/screener/results?presetId=consecutive_gainers&market=kr` returns rows that ALL satisfy `consecutive_up_days >= 5` AND `week_change_rate >= 0` (when filter wiring is correct, regardless of whether snapshots are populated — the filter operates on whatever data the enrichment passes through).
2. `GET /invest/api/screener/results?presetId=consecutive_gainers&market=us` same contract for US.
3. `freshness.dataState` reflects snapshot quality per D5.
4. `GET /invest/api/screener/snapshots/coverage?market=kr` returns `dataState="fresh"` and `snapshotsCoveringToday > 0` after backfill commit.
5. Existing `freshness.source` (`live | cached | previous_session`) is preserved — that field is independent of `dataState`.

The frontend "live" / "최근 데이터 기준" badging is a separate UI follow-up; it must not block this server-side hardening.

---

## File-by-File Plan

### Files to modify

| Path | Change |
|---|---|
| `app/mcp_server/tooling/screening/common.py` | Add `min_week_change_rate` to `normalize_screen_request` (with validation). Add `_apply_min_week_change_rate(rows, threshold)` helper next to `_apply_min_consecutive_up_days`. |
| `app/mcp_server/tooling/screening/enrichment.py` | Extend `_enrich_consecutive_up_days` to also populate `row["week_change_rate"]` from the OHLCV fallback path (when `len(closes) >= 5`). Snapshot path is unchanged. |
| `app/services/screener_service.py` | Accept `min_week_change_rate: float | None = None` in `list_screening` + `refresh_screening` signatures. Pass through `normalize_screen_request`, include in `filters` dict + cache key, in `normalized_filters_applied`, and in the `refresh_screening` → `list_screening` re-call. |
| `app/mcp_server/tooling/analysis_tool_handlers.py` | Accept `min_week_change_rate` in `screen_stocks_impl`. Pass through `normalize_screen_request`. After existing `_apply_min_consecutive_up_days` block, add a parallel `_apply_min_week_change_rate` block. |
| `app/services/invest_view_model/screener_presets.py` | Add `"min_week_change_rate": 0.0` to `consecutive_gainers` filter dict. |
| `app/services/invest_view_model/screener_service.py` | In `build_screener_results`, after `raw = await screening_service.list_screening(**filters)` and before the result-row loop, when `session is not None and requested_market in {"kr", "us"} and preset_id == "consecutive_gainers"`: import the async `_enrich_consecutive_up_days` from `app.mcp_server.tooling.screening.enrichment` and call it with `(rows, market=requested_market, session=session)` so `_screener_snapshot_state` is populated before `aggregate_states`. |
| `scripts/build_invest_screener_snapshots.py` | (a) Apply `is_active.is_(True)` to US symbol query. (b) Add `--all` and `--batch-size` flags; in `--all` mode, iterate active universe in chunks via `build_snapshots_for_market` with progress logging and per-batch `await session.commit()` (only when `--commit`). |

### Files to create

| Path | Purpose |
|---|---|
| `tests/test_invest_screener_snapshots_followup_wiring.py` | Cover D1: `build_screener_results` populates `_screener_snapshot_state` and produces non-`missing` `dataState` when snapshots are present and `session` is supplied. |
| `tests/test_invest_screener_week_change_filter.py` | Cover D2: `_apply_min_week_change_rate` drops `None`/`<threshold` rows; `consecutive_gainers` preset filter dict carries `min_week_change_rate=0.0`; `screen_stocks_impl` end-to-end drops a row with `week_change_rate=-2.5`. |
| `tests/test_build_invest_screener_snapshots_full_universe.py` | Cover D3: `--all` flag iterates the full active universe in batches; US path now filters `is_active`; `--all` is mutually exclusive with `--symbol`/`--limit`. |

### Files NOT touched (intentionally)

| Path | Why |
|---|---|
| `app/models/invest_screener_snapshot.py` | Schema is correct; `week_change_rate` already stored as `Numeric(10,4)`. |
| `app/services/invest_screener_snapshots/repository.py` | `get_fresh` already returns `week_change_rate`; no change needed. |
| `app/services/invest_screener_snapshots/builder.py` | Already computes and persists `week_change_rate`. |
| `app/services/invest_screener_snapshots/freshness.py` | `classify_state` and `aggregate_states` are correct; we only ensure they get called. |
| `app/services/invest_screener_snapshots/coverage_service.py` | Already correct; will report `dataState=fresh` after backfill. |
| `app/routers/invest_api.py` | `session=db` is already threaded; we just make it useful in the view-model. |
| `alembic/versions/82309c07b8a2_add_invest_screener_snapshots.py` | No schema change. |
| `app/schemas/invest_screener.py` | `dataState` field already exists; no DTO change. |

---

## Tasks

### Task 1: Add `_apply_min_week_change_rate` filter helper + validation

**Files:**
- Modify: `app/mcp_server/tooling/screening/common.py`
- Test: `tests/test_invest_screener_week_change_filter.py`

- [ ] **Step 1: Write the failing test for the helper**

Create `tests/test_invest_screener_week_change_filter.py` with:

```python
"""ROB-170 follow-up — week_change_rate >= 0 Toss filter."""

from __future__ import annotations

import pytest


@pytest.mark.unit
def test_apply_min_week_change_rate_drops_rows_below_threshold() -> None:
    from app.mcp_server.tooling.screening.common import (
        _apply_min_week_change_rate,
    )

    rows: list[dict] = [
        {"symbol": "A", "week_change_rate": 1.5},      # keep
        {"symbol": "B", "week_change_rate": 0.0},      # keep (>= 0)
        {"symbol": "C", "week_change_rate": -0.01},    # drop
        {"symbol": "D", "week_change_rate": None},     # drop (unknown)
        {"symbol": "E"},                                # drop (missing)
        {"symbol": "F", "week_change_rate": "2.3"},    # keep (string-coerced)
    ]
    out = _apply_min_week_change_rate(rows, threshold=0.0)
    assert [r["symbol"] for r in out] == ["A", "B", "F"]


@pytest.mark.unit
def test_apply_min_week_change_rate_passthrough_when_threshold_none() -> None:
    from app.mcp_server.tooling.screening.common import (
        _apply_min_week_change_rate,
    )

    rows = [{"symbol": "A", "week_change_rate": -10.0}]
    assert _apply_min_week_change_rate(rows, threshold=None) == rows
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_invest_screener_week_change_filter.py::test_apply_min_week_change_rate_drops_rows_below_threshold -v`
Expected: FAIL with `ImportError: cannot import name '_apply_min_week_change_rate'`.

- [ ] **Step 3: Add the helper to `common.py`**

In `app/mcp_server/tooling/screening/common.py`, immediately below the existing `_apply_min_consecutive_up_days` (around line 996), add:

```python
def _apply_min_week_change_rate(
    rows: list[dict[str, Any]], *, threshold: float | None
) -> list[dict[str, Any]]:
    if threshold is None:
        return rows
    out: list[dict[str, Any]] = []
    for row in rows:
        value = _to_optional_float(row.get("week_change_rate"))
        if value is None:
            continue
        if value < threshold:
            continue
        out.append(row)
    return out
```

`_to_optional_float` is already imported / defined in this module (used elsewhere); confirm with `grep -n "def _to_optional_float" app/mcp_server/tooling/screening/common.py` before editing — if it lives in `screening.common` itself, no import is needed; otherwise add `from app.mcp_server.tooling.screening.common import _to_optional_float` inside the helper or at module top.

- [ ] **Step 4: Run helper tests; expect PASS**

Run: `uv run pytest tests/test_invest_screener_week_change_filter.py -v -k "apply_min_week_change_rate"`
Expected: 2 passed.

- [ ] **Step 5: Add `min_week_change_rate` parameter to `normalize_screen_request`**

In `app/mcp_server/tooling/screening/common.py:456`, extend `normalize_screen_request`:

```python
def normalize_screen_request(
    *,
    market: str,
    # ... existing params ...
    min_consecutive_up_days: int | None = None,
    min_week_change_rate: float | None = None,
) -> dict[str, Any]:
    # ... existing body ...
    if min_consecutive_up_days is not None:
        if not (1 <= min_consecutive_up_days <= 30):
            raise ValueError("min_consecutive_up_days must be between 1 and 30")
    if min_week_change_rate is not None:
        try:
            mwcr = float(min_week_change_rate)
        except (TypeError, ValueError) as exc:
            raise ValueError("min_week_change_rate must be a finite number") from exc
        if mwcr != mwcr or mwcr in (float("inf"), float("-inf")):
            raise ValueError("min_week_change_rate must be a finite number")
        if not (-100.0 <= mwcr <= 1000.0):
            raise ValueError("min_week_change_rate must be between -100.0 and 1000.0")
        min_week_change_rate = mwcr
    # ... at the return dict, add:
    return {
        # ... existing keys ...
        "min_consecutive_up_days": min_consecutive_up_days,
        "min_week_change_rate": min_week_change_rate,
    }
```

- [ ] **Step 6: Add validation tests**

Append to `tests/test_invest_screener_week_change_filter.py`:

```python
@pytest.mark.unit
def test_normalize_screen_request_rejects_non_finite_week_change_rate() -> None:
    from app.mcp_server.tooling.screening.common import normalize_screen_request

    with pytest.raises(ValueError, match="finite"):
        normalize_screen_request(
            market="kr", asset_type="stock", category=None, sector=None,
            strategy=None, sort_by=None, sort_order="desc",
            min_market_cap=None, max_per=None, max_pbr=None,
            min_dividend_yield=None, min_dividend=None, min_analyst_buy=None,
            max_rsi=None, limit=20, min_week_change_rate=float("inf"),
        )


@pytest.mark.unit
def test_normalize_screen_request_accepts_zero_threshold() -> None:
    from app.mcp_server.tooling.screening.common import normalize_screen_request

    out = normalize_screen_request(
        market="kr", asset_type="stock", category=None, sector=None,
        strategy=None, sort_by=None, sort_order="desc",
        min_market_cap=None, max_per=None, max_pbr=None,
        min_dividend_yield=None, min_dividend=None, min_analyst_buy=None,
        max_rsi=None, limit=20, min_week_change_rate=0.0,
    )
    assert out["min_week_change_rate"] == 0.0
```

- [ ] **Step 7: Run validation tests; expect PASS**

Run: `uv run pytest tests/test_invest_screener_week_change_filter.py -v`
Expected: 4 passed.

- [ ] **Step 8: Commit**

```bash
git add app/mcp_server/tooling/screening/common.py \
        tests/test_invest_screener_week_change_filter.py
git commit -m "$(cat <<'EOF'
feat(ROB-170): add min_week_change_rate filter + validation

Toss screener parity: ``consecutive_gainers`` chip
"1주일 전 보다 · 0% 이상" must drop rows whose week_change_rate is
unknown or negative. Adds the post-enrichment helper symmetric to
``_apply_min_consecutive_up_days`` plus normalize_screen_request
validation. Wiring into screening service / preset comes in
follow-up tasks.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Thread `min_week_change_rate` through `screener_service` and `screen_stocks_impl`

**Files:**
- Modify: `app/services/screener_service.py:328-472, 474-556`
- Modify: `app/mcp_server/tooling/analysis_tool_handlers.py:531-692`
- Test: `tests/test_invest_screener_week_change_filter.py`

- [ ] **Step 1: Write the failing end-to-end filter test**

Append to `tests/test_invest_screener_week_change_filter.py`:

```python
@pytest.mark.asyncio
async def test_screen_stocks_impl_drops_negative_week_change(monkeypatch) -> None:
    from app.mcp_server.tooling import analysis_tool_handlers as handlers
    from app.mcp_server.tooling import analysis_screening

    fake_rows = [
        {"market": "kr", "code": "A", "consecutive_up_days": 6, "week_change_rate": 1.2},
        {"market": "kr", "code": "B", "consecutive_up_days": 5, "week_change_rate": -0.5},
        {"market": "kr", "code": "C", "consecutive_up_days": 7, "week_change_rate": None},
        {"market": "kr", "code": "D", "consecutive_up_days": 5, "week_change_rate": 0.0},
    ]

    async def fake_unified(**kwargs):
        return {"results": list(fake_rows), "total_count": 4, "filters_applied": {}}

    monkeypatch.setattr(analysis_screening, "screen_stocks_unified", fake_unified)

    async def fake_enrich(rows, *, market, session=None, lookback=10):
        return None

    monkeypatch.setattr(
        "app.mcp_server.tooling.screening.enrichment._enrich_consecutive_up_days",
        fake_enrich,
    )

    result = await handlers.screen_stocks_impl(
        market="kr",
        asset_type="stock",
        sort_by="change_rate",
        sort_order="desc",
        min_consecutive_up_days=5,
        min_week_change_rate=0.0,
        limit=20,
    )
    codes = [r["code"] for r in result["results"]]
    assert codes == ["A", "D"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_invest_screener_week_change_filter.py::test_screen_stocks_impl_drops_negative_week_change -v`
Expected: FAIL with `TypeError: screen_stocks_impl() got an unexpected keyword argument 'min_week_change_rate'`.

- [ ] **Step 3: Add `min_week_change_rate` to `screen_stocks_impl`**

In `app/mcp_server/tooling/analysis_tool_handlers.py:531`, add the parameter to `screen_stocks_impl`'s signature (place it next to `min_consecutive_up_days`), pass it to `normalize_screen_request`, and add this block immediately after the existing `if min_consecutive_up_days is not None:` block (around line 692):

```python
    if min_week_change_rate is not None:
        from app.mcp_server.tooling.screening.common import (
            _apply_min_week_change_rate,
        )

        rows: list[dict[str, Any]] = list(result.get("results") or [])
        rows = _apply_min_week_change_rate(rows, threshold=float(min_week_change_rate))
        filters_applied = dict(result.get("filters_applied") or {})
        if filters_applied:
            filters_applied["limit"] = limit
        result = {
            **result,
            "filters_applied": filters_applied or result.get("filters_applied"),
            "results": rows[:limit],
            "total_count": len(rows),
        }
```

(Note: `_apply_min_week_change_rate` does NOT need to call `_enrich_consecutive_up_days` again — when `min_consecutive_up_days` is set, that call already happened upstream and populated `week_change_rate` for snapshot-backed rows. When only `min_week_change_rate` is set without streak filter, we still need a one-shot enrichment pass — implement by re-using the existing block: refactor the existing `if min_consecutive_up_days is not None` block to instead be `if min_consecutive_up_days is not None or min_week_change_rate is not None` so the enrichment pass runs once when either filter is active, then apply both filters in sequence.)

The cleanest refactor of the block:

```python
    if min_consecutive_up_days is not None or min_week_change_rate is not None:
        from app.mcp_server.tooling.screening.common import (
            _apply_min_consecutive_up_days,
            _apply_min_week_change_rate,
        )
        from app.mcp_server.tooling.screening.enrichment import (
            _enrich_consecutive_up_days,
        )

        rows: list[dict[str, Any]] = list(result.get("results") or [])
        await _enrich_consecutive_up_days(rows, market=normalized_market)
        if min_consecutive_up_days is not None:
            rows = _apply_min_consecutive_up_days(rows, threshold=min_consecutive_up_days)
        if min_week_change_rate is not None:
            rows = _apply_min_week_change_rate(rows, threshold=float(min_week_change_rate))
        filters_applied = dict(result.get("filters_applied") or {})
        if filters_applied:
            filters_applied["limit"] = limit
        result = {
            **result,
            "filters_applied": filters_applied or result.get("filters_applied"),
            "results": rows[:limit],
            "total_count": len(rows),
        }
```

- [ ] **Step 4: Add `min_week_change_rate` to `screener_service.list_screening`**

In `app/services/screener_service.py:328`, add `min_week_change_rate: float | None = None` to `list_screening`'s signature next to `min_consecutive_up_days`. Pass it to `normalize_screen_request`. Include `"min_week_change_rate": normalized_request["min_week_change_rate"]` in the `filters` dict (line 372-389) so it's part of the cache key. Add it to `call_kwargs` filtering (line 395-399) — do NOT exclude it. Add `normalized_filters_applied.setdefault("min_week_change_rate", normalized_request["min_week_change_rate"])` next to the existing `min_consecutive_up_days` setdefault (line 446-448). Repeat the same changes in `refresh_screening` (line 474) including the `filters` dict and the re-call into `list_screening`.

- [ ] **Step 5: Run end-to-end test; expect PASS**

Run: `uv run pytest tests/test_invest_screener_week_change_filter.py -v`
Expected: 5 passed.

- [ ] **Step 6: Run full screener test suite to catch regressions**

Run: `uv run pytest tests/test_screener_service.py tests/test_screening_consecutive_up_days.py tests/test_invest_screener_presets.py tests/test_invest_api_screener_router.py -v`
Expected: all green.

- [ ] **Step 7: Commit**

```bash
git add app/mcp_server/tooling/analysis_tool_handlers.py \
        app/services/screener_service.py \
        tests/test_invest_screener_week_change_filter.py
git commit -m "$(cat <<'EOF'
feat(ROB-170): wire min_week_change_rate through screener service

screen_stocks_impl, list_screening, and refresh_screening now accept
min_week_change_rate. The post-enrichment block applies both streak
and week-change filters in a single pass so on-demand OHLCV is fetched
at most once per request.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Apply `min_week_change_rate=0.0` to the `consecutive_gainers` preset

**Files:**
- Modify: `app/services/invest_view_model/screener_presets.py:94-143`
- Test: `tests/test_invest_screener_week_change_filter.py`

- [ ] **Step 1: Write the failing preset test**

Append to `tests/test_invest_screener_week_change_filter.py`:

```python
@pytest.mark.unit
def test_consecutive_gainers_preset_includes_week_change_filter() -> None:
    from app.services.invest_view_model.screener_presets import (
        screening_filters_for,
    )

    kr_filters = screening_filters_for("consecutive_gainers", market="kr")
    assert kr_filters["min_consecutive_up_days"] == 5
    assert kr_filters["min_week_change_rate"] == 0.0

    us_filters = screening_filters_for("consecutive_gainers", market="us")
    assert us_filters["min_consecutive_up_days"] == 5
    assert us_filters["min_week_change_rate"] == 0.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_invest_screener_week_change_filter.py::test_consecutive_gainers_preset_includes_week_change_filter -v`
Expected: FAIL with `KeyError: 'min_week_change_rate'`.

- [ ] **Step 3: Add the filter to the preset config**

In `app/services/invest_view_model/screener_presets.py:95`, update the `consecutive_gainers` filter dict:

```python
    "consecutive_gainers": {
        "market": "kr",
        "asset_type": "stock",
        "sort_by": "change_rate",
        "sort_order": "desc",
        "min_consecutive_up_days": 5,
        "min_week_change_rate": 0.0,
        "limit": 20,
    },
```

- [ ] **Step 4: Run test; expect PASS**

Run: `uv run pytest tests/test_invest_screener_week_change_filter.py::test_consecutive_gainers_preset_includes_week_change_filter -v`
Expected: 1 passed.

- [ ] **Step 5: Verify the chip text already says "1주일 전 보다 · 0% 이상"**

Run: `grep -n "1주일 전" app/services/invest_view_model/screener_presets.py`
Expected: `app/services/invest_view_model/screener_presets.py:23:            ScreenerFilterChip(label="주가등락률", detail="1주일 전 보다 · 0% 이상"),`
The chip and the underlying filter now match.

- [ ] **Step 6: Commit**

```bash
git add app/services/invest_view_model/screener_presets.py \
        tests/test_invest_screener_week_change_filter.py
git commit -m "$(cat <<'EOF'
feat(ROB-170): consecutive_gainers preset enforces week_change >= 0

Aligns the preset's filter dict with its existing user-facing chip
"1주일 전 보다 · 0% 이상". Both KR and US variants now drop rows whose
week_change_rate is unknown or negative.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Extend on-demand enrichment to also populate `week_change_rate`

**Files:**
- Modify: `app/mcp_server/tooling/screening/enrichment.py:67-104`
- Test: `tests/test_invest_screener_snapshots_enrichment.py`

When `_hydrate_from_snapshots` does not populate `week_change_rate` (snapshot missing/stale), the on-demand OHLCV path needs to fill it from the same 10-bar window it already fetches; otherwise rows that pass `min_consecutive_up_days` but have no snapshot will be unfairly dropped by `_apply_min_week_change_rate`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_invest_screener_snapshots_enrichment.py`:

```python
@pytest.mark.asyncio
async def test_enrichment_fallback_populates_week_change_rate(monkeypatch):
    """When snapshot is missing, the OHLCV fallback must also fill week_change_rate."""
    import pandas as pd
    from unittest.mock import AsyncMock

    from app.mcp_server.tooling.screening import enrichment

    fake_df = pd.DataFrame(
        {
            "date": pd.date_range("2026-05-01", periods=6, freq="B"),
            "close": [100.0, 101.0, 102.0, 103.0, 104.0, 105.0],
        }
    )
    fetcher = AsyncMock(return_value=fake_df)
    monkeypatch.setattr(enrichment, "_fetch_ohlcv_for_indicators", fetcher)

    rows = [{"market": "kr", "code": "900099"}]
    await enrichment._enrich_consecutive_up_days(rows, market="kr", session=None)
    assert rows[0]["consecutive_up_days"] == 5
    assert rows[0]["week_change_rate"] == pytest.approx((105.0 - 101.0) / 101.0 * 100.0, rel=1e-6)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_invest_screener_snapshots_enrichment.py::test_enrichment_fallback_populates_week_change_rate -v`
Expected: FAIL — `week_change_rate` is not set.

- [ ] **Step 3: Extend the OHLCV fallback in `_enrich_consecutive_up_days`**

In `app/mcp_server/tooling/screening/enrichment.py:84-103`, replace the fallback body with:

```python
    async def _enrich_one(row: dict[str, Any]) -> None:
        already_have_streak = row.get("consecutive_up_days") is not None
        already_have_week = row.get("week_change_rate") is not None
        if already_have_streak and already_have_week:
            return
        symbol = _streak_symbol(row)
        if not symbol:
            return
        async with sem:
            try:
                df = await _fetch_ohlcv_for_indicators(
                    symbol, market_type, count=lookback
                )
            except Exception:
                return
        if df is None or df.empty or "close" not in df.columns:
            return
        closes = [float(c) for c in df["close"].tolist() if c is not None]
        if len(closes) < 2:
            return
        if not already_have_streak:
            streak = calculate_consecutive_up_days(closes)
            if streak is not None:
                row["consecutive_up_days"] = streak
        if not already_have_week and len(closes) >= 5:
            base = closes[-5]
            if base != 0:
                row["week_change_rate"] = (closes[-1] - base) / base * 100.0
```

- [ ] **Step 4: Run test; expect PASS**

Run: `uv run pytest tests/test_invest_screener_snapshots_enrichment.py::test_enrichment_fallback_populates_week_change_rate -v`
Expected: 1 passed.

- [ ] **Step 5: Run full enrichment suite**

Run: `uv run pytest tests/test_invest_screener_snapshots_enrichment.py -v`
Expected: all green (no regressions on existing 4-bar / mocked-snapshot scenarios).

- [ ] **Step 6: Commit**

```bash
git add app/mcp_server/tooling/screening/enrichment.py \
        tests/test_invest_screener_snapshots_enrichment.py
git commit -m "$(cat <<'EOF'
feat(ROB-170): on-demand OHLCV fallback populates week_change_rate

Makes the snapshot-fallback enrichment path symmetric with the
snapshot path so min_week_change_rate filtering is fair to symbols
whose snapshot is stale or missing. Pulls the same 10-bar window
already used for streak; only computes the metric when len(closes)
>= 5.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: Wire snapshot-hydration into `build_screener_results`

**Files:**
- Modify: `app/services/invest_view_model/screener_service.py:332-447`
- Test: `tests/test_invest_screener_snapshots_followup_wiring.py`

This is the most important task — it makes `dataState` actually mean something.

- [ ] **Step 1: Write the failing wiring test**

Create `tests/test_invest_screener_snapshots_followup_wiring.py`:

```python
"""ROB-170 follow-up — verify build_screener_results threads session into snapshot hydration."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from app.services.invest_screener_snapshots.repository import (
    InvestScreenerSnapshotsRepository,
    SnapshotUpsert,
)
from app.services.invest_view_model.screener_service import build_screener_results


class _StubResolver:
    def relation(self, market: str, symbol: str) -> str:
        return "neither"


class _StubScreeningService:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    async def list_screening(self, **kwargs):
        return {
            "results": list(self._rows),
            "total_count": len(self._rows),
            "filters_applied": kwargs,
            "timestamp": "2026-05-10T05:00:00+00:00",
            "cache_hit": False,
        }


@pytest.mark.asyncio
async def test_build_screener_results_data_state_fresh_when_snapshot_present(
    db_session, monkeypatch
):
    """When snapshots exist for all candidate symbols, dataState=='fresh'."""
    repo = InvestScreenerSnapshotsRepository(db_session)
    today = dt.date(2026, 5, 8)  # most recent KR business day before 2026-05-10 (Sunday)

    for symbol in ["005930", "000660"]:
        await repo.upsert(
            SnapshotUpsert(
                market="kr",
                symbol=symbol,
                snapshot_date=today,
                latest_close=Decimal("70000"),
                prev_close=Decimal("69000"),
                change_amount=Decimal("1000"),
                change_rate=Decimal("1.45"),
                consecutive_up_days=6,
                week_change_rate=Decimal("3.5"),
                closes_window=[68000, 68500, 69000, 69500, 70000, 70000],
                source="kis",
            )
        )
    await db_session.commit()

    monkeypatch.setattr(
        "app.services.invest_screener_snapshots.freshness.today_trading_date",
        lambda market, now=None: today,
    )

    rows = [
        {"market": "kr", "code": "005930", "consecutive_up_days": 6,
         "change_rate": 1.45, "close": 70000},
        {"market": "kr", "code": "000660", "consecutive_up_days": 6,
         "change_rate": 1.45, "close": 70000},
    ]
    service = _StubScreeningService(rows)

    result = await build_screener_results(
        preset_id="consecutive_gainers",
        screening_service=service,
        resolver=_StubResolver(),
        market="kr",
        session=db_session,
    )
    assert result.freshness.dataState == "fresh"


@pytest.mark.asyncio
async def test_build_screener_results_data_state_missing_without_snapshots(
    db_session, monkeypatch
):
    """When no snapshots exist, dataState=='missing' even though session is supplied."""
    rows = [{"market": "kr", "code": "999999", "consecutive_up_days": 6}]
    service = _StubScreeningService(rows)

    result = await build_screener_results(
        preset_id="consecutive_gainers",
        screening_service=service,
        resolver=_StubResolver(),
        market="kr",
        session=db_session,
    )
    assert result.freshness.dataState == "missing"


@pytest.mark.asyncio
async def test_build_screener_results_data_state_missing_when_no_session():
    """When session is None, hydration is skipped and dataState defaults to 'missing'."""
    rows = [{"market": "kr", "code": "005930", "consecutive_up_days": 6}]
    service = _StubScreeningService(rows)

    result = await build_screener_results(
        preset_id="consecutive_gainers",
        screening_service=service,
        resolver=_StubResolver(),
        market="kr",
        session=None,
    )
    assert result.freshness.dataState == "missing"
```

- [ ] **Step 2: Run failing test**

Run: `uv run pytest tests/test_invest_screener_snapshots_followup_wiring.py::test_build_screener_results_data_state_fresh_when_snapshot_present -v`
Expected: FAIL — `result.freshness.dataState == "missing"` because hydration never runs.

- [ ] **Step 3: Wire hydration into `build_screener_results`**

In `app/services/invest_view_model/screener_service.py:332`, modify the function. Locate the block at line 360-379 (where rows are extracted from `raw` and `_aggregated_data_state` is computed) and INSERT a hydration call between row extraction and aggregation:

```python
    raw = await screening_service.list_screening(**filters)
    rows: list[dict[str, Any]] = list(raw.get("results") or raw.get("stocks") or [])
    upstream_warnings: list[str] = list(raw.get("warnings") or [])

    # ROB-170 follow-up: snapshot-first hydration runs at the view-model layer so
    # the session reaches _enrich_consecutive_up_days. Without this call the
    # screening service path never sees the session and _screener_snapshot_state
    # is never populated, leaving dataState pinned at "missing".
    if (
        session is not None
        and requested_market in {"kr", "us"}
        and preset_id == "consecutive_gainers"
        and rows
    ):
        from app.mcp_server.tooling.screening.enrichment import (
            _enrich_consecutive_up_days as _async_enrich,
        )

        await _async_enrich(rows, market=requested_market, session=session)

    # Aggregate snapshot dataState from enriched rows ...
    from app.services.invest_screener_snapshots.freshness import aggregate_states
    _row_states: list[str] = [
        str(r.get("_screener_snapshot_state") or "missing") for r in rows
    ]
    _aggregated_data_state = aggregate_states(_row_states)
    # ... unchanged below
```

(Note: the local sync `_enrich_consecutive_up_days` at line 239 is a different function — it computes streak from `daily_closes` field already on the row. Don't remove it; the rename-on-import alias `_async_enrich` keeps both visible without shadowing.)

- [ ] **Step 4: Run wiring tests; expect PASS**

Run: `uv run pytest tests/test_invest_screener_snapshots_followup_wiring.py -v`
Expected: 3 passed.

- [ ] **Step 5: Run the full view-model + screener-router test suites**

Run: `uv run pytest tests/test_invest_api_screener_router.py tests/test_screener_service.py -v`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add app/services/invest_view_model/screener_service.py \
        tests/test_invest_screener_snapshots_followup_wiring.py
git commit -m "$(cat <<'EOF'
fix(ROB-170): wire snapshot hydration into /invest/screener path

build_screener_results now calls the async snapshot-aware
_enrich_consecutive_up_days with the request session so
_screener_snapshot_state is populated before aggregate_states.
Without this, freshness.dataState was pinned at "missing" in
production even when the snapshot table was healthy. Existing
"missing"-when-table-empty behavior is preserved.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: Add `--all` and align `is_active` to the build CLI

**Files:**
- Modify: `scripts/build_invest_screener_snapshots.py`
- Test: `tests/test_build_invest_screener_snapshots_full_universe.py`

- [ ] **Step 1: Write the failing CLI test**

Create `tests/test_build_invest_screener_snapshots_full_universe.py`:

```python
"""ROB-170 follow-up — full-universe backfill flag and is_active alignment."""

from __future__ import annotations

import pytest

from scripts.build_invest_screener_snapshots import parse_args


@pytest.mark.unit
def test_parse_args_all_flag_defaults_false():
    args = parse_args(["--market", "kr"])
    assert args.all is False
    assert args.batch_size == 200
    assert args.dry_run is True


@pytest.mark.unit
def test_parse_args_all_overrides_limit():
    args = parse_args(["--market", "kr", "--all"])
    assert args.all is True


@pytest.mark.unit
def test_parse_args_all_with_symbol_rejected():
    with pytest.raises(SystemExit):
        parse_args(["--market", "kr", "--all", "--symbol", "005930"])


@pytest.mark.unit
def test_parse_args_all_with_explicit_limit_rejected():
    with pytest.raises(SystemExit):
        parse_args(["--market", "kr", "--all", "--limit", "100"])


@pytest.mark.asyncio
async def test_us_resolver_filters_active(monkeypatch, db_session):
    """US universe iteration must filter is_active=True (alignment with KR + coverage)."""
    from datetime import date

    from app.models.us_symbol_universe import USSymbolUniverse
    from scripts.build_invest_screener_snapshots import _resolve_symbols

    db_session.add(USSymbolUniverse(symbol="AAPL", name_en="Apple", is_active=True))
    db_session.add(USSymbolUniverse(symbol="OBSO", name_en="Obsolete", is_active=False))
    await db_session.commit()

    # _resolve_symbols opens its own session via AsyncSessionLocal. We override the
    # factory to return our test session.
    monkeypatch.setattr(
        "scripts.build_invest_screener_snapshots.AsyncSessionLocal",
        lambda: _AsyncCtx(db_session),
    )
    out = await _resolve_symbols(market="us", override=[], limit=10)
    assert "AAPL" in out
    assert "OBSO" not in out


class _AsyncCtx:
    def __init__(self, session): self._session = session
    async def __aenter__(self): return self._session
    async def __aexit__(self, *a): return False
```

- [ ] **Step 2: Run failing CLI tests**

Run: `uv run pytest tests/test_build_invest_screener_snapshots_full_universe.py -v`
Expected: FAIL — `--all` and `--batch-size` flags don't exist; US resolver does not filter is_active.

- [ ] **Step 3: Modify `scripts/build_invest_screener_snapshots.py`**

a) Add flags in `parse_args` (line 37-61):

```python
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-mostly invest_screener_snapshots builder (ROB-170)."
    )
    parser.add_argument("--market", choices=["kr", "us"], required=True)
    parser.add_argument(
        "--symbol", action="append", default=[],
        help="Restrict to specific symbols. Can be passed multiple times.",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Max active universe symbols to process. Defaults to 20 unless --all.",
    )
    parser.add_argument(
        "--all", action="store_true",
        help="Iterate the full active universe in --batch-size chunks. Mutually "
             "exclusive with --symbol/--limit.",
    )
    parser.add_argument(
        "--batch-size", type=int, default=200,
        help="Symbols per processing batch when --all is set (default 200).",
    )
    parser.add_argument(
        "--commit", action="store_true",
        help="Actually write to the database. Default is --dry-run.",
    )
    parser.add_argument(
        "--concurrency", type=int, default=4, help="Per-symbol fetch concurrency."
    )
    args = parser.parse_args(argv)
    if args.all and (args.symbol or args.limit is not None):
        parser.error("--all is mutually exclusive with --symbol and --limit")
    if args.limit is None:
        args.limit = 20
    args.dry_run = not args.commit
    return args
```

b) Filter US resolver by `is_active.is_(True)` (line 76-82):

```python
        else:
            from app.models.us_symbol_universe import USSymbolUniverse
            stmt = (
                sa.select(USSymbolUniverse.symbol)
                .where(USSymbolUniverse.is_active.is_(True))
                .order_by(USSymbolUniverse.symbol)
                .limit(limit)
            )
```

c) Implement `--all` batched iteration in `run` (line 87):

```python
async def _resolve_active_universe(market: str) -> list[str]:
    async with AsyncSessionLocal() as session:
        if market == "kr":
            from app.models.kr_symbol_universe import KRSymbolUniverse
            stmt = (
                sa.select(KRSymbolUniverse.symbol)
                .where(KRSymbolUniverse.is_active.is_(True))
                .order_by(KRSymbolUniverse.symbol)
            )
        else:
            from app.models.us_symbol_universe import USSymbolUniverse
            stmt = (
                sa.select(USSymbolUniverse.symbol)
                .where(USSymbolUniverse.is_active.is_(True))
                .order_by(USSymbolUniverse.symbol)
            )
        result = await session.execute(stmt)
        return [r[0] for r in result.all()]


async def run(args: argparse.Namespace) -> int:
    today = datetime.now(UTC).date()
    if args.all:
        symbols = await _resolve_active_universe(args.market)
        logger.info("resolved %d symbols for FULL %s universe", len(symbols), args.market)
        total_built = 0
        for batch_idx, start in enumerate(range(0, len(symbols), args.batch_size)):
            batch = symbols[start:start + args.batch_size]
            payloads = await build_snapshots_for_market(
                market=args.market, symbols=batch, today=today,
                concurrency=args.concurrency,
            )
            if not args.dry_run:
                async with AsyncSessionLocal() as session:
                    repo = InvestScreenerSnapshotsRepository(session)
                    for p in payloads:
                        await repo.upsert(p)
                    await session.commit()
            total_built += len(payloads)
            print(
                f"  batch {batch_idx + 1}: built {len(payloads)}/{len(batch)} "
                f"(running total {total_built}/{len(symbols)}) dry_run={args.dry_run}"
            )
        print(f"\nbuilt {total_built}/{len(symbols)} snapshots, "
              f"committed={'no' if args.dry_run else 'yes'}\n")
        return 0

    # Non-`--all` path is unchanged
    symbols = await _resolve_symbols(args.market, args.symbol, args.limit)
    logger.info("resolved %d symbols for market=%s", len(symbols), args.market)
    payloads = await build_snapshots_for_market(
        market=args.market, symbols=symbols, today=today, concurrency=args.concurrency
    )
    print(
        f"\nbuilt {len(payloads)}/{len(symbols)} snapshots "
        f"(market={args.market}, dry_run={args.dry_run}):"
    )
    for p in payloads[:10]:
        print(
            f"  {p.market}:{p.symbol} {p.snapshot_date} "
            f"close={p.latest_close} streak={p.consecutive_up_days} "
            f"week={p.week_change_rate}"
        )
    if len(payloads) > 10:
        print(f"  ... ({len(payloads) - 10} more)")
    if args.dry_run:
        print("\n--dry-run: no rows written.\n")
        return 0
    async with AsyncSessionLocal() as session:
        repo = InvestScreenerSnapshotsRepository(session)
        for p in payloads:
            await repo.upsert(p)
        await session.commit()
    print(f"\ncommitted {len(payloads)} rows.\n")
    return 0
```

- [ ] **Step 4: Run CLI tests; expect PASS**

Run: `uv run pytest tests/test_build_invest_screener_snapshots_full_universe.py -v tests/test_build_invest_screener_snapshots_cli.py -v`
Expected: all green.

- [ ] **Step 5: Smoke the `--help` output**

Run: `uv run python -m scripts.build_invest_screener_snapshots --help`
Expected: shows `--all`, `--batch-size`, `--limit`, `--symbol`, `--commit`, `--concurrency`, with the mutual-exclusion note.

- [ ] **Step 6: Commit**

```bash
git add scripts/build_invest_screener_snapshots.py \
        tests/test_build_invest_screener_snapshots_full_universe.py
git commit -m "$(cat <<'EOF'
feat(ROB-170): full-universe backfill via --all + align US is_active

Adds --all (mutually exclusive with --symbol/--limit) and
--batch-size for backfilling the entire active KR/US universe in
chunks. US resolver now filters is_active.is_(True) to match KR and
coverage_service. --dry-run remains default; --commit still required
for writes.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: Update the snapshot runbook with the follow-up workflow

**Files:**
- Modify: `docs/runbooks/invest-screener-snapshots.md`

- [ ] **Step 1: Update the runbook**

Replace the "Build snapshots" section (around line 13-33) with:

```markdown
### Build snapshots (default: dry-run, no writes)

```bash
# KR — preview top 20 active universe symbols
uv run python -m scripts.build_invest_screener_snapshots --market kr --limit 20

# KR — full active universe, dry-run (RECOMMENDED before any --commit)
uv run python -m scripts.build_invest_screener_snapshots --market kr --all

# KR — full active universe, persist (REQUIRES OPERATOR APPROVAL)
uv run python -m scripts.build_invest_screener_snapshots --market kr --all --commit

# US — full active universe, persist
uv run python -m scripts.build_invest_screener_snapshots --market us --all --commit

# Specific symbols (small surgical refresh)
uv run python -m scripts.build_invest_screener_snapshots \
    --market kr --symbol 005930 --symbol 000660 --commit
```

`--dry-run` (default) prints payloads without writing. `--commit` persists rows
via `INSERT ON CONFLICT DO UPDATE`. `--all` iterates the full active universe in
`--batch-size` chunks (default 200), committing per batch when `--commit` is set.
`--all` is mutually exclusive with `--symbol` and `--limit`.

**Operator approval gating:** never run `--all --commit` against production
without explicit human approval citing dry-run evidence. The recommended
sequence is:

1. `--all` (no commit) → review log of total/built counts and a sample of payloads.
2. Inspect coverage before commit: `curl /invest/api/screener/snapshots/coverage`.
3. Wait for explicit "approved to commit" from a reviewer.
4. `--all --commit` → re-check coverage; expect `dataState="fresh"`.
```

Replace the "Scheduler — Deferred" section (around line 74-82) with:

```markdown
## 5. Scheduler — Deferred

**No recurring scheduler entry is active.** The table is filled on operator
demand. Recurring automation (e.g. nightly TaskIQ/Prefect job) requires:

- A separate Linear ticket
- At least one or two days of operator-run smoke evidence (coverage diagnostic
  output captured before/after `--all --commit`)
- Explicit reviewer approval citing that evidence

Do not introduce a recurring scheduler in the same PR as the snapshot read-path
wiring or the operator-CLI changes — they are intentionally split so the
scheduler activation can be reviewed against a known-stable manual baseline.
```

- [ ] **Step 2: Commit**

```bash
git add docs/runbooks/invest-screener-snapshots.md
git commit -m "$(cat <<'EOF'
docs(ROB-170): update snapshot runbook for --all backfill workflow

Documents the mandatory dry-run-then-approval-gated-commit sequence,
the new --all/--batch-size flags, and the explicit deferral of
recurring scheduler activation pending operator-run smoke evidence.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 8: Run full automated verification on the worktree branch

**Files:** none (read-only verification).

- [ ] **Step 1: Run lint + format**

Run: `make lint`
Expected: 0 errors. If `_to_optional_float` was added at the wrong import scope, ruff will flag it — fix and re-run.

- [ ] **Step 2: Run typecheck**

Run: `make typecheck`
Expected: 0 errors.

- [ ] **Step 3: Run the full unit + screener integration suite**

Run: `uv run pytest tests/test_invest_screener_snapshots_*.py tests/test_invest_screener_week_change_filter.py tests/test_invest_api_screener_router.py tests/test_screener_service.py tests/test_invest_screener_presets.py tests/test_screening_consecutive_up_days.py tests/test_build_invest_screener_snapshots_*.py -v`
Expected: all green.

- [ ] **Step 4: Capture verification evidence in the PR description**

When opening the PR, paste the test output + lint output into the PR body. Do NOT mark verification "done" without copying the actual command output — see CLAUDE.md verification-before-completion guidance.

- [ ] **Step 5: NO commit — this task is verification only**

---

### Task 9: Local end-to-end smoke against a dev DB

**Files:** none (read-only verification + transient writes to dev DB only).

This task confirms the wiring works against a real local Postgres before any prod backfill is attempted.

- [ ] **Step 1: Bring up local services**

Run: `docker compose up -d postgres redis && uv run alembic upgrade head`
Expected: services healthy, migration to `82309c07b8a2` (or later) applied.

- [ ] **Step 2: Run a small KR backfill against dev DB**

Run: `uv run python -m scripts.build_invest_screener_snapshots --market kr --limit 20 --commit`
Expected: `committed N rows.` printed (N typically 20 for a healthy KR universe).

- [ ] **Step 3: Verify coverage diagnostic**

Run: `uv run python -m scripts.diagnose_invest_screener_snapshots --market kr`
Expected: `coveringToday>=N`, `dataState=fallback` or `fresh` (NOT `missing` if your dev DB has the symbols you backfilled).

- [ ] **Step 4: Verify HTTP endpoint**

Run: `make dev` in another shell, then `curl 'http://localhost:8000/invest/api/screener/snapshots/coverage?market=kr'`
Expected: 200 with `snapshotsCoveringToday >= N`.

- [ ] **Step 5: Verify `/invest/api/screener/results` returns non-`missing` dataState**

Run: `curl -H "Authorization: Bearer <dev_token>" 'http://localhost:8000/invest/api/screener/results?presetId=consecutive_gainers&market=kr'`
Expected: response includes `freshness.dataState` ∈ `{"fresh", "partial", "fallback"}` and every row in `results[]` has its `metricValueLabel` matching `^\d+일$` (≥ 5 days). Eyeball: count rows with positive `changePctLabel` — they should align with `week_change_rate >= 0`.

- [ ] **Step 6: Capture evidence in a temp note (do NOT commit)**

Save `curl` output to a local scratchpad for the PR body. Discard before committing — these can include user-specific tokens.

- [ ] **Step 7: NO commit — verification only**

---

### Task 10: Open the PR (request review BEFORE any production commit)

**Files:** none.

- [ ] **Step 1: Push the branch**

Run: `git push -u origin feature/ROB-170-snapshot-followup`

- [ ] **Step 2: Create PR with full evidence**

Run:

```bash
gh pr create --title "feat(ROB-170): snapshot read-path wiring + Toss week-change filter" --body "$(cat <<'EOF'
## Summary
- Wires `_enrich_consecutive_up_days(rows, market, session)` into `build_screener_results` so `freshness.dataState` reflects snapshot state in production (was pinned at `missing`).
- Adds the second Toss filter `min_week_change_rate=0.0` to the `consecutive_gainers` preset; rows with unknown or negative week-change are dropped.
- Adds `--all` / `--batch-size` to the snapshot builder CLI for full-universe backfill, and aligns the US resolver with `is_active.is_(True)`.
- Extends on-demand OHLCV fallback to compute `week_change_rate` so symbols without snapshots are not unfairly dropped.
- No scheduler activation. No production `--commit` runs from this PR — that is a separate, approval-gated step (see follow-up checklist below).

## Test plan
- [ ] make lint
- [ ] make typecheck
- [ ] uv run pytest tests/test_invest_screener_snapshots_*.py tests/test_invest_screener_week_change_filter.py tests/test_invest_api_screener_router.py tests/test_screener_service.py tests/test_invest_screener_presets.py tests/test_screening_consecutive_up_days.py tests/test_build_invest_screener_snapshots_*.py
- [ ] Local dev smoke: `--limit 20 --commit` → coverage `dataState != "missing"` → `/screener/results` `freshness.dataState` ∈ {fresh, partial, fallback}.

## Production rollout (post-merge, gated)
1. Reviewer approves merge.
2. Operator runs `--all` (dry-run) for KR; captures `built/total` log + sample payloads in Linear comment.
3. Operator runs `diagnose_invest_screener_snapshots --market kr` (still 0 / `missing` — table is empty).
4. Reviewer responds with explicit "approved to commit KR".
5. Operator runs `--all --commit` for KR; re-runs diagnostic; expects `dataState="fresh"`, `coveringToday>0`.
6. Repeat for US.
7. Recurring scheduler activation is a SEPARATE ticket; do not enable in this rollout.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 3: Post the lint/test output as a PR comment**

Paste the actual command output from Task 8 into a PR comment so the reviewer has concrete evidence.

---

### Task 11: Production dry-run + diagnostic (operator-run, post-merge, gated)

**This task is for the operator, NOT the implementer agent.** Do NOT run it from inside the agent loop.

The operator runs:

```bash
# KR
uv run python -m scripts.build_invest_screener_snapshots --market kr --all
uv run python -m scripts.diagnose_invest_screener_snapshots --market kr
curl "https://<prod-host>/invest/api/screener/snapshots/coverage?market=kr"

# US
uv run python -m scripts.build_invest_screener_snapshots --market us --all
uv run python -m scripts.diagnose_invest_screener_snapshots --market us
curl "https://<prod-host>/invest/api/screener/snapshots/coverage?market=us"
```

The operator captures the output in the Linear ticket and explicitly requests reviewer approval to proceed to Task 12. **No implementer-agent action here.**

---

### Task 12: Production commit (operator-run, post-approval, GATED)

**HARD GATING.** This task only runs after a reviewer responds in Linear / on the PR with explicit text such as "approved to commit KR" / "approved to commit US", citing the dry-run evidence from Task 11. The implementer agent must NOT proactively execute this.

Operator runs (only after explicit approval):

```bash
# KR (post-approval)
uv run python -m scripts.build_invest_screener_snapshots --market kr --all --commit

# Verify
uv run python -m scripts.diagnose_invest_screener_snapshots --market kr
# Expect: coveringToday > 0, dataState=fresh

# Sanity-check the screener endpoint
curl "https://<prod-host>/invest/api/screener/results?presetId=consecutive_gainers&market=kr"
# Expect: freshness.dataState ∈ {"fresh", "partial", "fallback"}
#         every result row metricValueLabel ≥ "5일"
#         change-rate distribution skews positive (week_change_rate >= 0 enforcement)
```

Repeat for `--market us` after independent approval.

If anything goes wrong (universe mismatch, partial commit, schema drift), STOP and revert. The migration created the table empty, so a "revert" is just `TRUNCATE invest_screener_snapshots;` — but that is itself a destructive write and requires its own approval. Default action on incident is to STOP and escalate.

---

## Self-Review

### Spec coverage

| Spec requirement | Task |
|---|---|
| 1. Verify current code contracts | Decision Log (already verified during planning); not a runtime task |
| 2. Confirm exact snapshot build/backfill CLI, dry-run, commit, diagnostics | Task 6 (CLI changes), Task 7 (runbook), Task 11 (dry-run), Task 12 (commit) |
| 3. Code changes for `consecutive_up_days >= 5` and `week_change_rate >= 0` filters | Tasks 1–4 (helper, threading, preset, fallback symmetry) |
| 4. UI/API freshness/fallback dataState acceptance criteria | Task 5 (wiring fix), Task 8 + 9 (verification), D5 (semantics) |
| 5. Scheduler activation deferred and approval-gated | D4 (decision), Task 7 (runbook), Task 12 (gating) |
| Hard constraint: worktree only | Plan header (`/Users/mgh3326/worktrees/auto_trader/rob-170-snapshot-followup`) |
| Hard constraint: no broker/order/watch/order-intent mutation | Plan never touches those modules — only screening read path + CLI |
| Hard constraint: no destructive DB writes | Migrations untouched; only INSERT ON CONFLICT in tightly-scoped CLI |
| Hard constraint: no production `--commit` without dry-run + approval | Task 11 (dry-run) explicitly precedes Task 12 (commit), with reviewer-approval gate |
| Hard constraint: no recurring scheduler without separate approval | Out of Scope; Task 7 reinforces in runbook |

### Placeholder scan

No `TBD`, `TODO`, "implement later", or vague "add error handling" steps. Every code step shows the exact code or diff. Every test step shows the test body. Every command shows the exact invocation and expected output category.

### Type consistency

- `min_week_change_rate: float | None = None` is the canonical name across `normalize_screen_request`, `screen_stocks_impl`, `list_screening`, `refresh_screening`, and `screening_filters_for`.
- `_apply_min_week_change_rate(rows, *, threshold: float | None)` matches the keyword-only style of `_apply_min_consecutive_up_days(rows, *, threshold: int | None)`.
- The hydration alias in `build_screener_results` (`_async_enrich`) is local and avoids shadowing the existing sync `_enrich_consecutive_up_days(preset_id, row)` helper at line 239 of `screener_service.py`.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-05-10-rob-170-snapshot-followup-toss-parity.md`.

Two execution options:

**1. Subagent-Driven (recommended)** — Implementer dispatches a fresh subagent per task, with two-stage review between tasks.

**2. Inline Execution** — Implementer batches tasks with checkpoints under `superpowers:executing-plans`.

The reviewer/implementer should pick the approach when picking this plan up. Plan author preference: **subagent-driven** because Tasks 1–6 are largely independent slices (helper / threading / preset / fallback / wiring / CLI) and benefit from per-task review; Tasks 7–10 are coordination steps; Tasks 11–12 are operator-run and outside the agent loop entirely.

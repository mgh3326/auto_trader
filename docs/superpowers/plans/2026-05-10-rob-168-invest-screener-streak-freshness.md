# ROB-168 — /invest/screener Toss-style Consecutive Streak + Data Freshness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `/invest/screener` actually apply the Toss-style "연속 N일 상승" condition for the `consecutive_gainers` preset (default N=5) and surface explicit data-freshness wording (`YYYY.MM.DD HH:mm 기준`, `n분 전 갱신`, fallback `전 거래일 기준`) in the response and the desktop UI.

**Architecture:** Read-only end-to-end. Backend adds (a) a generic `min_consecutive_up_days: int` filter parameter inside the screening pipeline that uses the existing daily OHLCV cache (`_fetch_ohlcv_for_indicators`) to enrich the top-N rows of any preset that requests it, and (b) a `freshness` block on `ScreenerResultsResponse` derived from the existing `timestamp`/`cache_hit` fields already produced by `ScreenerService.list_screening`. Frontend renders the freshness label above the results table and continues to render `metricValueLabel` (which now reflects the real streak). No broker, order, watch-mutation, or scheduler changes.

**Tech Stack:** Python 3.13 (FastAPI, Pydantic v2, pytest, ruff); React 19 + Vite + Vitest + @testing-library/react.

**Linear:** https://linear.app/mgh3326/issue/ROB-168

**Branch:** `kanban/ROB-168-screener-streak-freshness` (already created from `origin/main` c7e3fb9b1f).

**Worktree:** `/Users/mgh3326/worktrees/auto_trader/ROB-168-screener-streak-freshness` — implementer MUST work here. **Hard rule:** never edit `/Users/mgh3326/services/auto_trader/current` directly.

---

## Decision: Fixed option vs. generic parameter

**Decision: Generic backend, fixed preset.** Backend exposes `min_consecutive_up_days: int | None` (1-30) end-to-end. Only the `consecutive_gainers` preset wires it (`min_consecutive_up_days=5`) for this MVP, so the user-visible label `연속 5일 상승` is locked in the preset chip while the contract stays flexible.

**Why generic:**
- Future presets ("연속 3일", "연속 10일") need zero backend churn.
- View-model already has `calculate_consecutive_up_days()` returning a number — capping the chip at 5 throws away free signal.
- The screener filter shape (`max_per`, `max_pbr`, `min_market_cap`, …) is already a flat kwargs bag. `min_consecutive_up_days` fits the pattern.

**Why hardcode in the preset for now:**
- ROB-168 acceptance only asks for the 5-day case to work in production.
- A generic UI control (slider/dropdown) is out of scope; adding it now would balloon the modal.

The `screening_filters_for("consecutive_gainers")` mapping in `screener_presets.py` is the single place to change N for the MVP.

---

## API / Response Contract

### Request (unchanged URLs, additive query params)

`GET /invest/api/screener/results?preset=consecutive_gainers&market=kr` — no new query parameter required at the view-model layer; the preset → filter mapping injects `min_consecutive_up_days=5` server-side.

Internal screening kwargs add **one** new optional field:

```python
# app/services/screener_service.py :: ScreenerService.list_screening
min_consecutive_up_days: int | None = None  # 1..30 inclusive, else 400
```

### Response (additive — `extra="forbid"` schemas need explicit fields)

`ScreenerResultsResponse` gets a new required `freshness` block plus the existing fields:

```python
# app/schemas/invest_screener.py
class ScreenerFreshness(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fetchedAt: str            # ISO8601 UTC, source-of-truth timestamp
    asOfLabel: str            # "2026.05.10 14:30 기준" (KST, formatted server-side)
    relativeLabel: str        # "방금 갱신" / "12분 전 갱신" / "전 거래일 기준"
    cacheHit: bool            # Mirrors ScreenerService cache_hit so UI can show "캐시" badge if ever needed
    source: Literal["live", "cached", "previous_session"]

class ScreenerResultsResponse(BaseModel):
    ...                       # existing fields unchanged
    freshness: ScreenerFreshness
```

`ScreenerResultRow.metricValueLabel` for the consecutive_gainers preset becomes `"5일"` / `"6일"` / etc. (already implemented downstream of `_metric_value_label`); rows where the streak fetch failed keep `"-"` and the existing `"연속상승 데이터 준비중"` warning.

### Frontend types

`frontend/invest/src/types/screener.ts` mirrors the new `freshness` block. `DesktopScreenerPage` renders it as a single line above the warnings list:

```
2026.05.10 14:30 기준 · 12분 전 갱신
```

If `source === "previous_session"` (KR market closed and no live tick today) the label collapses to:

```
전 거래일 기준 · 2026.05.09 15:30 종가
```

---

## Acceptance Checks (gate the merge)

1. `pytest tests/test_invest_view_model_screener_service.py tests/test_invest_screener_schemas.py tests/test_invest_screener_presets.py tests/test_screener_service.py tests/test_invest_view_model_safety.py -q` — all green.
2. `pytest tests/integration/test_screener_e2e.py -q -k consecutive` — passes against a fixture that returns ≥10 KR rows; the response trims to rows with `consecutive_up_days >= 5` and the rendered metricValueLabel is `"5일"` or higher.
3. `cd frontend/invest && npm run -s test -- ScreenerFreshness DesktopScreenerPage` — passes.
4. Smoke (manual, after deploy):
   - Open `/invest/screener` on production with `consecutive_gainers` selected.
   - Visible: `YYYY.MM.DD HH:mm 기준 · n분 전 갱신` line above the warnings.
   - Every visible row's metric column reads `N일` with N ≥ 5.
   - Switch market to 미국 → page still renders without freshness rendering errors (US fall-back is acceptable: the freshness line still appears).
5. `tests/test_invest_view_model_safety.py` — confirms `app.services.invest_view_model.screener_service` still does not transitively import `app.services.brokers.*` after this change.

---

## File Structure (locked before tasks begin)

| File | Change |
|---|---|
| `app/schemas/invest_screener.py` | Add `ScreenerFreshness` model and `freshness` field on `ScreenerResultsResponse`. |
| `app/services/invest_view_model/screener_service.py` | Build `ScreenerFreshness` from upstream `timestamp` + `cache_hit`; thread it into the response. Replace stub `_enrich_consecutive_up_days` (history-from-row only) with `_enrich_consecutive_up_days_async` that calls the new shared OHLCV fetch helper for top rows when streak is missing. |
| `app/services/invest_view_model/screener_presets.py` | Add `"min_consecutive_up_days": 5` to the `consecutive_gainers` filter dict. Update the chip detail string to `"5일 연속 상승"`. |
| `app/services/screener_service.py` | Accept `min_consecutive_up_days: int | None` in `list_screening`/`refresh_screening`. Pass it through `normalize_screen_request` and into `screen_stocks_impl`. Include it in cache key. |
| `app/mcp_server/tooling/analysis_screen_core.py` | Accept and validate `min_consecutive_up_days` (1-30) in `normalize_screen_request`. |
| `app/mcp_server/tooling/screening/common.py` | Add a single `_apply_min_consecutive_up_days` post-filter that runs after enrichment (top-N OHLCV lookup) and trims rows below the threshold. |
| `app/mcp_server/tooling/screening/enrichment.py` | New `_enrich_consecutive_up_days(rows, market)` that fetches up to 10 daily closes via `_fetch_ohlcv_for_indicators` per row (concurrency-limited), tolerating per-row failures. |
| `app/mcp_server/tooling/screening/kr.py` | Call the new enrichment step when `min_consecutive_up_days` is set. |
| `app/mcp_server/tooling/screening/us.py` | Same as KR. |
| `frontend/invest/src/types/screener.ts` | Add `ScreenerFreshness` interface + field on `ScreenerResultsResponse`. |
| `frontend/invest/src/desktop/screener/ScreenerFreshnessLine.tsx` | NEW — small presentational component, ~20 lines. |
| `frontend/invest/src/pages/desktop/DesktopScreenerPage.tsx` | Render `<ScreenerFreshnessLine freshness={results.freshness} />` directly under `<ScreenerFilterBar>`. |
| `frontend/invest/src/desktop/screener/screener.css` | Add `.screener-freshness` rule (one selector). |
| `tests/test_invest_screener_schemas.py` | Cover the `ScreenerFreshness` model + presence on `ScreenerResultsResponse`. |
| `tests/test_invest_view_model_screener_service.py` | Cover freshness construction (live, cached, previous_session) and the streak-trim path. |
| `tests/test_invest_screener_presets.py` | Cover the new `min_consecutive_up_days=5` filter mapping. |
| `tests/test_screener_service.py` | Cover param plumbing through `list_screening` (cache key includes new field; rejects N<1, N>30). |
| `tests/integration/test_screener_e2e.py` | Add a `consecutive_gainers` happy-path scenario asserting `metricValueLabel ≥ "5일"` and freshness present. |
| `tests/test_invest_view_model_safety.py` | No code change; rerun to confirm no broker import leak. |
| `frontend/invest/src/__tests__/ScreenerFreshnessLine.test.tsx` | NEW — covers live/cached/previous_session label formatting. |
| `frontend/invest/src/__tests__/DesktopScreenerPage.test.tsx` | Add assertion that the freshness line renders. |

No files outside the table are touched.

---

## Task 0: Branch + worktree sanity check

**Files:** none (git only).

- [ ] **Step 0.1: Confirm worktree + branch**

Run:

```bash
cd /Users/mgh3326/worktrees/auto_trader/ROB-168-screener-streak-freshness
git status
git rev-parse --abbrev-ref HEAD
git log -1 --oneline
```

Expected:
- `On branch kanban/ROB-168-screener-streak-freshness`, working tree clean.
- HEAD = `c7e3fb9b feat(news): ROB-155 US noise / crypto filter (#759)`.

If this fails, STOP and re-create the worktree from `origin/main`.

- [ ] **Step 0.2: Bootstrap python env**

```bash
cd /Users/mgh3326/worktrees/auto_trader/ROB-168-screener-streak-freshness
uv sync
uv run pytest tests/test_invest_view_model_safety.py -q
```

Expected: tests pass. This guards against a broken baseline before edits start.

---

## Task 1: Backend — `ScreenerFreshness` schema

**Files:**
- Modify: `app/schemas/invest_screener.py`
- Modify: `tests/test_invest_screener_schemas.py`

- [ ] **Step 1.1: Write failing schema tests**

Add to `tests/test_invest_screener_schemas.py`:

```python
import pytest
from pydantic import ValidationError

from app.schemas.invest_screener import (
    ScreenerFreshness,
    ScreenerResultsResponse,
)


@pytest.mark.unit
def test_screener_freshness_requires_all_fields() -> None:
    f = ScreenerFreshness(
        fetchedAt="2026-05-10T05:30:00+00:00",
        asOfLabel="2026.05.10 14:30 기준",
        relativeLabel="12분 전 갱신",
        cacheHit=False,
        source="live",
    )
    assert f.source == "live"


@pytest.mark.unit
def test_screener_freshness_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        ScreenerFreshness(
            fetchedAt="2026-05-10T05:30:00+00:00",
            asOfLabel="x",
            relativeLabel="x",
            cacheHit=True,
            source="live",
            unexpected="nope",  # type: ignore[call-arg]
        )


@pytest.mark.unit
def test_screener_results_response_requires_freshness() -> None:
    with pytest.raises(ValidationError):
        ScreenerResultsResponse(  # type: ignore[call-arg]
            presetId="consecutive_gainers",
            title="연속 상승세",
            description="",
            filterChips=[],
            metricLabel="연속상승",
            results=[],
            warnings=[],
        )
```

- [ ] **Step 1.2: Run tests; verify they fail**

```bash
uv run pytest tests/test_invest_screener_schemas.py -q -k "freshness or requires_freshness"
```

Expected: 3 failures (`ScreenerFreshness` undefined; `freshness` missing required field).

- [ ] **Step 1.3: Implement `ScreenerFreshness`**

Edit `app/schemas/invest_screener.py`:

```python
class ScreenerFreshness(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fetchedAt: str
    asOfLabel: str
    relativeLabel: str
    cacheHit: bool
    source: Literal["live", "cached", "previous_session"]


class ScreenerResultsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    presetId: str
    title: str
    description: str
    filterChips: list[ScreenerFilterChip]
    metricLabel: str
    results: list[ScreenerResultRow]
    warnings: list[str] = Field(default_factory=list)
    freshness: ScreenerFreshness  # NEW — required
```

- [ ] **Step 1.4: Run tests; verify pass**

```bash
uv run pytest tests/test_invest_screener_schemas.py -q
```

Expected: all green.

- [ ] **Step 1.5: Commit**

```bash
git add app/schemas/invest_screener.py tests/test_invest_screener_schemas.py
git commit -m "feat(invest-screener): add ScreenerFreshness response block (ROB-168)"
```

---

## Task 2: Backend — view-model wires `freshness` from upstream timestamp

**Files:**
- Modify: `app/services/invest_view_model/screener_service.py`
- Modify: `tests/test_invest_view_model_screener_service.py`

Context from current code: `ScreenerService.list_screening` already returns `timestamp` (ISO UTC, line 889 of `app/mcp_server/tooling/screening/common.py`) and `cache_hit: bool` (line 390 / 466 of `app/services/screener_service.py`). The view-model layer ignores both today.

- [ ] **Step 2.1: Write failing freshness construction tests**

Add to `tests/test_invest_view_model_screener_service.py`:

```python
@pytest.mark.unit
@pytest.mark.asyncio
async def test_build_screener_results_emits_freshness_live() -> None:
    fake = MagicMock()
    fake.list_screening = AsyncMock(
        return_value={
            "results": _stub_screening_rows(),
            "warnings": [],
            "timestamp": "2026-05-10T05:30:00+00:00",
            "cache_hit": False,
        }
    )
    resp = await build_screener_results(
        preset_id="consecutive_gainers",
        screening_service=fake,
        resolver=_FakeResolver(set()),
        now=lambda: datetime(2026, 5, 10, 5, 42, tzinfo=UTC),
    )
    assert resp.freshness.source == "live"
    assert resp.freshness.cacheHit is False
    assert resp.freshness.asOfLabel == "2026.05.10 14:30 기준"   # KST = UTC+9
    assert resp.freshness.relativeLabel == "12분 전 갱신"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_build_screener_results_emits_freshness_cached() -> None:
    fake = MagicMock()
    fake.list_screening = AsyncMock(
        return_value={
            "results": _stub_screening_rows(),
            "warnings": [],
            "timestamp": "2026-05-10T05:30:00+00:00",
            "cache_hit": True,
        }
    )
    resp = await build_screener_results(
        preset_id="consecutive_gainers",
        screening_service=fake,
        resolver=_FakeResolver(set()),
        now=lambda: datetime(2026, 5, 10, 5, 31, tzinfo=UTC),
    )
    assert resp.freshness.source == "cached"
    assert resp.freshness.cacheHit is True
    assert resp.freshness.relativeLabel == "방금 갱신"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_build_screener_results_emits_freshness_previous_session_when_market_closed() -> None:
    # Sat 11:00 UTC -> Sat 20:00 KST; KR market closed -> previous_session
    fake = MagicMock()
    fake.list_screening = AsyncMock(
        return_value={
            "results": _stub_screening_rows(),
            "warnings": [],
            "timestamp": "2026-05-08T06:30:00+00:00",  # Fri 15:30 KST close
            "cache_hit": True,
        }
    )
    resp = await build_screener_results(
        preset_id="consecutive_gainers",
        screening_service=fake,
        resolver=_FakeResolver(set()),
        market="kr",
        now=lambda: datetime(2026, 5, 10, 11, 0, tzinfo=UTC),  # Sat
    )
    assert resp.freshness.source == "previous_session"
    assert resp.freshness.relativeLabel == "전 거래일 기준"
```

(Add `from datetime import datetime, UTC` at top of test file if missing.)

- [ ] **Step 2.2: Run tests; verify they fail**

```bash
uv run pytest tests/test_invest_view_model_screener_service.py -q -k freshness
```

Expected: 3 failures (`now` kwarg unknown; `.freshness` AttributeError).

- [ ] **Step 2.3: Implement freshness builder**

Edit `app/services/invest_view_model/screener_service.py`. Add module-level imports:

```python
from datetime import UTC, datetime, timedelta, time as _time
from collections.abc import Callable
from zoneinfo import ZoneInfo

from app.schemas.invest_screener import ScreenerFreshness

_KST = ZoneInfo("Asia/Seoul")
_KR_OPEN = _time(9, 0)
_KR_CLOSE = _time(15, 30)
_CACHE_HIT_FRESH_SECONDS = 300  # matches ScreenerService.SCREENING_CACHE_TTL_SECONDS
```

Add helpers:

```python
def _format_relative_korean(delta_seconds: int) -> str:
    if delta_seconds < 60:
        return "방금 갱신"
    minutes = delta_seconds // 60
    if minutes < 60:
        return f"{minutes}분 전 갱신"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}시간 전 갱신"
    days = hours // 24
    return f"{days}일 전 갱신"


def _is_kr_market_open(at_kst: datetime) -> bool:
    if at_kst.weekday() >= 5:  # Sat=5, Sun=6
        return False
    return _KR_OPEN <= at_kst.time() <= _KR_CLOSE


def _build_freshness(
    *,
    raw_timestamp: str | None,
    cache_hit: bool,
    market: str,
    now: Callable[[], datetime],
) -> ScreenerFreshness:
    now_utc = now()
    if not raw_timestamp:
        fetched = now_utc
    else:
        try:
            fetched = datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00"))
        except ValueError:
            fetched = now_utc
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=UTC)
    fetched_kst = fetched.astimezone(_KST)
    now_kst = now_utc.astimezone(_KST)
    delta = max(0, int((now_utc - fetched).total_seconds()))

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

    return ScreenerFreshness(
        fetchedAt=fetched.astimezone(UTC).isoformat(),
        asOfLabel=fetched_kst.strftime("%Y.%m.%d %H:%M 기준"),
        relativeLabel=relative,
        cacheHit=bool(cache_hit),
        source=source,
    )
```

Update `build_screener_results` signature:

```python
async def build_screener_results(
    preset_id: str,
    screening_service: _ScreeningServiceProto,
    resolver: _ResolverProto,
    market: str = "kr",
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> ScreenerResultsResponse:
    ...
    raw = await screening_service.list_screening(**filters)
    rows: list[dict[str, Any]] = list(raw.get("results") or raw.get("stocks") or [])
    upstream_warnings: list[str] = list(raw.get("warnings") or [])
    freshness = _build_freshness(
        raw_timestamp=raw.get("timestamp"),
        cache_hit=bool(raw.get("cache_hit")),
        market=requested_market,
        now=now,
    )
    ...
    return ScreenerResultsResponse(
        presetId=preset.id,
        title=preset.name,
        description=preset.description,
        filterChips=preset.filterChips,
        metricLabel=preset.metricLabel,
        results=results,
        warnings=upstream_warnings,
        freshness=freshness,
    )
```

Also update the unknown-preset early-return in the same function so it still returns a `freshness` block (use `_build_freshness` with `raw_timestamp=None`, `cache_hit=False`).

- [ ] **Step 2.4: Run tests; verify pass**

```bash
uv run pytest tests/test_invest_view_model_screener_service.py -q
```

Expected: green (including pre-existing tests that constructed `ScreenerResultsResponse` — they pass `freshness` automatically because the view-model constructs it; only the few tests that called `ScreenerResultsResponse(...)` directly need a default `freshness=` argument added — fix any that fail).

- [ ] **Step 2.5: Commit**

```bash
git add app/services/invest_view_model/screener_service.py tests/test_invest_view_model_screener_service.py
git commit -m "feat(invest-screener): emit ScreenerFreshness from upstream timestamp (ROB-168)"
```

---

## Task 3: Backend — `min_consecutive_up_days` plumbing through `ScreenerService`

**Files:**
- Modify: `app/services/screener_service.py`
- Modify: `app/mcp_server/tooling/analysis_screen_core.py`
- Modify: `tests/test_screener_service.py`

- [ ] **Step 3.1: Write failing tests**

Add to `tests/test_screener_service.py`:

```python
@pytest.mark.unit
@pytest.mark.asyncio
async def test_list_screening_passes_min_consecutive_up_days(monkeypatch):
    captured = {}

    async def fake_screen(**kwargs):
        captured.update(kwargs)
        return {"results": [], "stocks": [], "filters_applied": {}, "timestamp": "2026-05-10T05:30:00+00:00"}

    monkeypatch.setattr(
        "app.services.screener_service.screen_stocks_impl",
        fake_screen,
    )
    svc = ScreenerService()
    svc._get_redis = AsyncMock(side_effect=Exception("no redis"))  # bypass cache
    out = await svc.list_screening(market="kr", min_consecutive_up_days=5)
    assert captured.get("min_consecutive_up_days") == 5
    assert "min_consecutive_up_days" in out.get("filters_applied", {})


@pytest.mark.unit
def test_normalize_screen_request_rejects_out_of_range_streak():
    from app.mcp_server.tooling.analysis_screen_core import normalize_screen_request
    with pytest.raises(ValueError):
        normalize_screen_request(market="kr", min_consecutive_up_days=0)
    with pytest.raises(ValueError):
        normalize_screen_request(market="kr", min_consecutive_up_days=31)
    out = normalize_screen_request(market="kr", min_consecutive_up_days=5)
    assert out["min_consecutive_up_days"] == 5
```

- [ ] **Step 3.2: Run tests; verify failure**

```bash
uv run pytest tests/test_screener_service.py -q -k "consecutive"
```

Expected: failures (param not accepted; normalizer rejects).

- [ ] **Step 3.3: Implement plumbing**

In `app/mcp_server/tooling/analysis_screen_core.py` add a `min_consecutive_up_days: int | None = None` kwarg to `normalize_screen_request`, validate `1 <= n <= 30`, place it in the returned dict.

In `app/services/screener_service.py`:

- Add `min_consecutive_up_days: int | None = None` to `list_screening` and `refresh_screening` signatures.
- Pass it into `normalize_screen_request(...)`.
- Include `"min_consecutive_up_days": normalized_request["min_consecutive_up_days"]` in the `filters` dict (so it participates in `_screening_cache_key`).
- Conditionally include in `call_kwargs` (drop only when `None`).
- Set `normalized_filters_applied.setdefault("min_consecutive_up_days", normalized_request["min_consecutive_up_days"])`.

- [ ] **Step 3.4: Run tests; verify pass**

```bash
uv run pytest tests/test_screener_service.py tests/test_invest_screener_presets.py -q
```

Expected: green.

- [ ] **Step 3.5: Commit**

```bash
git add app/services/screener_service.py app/mcp_server/tooling/analysis_screen_core.py tests/test_screener_service.py
git commit -m "feat(screener): add min_consecutive_up_days param to list/refresh_screening (ROB-168)"
```

---

## Task 4: Backend — preset wires `min_consecutive_up_days=5`

**Files:**
- Modify: `app/services/invest_view_model/screener_presets.py`
- Modify: `tests/test_invest_screener_presets.py`

- [ ] **Step 4.1: Write failing test**

Add to `tests/test_invest_screener_presets.py`:

```python
def test_consecutive_gainers_preset_requests_streak_filter():
    filters = screening_filters_for("consecutive_gainers", market="kr")
    assert filters.get("min_consecutive_up_days") == 5

def test_consecutive_gainers_chip_says_5_days():
    preset = get_preset("consecutive_gainers", market="kr")
    chip_details = [c.detail for c in preset.filterChips if c.detail]
    assert any("5일 연속 상승" in d for d in chip_details)
```

- [ ] **Step 4.2: Run tests; verify failure**

```bash
uv run pytest tests/test_invest_screener_presets.py -q -k "consecutive"
```

Expected: failures.

- [ ] **Step 4.3: Implement**

Edit `app/services/invest_view_model/screener_presets.py`:

In the `consecutive_gainers` preset definition, replace `ScreenerFilterChip(label="주가 연속상승", detail="최신 일봉 기준")` with `ScreenerFilterChip(label="주가 연속상승", detail="5일 연속 상승")`.

In `_SCREENING_FILTERS["consecutive_gainers"]`, add `"min_consecutive_up_days": 5,` (also remove the now-redundant `"sort_by": "change_rate"` if you want, but leave it — sort still useful as tie-breaker).

Update the docstring/comment at the top of the file: remove the warning that says we cannot apply "주가 연속상승" — we now can.

- [ ] **Step 4.4: Run tests; verify pass**

```bash
uv run pytest tests/test_invest_screener_presets.py tests/test_invest_view_model_screener_service.py -q
```

Expected: green.

- [ ] **Step 4.5: Commit**

```bash
git add app/services/invest_view_model/screener_presets.py tests/test_invest_screener_presets.py
git commit -m "feat(invest-screener): wire consecutive_gainers preset to min_consecutive_up_days=5 (ROB-168)"
```

---

## Task 5: Backend — OHLCV-driven streak enrichment in screening pipeline

**Files:**
- Modify: `app/mcp_server/tooling/screening/enrichment.py`
- Modify: `app/mcp_server/tooling/screening/common.py`
- Modify: `app/mcp_server/tooling/screening/kr.py`
- Modify: `app/mcp_server/tooling/screening/us.py`
- Modify: `tests/test_screening_tvscreener_support.py` (or create `tests/test_screening_consecutive_up_days.py`)

This is the only task that touches a path which transitively imports brokers, so it MUST stay inside `app.mcp_server.tooling.screening.*` — never move computation into `app.services.invest_view_model`.

- [ ] **Step 5.1: Write failing enrichment tests**

Create `tests/test_screening_consecutive_up_days.py`:

```python
"""ROB-168: post-screen OHLCV-based streak enrichment + filter."""
from __future__ import annotations
from unittest.mock import AsyncMock, patch

import pandas as pd
import pytest


@pytest.mark.unit
@pytest.mark.asyncio
async def test_enrich_consecutive_up_days_uses_daily_closes():
    from app.mcp_server.tooling.screening.enrichment import (
        _enrich_consecutive_up_days,
    )

    rows = [
        {"symbol": "005930", "market": "kr"},
        {"symbol": "035720", "market": "kr"},
    ]

    def fake_df(closes):
        return pd.DataFrame({"close": closes})

    async def fake_fetch(symbol, market_type, count=10):
        if symbol == "005930":
            return fake_df([100, 101, 102, 103, 104, 105])  # streak 5
        if symbol == "035720":
            return fake_df([100, 101, 100, 101, 102, 103])  # streak 3
        raise AssertionError(symbol)

    with patch(
        "app.mcp_server.tooling.screening.enrichment._fetch_ohlcv_for_indicators",
        side_effect=fake_fetch,
    ):
        await _enrich_consecutive_up_days(rows, market="kr", lookback=10)

    assert rows[0]["consecutive_up_days"] == 5
    assert rows[1]["consecutive_up_days"] == 3


@pytest.mark.unit
@pytest.mark.asyncio
async def test_enrich_consecutive_up_days_tolerates_per_row_failure():
    from app.mcp_server.tooling.screening.enrichment import (
        _enrich_consecutive_up_days,
    )

    rows = [{"symbol": "BAD", "market": "kr"}, {"symbol": "OK", "market": "kr"}]

    async def fake_fetch(symbol, market_type, count=10):
        if symbol == "BAD":
            raise RuntimeError("fetch failed")
        return pd.DataFrame({"close": [100, 101, 102]})

    with patch(
        "app.mcp_server.tooling.screening.enrichment._fetch_ohlcv_for_indicators",
        side_effect=fake_fetch,
    ):
        await _enrich_consecutive_up_days(rows, market="kr", lookback=10)

    assert "consecutive_up_days" not in rows[0]
    assert rows[1]["consecutive_up_days"] == 2


@pytest.mark.unit
def test_apply_min_consecutive_up_days_filter_drops_rows_below_threshold():
    from app.mcp_server.tooling.screening.common import (
        _apply_min_consecutive_up_days,
    )

    rows = [
        {"symbol": "A", "consecutive_up_days": 6},
        {"symbol": "B", "consecutive_up_days": 4},
        {"symbol": "C", "consecutive_up_days": None},
        {"symbol": "D"},  # missing -> drop
    ]
    out = _apply_min_consecutive_up_days(rows, threshold=5)
    assert [r["symbol"] for r in out] == ["A"]
```

- [ ] **Step 5.2: Run tests; verify failure**

```bash
uv run pytest tests/test_screening_consecutive_up_days.py -q
```

Expected: import errors / function-not-defined.

- [ ] **Step 5.3: Implement enrichment helper**

Edit `app/mcp_server/tooling/screening/enrichment.py`. Add at top:

```python
import asyncio
from app.mcp_server.tooling.market_data_indicators import (
    _fetch_ohlcv_for_indicators,
)
from app.services.invest_view_model.screener_service import (
    calculate_consecutive_up_days,
)
```

(Note: `screener_service.calculate_consecutive_up_days` is pure and broker-free. Importing it here keeps the streak math single-sourced.)

Add helper:

```python
_STREAK_LOOKBACK_DEFAULT = 10
_STREAK_CONCURRENCY = 4


async def _enrich_consecutive_up_days(
    rows: list[dict[str, Any]],
    *,
    market: str,
    lookback: int = _STREAK_LOOKBACK_DEFAULT,
) -> None:
    if not rows:
        return
    market_type = "equity_kr" if market == "kr" else "equity_us"
    sem = asyncio.Semaphore(_STREAK_CONCURRENCY)

    async def _enrich_one(row: dict[str, Any]) -> None:
        symbol = row.get("symbol")
        if not symbol or row.get("consecutive_up_days") is not None:
            return
        async with sem:
            try:
                df = await _fetch_ohlcv_for_indicators(
                    str(symbol), market_type, count=lookback
                )
            except Exception:
                return
        if df is None or df.empty or "close" not in df.columns:
            return
        closes = df["close"].tolist()
        streak = calculate_consecutive_up_days(closes)
        if streak is not None:
            row["consecutive_up_days"] = streak

    await asyncio.gather(*(_enrich_one(r) for r in rows))
```

Edit `app/mcp_server/tooling/screening/common.py`. Add helper:

```python
def _apply_min_consecutive_up_days(
    rows: list[dict[str, Any]], *, threshold: int | None
) -> list[dict[str, Any]]:
    if threshold is None:
        return rows
    return [
        r for r in rows
        if isinstance(r.get("consecutive_up_days"), int)
        and r["consecutive_up_days"] >= threshold
    ]
```

Edit `app/mcp_server/tooling/screening/kr.py` and `app/mcp_server/tooling/screening/us.py`. After the existing enrichment phase but before the response is built, add (KR shown):

```python
min_streak = filters_applied.get("min_consecutive_up_days")
if min_streak:
    await _enrich_consecutive_up_days(decorated_rows, market="kr")
    decorated_rows = _apply_min_consecutive_up_days(decorated_rows, threshold=int(min_streak))
```

Use `market="us"` in the US module. The `decorated_rows` variable name is the existing local — match whatever the file uses.

- [ ] **Step 5.4: Run tests; verify pass**

```bash
uv run pytest tests/test_screening_consecutive_up_days.py tests/test_invest_view_model_safety.py -q
```

Expected: green. **Critically**: `test_invest_view_model_safety.py` MUST still pass — `enrichment.py` imports brokers transitively, but it lives in `app.mcp_server.tooling.screening.*`, which is NOT in the forbidden list. The view-model's import of `calculate_consecutive_up_days` is fine because that function lives inside the view-model package itself.

If safety test fails: undo the import direction (move `calculate_consecutive_up_days` to a new `app/services/invest_view_model/streak_math.py` and have both sides import from there).

- [ ] **Step 5.5: Commit**

```bash
git add app/mcp_server/tooling/screening/enrichment.py app/mcp_server/tooling/screening/common.py app/mcp_server/tooling/screening/kr.py app/mcp_server/tooling/screening/us.py tests/test_screening_consecutive_up_days.py
git commit -m "feat(screener): OHLCV-driven consecutive_up_days enrichment + post-filter (ROB-168)"
```

---

## Task 6: Backend — view-model strips its now-redundant stub enrichment

**Files:**
- Modify: `app/services/invest_view_model/screener_service.py`
- Modify: `tests/test_invest_view_model_screener_service.py`

The existing `_enrich_consecutive_up_days(preset_id, row)` (line 228 of current file) only populates from `daily_closes`/`close_history`/`closes` keys that no upstream actually emits. With Task 5 in place, `consecutive_up_days` is populated upstream directly. Keep the stub as a no-op fallback; just stop emitting the "데이터 준비중" warning when the streak comes through and is `None` for non-consecutive_gainers presets.

- [ ] **Step 6.1: Update `_metric_value_label` test for consecutive_gainers when value present**

Existing test `test_consecutive_up_metric_prefers_consecutive_days` already covers this — confirm it still passes after Task 5 changes.

- [ ] **Step 6.2: Verify**

```bash
uv run pytest tests/test_invest_view_model_screener_service.py -q
```

Expected: green. No commit needed if no edit was required.

---

## Task 7: Backend — integration smoke

**Files:**
- Modify: `tests/integration/test_screener_e2e.py`

- [ ] **Step 7.1: Add consecutive-streak scenario**

Find the existing happy-path test and add a sibling scenario:

```python
@pytest.mark.integration
@pytest.mark.asyncio
async def test_screener_consecutive_gainers_returns_streak_and_freshness(
    monkeypatch, async_client
):
    # Fixture: 5 KR rows with computed streaks 6, 5, 4, 3, 7
    ...
    resp = await async_client.get(
        "/invest/api/screener/results?preset=consecutive_gainers&market=kr"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["freshness"]["asOfLabel"].endswith("기준")
    assert body["freshness"]["source"] in ("live", "cached", "previous_session")
    assert all("일" in row["metricValueLabel"] for row in body["results"])
    assert all(int(row["metricValueLabel"][:-1]) >= 5 for row in body["results"])
```

Mirror the existing fixture style in this file (do NOT introduce a brand-new fixture pattern). If the file uses `respx`/`httpx_mock` for the upstream OHLCV calls, stub `_fetch_ohlcv_for_indicators` for each fixture symbol.

- [ ] **Step 7.2: Verify**

```bash
uv run pytest tests/integration/test_screener_e2e.py -q -k consecutive
```

Expected: green.

- [ ] **Step 7.3: Commit**

```bash
git add tests/integration/test_screener_e2e.py
git commit -m "test(screener): integration coverage for consecutive_gainers + freshness (ROB-168)"
```

---

## Task 8: Frontend — `ScreenerFreshness` types

**Files:**
- Modify: `frontend/invest/src/types/screener.ts`

- [ ] **Step 8.1: Add types**

```ts
export type ScreenerFreshnessSource = "live" | "cached" | "previous_session";

export interface ScreenerFreshness {
  fetchedAt: string;
  asOfLabel: string;
  relativeLabel: string;
  cacheHit: boolean;
  source: ScreenerFreshnessSource;
}

export interface ScreenerResultsResponse {
  presetId: string;
  title: string;
  description: string;
  filterChips: ScreenerFilterChip[];
  metricLabel: string;
  results: ScreenerResultRow[];
  warnings: string[];
  freshness: ScreenerFreshness;        // NEW — required
}
```

- [ ] **Step 8.2: Type-check**

```bash
cd frontend/invest && npm run -s typecheck
```

Expected: errors at usage sites in `DesktopScreenerPage.tsx` and tests that construct `ScreenerResultsResponse` literals — those will be fixed in Task 9 / 10.

- [ ] **Step 8.3: Commit**

```bash
git add frontend/invest/src/types/screener.ts
git commit -m "types(screener): add ScreenerFreshness DTO (ROB-168)"
```

---

## Task 9: Frontend — `ScreenerFreshnessLine` component + tests

**Files:**
- Create: `frontend/invest/src/desktop/screener/ScreenerFreshnessLine.tsx`
- Create: `frontend/invest/src/__tests__/ScreenerFreshnessLine.test.tsx`
- Modify: `frontend/invest/src/desktop/screener/screener.css`

- [ ] **Step 9.1: Write failing component test**

```tsx
// frontend/invest/src/__tests__/ScreenerFreshnessLine.test.tsx
import { render, screen } from "@testing-library/react";
import { ScreenerFreshnessLine } from "../desktop/screener/ScreenerFreshnessLine";

test("renders asOfLabel and relativeLabel separated by '·'", () => {
  render(
    <ScreenerFreshnessLine
      freshness={{
        fetchedAt: "2026-05-10T05:30:00+00:00",
        asOfLabel: "2026.05.10 14:30 기준",
        relativeLabel: "12분 전 갱신",
        cacheHit: false,
        source: "live",
      }}
    />,
  );
  expect(screen.getByTestId("screener-freshness")).toHaveTextContent(
    "2026.05.10 14:30 기준 · 12분 전 갱신",
  );
});

test("collapses to '전 거래일 기준' when source is previous_session", () => {
  render(
    <ScreenerFreshnessLine
      freshness={{
        fetchedAt: "2026-05-08T06:30:00+00:00",
        asOfLabel: "2026.05.08 15:30 기준",
        relativeLabel: "전 거래일 기준",
        cacheHit: true,
        source: "previous_session",
      }}
    />,
  );
  expect(screen.getByTestId("screener-freshness")).toHaveTextContent(
    "전 거래일 기준 · 2026.05.08 15:30 종가",
  );
});
```

- [ ] **Step 9.2: Run; verify failure**

```bash
cd frontend/invest && npm run -s test -- ScreenerFreshnessLine
```

Expected: import error.

- [ ] **Step 9.3: Implement component**

```tsx
// frontend/invest/src/desktop/screener/ScreenerFreshnessLine.tsx
import type { ScreenerFreshness } from "../../types/screener";

export function ScreenerFreshnessLine({
  freshness,
}: {
  freshness: ScreenerFreshness;
}) {
  const text =
    freshness.source === "previous_session"
      ? `${freshness.relativeLabel} · ${freshness.asOfLabel.replace("기준", "종가")}`
      : `${freshness.asOfLabel} · ${freshness.relativeLabel}`;
  return (
    <div
      className="screener-freshness"
      data-testid="screener-freshness"
      aria-live="polite"
    >
      {text}
    </div>
  );
}
```

Add to `screener.css`:

```css
.screener-freshness {
  font-size: 12px;
  color: var(--fg-3);
  margin: 4px 0 12px;
}
```

- [ ] **Step 9.4: Run; verify pass**

```bash
cd frontend/invest && npm run -s test -- ScreenerFreshnessLine
```

Expected: green.

- [ ] **Step 9.5: Commit**

```bash
git add frontend/invest/src/desktop/screener/ScreenerFreshnessLine.tsx frontend/invest/src/__tests__/ScreenerFreshnessLine.test.tsx frontend/invest/src/desktop/screener/screener.css
git commit -m "feat(invest-screener-ui): add ScreenerFreshnessLine component (ROB-168)"
```

---

## Task 10: Frontend — render freshness line on `DesktopScreenerPage`

**Files:**
- Modify: `frontend/invest/src/pages/desktop/DesktopScreenerPage.tsx`
- Modify: `frontend/invest/src/__tests__/DesktopScreenerPage.test.tsx`

- [ ] **Step 10.1: Update page test**

In `DesktopScreenerPage.test.tsx`, the existing fake response object must now include `freshness` (the type narrowing in Task 8 enforces this). Add:

```tsx
freshness: {
  fetchedAt: "2026-05-10T05:30:00+00:00",
  asOfLabel: "2026.05.10 14:30 기준",
  relativeLabel: "방금 갱신",
  cacheHit: false,
  source: "live",
},
```

to the existing happy-path mock. Add an assertion:

```tsx
expect(await screen.findByTestId("screener-freshness")).toHaveTextContent(
  "2026.05.10 14:30 기준 · 방금 갱신",
);
```

- [ ] **Step 10.2: Run; verify failure**

```bash
cd frontend/invest && npm run -s test -- DesktopScreenerPage
```

Expected: assertion failure (component not yet rendered).

- [ ] **Step 10.3: Implement**

In `DesktopScreenerPage.tsx`, import `ScreenerFreshnessLine` and place it directly under `<ScreenerFilterBar ...>`:

```tsx
<ScreenerFilterBar ... />
<ScreenerFreshnessLine freshness={results.freshness} />
{results.warnings.length > 0 && (
  <ul className="screener-warnings" aria-label="warnings">
    ...
  </ul>
)}
```

- [ ] **Step 10.4: Run; verify pass**

```bash
cd frontend/invest && npm run -s test -- DesktopScreenerPage ScreenerFreshnessLine
cd frontend/invest && npm run -s typecheck
```

Expected: both green.

- [ ] **Step 10.5: Commit**

```bash
git add frontend/invest/src/pages/desktop/DesktopScreenerPage.tsx frontend/invest/src/__tests__/DesktopScreenerPage.test.tsx
git commit -m "feat(invest-screener-ui): render freshness line above warnings (ROB-168)"
```

---

## Task 11: Full local validation gate

**Files:** none.

- [ ] **Step 11.1: Backend full sweep**

```bash
cd /Users/mgh3326/worktrees/auto_trader/ROB-168-screener-streak-freshness
uv run pytest tests/test_invest_screener_schemas.py tests/test_invest_screener_presets.py tests/test_invest_view_model_screener_service.py tests/test_screener_service.py tests/test_screening_consecutive_up_days.py tests/test_invest_view_model_safety.py tests/integration/test_screener_e2e.py -q
```

Expected: all green. If `test_invest_view_model_safety.py` fails, see Task 5 escape hatch (move `calculate_consecutive_up_days` into a math-only module).

- [ ] **Step 11.2: Frontend full sweep**

```bash
cd frontend/invest && npm run -s typecheck && npm run -s test
```

Expected: green.

- [ ] **Step 11.3: Lint**

```bash
cd /Users/mgh3326/worktrees/auto_trader/ROB-168-screener-streak-freshness
uv run ruff check app/ tests/
uv run ruff format --check app/ tests/
```

Expected: clean.

---

## Task 12: PR + post-merge smoke

**Files:** none.

- [ ] **Step 12.1: Push branch**

```bash
cd /Users/mgh3326/worktrees/auto_trader/ROB-168-screener-streak-freshness
git push -u origin kanban/ROB-168-screener-streak-freshness
```

- [ ] **Step 12.2: Open PR**

Title: `feat(invest-screener): consecutive 5-day streak + data freshness (ROB-168)`

Body (HEREDOC):

```
## Summary
- Backend now actually applies the `연속 5일 상승` filter for the `consecutive_gainers` preset (generic `min_consecutive_up_days` parameter, hardcoded to 5 in the preset).
- `/invest/api/screener/results` now returns a `freshness` block (`asOfLabel`, `relativeLabel`, `source`) derived from the existing upstream `timestamp` + `cache_hit` fields.
- Desktop SPA renders `2026.05.10 14:30 기준 · n분 전 갱신` above the warnings list. Falls back to `전 거래일 기준 · YYYY.MM.DD HH:mm 종가` when the KR market is closed.

## Test plan
- [x] `pytest` (selectors in plan Task 11.1)
- [x] `npm run typecheck && npm run test` in `frontend/invest`
- [x] Ruff clean
- [ ] Post-deploy smoke: visit `/invest/screener`, confirm freshness line and `≥5일` metric on every visible row.
```

- [ ] **Step 12.3: After CI green, merge via squash**

(Operator step — not Claude.)

- [ ] **Step 12.4: Production smoke checklist**

After deploy of merged commit:

1. `curl -fsS https://<prod-host>/invest/api/screener/results?preset=consecutive_gainers&market=kr | jq '.freshness, (.results | map(.metricValueLabel))'` — `freshness` present, every metric label ends in `일` and parses ≥5.
2. Open `/invest/screener` in a browser:
   - Freshness line visible immediately under filter chips.
   - `consecutive_gainers` rows show `5일`+; switching presets does not break the page.
   - Toggle 미국 → page renders, freshness line updates.
3. If any acceptance check fails, file a follow-up Linear issue and revert via PR (do NOT hot-edit `current/`).

---

## Self-Review Notes (planner)

- **Spec coverage**: every bullet from the kanban body is mapped — streak applied (Tasks 3-5), freshness (Tasks 1-2 + 8-10), generic-vs-fixed answered up top, smoke checklist in Task 12.
- **Type consistency**: `ScreenerFreshness` shape identical between Pydantic (Task 1) and TS (Task 8); `source` literal is `"live" | "cached" | "previous_session"` in both; `min_consecutive_up_days` is `int` (1-30) end-to-end.
- **Safety boundaries**: only `app.mcp_server.tooling.screening.*` (which is allowed to import brokers) calls OHLCV. `app.services.invest_view_model.*` only imports the pure `calculate_consecutive_up_days` function, so `tests/test_invest_view_model_safety.py` stays green. No broker/order/watch/scheduler module is modified.
- **Cache key impact**: `min_consecutive_up_days` is included in `_screening_cache_key`, so existing cached entries without it will not collide — they simply miss and refetch once.

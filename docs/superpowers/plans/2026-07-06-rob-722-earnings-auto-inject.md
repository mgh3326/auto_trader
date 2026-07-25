# ROB-722 Earnings Auto-Inject Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Auto-inject each US/KR equity symbol's upcoming-earnings context (or an explicit "no earnings" signal) into `analyze_stock_batch` compact responses, reusing the ROB-711 attach rail.

**Architecture:** New tooling module `app/mcp_server/tooling/earnings_context.py` reuses the already-validated `handle_get_earnings_calendar` handler (US live Finnhub / KR market_events DB) plus a pure compact shaper. A new sibling `_attach_earnings` in `analysis_tool_handlers.py` runs as a batched, symbol-level fail-open post-pass right after `_attach_decision_history`. No new vendor/auth/schema. Migration 0.

**Tech Stack:** Python 3.13, async SQLAlchemy, pytest (`pytest.mark.asyncio`), existing MCP tooling layer.

## Global Constraints

- **No LLM providers** — pure deterministic shaping (ROB-501 static guard scans `app/**`). No provider imports.
- **Migration 0** — no schema/model changes.
- **Fail-open always** — any earnings lookup failure MUST leave the analysis result untouched; never raise out of the attach pass.
- **Compact contract only** — attach only when `quick=True` (mirrors decision_history scope). Skip crypto and error rows.
- **Layer discipline** — `earnings_context.py` lives in the tooling layer (same as `analysis_tool_handlers` and `_financials`); no service→tooling inversion.
- **Attached key name:** `earnings` (verified free of collision with `_summarize_analysis_result` output).
- Commit message trailer on every commit:
  ```
  Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01UG8sLTN1Kko1PuenppjvD8
  ```

## File Structure

- **Create** `app/mcp_server/tooling/earnings_context.py` — `_map_timing`, `_compact_earnings` (pure), `_kr_ingestion_freshness` (DB), `build_earnings_context` (dispatch + crypto skip + shape).
- **Create** `tests/mcp_server/test_earnings_context.py` — unit tests for shaper, freshness, build dispatch, crypto skip, no-earnings.
- **Modify** `app/mcp_server/tooling/analysis_tool_handlers.py` — add `_attach_earnings`, import `build_earnings_context`, wire call after `_attach_decision_history` (~:942).
- **Create** `tests/mcp_server/test_analyze_stock_batch_earnings.py` — attach-pass injection/fail-open/skip tests.

---

## Task 1: Pure compact shaper (`_map_timing` + `_compact_earnings`)

**Files:**
- Create: `app/mcp_server/tooling/earnings_context.py`
- Test: `tests/mcp_server/test_earnings_context.py`

**Interfaces:**
- Consumes: earnings tool-result dicts shaped by `handle_get_earnings_calendar` (US finnhub: keys `symbol`, `source="finnhub"`, `earnings:[{date, hour, eps_estimate, revenue_estimate, quarter, year}]`; KR market_events: `market="kr"`, `source="market_events"`, items add `time_hint`, `status`; error payload: key `error`).
- Produces:
  - `_map_timing(hour: str | None) -> str` → `"BMO"|"AMC"|"DMH"|"unknown"`.
  - `_compact_earnings(tool_result: dict, *, today: datetime.date, freshness: str, data_as_of: str | None) -> dict` → compact context (`symbol`, `market`, `as_of`, `window_days`, `has_upcoming`, `next_earnings`|`None`, `freshness`, `source`, optional `data_as_of`, optional `note`).

- [ ] **Step 1: Write the failing test**

```python
# tests/mcp_server/test_earnings_context.py
"""ROB-722 — earnings auto-inject context builder."""

from __future__ import annotations

import datetime

import pytest

from app.mcp_server.tooling import earnings_context as ec

TODAY = datetime.date(2026, 7, 6)


def test_map_timing_variants():
    assert ec._map_timing("bmo") == "BMO"
    assert ec._map_timing("AMC") == "AMC"
    assert ec._map_timing("dmh") == "DMH"
    assert ec._map_timing("") == "unknown"
    assert ec._map_timing(None) == "unknown"
    assert ec._map_timing("weird") == "unknown"


def test_compact_earnings_us_upcoming_picks_nearest_future():
    tool_result = {
        "symbol": "NVDA",
        "source": "finnhub",
        "earnings": [
            {"date": "2026-01-01", "hour": "amc"},  # past — excluded
            {"date": "2026-07-25", "hour": "bmo", "eps_estimate": 0.9},
            {"date": "2026-07-18", "hour": "amc", "eps_estimate": 0.84,
             "revenue_estimate": 26500000000, "quarter": 2, "year": 2026},
        ],
    }
    ctx = ec._compact_earnings(tool_result, today=TODAY, freshness="live", data_as_of=None)
    assert ctx["market"] == "us"
    assert ctx["source"] == "finnhub"
    assert ctx["freshness"] == "live"
    assert ctx["window_days"] == 30
    assert ctx["as_of"] == "2026-07-06"
    assert "data_as_of" not in ctx
    assert ctx["has_upcoming"] is True
    ne = ctx["next_earnings"]
    assert ne["date"] == "2026-07-18"       # nearest future, not the earlier past row
    assert ne["d_minus"] == 12
    assert ne["timing"] == "AMC"
    assert ne["eps_estimate"] == 0.84
    assert ne["quarter"] == 2


def test_compact_earnings_no_upcoming_is_explicit_signal():
    tool_result = {"symbol": "HCA", "source": "finnhub", "earnings": []}
    ctx = ec._compact_earnings(tool_result, today=TODAY, freshness="live", data_as_of=None)
    assert ctx["has_upcoming"] is False
    assert ctx["next_earnings"] is None
    assert ctx["note"] == "no scheduled earnings within 30 days"


def test_compact_earnings_kr_carries_freshness_and_data_as_of():
    tool_result = {
        "symbol": "005930",
        "market": "kr",
        "source": "market_events",
        "earnings": [
            {"date": "2026-07-25", "time_hint": "unknown", "status": "scheduled",
             "quarter": 2, "year": 2026},
        ],
    }
    ctx = ec._compact_earnings(
        tool_result, today=TODAY, freshness="stale", data_as_of="2026-07-01"
    )
    assert ctx["market"] == "kr"
    assert ctx["source"] == "market_events"
    assert ctx["freshness"] == "stale"
    assert ctx["data_as_of"] == "2026-07-01"
    assert ctx["next_earnings"]["timing"] == "unknown"
    assert ctx["next_earnings"]["status"] == "scheduled"


def test_compact_earnings_error_payload_degrades():
    tool_result = {"symbol": "NVDA", "source": "finnhub", "error": "429 quota"}
    ctx = ec._compact_earnings(tool_result, today=TODAY, freshness="live", data_as_of=None)
    assert ctx["has_upcoming"] is False
    assert ctx["next_earnings"] is None
    assert "degraded" in ctx["note"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/mcp_server/test_earnings_context.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.mcp_server.tooling.earnings_context'`

- [ ] **Step 3: Write minimal implementation**

```python
# app/mcp_server/tooling/earnings_context.py
"""ROB-722 — deterministic upcoming-earnings context for a symbol.

Read-only. No LLM (ROB-501), no schema change. Reuses the validated
handle_get_earnings_calendar dispatch (US live Finnhub / KR market_events DB)
and shapes a compact "next earnings D-n / timing / consensus, or explicit
no-earnings" signal. Attached to analyze_stock_batch compact responses so each
fresh analysis session sees the symbol's earnings proximity.

No-earnings is itself a signal (HCA: '30일 내 무실적' as an entry justification),
so a zero-earnings window yields has_upcoming=False + note — NOT omission.
Only crypto / non-equity symbols are omitted (no earnings concept).
"""

from __future__ import annotations

import datetime
from typing import Any

_WINDOW_DAYS = 30
_TIMING_MAP = {"bmo": "BMO", "amc": "AMC", "dmh": "DMH"}


def _map_timing(hour: str | None) -> str:
    if not hour:
        return "unknown"
    return _TIMING_MAP.get(hour.strip().lower(), "unknown")


def _compact_earnings(
    tool_result: dict[str, Any],
    *,
    today: datetime.date,
    freshness: str,
    data_as_of: str | None,
) -> dict[str, Any]:
    source = tool_result.get("source")
    market_label = "kr" if (
        tool_result.get("market") == "kr" or source == "market_events"
    ) else "us"

    ctx: dict[str, Any] = {
        "symbol": tool_result.get("symbol"),
        "market": market_label,
        "as_of": today.isoformat(),
        "window_days": _WINDOW_DAYS,
        "freshness": freshness,
        "source": source,
    }
    if data_as_of is not None:
        ctx["data_as_of"] = data_as_of

    if tool_result.get("error"):
        ctx["has_upcoming"] = False
        ctx["next_earnings"] = None
        ctx["note"] = f"earnings lookup degraded: {tool_result.get('error')}"
        return ctx

    upcoming: list[tuple[datetime.date, dict[str, Any]]] = []
    for item in tool_result.get("earnings") or []:
        raw = item.get("date")
        if not raw:
            continue
        try:
            edate = datetime.date.fromisoformat(raw)
        except (ValueError, TypeError):
            continue
        if edate >= today:
            upcoming.append((edate, item))

    if not upcoming:
        ctx["has_upcoming"] = False
        ctx["next_earnings"] = None
        ctx["note"] = f"no scheduled earnings within {_WINDOW_DAYS} days"
        return ctx

    upcoming.sort(key=lambda pair: pair[0])
    edate, item = upcoming[0]
    ctx["has_upcoming"] = True
    ctx["next_earnings"] = {
        "date": edate.isoformat(),
        "d_minus": (edate - today).days,
        "timing": _map_timing(item.get("hour") or item.get("time_hint")),
        "eps_estimate": item.get("eps_estimate"),
        "revenue_estimate": item.get("revenue_estimate"),
        "quarter": item.get("quarter"),
        "year": item.get("year"),
        "status": item.get("status"),
    }
    return ctx
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/mcp_server/test_earnings_context.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add app/mcp_server/tooling/earnings_context.py tests/mcp_server/test_earnings_context.py
git commit -m "feat(ROB-722): pure compact earnings shaper

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UG8sLTN1Kko1PuenppjvD8"
```

---

## Task 2: KR ingestion freshness (`_kr_ingestion_freshness`)

**Files:**
- Modify: `app/mcp_server/tooling/earnings_context.py`
- Test: `tests/mcp_server/test_earnings_context.py`

**Interfaces:**
- Consumes: `MarketEventIngestionPartition` (columns `market`, `category`, `status`, `finished_at`); reuses `STALE_AFTER_HOURS` (36h) + `_is_stale` + `_ensure_aware` from `app.services.market_events.freshness_service` (DRY on threshold).
- Produces: `async _kr_ingestion_freshness(db: AsyncSession) -> tuple[str, str | None]` → `(freshness, data_as_of)` where freshness ∈ `{"fresh","stale","unknown"}` and data_as_of is the newest succeeded `finished_at` date ISO (or None).

- [ ] **Step 1: Write the failing test**

```python
# append to tests/mcp_server/test_earnings_context.py
import datetime as _dt

from app.services.market_events.freshness_service import STALE_AFTER_HOURS


class _FakeScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeDB:
    def __init__(self, value):
        self._value = value

    async def execute(self, _stmt):
        return _FakeScalarResult(self._value)


@pytest.mark.asyncio
async def test_kr_freshness_none_partition_is_unknown():
    freshness, as_of = await ec._kr_ingestion_freshness(_FakeDB(None))
    assert freshness == "unknown"
    assert as_of is None


@pytest.mark.asyncio
async def test_kr_freshness_recent_is_fresh():
    recent = _dt.datetime.now(_dt.UTC) - _dt.timedelta(hours=1)
    freshness, as_of = await ec._kr_ingestion_freshness(_FakeDB(recent))
    assert freshness == "fresh"
    assert as_of == recent.date().isoformat()


@pytest.mark.asyncio
async def test_kr_freshness_old_is_stale():
    old = _dt.datetime.now(_dt.UTC) - _dt.timedelta(hours=STALE_AFTER_HOURS + 5)
    freshness, as_of = await ec._kr_ingestion_freshness(_FakeDB(old))
    assert freshness == "stale"
    assert as_of == old.date().isoformat()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/mcp_server/test_earnings_context.py -k kr_freshness -v`
Expected: FAIL — `AttributeError: module ... has no attribute '_kr_ingestion_freshness'`

- [ ] **Step 3: Write minimal implementation**

Add imports at the top of `earnings_context.py`:

```python
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.market_events import MarketEventIngestionPartition
from app.services.market_events.freshness_service import _ensure_aware, _is_stale
```

Add the function:

```python
async def _kr_ingestion_freshness(db: AsyncSession) -> tuple[str, str | None]:
    """Newest succeeded KR earnings ingestion → (freshness, data_as_of ISO|None).

    Global per (market=kr, category=earnings) — compute ONCE per batch, not per
    symbol. Reuses the market_events STALE_AFTER_HOURS threshold. Fail-open at
    the caller: a DB error leaves KR rows on ('unknown', None).
    """
    stmt = select(func.max(MarketEventIngestionPartition.finished_at)).where(
        MarketEventIngestionPartition.market == "kr",
        MarketEventIngestionPartition.category == "earnings",
        MarketEventIngestionPartition.status == "succeeded",
    )
    finished_at = (await db.execute(stmt)).scalar_one_or_none()
    if finished_at is None:
        return ("unknown", None)
    aware = _ensure_aware(finished_at)
    freshness = "stale" if _is_stale(aware, now=datetime.datetime.now(datetime.UTC)) else "fresh"
    return (freshness, aware.date().isoformat())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/mcp_server/test_earnings_context.py -k kr_freshness -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add app/mcp_server/tooling/earnings_context.py tests/mcp_server/test_earnings_context.py
git commit -m "feat(ROB-722): KR ingestion freshness derivation

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UG8sLTN1Kko1PuenppjvD8"
```

---

## Task 3: `build_earnings_context` dispatch + crypto skip

**Files:**
- Modify: `app/mcp_server/tooling/earnings_context.py`
- Test: `tests/mcp_server/test_earnings_context.py`

**Interfaces:**
- Consumes: `handle_get_earnings_calendar(symbol, from_date, to_date, market) -> dict` (from `app.mcp_server.tooling.fundamentals._financials`); `_compact_earnings`; `_kr_ingestion_freshness`.
- Produces: `async build_earnings_context(symbol: str, market: str, *, today: datetime.date | None = None, kr_freshness: tuple[str, str | None] | None = None) -> dict | None` — returns `None` for crypto/blank/non-equity market; else the compact context dict.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/mcp_server/test_earnings_context.py


@pytest.mark.asyncio
async def test_build_earnings_context_crypto_returns_none():
    ctx = await ec.build_earnings_context("BTC", "crypto", today=TODAY)
    assert ctx is None


@pytest.mark.asyncio
async def test_build_earnings_context_us_calls_handler_and_shapes(monkeypatch):
    async def _fake_handler(symbol, from_date, to_date, market):
        assert symbol == "NVDA"
        assert market == "us"
        assert from_date == "2026-07-06"
        assert to_date == "2026-08-05"  # today + 30d
        return {
            "symbol": "NVDA", "source": "finnhub",
            "earnings": [{"date": "2026-07-18", "hour": "amc", "eps_estimate": 0.84}],
        }

    monkeypatch.setattr(ec, "handle_get_earnings_calendar", _fake_handler)
    ctx = await ec.build_earnings_context("NVDA", "us", today=TODAY)
    assert ctx["market"] == "us"
    assert ctx["freshness"] == "live"
    assert ctx["has_upcoming"] is True
    assert ctx["next_earnings"]["d_minus"] == 12


@pytest.mark.asyncio
async def test_build_earnings_context_kr_uses_passed_freshness(monkeypatch):
    async def _fake_handler(symbol, from_date, to_date, market):
        return {
            "symbol": "005930", "market": "kr", "source": "market_events",
            "earnings": [{"date": "2026-07-25", "time_hint": "unknown"}],
        }

    monkeypatch.setattr(ec, "handle_get_earnings_calendar", _fake_handler)
    ctx = await ec.build_earnings_context(
        "005930", "kr", today=TODAY, kr_freshness=("stale", "2026-07-01")
    )
    assert ctx["freshness"] == "stale"
    assert ctx["data_as_of"] == "2026-07-01"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/mcp_server/test_earnings_context.py -k build_earnings -v`
Expected: FAIL — `AttributeError: module ... has no attribute 'build_earnings_context'`

- [ ] **Step 3: Write minimal implementation**

Add import at the top of `earnings_context.py`:

```python
from app.mcp_server.tooling.fundamentals._financials import (
    handle_get_earnings_calendar,
)
```

Add the function:

```python
_EQUITY_MARKETS = {"kr", "us"}


async def build_earnings_context(
    symbol: str,
    market: str,
    *,
    today: datetime.date | None = None,
    kr_freshness: tuple[str, str | None] | None = None,
) -> dict[str, Any] | None:
    """Compact upcoming-earnings context for one symbol, or None to omit.

    Omits (None) only for crypto / non-equity markets — earnings has no meaning
    there. For US/KR equities it ALWAYS returns a dict (no-earnings is an
    explicit has_upcoming=False signal). US freshness is "live"; KR freshness is
    taken from ``kr_freshness`` (computed once per batch by the caller)."""
    market_norm = (market or "").strip().lower()
    if market_norm not in _EQUITY_MARKETS:
        return None

    today = today or datetime.date.today()
    to_date = today + datetime.timedelta(days=_WINDOW_DAYS)

    tool_result = await handle_get_earnings_calendar(
        symbol, today.isoformat(), to_date.isoformat(), market_norm
    )

    if market_norm == "kr":
        freshness, data_as_of = kr_freshness or ("unknown", None)
    else:
        freshness, data_as_of = "live", None

    return _compact_earnings(
        tool_result, today=today, freshness=freshness, data_as_of=data_as_of
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/mcp_server/test_earnings_context.py -v`
Expected: PASS (all tests in file)

- [ ] **Step 5: Commit**

```bash
git add app/mcp_server/tooling/earnings_context.py tests/mcp_server/test_earnings_context.py
git commit -m "feat(ROB-722): build_earnings_context dispatch + crypto skip

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UG8sLTN1Kko1PuenppjvD8"
```

---

## Task 4: `_attach_earnings` batched fail-open pass + wiring

**Files:**
- Modify: `app/mcp_server/tooling/analysis_tool_handlers.py` (import ~:35, add `_attach_earnings` after `_attach_decision_history` ~:880, wire call ~:942)
- Test: `tests/mcp_server/test_analyze_stock_batch_earnings.py`

**Interfaces:**
- Consumes: `build_earnings_context(symbol, market, *, today, kr_freshness) -> dict | None`, `_kr_ingestion_freshness(db) -> tuple[str, str|None]`, `AsyncSessionLocal`.
- Produces: `async _attach_earnings(results: dict[str, Any], *, market: str | None) -> None` — mutates `results` in place, attaching `result["earnings"]` per non-error, non-crypto symbol.

- [ ] **Step 1: Write the failing test**

```python
# tests/mcp_server/test_analyze_stock_batch_earnings.py
"""ROB-722 — analyze_stock_batch earnings auto-inject attach pass."""

from __future__ import annotations

import pytest

from app.mcp_server.tooling import analysis_tool_handlers as h


@pytest.mark.asyncio
async def test_attach_earnings_injects_when_context_exists(monkeypatch):
    async def _fake_build(symbol, market, *, today=None, kr_freshness=None):
        return {"symbol": symbol, "market": market, "has_upcoming": False,
                "next_earnings": None}

    async def _fake_kr_fresh(db):
        return ("fresh", "2026-07-06")

    monkeypatch.setattr(h, "build_earnings_context", _fake_build, raising=False)
    monkeypatch.setattr(h, "_kr_ingestion_freshness", _fake_kr_fresh, raising=False)

    results = {"NVDA": {"symbol": "NVDA", "market_type": "us"}}
    await h._attach_earnings(results, market="us")

    assert results["NVDA"]["earnings"]["has_upcoming"] is False


@pytest.mark.asyncio
async def test_attach_earnings_fail_open_on_error(monkeypatch):
    async def _boom(symbol, market, *, today=None, kr_freshness=None):
        raise RuntimeError("finnhub down")

    monkeypatch.setattr(h, "build_earnings_context", _boom, raising=False)

    results = {"NVDA": {"symbol": "NVDA", "market_type": "us"}}
    await h._attach_earnings(results, market="us")  # must not raise

    assert "earnings" not in results["NVDA"]


@pytest.mark.asyncio
async def test_attach_earnings_skips_error_and_crypto_rows(monkeypatch):
    # Mirrors real build: None for crypto (skip), dict for equities. Error rows
    # are skipped by _attach_earnings before build is ever called.
    async def _fake_build(symbol, market, *, today=None, kr_freshness=None):
        if str(market).strip().lower() == "crypto":
            return None
        return {"symbol": symbol}

    monkeypatch.setattr(h, "build_earnings_context", _fake_build, raising=False)

    results = {
        "BADSYM": {"error": "not found"},
        "BTC": {"symbol": "BTC", "market_type": "crypto"},
        "NVDA": {"symbol": "NVDA", "market_type": "us"},
    }
    await h._attach_earnings(results, market=None)

    assert "earnings" not in results["BADSYM"]      # error row skipped pre-build
    assert "earnings" not in results["BTC"]         # build returns None → omit
    assert results["NVDA"]["earnings"] == {"symbol": "NVDA"}  # equity attached
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/mcp_server/test_analyze_stock_batch_earnings.py -v`
Expected: FAIL — `AttributeError: module ... has no attribute '_attach_earnings'`

- [ ] **Step 3: Write minimal implementation**

Add import near the existing decision_history import (~:35 in `analysis_tool_handlers.py`):

```python
from app.mcp_server.tooling.earnings_context import (
    _kr_ingestion_freshness,
    build_earnings_context,
)
```

Add the function immediately after `_attach_decision_history` (after ~:879):

```python
async def _attach_earnings(
    results: dict[str, Any],
    *,
    market: str | None,
) -> None:
    """ROB-722: inject per-symbol upcoming-earnings context (US live / KR DB).

    Batched (one session), symbol-level fail-open. No-earnings is an explicit
    signal (has_upcoming=False), so US/KR equity rows always receive an
    ``earnings`` field; crypto/error rows are skipped. KR ingestion freshness is
    computed at most once per batch and threaded into each build call.
    """
    if not any(
        isinstance(row, dict) and "error" not in row for row in results.values()
    ):
        return
    try:
        from app.core.db import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            kr_freshness: tuple[str, str | None] | None = None
            for sym, result in results.items():
                if not isinstance(result, dict) or "error" in result:
                    continue
                mkt = result.get("market_type") or market
                if str(mkt or "").strip().lower() == "kr" and kr_freshness is None:
                    try:
                        kr_freshness = await _kr_ingestion_freshness(db)
                    except Exception:  # fail-open: freshness is advisory
                        kr_freshness = ("unknown", None)
                try:
                    ctx = await build_earnings_context(
                        str(sym), str(mkt or ""), kr_freshness=kr_freshness
                    )
                except Exception as exc:  # symbol-level fail-open (e.g. 429)
                    logger.debug("earnings injection skipped for %s: %s", sym, exc)
                    continue
                if ctx is not None:
                    result["earnings"] = ctx
    except Exception as exc:  # fail-open: advisory-only
        logger.debug("earnings injection skipped: %s", exc)
```

Wire the call in `analyze_stock_batch_impl` immediately after the `_attach_decision_history` line (~:942):

```python
    if quick:
        await _attach_fresh_artifact_hints(response.get("results", {}), market=market)
        await _attach_decision_history(response.get("results", {}), market=market)
        await _attach_earnings(response.get("results", {}), market=market)
    return response
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/mcp_server/test_analyze_stock_batch_earnings.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add app/mcp_server/tooling/analysis_tool_handlers.py tests/mcp_server/test_analyze_stock_batch_earnings.py
git commit -m "feat(ROB-722): wire _attach_earnings into analyze_stock_batch

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UG8sLTN1Kko1PuenppjvD8"
```

---

## Task 5: Full-suite gate (lint + typecheck + LLM guard + regression)

**Files:** none (verification only).

- [ ] **Step 1: Run the two new test modules + the decision_history regression**

Run:
```bash
uv run pytest tests/mcp_server/test_earnings_context.py \
  tests/mcp_server/test_analyze_stock_batch_earnings.py \
  tests/mcp_server/test_analyze_stock_batch_decision_history.py \
  tests/test_mcp_earnings_calendar.py -v
```
Expected: PASS (all).

- [ ] **Step 2: Static LLM-import guard (ROB-501) must still pass**

Run: `uv run pytest tests/services/action_report/snapshot_backed/test_no_internal_llm_imports.py -v`
Expected: PASS.

- [ ] **Step 3: Lint + typecheck the touched files**

Run: `make lint` (Ruff + ty). If unavailable, `uv run ruff check app/mcp_server/tooling/earnings_context.py app/mcp_server/tooling/analysis_tool_handlers.py`
Expected: no new findings.

- [ ] **Step 4: Confirm migration 0**

Run: `git status --short` and confirm no files under `alembic/versions/` were added.
Expected: only `app/mcp_server/tooling/earnings_context.py`, `app/mcp_server/tooling/analysis_tool_handlers.py`, and the two test files (plus docs) changed.

- [ ] **Step 5: Commit (only if lint/format applied changes)**

```bash
git add -A
git commit -m "chore(ROB-722): lint/format pass

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UG8sLTN1Kko1PuenppjvD8"
```

---

## Self-Review

**Spec coverage:**
- US live Finnhub path → Task 3 (`build_earnings_context` market="us", freshness="live") + Task 1 shaper. ✓
- KR market_events DB path + stale marker → Task 2 (`_kr_ingestion_freshness`) + Task 3 (kr branch). ✓
- No-earnings explicit signal (HCA) → Task 1 (`has_upcoming=False` + note). ✓
- Attach rail sibling to `_attach_decision_history`, wired after it → Task 4. ✓
- Symbol-level fail-open for US rate-limit → Task 4 inner try/except + handler's own 429→`_error_payload` degrade (Task 1 error branch). ✓
- crypto skip → Task 3 (`_EQUITY_MARKETS` guard → None) + Task 4 (None → omit). ✓
- Tests: US live-Finnhub + KR DB + no-earnings + fail-open + crypto → Tasks 1/3/4. ✓
- migration 0, no LLM imports → Task 5 gates. ✓

**Placeholder scan:** none — every code step shows complete code; no TBD/TODO. ✓

**Type consistency:** `build_earnings_context(symbol, market, *, today, kr_freshness)` and `_kr_ingestion_freshness(db) -> tuple[str, str|None]` signatures identical across Tasks 2/3/4 and their tests. `_compact_earnings(tool_result, *, today, freshness, data_as_of)` identical in Tasks 1/3. Attached key `earnings` consistent. ✓

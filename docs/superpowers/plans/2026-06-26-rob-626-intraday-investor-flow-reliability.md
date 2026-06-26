# ROB-626 — Intraday Investor-Flow Reliability + 외인수급 Self-Sufficiency Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `get_intraday_investor_flow` return a deterministic, never-falsely-`observed` freshness label and embed the confirmed multi-day Naver series (외/기/개 순매수 + 외인소진율) so the foreign-supply decision needs only one tool; add a daily ownership-trend flag to `get_investor_trends`.

**Architecture:** A single captured `now` is threaded through all time logic. A new shared module `fundamentals/_investor_flow_common.py` fetches the confirmed daily Naver series (best-effort) and computes ownership summary/trend; both `get_intraday_investor_flow` (embed + freshness anchor) and `get_investor_trends` (daily enrichment) consume it. The intraday classifier is rewritten as a pure function of `(now, slot_time, market_state, last_confirmed_date)` with a 6-rule conservative truth table.

**Tech Stack:** Python 3.13, `uv`, pytest (async), ruff + ty, FastMCP tools. Data sources: KIS `investor-trend-estimate` (already wired), Naver Finance investor table via `_fetch_investor_trends_naver` (already wired).

**Spec:** `docs/superpowers/specs/2026-06-25-rob-626-intraday-investor-flow-reliability-design.md`

## Global Constraints

- **Migration: 0** — no new DB table/column; reuse existing fetchers. Do not touch `alembic/`.
- **KR-only** — US/crypto code paths unchanged.
- **Back-compat** — every existing top-level key of `get_intraday_investor_flow` stays present with unchanged meaning. The ONLY changed-behavior label is the new `confidence` value `provisional_unconfirmed` (replaces the old `observed`/`inferred` in the ambiguous post-14:30 / after-close-unconfirmed window). `as_of` stays `null` for `carry_over` and `provisional_unconfirmed` (never a fabricated time).
- **No in-process LLM imports** — do not import Gemini/OpenAI/etc. (static guard `tests/services/action_report/snapshot_backed/test_no_internal_llm_imports.py`). N/A here but keep in mind.
- **Lint gate** — `make lint` must pass (`ruff check app/ tests/`, `ruff format --check`, `ty check app/`). Run `ruff format` before committing.
- **Commit trailers** (repo convention) — every commit ends with:
  ```
  Co-Authored-By: Paperclip <noreply@paperclip.ing>
  Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01GaYfeLjZbFtkhS2Y8Dh1Gp
  ```
- **Tests must mock all network** — KIS via `MockKISClient`, Naver via mocking `build_confirmed_block` / `_fetch_investor_trends_naver` (never real HTTP; shared-DB-free).

## File Structure

- **Create:** `app/mcp_server/tooling/fundamentals/_investor_flow_common.py` — pure ownership helpers + best-effort confirmed-daily block builder. One responsibility: the Naver-confirmed daily series + its derived summary, shared by both tools.
- **Modify:** `app/mcp_server/tooling/fundamentals/_valuation.py` — import the shared `holding_rate_change`; add daily ownership summary to `handle_get_investor_trends`.
- **Modify:** `app/mcp_server/tooling/fundamentals/_intraday_investor_flow.py` — rewrite `_classify_session` (pure, new signature), thread one `now`, wire `build_confirmed_block`, add new output fields.
- **Modify:** `app/mcp_server/tooling/fundamentals_handlers.py:230-248` — tool description.
- **Modify:** `app/mcp_server/README.md` — tool contract docs.
- **Test:** `tests/test_mcp_fundamentals_tools.py` — new common-module tests, classifier truth-table, updated + new `TestGetIntradayInvestorFlow`, `get_investor_trends` daily flag test.

---

### Task 1: Shared ownership helpers (`_investor_flow_common.py`)

**Files:**
- Create: `app/mcp_server/tooling/fundamentals/_investor_flow_common.py`
- Modify: `app/mcp_server/tooling/fundamentals/_valuation.py:170-180` (replace local `_holding_rate_change` def with import)
- Test: `tests/test_mcp_fundamentals_tools.py` (new `TestInvestorFlowCommon` class)

**Interfaces:**
- Produces:
  - `derive_individual_net(institutional_net, foreign_net) -> int | None`
  - `holding_rate_change(rows_newest_first: list[dict]) -> float | None`
  - `ownership_trend(rate_change: float | None) -> str | None`  → `"up" | "down" | "flat" | None`
  - `ownership_summary(rows_newest_first: list[dict]) -> dict` → keys `foreign_ownership_pct`, `foreign_ownership_trend`, `foreign_ownership_rate_change`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_mcp_fundamentals_tools.py` (near the other fundamentals tests):

```python
from app.mcp_server.tooling.fundamentals import _investor_flow_common as ifc


class TestInvestorFlowCommon:
    def test_derive_individual_net(self):
        assert ifc.derive_individual_net(2969153, -596340) == -2372813
        assert ifc.derive_individual_net(None, -596340) is None
        assert ifc.derive_individual_net(100, None) is None

    def test_holding_rate_change(self):
        rows = [{"foreign_holding_rate": 47.41}, {"foreign_holding_rate": 47.83}]
        assert ifc.holding_rate_change(rows) == -0.42
        assert ifc.holding_rate_change([]) is None
        assert ifc.holding_rate_change([{"foreign_holding_rate": None},
                                        {"foreign_holding_rate": 47.0}]) is None

    def test_ownership_trend(self):
        assert ifc.ownership_trend(-0.42) == "down"
        assert ifc.ownership_trend(0.42) == "up"
        assert ifc.ownership_trend(0.0) == "flat"
        assert ifc.ownership_trend(0.005) == "flat"
        assert ifc.ownership_trend(None) is None

    def test_ownership_summary(self):
        rows = [{"foreign_holding_rate": 47.41}, {"foreign_holding_rate": 47.83}]
        summary = ifc.ownership_summary(rows)
        assert summary == {
            "foreign_ownership_pct": 47.41,
            "foreign_ownership_trend": "down",
            "foreign_ownership_rate_change": -0.42,
        }
        assert ifc.ownership_summary([]) == {
            "foreign_ownership_pct": None,
            "foreign_ownership_trend": None,
            "foreign_ownership_rate_change": None,
        }
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_mcp_fundamentals_tools.py::TestInvestorFlowCommon -v`
Expected: FAIL with `ModuleNotFoundError: ...._investor_flow_common`

- [ ] **Step 3: Create the module**

Create `app/mcp_server/tooling/fundamentals/_investor_flow_common.py`:

```python
"""ROB-626: shared confirmed-daily investor-flow helpers (Naver-backed).

Pure ownership/derivation helpers + a best-effort confirmed-daily block
builder, shared by ``get_investor_trends`` (daily enrichment) and
``get_intraday_investor_flow`` (confirmed block embed + freshness anchor).
"""

from __future__ import annotations

from typing import Any

from app.mcp_server.tooling.fundamentals_sources_naver import (
    _fetch_investor_trends_naver,
)

# Ownership-rate delta below this magnitude (pp) reads as flat, not up/down.
_OWNERSHIP_FLAT_EPS = 0.01


def derive_individual_net(
    institutional_net: Any, foreign_net: Any
) -> int | None:
    """개인 순매수 = -(기관 + 외인). 한쪽이라도 None이면 None."""
    if institutional_net is None or foreign_net is None:
        return None
    return -(int(institutional_net) + int(foreign_net))


def holding_rate_change(
    rows_newest_first: list[dict[str, Any]],
) -> float | None:
    """ROB-448: 외인 보유율 델타 (newest − oldest, pp). 끝점 결측이면 None."""
    if not rows_newest_first:
        return None
    newest = rows_newest_first[0].get("foreign_holding_rate")
    oldest = rows_newest_first[-1].get("foreign_holding_rate")
    if newest is None or oldest is None:
        return None
    return round(newest - oldest, 2)


def ownership_trend(rate_change: float | None) -> str | None:
    """rate_change(pp) → 'up' | 'down' | 'flat' | None."""
    if rate_change is None:
        return None
    if abs(rate_change) < _OWNERSHIP_FLAT_EPS:
        return "flat"
    return "up" if rate_change > 0 else "down"


def ownership_summary(
    rows_newest_first: list[dict[str, Any]],
) -> dict[str, Any]:
    """{foreign_ownership_pct, foreign_ownership_trend, foreign_ownership_rate_change}."""
    pct = (
        rows_newest_first[0].get("foreign_holding_rate")
        if rows_newest_first
        else None
    )
    change = holding_rate_change(rows_newest_first)
    return {
        "foreign_ownership_pct": pct,
        "foreign_ownership_trend": ownership_trend(change),
        "foreign_ownership_rate_change": change,
    }
```

- [ ] **Step 4: Swap `_valuation` to the shared `holding_rate_change`**

In `app/mcp_server/tooling/fundamentals/_valuation.py`, DELETE the local `_holding_rate_change` function (currently :170-180) and add an import alias near the other fundamentals imports (after line 12):

```python
from app.mcp_server.tooling.fundamentals._investor_flow_common import (
    holding_rate_change as _holding_rate_change,
)
```

(The `_aggregate_investor_data` call site `_holding_rate_change(rows_sorted)` at ~:230 is unchanged — same signature, newest-first input. Import ONLY `holding_rate_change` here — `ownership_summary` is added to this import in Task 3 where it is first used, to avoid an unused-import lint failure now.)

- [ ] **Step 5: Run tests to verify they pass + existing week/month path still green**

Run: `uv run pytest tests/test_mcp_fundamentals_tools.py::TestInvestorFlowCommon -v`
Expected: PASS (4 tests)

Run: `uv run pytest tests/test_mcp_fundamentals_tools.py -k "investor_trends or InvestorTrends" -v`
Expected: PASS (existing investor-trends tests, incl. week/month aggregation, still green)

- [ ] **Step 6: Lint + commit**

Run: `uv run ruff format app/mcp_server/tooling/fundamentals/ tests/test_mcp_fundamentals_tools.py && uv run ruff check app/ tests/ && uv run ty check app/mcp_server/tooling/fundamentals/`
Expected: clean

```bash
git add app/mcp_server/tooling/fundamentals/_investor_flow_common.py \
        app/mcp_server/tooling/fundamentals/_valuation.py \
        tests/test_mcp_fundamentals_tools.py
git commit  # message: "feat(ROB-626): shared investor-flow ownership helpers" + trailers
```

---

### Task 2: Best-effort confirmed-daily block builder

**Files:**
- Modify: `app/mcp_server/tooling/fundamentals/_investor_flow_common.py` (add `build_confirmed_block`)
- Test: `tests/test_mcp_fundamentals_tools.py::TestInvestorFlowCommon`

**Interfaces:**
- Consumes: `_fetch_investor_trends_naver(symbol, days) -> dict` (returns `{"data": [rows], "source": "naver", ...}`; each row has `date` (ISO str), `close`, `institutional_net`, `foreign_net`, `foreign_holding_rate`).
- Produces: `async build_confirmed_block(symbol: str, days: int = 5) -> tuple[dict, str | None]` → `(block, last_confirmed_date)`.
  - `block` keys: `source` (`"naver"`), `foreign_ownership_pct`, `foreign_ownership_trend`, `foreign_ownership_rate_change`, `history` (list of `{date, foreign_net, institutional_net, individual_net, close}`, newest-first), `days`; on fetch failure also `error` and empty `history`.
  - `last_confirmed_date`: ISO date string of the newest confirmed row, or `None` (no rows / fetch failed).

- [ ] **Step 1: Write the failing tests**

Add to `TestInvestorFlowCommon`:

```python
    async def test_build_confirmed_block_success(self, monkeypatch):
        async def fake_fetch(symbol, days):
            assert symbol == "005930" and days == 5
            return {"source": "naver", "data": [
                {"date": "2026-06-24", "close": 340500,
                 "institutional_net": 2969153, "foreign_net": -596340,
                 "foreign_holding_rate": 47.41},
                {"date": "2026-06-23", "close": 310000,
                 "institutional_net": -4359775, "foreign_net": -2251501,
                 "foreign_holding_rate": 47.83},
            ]}
        monkeypatch.setattr(ifc, "_fetch_investor_trends_naver", fake_fetch)

        block, last_confirmed = await ifc.build_confirmed_block("005930", days=5)

        assert last_confirmed == "2026-06-24"
        assert block["source"] == "naver"
        assert block["foreign_ownership_pct"] == 47.41
        assert block["foreign_ownership_trend"] == "down"
        assert block["foreign_ownership_rate_change"] == -0.42
        assert block["days"] == 2
        assert block["history"][0] == {
            "date": "2026-06-24", "foreign_net": -596340,
            "institutional_net": 2969153, "individual_net": -2372813,
            "close": 340500,
        }
        assert "error" not in block

    async def test_build_confirmed_block_degrades_on_fetch_error(self, monkeypatch):
        async def boom(symbol, days):
            raise RuntimeError("naver down")
        monkeypatch.setattr(ifc, "_fetch_investor_trends_naver", boom)

        block, last_confirmed = await ifc.build_confirmed_block("005930")

        assert last_confirmed is None
        assert block["source"] == "naver"
        assert block["error"] == "naver down"
        assert block["history"] == []
        assert block["days"] == 0
        assert block["foreign_ownership_pct"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest "tests/test_mcp_fundamentals_tools.py::TestInvestorFlowCommon::test_build_confirmed_block_success" -v`
Expected: FAIL with `AttributeError: module ... has no attribute 'build_confirmed_block'`

- [ ] **Step 3: Implement `build_confirmed_block`**

Append to `_investor_flow_common.py`:

```python
async def build_confirmed_block(
    symbol: str, days: int = 5
) -> tuple[dict[str, Any], str | None]:
    """Best-effort Naver 확정 일별 블록 + freshness 앵커.

    Returns ``(block, last_confirmed_date)``. Naver 페치 예외를 흡수하여 intraday
    도구 전체 실패를 막는다(열화: error 키 + 빈 history).
    """
    try:
        fetched = await _fetch_investor_trends_naver(symbol, days)
        rows = fetched.get("data") or []
    except Exception as exc:  # noqa: BLE001 — best-effort degrade
        return (
            {
                "source": "naver",
                "error": str(exc),
                "foreign_ownership_pct": None,
                "foreign_ownership_trend": None,
                "foreign_ownership_rate_change": None,
                "history": [],
                "days": 0,
            },
            None,
        )

    history = [
        {
            "date": row.get("date"),
            "foreign_net": row.get("foreign_net"),
            "institutional_net": row.get("institutional_net"),
            "individual_net": derive_individual_net(
                row.get("institutional_net"), row.get("foreign_net")
            ),
            "close": row.get("close"),
        }
        for row in rows
    ]
    block = {
        "source": "naver",
        **ownership_summary(rows),
        "history": history,
        "days": len(history),
    }
    last_confirmed_date = rows[0].get("date") if rows else None
    return block, last_confirmed_date
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest "tests/test_mcp_fundamentals_tools.py::TestInvestorFlowCommon" -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Lint + commit**

Run: `uv run ruff format app/mcp_server/tooling/fundamentals/_investor_flow_common.py tests/test_mcp_fundamentals_tools.py && uv run ruff check app/ tests/ && uv run ty check app/mcp_server/tooling/fundamentals/`
Expected: clean

```bash
git add app/mcp_server/tooling/fundamentals/_investor_flow_common.py tests/test_mcp_fundamentals_tools.py
git commit  # "feat(ROB-626): best-effort confirmed-daily block builder" + trailers
```

---

### Task 3: `get_investor_trends` daily ownership-trend flag

**Files:**
- Modify: `app/mcp_server/tooling/fundamentals/_valuation.py:160-167` (`handle_get_investor_trends`)
- Test: `tests/test_mcp_fundamentals_tools.py` (new test in the investor-trends test area)

**Interfaces:**
- Consumes: `_ownership_summary` (imported in Task 1 Step 4).
- Produces: `get_investor_trends(symbol, days, period="day")` adds top-level `foreign_ownership_pct`, `foreign_ownership_trend`, `foreign_ownership_rate_change`.

- [ ] **Step 1: Write the failing test**

Add (next to the other get_investor_trends tests; uses the existing `build_tools` + Naver mock pattern — adapt the existing investor-trends mock if one exists, else):

```python
class TestGetInvestorTrendsOwnershipFlag:
    async def test_daily_period_adds_ownership_summary(self, monkeypatch):
        from app.mcp_server.tooling.fundamentals import _valuation as valuation_mod

        async def fake_fetch(symbol, days):
            return {"source": "naver", "data": [
                {"date": "2026-06-24", "close": 340500, "volume": 1,
                 "institutional_net": 2969153, "foreign_net": -596340,
                 "foreign_holding_rate": 47.41},
                {"date": "2026-06-23", "close": 310000, "volume": 1,
                 "institutional_net": -4359775, "foreign_net": -2251501,
                 "foreign_holding_rate": 47.83},
            ]}
        monkeypatch.setattr(valuation_mod, "_fetch_investor_trends_naver", fake_fetch)

        tools = build_tools()
        result = await tools["get_investor_trends"]("005930", 5, "day")

        assert result["period"] == "day"
        assert result["foreign_ownership_pct"] == 47.41
        assert result["foreign_ownership_trend"] == "down"
        assert result["foreign_ownership_rate_change"] == -0.42
        # existing per-row data unchanged (individual_net still derived)
        assert result["data"][0]["individual_net"] == -2372813
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest "tests/test_mcp_fundamentals_tools.py::TestGetInvestorTrendsOwnershipFlag" -v`
Expected: FAIL with `KeyError: 'foreign_ownership_pct'`

- [ ] **Step 3: Add the daily ownership summary**

First, extend the Task-1 import in `_valuation.py` to add `ownership_summary` (now first used here):

```python
from app.mcp_server.tooling.fundamentals._investor_flow_common import (
    holding_rate_change as _holding_rate_change,
    ownership_summary as _ownership_summary,
)
```

Then in `handle_get_investor_trends`, after `result["days"] = len(result["data"])` (currently :166), before `return result`:

```python
    if period == "day":
        result.update(_ownership_summary(result["data"]))
    return result
```

(`_ownership_summary` computes pct/trend/rate_change from the newest-first daily rows. Week/month already carry per-bucket `foreign_holding_rate_change`, so only daily needs the top-level summary.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest "tests/test_mcp_fundamentals_tools.py::TestGetInvestorTrendsOwnershipFlag" -v`
Expected: PASS

- [ ] **Step 5: Lint + commit**

Run: `uv run ruff format app/mcp_server/tooling/fundamentals/_valuation.py tests/test_mcp_fundamentals_tools.py && uv run ruff check app/ tests/ && uv run ty check app/mcp_server/tooling/fundamentals/`
Expected: clean

```bash
git add app/mcp_server/tooling/fundamentals/_valuation.py tests/test_mcp_fundamentals_tools.py
git commit  # "feat(ROB-626): get_investor_trends daily ownership-trend flag" + trailers
```

---

### Task 4: Deterministic classifier + handler integration

> **Single reviewable unit, single commit.** The classifier signature change breaks the old handler call site, and the existing handler tests change behavior — so the classifier rewrite and its handler wiring must land together (green only at the end). TDD runs in two cycles within this one task: (A) the pure classifier, then (B) the handler. The intermediate "classifier green / handler red" state is expected between steps; the task commits once when the whole file is green.

**Files:**
- Modify: `app/mcp_server/tooling/fundamentals/_intraday_investor_flow.py` (constants :38-53; `_classify_session` :87-134; `handle_get_intraday_investor_flow` :137-204; import :21)
- Test: `tests/test_mcp_fundamentals_tools.py` (new `TestClassifySession`; update + extend `TestGetIntradayInvestorFlow`)

**Interfaces:**
- Consumes: `build_confirmed_block(symbol, days=5) -> tuple[dict, str | None]` (Task 2).
- Produces:
  - `_classify_session(slot_time, *, now, market_state, last_confirmed_date) -> tuple[as_of, as_of_date, confidence, is_prior_session, today_available]`
  - constants `CONFIDENCE_PROVISIONAL_UNCONFIRMED = "provisional_unconfirmed"`, `_LAST_SLOT_TIME` (datetime.time 14:30)
  - `get_intraday_investor_flow(symbol)` output adds `today_available: bool`, `last_confirmed_session_date: str | None`, `confirmed: dict`; `confidence` may be `provisional_unconfirmed`.

**Cycle A — pure classifier (Steps 1-4):**

- [ ] **Step 1: Write the failing truth-table tests**

Add to `tests/test_mcp_fundamentals_tools.py`:

```python
class TestClassifySession:
    """Pure truth-table for _classify_session (no I/O). KST-aware now."""

    KST = intraday_investor_flow.KST

    def _now(self, y, mo, d, h, mi):
        import datetime as _dt
        return _dt.datetime(y, mo, d, h, mi, tzinfo=self.KST)

    def _classify(self, slot_time, now, market_state, last_confirmed, monkeypatch,
                  session_day=True, prior="2026-06-09"):
        import datetime as _dt
        monkeypatch.setattr(intraday_investor_flow, "is_kr_session_day",
                            lambda date: session_day)
        monkeypatch.setattr(intraday_investor_flow, "previous_kr_session",
                            lambda date: _dt.date.fromisoformat(prior))
        return intraday_investor_flow._classify_session(
            slot_time, now=now, market_state=market_state,
            last_confirmed_date=last_confirmed,
        )

    def test_no_rows(self, monkeypatch):
        out = self._classify(None, self._now(2026, 6, 10, 12, 0), "fresh",
                             "2026-06-09", monkeypatch)
        assert out == (None, None, None, False, False)

    def test_non_session_day_is_carry_over(self, monkeypatch):
        out = self._classify("14:30", self._now(2026, 6, 13, 15, 0), "market_closed",
                             "2026-06-09", monkeypatch, session_day=False,
                             prior="2026-06-12")
        as_of, as_of_date, conf, prior_sess, today_avail = out
        assert conf == "carry_over" and prior_sess is True
        assert as_of is None and as_of_date == "2026-06-12" and today_avail is False

    def test_future_slot_is_carry_over(self, monkeypatch):
        out = self._classify("14:30", self._now(2026, 6, 10, 11, 0), "fresh",
                             "2026-06-09", monkeypatch)
        as_of, as_of_date, conf, prior_sess, today_avail = out
        assert conf == "carry_over" and as_of is None
        assert as_of_date == "2026-06-09" and today_avail is False

    def test_today_confirmed_is_inferred(self, monkeypatch):
        out = self._classify("14:30", self._now(2026, 6, 10, 16, 0), "market_closed",
                             "2026-06-10", monkeypatch)
        as_of, as_of_date, conf, prior_sess, today_avail = out
        assert conf == "inferred" and today_avail is True
        assert as_of == "2026-06-10T14:30:00+09:00" and as_of_date == "2026-06-10"

    def test_observed_live_before_last_slot(self, monkeypatch):
        out = self._classify("11:20", self._now(2026, 6, 10, 12, 0), "fresh",
                             "2026-06-09", monkeypatch)
        as_of, as_of_date, conf, prior_sess, today_avail = out
        assert conf == "observed" and today_avail is True
        assert as_of == "2026-06-10T11:20:00+09:00" and as_of_date == "2026-06-10"

    def test_provisional_unconfirmed_live_after_last_slot(self, monkeypatch):
        out = self._classify("14:30", self._now(2026, 6, 10, 15, 0), "fresh",
                             "2026-06-09", monkeypatch)
        as_of, as_of_date, conf, prior_sess, today_avail = out
        assert conf == "provisional_unconfirmed" and today_avail is False
        assert as_of is None and as_of_date is None

    def test_provisional_unconfirmed_after_close_unconfirmed(self, monkeypatch):
        out = self._classify("14:30", self._now(2026, 6, 10, 16, 0), "market_closed",
                             "2026-06-09", monkeypatch)
        assert out[2] == "provisional_unconfirmed" and out[4] is False

    def test_stale_full_set_is_never_observed(self, monkeypatch):
        # Same stale "14:30" full set across the day → carry_over (am) then
        # provisional_unconfirmed (pm), NEVER observed.
        am = self._classify("14:30", self._now(2026, 6, 10, 10, 0), "fresh",
                            "2026-06-09", monkeypatch)
        pm = self._classify("14:30", self._now(2026, 6, 10, 15, 0), "fresh",
                            "2026-06-09", monkeypatch)
        assert am[2] == "carry_over"
        assert pm[2] == "provisional_unconfirmed"
        assert am[2] != "observed" and pm[2] != "observed"

    def test_deterministic_same_inputs(self, monkeypatch):
        now = self._now(2026, 6, 10, 12, 0)
        a = self._classify("11:20", now, "fresh", "2026-06-09", monkeypatch)
        b = self._classify("11:20", now, "fresh", "2026-06-09", monkeypatch)
        assert a == b
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest "tests/test_mcp_fundamentals_tools.py::TestClassifySession" -v`
Expected: FAIL — `_classify_session()` got an unexpected keyword argument `now` (old signature is positional `slot_time` only).

- [ ] **Step 3: Rewrite constants + `_classify_session`**

In `app/mcp_server/tooling/fundamentals/_intraday_investor_flow.py`:

(a) Add the new confidence constant after `CONFIDENCE_CARRY_OVER = "carry_over"` (currently :46):

```python
CONFIDENCE_PROVISIONAL_UNCONFIRMED = "provisional_unconfirmed"
```

(b) Add a module-level last-slot constant after the `_SLOT_TIMES` dict (after :31):

```python
# Latest publish slot (14:30). Past it, a stale full set is indistinguishable
# from a fresh one, so we refuse to claim `observed`.
_LAST_SLOT_TIME = max(
    datetime.time(int(_h), int(_m))
    for _h, _m in (t.split(":") for t in _SLOT_TIMES.values())
)
```

(c) Replace the whole `_classify_session` function (currently :87-134) with:

```python
def _classify_session(
    slot_time: str | None,
    *,
    now: datetime.datetime,
    market_state: str,
    last_confirmed_date: str | None,
) -> tuple[str | None, str | None, str | None, bool, bool]:
    """Deterministic session attribution for the latest KIS slot.

    Returns ``(as_of, as_of_date, confidence, is_prior_session,
    today_available)``. All time-dependent inputs are passed in (a single
    captured ``now``, the resolved ``market_state``, and the Naver-confirmed
    ``last_confirmed_date``), so the label is a pure function of its arguments —
    identical inputs always yield identical output, and a stale prior-session
    payload is never labeled ``observed``.

    Rules (first match wins):
      1. no rows → all null.
      2. not a session day → carry_over (prior-session leftover).
      3. latest slot in the future (incl. pre-open) → carry_over.
      4. not-future AND Naver already confirmed today → inferred.
      5. not-future, today unconfirmed, market fresh AND now < 14:30 → observed.
      6. otherwise (≥14:30 live full-set, or after-close unconfirmed) →
         provisional_unconfirmed (refuse to claim today).
    """
    if slot_time is None:
        return None, None, None, False, False

    today = now.date()
    hour, minute = (int(part) for part in slot_time.split(":", maxsplit=1))
    slot_dt = datetime.datetime.combine(
        today, datetime.time(hour=hour, minute=minute), tzinfo=KST
    )

    # Rule 2: weekend/holiday → rows belong to the prior session.
    if not is_kr_session_day(today):
        prior = previous_kr_session(today)
        return None, prior.isoformat(), CONFIDENCE_CARRY_OVER, True, False

    # Rule 3: future slot (a stale full set in the morning, or pre-open) cannot
    # be today's data.
    if slot_dt > now:
        prior = previous_kr_session(today)
        return None, prior.isoformat(), CONFIDENCE_CARRY_OVER, True, False

    today_iso = today.isoformat()

    # Rule 4: Naver already posted today's confirmed row → today, inferred.
    if last_confirmed_date == today_iso:
        return slot_dt.isoformat(), today_iso, CONFIDENCE_INFERRED, False, True

    # Rule 5: live session before the last slot → a stale full set would have
    # been caught as "future" above, so this is genuine-today.
    max_slot_dt = datetime.datetime.combine(today, _LAST_SLOT_TIME, tzinfo=KST)
    if market_state == DATA_STATE_FRESH and now < max_slot_dt:
        return slot_dt.isoformat(), today_iso, CONFIDENCE_OBSERVED, False, True

    # Rule 6: irreducibly ambiguous — refuse to claim today.
    return None, None, CONFIDENCE_PROVISIONAL_UNCONFIRMED, False, False
```

(Note: `now_kst` is no longer called inside `_classify_session`; it stays imported because the handler uses it — Task 5. `DATA_STATE_FRESH`, `is_kr_session_day`, `previous_kr_session`, `KST` are already imported at the top of the file.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest "tests/test_mcp_fundamentals_tools.py::TestClassifySession" -v`
Expected: PASS (9 tests)

(Cycle A complete — `_classify_session` is green in isolation. **Do NOT commit yet**: the handler still calls the old positional signature and its tests are red. Continue to Cycle B, commit at the end.)

**Cycle B — handler integration (Steps 5-10):**

- [ ] **Step 5: Update the existing 7 handler tests + add 4 (write the new expectations first)**

In `tests/test_mcp_fundamentals_tools.py`, edit `TestGetIntradayInvestorFlow`. Apply these changes:

**(i)** Add a module-level helper at the top of the class for the Naver mock:

```python
    @staticmethod
    def _mock_confirmed(monkeypatch, last_confirmed="2026-06-09", history=None):
        resolved_history = history if history is not None else [
            {"date": last_confirmed, "foreign_net": -596340,
             "institutional_net": 2969153, "individual_net": -2372813,
             "close": 340500},
        ]
        block = {
            "source": "naver",
            "foreign_ownership_pct": 47.41 if resolved_history else None,
            "foreign_ownership_trend": "down" if resolved_history else None,
            "foreign_ownership_rate_change": -0.42 if resolved_history else None,
            "history": resolved_history,
            "days": len(resolved_history),
        }

        async def fake_build(symbol, days=5):
            return block, last_confirmed

        monkeypatch.setattr(intraday_investor_flow, "build_confirmed_block", fake_build)
        return block
```

**(ii)** In EVERY existing test that reaches the success path (all except `test_rejects_non_kr_symbol` and `test_upstream_error_returns_error_payload`), (a) call `self._mock_confirmed(monkeypatch, ...)`, and (b) change the `kr_market_data_state` mock from `lambda: "..."` to `lambda *_a, **_k: "..."` (now takes a `now` arg).

**(iii)** Rename/repurpose `test_maps_latest_kis_intraday_estimate` (:5217) to assert the new ambiguous-window behavior, and add a dedicated observed test. Replace the body's tail (the assertions after the mocks) and the now/state per below:

```python
    async def test_maps_latest_kis_intraday_estimate(self, monkeypatch):
        # now 15:01 live + latest slot 14:30 → ambiguous → provisional_unconfirmed.
        import datetime as _dt
        tools = build_tools()

        class MockKISClient:
            async def investor_trend_estimate(self, code):
                assert code == "000660"
                return [
                    {"bsop_hour_gb": "1", "frgn_fake_ntby_qty": "-10000",
                     "orgn_fake_ntby_qty": "", "sum_fake_ntby_qty": "-10000"},
                    {"bsop_hour_gb": "5", "frgn_fake_ntby_qty": "-120000",
                     "orgn_fake_ntby_qty": "50000", "sum_fake_ntby_qty": "-70000"},
                ]

        monkeypatch.setattr(intraday_investor_flow, "KISClient", MockKISClient)
        monkeypatch.setattr(intraday_investor_flow, "now_kst",
            lambda: _dt.datetime(2026, 6, 10, 15, 1, tzinfo=intraday_investor_flow.KST))
        monkeypatch.setattr(intraday_investor_flow, "kr_market_data_state",
            lambda *_a, **_k: "fresh")
        self._mock_confirmed(monkeypatch, last_confirmed="2026-06-09")

        result = await tools["get_intraday_investor_flow"]("000660")

        assert result["source"] == "kis"
        assert result["confidence"] == "provisional_unconfirmed"
        assert result["today_available"] is False
        assert result["as_of"] is None
        assert result["as_of_date"] is None
        assert result["foreign_net_qty"] == -120000
        assert result["institution_net_qty"] == 50000
        assert result["combined_net_qty"] == -70000
        assert len(result["rows"]) == 2
        assert result["confirmed"]["foreign_ownership_pct"] == 47.41
        assert result["last_confirmed_session_date"] == "2026-06-09"

    async def test_observed_during_live_session_before_last_slot(self, monkeypatch):
        import datetime as _dt
        tools = build_tools()

        class MockKISClient:
            async def investor_trend_estimate(self, code):
                return [
                    {"bsop_hour_gb": "3", "frgn_fake_ntby_qty": "-120000",
                     "orgn_fake_ntby_qty": "50000", "sum_fake_ntby_qty": "-70000"},
                ]

        monkeypatch.setattr(intraday_investor_flow, "KISClient", MockKISClient)
        monkeypatch.setattr(intraday_investor_flow, "now_kst",
            lambda: _dt.datetime(2026, 6, 10, 12, 0, tzinfo=intraday_investor_flow.KST))
        monkeypatch.setattr(intraday_investor_flow, "is_kr_session_day", lambda d: True)
        monkeypatch.setattr(intraday_investor_flow, "kr_market_data_state",
            lambda *_a, **_k: "fresh")
        self._mock_confirmed(monkeypatch, last_confirmed="2026-06-09")

        result = await tools["get_intraday_investor_flow"]("000660")

        assert result["confidence"] == "observed"
        assert result["today_available"] is True
        assert result["as_of"] == "2026-06-10T11:20:00+09:00"
        assert result["as_of_date"] == "2026-06-10"
        assert result["warning"] is None

    async def test_inferred_when_naver_confirms_today(self, monkeypatch):
        import datetime as _dt
        tools = build_tools()

        class MockKISClient:
            async def investor_trend_estimate(self, code):
                return [
                    {"bsop_hour_gb": "5", "frgn_fake_ntby_qty": "-120000",
                     "orgn_fake_ntby_qty": "50000", "sum_fake_ntby_qty": "-70000"},
                ]

        monkeypatch.setattr(intraday_investor_flow, "KISClient", MockKISClient)
        monkeypatch.setattr(intraday_investor_flow, "now_kst",
            lambda: _dt.datetime(2026, 6, 10, 16, 0, tzinfo=intraday_investor_flow.KST))
        monkeypatch.setattr(intraday_investor_flow, "is_kr_session_day", lambda d: True)
        monkeypatch.setattr(intraday_investor_flow, "kr_market_data_state",
            lambda *_a, **_k: "market_closed")
        self._mock_confirmed(monkeypatch, last_confirmed="2026-06-10")

        result = await tools["get_intraday_investor_flow"]("000660")

        assert result["confidence"] == "inferred"
        assert result["today_available"] is True
        assert result["as_of"] == "2026-06-10T14:30:00+09:00"
        assert result["as_of_date"] == "2026-06-10"
        assert result["last_confirmed_session_date"] == "2026-06-10"
```

**(iv)** For `test_as_of_stamped_after_close_on_session_day` (:5410), change its meaning to provisional_unconfirmed (today unconfirmed) and add the mocks:

```python
    async def test_provisional_unconfirmed_after_close_today_unconfirmed(self, monkeypatch):
        import datetime as _dt
        tools = build_tools()

        class MockKISClient:
            async def investor_trend_estimate(self, code):
                return [
                    {"bsop_hour_gb": "5", "frgn_fake_ntby_qty": "-120000",
                     "orgn_fake_ntby_qty": "50000", "sum_fake_ntby_qty": "-70000"},
                ]

        monkeypatch.setattr(intraday_investor_flow, "KISClient", MockKISClient)
        monkeypatch.setattr(intraday_investor_flow, "now_kst",
            lambda: _dt.datetime(2026, 6, 10, 16, 0, tzinfo=intraday_investor_flow.KST))
        monkeypatch.setattr(intraday_investor_flow, "is_kr_session_day", lambda d: True)
        monkeypatch.setattr(intraday_investor_flow, "kr_market_data_state",
            lambda *_a, **_k: "market_closed")
        self._mock_confirmed(monkeypatch, last_confirmed="2026-06-09")

        result = await tools["get_intraday_investor_flow"]("000660")

        assert result["confidence"] == "provisional_unconfirmed"
        assert result["today_available"] is False
        assert result["as_of"] is None
        assert result["as_of_date"] is None
        assert result["warning"] is None
        assert result["combined_net_qty"] == -70000
```

**(v)** For `test_as_of_null_when_latest_slot_is_in_future` (:5308) and `test_as_of_null_on_non_session_day` (:5359): keep all existing assertions; ADD `self._mock_confirmed(monkeypatch, last_confirmed=...)` and change the `kr_market_data_state` lambda to `lambda *_a, **_k: "..."`. Add `assert result["today_available"] is False`.

**(vi)** For `test_returns_empty_success_when_kis_has_no_rows` (:5272): add `self._mock_confirmed(monkeypatch, last_confirmed=None)` (use `history=[]`), change the state lambda to `lambda *_a, **_k: "premarket_unavailable"`, and add `assert result["today_available"] is False` and `assert result["last_confirmed_session_date"] is None`.

**(vii)** Add the embedding + degradation + determinism tests:

```python
    async def test_naver_degraded_keeps_kis_block(self, monkeypatch):
        import datetime as _dt
        tools = build_tools()

        class MockKISClient:
            async def investor_trend_estimate(self, code):
                return [
                    {"bsop_hour_gb": "3", "frgn_fake_ntby_qty": "-120000",
                     "orgn_fake_ntby_qty": "50000", "sum_fake_ntby_qty": "-70000"},
                ]

        async def boom_build(symbol, days=5):
            return ({"source": "naver", "error": "naver down",
                     "foreign_ownership_pct": None, "foreign_ownership_trend": None,
                     "foreign_ownership_rate_change": None, "history": [], "days": 0},
                    None)

        monkeypatch.setattr(intraday_investor_flow, "KISClient", MockKISClient)
        monkeypatch.setattr(intraday_investor_flow, "now_kst",
            lambda: _dt.datetime(2026, 6, 10, 12, 0, tzinfo=intraday_investor_flow.KST))
        monkeypatch.setattr(intraday_investor_flow, "is_kr_session_day", lambda d: True)
        monkeypatch.setattr(intraday_investor_flow, "kr_market_data_state",
            lambda *_a, **_k: "fresh")
        monkeypatch.setattr(intraday_investor_flow, "previous_kr_session",
            lambda d: _dt.date(2026, 6, 9))
        monkeypatch.setattr(intraday_investor_flow, "build_confirmed_block", boom_build)

        result = await tools["get_intraday_investor_flow"]("000660")

        # KIS provisional block intact despite Naver failure.
        assert result["foreign_net_qty"] == -120000
        assert result["confidence"] == "observed"  # anchor unavailable → time-only
        assert result["confirmed"]["error"] == "naver down"
        assert result["last_confirmed_session_date"] == "2026-06-09"  # fallback

    async def test_freshness_label_is_deterministic_across_calls(self, monkeypatch):
        import datetime as _dt
        tools = build_tools()

        class MockKISClient:
            async def investor_trend_estimate(self, code):
                return [
                    {"bsop_hour_gb": "3", "frgn_fake_ntby_qty": "-120000",
                     "orgn_fake_ntby_qty": "50000", "sum_fake_ntby_qty": "-70000"},
                ]

        monkeypatch.setattr(intraday_investor_flow, "KISClient", MockKISClient)
        monkeypatch.setattr(intraday_investor_flow, "now_kst",
            lambda: _dt.datetime(2026, 6, 10, 12, 0, tzinfo=intraday_investor_flow.KST))
        monkeypatch.setattr(intraday_investor_flow, "is_kr_session_day", lambda d: True)
        monkeypatch.setattr(intraday_investor_flow, "kr_market_data_state",
            lambda *_a, **_k: "fresh")
        self._mock_confirmed(monkeypatch, last_confirmed="2026-06-09")

        r1 = await tools["get_intraday_investor_flow"]("000660")
        r2 = await tools["get_intraday_investor_flow"]("000660")
        for key in ("confidence", "as_of", "as_of_date", "today_available",
                    "last_confirmed_session_date"):
            assert r1[key] == r2[key]
```

- [ ] **Step 6: Run the tests to verify they fail**

Run: `uv run pytest "tests/test_mcp_fundamentals_tools.py::TestGetIntradayInvestorFlow" -v`
Expected: FAIL — handler doesn't yet pass `now`/`market_state`/`last_confirmed_date` to `_classify_session`, lacks `today_available`/`last_confirmed_session_date`/`confirmed` keys, and doesn't call `build_confirmed_block`.

- [ ] **Step 7: Add the new note constant + import + rewrite the handler**

In `_intraday_investor_flow.py`:

(a) Add the import near the top (after the `KISClient` import, :21):

```python
from app.mcp_server.tooling.fundamentals._investor_flow_common import (
    build_confirmed_block,
)
```

(b) Add a note constant after `_PRIOR_SESSION_NOTE` (:41):

```python
_UNCONFIRMED_NOTE = (
    " Today's data could not be positively confirmed (rows may belong to the "
    "current OR a prior session); as_of is null. See `confirmed` for the most "
    "recent confirmed daily series."
)
```

(c) Replace the whole `handle_get_intraday_investor_flow` body (:137-204) with:

```python
async def handle_get_intraday_investor_flow(symbol: str) -> dict[str, Any]:
    symbol = (symbol or "").strip()
    if not symbol:
        raise ValueError("symbol is required")

    if not _is_korean_equity_code(symbol):
        raise ValueError(
            "Intraday investor flow is only available for Korean stocks "
            "(6-digit codes like '005930')"
        )

    now = now_kst()  # single capture, threaded through all time logic

    try:
        raw_rows = await KISClient().investor_trend_estimate(symbol)
    except Exception as exc:
        return _error_payload(
            source="kis",
            message=str(exc),
            symbol=symbol,
            instrument_type="equity_kr",
        )

    # Best-effort confirmed-daily anchor + embed (Naver). Never fails the tool.
    confirmed_block, last_confirmed_date = await build_confirmed_block(symbol, days=5)

    market_state = kr_market_data_state(now)

    rows = [_normalize_row(row) for row in raw_rows]
    rows.sort(key=_slot_sort_key)
    latest = rows[-1] if rows else None
    latest_time = latest.get("as_of_time_kst") if latest is not None else None
    (
        as_of,
        as_of_date,
        confidence,
        is_prior_session,
        today_available,
    ) = _classify_session(
        latest_time,
        now=now,
        market_state=market_state,
        last_confirmed_date=last_confirmed_date,
    )

    # Always-populated floor: Naver-recent if available, else previous session.
    if last_confirmed_date is not None:
        last_confirmed_session_date = last_confirmed_date
    elif latest_time is not None:
        last_confirmed_session_date = previous_kr_session(now.date()).isoformat()
    else:
        last_confirmed_session_date = None

    warning = (
        {
            "code": _CARRY_OVER_WARNING_CODE,
            "message": _CARRY_OVER_WARNING_MESSAGE,
        }
        if is_prior_session
        else None
    )

    if not rows:
        note = "No KIS provisional investor-flow rows were returned."
    elif is_prior_session:
        note = _PROVISIONAL_NOTE + _PRIOR_SESSION_NOTE
    elif confidence == CONFIDENCE_PROVISIONAL_UNCONFIRMED:
        note = _PROVISIONAL_NOTE + _UNCONFIRMED_NOTE
    else:
        note = _PROVISIONAL_NOTE

    return {
        "symbol": symbol,
        "instrument_type": "equity_kr",
        "source": "kis",
        "data_state": DATA_STATE_INTRADAY_PROVISIONAL,
        "market_session_state": market_state,
        "provisional": True,
        "as_of": as_of,
        "as_of_date": as_of_date,
        "confidence": confidence,
        "is_prior_session": is_prior_session,
        "today_available": today_available,
        "last_confirmed_session_date": last_confirmed_session_date,
        "warning": warning,
        "as_of_time_kst": latest_time,
        "foreign_net_qty": (
            latest.get("foreign_net_qty") if latest is not None else None
        ),
        "institution_net_qty": (
            latest.get("institution_net_qty") if latest is not None else None
        ),
        "combined_net_qty": (
            latest.get("combined_net_qty") if latest is not None else None
        ),
        "rows": rows,
        "confirmed": confirmed_block,
        "note": note,
    }
```

- [ ] **Step 8: Run the full intraday test class**

Run: `uv run pytest "tests/test_mcp_fundamentals_tools.py::TestGetIntradayInvestorFlow" -v`
Expected: PASS (all updated + new tests). If `test_naver_degraded_keeps_kis_block` fails on `last_confirmed_session_date`, confirm `previous_kr_session` is mocked in that test (it is, to 2026-06-09).

- [ ] **Step 9: Run the broader fundamentals suite (no regressions)**

Run: `uv run pytest tests/test_mcp_fundamentals_tools.py -q`
Expected: PASS (entire file green — both `TestClassifySession` and `TestGetIntradayInvestorFlow`)

- [ ] **Step 10: Lint + commit (single commit for the whole unit)**

Run: `uv run ruff format app/mcp_server/tooling/fundamentals/_intraday_investor_flow.py tests/test_mcp_fundamentals_tools.py && uv run ruff check app/ tests/ && uv run ty check app/mcp_server/tooling/`
Expected: clean

```bash
git add app/mcp_server/tooling/fundamentals/_intraday_investor_flow.py tests/test_mcp_fundamentals_tools.py
git commit  # "feat(ROB-626): deterministic freshness + confirmed-series embed in intraday flow" + trailers
```

---

### Task 5: Tool description + README contract docs

**Files:**
- Modify: `app/mcp_server/tooling/fundamentals_handlers.py:232-248`
- Modify: `app/mcp_server/README.md` (get_intraday_investor_flow section)

**Interfaces:** none (docs only).

- [ ] **Step 1: Update the MCP tool description**

Replace the `description=( ... )` block of `get_intraday_investor_flow` (currently :232-248) with:

```python
        description=(
            "Get same-day intraday provisional foreign/institution net-buy "
            "quantity estimates for a Korean stock, PLUS an embedded confirmed "
            "multi-day series. Korean stocks only. The KIS intraday payload "
            "carries no date, so session attribution is deterministic and "
            "conservative via these ADDITIVE fields: `confidence` ('observed' = "
            "KRX session live before 14:30 and the rows are positively today's; "
            "'inferred' = today's confirmed daily row already exists; "
            "'carry_over' = future slot or non-session day, rows belong to a "
            "prior session; 'provisional_unconfirmed' = could be today OR a prior "
            "session and today could NOT be positively confirmed — e.g. live "
            "after 14:30, or after close before the confirmed daily is posted), "
            "`today_available` (bool — true only when today's data is positively "
            "confirmed), `as_of_date` (ISO DATE; null for provisional_unconfirmed; "
            "prior XKRX session DATE for carry_over — never a fabricated time), "
            "`is_prior_session` (bool), `warning` ({code, message} when carry_over, "
            "else null), and `last_confirmed_session_date` (most recent confirmed "
            "session). `as_of` is a full ISO datetime only for observed/inferred "
            "and is null for carry_over/provisional_unconfirmed — never silently "
            "upgraded. The `confirmed` object (source 'naver') carries "
            "`foreign_ownership_pct` (외인소진율), `foreign_ownership_trend` "
            "(up/down/flat), and `history` (last 5 confirmed days of foreign/"
            "institution/individual net-buy + close). Existing `as_of`/`note` keys "
            "are unchanged for back-compat."
        ),
```

- [ ] **Step 2: Update the README**

Run: `grep -n "get_intraday_investor_flow" app/mcp_server/README.md`

In the located section, update the field list to add `today_available`, `last_confirmed_session_date`, the `confirmed` block (`foreign_ownership_pct`, `foreign_ownership_trend`, `history`), and the new `provisional_unconfirmed` confidence value with the same semantics as the tool description above. Keep the existing field docs intact.

- [ ] **Step 3: Verify the server still builds (description is valid)**

Run: `uv run pytest tests/test_mcp_fundamentals_tools.py -k "IntradayInvestorFlow" -q`
Expected: PASS (tool still registers/handles)

- [ ] **Step 4: Lint + commit**

Run: `uv run ruff format app/mcp_server/tooling/fundamentals_handlers.py && uv run ruff check app/`
Expected: clean

```bash
git add app/mcp_server/tooling/fundamentals_handlers.py app/mcp_server/README.md
git commit  # "docs(ROB-626): intraday investor-flow tool contract" + trailers
```

---

## Final verification

- [ ] **Full lint gate:** `make lint` → clean (`ruff check app/ tests/`, `ruff format --check`, `ty check app/`).
- [ ] **Targeted suite:** `uv run pytest tests/test_mcp_fundamentals_tools.py -q` → all green.
- [ ] **Broader MCP suite (catch cross-tool regressions):** `uv run pytest tests/ -k "fundamentals or investor or mcp_server" -q` → green.
- [ ] **No migration:** `git diff --name-only origin/main... | grep alembic` → empty.

## Self-review checklist (verify before opening PR)

- [ ] **Determinism:** identical `(now, rows, confirmed)` → identical label (test `test_freshness_label_is_deterministic_across_calls`, `TestClassifySession::test_deterministic_same_inputs`).
- [ ] **Never fake-observed:** stale full-set never `observed` at any time (test `test_stale_full_set_is_never_observed`).
- [ ] **Back-compat:** every original top-level key present; `as_of` null for carry_over preserved.
- [ ] **Degradation:** Naver down → KIS block intact, `confirmed.error` set (test `test_naver_degraded_keeps_kis_block`).

## Post-implementation / out-of-band actions (not code)

1. **File the separate Linear issue** (per spec §10): *"analyze_stock_batch 컨센서스 목표가 recency — 폭락/회복 국면에서 upside_pct 과대표시 (newest_opinion 날짜 가중/경고 강화)"* (Bug/Med, RobinCompany, project auto_trader). Reference ROB-626 '참고' section.
2. **Operator (post-merge):** redeploy + MCP restart, then live in-session smoke during KRX hours:
   - `get_intraday_investor_flow("005930")` and `("000660")` — confirm `confidence` is stable across repeated calls, `today_available` matches reality, and `confirmed.history` / `foreign_ownership_pct` populate.
3. **Validation note (spec §11):** observe whether KIS `investor-trend-estimate` resets at open during a live session; if the 14:30–15:30 `provisional_unconfirmed` window proves too conservative in practice, add the `inquire_investor` (FHKST01010900) strong anchor as a follow-up.

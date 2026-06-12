# ROB-537 Toss Candle Source Resume Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Resume the interrupted ROB-537 work by finishing the dirty KR minute Toss sync option, documenting the finished OHLCV routing behavior, and verifying the already-committed Toss candle integration.

**Architecture:** Treat commits through `d1cb02d5 feat(ROB-537): add Toss daily candle sync fallback` as completed baseline work. Preserve the current dirty KR candle sync changes, fix their test isolation and style issues, then add the remaining MCP README update and focused verification without reopening the completed DTO/client/OHLCV/daily-sync tasks.

**Tech Stack:** Python 3.13, pandas, pytest, pytest-asyncio, Ruff, Linear issue ROB-537 context.

---

## Current State

Completed and committed:

- `d3f2203f` parses Toss candle responses.
- `e3f970dd` types `TossReadClient.candles()`.
- `3ea2b668` normalizes Toss candles to OHLCV DataFrames.
- `88bd22a1` adds Toss OHLCV market-data helpers.
- `8d8dd601` wires Toss-first KR intraday and Toss US daily fallback in service layer.
- `363b7d54` mirrors Toss OHLCV routing in the MCP tool.
- `004769ec` adds Toss daily fetcher for `DailyCandleSyncService`.
- `d1cb02d5` adds Toss daily candle sync fallback.

Current dirty files:

- `app/jobs/kr_candles.py`
- `app/services/kr_candles_sync_service.py`
- `scripts/sync_kr_candles.py`
- `tests/test_kr_candles_sync.py`
- `docs/superpowers/plans/2026-06-12-rob-537-toss-candle-source.md` is an old untracked initial plan and should not be used as the resume source of truth.

Observed failures before this plan:

```bash
uv run pytest tests/test_kr_candles_sync.py -q
```

Result: 19 passed, 1 failed. The failing test is `test_sync_kr_candles_toss_source_warning`; it accidentally constructs the real `KISClient` path and tries to fetch a KIS token.

```bash
uv run ruff check app/jobs/kr_candles.py app/services/kr_candles_sync_service.py scripts/sync_kr_candles.py tests/test_kr_candles_sync.py
```

Result: one Ruff import-order failure in `tests/test_kr_candles_sync.py`.

## Remaining File Structure

- Modify: `tests/test_kr_candles_sync.py`
  - Isolate the Toss-source test from KIS network/token paths and fix import ordering.
- Modify: `app/jobs/kr_candles.py`
  - Format long call and keep `source` pass-through.
- Modify: `app/services/kr_candles_sync_service.py`
  - Keep explicit `source='toss'` path, but format long lines and tighten the result/test contract.
- Modify: `scripts/sync_kr_candles.py`
  - Keep `--source kis|toss` CLI pass-through; only style changes are expected.
- Modify: `app/mcp_server/README.md`
  - Document actual Toss-first and fallback behavior.
- Remove or leave untracked intentionally: `docs/superpowers/plans/2026-06-12-rob-537-toss-candle-source.md`
  - Prefer leaving it uncommitted unless the owner wants to keep the initial long plan.

---

### Task 1: Finish Dirty KR Minute Toss Sync Work

**Files:**
- Modify: `tests/test_kr_candles_sync.py`
- Modify: `app/jobs/kr_candles.py`
- Modify: `app/services/kr_candles_sync_service.py`
- Modify: `scripts/sync_kr_candles.py`

- [ ] **Step 1: Confirm current dirty failure**

Run:

```bash
uv run pytest tests/test_kr_candles_sync.py::test_sync_kr_candles_toss_source_warning -q
```

Expected: FAIL with `KeyError: 'access_token'` from the real KIS token path.

- [ ] **Step 2: Fix test imports**

In `tests/test_kr_candles_sync.py`, change the imports at the top from:

```python
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
```

to:

```python
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pandas as pd
import pytest
```

Remove the inner `import pandas as pd` from `test_sync_kr_candles_toss_source_warning`.

- [ ] **Step 3: Replace the Toss-source warning test with an isolated test**

Replace the entire current `test_sync_kr_candles_toss_source_warning` body in `tests/test_kr_candles_sync.py` with:

```python
@pytest.mark.asyncio
async def test_sync_kr_candles_toss_source_warning(monkeypatch):
    from app.services.kr_candles_sync_service import sync_kr_candles

    class DummyKISClient:
        async def fetch_my_stocks(self):
            return [{"pdno": "005930"}]

    class DummyManualHoldingsService:
        def __init__(self, session):
            self.session = session

        async def get_holdings_by_user(self, *, user_id, market_type):
            return []

    class DummySession:
        async def commit(self):
            return None

        async def rollback(self):
            return None

        async def close(self):
            return None

    session = DummySession()
    toss_df = pd.DataFrame(
        [
            {
                "datetime": pd.Timestamp("2026-06-12 09:00:00"),
                "open": 100.0,
                "high": 110.0,
                "low": 90.0,
                "close": 105.0,
                "volume": 1000.0,
                "value": 105000.0,
            }
        ]
    )
    upsert_mock = AsyncMock(return_value=1)

    monkeypatch.setattr(
        "app.services.kr_candles_sync_service.KISClient",
        DummyKISClient,
    )
    monkeypatch.setattr(
        "app.services.kr_candles_sync_service.ManualHoldingsService",
        DummyManualHoldingsService,
    )
    monkeypatch.setattr(
        "app.services.kr_candles_sync_service.AsyncSessionLocal",
        lambda: session,
    )
    monkeypatch.setattr(
        "app.services.kr_candles_sync_service._load_universe_context",
        AsyncMock(
            return_value=(
                [_make_universe_row("005930", nxt_eligible=True, is_active=True)],
                True,
            )
        ),
    )
    monkeypatch.setattr(
        "app.services.kr_candles_sync_service.fetch_kr_intraday_toss_frame",
        AsyncMock(return_value=toss_df),
    )
    monkeypatch.setattr(
        "app.services.kr_candles_sync_service._upsert_rows",
        upsert_mock,
    )

    result = await sync_kr_candles(mode="incremental", source="toss")

    assert result["source"] == "toss"
    assert result["rows_upserted"] == 1
    assert result["symbol_venues_total"] == 1
    assert result["pairs_processed"] == 1
    assert any("provider source column" in w for w in result.get("warnings", []))
    upsert_mock.assert_awaited_once()
```

- [ ] **Step 4: Format long production lines**

In `app/jobs/kr_candles.py`, replace:

```python
        result = await sync_kr_candles(mode=mode, sessions=sessions, user_id=user_id, source=source)
```

with:

```python
        result = await sync_kr_candles(
            mode=mode,
            sessions=sessions,
            user_id=user_id,
            source=source,
        )
```

In `app/services/kr_candles_sync_service.py`, replace:

```python
                    logger.error("Failed to sync Toss candles for symbol=%s: %s", symbol, exc, exc_info=True)
```

with:

```python
                    logger.error(
                        "Failed to sync Toss candles for symbol=%s: %s",
                        symbol,
                        exc,
                        exc_info=True,
                    )
```

- [ ] **Step 5: Run focused KR sync tests**

Run:

```bash
uv run pytest tests/test_kr_candles_sync.py -q
```

Expected: PASS.

- [ ] **Step 6: Run focused Ruff check**

Run:

```bash
uv run ruff check app/jobs/kr_candles.py app/services/kr_candles_sync_service.py scripts/sync_kr_candles.py tests/test_kr_candles_sync.py
```

Expected: PASS.

- [ ] **Step 7: Commit Task 1**

Run:

```bash
git add app/jobs/kr_candles.py app/services/kr_candles_sync_service.py scripts/sync_kr_candles.py tests/test_kr_candles_sync.py
git commit -m "feat(ROB-537): add explicit Toss source for KR minute sync"
```

Expected: commit succeeds. The untracked plan files should remain uncommitted unless deliberately added later.

---

### Task 2: Document Final OHLCV Routing

**Files:**
- Modify: `app/mcp_server/README.md`

- [ ] **Step 1: Find the `get_ohlcv` section**

Run:

```bash
sed -n '60,100p' app/mcp_server/README.md
```

Expected: output includes the `get_ohlcv(symbol, count=100, period="day", end_date=None, market=None, include_indicators=False)` bullet list.

- [ ] **Step 2: Update KR intraday bullets**

In `app/mcp_server/README.md`, replace the existing KR intraday bullets:

```markdown
  - KR intraday (`1m/5m/15m/30m/1h`) overlays the most recent 30 minutes from `public.kr_candles_1m` + KIS minute API to cover the unchanged 10-minute sync cadence
  - KR intraday includes the current partial bucket when minute data is available
```

with:

```markdown
  - KR intraday (`1m/5m/15m/30m/1h`) uses Toss candles first when `TOSS_API_ENABLED` is configured, then falls back to the existing DB/KIS reader
  - Toss only provides `1m`; `5m/15m/30m/1h` are aggregated from Toss `1m` using the same bucket rules as the KIS path
  - On Toss fallback, KR intraday overlays the most recent 30 minutes from `public.kr_candles_1m` + KIS minute API to cover the unchanged 10-minute sync cadence
  - KR intraday includes the current partial bucket when minute data is available
```

- [ ] **Step 3: Add US day-only Toss fallback bullet**

In the same bullet list, add this near the US OHLCV bullets:

```markdown
  - US daily uses Yahoo first and Toss as a `period="day"` fallback; US `week` and `month` remain Yahoo-only
```

- [ ] **Step 4: Run README grep check**

Run:

```bash
rg -n "Toss candles first|period=\"day\" fallback|Yahoo-only" app/mcp_server/README.md
```

Expected: all three phrases appear.

- [ ] **Step 5: Commit Task 2**

Run:

```bash
git add app/mcp_server/README.md
git commit -m "docs(ROB-537): document Toss OHLCV fallback routing"
```

Expected: commit succeeds.

---

### Task 3: Focused End-to-End Verification

**Files:**
- Read-only verification across committed ROB-537 surfaces.

- [ ] **Step 1: Run Toss broker tests**

Run:

```bash
uv run pytest tests/services/brokers/toss -q
```

Expected: PASS.

- [ ] **Step 2: Run market-data service tests**

Run:

```bash
uv run pytest tests/test_market_data_service.py -k "get_ohlcv or toss" -q
```

Expected: PASS.

- [ ] **Step 3: Run MCP OHLCV tests**

Run:

```bash
uv run pytest tests/test_mcp_ohlcv_tools.py -q
```

Expected: PASS.

- [ ] **Step 4: Run daily candle unit tests**

Run:

```bash
uv run pytest tests/unit/services/daily_candles -q
```

Expected: PASS.

- [ ] **Step 5: Run KR candle sync tests**

Run:

```bash
uv run pytest tests/test_kr_candles_sync.py -q
```

Expected: PASS.

- [ ] **Step 6: Run focused Ruff check**

Run:

```bash
uv run ruff check app/services/brokers/toss app/services/market_data app/services/daily_candles app/mcp_server/tooling/market_data_quotes.py app/services/kr_candles_sync_service.py app/jobs/kr_candles.py scripts/sync_kr_candles.py tests/services/brokers/toss tests/test_market_data_service.py tests/test_mcp_ohlcv_tools.py tests/unit/services/daily_candles tests/test_kr_candles_sync.py
```

Expected: PASS.

- [ ] **Step 7: Check final git state**

Run:

```bash
git status --short
```

Expected: only intentional plan files remain untracked, or a clean tree if the plan files were committed separately.

---

### Task 4: Plan File Cleanup Decision

**Files:**
- `docs/superpowers/plans/2026-06-12-rob-537-resume-plan.md`
- `docs/superpowers/plans/2026-06-12-rob-537-toss-candle-source.md`

- [ ] **Step 1: Decide which plan files to keep**

Use this default unless the owner says otherwise:

```text
Keep and commit: docs/superpowers/plans/2026-06-12-rob-537-resume-plan.md
Leave uncommitted or remove: docs/superpowers/plans/2026-06-12-rob-537-toss-candle-source.md
```

- [ ] **Step 2: Commit resume plan if desired**

Run:

```bash
git add docs/superpowers/plans/2026-06-12-rob-537-resume-plan.md
git commit -m "docs(ROB-537): add resume plan"
```

Expected: commit succeeds.

- [ ] **Step 3: Remove stale initial plan only if the owner does not want it**

Run this only for the untracked stale initial plan:

```bash
rm docs/superpowers/plans/2026-06-12-rob-537-toss-candle-source.md
```

Expected: the stale initial plan is removed from the working tree.

---

## Self-Review

- Spec coverage: Remaining ROB-537 scope after commits through Task 8 is covered by KR minute Toss sync completion, MCP docs, and focused verification.
- Placeholder scan: no unresolved placeholder tokens or unspecified implementation steps remain.
- Type consistency: Current function names `sync_kr_candles`, `run_kr_candles_sync`, `fetch_kr_intraday_toss_frame`, and `fetch_daily_toss_frame` match the codebase.
- Scope check: This resume plan avoids redoing committed Toss DTO/client/OHLCV/daily-sync work and focuses only on interrupted work plus documentation and verification.

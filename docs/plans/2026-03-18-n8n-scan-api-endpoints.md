# n8n Scan API Endpoints Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Expose strategy scan and crash detection as REST API endpoints under `/api/n8n/scan/` so n8n can orchestrate scheduling and Discord delivery instead of TaskIQ.

**Architecture:** Add `alert_mode="none"` to `DailyScanner` to skip alert sending while preserving Redis cooldowns. Create a new router `app/routers/n8n_scan.py` with two GET endpoints that instantiate DailyScanner, run scans, and return structured JSON. The existing n8n API key auth in `AuthMiddleware` automatically protects all `/api/n8n/*` routes.

**Tech Stack:** FastAPI, Pydantic v2, Redis (cooldowns), existing DailyScanner job

---

## Key Code References

| File | Role |
|------|------|
| `app/jobs/daily_scan.py` | `DailyScanner` class — scan logic, alert sending, cooldown recording |
| `app/tasks/daily_scan_tasks.py` | TaskIQ scheduled tasks (will be disabled later) |
| `app/routers/n8n.py` | Existing n8n router pattern (prefix `/api/n8n`, Pydantic responses) |
| `app/middleware/auth.py:111-119` | N8N_API_KEY auth for `/api/n8n/*` paths |
| `app/main.py:138-161` | Router registration block |
| `tests/test_daily_scan.py` | Existing scanner tests with `_FakeRedis` and `_DummyOpenClawClient` |

## Design Decisions

1. **Separate router file** (`n8n_scan.py`) rather than adding to `n8n.py` — the existing n8n router is already 458 lines with its own service-layer pattern. Scan endpoints directly call `DailyScanner` (a job), not a service.

2. **`_send_alert` returns `"none"` sentinel** when `alert_mode="none"` — this makes the existing `run_strategy_scan()` / `run_crash_detection()` flow work without structural changes: the non-None return triggers cooldown recording.

3. **Enriched return dict** — both `run_*` methods return `message` (formatted string) and `details` (structured dict) alongside existing `alerts_sent`. The existing `details: list[str]` field changes to `details: dict` — safe because TaskIQ tasks don't inspect return shape.

---

### Task 1: Add `alert_mode="none"` to DailyScanner

**Files:**
- Modify: `app/jobs/daily_scan.py:34-41` (constructor) and `app/jobs/daily_scan.py:612-630` (`_send_alert`)
- Test: `tests/test_daily_scan.py`

**Step 1: Write the failing test**

Add to `tests/test_daily_scan.py`:

```python
@pytest.mark.unit
async def test_alert_mode_none_skips_send(scanner_env):
    """alert_mode='none' should skip sending but return truthy value."""
    scanner, _, openclaw, _, _ = scanner_env
    scanner._alert_mode = "none"

    result = await scanner._send_alert("test message")

    assert result == "none"
    assert len(openclaw.messages) == 0
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_daily_scan.py::test_alert_mode_none_skips_send -xvs`
Expected: FAIL — `"none"` is not a valid `alert_mode` literal value / method doesn't handle it

**Step 3: Implement `alert_mode="none"`**

In `app/jobs/daily_scan.py`:

1. Update the type annotation on lines 37 and 41:
```python
# line 37
alert_mode: Literal["both", "telegram_only", "openclaw_only", "none"] = "both",
# line 41
self._alert_mode: Literal["both", "telegram_only", "openclaw_only", "none"] = alert_mode
```

2. Add early return to `_send_alert()` (insert at top of method, line 613):
```python
async def _send_alert(self, message: str) -> str | None:
    if self._alert_mode == "none":
        return "none"
    # ... rest unchanged
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_daily_scan.py::test_alert_mode_none_skips_send -xvs`
Expected: PASS

**Step 5: Run full existing test suite to check no regressions**

Run: `uv run pytest tests/test_daily_scan.py -x --timeout=60`
Expected: All existing tests PASS

**Step 6: Commit**

```bash
git add app/jobs/daily_scan.py tests/test_daily_scan.py
git commit -m "feat(daily-scan): add alert_mode='none' to skip sending alerts"
```

---

### Task 2: Enrich `run_strategy_scan()` return value

**Files:**
- Modify: `app/jobs/daily_scan.py:655-705` (`run_strategy_scan`)
- Test: `tests/test_daily_scan.py`

**Step 1: Write the failing test**

Add to `tests/test_daily_scan.py`:

```python
@pytest.mark.unit
async def test_run_strategy_scan_returns_message_and_details(scanner_env, monkeypatch):
    """run_strategy_scan should return message and structured details."""
    scanner, fake_redis, _, _, _ = scanner_env

    # Provide one oversold signal
    from app.jobs import daily_scan as ds_mod

    oversold_df = _make_ohlcv([50.0] * 20 + [20.0])  # RSI will be low
    monkeypatch.setattr(ds_mod, "fetch_ohlcv", AsyncMock(return_value=oversold_df))
    monkeypatch.setattr(
        ds_mod,
        "fetch_top_traded_coins",
        AsyncMock(return_value=[{"market": "KRW-TEST"}]),
    )
    monkeypatch.setattr(ds_mod, "fetch_my_coins", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        ds_mod,
        "fetch_multiple_tickers",
        AsyncMock(return_value=[{"signed_change_rate": -0.01}]),
    )
    monkeypatch.setattr(
        ds_mod,
        "get_fear_greed_index_impl",
        AsyncMock(return_value={"success": False}),
    )

    result = await scanner.run_strategy_scan()

    assert "message" in result
    assert "details" in result
    assert isinstance(result["details"], dict)
    if result["alerts_sent"] > 0:
        assert result["message"]  # non-empty string
        assert "buy_signals" in result["details"]
        assert "sell_signals" in result["details"]
        assert "sentiment_signals" in result["details"]
        assert "btc_context" in result["details"]
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_daily_scan.py::test_run_strategy_scan_returns_message_and_details -xvs`
Expected: FAIL — `message` key not in result dict

**Step 3: Modify `run_strategy_scan()` return values**

In `app/jobs/daily_scan.py`, replace the return statements in `run_strategy_scan()` (lines 655-705):

```python
async def run_strategy_scan(self) -> dict:
    if not settings.DAILY_SCAN_ENABLED:
        return {"skipped": True, "reason": "disabled"}

    btc_ctx = await self._get_btc_context()
    pending_cooldowns: list[tuple[str, str]] = []

    overbought_alerts = await self.check_overbought_holdings(
        btc_ctx,
        send_immediately=False,
        pending_cooldowns=pending_cooldowns,
    )
    oversold_alerts = await self.check_oversold_top30(
        btc_ctx,
        send_immediately=False,
        pending_cooldowns=pending_cooldowns,
    )
    fng_alerts = await self.check_fear_greed(
        send_immediately=False,
        pending_cooldowns=pending_cooldowns,
    )
    sma_alerts = await self.check_sma20_crossings(
        send_immediately=False,
        pending_cooldowns=pending_cooldowns,
    )

    buy_signals = [*oversold_alerts]
    sell_signals = [*overbought_alerts]
    for sma_alert in sma_alerts:
        if "골든크로스" in sma_alert:
            buy_signals.append(sma_alert)
        elif "데드크로스" in sma_alert:
            sell_signals.append(sma_alert)

    details = {
        "buy_signals": buy_signals,
        "sell_signals": sell_signals,
        "sentiment_signals": fng_alerts,
        "btc_context": btc_ctx,
    }

    if not buy_signals and not sell_signals and not fng_alerts:
        return {"alerts_sent": 0, "message": "", "details": details}

    batched_message = self._build_strategy_scan_batch_message(
        btc_ctx=btc_ctx,
        buy_signals=buy_signals,
        sell_signals=sell_signals,
        sentiment_signals=fng_alerts,
    )
    request_id = await self._send_alert(batched_message)
    if not request_id:
        return {"alerts_sent": 0, "message": "", "details": details}

    for symbol, alert_type in self._dedupe_pending_cooldowns(pending_cooldowns):
        await self._record_alert(symbol, alert_type)

    return {"alerts_sent": 1, "message": batched_message, "details": details}
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_daily_scan.py::test_run_strategy_scan_returns_message_and_details -xvs`
Expected: PASS

**Step 5: Run full suite**

Run: `uv run pytest tests/test_daily_scan.py -x --timeout=60`
Expected: All PASS (existing tests may need minor tweaks if they assert on exact `details` shape — check and fix)

**Step 6: Commit**

```bash
git add app/jobs/daily_scan.py tests/test_daily_scan.py
git commit -m "feat(daily-scan): enrich run_strategy_scan return with message and details"
```

---

### Task 3: Enrich `run_crash_detection()` return value

**Files:**
- Modify: `app/jobs/daily_scan.py:707-729` (`run_crash_detection`)
- Test: `tests/test_daily_scan.py`

**Step 1: Write the failing test**

Add to `tests/test_daily_scan.py`:

```python
@pytest.mark.unit
async def test_run_crash_detection_returns_message_and_details(scanner_env, monkeypatch):
    """run_crash_detection should return message and structured details."""
    scanner, fake_redis, _, _, _ = scanner_env

    from app.jobs import daily_scan as ds_mod

    monkeypatch.setattr(
        ds_mod,
        "fetch_top_traded_coins",
        AsyncMock(return_value=[{"market": "KRW-TEST"}]),
    )
    monkeypatch.setattr(ds_mod, "fetch_my_coins", AsyncMock(return_value=[]))
    # +15% change triggers crash detection
    monkeypatch.setattr(
        ds_mod,
        "fetch_multiple_tickers",
        AsyncMock(return_value=[{"market": "KRW-TEST", "signed_change_rate": 0.15}]),
    )

    result = await scanner.run_crash_detection()

    assert "message" in result
    assert "details" in result
    assert isinstance(result["details"], dict)
    if result["alerts_sent"] > 0:
        assert result["message"]  # non-empty string
        assert "crash_signals" in result["details"]
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_daily_scan.py::test_run_crash_detection_returns_message_and_details -xvs`
Expected: FAIL — `message` key not in result dict

**Step 3: Modify `run_crash_detection()` return values**

In `app/jobs/daily_scan.py`, replace `run_crash_detection()` (lines 707-729):

```python
async def run_crash_detection(self) -> dict:
    if not settings.DAILY_SCAN_ENABLED:
        return {"skipped": True, "reason": "disabled"}

    pending_cooldowns: list[tuple[str, str]] = []
    alerts = await self.check_price_crash(
        send_immediately=False,
        pending_cooldowns=pending_cooldowns,
    )

    details = {"crash_signals": alerts}

    if not alerts:
        return {"alerts_sent": 0, "message": "", "details": details}

    batched_message = self._build_crash_detection_batch_message(
        crash_signals=alerts
    )
    request_id = await self._send_alert(batched_message)
    if not request_id:
        return {"alerts_sent": 0, "message": "", "details": details}

    for symbol, alert_type in self._dedupe_pending_cooldowns(pending_cooldowns):
        await self._record_alert(symbol, alert_type)

    return {"alerts_sent": 1, "message": batched_message, "details": details}
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_daily_scan.py::test_run_crash_detection_returns_message_and_details -xvs`
Expected: PASS

**Step 5: Run full suite**

Run: `uv run pytest tests/test_daily_scan.py -x --timeout=60`
Expected: All PASS

**Step 6: Commit**

```bash
git add app/jobs/daily_scan.py tests/test_daily_scan.py
git commit -m "feat(daily-scan): enrich run_crash_detection return with message and details"
```

---

### Task 4: Create Pydantic response schemas

**Files:**
- Create: `app/schemas/n8n_scan.py`
- Test: `tests/test_n8n_scan_api.py` (schema validation in later tasks)

**Step 1: Create the schema file**

Create `app/schemas/n8n_scan.py`:

```python
"""Pydantic response schemas for n8n scan API endpoints."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class N8nStrategyScanDetails(BaseModel):
    buy_signals: list[str] = Field(default_factory=list)
    sell_signals: list[str] = Field(default_factory=list)
    sentiment_signals: list[str] = Field(default_factory=list)
    btc_context: str = ""


class N8nStrategyScanResponse(BaseModel):
    success: bool
    as_of: str
    scan_type: Literal["strategy"] = "strategy"
    alerts_sent: int = 0
    message: str = ""
    details: N8nStrategyScanDetails = Field(default_factory=N8nStrategyScanDetails)
    errors: list[dict] = Field(default_factory=list)


class N8nCrashScanDetails(BaseModel):
    crash_signals: list[str] = Field(default_factory=list)


class N8nCrashScanResponse(BaseModel):
    success: bool
    as_of: str
    scan_type: Literal["crash_detection"] = "crash_detection"
    alerts_sent: int = 0
    message: str = ""
    details: N8nCrashScanDetails = Field(default_factory=N8nCrashScanDetails)
    errors: list[dict] = Field(default_factory=list)
```

**Step 2: Verify schema import works**

Run: `uv run python -c "from app.schemas.n8n_scan import N8nStrategyScanResponse, N8nCrashScanResponse; print('OK')"`
Expected: `OK`

**Step 3: Commit**

```bash
git add app/schemas/n8n_scan.py
git commit -m "feat(schemas): add Pydantic response schemas for n8n scan endpoints"
```

---

### Task 5: Create router `app/routers/n8n_scan.py`

**Files:**
- Create: `app/routers/n8n_scan.py`
- Test: `tests/test_n8n_scan_api.py`

**Step 1: Write the failing tests**

Create `tests/test_n8n_scan_api.py`:

```python
"""Tests for n8n scan API endpoints."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient


def _build_test_client() -> TestClient:
    """Build test client with auth middleware bypassed."""
    with patch("app.middleware.auth.settings") as mock_settings:
        mock_settings.N8N_API_KEY = ""
        mock_settings.DOCS_ENABLED = False
        mock_settings.PUBLIC_API_PATHS = []
        # Import after patching to pick up middleware config
        from app.main import create_app

        app = create_app()
        return TestClient(app)


@pytest.fixture
def client():
    return _build_test_client()


@pytest.mark.unit
class TestStrategyScanEndpoint:
    def test_strategy_scan_success(self, client, monkeypatch):
        mock_result = {
            "alerts_sent": 1,
            "message": "🔎 크립토 스캔 (07:30)\n📌 BTC 컨텍스트: RSI14 63.5",
            "details": {
                "buy_signals": ["📉 TEST RSI 29.8"],
                "sell_signals": [],
                "sentiment_signals": [],
                "btc_context": "📌 BTC 컨텍스트: RSI14 63.5",
            },
        }
        with patch(
            "app.routers.n8n_scan.DailyScanner"
        ) as MockScanner:
            instance = MockScanner.return_value
            instance.run_strategy_scan = AsyncMock(return_value=mock_result)
            instance.close = AsyncMock()

            resp = client.get("/api/n8n/scan/strategy")

        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert body["scan_type"] == "strategy"
        assert body["alerts_sent"] == 1
        assert body["message"]
        assert body["details"]["buy_signals"] == ["📉 TEST RSI 29.8"]

    def test_strategy_scan_no_signals(self, client):
        mock_result = {
            "alerts_sent": 0,
            "message": "",
            "details": {
                "buy_signals": [],
                "sell_signals": [],
                "sentiment_signals": [],
                "btc_context": "📌 BTC 컨텍스트: RSI14 63.5",
            },
        }
        with patch(
            "app.routers.n8n_scan.DailyScanner"
        ) as MockScanner:
            instance = MockScanner.return_value
            instance.run_strategy_scan = AsyncMock(return_value=mock_result)
            instance.close = AsyncMock()

            resp = client.get("/api/n8n/scan/strategy")

        assert resp.status_code == 200
        body = resp.json()
        assert body["alerts_sent"] == 0

    def test_strategy_scan_exception_returns_500(self, client):
        with patch(
            "app.routers.n8n_scan.DailyScanner"
        ) as MockScanner:
            instance = MockScanner.return_value
            instance.run_strategy_scan = AsyncMock(
                side_effect=RuntimeError("upstream failure")
            )
            instance.close = AsyncMock()

            resp = client.get("/api/n8n/scan/strategy")

        assert resp.status_code == 500
        body = resp.json()
        assert body["success"] is False
        assert body["errors"]


@pytest.mark.unit
class TestCrashScanEndpoint:
    def test_crash_scan_success(self, client):
        mock_result = {
            "alerts_sent": 1,
            "message": "크래시 감지 스캔 (05:00)\n\n변동성 경보\n- TEST 24h +11.00%",
            "details": {
                "crash_signals": ["TEST 24h +11.00% — 급등 감지"],
            },
        }
        with patch(
            "app.routers.n8n_scan.DailyScanner"
        ) as MockScanner:
            instance = MockScanner.return_value
            instance.run_crash_detection = AsyncMock(return_value=mock_result)
            instance.close = AsyncMock()

            resp = client.get("/api/n8n/scan/crash")

        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert body["scan_type"] == "crash_detection"
        assert body["alerts_sent"] == 1
        assert body["details"]["crash_signals"]

    def test_crash_scan_no_alerts(self, client):
        mock_result = {
            "alerts_sent": 0,
            "message": "",
            "details": {"crash_signals": []},
        }
        with patch(
            "app.routers.n8n_scan.DailyScanner"
        ) as MockScanner:
            instance = MockScanner.return_value
            instance.run_crash_detection = AsyncMock(return_value=mock_result)
            instance.close = AsyncMock()

            resp = client.get("/api/n8n/scan/crash")

        assert resp.status_code == 200
        body = resp.json()
        assert body["alerts_sent"] == 0

    def test_crash_scan_exception_returns_500(self, client):
        with patch(
            "app.routers.n8n_scan.DailyScanner"
        ) as MockScanner:
            instance = MockScanner.return_value
            instance.run_crash_detection = AsyncMock(
                side_effect=RuntimeError("upstream failure")
            )
            instance.close = AsyncMock()

            resp = client.get("/api/n8n/scan/crash")

        assert resp.status_code == 500
        body = resp.json()
        assert body["success"] is False
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_n8n_scan_api.py -xvs`
Expected: FAIL — `app.routers.n8n_scan` does not exist

**Step 3: Create the router**

Create `app/routers/n8n_scan.py`:

```python
"""n8n scan API endpoints — strategy scan and crash detection."""

from __future__ import annotations

import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.core.timezone import now_kst
from app.jobs.daily_scan import DailyScanner
from app.schemas.n8n_scan import (
    N8nCrashScanDetails,
    N8nCrashScanResponse,
    N8nStrategyScanDetails,
    N8nStrategyScanResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/n8n/scan", tags=["n8n-scan"])


@router.get("/strategy", response_model=N8nStrategyScanResponse)
async def strategy_scan() -> N8nStrategyScanResponse | JSONResponse:
    """Run crypto strategy scan (overbought/oversold/SMA20/F&G)."""
    as_of = now_kst().replace(microsecond=0).isoformat()
    scanner = DailyScanner(alert_mode="none")
    try:
        result = await scanner.run_strategy_scan()
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to run strategy scan")
        payload = N8nStrategyScanResponse(
            success=False,
            as_of=as_of,
            errors=[{"error": str(exc)}],
        )
        return JSONResponse(status_code=500, content=payload.model_dump())
    finally:
        await scanner.close()

    if result.get("skipped"):
        return N8nStrategyScanResponse(
            success=True,
            as_of=as_of,
            alerts_sent=0,
            message=f"Skipped: {result.get('reason', 'unknown')}",
        )

    details_raw = result.get("details", {})
    return N8nStrategyScanResponse(
        success=True,
        as_of=as_of,
        alerts_sent=result.get("alerts_sent", 0),
        message=result.get("message", ""),
        details=N8nStrategyScanDetails(**details_raw),
    )


@router.get("/crash", response_model=N8nCrashScanResponse)
async def crash_scan() -> N8nCrashScanResponse | JSONResponse:
    """Run crash detection scan (rapid price movements)."""
    as_of = now_kst().replace(microsecond=0).isoformat()
    scanner = DailyScanner(alert_mode="none")
    try:
        result = await scanner.run_crash_detection()
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to run crash detection scan")
        payload = N8nCrashScanResponse(
            success=False,
            as_of=as_of,
            errors=[{"error": str(exc)}],
        )
        return JSONResponse(status_code=500, content=payload.model_dump())
    finally:
        await scanner.close()

    if result.get("skipped"):
        return N8nCrashScanResponse(
            success=True,
            as_of=as_of,
            alerts_sent=0,
            message=f"Skipped: {result.get('reason', 'unknown')}",
        )

    details_raw = result.get("details", {})
    return N8nCrashScanResponse(
        success=True,
        as_of=as_of,
        alerts_sent=result.get("alerts_sent", 0),
        message=result.get("message", ""),
        details=N8nCrashScanDetails(**details_raw),
    )
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_n8n_scan_api.py -xvs`
Expected: FAIL — router not registered in app yet (endpoints return 404). That's expected — we register in the next task.

**Step 5: Commit (router created but not yet wired)**

```bash
git add app/routers/n8n_scan.py tests/test_n8n_scan_api.py
git commit -m "feat(routers): add n8n scan router with strategy and crash endpoints"
```

---

### Task 6: Register router in `app/main.py`

**Files:**
- Modify: `app/main.py:21-38` (imports) and `app/main.py:138-161` (include_router)

**Step 1: Add import**

In `app/main.py`, add `n8n_scan` to the import block (line 27, after `n8n`):

```python
from app.routers import (
    analysis_json,
    dashboard,
    deprecated_pages,
    health,
    kospi200,
    n8n,
    n8n_scan,  # ADD THIS LINE
    news_analysis,
    openclaw_callback,
    orderbook,
    portfolio,
    screener,
    stock_latest,
    symbol_settings,
    test,
    trading,
    websocket,
)
```

**Step 2: Register router**

Add after line 147 (`app.include_router(n8n.router)`):

```python
app.include_router(n8n_scan.router)
```

**Step 3: Run the API tests now**

Run: `uv run pytest tests/test_n8n_scan_api.py -xvs`
Expected: All PASS (endpoints now return 200/500 as expected)

**Step 4: Verify auth middleware protects the new routes**

Run: `uv run pytest tests/test_n8n_api_key_auth.py -xvs`
Expected: All PASS (middleware already covers `/api/n8n/*`)

**Step 5: Run full test suite**

Run: `uv run pytest tests/ -x -m "not live" --timeout=120`
Expected: All PASS

**Step 6: Commit**

```bash
git add app/main.py
git commit -m "feat(main): register n8n_scan router"
```

---

### Task 7: Verify existing TaskIQ tasks still work

**Files:**
- Read only: `app/tasks/daily_scan_tasks.py`, `tests/test_daily_scan_tasks.py`

**Step 1: Run TaskIQ task tests**

Run: `uv run pytest tests/test_daily_scan_tasks.py -xvs`
Expected: All PASS — the tasks use `alert_mode="telegram_only"` which is unchanged

**Step 2: Verify return shape compatibility**

The TaskIQ tasks just `return await scanner.run_strategy_scan()` — adding `message` and `details` keys to the return dict is additive and non-breaking. No changes needed.

**Step 3: Run lint**

Run: `uv run ruff check app/jobs/daily_scan.py app/routers/n8n_scan.py app/schemas/n8n_scan.py app/main.py`
Expected: No errors

**Step 4: Run format**

Run: `uv run ruff format app/jobs/daily_scan.py app/routers/n8n_scan.py app/schemas/n8n_scan.py app/main.py`

---

### Task 8: Final integration verification

**Step 1: Run full unit test suite**

Run: `uv run pytest tests/ -x -m "not live" --timeout=120`
Expected: All PASS

**Step 2: Run lint and format on all changed files**

Run: `make lint`
Expected: Clean

**Step 3: Verify OpenAPI docs include new endpoints**

Run: `uv run python -c "from app.main import create_app; app = create_app(); print([r.path for r in app.routes if '/n8n/scan' in getattr(r, 'path', '')])"`
Expected: `['/api/n8n/scan/strategy', '/api/n8n/scan/crash']`

**Step 4: Final commit (if any format/lint fixes)**

```bash
git add -A
git commit -m "chore: lint and format fixes for n8n scan endpoints"
```

---

## Files Changed Summary

| Action | File |
|--------|------|
| Modify | `app/jobs/daily_scan.py` — add `alert_mode="none"`, enrich return values |
| Create | `app/schemas/n8n_scan.py` — Pydantic response schemas |
| Create | `app/routers/n8n_scan.py` — GET `/strategy` and `/crash` endpoints |
| Modify | `app/main.py` — import and register new router |
| Create | `tests/test_n8n_scan_api.py` — endpoint tests |
| Modify | `tests/test_daily_scan.py` — tests for new alert_mode and return shapes |

## NOT in scope (deferred)

- TaskIQ schedule removal — do after n8n workflows are confirmed stable
- Pydantic schema for `app/schemas/n8n.py` re-export — separate concern
- n8n workflow creation — not code, done in n8n UI

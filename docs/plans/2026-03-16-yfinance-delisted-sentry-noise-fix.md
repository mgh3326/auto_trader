# yfinance "possibly delisted" Sentry Noise Fix

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Filter yfinance "possibly delisted" ERROR log events from creating Sentry issues, fixing AUTO_TRADER-E and AUTO_TRADER-F.

**Architecture:** yfinance internally logs at ERROR level when `yf.Ticker().fast_info` encounters tickers with missing chart data (e.g., recently created leveraged ETFs). The Sentry `LoggingIntegration(event_level=logging.ERROR)` captures these as Sentry events. Our holdings code already handles these failures gracefully via `price_error` per position — the Sentry events are pure noise. The fix adds a targeted filter in `_before_send` and `_before_send_log` to drop yfinance "possibly delisted" messages.

**Tech Stack:** Python, Sentry SDK, pytest

**Sentry Issues:**
- [AUTO_TRADER-E](https://mgh3326-daum.sentry.io/issues/7337385961/): `$DIREXION TESLA 2X: possibly delisted; no price data found (period=1y)`
- [AUTO_TRADER-F](https://mgh3326-daum.sentry.io/issues/7337385977/): `$DIREXION TESLA 2X: possibly delisted; no price data found (period=5d)`

**Root Cause Analysis:**
1. `get_holdings` → `_fetch_price_map_for_positions` → `_fetch_quote_equity_us` → `yahoo_service.fetch_fast_info`
2. `fetch_fast_info` calls `yf.Ticker(symbol).fast_info` which internally hits Yahoo's `/v8/finance/chart/` API
3. When chart data is missing (e.g., for a newly created leveraged ETF like TSLL), yfinance retries with different periods (`5d`, `1y`) and logs ERROR on each failure
4. yfinance uses the security's **display name** (e.g., `$DIREXION TESLA 2X`) in its error messages, not the input ticker
5. Sentry `LoggingIntegration` captures these ERROR-level log records as Sentry events
6. The holdings price fetch already catches the exception and returns `price_error` — the Sentry event is redundant noise

**Why this is the right fix:**
- The holdings flow already handles missing prices gracefully (returns `price_error` per position)
- yfinance "no data found" is expected for some tickers (recently listed, low liquidity, etc.)
- We should NOT suppress ALL yfinance errors — only the known "possibly delisted" noise pattern
- We should NOT change yfinance's logger level globally — that could hide real transport errors

---

### Task 1: Add yfinance noise filter to Sentry `_before_send`

**Files:**
- Modify: `app/monitoring/sentry.py:56-60` (add helper), `app/monitoring/sentry.py:142-146` (modify `_before_send`)

**Step 1: Write the failing test**

Create test in `tests/test_sentry.py` (or the existing sentry test file — check which exists):

```python
@pytest.mark.unit
class TestYfinanceNoiseFilter:
    """Tests for filtering yfinance 'possibly delisted' noise from Sentry events."""

    def test_yfinance_possibly_delisted_event_dropped(self):
        """_before_send drops yfinance 'possibly delisted' log events."""
        from app.monitoring.sentry import _before_send

        event = {
            "logger": "yfinance",
            "logentry": {
                "message": "$DIREXION TESLA 2X: possibly delisted; no price data found  (period=5d) (Yahoo error = \"No data found, symbol may be delisted\")",
                "formatted": "$DIREXION TESLA 2X: possibly delisted; no price data found  (period=5d) (Yahoo error = \"No data found, symbol may be delisted\")",
            },
        }
        hint: dict = {}
        result = _before_send(event, hint)
        assert result is None

    def test_yfinance_no_data_found_event_dropped(self):
        """_before_send drops yfinance 'No data found' log events."""
        from app.monitoring.sentry import _before_send

        event = {
            "logger": "yfinance",
            "logentry": {
                "message": "TSLL: No data found for this date range, symbol may be delisted",
                "formatted": "TSLL: No data found for this date range, symbol may be delisted",
            },
        }
        hint: dict = {}
        result = _before_send(event, hint)
        assert result is None

    def test_yfinance_no_price_data_event_dropped(self):
        """_before_send drops yfinance 'no price data found' log events."""
        from app.monitoring.sentry import _before_send

        event = {
            "logger": "yfinance",
            "logentry": {
                "message": "AAPL: possibly delisted; no price data found (period=1y)",
                "formatted": "AAPL: possibly delisted; no price data found (period=1y)",
            },
        }
        hint: dict = {}
        result = _before_send(event, hint)
        assert result is None

    def test_yfinance_real_error_not_dropped(self):
        """_before_send keeps genuine yfinance errors."""
        from app.monitoring.sentry import _before_send

        event = {
            "logger": "yfinance",
            "logentry": {
                "message": "Connection timeout to Yahoo Finance API",
                "formatted": "Connection timeout to Yahoo Finance API",
            },
        }
        hint: dict = {}
        result = _before_send(event, hint)
        assert result is not None

    def test_non_yfinance_error_not_dropped(self):
        """_before_send keeps non-yfinance error events."""
        from app.monitoring.sentry import _before_send

        event = {
            "logger": "app.services",
            "logentry": {
                "message": "Database connection failed",
                "formatted": "Database connection failed",
            },
        }
        hint: dict = {}
        result = _before_send(event, hint)
        assert result is not None

    def test_healthcheck_still_filtered(self):
        """Existing healthcheck filter still works alongside yfinance filter."""
        from app.monitoring.sentry import _before_send
        import logging

        log_record = logging.LogRecord(
            name="uvicorn.access",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg='127.0.0.1 - "GET /healthz HTTP/1.1" 200',
            args=(),
            exc_info=None,
        )
        event = {
            "logger": "uvicorn.access",
            "message": '127.0.0.1 - "GET /healthz HTTP/1.1" 200',
        }
        hint = {"log_record": log_record}
        result = _before_send(event, hint)
        assert result is None
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_sentry.py::TestYfinanceNoiseFilter -v`
Expected: FAIL — `_before_send` currently passes yfinance events through.

**Step 3: Implement the yfinance noise filter**

In `app/monitoring/sentry.py`, add a helper function after `_is_healthcheck_access_log` (around line 60):

```python
_YFINANCE_NOISE_PATTERNS = (
    "possibly delisted",
    "No data found",
    "no price data found",
    "symbol may be delisted",
)


def _is_yfinance_noise_log(logger_name: str | None, message: str | None) -> bool:
    """Return True for yfinance log events that are expected operational noise.

    yfinance logs at ERROR level when chart data is unavailable for a ticker
    (e.g., newly created ETFs, low-liquidity tickers). These are already
    handled gracefully in the holdings flow via per-symbol ``price_error``
    fields and should not create Sentry issues.
    """
    if logger_name != "yfinance" or not message:
        return False
    return any(pattern in message for pattern in _YFINANCE_NOISE_PATTERNS)
```

Then update `_before_send` to add the new filter:

```python
def _before_send(event: Event, hint: Hint) -> Event | None:
    logger_name, message = _extract_log_context(event, hint)
    if _is_healthcheck_access_log(logger_name, message):
        return None
    if _is_yfinance_noise_log(logger_name, message):
        return None
    return _sanitize_in_place(event)
```

And update `_before_send_log` to add the same filter:

```python
def _before_send_log(sentry_log: Log, hint: Hint) -> Log | None:
    logger_name, message = _extract_sentry_log_context(sentry_log, hint)
    if _is_healthcheck_access_log(logger_name, message):
        return None
    if _is_yfinance_noise_log(logger_name, message):
        return None
    return _sanitize_in_place(sentry_log)
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_sentry.py::TestYfinanceNoiseFilter -v`
Expected: PASS — all 6 test cases pass.

**Step 5: Run full test suite to check for regressions**

Run: `make test`
Expected: PASS — no regressions.

**Step 6: Commit**

```bash
git add app/monitoring/sentry.py tests/test_sentry.py
git commit -m "fix: filter yfinance 'possibly delisted' noise from Sentry events

yfinance logs at ERROR level when chart data is missing for a ticker
(e.g., recently created leveraged ETFs). These errors are already handled
gracefully in the holdings flow via per-symbol price_error fields.
The LoggingIntegration was capturing them as Sentry events (noise).

Add _is_yfinance_noise_log filter to _before_send and _before_send_log
to drop known yfinance noise patterns: 'possibly delisted',
'No data found', 'no price data found', 'symbol may be delisted'.

Fixes AUTO_TRADER-E
Fixes AUTO_TRADER-F"
```

---

### Task 2: Add `_before_send_log` filter test

**Files:**
- Modify: `tests/test_sentry.py`

**Step 1: Write the failing test**

Add to the same test class:

```python
    def test_before_send_log_drops_yfinance_noise(self):
        """_before_send_log drops yfinance noise from Sentry structured logs."""
        from app.monitoring.sentry import _before_send_log

        sentry_log = {
            "body": "$DIREXION TESLA 2X: possibly delisted; no price data found  (period=1y) (Yahoo error = \"No data found, symbol may be delisted\")",
            "attributes": {
                "logger.name": "yfinance",
            },
        }
        hint: dict = {}
        result = _before_send_log(sentry_log, hint)
        assert result is None

    def test_before_send_log_keeps_real_yfinance_errors(self):
        """_before_send_log keeps genuine yfinance errors in structured logs."""
        from app.monitoring.sentry import _before_send_log

        sentry_log = {
            "body": "Failed to decode JSON response from Yahoo Finance",
            "attributes": {
                "logger.name": "yfinance",
            },
        }
        hint: dict = {}
        result = _before_send_log(sentry_log, hint)
        assert result is not None

    def test_before_send_log_keeps_non_yfinance_logs(self):
        """_before_send_log keeps non-yfinance structured logs."""
        from app.monitoring.sentry import _before_send_log

        sentry_log = {
            "body": "Trade executed successfully",
            "attributes": {
                "logger.name": "app.services.trading",
            },
        }
        hint: dict = {}
        result = _before_send_log(sentry_log, hint)
        assert result is not None
```

**Step 2: Run test to verify it passes**

Run: `uv run pytest tests/test_sentry.py::TestYfinanceNoiseFilter -v`
Expected: PASS — these tests should pass since we already added the filter in Task 1.

**Step 3: Commit**

```bash
git add tests/test_sentry.py
git commit -m "test: add _before_send_log yfinance noise filter tests"
```

---

### Task 3: Verify lint/type checks pass

**Files:**
- No changes — verification only.

**Step 1: Run linting**

Run: `make lint`
Expected: PASS

**Step 2: Run type checking**

Run: `uv run ty check app/monitoring/sentry.py`
Expected: PASS — no type errors.

**Step 3: Verify existing Sentry tests still pass**

Run: `uv run pytest tests/ -k sentry -v`
Expected: PASS — all existing Sentry tests remain green.

---

## Summary

| Task | What | Files | Tests |
|------|------|-------|-------|
| 1 | Add `_is_yfinance_noise_log` + wire into `_before_send` and `_before_send_log` | `app/monitoring/sentry.py` | 6 new tests |
| 2 | Add `_before_send_log`-specific tests | `tests/test_sentry.py` | 3 new tests |
| 3 | Verify lint/type/existing tests | — | Verification only |

**Total new test count:** 9 tests
**Total files modified:** 2 (`app/monitoring/sentry.py`, `tests/test_sentry.py`)
**Risk level:** Low — only Sentry event filtering, no business logic changes

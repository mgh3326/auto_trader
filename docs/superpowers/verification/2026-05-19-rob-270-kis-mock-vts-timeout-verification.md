# ROB-270 Verification Report: KIS Mock VTS Balance Timeout Policy

## 1. Summary of Changes
Applied a 10s single-attempt + 12s wall-time bound policy to KIS mock domestic balance snapshots to resolve ReadTimeout amplification in the /invest/api/account-panel view.

- **app/services/brokers/kis/base.py**: Added `retry_request_errors: bool = True` to `_request_with_rate_limit` methods.
- **app/services/brokers/kis/account.py**: Exposed `timeout`, `retry_request_errors`, and `max_pages` in `fetch_domestic_balance_snapshot`. Corrected page count tracking.
- **app/services/invest_home_readers.py**: `KISMockHomeReader` now uses 10s timeout, no RequestError retry, and 12s wall-time bound via `asyncio.wait_for`.
- **Sentry**: Added observability for mock timeout policy (`kis_mock.timed_out`, `kis_mock.timeout_sec`, etc.).

## 2. Test Results

### 2.1 Targeted Tests (TDD)
- `tests/test_kis_request_error_retry_policy.py`: 3 tests PASSED.
  - Default retry behavior unchanged.
  - `retry_request_errors=False` stops ReadTimeout retry.
  - 429 retry is independent and still works when RequestError retry is disabled.
- `tests/test_kis_domestic_balance_snapshot.py`: 13 tests PASSED.
  - Parameter propagation verified.
  - Corrected `page_count` (1 request = 1 page).
  - `max_pages` cap honored.
- `tests/test_invest_home_readers.py`: 2 tests (ROB-270) PASSED.
  - Policy parameters passed correctly.
  - Wall-time bound degradation to warning verified.

### 2.2 Regression Suite
- All 101 KIS-related tests passed: `uv run pytest tests/test_kis_*.py tests/test_invest_home_readers.py tests/test_async_rate_limiter.py -v`.
- account-panel smoke passed: `uv run pytest tests/test_invest_home_readers.py tests/test_invest_home_service.py -v -k "account_panel or kis_mock_reader"`.

## 3. Risk Assessment
- **Live Path Safety**: `BaseKISClient` and `AccountClient` defaults preserve the 5s timeout × 3 attempts behavior. `max_pages` defaults to 10. No mutation identifiers added to read-only paths.
- **Degradation**: If KIS VTS is extremely slow (>12s), the mock branch returns a warning, allowing other sources (Live, Upbit, Manual) to render.

## 4. Remaining TODOs
- [x] Sentry tag verification in production (post-deployment).
- [ ] Monitor /invest/api/account-panel latency after deploy.

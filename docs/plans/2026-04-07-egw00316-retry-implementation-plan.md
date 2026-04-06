# EGW00316 KIS API Transient Error Retry Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add retry logic for EGW00316 transient error in KIS account balance inquiry to prevent unnecessary RuntimeError failures.

**Architecture:** Add `EGW00316` to the retryable error codes list and implement the same retry pattern used in domestic/overseas orders (exponential backoff, max 3 attempts) in the `fetch_my_stocks` function.

**Tech Stack:** Python 3.13+, asyncio, existing KIS constants module

---

## Background

### Problem
The Sentry issue [AUTO_TRADER-5J](https://mgh3326-daum.sentry.io/issues/7390203132/) shows that KIS API returns error code `EGW00316` with message "조회 처리 중 오류 발생하였습니다. 재 조회 수행 부탁드립니다." (An error occurred during inquiry. Please re-perform the inquiry.) during overseas stock balance fetch (`fetch_my_stocks`).

Currently:
- `account.py` raises RuntimeError immediately for this error
- `domestic_orders.py` and `overseas_orders.py` already handle transient errors with retry logic
- `constants.py` has `RETRYABLE_MSG_CODES = frozenset({"SYDB0050"})` but `EGW00316` is not included

### Solution
1. Add `EGW00316` to `RETRYABLE_MSG_CODES` in `constants.py`
2. Add transient retry logic to `fetch_my_stocks` in `account.py` following the existing pattern in orders modules

---

## Task 1: Add EGW00316 to Retryable Error Codes

**Files:**
- Modify: `app/services/brokers/kis/constants.py:292`

**Step 1: Update RETRYABLE_MSG_CODES**

```python
# Before:
RETRYABLE_MSG_CODES: frozenset[str] = frozenset({"SYDB0050"})

# After:
RETRYABLE_MSG_CODES: frozenset[str] = frozenset({
    "SYDB0050",  # "조회이후에 자료가 변경되었습니다.(다시 조회하세요)" — DB race condition
    "EGW00316",  # "조회 처리 중 오류 발생하였습니다. 재 조회 수행 부탁드립니다." — Inquiry processing error
})
```

**Step 2: Verify the change**

Run: `python -c "from app.services.brokers.kis.constants import RETRYABLE_MSG_CODES; print(RETRYABLE_MSG_CODES)"`
Expected: `{'SYDB0050', 'EGW00316'}`

**Step 3: Commit**

```bash
git add app/services/brokers/kis/constants.py
git commit -m "fix(kis): add EGW00316 to retryable error codes

EGW00316 is a transient error from KIS API that indicates
inquiry processing failure and explicitly requests retry.
This prevents unnecessary RuntimeError in account balance fetch.

Refs: AUTO_TRADER-5J"
```

---

## Task 2: Add Retry Logic to fetch_my_stocks

**Files:**
- Modify: `app/services/brokers/kis/account.py:180-300`

**Step 1: Add transient_retry_count variable**

At the beginning of the while loop (around line 183), add transient retry counter:

```python
# Find this section (around line 183-190):
        page = 1
        max_pages = 10
        token_retry_count = 0
        max_token_retries = 3

# Add after:
        transient_retry_count = 0
```

**Step 2: Add retry logic for transient errors**

Replace the error handling block at lines 249-262 with this enhanced version:

```python
            if js.get("rt_cd") != "0":
                if js.get("msg_cd") in [
                    "EGW00123",
                    "EGW00121",
                ]:
                    token_retry_count += 1
                    if token_retry_count >= max_token_retries:
                        error_msg = f"{js.get('msg_cd')} {js.get('msg1')} (token retry limit exceeded)"
                        logging.error(
                            f"{'해외' if is_overseas else '국내'}주식 잔고 조회 실패: {error_msg}"
                        )
                        raise RuntimeError(error_msg)
                    await self._parent._token_manager.clear_token()
                    await self._parent._ensure_token()
                    continue

                if js.get("msg_cd") in constants.RETRYABLE_MSG_CODES:
                    transient_retry_count += 1
                    if transient_retry_count < constants.RETRYABLE_MAX_ATTEMPTS:
                        logging.warning(
                            "{'해외' if is_overseas else '국내'}주식 잔고조회 transient 에러 (시도 %d/%d): %s %s",
                            transient_retry_count,
                            constants.RETRYABLE_MAX_ATTEMPTS,
                            js.get("msg_cd"),
                            js.get("msg1"),
                        )
                        await asyncio.sleep(
                            constants.RETRYABLE_BASE_DELAY * transient_retry_count
                        )
                        continue

                error_msg = f"{js.get('msg_cd')} {js.get('msg1')}"
                logging.error(
                    f"{'해외' if is_overseas else '국내'}주식 잔고 조회 실패: {error_msg}"
                )
                raise RuntimeError(error_msg)
```

**Step 3: Verify imports**

Ensure `constants` is imported at the top of the file. Check line ~15:

```python
from app.services.brokers.kis import constants
```

If not present, add it.

**Step 4: Run type checker**

Run: `cd /Users/robin/.superset/worktrees/auto_trader/sentry-issue-plan-3 && python -m pyright app/services/brokers/kis/account.py --pythonversion 3.13`
Expected: No errors related to our changes

**Step 5: Commit**

```bash
git add app/services/brokers/kis/account.py
git commit -m "fix(kis): add transient error retry to fetch_my_stocks

Implement retry logic for RETRYABLE_MSG_CODES (including EGW00316)
in fetch_my_stocks function following the pattern used in
domestic/overseas orders modules.

- Add transient_retry_count tracking
- Retry up to RETRYABLE_MAX_ATTEMPTS (3) with exponential backoff
- Log warning on each retry attempt

Refs: AUTO_TRADER-5J"
```

---

## Task 3: Write Tests for Retry Logic

**Files:**
- Create: `tests/test_kis_account_retry.py`

**Step 1: Create test file with retry scenarios**

```python
"""Tests for KIS account fetch_my_stocks transient error retry logic."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


class TestFetchMyStocksRetry:
    """Test retry behavior for transient errors in fetch_my_stocks."""

    @pytest.fixture
    def mock_parent(self):
        """Create mock parent with required attributes."""
        parent = MagicMock()
        parent._hdr_base = {"content-type": "application/json"}
        parent._token_manager = AsyncMock()
        parent._ensure_token = AsyncMock()
        parent._request_with_rate_limit = AsyncMock()
        return parent

    @pytest.fixture
    def account_client(self, mock_parent):
        """Create AccountClient instance with mocked parent."""
        from app.services.brokers.kis.account import AccountClient
        from app.services.brokers.kis.settings import KISSettings

        with patch.object(KISSettings, "__init__", return_value=None):
            client = AccountClient(mock_parent)
            client._settings = MagicMock()
            client._settings.kis_access_token = "test_token"
            client._settings.kis_account_number = "12345678"
        return client

    @pytest.mark.asyncio
    async def test_egw00316_transient_error_triggers_retry(self, account_client, mock_parent):
        """Verify EGW00316 transient errors trigger retry in fetch_my_stocks."""
        # First call fails with EGW00316, second succeeds
        transient_response = {
            "rt_cd": "1",
            "msg_cd": "EGW00316",
            "msg1": "조회 처리 중 오류 발생하였습니다. 재 조회 수행 부탁드립니다.",
        }
        success_response = {
            "rt_cd": "0",
            "msg_cd": "MSG00000",
            "output1": [],
        }
        mock_parent._request_with_rate_limit.side_effect = [
            transient_response,
            success_response,
        ]

        result = await account_client.fetch_my_stocks(is_overseas=True, exchange="NASD")

        assert result == []
        assert mock_parent._request_with_rate_limit.call_count == 2

    @pytest.mark.asyncio
    async def test_sydb0050_transient_error_triggers_retry(self, account_client, mock_parent):
        """Verify SYDB0050 transient errors trigger retry."""
        transient_response = {
            "rt_cd": "1",
            "msg_cd": "SYDB0050",
            "msg1": "조회이후에 자료가 변경되었습니다.(다시 조회하세요)",
        }
        success_response = {
            "rt_cd": "0",
            "msg_cd": "MSG00000",
            "output1": [{"stock_code": "AAPL"}],
        }
        mock_parent._request_with_rate_limit.side_effect = [
            transient_response,
            success_response,
        ]

        result = await account_client.fetch_my_stocks(is_overseas=False)

        assert len(result) == 1
        assert mock_parent._request_with_rate_limit.call_count == 2

    @pytest.mark.asyncio
    async def test_transient_error_exceeds_max_attempts_raises(self, account_client, mock_parent):
        """Verify transient errors raise after max retry attempts exceeded."""
        from app.services.brokers.kis import constants

        transient_response = {
            "rt_cd": "1",
            "msg_cd": "EGW00316",
            "msg1": "조회 처리 중 오류 발생하였습니다. 재 조회 수행 부탁드립니다.",
        }
        # Return transient error more than max attempts
        mock_parent._request_with_rate_limit.return_value = transient_response

        with pytest.raises(RuntimeError) as exc_info:
            await account_client.fetch_my_stocks(is_overseas=True, exchange="NASD")

        assert "EGW00316" in str(exc_info.value)
        assert mock_parent._request_with_rate_limit.call_count == constants.RETRYABLE_MAX_ATTEMPTS

    @pytest.mark.asyncio
    async def test_non_retryable_error_raises_immediately(self, account_client, mock_parent):
        """Verify non-retryable errors raise immediately without retry."""
        error_response = {
            "rt_cd": "1",
            "msg_cd": "EGW99999",
            "msg1": "Some other error",
        }
        mock_parent._request_with_rate_limit.return_value = error_response

        with pytest.raises(RuntimeError) as exc_info:
            await account_client.fetch_my_stocks(is_overseas=False)

        assert "EGW99999" in str(exc_info.value)
        assert mock_parent._request_with_rate_limit.call_count == 1
```

**Step 2: Run the new tests**

Run: `cd /Users/robin/.superset/worktrees/auto_trader/sentry-issue-plan-3 && uv run pytest tests/test_kis_account_retry.py -v`
Expected: All 4 tests PASS

**Step 3: Commit**

```bash
git add tests/test_kis_account_retry.py
git commit -m "test(kis): add tests for fetch_my_stocks retry logic

Add comprehensive tests for transient error retry behavior:
- EGW00316 triggers retry and succeeds on second attempt
- SYDB0050 triggers retry (existing retryable code)
- Exceeding max attempts raises RuntimeError
- Non-retryable errors raise immediately

Refs: AUTO_TRADER-5J"
```

---

## Task 4: Run Full Test Suite

**Files:**
- All existing tests

**Step 1: Run KIS-related tests**

Run: `cd /Users/robin/.superset/worktrees/auto_trader/sentry-issue-plan-3 && uv run pytest tests/test_kis_*.py -v --tb=short`
Expected: All tests PASS (including new retry tests)

**Step 2: Run services tests**

Run: `cd /Users/robin/.superset/worktrees/auto_trader/sentry-issue-plan-3 && make test-services-split`
Expected: PASS

**Step 3: Run full test suite (fast gate)**

Run: `cd /Users/robin/.superset/worktrees/auto_trader/sentry-issue-plan-3 && make test`
Expected: PASS

**Step 4: Commit (if any fixes needed)**

If test fixes were needed:
```bash
git add -A
git commit -m "test(kis): fix any test regressions from retry logic changes"
```

---

## Task 5: Verify Code Quality

**Files:**
- All modified files

**Step 1: Run linter**

Run: `cd /Users/robin/.superset/worktrees/auto_trader/sentry-issue-plan-3 && make lint`
Expected: No errors in modified files

**Step 2: Run type checker on modified files**

Run: `cd /Users/robin/.superset/worktrees/auto_trader/sentry-issue-plan-3 && python -m pyright app/services/brokers/kis/constants.py app/services/brokers/kis/account.py --pythonversion 3.13`
Expected: No type errors

**Step 3: Run security check**

Run: `cd /Users/robin/.superset/worktrees/auto_trader/sentry-issue-plan-3 && make security`
Expected: No new security issues

---

## Summary

### Changes Made

1. **constants.py**: Added `EGW00316` to `RETRYABLE_MSG_CODES`
2. **account.py**: Implemented transient error retry logic in `fetch_my_stocks` with:
   - `transient_retry_count` tracking
   - Check against `RETRYABLE_MSG_CODES`
   - Exponential backoff using `RETRYABLE_BASE_DELAY`
   - Warning logging on each retry
   - Max attempts limit from `RETRYABLE_MAX_ATTEMPTS`
3. **test_kis_account_retry.py**: Added 4 comprehensive test cases

### Behavior Change

**Before**: `EGW00316` error immediately raised RuntimeError, causing US candles sync to fail.

**After**: `EGW00316` error triggers up to 3 retry attempts with exponential backoff (0.3s, 0.6s, 0.9s), only raising RuntimeError if all retries fail.

### Error Code Reference

| Code | Message | Category | Action |
|------|---------|----------|--------|
| EGW00123, EGW00121 | Token expired | Auth | Clear token, re-auth, retry |
| SYDB0050 | Data changed after inquiry | Transient | Retry with backoff |
| EGW00316 | Inquiry processing error | Transient | Retry with backoff (NEW) |
| Others | Various | Fatal | Raise RuntimeError |

---

## Rollback Plan

If issues occur in production:

```bash
# Revert constants.py change
git revert <commit-hash-for-constants>

# Revert account.py change
git revert <commit-hash-for-account>
```

Or manually:
1. Remove `EGW00316` from `RETRYABLE_MSG_CODES`
2. Remove transient retry logic block from `fetch_my_stocks`

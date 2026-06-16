# [ROB-585] KIS Batch Order Rate Limit Handling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement safer and more robust rate limit handling for KIS batch orders by reducing the client-side throughput for order TRs, increasing retries specifically for orders, and reporting rate limit events in the response.

**Architecture:** 
1. Update `DEFAULT_KIS_API_RATE_LIMITS` to 8/s for order TRs to ensure we stay under the ledger limit.
2. Enhance `BaseKISClient` to support retry count overrides and track retry metrics.
3. Update order execution paths to use 3 retries and disable retries on `httpx.RequestError` (timeout/network) to avoid duplicate orders.
4. Expose `rate_limited` and `rate_limit_retries` in the final MCP response.

**Tech Stack:** Python, FastAPI, KIS API, AsyncSlidingWindowRateLimiter

---

### Task 1: Update Rate Limit Configuration

**Files:**
- Modify: `app/core/config.py`

- [x] **Step 1: Add order TR IDs to DEFAULT_KIS_API_RATE_LIMITS**
Set rate to 8 and period to 1.0 for the following TR IDs:
- `TTTC0012U` (Domestic Buy)
- `TTTC0011U` (Domestic Sell)
- `TTTC0013U` (Domestic Cancel/Modify)
- `TTTT1002U` (Overseas Buy)
- `TTTT1006U` (Overseas Sell)
- `TTTT1004U` (Overseas Cancel/Modify)
- Including mock variants (prefixed with `V`).

### Task 2: Enhance BaseKISClient for Order-Specific Retries

**Files:**
- Modify: `app/services/brokers/kis/base.py`

- [x] **Step 1: Update `_request_with_rate_limit_with_headers` signature**
Add `max_retries_override: int | None = None`.

- [x] **Step 2: Track retries and inject into response**
Inside the retry loop, keep track of how many times it was rate-limited. If `is_rate_limited` is True or 429 is received, increment a counter. Inject `rate_limited: bool` and `rate_limit_retries: int` into the `data` dict before returning.

### Task 3: Update Order Clients to use Enhanced Retries

**Files:**
- Modify: `app/services/brokers/kis/domestic_orders.py`
- Modify: `app/services/brokers/kis/overseas_orders.py`

- [x] **Step 1: Update domestic order calls**
In `order_korea_stock`, `cancel_korea_order`, and `modify_korea_order`, set `retry_request_errors=False` and `max_retries_override=3` (or pass a value that results in 3 total retries).

- [x] **Step 2: Update overseas order calls**
Repeat for `order_overseas_stock`, `cancel_overseas_order`, and `modify_overseas_order`.

### Task 4: Propagate Rate Limit Metadata to MCP Response

**Files:**
- Modify: `app/mcp_server/tooling/kis_live_ledger.py`
- Modify: `app/mcp_server/tooling/live_order_ledger.py`

- [x] **Step 1: Capture and return metadata**
Update `_record_kis_live_order` and `_record_live_order` to extract `rate_limited` and `rate_limit_retries` from `execution_result` and include them in the final returned dictionary.

### Task 5: Verification

- [x] **Step 1: Run integration tests for KIS orders**
Verify that the rate limit metadata is present in the response.
- [x] **Step 2: Manual verification with a simulated batch**
(If possible in the environment) ensure that 10+ orders are throttled correctly and don't fail due to EGW00215 after retries.

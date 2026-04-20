# Watch Alert Target Kind Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Extend `manage_watch_alerts` and the scanner to support `target_kind=asset|index|fx`, KR `trade_value`, KOSPI/KOSDAQ index price watches, and USDKRW FX price watches while preserving legacy asset watch compatibility.

**Architecture:** Keep `market` as the venue bucket and introduce `target_kind` as the watched instrument classifier. Store new Redis hash fields as four-part identities (`target_kind:symbol:condition_type:threshold`) while parsing legacy three-part asset fields and removing them as-is after successful delivery. Route scanner evaluation by `(target_kind, metric)` through existing market-data services plus focused wrappers for index and USD/KRW quote reads.

**Tech Stack:** Python 3.13, Redis hashes, FastAPI/MCP tooling, pytest, n8n workflow JSON with embedded JavaScript regression tests.

---

### Task 1: Service Contract

**Files:**
- Modify: `tests/test_watch_alerts.py`
- Modify: `app/services/watch_alerts.py`

**Steps:**
1. Add failing tests for default `target_kind="asset"`, new four-part Redis fields, legacy three-part list/remove fallback, KR-only `trade_value`, KOSPI/KOSDAQ index price, USDKRW FX price, and unsupported metric/target combinations.
2. Run `uv run pytest tests/test_watch_alerts.py -q` and confirm the new tests fail for missing `target_kind`.
3. Implement normalization and validation matrix in `WatchAlertService`.
4. Run `uv run pytest tests/test_watch_alerts.py -q` and confirm green.

### Task 2: Scanner Dispatch

**Files:**
- Modify: `tests/test_watch_scanner.py`
- Modify: `app/jobs/watch_scanner.py`
- Modify: `app/services/exchange_rate_service.py`

**Steps:**
1. Add failing tests for asset `trade_value`, index price, FX price, triggered payload `target_kind`, unsupported watches skipped without deletion, and delivery failure retention.
2. Run `uv run pytest tests/test_watch_scanner.py -q` and confirm red.
3. Add dispatcher methods for asset price/rsi/trade_value, index price, and FX price. Reuse current one-shot deletion only after successful n8n delivery.
4. Run `uv run pytest tests/test_watch_scanner.py -q` and confirm green.

### Task 3: MCP Contract and Docs

**Files:**
- Modify: `tests/test_mcp_watch_alerts.py`
- Modify: `app/mcp_server/tooling/watch_alerts_registration.py`
- Modify: `app/mcp_server/README.md`

**Steps:**
1. Add failing tests for `target_kind` parameter pass-through and clear unsupported target/metric errors.
2. Run `uv run pytest tests/test_mcp_watch_alerts.py -q` and confirm red.
3. Extend MCP parameter handling and docs/examples.
4. Run `uv run pytest tests/test_mcp_watch_alerts.py -q` and confirm green.

### Task 4: n8n Regression

**Files:**
- Modify: `tests/test_n8n_watch_alert_workflow.py`
- Modify: `n8n/workflows/paperclip-watch-alert.json`

**Steps:**
1. Add failing tests proving fingerprints include `target_kind` and invalid payload validation requires it.
2. Run `uv run pytest tests/test_n8n_watch_alert_workflow.py -q` and confirm red.
3. Update embedded n8n workflow JavaScript to validate and include `target_kind` in dedupe fingerprints.
4. Run `uv run pytest tests/test_n8n_watch_alert_workflow.py -q` and confirm green.

### Task 5: Verification and Handoff

**Files:**
- All modified files

**Steps:**
1. Run targeted suites: `uv run pytest tests/test_watch_alerts.py tests/test_watch_scanner.py tests/test_mcp_watch_alerts.py tests/test_n8n_watch_alert_workflow.py -q`.
2. Run broader gates as feasible: `make test-unit` and `make lint`.
3. Commit with `Co-Authored-By: Paperclip <noreply@paperclip.ing>`.
4. Push `feature/ROB-307-watch-alert-target-kind` and open a PR against `main`.
5. Comment on ROB-307 with summary, tests, residual risks, n8n update note, and `hold_for_final_review`.

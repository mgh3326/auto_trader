# ROB-820 Mock Data Truthfulness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make KIS mock portfolio, quote, fundamentals, and failure envelopes truthful without adding a broker adapter or alternate account truth source.

**Architecture:** Enforce KIS-only mock scope inside the existing portfolio implementation functions so holdings, cash, allocation, and position readers inherit one boundary. Add pure freshness/availability annotators at existing quote and financial handler seams, leaving provider clients and mutation paths unchanged.

**Tech Stack:** Python 3.13, FastMCP handlers, pandas, pytest/pytest-asyncio, Ruff, ty, uv.

## Global Constraints

- Do not create a ROB-851 adapter or a separate holdings/cash truth source.
- Do not absorb ROB-843/ROB-853 mutation boundaries or ROB-819 work.
- Do not use real account data, credentials, live API calls, or live mutation.
- Every unresolved gap must follow RED -> GREEN with the exact targeted command recorded.

---

### Task 1: Lock the KIS mock account/provenance boundary

**Files:**
- Modify: `app/mcp_server/tooling/portfolio_cash.py`
- Modify: `app/mcp_server/tooling/portfolio_holdings.py`
- Modify: `app/mcp_server/README.md`
- Test: `tests/test_rob820_mock_data_truthfulness.py`

**Interfaces:**
- Consumes: existing `is_mock: bool`, normalized account filters, and routing metadata.
- Produces: KIS-only rows for mock reads; stable `ValueError` for incompatible account/market selectors; nested mock cash provenance.

- [ ] **Step 1: Write failing holdings and cash isolation tests**

  Use provider fakes that raise if Upbit, Toss, or manual collectors are called;
  call the real portfolio implementations with `is_mock=True`; assert only KIS
  rows remain and incompatible selectors fail closed.

- [ ] **Step 2: Run tests to verify RED**

  Run: `uv run pytest tests/test_rob820_mock_data_truthfulness.py -k 'mock and (holdings or cash or selector)' -q`

  Expected: failures show current Upbit/manual/Toss calls and accepted incompatible selectors.

- [ ] **Step 3: Implement the minimal boundary**

  Add a small selector validator in the portfolio layer. Skip all non-KIS
  collection and manual cash reads when `is_mock=True`; annotate KIS mock cash
  rows/errors with `account_mode="kis_mock"`.

- [ ] **Step 4: Run tests to verify GREEN**

  Run the Step 2 command and expect all selected tests to pass.

- [ ] **Step 5: Commit**

  `git add app/mcp_server/tooling/portfolio_cash.py app/mcp_server/tooling/portfolio_holdings.py app/mcp_server/README.md tests/test_rob820_mock_data_truthfulness.py && git commit -m "fix(ROB-820): isolate KIS mock account reads"`

### Task 2: Make quote and NXT freshness non-scoreable when timestamps are invalid

**Files:**
- Modify: `app/mcp_server/tooling/market_data_quotes.py`
- Modify: `app/mcp_server/tooling/analysis_analyze.py`
- Modify: `app/services/nxt_preflight.py`
- Test: `tests/test_rob820_mock_data_truthfulness.py`
- Test: `tests/test_mcp_quotes_tools.py`
- Test: `tests/test_mcp_fundamentals_tools.py`

**Interfaces:**
- Produces: `price_as_of: str | None`, `price_freshness`, `price_usable`, optional `price_unavailable_reason`; stale NXT public values become unavailable while preserving observed evidence.

- [ ] **Step 1: Write failing epoch/missing/stale tests**

  Cover RangeIndex OHLCV, explicit epoch zero, missing live as-of, old daily
  as-of, missing NXT as-of, stale NXT as-of, and a fresh NXT control.

- [ ] **Step 2: Run tests to verify RED**

  Run: `uv run pytest tests/test_rob820_mock_data_truthfulness.py -k 'price or nxt' -q`

  Expected: epoch is currently rendered as 1970 and stale NXT remains a usable boolean.

- [ ] **Step 3: Implement pure timestamp parsing and annotations**

  Parse the row `date` first and a datetime index only as fallback; reject
  missing/NaT/epoch values. Annotate quote usability without deleting observed
  prices. Change only stale/missing `NxtTradability.public_fields()` output;
  leave `evaluate_nxt_preflight` unchanged.

- [ ] **Step 4: Run tests to verify GREEN and update the obsolete 1970 assertion**

  Run the Step 2 command plus the targeted existing analyze/quote tests.

- [ ] **Step 5: Commit**

  `git add app/mcp_server/tooling/market_data_quotes.py app/mcp_server/tooling/analysis_analyze.py app/services/nxt_preflight.py tests/test_rob820_mock_data_truthfulness.py tests/test_mcp_quotes_tools.py tests/test_mcp_fundamentals_tools.py && git commit -m "fix(ROB-820): fail closed stale quote evidence"`

### Task 3: Expose empty fundamentals as unavailable

**Files:**
- Modify: `app/mcp_server/tooling/fundamentals/_financials.py`
- Modify: `app/mcp_server/README.md`
- Test: `tests/test_rob820_mock_data_truthfulness.py`

**Interfaces:**
- Consumes: provider payloads containing `metrics`, `reports`, or `data`.
- Produces: additive `status`, `scoreable`, `reason`, and `evidence` fields without fabricating numeric values.

- [ ] **Step 1: Write failing empty/non-empty provider-shape tests**

  Assert empty KR metrics are unavailable/non-scoreable and real KR/US payloads
  are available/scoreable.

- [ ] **Step 2: Run tests to verify RED**

  Run: `uv run pytest tests/test_rob820_mock_data_truthfulness.py -k financials -q`

  Expected: current payload has no availability contract.

- [ ] **Step 3: Add the minimal availability normalizer**

  Implement one pure helper in `_financials.py` and apply it after each successful
  provider fetch. Keep existing error payload behavior unchanged.

- [ ] **Step 4: Run tests to verify GREEN**

  Run the Step 2 command and expect all selected tests to pass.

- [ ] **Step 5: Commit**

  `git add app/mcp_server/tooling/fundamentals/_financials.py app/mcp_server/README.md tests/test_rob820_mock_data_truthfulness.py && git commit -m "fix(ROB-820): mark empty fundamentals unavailable"`

### Task 4: Prove existing timeout/circuit behavior and run regressions

**Files:**
- Modify: `tests/test_rob820_mock_data_truthfulness.py`

**Interfaces:**
- Verifies: ROB-600 `ReadTimeout` reason plus `unavailable_sources`; current `KISCircuitOpen` reason plus source/market evidence.

- [ ] **Step 1: Add regression tests for already-fixed behavior**

  Use `httpx.ReadTimeout("")` and real `KISCircuitOpen(45.0)` exceptions at the
  existing MCP catch boundaries.

- [ ] **Step 2: Run and record that these tests are GREEN on unchanged main behavior**

  Run: `uv run pytest tests/test_rob820_mock_data_truthfulness.py -k 'timeout or circuit' -q`

- [ ] **Step 3: Run broad regressions and quality gates**

  Run relevant portfolio/allocation/quotes/fundamentals/KIS read test files,
  then `ruff check`, `ruff format --check`, `ty check`, and `git diff --check`.

- [ ] **Step 4: Self-review the full diff against all five gaps**

  Inspect `git diff origin/main...HEAD`, verify no adapter/mutation/ROB-819 code,
  and rerun any test affected by review corrections.

- [ ] **Step 5: Push and open a PR without merging**

  Push `rob-820`, create a GitHub PR against `main`, and keep the worktree for
  review iteration.


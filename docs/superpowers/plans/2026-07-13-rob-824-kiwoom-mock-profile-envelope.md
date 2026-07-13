# ROB-824 Kiwoom Mock Profile Envelope Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the seven existing typed Kiwoom mock tools to the precise TradingCodex profile allowlists and return stable, provenance-safe normalized account reads.

**Architecture:** Restricted profiles reuse the existing Kiwoom registration through `_AllowlistedMCP`. A focused broker normalizer converts official kt00018/kt00009 payloads, rejects malformed/live-provenance evidence, and deep-redacts secrets before the MCP handler returns raw evidence plus normalized rows.

**Tech Stack:** Python 3.13, FastMCP, Pydantic settings, pytest/pytest-asyncio, Ruff, ty, uv.

## Global Constraints

- `account_read` adds only `kiwoom_mock_get_positions`, `kiwoom_mock_get_orderable_cash`, and `kiwoom_mock_get_order_history`; mutations remain zero.
- `tradingcodex_execution` adds exactly the existing seven typed `kiwoom_mock_*` tools.
- No Kiwoom live/general unscoped order surface, new profile, or additional DEFAULT exposure.
- Preserve `KIWOOM_MOCK_ENABLED` default-off, exact mock host pin, KRX-only behavior, and `dry_run=False` plus `confirm=True` mutation gates.
- Keep raw broker evidence with secret redaction; fail closed on conflicting live provenance.
- Do not implement ROB-852 lifecycle, reconcile, fills, or P&L work.

---

### Task 1: Pin profile and startup safety contracts

**Files:**
- Modify: `tests/test_mcp_profiles.py`
- Modify: `tests/test_mcp_server_main.py`

**Interfaces:**
- Consumes: `ACCOUNT_READ_TOOL_NAMES`, `TRADINGCODEX_EXECUTION_TOOL_NAMES`, `KIWOOM_MOCK_TOOL_NAMES`
- Produces: executable surface and startup fail-close contracts

- [ ] **Step 1: Write failing exact-matrix tests**

  Assert the account-read Kiwoom intersection equals the three read names, its
  Kiwoom mutation intersection is empty, and the execution Kiwoom intersection
  equals `KIWOOM_MOCK_TOOL_NAMES`. Explicitly assert generic and live Kiwoom
  order names are absent.

- [ ] **Step 2: Run RED**

  Run: `ENV_FILE=/dev/null uv run pytest -q tests/test_mcp_profiles.py`

  Expected: failures report the three/seven Kiwoom tools are missing.

- [ ] **Step 3: Add startup tests**

  Cover disabled config, enabled missing credential names, and enabled live
  base URL for both restricted profiles without printing values.

- [ ] **Step 4: Run RED**

  Run: `ENV_FILE=/dev/null uv run pytest -q tests/test_mcp_server_main.py`

  Expected: enabled incomplete/live-host cases do not yet raise.

### Task 2: Pin normalized envelope and evidence behavior

**Files:**
- Create: `tests/test_kiwoom_mock_normalization.py`
- Modify: `tests/test_mcp_kiwoom_order_variants.py`

**Interfaces:**
- Produces: `normalize_positions`, `normalize_orders`, `redact_broker_response`, and mock provenance contract

- [ ] **Step 1: Write kt00018 failing tests**

  Use official `acnt_evlt_remn_indv_tot` rows and assert exact normalized
  `symbol`, `quantity`, `average_price`, and `currency` output.

- [ ] **Step 2: Write kt00009 failing tests**

  Use official `acnt_ord_cntr_prst_array` rows covering open, partial, filled,
  and cancelled status derivation and all seven stable keys.

- [ ] **Step 3: Write fail-close/redaction tests**

  Assert malformed rows raise, secret/account fields are recursively redacted,
  and live environment/account/host provenance rejects the whole response.

- [ ] **Step 4: Run RED**

  Run: `ENV_FILE=/dev/null uv run pytest -q tests/test_kiwoom_mock_normalization.py tests/test_mcp_kiwoom_order_variants.py`

  Expected: import/assertion failures because normalization/envelopes do not exist.

### Task 3: Implement minimum registration and normalization

**Files:**
- Create: `app/services/brokers/kiwoom/normalization.py`
- Modify: `app/mcp_server/tooling/account_read_registration.py`
- Modify: `app/mcp_server/tooling/tradingcodex_execution_registration.py`
- Modify: `app/mcp_server/tooling/orders_kiwoom_variants.py`
- Modify: `app/mcp_server/main.py`

**Interfaces:**
- Produces: `normalize_positions(payload)`, `normalize_orders(payload)`,
  `redact_broker_response(payload)`, `validate_mock_response_provenance(payload)`

- [ ] **Step 1: Register through filtered MCP instances**

  Add only the three read names to `ACCOUNT_READ_TOOL_NAMES`, add the full typed
  set to execution, invoke `orders_kiwoom_variants.register(filtered)`, and
  subtract allowed names in forbidden sets.

- [ ] **Step 2: Implement strict parsing and redaction**

  Parse official containers/fields, normalize padded integers and prefixed KR
  symbols, derive status, recursively redact sensitive keys, and raise a typed
  evidence error for malformed/conflicting provenance.

- [ ] **Step 3: Attach stable envelopes**

  In position/history handlers, validate provenance, normalize successful raw
  responses, preserve redacted `broker_response`, and attach fixed mock
  provenance. Convert normalization errors to `success=false` with empty stable
  arrays and no broker call retry.

- [ ] **Step 4: Add conditional startup validation**

  When either restricted profile has Kiwoom mock enabled, require all credential
  names and the exact mock base URL. Do nothing while disabled.

- [ ] **Step 5: Run GREEN**

  Run the Task 1 and Task 2 pytest commands until all pass.

### Task 4: Synchronize MCP documentation

**Files:**
- Modify: `app/mcp_server/README.md`

**Interfaces:**
- Documents: exact profile allowlists, normalized fields, fail-close behavior,
  raw evidence redaction, and unchanged operational constraints

- [ ] **Step 1: Update profile table and allowlists**

  List the three account-read and seven execution Kiwoom mock tools. State that
  Kiwoom live/general tools remain absent.

- [ ] **Step 2: Document stable read envelopes**

  Record position/order keys, provenance, redacted `broker_response`, and
  malformed/live-provenance failure behavior.

### Task 5: Verify, review, and deliver

**Files:**
- Verify all changed files only; no new production scope

**Interfaces:**
- Produces: test/lint/type evidence, commit SHA, PR URL, and Linear comment

- [ ] **Step 1: Run targeted and broad tests**

  Run relevant Kiwoom/profile/startup suites, then `make test-unit` and the
  non-live broad regression with `ENV_FILE=/dev/null` as environment permits.

- [ ] **Step 2: Run static gates**

  Run `ruff check`, `ruff format --check`, `ty check`, and `git diff --check`.

- [ ] **Step 3: Run safe smokes**

  Run fake profile read/preview smoke. Run real-profile preflight/read/preview
  only if credentials exist in the process environment; never read `.env`,
  mutate mock orders without explicit approval, or access Kiwoom live.

- [ ] **Step 4: Review and deliver**

  Review the complete diff against `origin/main`, commit only ROB-824 files,
  push `rob-824`, create a PR without merging, and comment on ROB-824 with SHA,
  PR, RED→GREEN evidence, exact test counts, and operational constraints.

## Self-review

- Every user requirement maps to a task above; ROB-852 work is explicitly excluded.
- No placeholder text or inconsistent function/envelope names remain.
- Test commands force `ENV_FILE=/dev/null` so the user `.env` is not read.

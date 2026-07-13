# ROB-845 Paper Execution Platform Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship and merge a default-off `paper_execution` MCP profile with provenance-first canonical contracts and guarded production adapters for Binance Spot Demo and Alpaca Crypto Paper.

**Architecture:** A broker-neutral application validates an experiment request, asks an injected verifier for exact-bound immutable provenance, derives the server idempotency identity, checks the single capability registry, and only then calls a venue adapter. Adapters reuse the existing guarded executor/coordinator and native ledgers; the common layer persists no lifecycle, fill, risk, or P&L state.

**Tech Stack:** Python 3.13, Pydantic v2, FastMCP, SQLAlchemy async/PostgreSQL advisory locks, pytest/pytest-asyncio, Ruff, ty.

## Global constraints

- Test-first for every behavior change: observe the focused RED before implementation.
- No Alembic revision, table, column, model, or common ledger.
- No live broker mutation surface and no raw broker submit in adapters.
- `origin` and `idempotency_key` are server-owned and absent from MCP input DTOs.
- Missing or mismatched provenance fails before adapter resolution/native ledger/broker calls.
- Existing manual-smoke and venue-native profile behavior remains compatible.
- ROB-849 storage/models are not imported; it will implement the Protocol later.

---

### Task 1: Canonical capability and order contracts

**Files:**
- Modify: `app/services/brokers/capabilities.py`
- Create: `app/services/brokers/paper/__init__.py`
- Create: `app/services/brokers/paper/contracts.py`
- Test: `tests/services/brokers/paper/test_capabilities.py`
- Test: `tests/services/brokers/paper/test_contracts.py`

**Interfaces:**
- `PaperBrokerCapabilities`, `PAPER_BROKER_CAPABILITIES`, `get_paper_capabilities(...)`
- `PaperOrderRequest`, `VerifiedExperimentProvenance`, `VerifiedPaperOrderIntent`
- `PaperRiskSnapshot`, `PaperOperationResult`, `PaperOperation`, `PaperBrokerPort`
- `derive_paper_idempotency_key(...)`

- [ ] Write validation/capability tests for exact V1 Binance and Alpaca surfaces, missing IDs/hashes, invalid quantities/prices, frozen evidence, deterministic keys, and stable `unsupported_capability`.
- [ ] Run the focused tests and confirm missing modules/types fail.
- [ ] Implement the minimal frozen types, Protocol, canonical hashing, and single registry entries.
- [ ] Re-run the focused tests and all existing broker-capability tests.

### Task 2: Provenance-first application and adapter registry

**Files:**
- Create: `app/services/brokers/paper/adapter_registry.py`
- Create: `app/services/brokers/paper/application.py`
- Test: `tests/services/brokers/paper/test_application.py`
- Test: `tests/services/brokers/paper/test_adapter_registry.py`

**Interfaces:**
- `ExperimentProvenanceVerifier.verify(request) -> VerifiedExperimentProvenance`
- `PaperAdapterRegistry.register/resolve`
- `PaperExecutionApplication.preview/submit/cancel/get_order/reconcile`

- [ ] Write counter-based tests proving missing verifier, verifier exception, missing exact field, and mismatch result in zero adapter calls.
- [ ] Write support-matrix tests proving unsupported operations do not resolve/call the adapter and return `unsupported_capability`.
- [ ] Write duplicate-registry and valid exact-binding dispatch tests.
- [ ] Run and confirm RED, then implement request/evidence comparison, server-owned origin/key, capability-before-adapter ordering, and typed results.
- [ ] Re-run focused tests and concurrency-safe deterministic-key tests.

### Task 3: Default-off allowlist profile and typed façade tools

**Files:**
- Modify: `app/core/config.py`
- Modify: `app/mcp_server/profiles.py`
- Modify: `app/mcp_server/main.py`
- Modify: `app/mcp_server/tooling/registry.py`
- Create: `app/mcp_server/tooling/paper_execution_registration.py`
- Modify: `app/mcp_server/README.md`
- Test: `tests/test_mcp_profiles.py`
- Test: `tests/test_mcp_server_main.py`
- Test: `tests/test_mcp_tool_registration_boot.py`
- Test: `tests/mcp_server/tooling/test_paper_execution_registration.py`
- Modify enum-exhaustive tests identified by `rg 'McpProfile' tests` only where required.

**Interfaces:**
- `McpProfile.PAPER_EXECUTION`
- `settings.PAPER_EXECUTION_ENABLED` default false
- `register_paper_execution_tools(mcp)`

- [ ] Write startup tests: flag-off fails before `FastMCP`, empty auth fails, flag-on with auth boots.
- [ ] Write direct-registry tests: flag-off registers zero; flag-on registers the exact façade allowlist and no venue-native/live mutation name.
- [ ] Write DTO-signature tests proving no `origin`, native client ID, or idempotency-key input.
- [ ] Confirm RED, implement enum/config/startup early-return registrar and verifier-unavailable composition default, then GREEN.
- [ ] Update README with flags, exact tools, V1 surface, and ROB-849 fail-closed handoff.

### Task 4: Binance deterministic native identity and adapter

**Files:**
- Modify: `app/services/brokers/binance/demo_scalping_exec/executor.py`
- Modify: `app/services/brokers/binance/demo/ledger/repository.py`
- Modify: `app/services/brokers/binance/demo/ledger/service.py`
- Create: `app/services/brokers/binance/paper_adapter.py`
- Test: `tests/services/brokers/binance/paper/test_adapter.py`
- Test: `tests/services/brokers/binance/demo/test_ledger_reservation_idempotency.py`
- Modify focused executor/ledger tests as compatibility requires.

**Interfaces:**
- Optional frozen execution identity supplied to `DemoScalpingExecutor`; absent retains UUID behavior.
- Deterministic root/close IDs within provider length limits.
- Native reservation outcomes: reserved, replayed, in-progress, collision, cap-blocked.
- `BinanceSpotDemoPaperAdapter` over `DemoScalpingExecutor` and `BinanceDemoLedgerService`.

- [ ] Write RED tests for deterministic IDs, exact sequential/concurrent replay with one open POST, collision, in-flight no-POST, and terminal replay despite stale market data.
- [ ] Implement lookup-before-cap inside the existing advisory-lock reservation and immutable metadata comparison; do not change schema.
- [ ] Write and run adapter RED tests for BTC/ETH BUY MARKET notional, native root/close evidence, and every unsupported method with zero transport calls.
- [ ] Implement the Spot-only adapter with dedicated risk limits and guarded executor composition; add no raw signed endpoint call.
- [ ] Run ROB-841/844 executor/ledger regressions and the new Binance adapter suite.

### Task 5: Alpaca application extraction and source-bound sell

**Files:**
- Create: `app/services/alpaca_paper_order_application.py`
- Modify: `app/services/paper_approval_packet.py`
- Modify: `app/services/crypto_execution_mapping.py`
- Modify: `app/services/alpaca_paper_submit_service.py`
- Create: `app/services/brokers/alpaca/paper_adapter.py`
- Modify: `app/mcp_server/tooling/alpaca_paper_automated_orders.py`
- Modify: `app/mcp_server/tooling/alpaca_paper_orders.py` only where needed to consume extracted application behavior without changing its public contract.
- Test: `tests/services/brokers/alpaca/paper/test_adapter.py`
- Test: `tests/services/test_alpaca_paper_order_application.py`
- Modify: focused packet/coordinator/tooling regression tests.

**Interfaces:**
- `AlpacaPaperOrderApplication.preview/submit/cancel/get_order`
- Additive `binance_public_spot` signal mapping for BTC/ETH only.
- Source-bound automated sell packet fields and exact native source execution verifier.
- `AlpacaCryptoPaperAdapter` calls only the application service.

- [ ] Write mapping RED tests for BTCUSDT/ETHUSDT and closed failure for SOL/unrecognized venues.
- [ ] Write source-authority RED tests: missing, wrong side/account/symbol/state, malformed/non-positive filled qty, source quantity ceiling, and independent USD 50 ceiling all yield POST zero.
- [ ] Write valid/concurrent sell tests proving exact native source binding plus existing live-position/advisory reservation yields at most one overselling POST.
- [ ] Extract packet/submit/read/cancel application behavior and make existing handlers delegate without signature or response regressions.
- [ ] Extend packet evidence additively, enable only source-bound façade automated sell, and keep legacy source-less tokens disabled.
- [ ] Implement the adapter and run ROB-842 cancel, async-fill, freshness-at-send, terminal replay, and oversell suites.

### Task 6: Cross-layer conformance and static safety guards

**Files:**
- Create: `tests/services/brokers/paper/test_production_adapter_contract.py`
- Create: `tests/services/brokers/paper/test_safety_guards.py`
- Modify: `tests/services/brokers/binance/test_audit_no_signed_endpoints.py` only with exact canonical adapter allowlisting if its repository-wide scan requires it.
- Modify: profile registry-diff tests.

- [ ] Contract-test every advertised port bit against both production adapters and every false bit against `unsupported_capability` with zero client/ledger activity.
- [ ] Add AST/import guards: adapter raw submit, live imports, adapter-to-tooling imports, common ORM/repository/migration creation, duplicate `PAPER_EXECUTION` and paper capability registry definitions, and ROB-848/849 imports.
- [ ] Add registry-diff proof that the profile contains no live or venue-native mutation tools.
- [ ] Run focused contract/static suites and fix only proven failures.

### Task 7: Verification, review, PR, CI, and merge

**Files:**
- Modify implementation/tests/docs only for verified review findings.

- [ ] Rebase once onto latest `origin/main` before first push and rerun focused suites.
- [ ] Run Ruff check/format, ty, `git diff --check`, migration-head check, and the broad non-live broker/profile regression suite.
- [ ] Use `superpowers:verification-before-completion`; record exact commands and counts.
- [ ] Request at least one independent code review and run the repository review checklist; resolve every actionable P0/P1 and disclose remaining risk.
- [ ] Push `rob-845`, create a ROB-845 PR with design, safety, test evidence, and no-migration statement.
- [ ] Wait for required GitHub checks, inspect failed logs if any, fix through new commits, and repeat verification.
- [ ] Merge only after review and required checks are green; do not deploy.
- [ ] Verify the PR merge SHA is contained in latest `origin/main`, update ROB-845 with PR/SHA/evidence/remaining limitations, mark Done, and report ROB-848/849/852/853 unblocked.


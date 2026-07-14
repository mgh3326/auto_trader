# ROB-872 Kiwoom Mock US Safety Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Kiwoom Mock US smoke cleanup, broker acceptance, and MCP network authentication fail closed with fake-only evidence.

**Architecture:** Keep schema-aware reconciliation private to the smoke CLI, apply the tracked-mutation acceptance finalizer to confirmed place/modify operations, and validate transport-aware auth at MCP module import. Preserve existing read/cancel contracts.

**Tech Stack:** Python 3.13, pytest/pytest-asyncio, FastMCP startup fakes, Ruff, ty, uv.

## Global Constraints

- `ENV_FILE=/dev/null`; no broker network, credentials, user `.env`, live hosts, or real sleep.
- No DB migration, ledger, reservation, risk sizing, scheduler, `PaperBrokerPort`, ROB-849/850, or ROB-873-876 implementation.
- No push, PR, or merge. Preserve leading zeroes and accept only 1-18 digit order IDs.

---

### Task 1: Strict shared acceptance

**Files:**
- Modify: `tests/test_mcp_kiwoom_shared.py`
- Modify: `tests/test_mcp_kiwoom_us_order_variants.py`
- Modify: `app/mcp_server/tooling/orders_kiwoom_shared.py`
- Modify: `app/mcp_server/tooling/orders_kiwoom_us_variants.py`

**Interfaces:**
- Produces: `finalize_place_broker_response(base, broker_response)` with `submitted`, `accepted_untracked`, `acceptance_uncertain`, and `rejected` outcomes for confirmed place/modify operations.

- [x] Add parametrized failing tests for non-strict return codes and missing/invalid place IDs.
- [x] Run the named tests and confirm failures are contract mismatches.
- [x] Implement a tracked-mutation finalizer using `derive_broker_success` and `validate_us_order_id` without changing common read/cancel shaping.
- [x] Re-run the named tests and preserve redacted `broker_response` evidence.
- [x] Reuse one US mock client per registration and enforce the documented one-second per-TR mock dispatch interval at the transport boundary.

### Task 2: Truthful smoke lifecycle

**Files:**
- Modify: `tests/test_kiwoom_mock_us_smoke_cli.py`
- Modify: `scripts/kiwoom_mock_us_smoke.py`

**Interfaces:**
- Produces: documented-field order extraction, `collect_pages`, target classifier, position snapshot, and shared `prove_cleanup` with injected `clock`/`sleep`.

- [x] Add failing tests for arbitrary-number non-match and bounded order-ID normalization.
- [x] Add failing async tests for empty history, page-two discovery, repeated tokens/page cap, partial fill plus delta, timeout, and probe-open-after-cancel.
- [x] Run each red group and record the expected assertion failure.
- [x] Implement bounded schema-aware page collection and fail-close continuation validation.
- [x] Implement documented history/position extractors and lifecycle classification.
- [x] Refactor full and probe through the same baseline/cleanup proof and injected polling dependencies.
- [x] Re-run the smoke suite with no real sleep and confirm all new cases pass.

### Task 3: Transport-aware startup authentication

**Files:**
- Modify: `tests/test_mcp_server_main.py`
- Modify: `app/mcp_server/main.py`

**Interfaces:**
- Produces: `_validate_profile_auth_token(profile, token, mcp_type)` where network Kiwoom mutation exposure requires auth.

- [x] Extend the isolated main-module loader with Kiwoom/default profile, `MCP_TYPE`, and US gate settings.
- [x] Add red tests for HTTP/SSE Kiwoom no-token and default-profile gate-on no-token, plus green regression expectations for valid token, stdio, unrelated profile, and gate-off.
- [x] Run the startup tests and confirm the network cases fail before implementation.
- [x] Read transport before import-time validation and enforce the narrow network predicate.
- [x] Re-run startup/profile tests and confirm environment/module isolation.

### Task 4: Documentation alignment and verification

**Files:**
- Modify: `docs/runbooks/kiwoom-mock-us-smoke.md`
- Modify: `app/mcp_server/README.md`

**Interfaces:**
- Documents: exit 2, accepted-untracked reconciliation, bounded 1-18 digit IDs, cleanup polling, and pending/unverified mutation evidence.

- [x] Replace exact-nine-digit operator wording and document the actual lifecycle statuses and cleanup proof.
- [x] Run the required nine-file pytest command and wider non-live Kiwoom/MCP/profile regression, recording exact pass counts.
- [x] Run Ruff check/format on changed files, ty on related modules, `git diff --check origin/main..HEAD`, and `git status --short`.
- [x] Fetch origin, rebase only if main advanced, make a separate local commit, and record final SHA/base/status in Linear while leaving ROB-872 In Progress.

## Self-review

All four issue areas have an explicit red-green task, interfaces are consistent,
and no task introduces a placeholder or out-of-scope stable envelope.

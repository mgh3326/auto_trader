# Alpaca Paper Reconcile Orders Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Provide a confirm-gated MCP reconciliation route that books broker-confirmed Alpaca paper fills into the existing ledger.

**Architecture:** A focused service lists only non-terminal Alpaca paper execution rows, normalizes the read-only Alpaca order/fill data into the shared KIS fill-evidence classifier input, and records only classifier-confirmed transitions. The MCP handler supplies scoping and the dry-run/confirm gate.

**Tech Stack:** Python 3.13, FastMCP, SQLAlchemy async, Pydantic, pytest.

## Global Constraints

- Reuse `classify_fill_evidence`; do not duplicate verdict logic.
- No broker mutation, production DB access, migration, or retrospective-service change.
- Default to `dry_run=True`; `dry_run=False` requires `confirm=True`.
- Missing/error evidence is fail-closed and reports manual review.

---

### Task 1: Evidence-first reconciliation service

**Files:**
- Create: `app/services/alpaca_paper_reconcile_service.py`
- Test: `tests/services/test_alpaca_paper_reconcile_service.py`

- [ ] Write RED tests for filled, partial, pending, missing evidence, anomaly, dry-run, and idempotency.
- [ ] Run the focused tests and observe the missing-service failure.
- [ ] Normalize Alpaca order/fill fields to `odno`, `ord_qty`, `tot_ccld_qty`, and `ccld_unpr`, then call `classify_fill_evidence`.
- [ ] Use existing ledger `record_status` only for booked evidence and return structured no-op/manual-review plans otherwise.
- [ ] Run focused service tests green.

### Task 2: MCP exposure and contract tests

**Files:**
- Modify: `app/mcp_server/tooling/alpaca_paper_orders.py`
- Modify: `tests/test_alpaca_paper_orders_tools.py`

- [ ] Write RED tests covering the dry-run default, confirm gate, registration, scope, and ISRG evidence fixture.
- [ ] Run the focused tests and observe the missing-tool failure.
- [ ] Add `alpaca_paper_reconcile_orders` to the existing Alpaca paper mutating registration surface and delegate to Task 1.
- [ ] Run focused tool tests green.

### Task 3: Verification and delivery

**Files:**
- Modify: `app/mcp_server/README.md`

- [ ] Document the reconcile tool and fail-closed scope.
- [ ] Run the requested Alpaca, lane guard, lint, diff, and Alembic checks.
- [ ] Commit, push the dedicated branch, and open a main-targeted PR.

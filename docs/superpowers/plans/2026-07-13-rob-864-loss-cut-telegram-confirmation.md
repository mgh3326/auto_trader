# ROB-864 Loss-Cut Telegram Confirmation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make loss-cut proposals executable without Paperclip by requiring a Telegram first approval and a bound, expiring second confirmation before the existing fresh revalidation and submit path.

**Architecture:** Reuse `OrderProposal.approval_nonce` as the single-use token for each click and persist a server-side two-step audit envelope in `source_asof`. Proposal adapters explicitly enable Paperclip-free loss-cut validation; direct loss-cut and defensive-trim calls fail closed and direct callers are directed to the proposal workflow.

**Tech Stack:** Python 3.13, FastAPI/MCP, SQLAlchemy async, pytest, Ruff, ty, Telegram inline callbacks.

## Global Constraints

- No broker, Telegram, or Paperclip network call in tests.
- Second-step nonce TTL is exactly 90 seconds and binds proposal id plus every pending rung's `rung_index` and `approval_revision`.
- Retrospective id, 72-hour freshness, symbol/trigger match, sell/limit/live-only, slip band, and approval hash remain mandatory.
- `approval_issue_id` is optional free-text audit metadata; no external lookup occurs.
- Normal proposal approval remains one click.
- No ROB-861 buying-power changes and no database migration.
- Commits use the repository default format with no Paperclip co-author trailer.

---

### Task 1: Lock the validation boundary with failing tests

**Files:**
- Modify: `tests/mcp_server/tooling/test_loss_cut_preconditions.py`
- Modify: `tests/mcp_server/tooling/test_loss_cut_place_order.py`
- Modify: `tests/mcp_server/tooling/test_toss_loss_cut.py`
- Modify: `tests/services/order_proposals/test_service.py`

**Interfaces:**
- Consumes: existing `_validate_loss_cut_preconditions` and proposal creation APIs.
- Produces: desired `proposal_flow: bool = False` validation contract and optional `approval_issue_id` proposal contract.

- [ ] **Step 1: Write tests that prove proposal flow succeeds with no Paperclip environment or issue id, and direct flow fails with `loss_cut_direct_path_disabled_use_order_proposal_create`.**

```python
ctx, errors = await ov._validate_loss_cut_preconditions(
    exit_intent="loss_cut", retrospective_id=42, exit_reason="stop_loss",
    approval_issue_id=None, side="sell", order_type="limit", is_mock=False,
    symbol="AAPL", proposal_flow=True,
)
assert errors == []
assert ctx.approval_issue_id is None
```

- [ ] **Step 2: Run focused tests and verify RED because `proposal_flow` is unknown and `approval_issue_id` is still required.**

Run: `uv run pytest tests/mcp_server/tooling/test_loss_cut_preconditions.py tests/mcp_server/tooling/test_loss_cut_place_order.py tests/mcp_server/tooling/test_toss_loss_cut.py tests/services/order_proposals/test_service.py -q`

- [ ] **Step 3: Implement the minimal validation split.**

Add `proposal_flow: bool = False`, make `LossCutContext.approval_issue_id` optional, reject non-proposal loss-cut immediately, remove Paperclip status checks from loss-cut, disable direct defensive-trim with proposal guidance, and pass the internal proposal signal from revalidation adapters/Toss context.

- [ ] **Step 4: Re-run the focused tests and verify GREEN.**

Run: `uv run pytest tests/mcp_server/tooling/test_loss_cut_preconditions.py tests/mcp_server/tooling/test_loss_cut_place_order.py tests/mcp_server/tooling/test_toss_loss_cut.py tests/services/order_proposals/test_service.py -q`

### Task 2: Add two-step Telegram confirmation by TDD

**Files:**
- Modify: `tests/services/order_proposals/test_approval_message.py`
- Modify: `tests/services/order_proposals/test_telegram_callback.py`
- Modify: `app/services/order_proposals/approval_message.py`
- Modify: `app/services/order_proposals/service.py`
- Modify: `app/services/order_proposals/revalidation.py`
- Modify: `app/services/order_proposals/telegram_callback.py`
- Modify: `app/mcp_server/tooling/orders_toss_variants.py`

**Interfaces:**
- Produces: callback action `lc`; `build_loss_cut_confirmation_message(...)`; `preview_loss_cut_confirmation(...)`; service methods to issue, validate, and record the confirmation envelope.

- [ ] **Step 1: Write RED tests for first-click zero submits, `⚠️ 손절 확인` issuance and summary, second-click submit, nonce replay, 90-second expiry, and rung/revision mismatch.**

```python
first = await handle_callback_update(first_update, revalidate_fn=fake_submit)
assert first["reason"] == "loss_cut_confirmation_required"
assert submit_calls == []
second = await handle_callback_update(second_update, revalidate_fn=fake_submit)
assert second["reason"] == "approved"
assert len(submit_calls) == 1
```

- [ ] **Step 2: Run callback/message tests and verify RED for missing `lc` parsing and confirmation flow.**

Run: `uv run pytest tests/services/order_proposals/test_approval_message.py tests/services/order_proposals/test_telegram_callback.py -q`

- [ ] **Step 3: Implement minimal two-step flow and audit envelope.**

The envelope key is `loss_cut_confirmation`; it contains `proposal_id`, `rungs`, `nonce`, `issued_at`, `expires_at`, `first_click`, and `second_click`. First click performs a mocked/injectable fresh preview and persists the envelope before editing the message. Second click validates envelope and consumes the current nonce before invoking the unchanged full revalidation.

- [ ] **Step 4: Re-run callback/message tests and verify GREEN.**

Run: `uv run pytest tests/services/order_proposals/test_approval_message.py tests/services/order_proposals/test_telegram_callback.py -q`

### Task 3: Prove click-time guards and Paperclip-free E2E

**Files:**
- Modify: `tests/services/order_proposals/test_telegram_callback.py`
- Modify: `tests/services/order_proposals/test_revalidation.py`
- Modify: `tests/mcp_server/tooling/test_toss_live_ledger.py`

**Interfaces:**
- Consumes: two-step callback and existing `revalidate_and_submit`/reconcile APIs.
- Produces: regression proof that stale retro/slip-band failures happen on step two and a normal proposal still submits on its first click.

- [ ] **Step 1: Add RED integration tests for second-click stale-retro and slip-band guard blocking, normal one-click approval, and create→first→second→mock submit→reconcile terminal convergence with all Paperclip env vars absent.**

- [ ] **Step 2: Run those exact tests and verify RED before any corrective production change.**

Run: `uv run pytest tests/services/order_proposals -q -k 'loss_cut or normal_proposal'`

- [ ] **Step 3: Make only the minimal adapter/preview response changes required for current price, average cost, loss percentage, lesson excerpt, and full second-click revalidation.**

- [ ] **Step 4: Re-run the order-proposal and MCP loss-cut suites and verify GREEN.**

Run: `uv run pytest tests/services/order_proposals tests/mcp_server/tooling/test_loss_cut_preconditions.py tests/mcp_server/tooling/test_loss_cut_place_order.py tests/mcp_server/tooling/test_toss_loss_cut.py -q`

### Task 4: Update contracts, review, and ship

**Files:**
- Modify: `docs/runbooks/order-proposals.md`
- Modify: `app/mcp_server/README.md`
- Modify: `app/mcp_server/tooling/orders_registration.py`
- Modify: `app/mcp_server/tooling/order_proposal_tools.py`
- Modify: `docs/plans/2026-07-13-rob-858-toss-losscut-decision.md`

- [ ] **Step 1: Replace proposal-path Paperclip language with the Telegram two-step contract and mark ROB-858's prior decision as superseded by ROB-864.**

- [ ] **Step 2: Enumerate all `_validate_loss_cut_preconditions` and `_fetch_approval_issue_status` callsites and record the direct-path decision in the PR body.**

Run: `rg -n '_validate_loss_cut_preconditions|_fetch_approval_issue_status' app tests`

- [ ] **Step 3: Run formatting, lint/type checks, relevant suites, and diff checks.**

Run: `uv run ruff format --check app tests`

Run: `make lint`

Run: `uv run pytest tests/services/order_proposals tests/mcp_server/tooling/test_loss_cut_preconditions.py tests/mcp_server/tooling/test_loss_cut_place_order.py tests/mcp_server/tooling/test_toss_loss_cut.py -q`

Run: `git diff --check && uv run alembic heads`

- [ ] **Step 4: Review the complete diff, commit with default trailers, push `rob-864`, and open a `main`-based PR titled `feat(ROB-864): replace Paperclip loss-cut approval with Telegram confirmation`.**

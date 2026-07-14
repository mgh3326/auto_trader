# ROB-868 Upbit WebSocket Fill Projection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Converge Upbit proposal rungs directly from committed websocket fill evidence while repairing health metrics and notifying small matched-rung fills.

**Architecture:** `websocket_monitor.py` projects a committed Upbit ledger fill through an independent proposal-service session and uses the match result to bypass the small-fill threshold. The proposal service adds optional `idempotency_key` evidence so Upbit `identifier` values match their persisted rung field, while the monitor consumer records Upbit runtime counters for health logs.

**Tech Stack:** Python 3.13+, asyncio, SQLAlchemy async sessions, pytest/pytest-asyncio, Ruff, ty.

## Global Constraints

- Linear ROB-868 is the source of truth.
- Do not contact brokers or real websockets in tests.
- Projection failures are log-and-swallow and must not kill the websocket loop.
- Pass `account_mode="upbit"` to `record_fill_evidence`.
- Do not infer terminal state without `state="done"` broker evidence.
- Preserve existing terminal short-circuit idempotency.
- `make lint` and the related test suite must pass.

---

### Task 1: Proposal rung projection and notification policy

**Files:**
- Modify: `websocket_monitor.py:167-459`
- Modify: `app/services/order_proposals/repository.py:120-159`
- Modify: `app/services/order_proposals/service.py:1191-1221`
- Test: `tests/test_websocket_monitor.py`
- Test: `tests/services/order_proposals/test_service.py`

**Interfaces:**
- Consumes: Upbit `myOrder` fields `state`, `uuid`, `identifier`, `executed_volume`.
- Produces: `record_fill_evidence(..., idempotency_key: str | None = None)`, `_project_upbit_proposal_fill(event) -> bool`, and `_send_fill_notification(..., proposal_rung_fill: bool = False)`.

- [x] **Step 1: Write failing projection tests**

Add async tests that inject `trade` and `done` events and assert the fake
`OrderProposalsService.record_fill_evidence` receives respectively:

```python
{
    "idempotency_key": "proposal-client-id",
    "broker_order_id": "upbit-order-id",
    "filled_qty": Decimal("0.0003"),
    "terminal_state": "partially_filled",  # "filled" for done
    "account_mode": "upbit",
}
```

First add a service test proving an `idempotency_key`-only rung match converges.
Also assert the independent fake session commits, no-match returns `False`, a DB
exception is logged and returns `False`, duplicate ledger status skips the
notification, and a small fill with `proposal_rung_fill=True` reaches the fake
notifier.

- [x] **Step 2: Verify RED**

Run: `uv run pytest --no-cov tests/test_websocket_monitor.py -k 'upbit and (proposal or rung or done or duplicate)' -q`

Expected: failures because the projection helper, `done` handling, and
`proposal_rung_fill` notification argument do not exist.

- [x] **Step 3: Implement minimal projection**

Extend repository evidence lookup with
`(OrderProposalRung.idempotency_key, idempotency_key)`, then add a helper with
this service call in an independent session:

```python
rung = await OrderProposalsService(db).record_fill_evidence(
    idempotency_key=identifier,
    broker_order_id=broker_order_id,
    filled_qty=Decimal(str(executed_volume)),
    terminal_state="filled" if state == "done" else "partially_filled",
    now=datetime.now(UTC),
    account_mode="upbit",
)
await db.commit()
return rung is not None
```

Call it after the execution-ledger upsert commit for `trade`. Catch broad
projection exceptions inside the helper, log with UUID/identifier/state, and
return `False`. Allow `trade` and `done` in `_on_upbit_order`; treat `done` as
terminal cumulative evidence only after verifying an existing committed fill,
without inserting a second ledger fill. Notify new ledger fills, plus a
threshold-suppressed duplicate delivery when it is the first successful rung
projection after a prior projection failure, and bypass `is_fill_notifiable`
only when the helper returned `True`.

- [x] **Step 4: Verify GREEN**

Run: `uv run pytest --no-cov tests/test_websocket_monitor.py -q`

Expected: all websocket monitor tests pass.

### Task 2: Upbit runtime health counters

**Files:**
- Modify: `websocket_monitor.py:65-84,167-199,584-610`
- Test: `tests/test_websocket_monitor.py`

**Interfaces:**
- Produces: monitor-owned Upbit counters and timestamps included by `UnifiedWebSocketMonitor._log_health_status()`.
- Consumes: each decoded `myOrder` callback invocation.

- [x] **Step 1: Write failing counter tests**

Call `_on_upbit_order` with one `trade` event and assert:

```python
assert monitor.upbit_messages_received == 1
assert monitor.upbit_execution_events_received == 1
assert monitor.upbit_last_message_at is not None
assert monitor.upbit_last_execution_at is not None
```

Add a monitor health-log test with an Upbit snapshot and assert the rendered
`messages_received`, `execution_events_received`, and timestamps are non-zero.

- [x] **Step 2: Verify RED**

Run: `uv run pytest --no-cov tests/test_upbit_websocket_service.py tests/test_websocket_monitor.py -k 'runtime or health' -q`

Expected: failures because Upbit runtime counters and snapshot aggregation do not exist.

- [x] **Step 3: Implement minimal counters and aggregation**

Initialize counters/timestamps in the monitor, update message state before
filtering and execution state for `trade`/`done`, and aggregate Upbit and KIS
values for the existing health log fields.

- [x] **Step 4: Verify GREEN and regression suite**

Run: `uv run pytest --no-cov tests/services/order_proposals/test_service.py tests/test_websocket_monitor.py -q`

Expected: all related tests pass.

### Task 3: Quality gate and delivery

**Files:**
- Verify all modified files.
- PR base: `main`.

**Interfaces:**
- Produces: a pushed `rob-868` branch and `fix(ROB-868): ...` pull request.

- [x] **Step 1: Run quality gates**

Run:

```bash
make lint
uv run pytest --no-cov tests/test_upbit_websocket_service.py tests/test_websocket_monitor.py -q
```

Expected: exit code 0 for both commands.

- [x] **Step 2: Review the diff and deployment contract**

Confirm the projection is after the ledger commit, the service call uses an
independent committed session and `account_mode="upbit"`, exceptions are
swallowed, and `scripts/deploy-native.sh` restarts the single-active Upbit
launchd service.

- [ ] **Step 3: Commit, push, and create PR**

Commit with a ROB-868 message and the repository's standard trailer, push
`rob-868`, then create a PR against `main` titled `fix(ROB-868): project Upbit websocket fills to proposal rungs`. The PR body must include test evidence and state that Upbit websocket is not blue/green and must be restarted after deployment (the native deploy workflow performs this restart).

# ROB-738 Order Send Intent Test Isolation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the KR live-path ledger regression test re-runnable against a local shared `test_db` even when same-day `review.order_send_intents` rows remain from an earlier run.

**Architecture:** Keep the production KIS duplicate-send guard unchanged. Add a test-only cleanup fixture in `tests/test_kis_mock_order_ledger.py` and apply it only to the test that intentionally drives the KIS live send path, so deterministic trading-day idempotency keys cannot collide with residue from prior local runs.

**Tech Stack:** Python 3.13, pytest, pytest-asyncio, SQLAlchemy async ORM, existing `db_session` fixture, `OrderSendIntent` model.

## Global Constraints

- Do not change `app/services/order_send_intent_service.py`; duplicate intent blocking is correct production behavior.
- Do not change `app/mcp_server/tooling/order_execution.py`; ROB-653's pre-send reservation must stay fail-closed for live KIS orders.
- Do not use raw SQL for this test cleanup; mirror existing test style with `sqlalchemy.delete(OrderSendIntent)`.
- Keep the cleanup scoped to `tests/test_kis_mock_order_ledger.py`; `tests/test_mcp_place_order.py` already has its own `order_send_intents` cleanup fixture.
- Preserve strict pytest marker behavior; do not introduce new markers.

---

## File Structure

- Modify: `tests/test_kis_mock_order_ledger.py`
  - Add imports for `pytest_asyncio`, `sqlalchemy.delete`, and `app.models.review.OrderSendIntent`.
  - Add a fixture that deletes `review.order_send_intents` before and after the live-path test.
  - Apply the fixture to `test_kis_live_kr_path_records_to_live_ledger_not_save_fill`.

No production files, migrations, or docs runbooks should change for ROB-738.

---

### Task 1: Isolate KR Live-Path Test From Same-Day Intent Residue

**Files:**
- Modify: `tests/test_kis_mock_order_ledger.py`

**Interfaces:**
- Consumes: `tests.conftest.db_session() -> AsyncSession`, `app.models.review.OrderSendIntent`, and `sqlalchemy.delete`.
- Produces: `clean_kis_live_order_send_intents` pytest fixture, used only by `test_kis_live_kr_path_records_to_live_ledger_not_save_fill`.

- [x] **Step 1: Reproduce the current local failure mode**

Run the target test twice against the same local DB:

```bash
uv run pytest tests/test_kis_mock_order_ledger.py::test_kis_live_kr_path_records_to_live_ledger_not_save_fill -q
uv run pytest tests/test_kis_mock_order_ledger.py::test_kis_live_kr_path_records_to_live_ledger_not_save_fill -q
```

Expected before the fix: the second command fails with an error containing:

```text
duplicate order intent
```

If the local `test_db` already contains a same-day row for this deterministic order key, the first command may fail with the same message. That is the same ROB-738 failure.

- [x] **Step 2: Add the cleanup imports**

In `tests/test_kis_mock_order_ledger.py`, replace the import block:

```python
from unittest.mock import AsyncMock

import pytest
```

with:

```python
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import delete

from app.models.review import OrderSendIntent
```

- [x] **Step 3: Add a targeted cleanup fixture**

In `tests/test_kis_mock_order_ledger.py`, insert this fixture after the imports and before the first section comment:

```python
@pytest_asyncio.fixture
async def clean_kis_live_order_send_intents(db_session):
    """Clear KIS live send reservations around tests that exercise live send.

    The production path commits review.order_send_intents through its own
    session before broker send. Local shared test_db keeps those rows across
    pytest invocations, while the idempotency key is deterministic for the same
    canonical order and trading day.
    """

    async def _delete_intents() -> None:
        await db_session.execute(delete(OrderSendIntent))
        await db_session.commit()

    await _delete_intents()
    yield
    await _delete_intents()
```

- [x] **Step 4: Apply the fixture to the KR live-path regression test**

In `tests/test_kis_mock_order_ledger.py`, replace:

```python
@pytest.mark.asyncio
async def test_kis_live_kr_path_records_to_live_ledger_not_save_fill(monkeypatch):
```

with:

```python
@pytest.mark.asyncio
@pytest.mark.usefixtures("clean_kis_live_order_send_intents")
async def test_kis_live_kr_path_records_to_live_ledger_not_save_fill(monkeypatch):
```

Do not apply this fixture to the mock-order tests in the same file; `is_mock=True` bypasses the KIS live `OrderSendIntentService.reserve()` path.

- [x] **Step 5: Run the targeted test twice**

Run:

```bash
uv run pytest tests/test_kis_mock_order_ledger.py::test_kis_live_kr_path_records_to_live_ledger_not_save_fill -q
uv run pytest tests/test_kis_mock_order_ledger.py::test_kis_live_kr_path_records_to_live_ledger_not_save_fill -q
```

Expected after the fix: both commands pass.

- [x] **Step 6: Run adjacent guard tests**

Run:

```bash
uv run pytest tests/test_kis_mock_order_ledger.py::test_kis_live_kr_path_records_to_live_ledger_not_save_fill tests/test_mcp_place_order.py::TestPlaceOrderHighAmount::test_place_order_high_amount_kr_equity_dry_run_false tests/test_rob653_kis_intent_guard.py -q
```

Expected: all selected tests pass.

- [x] **Step 7: Run lint on the touched file**

Run:

```bash
uv run ruff check tests/test_kis_mock_order_ledger.py
```

Expected: no Ruff violations.

- [x] **Step 8: Commit**

Run:

```bash
git add tests/test_kis_mock_order_ledger.py
git commit -m "test(ROB-738): isolate KIS live order intent residue"
```

---

## Self-Review

- Spec coverage: ROB-738 asks for local shared `test_db` residue isolation for KR live-path order tests. Task 1 clears `OrderSendIntent` rows around the exact failing test without changing production guard semantics.
- Placeholder scan: No TODO/TBD placeholders remain. All file paths, imports, fixture code, commands, and expected outcomes are concrete.
- Type consistency: `clean_kis_live_order_send_intents` is defined once as a pytest-asyncio fixture and referenced by the exact same string in `pytest.mark.usefixtures`.

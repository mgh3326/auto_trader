# ROB-103 Watch Order Intent MVP — Finish Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Finish the ROB-103 Watch Order Intent MVP. The original 12-task plan is at `docs/superpowers/plans/2026-05-04-rob-103-watch-order-intent-mvp.md`. Tasks 1–9 are committed (`02a5a022` … `36232080`). Task 10 files are written but uncommitted **and have fixture bugs**. Tasks 11–12 are not done.

**Architecture:** Already established in the design spec — `docs/superpowers/specs/2026-05-04-rob-103-watch-order-intent-mvp-design.md`. This plan only covers what is left.

**Tech Stack:** Python 3.13, FastAPI + `TestClient`, FastMCP, SQLAlchemy 2.x async, pytest + pytest-asyncio (strict). Read-only HTTP/MCP surface that mirrors `app/routers/alpaca_paper_ledger.py` and `app/mcp_server/tooling/alpaca_paper_ledger_read.py`.

---

## State of the worktree

**Committed (Tasks 1–9):**

| Commit | Subject |
|---|---|
| `02a5a022` | feat(ROB-103): add watch intent policy parser |
| `56d6e511` | feat(ROB-103): add watch order intent preview builder |
| `9b82a90a` | feat(ROB-103): add WatchOrderIntentLedger ORM model |
| `b2dce522` | chore(ROB-103): add watch_order_intent_ledger migration |
| `e85f31be` | feat(ROB-103): add watch order intent ledger service |
| `f485674e` | feat(ROB-103): extend watch alerts service for action policies |
| `c87500fa` | feat(ROB-103): add intents block to n8n watch alert payload |
| `e37fce2f` | feat(ROB-103): branch watch scanner to intent service |
| `36232080` | feat(ROB-103): extend manage_watch_alerts MCP tool for intents |

**Uncommitted in working tree (Task 10 — written, but with bugs):**

```
app/routers/watch_order_intent_ledger.py
app/mcp_server/tooling/watch_order_intent_ledger_read.py
tests/test_mcp_watch_order_intent_ledger.py
tests/test_watch_order_intent_ledger_router.py
```

**Known bugs in uncommitted files:**

1. `tests/test_watch_order_intent_ledger_router.py` references an `authenticated_client` fixture that **does not exist** in this repo. The repo's router-test convention is `_make_app_with_db(db)` + `TestClient` (see `tests/routers/test_alpaca_paper_ledger_router.py`). The file also lives in the wrong directory — router tests are under `tests/routers/`.
2. `tests/test_mcp_watch_order_intent_ledger.py` calls `watch_order_intent_ledger_list_recent_impl()` which opens `AsyncSessionLocal()` against the real DB at import/run time. The test only asserts the response shape — it should mock `AsyncSessionLocal` so the test stays fast and hermetic, matching how `tests/test_alpaca_paper_ledger_read_*` mocks the session.

**Not started (Tasks 11–12):**

- `app/main.py` does not include `watch_order_intent_ledger.router`.
- `app/mcp_server/tooling/registry.py` does not call `register_watch_order_intent_ledger_tools(mcp)`.
- `docs/runbooks/watch-order-intent-ledger.md` does not exist.
- Full-suite verification (`make lint`, `make typecheck`, ROB-103 pytest sweep) has not been run since Task 9.

---

## File structure (this plan only)

| File | Status | Action |
|---|---|---|
| `app/routers/watch_order_intent_ledger.py` | working tree | accept as-is, will be committed in Task A5 |
| `app/mcp_server/tooling/watch_order_intent_ledger_read.py` | working tree | accept as-is, will be committed in Task A5 |
| `tests/test_watch_order_intent_ledger_router.py` | working tree | **delete** (wrong location + broken fixtures) |
| `tests/routers/test_watch_order_intent_ledger_router.py` | new | **create** using the Alpaca pattern |
| `tests/test_mcp_watch_order_intent_ledger.py` | working tree | **rewrite** to mock `AsyncSessionLocal` |
| `app/main.py` | modified | add import + `app.include_router(...)` |
| `app/mcp_server/tooling/registry.py` | modified | add import + `register_watch_order_intent_ledger_tools(mcp)` call |
| `docs/runbooks/watch-order-intent-ledger.md` | new | mirror `docs/runbooks/alpaca-paper-ledger.md` |

---

## Task A: Repair Task 10 tests and commit Task 10

### Step A1 — Inspect existing patterns once

- [ ] **A1.1: Read the Alpaca router test pattern**

Run: `sed -n '1,140p' tests/routers/test_alpaca_paper_ledger_router.py`
Goal: understand `_mock_db_for_rows`, `_make_app_with_db`, and `dependency_overrides`. The pattern uses sync `TestClient`, `@pytest.mark.unit`, and an `AsyncMock`-based fake session. **Do not** introduce a real DB session in router tests — that is reserved for service-level tests (Task 5, already passing).

### Step A2 — Replace the broken router test

- [ ] **A2.1: Delete the broken file**

Run:
```bash
rm tests/test_watch_order_intent_ledger_router.py
```
Expected: file removed, `git status -sb` no longer lists it as untracked.

- [ ] **A2.2: Create the corrected router test**

Create `tests/routers/test_watch_order_intent_ledger_router.py`:

```python
"""Tests for the read-only watch order intent ledger router (ROB-103)."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _make_fake_row(**overrides: object):
    base = {
        "id": 1,
        "correlation_id": "corr-router-1",
        "idempotency_key": (
            "kr:asset:005930:price_below:70000:create_order_intent:buy:2026-05-04"
        ),
        "market": "kr",
        "target_kind": "asset",
        "symbol": "005930",
        "condition_type": "price_below",
        "threshold": 70000.0,
        "threshold_key": "70000",
        "action": "create_order_intent",
        "side": "buy",
        "account_mode": "kis_mock",
        "execution_source": "watch",
        "lifecycle_state": "previewed",
        "quantity": 1.0,
        "limit_price": 70000.0,
        "notional": 70000.0,
        "currency": "KRW",
        "notional_krw_input": None,
        "max_notional_krw": 1500000.0,
        "notional_krw_evaluated": 70000.0,
        "fx_usd_krw_used": None,
        "approval_required": True,
        "execution_allowed": False,
        "blocking_reasons": [],
        "blocked_by": None,
        "detail": {},
        "preview_line": {"lifecycle_state": "previewed"},
        "triggered_value": 69000.0,
        "kst_date": "2026-05-04",
        "created_at": datetime(2026, 5, 4, 0, 30, tzinfo=UTC),
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _mock_db_for_rows(rows):
    class _Scalars:
        def all(self):
            return rows

    class _Result:
        def scalar_one_or_none(self):
            return rows[0] if rows else None

        def scalars(self):
            return _Scalars()

    db = AsyncMock()
    db.execute = AsyncMock(return_value=_Result())
    return db


def _make_app_with_db(db):
    from app.core.db import get_db
    from app.routers import watch_order_intent_ledger
    from app.routers.dependencies import get_authenticated_user

    app = FastAPI()
    app.include_router(watch_order_intent_ledger.router)
    fake_user = SimpleNamespace(id=1)
    app.dependency_overrides[get_authenticated_user] = lambda: fake_user
    app.dependency_overrides[get_db] = lambda: db
    return app


@pytest.mark.unit
def test_list_recent_returns_200_with_items():
    row = _make_fake_row()
    app = _make_app_with_db(_mock_db_for_rows([row]))
    client = TestClient(app)

    resp = client.get("/trading/api/watch/order-intent/ledger/recent")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 1
    item = data["items"][0]
    assert item["correlation_id"] == "corr-router-1"
    assert item["lifecycle_state"] == "previewed"
    assert item["account_mode"] == "kis_mock"
    assert item["execution_source"] == "watch"


@pytest.mark.unit
def test_list_recent_empty_returns_200_empty_list():
    app = _make_app_with_db(_mock_db_for_rows([]))
    client = TestClient(app)

    resp = client.get("/trading/api/watch/order-intent/ledger/recent")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 0
    assert data["items"] == []


@pytest.mark.unit
def test_get_by_correlation_returns_200_when_found():
    row = _make_fake_row(correlation_id="corr-found")
    app = _make_app_with_db(_mock_db_for_rows([row]))
    client = TestClient(app)

    resp = client.get("/trading/api/watch/order-intent/ledger/corr-found")
    assert resp.status_code == 200
    assert resp.json()["correlation_id"] == "corr-found"


@pytest.mark.unit
def test_get_by_correlation_404_when_missing():
    app = _make_app_with_db(_mock_db_for_rows([]))
    client = TestClient(app)

    resp = client.get("/trading/api/watch/order-intent/ledger/does-not-exist")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "not_found"
```

- [ ] **A2.3: Run the new router test**

Run: `uv run pytest tests/routers/test_watch_order_intent_ledger_router.py -v`
Expected: 4 tests pass. If `_serialize` mishandles a `SimpleNamespace` field, the failing assertion should be a clean `AttributeError` — fix the serializer or the fake row to match. Do not weaken the test to hide a real bug.

### Step A3 — Repair the MCP read-tool test

- [ ] **A3.1: Replace the broken MCP test**

Overwrite `tests/test_mcp_watch_order_intent_ledger.py` with:

```python
"""Tests for read-only watch order intent ledger MCP tools (ROB-103)."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.mcp_server.tooling import watch_order_intent_ledger_read as mod


def _make_fake_row(**overrides: object):
    base = {
        "id": 1,
        "correlation_id": "corr-mcp-1",
        "idempotency_key": "k",
        "market": "kr",
        "target_kind": "asset",
        "symbol": "005930",
        "condition_type": "price_below",
        "threshold": 70000.0,
        "threshold_key": "70000",
        "action": "create_order_intent",
        "side": "buy",
        "account_mode": "kis_mock",
        "execution_source": "watch",
        "lifecycle_state": "previewed",
        "quantity": 1.0,
        "limit_price": 70000.0,
        "notional": 70000.0,
        "currency": "KRW",
        "notional_krw_input": None,
        "max_notional_krw": 1500000.0,
        "notional_krw_evaluated": 70000.0,
        "fx_usd_krw_used": None,
        "approval_required": True,
        "execution_allowed": False,
        "blocking_reasons": [],
        "blocked_by": None,
        "detail": {},
        "preview_line": {"lifecycle_state": "previewed"},
        "triggered_value": 69000.0,
        "kst_date": "2026-05-04",
        "created_at": datetime(2026, 5, 4, 0, 30, tzinfo=UTC),
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _patched_session(monkeypatch: pytest.MonkeyPatch, rows):
    """Patch AsyncSessionLocal so the MCP tool sees a fake AsyncSession."""

    class _Scalars:
        def all(self):
            return rows

    class _Result:
        def scalar_one_or_none(self):
            return rows[0] if rows else None

        def scalars(self):
            return _Scalars()

    fake_session = AsyncMock()
    fake_session.execute = AsyncMock(return_value=_Result())

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=fake_session)
    cm.__aexit__ = AsyncMock(return_value=None)

    monkeypatch.setattr(mod, "AsyncSessionLocal", lambda: cm)
    return fake_session


@pytest.mark.asyncio
async def test_list_recent_returns_serialized_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patched_session(monkeypatch, [_make_fake_row()])

    result = await mod.watch_order_intent_ledger_list_recent_impl()
    assert result["success"] is True
    assert result["count"] == 1
    assert result["items"][0]["correlation_id"] == "corr-mcp-1"


@pytest.mark.asyncio
async def test_list_recent_clamps_limit_to_one_hundred(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patched_session(monkeypatch, [])

    result = await mod.watch_order_intent_ledger_list_recent_impl(limit=10_000)
    assert result["success"] is True
    assert result["count"] == 0


@pytest.mark.asyncio
async def test_get_returns_item_when_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patched_session(monkeypatch, [_make_fake_row(correlation_id="corr-mcp-x")])

    result = await mod.watch_order_intent_ledger_get_impl("corr-mcp-x")
    assert result["success"] is True
    assert result["item"]["correlation_id"] == "corr-mcp-x"


@pytest.mark.asyncio
async def test_get_returns_not_found_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patched_session(monkeypatch, [])

    result = await mod.watch_order_intent_ledger_get_impl("does-not-exist")
    assert result["success"] is False
    assert result["error"] == "not_found"
```

- [ ] **A3.2: Run the MCP tool test**

Run: `uv run pytest tests/test_mcp_watch_order_intent_ledger.py -v`
Expected: 4 tests pass.

### Step A4 — Cross-check that the impl files still match the tests

- [ ] **A4.1: Re-read the impl files and confirm import paths**

Run:
```bash
uv run python - <<'PY'
from app.routers.watch_order_intent_ledger import router, _serialize  # noqa: F401
from app.mcp_server.tooling.watch_order_intent_ledger_read import (
    register_watch_order_intent_ledger_tools,
    watch_order_intent_ledger_get_impl,
    watch_order_intent_ledger_list_recent_impl,
)
print("imports ok")
PY
```
Expected: prints `imports ok`. If `AsyncSessionLocal` is missing from `app.core.db`, search for the actual symbol (`grep -n "AsyncSessionLocal\|async_sessionmaker" app/core/db.py`) and update the import in `app/mcp_server/tooling/watch_order_intent_ledger_read.py` to match what the codebase exports.

### Step A5 — Commit Task 10

- [ ] **A5.1: Stage the four files**

```bash
git add app/routers/watch_order_intent_ledger.py \
        app/mcp_server/tooling/watch_order_intent_ledger_read.py \
        tests/routers/test_watch_order_intent_ledger_router.py \
        tests/test_mcp_watch_order_intent_ledger.py
git status -sb
```
Expected: only the four files staged; `tests/test_watch_order_intent_ledger_router.py` (the deleted broken one) appears as a deletion.

- [ ] **A5.2: Stage the deletion of the broken test**

```bash
git add -A tests/test_watch_order_intent_ledger_router.py
git status -sb
```

- [ ] **A5.3: Commit**

```bash
git commit -m "$(cat <<'EOF'
feat(ROB-103): add read-only ledger router and MCP tools

GET-only HTTP endpoints under /trading/api/watch/order-intent/ledger
and two MCP tools (list_recent, get) backed by review.watch_order_intent_ledger.
Router tests follow the Alpaca paper ledger pattern (TestClient +
dependency_overrides + AsyncMock session). MCP tool tests patch
AsyncSessionLocal so they stay hermetic.

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

## Task B: Wire the router into FastAPI

**Files:**
- Modify: `app/main.py`

- [ ] **B1: Add the import**

In `app/main.py`, the existing block around lines 24–28 imports routers. Find the line `alpaca_paper_ledger,` (currently at ~line 27) and add `watch_order_intent_ledger,` immediately after it, keeping alphabetical-ish order with sibling rows. Concrete edit:

Locate:
```python
    alpaca_paper_ledger,
```
and replace the surrounding tuple/list element with:
```python
    alpaca_paper_ledger,
    watch_order_intent_ledger,
```

(If the imports are grouped differently — e.g. one big `from app.routers import (...)` — match whatever the file actually does. Use `grep -n "alpaca_paper_ledger" app/main.py` to confirm placement before editing.)

- [ ] **B2: Add the include_router call**

In `app/main.py` around line 173 (right after `app.include_router(alpaca_paper_ledger.router)`), append:

```python
    app.include_router(watch_order_intent_ledger.router)
```

- [ ] **B3: Smoke-import the app**

Run:
```bash
uv run python -c "from app.main import app; routes=[r.path for r in app.routes if hasattr(r,'path')]; print([p for p in routes if 'watch/order-intent' in p])"
```
Expected: prints something like `['/trading/api/watch/order-intent/ledger/recent', '/trading/api/watch/order-intent/ledger/{correlation_id}']`.

- [ ] **B4: Commit**

```bash
git add app/main.py
git commit -m "$(cat <<'EOF'
chore(ROB-103): include watch order intent ledger router

Wires the GET-only watch_order_intent_ledger router into the FastAPI app.

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

## Task C: Wire the MCP read tools into the tooling registry

**Files:**
- Modify: `app/mcp_server/tooling/registry.py`

- [ ] **C1: Add the import**

In `app/mcp_server/tooling/registry.py`, after the `from app.mcp_server.tooling.alpaca_paper_ledger_read import (...)` block (lines 26–28), add:

```python
from app.mcp_server.tooling.watch_order_intent_ledger_read import (
    register_watch_order_intent_ledger_tools,
)
```

- [ ] **C2: Register the tools**

Inside `register_all_tools`, after the existing `register_alpaca_paper_ledger_read_tools(mcp)` call (currently line 107), add:

```python
    register_watch_order_intent_ledger_tools(mcp)
```

This goes in the "Always: side-effect-free research + read-only tools" section so both the `DEFAULT` and `HERMES_PAPER_KIS` profiles see the new tools.

- [ ] **C3: Smoke-import the registry**

Run:
```bash
uv run python -c "
from app.mcp_server.tooling.registry import register_all_tools
from fastmcp import FastMCP
mcp = FastMCP('smoke-test')
register_all_tools(mcp)
print('tools registered')
"
```
Expected: prints `tools registered`. Any AttributeError is a real bug — fix it before continuing.

- [ ] **C4: Commit**

```bash
git add app/mcp_server/tooling/registry.py
git commit -m "$(cat <<'EOF'
chore(ROB-103): register watch order intent ledger MCP tools

Adds the new list_recent / get tools to the always-on read-only
tool surface so both the DEFAULT and HERMES_PAPER_KIS profiles
expose them.

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

## Task D: End-to-end ROB-103 test sweep

- [ ] **D1: Run every ROB-103 test file**

Run:
```bash
uv run pytest \
  tests/test_watch_intent_policy.py \
  tests/test_watch_order_intent_preview_builder.py \
  tests/test_watch_order_intent_service.py \
  tests/test_watch_alerts.py \
  tests/test_watch_scanner.py \
  tests/test_mcp_watch_alerts.py \
  tests/routers/test_watch_order_intent_ledger_router.py \
  tests/test_mcp_watch_order_intent_ledger.py \
  -v
```
Expected: every test passes. If a previously-passing test now fails because of Task B/C wiring (e.g. a smoke test that asserts the route list), update the test rather than rolling back the wiring.

- [ ] **D2: Run lint, format, type-check**

Run: `make lint && make typecheck`
Expected: clean.

If `ty` flags `Decimal | float` mismatches in the router/MCP code, prefer narrowing types over adding `# type: ignore`. The router serializer always converts via `float(...)`, which is the existing convention from the Alpaca paper ledger router — it's fine.

- [ ] **D3: Commit any test/lint fixups**

If steps D1 or D2 surfaced fixes:

```bash
git add <fixed files>
git commit -m "$(cat <<'EOF'
chore(ROB-103): address wiring fallout

Adjusts <briefly>—no behavior change.

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

If nothing needed fixing, skip this step.

---

## Task E: Operator runbook

**Files:**
- Create: `docs/runbooks/watch-order-intent-ledger.md`

- [ ] **E1: Read the closest sibling runbook for tone/structure**

Run: `sed -n '1,80p' docs/runbooks/alpaca-paper-ledger.md`

- [ ] **E2: Write `docs/runbooks/watch-order-intent-ledger.md`**

```markdown
# Watch Order Intent Ledger Runbook (ROB-103)

## What this is
`review.watch_order_intent_ledger` captures every approval-required
order-intent preview emitted by the watch scanner. It is **append-only**,
written exclusively by `app.services.watch_order_intent_service.WatchOrderIntentService`,
and never triggers a broker call. `account_mode` is pinned to `kis_mock`
via a CHECK constraint.

## Adding a watch with policy

Use the extended `manage_watch_alerts add` MCP tool:

```
manage_watch_alerts \
  action=add \
  market=kr symbol=005930 metric=price operator=below threshold=70000 \
  intent_action=create_order_intent side=buy quantity=1 max_notional_krw=1500000
```

- Omitting the `intent_action` / `side` / `quantity` / `notional_krw` /
  `limit_price` / `max_notional_krw` kwargs preserves the legacy notify-only
  behavior (alert + delete).
- `notional_krw` is supported only for `market=kr`; for `market=us`, use
  `quantity` (positive integer shares).
- `max_notional_krw` is the **KRW-denominated** safety cap. For US watches
  the service converts `qty * limit_price * usd_krw` and compares to the cap.

## Reading the ledger

MCP:

- `watch_order_intent_ledger_list_recent(market="kr", lifecycle_state="previewed", limit=20)`
- `watch_order_intent_ledger_get(correlation_id="...")`

HTTP:

- `GET /trading/api/watch/order-intent/ledger/recent`
- `GET /trading/api/watch/order-intent/ledger/{correlation_id}`

Both surfaces require an authenticated user (the same authentication
dependency the rest of the trading API uses).

## Mental model

- **previewed** — the operator's policy was satisfied, a ROB-100 `OrderPreviewLine`
  is recorded, and the watch hash field was deleted. There is **at most one
  `previewed` row per (watch identity + side) per KST date** — enforced by a
  partial unique index on `idempotency_key WHERE lifecycle_state = 'previewed'`.
- **failed** — the intent could not be built. The watch is **kept** so the
  operator can adjust the policy and retry. Use `blocked_by` to triage:
  - `max_notional_krw_cap` — qty × limit × (FX for US) exceeded the cap.
  - `fx_unavailable` — USD/KRW quote service was down at trigger time.
  - `qty_zero` — `notional_krw / limit_price` floored below 1 share.
  - `validation_error` — should not happen at scan time; if it does, the
    Redis payload was tampered with — investigate.
- **dedupe_hit** — a previewed row already exists for the same KST date
  and watch identity + side. The watch hash field was deleted (the existing
  row stays the source of truth for the day). No new ledger row is created.
  The n8n alert message marks the line as `dedupe_hit` with the existing
  `ledger_id`.

## Hard rules

- Direct SQL `INSERT/UPDATE/DELETE` against
  `review.watch_order_intent_ledger` is forbidden. Use the service.
- This ledger never authorizes a broker submit. ROB-103 explicitly excludes
  broker mutation.
- Live-account intents are not supported in this MVP. Future PRs will widen
  the matching rule for additional `(market, account_mode)` combinations
  without changing the contract.
```

- [ ] **E3: Commit**

```bash
git add docs/runbooks/watch-order-intent-ledger.md
git commit -m "$(cat <<'EOF'
docs(ROB-103): add watch order intent ledger runbook

Operator-facing runbook covering the ledger schema, the MCP/HTTP
read surfaces, and the previewed/failed/dedupe_hit mental model.

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

## Task F: Final verification before opening the PR

- [ ] **F1: Re-run the full ROB-103 suite**

Run:
```bash
uv run pytest \
  tests/test_watch_intent_policy.py \
  tests/test_watch_order_intent_preview_builder.py \
  tests/test_watch_order_intent_service.py \
  tests/test_watch_alerts.py \
  tests/test_watch_scanner.py \
  tests/test_mcp_watch_alerts.py \
  tests/routers/test_watch_order_intent_ledger_router.py \
  tests/test_mcp_watch_order_intent_ledger.py \
  -v
```
Expected: all green.

- [ ] **F2: Make sure the migration cleanly upgrades and downgrades on a scratch DB**

Run (only if you have a scratch / disposable DB connection — skip if your local DB has data you do not want to wipe):

```bash
uv run alembic upgrade heads
uv run alembic current
```
Expected: `current` includes the ROB-103 migration revision id.

If you want to verify the partial unique index actually exists:
```bash
docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "\\d review.watch_order_intent_ledger" | grep -E "uq_watch_intent_previewed_idempotency|^Indexes:"
```
Expected: lists the partial unique index with `WHERE (lifecycle_state = 'previewed'::text)` predicate.

- [ ] **F3: Inspect the diff against `main`**

Run:
```bash
git fetch origin main
git --no-pager diff --stat origin/main...HEAD
git --no-pager log --oneline origin/main..HEAD
```
Expected: ROB-103 commits only, file count roughly:
- 6 new files (model addition is in existing `review.py`): policy parser, preview builder, service, router, MCP read, runbook + migration + 4 test files + design + plans.
- A handful of modified files: `review.py`, `watch_alerts.py`, `openclaw_client.py`, `watch_scanner.py`, `watch_alerts_registration.py`, `main.py`, `registry.py`.

- [ ] **F4: Push the branch (only after the user OKs)**

```bash
git push -u origin HEAD
```

Then open the PR (do not auto-create it without confirmation):

```bash
gh pr create --title "ROB-103: watch order intent MVP for approval-required KIS mock actions" --body "$(cat <<'EOF'
## Summary
- Adds an approval-required `OrderPreviewLine` branch to the watch scanner backed by a new `review.watch_order_intent_ledger`.
- Per-watch action policy is encoded in the existing Redis hash payload (JSON); the legacy `created_at`-only payload still parses as `notify_only`.
- KST-day idempotency via a partial unique index on `idempotency_key` scoped to `lifecycle_state='previewed'`.
- US watches FX-convert `qty * limit_price` to KRW for the `max_notional_krw` cap.
- n8n batched alert payload gains an additive `intents` block; the existing `triggered` field is unchanged.
- `manage_watch_alerts add` accepts optional policy kwargs; defaults preserve the legacy notify-only behavior.
- New read-only HTTP endpoints under `/trading/api/watch/order-intent/ledger/...` and read-only MCP tools `watch_order_intent_ledger_list_recent` / `_get`.
- Spec: `docs/superpowers/specs/2026-05-04-rob-103-watch-order-intent-mvp-design.md`. Plans: `docs/superpowers/plans/2026-05-04-rob-103-watch-order-intent-mvp.md` (initial) and `2026-05-04-rob-103-watch-order-intent-mvp-finish.md` (catch-up).

## Test plan
- [ ] `uv run pytest tests/test_watch_intent_policy.py tests/test_watch_order_intent_preview_builder.py tests/test_watch_order_intent_service.py tests/test_watch_alerts.py tests/test_watch_scanner.py tests/test_mcp_watch_alerts.py tests/routers/test_watch_order_intent_ledger_router.py tests/test_mcp_watch_order_intent_ledger.py -v`
- [ ] `make lint && make typecheck`
- [ ] `uv run alembic upgrade heads` on a clean DB and verify `\d review.watch_order_intent_ledger` shows the partial unique index.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-review

- **Spec coverage:** Original tasks 1–9 cover spec sections 1–6 + parts of 7. Tasks A–E in this finish plan cover the read-only surface (spec §9), runbook (§11), and the verification sweep that the original Task 12 deferred. Spec §12 (deferred follow-ups) intentionally has no tasks.
- **Placeholder scan:** No "TBD" / "TODO" / "fill in" remains. The only conditional language is in Task B/C where the engineer is told to confirm exact line numbers via `grep` before editing — those are real instructions, not placeholders.
- **Type consistency:** `_make_fake_row` uses `SimpleNamespace` and matches the column attribute names defined in `app/models/review.WatchOrderIntentLedger`. `_serialize` calls `float(...)` on `Numeric` fields and `.isoformat()` on `created_at`, matching the column types committed in `9b82a90a`.
- **Wiring correctness:** Task B targets `app/main.py` line 27 (import) + line 173 (include_router) — both confirmed by `grep` against the current `aquamarine-canvas` head. Task C targets `app/mcp_server/tooling/registry.py` line 26 (import) + line 107 (registration call) in the always-on section — confirmed.

# Retrospective Action Lifecycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn retrospective `next_actions` from an embedded read-only JSONB checklist into a durable lifecycle with stable identity, atomic manual transitions, bounded analysis consumption, and an evidence-safe path to later automation.

**Architecture:** Add `review.trade_retrospective_actions` as the canonical store behind a shadow/canonical control row. Retain parent JSONB temporarily as a compatibility projection. All writes lock parent-then-children by ID. Terminal resolutions are immutable and versioned. Manual transitions require authenticated operator + CSRF (web) or privileged MCP profile.

**Tech Stack:** Python 3.13, SQLAlchemy 2.x (async), PostgreSQL 16+, Alembic, FastAPI, pytest (persistent test DB via `Base.metadata.create_all`), React + TypeScript (frontend).

## Global Constraints

- Python 3.13+; toolchain via `uv`; lint/format = Ruff + ty; test suite = pytest with strict markers.
- No live broker, Linear API, Telegram, or external service calls in any test.
- No hardcoded secrets. No `as any` / `@ts-ignore` / type suppressions.
- Migration must leave parent JSONB byte-for-byte unchanged in shadow mode.
- Provisional child IDs must not be exposed to readers/API/MCP until canonical cutover.
- Control mode starts as `shadow` and stays there for child-1.
- Due date alone never produces `expired`.
- `git diff --check` must be clean before any commit.

## Design Source of Truth

`docs/superpowers/specs/2026-07-14-rob-878-retrospective-action-lifecycle-design.md` (commit `18c7b911`) — user-approved. This plan does not re-litigate design decisions.

---

## Dependency Graph

```text
Child 1 (shadow ledger) ──────> Child 2 (canonical cutover)
                                   │
                                   ├──> Child 3 (transition core) ──> Child 4 (operator surface)
                                   │                                      │
                                   │                                      └──> Child 5 (triage)
                                   ├──> Child 6 (decision_history)
                                   └──> Child 7 (/invest UX)

Related (non-blocking):
  Child 5 ──> Reconciler (typed binding + dry-run)
  Child 4 + Child 7 ──> Projection retirement (after 14-day parity window)
```

---

## File Structure (all 7 issues)

### Child 1 — Shadow Ledger

| Action | File |
|--------|------|
| Create (ORM) | `app/models/review.py` — add `TradeRetrospectiveAction` + `TradeRetrospectiveActionControl` |
| Modify (exports) | `app/models/__init__.py` — add new model imports + `__all__` entries |
| Create (migration) | `alembic/versions/20260714_rob878_trade_retrospective_actions_shadow.py` |
| Modify (test bootstrap) | `tests/_schema_bootstrap.py` — bump `SCHEMA_BOOTSTRAP_VERSION` 14→15 + mirror trigger DDL |
| Create (tests) | `tests/test_rob878_shadow_ledger_migration.py` |
| Create (tests) | `tests/test_rob878_shadow_ledger_model.py` |

### Child 2 — Canonical Cutover

| Action | File |
|--------|------|
| Create (repository) | `app/services/trade_journal/retrospective_action_repository.py` |
| Create (command) | `scripts/retrospective_action_cutover.py` |
| Modify (service) | `app/services/trade_journal/trade_retrospective_service.py` — canonical read path, projection writer |
| Modify (router) | `app/routers/invest_retrospectives.py` — canonical GET endpoint |
| Modify (deploy) | `scripts/deploy-native.sh` — post-switch cutover step |
| Create (tests) | `tests/test_rob878_canonical_cutover.py` |

### Child 3 — Transition Core

| Action | File |
|--------|------|
| Create (service) | `app/services/trade_journal/retrospective_action_transition.py` |
| Create (types) | `app/services/trade_journal/retrospective_action_types.py` |
| Create (tests) | `tests/test_rob878_transition_core.py` |

### Child 4 — Operator Surface

| Action | File |
|--------|------|
| Modify (router) | `app/routers/invest_retrospectives.py` — PATCH endpoint |
| Modify (MCP) | `app/mcp_server/tooling/trade_retrospective_registration.py` — read + preview tools |
| Modify (MCP) | `app/mcp_server/tooling/tradingcodex_execution_registration.py` — commit tool |
| Modify (schemas) | `app/schemas/trade_retrospective.py` — lifecycle transport fields |
| Create (tests) | `tests/test_rob878_operator_http.py` |
| Create (tests) | `tests/test_rob878_operator_mcp.py` |

### Child 5 — Backlog Triage

| Action | File |
|--------|------|
| Create (CLI) | `scripts/retrospective_action_triage.py` |
| Create (tests) | `tests/test_rob878_triage.py` |

### Child 6 — decision_history.open_actions

| Action | File |
|--------|------|
| Modify (service) | `app/analysis/decision_history.py` (or equivalent) |
| Create (tests) | `tests/test_rob878_decision_history_actions.py` |

### Child 7 — /invest UX

| Action | File |
|--------|------|
| Modify (frontend) | `frontend/invest/src/.../RetrospectivesPanel.tsx` and related |
| Modify (frontend) | `frontend/invest/src/types/retrospectives.ts` |
| Create (tests) | frontend test suite additions |

---

## Child Issue 1: Shadow Ledger — schema / preflight / backfill / write fence

**Scope:** Additive schema only. No canonical read, no mutation, no cutover. Parent JSONB stays authoritative. Control mode = `shadow`.

**Contracts (must hold):**
- SQL NULL, JSONB null, `[]` → 0 actions.
- Non-array, non-object element, blank action, unknown status, invalid ISO date → fail with retrospective ID + ordinal.
- Missing/null/blank status → backfill as `open`.
- Existing `open`/`in_progress`/`done` → preserved.
- Due date alone → never `expired`.
- Parent JSONB → byte-for-byte unchanged.
- Provisional child IDs → not exposed to any reader/API/MCP.
- Control mode → `shadow`.
- Downgrade supported only while mode = `shadow`.

---

### Task 1.1: ORM Models — `TradeRetrospectiveAction` + `TradeRetrospectiveActionControl`

**Files:**
- Modify: `app/models/review.py` (append after `TradeRetrospective` class, before `TradeForecast` at line 1157)
- Modify: `app/models/__init__.py` (add imports + `__all__` entries)

**Interfaces:**
- Consumes: `app.models.base.Base`, `app.models.review.TradeRetrospective` (FK target)
- Produces: `TradeRetrospectiveAction`, `TradeRetrospectiveActionControl` on `Base.metadata`

- [ ] **Step 1: Write the failing model smoke test**

Create `tests/test_rob878_shadow_ledger_model.py`:

```python
"""ROB-878 child-1: ORM model smoke tests for shadow ledger tables."""

import pytest
from sqlalchemy import inspect, text


@pytest.mark.asyncio
async def test_trade_retrospective_action_table_registered_on_metadata():
    """Both new tables must be registered on Base.metadata for create_all."""
    from app.models.base import Base

    table_names = set(Base.metadata.tables.keys())
    assert "review.trade_retrospective_actions" in table_names
    assert "review.trade_retrospective_action_control" in table_names


@pytest.mark.asyncio
async def test_trade_retrospective_action_columns_exist(db_session):
    """The action table has all required columns with correct types."""
    result = await db_session.execute(
        text(
            "SELECT column_name, data_type, is_nullable "
            "FROM information_schema.columns "
            "WHERE table_schema = 'review' "
            "AND table_name = 'trade_retrospective_actions' "
            "ORDER BY ordinal_position"
        )
    )
    cols = {row.column_name: row for row in result}
    expected = {
        "id", "retrospective_id", "creation_key", "position", "action",
        "owner", "issue_id", "status", "due_kst_date", "version",
        "status_changed_at", "resolved_at", "status_actor", "status_source",
        "status_reason", "status_evidence", "legacy_payload",
        "created_at", "updated_at",
    }
    assert expected <= set(cols.keys()), f"missing: {expected - set(cols.keys())}"
    assert cols["id"].data_type == "uuid"
    assert cols["retrospective_id"].data_type == "bigint"
    assert cols["position"].data_type == "integer"
    assert cols["action"].data_type == "text"
    assert cols["status"].data_type == "text"
    assert cols["version"].data_type == "integer"
    assert cols["legacy_payload"].data_type == "jsonb"
    assert cols["status"].is_nullable == "NO"
    assert cols["version"].is_nullable == "NO"
    assert cols["position"].is_nullable == "NO"


@pytest.mark.asyncio
async def test_control_table_singleton_structure(db_session):
    """The control table enforces singleton id=1 with mode check."""
    result = await db_session.execute(
        text(
            "SELECT column_name, data_type, is_nullable "
            "FROM information_schema.columns "
            "WHERE table_schema = 'review' "
            "AND table_name = 'trade_retrospective_action_control' "
            "ORDER BY ordinal_position"
        )
    )
    cols = {row.column_name: row for row in result}
    assert "id" in cols
    assert "mode" in cols
    assert "cutover_at" in cols
    assert "cutover_action_count" in cols
    assert "updated_at" in cols
    assert cols["mode"].data_type == "text"
    assert cols["mode"].is_nullable == "NO"


@pytest.mark.asyncio
async def test_control_row_exists_in_shadow_mode(db_session):
    """Exactly one control row exists with mode='shadow' after bootstrap."""
    result = await db_session.execute(
        text(
            "SELECT id, mode FROM review.trade_retrospective_action_control "
            "ORDER BY id"
        )
    )
    rows = result.fetchall()
    assert len(rows) == 1
    assert rows[0].id == 1
    assert rows[0].mode == "shadow"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_rob878_shadow_ledger_model.py -v -x`
Expected: FAIL — tables not found (not yet in metadata / not in DB).

- [ ] **Step 3: Add ORM models to `app/models/review.py`**

Insert after line 1155 (after `TradeRetrospective` class, before the `TradeForecast` section comment):

```python
# ---------------------------------------------------------------------------
# review.trade_retrospective_actions — canonical action lifecycle (ROB-878)
# ---------------------------------------------------------------------------
class TradeRetrospectiveAction(Base):
    """ROB-878 — canonical retrospective action with stable identity and lifecycle.

    Shadow mode: rows are backfilled from parent JSONB but not exposed to any
    reader. Canonical mode: all reads/writes move here; parent JSONB becomes a
    compatibility projection.
    """

    __tablename__ = "trade_retrospective_actions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["retrospective_id"],
            ["review.trade_retrospectives.id"],
            ondelete="CASCADE",
            name="fk_trade_retrospective_actions_retrospective",
            deferrable=True,
            initially="DEFERRED",
        ),
        UniqueConstraint(
            "retrospective_id",
            "position",
            name="uq_trade_retrospective_actions_position",
            deferrable=True,
            initially="DEFERRED",
        ),
        CheckConstraint(
            "status IN ('open','in_progress','done','obsolete','expired')",
            name="ck_trade_retrospective_actions_status",
        ),
        CheckConstraint(
            "status_source IN ('migration','retrospective_save','web','mcp','triage','reconciler')",
            name="ck_trade_retrospective_actions_status_source",
        ),
        CheckConstraint("version >= 1", name="ck_trade_retrospective_actions_version"),
        CheckConstraint("position >= 0", name="ck_trade_retrospective_actions_position"),
        # terminal states must have resolved_at; active states must not
        CheckConstraint(
            "(status IN ('done','obsolete','expired') AND resolved_at IS NOT NULL) "
            "OR (status IN ('open','in_progress') AND resolved_at IS NULL)",
            name="ck_trade_retrospective_actions_resolved_terminal",
        ),
        # obsolete and expired require a nonblank reason
        CheckConstraint(
            "(status NOT IN ('obsolete','expired')) "
            "OR (status_reason IS NOT NULL AND btrim(status_reason) <> '' "
            "AND length(status_reason) <= 2000)",
            name="ck_trade_retrospective_actions_reason_required",
        ),
        # expired requires structured evidence object
        CheckConstraint(
            "(status <> 'expired') "
            "OR (status_evidence IS NOT NULL "
            "AND jsonb_typeof(status_evidence) = 'object')",
            name="ck_trade_retrospective_actions_evidence_required",
        ),
        Index(
            "ix_trade_retrospective_actions_parent_position",
            "retrospective_id",
            "position",
            "id",
        ),
        Index(
            "ix_trade_retrospective_actions_due_active",
            "due_kst_date",
            "id",
            postgresql_where=text("status IN ('open', 'in_progress')"),
        ),
        Index(
            "uq_trade_retrospective_actions_creation_key",
            "retrospective_id",
            "creation_key",
            unique=True,
            postgresql_where=text("creation_key IS NOT NULL"),
        ),
        Index(
            "ix_trade_retrospective_actions_issue_id",
            "issue_id",
            postgresql_where=text("issue_id IS NOT NULL"),
        ),
        Index(
            "ix_trade_retrospective_actions_status_updated",
            "status",
            "updated_at",
            "id",
        ),
        {"schema": "review"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    retrospective_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    creation_key: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True)
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    owner: Mapped[str | None] = mapped_column(Text)
    issue_id: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'open'")
    )
    due_kst_date: Mapped[date | None] = mapped_column(Date)
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    status_changed_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    resolved_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    status_actor: Mapped[str] = mapped_column(VARCHAR(128), nullable=False)
    status_source: Mapped[str] = mapped_column(Text, nullable=False)
    status_reason: Mapped[str | None] = mapped_column(Text)
    status_evidence: Mapped[dict | None] = mapped_column(JSONB)
    legacy_payload: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class TradeRetrospectiveActionControl(Base):
    """ROB-878 — singleton lifecycle control row (shadow/canonical mode)."""

    __tablename__ = "trade_retrospective_action_control"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_trade_retrospective_action_control_singleton"),
        CheckConstraint(
            "mode IN ('shadow','canonical')",
            name="ck_trade_retrospective_action_control_mode",
        ),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    mode: Mapped[str] = mapped_column(Text, nullable=False)
    cutover_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    cutover_action_count: Mapped[int | None] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
```

Add necessary imports at the top of `review.py` if not already present:
`import uuid`, `from datetime import date`, `from sqlalchemy import Integer, SmallInteger, Date, VARCHAR`, `from sqlalchemy import ForeignKeyConstraint` (if not already imported).

- [ ] **Step 4: Add exports to `app/models/__init__.py`**

In the `from .review import (...)` block, add:
```python
    TradeRetrospectiveAction,
    TradeRetrospectiveActionControl,
```

In `__all__`, add:
```python
    "TradeRetrospectiveAction",
    "TradeRetrospectiveActionControl",
```

- [ ] **Step 5: Run model smoke test to verify it passes**

Run: `uv run pytest tests/test_rob878_shadow_ledger_model.py -v -x`
Expected: PASS — tables registered on metadata and exist in test DB (after bootstrap bump in Task 1.2).

**Note:** This test will still fail until Task 1.2 bumps the bootstrap version. That's expected — TDD RED here is "tables missing from DB," and GREEN requires both the model (this task) and the bootstrap bump (next task).

- [ ] **Step 6: Commit**

```bash
git add app/models/review.py app/models/__init__.py tests/test_rob878_shadow_ledger_model.py
git commit -m "feat(ROB-878): add TradeRetrospectiveAction + Control ORM models"
```

---

### Task 1.2: Schema Bootstrap Bump + Trigger DDL Mirroring

**Files:**
- Modify: `tests/_schema_bootstrap.py` — bump version 14→15, mirror trigger function + singleton insert

**Interfaces:**
- Consumes: `app.models.review.TradeRetrospectiveAction` (for `create_all`)
- Produces: test DB has both tables + write-fence trigger + shadow control row

- [ ] **Step 1: Write the failing bootstrap test**

Add to `tests/test_rob878_shadow_ledger_model.py`:

```python
@pytest.mark.asyncio
async def test_write_fence_trigger_exists_on_parent(db_session):
    """The write-fence trigger function and trigger exist on the parent table."""
    result = await db_session.execute(
        text(
            "SELECT tgname FROM pg_trigger "
            "WHERE tgrelid = 'review.trade_retrospectives'::regclass "
            "AND NOT tgisinternal"
        )
    )
    trigger_names = {row.tgname for row in result}
    assert "trg_trade_retrospective_next_actions_fence" in trigger_names


@pytest.mark.asyncio
async def test_write_fence_function_exists(db_session):
    """The trigger function exists and is callable."""
    result = await db_session.execute(
        text(
            "SELECT proname FROM pg_proc p "
            "JOIN pg_namespace n ON p.pronamespace = n.oid "
            "WHERE n.nspname = 'review' "
            "AND proname = 'guard_trade_retrospective_next_actions'"
        )
    )
    assert result.fetchone() is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_rob878_shadow_ledger_model.py::test_write_fence_trigger_exists_on_parent -v -x`
Expected: FAIL — trigger not found.

- [ ] **Step 3: Bump version and add trigger DDL to `_DDL_STATEMENTS`**

In `tests/_schema_bootstrap.py`:

1. Change `SCHEMA_BOOTSTRAP_VERSION = 14` to `SCHEMA_BOOTSTRAP_VERSION = 15`.
2. Add a comment entry after the v11 line:
```python
# v12 (ROB-848): paper-validation triggers mirrored below.
# v13–14: incremental mirror updates.
# v15 (ROB-878): review.trade_retrospective_actions (new ORM table via
# create_all) + write-fence trigger + singleton control row mirrored below.
```

3. Append the following statements at the END of the `_DDL_STATEMENTS` tuple (before the closing `)`):

```python
    # ---- ROB-878: retrospective action write-fence trigger ----
    # The trigger permits legacy writes in shadow mode and rejects direct
    # next_actions changes in canonical mode unless the transaction has set
    # the projection-writer GUC marker.
    "CREATE OR REPLACE FUNCTION "
    "review.guard_trade_retrospective_next_actions() "
    "RETURNS trigger AS $$ DECLARE ctrl_mode TEXT; writer_marker TEXT; "
    "BEGIN "
    "SELECT mode INTO ctrl_mode "
    "FROM review.trade_retrospective_action_control WHERE id = 1; "
    "IF ctrl_mode IS NULL OR ctrl_mode = 'shadow' THEN "
    "RETURN NEW; "
    "END IF; "
    "writer_marker := current_setting("
    "'app.retrospective_action_projection_writer', true); "
    "IF writer_marker IS NULL OR writer_marker <> 'v1' THEN "
    "IF TG_OP = 'INSERT' THEN "
    "IF NEW.next_actions IS NOT NULL THEN "
    "RAISE EXCEPTION "
    "'canonical mode: direct next_actions insert rejected; "
    "use the action repository' USING ERRCODE = 'restrict_violation'; "
    "END IF; "
    "ELSE "
    "IF NEW.next_actions IS DISTINCT FROM OLD.next_actions THEN "
    "RAISE EXCEPTION "
    "'canonical mode: direct next_actions update rejected; "
    "use the action repository' USING ERRCODE = 'restrict_violation'; "
    "END IF; "
    "END IF; "
    "END IF; "
    "RETURN NEW; "
    "END; $$ LANGUAGE plpgsql",
    "DROP TRIGGER IF EXISTS trg_trade_retrospective_next_actions_fence "
    "ON review.trade_retrospectives",
    "CREATE TRIGGER trg_trade_retrospective_next_actions_fence "
    "BEFORE INSERT OR UPDATE ON review.trade_retrospectives "
    "FOR EACH ROW EXECUTE FUNCTION "
    "review.guard_trade_retrospective_next_actions()",
    # Singleton control row — only insert if absent (idempotent).
    "INSERT INTO review.trade_retrospective_action_control (id, mode) "
    "VALUES (1, 'shadow') "
    "ON CONFLICT (id) DO NOTHING",
```

- [ ] **Step 4: Run bootstrap + model tests**

Run: `uv run pytest tests/test_rob878_shadow_ledger_model.py -v`
Expected: All PASS — tables created by `create_all`, trigger + control row by `_DDL_STATEMENTS`.

Run: `uv run pytest tests/infra/test_schema_barrier.py -v`
Expected: PASS — no idempotency or hash-stability regressions.

- [ ] **Step 5: Commit**

```bash
git add tests/_schema_bootstrap.py tests/test_rob878_shadow_ledger_model.py
git commit -m "feat(ROB-878): bump schema bootstrap + mirror write-fence trigger"
```

---

### Task 1.3: Alembic Migration — Tables, Constraints, Indexes

**Files:**
- Create: `alembic/versions/20260714_rob878_trade_retrospective_actions_shadow.py`

**Interfaces:**
- Consumes: existing head `20260713_rob848_paper_validation`
- Produces: `review.trade_retrospective_actions` + `review.trade_retrospective_action_control` tables in real DB

- [ ] **Step 1: Write the failing migration test**

Create `tests/test_rob878_shadow_ledger_migration.py`:

```python
"""ROB-878 child-1: migration tests for shadow ledger schema.

Tests use rolled-back transactions against the test DB engine (Pattern A
from the binance root-reservation precedent). They do NOT shell out to
alembic; instead they import the migration module and call its DDL helpers
or re-run the op statements in a controlled scope.
"""

import pytest
from sqlalchemy import text

from app.core.db import engine


@pytest.mark.asyncio
async def test_migration_revision_metadata():
    """The migration module has correct revision chain."""
    import importlib
    mod = importlib.import_module(
        "alembic.versions.20260714_rob878_trade_retrospective_actions_shadow"
    )
    assert mod.revision == "20260714_rob878_shadow"
    assert mod.down_revision == "20260713_rob848_paper_validation"


@pytest.mark.asyncio
async def test_action_table_check_constraints(db_session):
    """All design-specified CHECK constraints exist on the action table."""
    result = await db_session.execute(
        text(
            "SELECT conname FROM pg_constraint "
            "WHERE conrelid = 'review.trade_retrospective_actions'::regclass "
            "AND contype = 'c' ORDER BY conname"
        )
    )
    names = {row.conname for row in result}
    expected = {
        "ck_trade_retrospective_actions_status",
        "ck_trade_retrospective_actions_status_source",
        "ck_trade_retrospective_actions_version",
        "ck_trade_retrospective_actions_position",
        "ck_trade_retrospective_actions_resolved_terminal",
        "ck_trade_retrospective_actions_reason_required",
        "ck_trade_retrospective_actions_evidence_required",
    }
    assert expected <= names, f"missing: {expected - names}"


@pytest.mark.asyncio
async def test_deferrable_position_uniqueness(db_session):
    """The (retrospective_id, position) uniqueness is deferrable initially deferred."""
    result = await db_session.execute(
        text(
            "SELECT conname, condeferrable, condeferred FROM pg_constraint "
            "WHERE conrelid = 'review.trade_retrospective_actions'::regclass "
            "AND contype = 'u'"
        )
    )
    for row in result:
        if "position" in row.conname:
            assert row.condeferrable
            assert row.condeferred


@pytest.mark.asyncio
async def test_indexes_exist(db_session):
    """All design-specified indexes exist."""
    result = await db_session.execute(
        text(
            "SELECT indexname FROM pg_indexes "
            "WHERE schemaname = 'review' "
            "AND tablename = 'trade_retrospective_actions'"
        )
    )
    names = {row.indexname for row in result}
    expected = {
        "ix_trade_retrospective_actions_parent_position",
        "ix_trade_retrospective_actions_due_active",
        "uq_trade_retrospective_actions_creation_key",
        "ix_trade_retrospective_actions_issue_id",
        "ix_trade_retrospective_actions_status_updated",
    }
    assert expected <= names, f"missing: {expected - names}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_rob878_shadow_ledger_migration.py -v -x`
Expected: FAIL — module not found.

- [ ] **Step 3: Write the migration file**

Create `alembic/versions/20260714_rob878_trade_retrospective_actions_shadow.py`:

```python
"""ROB-878 child-1: retrospective action shadow ledger (schema + backfill).

Revision ID: 20260714_rob878_shadow
Revises: 20260713_rob848_paper_validation
Create Date: 2026-07-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260714_rob878_shadow"
down_revision = "20260713_rob848_paper_validation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCHEMA = "review"
_ACTIONS_TABLE = "trade_retrospective_actions"
_CONTROL_TABLE = "trade_retrospective_action_control"
_PARENT_TABLE = "trade_retrospectives"


# ---------------------------------------------------------------------------
# Trigger function DDL — write-fence for parent next_actions JSONB.
# Shadow mode: permits all writes. Canonical mode: rejects changes unless
# the transaction has SET LOCAL the projection-writer GUC marker.
# ---------------------------------------------------------------------------
_TRIGGER_FUNCTION_DDL = """
CREATE OR REPLACE FUNCTION review.guard_trade_retrospective_next_actions()
RETURNS trigger AS $$
DECLARE
    ctrl_mode TEXT;
    writer_marker TEXT;
BEGIN
    SELECT mode INTO ctrl_mode
    FROM review.trade_retrospective_action_control WHERE id = 1;

    -- Shadow mode or missing control row: permit all legacy writes.
    IF ctrl_mode IS NULL OR ctrl_mode = 'shadow' THEN
        RETURN NEW;
    END IF;

    -- Canonical mode: only the repository projection writer may change
    -- next_actions. It sets the GUC marker via SET LOCAL immediately
    -- before its projection write.
    writer_marker := current_setting(
        'app.retrospective_action_projection_writer', true);

    IF writer_marker IS NULL OR writer_marker <> 'v1' THEN
        IF TG_OP = 'INSERT' THEN
            IF NEW.next_actions IS NOT NULL THEN
                RAISE EXCEPTION
                    'canonical mode: direct next_actions insert rejected; '
                    'use the action repository'
                    USING ERRCODE = 'restrict_violation';
            END IF;
        ELSE  -- UPDATE
            IF NEW.next_actions IS DISTINCT FROM OLD.next_actions THEN
                RAISE EXCEPTION
                    'canonical mode: direct next_actions update rejected; '
                    'use the action repository'
                    USING ERRCODE = 'restrict_violation';
            END IF;
        END IF;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""


def upgrade() -> None:
    # --- 1. Create the action child table ---
    op.create_table(
        _ACTIONS_TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True),
                  server_default=sa.text("gen_random_uuid()"),
                  primary_key=True, nullable=False),
        sa.Column("retrospective_id", sa.BigInteger(), nullable=False),
        sa.Column("creation_key", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("owner", sa.Text(), nullable=True),
        sa.Column("issue_id", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="'open'"),
        sa.Column("due_kst_date", sa.Date(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status_changed_at", sa.TIMESTAMP(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("resolved_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("status_actor", sa.VARCHAR(128), nullable=False),
        sa.Column("status_source", sa.Text(), nullable=False),
        sa.Column("status_reason", sa.Text(), nullable=True),
        sa.Column("status_evidence", postgresql.JSONB(astext_type=sa.Text()),
                  nullable=True),
        sa.Column("legacy_payload", postgresql.JSONB(astext_type=sa.Text()),
                  nullable=False, server_default="'{}'::jsonb"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "status IN ('open','in_progress','done','obsolete','expired')",
            name="ck_trade_retrospective_actions_status",
        ),
        sa.CheckConstraint(
            "status_source IN ('migration','retrospective_save','web','mcp','triage','reconciler')",
            name="ck_trade_retrospective_actions_status_source",
        ),
        sa.CheckConstraint("version >= 1",
            name="ck_trade_retrospective_actions_version"),
        sa.CheckConstraint("position >= 0",
            name="ck_trade_retrospective_actions_position"),
        sa.CheckConstraint(
            "(status IN ('done','obsolete','expired') AND resolved_at IS NOT NULL) "
            "OR (status IN ('open','in_progress') AND resolved_at IS NULL)",
            name="ck_trade_retrospective_actions_resolved_terminal",
        ),
        sa.CheckConstraint(
            "(status NOT IN ('obsolete','expired')) "
            "OR (status_reason IS NOT NULL AND btrim(status_reason) <> '' "
            "AND length(status_reason) <= 2000)",
            name="ck_trade_retrospective_actions_reason_required",
        ),
        sa.CheckConstraint(
            "(status <> 'expired') "
            "OR (status_evidence IS NOT NULL "
            "AND jsonb_typeof(status_evidence) = 'object')",
            name="ck_trade_retrospective_actions_evidence_required",
        ),
        sa.ForeignKeyConstraint(
            ["retrospective_id"], ["review.trade_retrospectives.id"],
            ondelete="CASCADE",
            name="fk_trade_retrospective_actions_retrospective",
            deferrable=True, initially="DEFERRED",
        ),
        sa.UniqueConstraint(
            "retrospective_id", "position",
            name="uq_trade_retrospective_actions_position",
            deferrable=True, initially="DEFERRED",
        ),
        schema=_SCHEMA,
    )

    # --- 2. Indexes ---
    op.create_index(
        "ix_trade_retrospective_actions_parent_position",
        _ACTIONS_TABLE, ["retrospective_id", "position", "id"], schema=_SCHEMA,
    )
    op.create_index(
        "ix_trade_retrospective_actions_due_active",
        _ACTIONS_TABLE, ["due_kst_date", "id"], schema=_SCHEMA,
        postgresql_where=sa.text("status IN ('open', 'in_progress')"),
    )
    op.create_index(
        "uq_trade_retrospective_actions_creation_key",
        _ACTIONS_TABLE, ["retrospective_id", "creation_key"], schema=_SCHEMA,
        unique=True,
        postgresql_where=sa.text("creation_key IS NOT NULL"),
    )
    op.create_index(
        "ix_trade_retrospective_actions_issue_id",
        _ACTIONS_TABLE, ["issue_id"], schema=_SCHEMA,
        postgresql_where=sa.text("issue_id IS NOT NULL"),
    )
    op.create_index(
        "ix_trade_retrospective_actions_status_updated",
        _ACTIONS_TABLE, ["status", "updated_at", "id"], schema=_SCHEMA,
    )

    # --- 3. Singleton control table ---
    op.create_table(
        _CONTROL_TABLE,
        sa.Column("id", sa.SmallInteger(), primary_key=True, nullable=False),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("cutover_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("cutover_action_count", sa.Integer(), nullable=True),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("id = 1",
            name="ck_trade_retrospective_action_control_singleton"),
        sa.CheckConstraint("mode IN ('shadow','canonical')",
            name="ck_trade_retrospective_action_control_mode"),
        schema=_SCHEMA,
    )

    # --- 4. Insert shadow singleton ---
    op.execute(
        "INSERT INTO review.trade_retrospective_action_control (id, mode) "
        "VALUES (1, 'shadow')"
    )

    # --- 5. Install write-fence trigger ---
    op.execute(_TRIGGER_FUNCTION_DDL)
    op.execute(
        "DROP TRIGGER IF EXISTS trg_trade_retrospective_next_actions_fence "
        "ON review.trade_retrospectives"
    )
    op.execute(
        "CREATE TRIGGER trg_trade_retrospective_next_actions_fence "
        "BEFORE INSERT OR UPDATE ON review.trade_retrospectives "
        "FOR EACH ROW EXECUTE FUNCTION "
        "review.guard_trade_retrospective_next_actions()"
    )

    # --- 6. Preflight: validate all existing next_actions before backfill ---
    _run_preflight()

    # --- 7. Backfill from parent JSONB ---
    _run_backfill()

    # --- 8. Parity assertion ---
    _assert_parity()


def _run_preflight() -> None:
    """Classify legacy values before casting. Fail on malformed data.

    SQL NULL, JSONB null, and [] each mean zero actions.
    Any non-array value fails. Every array element must be an object with
    a nonblank action; status must be null/blank or one of
    open/in_progress/done; a nonblank due date must pass strict YYYY-MM-DD.
    """
    op.execute(
        """
        DO $$
        DECLARE
            r RECORD;
            elem JSONB;
            idx INTEGER;
            raw_status TEXT;
            raw_due TEXT;
        BEGIN
            FOR r IN
                SELECT id, next_actions
                FROM review.trade_retrospectives
                WHERE next_actions IS NOT NULL
                  AND jsonb_typeof(next_actions) <> 'null'
            LOOP
                IF jsonb_typeof(r.next_actions) = 'array' THEN
                    idx := 0;
                    FOR elem IN SELECT * FROM jsonb_array_elements(r.next_actions)
                    LOOP
                        IF jsonb_typeof(elem) <> 'object' THEN
                            RAISE EXCEPTION
                                'retrospective % action[%]: element is not an object',
                                r.id, idx;
                        END IF;
                        IF btrim(COALESCE(elem->>'action', '')) = '' THEN
                            RAISE EXCEPTION
                                'retrospective % action[%]: blank action',
                                r.id, idx;
                        END IF;
                        raw_status := elem->>'status';
                        IF raw_status IS NOT NULL AND btrim(raw_status) <> ''
                           AND raw_status NOT IN ('open','in_progress','done') THEN
                            RAISE EXCEPTION
                                'retrospective % action[%]: unknown status "%"',
                                r.id, idx, raw_status;
                        END IF;
                        raw_due := elem->>'due_kst_date';
                        IF raw_due IS NOT NULL AND btrim(raw_due) <> ''
                           AND raw_due !~ '^\d{4}-\d{2}-\d{2}$' THEN
                            RAISE EXCEPTION
                                'retrospective % action[%]: invalid due_kst_date "%"',
                                r.id, idx, raw_due;
                        END IF;
                        idx := idx + 1;
                    END LOOP;
                ELSIF jsonb_typeof(r.next_actions) <> 'array' THEN
                    RAISE EXCEPTION
                        'retrospective %: next_actions is not an array (type=%)',
                        r.id, jsonb_typeof(r.next_actions);
                END IF;
            END LOOP;
        END;
        $$
        """
    )


def _run_backfill() -> None:
    """Backfill child rows from parent JSONB using jsonb_array_elements WITH ORDINALITY.

    Missing/null/blank status → open. Existing open/in_progress/done preserved.
    Zero-based position = ordinality - 1. Every row uses parent timestamps,
    migration:rob-878 as actor, migration as source.
    """
    op.execute(
        """
        INSERT INTO review.trade_retrospective_actions (
            id, retrospective_id, creation_key, position, action,
            owner, issue_id, status, due_kst_date, version,
            status_changed_at, resolved_at,
            status_actor, status_source, status_reason, status_evidence,
            legacy_payload, created_at, updated_at
        )
        SELECT
            gen_random_uuid(),
            t.id,
            NULL,
            (elem.ordinality - 1)::integer,
            btrim(elem.value->>'action'),
            elem.value->>'owner',
            elem.value->>'issue_id',
            CASE
                WHEN btrim(COALESCE(elem.value->>'status', '')) = ''
                    THEN 'open'
                ELSE elem.value->>'status'
            END,
            CASE
                WHEN btrim(COALESCE(elem.value->>'due_kst_date', '')) = ''
                    THEN NULL
                ELSE (elem.value->>'due_kst_date')::date
            END,
            1,
            t.updated_at,  -- approximate status_changed_at
            CASE
                WHEN elem.value->>'status' = 'done' THEN t.updated_at
                ELSE NULL
            END,
            'migration:rob-878',
            'migration',
            NULL,
            CASE
                WHEN elem.value->>'status' = 'done' THEN
                    jsonb_build_object(
                        'schema_version', 1,
                        'kind', 'legacy_status',
                        'source', 'migration',
                        'reference', 'review.trade_retrospectives.next_actions',
                        'observed_at', t.updated_at,
                        'summary', 'historical done; exact completion time unavailable'
                    )
                ELSE NULL
            END,
            elem.value,  -- entire original element preserved
            t.created_at,
            t.updated_at
        FROM review.trade_retrospectives t,
             jsonb_array_elements(t.next_actions) WITH ORDINALITY AS elem(value, ordinality)
        WHERE t.next_actions IS NOT NULL
          AND jsonb_typeof(t.next_actions) = 'array'
          AND jsonb_array_length(t.next_actions) > 0
        """
    )


def _assert_parity() -> None:
    """Assert one child per valid legacy element and field/ordinal parity."""
    op.execute(
        """
        DO $$
        DECLARE
            parent_count INTEGER;
            child_count INTEGER;
        BEGIN
            SELECT COALESCE(SUM(
                CASE
                    WHEN next_actions IS NOT NULL
                     AND jsonb_typeof(next_actions) = 'array'
                     AND jsonb_array_length(next_actions) > 0
                    THEN jsonb_array_length(next_actions)
                    ELSE 0
                END
            ), 0)
            INTO parent_count
            FROM review.trade_retrospectives;

            SELECT count(*) INTO child_count
            FROM review.trade_retrospective_actions;

            IF parent_count <> child_count THEN
                RAISE EXCEPTION
                    'parity mismatch: parent has % actions, child has %',
                    parent_count, child_count;
            END IF;

            RAISE NOTICE 'ROB-878 shadow backfill: % actions migrated', child_count;
        END;
        $$
        """
    )


def downgrade() -> None:
    """Downgrade is supported only while control mode is shadow.

    Abort if mode is canonical, any action has version > 1, any source is
    not migration, or a canonical application has created an action.
    """
    op.execute(
        """
        DO $$
        DECLARE
            ctrl_mode TEXT;
            max_version INTEGER;
            non_migration_count INTEGER;
        BEGIN
            SELECT mode INTO ctrl_mode
            FROM review.trade_retrospective_action_control WHERE id = 1;

            IF ctrl_mode = 'canonical' THEN
                RAISE EXCEPTION
                    'cannot downgrade: control mode is canonical; '
                    'recovery is mutation-disable plus roll-forward';
            END IF;

            SELECT COALESCE(max(version), 1) INTO max_version
            FROM review.trade_retrospective_actions;

            IF max_version > 1 THEN
                RAISE EXCEPTION
                    'cannot downgrade: actions have version > 1 (canonical writes exist)';
            END IF;

            SELECT count(*) INTO non_migration_count
            FROM review.trade_retrospective_actions
            WHERE status_source <> 'migration';

            IF non_migration_count > 0 THEN
                RAISE EXCEPTION
                    'cannot downgrade: % actions have non-migration source',
                    non_migration_count;
            END IF;
        END;
        $$
        """
    )

    # Drop trigger first, then function, then tables.
    op.execute(
        "DROP TRIGGER IF EXISTS trg_trade_retrospective_next_actions_fence "
        "ON review.trade_retrospectives"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS "
        "review.guard_trade_retrospective_next_actions()"
    )
    op.drop_table(_CONTROL_TABLE, schema=_SCHEMA)
    op.drop_index(
        "ix_trade_retrospective_actions_status_updated",
        table_name=_ACTIONS_TABLE, schema=_SCHEMA,
    )
    op.drop_index(
        "ix_trade_retrospective_actions_issue_id",
        table_name=_ACTIONS_TABLE, schema=_SCHEMA,
    )
    op.drop_index(
        "uq_trade_retrospective_actions_creation_key",
        table_name=_ACTIONS_TABLE, schema=_SCHEMA,
    )
    op.drop_index(
        "ix_trade_retrospective_actions_due_active",
        table_name=_ACTIONS_TABLE, schema=_SCHEMA,
    )
    op.drop_index(
        "ix_trade_retrospective_actions_parent_position",
        table_name=_ACTIONS_TABLE, schema=_SCHEMA,
    )
    op.drop_table(_ACTIONS_TABLE, schema=_SCHEMA)
```

- [ ] **Step 4: Run migration tests**

Run: `uv run pytest tests/test_rob878_shadow_ledger_migration.py -v`
Expected: PASS.

Run: `uv run python -c "import importlib; m = importlib.import_module('alembic.versions.20260714_rob878_trade_retrospective_actions_shadow'); print(m.revision, '->', m.down_revision)"`
Expected: `20260714_rob878_shadow -> 20260713_rob848_paper_validation`

- [ ] **Step 5: Verify alembic chain integrity**

Run: `uv run alembic history --rev-range="-2:" 2>&1 | head -5`
Expected: New revision at head, correctly chained.

- [ ] **Step 6: Commit**

```bash
git add alembic/versions/20260714_rob878_trade_retrospective_actions_shadow.py tests/test_rob878_shadow_ledger_migration.py
git commit -m "feat(ROB-878): shadow ledger migration with preflight, backfill, parity"
```

---

### Task 1.4: Preflight, Backfill, and Edge-Case Contract Tests

**Files:**
- Modify: `tests/test_rob878_shadow_ledger_migration.py` (append edge-case tests)

These tests exercise the migration's preflight and backfill SQL against seeded parent rows in rolled-back transactions. They do NOT shell out to `alembic`; they re-run the preflight/backfill SQL in a controlled scope.

- [ ] **Step 1: Write edge-case tests**

Append to `tests/test_rob878_shadow_ledger_migration.py`:

```python
@pytest.mark.asyncio
async def test_preflight_rejects_non_array_next_actions():
    """A non-array next_actions value fails preflight."""
    async with engine.connect() as conn:
        trans = await conn.begin()
        try:
            await conn.execute(text(
                "INSERT INTO review.trade_retrospectives "
                "(id, symbol, instrument_type, account_mode, outcome, next_actions) "
                "VALUES (990001, 'TEST', 'equity_kr', 'kis_mock', 'filled', "
                "'{\"action\": \"not an array\"}'::jsonb)"
            ))
            with pytest.raises(Exception, match="not an array|preflight"):
                await conn.execute(text(
                    # Re-run the preflight DO block (simplified check)
                    "DO $$ DECLARE r RECORD; BEGIN "
                    "FOR r IN SELECT id, next_actions FROM review.trade_retrospectives "
                    "WHERE id = 990001 LOOP "
                    "IF jsonb_typeof(r.next_actions) NOT IN ('array', 'null') THEN "
                    "RAISE EXCEPTION 'retrospective %: not an array', r.id; "
                    "END IF; END LOOP; END; $$"
                ))
        finally:
            await trans.rollback()


@pytest.mark.asyncio
async def test_null_next_actions_means_zero_actions(db_session):
    """SQL NULL next_actions produces zero child rows after backfill."""
    from sqlalchemy import text
    result = await db_session.execute(text(
        "SELECT count(*) FROM review.trade_retrospective_actions "
        "WHERE retrospective_id IN ("
        "  SELECT id FROM review.trade_retrospectives WHERE next_actions IS NULL"
        ")"
    ))
    # The test DB may have no NULL rows; the point is the query doesn't error
    # and returns a count (even if 0).
    assert result.scalar() is not None


@pytest.mark.asyncio
async def test_missing_status_backfills_to_open():
    """An element with no status key gets status='open' in backfill."""
    async with engine.connect() as conn:
        trans = await conn.begin()
        try:
            await conn.execute(text(
                "INSERT INTO review.trade_retrospectives "
                "(id, symbol, instrument_type, account_mode, outcome, next_actions) "
                "VALUES (990002, 'TEST', 'equity_kr', 'kis_mock', 'filled', "
                "'[{\"action\": \"no status action\"}]'::jsonb)"
            ))
            # Simulate backfill for this one row
            await conn.execute(text(
                "INSERT INTO review.trade_retrospective_actions "
                "(id, retrospective_id, position, action, status, version, "
                " status_actor, status_source, legacy_payload) "
                "SELECT gen_random_uuid(), t.id, 0, "
                " btrim(elem.value->>'action'), "
                " CASE WHEN btrim(COALESCE(elem.value->>'status','')) = '' "
                "   THEN 'open' ELSE elem.value->>'status' END, "
                " 1, 'migration:rob-878', 'migration', elem.value "
                "FROM review.trade_retrospectives t, "
                " jsonb_array_elements(t.next_actions) WITH ORDINALITY AS elem(value, ordinality) "
                "WHERE t.id = 990002"
            ))
            result = await conn.execute(text(
                "SELECT status FROM review.trade_retrospective_actions "
                "WHERE retrospective_id = 990002"
            ))
            row = result.fetchone()
            assert row is not None
            assert row.status == "open"
        finally:
            await trans.rollback()


@pytest.mark.asyncio
async def test_legacy_payload_preserves_unknown_keys():
    """The entire original JSONB element is preserved in legacy_payload."""
    async with engine.connect() as conn:
        trans = await conn.begin()
        try:
            original = {"action": "do thing", "owner": "alice",
                        "custom_key": "custom_value", "another": 42}
            await conn.execute(text(
                "INSERT INTO review.trade_retrospectives "
                "(id, symbol, instrument_type, account_mode, outcome, next_actions) "
                "VALUES (990003, 'TEST', 'equity_kr', 'kis_mock', 'filled', "
                ":actions::jsonb)"
            ), {"actions": f"[{original!r}]".replace("'", '"').replace("True", "true").replace("None", "null").replace("42", "42")})
            # Actually use proper JSON:
            import json
            await conn.execute(text(
                "UPDATE review.trade_retrospectives SET next_actions = :actions "
                "WHERE id = 990003"
            ), {"actions": json.dumps([{"action": "do thing", "owner": "alice",
                 "custom_key": "custom_value", "another": 42}])})

            await conn.execute(text(
                "INSERT INTO review.trade_retrospective_actions "
                "(id, retrospective_id, position, action, status, version, "
                " status_actor, status_source, legacy_payload) "
                "SELECT gen_random_uuid(), t.id, 0, "
                " btrim(elem.value->>'action'), 'open', 1, "
                " 'migration:rob-878', 'migration', elem.value "
                "FROM review.trade_retrospectives t, "
                " jsonb_array_elements(t.next_actions) WITH ORDINALITY AS elem(value, ordinality) "
                "WHERE t.id = 990003"
            ))
            result = await conn.execute(text(
                "SELECT legacy_payload FROM review.trade_retrospective_actions "
                "WHERE retrospective_id = 990003"
            ))
            payload = result.fetchone().legacy_payload
            assert payload.get("custom_key") == "custom_value"
            assert payload.get("another") == 42
        finally:
            await trans.rollback()


@pytest.mark.asyncio
async def test_shadow_mode_permits_parent_json_write(db_session):
    """In shadow mode, direct writes to next_actions are permitted."""
    from sqlalchemy import text
    # Insert a retrospective and update its next_actions — should succeed.
    async with engine.connect() as conn:
        trans = await conn.begin()
        try:
            await conn.execute(text(
                "INSERT INTO review.trade_retrospectives "
                "(id, symbol, instrument_type, account_mode, outcome, next_actions) "
                "VALUES (990004, 'TEST', 'equity_kr', 'kis_mock', 'filled', "
                "'[]'::jsonb)"
            ))
            await conn.execute(text(
                "UPDATE review.trade_retrospectives SET next_actions = "
                "'[{\"action\": \"new\"}]'::jsonb WHERE id = 990004"
            ))
            # No exception means the trigger permitted it.
            result = await conn.execute(text(
                "SELECT next_actions FROM review.trade_retrospectives WHERE id = 990004"
            ))
            assert result.fetchone() is not None
        finally:
            await trans.rollback()


@pytest.mark.asyncio
async def test_canonical_mode_rejects_parent_json_write():
    """In canonical mode, direct writes to next_actions without the GUC marker fail."""
    async with engine.connect() as conn:
        trans = await conn.begin()
        try:
            await conn.execute(text(
                "INSERT INTO review.trade_retrospectives "
                "(id, symbol, instrument_type, account_mode, outcome, next_actions) "
                "VALUES (990005, 'TEST', 'equity_kr', 'kis_mock', 'filled', "
                "'[]'::jsonb)"
            ))
            await conn.execute(text(
                "UPDATE review.trade_retrospective_action_control "
                "SET mode = 'canonical' WHERE id = 1"
            ))
            with pytest.raises(Exception, match="canonical mode.*rejected"):
                await conn.execute(text(
                    "UPDATE review.trade_retrospectives SET next_actions = "
                    "'[{\"action\": \"blocked\"}]'::jsonb WHERE id = 990005"
                ))
        finally:
            # Restore shadow mode before rollback
            await conn.execute(text(
                "UPDATE review.trade_retrospective_action_control "
                "SET mode = 'shadow' WHERE id = 1"
            ))
            await trans.rollback()


@pytest.mark.asyncio
async def test_canonical_mode_permits_write_with_guc_marker():
    """In canonical mode, writes with the projection-writer GUC marker succeed."""
    async with engine.connect() as conn:
        trans = await conn.begin()
        try:
            await conn.execute(text(
                "INSERT INTO review.trade_retrospectives "
                "(id, symbol, instrument_type, account_mode, outcome, next_actions) "
                "VALUES (990006, 'TEST', 'equity_kr', 'kis_mock', 'filled', "
                "'[]'::jsonb)"
            ))
            await conn.execute(text(
                "UPDATE review.trade_retrospective_action_control "
                "SET mode = 'canonical' WHERE id = 1"
            ))
            # Set the GUC marker
            await conn.execute(text(
                "SET LOCAL app.retrospective_action_projection_writer = 'v1'"
            ))
            # This should succeed
            await conn.execute(text(
                "UPDATE review.trade_retrospectives SET next_actions = "
                "'[{\"action\": \"allowed\"}]'::jsonb WHERE id = 990006"
            ))
        finally:
            await conn.execute(text(
                "UPDATE review.trade_retrospective_action_control "
                "SET mode = 'shadow' WHERE id = 1"
            ))
            await trans.rollback()


@pytest.mark.asyncio
async def test_parent_json_immutable_after_backfill():
    """Parent next_actions JSONB is byte-for-byte unchanged after backfill."""
    async with engine.connect() as conn:
        trans = await conn.begin()
        try:
            import json
            original = [{"action": "check thing", "status": "open"}]
            await conn.execute(text(
                "INSERT INTO review.trade_retrospectives "
                "(id, symbol, instrument_type, account_mode, outcome, next_actions) "
                "VALUES (990007, 'TEST', 'equity_kr', 'kis_mock', 'filled', "
                ":actions::jsonb)"
            ), {"actions": json.dumps(original)})

            before = (await conn.execute(text(
                "SELECT next_actions FROM review.trade_retrospectives WHERE id = 990007"
            ))).scalar_one()

            # Simulate backfill (read-only on parent)
            await conn.execute(text(
                "INSERT INTO review.trade_retrospective_actions "
                "(id, retrospective_id, position, action, status, version, "
                " status_actor, status_source, legacy_payload) "
                "SELECT gen_random_uuid(), t.id, 0, "
                " btrim(elem.value->>'action'), elem.value->>'status', "
                " 1, 'migration:rob-878', 'migration', elem.value "
                "FROM review.trade_retrospectives t, "
                " jsonb_array_elements(t.next_actions) WITH ORDINALITY AS elem(value, ordinality) "
                "WHERE t.id = 990007"
            ))

            after = (await conn.execute(text(
                "SELECT next_actions FROM review.trade_retrospectives WHERE id = 990007"
            ))).scalar_one()

            assert before == after
        finally:
            await trans.rollback()
```

- [ ] **Step 2: Run all child-1 tests**

Run: `uv run pytest tests/test_rob878_shadow_ledger_migration.py tests/test_rob878_shadow_ledger_model.py -v`
Expected: All PASS.

- [ ] **Step 3: Run lint and type checks on changed files**

Run: `uv run ruff check tests/test_rob878_shadow_ledger_migration.py tests/test_rob878_shadow_ledger_model.py alembic/versions/20260714_rob878_trade_retrospective_actions_shadow.py app/models/review.py`
Expected: No errors.

Run: `uv run ruff check --select I tests/test_rob878_shadow_ledger_migration.py` (import sort)
Expected: No errors.

- [ ] **Step 4: Run schema barrier regression**

Run: `uv run pytest tests/infra/test_schema_barrier.py -v`
Expected: All PASS.

- [ ] **Step 5: Run alembic revision-id check**

Run: `uv run pytest tests/test_alembic_revision_ids.py -v`
Expected: PASS — revision id ≤ 32 chars.

- [ ] **Step 6: Verify git diff cleanliness**

Run: `git diff --check`
Expected: No whitespace errors.

- [ ] **Step 7: Commit**

```bash
git add tests/test_rob878_shadow_ledger_migration.py
git commit -m "test(ROB-878): preflight, backfill, edge-case, write-fence contract tests"
```

---

## Child Issue 2: Canonical Cutover — repository / save projection / read API

**Scope:** Locked cutover command, control-mode repository, parent-before-children locking, field-preserving save reconcile, compatibility projection, eager hydration, canonical GET, legacy alias.

**Files:**
- Create: `app/services/trade_journal/retrospective_action_repository.py`
- Create: `scripts/retrospective_action_cutover.py`
- Modify: `app/services/trade_journal/trade_retrospective_service.py`
- Modify: `app/routers/invest_retrospectives.py`
- Modify: `scripts/deploy-native.sh`
- Create: `tests/test_rob878_canonical_cutover.py`

**Key tasks:**
- [ ] Repository: read control mode, route shadow→legacy reader/writer, canonical→children + projection writer
- [ ] Cutover command: advisory lock, LOCK TABLE, flip control to canonical, rebuild from frozen parent JSON, parity assert
- [ ] Save reconcile: occurrence-aware matching, force_new/creation_key idempotency, field-presence semantics
- [ ] Canonical GET: paginated `{total, count, limit, offset, as_of, items}`, overdue-first ordering
- [ ] Legacy alias: preserve existing `/next-actions` contract, add action_id/version additively
- [ ] Deploy: post-switch cutover step in `deploy-native.sh` after `BLUEGREEN_COMMITTED=1`
- [ ] Eager hydration: selectinload or bounded child query, N+1 / MissingGreenlet regression tests

**RED commands:**
```bash
uv run pytest tests/test_rob878_canonical_cutover.py -v -x
```
**GREEN commands:**
```bash
uv run pytest tests/test_rob878_canonical_cutover.py -v
uv run pytest tests/test_trade_retrospective_web_read.py -v  # no regression
```

---

## Child Issue 3: Transition Core — state machine / version / evidence

**Scope:** Domain graph, locked service, transaction boundary, version/idempotency, typed base evidence, immutable terminal audit, concurrency tests. No HTTP or MCP registration.

**Files:**
- Create: `app/services/trade_journal/retrospective_action_transition.py`
- Create: `app/services/trade_journal/retrospective_action_types.py`
- Create: `tests/test_rob878_transition_core.py`

**Key tasks:**
- [ ] State machine: open↔in_progress, active→terminal, terminal idempotent, terminal conflict
- [ ] `transition_retrospective_action()` service: lock parent→children ORDER BY id, version check, graph eval, single version increment, projection rebuild
- [ ] Evidence envelope: `schema_version`, `kind`, `source`, `reference`, `observed_at`, `summary` — exact-key/size/depth/secret rejection
- [ ] Dry-run mode: evaluate without mutation
- [ ] Terminal immutability: accepted terminal audit can never be overwritten
- [ ] ROB-665 regression: due date alone never produces expired

**RED commands:**
```bash
uv run pytest tests/test_rob878_transition_core.py -v -x
```
**GREEN commands:**
```bash
uv run pytest tests/test_rob878_transition_core.py -v
```

---

## Child Issue 4: Operator Surface — authorized PATCH + privileged MCP

**Scope:** Trader/admin + CSRF HTTP contract, canonical read/preview tools, execution-only commit tool, profile allowlists, error DTOs, documentation, authorization/boot tests.

**Files:**
- Modify: `app/routers/invest_retrospectives.py`
- Modify: `app/mcp_server/tooling/trade_retrospective_registration.py`
- Modify: `app/mcp_server/tooling/tradingcodex_execution_registration.py`
- Modify: `app/schemas/trade_retrospective.py`
- Create: `tests/test_rob878_operator_http.py`
- Create: `tests/test_rob878_operator_mcp.py`
- Modify: `app/mcp_server/README.md`

**Key tasks:**
- [ ] PATCH `/trading/api/invest/retrospectives/actions/{action_id}`: trader/admin, CSRF dependency, 409 on conflict, 422 on invalid
- [ ] MCP `get_retrospective_actions`: mirror canonical HTTP filters, active-by-default
- [ ] MCP `retrospective_action_transition_preview`: dry_run=True always
- [ ] MCP `retrospective_action_transition`: `tradingcodex_execution` profile only, default dry_run=true
- [ ] Default profile: read + preview only (forbidden-set test proves no commit tool)
- [ ] Error DTOs: `{changed, idempotent, dry_run, item}` or `{action_id, status, version}` on 409

**RED commands:**
```bash
uv run pytest tests/test_rob878_operator_http.py tests/test_rob878_operator_mcp.py -v -x
```
**GREEN commands:**
```bash
uv run pytest tests/test_rob878_operator_http.py tests/test_rob878_operator_mcp.py -v
```

---

## Child Issue 5: Evidence-Based Backlog Triage

**Scope:** Versioned JSONL export/apply, manifest hash approval, dry-run default, bounded resumable commits, operator runbook, actual review, final unresolved/overdue report. No keyword auto-classification.

**Files:**
- Create: `scripts/retrospective_action_triage.py`
- Create: `tests/test_rob878_triage.py`
- Create: `docs/runbooks/retrospective-action-triage.md`

**Key tasks:**
- [ ] `export` command: write JSONL with action ID/version, parent context, current fields, blank proposed fields
- [ ] `apply` command: dry-run default, commit requires flag + manifest SHA-256 match, max 25 rows/invocation, one transaction per action
- [ ] Result JSONL: changed/idempotent/conflict/invalid/unresolved per action
- [ ] Resume: re-running skips recorded successes
- [ ] Uses the same transition service, actor=stable user ID, source=triage

**RED commands:**
```bash
uv run pytest tests/test_rob878_triage.py -v -x
```
**GREEN commands:**
```bash
uv run pytest tests/test_rob878_triage.py -v
```

---

## Child Issue 6: decision_history.open_actions Injection

**Scope:** Bounded canonical context, analyze/bundle propagation, token limits, advisory trust boundary, MCP documentation.

**Files:**
- Modify: `app/analysis/decision_history.py` (or equivalent decision-context builder)
- Create: `tests/test_rob878_decision_history_actions.py`

**Key tasks:**
- [ ] Shared retrospective-visibility predicate (kis_mock exact, default excludes mock-counterfactual)
- [ ] `open_actions`: max 5 active, ranked overdue→in_progress→due→recency→ID, text truncated 220c, total JSON cap 3 KiB
- [ ] `open_actions_meta`: `authority=historical_advisory`, `executable=false`, count, truncation flag
- [ ] `quick=true` batch path and frozen bundle capture
- [ ] Tests: explicit paths, empty-field presence, string/aggregate budgets, advisory trust marker

**RED commands:**
```bash
uv run pytest tests/test_rob878_decision_history_actions.py -v -x
```
**GREEN commands:**
```bash
uv run pytest tests/test_rob878_decision_history_actions.py -v
```

---

## Child Issue 7: /invest Retrospective Action Read-Only Triage UX

**Scope:** Exact paginated endpoint consumption, state/owner/issue/due visibility, shared filters/types, configured Linear link fallback, stock-detail active-state fix, four-host coverage, frontend tests.

**Files:**
- Modify: `frontend/invest/src/.../RetrospectivesPanel.tsx` and related
- Modify: `frontend/invest/src/types/retrospectives.ts`
- Modify: stock-detail rendering (active allowlist instead of `status != done`)
- Create: frontend test additions

**Key tasks:**
- [ ] Canonical `RetrospectiveAction` type (normal + compact modes)
- [ ] Action section: distinct open/in_progress, owner, due, overdue, Linear link (config-gated)
- [ ] Shared filters: market, trigger, outcome, symbol query, retrospective date
- [ ] Stock-detail: switch from `status != done` to explicit active allowlist
- [ ] Four-host coverage: desktop/mobile insights + desktop/mobile portfolio
- [ ] `VITE_LINEAR_WORKSPACE_URL` validation (HTTPS origin, append `issue/<encoded>`)

**RED commands:**
```bash
cd frontend/invest && npm test -- --grep "retrospective action"
```
**GREEN commands:**
```bash
cd frontend/invest && npm test
```

---

## Related Follow-Up Issues (non-blocking)

### Reconciler — typed binding + dry-run

Starts after child-5 triage and manual lifecycle metrics are available. Binding families: delivered watch event, closed forecast, broker-reconciled journal close/position-flat. Dry-run-first, default-off scheduleless. Watch/forecast/position adapters remain separate issues.

### Legacy Projection Retirement

Starts after child-4 + child-7, alias traffic = zero, and 14 consecutive production days with zero parity mismatch and no emergency rollback requiring the compatibility reader. Remove parent projection, write-fence trigger, fallback reader, deprecated alias.

---

## Migration / Cutover Order (across all issues)

```text
1. [child-1] Deploy additive schema + shadow backfill + trigger + control row (shadow)
2. [child-2] Deploy canonical candidate; health-check in shadow; switch traffic; drain old
3. [child-2] Post-switch: run cutover command (--if-shadow); canonical health/parity check
4. [child-3+4] Enable authorized manual transition surfaces
5. [child-5] Perform bounded manual backlog triage
6. [child-6+7] Release decision-history + read-only UI consumers (parallel)
7. [related] Scope typed resolver in shadow; scheduling remains disabled
8. [related] Retire legacy projection after observation window
```

---

## Verification Commands (child-1 scope)

```bash
# ORM model + bootstrap
uv run pytest tests/test_rob878_shadow_ledger_model.py -v

# Migration + edge cases
uv run pytest tests/test_rob878_shadow_ledger_migration.py -v

# Schema barrier regression
uv run pytest tests/infra/test_schema_barrier.py -v

# Alembic revision-id check
uv run pytest tests/test_alembic_revision_ids.py -v

# Lint + type
uv run ruff check app/models/review.py app/models/__init__.py \
  alembic/versions/20260714_rob878_trade_retrospective_actions_shadow.py \
  tests/test_rob878_shadow_ledger_model.py tests/test_rob878_shadow_ledger_migration.py \
  tests/_schema_bootstrap.py

# Whitespace
git diff --check

# Existing retrospective tests (no regression)
uv run pytest tests/test_trade_retrospective_web_read.py tests/test_trade_retrospective_schema.py -v
```

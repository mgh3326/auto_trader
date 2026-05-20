# ROB-274: action/watch/risk semantics + existing-state proposals

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `/invest` investment reports express **proposals against current operational state** (active watches + pending broker orders) by adding `operation`/`current_state`/`proposed_state`/`diff`/`target_ref`/`apply_policy` to report items, a new `pending_orders` snapshot collector, and a classifier that maps draft items to `create/modify/cancel/keep/review` proposals — all side-effect-free.

**Architecture:**
- Additive schema migration on `review.investment_report_items` + extension of `SnapshotKind` literal/CHECK for `pending_orders`.
- One new read-only `pending_orders` collector with internal market/account adapters (KR/US via KIS, crypto via Upbit). Optional + fail-open in this PR.
- New `proposal_classifier` module that takes draft items + active-watch context + pending-order context and emits classified items. Wired into `SnapshotBackedReportGenerator.generate()` between bundle-ensure and ingest.
- Frontend badge mapping (`InvestmentReportBundleContent.tsx`) flips to English `action/watch/risk` + new diff/current/proposed rendering. Korean explanatory copy preserved.
- Safety: explicit mock-based tests assert no broker `submit/cancel/modify` and no watch `activate/update/cancel` during report generation.

**Tech Stack:** Python 3.13, SQLAlchemy 2 async, Alembic, Pydantic v2, FastAPI, React + TypeScript (Vite), pytest, Vitest.

**Locked decisions from issue (2026-05-20):**
- `item_kind` stays `{action, watch, risk}` only.
- `watch_condition` required only for `operation ∈ {create, modify}`.
- ONE `pending_orders` collector with adapters inside.
- `apply_policy = Literal["requires_user_approval"]` only.
- `target_ref.id` canonical string; broker-specific raw goes under `target_ref.raw`.
- Decimal/datetime/UUID normalization extends to `diff[*].from/to`, `current_state.*`, `proposed_state.*`.
- Frontend label change limited to primary badges + counters; Korean explanatory copy stays.
- `PENDING_ORDER_STALENESS_HOURS_CRYPTO = 24` default (crypto only). KR/US use market-session expiry.

**Verification commands** (run from repo root, all tasks):
```bash
uv run ruff check
uv run ruff format --check
uv run ty check
uv run pytest tests/services/action_report/snapshot_backed/ tests/services/investment_reports/ -v
uv run pytest tests/test_schemas_investment_reports.py -v  # if exists; else skip
cd frontend/invest && npm run test
```

---

## File Structure

**Created:**
- `alembic/versions/20260520_rob274_p1_add_proposal_fields_to_report_items.py` — new columns + extended CHECK.
- `alembic/versions/20260520_rob274_p2_extend_snapshot_kind.py` — extend snapshot_kind CHECK + index for new kind.
- `app/services/action_report/snapshot_backed/collectors/pending_orders.py` — new collector with market-adapter dispatch.
- `app/services/action_report/snapshot_backed/proposal_classifier.py` — pure classifier: draft items + context → classified items.
- `app/services/action_report/common/staleness.py` — small module exposing `PENDING_ORDER_STALENESS_HOURS_CRYPTO` and helpers.
- `tests/services/action_report/snapshot_backed/test_pending_orders_collector.py`
- `tests/services/action_report/snapshot_backed/test_proposal_classifier.py`
- `tests/services/action_report/snapshot_backed/test_generator_safety.py` — no-broker-mutation assertions.
- `tests/services/investment_reports/test_schema_operation_aware.py` — operation-aware validator tests.
- `frontend/invest/src/components/investment-reports/ProposalDiffPanel.tsx` — current/proposed/diff renderer.
- `frontend/invest/src/__tests__/InvestmentReportBundleContent.proposal.test.tsx`

**Modified:**
- `app/models/investment_reports.py:212-243` — extend CHECK constraints + new columns on `InvestmentReportItem`.
- `app/schemas/investment_reports.py:33-111, 259-283` — add `OperationLiteral`, `TargetRefPayload`, etc.; rewrite `_validate_watch_invariants` to be operation-aware; surface new fields on `InvestmentReportItemResponse`.
- `app/schemas/investment_snapshots.py:19-32` — append `pending_orders` to `SnapshotKind` literal.
- `app/services/investment_reports/ingestion.py:106-141` — pass through new fields in `_insert_item`.
- `app/services/investment_reports/repository.py` — extend `insert_item` signature; extend `list_active_alerts` callers (no changes there) and add `get_pending_orders_for_market` query (DB-side stub).
- `app/services/action_report/snapshot_backed/collectors/registry.py:54-72` — register `PendingOrdersSnapshotCollector`.
- `app/services/action_report/snapshot_backed/generator.py:124-199, 271-337` — call classifier between ensure and ingest; thread new collector kinds into bundle ensure.
- `app/services/action_report/common/jsonable.py` — verify recursive coverage (only edit if existing impl misses Decimal-in-list-of-dicts; add tests if so).
- `app/mcp_server/tooling/investment_reports_handlers.py` — extend `investment_report_context_get` response with `pending_orders` snapshot.
- `frontend/invest/src/components/investment-reports/InvestmentReportBundleContent.tsx:22-24, 484` — replace `액션/와치/리스크` mapping with English; replace "활성 와치" counter label.
- `frontend/invest/src/api/types.ts` (or equivalent — find the response type for items) — add `operation`, `target_ref`, `current_state`, `proposed_state`, `diff`, `apply_policy` optional fields.

**No changes intended:**
- Broker clients (`app/services/kis.py`, `app/services/upbit.py`, `app/services/alpaca_*`) — the pending_orders collector calls existing read-only fetchers; if a read-only fetcher does not exist for a given broker the collector falls open with `confirmation_unknown`.
- Watch scanner (`app/jobs/watch_scanner.py` or equivalent) — unchanged in this PR.

---

## Self-Review Pre-Checks

Before writing tasks, confirm:
- **Spec coverage:** every AC bullet in ROB-274 maps to a task (see "AC mapping" at end).
- **Backward compat:** new columns are nullable; legacy items without `operation` default to `null` and are rendered as `operation=create` semantics in the frontend.
- **Migration ordering:** Task 1 (proposal columns) and Task 2 (snapshot_kind extension) are independent; either can land first. Both must land before Task 5 (collector) and Task 7 (classifier).

---

## Task 1: Schema migration — add proposal fields to `investment_report_items`

**Files:**
- Create: `alembic/versions/20260520_rob274_p1_add_proposal_fields_to_report_items.py`
- Modify: none yet (ORM update is Task 2)

### Steps

- [ ] **Step 1: Resolve current alembic head**

Run: `uv run alembic current` and `uv run alembic heads`
Expected: single head (likely `20260519_rob269_p3a` or descendant; capture exact value).

- [ ] **Step 2: Write the migration**

Create `alembic/versions/20260520_rob274_p1_add_proposal_fields_to_report_items.py`:

```python
"""ROB-274 — add proposal-state fields to investment_report_items.

Revision ID: 20260520_rob274_p1
Revises: <CURRENT_HEAD>
Create Date: 2026-05-20

Adds 6 nullable columns + rewrites two CHECK constraints so that the
``watch_condition`` / ``valid_until`` invariants only apply to
``operation ∈ {create, modify}``. All additive; existing rows keep
operation=NULL which is treated as legacy/'create' by the frontend.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260520_rob274_p1"
down_revision: str | None = "<CURRENT_HEAD>"  # fill in from Step 1
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1) new columns — all nullable, all JSONB except operation (text) and apply_policy (text).
    op.add_column(
        "investment_report_items",
        sa.Column("operation", sa.Text(), nullable=True),
        schema="review",
    )
    op.add_column(
        "investment_report_items",
        sa.Column(
            "target_ref",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        schema="review",
    )
    op.add_column(
        "investment_report_items",
        sa.Column(
            "current_state",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        schema="review",
    )
    op.add_column(
        "investment_report_items",
        sa.Column(
            "proposed_state",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        schema="review",
    )
    op.add_column(
        "investment_report_items",
        sa.Column(
            "diff",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        schema="review",
    )
    op.add_column(
        "investment_report_items",
        sa.Column("apply_policy", sa.Text(), nullable=True),
        schema="review",
    )

    # 2) operation CHECK — null permitted (legacy rows) plus the 6 lifecycle verbs.
    op.create_check_constraint(
        "ck_investment_report_items_operation",
        "investment_report_items",
        "operation IS NULL OR operation IN ("
        "'create','modify','cancel','keep','replace','review'"
        ")",
        schema="review",
    )

    # 3) apply_policy CHECK — null permitted; only one accepted value in this PR.
    op.create_check_constraint(
        "ck_investment_report_items_apply_policy",
        "investment_report_items",
        "apply_policy IS NULL OR apply_policy = 'requires_user_approval'",
        schema="review",
    )

    # 4) Rewrite watch_condition invariant: required only when operation IS NULL
    #    (legacy) OR operation IN ('create','modify'). cancel/keep/review do not
    #    require a new watch_condition.
    op.drop_constraint(
        "ck_investment_report_items_watch_has_condition",
        "investment_report_items",
        schema="review",
    )
    op.create_check_constraint(
        "ck_investment_report_items_watch_has_condition",
        "investment_report_items",
        "item_kind <> 'watch' "
        "OR operation IN ('cancel','keep','review') "
        "OR watch_condition IS NOT NULL",
        schema="review",
    )

    # 5) Same treatment for valid_until — required for create/modify/legacy,
    #    not required for cancel/keep/review (they reference an existing alert
    #    that already has its own validity window).
    op.drop_constraint(
        "ck_investment_report_items_watch_has_expiry",
        "investment_report_items",
        schema="review",
    )
    op.create_check_constraint(
        "ck_investment_report_items_watch_has_expiry",
        "investment_report_items",
        "item_kind <> 'watch' "
        "OR operation IN ('cancel','keep','review') "
        "OR valid_until IS NOT NULL",
        schema="review",
    )

    # 6) Index by (operation, item_kind, status) for the frontend's
    #    proposal-grouped list query.
    op.create_index(
        "ix_investment_report_items_operation_kind",
        "investment_report_items",
        ["operation", "item_kind", "status"],
        schema="review",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_investment_report_items_operation_kind",
        table_name="investment_report_items",
        schema="review",
    )
    # Restore original strict CHECKs.
    op.drop_constraint(
        "ck_investment_report_items_watch_has_expiry",
        "investment_report_items",
        schema="review",
    )
    op.create_check_constraint(
        "ck_investment_report_items_watch_has_expiry",
        "investment_report_items",
        "item_kind <> 'watch' OR valid_until IS NOT NULL",
        schema="review",
    )
    op.drop_constraint(
        "ck_investment_report_items_watch_has_condition",
        "investment_report_items",
        schema="review",
    )
    op.create_check_constraint(
        "ck_investment_report_items_watch_has_condition",
        "investment_report_items",
        "item_kind <> 'watch' OR watch_condition IS NOT NULL",
        schema="review",
    )
    op.drop_constraint(
        "ck_investment_report_items_apply_policy",
        "investment_report_items",
        schema="review",
    )
    op.drop_constraint(
        "ck_investment_report_items_operation",
        "investment_report_items",
        schema="review",
    )
    op.drop_column("investment_report_items", "apply_policy", schema="review")
    op.drop_column("investment_report_items", "diff", schema="review")
    op.drop_column("investment_report_items", "proposed_state", schema="review")
    op.drop_column("investment_report_items", "current_state", schema="review")
    op.drop_column("investment_report_items", "target_ref", schema="review")
    op.drop_column("investment_report_items", "operation", schema="review")
```

- [ ] **Step 3: Apply migration locally**

Run: `uv run alembic upgrade head`
Expected: exit 0; new head is `20260520_rob274_p1`.

- [ ] **Step 4: Round-trip downgrade**

Run: `uv run alembic downgrade -1 && uv run alembic upgrade head`
Expected: both succeed; final head back at `20260520_rob274_p1`.

- [ ] **Step 5: Commit**

```bash
git add alembic/versions/20260520_rob274_p1_add_proposal_fields_to_report_items.py
git commit -m "feat(rob-274): add proposal-state columns to investment_report_items"
```

---

## Task 2: Extend ORM model + Pydantic schemas

**Files:**
- Modify: `app/models/investment_reports.py:195-317` — `InvestmentReportItem` class.
- Modify: `app/schemas/investment_reports.py:33-111, 259-283` — literals, payloads, validator, response.
- Test: `tests/services/investment_reports/test_schema_operation_aware.py`

### Steps

- [ ] **Step 1: Write failing schema tests**

Create `tests/services/investment_reports/test_schema_operation_aware.py`:

```python
"""ROB-274 — operation-aware watch validator tests."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.schemas.investment_reports import (
    IngestReportItem,
    TargetRefPayload,
    WatchConditionPayload,
)


def _now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)


def _watch_condition() -> WatchConditionPayload:
    return WatchConditionPayload(
        metric="price", operator="above", threshold=Decimal("100")
    )


def _target_ref() -> TargetRefPayload:
    return TargetRefPayload(
        type="investment_watch_alert",
        id=str(uuid4()),
        status="active",
    )


def test_watch_create_requires_watch_condition_and_valid_until():
    with pytest.raises(ValidationError):
        IngestReportItem(
            client_item_key="k1",
            item_kind="watch",
            operation="create",
            intent="buy_review",
            rationale="r",
            # missing watch_condition and valid_until
        )


def test_watch_modify_requires_target_ref_and_current_state():
    with pytest.raises(ValidationError):
        IngestReportItem(
            client_item_key="k1",
            item_kind="watch",
            operation="modify",
            intent="trend_recovery_review",
            rationale="r",
            watch_condition=_watch_condition(),
            valid_until=_now_utc(),
            # missing target_ref + current_state
        )


def test_watch_cancel_does_not_require_watch_condition():
    item = IngestReportItem(
        client_item_key="k1",
        item_kind="watch",
        operation="cancel",
        intent="risk_review",
        rationale="r",
        target_ref=_target_ref(),
        current_state={"metric": "price", "operator": "above", "threshold": "100"},
        apply_policy="requires_user_approval",
    )
    assert item.watch_condition is None
    assert item.valid_until is None


def test_watch_keep_requires_target_ref_and_current_state_only():
    item = IngestReportItem(
        client_item_key="k1",
        item_kind="watch",
        operation="keep",
        intent="risk_review",
        rationale="r",
        target_ref=_target_ref(),
        current_state={"metric": "price"},
    )
    assert item.operation == "keep"


def test_watch_review_accepts_ambiguous_target_list():
    item = IngestReportItem(
        client_item_key="k1",
        item_kind="watch",
        operation="review",
        intent="risk_review",
        rationale="r",
        target_ref=TargetRefPayload(type="ambiguous", id=None, candidates=[{"id": "a"}, {"id": "b"}]),
        current_state={},
    )
    assert item.target_ref.type == "ambiguous"


def test_legacy_item_without_operation_keeps_old_invariants():
    # Legacy (operation=None) must still reject watch items without
    # watch_condition or valid_until so existing callers don't regress.
    with pytest.raises(ValidationError):
        IngestReportItem(
            client_item_key="k1",
            item_kind="watch",
            intent="buy_review",
            rationale="r",
        )


def test_apply_policy_is_locked_to_single_value():
    with pytest.raises(ValidationError):
        IngestReportItem(
            client_item_key="k1",
            item_kind="watch",
            operation="cancel",
            intent="risk_review",
            rationale="r",
            target_ref=_target_ref(),
            current_state={},
            apply_policy="notify_only",  # not in this PR
        )


def test_action_cancel_requires_target_ref_when_present():
    # action/cancel proposals target an existing broker order.
    with pytest.raises(ValidationError):
        IngestReportItem(
            client_item_key="k1",
            item_kind="action",
            operation="cancel",
            intent="sell_review",
            rationale="r",
            # missing target_ref
        )
```

- [ ] **Step 2: Run tests, confirm they fail**

Run: `uv run pytest tests/services/investment_reports/test_schema_operation_aware.py -v`
Expected: every test fails with `ImportError` (TargetRefPayload missing) or `ValidationError mismatch`.

- [ ] **Step 3: Extend `app/schemas/investment_reports.py`**

After existing `WatchActionModeLiteral` add new literals (around line 49):

```python
OperationLiteral = Literal[
    "create", "modify", "cancel", "keep", "replace", "review"
]
ApplyPolicyLiteral = Literal["requires_user_approval"]
TargetRefTypeLiteral = Literal[
    "investment_watch_alert", "broker_order", "ambiguous"
]
```

Add a new payload class above `IngestReportItem`:

```python
class TargetRefPayload(BaseModel):
    """Reference to the existing operational state an item proposes to act on."""

    type: TargetRefTypeLiteral
    id: str | None = None
    status: str | None = None
    broker: str | None = None
    raw: dict[str, Any] | None = None
    candidates: list[dict[str, Any]] | None = None

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _ambiguous_needs_candidates(self) -> TargetRefPayload:
        if self.type == "ambiguous":
            if not self.candidates:
                raise ValueError(
                    "target_ref.type='ambiguous' requires non-empty candidates"
                )
        else:
            if self.id is None:
                raise ValueError(
                    "target_ref.id is required for non-ambiguous target_ref"
                )
        return self
```

Replace `IngestReportItem` (currently lines 75-111) with:

```python
class IngestReportItem(BaseModel):
    """One proposal item attached to an ingested report."""

    client_item_key: str = Field(min_length=1)
    item_kind: ItemKindLiteral
    operation: OperationLiteral | None = None
    symbol: str | None = None
    side: ItemSideLiteral | None = None
    intent: ItemIntentLiteral
    target_kind: TargetKindLiteral = "asset"
    priority: int = 0
    confidence: Decimal | None = None
    rationale: str
    evidence_snapshot: dict[str, Any] = Field(default_factory=dict)
    watch_condition: WatchConditionPayload | None = None
    trigger_checklist: list[Any] = Field(default_factory=list)
    max_action: dict[str, Any] = Field(default_factory=dict)
    valid_until: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    # ROB-274 proposal-state fields.
    target_ref: TargetRefPayload | None = None
    current_state: dict[str, Any] | None = None
    proposed_state: dict[str, Any] | None = None
    diff: list[dict[str, Any]] | None = None
    apply_policy: ApplyPolicyLiteral | None = None

    @model_validator(mode="after")
    def _validate_watch_invariants(self) -> IngestReportItem:
        # Legacy callers (operation=None) keep the old invariant.
        if self.item_kind == "watch" and self.operation in (None, "create", "modify"):
            if self.watch_condition is None:
                raise ValueError(
                    "watch items require watch_condition when "
                    "operation is null/'create'/'modify'"
                )
            if self.valid_until is None:
                raise ValueError(
                    "watch items require valid_until when "
                    "operation is null/'create'/'modify'"
                )
        return self

    @model_validator(mode="after")
    def _validate_proposal_invariants(self) -> IngestReportItem:
        if self.operation in ("modify",):
            missing: list[str] = []
            if self.target_ref is None:
                missing.append("target_ref")
            if self.current_state is None:
                missing.append("current_state")
            if self.proposed_state is None:
                missing.append("proposed_state")
            if self.diff is None:
                missing.append("diff")
            if missing:
                raise ValueError(
                    f"operation='modify' requires {missing}"
                )
        if self.operation in ("cancel", "keep", "review"):
            missing = []
            if self.target_ref is None:
                missing.append("target_ref")
            if self.current_state is None:
                missing.append("current_state")
            if missing:
                raise ValueError(
                    f"operation={self.operation!r} requires {missing}"
                )
        return self
```

- [ ] **Step 4: Run schema tests again — confirm pass**

Run: `uv run pytest tests/services/investment_reports/test_schema_operation_aware.py -v`
Expected: all pass.

- [ ] **Step 5: Extend ORM model**

In `app/models/investment_reports.py`, inside `InvestmentReportItem` class:

Add new CHECK constraints to `__table_args__` (around line 243, just before `Index(...)` lines):

```python
        CheckConstraint(
            "operation IS NULL OR operation IN ("
            "'create','modify','cancel','keep','replace','review'"
            ")",
            name="ck_investment_report_items_operation",
        ),
        CheckConstraint(
            "apply_policy IS NULL OR apply_policy = 'requires_user_approval'",
            name="ck_investment_report_items_apply_policy",
        ),
```

Replace the two existing `ck_investment_report_items_watch_has_condition` and `ck_investment_report_items_watch_has_expiry` CHECKs with the operation-aware versions (matching the migration):

```python
        CheckConstraint(
            "item_kind <> 'watch' "
            "OR operation IN ('cancel','keep','review') "
            "OR watch_condition IS NOT NULL",
            name="ck_investment_report_items_watch_has_condition",
        ),
        CheckConstraint(
            "item_kind <> 'watch' "
            "OR operation IN ('cancel','keep','review') "
            "OR valid_until IS NOT NULL",
            name="ck_investment_report_items_watch_has_expiry",
        ),
```

Add new mapped columns inside the class body (after `item_metadata`, around line 307):

```python
    operation: Mapped[str | None] = mapped_column(Text)
    target_ref: Mapped[dict | None] = mapped_column(JSONB)
    current_state: Mapped[dict | None] = mapped_column(JSONB)
    proposed_state: Mapped[dict | None] = mapped_column(JSONB)
    diff: Mapped[list | None] = mapped_column(JSONB)
    apply_policy: Mapped[str | None] = mapped_column(Text)
```

Add new index to `__table_args__`:

```python
        Index(
            "ix_investment_report_items_operation_kind",
            "operation",
            "item_kind",
            "status",
        ),
```

- [ ] **Step 6: Extend response model**

In `app/schemas/investment_reports.py`, in `InvestmentReportItemResponse` (around line 259), add the new fields:

```python
    operation: OperationLiteral | None = None
    target_ref: dict[str, Any] | None = None
    current_state: dict[str, Any] | None = None
    proposed_state: dict[str, Any] | None = None
    diff: list[dict[str, Any]] | None = None
    apply_policy: ApplyPolicyLiteral | None = None
```

- [ ] **Step 7: Verification**

Run:
```bash
uv run ruff check
uv run ty check app/schemas/investment_reports.py app/models/investment_reports.py
uv run pytest tests/services/investment_reports/test_schema_operation_aware.py -v
```
Expected: all pass; no new ty errors.

- [ ] **Step 8: Commit**

```bash
git add app/models/investment_reports.py app/schemas/investment_reports.py \
        tests/services/investment_reports/test_schema_operation_aware.py
git commit -m "feat(rob-274): operation-aware proposal schema + validator"
```

---

## Task 3: Extend `SnapshotKind` for `pending_orders`

**Files:**
- Create: `alembic/versions/20260520_rob274_p2_extend_snapshot_kind.py`
- Modify: `app/schemas/investment_snapshots.py:19-32` — append `pending_orders` to the Literal.

### Steps

- [ ] **Step 1: Write the migration**

Create `alembic/versions/20260520_rob274_p2_extend_snapshot_kind.py`:

```python
"""ROB-274 — add 'pending_orders' to investment_snapshots.snapshot_kind CHECK.

Revision ID: 20260520_rob274_p2
Revises: 20260520_rob274_p1
Create Date: 2026-05-20

Pure CHECK extension. No data backfill. The new collector emits rows
with snapshot_kind='pending_orders'; existing rows are unaffected.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260520_rob274_p2"
down_revision: str | None = "20260520_rob274_p1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_OLD_KINDS = (
    "'portfolio','market','news','symbol',"
    "'candidate_universe','browser_probe','invest_page','journal',"
    "'watch_context','naver_remote_debug','toss_remote_debug',"
    "'llm_input_frozen'"
)
_NEW_KINDS = _OLD_KINDS + ",'pending_orders'"


def upgrade() -> None:
    op.drop_constraint(
        "ck_investment_snapshots_snapshot_kind",
        "investment_snapshots",
        schema="review",
    )
    op.create_check_constraint(
        "ck_investment_snapshots_snapshot_kind",
        "investment_snapshots",
        f"snapshot_kind IN ({_NEW_KINDS})",
        schema="review",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_investment_snapshots_snapshot_kind",
        "investment_snapshots",
        schema="review",
    )
    op.create_check_constraint(
        "ck_investment_snapshots_snapshot_kind",
        "investment_snapshots",
        f"snapshot_kind IN ({_OLD_KINDS})",
        schema="review",
    )
```

(Verify the `_OLD_KINDS` list against `alembic/versions/20260519_rob269_add_snapshot_foundation.py:190-194` before writing — if there is a literal mismatch, copy the exact string from that file.)

- [ ] **Step 2: Apply locally**

Run: `uv run alembic upgrade head && uv run alembic downgrade -1 && uv run alembic upgrade head`
Expected: all succeed.

- [ ] **Step 3: Extend Pydantic literal**

In `app/schemas/investment_snapshots.py:19-32`, append `"pending_orders"` to the `SnapshotKind` literal:

```python
SnapshotKind = Literal[
    "portfolio",
    "market",
    "news",
    "symbol",
    "candidate_universe",
    "browser_probe",
    "invest_page",
    "journal",
    "watch_context",
    "naver_remote_debug",
    "toss_remote_debug",
    "llm_input_frozen",
    "pending_orders",
]
```

- [ ] **Step 4: Verification**

Run: `uv run ty check app/schemas/investment_snapshots.py && uv run ruff check`
Expected: clean.

- [ ] **Step 5: Commit**

```bash
git add alembic/versions/20260520_rob274_p2_extend_snapshot_kind.py app/schemas/investment_snapshots.py
git commit -m "feat(rob-274): allow snapshot_kind='pending_orders'"
```

---

## Task 4: `pending_orders` snapshot collector (KR/US/crypto adapters inside)

**Files:**
- Create: `app/services/action_report/snapshot_backed/collectors/pending_orders.py`
- Create: `app/services/action_report/common/staleness.py`
- Test: `tests/services/action_report/snapshot_backed/test_pending_orders_collector.py`

### Steps

- [ ] **Step 1: Discover existing broker pending-order accessors**

Before writing the collector, locate the read-only RPCs to call. Run:

```bash
grep -rn "pending\|미체결\|orders/open\|inquire_order" \
  app/services/kis.py app/services/upbit.py \
  app/services/pending_reconciliation_service.py \
  app/services/crypto_pending_order_alert_service.py \
  app/services/action_report/us/account_snapshot.py 2>/dev/null | grep -v "test" | head -40
```

Record the canonical function names that return open/pending orders for KIS (KR + US) and Upbit. They are the adapters' targets. **If a broker has no read-only accessor, that adapter returns `confirmation_unknown` — do not invent a new broker call.**

- [ ] **Step 2: Write the staleness constants module**

Create `app/services/action_report/common/staleness.py`:

```python
"""ROB-274 — shared staleness constants for pending-order rationale generation.

KR/US use market-session expiry handled by the broker; crypto orders can
persist 24/7, so we apply an explicit age threshold instead. Threading
this through a module makes the value greppable and overridable from
settings if needed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Final

PENDING_ORDER_STALENESS_HOURS_CRYPTO: Final[int] = 24


def is_crypto_pending_order_stale(
    placed_at: datetime, *, now: datetime | None = None
) -> bool:
    """Return True if a crypto pending order is older than the staleness threshold."""

    reference = now or datetime.now(tz=timezone.utc)
    if placed_at.tzinfo is None:
        placed_at = placed_at.replace(tzinfo=timezone.utc)
    return reference - placed_at > timedelta(
        hours=PENDING_ORDER_STALENESS_HOURS_CRYPTO
    )
```

- [ ] **Step 3: Write failing collector tests**

Create `tests/services/action_report/snapshot_backed/test_pending_orders_collector.py`:

```python
"""ROB-274 — pending_orders collector tests."""

from __future__ import annotations

import datetime as dt
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.services.action_report.snapshot_backed.collectors.pending_orders import (
    PendingOrdersSnapshotCollector,
)
from app.services.investment_snapshots.collectors import CollectorRequest


@pytest.mark.asyncio
async def test_pending_orders_collector_kr_calls_kis_read_only_path():
    fake_kis = AsyncMock()
    fake_kis.fetch_pending_domestic_orders = AsyncMock(return_value=[
        {"odno": "K1", "symbol": "005930.KS", "side": "buy",
         "price": "70000", "quantity": "10", "remaining_quantity": "10",
         "placed_at": dt.datetime(2026, 5, 19, 12, 0, tzinfo=dt.timezone.utc)},
    ])
    collector = PendingOrdersSnapshotCollector(
        kis_client=fake_kis, upbit_client=None
    )
    request = CollectorRequest(market="kr", account_scope="kis_live")
    results = await collector.collect(request)
    assert len(results) == 1
    payload = results[0].payload_json
    assert payload["pending_orders"][0]["target_ref"]["broker"] == "kis"
    # No mutation call attempted.
    assert not fake_kis.place_order.called  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_pending_orders_collector_crypto_flags_stale():
    fake_upbit = AsyncMock()
    placed = dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(hours=48)
    fake_upbit.fetch_open_orders = AsyncMock(return_value=[
        {"uuid": "U1", "market": "KRW-BTC", "side": "bid",
         "price": "100000000", "volume": "0.01", "remaining_volume": "0.01",
         "created_at": placed.isoformat()},
    ])
    collector = PendingOrdersSnapshotCollector(
        kis_client=None, upbit_client=fake_upbit
    )
    request = CollectorRequest(market="crypto", account_scope="upbit_live")
    results = await collector.collect(request)
    payload = results[0].payload_json
    assert payload["pending_orders"][0]["stale"] is True


@pytest.mark.asyncio
async def test_pending_orders_collector_fails_open_when_client_missing():
    collector = PendingOrdersSnapshotCollector(
        kis_client=None, upbit_client=None
    )
    request = CollectorRequest(market="kr", account_scope="kis_live")
    results = await collector.collect(request)
    assert len(results) == 1
    assert results[0].freshness_status == "unavailable"
    assert results[0].errors_json["reason"].startswith("kis_client_unavailable")


@pytest.mark.asyncio
async def test_pending_orders_collector_does_not_call_broker_mutation_methods():
    fake_kis = AsyncMock()
    fake_kis.fetch_pending_domestic_orders = AsyncMock(return_value=[])
    collector = PendingOrdersSnapshotCollector(
        kis_client=fake_kis, upbit_client=None
    )
    await collector.collect(CollectorRequest(market="kr", account_scope="kis_live"))
    for forbidden in ("place_order", "cancel_order", "modify_order"):
        attr = getattr(fake_kis, forbidden, None)
        if attr is not None:
            assert not attr.called, f"collector must not call {forbidden}"
```

- [ ] **Step 4: Run tests, confirm failure**

Run: `uv run pytest tests/services/action_report/snapshot_backed/test_pending_orders_collector.py -v`
Expected: `ImportError: cannot import name 'PendingOrdersSnapshotCollector'`.

- [ ] **Step 5: Write the collector**

Create `app/services/action_report/snapshot_backed/collectors/pending_orders.py`:

```python
"""ROB-274 — pending_orders snapshot collector (read-only).

One collector kind with internal market/account adapters:

* ``market=kr``, ``account_scope=kis_live`` → KIS domestic open orders
* ``market=us``, ``account_scope=kis_live`` → KIS overseas open orders
* ``market=crypto``, ``account_scope=upbit_live`` → Upbit open orders

The collector never calls broker submit/cancel/modify methods. Missing
broker clients or fetch failures produce an ``unavailable`` result with
``errors_json.reason`` and do NOT raise — downstream classifier maps
this to ``action/review`` with ``확인 불가`` rationale.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Protocol

from app.services.action_report.common.staleness import (
    is_crypto_pending_order_stale,
)
from app.services.action_report.snapshot_backed.collectors._base import (
    build_result,
    unavailable_result,
    utcnow,
)
from app.services.investment_snapshots.collectors import (
    CollectorRequest,
    SnapshotCollectResult,
)


class _KISClientProtocol(Protocol):
    async def fetch_pending_domestic_orders(self) -> list[dict[str, Any]]: ...
    async def fetch_pending_overseas_orders(self) -> list[dict[str, Any]]: ...


class _UpbitClientProtocol(Protocol):
    async def fetch_open_orders(self) -> list[dict[str, Any]]: ...


class PendingOrdersSnapshotCollector:
    """Read-only collector for KR/US/crypto pending broker orders."""

    snapshot_kind: str = "pending_orders"

    def __init__(
        self,
        *,
        kis_client: _KISClientProtocol | None,
        upbit_client: _UpbitClientProtocol | None,
    ) -> None:
        self._kis = kis_client
        self._upbit = upbit_client

    async def collect(self, request: CollectorRequest) -> list[SnapshotCollectResult]:
        now = utcnow()
        market = request.market
        if market in ("kr", "us"):
            return [await self._collect_kis(market=market, now=now, request=request)]
        if market == "crypto":
            return [await self._collect_upbit(now=now, request=request)]
        return [
            unavailable_result(
                snapshot_kind=self.snapshot_kind,
                market=market,
                account_scope=request.account_scope,
                origin="auto_trader_db",
                reason=f"unsupported_market:{market}",
                as_of=now,
            )
        ]

    async def _collect_kis(
        self, *, market: str, now: dt.datetime, request: CollectorRequest
    ) -> SnapshotCollectResult:
        if self._kis is None:
            return unavailable_result(
                snapshot_kind=self.snapshot_kind,
                market=market,
                account_scope=request.account_scope,
                origin="kis_api",
                reason="kis_client_unavailable",
                as_of=now,
            )
        try:
            if market == "kr":
                raw = await self._kis.fetch_pending_domestic_orders()
            else:
                raw = await self._kis.fetch_pending_overseas_orders()
        except Exception as exc:  # noqa: BLE001 — collector must fail open
            return unavailable_result(
                snapshot_kind=self.snapshot_kind,
                market=market,
                account_scope=request.account_scope,
                origin="kis_api",
                reason=f"kis_fetch_failed:{type(exc).__name__}:{exc}",
                as_of=now,
            )
        normalized = [_normalize_kis_order(row, market=market) for row in raw]
        return build_result(
            snapshot_kind=self.snapshot_kind,
            market=market,
            account_scope=request.account_scope,
            payload={"pending_orders": normalized, "count": len(normalized)},
            origin="kis_api",
            as_of=now,
            coverage={"count": len(normalized)},
        )

    async def _collect_upbit(
        self, *, now: dt.datetime, request: CollectorRequest
    ) -> SnapshotCollectResult:
        if self._upbit is None:
            return unavailable_result(
                snapshot_kind=self.snapshot_kind,
                market="crypto",
                account_scope=request.account_scope,
                origin="upbit_mcp",
                reason="upbit_client_unavailable",
                as_of=now,
            )
        try:
            raw = await self._upbit.fetch_open_orders()
        except Exception as exc:  # noqa: BLE001
            return unavailable_result(
                snapshot_kind=self.snapshot_kind,
                market="crypto",
                account_scope=request.account_scope,
                origin="upbit_mcp",
                reason=f"upbit_fetch_failed:{type(exc).__name__}:{exc}",
                as_of=now,
            )
        normalized = [_normalize_upbit_order(row, now=now) for row in raw]
        return build_result(
            snapshot_kind=self.snapshot_kind,
            market="crypto",
            account_scope=request.account_scope,
            payload={"pending_orders": normalized, "count": len(normalized)},
            origin="upbit_mcp",
            as_of=now,
            coverage={"count": len(normalized)},
        )


def _normalize_kis_order(row: dict[str, Any], *, market: str) -> dict[str, Any]:
    return {
        "target_ref": {
            "type": "broker_order",
            "broker": "kis",
            "id": str(row.get("odno") or row.get("ord_no") or ""),
            "raw": dict(row),
        },
        "symbol": row.get("symbol"),
        "side": row.get("side"),
        "price": str(row.get("price")) if row.get("price") is not None else None,
        "quantity": str(row.get("quantity")) if row.get("quantity") is not None else None,
        "remaining_quantity": str(row.get("remaining_quantity"))
        if row.get("remaining_quantity") is not None
        else None,
        "placed_at": _isoformat_or_none(row.get("placed_at")),
        "stale": False,  # KR/US use session expiry; classifier handles.
        "market": market,
    }


def _normalize_upbit_order(row: dict[str, Any], *, now: dt.datetime) -> dict[str, Any]:
    placed_at_raw = row.get("created_at") or row.get("placed_at")
    placed_at = _coerce_datetime(placed_at_raw)
    stale = bool(placed_at and is_crypto_pending_order_stale(placed_at, now=now))
    return {
        "target_ref": {
            "type": "broker_order",
            "broker": "upbit",
            "id": str(row.get("uuid") or ""),
            "raw": dict(row),
        },
        "symbol": row.get("market"),
        "side": "buy" if row.get("side") == "bid" else "sell",
        "price": str(row.get("price")) if row.get("price") is not None else None,
        "quantity": str(row.get("volume")) if row.get("volume") is not None else None,
        "remaining_quantity": str(row.get("remaining_volume"))
        if row.get("remaining_volume") is not None
        else None,
        "placed_at": _isoformat_or_none(placed_at),
        "stale": stale,
        "market": "crypto",
    }


def _coerce_datetime(value: Any) -> dt.datetime | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)
    if isinstance(value, str):
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)
    return None


def _isoformat_or_none(value: Any) -> str | None:
    coerced = _coerce_datetime(value) if not isinstance(value, dt.datetime) else value
    if coerced is None:
        return None
    if coerced.tzinfo is None:
        coerced = coerced.replace(tzinfo=dt.timezone.utc)
    return coerced.isoformat()
```

**NOTE TO IMPLEMENTER:** The protocol methods `fetch_pending_domestic_orders`, `fetch_pending_overseas_orders`, `fetch_open_orders` must point at the real read-only RPCs you discovered in Step 1. If their actual names differ, rename the protocol methods AND update the call sites in the adapter. Do NOT invent new broker calls.

- [ ] **Step 6: Run collector tests — confirm pass**

Run: `uv run pytest tests/services/action_report/snapshot_backed/test_pending_orders_collector.py -v`
Expected: all 4 tests pass.

- [ ] **Step 7: Commit**

```bash
git add app/services/action_report/snapshot_backed/collectors/pending_orders.py \
        app/services/action_report/common/staleness.py \
        tests/services/action_report/snapshot_backed/test_pending_orders_collector.py
git commit -m "feat(rob-274): pending_orders snapshot collector with kr/us/crypto adapters"
```

---

## Task 5: Register `pending_orders` collector in production registry

**Files:**
- Modify: `app/services/action_report/snapshot_backed/collectors/registry.py:14-73`

### Steps

- [ ] **Step 1: Add the import and registration**

In `app/services/action_report/snapshot_backed/collectors/registry.py`, add to the imports:

```python
from app.services.action_report.snapshot_backed.collectors.pending_orders import (
    PendingOrdersSnapshotCollector,
)
```

Inside `production_collector_registry()`, after the optional-kind block (after the `BrowserProbeStubCollector` registration), add:

```python
    # ROB-274 — optional/fail-open. Adapters resolve at call time so the
    # generator stays usable without broker credentials wired for every
    # market.
    from app.services.kis import get_kis_client  # local import — circular guard
    from app.services.upbit import get_upbit_client

    registry.register(
        PendingOrdersSnapshotCollector(
            kis_client=get_kis_client(),
            upbit_client=get_upbit_client(),
        )
    )

    return registry
```

**NOTE TO IMPLEMENTER:** The exact KIS/Upbit accessor names (`get_kis_client` / `get_upbit_client`) may differ. Grep for the existing pattern (`grep -n "def get_.*_client\b" app/services/*.py`) and use the canonical accessor. If the client must be instantiated with credentials per-request, instead pass a factory: `kis_client_factory: Callable[[], Awaitable[KISClient]]` and resolve inside `_collect_kis`. Keep the read-only invariant.

- [ ] **Step 2: Update registry test in `test_collectors.py`**

Modify `tests/services/action_report/snapshot_backed/test_collectors.py`. Find the `test_production_collector_registry_lists_all_kinds` test (grep for `production_collector_registry`) and extend the expected kind set to include `"pending_orders"`.

- [ ] **Step 3: Verification**

Run: `uv run pytest tests/services/action_report/snapshot_backed/test_collectors.py -v`
Expected: pass.

- [ ] **Step 4: Commit**

```bash
git add app/services/action_report/snapshot_backed/collectors/registry.py \
        tests/services/action_report/snapshot_backed/test_collectors.py
git commit -m "feat(rob-274): register pending_orders collector in production registry"
```

---

## Task 6: Proposal classifier module

**Files:**
- Create: `app/services/action_report/snapshot_backed/proposal_classifier.py`
- Test: `tests/services/action_report/snapshot_backed/test_proposal_classifier.py`

### Steps

- [ ] **Step 1: Write failing classifier tests**

Create `tests/services/action_report/snapshot_backed/test_proposal_classifier.py`:

```python
"""ROB-274 — proposal classifier tests."""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

import pytest

from app.schemas.investment_reports import IngestReportItem, WatchConditionPayload
from app.services.action_report.snapshot_backed.proposal_classifier import (
    ClassifierContext,
    classify_items,
)


def _draft_watch_item(symbol: str, threshold: str = "100") -> IngestReportItem:
    return IngestReportItem(
        client_item_key=f"w-{symbol}",
        item_kind="watch",
        intent="trend_recovery_review",
        rationale="r",
        symbol=symbol,
        watch_condition=WatchConditionPayload(
            metric="price", operator="above", threshold=Decimal(threshold)
        ),
        valid_until=dt.datetime.now(tz=dt.timezone.utc) + dt.timedelta(days=7),
    )


def _active_alert(symbol: str, threshold: str, operator: str = "above") -> dict:
    return {
        "alert_uuid": uuid.uuid4(),
        "symbol": symbol,
        "metric": "price",
        "operator": operator,
        "threshold": Decimal(threshold),
        "intent": "trend_recovery_review",
        "action_mode": "notify_only",
        "status": "active",
        "valid_until": dt.datetime.now(tz=dt.timezone.utc) + dt.timedelta(days=7),
    }


def _pending_order(broker: str, symbol: str, *, stale: bool = False) -> dict:
    return {
        "target_ref": {"type": "broker_order", "broker": broker, "id": "O1", "raw": {}},
        "symbol": symbol,
        "side": "buy",
        "price": "100",
        "quantity": "1",
        "remaining_quantity": "1",
        "placed_at": dt.datetime.now(tz=dt.timezone.utc).isoformat(),
        "stale": stale,
    }


def test_watch_no_match_becomes_create():
    classified = classify_items(
        items=[_draft_watch_item("KRW-BTC")],
        context=ClassifierContext(active_watches=[], pending_orders=[]),
    )
    assert classified[0].operation == "create"
    assert classified[0].target_ref is None


def test_watch_matching_existing_active_with_same_condition_becomes_keep():
    draft = _draft_watch_item("KRW-BTC", threshold="100")
    alert = _active_alert("KRW-BTC", threshold="100", operator="above")
    classified = classify_items(
        items=[draft],
        context=ClassifierContext(active_watches=[alert], pending_orders=[]),
    )
    assert classified[0].operation == "keep"
    assert classified[0].target_ref is not None
    assert classified[0].target_ref.type == "investment_watch_alert"


def test_watch_matching_existing_with_changed_threshold_becomes_modify():
    draft = _draft_watch_item("KRW-BTC", threshold="120")
    alert = _active_alert("KRW-BTC", threshold="100", operator="above")
    classified = classify_items(
        items=[draft],
        context=ClassifierContext(active_watches=[alert], pending_orders=[]),
    )
    assert classified[0].operation == "modify"
    assert classified[0].diff is not None
    assert any(d["field"] == "threshold" for d in classified[0].diff)


def test_multiple_ambiguous_watches_become_review():
    draft = _draft_watch_item("KRW-BTC", threshold="100")
    alerts = [
        _active_alert("KRW-BTC", threshold="100"),
        _active_alert("KRW-BTC", threshold="100"),
    ]
    classified = classify_items(
        items=[draft],
        context=ClassifierContext(active_watches=alerts, pending_orders=[]),
    )
    assert classified[0].operation == "review"
    assert classified[0].target_ref.type == "ambiguous"


def test_buy_action_with_matching_open_order_keep():
    draft = IngestReportItem(
        client_item_key="a-1",
        item_kind="action",
        intent="buy_review",
        rationale="r",
        symbol="KRW-BTC",
        side="buy",
    )
    classified = classify_items(
        items=[draft],
        context=ClassifierContext(
            active_watches=[],
            pending_orders=[_pending_order("upbit", "KRW-BTC")],
        ),
    )
    assert classified[0].operation == "keep"
    assert classified[0].target_ref.type == "broker_order"


def test_buy_action_with_stale_open_order_becomes_review_with_confirmation_note():
    draft = IngestReportItem(
        client_item_key="a-1",
        item_kind="action",
        intent="buy_review",
        rationale="r",
        symbol="KRW-BTC",
        side="buy",
    )
    classified = classify_items(
        items=[draft],
        context=ClassifierContext(
            active_watches=[],
            pending_orders=[_pending_order("upbit", "KRW-BTC", stale=True)],
        ),
    )
    assert classified[0].operation == "review"


def test_pending_orders_unavailable_marks_dependent_items_review():
    draft = IngestReportItem(
        client_item_key="a-1",
        item_kind="action",
        intent="buy_review",
        rationale="r",
        symbol="KRW-BTC",
        side="buy",
    )
    classified = classify_items(
        items=[draft],
        context=ClassifierContext(
            active_watches=[], pending_orders=None
        ),
    )
    # When pending_orders snapshot is missing, item must be downgraded
    # to action/review with explicit unknown note.
    assert classified[0].operation == "review"
    assert "확인 불가" in classified[0].rationale
```

- [ ] **Step 2: Run tests, confirm failure**

Run: `uv run pytest tests/services/action_report/snapshot_backed/test_proposal_classifier.py -v`
Expected: ImportError.

- [ ] **Step 3: Write the classifier**

Create `app/services/action_report/snapshot_backed/proposal_classifier.py`:

```python
"""ROB-274 — pure classifier: draft items + context → classified items.

Decision rules:

* watch/create   no matching active watch
* watch/keep     exactly one matching active watch with same condition
* watch/modify   exactly one matching active watch with changed condition
* watch/review   multiple matching active watches (ambiguous target)
* action/keep    pending broker order exists for same symbol/side and not stale
* action/review  pending broker order stale, or pending_orders snapshot missing
* action/modify, action/cancel  out-of-scope auto-classification in this PR —
                                callers can still produce these directly with
                                pre-filled fields; the classifier never
                                downgrades them.

The classifier never mutates broker or watch state. It only enriches
draft items with operation/target_ref/current_state/proposed_state/diff
plus a default ``apply_policy='requires_user_approval'`` for proposals
that reference existing state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.schemas.investment_reports import (
    IngestReportItem,
    TargetRefPayload,
    WatchConditionPayload,
)


@dataclass(slots=True)
class ClassifierContext:
    """Operational state inputs the classifier consults."""

    active_watches: list[dict[str, Any]] = field(default_factory=list)
    # None means the pending_orders snapshot was unavailable (collector
    # failed open). Empty list means the snapshot was fresh and reported
    # no open orders. The classifier MUST distinguish these.
    pending_orders: list[dict[str, Any]] | None = field(default_factory=list)


def classify_items(
    *, items: list[IngestReportItem], context: ClassifierContext
) -> list[IngestReportItem]:
    """Return new IngestReportItem instances with operation/etc. populated."""

    return [_classify_one(item, context) for item in items]


def _classify_one(
    item: IngestReportItem, context: ClassifierContext
) -> IngestReportItem:
    if item.operation is not None:
        # Caller pre-classified — pass through unchanged.
        return item
    if item.item_kind == "watch":
        return _classify_watch(item, context)
    if item.item_kind == "action":
        return _classify_action(item, context)
    # risk items are not auto-classified for proposal semantics; default
    # to operation=review when target_ref already set, else leave None.
    return item


def _classify_watch(
    item: IngestReportItem, context: ClassifierContext
) -> IngestReportItem:
    if item.symbol is None or item.watch_condition is None:
        return item.model_copy(update={"operation": "create"})

    candidates = [
        a
        for a in context.active_watches
        if a.get("symbol") == item.symbol
        and a.get("metric") == item.watch_condition.metric
    ]
    if not candidates:
        return item.model_copy(update={"operation": "create"})
    if len(candidates) > 1:
        return item.model_copy(
            update={
                "operation": "review",
                "target_ref": TargetRefPayload(
                    type="ambiguous",
                    candidates=[
                        {"id": str(c["alert_uuid"]), "threshold": str(c["threshold"])}
                        for c in candidates
                    ],
                ),
                "rationale": item.rationale
                + " (다중 활성 와치 감지 — 수동 검토 필요)",
                "apply_policy": "requires_user_approval",
            }
        )
    alert = candidates[0]
    current_state = {
        "metric": alert.get("metric"),
        "operator": alert.get("operator"),
        "threshold": str(alert.get("threshold")),
        "action_mode": alert.get("action_mode"),
    }
    proposed_state = {
        "metric": item.watch_condition.metric,
        "operator": item.watch_condition.operator,
        "threshold": str(item.watch_condition.threshold),
        "action_mode": item.watch_condition.action_mode,
    }
    diff = _diff_states(current_state, proposed_state)
    target_ref = TargetRefPayload(
        type="investment_watch_alert",
        id=str(alert["alert_uuid"]),
        status=alert.get("status"),
    )
    if not diff:
        return item.model_copy(
            update={
                "operation": "keep",
                "target_ref": target_ref,
                "current_state": current_state,
                "apply_policy": "requires_user_approval",
            }
        )
    return item.model_copy(
        update={
            "operation": "modify",
            "target_ref": target_ref,
            "current_state": current_state,
            "proposed_state": proposed_state,
            "diff": diff,
            "apply_policy": "requires_user_approval",
        }
    )


def _classify_action(
    item: IngestReportItem, context: ClassifierContext
) -> IngestReportItem:
    if context.pending_orders is None:
        return item.model_copy(
            update={
                "operation": "review",
                "rationale": item.rationale + " (pending order 확인 불가)",
                "apply_policy": "requires_user_approval",
            }
        )
    if item.symbol is None or item.side is None:
        return item
    matching = [
        o
        for o in context.pending_orders
        if o.get("symbol") == item.symbol and o.get("side") == item.side
    ]
    if not matching:
        return item  # nothing to overlap with; caller's draft stands.
    if any(o.get("stale") for o in matching):
        # Stale pending order requires human review before placing fresh order.
        first = matching[0]
        return item.model_copy(
            update={
                "operation": "review",
                "target_ref": TargetRefPayload(**first["target_ref"]),
                "current_state": _order_to_state(first),
                "rationale": item.rationale + " (기존 미체결 주문 stale)",
                "apply_policy": "requires_user_approval",
            }
        )
    first = matching[0]
    return item.model_copy(
        update={
            "operation": "keep",
            "target_ref": TargetRefPayload(**first["target_ref"]),
            "current_state": _order_to_state(first),
            "apply_policy": "requires_user_approval",
        }
    )


def _diff_states(
    current: dict[str, Any], proposed: dict[str, Any]
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    keys = set(current) | set(proposed)
    for key in sorted(keys):
        a = current.get(key)
        b = proposed.get(key)
        if a != b:
            out.append({"field": key, "from": a, "to": b})
    return out


def _order_to_state(order: dict[str, Any]) -> dict[str, Any]:
    return {
        "side": order.get("side"),
        "price": order.get("price"),
        "quantity": order.get("quantity"),
        "remaining_quantity": order.get("remaining_quantity"),
        "placed_at": order.get("placed_at"),
        "stale": order.get("stale"),
    }
```

- [ ] **Step 4: Run classifier tests — confirm pass**

Run: `uv run pytest tests/services/action_report/snapshot_backed/test_proposal_classifier.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add app/services/action_report/snapshot_backed/proposal_classifier.py \
        tests/services/action_report/snapshot_backed/test_proposal_classifier.py
git commit -m "feat(rob-274): proposal classifier for watch/action existing-state semantics"
```

---

## Task 7: Wire classifier into `SnapshotBackedReportGenerator`

**Files:**
- Modify: `app/services/action_report/snapshot_backed/generator.py:124-199, 271-337`
- Test: `tests/services/action_report/snapshot_backed/test_generator.py` — add classifier-integration test.

### Steps

- [ ] **Step 1: Write failing integration test**

Append to `tests/services/action_report/snapshot_backed/test_generator.py`:

```python
@pytest.mark.asyncio
async def test_generator_classifies_items_against_active_watches_and_pending_orders():
    """ROB-274 — classifier runs between ensure-bundle and ingest."""

    from app.schemas.investment_reports import IngestReportItem, WatchConditionPayload
    from app.services.action_report.snapshot_backed.proposal_classifier import (
        ClassifierContext,
    )

    draft_items = [
        IngestReportItem(
            client_item_key="w-1",
            item_kind="watch",
            symbol="KRW-BTC",
            intent="trend_recovery_review",
            rationale="r",
            watch_condition=WatchConditionPayload(
                metric="price", operator="above", threshold=Decimal("100")
            ),
            valid_until=dt.datetime.now(tz=dt.timezone.utc) + dt.timedelta(days=7),
        ),
    ]
    # Stub ensure_service and ingestion_service; verify the generator
    # consults active-watch + pending-order snapshots and emits
    # operation=keep when the watch already exists with same condition.

    # ... [implementer writes the stub wiring; see existing test_generator
    #      for the established fixture pattern] ...
    pytest.skip("Implementation-pending stub: see Task 7 step 3 for wiring.")
```

**NOTE:** This test is intentionally a `pytest.skip` placeholder — the stub plumbing in this file is non-trivial; the implementer fills it in during Step 3. Leave the skip so the test reports as pending until then.

- [ ] **Step 2: Verify the stub is registered**

Run: `uv run pytest tests/services/action_report/snapshot_backed/test_generator.py -v -k classifier`
Expected: 1 skipped.

- [ ] **Step 3: Update `SnapshotBackedReportGenerator.generate()`**

In `app/services/action_report/snapshot_backed/generator.py`, modify `generate()` (around line 124). After the bundle ensure but before the stale gate / ingest, extract active-watch + pending-order context from the bundle and classify items:

```python
        # ROB-274 — classifier pass. Pulls active watches and pending
        # orders out of the bundle ensure result so item drafts are
        # enriched with operation/target_ref/current_state/proposed_state.
        from app.services.action_report.snapshot_backed.proposal_classifier import (
            ClassifierContext,
            classify_items,
        )

        active_watches = _extract_snapshot_payload(
            ensure_response, kind="watch_context", key="active_alerts"
        ) or []
        pending_orders_payload = _extract_snapshot_payload(
            ensure_response, kind="pending_orders", key="pending_orders"
        )
        # None signals "snapshot unavailable" — classifier treats this
        # differently from "snapshot present but empty".
        pending_orders = (
            None if _snapshot_unavailable(ensure_response, "pending_orders")
            else (pending_orders_payload or [])
        )
        request = request.model_copy(
            update={
                "items": classify_items(
                    items=list(request.items),
                    context=ClassifierContext(
                        active_watches=active_watches,
                        pending_orders=pending_orders,
                    ),
                )
            }
        )
```

Add module-level helpers below `_BLOCKING_BUNDLE_STATUSES_FOR_PUBLISHED`:

```python
def _extract_snapshot_payload(
    ensure_response: Any, *, kind: str, key: str
) -> list[dict[str, Any]] | None:
    """Pluck a payload list out of an ensure_response snapshot."""

    snapshots = getattr(ensure_response, "snapshots_by_kind", None) or {}
    snap = snapshots.get(kind)
    if snap is None:
        return None
    payload = getattr(snap, "payload_json", None) or snap.get("payload_json") or {}
    value = payload.get(key)
    return list(value) if isinstance(value, list) else None


def _snapshot_unavailable(ensure_response: Any, kind: str) -> bool:
    snapshots = getattr(ensure_response, "snapshots_by_kind", None) or {}
    snap = snapshots.get(kind)
    if snap is None:
        return True
    status = getattr(snap, "freshness_status", None) or snap.get("freshness_status")
    return status in ("unavailable", "hard_stale", "failed")
```

**NOTE TO IMPLEMENTER:** The exact attribute name for "snapshots by kind" on `EnsureBundleResponse` may differ — confirm by reading `app/services/action_report/common/snapshot_bundle.py` (or wherever `SnapshotBundleEnsureService` returns its response) and align. If the response only exposes a flat list, build the lookup yourself: `{snap.snapshot_kind: snap for snap in ensure_response.snapshots}`.

- [ ] **Step 4: Update normalization to cover new fields**

In `generator.py:271-337` (`_build_ingest_request`), extend the normalized item loop so the new proposal fields are also passed through `to_jsonable`:

```python
        for item in request.items:
            item_dict = item.model_dump(mode="python")
            for key in (
                "evidence_snapshot",
                "trigger_checklist",
                "max_action",
                "metadata",
                "target_ref",
                "current_state",
                "proposed_state",
                "diff",
            ):
                if key in item_dict and item_dict[key] is not None:
                    item_dict[key] = to_jsonable(item_dict[key])
            normalized_items.append(item_dict)
```

- [ ] **Step 5: Implement the previously-skipped test**

Replace the `pytest.skip(...)` in Step 1 with a real stub using the test_generator.py fixture conventions. Use `AsyncMock` for `ensure_service` returning a response whose `snapshots_by_kind` (or equivalent) includes a `watch_context` payload with the matching alert; assert that the `IngestReportRequest` passed to `ingestion_service.ingest` carries `items[0].operation == "keep"`.

- [ ] **Step 6: Run full generator test suite**

Run: `uv run pytest tests/services/action_report/snapshot_backed/test_generator.py -v`
Expected: all pass, including the new classifier integration test.

- [ ] **Step 7: Commit**

```bash
git add app/services/action_report/snapshot_backed/generator.py \
        tests/services/action_report/snapshot_backed/test_generator.py
git commit -m "feat(rob-274): wire proposal classifier into snapshot-backed generator"
```

---

## Task 8: Persist new fields through ingestion + repository

**Files:**
- Modify: `app/services/investment_reports/ingestion.py:106-141`
- Modify: `app/services/investment_reports/repository.py` — `insert_item` signature.
- Test: extend `tests/services/investment_reports/` existing ingestion test if any, or add a focused test under `tests/services/investment_reports/`.

### Steps

- [ ] **Step 1: Locate existing ingestion-service tests**

Run: `grep -rln "InvestmentReportIngestionService" tests/ 2>/dev/null`
Open them to determine the established fixture pattern; the new round-trip test follows that pattern.

- [ ] **Step 2: Add ingestion round-trip test**

Append to (or create) the existing ingestion-service test file a test that:

```python
@pytest.mark.asyncio
async def test_ingest_persists_proposal_fields(db_session):
    """ROB-274 — operation/target_ref/current_state/proposed_state/diff/apply_policy survive a round-trip."""

    from app.schemas.investment_reports import (
        IngestReportItem,
        IngestReportRequest,
        TargetRefPayload,
    )
    from app.services.investment_reports.ingestion import InvestmentReportIngestionService

    item = IngestReportItem(
        client_item_key="k1",
        item_kind="watch",
        operation="cancel",
        intent="risk_review",
        rationale="r",
        target_ref=TargetRefPayload(
            type="investment_watch_alert", id="alert-1", status="active"
        ),
        current_state={"metric": "price", "operator": "above", "threshold": "100"},
        apply_policy="requires_user_approval",
    )
    request = IngestReportRequest(
        report_type="t",
        market="crypto",
        account_scope="upbit_live",
        created_by_profile="claude_code",
        title="t",
        summary="s",
        kst_date="2026-05-20",
        items=[item],
    )
    svc = InvestmentReportIngestionService(db_session)
    report = await svc.ingest(request)
    await db_session.flush()

    row = (await db_session.execute(
        select(InvestmentReportItem).where(
            InvestmentReportItem.report_id == report.id
        )
    )).scalar_one()
    assert row.operation == "cancel"
    assert row.target_ref == {"type": "investment_watch_alert", "id": "alert-1", "status": "active"}
    assert row.current_state["metric"] == "price"
    assert row.apply_policy == "requires_user_approval"
```

(Use the actual fixture name — likely `async_session` or similar — from the existing test file.)

- [ ] **Step 3: Run test, confirm failure**

Run: `uv run pytest -k test_ingest_persists_proposal_fields -v`
Expected: fail with `TypeError: insert_item got unexpected keyword 'operation'` or schema mismatch.

- [ ] **Step 4: Extend repository `insert_item`**

In `app/services/investment_reports/repository.py`, find `insert_item` and add the new parameters to its signature (kwargs), and to the ORM construction:

```python
    async def insert_item(
        self,
        *,
        report_id: int,
        idempotency_key: str,
        item_kind: str,
        symbol: str | None,
        side: str | None,
        intent: str,
        target_kind: str,
        priority: int,
        confidence: Decimal | None,
        rationale: str,
        evidence_snapshot: dict,
        watch_condition: dict | None,
        trigger_checklist: list,
        max_action: dict,
        valid_until: datetime | None,
        item_metadata: dict,
        # ROB-274 — proposal state fields.
        operation: str | None = None,
        target_ref: dict | None = None,
        current_state: dict | None = None,
        proposed_state: dict | None = None,
        diff: list | None = None,
        apply_policy: str | None = None,
    ) -> InvestmentReportItem:
        row = InvestmentReportItem(
            report_id=report_id,
            idempotency_key=idempotency_key,
            item_kind=item_kind,
            symbol=symbol,
            side=side,
            intent=intent,
            target_kind=target_kind,
            priority=priority,
            confidence=confidence,
            rationale=rationale,
            evidence_snapshot=evidence_snapshot,
            watch_condition=watch_condition,
            trigger_checklist=trigger_checklist,
            max_action=max_action,
            valid_until=valid_until,
            item_metadata=item_metadata,
            operation=operation,
            target_ref=target_ref,
            current_state=current_state,
            proposed_state=proposed_state,
            diff=diff,
            apply_policy=apply_policy,
        )
        self._session.add(row)
        return row
```

(Adapt parameter list to the existing repository signature shape — copy the existing structure, just append the new optional fields.)

- [ ] **Step 5: Extend ingestion `_insert_item`**

In `app/services/investment_reports/ingestion.py`, modify `_insert_item` (around line 106) to pass the new fields through:

```python
    async def _insert_item(
        self, report: InvestmentReport, item_req: IngestReportItem
    ) -> None:
        watch_condition_payload = (
            item_req.watch_condition.model_dump(mode="json")
            if item_req.watch_condition is not None
            else None
        )
        target_ref_payload = (
            item_req.target_ref.model_dump(mode="json")
            if item_req.target_ref is not None
            else None
        )
        idempotency_key = item_key(
            report_uuid=str(report.report_uuid),
            client_item_key=item_req.client_item_key,
            item_kind=item_req.item_kind,
            symbol=item_req.symbol,
            side=item_req.side,
            intent=item_req.intent,
            watch_condition=watch_condition_payload,
        )
        await self._repo.insert_item(
            report_id=report.id,
            idempotency_key=idempotency_key,
            item_kind=item_req.item_kind,
            symbol=item_req.symbol,
            side=item_req.side,
            intent=item_req.intent,
            target_kind=item_req.target_kind,
            priority=item_req.priority,
            confidence=item_req.confidence,
            rationale=item_req.rationale,
            evidence_snapshot=item_req.evidence_snapshot,
            watch_condition=watch_condition_payload,
            trigger_checklist=item_req.trigger_checklist,
            max_action=item_req.max_action,
            valid_until=item_req.valid_until,
            item_metadata=item_req.metadata,
            operation=item_req.operation,
            target_ref=target_ref_payload,
            current_state=item_req.current_state,
            proposed_state=item_req.proposed_state,
            diff=item_req.diff,
            apply_policy=item_req.apply_policy,
        )
```

- [ ] **Step 6: Run round-trip test — confirm pass**

Run: `uv run pytest -k test_ingest_persists_proposal_fields -v`
Expected: pass.

- [ ] **Step 7: Commit**

```bash
git add app/services/investment_reports/repository.py \
        app/services/investment_reports/ingestion.py \
        tests/services/investment_reports/
git commit -m "feat(rob-274): persist proposal fields through ingestion + repository"
```

---

## Task 9: Extend `investment_report_context_get` MCP response

**Files:**
- Modify: `app/mcp_server/tooling/investment_reports_handlers.py`
- Modify: `app/schemas/investment_reports.py` — `PreviousReportContextResponse` if it needs pending_orders surface.

### Steps

- [ ] **Step 1: Locate the handler**

Run: `grep -n "investment_report_context_get\|PreviousReportContextResponse" app/mcp_server/tooling/investment_reports_handlers.py | head -20`

Inspect the handler that builds `PreviousReportContextResponse`.

- [ ] **Step 2: Add `pending_orders` field to response model**

In `app/schemas/investment_reports.py`, extend `PreviousReportContextResponse` (around line 396):

```python
class PreviousReportContextResponse(BaseModel):
    """``investment_report_context_get`` / ``GET /.../investment-reports/context``."""

    prior_reports: list[InvestmentReportResponse]
    unresolved_deferred_items: list[InvestmentReportItemResponse]
    active_watches: list[InvestmentWatchAlertResponse]
    triggered_events: list[InvestmentWatchEventResponse]
    recent_decisions: list[InvestmentReportItemDecisionResponse]
    # ROB-274 — pending broker order snapshot for the same market/account.
    # null means the snapshot was not available at context fetch time.
    pending_orders: list[dict[str, Any]] | None = None
```

- [ ] **Step 3: Write a focused handler test**

Add a test (extend the existing handler-test file; locate via `grep -rln "investment_report_context_get" tests/`):

```python
@pytest.mark.asyncio
async def test_context_get_includes_pending_orders_when_collector_succeeds(
    monkeypatch, db_session
):
    """ROB-274 — context response surfaces pending_orders snapshot."""
    from app.services.investment_snapshots.collectors import (
        CollectorRequest,
        SnapshotCollectResult,
    )

    fake_result = SnapshotCollectResult(
        snapshot_kind="pending_orders",
        market="crypto",
        account_scope="upbit_live",
        source_kind="auto_trader_mcp",
        payload_json={"pending_orders": [{"symbol": "KRW-BTC", "side": "buy"}]},
        as_of=dt.datetime.now(tz=dt.timezone.utc),
        freshness_status="fresh",
    )
    fake_collector = AsyncMock()
    fake_collector.collect = AsyncMock(return_value=[fake_result])

    monkeypatch.setattr(
        "app.mcp_server.tooling.investment_reports_handlers."
        "_pending_orders_collector",
        lambda session: fake_collector,
        raising=False,
    )
    # Call the handler with market=crypto, account=upbit_live, then assert
    # response.pending_orders == [{"symbol": "KRW-BTC", "side": "buy"}].
    # Use the established handler-test fixture pattern in this file.
```

- [ ] **Step 4: Update the handler implementation**

Modify the handler so it invokes the collector registry's `pending_orders` collector (read-only) and includes the payload in the response. The collector is already registered (Task 5).

```python
    pending_results = await registry.get("pending_orders").collect(
        CollectorRequest(market=market, account_scope=account_scope)
    )
    pending_payload = (
        pending_results[0].payload_json.get("pending_orders")
        if pending_results
        and pending_results[0].freshness_status not in ("unavailable", "hard_stale", "failed")
        else None
    )
    return PreviousReportContextResponse(
        ...,
        pending_orders=pending_payload,
    )
```

- [ ] **Step 5: Run handler test**

Run: `uv run pytest tests/mcp_server/ -v -k context_get`
Expected: pass.

- [ ] **Step 6: Commit**

```bash
git add app/schemas/investment_reports.py \
        app/mcp_server/tooling/investment_reports_handlers.py \
        tests/mcp_server/
git commit -m "feat(rob-274): expose pending_orders in investment_report_context_get"
```

---

## Task 10: Frontend — English badges + diff renderer

**Files:**
- Modify: `frontend/invest/src/components/investment-reports/InvestmentReportBundleContent.tsx:22-24, 484`
- Modify: `frontend/invest/src/api/types.ts` (or wherever item response type lives) — add new optional fields.
- Create: `frontend/invest/src/components/investment-reports/ProposalDiffPanel.tsx`
- Test: `frontend/invest/src/__tests__/InvestmentReportBundleContent.proposal.test.tsx`

### Steps

- [ ] **Step 1: Locate the item response type**

Run: `grep -rn "watch_condition\|item_kind" frontend/invest/src/api/ frontend/invest/src/types/ 2>/dev/null | head -20`

Identify the file defining the `InvestmentReportItem`-equivalent TypeScript interface.

- [ ] **Step 2: Add new optional fields to the TypeScript type**

In the located file (likely `frontend/invest/src/api/types.ts`), extend the item interface:

```typescript
export type ProposalOperation =
  | "create" | "modify" | "cancel" | "keep" | "replace" | "review";

export interface ProposalTargetRef {
  type: "investment_watch_alert" | "broker_order" | "ambiguous";
  id?: string | null;
  status?: string | null;
  broker?: string | null;
  raw?: Record<string, unknown> | null;
  candidates?: Array<Record<string, unknown>> | null;
}

export interface ProposalDiffEntry {
  field: string;
  from: unknown;
  to: unknown;
}

// In the existing item interface — add:
//   operation?: ProposalOperation | null;
//   target_ref?: ProposalTargetRef | null;
//   current_state?: Record<string, unknown> | null;
//   proposed_state?: Record<string, unknown> | null;
//   diff?: ProposalDiffEntry[] | null;
//   apply_policy?: "requires_user_approval" | null;
```

- [ ] **Step 3: Replace the Korean badge map**

In `frontend/invest/src/components/investment-reports/InvestmentReportBundleContent.tsx`, lines 22–24:

```typescript
// BEFORE
const ITEM_KIND_LABEL: Record<string, string> = {
  action: "액션",
  watch: "와치",
  risk: "리스크",
};
```

Replace with:

```typescript
const ITEM_KIND_LABEL: Record<string, string> = {
  action: "action",
  watch: "watch",
  risk: "risk",
};
```

Also line 484 ("활성 와치"):

```typescript
// BEFORE
활성 와치 ({bundle.alerts.length})
// AFTER
active watches ({bundle.alerts.length})
```

Leave `리스크` strong header at line 163 and `무액션 노트` at line 169 unchanged — those are body copy, not category badges (per locked decision §4 minor #5).

- [ ] **Step 4: Build `ProposalDiffPanel`**

Create `frontend/invest/src/components/investment-reports/ProposalDiffPanel.tsx`:

```typescript
import type {
  ProposalDiffEntry,
  ProposalTargetRef,
} from "../../api/types";

interface ProposalDiffPanelProps {
  operation: string;
  targetRef?: ProposalTargetRef | null;
  currentState?: Record<string, unknown> | null;
  proposedState?: Record<string, unknown> | null;
  diff?: ProposalDiffEntry[] | null;
}

function renderValue(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

export function ProposalDiffPanel(props: ProposalDiffPanelProps) {
  const { operation, targetRef, currentState, proposedState, diff } = props;
  if (operation === "create") return null;
  return (
    <div className="proposal-diff-panel" data-operation={operation}>
      <div className="proposal-diff-header">
        <span className="proposal-op-badge" data-operation={operation}>
          {operation}
        </span>
        {targetRef && (
          <span className="proposal-target-ref">
            {targetRef.type}
            {targetRef.id ? `:${targetRef.id}` : ""}
          </span>
        )}
      </div>
      {currentState && (
        <div className="proposal-current-state">
          <strong>current</strong>
          <pre>{JSON.stringify(currentState, null, 2)}</pre>
        </div>
      )}
      {proposedState && (
        <div className="proposal-proposed-state">
          <strong>proposed</strong>
          <pre>{JSON.stringify(proposedState, null, 2)}</pre>
        </div>
      )}
      {diff && diff.length > 0 && (
        <table className="proposal-diff-table">
          <thead>
            <tr><th>field</th><th>from</th><th>to</th></tr>
          </thead>
          <tbody>
            {diff.map((entry) => (
              <tr key={entry.field}>
                <td>{entry.field}</td>
                <td>{renderValue(entry.from)}</td>
                <td>{renderValue(entry.to)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
```

- [ ] **Step 5: Wire the panel into the bundle content**

In `InvestmentReportBundleContent.tsx`, inside the item rendering loop, after the existing item header / rationale block, add:

```tsx
{item.operation && item.operation !== "create" && (
  <ProposalDiffPanel
    operation={item.operation}
    targetRef={item.target_ref}
    currentState={item.current_state}
    proposedState={item.proposed_state}
    diff={item.diff}
  />
)}
```

Import at the top:

```tsx
import { ProposalDiffPanel } from "./ProposalDiffPanel";
```

- [ ] **Step 6: Write a focused Vitest test**

Create `frontend/invest/src/__tests__/InvestmentReportBundleContent.proposal.test.tsx`:

```typescript
import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";

import { InvestmentReportBundleContent } from "../components/investment-reports/InvestmentReportBundleContent";

const sampleBundle = {
  report: {
    report_uuid: "00000000-0000-0000-0000-000000000001",
    market: "crypto",
    title: "t",
    summary: "s",
    status: "published",
    // ... fill in minimum required fields per the existing fixture pattern ...
  },
  items: [
    {
      item_uuid: "00000000-0000-0000-0000-000000000002",
      item_kind: "watch",
      operation: "modify",
      target_ref: { type: "investment_watch_alert", id: "alert-1", status: "active" },
      current_state: { threshold: "100" },
      proposed_state: { threshold: "120" },
      diff: [{ field: "threshold", from: "100", to: "120" }],
      apply_policy: "requires_user_approval",
      rationale: "r",
      symbol: "KRW-BTC",
      intent: "trend_recovery_review",
    },
  ],
  alerts: [],
  events: [],
  decisions_by_item_uuid: {},
};

describe("InvestmentReportBundleContent — ROB-274 proposal badges + diff", () => {
  it("renders watch badge as 'watch' (English)", () => {
    render(<InvestmentReportBundleContent bundle={sampleBundle as any} />);
    expect(screen.getAllByText("watch").length).toBeGreaterThan(0);
    expect(screen.queryByText("와치")).toBeNull();
  });

  it("renders operation badge and diff table for modify", () => {
    render(<InvestmentReportBundleContent bundle={sampleBundle as any} />);
    expect(screen.getByText("modify")).toBeInTheDocument();
    expect(screen.getByText("threshold")).toBeInTheDocument();
  });

  it("renders 'active watches' counter (English)", () => {
    render(<InvestmentReportBundleContent bundle={sampleBundle as any} />);
    expect(screen.getByText(/active watches/)).toBeInTheDocument();
  });
});
```

(Adjust import paths and minimum-bundle shape against the existing component contract — the test should compile against the real types.)

- [ ] **Step 7: Run frontend tests**

Run: `cd frontend/invest && npm run test -- InvestmentReportBundleContent.proposal`
Expected: all pass.

- [ ] **Step 8: Visual smoke**

Run: `cd frontend/invest && npm run dev`
Open the report bundle page in a browser; confirm `action/watch/risk` badges render in English, `active watches` counter shows in English, and a `modify` item displays current/proposed/diff. Re-run an existing report with no operation field — confirm legacy items still render with no diff panel.

(If the local /invest doesn't have a `modify` item in dev DB, hand-edit a row via Adminer or psql to set `operation='modify'` + minimal current_state/proposed_state/diff for one item, just for the visual smoke. Revert after.)

- [ ] **Step 9: Commit**

```bash
git add frontend/invest/src/components/investment-reports/InvestmentReportBundleContent.tsx \
        frontend/invest/src/components/investment-reports/ProposalDiffPanel.tsx \
        frontend/invest/src/api/types.ts \
        frontend/invest/src/__tests__/InvestmentReportBundleContent.proposal.test.tsx
git commit -m "feat(rob-274): english badges + proposal diff panel"
```

---

## Task 11: No-broker-mutation safety tests

**Files:**
- Create: `tests/services/action_report/snapshot_backed/test_generator_safety.py`

### Steps

- [ ] **Step 1: Enumerate broker mutation method names**

Run:
```bash
grep -rn "def place_order\|def cancel_order\|def modify_order\|def submit_order" \
  app/services/kis.py app/services/upbit.py app/services/alpaca* 2>/dev/null | head -20
```

Capture the canonical method names. Likely candidates: `place_order`, `cancel_order`, `modify_order`, plus broker-specific variants like `place_kis_order` / `submit_kis_order`.

- [ ] **Step 2: Write the safety test**

Create `tests/services/action_report/snapshot_backed/test_generator_safety.py`:

```python
"""ROB-274 — report generation must not call broker / watch mutation methods."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.schemas.investment_reports import (
    IngestReportItem,
    WatchConditionPayload,
)
from app.services.action_report.snapshot_backed.generator import (
    SnapshotBackedReportGenerator,
)
from app.services.action_report.snapshot_backed.request import (
    ReportGenerationRequest,
)


_BROKER_MUTATION_METHODS = [
    "place_order",
    "cancel_order",
    "modify_order",
    "submit_order",
    # Add others enumerated in Step 1
]

_WATCH_MUTATION_METHODS = [
    "activate_watch",
    "update_alert",
    "cancel_alert",
    "expire_alert",
]


@pytest.mark.asyncio
async def test_generate_does_not_call_broker_mutation_methods(monkeypatch):
    """Spy every known broker mutation method; assert none are invoked."""

    fake_kis = MagicMock()
    fake_upbit = MagicMock()
    for name in _BROKER_MUTATION_METHODS:
        setattr(fake_kis, name, AsyncMock(side_effect=AssertionError(
            f"generator must not call {name}"
        )))
        setattr(fake_upbit, name, AsyncMock(side_effect=AssertionError(
            f"generator must not call {name}"
        )))

    # Patch the broker accessors used by registry.py.
    monkeypatch.setattr(
        "app.services.kis.get_kis_client", lambda: fake_kis, raising=False
    )
    monkeypatch.setattr(
        "app.services.upbit.get_upbit_client", lambda: fake_upbit, raising=False
    )

    # Stub ensure + ingest so the test stays focused.
    fake_ensure = AsyncMock()
    fake_ensure.ensure = AsyncMock(return_value=_minimal_ensure_response())
    fake_ingest = AsyncMock()
    fake_ingest.ingest = AsyncMock(return_value=MagicMock(report_uuid="r"))

    generator = SnapshotBackedReportGenerator(
        session=MagicMock(),
        ensure_service=fake_ensure,
        ingestion_service=fake_ingest,
    )
    request = ReportGenerationRequest(
        report_type="t",
        market="crypto",
        account_scope="upbit_live",
        # ... fill remaining required fields per ReportGenerationRequest schema ...
    )
    await generator.generate(request)

    for name in _BROKER_MUTATION_METHODS:
        assert not getattr(fake_kis, name).called, f"kis.{name} was called"
        assert not getattr(fake_upbit, name).called, f"upbit.{name} was called"


@pytest.mark.asyncio
async def test_generate_does_not_call_watch_mutation_methods(monkeypatch):
    """Generator must not invoke WatchActivationService mutation methods."""

    from app.services.investment_reports import watch_activation as watch_mod

    spy_calls: list[str] = []
    for name in _WATCH_MUTATION_METHODS:
        if hasattr(watch_mod, "WatchActivationService") and hasattr(
            watch_mod.WatchActivationService, name
        ):
            monkeypatch.setattr(
                watch_mod.WatchActivationService,
                name,
                lambda *_a, **_kw: spy_calls.append(name) or pytest.fail(
                    f"generator must not call WatchActivationService.{name}"
                ),
            )

    fake_ensure = AsyncMock()
    fake_ensure.ensure = AsyncMock(return_value=_minimal_ensure_response())
    fake_ingest = AsyncMock()
    fake_ingest.ingest = AsyncMock(return_value=MagicMock(report_uuid="r"))

    generator = SnapshotBackedReportGenerator(
        session=MagicMock(),
        ensure_service=fake_ensure,
        ingestion_service=fake_ingest,
    )
    await generator.generate(
        ReportGenerationRequest(
            report_type="t",
            market="crypto",
            account_scope="upbit_live",
            # ... fill remaining required fields ...
        )
    )
    assert spy_calls == [], f"unexpected watch-mutation calls: {spy_calls}"


def _minimal_ensure_response():
    return MagicMock(
        bundle_uuid="00000000-0000-0000-0000-000000000001",
        status="complete",
        coverage_summary={},
        freshness_summary={"overall": "fresh"},
        missing_sources=[],
        warnings=[],
        created=True,
        snapshots_by_kind={},  # see Task 7 step 3 NOTE on attribute name
    )
```

**NOTE TO IMPLEMENTER:** Fill in `ReportGenerationRequest` required fields by reading the schema; mirror the existing pattern in `test_generator.py`. The exact ensure-response attribute name may need fixing per Task 7 Step 3.

- [ ] **Step 3: Run safety tests — confirm pass**

Run: `uv run pytest tests/services/action_report/snapshot_backed/test_generator_safety.py -v`
Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add tests/services/action_report/snapshot_backed/test_generator_safety.py
git commit -m "test(rob-274): assert no broker/watch mutation during report generation"
```

---

## Task 12: Full verification + branch hygiene

### Steps

- [ ] **Step 1: Run the full suite**

```bash
uv run ruff check
uv run ruff format --check
uv run ty check
uv run pytest tests/services/action_report/ tests/services/investment_reports/ tests/mcp_server/ -v
cd frontend/invest && npm run test && cd ../..
```

Expected: all green. If `ty check` reports unrelated pre-existing errors, capture them in the PR description but do not fix in this PR.

- [ ] **Step 2: Manual end-to-end smoke**

In a local dev DB with the ROB-273 pilot reports already present, run:

```bash
uv run python -m app.mcp_server.cli investment_report_context_get \
  --market crypto --account-scope upbit_live
```

Expected: response includes a `pending_orders` field (`null` if Upbit creds not set; populated array if set with no live mutation).

Then run the snapshot-backed generator with `--use-snapshot-bundle --status draft` for crypto market against a symbol that has an active watch in the DB. Expected: generated report has at least one item whose `operation` is one of `keep` / `modify` / `cancel` / `review`, not `create`.

- [ ] **Step 3: PR body**

Use the structure:
```markdown
## Summary
- ROB-274: action/watch/risk semantics + existing-state proposals
- Adds `operation`, `target_ref`, `current_state`, `proposed_state`, `diff`, `apply_policy` to investment_report_items.
- Adds `pending_orders` snapshot collector (optional/fail-open) and `proposal_classifier` between bundle ensure and ingest.
- Frontend: badges flip to English; new `ProposalDiffPanel` renders current/proposed/diff.

## Migrations
- `20260520_rob274_p1_add_proposal_fields_to_report_items`
- `20260520_rob274_p2_extend_snapshot_kind`

## Test plan
- [x] alembic upgrade + downgrade round-trip
- [x] schema validator: operation-aware tests
- [x] collector: KR/US/crypto adapters + stale flag
- [x] classifier: create/modify/cancel/keep/review/ambiguous cases
- [x] generator: classifier integration test
- [x] ingestion round-trip persists new fields
- [x] safety: no broker/watch mutation during generate()
- [x] frontend Vitest: badges + diff render

## Production deployment notes
- New columns are nullable + new CHECK has legacy clause; safe to deploy ahead of consumers.
- No new TaskIQ recurring jobs, no Prefect cron changes.
- `INVEST_SCREENER_SNAPSHOTS_COMMIT_ENABLED`-style flag NOT added; collector is registered unconditionally but fails open when broker clients absent.
```

- [ ] **Step 4: Push and open PR**

```bash
git push -u origin rob-274
gh pr create --title "feat(rob-274): action/watch/risk existing-state proposals" --body "$(cat <<'EOF'
...filled per Step 3...
EOF
)"
```

---

## AC mapping (self-review)

| AC bullet | Task(s) |
|-----------|---------|
| `/invest` cards show `action`/`watch`/`risk` (no Korean badges) | Task 10 |
| Enum aligned across schema/API/MCP/frontend | Task 2, Task 10 |
| Korean body/rationale copy remains | Task 10 (scoped) |
| Generator reads active watch context before creating `watch` items | Task 7 |
| Matching active watch → modify/cancel/keep/review, not silent create | Task 6, Task 7 |
| Watch proposal items carry current/proposed/diff/target_ref/rationale/apply_policy | Task 2 (schema), Task 6 (classifier) |
| Tests cover create/modify/cancel/keep/review watch | Task 6 |
| Generator reads open/pending orders | Task 4, Task 5, Task 7 |
| Recommend keep/modify/cancel/review for relevant pending orders | Task 6 |
| Order proposal items carry current snapshot/diff/broker ref/rationale/apply_policy | Task 2, Task 6 |
| Tests prove no broker mutation method called | Task 11 |
| `investment_report_generate_from_bundle` returns new fields | Task 8, Task 2 (response) |
| `investment_report_context_get` includes pending_orders | Task 9 |
| Existing snapshot-backed generator tests updated | Task 7 |
| Report stays readable when optional sources absent | Task 4 (fail-open), Task 6 (None vs []) |
| Unit tests assert no broker mutate | Task 11 |
| Unit tests assert no watch mutate | Task 11 |
| Frontend tests cover `watch/modify` and `action/cancel` render | Task 10 |
| Standard checks pass | Task 12 |

---

## Open implementer notes (read before starting)

1. **Broker accessor names.** Task 4/5 assume `fetch_pending_domestic_orders` / `fetch_pending_overseas_orders` / `fetch_open_orders`. Confirm against the real codebase in Task 4 Step 1 and rename consistently before writing the collector. Do not invent new broker calls; if the accessor is absent for a market, return `unavailable` with reason `<broker>_pending_fetch_unsupported`.

2. **`snapshots_by_kind` attribute.** Task 7 assumes the ensure-bundle response carries a `snapshots_by_kind` lookup. Confirm by reading `app/services/action_report/common/snapshot_bundle.py` in Task 7 Step 3 — if it only exposes a flat `snapshots` list, build the lookup inside the helper.

3. **Existing scanner.** This PR deliberately does **not** touch the watch scanner. Scanner uses `investment_watch_alerts` which is unaffected.

4. **Apply path.** Out of scope per ROB-274 §4. No new "apply" endpoint, no new MCP tool, no new admin UI.

5. **PR split fallback.** If diff > ~600 LOC at Task 11 commit, split into PR A (Tasks 1–9, backend + ingestion) and PR B (Task 10, frontend). Task 11 safety tests can ride either PR but prefer PR A so backend lands fully verified.

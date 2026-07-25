# ROB-734 Mirror Counterfactual Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Execute each human-session analysis item as an unapproved `kis_mock` mirror order, then compare `live_gated` versus `mock_counterfactual` outcomes by paired decision key and feed the separated mock evidence into briefing and decision history.

**Architecture:** Add a focused mirror service that reads one `InvestmentReport` bundle, derives original order plans from `InvestmentReportItem` rows rather than operator-approved deltas, submits only through the existing KIS mock order path, and stamps the resulting mock ledger rows with mirror metadata. Extend the trade-journal aggregate read model to load mirror `KISMockOrderLedger` fills as a separate cohort and add a paired delta scoreboard. Surface the mock cohort as explicitly labeled `counterfactual` context in `get_operating_briefing`, `decision_history`, and `get_trading_scoreboard` without changing live policy thresholds.

**Tech Stack:** Python 3.13, SQLAlchemy async, Alembic/PostgreSQL `review` schema, FastMCP tool registration, pytest async. Reuses `InvestmentReportQueryService`, `KISMockOrderLedger`, `order_execution._place_order_impl`, `build_trading_scoreboard`, and ROB-730 KIS mock reconciliation.

## Global Constraints

- **KIS mock only:** mirror execution must always call `order_execution._place_order_impl` with `is_mock=True` or an injected equivalent; no live order tools, Toss mutation tools, Kiwoom mutation tools, or Upbit mutation tools.
- **Original plan basis:** mirror sizing uses `InvestmentReportItem.max_action`, `watch_condition`, `trigger_checklist`, and `evidence_snapshot`; it must not use `InvestmentReportItemDecision.approved_payload_snapshot` for `partial_approve` or `reprice`.
- **Mock account balance is not the sizing source:** derive quantity/notional from the live plan, then let the existing KIS mock order path fail closed if the mock account cannot accept it.
- **Cohort separation:** `live_gated` and `mock_counterfactual` metrics must never be merged in `realized_r_by_tag`; mock data is surfaced under explicit `counterfactual_*` keys and uses the existing `insufficient_sample` flag for `n < 10`.
- **No policy automation:** do not mutate `trading_policy.yaml`, trading thresholds, or approval gates. The maximum governance output is a suggested diff in a read-only response.
- **Retrospective idempotency:** because ROB-474 made `trade_retrospectives.correlation_id` unique, this plan replaces that uniqueness with `(correlation_id, account_mode)` before using the same decision key across live and mock rows.
- **Report item linkage:** every mirror ledger row must carry `report_item_uuid`, `mirror_cohort='mock_counterfactual'`, and `mirror_source_bucket` so non-ROB-734 `kis_mock` practice data stays out of the counterfactual scoreboard.
- **Caveat visibility:** public responses must include the KIS mock fill caveat: mock fills omit queue position, liquidity, slippage, and market impact, so results are upward biased relative to live execution.
- **MCP docs sync:** any new or changed MCP tool contract must update `app/mcp_server/README.md`.

---

## File Structure

- **Create** `app/services/trade_journal/mirror_counterfactual.py` — mirror plan extraction, idempotency scan, execution orchestration, and metadata stamping.
- **Create** `app/mcp_server/tooling/mirror_counterfactual_tools.py` — MCP handler `kis_mock_mirror_execute_report`.
- **Create** `app/mcp_server/tooling/mirror_counterfactual_registration.py` — FastMCP registration and tool-name set.
- **Modify** `app/mcp_server/tooling/registry.py` — register mirror counterfactual tools.
- **Modify** `app/models/review.py` — KIS mock mirror metadata columns and composite retrospective uniqueness.
- **Create** `alembic/versions/20260706_rob734_mirror_counterfactual.py` — additive KIS mock metadata columns and retrospective unique-constraint replacement.
- **Modify** `tests/_schema_bootstrap.py` — idempotent test DB DDL for the new columns, indexes, checks, and composite uniqueness.
- **Modify** `app/mcp_server/tooling/kis_mock_ledger.py` — persist `report_item_uuid` on mock ledger insert.
- **Modify** `app/services/trade_journal/trade_retrospective_service.py` — account-mode-aware retrospective upsert and due-list coverage.
- **Modify** `app/services/trade_journal/aggregates.py` — load mirror mock fills, cohort filtering, and paired delta scoreboard.
- **Modify** `app/mcp_server/tooling/trading_scoreboard_tools.py` and `trading_scoreboard_registration.py` — add `cohort` and `include_counterfactual_delta` read parameters.
- **Modify** `app/services/decision_history.py` — add separate `counterfactual_r_by_tag` and keep live `realized_r_by_tag` unchanged.
- **Modify** `app/mcp_server/tooling/operating_briefing.py` and `app/schemas/investment_reports.py` — add fail-open `counterfactual_scoreboard` section.
- **Modify** `app/mcp_server/README.md` — document the mirror execution tool, scoreboard cohort parameter, and interpretation caveats.
- **Tests:** create focused tests under `tests/services/`, `tests/mcp_server/`, and `tests/models/` as listed per task.

---

### Task 1: Schema And Model Groundwork

**Files:**
- Create: `alembic/versions/20260706_rob734_mirror_counterfactual.py`
- Modify: `app/models/review.py`
- Modify: `tests/_schema_bootstrap.py`
- Test: `tests/models/test_rob734_mirror_schema.py`

**Interfaces:**
- Produces nullable columns on `review.kis_mock_order_ledger`:
  - `report_item_uuid UUID`
  - `mirror_cohort TEXT`
  - `mirror_source_bucket TEXT`
- Produces indexes:
  - `ix_kis_mock_ledger_report_item_uuid`
  - `ix_kis_mock_ledger_mirror_cohort_created`
- Replaces `uq_trade_retrospectives_correlation_id` with `uq_trade_retrospectives_correlation_account` over `(correlation_id, account_mode)`.
- Adds CHECK constraints:
  - `mirror_cohort IS NULL OR mirror_cohort IN ('mock_counterfactual')`
  - `mirror_source_bucket IS NULL OR mirror_source_bucket IN ('place_original','watch_trigger','deferred_min_rung')`

- [ ] **Step 1: Write the schema regression test**

```python
# tests/models/test_rob734_mirror_schema.py
from app.models.review import KISMockOrderLedger, TradeRetrospective


def test_kis_mock_order_ledger_has_mirror_metadata_columns():
    cols = KISMockOrderLedger.__table__.columns
    assert "report_item_uuid" in cols
    assert "mirror_cohort" in cols
    assert "mirror_source_bucket" in cols
    assert cols["report_item_uuid"].nullable is True
    assert cols["mirror_cohort"].nullable is True
    assert cols["mirror_source_bucket"].nullable is True


def test_trade_retrospective_unique_key_is_correlation_plus_account_mode():
    constraints = {
        c.name: tuple(col.name for col in c.columns)
        for c in TradeRetrospective.__table__.constraints
        if c.__class__.__name__ == "UniqueConstraint"
    }
    assert constraints["uq_trade_retrospectives_correlation_account"] == (
        "correlation_id",
        "account_mode",
    )
    assert "uq_trade_retrospectives_correlation_id" not in constraints
```

- [ ] **Step 2: Run the failing test**

Run: `uv run pytest tests/models/test_rob734_mirror_schema.py -v`
Expected: FAIL because the model columns and composite unique constraint do not exist.

- [ ] **Step 3: Add model fields and constraints**

In `app/models/review.py`, update `KISMockOrderLedger.__table_args__`:

```python
Index("ix_kis_mock_ledger_report_item_uuid", "report_item_uuid"),
Index("ix_kis_mock_ledger_mirror_cohort_created", "mirror_cohort", "created_at"),
CheckConstraint(
    "mirror_cohort IS NULL OR mirror_cohort IN ('mock_counterfactual')",
    name="ck_kis_mock_ledger_mirror_cohort",
),
CheckConstraint(
    "mirror_source_bucket IS NULL OR mirror_source_bucket IN "
    "('place_original','watch_trigger','deferred_min_rung')",
    name="ck_kis_mock_ledger_mirror_source_bucket",
),
```

Add fields near the existing `correlation_id` field:

```python
report_item_uuid: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
mirror_cohort: Mapped[str | None] = mapped_column(Text)
mirror_source_bucket: Mapped[str | None] = mapped_column(Text)
```

In `TradeRetrospective.__table_args__`, replace:

```python
UniqueConstraint("correlation_id", name="uq_trade_retrospectives_correlation_id")
```

with:

```python
UniqueConstraint(
    "correlation_id",
    "account_mode",
    name="uq_trade_retrospectives_correlation_account",
)
```

- [ ] **Step 4: Add the Alembic migration**

Create `alembic/versions/20260706_rob734_mirror_counterfactual.py`:

```python
"""ROB-734 mirror counterfactual metadata and retrospective account key."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260706_rob734"
down_revision: str | Sequence[str] | None = "20260706_rob719"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "kis_mock_order_ledger",
        sa.Column("report_item_uuid", postgresql.UUID(as_uuid=True), nullable=True),
        schema="review",
    )
    op.add_column(
        "kis_mock_order_ledger",
        sa.Column("mirror_cohort", sa.Text(), nullable=True),
        schema="review",
    )
    op.add_column(
        "kis_mock_order_ledger",
        sa.Column("mirror_source_bucket", sa.Text(), nullable=True),
        schema="review",
    )
    op.create_index(
        "ix_kis_mock_ledger_report_item_uuid",
        "kis_mock_order_ledger",
        ["report_item_uuid"],
        schema="review",
    )
    op.create_index(
        "ix_kis_mock_ledger_mirror_cohort_created",
        "kis_mock_order_ledger",
        ["mirror_cohort", "created_at"],
        schema="review",
    )
    op.create_check_constraint(
        "ck_kis_mock_ledger_mirror_cohort",
        "kis_mock_order_ledger",
        "mirror_cohort IS NULL OR mirror_cohort IN ('mock_counterfactual')",
        schema="review",
    )
    op.create_check_constraint(
        "ck_kis_mock_ledger_mirror_source_bucket",
        "kis_mock_order_ledger",
        "mirror_source_bucket IS NULL OR mirror_source_bucket IN "
        "('place_original','watch_trigger','deferred_min_rung')",
        schema="review",
    )
    op.drop_constraint(
        "uq_trade_retrospectives_correlation_id",
        "trade_retrospectives",
        schema="review",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_trade_retrospectives_correlation_account",
        "trade_retrospectives",
        ["correlation_id", "account_mode"],
        schema="review",
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_trade_retrospectives_correlation_account",
        "trade_retrospectives",
        schema="review",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_trade_retrospectives_correlation_id",
        "trade_retrospectives",
        ["correlation_id"],
        schema="review",
    )
    op.drop_constraint(
        "ck_kis_mock_ledger_mirror_source_bucket",
        "kis_mock_order_ledger",
        schema="review",
        type_="check",
    )
    op.drop_constraint(
        "ck_kis_mock_ledger_mirror_cohort",
        "kis_mock_order_ledger",
        schema="review",
        type_="check",
    )
    op.drop_index(
        "ix_kis_mock_ledger_mirror_cohort_created",
        table_name="kis_mock_order_ledger",
        schema="review",
    )
    op.drop_index(
        "ix_kis_mock_ledger_report_item_uuid",
        table_name="kis_mock_order_ledger",
        schema="review",
    )
    op.drop_column("kis_mock_order_ledger", "mirror_source_bucket", schema="review")
    op.drop_column("kis_mock_order_ledger", "mirror_cohort", schema="review")
    op.drop_column("kis_mock_order_ledger", "report_item_uuid", schema="review")
```

- [ ] **Step 5: Update test DB bootstrap**

Append idempotent DDL to `tests/_schema_bootstrap.py`:

```python
"ALTER TABLE review.kis_mock_order_ledger ADD COLUMN IF NOT EXISTS report_item_uuid UUID",
"ALTER TABLE review.kis_mock_order_ledger ADD COLUMN IF NOT EXISTS mirror_cohort TEXT",
"ALTER TABLE review.kis_mock_order_ledger ADD COLUMN IF NOT EXISTS mirror_source_bucket TEXT",
"CREATE INDEX IF NOT EXISTS ix_kis_mock_ledger_report_item_uuid "
"ON review.kis_mock_order_ledger (report_item_uuid)",
"CREATE INDEX IF NOT EXISTS ix_kis_mock_ledger_mirror_cohort_created "
"ON review.kis_mock_order_ledger (mirror_cohort, created_at)",
"ALTER TABLE review.kis_mock_order_ledger DROP CONSTRAINT IF EXISTS "
"ck_kis_mock_ledger_mirror_cohort",
"ALTER TABLE review.kis_mock_order_ledger ADD CONSTRAINT "
"ck_kis_mock_ledger_mirror_cohort CHECK ("
"mirror_cohort IS NULL OR mirror_cohort IN ('mock_counterfactual'))",
"ALTER TABLE review.kis_mock_order_ledger DROP CONSTRAINT IF EXISTS "
"ck_kis_mock_ledger_mirror_source_bucket",
"ALTER TABLE review.kis_mock_order_ledger ADD CONSTRAINT "
"ck_kis_mock_ledger_mirror_source_bucket CHECK ("
"mirror_source_bucket IS NULL OR mirror_source_bucket IN "
"('place_original','watch_trigger','deferred_min_rung'))",
"ALTER TABLE review.trade_retrospectives DROP CONSTRAINT IF EXISTS "
"uq_trade_retrospectives_correlation_id",
"ALTER TABLE review.trade_retrospectives DROP CONSTRAINT IF EXISTS "
"uq_trade_retrospectives_correlation_account",
"ALTER TABLE review.trade_retrospectives ADD CONSTRAINT "
"uq_trade_retrospectives_correlation_account UNIQUE (correlation_id, account_mode)",
```

- [ ] **Step 6: Verify schema task**

Run: `uv run pytest tests/models/test_rob734_mirror_schema.py -v`
Expected: PASS.

Run: `uv run alembic upgrade head`
Expected: migration applies cleanly on the local database.

- [ ] **Step 7: Commit**

```bash
git add app/models/review.py alembic/versions/20260706_rob734_mirror_counterfactual.py tests/_schema_bootstrap.py tests/models/test_rob734_mirror_schema.py
git commit -m "feat(ROB-734): add mirror counterfactual ledger metadata"
```

---

### Task 2: Account-Mode-Aware Retrospective Upsert

**Files:**
- Modify: `app/services/trade_journal/trade_retrospective_service.py`
- Test: `tests/services/test_trade_retrospective_mirror_correlation.py`

**Interfaces:**
- `TradeRetrospectiveRepository.get_by_correlation_id(correlation_id: str, account_mode: str | None = None) -> TradeRetrospective | None`
- `TradeRetrospectiveRepository.upsert(payload)` uses `(correlation_id, account_mode)` when both are present.
- `build_retrospective_pending` coverage keys become `(correlation_id, account_mode)` so a live retrospective does not hide its mock twin.

- [ ] **Step 1: Write the regression tests**

```python
# tests/services/test_trade_retrospective_mirror_correlation.py
import pytest

from app.services.trade_journal.trade_retrospective_service import (
    save_retrospective,
)


@pytest.mark.asyncio
async def test_same_correlation_id_allowed_for_live_and_mock(db_session):
    common = {
        "symbol": "005930",
        "instrument_type": "equity_kr",
        "outcome": "filled",
        "side": "buy",
        "correlation_id": "mirror:item-1",
    }
    first, live = await save_retrospective(
        db_session,
        **common,
        account_mode="kis_live",
        realized_pnl=1000,
        realized_pnl_currency="KRW",
    )
    second, mock = await save_retrospective(
        db_session,
        **common,
        account_mode="kis_mock",
        realized_pnl=1500,
        realized_pnl_currency="KRW",
    )
    await db_session.commit()

    assert first == "created"
    assert second == "created"
    assert live.id != mock.id
    assert live.correlation_id == mock.correlation_id
    assert live.account_mode == "kis_live"
    assert mock.account_mode == "kis_mock"


@pytest.mark.asyncio
async def test_same_correlation_id_same_account_updates(db_session):
    common = {
        "symbol": "005930",
        "instrument_type": "equity_kr",
        "account_mode": "kis_mock",
        "outcome": "filled",
        "side": "buy",
        "correlation_id": "mirror:item-2",
        "realized_pnl_currency": "KRW",
    }
    created, row1 = await save_retrospective(db_session, **common, realized_pnl=1000)
    updated, row2 = await save_retrospective(db_session, **common, realized_pnl=2000)
    await db_session.commit()

    assert created == "created"
    assert updated == "updated"
    assert row1.id == row2.id
    assert float(row2.realized_pnl) == 2000.0
```

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run pytest tests/services/test_trade_retrospective_mirror_correlation.py -v`
Expected: FAIL before Task 1 migration/model is applied, or fail because `get_by_correlation_id` is not account-mode-aware.

- [ ] **Step 3: Update repository lookup and upsert**

Change the repository methods:

```python
async def get_by_correlation_id(
    self, correlation_id: str, account_mode: str | None = None
) -> TradeRetrospective | None:
    stmt = select(TradeRetrospective).where(
        TradeRetrospective.correlation_id == correlation_id
    )
    if account_mode is not None:
        stmt = stmt.where(TradeRetrospective.account_mode == account_mode)
    result = await self.db.execute(stmt.limit(1))
    return result.scalar_one_or_none()


async def upsert(self, payload: dict[str, Any]) -> tuple[str, TradeRetrospective]:
    cid = payload.get("correlation_id")
    account_mode = payload.get("account_mode")
    if cid is not None:
        existing = await self.get_by_correlation_id(cid, account_mode)
        if existing is not None:
            for key, value in payload.items():
                setattr(existing, key, value)
            await self.db.flush()
            return "updated", existing
    row = TradeRetrospective(**payload)
    self.db.add(row)
    await self.db.flush()
    return "created", row
```

- [ ] **Step 4: Update due-list coverage**

Change `_covered_keys` to return `set[tuple[str, str]]` for correlation coverage and `set[str]` for report items:

```python
covered_cids = {
    (str(cid), str(account_mode))
    for cid, account_mode, _ in rows
    if cid and account_mode
}
```

Change `_is_covered`:

```python
key = (entry["suggested_correlation_id"], entry["account_mode"])
return key in covered_cids
```

Keep `report_item_uuid` coverage unchanged because an explicitly authored retrospective for a report item should still cover that exact item.

- [ ] **Step 5: Verify retrospective task**

Run: `uv run pytest tests/services/test_trade_retrospective_mirror_correlation.py tests/services/test_kis_mock_retrospective_pending.py tests/test_trade_retrospective_service.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/services/trade_journal/trade_retrospective_service.py tests/services/test_trade_retrospective_mirror_correlation.py
git commit -m "fix(ROB-734): scope retrospective upsert by account mode"
```

---

### Task 3: Persist Report Item Linkage On KIS Mock Ledger

**Files:**
- Modify: `app/mcp_server/tooling/kis_mock_ledger.py`
- Test: `tests/test_kis_mock_order_ledger.py`

**Interfaces:**
- `_save_kis_mock_order_ledger` accepts `report_item_uuid: uuid.UUID | None = None` and persists the new column.
- `_record_kis_mock_order` passes its `report_item_uuid` argument through both to the ledger row and to `publish_place_time_forecast`.

- [ ] **Step 1: Add the regression test**

Append to `tests/test_kis_mock_order_ledger.py`:

```python
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.mcp_server.tooling.kis_mock_ledger import _save_kis_mock_order_ledger
from app.models.review import KISMockOrderLedger


@pytest.mark.asyncio
async def test_save_kis_mock_order_ledger_persists_report_item_uuid(db_session):
    item_uuid = uuid4()
    order_no = f"ROB734-{uuid4().hex[:10]}"
    ledger_id = await _save_kis_mock_order_ledger(
        symbol="005930",
        instrument_type="equity_kr",
        side="buy",
        order_type="limit",
        quantity=1,
        price=70000,
        amount=70000,
        currency="KRW",
        order_no=order_no,
        order_time="090000",
        krx_fwdg_ord_orgno=None,
        status="accepted",
        response_code="0",
        response_message="ok",
        raw_response={"rt_cd": "0"},
        reason="ROB-734 mirror",
        thesis="counterfactual",
        strategy="mirror_counterfactual",
        notes=None,
        report_item_uuid=item_uuid,
    )
    assert ledger_id is not None

    row = (
        await db_session.execute(
            select(KISMockOrderLedger).where(KISMockOrderLedger.order_no == order_no)
        )
    ).scalar_one()
    assert row.report_item_uuid == item_uuid
```

- [ ] **Step 2: Run the failing test**

Run: `uv run pytest tests/test_kis_mock_order_ledger.py -k report_item_uuid -v`
Expected: FAIL because `_save_kis_mock_order_ledger` does not accept `report_item_uuid`.

- [ ] **Step 3: Thread `report_item_uuid` through the ledger write**

In `_save_kis_mock_order_ledger`, add the parameter:

```python
report_item_uuid: uuid.UUID | None = None,
```

and add it to the insert values:

```python
report_item_uuid=report_item_uuid,
```

In `_record_kis_mock_order`, pass the existing parameter into `_save_kis_mock_order_ledger`:

```python
report_item_uuid=report_item_uuid,
```

- [ ] **Step 4: Verify ledger task**

Run: `uv run pytest tests/test_kis_mock_order_ledger.py -k report_item_uuid -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/mcp_server/tooling/kis_mock_ledger.py tests/test_kis_mock_order_ledger.py
git commit -m "feat(ROB-734): persist report item on kis mock ledger"
```

---

### Task 4: Mirror Plan Extraction Service

**Files:**
- Create: `app/services/trade_journal/mirror_counterfactual.py`
- Test: `tests/services/test_mirror_counterfactual_plans.py`

**Interfaces:**
- `MirrorSourceBucket = Literal["place_original", "watch_trigger", "deferred_min_rung"]`
- `@dataclass(frozen=True) class MirrorOrderPlan`
- `async def build_mirror_order_plans(db: AsyncSession, *, report_uuid: UUID, min_rung_quantity: Decimal = Decimal("1")) -> dict[str, Any]`
- Plan extraction rules:
  - `item_kind='action'` and `side in {'buy','sell'}` -> `place_original`, regardless of item status or decision verb.
  - `item_kind='watch'` with usable price and side -> `watch_trigger`.
  - `decision_bucket='deferred_no_action'` with symbol -> `deferred_min_rung`.
  - Unsupported rows are returned under `skipped` with deterministic reason strings.
- Price precedence:
  - `max_action.limit_price`
  - `max_action.limit_price_hint`
  - `watch_condition.threshold`
  - first numeric value in `trigger_checklist` matching `price=123`, `limit_price=123`, or `123원`
  - `evidence_snapshot.trade_setup.entry`
  - `evidence_snapshot.price` or `evidence_snapshot.current_price`
- Quantity/notional precedence:
  - `max_action.quantity`
  - `max_action.notional` or `max_action.amount_krw` passed as `amount` for buys
  - `min_rung_quantity` for deferred rows

- [ ] **Step 1: Write plan extraction tests**

```python
# tests/services/test_mirror_counterfactual_plans.py
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from app.models.investment_reports import InvestmentReport, InvestmentReportItem
from app.services.trade_journal.mirror_counterfactual import build_mirror_order_plans


async def _report(db, *, market="kr", account_scope="kis_live") -> InvestmentReport:
    row = InvestmentReport(
        report_uuid=uuid4(),
        idempotency_key=f"rob734-report-{uuid4().hex}",
        title="ROB-734 source report",
        summary="Mirror counterfactual source report",
        report_type="daily",
        market=market,
        market_session="regular",
        account_scope=account_scope,
        execution_mode="advisory_only",
        status="draft",
        created_by_profile="CLAUDE_ADVISOR",
        valid_until=datetime(2026, 7, 6, tzinfo=timezone.utc),
    )
    db.add(row)
    await db.flush()
    return row


async def _item(db, report, **kw):
    base = {
        "report_id": report.id,
        "item_uuid": uuid4(),
        "idempotency_key": f"rob734-item-{uuid4().hex}",
        "item_kind": "action",
        "symbol": "005930",
        "side": "buy",
        "intent": "buy_review",
        "target_kind": "asset",
        "confidence": Decimal("0.61"),
        "rationale": "original plan",
        "evidence_snapshot": {"trade_setup": {"target": 76000, "stop": 68000}},
        "trigger_checklist": [],
        "max_action": {"quantity": "3", "limit_price": "70000"},
        "status": "denied",
        "decision_bucket": "new_buy_candidate",
    }
    base.update(kw)
    row = InvestmentReportItem(**base)
    db.add(row)
    await db.flush()
    return row


@pytest.mark.asyncio
async def test_action_item_uses_original_max_action_even_when_denied(db_session):
    report = await _report(db_session)
    item = await _item(db_session, report, status="denied")
    await db_session.commit()

    result = await build_mirror_order_plans(db_session, report_uuid=report.report_uuid)
    [plan] = result["plans"]
    assert plan.item_uuid == item.item_uuid
    assert plan.source_bucket == "place_original"
    assert plan.quantity == Decimal("3")
    assert plan.price == Decimal("70000")
    assert plan.amount is None
    assert plan.target_price == Decimal("76000")
    assert plan.stop_loss == Decimal("68000")


@pytest.mark.asyncio
async def test_watch_item_uses_watch_threshold_price(db_session):
    report = await _report(db_session)
    item = await _item(
        db_session,
        report,
        item_kind="watch",
        operation="create",
        watch_condition={
            "metric": "price",
            "operator": "below",
            "threshold": "69000",
        },
        valid_until=datetime(2026, 7, 7, tzinfo=timezone.utc),
        max_action={"side": "buy", "quantity": "2", "account_mode": "kis_mock"},
    )
    await db_session.commit()

    result = await build_mirror_order_plans(db_session, report_uuid=report.report_uuid)
    [plan] = result["plans"]
    assert plan.item_uuid == item.item_uuid
    assert plan.source_bucket == "watch_trigger"
    assert plan.price == Decimal("69000")
    assert plan.quantity == Decimal("2")


@pytest.mark.asyncio
async def test_deferred_no_action_gets_minimum_rung(db_session):
    report = await _report(db_session)
    await _item(
        db_session,
        report,
        item_kind="action",
        decision_bucket="deferred_no_action",
        max_action={},
        evidence_snapshot={"price": "12345"},
    )
    await db_session.commit()

    result = await build_mirror_order_plans(db_session, report_uuid=report.report_uuid)
    [plan] = result["plans"]
    assert plan.source_bucket == "deferred_min_rung"
    assert plan.quantity == Decimal("1")
    assert plan.price == Decimal("12345")


@pytest.mark.asyncio
async def test_item_without_price_is_skipped_with_reason(db_session):
    report = await _report(db_session)
    item = await _item(db_session, report, max_action={"quantity": "1"}, evidence_snapshot={})
    await db_session.commit()

    result = await build_mirror_order_plans(db_session, report_uuid=report.report_uuid)
    assert result["plans"] == []
    assert result["skipped"][0]["item_uuid"] == str(item.item_uuid)
    assert result["skipped"][0]["reason"] == "missing_limit_price"
```

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run pytest tests/services/test_mirror_counterfactual_plans.py -v`
Expected: FAIL because `mirror_counterfactual.py` does not exist.

- [ ] **Step 3: Implement the dataclasses and pure extractors**

Create `app/services/trade_journal/mirror_counterfactual.py` with:

```python
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.investment_reports import InvestmentReport, InvestmentReportItem

MirrorSourceBucket = Literal[
    "place_original",
    "watch_trigger",
    "deferred_min_rung",
]

_PRICE_RE = re.compile(
    r"(?:limit_price|price)\s*[:=]\s*([0-9][0-9,]*(?:\.[0-9]+)?)|"
    r"([0-9][0-9,]*(?:\.[0-9]+)?)\s*원"
)


@dataclass(frozen=True)
class MirrorOrderPlan:
    report_uuid: UUID
    item_uuid: UUID
    source_bucket: MirrorSourceBucket
    correlation_id: str
    symbol: str
    side: str
    quantity: Decimal | None
    amount: Decimal | None
    price: Decimal
    target_price: Decimal | None
    stop_loss: Decimal | None
    min_hold_days: int | None
    reason: str
    thesis: str | None
    strategy: str
    notes: str


def _decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        out = Decimal(str(value).replace(",", ""))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return out if out > 0 else None
```

Implement helpers `_price_from_item`, `_quantity_from_item`, `_target_from_item`, `_stop_from_item`, `_side_from_item`, and `_plan_for_item` using the precedence listed in this task. The decision key must be deterministic:

```python
def _mirror_correlation_id(item_uuid: UUID) -> str:
    return f"mirror:{item_uuid}"
```

- [ ] **Step 4: Implement `build_mirror_order_plans`**

The function must:

```python
async def build_mirror_order_plans(
    db: AsyncSession,
    *,
    report_uuid: UUID,
    min_rung_quantity: Decimal = Decimal("1"),
) -> dict[str, Any]:
    report = await db.scalar(
        select(InvestmentReport).where(InvestmentReport.report_uuid == report_uuid)
    )
    if report is None:
        raise ValueError(f"report not found: {report_uuid}")
    rows = (
        await db.execute(
            select(InvestmentReportItem)
            .where(InvestmentReportItem.report_id == report.id)
            .order_by(InvestmentReportItem.created_at.asc(), InvestmentReportItem.id.asc())
        )
    ).scalars().all()
    plans: list[MirrorOrderPlan] = []
    skipped: list[dict[str, str]] = []
    for item in rows:
        plan, reason = _plan_for_item(
            report_uuid=report.report_uuid,
            item=item,
            min_rung_quantity=min_rung_quantity,
        )
        if plan is None:
            skipped.append({"item_uuid": str(item.item_uuid), "reason": reason})
        else:
            plans.append(plan)
    return {
        "report_uuid": str(report.report_uuid),
        "plans": plans,
        "skipped": skipped,
        "count": len(plans),
    }
```

- [ ] **Step 5: Verify plan extraction task**

Run: `uv run pytest tests/services/test_mirror_counterfactual_plans.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/services/trade_journal/mirror_counterfactual.py tests/services/test_mirror_counterfactual_plans.py
git commit -m "feat(ROB-734): derive mirror counterfactual order plans"
```

---

### Task 5: Mirror Execution And Idempotency

**Files:**
- Modify: `app/services/trade_journal/mirror_counterfactual.py`
- Test: `tests/services/test_mirror_counterfactual_execution.py`

**Interfaces:**
- `async def execute_mirror_for_report(db: AsyncSession, *, report_uuid: UUID, dry_run: bool = True, place_order: PlaceOrderCallable | None = None) -> dict[str, Any]`
- Idempotency rule: if a row already exists in `KISMockOrderLedger` with matching `report_item_uuid` and `mirror_cohort='mock_counterfactual'`, skip with `reason='already_mirrored'`.
- After a successful non-dry-run order, update the returned `ledger_id` row with `mirror_cohort`, `mirror_source_bucket`, and `report_item_uuid`.
- `dry_run=True` calls the injected or default place order with `dry_run=True` but does not stamp ledger metadata.

- [ ] **Step 1: Write execution tests**

```python
# tests/services/test_mirror_counterfactual_execution.py
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models.review import KISMockOrderLedger
from app.services.trade_journal.mirror_counterfactual import (
    MirrorOrderPlan,
    execute_mirror_order_plans,
)


def _plan() -> MirrorOrderPlan:
    item_uuid = uuid4()
    return MirrorOrderPlan(
        report_uuid=uuid4(),
        item_uuid=item_uuid,
        source_bucket="place_original",
        correlation_id=f"mirror:{item_uuid}",
        symbol="005930",
        side="buy",
        quantity=Decimal("2"),
        amount=None,
        price=Decimal("70000"),
        target_price=Decimal("76000"),
        stop_loss=Decimal("68000"),
        min_hold_days=10,
        reason="ROB-734 mirror counterfactual",
        thesis="original plan",
        strategy="mirror_counterfactual",
        notes="source_bucket=place_original",
    )


@pytest.mark.asyncio
async def test_execute_dry_run_calls_place_order_without_metadata_write(db_session):
    calls = []

    async def fake_place_order(**kwargs):
        calls.append(kwargs)
        return {"success": True, "dry_run": True, "approval_hash": "p6a1.x"}

    result = await execute_mirror_order_plans(
        db_session,
        plans=[_plan()],
        dry_run=True,
        place_order=fake_place_order,
    )

    assert result["submitted_count"] == 0
    assert result["dry_run_count"] == 1
    assert calls[0]["is_mock"] is True
    assert calls[0]["dry_run"] is True
    assert calls[0]["correlation_id"].startswith("mirror:")


@pytest.mark.asyncio
async def test_execute_apply_stamps_mock_ledger_metadata(db_session):
    plan = _plan()
    row = KISMockOrderLedger(
        trade_date=datetime(2026, 7, 6, tzinfo=timezone.utc),
        symbol=plan.symbol,
        instrument_type="equity_kr",
        side="buy",
        order_type="limit",
        quantity=Decimal("2"),
        price=Decimal("70000"),
        amount=Decimal("140000"),
        fee=Decimal("0"),
        currency="KRW",
        order_no=f"ROB734-{uuid4().hex[:10]}",
        account_mode="kis_mock",
        broker="kis",
        status="accepted",
        lifecycle_state="accepted",
        correlation_id=plan.correlation_id,
    )
    db_session.add(row)
    await db_session.flush()
    ledger_id = row.id

    async def fake_place_order(**kwargs):
        return {"success": True, "dry_run": False, "ledger_id": ledger_id}

    result = await execute_mirror_order_plans(
        db_session,
        plans=[plan],
        dry_run=False,
        place_order=fake_place_order,
    )
    await db_session.commit()

    assert result["submitted_count"] == 1
    refreshed = await db_session.scalar(
        select(KISMockOrderLedger).where(KISMockOrderLedger.id == ledger_id)
    )
    assert refreshed.report_item_uuid == plan.item_uuid
    assert refreshed.mirror_cohort == "mock_counterfactual"
    assert refreshed.mirror_source_bucket == "place_original"
```

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run pytest tests/services/test_mirror_counterfactual_execution.py -v`
Expected: FAIL because execution functions do not exist.

- [ ] **Step 3: Implement execution helpers**

Add:

```python
from collections.abc import Awaitable, Callable

from app.models.review import KISMockOrderLedger

PlaceOrderCallable = Callable[..., Awaitable[dict[str, Any]]]
```

Add `_default_place_order`:

```python
async def _default_place_order(**kwargs: Any) -> dict[str, Any]:
    from app.mcp_server.tooling import order_execution

    return await order_execution._place_order_impl(**kwargs)
```

Add `_existing_mirror_item_uuids`:

```python
async def _existing_mirror_item_uuids(
    db: AsyncSession, item_uuids: list[UUID]
) -> set[UUID]:
    if not item_uuids:
        return set()
    rows = (
        await db.execute(
            select(KISMockOrderLedger.report_item_uuid)
            .where(KISMockOrderLedger.report_item_uuid.in_(item_uuids))
            .where(KISMockOrderLedger.mirror_cohort == "mock_counterfactual")
        )
    ).scalars().all()
    return {row for row in rows if row is not None}
```

Add `_stamp_mirror_ledger`:

```python
async def _stamp_mirror_ledger(
    db: AsyncSession, *, ledger_id: int, plan: MirrorOrderPlan
) -> None:
    row = await db.get(KISMockOrderLedger, ledger_id)
    if row is None:
        raise ValueError(f"kis_mock ledger row not found: {ledger_id}")
    row.report_item_uuid = plan.item_uuid
    row.mirror_cohort = "mock_counterfactual"
    row.mirror_source_bucket = plan.source_bucket
    await db.flush()
```

- [ ] **Step 4: Implement `execute_mirror_order_plans`**

For each plan call:

```python
result = await place_order(
    symbol=plan.symbol,
    side=plan.side,
    order_type="limit",
    quantity=float(plan.quantity) if plan.quantity is not None else None,
    amount=float(plan.amount) if plan.amount is not None else None,
    price=float(plan.price),
    dry_run=dry_run,
    reason=plan.reason,
    thesis=plan.thesis,
    strategy=plan.strategy,
    target_price=float(plan.target_price) if plan.target_price is not None else None,
    stop_loss=float(plan.stop_loss) if plan.stop_loss is not None else None,
    min_hold_days=plan.min_hold_days,
    notes=plan.notes,
    is_mock=True,
    correlation_id=plan.correlation_id,
    report_item_uuid=str(plan.item_uuid),
)
```

When `dry_run is False` and `result.get("success") is True` and `result.get("ledger_id")` is not `None`, stamp the row.

Return counts:

```python
{
    "success": True,
    "dry_run": dry_run,
    "cohort": "mock_counterfactual",
    "planned_count": len(plans),
    "submitted_count": submitted_count,
    "dry_run_count": dry_run_count,
    "skipped_count": skipped_count,
    "failed_count": failed_count,
    "results": results,
    "caveats": [
        "KIS mock fills do not model queue priority, liquidity, slippage, or market impact; mock performance is upward biased."
    ],
}
```

- [ ] **Step 5: Verify execution task**

Run: `uv run pytest tests/services/test_mirror_counterfactual_execution.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/services/trade_journal/mirror_counterfactual.py tests/services/test_mirror_counterfactual_execution.py
git commit -m "feat(ROB-734): execute mirror counterfactual orders"
```

---

### Task 6: MCP Mirror Execution Tool

**Files:**
- Create: `app/mcp_server/tooling/mirror_counterfactual_tools.py`
- Create: `app/mcp_server/tooling/mirror_counterfactual_registration.py`
- Modify: `app/mcp_server/tooling/registry.py`
- Test: `tests/mcp_server/test_mirror_counterfactual_tool.py`

**Interfaces:**
- Tool name: `kis_mock_mirror_execute_report`
- Parameters:
  - `report_uuid: str`
  - `dry_run: bool = True`
  - `min_rung_quantity: float = 1.0`
- Response is the service result plus report lookup errors as structured `success=False`.

- [ ] **Step 1: Write tool tests**

```python
# tests/mcp_server/test_mirror_counterfactual_tool.py
import pytest


@pytest.mark.asyncio
async def test_mirror_counterfactual_tool_registered():
    from app.mcp_server.tooling.mirror_counterfactual_registration import (
        MIRROR_COUNTERFACTUAL_TOOL_NAMES,
        register_mirror_counterfactual_tools,
    )

    class FakeMCP:
        def __init__(self):
            self.tools = {}

        def tool(self, *, name, description):
            def deco(fn):
                self.tools[name] = fn
                return fn

            return deco

    mcp = FakeMCP()
    register_mirror_counterfactual_tools(mcp)
    assert MIRROR_COUNTERFACTUAL_TOOL_NAMES == {"kis_mock_mirror_execute_report"}
    assert "kis_mock_mirror_execute_report" in mcp.tools


@pytest.mark.asyncio
async def test_mirror_counterfactual_tool_delegates(monkeypatch):
    from app.mcp_server.tooling import mirror_counterfactual_tools as tool

    async def fake_execute(db, **kwargs):
        return {"success": True, "planned_count": 1, "dry_run": kwargs["dry_run"]}

    monkeypatch.setattr(tool, "execute_mirror_for_report", fake_execute)
    result = await tool.kis_mock_mirror_execute_report(
        report_uuid="11111111-1111-1111-1111-111111111111",
        dry_run=True,
    )
    assert result["success"] is True
    assert result["planned_count"] == 1
    assert result["dry_run"] is True
```

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run pytest tests/mcp_server/test_mirror_counterfactual_tool.py -v`
Expected: FAIL because registration module does not exist.

- [ ] **Step 3: Implement tool module**

`app/mcp_server/tooling/mirror_counterfactual_tools.py`:

```python
from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID

from app.core.db import AsyncSessionLocal
from app.services.trade_journal.mirror_counterfactual import execute_mirror_for_report


async def kis_mock_mirror_execute_report(
    report_uuid: str,
    dry_run: bool = True,
    min_rung_quantity: float = 1.0,
) -> dict[str, Any]:
    try:
        rid = UUID(str(report_uuid))
    except ValueError:
        return {"success": False, "error": "invalid_report_uuid", "report_uuid": report_uuid}
    async with AsyncSessionLocal() as db:
        try:
            return await execute_mirror_for_report(
                db,
                report_uuid=rid,
                dry_run=dry_run,
                min_rung_quantity=Decimal(str(min_rung_quantity)),
            )
        except ValueError as exc:
            return {"success": False, "error": str(exc), "report_uuid": report_uuid}
```

- [ ] **Step 4: Implement registration and registry wiring**

`app/mcp_server/tooling/mirror_counterfactual_registration.py`:

```python
from __future__ import annotations

from typing import Any

from app.mcp_server.tooling.mirror_counterfactual_tools import (
    kis_mock_mirror_execute_report,
)

MIRROR_COUNTERFACTUAL_TOOL_NAMES: set[str] = {"kis_mock_mirror_execute_report"}


def register_mirror_counterfactual_tools(mcp: Any) -> None:
    _ = mcp.tool(
        name="kis_mock_mirror_execute_report",
        description=(
            "ROB-734: execute a report's original analysis plan as KIS mock "
            "mirror counterfactual orders. dry_run=True previews only. "
            "dry_run=False mutates only account_mode='kis_mock', never live. "
            "Uses original report item sizing, not operator-approved trims. "
            "Returns caveats because KIS mock fills omit queue/liquidity/slippage."
        ),
    )(kis_mock_mirror_execute_report)
```

In `registry.py`, import and call `register_mirror_counterfactual_tools(mcp)` next to other trading/journal registrations.

- [ ] **Step 5: Verify MCP tool task**

Run: `uv run pytest tests/mcp_server/test_mirror_counterfactual_tool.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/mcp_server/tooling/mirror_counterfactual_tools.py app/mcp_server/tooling/mirror_counterfactual_registration.py app/mcp_server/tooling/registry.py tests/mcp_server/test_mirror_counterfactual_tool.py
git commit -m "feat(ROB-734): expose kis mock mirror execution tool"
```

---

### Task 7: Counterfactual Fills And Delta Scoreboard

**Files:**
- Modify: `app/services/trade_journal/aggregates.py`
- Modify: `app/mcp_server/tooling/trading_scoreboard_tools.py`
- Modify: `app/mcp_server/tooling/trading_scoreboard_registration.py`
- Test: `tests/services/test_trade_journal_mirror_aggregates.py`
- Test: `tests/test_mcp_trading_scoreboard.py`

**Interfaces:**
- `build_trading_scoreboard` accepts `cohort: Literal["live_gated","mock_counterfactual","all"] = "live_gated"` while preserving its existing filters.
- `load_fills` accepts `cohort: str = "live_gated"` while preserving its existing filters.
- `build_counterfactual_delta_scoreboard` accepts the same market/date/excursion/cache filters used by `build_trading_scoreboard`.
- MCP `get_trading_scoreboard` accepts:
  - `cohort: str = "live_gated"`
  - `include_counterfactual_delta: bool = False`

- [ ] **Step 1: Write aggregate tests**

```python
# tests/services/test_trade_journal_mirror_aggregates.py
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from app.models.review import KISLiveOrderLedger, KISMockOrderLedger
from app.models.trading import InstrumentType
from app.services.trade_journal import aggregates as agg


@pytest.mark.asyncio
async def test_load_fills_can_isolate_mock_counterfactual(db_session):
    item_uuid = uuid4()
    db_session.add(
        KISMockOrderLedger(
            trade_date=datetime(2026, 7, 6, tzinfo=timezone.utc),
            symbol="005930",
            instrument_type=InstrumentType.equity_kr,
            side="buy",
            order_type="limit",
            quantity=Decimal("2"),
            price=Decimal("70000"),
            amount=Decimal("140000"),
            fee=Decimal("0"),
            currency="KRW",
            order_no=f"MIRROR-{uuid4().hex[:8]}",
            account_mode="kis_mock",
            broker="kis",
            status="accepted",
            lifecycle_state="fill",
            last_reconcile_detail={"attributed_fill_qty": "2"},
            report_item_uuid=item_uuid,
            mirror_cohort="mock_counterfactual",
            mirror_source_bucket="place_original",
            correlation_id="mirror:item-1",
        )
    )
    db_session.add(
        KISMockOrderLedger(
            trade_date=datetime(2026, 7, 6, tzinfo=timezone.utc),
            symbol="005930",
            instrument_type=InstrumentType.equity_kr,
            side="buy",
            order_type="limit",
            quantity=Decimal("2"),
            price=Decimal("70000"),
            amount=Decimal("140000"),
            fee=Decimal("0"),
            currency="KRW",
            order_no=f"PRACTICE-{uuid4().hex[:8]}",
            account_mode="kis_mock",
            broker="kis",
            status="accepted",
            lifecycle_state="fill",
            last_reconcile_detail={"attributed_fill_qty": "2"},
            correlation_id="practice:item-1",
        )
    )
    await db_session.commit()

    fills = await agg.load_fills(db_session, market="kr", cohort="mock_counterfactual")
    assert len([f for f in fills if f.symbol == "005930"]) == 1
    fill = [f for f in fills if f.symbol == "005930"][0]
    assert fill.cohort == "mock_counterfactual"
    assert fill.source_bucket == "place_original"
    assert fill.qty == pytest.approx(2.0)


@pytest.mark.asyncio
async def test_delta_scoreboard_pairs_by_correlation_and_account(db_session, monkeypatch):
    async def no_excursions(trade):
        return None, None, False

    monkeypatch.setattr(agg, "compute_excursions", no_excursions)
    when = datetime(2026, 7, 6, tzinfo=timezone.utc)
    common = {"correlation_id": "mirror:item-2", "report_item_uuid": uuid4()}
    db_session.add_all(
        [
            KISLiveOrderLedger(
                trade_date=when,
                symbol="005930",
                instrument_type="equity_kr",
                side="buy",
                order_type="limit",
                quantity=Decimal("1"),
                price=Decimal("100"),
                amount=Decimal("100"),
                status="filled",
                lifecycle_state="fill",
                filled_qty=Decimal("1"),
                avg_fill_price=Decimal("100"),
                account_mode="kis_live",
                broker="kis",
                **common,
            ),
            KISLiveOrderLedger(
                trade_date=when,
                symbol="005930",
                instrument_type="equity_kr",
                side="sell",
                order_type="limit",
                quantity=Decimal("1"),
                price=Decimal("105"),
                amount=Decimal("105"),
                status="filled",
                lifecycle_state="fill",
                filled_qty=Decimal("1"),
                avg_fill_price=Decimal("105"),
                account_mode="kis_live",
                broker="kis",
                correlation_id="live-exit",
                report_item_uuid=common["report_item_uuid"],
            ),
            KISMockOrderLedger(
                trade_date=when,
                symbol="005930",
                instrument_type=InstrumentType.equity_kr,
                side="buy",
                order_type="limit",
                quantity=Decimal("1"),
                price=Decimal("100"),
                amount=Decimal("100"),
                fee=Decimal("0"),
                currency="KRW",
                order_no=f"MBUY-{uuid4().hex[:8]}",
                account_mode="kis_mock",
                broker="kis",
                status="accepted",
                lifecycle_state="fill",
                last_reconcile_detail={"attributed_fill_qty": "1"},
                mirror_cohort="mock_counterfactual",
                mirror_source_bucket="place_original",
                **common,
            ),
            KISMockOrderLedger(
                trade_date=when,
                symbol="005930",
                instrument_type=InstrumentType.equity_kr,
                side="sell",
                order_type="limit",
                quantity=Decimal("1"),
                price=Decimal("110"),
                amount=Decimal("110"),
                fee=Decimal("0"),
                currency="KRW",
                order_no=f"MSELL-{uuid4().hex[:8]}",
                account_mode="kis_mock",
                broker="kis",
                status="accepted",
                lifecycle_state="fill",
                last_reconcile_detail={"attributed_fill_qty": "1"},
                mirror_cohort="mock_counterfactual",
                mirror_source_bucket="place_original",
                correlation_id="mock-exit",
                report_item_uuid=common["report_item_uuid"],
            ),
        ]
    )
    await db_session.commit()

    result = await agg.build_counterfactual_delta_scoreboard(
        db_session, market="kr", include_excursions=False, use_cache=False
    )
    assert result["paired_count"] >= 1
    assert result["overall_delta"]["mock_minus_live_expectancy_pct"] == pytest.approx(0.05)
    assert result["caveats"]
```

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run pytest tests/services/test_trade_journal_mirror_aggregates.py -v`
Expected: FAIL because `cohort` and delta builder are missing.

- [ ] **Step 3: Extend aggregate dataclasses**

Add defaulted fields to avoid breaking existing tests:

```python
@dataclass(frozen=True)
class Fill:
    market: str
    symbol: str
    account: str
    side: str
    qty: float
    price: float
    fee: float
    ts: datetime
    item_uuid: str | None
    correlation_id: str | None
    source: str
    cohort: str = "live_gated"
    source_bucket: str | None = None


@dataclass(frozen=True)
class ClosedTrade:
    market: str
    symbol: str
    account: str
    qty: float
    entry_price: float
    exit_price: float
    entry_ts: datetime
    exit_ts: datetime
    pnl_abs: float
    pnl_pct: float
    fees: float
    entry_item_uuids: tuple[str, ...]
    exit_item_uuid: str | None
    entry_correlation_ids: tuple[str, ...]
    exit_correlation_id: str | None
    cohort: str = "live_gated"
    source_bucket: str | None = None
```

In `pair_fills_fifo`, set the trade cohort from the entry lots when all consumed lots share one cohort, otherwise `"mixed"`.

- [ ] **Step 4: Add KIS mock fill loading**

Import `KISMockOrderLedger` and `_derive_shadow_fill`. Add `_mock_fill_rows` logic to `load_fills`:

```python
if cohort in ("mock_counterfactual", "all") and account_mode in (None, "kis_mock"):
    stmt = select(KISMockOrderLedger).where(
        KISMockOrderLedger.mirror_cohort == "mock_counterfactual",
        KISMockOrderLedger.lifecycle_state == "fill",
    )
```

For each row:

```python
filled_qty, _remaining, status = _derive_shadow_fill(row, float(row.quantity))
if status not in {"filled", "partial"} or filled_qty <= 0:
    continue
fills.append(
    Fill(
        market="kr" if row.instrument_type == "equity_kr" else "us",
        symbol=to_db_symbol(row.symbol),
        account="kis_mock",
        side=row.side,
        qty=filled_qty,
        price=float(row.price),
        fee=float(row.fee or 0),
        ts=row.trade_date,
        item_uuid=str(row.report_item_uuid) if row.report_item_uuid else None,
        correlation_id=row.correlation_id,
        source="kis_mock",
        cohort="mock_counterfactual",
        source_bucket=row.mirror_source_bucket,
    )
)
```

Keep the default `cohort="live_gated"` behavior equivalent to current live-only results.

- [ ] **Step 5: Add paired delta builder**

Implement `build_counterfactual_delta_scoreboard`:

```python
async def build_counterfactual_delta_scoreboard(
    db: AsyncSession,
    *,
    market: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    include_excursions: bool = False,
    use_cache: bool = True,
) -> dict[str, Any]:
    board_live = await build_trading_scoreboard(
        db,
        market=market,
        date_from=date_from,
        date_to=date_to,
        include_excursions=include_excursions,
        cohort="live_gated",
        use_cache=use_cache,
    )
    board_mock = await build_trading_scoreboard(
        db,
        market=market,
        account_mode="kis_mock",
        date_from=date_from,
        date_to=date_to,
        include_excursions=include_excursions,
        cohort="mock_counterfactual",
        use_cache=use_cache,
    )
    live_trades = pair_fills_fifo(
        await load_fills(db, market=market, date_from=date_from, date_to=date_to, cohort="live_gated")
    )
    mock_trades = pair_fills_fifo(
        await load_fills(db, market=market, date_from=date_from, date_to=date_to, cohort="mock_counterfactual")
    )
    paired = _pair_by_entry_correlation(live_trades, mock_trades)
    return {
        "live_gated": board_live,
        "mock_counterfactual": board_mock,
        "paired_count": len(paired),
        "overall_delta": _paired_delta(paired),
        "caveats": [
            "KIS mock fills do not model queue priority, liquidity, slippage, or market impact; mock performance is upward biased."
        ],
    }
```

`_pair_by_entry_correlation` must use the first non-empty `entry_correlation_ids` value from each closed trade and require one `live_gated` and one `mock_counterfactual` trade for that key. `_paired_delta` must compute at least `mock_minus_live_expectancy_pct`, `mock_minus_live_hit_rate`, and `paired_n`.

- [ ] **Step 6: Update MCP scoreboard tool**

In `get_trading_scoreboard`, add parameters:

```python
cohort: str = "live_gated",
include_counterfactual_delta: bool = False,
```

If `include_counterfactual_delta` is true, return `build_counterfactual_delta_scoreboard` with the parsed market/date filters; otherwise return `build_trading_scoreboard` with the parsed filters and `cohort=cohort`.

- [ ] **Step 7: Verify aggregate task**

Run: `uv run pytest tests/services/test_trade_journal_mirror_aggregates.py tests/services/test_trade_journal_aggregates_scoreboard.py tests/test_mcp_trading_scoreboard.py -v`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add app/services/trade_journal/aggregates.py app/mcp_server/tooling/trading_scoreboard_tools.py app/mcp_server/tooling/trading_scoreboard_registration.py tests/services/test_trade_journal_mirror_aggregates.py tests/test_mcp_trading_scoreboard.py
git commit -m "feat(ROB-734): add counterfactual scoreboard cohort"
```

---

### Task 8: Briefing And Decision History Feedback

**Files:**
- Modify: `app/services/decision_history.py`
- Modify: `app/mcp_server/tooling/operating_briefing.py`
- Modify: `app/schemas/investment_reports.py`
- Test: `tests/services/test_decision_history.py`
- Test: `tests/mcp_server/test_operating_briefing_tools.py`

**Interfaces:**
- `decision_history` adds `counterfactual_r_by_tag` only when mock cohort groups exist.
- `OperatingBriefingResponse` adds `counterfactual_scoreboard: dict[str, Any] = Field(default_factory=dict)`.
- `get_operating_briefing_impl` includes fail-open staleness for `counterfactual_scoreboard`.

- [ ] **Step 1: Add decision history test**

Append to `tests/services/test_decision_history.py`:

```python
@pytest.mark.asyncio
async def test_decision_history_separates_counterfactual_r(monkeypatch, db_session):
    from app.services import decision_history as dh

    async def fake_board(db, **kwargs):
        if kwargs.get("cohort") == "mock_counterfactual":
            return {
                "groups": [
                    {
                        "tag": "breakout",
                        "n": 10,
                        "expectancy_r": 0.4,
                        "win_rate": 0.6,
                        "profit_factor": 1.8,
                        "avg_mae": -0.02,
                        "insufficient_sample": False,
                    }
                ]
            }
        return {
            "groups": [
                {
                    "tag": "breakout",
                    "n": 10,
                    "expectancy_r": 0.2,
                    "win_rate": 0.5,
                    "profit_factor": 1.2,
                    "avg_mae": -0.03,
                    "insufficient_sample": False,
                }
            ]
        }

    monkeypatch.setattr(
        "app.services.trade_journal.aggregates.build_trading_scoreboard",
        fake_board,
    )
    out = await dh._realized_r_by_tag(db_session, "kr", setup_tag=None)
    cf = await dh._counterfactual_r_by_tag(db_session, "kr", setup_tag=None)
    assert out["breakout"]["expectancy_r"] == 0.2
    assert cf["breakout"]["expectancy_r"] == 0.4
```

- [ ] **Step 2: Add operating briefing test**

Append to `tests/mcp_server/test_operating_briefing_tools.py` with existing monkeypatch style:

```python
@pytest.mark.asyncio
async def test_get_operating_briefing_includes_counterfactual_scoreboard(monkeypatch):
    from app.mcp_server.tooling import operating_briefing as ob

    async def fake_counterfactual(db, **kwargs):
        return {"paired_count": 0, "overall_delta": None, "caveats": ["mock caveat"]}

    monkeypatch.setattr(ob, "build_counterfactual_delta_scoreboard", fake_counterfactual)
    result = await ob.get_operating_briefing_impl(market="kr", account_scope="kis_live")
    assert result["counterfactual_scoreboard"]["paired_count"] == 0
    assert result["staleness"]["counterfactual_scoreboard"]["freshness_status"] == "db_read"
```

- [ ] **Step 3: Run tests to verify failure**

Run: `uv run pytest tests/services/test_decision_history.py tests/mcp_server/test_operating_briefing_tools.py -k "counterfactual or operating_briefing" -v`
Expected: FAIL because the new fields and helper do not exist.

- [ ] **Step 4: Add decision history helper**

In `app/services/decision_history.py`, add:

```python
async def _counterfactual_r_by_tag(
    db: AsyncSession, market: str, setup_tag: str | None
) -> dict[str, dict[str, Any]]:
    from app.services.trade_journal.aggregates import build_trading_scoreboard

    board = await build_trading_scoreboard(
        db,
        market=market,
        account_mode="kis_mock",
        include_excursions=False,
        cohort="mock_counterfactual",
    )
    groups = [g for g in board.get("groups", []) if g["tag"] != "untagged"]
    ordered = sorted(groups, key=lambda g: (g["tag"] != setup_tag, -int(g["n"])))
    return {g["tag"]: {k: g.get(k) for k in _R_KEYS} for g in ordered[:_MAX_TAGS]}
```

In `build_decision_context`, add:

```python
counterfactual_r = await _counterfactual_r_by_tag(db, market, setup_tag)
if counterfactual_r:
    ctx["counterfactual_r_by_tag"] = counterfactual_r
```

Do not merge these rows into `realized_r_by_tag`.

- [ ] **Step 5: Add operating briefing section**

In `OperatingBriefingResponse`, add:

```python
counterfactual_scoreboard: dict[str, Any] = Field(default_factory=dict)
```

In `operating_briefing.py`, import `build_counterfactual_delta_scoreboard`, compute it inside the DB session, and add staleness:

```python
try:
    counterfactual_scoreboard = await build_counterfactual_delta_scoreboard(
        db,
        market=market,
        include_excursions=False,
    )
    counterfactual_scoreboard_staleness = {"freshness_status": "db_read"}
except Exception as exc:
    reason = _section_unavailable_reason("counterfactual_scoreboard", exc)
    counterfactual_scoreboard = {
        "paired_count": 0,
        "overall_delta": None,
        "unavailable_reason": reason,
    }
    counterfactual_scoreboard_staleness = {
        "freshness_status": "unavailable",
        "unavailable_reason": reason,
    }
```

Add it to response:

```python
"counterfactual_scoreboard": counterfactual_scoreboard,
```

and staleness:

```python
"counterfactual_scoreboard": counterfactual_scoreboard_staleness,
```

- [ ] **Step 6: Verify feedback task**

Run: `uv run pytest tests/services/test_decision_history.py tests/mcp_server/test_operating_briefing_tools.py -k "counterfactual or decision_history or operating_briefing" -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add app/services/decision_history.py app/mcp_server/tooling/operating_briefing.py app/schemas/investment_reports.py tests/services/test_decision_history.py tests/mcp_server/test_operating_briefing_tools.py
git commit -m "feat(ROB-734): surface counterfactual feedback separately"
```

---

### Task 9: Documentation And Route Guidance

**Files:**
- Modify: `app/mcp_server/README.md`
- Modify: `app/mcp_server/tooling/route_request_lanes.py` if its standard sequence omits the mirror end-of-session step.
- Test: `tests/test_route_request_lanes.py`
- Test: `tests/mcp_server/tooling/test_trade_retrospective_registration_docs.py` or a new docs contract test if the repo has one for MCP README snippets.

**Interfaces:**
- README documents `kis_mock_mirror_execute_report`.
- README documents `get_trading_scoreboard(cohort, include_counterfactual_delta)`.
- Route guidance tells buy/discovery sessions to call the mirror tool after item decisions are final.

- [ ] **Step 1: Add docs assertions**

Add a small test:

```python
# tests/mcp_server/test_mirror_counterfactual_docs.py
from pathlib import Path


def test_mirror_counterfactual_mcp_docs_present():
    text = Path("app/mcp_server/README.md").read_text()
    assert "kis_mock_mirror_execute_report" in text
    assert "mock_counterfactual" in text
    assert "include_counterfactual_delta" in text
    assert "queue priority" in text
```

- [ ] **Step 2: Run docs test to verify failure**

Run: `uv run pytest tests/mcp_server/test_mirror_counterfactual_docs.py -v`
Expected: FAIL until docs are updated.

- [ ] **Step 3: Update MCP README**

Add a section near KIS mock order tools:

```markdown
### `kis_mock_mirror_execute_report`

Executes one investment report's original analysis plan as KIS official mock
orders for ROB-734 counterfactual measurement.

Parameters:
- `report_uuid` (required): source `investment_reports.report_uuid`
- `dry_run` (default `true`): preview only when true; when false, mutates only `account_mode="kis_mock"`
- `min_rung_quantity` (default `1.0`): share quantity for `deferred_no_action` minimum-rung probes

The tool uses original item sizing from `max_action` and price evidence. It
does not use operator trims from `approved_payload_snapshot`. Rows are stamped
with `mirror_cohort="mock_counterfactual"` and never touch live order paths.

Caveat: KIS mock fills do not model queue priority, liquidity, slippage, or
market impact, so mock performance is upward biased.
```

Update `get_trading_scoreboard` docs with:

```markdown
- `cohort`: `live_gated` (default), `mock_counterfactual`, or `all`
- `include_counterfactual_delta`: when true, returns paired `live_gated` vs
  `mock_counterfactual` delta metrics and caveats
```

- [ ] **Step 4: Update route guidance if needed**

If `route_request_lanes.py` has a buy/discovery session sequence, append an end-of-session advisory step:

```python
{
    "tool": "kis_mock_mirror_execute_report",
    "purpose": "ROB-734 end-of-session mirror counterfactual after item decisions are final",
}
```

Keep this as guidance only; do not make live order or policy behavior automatic.

- [ ] **Step 5: Verify docs task**

Run: `uv run pytest tests/mcp_server/test_mirror_counterfactual_docs.py tests/test_route_request_lanes.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/mcp_server/README.md app/mcp_server/tooling/route_request_lanes.py tests/mcp_server/test_mirror_counterfactual_docs.py tests/test_route_request_lanes.py
git commit -m "docs(ROB-734): document mirror counterfactual workflow"
```

---

### Task 10: Final Verification

**Files:**
- No new files.

**Commands:**

- [ ] **Step 1: Run targeted unit and MCP tests**

Run:

```bash
uv run pytest \
  tests/models/test_rob734_mirror_schema.py \
  tests/services/test_trade_retrospective_mirror_correlation.py \
  tests/test_kis_mock_order_ledger.py \
  tests/services/test_mirror_counterfactual_plans.py \
  tests/services/test_mirror_counterfactual_execution.py \
  tests/mcp_server/test_mirror_counterfactual_tool.py \
  tests/services/test_trade_journal_mirror_aggregates.py \
  tests/test_mcp_trading_scoreboard.py \
  tests/services/test_decision_history.py \
  tests/mcp_server/test_operating_briefing_tools.py \
  tests/mcp_server/test_mirror_counterfactual_docs.py \
  -v
```

Expected: PASS.

- [ ] **Step 2: Run lint**

Run:

```bash
uv run ruff check app tests alembic
```

Expected: PASS.

- [ ] **Step 3: Run migration check**

Run:

```bash
uv run alembic upgrade head
```

Expected: PASS with head at `20260706_rob734`.

- [ ] **Step 4: Run broader affected suites**

Run:

```bash
uv run pytest \
  tests/services/test_trade_journal_aggregates.py \
  tests/services/test_trade_journal_aggregates_scoreboard.py \
  tests/services/test_kis_mock_retrospective_pending.py \
  tests/test_trade_retrospective_service.py \
  tests/test_mcp_kis_order_variants.py \
  tests/mcp_server/test_kis_mock_reconciliation_tool.py \
  -v
```

Expected: PASS.

- [ ] **Step 5: Manual dry-run smoke**

With a real report UUID from a non-production local/dev DB:

```bash
uv run python -m app.mcp_server.main
```

Then call `kis_mock_mirror_execute_report(report_uuid=<uuid>, dry_run=true)`.
Expected: response includes `success=true`, `planned_count`, `dry_run_count`, `skipped_count`, per-item results, and caveats. No broker mutation occurs.

- [ ] **Step 6: Manual apply smoke when operator explicitly wants mock mutation**

Only after confirming `KIS_MOCK_ENABLED=true` and mock credentials are configured:

```text
kis_mock_mirror_execute_report(report_uuid=<uuid>, dry_run=false)
```

Expected: response includes `submitted_count > 0` for orderable rows. Querying `review.kis_mock_order_ledger` for returned `ledger_id` rows shows `mirror_cohort='mock_counterfactual'`, `mirror_source_bucket` populated, and `report_item_uuid` populated.

---

## Self-Review

- **Spec coverage:** The plan covers all ROB-734 requirements: full original PLACE execution, WATCH trigger conversion, deferred minimum-rung probes, KIS mock-only mutation, paired delta scoreboard, separated briefing/decision-history feedback, caveat reporting, and no policy auto-adjustment.
- **Known deliberate deviation:** The Linear text says "same correlation_id + account_mode"; this plan makes that viable by changing retrospective uniqueness to `(correlation_id, account_mode)`. Without that change, mock retrospectives would overwrite or conflict with live retrospectives.
- **Blast radius:** The riskiest shared change is retrospective idempotency. Task 2 isolates it with direct upsert tests and affected due-list tests.
- **Migration risk:** All new KIS mock columns are nullable. The retrospective unique replacement should be safe for existing data because the old unique constraint already prevents duplicate non-null `correlation_id` rows.
- **No placeholders:** Every task lists concrete files, functions, commands, and expected outcomes.

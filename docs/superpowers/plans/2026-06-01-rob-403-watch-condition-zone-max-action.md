# ROB-403 — watch_condition zone/다중조건 + max_action 스키마 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** watch가 가격존(between)·다중메트릭 AND 조건과 구조화된 `max_action`(side/quantity|notional/limit_price/account_mode)을 표현하고, 스캐너가 이를 평가하도록 확장한다 (ROB-402가 소비할 계약 고정).

**Architecture:** `watch_condition`을 JSONB `conditions[]`+`combine` 표현으로 진화(구형 flat은 ingest 시 단일조건으로 정규화). alert에 `conditions`/`combine`/`threshold_high` 컬럼 추가, flat 컬럼은 primary 조건 요약으로 유지(back-compat). 스캐너는 conditions 있으면 다중평가, 없으면 flat fallback. `max_action`은 기존 JSONB 컬럼에 Pydantic 검증 레이어(`extra="allow"`)만 추가.

**Tech Stack:** Python 3.13, Pydantic v2, SQLAlchemy async, Postgres, alembic, pytest/pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-06-01-rob-403-watch-condition-zone-max-action-design.md`

---

## File Structure

| 파일 | 역할 | 변경 |
|---|---|---|
| `app/schemas/investment_reports.py` | watch_condition + max_action Pydantic | `WatchConditionClause`, `WatchConditionPayload` v2(normalize), `MaxActionPayload`, `IngestReportItem` max_action 검증 |
| `app/models/investment_reports.py` | `InvestmentWatchAlert` + `InvestmentWatchEvent` 컬럼/CHECK | alert `conditions`/`combine`/`threshold_high` + operator CHECK; event operator CHECK(between) + `threshold_high` |
| `app/services/hermes_client.py` | `ReviewTriggerPayload` | operator→between + `threshold_high` 필드 |
| `alembic/versions/<rev>_rob403_*.py` | prod 마이그레이션 | 신규 |
| `tests/conftest.py` | 영속 테스트 DB drift 패치 | 컬럼 add-if-not-exists + operator CHECK drop+recreate |
| `app/services/investment_reports/watch_activation.py` | 활성화 매핑 | conditions/combine/threshold_high + flat primary 요약 파생 |
| `app/jobs/watch_market_data.py` | 조건 평가 | `evaluate_clause`, `evaluate_alert_conditions` |
| `app/jobs/investment_watch_scanner.py` | 트리거 분기 | conditions 우선/flat fallback |
| `tests/test_investment_reports_schemas.py` | 스키마 테스트 | 추가 |
| `tests/test_watch_condition_evaluation.py` | 평가 단위 테스트 | 신규 |
| `tests/test_investment_reports_watch_activation.py` | 활성화 매핑 테스트 | 추가 |
| `tests/test_investment_watch_scanner.py` | 스캐너 회귀/zone | 추가 |

---

## Task 1: WatchConditionClause + WatchConditionPayload v2 (정규화)

**Files:**
- Modify: `app/schemas/investment_reports.py:57-90`
- Test: `tests/test_investment_reports_schemas.py`

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_investment_reports_schemas.py` 끝에 추가:

```python
from decimal import Decimal

from app.schemas.investment_reports import (
    WatchConditionClause,
    WatchConditionPayload,
)


def test_clause_between_requires_low_high_and_orders():
    c = WatchConditionClause(metric="price", op="between", low="100", high="200")
    assert c.low == Decimal("100") and c.high == Decimal("200")
    with pytest.raises(ValueError):
        WatchConditionClause(metric="price", op="between", low="200", high="100")
    with pytest.raises(ValueError):
        WatchConditionClause(metric="price", op="above")  # missing threshold


def test_legacy_flat_payload_normalizes_to_single_condition():
    p = WatchConditionPayload(metric="price", operator="below", threshold="55000")
    assert len(p.conditions) == 1
    assert p.conditions[0].metric == "price"
    assert p.conditions[0].op == "below"
    assert p.conditions[0].threshold == Decimal("55000")
    assert p.combine == "and"
    assert p.threshold_key == "55000"  # legacy dedup key preserved


def test_conditions_payload_multi_metric_and():
    p = WatchConditionPayload(
        conditions=[
            {"metric": "price", "op": "between", "low": "50000", "high": "55000"},
            {"metric": "rsi", "op": "below", "threshold": "35"},
        ]
    )
    assert len(p.conditions) == 2
    assert p.combine == "and"


def test_payload_requires_conditions_or_flat():
    with pytest.raises(ValueError):
        WatchConditionPayload(target_kind="asset")  # neither flat nor conditions
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_investment_reports_schemas.py -k "clause or payload" -v`
Expected: FAIL — `ImportError: cannot import name 'WatchConditionClause'`.

- [ ] **Step 3: 구현**

`app/schemas/investment_reports.py` — `WatchOperatorLiteral` 정의(58행) 다음에 추가:

```python
WatchClauseOpLiteral = Literal["above", "below", "between"]
WatchCombineLiteral = Literal["and"]
```

`WatchConditionPayload` 클래스(72–90행) **전체를 교체**:

```python
class WatchConditionClause(BaseModel):
    """One condition clause. above/below use ``threshold``; between uses low/high."""

    metric: WatchMetricLiteral
    op: WatchClauseOpLiteral
    threshold: Decimal | None = None
    low: Decimal | None = None
    high: Decimal | None = None

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _validate_clause(self) -> WatchConditionClause:
        if self.op in ("above", "below"):
            if self.threshold is None:
                raise ValueError(f"op={self.op} requires threshold")
            if self.low is not None or self.high is not None:
                raise ValueError(f"op={self.op} must not set low/high")
        elif self.op == "between":
            if self.low is None or self.high is None:
                raise ValueError("op=between requires low and high")
            if self.low > self.high:
                raise ValueError("op=between requires low <= high")
            if self.threshold is not None:
                raise ValueError("op=between must not set threshold")
        return self


def _derive_condition_key(clauses: list[WatchConditionClause]) -> str:
    """Deterministic dedup key. Single above/below clause keeps legacy str(threshold)."""
    if len(clauses) == 1 and clauses[0].op in ("above", "below"):
        return str(clauses[0].threshold)
    parts: list[str] = []
    for c in clauses:
        if c.op == "between":
            parts.append(f"{c.metric}:between:{c.low}-{c.high}")
        else:
            parts.append(f"{c.metric}:{c.op}:{c.threshold}")
    return "and(" + ",".join(parts) + ")"


class WatchConditionPayload(BaseModel):
    """Embedded condition for a watch item. Persisted as JSONB.

    Two accepted input shapes, both normalized to ``conditions``:
    - legacy flat: ``metric`` + ``operator`` + ``threshold``
    - v2: ``conditions=[{metric, op, threshold|low/high}]`` + ``combine``
    """

    # legacy flat (optional)
    metric: WatchMetricLiteral | None = None
    operator: WatchOperatorLiteral | None = None
    threshold: Decimal | None = None
    threshold_key: str | None = None
    target_kind: TargetKindLiteral = "asset"
    action_mode: WatchActionModeLiteral = "notify_only"
    # v2
    conditions: list[WatchConditionClause] | None = None
    combine: WatchCombineLiteral = "and"

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _normalize(self) -> WatchConditionPayload:
        if self.conditions is None:
            if self.metric is None or self.operator is None or self.threshold is None:
                raise ValueError(
                    "watch_condition requires either conditions[] or "
                    "metric+operator+threshold"
                )
            self.conditions = [
                WatchConditionClause(
                    metric=self.metric, op=self.operator, threshold=self.threshold
                )
            ]
        elif not self.conditions:
            raise ValueError("conditions must be non-empty")
        if self.threshold_key is None:
            self.threshold_key = _derive_condition_key(self.conditions)
        return self
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_investment_reports_schemas.py -v`
Expected: PASS (신규 + 기존 watch_condition 테스트 — 기존은 flat 입력이므로 정규화로 통과).

- [ ] **Step 5: 커밋**

```bash
git add app/schemas/investment_reports.py tests/test_investment_reports_schemas.py
git commit -m "feat(ROB-403): watch_condition conditions[] schema (flat back-compat normalize)"
```

---

## Task 2: MaxActionPayload + IngestReportItem 검증

**Files:**
- Modify: `app/schemas/investment_reports.py` (MaxActionPayload 추가 + IngestReportItem validator)
- Test: `tests/test_investment_reports_schemas.py`

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_investment_reports_schemas.py`에 추가:

```python
from app.schemas.investment_reports import IngestReportItem, MaxActionPayload


def test_max_action_xor_quantity_notional():
    MaxActionPayload(side="buy", quantity="10", account_mode="kis_mock")
    MaxActionPayload(side="sell", notional="1000000", account_mode="kis_mock")
    with pytest.raises(ValueError):
        MaxActionPayload(side="buy", account_mode="kis_mock")  # neither
    with pytest.raises(ValueError):
        MaxActionPayload(
            side="buy", quantity="10", notional="100", account_mode="kis_mock"
        )  # both


def test_max_action_allows_extra_legacy_keys():
    m = MaxActionPayload(
        side="buy", quantity="10", account_mode="kis_mock", notional_usd="500"
    )
    assert m.model_dump()["notional_usd"] == "500"


def test_ingest_item_validates_max_action_when_present():
    with pytest.raises(ValueError):
        IngestReportItem(
            client_item_key="k1",
            item_kind="watch",
            operation="create",
            intent="buy_review",
            rationale="r",
            symbol="005930",
            watch_condition={"metric": "price", "operator": "below", "threshold": "5"},
            valid_until="2026-12-31T00:00:00Z",
            max_action={"side": "buy"},  # invalid: no quantity/notional
        )
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_investment_reports_schemas.py -k "max_action" -v`
Expected: FAIL — `ImportError: cannot import name 'MaxActionPayload'`.

- [ ] **Step 3-a: MaxActionPayload 추가**

`app/schemas/investment_reports.py` — `WatchConditionPayload` 정의 다음에 추가. (파일 상단 import에 `from app.schemas.execution_contracts import AccountMode` 추가; 없으면 함께.)

```python
class MaxActionPayload(BaseModel):
    """Structured order params a watch trigger proposes. Consumed by ROB-402.

    ``extra='allow'`` preserves legacy keys (e.g. ``notional_usd`` used by
    mock_preview). The live auto-execute block is enforced by ROB-402 on the
    (action_mode, account_mode) combination, not here.
    """

    side: ItemSideLiteral
    quantity: Decimal | None = None
    notional: Decimal | None = None
    limit_price: Decimal | None = None
    account_mode: AccountMode

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="after")
    def _xor_quantity_notional(self) -> MaxActionPayload:
        has_qty = self.quantity is not None
        has_notional = self.notional is not None
        if has_qty == has_notional:
            raise ValueError(
                "max_action requires exactly one of quantity or notional"
            )
        return self
```

- [ ] **Step 3-b: IngestReportItem 검증 추가**

`app/schemas/investment_reports.py` — `IngestReportItem._validate_watch_invariants`(176–190행) 다음에 추가:

```python
    @model_validator(mode="after")
    def _validate_max_action(self) -> IngestReportItem:
        if (
            self.item_kind == "watch"
            and self.operation in ("create", "modify")
            and self.max_action
        ):
            MaxActionPayload.model_validate(self.max_action)
        return self
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_investment_reports_schemas.py -v`
Expected: PASS.

- [ ] **Step 5: 커밋**

```bash
git add app/schemas/investment_reports.py tests/test_investment_reports_schemas.py
git commit -m "feat(ROB-403): MaxActionPayload (side/qty|notional XOR/account_mode) + ingest validation"
```

---

## Task 3: Alert 컬럼 + operator CHECK (모델 + 마이그레이션 + conftest)

**Files:**
- Modify: `app/models/investment_reports.py:462-465` (operator CHECK) + 514행 근처(컬럼)
- Modify: `tests/conftest.py`
- Create: `alembic/versions/<rev>_rob403_watch_conditions.py`
- Test: `tests/test_investment_reports_watch_activation.py`

> **DB 픽스처 주의**: 이 파일의 테스트는 `db_session`이 아니라 `tests/_investment_reports_helpers.py`의 **`session`** 픽스처를 쓴다. 그 픽스처는 자체 DDL drift 패치(create_all checkfirst + ALTER 튜플)를 갖고 있어, ROB-403 컬럼/CHECK 패치를 **conftest.py와 이 helper 양쪽**에 넣어야 한다(Step 3-b).

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_investment_reports_watch_activation.py` 끝에 추가:

```python
@pytest.mark.asyncio
async def test_alert_accepts_between_operator_and_conditions(
    session: AsyncSession,
) -> None:
    alert = InvestmentWatchAlert(
        alert_uuid=uuid.uuid4(),
        idempotency_key=f"k-{uuid.uuid4()}",
        source_report_uuid=uuid.uuid4(),
        source_item_uuid=uuid.uuid4(),
        market="kr",
        target_kind="asset",
        symbol="005930",
        metric="price",
        operator="between",
        threshold=Decimal("50000"),
        threshold_high=Decimal("55000"),
        threshold_key="and(price:between:50000-55000)",
        conditions=[{"metric": "price", "op": "between", "low": "50000", "high": "55000"}],
        combine="and",
        intent="buy_review",
        action_mode="notify_only",
        rationale="zone buy",
        valid_until=future_datetime(),
    )
    session.add(alert)
    await session.commit()
    fetched = await session.get(InvestmentWatchAlert, alert.id)
    assert fetched.operator == "between"
    assert fetched.conditions[0]["op"] == "between"
    assert fetched.combine == "and"
```

> 파일 상단 import에 `InvestmentWatchAlert`를 추가한다: 기존 `from app.models.investment_reports import InvestmentReportItem` 라인을 `from app.models.investment_reports import InvestmentReportItem, InvestmentWatchAlert`로.

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_investment_reports_watch_activation.py -k "between_operator" -v`
Expected: FAIL — `AttributeError`/`TypeError` (`threshold_high`/`conditions`/`combine` 컬럼 없음) 또는 CHECK 위반(`operator='between'`).

- [ ] **Step 3-a: 모델 컬럼 + CHECK**

`app/models/investment_reports.py` — operator CHECK(462–465행) 교체:

```python
        CheckConstraint(
            "operator IN ('above','below','between')",
            name="ck_investment_watch_alerts_operator",
        ),
```

`market` CHECK 블록(470–473행) 다음, `Index(...)` 앞에 combine CHECK 추가:

```python
        CheckConstraint(
            "combine IN ('and')",
            name="ck_investment_watch_alerts_combine",
        ),
```

`threshold_key` 컬럼(514행) 다음에 컬럼 3개 추가:

```python
    threshold_high: Mapped[float | None] = mapped_column(Numeric(20, 8))
    conditions: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    combine: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'and'")
    )
```

같은 파일 `InvestmentWatchEvent`(560행~) — operator CHECK(601–602행) 교체:

```python
        CheckConstraint(
            "operator IN ('above','below','between')",
            name="ck_investment_watch_events_operator",
        ),
```

`InvestmentWatchEvent`의 `operator` 컬럼(665행) 다음에 `threshold_high` 추가:

```python
    threshold_high: Mapped[float | None] = mapped_column(Numeric(20, 8))
```

> between은 alert→event→Hermes payload 체인을 타므로 event도 operator='between'를 허용해야 하고, 상위 bound 유실 방지로 event에도 threshold_high를 둔다.

- [ ] **Step 3-b: 영속 테스트 DB drift 패치 (두 곳)**

ROB-403 컬럼/CHECK는 두 개의 독립 테스트-DB 셋업 경로 모두에 패치해야 한다: `tests/conftest.py`의 `db_session`(cross-domain 테스트)과 `tests/_investment_reports_helpers.py`의 `session`(investment_reports 테스트 — Task 4 활성화 테스트가 사용).

**(i) `tests/conftest.py`** — ROB-406 lifecycle CHECK 패치 블록 다음(또는 ROB-329 블록 근처)에 추가:

```python
                # ROB-403 — investment_watch_alerts: add conditions/combine/
                # threshold_high columns + extend operator CHECK to 'between'.
                # create_all is no-op on the persistent test table.
                await conn.execute(
                    text(
                        "ALTER TABLE review.investment_watch_alerts "
                        "ADD COLUMN IF NOT EXISTS threshold_high NUMERIC(20,8)"
                    )
                )
                await conn.execute(
                    text(
                        "ALTER TABLE review.investment_watch_alerts "
                        "ADD COLUMN IF NOT EXISTS conditions JSONB "
                        "NOT NULL DEFAULT '[]'::jsonb"
                    )
                )
                await conn.execute(
                    text(
                        "ALTER TABLE review.investment_watch_alerts "
                        "ADD COLUMN IF NOT EXISTS combine TEXT "
                        "NOT NULL DEFAULT 'and'"
                    )
                )
                await conn.execute(
                    text(
                        "ALTER TABLE review.investment_watch_alerts "
                        "DROP CONSTRAINT IF EXISTS "
                        "ck_investment_watch_alerts_operator"
                    )
                )
                await conn.execute(
                    text(
                        "ALTER TABLE review.investment_watch_alerts "
                        "ADD CONSTRAINT ck_investment_watch_alerts_operator "
                        "CHECK (operator IN ('above','below','between'))"
                    )
                )
                await conn.execute(
                    text(
                        "ALTER TABLE review.investment_watch_alerts "
                        "DROP CONSTRAINT IF EXISTS "
                        "ck_investment_watch_alerts_combine"
                    )
                )
                await conn.execute(
                    text(
                        "ALTER TABLE review.investment_watch_alerts "
                        "ADD CONSTRAINT ck_investment_watch_alerts_combine "
                        "CHECK (combine IN ('and'))"
                    )
                )
                # ROB-403 — investment_watch_events: between + threshold_high.
                await conn.execute(
                    text(
                        "ALTER TABLE review.investment_watch_events "
                        "ADD COLUMN IF NOT EXISTS threshold_high NUMERIC(20,8)"
                    )
                )
                await conn.execute(
                    text(
                        "ALTER TABLE review.investment_watch_events "
                        "DROP CONSTRAINT IF EXISTS "
                        "ck_investment_watch_events_operator"
                    )
                )
                await conn.execute(
                    text(
                        "ALTER TABLE review.investment_watch_events "
                        "ADD CONSTRAINT ck_investment_watch_events_operator "
                        "CHECK (operator IN ('above','below','between'))"
                    )
                )
```

**(ii) `tests/_investment_reports_helpers.py`** — `session` 픽스처의 `for stmt in ( ... ):` 튜플(약 90–214행, 215행 `await conn.execute(sa.text(stmt))`로 실행)의 **끝(닫는 `):` 직전)** 에 문자열 항목들을 추가. 이 루프는 각 항목을 순차 실행하므로 DROP→ADD CONSTRAINT를 두 항목으로 넣는다:

```python
                        # ROB-403 — investment_watch_alerts conditions/combine/
                        # threshold_high + operator CHECK extend. Idempotent;
                        # mirrors the alembic migration.
                        "ALTER TABLE review.investment_watch_alerts "
                        "ADD COLUMN IF NOT EXISTS threshold_high NUMERIC(20,8)",
                        "ALTER TABLE review.investment_watch_alerts "
                        "ADD COLUMN IF NOT EXISTS conditions JSONB "
                        "NOT NULL DEFAULT '[]'::jsonb",
                        "ALTER TABLE review.investment_watch_alerts "
                        "ADD COLUMN IF NOT EXISTS combine TEXT NOT NULL DEFAULT 'and'",
                        "ALTER TABLE review.investment_watch_alerts "
                        "DROP CONSTRAINT IF EXISTS ck_investment_watch_alerts_operator",
                        "ALTER TABLE review.investment_watch_alerts "
                        "ADD CONSTRAINT ck_investment_watch_alerts_operator "
                        "CHECK (operator IN ('above','below','between'))",
                        "ALTER TABLE review.investment_watch_alerts "
                        "DROP CONSTRAINT IF EXISTS ck_investment_watch_alerts_combine",
                        "ALTER TABLE review.investment_watch_alerts "
                        "ADD CONSTRAINT ck_investment_watch_alerts_combine "
                        "CHECK (combine IN ('and'))",
                        # ROB-403 — investment_watch_events: between + threshold_high.
                        "ALTER TABLE review.investment_watch_events "
                        "ADD COLUMN IF NOT EXISTS threshold_high NUMERIC(20,8)",
                        "ALTER TABLE review.investment_watch_events "
                        "DROP CONSTRAINT IF EXISTS ck_investment_watch_events_operator",
                        "ALTER TABLE review.investment_watch_events "
                        "ADD CONSTRAINT ck_investment_watch_events_operator "
                        "CHECK (operator IN ('above','below','between'))",
```

- [ ] **Step 3-c: alembic 마이그레이션**

현재 head 확인 후 빈 리비전 생성(CHECK/JSONB는 수동 작성):

```bash
uv run alembic heads
uv run alembic revision -m "rob403 watch conditions zone"
```

생성 파일 본문:

```python
"""rob403 watch conditions zone

Revision ID: <자동생성>
Revises: <현재 head>
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "<자동생성>"
down_revision = "<현재 head>"
branch_labels = None
depends_on = None

_S = "review"
_T = "investment_watch_alerts"
_OP_NAME = "ck_investment_watch_alerts_operator"
_COMBINE_NAME = "ck_investment_watch_alerts_combine"
_ET = "investment_watch_events"
_EV_OP_NAME = "ck_investment_watch_events_operator"


def upgrade() -> None:
    op.add_column(
        _T,
        sa.Column("threshold_high", sa.Numeric(20, 8), nullable=True),
        schema=_S,
    )
    op.add_column(
        _T,
        sa.Column(
            "conditions",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        schema=_S,
    )
    op.add_column(
        _T,
        sa.Column(
            "combine",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'and'"),
        ),
        schema=_S,
    )
    op.drop_constraint(_OP_NAME, _T, schema=_S, type_="check")
    op.create_check_constraint(
        _OP_NAME, _T, "operator IN ('above','below','between')", schema=_S
    )
    op.create_check_constraint(
        _COMBINE_NAME, _T, "combine IN ('and')", schema=_S
    )
    # events: between + threshold_high
    op.add_column(
        _ET,
        sa.Column("threshold_high", sa.Numeric(20, 8), nullable=True),
        schema=_S,
    )
    op.drop_constraint(_EV_OP_NAME, _ET, schema=_S, type_="check")
    op.create_check_constraint(
        _EV_OP_NAME, _ET, "operator IN ('above','below','between')", schema=_S
    )


def downgrade() -> None:
    op.drop_constraint(_EV_OP_NAME, _ET, schema=_S, type_="check")
    op.create_check_constraint(
        _EV_OP_NAME, _ET, "operator IN ('above','below')", schema=_S
    )
    op.drop_column(_ET, "threshold_high", schema=_S)
    op.drop_constraint(_COMBINE_NAME, _T, schema=_S, type_="check")
    op.drop_constraint(_OP_NAME, _T, schema=_S, type_="check")
    op.create_check_constraint(
        _OP_NAME, _T, "operator IN ('above','below')", schema=_S
    )
    op.drop_column(_T, "combine", schema=_S)
    op.drop_column(_T, "conditions", schema=_S)
    op.drop_column(_T, "threshold_high", schema=_S)
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_investment_reports_watch_activation.py -k "between_operator" -v`
Expected: PASS.

- [ ] **Step 5: 커밋**

```bash
git add app/models/investment_reports.py tests/conftest.py tests/_investment_reports_helpers.py alembic/versions tests/test_investment_reports_watch_activation.py
git commit -m "feat(ROB-403): alert conditions/combine/threshold_high columns + operator CHECK"
```

---

## Task 4: 활성화 매핑 (watch_activation)

**Files:**
- Modify: `app/services/investment_reports/watch_activation.py:79-103`
- Test: `tests/test_investment_reports_watch_activation.py`

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_investment_reports_watch_activation.py`에 추가. 같은 파일 `test_activate_copies_snapshot_and_transitions_item`(82행)의 ingest→approve→activate 절차를 복제하되 zone `WatchConditionPayload`를 사용. 상단 import에 `WatchConditionClause`를 `app.schemas.investment_reports`에서 추가:

```python
@pytest.mark.asyncio
async def test_activate_maps_conditions_and_flat_primary(
    session: AsyncSession,
) -> None:
    ingest = InvestmentReportIngestionService(session)
    report = await ingest.ingest(
        IngestReportRequest(
            report_type="kr_morning",
            market="kr",
            account_scope="kis_mock",
            execution_mode="mock_preview",
            created_by_profile="t",
            title="t",
            summary="s",
            kst_date="2026-05-18",
            items=[
                IngestReportItem(
                    client_item_key="watch-zone-1",
                    item_kind="watch",
                    symbol="005930",
                    intent="buy_review",
                    rationale="zone buy",
                    watch_condition=WatchConditionPayload(
                        conditions=[
                            WatchConditionClause(
                                metric="price",
                                op="between",
                                low=Decimal("50000"),
                                high=Decimal("55000"),
                            )
                        ]
                    ),
                    valid_until=future_datetime(),
                )
            ],
        )
    )
    repo = InvestmentReportsRepository(session)
    item = (await repo.list_items_for_report(report.id))[0]
    await InvestmentReportDecisionService(session).record(
        RecordDecisionRequest(
            item_uuid=item.item_uuid, decision="approve", actor="operator-test"
        )
    )
    alert = await WatchActivationService(session).activate(
        ActivateWatchRequest(item_uuid=item.item_uuid, actor="operator-test")
    )
    assert alert.conditions[0]["op"] == "between"
    assert alert.combine == "and"
    # flat primary 요약 (between → operator='between', threshold=low, high)
    assert alert.operator == "between"
    assert Decimal(alert.threshold) == Decimal("50000")
    assert Decimal(alert.threshold_high) == Decimal("55000")
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_investment_reports_watch_activation.py -k "maps_conditions" -v`
Expected: FAIL — 활성화가 conditions/combine/threshold_high를 매핑하지 않음(`alert.conditions == []`) 또는 `condition["operator"]` KeyError(zone 입력엔 flat operator 없음).

- [ ] **Step 3: 구현**

`app/services/investment_reports/watch_activation.py` — `condition` 추출부터 `insert_alert` 호출까지(79–103행 영역)를 교체:

```python
        condition: dict[str, Any] = item.watch_condition
        clauses: list[dict[str, Any]] = list(condition.get("conditions") or [])
        if not clauses:
            # legacy flat payload that predates normalization
            clauses = [
                {
                    "metric": condition["metric"],
                    "op": condition["operator"],
                    "threshold": condition.get("threshold"),
                }
            ]
        combine = condition.get("combine", "and")
        primary = clauses[0]
        primary_metric = primary["metric"]
        if primary["op"] == "between":
            primary_operator = "between"
            primary_threshold = _to_decimal(primary.get("low"))
            primary_threshold_high: Decimal | None = _to_decimal(primary.get("high"))
        else:
            primary_operator = primary["op"]
            primary_threshold = _to_decimal(primary.get("threshold"))
            primary_threshold_high = None
        threshold_key = condition.get("threshold_key") or str(primary_threshold)

        alert = await self._repo.insert_alert(
            alert_uuid=None,  # default from PG
            idempotency_key=idempotency_key,
            source_report_uuid=report.report_uuid,
            source_item_uuid=item.item_uuid,
            market=report.market,
            target_kind=item.target_kind,
            symbol=item.symbol,
            metric=primary_metric,
            operator=primary_operator,
            threshold=primary_threshold,
            threshold_high=primary_threshold_high,
            threshold_key=threshold_key,
            conditions=clauses,
            combine=combine,
            intent=item.intent,
            action_mode=condition.get("action_mode", "notify_only"),
            rationale=item.rationale,
            trigger_checklist=list(item.trigger_checklist),
            max_action=dict(item.max_action),
            valid_until=item.valid_until,
        )
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_investment_reports_watch_activation.py -v`
Expected: PASS (신규 + 기존 활성화 테스트 — flat 입력은 legacy 분기로 동일 동작).

- [ ] **Step 5: 커밋**

```bash
git add app/services/investment_reports/watch_activation.py tests/test_investment_reports_watch_activation.py
git commit -m "feat(ROB-403): activation maps conditions/combine + flat primary summary"
```

---

## Task 5: 스캐너 조건 평가 (zone/다중조건)

**Files:**
- Modify: `app/jobs/watch_market_data.py:201-208` (평가 함수 추가)
- Modify: `app/jobs/investment_watch_scanner.py:133-155` (분기)
- Test: `tests/test_watch_condition_evaluation.py` (신규), `tests/test_investment_watch_scanner.py`

- [ ] **Step 1: 평가 단위 실패 테스트 작성**

`tests/test_watch_condition_evaluation.py` 생성:

```python
"""ROB-403 — clause/condition evaluation."""

from __future__ import annotations

import pytest

from app.jobs.watch_market_data import evaluate_clause


@pytest.mark.parametrize(
    "current,clause,expected",
    [
        (100.0, {"metric": "price", "op": "above", "threshold": "90"}, True),
        (80.0, {"metric": "price", "op": "above", "threshold": "90"}, False),
        (80.0, {"metric": "price", "op": "below", "threshold": "90"}, True),
        (52.0, {"metric": "price", "op": "between", "low": "50", "high": "55"}, True),
        (60.0, {"metric": "price", "op": "between", "low": "50", "high": "55"}, False),
        (50.0, {"metric": "price", "op": "between", "low": "50", "high": "55"}, True),
        (None, {"metric": "price", "op": "above", "threshold": "90"}, False),
    ],
)
def test_evaluate_clause(current, clause, expected):
    assert evaluate_clause(current, clause) is expected
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_watch_condition_evaluation.py -v`
Expected: FAIL — `ImportError: cannot import name 'evaluate_clause'`.

- [ ] **Step 3-a: 평가 함수 구현**

`app/jobs/watch_market_data.py` — `is_triggered`(201–208행) 다음에 추가:

```python
def evaluate_clause(current: float | None, clause: dict) -> bool:
    """Evaluate one condition clause against a current value."""
    if current is None:
        return False
    op = clause.get("op")
    if op == "above":
        return current > float(clause["threshold"])
    if op == "below":
        return current < float(clause["threshold"])
    if op == "between":
        return float(clause["low"]) <= current <= float(clause["high"])
    return False


async def evaluate_alert_conditions(
    *,
    target_kind: str,
    symbol: str,
    market: str,
    conditions: list[dict],
    combine: str,
) -> tuple[bool, float | None]:
    """Evaluate normalized conditions. Returns (triggered, primary_value).

    primary_value is the first clause's current value (used for event detail).
    All clauses share the alert's target_kind/symbol/market; only metric varies.
    """
    primary_value: float | None = None
    results: list[bool] = []
    for idx, clause in enumerate(conditions):
        value = await get_current_value(
            target_kind=target_kind,
            metric=clause["metric"],
            symbol=symbol,
            market=market,
        )
        if idx == 0:
            primary_value = value
        results.append(evaluate_clause(value, clause))
    triggered = bool(results) and all(results)  # combine == "and"
    return triggered, primary_value
```

- [ ] **Step 4-a: 평가 단위 통과 확인**

Run: `uv run pytest tests/test_watch_condition_evaluation.py -v`
Expected: PASS.

- [ ] **Step 4-b: 스캐너 분기 구현**

`app/jobs/investment_watch_scanner.py` — current_value 조회 + is_triggered 블록(133–155행)을 교체:

```python
                try:
                    if alert.conditions:
                        triggered, current_value = await evaluate_alert_conditions(
                            target_kind=alert.target_kind,
                            symbol=alert.symbol,
                            market=alert.market,
                            conditions=alert.conditions,
                            combine=alert.combine,
                        )
                    else:
                        current_value = await get_current_value(
                            target_kind=alert.target_kind,
                            metric=alert.metric,
                            symbol=alert.symbol,
                            market=alert.market,
                        )
                        triggered = is_triggered(
                            current_value, alert.operator, float(alert.threshold)
                        )
                except Exception as exc:
                    logger.warning(
                        "investment-watch lookup failed: "
                        "alert_uuid=%s market=%s symbol=%s metric=%s error=%s",
                        alert.alert_uuid,
                        alert.market,
                        alert.symbol,
                        alert.metric,
                        exc,
                    )
                    stats.failed_lookups += 1
                    continue

                if not triggered:
                    continue
```

`investment_watch_scanner.py` 상단 import에 `evaluate_alert_conditions` 추가(기존 `from app.jobs.watch_market_data import ... is_triggered, get_current_value` 라인에 합류).

- [ ] **Step 5: flat 스캐너 회귀 확인**

분기 리팩터가 기존 flat 트리거를 깨지 않았는지 확인(신규 테스트 없음 — 기존 테스트가 flat 경로 커버):

Run: `uv run pytest tests/test_investment_watch_scanner.py tests/test_watch_condition_evaluation.py -v`
Expected: PASS (기존 flat 트리거 + 신규 evaluate 단위). zone end-to-end는 Task 6(payload widening) 이후.

- [ ] **Step 6: 커밋**

```bash
git add app/jobs/watch_market_data.py app/jobs/investment_watch_scanner.py tests/test_watch_condition_evaluation.py
git commit -m "feat(ROB-403): scanner evaluates zone/multi-condition (flat fallback)"
```

---

## Task 6: between operator 전파 (Hermes payload + event emission) + zone end-to-end

**Files:**
- Modify: `app/services/hermes_client.py:65-66` (ReviewTriggerPayload)
- Modify: `app/jobs/investment_watch_scanner.py:248-268` (insert_event) + `:284-307` (payload)
- Test: `tests/test_investment_watch_scanner.py`

- [ ] **Step 1: zone end-to-end 실패 테스트 작성**

`tests/test_investment_watch_scanner.py`에 추가. 상단 import에 `WatchConditionClause`를 `app.schemas.investment_reports`에서 추가:

```python
@pytest.mark.asyncio
async def test_scan_market_triggers_on_zone_inside(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.jobs.watch_market_data as wmd

    ingest = InvestmentReportIngestionService(session)
    report = await ingest.ingest(
        IngestReportRequest(
            report_type="kr_morning",
            market="kr",
            market_session="regular",
            account_scope="kis_mock",
            execution_mode="mock_preview",
            created_by_profile="test",
            title="t",
            summary="s",
            kst_date="2026-05-18",
            items=[
                IngestReportItem(
                    client_item_key="watch-zone",
                    item_kind="watch",
                    symbol="005930",
                    intent="buy_review",
                    rationale="zone",
                    watch_condition=WatchConditionPayload(
                        conditions=[
                            WatchConditionClause(
                                metric="price",
                                op="between",
                                low=Decimal("50000"),
                                high=Decimal("55000"),
                            )
                        ]
                    ),
                    valid_until=future_datetime(days=30),
                )
            ],
        )
    )
    repo = InvestmentReportsRepository(session)
    item = (await repo.list_items_for_report(report.id))[0]
    await InvestmentReportDecisionService(session).record(
        RecordDecisionRequest(item_uuid=item.item_uuid, decision="approve", actor="op")
    )
    await WatchActivationService(session).activate(
        ActivateWatchRequest(item_uuid=item.item_uuid, actor="op")
    )
    await session.commit()

    async def _price_inside(**_kwargs) -> float:
        return 52000.0  # inside [50000, 55000] → triggered

    monkeypatch.setattr(scanner_module, "is_market_open", lambda _market: True)
    monkeypatch.setattr(wmd, "get_current_value", _price_inside)

    stub = _StubHermesClient()
    scanner = InvestmentWatchScanner(hermes_client=stub)
    summary = await scanner.scan_market("kr")

    assert summary["triggered"] == 1
    assert len(stub.calls) == 1
    payload = stub.calls[0]
    assert payload.operator == "between"
    assert payload.threshold == Decimal("50000")
    assert payload.threshold_high == Decimal("55000")
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_investment_watch_scanner.py -k "zone_inside" -v`
Expected: FAIL — event insert가 `ck_investment_watch_events_operator`(between 미허용)에 걸리거나 `ReviewTriggerPayload`가 `operator='between'`/`threshold_high` 미지원으로 ValidationError.

- [ ] **Step 3-a: ReviewTriggerPayload 확장**

`app/services/hermes_client.py` — `operator` 필드(66행) 타입을 확장하고 `threshold` 다음에 `threshold_high` 추가. (파일 상단에서 `WatchClauseOpLiteral`를 `app.schemas.investment_reports`에서 import; 순환참조 위험 시 `Literal["above","below","between"]`를 인라인 정의.)

```python
    operator: Literal["above", "below", "between"]
```

`threshold: Decimal` 필드 바로 다음 줄에 추가:

```python
    threshold_high: Decimal | None = None
```

- [ ] **Step 3-b: event emission에 threshold_high 전달**

`app/jobs/investment_watch_scanner.py` — `_upsert_event` 안에서 alert의 threshold_high를 로컬로 잡는다. `alert_threshold = ...`를 잡는 곳 근처(약 240행 위)에 `alert_threshold_high = alert.threshold_high` 추가. 그다음:

`repo.insert_event(...)`(248–268행) 호출 인자에 추가:

```python
                threshold=alert_threshold,
                threshold_high=alert_threshold_high,
```

`ReviewTriggerPayload(...)`(284–307행) 인자에 추가:

```python
            threshold=Decimal(str(event.threshold)),
            threshold_high=(
                Decimal(str(event.threshold_high))
                if event.threshold_high is not None
                else None
            ),
```

> `repo.insert_event`가 `**fields` 형태가 아니라 명시 시그니처면 `threshold_high` 파라미터를 추가하고 event row에 매핑한다. (repository.py의 insert_event 확인 후 동일 패턴으로.)

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_investment_watch_scanner.py -k "zone_inside" -v`
Expected: PASS.

- [ ] **Step 5: 커밋**

```bash
git add app/services/hermes_client.py app/jobs/investment_watch_scanner.py tests/test_investment_watch_scanner.py
git commit -m "feat(ROB-403): propagate between operator + threshold_high to event/Hermes payload"
```

---

## Task 7: 회귀 + lint/format/typecheck

**Files:** (검증만)

- [ ] **Step 1: 관련 스위트 회귀**

Run:
```bash
uv run pytest tests/test_investment_reports_schemas.py tests/test_investment_reports_watch_activation.py tests/test_watch_condition_evaluation.py tests/test_investment_watch_scanner.py -p no:randomly -v
```
Expected: 전부 PASS.

- [ ] **Step 2: lint + format**

Run:
```bash
uv run ruff check app/ tests/
uv run ruff format --check app/ tests/
```
Expected: 통과(필요 시 `uv run ruff format app/ tests/` 후 재확인 + 커밋).

- [ ] **Step 3: typecheck (변경 파일)**

Run:
```bash
uv run ty check app/schemas/investment_reports.py app/models/investment_reports.py app/jobs/watch_market_data.py app/jobs/investment_watch_scanner.py app/services/investment_reports/watch_activation.py app/services/hermes_client.py
```
Expected: 통과.

- [ ] **Step 4: 커밋(필요 시 format)**

```bash
git add -A && git commit -m "style(ROB-403): ruff format" || echo "nothing to format"
```

---

## 검증 / 인수 기준

- watch_condition이 zone(between)·다중메트릭 AND를 표현, 구형 flat 입력은 단일조건으로 정규화(무손상).
- 스캐너가 conditions를 평가(zone inside 트리거, multi-AND 전부 충족 시만); legacy alert는 flat fallback.
- `max_action`이 side/quantity|notional(XOR)/limit_price/account_mode로 검증되며 기존 키 보존.
- alert에 conditions/combine/threshold_high 영속 + operator='between' 허용; flat primary 요약 동기.
- migration 포함, operator `alembic upgrade head` 별도.

## 범위 밖 (후속)

- ROB-402: max_action을 실제 주문 파라미터로 사용 + (auto_execute_mock+live) reject.
- ROB-393: review-op activate 계약(별도 이슈).
- `combine="or"`, 추가 metric, `valid_until` 자동만료.

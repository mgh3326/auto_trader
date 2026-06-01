# ROB-402 — auto_execute_mock watch→kis_mock 자동집행 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** watch alert에 `auto_execute_mock` action_mode를 추가하고, 트리거 시 mock 계좌면 `watch_order_intent_ledger`에 intent를 기록한 뒤 `kis_mock_place_order`를 호출하도록 배선한다. live 자동집행은 코드 가드+DB CHECK+테스트로 영구 차단하고, 전체를 default-off inert로 둔다.

**Architecture:** 스캐너가 auto_execute_mock alert 트리거 시 `maybe_auto_execute` 서비스를 호출 → live-block 가드 + global flag + max_action 검증 → intent row(previewed|failed) 기록 → 통과 시 `_place_order_impl(is_mock=True, correlation_id=...)` 호출(executor는 항상 mock 하드핀). 멱등=intent ledger correlation_id UNIQUE. 실제 주문은 KISMockOrderLedger(correlation_id 링크).

**Tech Stack:** Python 3.13, SQLAlchemy async + pg_insert ON CONFLICT, Pydantic v2, pytest/pytest-asyncio.

**의존(중요)**: 403 `MaxActionPayload`(#1075)에 코드 의존 → **#1075 머지 후 origin/main에 rebase**하고 구현. 현 worktree는 origin/main 기준이라 rebase 전엔 `MaxActionPayload` import 불가.

**Spec:** `docs/superpowers/specs/2026-06-01-rob-402-auto-execute-mock-design.md`

---

## File Structure

| 파일 | 역할 | 변경 |
|---|---|---|
| `app/core/config.py` | 게이트 | `WATCH_AUTO_EXECUTE_MOCK_ENABLED: bool = False` |
| `app/schemas/investment_reports.py` | action_mode literal | `auto_execute_mock` 추가 |
| `app/models/investment_reports.py` | CHECK×2 | alert+event action_mode CHECK widening |
| `app/models/review.py` | intent ledger ORM | `WatchOrderIntentLedger`(마이그레이션 미러) |
| `alembic/versions/<rev>_rob402_*.py` | 마이그레이션 | action_mode CHECK 2곳 |
| `tests/conftest.py` + `tests/_investment_reports_helpers.py` | DDL drift | action_mode CHECK 패치(alert+event) |
| `app/services/investment_reports/auto_execute_guard.py` | live-block 가드 | 신규 |
| `app/services/investment_reports/watch_auto_execute.py` | 자동집행 서비스 | 신규 `maybe_auto_execute` |
| `app/mcp_server/tooling/order_execution.py` + `app/mcp_server/tooling/kis_mock_ledger.py` | correlation_id 스레딩 | 인자 추가 |
| `app/jobs/investment_watch_scanner.py` | 훅 | outcome dict + maybe_auto_execute 호출 |
| `tests/test_auto_execute_guard.py` / `tests/test_watch_auto_execute.py` / 스캐너·스키마·ledger 테스트 | | 신규/추가 |

---

## Task 1: config 플래그 + action_mode literal/CHECK + 마이그레이션 + conftest

**Files:**
- Modify: `app/core/config.py:460`, `app/schemas/investment_reports.py:59`, `app/models/investment_reports.py:470,608`
- Modify: `tests/conftest.py`, `tests/_investment_reports_helpers.py`
- Create: `alembic/versions/<rev>_rob402_auto_execute_mock.py`
- Test: `tests/test_investment_reports_schemas.py`

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_investment_reports_schemas.py` 끝에 추가:

```python
def test_auto_execute_mock_action_mode_flag_and_literal():
    from app.core.config import settings
    from app.schemas.investment_reports import WatchConditionPayload

    assert settings.WATCH_AUTO_EXECUTE_MOCK_ENABLED is False
    # auto_execute_mock is now a valid action_mode literal value
    p = WatchConditionPayload(
        metric="price", operator="below", threshold="5", action_mode="auto_execute_mock"
    )
    assert p.action_mode == "auto_execute_mock"
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_investment_reports_schemas.py -k "auto_execute_mock_action_mode" -v`
Expected: FAIL — `AttributeError`(flag 없음) 또는 ValidationError(literal 미허용).

- [ ] **Step 3-a: config 플래그**

`app/core/config.py` — `EXECUTION_LEDGER_COMMIT_ENABLED`(460행) 다음에:

```python
    # ROB-402 — watch auto_execute_mock. Default off: the merged PR is inert
    # (no real mock orders) until an operator flips this.
    WATCH_AUTO_EXECUTE_MOCK_ENABLED: bool = False
```

- [ ] **Step 3-b: literal 확장**

`app/schemas/investment_reports.py:59` 교체:

```python
WatchActionModeLiteral = Literal[
    "notify_only", "preview_only", "approval_required", "auto_execute_mock"
]
```

- [ ] **Step 3-c: CHECK×2 확장**

`app/models/investment_reports.py` — `ck_investment_watch_alerts_action_mode`(470 근처) + `ck_investment_watch_events_action_mode`(608 근처) 두 CheckConstraint를 교체:

```python
        CheckConstraint(
            "action_mode IN ('notify_only','preview_only','approval_required',"
            "'auto_execute_mock')",
            name="ck_investment_watch_alerts_action_mode",
        ),
```
(event 쪽은 name=`ck_investment_watch_events_action_mode`로 동일 패턴.)

- [ ] **Step 3-d: conftest×2 DDL drift 패치**

`tests/conftest.py`(db_session 블록, ROB-403 패치 근처)에 추가:

```python
                # ROB-402 — action_mode auto_execute_mock on alerts + events.
                for _t in ("investment_watch_alerts", "investment_watch_events"):
                    _c = f"ck_{_t}_action_mode"
                    await conn.execute(
                        text(f"ALTER TABLE review.{_t} DROP CONSTRAINT IF EXISTS {_c}")
                    )
                    await conn.execute(
                        text(
                            f"ALTER TABLE review.{_t} ADD CONSTRAINT {_c} "
                            "CHECK (action_mode IN ('notify_only','preview_only',"
                            "'approval_required','auto_execute_mock'))"
                        )
                    )
```

`tests/_investment_reports_helpers.py`의 `for stmt in ( ... ):` 튜플 끝에 4개 문자열 추가:

```python
                        "ALTER TABLE review.investment_watch_alerts "
                        "DROP CONSTRAINT IF EXISTS ck_investment_watch_alerts_action_mode",
                        "ALTER TABLE review.investment_watch_alerts "
                        "ADD CONSTRAINT ck_investment_watch_alerts_action_mode "
                        "CHECK (action_mode IN ('notify_only','preview_only',"
                        "'approval_required','auto_execute_mock'))",
                        "ALTER TABLE review.investment_watch_events "
                        "DROP CONSTRAINT IF EXISTS ck_investment_watch_events_action_mode",
                        "ALTER TABLE review.investment_watch_events "
                        "ADD CONSTRAINT ck_investment_watch_events_action_mode "
                        "CHECK (action_mode IN ('notify_only','preview_only',"
                        "'approval_required','auto_execute_mock'))",
```

- [ ] **Step 3-e: alembic 마이그레이션**

```bash
uv run alembic heads
uv run alembic revision -m "rob402 auto_execute_mock action_mode"
```

생성 파일 본문(`down_revision`=현재 head):

```python
"""rob402 auto_execute_mock action_mode

Revision ID: <자동생성>
Revises: <현재 head>
"""

from alembic import op

revision = "<자동생성>"
down_revision = "<현재 head>"
branch_labels = None
depends_on = None

_S = "review"
_NEW = (
    "action_mode IN ('notify_only','preview_only','approval_required',"
    "'auto_execute_mock')"
)
_OLD = "action_mode IN ('notify_only','preview_only','approval_required')"
_TABLES = (
    ("investment_watch_alerts", "ck_investment_watch_alerts_action_mode"),
    ("investment_watch_events", "ck_investment_watch_events_action_mode"),
)


def upgrade() -> None:
    for table, name in _TABLES:
        op.drop_constraint(name, table, schema=_S, type_="check")
        op.create_check_constraint(name, table, _NEW, schema=_S)


def downgrade() -> None:
    for table, name in _TABLES:
        op.drop_constraint(name, table, schema=_S, type_="check")
        op.create_check_constraint(name, table, _OLD, schema=_S)
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_investment_reports_schemas.py -k "auto_execute_mock_action_mode" -v`
Expected: PASS.

- [ ] **Step 5: 커밋**

```bash
git add app/core/config.py app/schemas/investment_reports.py app/models/investment_reports.py tests/conftest.py tests/_investment_reports_helpers.py alembic/versions tests/test_investment_reports_schemas.py
git commit -m "feat(ROB-402): auto_execute_mock action_mode literal + CHECK + gate flag"
```

---

## Task 2: WatchOrderIntentLedger ORM

**Files:**
- Modify: `app/models/review.py` (ORM 클래스 추가)
- Test: `tests/test_watch_order_intent_ledger.py`

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_watch_order_intent_ledger.py` 생성:

```python
"""ROB-402 — WatchOrderIntentLedger ORM mirrors migration daf4130b13ce."""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.review import WatchOrderIntentLedger


def _row(**over):
    base = dict(
        correlation_id=f"corr-{uuid4().hex}",
        idempotency_key=f"idem-{uuid4().hex}",
        market="kr",
        target_kind="asset",
        symbol="005930",
        condition_type="below",
        threshold=Decimal("55000"),
        threshold_key="55000",
        action="auto_execute_mock",
        side="buy",
        account_mode="kis_mock",
        execution_source="watch",
        lifecycle_state="previewed",
        preview_line={"symbol": "005930", "side": "buy"},
        kst_date="2026-06-01",
    )
    base.update(over)
    return WatchOrderIntentLedger(**base)


@pytest.mark.asyncio
async def test_intent_row_inserts(db_session: AsyncSession):
    row = _row()
    db_session.add(row)
    await db_session.commit()
    assert row.id is not None
    assert row.execution_allowed is False  # server default


@pytest.mark.asyncio
async def test_intent_account_mode_check_blocks_live(db_session: AsyncSession):
    db_session.add(_row(account_mode="kis_live"))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_watch_order_intent_ledger.py -v`
Expected: FAIL — `ImportError: cannot import name 'WatchOrderIntentLedger'`.

- [ ] **Step 3: ORM 추가**

`app/models/review.py` 끝에 추가(마이그레이션 `daf4130b13ce` 정확 미러):

```python
class WatchOrderIntentLedger(Base):
    """ROB-402 — watch-sourced order intent (kis_mock only). Audit of the
    auto-execute decision; the actual order lives in KISMockOrderLedger linked
    by correlation_id. Mirrors migration daf4130b13ce."""

    __tablename__ = "watch_order_intent_ledger"
    __table_args__ = (
        UniqueConstraint("correlation_id", name="uq_watch_intent_correlation_id"),
        CheckConstraint(
            "lifecycle_state IN ('previewed','failed')",
            name="watch_intent_ledger_lifecycle_state",
        ),
        CheckConstraint("side IN ('buy','sell')", name="watch_intent_ledger_side"),
        CheckConstraint(
            "account_mode = 'kis_mock'", name="watch_intent_ledger_account_mode"
        ),
        CheckConstraint(
            "execution_source = 'watch'", name="watch_intent_ledger_execution_source"
        ),
        CheckConstraint(
            "currency IS NULL OR currency IN ('KRW','USD')",
            name="watch_intent_ledger_currency",
        ),
        Index("ix_watch_intent_kst_date", "kst_date"),
        Index("ix_watch_intent_market_symbol", "market", "symbol"),
        Index("ix_watch_intent_state_created_at", "lifecycle_state", "created_at"),
        Index(
            "uq_watch_intent_previewed_idempotency",
            "idempotency_key",
            unique=True,
            postgresql_where=text("lifecycle_state = 'previewed'"),
        ),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    correlation_id: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    market: Mapped[str] = mapped_column(Text, nullable=False)
    target_kind: Mapped[str] = mapped_column(Text, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    condition_type: Mapped[str] = mapped_column(Text, nullable=False)
    threshold: Mapped[float] = mapped_column(Numeric(18, 8), nullable=False)
    threshold_key: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    side: Mapped[str] = mapped_column(Text, nullable=False)
    account_mode: Mapped[str] = mapped_column(Text, nullable=False)
    execution_source: Mapped[str] = mapped_column(Text, nullable=False)
    lifecycle_state: Mapped[str] = mapped_column(Text, nullable=False)
    quantity: Mapped[float | None] = mapped_column(Numeric(18, 8))
    limit_price: Mapped[float | None] = mapped_column(Numeric(18, 8))
    notional: Mapped[float | None] = mapped_column(Numeric(18, 8))
    currency: Mapped[str | None] = mapped_column(Text)
    notional_krw_input: Mapped[float | None] = mapped_column(Numeric(18, 2))
    max_notional_krw: Mapped[float | None] = mapped_column(Numeric(18, 2))
    notional_krw_evaluated: Mapped[float | None] = mapped_column(Numeric(18, 2))
    fx_usd_krw_used: Mapped[float | None] = mapped_column(Numeric(18, 4))
    approval_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    execution_allowed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    blocking_reasons: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    blocked_by: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    preview_line: Mapped[dict] = mapped_column(JSONB, nullable=False)
    triggered_value: Mapped[float | None] = mapped_column(Numeric(18, 8))
    kst_date: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
```

> `app/models/review.py` 상단 import에 `Boolean`이 없으면 추가(`from sqlalchemy import ..., Boolean`). `text`, `func`, `Index`, `Numeric`, `TIMESTAMP`, `JSONB`, `Mapped`, `mapped_column`은 기존 import 확인 후 부족분 추가.

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_watch_order_intent_ledger.py -v`
Expected: PASS (insert + live CHECK reject).

- [ ] **Step 5: 커밋**

```bash
git add app/models/review.py tests/test_watch_order_intent_ledger.py
git commit -m "feat(ROB-402): WatchOrderIntentLedger ORM (mirrors daf4130b13ce)"
```

---

## Task 3: Live-block 가드

**Files:**
- Create: `app/services/investment_reports/auto_execute_guard.py`
- Test: `tests/test_auto_execute_guard.py`

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_auto_execute_guard.py` 생성:

```python
"""ROB-402 — live auto-execute is permanently blocked."""

import pytest

from app.services.investment_reports.auto_execute_guard import (
    AutoExecuteLiveBlocked,
    AutoExecuteUnsupported,
    assert_auto_execute_account_allowed,
)


def test_live_account_blocked():
    with pytest.raises(AutoExecuteLiveBlocked):
        assert_auto_execute_account_allowed("auto_execute_mock", "kis_live")
    with pytest.raises(AutoExecuteLiveBlocked):
        assert_auto_execute_account_allowed("auto_execute_mock", "upbit_live")


def test_kiwoom_mock_unsupported():
    with pytest.raises(AutoExecuteUnsupported):
        assert_auto_execute_account_allowed("auto_execute_mock", "kiwoom_mock")


def test_kis_mock_allowed():
    assert_auto_execute_account_allowed("auto_execute_mock", "kis_mock")  # no raise


def test_non_auto_mode_is_noop():
    # any account is fine when not auto-executing
    assert_auto_execute_account_allowed("notify_only", "kis_live")  # no raise
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_auto_execute_guard.py -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: 구현**

`app/services/investment_reports/auto_execute_guard.py` 생성:

```python
"""ROB-402 — auto-execute account guard. live is permanently blocked."""

from __future__ import annotations

_LIVE_ACCOUNT_MODES = frozenset({"kis_live", "upbit_live"})
_AUTO_EXECUTE_ALLOWED = frozenset({"kis_mock"})  # kiwoom_mock = ROB-399 follow-up


class AutoExecuteLiveBlocked(Exception):
    """auto_execute_mock attempted against a live account — never allowed."""

    def __init__(self, account_mode: str) -> None:
        super().__init__(
            f"auto_execute_mock is permanently blocked for live account "
            f"'{account_mode}'"
        )
        self.account_mode = account_mode


class AutoExecuteUnsupported(Exception):
    """auto_execute_mock against a non-live, non-kis_mock account (not yet wired)."""

    def __init__(self, account_mode: str) -> None:
        super().__init__(
            f"auto_execute_mock is not supported for account '{account_mode}'"
        )
        self.account_mode = account_mode


def assert_auto_execute_account_allowed(action_mode: str, account_mode: str) -> None:
    if action_mode != "auto_execute_mock":
        return
    if account_mode in _LIVE_ACCOUNT_MODES:
        raise AutoExecuteLiveBlocked(account_mode)
    if account_mode not in _AUTO_EXECUTE_ALLOWED:
        raise AutoExecuteUnsupported(account_mode)
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_auto_execute_guard.py -v`
Expected: PASS.

- [ ] **Step 5: 커밋**

```bash
git add app/services/investment_reports/auto_execute_guard.py tests/test_auto_execute_guard.py
git commit -m "feat(ROB-402): auto-execute live-block guard"
```

---

## Task 4: correlation_id 스레딩 (place_order → mock ledger)

**Files:**
- Modify: `app/mcp_server/tooling/order_execution.py` (`_place_order_impl` 인자 + 전달)
- Modify: `app/mcp_server/tooling/kis_mock_ledger.py` (`_record_kis_mock_order` 인자 + save 전달)
- Test: `tests/test_kis_mock_order_ledger.py`

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_kis_mock_order_ledger.py` 끝에 추가:

```python
@pytest.mark.asyncio
async def test_place_order_impl_threads_correlation_id(db_session, monkeypatch):
    from app.mcp_server.tooling import order_execution
    from app.models.review import KISMockOrderLedger
    from sqlalchemy import select

    result = await order_execution._place_order_impl(
        symbol="005930",
        side="buy",
        order_type="limit",
        quantity=10,
        price=55000,
        dry_run=False,
        reason="rob402-test",
        is_mock=True,
        correlation_id="corr-rob402",
    )
    assert result["success"] is True
    row = (
        await db_session.execute(
            select(KISMockOrderLedger).where(
                KISMockOrderLedger.correlation_id == "corr-rob402"
            )
        )
    ).scalar_one_or_none()
    assert row is not None
```

> 이 테스트는 KIS mock holdings 호출이 필요할 수 있다. 기존 `tests/test_kis_mock_order_ledger.py`의 mock 패턴(KISClient/holdings monkeypatch)을 동일 적용한다. 핵심 단언은 `correlation_id`가 ledger에 기록되는 것.

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_kis_mock_order_ledger.py -k "threads_correlation_id" -v`
Expected: FAIL — `_place_order_impl() got an unexpected keyword argument 'correlation_id'`.

- [ ] **Step 3-a: `_place_order_impl` 인자 + 전달**

`app/mcp_server/tooling/order_execution.py` — `_place_order_impl` 시그니처 끝(`scalping_exit_reason: str | None = None,` 다음)에 추가:

```python
    correlation_id: str | None = None,
```

`_record_kis_mock_order(...)` 호출(707행)에 인자 추가:

```python
        return await _record_kis_mock_order(
            normalized_symbol=normalized_symbol,
            market_type=market_type,
            side=side,
            order_type=order_type,
            dry_run_result=dry_run_result,
            execution_result=execution_result,
            reason=reason,
            thesis=thesis,
            strategy=strategy,
            notes=notes,
            holdings_baseline_qty=kis_mock_baseline_qty,
            correlation_id=correlation_id,
        )
```

- [ ] **Step 3-b: `_record_kis_mock_order` 인자 + save 전달**

`app/mcp_server/tooling/kis_mock_ledger.py` — `_record_kis_mock_order` 시그니처에 `correlation_id: str | None = None,` 추가(`holdings_baseline_qty` 다음), 그리고 `_save_kis_mock_order_ledger(...)` 호출에 `correlation_id=correlation_id,` 추가(save는 이미 파라미터 보유).

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_kis_mock_order_ledger.py -v`
Expected: PASS.

- [ ] **Step 5: 커밋**

```bash
git add app/mcp_server/tooling/order_execution.py app/mcp_server/tooling/kis_mock_ledger.py tests/test_kis_mock_order_ledger.py
git commit -m "feat(ROB-402): thread correlation_id place_order → kis_mock ledger"
```

---

## Task 5: 자동집행 서비스 `maybe_auto_execute`

**Files:**
- Create: `app/services/investment_reports/watch_auto_execute.py`
- Test: `tests/test_watch_auto_execute.py`

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_watch_auto_execute.py` 생성:

```python
"""ROB-402 — maybe_auto_execute service."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.investment_reports import InvestmentWatchAlert
from app.models.review import WatchOrderIntentLedger
from app.services.investment_reports import watch_auto_execute


def _alert(max_action: dict | None, action_mode="auto_execute_mock"):
    return InvestmentWatchAlert(
        alert_uuid=uuid.uuid4(),
        idempotency_key=f"k-{uuid.uuid4()}",
        source_report_uuid=uuid.uuid4(),
        source_item_uuid=uuid.uuid4(),
        market="kr",
        target_kind="asset",
        symbol="005930",
        metric="price",
        operator="below",
        threshold=Decimal("55000"),
        threshold_key="55000",
        intent="buy_review",
        action_mode=action_mode,
        rationale="r",
        max_action=max_action or {},
        valid_until=datetime(2026, 12, 31, tzinfo=UTC),
    )


def _good_max_action():
    return {
        "side": "buy",
        "quantity": "10",
        "limit_price": "55000",
        "account_mode": "kis_mock",
    }


async def _intent_for(db, correlation_id):
    return (
        await db.execute(
            select(WatchOrderIntentLedger).where(
                WatchOrderIntentLedger.correlation_id == correlation_id
            )
        )
    ).scalar_one_or_none()


def _make_place_spy():
    calls = []

    async def _spy(**kwargs):
        calls.append(kwargs)
        return {"success": True, "order_no": "X1"}

    return _spy, calls


@pytest.mark.asyncio
async def test_global_flag_off_blocks(db_session: AsyncSession, monkeypatch):
    monkeypatch.setattr(
        watch_auto_execute.settings, "WATCH_AUTO_EXECUTE_MOCK_ENABLED", False
    )
    spy, calls = _make_place_spy()
    alert = _alert(_good_max_action())
    cid = f"corr-{uuid.uuid4().hex}"
    outcome = await watch_auto_execute.maybe_auto_execute(
        db_session, alert=alert, correlation_id=cid, kst_date="2026-06-01",
        place_order_fn=spy,
    )
    assert outcome["executed"] is False
    assert "auto_execute_globally_disabled" in outcome["blocking_reasons"]
    assert calls == []
    row = await _intent_for(db_session, cid)
    assert row.lifecycle_state == "failed"


@pytest.mark.asyncio
async def test_happy_path_places_order(db_session: AsyncSession, monkeypatch):
    monkeypatch.setattr(
        watch_auto_execute.settings, "WATCH_AUTO_EXECUTE_MOCK_ENABLED", True
    )
    spy, calls = _make_place_spy()
    alert = _alert(_good_max_action())
    cid = f"corr-{uuid.uuid4().hex}"
    outcome = await watch_auto_execute.maybe_auto_execute(
        db_session, alert=alert, correlation_id=cid, kst_date="2026-06-01",
        place_order_fn=spy,
    )
    assert outcome["executed"] is True
    assert len(calls) == 1
    assert calls[0]["is_mock"] is True
    assert calls[0]["dry_run"] is False
    assert calls[0]["correlation_id"] == cid
    assert calls[0]["symbol"] == "005930"
    assert calls[0]["side"] == "buy"
    row = await _intent_for(db_session, cid)
    assert row.lifecycle_state == "previewed"
    assert row.execution_allowed is True


@pytest.mark.asyncio
async def test_idempotent_on_duplicate_correlation_id(db_session, monkeypatch):
    monkeypatch.setattr(
        watch_auto_execute.settings, "WATCH_AUTO_EXECUTE_MOCK_ENABLED", True
    )
    spy, calls = _make_place_spy()
    alert = _alert(_good_max_action())
    cid = f"corr-{uuid.uuid4().hex}"
    await watch_auto_execute.maybe_auto_execute(
        db_session, alert=alert, correlation_id=cid, kst_date="2026-06-01",
        place_order_fn=spy,
    )
    second = await watch_auto_execute.maybe_auto_execute(
        db_session, alert=alert, correlation_id=cid, kst_date="2026-06-01",
        place_order_fn=spy,
    )
    assert second["executed"] is False
    assert second.get("skipped") == "duplicate"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_live_account_blocked_no_order(db_session, monkeypatch):
    monkeypatch.setattr(
        watch_auto_execute.settings, "WATCH_AUTO_EXECUTE_MOCK_ENABLED", True
    )
    spy, calls = _make_place_spy()
    alert = _alert({**_good_max_action(), "account_mode": "kis_live"})
    cid = f"corr-{uuid.uuid4().hex}"
    outcome = await watch_auto_execute.maybe_auto_execute(
        db_session, alert=alert, correlation_id=cid, kst_date="2026-06-01",
        place_order_fn=spy,
    )
    assert outcome["executed"] is False
    assert outcome["blocked_by"] == "live_account"
    assert calls == []
    # no kis_mock intent row written for a live attempt
    assert await _intent_for(db_session, cid) is None


@pytest.mark.asyncio
async def test_missing_limit_price_blocks(db_session, monkeypatch):
    monkeypatch.setattr(
        watch_auto_execute.settings, "WATCH_AUTO_EXECUTE_MOCK_ENABLED", True
    )
    spy, calls = _make_place_spy()
    ma = _good_max_action()
    ma.pop("limit_price")
    alert = _alert(ma)
    cid = f"corr-{uuid.uuid4().hex}"
    outcome = await watch_auto_execute.maybe_auto_execute(
        db_session, alert=alert, correlation_id=cid, kst_date="2026-06-01",
        place_order_fn=spy,
    )
    assert outcome["executed"] is False
    assert "missing_limit_price" in outcome["blocking_reasons"]
    assert calls == []
    assert (await _intent_for(db_session, cid)).lifecycle_state == "failed"
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_watch_auto_execute.py -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: 구현**

`app/services/investment_reports/watch_auto_execute.py` 생성:

```python
"""ROB-402 — watch auto_execute_mock service.

Records an intent (audit) and, when all gates pass, places a kis_mock order.
The executor is hard-pinned is_mock=True; the live-block guard rejects explicit
live/non-mock accounts before any insert. Default off via gate flag.
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.config import settings
from app.models.review import WatchOrderIntentLedger
from app.services.investment_reports.auto_execute_guard import (
    AutoExecuteLiveBlocked,
    AutoExecuteUnsupported,
    assert_auto_execute_account_allowed,
)

logger = logging.getLogger(__name__)


def _to_decimal(v: Any) -> Decimal | None:
    if v in (None, ""):
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, TypeError, ValueError):
        return None


async def _default_place_order_fn(**kwargs):
    # Lazy import to avoid heavy import at module load.
    from app.mcp_server.tooling.order_execution import _place_order_impl

    return await _place_order_impl(**kwargs)


async def maybe_auto_execute(
    db,
    *,
    alert,
    correlation_id: str,
    kst_date: str,
    place_order_fn: Callable[..., Any] = _default_place_order_fn,
) -> dict[str, Any]:
    """Evaluate gates and (if all pass) place a kis_mock order for the alert."""
    if alert.action_mode != "auto_execute_mock":
        return {"executed": False, "skipped": "not_auto_execute_mock"}

    max_action: dict = alert.max_action or {}
    account_mode = max_action.get("account_mode") or "kis_mock"

    # 1) live-block guard (hard reject before any insert).
    try:
        assert_auto_execute_account_allowed("auto_execute_mock", account_mode)
    except AutoExecuteLiveBlocked:
        logger.warning(
            "auto_execute_mock blocked for live account on alert %s", alert.alert_uuid
        )
        return {"executed": False, "blocked_by": "live_account"}
    except AutoExecuteUnsupported:
        logger.warning(
            "auto_execute_mock unsupported account on alert %s", alert.alert_uuid
        )
        return {"executed": False, "blocked_by": "unsupported_account"}

    # 2) precondition checks (account is kis_mock from here on).
    reasons: list[str] = []
    if not settings.WATCH_AUTO_EXECUTE_MOCK_ENABLED:
        reasons.append("auto_execute_globally_disabled")
    side = max_action.get("side")
    quantity = _to_decimal(max_action.get("quantity"))
    limit_price = _to_decimal(max_action.get("limit_price"))
    if side not in ("buy", "sell"):
        reasons.append("missing_or_invalid_side")
    if quantity is None or quantity <= 0:
        reasons.append("missing_quantity")
    if limit_price is None or limit_price <= 0:
        reasons.append("missing_limit_price")

    allowed = not reasons
    lifecycle = "previewed" if allowed else "failed"
    preview_line = {
        "symbol": alert.symbol,
        "side": side,
        "quantity": str(quantity) if quantity is not None else None,
        "limit_price": str(limit_price) if limit_price is not None else None,
        "account_mode": "kis_mock",
        "action_mode": "auto_execute_mock",
    }

    # 3) write intent row (ON CONFLICT correlation_id → idempotent skip).
    stmt = (
        pg_insert(WatchOrderIntentLedger)
        .values(
            correlation_id=correlation_id,
            idempotency_key=f"intent:{alert.alert_uuid}:{kst_date}:{alert.threshold_key}",
            market=alert.market,
            target_kind=alert.target_kind,
            symbol=alert.symbol,
            condition_type=alert.operator,
            threshold=_to_decimal(alert.threshold),
            threshold_key=alert.threshold_key,
            action="auto_execute_mock",
            side=side if side in ("buy", "sell") else "buy",
            account_mode="kis_mock",
            execution_source="watch",
            lifecycle_state=lifecycle,
            quantity=quantity,
            limit_price=limit_price,
            execution_allowed=allowed,
            approval_required=False,
            blocking_reasons=reasons,
            blocked_by=(reasons[0] if reasons else None),
            preview_line=preview_line,
            kst_date=kst_date,
        )
        .on_conflict_do_nothing(constraint="uq_watch_intent_correlation_id")
        .returning(WatchOrderIntentLedger.id)
    )
    result = await db.execute(stmt)
    inserted_id = result.scalar_one_or_none()
    await db.commit()

    if inserted_id is None:
        return {"executed": False, "skipped": "duplicate"}
    if not allowed:
        return {"executed": False, "blocking_reasons": reasons}

    # 4) place the kis_mock order (executor hard-pinned is_mock=True).
    await place_order_fn(
        symbol=alert.symbol,
        side=side,
        order_type="limit",
        quantity=float(quantity),
        price=float(limit_price),
        dry_run=False,
        reason="watch auto_execute_mock",
        is_mock=True,
        correlation_id=correlation_id,
    )
    return {"executed": True, "correlation_id": correlation_id}
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_watch_auto_execute.py -v`
Expected: PASS (5건).

- [ ] **Step 5: 커밋**

```bash
git add app/services/investment_reports/watch_auto_execute.py tests/test_watch_auto_execute.py
git commit -m "feat(ROB-402): maybe_auto_execute service (gates/guard/intent/place)"
```

---

## Task 6: 스캐너 훅

**Files:**
- Modify: `app/jobs/investment_watch_scanner.py:56-65,172-174`
- Test: `tests/test_investment_watch_scanner.py`

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_investment_watch_scanner.py`에 추가(기존 `_seed_active_kr_alert` 헬퍼 재사용; auto_execute_mock alert를 만들고 maybe_auto_execute가 호출되는지 검증):

```python
@pytest.mark.asyncio
async def test_scan_calls_auto_execute_for_auto_execute_mock(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.jobs.investment_watch_scanner as scanner_mod

    await _seed_active_kr_alert(session, action_mode="auto_execute_mock")

    async def _fake_current_value(**_kwargs) -> float:
        return 25.0  # below 30 → triggered

    monkeypatch.setattr(scanner_mod, "is_market_open", lambda _market: True)
    monkeypatch.setattr(scanner_mod, "get_current_value", _fake_current_value)

    captured: list = []

    async def _fake_maybe_auto_execute(db, *, alert, correlation_id, kst_date, **kw):
        captured.append({"symbol": alert.symbol, "cid": correlation_id})
        return {"executed": False, "skipped": "stubbed"}

    monkeypatch.setattr(
        scanner_mod, "maybe_auto_execute", _fake_maybe_auto_execute
    )

    stub = _StubHermesClient()
    scanner = InvestmentWatchScanner(hermes_client=stub)
    summary = await scanner.scan_market("kr")

    assert summary["triggered"] == 1
    assert len(captured) == 1
    assert captured[0]["symbol"] == "005930"
```

> `_seed_active_kr_alert`가 `action_mode`를 watch_condition에 싣는지 확인: 기존 헬퍼는 `action_mode` 인자를 받아 `WatchConditionPayload(action_mode=...)`에 전달하므로 활성화된 alert.action_mode가 'auto_execute_mock'이 된다(Task 1에서 literal 허용).

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_investment_watch_scanner.py -k "calls_auto_execute" -v`
Expected: FAIL — `AttributeError: module ... has no attribute 'maybe_auto_execute'` (import 전) 또는 captured 비어있음(훅 미배선).

- [ ] **Step 3-a: outcome dict + import**

`app/jobs/investment_watch_scanner.py` — `_OUTCOME_BY_ACTION_MODE`(56–65)에 항목 추가:

```python
_OUTCOME_BY_ACTION_MODE: dict[str, str] = {
    "notify_only": "notified",
    "preview_only": "preview_attached",
    "approval_required": "review_required",
    "auto_execute_mock": "executed",
}
```

파일 상단 import에 추가:

```python
from app.services.investment_reports.watch_auto_execute import maybe_auto_execute
```

- [ ] **Step 3-b: 훅 호출**

`scan_market` 루프의 `is_first_attempt` 블록(172–174행) 다음에 추가:

```python
                if is_first_attempt:
                    stats.triggered += 1
                    stats.details.append(emission["detail"])
                    if alert.action_mode == "auto_execute_mock":
                        payload = emission["payload"]
                        try:
                            await maybe_auto_execute(
                                db,
                                alert=alert,
                                correlation_id=payload.correlation_id,
                                kst_date=payload.kst_date,
                            )
                        except Exception:  # noqa: BLE001 - never kill the scan loop
                            logger.exception(
                                "auto_execute_mock failed for alert %s",
                                emission["alert_uuid"],
                            )
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_investment_watch_scanner.py -k "calls_auto_execute" -v`
Expected: PASS.

- [ ] **Step 5: 커밋**

```bash
git add app/jobs/investment_watch_scanner.py tests/test_investment_watch_scanner.py
git commit -m "feat(ROB-402): scanner hooks auto_execute_mock → maybe_auto_execute"
```

---

## Task 7: 회귀 + lint/format/typecheck

**Files:** (검증만)

- [ ] **Step 1: 관련 스위트 회귀**

Run:
```bash
uv run pytest tests/test_investment_reports_schemas.py tests/test_watch_order_intent_ledger.py tests/test_auto_execute_guard.py tests/test_watch_auto_execute.py tests/test_investment_watch_scanner.py tests/test_kis_mock_order_ledger.py tests/test_mcp_kis_order_variants.py -p no:randomly -v
```
Expected: 전부 PASS.

- [ ] **Step 2: lint + format**

Run:
```bash
uv run ruff check app/ tests/
uv run ruff format --check app/ tests/
```
Expected: 통과(필요 시 `uv run ruff format app/ tests/`).

- [ ] **Step 3: typecheck (변경 파일)**

Run:
```bash
uv run ty check app/services/investment_reports/watch_auto_execute.py app/services/investment_reports/auto_execute_guard.py app/models/review.py app/jobs/investment_watch_scanner.py app/mcp_server/tooling/order_execution.py
```
Expected: 통과.

- [ ] **Step 4: 커밋(필요 시 format)**

```bash
git add -A && git commit -m "style(ROB-402): ruff format" || echo "nothing to format"
```

---

## 검증 / 인수 기준

- `auto_execute_mock` action_mode 추가, alert·event CHECK 허용.
- live(kis_live/upbit_live) → 가드 hard reject + 주문 미호출 + intent 미기록(테스트). kiwoom_mock → unsupported.
- global flag OFF(default) → intent failed(blocking_reason), 실주문 0(inert).
- flag ON + kis_mock + max_action(side+quantity+limit_price) → place_order(is_mock=True, dry_run=False, correlation_id) + intent previewed + KISMockOrderLedger 링크.
- 멱등(correlation_id UNIQUE → 재트리거 1회만).
- 마이그레이션은 action_mode CHECK만(intent ledger 테이블은 daf4130b13ce 기존).

## 범위 밖 (후속)

- global flag flip + operator live-mock smoke.
- kiwoom_mock 자동집행(ROB-399 후속).
- notional-sizing / market order.
- ROB-405 회고 배선(correlation_id 링크 위에).

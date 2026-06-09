# ROB-473 — report_item ↔ order_id 링크 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 라이브 주문 ledger(kis_live KR, live US/crypto)에 `report_item_uuid` send-time 컬럼을 추가하고 주문 도구가 그것을 기록·역조회하게 하여, 어느 report item이 라이브 주문을 유발했는지 감사 추적한다.

**Architecture:** Reverse 방향 — `report_item_uuid`(PG_UUID, nullable, **FK 없음**)를 두 라이브 ledger에 추가(AlpacaPaper `candidate_uuid` 패턴). 주문 도구(`_place_order_impl`)가 send-time에 ledger row에 기록, reconcile은 무변경이라 불변 보존. mock/alpaca/binance 무관.

**Tech Stack:** Python 3.13, SQLAlchemy async, Alembic, FastMCP, pytest. 스펙: `docs/superpowers/specs/2026-06-09-rob473-report-item-order-linkage-design.md`. 워크트리: `auto_trader.rob-473`(branch `rob-473`, off main `ad764592`). alembic head: `20260609_rob455`.

**제약:** broker mutation 0 · forward 방향 없음 · reconcile 로직 무변경 · mock/alpaca/binance 무변경 · **migration 1개(operator-gated)** · Slice 2(read join-back) deferred.

---

## 파일 맵
- Modify: `app/models/review.py` — `KISLiveOrderLedger`/`LiveOrderLedger`에 컬럼+index.
- Create: `alembic/versions/20260609_rob473_add_report_item_uuid_to_live_ledgers.py` — additive migration.
- Modify: `app/mcp_server/tooling/kis_live_ledger.py` — `_save_kis_live_order_ledger`/`_record_kis_live_order` 컬럼 기록.
- Modify: `app/mcp_server/tooling/live_order_ledger.py` — `_save_live_order_ledger`/`_record_live_order` 컬럼 기록.
- Modify: `app/mcp_server/tooling/order_execution.py` — `_execute_and_record`/`_place_order_impl` threading + UUID 파싱.
- Modify: `app/mcp_server/tooling/orders_kis_variants.py` — `_place_order_variant` + 라이브 변형 등록에 param.
- Test: `tests/` 기존 kis_live/live ledger 테스트 파일에 추가(아래 각 태스크).

---

### Task 1: 스키마 — 두 라이브 ledger에 `report_item_uuid` 컬럼 + migration

**Files:**
- Modify: `app/models/review.py` (`KISLiveOrderLedger` :277-321, `LiveOrderLedger` :351-402)
- Create: `alembic/versions/20260609_rob473_add_report_item_uuid_to_live_ledgers.py`
- Test: `tests/test_rob473_report_item_link_schema.py` (신규)

- [ ] **Step 1: 모델 컬럼 + index 추가**

`app/models/review.py` `KISLiveOrderLedger.__table_args__`에 index 한 줄 추가:

```python
    __table_args__ = (
        UniqueConstraint("order_no", name="uq_kis_live_ledger_order_no"),
        Index("ix_kis_live_ledger_status", "status"),
        Index("ix_kis_live_ledger_symbol", "symbol"),
        Index("ix_kis_live_ledger_report_item_uuid", "report_item_uuid"),
        {"schema": "review"},
    )
```

`KISLiveOrderLedger`의 `indicators_snapshot: Mapped[dict | None] = mapped_column(JSONB)`(:321) 바로 아래에 컬럼 추가:

```python
    # ROB-473 — audit linkage to the report item that drove this order.
    # send-time, immutable through reconcile; NO FK (mirrors
    # AlpacaPaperOrderLedger.candidate_uuid). nullable: legacy/unlinked → NULL.
    report_item_uuid: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
```

`LiveOrderLedger.__table_args__`에 index 추가:

```python
    __table_args__ = (
        UniqueConstraint(
            "broker", "account_scope", "order_no", name="uq_live_ledger_order"
        ),
        Index("ix_live_ledger_status", "status"),
        Index("ix_live_ledger_market_symbol", "market", "symbol"),
        Index("ix_live_ledger_report_item_uuid", "report_item_uuid"),
        {"schema": "review"},
    )
```

`LiveOrderLedger`의 `indicators_snapshot: Mapped[dict | None] = mapped_column(JSONB)`(:402) 바로 아래에 동일 컬럼 추가:

```python
    # ROB-473 — audit linkage to the report item that drove this order (see
    # KISLiveOrderLedger.report_item_uuid). send-time, immutable, no FK.
    report_item_uuid: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
```

(`uuid`, `PG_UUID`는 이미 import됨 — review.py:5,24.)

- [ ] **Step 2: migration 작성**

먼저 head 확인:

Run: `cd /Users/mgh3326/work/auto_trader.rob-473 && uv run alembic heads`
Expected: 단일 head `20260609_rob455`. (다르면 아래 down_revision을 그 값으로.)

신규 `alembic/versions/20260609_rob473_add_report_item_uuid_to_live_ledgers.py`:

```python
"""ROB-473 — add report_item_uuid to live order ledgers (audit linkage)

Revision ID: 20260609_rob473
Revises: 20260609_rob455
Create Date: 2026-06-09
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision = "20260609_rob473"
down_revision = "20260609_rob455"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("kis_live_order_ledger", "live_order_ledger"):
        op.add_column(
            table,
            sa.Column("report_item_uuid", PG_UUID(as_uuid=True), nullable=True),
            schema="review",
        )
    op.create_index(
        "ix_kis_live_ledger_report_item_uuid",
        "kis_live_order_ledger",
        ["report_item_uuid"],
        schema="review",
    )
    op.create_index(
        "ix_live_ledger_report_item_uuid",
        "live_order_ledger",
        ["report_item_uuid"],
        schema="review",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_live_ledger_report_item_uuid",
        table_name="live_order_ledger",
        schema="review",
    )
    op.drop_index(
        "ix_kis_live_ledger_report_item_uuid",
        table_name="kis_live_order_ledger",
        schema="review",
    )
    for table in ("live_order_ledger", "kis_live_order_ledger"):
        op.drop_column(table, "report_item_uuid", schema="review")
```

- [ ] **Step 3: 스키마 테스트 작성 (실패 예상)**

신규 `tests/test_rob473_report_item_link_schema.py`:

```python
"""ROB-473 — report_item_uuid 컬럼이 라이브 ledger 모델에 존재."""

from __future__ import annotations

import pytest

from app.models.review import KISLiveOrderLedger, LiveOrderLedger

pytestmark = pytest.mark.unit


def test_kis_live_ledger_has_report_item_uuid_column():
    assert "report_item_uuid" in KISLiveOrderLedger.__table__.columns
    col = KISLiveOrderLedger.__table__.columns["report_item_uuid"]
    assert col.nullable is True


def test_live_ledger_has_report_item_uuid_column():
    assert "report_item_uuid" in LiveOrderLedger.__table__.columns
    assert LiveOrderLedger.__table__.columns["report_item_uuid"].nullable is True
```

- [ ] **Step 4: 실행 (모델 테스트 통과 / migration 적용)**

Run: `uv run pytest tests/test_rob473_report_item_link_schema.py -p no:randomly -q`
Expected: PASS (2 tests)

Run: `uv run alembic upgrade head && uv run alembic downgrade -1 && uv run alembic upgrade head`
Expected: 에러 없이 up→down→up (migration 대칭 확인). (로컬 DB 필요.)

- [ ] **Step 5: 커밋**

```bash
git add app/models/review.py alembic/versions/20260609_rob473_add_report_item_uuid_to_live_ledgers.py tests/test_rob473_report_item_link_schema.py
git commit -m "feat(ROB-473): 라이브 ledger에 report_item_uuid 컬럼+index (additive)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: ledger save/record 함수가 `report_item_uuid` 기록

**Files:**
- Modify: `app/mcp_server/tooling/kis_live_ledger.py` (`_save_kis_live_order_ledger` :79-152, `_record_kis_live_order` :155-217)
- Modify: `app/mcp_server/tooling/live_order_ledger.py` (`_save_live_order_ledger` :40-115, `_record_live_order` :360-)
- Test: `tests/test_rob473_report_item_link_ledger.py` (신규)

- [ ] **Step 1: 실패 테스트 작성**

신규 `tests/test_rob473_report_item_link_ledger.py`:

```python
"""ROB-473 — 라이브 ledger save가 report_item_uuid를 기록한다."""

from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.asyncio


async def test_kis_live_save_records_report_item_uuid():
    from app.mcp_server.tooling import kis_live_ledger as m

    rid = uuid.uuid4()
    ledger_id = await m._save_kis_live_order_ledger(
        symbol="005930",
        instrument_type="equity_kr",
        side="buy",
        order_type="limit",
        quantity=1.0,
        price=70000.0,
        amount=70000.0,
        currency="KRW",
        order_no="0000000001",
        order_time="090000",
        krx_fwdg_ord_orgno="00950",
        status="accepted",
        response_code="0",
        response_message="ok",
        raw_response={},
        reason="r",
        thesis=None,
        strategy=None,
        target_price=None,
        stop_loss=None,
        min_hold_days=None,
        notes=None,
        exit_reason=None,
        indicators_snapshot=None,
        report_item_uuid=rid,
    )
    assert ledger_id is not None
    from app.core.db import AsyncSessionLocal
    from app.models.review import KISLiveOrderLedger

    async with AsyncSessionLocal() as db:
        row = await db.get(KISLiveOrderLedger, ledger_id)
        assert row.report_item_uuid == rid


async def test_live_save_records_report_item_uuid():
    from app.mcp_server.tooling import live_order_ledger as m

    rid = uuid.uuid4()
    ledger_id = await m._save_live_order_ledger(
        broker="kis",
        account_scope="kis_live",
        market="us",
        symbol="AAPL",
        exchange="NASD",
        market_symbol=None,
        side="buy",
        order_kind="limit",
        quantity=1.0,
        price=200.0,
        amount=200.0,
        currency="USD",
        order_no="usodno1",
        order_time="0930",
        status="accepted",
        response_code="0",
        response_message="ok",
        raw_response={},
        reason="r",
        thesis=None,
        strategy=None,
        target_price=None,
        stop_loss=None,
        min_hold_days=None,
        notes=None,
        exit_reason=None,
        indicators_snapshot=None,
        report_item_uuid=rid,
    )
    from app.core.db import AsyncSessionLocal
    from app.models.review import LiveOrderLedger

    async with AsyncSessionLocal() as db:
        row = await db.get(LiveOrderLedger, ledger_id)
        assert row.report_item_uuid == rid
```

(주의: 이 ledger save 함수들은 `_order_session_factory()` 자체 세션을 연다. 위 테스트는 기록 후 별도 세션으로 읽는다. 기존 kis_live/live ledger 테스트가 쓰는 DB 픽스처/세션 팩토리 관례를 따른다 — `git grep -l "_save_kis_live_order_ledger\|_save_live_order_ledger" tests/`로 선례 확인.)

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_rob473_report_item_link_ledger.py -p no:randomly -q`
Expected: FAIL — `_save_*` 가 `report_item_uuid` kwarg 미수용 (TypeError).

- [ ] **Step 3: `_save_kis_live_order_ledger` 수정**

`kis_live_ledger.py`:
- 시그니처(`:79-106`) 끝 `fee: float = 0.0,` 다음에 추가:
```python
    report_item_uuid: uuid.UUID | None = None,
```
- `.values(...)`(`:112-142`)에서 `indicators_snapshot=indicators_snapshot,` 다음에 추가:
```python
                    report_item_uuid=report_item_uuid,
```
- 파일 상단에 `import uuid`가 없으면 추가(`git grep "^import uuid" app/mcp_server/tooling/kis_live_ledger.py`로 확인).

- [ ] **Step 4: `_record_kis_live_order` 수정**

`kis_live_ledger.py` `_record_kis_live_order`(`:155-172`) 시그니처 끝 `indicators_snapshot: dict[str, Any] | None,` 다음에 추가:
```python
    report_item_uuid: uuid.UUID | None = None,
```
그리고 `_save_kis_live_order_ledger(...)` 호출(`:192-217`)의 `indicators_snapshot=indicators_snapshot,` 다음에 추가:
```python
        report_item_uuid=report_item_uuid,
```

- [ ] **Step 5: `_save_live_order_ledger` 수정**

`live_order_ledger.py`:
- 시그니처(`:40-71`) 끝 `dt_caller_source: str | None = None,` 다음에 추가:
```python
    report_item_uuid: uuid.UUID | None = None,
```
- `LiveOrderLedger(...)` 생성자(`:74-106`)의 `dt_caller_source=dt_caller_source,` 다음에 추가:
```python
            report_item_uuid=report_item_uuid,
```
- 상단에 `import uuid` 없으면 추가.

- [ ] **Step 6: `_record_live_order` 수정**

`live_order_ledger.py` `_record_live_order`(`:360-`) 시그니처에 `report_item_uuid: uuid.UUID | None = None,`를 keyword-only 인자로 추가하고, 내부에서 `_save_live_order_ledger(...)`를 호출하는 곳에 `report_item_uuid=report_item_uuid,`를 전달한다. (이 함수는 인자를 `_save_live_order_ledger`로 포워딩하는 래퍼다 — `git grep -n "_save_live_order_ledger(" app/mcp_server/tooling/live_order_ledger.py`로 호출부 확인 후 한 줄 전달 추가.)

- [ ] **Step 7: 통과 확인**

Run: `uv run pytest tests/test_rob473_report_item_link_ledger.py -p no:randomly -q`
Expected: PASS (2 tests)

- [ ] **Step 8: 커밋**

```bash
git add app/mcp_server/tooling/kis_live_ledger.py app/mcp_server/tooling/live_order_ledger.py tests/test_rob473_report_item_link_ledger.py
git commit -m "feat(ROB-473): 라이브 ledger save/record가 report_item_uuid 기록(send-time)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: 주문 도구 threading (`_place_order_impl` → ledger)

**Files:**
- Modify: `app/mcp_server/tooling/order_execution.py` (`_execute_and_record` :621-646/:726-845, `_place_order_impl` :896-920/:1070-1093)
- Modify: `app/mcp_server/tooling/orders_kis_variants.py` (`_place_order_variant` :191- + 라이브 변형 등록)
- Test: `tests/test_rob473_report_item_link_threading.py` (신규)

- [ ] **Step 1: 실패 테스트 작성**

신규 `tests/test_rob473_report_item_link_threading.py`:

```python
"""ROB-473 — _place_order_impl이 report_item_uuid를 ledger record로 스레딩."""

from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.asyncio


async def test_execute_and_record_threads_report_item_uuid_to_kis_live(monkeypatch):
    from app.mcp_server.tooling import order_execution as oe

    captured = {}

    async def _fake_record_kis_live(**kwargs):
        captured.update(kwargs)
        return {"success": True}

    # _record_kis_live_order는 함수 내부 import이므로 원본 모듈에서 패치
    from app.mcp_server.tooling import kis_live_ledger

    monkeypatch.setattr(
        kis_live_ledger, "_record_kis_live_order", _fake_record_kis_live
    )

    rid = uuid.uuid4()
    await oe._execute_and_record(
        normalized_symbol="005930",
        side="buy",
        order_type="limit",
        order_quantity=1.0,
        price=70000.0,
        market_type="equity_kr",
        current_price=70000.0,
        avg_price=0.0,
        dry_run_result={"price": 70000.0, "quantity": 1.0, "estimated_value": 70000.0},
        order_amount=70000.0,
        reason="r",
        exit_reason=None,
        thesis=None,
        strategy=None,
        target_price=None,
        stop_loss=None,
        min_hold_days=None,
        notes=None,
        indicators_snapshot=None,
        defensive_trim_ctx=None,
        order_error_fn=lambda m: {"success": False, "error": m},
        is_mock=False,
        report_item_uuid=rid,
    )
    assert captured.get("report_item_uuid") == rid


def test_place_order_impl_parses_report_item_uuid_fail_open():
    # 잘못된 uuid 문자열은 주문을 차단하지 않고 None으로 fail-open 처리되어야 한다.
    from app.mcp_server.tooling.order_execution import _coerce_report_item_uuid

    assert _coerce_report_item_uuid(None) is None
    assert _coerce_report_item_uuid("not-a-uuid") is None
    good = uuid.uuid4()
    assert _coerce_report_item_uuid(str(good)) == good
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_rob473_report_item_link_threading.py -p no:randomly -q`
Expected: FAIL — `_execute_and_record`가 `report_item_uuid` kwarg 미수용 + `_coerce_report_item_uuid` 미정의.

- [ ] **Step 3: UUID 파싱 헬퍼 + `_execute_and_record` param**

`order_execution.py`에 모듈-레벨 헬퍼 추가(상단 헬퍼 영역, `import uuid` 필요 시 추가):

```python
def _coerce_report_item_uuid(value: str | None) -> uuid.UUID | None:
    """ROB-473 — parse a report_item_uuid string fail-open.

    Audit metadata only — a malformed value must never block the order, so a
    bad string resolves to None (no linkage) rather than raising.
    """
    if not value:
        return None
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None
```

`_execute_and_record`(`:621-646`) 시그니처 끝 `correlation_id: str | None = None,` 다음에 추가:
```python
    report_item_uuid: uuid.UUID | None = None,
```

- [ ] **Step 4: `_execute_and_record` 3개 record 호출에 전달**

`order_execution.py`:
- `_record_kis_live_order(...)` 호출(`:729-745`)의 `indicators_snapshot=indicators_snapshot,` 다음에 추가: `report_item_uuid=report_item_uuid,`
- `_record_live_order(...)` 호출 (equity_us, `:755-`)의 인자 목록 끝에 추가: `report_item_uuid=report_item_uuid,`
- `_record_live_order(...)` 호출 (crypto, `:812-`)의 인자 목록 끝에 추가: `report_item_uuid=report_item_uuid,`

(kis_mock 경로(`:720`)는 변경하지 않음 — 범위 밖.)

- [ ] **Step 5: `_place_order_impl` param + 파싱 + 전달**

`order_execution.py` `_place_order_impl`(`:896-920`) 시그니처 끝 `correlation_id: str | None = None,` 다음에 추가:
```python
    report_item_uuid: str | None = None,
```
그리고 `_execute_and_record(...)` 호출(`:1070-1093`)의 `correlation_id=correlation_id,` 다음에 추가:
```python
            report_item_uuid=_coerce_report_item_uuid(report_item_uuid),
```

- [ ] **Step 6: 변형 도구에 param 노출**

`orders_kis_variants.py` `_place_order_variant`(`:191-`) 시그니처 끝(`account_type: str | None,` 다음)에 추가:
```python
    report_item_uuid: str | None = None,
```
그리고 `order_execution._place_order_impl(...)` 호출의 인자 목록 끝에 추가: `report_item_uuid=report_item_uuid,`

라이브 변형 등록(`kis_live_place_order` 및 US/crypto live 변형 — `git grep -n "_place_order_variant(" app/mcp_server/tooling/orders_kis_variants.py`로 등록 래퍼 확인)의 시그니처에 `report_item_uuid: str | None = None`를 추가하고 `_place_order_variant(..., report_item_uuid=report_item_uuid)`로 전달. 등록 description에 한 줄 추가: "report item에서 비롯된 주문이면 investment_report_get의 item_uuid를 report_item_uuid로 넘겨 감사 링크(ROB-473)."

- [ ] **Step 7: 통과 확인 + 회귀**

Run: `uv run pytest tests/test_rob473_report_item_link_threading.py -p no:randomly -q`
Expected: PASS (2 tests)

Run: `uv run pytest tests/ -k "place_order or order_execution or kis_variant" -p no:randomly -q`
Expected: 기존 주문 도구 테스트 무회귀 PASS.

- [ ] **Step 8: ruff + 커밋**

Run: `uv run ruff check app/mcp_server/tooling/order_execution.py app/mcp_server/tooling/orders_kis_variants.py tests/test_rob473_report_item_link_threading.py`
Expected: All checks passed!

```bash
git add app/mcp_server/tooling/order_execution.py app/mcp_server/tooling/orders_kis_variants.py tests/test_rob473_report_item_link_threading.py
git commit -m "feat(ROB-473): 주문 도구가 report_item_uuid를 라이브 ledger로 스레딩(fail-open 파싱)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: 역조회 + reconcile 불변 검증

**Files:**
- Modify: `app/mcp_server/tooling/kis_live_ledger.py` / `live_order_ledger.py` — `list_*_by_report_item_uuid` 헬퍼.
- Test: `tests/test_rob473_report_item_link_ledger.py` (append)

- [ ] **Step 1: 역조회 + 불변 테스트 작성 (append)**

`tests/test_rob473_report_item_link_ledger.py`에 append:

```python
async def test_list_kis_live_orders_by_report_item_uuid():
    from app.mcp_server.tooling import kis_live_ledger as m

    rid = uuid.uuid4()
    await m._save_kis_live_order_ledger(
        symbol="005930", instrument_type="equity_kr", side="buy", order_type="limit",
        quantity=1.0, price=70000.0, amount=70000.0, currency="KRW",
        order_no="rob473-q1", order_time="090000", krx_fwdg_ord_orgno="00950",
        status="accepted", response_code="0", response_message="ok", raw_response={},
        reason="r", thesis=None, strategy=None, target_price=None, stop_loss=None,
        min_hold_days=None, notes=None, exit_reason=None, indicators_snapshot=None,
        report_item_uuid=rid,
    )
    rows = await m.list_kis_live_orders_by_report_item_uuid(rid)
    assert any(r["order_no"] == "rob473-q1" for r in rows)
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_rob473_report_item_link_ledger.py -k list_kis_live -p no:randomly -q`
Expected: FAIL — `list_kis_live_orders_by_report_item_uuid` 미정의.

- [ ] **Step 3: 역조회 헬퍼 구현**

`kis_live_ledger.py`에 추가:

```python
async def list_kis_live_orders_by_report_item_uuid(
    report_item_uuid: uuid.UUID,
) -> list[dict[str, Any]]:
    """ROB-473 — return live KR orders linked to a report item (audit)."""
    from sqlalchemy import select

    async with _order_session_factory()() as db:
        rows = (
            (
                await db.execute(
                    select(KISLiveOrderLedger)
                    .where(KISLiveOrderLedger.report_item_uuid == report_item_uuid)
                    .order_by(KISLiveOrderLedger.id.desc())
                )
            )
            .scalars()
            .all()
        )
    return [
        {
            "ledger_id": r.id,
            "order_no": r.order_no,
            "symbol": r.symbol,
            "side": r.side,
            "status": r.status,
            "report_item_uuid": str(r.report_item_uuid)
            if r.report_item_uuid
            else None,
        }
        for r in rows
    ]
```

`live_order_ledger.py`에 동등한 `list_live_orders_by_report_item_uuid(report_item_uuid)` 추가(같은 패턴, `LiveOrderLedger` 대상, `account_scope`/`market` 필드 포함).

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_rob473_report_item_link_ledger.py -p no:randomly -q`
Expected: PASS (전체)

- [ ] **Step 5: reconcile 불변 회귀 (기존 reconcile 테스트)**

Run: `uv run pytest tests/ -k "kis_live_reconcile or live_reconcile" -p no:randomly -q`
Expected: 기존 reconcile 테스트 PASS(불변 — reconcile은 report_item_uuid를 건드리지 않음).

- [ ] **Step 6: 슬라이스 전체 검증 + 커밋**

Run: `uv run pytest tests/test_rob473_report_item_link_schema.py tests/test_rob473_report_item_link_ledger.py tests/test_rob473_report_item_link_threading.py -p no:randomly -q`
Run: `uv run ruff check $(git diff --name-only origin/main...HEAD)`
Expected: 전체 PASS, ruff clean.

```bash
git add app/mcp_server/tooling/kis_live_ledger.py app/mcp_server/tooling/live_order_ledger.py tests/test_rob473_report_item_link_ledger.py
git commit -m "feat(ROB-473): report_item_uuid 역조회 list_*_by_report_item_uuid

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

→ PR 생성 준비 완료(operator alembic upgrade 게이트 명시).

---

## Self-Review (스펙 대비)

**1. Spec coverage**
- §3-1 스키마(두 ledger 컬럼+index, FK없음, migration) → Task 1. ✅
- §3-2 threading(_place_order_impl→_execute_and_record→record_*→save_*, 변형 노출, reconcile 무변경) → Task 2(save/record)+Task 3(order tool/variant). ✅
- §3-3 쿼리(list_*_by_report_item_uuid) → Task 4. ✅
- §3-4 에러처리(record-as-provided, fail-open 파싱) → Task 3 `_coerce_report_item_uuid`. ✅
- §4 테스트(스키마/기록/threading/reconcile불변/역조회/fail-open) → Task 1-4 전반. ✅
- §5 제약(additive/migration operator-gated/mock·alpaca·binance 무변경) → 코드가 라이브 2 ledger만 변경, mock 경로 명시 제외. ✅

**2. Placeholder scan** — 모든 코드 스텝 실제 코드/명령/기대출력. Task 2 Step 6 / Task 3 Step 6은 "포워딩 래퍼에 한 줄 전달 + grep으로 호출부 확인"으로 구체화(파일 내 대칭 포워딩이라 정확한 한 줄). TBD/TODO 없음.

**3. Type consistency**
- `report_item_uuid` 타입: 모델/save/record/_execute_and_record는 `uuid.UUID | None`; MCP 표면(_place_order_impl/variant)은 `str | None` → `_coerce_report_item_uuid`로 경계에서 변환. 일관. ✅
- `_coerce_report_item_uuid(str|None)->uuid.UUID|None` — Task 3 정의·사용·테스트 일관. ✅
- `list_kis_live_orders_by_report_item_uuid` / `list_live_orders_by_report_item_uuid` — Task 4 정의·테스트 일관. ✅
- migration revision `20260609_rob473` / down_revision `20260609_rob455`(head 확인 후). ✅

**열린 실행-시 확인(차단 아님):** `_record_live_order` 호출부 정확 위치(grep); 변형 등록 래퍼 목록(kis_live + US/crypto live); ledger DB 테스트의 세션/xdist 픽스처 관례; `import uuid` 각 파일 존재 여부.

---

## Execution Handoff
(상위 세션에서 안내)

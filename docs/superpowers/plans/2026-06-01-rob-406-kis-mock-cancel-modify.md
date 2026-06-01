# ROB-406 — kis_mock 취소·정정 복구 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** kis_mock 주문 취소/정정이 미지원 pending-orders 조회(TTTC8036R)에 의존하지 않고 ledger에 저장된 `krx_fwdg_ord_orgno`로 직접 `VTTC0013U` TR을 호출하도록 복구하고, broker가 mock에서 TR을 거부하면 fail-closed soft-cancel로 폴백한다.

**Architecture:** ledger가 단일 진실원천. 신규 resolver가 `order_no`로 취소/정정에 필요한 필드(orgno/symbol/side/qty/price)를 반환하고, MCP cancel/modify의 mock 분기가 이를 사용해 broker TR을 호출한 뒤 결과를 success/unsupported→soft-cancel/error로 분류한다. live 경로는 무변경.

**Tech Stack:** Python 3.13, SQLAlchemy async, Postgres, alembic, pytest/pytest-asyncio, KIS REST.

**Spec:** `docs/superpowers/specs/2026-06-01-rob-406-kis-mock-cancel-modify-design.md`

---

## File Structure

| 파일 | 역할 | 변경 |
|---|---|---|
| `app/schemas/execution_contracts.py` | ROB-100 lifecycle vocabulary | `cancelled` 추가 (Literal + 2 frozenset) |
| `app/models/review.py` | `KISMockOrderLedger` CHECK | `cancelled` 추가 |
| `alembic/versions/<rev>_rob406_*.py` | prod CHECK 마이그레이션 | 신규 |
| `tests/conftest.py` | 영속 테스트 DB CHECK drift 패치 | drop+recreate CHECK |
| `app/services/kis_mock_lifecycle_service.py` | ledger resolver + term update | 메서드 2개 추가 |
| `app/mcp_server/tooling/kis_mock_ledger.py` | 세션 래핑 헬퍼 | 함수 3개 추가 |
| `app/mcp_server/tooling/orders_modify_cancel.py` | mock cancel/modify 분기 + 분류기 | 함수 추가 + 2곳 분기 |
| `tests/test_execution_contracts.py` | contract 테스트 | 추가 |
| `tests/services/test_kis_mock_lifecycle_service.py` | service 테스트 | 추가 |
| `tests/test_kis_mock_cancel_modify.py` | mock cancel/modify 동작 테스트 | 신규 |

---

## Task 1: lifecycle vocabulary에 `cancelled` 등록

**Files:**
- Modify: `app/schemas/execution_contracts.py:43-75`
- Test: `tests/test_execution_contracts.py`

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_execution_contracts.py` 끝에 추가:

```python
def test_cancelled_is_registered_terminal_state():
    from app.schemas.execution_contracts import (
        ORDER_LIFECYCLE_STATES,
        TERMINAL_LIFECYCLE_STATES,
        is_terminal_state,
    )

    assert "cancelled" in ORDER_LIFECYCLE_STATES
    assert "cancelled" in TERMINAL_LIFECYCLE_STATES
    assert is_terminal_state("cancelled") is True
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_execution_contracts.py::test_cancelled_is_registered_terminal_state -v`
Expected: FAIL — `assert "cancelled" in ORDER_LIFECYCLE_STATES`.

- [ ] **Step 3: 구현**

`app/schemas/execution_contracts.py` — `OrderLifecycleState` Literal에 `"cancelled"` 추가(닫는 `]` 직전):

```python
    "anomaly",
    "cancelled",
]
```

`ORDER_LIFECYCLE_STATES` frozenset에 추가(`"anomaly",` 다음):

```python
        "anomaly",
        "cancelled",
    }
)
```

`TERMINAL_LIFECYCLE_STATES`를 교체:

```python
TERMINAL_LIFECYCLE_STATES: frozenset[str] = frozenset(
    {"reconciled", "failed", "stale", "cancelled"}
)
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_execution_contracts.py -v`
Expected: PASS (신규 + 기존 모두).

- [ ] **Step 5: 커밋**

```bash
git add app/schemas/execution_contracts.py tests/test_execution_contracts.py
git commit -m "feat(ROB-406): register 'cancelled' lifecycle state (ROB-100 contract)"
```

---

## Task 2: `KISMockOrderLedger` CHECK에 `cancelled` 추가 (모델 + 마이그레이션 + conftest)

**Files:**
- Modify: `app/models/review.py:193-199`
- Modify: `tests/conftest.py` (CHECK drift 패치 — ROB-329 블록 근처)
- Create: `alembic/versions/<rev>_rob406_kis_mock_cancelled_state.py`
- Test: `tests/services/test_kis_mock_lifecycle_service.py`

- [ ] **Step 1: 실패 테스트 작성**

`tests/services/test_kis_mock_lifecycle_service.py` 끝에 추가:

```python
@pytest.mark.asyncio
async def test_apply_lifecycle_transition_to_cancelled_persists(
    db_session: AsyncSession, seeded_ledger_id: int
):
    svc = KISMockLifecycleService(db_session)
    summary = await svc.apply_lifecycle_transition(
        ledger_id=seeded_ledger_id,
        next_state="cancelled",
        reason_code="broker_cancel_confirmed",
        detail={"broker_cancel_confirmed": True},
        dry_run=False,
    )
    assert summary["applied"] is True
    assert summary["next_state"] == "cancelled"

    row = await db_session.get(KISMockOrderLedger, seeded_ledger_id)
    assert row.lifecycle_state == "cancelled"
    assert row.reconciled_at is not None  # terminal → stamped
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/services/test_kis_mock_lifecycle_service.py::test_apply_lifecycle_transition_to_cancelled_persists -v`
Expected: FAIL — CHECK 위반 `kis_mock_ledger_lifecycle_state_allowed` (영속 테스트 DB의 기존 CHECK는 `cancelled` 미허용). Task 1 미적용 시 `ValueError`로도 실패 가능 — Task 1 먼저 머지된 상태를 가정.

- [ ] **Step 3-a: 모델 CHECK 갱신**

`app/models/review.py` — `KISMockOrderLedger.__table_args__`의 `lifecycle_state` CHECK 문자열을 교체:

```python
        CheckConstraint(
            "lifecycle_state IN ("
            "'planned','previewed','submitted','accepted','pending','fill',"
            "'reconciled','stale','failed','anomaly','cancelled'"
            ")",
            name="kis_mock_ledger_lifecycle_state_allowed",
        ),
```

- [ ] **Step 3-b: conftest drift 패치 추가**

`tests/conftest.py` — ROB-329 snapshot_kind CHECK 블록(약 554–583행) **다음**에 추가:

```python
                # ROB-406 — extend kis_mock_order_ledger.lifecycle_state CHECK
                # to include 'cancelled'. create_all is no-op on the persistent
                # test table, so drop+recreate here; canonical schema lives in
                # migration <rev>_rob406_kis_mock_cancelled_state.py.
                await conn.execute(
                    text(
                        "ALTER TABLE review.kis_mock_order_ledger "
                        "DROP CONSTRAINT IF EXISTS "
                        "kis_mock_ledger_lifecycle_state_allowed"
                    )
                )
                await conn.execute(
                    text(
                        "ALTER TABLE review.kis_mock_order_ledger "
                        "ADD CONSTRAINT kis_mock_ledger_lifecycle_state_allowed "
                        "CHECK (lifecycle_state IN ("
                        "'planned','previewed','submitted','accepted','pending',"
                        "'fill','reconciled','stale','failed','anomaly','cancelled'"
                        "))"
                    )
                )
```

- [ ] **Step 3-c: alembic 마이그레이션 생성**

현재 head 확인 후 빈 리비전을 만든다(autogenerate는 CHECK 문자열 변경을 못 잡으므로 수동 작성):

```bash
uv run alembic heads
uv run alembic revision -m "rob406 kis_mock cancelled state"
```

생성된 파일을 다음 내용으로 작성(`down_revision`은 위 head 값으로):

```python
"""rob406 kis_mock cancelled state

Revision ID: <자동생성>
Revises: <현재 head>
"""

from alembic import op

revision = "<자동생성>"
down_revision = "<현재 head>"
branch_labels = None
depends_on = None

_OLD = (
    "'planned','previewed','submitted','accepted','pending','fill',"
    "'reconciled','stale','failed','anomaly'"
)
_NEW = (
    "'planned','previewed','submitted','accepted','pending','fill',"
    "'reconciled','stale','failed','anomaly','cancelled'"
)
_NAME = "kis_mock_ledger_lifecycle_state_allowed"
_TABLE = "kis_mock_order_ledger"
_SCHEMA = "review"


def upgrade() -> None:
    op.drop_constraint(_NAME, _TABLE, schema=_SCHEMA, type_="check")
    op.create_check_constraint(
        _NAME, _TABLE, f"lifecycle_state IN ({_NEW})", schema=_SCHEMA
    )


def downgrade() -> None:
    op.drop_constraint(_NAME, _TABLE, schema=_SCHEMA, type_="check")
    op.create_check_constraint(
        _NAME, _TABLE, f"lifecycle_state IN ({_OLD})", schema=_SCHEMA
    )
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/services/test_kis_mock_lifecycle_service.py -v`
Expected: PASS (신규 cancelled 테스트 + 기존 전부).

- [ ] **Step 5: 커밋**

```bash
git add app/models/review.py tests/conftest.py alembic/versions
git commit -m "feat(ROB-406): allow 'cancelled' on kis_mock_order_ledger (model+migration+conftest)"
```

---

## Task 3: ledger resolver + term-update 추가 (KISMockLifecycleService)

**Files:**
- Modify: `app/services/kis_mock_lifecycle_service.py`
- Test: `tests/services/test_kis_mock_lifecycle_service.py`

- [ ] **Step 1: 실패 테스트 작성**

`tests/services/test_kis_mock_lifecycle_service.py` 끝에 추가:

```python
@pytest.mark.asyncio
async def test_get_by_order_no_returns_row(
    db_session: AsyncSession, seeded_ledger_id: int
):
    seeded = await db_session.get(KISMockOrderLedger, seeded_ledger_id)
    svc = KISMockLifecycleService(db_session)
    row = await svc.get_by_order_no(order_no=seeded.order_no)
    assert row is not None
    assert row.id == seeded_ledger_id


@pytest.mark.asyncio
async def test_get_by_order_no_missing_returns_none(db_session: AsyncSession):
    svc = KISMockLifecycleService(db_session)
    assert await svc.get_by_order_no(order_no="NO-SUCH-ORDER") is None


@pytest.mark.asyncio
async def test_update_order_terms_persists(
    db_session: AsyncSession, seeded_ledger_id: int
):
    svc = KISMockLifecycleService(db_session)
    await svc.update_order_terms(
        ledger_id=seeded_ledger_id,
        price=Decimal("71000"),
        quantity=Decimal("8"),
        detail={"modified_via": "test"},
    )
    row = await db_session.get(KISMockOrderLedger, seeded_ledger_id)
    assert row.price == Decimal("71000")
    assert row.quantity == Decimal("8")
    assert row.last_reconcile_detail == {"modified_via": "test"}
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/services/test_kis_mock_lifecycle_service.py -k "get_by_order_no or update_order_terms" -v`
Expected: FAIL — `AttributeError: 'KISMockLifecycleService' object has no attribute 'get_by_order_no'`.

- [ ] **Step 3: 구현**

`app/services/kis_mock_lifecycle_service.py` — `list_open_orders` 메서드 다음에 추가:

```python
    async def get_by_order_no(
        self, *, order_no: str
    ) -> KISMockOrderLedger | None:
        """Look up a single ledger row by broker order number.

        Used by cancel/modify so KIS mock never depends on the unsupported
        TTTC8036R pending-orders inquiry.
        """
        stmt = select(KISMockOrderLedger).where(
            KISMockOrderLedger.order_no == order_no
        )
        result = await self._db.execute(stmt)
        return result.scalar_one_or_none()

    async def update_order_terms(
        self,
        *,
        ledger_id: int,
        price: Decimal | None = None,
        quantity: Decimal | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        """Reflect a broker-confirmed modify on the ledger row."""
        row = await self._db.get(KISMockOrderLedger, ledger_id)
        if row is None:
            raise LedgerNotFoundError(str(ledger_id))
        if price is not None:
            row.price = price
        if quantity is not None:
            row.quantity = quantity
        if detail is not None:
            row.last_reconcile_detail = {
                **(row.last_reconcile_detail or {}),
                **detail,
            }
        await self._db.commit()
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/services/test_kis_mock_lifecycle_service.py -v`
Expected: PASS.

- [ ] **Step 5: 커밋**

```bash
git add app/services/kis_mock_lifecycle_service.py tests/services/test_kis_mock_lifecycle_service.py
git commit -m "feat(ROB-406): add order_no resolver + term-update to KISMockLifecycleService"
```

---

## Task 4: 세션 래핑 헬퍼 추가 (kis_mock_ledger.py)

**Files:**
- Modify: `app/mcp_server/tooling/kis_mock_ledger.py`
- Test: `tests/test_kis_mock_cancel_modify.py` (신규)

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_kis_mock_cancel_modify.py` 생성:

```python
"""ROB-406 — kis_mock cancel/modify via ledger (no TTTC8036R inquiry)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.review import KISMockOrderLedger
import app.mcp_server.tooling.kis_mock_ledger as kml


async def _seed(db_session: AsyncSession, **overrides) -> KISMockOrderLedger:
    row = KISMockOrderLedger(
        trade_date=datetime(2026, 6, 1, 9, 0, tzinfo=UTC),
        symbol=overrides.get("symbol", "005930"),
        instrument_type="equity_kr",
        side=overrides.get("side", "buy"),
        order_type="limit",
        quantity=Decimal(overrides.get("quantity", "10")),
        price=Decimal(overrides.get("price", "70000")),
        amount=Decimal("700000"),
        currency="KRW",
        order_no=overrides.get("order_no", f"MOCK-{uuid4()}"),
        krx_fwdg_ord_orgno=overrides.get("orgno", "00950"),
        account_mode="kis_mock",
        broker="kis",
        status="accepted",
        lifecycle_state="accepted",
        holdings_baseline_qty=Decimal("0"),
    )
    db_session.add(row)
    await db_session.commit()
    return row


@pytest.mark.asyncio
async def test_resolve_mock_order_for_cancel_returns_fields(
    db_session: AsyncSession,
):
    row = await _seed(db_session, orgno="00950", side="buy")
    resolved = await kml.resolve_mock_order_for_cancel(row.order_no)
    assert resolved is not None
    assert resolved["ledger_id"] == row.id
    assert resolved["symbol"] == "005930"
    assert resolved["krx_fwdg_ord_orgno"] == "00950"
    assert resolved["side"] == "buy"


@pytest.mark.asyncio
async def test_resolve_mock_order_for_cancel_missing_returns_none(
    db_session: AsyncSession,
):
    assert await kml.resolve_mock_order_for_cancel("NOPE") is None


@pytest.mark.asyncio
async def test_mark_cancelled_sets_state_and_flag(db_session: AsyncSession):
    row = await _seed(db_session)
    await kml.mark_kis_mock_order_cancelled(
        ledger_id=row.id, broker_confirmed=False, detail={"reason": "x"}
    )
    await db_session.refresh(row)
    assert row.lifecycle_state == "cancelled"
    assert row.last_reconcile_detail["broker_cancel_confirmed"] is False
```

> 참고: `kml.*` 헬퍼는 자체적으로 `AsyncSessionLocal` 세션을 열어 같은 테스트 DB에 커밋한다. `db_session` 픽스처는 시드/검증용. 검증 read 전 `await db_session.refresh(row)` 또는 새 `get`으로 헬퍼 커밋을 반영한다.

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_kis_mock_cancel_modify.py -k "resolve or mark_cancelled" -v`
Expected: FAIL — `AttributeError: module ... has no attribute 'resolve_mock_order_for_cancel'`.

- [ ] **Step 3: 구현**

`app/mcp_server/tooling/kis_mock_ledger.py` 끝에 추가:

```python
async def resolve_mock_order_for_cancel(order_no: str) -> dict[str, Any] | None:
    """Resolve cancel/modify inputs from the ledger (no TTTC8036R inquiry).

    Returns ledger_id + the fields the KIS cancel/modify TR needs, or None
    when no row matches ``order_no``.
    """
    async with _order_session_factory()() as db:
        svc = KISMockLifecycleService(db)
        row = await svc.get_by_order_no(order_no=order_no)
        if row is None:
            return None
        return {
            "ledger_id": row.id,
            "symbol": row.symbol,
            "side": row.side,
            "quantity": _decimal_to_float(row.quantity),
            "price": _decimal_to_float(row.price),
            "krx_fwdg_ord_orgno": row.krx_fwdg_ord_orgno,
            "instrument_type": row.instrument_type,
            "lifecycle_state": row.lifecycle_state,
        }


async def mark_kis_mock_order_cancelled(
    *,
    ledger_id: int,
    broker_confirmed: bool,
    detail: dict[str, Any],
) -> None:
    """Transition a ledger row to 'cancelled' via the single write chokepoint."""
    async with _order_session_factory()() as db:
        svc = KISMockLifecycleService(db)
        await svc.apply_lifecycle_transition(
            ledger_id=ledger_id,
            next_state="cancelled",
            reason_code=(
                "broker_cancel_confirmed"
                if broker_confirmed
                else "soft_cancel_broker_unsupported"
            ),
            detail={"broker_cancel_confirmed": broker_confirmed, **detail},
            dry_run=False,
        )


async def update_kis_mock_order_terms(
    *,
    ledger_id: int,
    price: Decimal | None = None,
    quantity: Decimal | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """Reflect a broker-confirmed modify on the ledger row."""
    async with _order_session_factory()() as db:
        svc = KISMockLifecycleService(db)
        await svc.update_order_terms(
            ledger_id=ledger_id, price=price, quantity=quantity, detail=detail
        )
```

`OrderLifecycleState` 캐스트가 필요하면 `apply_lifecycle_transition`의 `next_state="cancelled"`는 Literal 멤버이므로 추가 import 불필요.

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_kis_mock_cancel_modify.py -k "resolve or mark_cancelled" -v`
Expected: PASS.

- [ ] **Step 5: 커밋**

```bash
git add app/mcp_server/tooling/kis_mock_ledger.py tests/test_kis_mock_cancel_modify.py
git commit -m "feat(ROB-406): ledger-session helpers for kis_mock cancel/modify"
```

---

## Task 5: mock cancel 분기 + unsupported 분류기 (orders_modify_cancel.py)

**Files:**
- Modify: `app/mcp_server/tooling/orders_modify_cancel.py:446-451` (분기 추가) + 신규 함수
- Test: `tests/test_kis_mock_cancel_modify.py`

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_kis_mock_cancel_modify.py`에 추가:

```python
import app.mcp_server.tooling.orders_modify_cancel as omc


class _FakeKisCancelOK:
    async def cancel_korea_order(self, **kwargs):
        self.kwargs = kwargs
        return {"odno": "REV-1", "ord_tmd": "0901", "msg": "ok"}

    async def inquire_korea_orders(self, *a, **k):  # must NOT be called
        raise AssertionError("inquire_korea_orders called in mock cancel path")


class _FakeKisCancelUnsupported:
    async def cancel_korea_order(self, **kwargs):
        raise RuntimeError("APBK0918 not available in mock mode")

    async def inquire_korea_orders(self, *a, **k):
        raise AssertionError("inquire_korea_orders called in mock cancel path")


class _FakeKisCancelError:
    async def cancel_korea_order(self, **kwargs):
        raise RuntimeError("APBK1234 already filled order")

    async def inquire_korea_orders(self, *a, **k):
        raise AssertionError("inquire_korea_orders called in mock cancel path")


@pytest.mark.asyncio
async def test_mock_cancel_success_confirms_and_cancels(
    db_session: AsyncSession, monkeypatch
):
    row = await _seed(db_session, orgno="00950")
    fake = _FakeKisCancelOK()
    monkeypatch.setattr(omc, "_create_kis_client", lambda *, is_mock: fake)

    result = await omc._cancel_kis_domestic(row.order_no, None, is_mock=True)

    assert result["success"] is True
    assert result["broker_cancel_confirmed"] is True
    assert fake.kwargs["krx_fwdg_ord_orgno"] == "00950"
    assert fake.kwargs["is_mock"] is True
    await db_session.refresh(row)
    assert row.lifecycle_state == "cancelled"


@pytest.mark.asyncio
async def test_mock_cancel_unsupported_soft_cancels(
    db_session: AsyncSession, monkeypatch
):
    row = await _seed(db_session, orgno="00950")
    monkeypatch.setattr(
        omc, "_create_kis_client", lambda *, is_mock: _FakeKisCancelUnsupported()
    )

    result = await omc._cancel_kis_domestic(row.order_no, None, is_mock=True)

    assert result["success"] is True
    assert result["broker_cancel_confirmed"] is False
    assert result["mock_unsupported"] is True
    assert "warning" in result
    await db_session.refresh(row)
    assert row.lifecycle_state == "cancelled"


@pytest.mark.asyncio
async def test_mock_cancel_other_error_surfaces_no_soft_cancel(
    db_session: AsyncSession, monkeypatch
):
    row = await _seed(db_session, orgno="00950")
    monkeypatch.setattr(
        omc, "_create_kis_client", lambda *, is_mock: _FakeKisCancelError()
    )

    result = await omc._cancel_kis_domestic(row.order_no, None, is_mock=True)

    assert result["success"] is False
    assert result.get("broker_cancel_confirmed") is False
    await db_session.refresh(row)
    assert row.lifecycle_state == "accepted"  # unchanged


@pytest.mark.asyncio
async def test_mock_cancel_unknown_order_fails(
    db_session: AsyncSession, monkeypatch
):
    monkeypatch.setattr(
        omc, "_create_kis_client", lambda *, is_mock: _FakeKisCancelOK()
    )
    result = await omc._cancel_kis_domestic("NO-SUCH", None, is_mock=True)
    assert result["success"] is False
    assert "ledger" in result["error"]
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_kis_mock_cancel_modify.py -k "mock_cancel" -v`
Expected: FAIL — 현재 `_cancel_kis_domestic`가 mock에서 `inquire_korea_orders`를 호출 → `_FakeKisCancelOK.inquire_korea_orders`의 `AssertionError`.

- [ ] **Step 3-a: 분류기 + mock 분기 함수 추가**

`app/mcp_server/tooling/orders_modify_cancel.py` — `_cancel_kis_domestic`(446행) **앞**에 추가:

```python
_KIS_MOCK_UNSUPPORTED_MARKERS: tuple[str, ...] = (
    "not available in mock",
    "not supported",
    "unsupported",
    "미지원",
    "tttc8036r",
)


def _is_kis_mock_unsupported(message: str) -> bool:
    """True when a broker error indicates the TR is unsupported in mock mode.

    Conservative: only soft-cancel on these markers. Any other broker error
    (e.g. already-filled, invalid order) is surfaced as a genuine failure.
    The exact marker set is refined after the operator VTTC0013U mock smoke.
    """
    lowered = message.lower()
    return any(marker in lowered for marker in _KIS_MOCK_UNSUPPORTED_MARKERS)


async def _cancel_kis_mock_domestic(
    order_id: str,
    symbol: str | None,
) -> dict[str, Any]:
    """Cancel a KIS *mock* domestic order via the ledger (no TTTC8036R)."""
    from app.mcp_server.tooling.kis_mock_ledger import (
        mark_kis_mock_order_cancelled,
        resolve_mock_order_for_cancel,
    )

    market = _normalize_market_type_to_external("equity_kr")
    resolved = await resolve_mock_order_for_cancel(order_id)
    if resolved is None:
        return {
            "success": False,
            "order_id": order_id,
            "error": "kis_mock: order not found in kis_mock_order_ledger",
            "market": market,
        }

    resolved_symbol = symbol or resolved["symbol"]
    orgno = resolved["krx_fwdg_ord_orgno"]
    side = resolved["side"]
    quantity = int(resolved["quantity"]) or 1
    price = int(resolved["price"])

    async def _soft_cancel(reason: str) -> dict[str, Any]:
        await mark_kis_mock_order_cancelled(
            ledger_id=resolved["ledger_id"],
            broker_confirmed=False,
            detail={"reason": reason, "order_no": order_id},
        )
        return {
            "success": True,
            "order_id": order_id,
            "symbol": resolved_symbol,
            "broker_cancel_confirmed": False,
            "mock_unsupported": True,
            "soft_cancelled": True,
            "warning": (
                "kis_mock soft-cancel: ledger marked cancelled but the broker "
                "resting order may still be live; a later fill is reconciled."
            ),
            "market": market,
        }

    if not orgno:
        return await _soft_cancel("missing_krx_fwdg_ord_orgno")

    try:
        kis = _create_kis_client(is_mock=True)
        result = await kis.cancel_korea_order(
            order_number=order_id,
            stock_code=resolved_symbol,
            quantity=quantity,
            price=price,
            order_type=side,
            krx_fwdg_ord_orgno=orgno,
            is_mock=True,
        )
    except RuntimeError as exc:
        if _is_kis_mock_unsupported(str(exc)):
            return await _soft_cancel(f"broker_unsupported: {exc}")
        return {
            "success": False,
            "order_id": order_id,
            "symbol": resolved_symbol,
            "broker_cancel_confirmed": False,
            "error": str(exc),
            "market": market,
        }
    except Exception as exc:  # noqa: BLE001 - surface unexpected broker errors
        return {
            "success": False,
            "order_id": order_id,
            "symbol": resolved_symbol,
            "broker_cancel_confirmed": False,
            "error": str(exc),
            "market": market,
        }

    await mark_kis_mock_order_cancelled(
        ledger_id=resolved["ledger_id"],
        broker_confirmed=True,
        detail={"order_no": order_id, "broker_response": result},
    )
    return {
        "success": True,
        "order_id": order_id,
        "symbol": resolved_symbol,
        "broker_cancel_confirmed": True,
        "cancelled_at": result.get("ord_tmd", ""),
        "market": market,
    }
```

- [ ] **Step 3-b: live 함수 상단에서 mock 분기**

`_cancel_kis_domestic`(446행) 본문 첫 줄(docstring 다음)에 추가:

```python
async def _cancel_kis_domestic(
    order_id: str,
    symbol: str | None,
    *,
    is_mock: bool = False,
) -> dict[str, Any]:
    """Cancel a KIS domestic (Korean equity) order."""
    if is_mock:
        return await _cancel_kis_mock_domestic(order_id, symbol)
    if not symbol:
        # ... (기존 live 경로 그대로)
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_kis_mock_cancel_modify.py -k "mock_cancel" -v`
Expected: PASS (4건).

- [ ] **Step 5: 커밋**

```bash
git add app/mcp_server/tooling/orders_modify_cancel.py tests/test_kis_mock_cancel_modify.py
git commit -m "feat(ROB-406): kis_mock cancel via ledger + soft-cancel fallback"
```

---

## Task 6: mock modify 분기 (fail-closed on unsupported)

**Files:**
- Modify: `app/mcp_server/tooling/orders_modify_cancel.py:877-886` (분기 추가) + 신규 함수
- Test: `tests/test_kis_mock_cancel_modify.py`

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_kis_mock_cancel_modify.py`에 추가:

```python
class _FakeKisModifyOK:
    async def modify_korea_order(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        return {"odno": "REV-2"}

    async def inquire_korea_orders(self, *a, **k):
        raise AssertionError("inquire_korea_orders called in mock modify path")


class _FakeKisModifyUnsupported:
    async def modify_korea_order(self, *args, **kwargs):
        raise RuntimeError("APBK0918 not available in mock mode")

    async def inquire_korea_orders(self, *a, **k):
        raise AssertionError("inquire_korea_orders called in mock modify path")


@pytest.mark.asyncio
async def test_mock_modify_success_updates_ledger(
    db_session: AsyncSession, monkeypatch
):
    row = await _seed(db_session, orgno="00950", price="70000", quantity="10")
    fake = _FakeKisModifyOK()
    monkeypatch.setattr(omc, "_create_kis_client", lambda *, is_mock: fake)

    result = await omc._modify_kis_domestic(
        row.order_no, "005930", "equity_kr",
        new_price=71000.0, new_quantity=8.0, dry_run=False, is_mock=True,
    )

    assert result["success"] is True
    assert result["status"] == "modified"
    await db_session.refresh(row)
    assert row.price == Decimal("71000")
    assert row.quantity == Decimal("8")


@pytest.mark.asyncio
async def test_mock_modify_unsupported_fails_closed(
    db_session: AsyncSession, monkeypatch
):
    row = await _seed(db_session, orgno="00950")
    monkeypatch.setattr(
        omc, "_create_kis_client", lambda *, is_mock: _FakeKisModifyUnsupported()
    )

    result = await omc._modify_kis_domestic(
        row.order_no, "005930", "equity_kr",
        new_price=71000.0, new_quantity=None, dry_run=False, is_mock=True,
    )

    assert result["success"] is False
    assert result["mock_unsupported"] is True
    await db_session.refresh(row)
    assert row.lifecycle_state == "accepted"  # unchanged, not soft-modified
    assert row.price == Decimal("70000")  # unchanged
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_kis_mock_cancel_modify.py -k "mock_modify" -v`
Expected: FAIL — 현재 `_modify_kis_domestic`가 mock에서 `inquire_korea_orders` 호출 → `AssertionError`.

- [ ] **Step 3-a: mock modify 함수 추가**

`app/mcp_server/tooling/orders_modify_cancel.py` — `_modify_kis_domestic`(877행) **앞**에 추가:

```python
async def _modify_kis_mock_domestic(
    order_id: str,
    normalized_symbol: str,
    market_type: str,
    new_price: float | None,
    new_quantity: float | None,
    dry_run: bool,
) -> dict[str, Any]:
    """Modify a KIS *mock* domestic order via the ledger (no TTTC8036R).

    Fail-closed: if the broker rejects VTTC0013U as unsupported in mock, we do
    NOT soft-modify (we cannot honestly claim a resting order was changed).
    Use cancel + re-place instead.
    """
    from app.mcp_server.tooling.kis_mock_ledger import (
        resolve_mock_order_for_cancel,
        update_kis_mock_order_terms,
    )

    market = _normalize_market_type_to_external(market_type)
    resolved = await resolve_mock_order_for_cancel(order_id)
    if resolved is None:
        return {
            "success": False,
            "status": "failed",
            "order_id": order_id,
            "symbol": normalized_symbol,
            "error": "kis_mock: order not found in kis_mock_order_ledger",
            "market": market,
            "dry_run": dry_run,
        }

    orgno = resolved["krx_fwdg_ord_orgno"]
    side = resolved["side"]
    original_price = int(resolved["price"])
    original_quantity = int(resolved["quantity"])
    final_price = int(
        adjust_tick_size_kr(
            float(new_price) if new_price is not None else original_price, side
        )
    )
    final_quantity = (
        int(new_quantity) if new_quantity is not None else original_quantity
    )

    if not orgno:
        return {
            "success": False,
            "status": "failed",
            "order_id": order_id,
            "symbol": normalized_symbol,
            "mock_unsupported": True,
            "error": "kis_mock: missing krx_fwdg_ord_orgno; use cancel + re-place",
            "market": market,
            "dry_run": dry_run,
        }

    try:
        kis = _create_kis_client(is_mock=True)
        result = await kis.modify_korea_order(
            order_id,
            normalized_symbol,
            final_quantity,
            final_price,
            krx_fwdg_ord_orgno=orgno,
            is_mock=True,
        )
    except RuntimeError as exc:
        if _is_kis_mock_unsupported(str(exc)):
            return {
                "success": False,
                "status": "failed",
                "order_id": order_id,
                "symbol": normalized_symbol,
                "mock_unsupported": True,
                "error": (
                    "kis_mock modify unsupported by broker — use cancel + "
                    f"re-place. ({exc})"
                ),
                "market": market,
                "dry_run": dry_run,
            }
        return {
            "success": False,
            "status": "failed",
            "order_id": order_id,
            "symbol": normalized_symbol,
            "error": str(exc),
            "market": market,
            "dry_run": dry_run,
        }

    if not result.get("odno"):
        return {
            "success": False,
            "status": "failed",
            "order_id": order_id,
            "symbol": normalized_symbol,
            "error": "kis_mock modify returned no order number",
            "market": market,
            "dry_run": dry_run,
        }

    await update_kis_mock_order_terms(
        ledger_id=resolved["ledger_id"],
        price=Decimal(str(final_price)),
        quantity=Decimal(str(final_quantity)),
        detail={"modified_to_order_no": result.get("odno")},
    )
    return {
        "success": True,
        "status": "modified",
        "order_id": order_id,
        "new_order_id": result["odno"],
        "symbol": normalized_symbol,
        "market": market,
        "changes": {
            "price": {"from": original_price, "to": final_price}
            if final_price != original_price
            else None,
            "quantity": {"from": original_quantity, "to": final_quantity}
            if final_quantity != original_quantity
            else None,
        },
        "method": "api_modify",
        "dry_run": dry_run,
        "message": "KIS mock order modified via ledger-resolved orgno",
    }
```

파일 상단 import에 `Decimal`이 없으면 추가: `from decimal import Decimal` (이미 `from typing import Any`만 있으면 함께).

- [ ] **Step 3-b: live modify 함수 상단에서 mock 분기**

`_modify_kis_domestic`(877행) 본문 첫 줄(docstring 다음, `try:` 앞)에 추가:

```python
    """Modify a KIS domestic (Korean equity) order."""
    if is_mock:
        return await _modify_kis_mock_domestic(
            order_id, normalized_symbol, market_type, new_price, new_quantity, dry_run
        )
    try:
        # ... (기존 live 경로 그대로)
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_kis_mock_cancel_modify.py -v`
Expected: PASS (전체).

- [ ] **Step 5: 커밋**

```bash
git add app/mcp_server/tooling/orders_modify_cancel.py tests/test_kis_mock_cancel_modify.py
git commit -m "feat(ROB-406): kis_mock modify via ledger, fail-closed on unsupported"
```

---

## Task 7: live 경로 회귀 가드 + 전체 검증

**Files:**
- Test: `tests/test_kis_mock_cancel_modify.py`

- [ ] **Step 1: live 경로 회귀 테스트 작성**

`tests/test_kis_mock_cancel_modify.py`에 추가:

```python
@pytest.mark.asyncio
async def test_live_cancel_still_uses_inquire(monkeypatch):
    """is_mock=False must keep using inquire_korea_orders (unchanged path)."""
    called = {"inquire": False}

    class _FakeLive:
        async def inquire_korea_orders(self, *a, **k):
            called["inquire"] = True
            return []

        async def cancel_korea_order(self, **kwargs):
            return {"odno": "L-1", "ord_tmd": "0900"}

    monkeypatch.setattr(omc, "_create_kis_client", lambda *, is_mock: _FakeLive())
    # symbol 미제공 → live 경로는 inquire로 심볼 조회 시도
    await omc._cancel_kis_domestic("LIVE-1", None, is_mock=False)
    assert called["inquire"] is True
```

- [ ] **Step 2: 통과 확인 (신규 + 파일 전체)**

Run: `uv run pytest tests/test_kis_mock_cancel_modify.py -v`
Expected: PASS (전부).

- [ ] **Step 3: 관련 스위트 회귀 확인**

Run:
```bash
uv run pytest tests/test_execution_contracts.py tests/services/test_kis_mock_lifecycle_service.py tests/test_kis_mock_cancel_modify.py tests/test_mcp_kis_order_variants.py tests/test_orders_history_kis_mock.py -v
```
Expected: 전부 PASS.

- [ ] **Step 4: lint + format + typecheck**

Run:
```bash
uv run ruff check app/ tests/
uv run ruff format --check app/ tests/
uv run ty check app/mcp_server/tooling/orders_modify_cancel.py app/services/kis_mock_lifecycle_service.py app/schemas/execution_contracts.py
```
Expected: 통과(또는 format 필요 시 `uv run ruff format app/ tests/` 후 재확인).

- [ ] **Step 5: 커밋**

```bash
git add tests/test_kis_mock_cancel_modify.py
git commit -m "test(ROB-406): live cancel-path regression guard"
```

---

## 검증 / 인수 기준 (구현 후)

- `kis_mock_cancel_order`가 TTTC8036R 의존 없이 동작: success→`cancelled`+`broker_cancel_confirmed=true`; unsupported→soft-cancel(경고+`broker_cancel_confirmed=false`); 기타 에러→실패 surface.
- `kis_mock_modify_order`: success→ledger term 갱신; unsupported→fail-closed(soft-modify 안 함).
- live cancel/modify 경로 무변경(회귀 테스트).
- `cancelled` ∈ ROB-100 contract + ledger CHECK + conftest 패치 동기.
- migration은 PR에 포함되나 operator가 `alembic upgrade head` 별도 실행.

## 범위 밖 (후속)

- `valid_until` 자동만료 / GTC→IOC/Day 정책 → 별도 이슈.
- modify의 cancel+replace 합성.
- **operator VTTC0013U mock smoke**: 실제 KIS mock이 취소/정정 TR을 지원하는지, unsupported 신호의 정확한 `msg_cd`/문구 확인 → `_KIS_MOCK_UNSUPPORTED_MARKERS` 정련. 결과를 ROB-406/ROB-410에 증거로 남긴다. (creds 부재로 이 PR에서 미수행)
```

# ROB-407 — live 주문 선반영 차단 (US/해외 + crypto) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** US/해외(`equity_us`)·crypto(`crypto`) live 주문이 전송 시점에 fill/journal/realized_pnl을 선반영하지 않게 하고, broker별 evidence-gated reconcile 경로에서만 장부를 확정한다 (ROB-395 KR 패턴의 확장).

**Architecture:** 새 제네릭 테이블 `review.live_order_ledger`에 전송 시 accepted-only 기록 → broker별 evidence 어댑터(US 해외 일별주문 거래소 순회 / Upbit order-state)가 체결 증거를 가져와 `classify_fill_evidence`(또는 Upbit 전용 분류)로 verdict 산출 → FILLED/PARTIAL일 때만 기존 `_save_order_fill`/`_create_trade_journal_for_buy`/`_close_journals_on_sell` 재사용해 booking. KR domestic(`kis_live_order_ledger`)은 무변경. 부분체결은 델타 멱등 booking.

**Tech Stack:** Python 3.13, SQLAlchemy(async) + Alembic, FastMCP `@mcp.tool`, pytest(`-m unit`, async). 기존 ROB-395 모듈(`kis_live_ledger.py`, `fill_evidence.py`)을 패턴/코드 소스로 재사용.

**PR 슬라이스:** PR1 = Task 1–8 (제네릭 ledger 기반 + US/해외). PR2 = Task 9–12 (crypto/Upbit). PR2는 PR1의 테이블/서비스/어댑터 레지스트리에 의존하므로 순차.

---

## File Structure

**PR1 (US/해외 기반):**
- Create `app/models/review.py::LiveOrderLedger` — 제네릭 ledger ORM (KISLiveOrderLedger 미러 + 디스크리미네이터/시장 메타).
- Create `alembic/versions/<rev>_add_live_order_ledger.py` — `review.live_order_ledger` 테이블.
- Create `app/mcp_server/tooling/live_order_ledger.py` — accepted-only writer + 제네릭 reconcile impl + per-row 델타 reconcile.
- Create `app/mcp_server/tooling/live_order_evidence.py` — evidence 어댑터 protocol + `UsOverseasEvidenceAdapter` + 레지스트리(`get_evidence_adapter`).
- Modify `app/mcp_server/tooling/order_execution.py` — `_execute_and_record`에 `equity_us` live 분기 추가.
- Modify `app/mcp_server/tooling/orders_kis_variants.py` — `live_reconcile_orders` MCP 도구 등록(이미 서버에 wired된 파일).
- Test `tests/mcp_server/tooling/test_live_order_ledger.py`, `tests/mcp_server/tooling/test_live_order_evidence_us.py`, `tests/mcp_server/tooling/test_execute_and_record_routing.py`.

**PR2 (crypto):**
- Modify `app/mcp_server/tooling/live_order_evidence.py` — `UpbitEvidenceAdapter` 추가 + 레지스트리 등록.
- Modify `app/mcp_server/tooling/order_execution.py` — `_execute_and_record`에 `crypto` live 분기(+시장가 inline 확인).
- Modify `app/mcp_server/tooling/live_order_ledger.py` — `_record_live_order`에 `inline_confirm` 경로.
- Test `tests/mcp_server/tooling/test_live_order_evidence_upbit.py`, `tests/mcp_server/tooling/test_crypto_live_routing.py`.

---

# PR1 — 제네릭 ledger 기반 + US/해외

## Task 1: `LiveOrderLedger` 모델

**Files:**
- Modify: `app/models/review.py` (KISLiveOrderLedger 클래스 바로 아래에 추가)
- Test: `tests/mcp_server/tooling/test_live_order_ledger.py`

- [ ] **Step 1: 모델 import 가능 여부 테스트 작성**

`tests/mcp_server/tooling/test_live_order_ledger.py`:
```python
import pytest


@pytest.mark.unit
def test_live_order_ledger_model_shape():
    from app.models.review import LiveOrderLedger

    assert LiveOrderLedger.__tablename__ == "live_order_ledger"
    cols = set(LiveOrderLedger.__table__.columns.keys())
    # 디스크리미네이터 + 시장 메타가 존재
    for c in (
        "broker",
        "account_scope",
        "market",
        "symbol",
        "exchange",
        "market_symbol",
        "order_no",
        "order_kind",
        "status",
        "filled_qty",
        "avg_fill_price",
        "trade_id",
        "journal_id",
    ):
        assert c in cols, f"missing column {c}"
    assert LiveOrderLedger.__table__.schema == "review"
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `uv run pytest tests/mcp_server/tooling/test_live_order_ledger.py::test_live_order_ledger_model_shape -v`
Expected: FAIL — `ImportError: cannot import name 'LiveOrderLedger'`

- [ ] **Step 3: 모델 구현**

`app/models/review.py` 의 `KISLiveOrderLedger` 클래스 정의 끝(마지막 `updated_at` 컬럼 다음) 바로 아래에 추가. (이미 import된 `Base, BigInteger, TIMESTAMP, Text, Numeric, SmallInteger, JSONB, UniqueConstraint, Index, Mapped, mapped_column, func, datetime, Decimal` 재사용.)
```python
class LiveOrderLedger(Base):
    """ROB-407 — 제네릭 live (real-money) order lifecycle ledger.

    US/해외(`equity_us`)·crypto(`crypto`) live 주문을 전송 시 accepted-only로 기록한다.
    KISLiveOrderLedger(KR domestic 전용)와 동일 evidence-gated 계약을 따르되,
    broker/market 디스크리미네이터와 시장별 메타(exchange/market_symbol)를 갖는다.
    fill/journal/realized_pnl은 live_reconcile_orders가 broker 체결 증거로만 반영한다.
    """

    __tablename__ = "live_order_ledger"
    __table_args__ = (
        UniqueConstraint(
            "broker", "account_scope", "order_no", name="uq_live_ledger_order"
        ),
        Index("ix_live_ledger_status", "status"),
        Index("ix_live_ledger_market_symbol", "market", "symbol"),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    trade_date: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )

    # discriminators / market metadata
    broker: Mapped[str] = mapped_column(Text, nullable=False)  # kis | upbit
    account_scope: Mapped[str] = mapped_column(Text, nullable=False)  # kis_live | upbit_live
    market: Mapped[str] = mapped_column(Text, nullable=False)  # us | crypto
    symbol: Mapped[str] = mapped_column(Text, nullable=False)  # DB dot-format
    exchange: Mapped[str | None] = mapped_column(Text)  # US: NASD/NYSE/AMEX
    market_symbol: Mapped[str | None] = mapped_column(Text)  # crypto: KRW-BTC

    side: Mapped[str] = mapped_column(Text, nullable=False)  # buy | sell
    order_kind: Mapped[str] = mapped_column(Text, nullable=False)  # market | limit
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    price: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    currency: Mapped[str | None] = mapped_column(Text)

    order_no: Mapped[str | None] = mapped_column(Text)  # KIS odno / Upbit uuid
    order_time: Mapped[str | None] = mapped_column(Text)

    # send-time status: accepted | rejected ; reconcile updates to
    # filled | partial | pending | cancelled | anomaly
    status: Mapped[str] = mapped_column(Text, nullable=False)
    lifecycle_state: Mapped[str] = mapped_column(Text, nullable=False)
    response_code: Mapped[str | None] = mapped_column(Text)
    response_message: Mapped[str | None] = mapped_column(Text)
    raw_response: Mapped[dict | None] = mapped_column(JSONB)

    # buy/sell intent captured at send, consumed by reconcile
    reason: Mapped[str | None] = mapped_column(Text)
    thesis: Mapped[str | None] = mapped_column(Text)
    strategy: Mapped[str | None] = mapped_column(Text)
    target_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    stop_loss: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    min_hold_days: Mapped[int | None] = mapped_column(SmallInteger)
    notes: Mapped[str | None] = mapped_column(Text)
    exit_reason: Mapped[str | None] = mapped_column(Text)
    indicators_snapshot: Mapped[dict | None] = mapped_column(JSONB)

    # reconcile outcomes (filled_qty = 이미 booked된 누적 체결량, 델타 멱등용)
    filled_qty: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    avg_fill_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    trade_id: Mapped[int | None] = mapped_column(BigInteger)
    journal_id: Mapped[int | None] = mapped_column(BigInteger)
    reconciled_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
```

NOTE: 만약 `app/models/review.py` 상단 import에 `SmallInteger`가 없으면 `from sqlalchemy import SmallInteger`를 기존 sqlalchemy import 라인에 추가한다 (KISLiveOrderLedger가 이미 `SmallInteger`를 쓰므로 보통 존재함 — 확인만).

- [ ] **Step 4: 테스트 통과 확인**

Run: `uv run pytest tests/mcp_server/tooling/test_live_order_ledger.py::test_live_order_ledger_model_shape -v`
Expected: PASS

- [ ] **Step 5: 커밋**

```bash
git add app/models/review.py tests/mcp_server/tooling/test_live_order_ledger.py
git commit -m "feat(ROB-407): add generic LiveOrderLedger model (US/crypto live)"
```

---

## Task 2: Alembic 마이그레이션

**Files:**
- Create: `alembic/versions/<rev>_add_live_order_ledger.py` (rev 해시는 autogenerate가 부여)

- [ ] **Step 1: 현재 head 확인 + autogenerate**

```bash
uv run alembic current
uv run alembic revision --autogenerate -m "add live_order_ledger"
```
생성된 파일을 연다. autogenerate가 `live_order_ledger` create_table을 잡았는지 확인.

- [ ] **Step 2: 마이그레이션 본문 검토/정정**

생성된 `upgrade()`가 아래 형태인지 확인하고, 누락 시 `kis_live_order_ledger` 마이그레이션(`alembic/versions/14fa36b85d0a_add_kis_live_order_ledger.py`) 스타일로 맞춘다:
```python
def upgrade() -> None:
    op.create_table(
        "live_order_ledger",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("trade_date", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("broker", sa.Text(), nullable=False),
        sa.Column("account_scope", sa.Text(), nullable=False),
        sa.Column("market", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("exchange", sa.Text(), nullable=True),
        sa.Column("market_symbol", sa.Text(), nullable=True),
        sa.Column("side", sa.Text(), nullable=False),
        sa.Column("order_kind", sa.Text(), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=20, scale=8), nullable=True),
        sa.Column("price", sa.Numeric(precision=20, scale=8), nullable=True),
        sa.Column("amount", sa.Numeric(precision=20, scale=8), nullable=True),
        sa.Column("currency", sa.Text(), nullable=True),
        sa.Column("order_no", sa.Text(), nullable=True),
        sa.Column("order_time", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("lifecycle_state", sa.Text(), nullable=False),
        sa.Column("response_code", sa.Text(), nullable=True),
        sa.Column("response_message", sa.Text(), nullable=True),
        sa.Column("raw_response", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("thesis", sa.Text(), nullable=True),
        sa.Column("strategy", sa.Text(), nullable=True),
        sa.Column("target_price", sa.Numeric(precision=20, scale=8), nullable=True),
        sa.Column("stop_loss", sa.Numeric(precision=20, scale=8), nullable=True),
        sa.Column("min_hold_days", sa.SmallInteger(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("exit_reason", sa.Text(), nullable=True),
        sa.Column("indicators_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("filled_qty", sa.Numeric(precision=20, scale=8), nullable=True),
        sa.Column("avg_fill_price", sa.Numeric(precision=20, scale=8), nullable=True),
        sa.Column("trade_id", sa.BigInteger(), nullable=True),
        sa.Column("journal_id", sa.BigInteger(), nullable=True),
        sa.Column("reconciled_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_live_order_ledger")),
        sa.UniqueConstraint("broker", "account_scope", "order_no", name="uq_live_ledger_order"),
        schema="review",
    )
    op.create_index("ix_live_ledger_status", "live_order_ledger", ["status"], unique=False, schema="review")
    op.create_index("ix_live_ledger_market_symbol", "live_order_ledger", ["market", "symbol"], unique=False, schema="review")


def downgrade() -> None:
    op.drop_index("ix_live_ledger_market_symbol", table_name="live_order_ledger", schema="review")
    op.drop_index("ix_live_ledger_status", table_name="live_order_ledger", schema="review")
    op.drop_table("live_order_ledger", schema="review")
```
파일 상단 import에 `from sqlalchemy.dialects import postgresql`가 있는지 확인(autogenerate가 보통 추가).

- [ ] **Step 3: 로컬 DB에 적용/롤백 검증**

```bash
uv run alembic upgrade head
uv run alembic downgrade -1
uv run alembic upgrade head
```
Expected: 모두 에러 없이 통과. (production `alembic upgrade`는 operator-gated — 이 PR에 포함되지만 운영 실행은 별도.)

- [ ] **Step 4: 커밋**

```bash
git add alembic/versions/
git commit -m "feat(ROB-407): migration for review.live_order_ledger"
```

---

## Task 3: accepted-only writer + ledger 헬퍼

**Files:**
- Create: `app/mcp_server/tooling/live_order_ledger.py`
- Test: `tests/mcp_server/tooling/test_live_order_ledger.py` (추가)

- [ ] **Step 1: writer 테스트 작성 (실 DB, accepted-only — journal 없음)**

`tests/mcp_server/tooling/test_live_order_ledger.py` 상단에 fixture + 테스트 추가. (KIS live 테스트의 cleanup 패턴 미러: `_order_session_factory` 사용.)
```python
import pytest_asyncio
from sqlalchemy import delete


@pytest_asyncio.fixture(autouse=True)
async def _clean_live_ledger():
    from app.models.review import LiveOrderLedger
    from app.mcp_server.tooling.live_order_ledger import _order_session_factory

    async with _order_session_factory()() as db:
        await db.execute(delete(LiveOrderLedger))
        await db.commit()
    yield


@pytest.mark.unit
@pytest.mark.asyncio
async def test_save_live_order_ledger_accepted_only():
    from app.mcp_server.tooling import live_order_ledger as ll

    lid = await ll._save_live_order_ledger(
        broker="kis",
        account_scope="kis_live",
        market="us",
        symbol="AAPL",
        exchange="NASD",
        market_symbol=None,
        side="buy",
        order_kind="limit",
        quantity=2.0,
        price=190.0,
        amount=380.0,
        currency="USD",
        order_no="US-ACC-1",
        order_time="0930",
        status="accepted",
        response_code="0",
        response_message=None,
        raw_response={"odno": "US-ACC-1"},
        reason=None,
        thesis="t",
        strategy="s",
        target_price=None,
        stop_loss=None,
        min_hold_days=None,
        notes=None,
        exit_reason=None,
        indicators_snapshot=None,
    )
    row = await ll._load_live_ledger_row(lid)
    assert row is not None
    assert row.status == "accepted"
    assert row.trade_id is None and row.journal_id is None  # no booking at send
    assert row.filled_qty is None
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/mcp_server/tooling/test_live_order_ledger.py::test_save_live_order_ledger_accepted_only -v`
Expected: FAIL — `ModuleNotFoundError: app.mcp_server.tooling.live_order_ledger`

- [ ] **Step 3: writer + 헬퍼 구현**

`app/mcp_server/tooling/live_order_ledger.py` 생성. (세션 팩토리/공용 헬퍼는 `kis_live_ledger.py`와 동일 출처를 재사용한다.)
```python
"""ROB-407 — 제네릭 live 주문 accepted-only ledger + evidence-gated reconcile.

US/해외(equity_us)·crypto(crypto) live 주문 전용. KR domestic은 kis_live_ledger.py 유지.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from app.models.review import LiveOrderLedger
from app.mcp_server.tooling.kis_live_ledger import _order_session_factory, _to_float
from app.mcp_server.tooling.order_journal import (
    _close_journals_on_sell,
    _create_trade_journal_for_buy,
    _link_journal_to_fill,
    _save_order_fill,
)
from app.services.brokers.kis.mock_scalping_exec.fill_evidence import (
    FillEvidence,
    FillVerdict,
)

logger = logging.getLogger(__name__)


def _to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


async def _save_live_order_ledger(
    *,
    broker: str,
    account_scope: str,
    market: str,
    symbol: str,
    exchange: str | None,
    market_symbol: str | None,
    side: str,
    order_kind: str,
    quantity: float | None,
    price: float | None,
    amount: float | None,
    currency: str | None,
    order_no: str | None,
    order_time: str | None,
    status: str,
    response_code: str | None,
    response_message: str | None,
    raw_response: dict[str, Any] | None,
    reason: str | None,
    thesis: str | None,
    strategy: str | None,
    target_price: float | None,
    stop_loss: float | None,
    min_hold_days: int | None,
    notes: str | None,
    exit_reason: str | None,
    indicators_snapshot: dict[str, Any] | None,
) -> int:
    async with _order_session_factory()() as db:
        row = LiveOrderLedger(
            trade_date=datetime.now(timezone.utc),
            broker=broker,
            account_scope=account_scope,
            market=market,
            symbol=symbol,
            exchange=exchange,
            market_symbol=market_symbol,
            side=side,
            order_kind=order_kind,
            quantity=_to_decimal(quantity),
            price=_to_decimal(price),
            amount=_to_decimal(amount),
            currency=currency,
            order_no=order_no,
            order_time=order_time,
            status=status,
            lifecycle_state="accepted" if status == "accepted" else "rejected",
            response_code=response_code,
            response_message=response_message,
            raw_response=raw_response,
            reason=reason,
            thesis=thesis,
            strategy=strategy,
            target_price=_to_decimal(target_price),
            stop_loss=_to_decimal(stop_loss),
            min_hold_days=min_hold_days,
            notes=notes,
            exit_reason=exit_reason,
            indicators_snapshot=indicators_snapshot,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row.id


async def _load_live_ledger_row(ledger_id: int) -> LiveOrderLedger | None:
    async with _order_session_factory()() as db:
        row = await db.get(LiveOrderLedger, ledger_id)
        if row is not None:
            db.expunge(row)
        return row


def _derive_live_send_status(*, rt_cd: str | None, order_no: str | None) -> str:
    """rt_cd=='0' (또는 order_no 존재) → accepted, 그 외 rejected."""
    if rt_cd is not None and str(rt_cd) not in ("0", ""):
        return "rejected"
    if order_no:
        return "accepted"
    return "rejected" if rt_cd not in (None, "0", "") else "accepted"
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/mcp_server/tooling/test_live_order_ledger.py -v`
Expected: PASS (model-shape + accepted-only writer)

- [ ] **Step 5: 커밋**

```bash
git add app/mcp_server/tooling/live_order_ledger.py tests/mcp_server/tooling/test_live_order_ledger.py
git commit -m "feat(ROB-407): live_order_ledger accepted-only writer + helpers"
```

---

## Task 4: Evidence 어댑터 protocol + US 해외 어댑터

**Files:**
- Create: `app/mcp_server/tooling/live_order_evidence.py`
- Test: `tests/mcp_server/tooling/test_live_order_evidence_us.py`

핵심: 해외 일별주문 row는 `ft_ccld_qty`/`ft_ccld_unpr3`/`ft_ord_qty` 키를 쓴다(도메스틱 `tot_ccld_qty`/`ccld_unpr`/`ord_qty`와 다름). 어댑터가 이를 `classify_fill_evidence`가 기대하는 canonical 키로 정규화한 뒤 재사용한다.

- [ ] **Step 1: US 어댑터 테스트 작성 (fake KIS client)**

`tests/mcp_server/tooling/test_live_order_evidence_us.py`:
```python
import pytest
from decimal import Decimal
from unittest.mock import AsyncMock, patch


def _row(**kw):
    base = {"odno": "US-1", "pdno": "AAPL", "ovrs_excg_cd": "NASD"}
    base.update(kw)
    return base


@pytest.mark.unit
def test_normalize_overseas_row_maps_ft_keys():
    from app.mcp_server.tooling.live_order_evidence import _normalize_overseas_for_classify

    norm = _normalize_overseas_for_classify(
        _row(ft_ord_qty="3", ft_ccld_qty="3", ft_ccld_unpr3="191.5")
    )
    assert norm["odno"] == "US-1"
    assert norm["ord_qty"] == "3"
    assert norm["tot_ccld_qty"] == "3"
    assert norm["ccld_unpr"] == "191.5"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_us_adapter_filled():
    from app.mcp_server.tooling import live_order_evidence as ev
    from app.services.brokers.kis.mock_scalping_exec.fill_evidence import FillVerdict

    class _Row:
        symbol = "AAPL"
        exchange = "NASD"
        order_no = "US-1"

    fake_kis = object()
    with (
        patch.object(ev, "_create_live_kis_client", return_value=fake_kis),
        patch.object(
            ev,
            "_build_us_exchange_candidates",
            new=AsyncMock(return_value=["NASD"]),
        ),
        patch.object(
            ev,
            "_find_us_order_in_recent_history",
            new=AsyncMock(
                return_value=(
                    _row(ft_ord_qty="3", ft_ccld_qty="3", ft_ccld_unpr3="191.5"),
                    "NASD",
                )
            ),
        ),
    ):
        adapter = ev.UsOverseasEvidenceAdapter()
        evidence = await adapter.fetch_evidence(_Row())
    assert evidence.verdict == FillVerdict.FILLED
    assert evidence.filled_qty == Decimal("3")
    assert evidence.avg_price == Decimal("191.5")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_us_adapter_not_found_is_pending():
    from app.mcp_server.tooling import live_order_evidence as ev
    from app.services.brokers.kis.mock_scalping_exec.fill_evidence import FillVerdict

    class _Row:
        symbol = "AAPL"
        exchange = "NASD"
        order_no = "US-MISSING"

    with (
        patch.object(ev, "_create_live_kis_client", return_value=object()),
        patch.object(
            ev, "_build_us_exchange_candidates", new=AsyncMock(return_value=["NASD"])
        ),
        patch.object(
            ev,
            "_find_us_order_in_recent_history",
            new=AsyncMock(return_value=(None, None)),
        ),
    ):
        evidence = await ev.UsOverseasEvidenceAdapter().fetch_evidence(_Row())
    assert evidence.verdict == FillVerdict.PENDING  # fail-closed, no booking
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/mcp_server/tooling/test_live_order_evidence_us.py -v`
Expected: FAIL — module 없음

- [ ] **Step 3: 어댑터 구현**

`app/mcp_server/tooling/live_order_evidence.py` 생성:
```python
"""ROB-407 — broker별 live 주문 체결 evidence 어댑터.

각 어댑터는 ledger row를 받아 FillEvidence(verdict/filled_qty/avg_price)를 돌려준다.
US 해외는 일별주문 거래소 순회 + client-side 필터(KIS odno 미지원) 후
classify_fill_evidence를 재사용(해외 ft_ 키를 canonical 키로 정규화).
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Protocol

from app.mcp_server.tooling.kis_live_ledger import _create_live_kis_client
from app.mcp_server.tooling.orders_modify_cancel import (
    _build_us_exchange_candidates,
    _find_us_order_in_recent_history,
)
from app.services.brokers.kis.mock_scalping_exec.fill_evidence import (
    FillEvidence,
    FillVerdict,
    classify_fill_evidence,
)

logger = logging.getLogger(__name__)


class LiveFillEvidenceAdapter(Protocol):
    broker: str

    async def fetch_evidence(self, row: Any) -> FillEvidence: ...


def _normalize_overseas_for_classify(order: dict[str, Any]) -> dict[str, Any]:
    """해외 일별주문 row(ft_ 키)를 classify_fill_evidence canonical 키로 정규화."""
    return {
        "odno": order.get("odno") or order.get("ODNO") or order.get("ord_no"),
        "ord_qty": order.get("ft_ord_qty") or order.get("FT_ORD_QTY") or order.get("ord_qty") or 0,
        "tot_ccld_qty": order.get("ft_ccld_qty") or order.get("FT_CCLD_QTY") or order.get("ccld_qty") or 0,
        "ccld_unpr": order.get("ft_ccld_unpr3") or order.get("FT_CCLD_UNPR3") or order.get("ccld_unpr") or 0,
    }


class UsOverseasEvidenceAdapter:
    broker = "kis"

    async def fetch_evidence(self, row: Any) -> FillEvidence:
        kis = _create_live_kis_client()
        candidates = await _build_us_exchange_candidates(row.symbol)
        order, _exch = await _find_us_order_in_recent_history(
            kis, str(row.order_no), str(row.symbol), candidates
        )
        if order is None:
            # fail-closed: 증거 미발견 → pending 유지(취소/만료 단정 금지)
            return FillEvidence(
                FillVerdict.PENDING,
                Decimal("0"),
                None,
                None,
                "not_found",
                f"order {row.order_no} not in recent overseas history",
            )
        normalized = _normalize_overseas_for_classify(order)
        return classify_fill_evidence(order_no=str(row.order_no), rows=[normalized])


_ADAPTERS: dict[str, LiveFillEvidenceAdapter] = {
    "kis": UsOverseasEvidenceAdapter(),
}


def get_evidence_adapter(broker: str) -> LiveFillEvidenceAdapter:
    adapter = _ADAPTERS.get(broker)
    if adapter is None:
        raise ValueError(f"no live evidence adapter for broker={broker!r}")
    return adapter
```

NOTE: `_create_live_kis_client`는 `kis_live_ledger.py`에 정의되어 있다(Task 시작 전 reference). 없으면 `kis_live_ledger`에서 live KIS client를 만드는 함수명을 확인해 import를 맞춘다.

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/mcp_server/tooling/test_live_order_evidence_us.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: 커밋**

```bash
git add app/mcp_server/tooling/live_order_evidence.py tests/mcp_server/tooling/test_live_order_evidence_us.py
git commit -m "feat(ROB-407): US overseas fill-evidence adapter (ft_ key normalization)"
```

---

## Task 5: 제네릭 reconcile (델타 멱등 booking)

**Files:**
- Modify: `app/mcp_server/tooling/live_order_ledger.py`
- Test: `tests/mcp_server/tooling/test_live_order_ledger.py` (추가)

- [ ] **Step 1: reconcile 테스트 작성 (FILLED→booking, 재실행 멱등, CANCELLED→no journal)**

`tests/mcp_server/tooling/test_live_order_ledger.py`에 추가:
```python
@pytest.mark.unit
@pytest.mark.asyncio
async def test_reconcile_filled_buy_books_once_and_idempotent():
    from decimal import Decimal
    from unittest.mock import AsyncMock, patch
    from app.mcp_server.tooling import live_order_ledger as ll
    from app.services.brokers.kis.mock_scalping_exec.fill_evidence import (
        FillEvidence,
        FillVerdict,
    )

    lid = await ll._save_live_order_ledger(
        broker="kis", account_scope="kis_live", market="us", symbol="AAPL",
        exchange="NASD", market_symbol=None, side="buy", order_kind="limit",
        quantity=3.0, price=190.0, amount=570.0, currency="USD",
        order_no="US-RC-1", order_time="0930", status="accepted",
        response_code="0", response_message=None, raw_response=None,
        reason=None, thesis="t", strategy="s", target_price=None, stop_loss=None,
        min_hold_days=None, notes=None, exit_reason=None, indicators_snapshot=None,
    )
    row = await ll._load_live_ledger_row(lid)
    filled = FillEvidence(FillVerdict.FILLED, Decimal("3"), Decimal("191.5"), None, "filled", "")

    class _Adapter:
        broker = "kis"
        fetch_evidence = AsyncMock(return_value=filled)

    with (
        patch.object(ll, "get_evidence_adapter", return_value=_Adapter()),
        patch.object(ll, "_save_order_fill", new=AsyncMock(return_value=111)) as m_fill,
        patch.object(
            ll, "_create_trade_journal_for_buy",
            new=AsyncMock(return_value={"journal_created": True, "journal_id": 9, "journal_status": "draft"}),
        ) as m_buy,
        patch.object(ll, "_link_journal_to_fill", new=AsyncMock(return_value=None)),
    ):
        out1 = await ll._reconcile_one_live_row(row, dry_run=False)
        # 재실행: 이미 booked → 델타 0 → 추가 booking 없음
        row2 = await ll._load_live_ledger_row(lid)
        out2 = await ll._reconcile_one_live_row(row2, dry_run=False)

    assert out1["verdict"] == "filled"
    # broker 확정 qty/price로 1회만 fill booking
    _, fkw = m_fill.await_args
    assert float(fkw["quantity"]) == 3.0
    assert float(fkw["price"]) == 191.5
    assert m_fill.await_count == 1  # 멱등: 두번째 reconcile은 booking 안 함
    assert m_buy.await_count == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reconcile_cancelled_no_journal():
    from decimal import Decimal
    from unittest.mock import AsyncMock, patch
    from app.mcp_server.tooling import live_order_ledger as ll
    from app.services.brokers.kis.mock_scalping_exec.fill_evidence import (
        FillEvidence,
        FillVerdict,
    )

    lid = await ll._save_live_order_ledger(
        broker="kis", account_scope="kis_live", market="us", symbol="AAPL",
        exchange="NASD", market_symbol=None, side="buy", order_kind="limit",
        quantity=3.0, price=190.0, amount=570.0, currency="USD",
        order_no="US-RC-2", order_time="0930", status="accepted",
        response_code="0", response_message=None, raw_response=None,
        reason=None, thesis=None, strategy=None, target_price=None, stop_loss=None,
        min_hold_days=None, notes=None, exit_reason=None, indicators_snapshot=None,
    )
    row = await ll._load_live_ledger_row(lid)
    none_ev = FillEvidence(FillVerdict.NONE, Decimal("0"), None, None, "cancelled", "")

    class _Adapter:
        broker = "kis"
        fetch_evidence = AsyncMock(return_value=none_ev)

    with (
        patch.object(ll, "get_evidence_adapter", return_value=_Adapter()),
        patch.object(ll, "_save_order_fill", new=AsyncMock()) as m_fill,
        patch.object(ll, "_create_trade_journal_for_buy", new=AsyncMock()) as m_buy,
    ):
        out = await ll._reconcile_one_live_row(row, dry_run=False)

    assert out["verdict"] == "none"
    m_fill.assert_not_awaited()
    m_buy.assert_not_awaited()
    after = await ll._load_live_ledger_row(lid)
    assert after.status == "cancelled"
    assert after.journal_id is None
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/mcp_server/tooling/test_live_order_ledger.py -k reconcile -v`
Expected: FAIL — `_reconcile_one_live_row` / `get_evidence_adapter` 미정의

- [ ] **Step 3: reconcile 구현**

`app/mcp_server/tooling/live_order_ledger.py`에 추가. 상단 import에 어댑터 레지스트리 추가:
```python
from app.mcp_server.tooling.live_order_evidence import get_evidence_adapter
```
함수들 추가:
```python
async def _list_open_live_ledger_rows(
    *,
    market: str | None,
    broker: str | None,
    symbol: str | None,
    order_no: str | None,
    limit: int,
) -> list[LiveOrderLedger]:
    async with _order_session_factory()() as db:
        stmt = select(LiveOrderLedger).where(
            LiveOrderLedger.status.in_(("accepted", "pending", "partial"))
        )
        if market:
            stmt = stmt.where(LiveOrderLedger.market == market)
        if broker:
            stmt = stmt.where(LiveOrderLedger.broker == broker)
        if symbol:
            stmt = stmt.where(LiveOrderLedger.symbol == symbol)
        if order_no:
            stmt = stmt.where(LiveOrderLedger.order_no == order_no)
        stmt = stmt.order_by(LiveOrderLedger.created_at.asc()).limit(limit)
        rows = list((await db.execute(stmt)).scalars().all())
        for r in rows:
            db.expunge(r)
        return rows


async def _update_live_ledger_outcome(
    *,
    ledger_id: int,
    status: str,
    filled_qty: Decimal | None = None,
    avg_fill_price: Decimal | None = None,
    trade_id: int | None = None,
    journal_id: int | None = None,
) -> None:
    async with _order_session_factory()() as db:
        row = await db.get(LiveOrderLedger, ledger_id)
        if row is None:
            return
        row.status = status
        if filled_qty is not None:
            row.filled_qty = filled_qty
        if avg_fill_price is not None:
            row.avg_fill_price = avg_fill_price
        if trade_id is not None:
            row.trade_id = trade_id
        if journal_id is not None:
            row.journal_id = journal_id
        row.reconciled_at = datetime.now(timezone.utc)
        await db.commit()


async def _reconcile_one_live_row(
    row: LiveOrderLedger, *, dry_run: bool
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "ledger_id": row.id,
        "order_id": row.order_no,
        "broker": row.broker,
        "market": row.market,
        "symbol": row.symbol,
    }
    adapter = get_evidence_adapter(row.broker)
    evidence = await adapter.fetch_evidence(row)
    base["verdict"] = evidence.verdict.value

    if evidence.verdict == FillVerdict.PENDING:
        base["action"] = "noop_pending"
        return base

    if evidence.verdict == FillVerdict.NONE:
        base["action"] = "marked_cancelled"
        if not dry_run:
            await _update_live_ledger_outcome(ledger_id=row.id, status="cancelled")
        return base

    # FILLED / PARTIAL — broker 확정값. 델타 멱등 booking.
    broker_cum = evidence.filled_qty or Decimal("0")
    already = row.filled_qty or Decimal("0")
    delta = broker_cum - already
    avg_price = evidence.avg_price or Decimal("0")
    new_status = "filled" if evidence.verdict == FillVerdict.FILLED else "partial"
    base["filled_qty"] = float(broker_cum)
    base["avg_price"] = float(avg_price)
    base["delta_qty"] = float(delta)

    if delta <= 0:
        base["action"] = "noop_already_booked"
        if not dry_run:
            await _update_live_ledger_outcome(
                ledger_id=row.id, status=new_status,
                filled_qty=broker_cum, avg_fill_price=avg_price,
            )
        return base

    if dry_run:
        base["action"] = "would_book"
        return base

    trade_id = await _save_order_fill(
        symbol=row.symbol,
        side=row.side,
        quantity=float(delta),
        price=float(avg_price),
        market_type=("equity_us" if row.market == "us" else "crypto"),
    )
    journal_id = row.journal_id
    if row.side == "buy" and row.journal_id is None:
        jr = await _create_trade_journal_for_buy(
            symbol=row.symbol,
            quantity=float(broker_cum),
            entry_price=float(avg_price),
            thesis=row.thesis,
            strategy=row.strategy,
            target_price=float(row.target_price) if row.target_price else None,
            stop_loss=float(row.stop_loss) if row.stop_loss else None,
            min_hold_days=row.min_hold_days,
        )
        journal_id = jr.get("journal_id")
        if trade_id and journal_id:
            await _link_journal_to_fill(trade_id=trade_id, journal_id=journal_id)
    elif row.side == "sell":
        await _close_journals_on_sell(
            symbol=row.symbol,
            quantity=float(delta),
            exit_price=float(avg_price),
            exit_reason=row.exit_reason,
        )

    await _update_live_ledger_outcome(
        ledger_id=row.id, status=new_status,
        filled_qty=broker_cum, avg_fill_price=avg_price,
        trade_id=trade_id, journal_id=journal_id,
    )
    base["action"] = "booked"
    base["trade_id"] = trade_id
    base["journal_id"] = journal_id
    return base


async def live_reconcile_orders_impl(
    *,
    market: str | None = None,
    broker: str | None = None,
    symbol: str | None = None,
    order_id: str | None = None,
    dry_run: bool = True,
    limit: int = 100,
) -> dict[str, Any]:
    try:
        rows = await _list_open_live_ledger_rows(
            market=market, broker=broker, symbol=symbol, order_no=order_id, limit=limit
        )
    except Exception as exc:
        logger.exception("Failed to list open live ledger rows: %s", exc)
        return {"success": False, "error": str(exc) or exc.__class__.__name__}

    reconciled: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for row in rows:
        try:
            outcome = await _reconcile_one_live_row(row, dry_run=dry_run)
        except Exception as exc:
            logger.warning("live reconcile failed order_no=%s: %s", row.order_no, exc)
            outcome = {
                "ledger_id": row.id, "order_id": row.order_no,
                "verdict": "anomaly", "error": str(exc) or exc.__class__.__name__,
            }
        reconciled.append(outcome)
        v = str(outcome.get("verdict", "anomaly"))
        counts[v] = counts.get(v, 0) + 1

    return {
        "success": True,
        "dry_run": dry_run,
        "counts": counts,
        "reconciled": reconciled,
        "message": f"Reconciled {len(reconciled)} live order(s) (dry_run={dry_run}): {counts}",
    }
```

NOTE: `_save_order_fill` / `_create_trade_journal_for_buy` / `_close_journals_on_sell` / `_link_journal_to_fill`의 **실제 키워드 인자**는 `app/mcp_server/tooling/order_journal.py` 시그니처를 열어 정확히 맞춘다. 위 호출의 인자명이 다르면 그 시그니처에 맞게 조정(특히 `_create_trade_journal_for_buy`가 `entry_price` vs `price`, `_save_order_fill`가 `market_type` 요구 여부). 테스트는 mock이므로 통과하지만, **구현 호출 인자명은 반드시 실제 시그니처와 일치**시킬 것.

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/mcp_server/tooling/test_live_order_ledger.py -v`
Expected: PASS (model + writer + 2 reconcile)

- [ ] **Step 5: 커밋**

```bash
git add app/mcp_server/tooling/live_order_ledger.py tests/mcp_server/tooling/test_live_order_ledger.py
git commit -m "feat(ROB-407): generic evidence-gated reconcile w/ delta-idempotent booking"
```

---

## Task 6: `live_reconcile_orders` MCP 도구 등록

**Files:**
- Modify: `app/mcp_server/tooling/orders_kis_variants.py` (kis_live_reconcile_orders 등록 근처)
- Test: `tests/mcp_server/tooling/test_live_order_ledger.py` (impl 호출 테스트)

- [ ] **Step 1: impl 위임 테스트 작성**

`tests/mcp_server/tooling/test_live_order_ledger.py`에 추가:
```python
@pytest.mark.unit
@pytest.mark.asyncio
async def test_live_reconcile_impl_dry_run_empty():
    from app.mcp_server.tooling import live_order_ledger as ll

    out = await ll.live_reconcile_orders_impl(dry_run=True, limit=10)
    assert out["success"] is True
    assert out["dry_run"] is True
    assert out["counts"] == {}
    assert out["reconciled"] == []
```

- [ ] **Step 2: 실패/통과 확인**

Run: `uv run pytest tests/mcp_server/tooling/test_live_order_ledger.py::test_live_reconcile_impl_dry_run_empty -v`
Expected: PASS (impl은 Task 5에서 이미 구현됨 — 빈 ledger에서 빈 결과). 만약 FAIL이면 impl 반환 형태를 맞춘다.

- [ ] **Step 3: MCP 도구 등록**

`app/mcp_server/tooling/orders_kis_variants.py`에서 `kis_live_reconcile_orders` 정의 바로 아래에 추가:
```python
@mcp.tool(
    name="live_reconcile_orders",
    description=(
        "Reconcile accepted/pending US/overseas + crypto live (real-money) orders "
        "against broker fill evidence (overseas daily-order / Upbit order-state). "
        "Books fills/journals/realized_pnl ONLY from confirmed fills (delta-idempotent); "
        "marks unfilled/cancelled without journal side-effects. dry_run=True by default. "
        "KR domestic uses kis_live_reconcile_orders instead."
    ),
)
async def live_reconcile_orders(
    market: str | None = None,
    broker: str | None = None,
    symbol: str | None = None,
    order_id: str | None = None,
    dry_run: bool = True,
    limit: int = 100,
) -> dict[str, Any]:
    from app.mcp_server.tooling.live_order_ledger import live_reconcile_orders_impl

    return await live_reconcile_orders_impl(
        market=market,
        broker=broker,
        symbol=symbol,
        order_id=order_id,
        dry_run=dry_run,
        limit=limit,
    )
```
파일 상단에 `mcp`, `Any`가 이미 import돼 있는지 확인(`kis_live_reconcile_orders`가 같은 것을 쓰므로 존재).

- [ ] **Step 4: 등록 검증 (import 에러 없음)**

Run: `uv run python -c "import app.mcp_server.tooling.orders_kis_variants"`
Expected: 에러 없이 종료(0).

- [ ] **Step 5: 커밋**

```bash
git add app/mcp_server/tooling/orders_kis_variants.py tests/mcp_server/tooling/test_live_order_ledger.py
git commit -m "feat(ROB-407): register live_reconcile_orders MCP tool"
```

---

## Task 7: `_execute_and_record` US live 라우팅 전환

**Files:**
- Modify: `app/mcp_server/tooling/order_execution.py` (KR carve-out 분기 다음)
- Test: `tests/mcp_server/tooling/test_execute_and_record_routing.py`

- [ ] **Step 1: 라우팅 테스트 작성 (US live → accepted-only, _record_fill_and_journals 미호출)**

`tests/mcp_server/tooling/test_execute_and_record_routing.py`:
```python
import pytest
from unittest.mock import AsyncMock, patch


@pytest.mark.unit
@pytest.mark.asyncio
async def test_us_live_routes_to_accepted_only(monkeypatch):
    from app.mcp_server.tooling import order_execution as oe

    # execute_order가 broker accept(odno 반환)했다고 가정
    exec_result = {"rt_cd": "0", "odno": "US-ROUTE-1", "ovrs_excg_cd": "NASD", "output": {}}

    with (
        patch.object(oe, "_execute_order_phase", new=AsyncMock(return_value=exec_result))
        if hasattr(oe, "_execute_order_phase")
        else patch.object(oe, "execute_order", new=AsyncMock(return_value=exec_result)),
        patch.object(oe, "_record_fill_and_journals", new=AsyncMock()) as m_legacy,
        patch.object(oe, "_record_live_order", new=AsyncMock(return_value={"fill_recorded": False, "ledger_id": 1})) as m_accept,
    ):
        result = await oe._execute_and_record(
            normalized_symbol="AAPL", side="buy", order_type="limit",
            order_quantity=2.0, price=190.0, market_type="equity_us",
            current_price=191.0, avg_price=0.0, dry_run_result={"price": 190.0, "quantity": 2.0, "estimated_value": 380.0},
            order_amount=380.0, reason="r", exit_reason=None, thesis="t", strategy="s",
            target_price=None, stop_loss=None, min_hold_days=None, notes=None,
            indicators_snapshot=None, defensive_trim_ctx=None, order_error_fn=lambda *a, **k: None,
            is_mock=False,
        )

    m_accept.assert_awaited_once()      # accepted-only 경로
    m_legacy.assert_not_awaited()       # 선반영 경로 미사용
    assert result["fill_recorded"] is False
```

NOTE: 실제 execute 단계 함수명은 `_execute_and_record` 본문(line ~647–701)을 열어 확인한다. 위 테스트는 `_execute_order_phase` 또는 `execute_order` 중 실제 사용되는 것을 patch하도록 가드를 둔다 — 구현 확인 후 한쪽으로 단순화할 것.

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/mcp_server/tooling/test_execute_and_record_routing.py -v`
Expected: FAIL — `_record_live_order` (order_execution 네임스페이스) 미정의 / 라우팅 분기 없음

- [ ] **Step 3: 라우팅 + `_record_live_order` 구현**

먼저 `app/mcp_server/tooling/live_order_ledger.py`에 accepted-only 응답 래퍼 `_record_live_order` 추가:
```python
async def _record_live_order(
    *,
    broker: str,
    account_scope: str,
    market: str,
    normalized_symbol: str,
    exchange: str | None,
    market_symbol: str | None,
    side: str,
    order_kind: str,
    currency: str,
    order_no: str | None,
    order_time: str | None,
    rt_cd: str | None,
    response_message: str | None,
    dry_run_result: dict[str, Any],
    execution_result: dict[str, Any],
    reason: str | None,
    exit_reason: str | None,
    thesis: str | None,
    strategy: str | None,
    target_price: float | None,
    stop_loss: float | None,
    min_hold_days: int | None,
    notes: str | None,
    indicators_snapshot: dict[str, Any] | None,
    inline_confirm: bool = False,
) -> dict[str, Any]:
    price_val = _to_float(dry_run_result.get("price"), default=0.0)
    qty_val = _to_float(dry_run_result.get("quantity"), default=0.0)
    amt_val = _to_float(dry_run_result.get("estimated_value"), default=0.0)
    status = _derive_live_send_status(
        rt_cd=rt_cd, order_no=str(order_no) if order_no else None
    )
    ledger_id = await _save_live_order_ledger(
        broker=broker, account_scope=account_scope, market=market,
        symbol=normalized_symbol, exchange=exchange, market_symbol=market_symbol,
        side=side, order_kind=order_kind, quantity=qty_val, price=price_val,
        amount=amt_val, currency=currency, order_no=str(order_no) if order_no else None,
        order_time=order_time, status=status, response_code=rt_cd,
        response_message=response_message, raw_response=execution_result,
        reason=reason, thesis=thesis, strategy=strategy, target_price=target_price,
        stop_loss=stop_loss, min_hold_days=min_hold_days, notes=notes,
        exit_reason=exit_reason, indicators_snapshot=indicators_snapshot,
    )
    fill_recorded = False
    inline_outcome: dict[str, Any] | None = None
    if inline_confirm and status == "accepted":
        row = await _load_live_ledger_row(ledger_id)
        if row is not None:
            inline_outcome = await _reconcile_one_live_row(row, dry_run=False)
            fill_recorded = inline_outcome.get("action") == "booked"
    return {
        "success": True,
        "dry_run": False,
        "preview": dry_run_result,
        "execution": execution_result,
        "broker": broker,
        "account_scope": account_scope,
        "market": market,
        "ledger_id": ledger_id,
        "order_id": str(order_no) if order_no else None,
        "broker_status": status,
        "fill_recorded": fill_recorded,
        "journal_created": bool(inline_outcome and inline_outcome.get("journal_id")),
        "inline_reconcile": inline_outcome,
        "message": (
            "Live order accepted (pending fill); run live_reconcile_orders to book fill"
            if status == "accepted" and not fill_recorded
            else ("Live order filled inline" if fill_recorded else f"Live order not accepted (broker_status={status})")
        ),
    }
```
그다음 `app/mcp_server/tooling/order_execution.py`의 KR carve-out 분기(`if not is_mock and market_type == "equity_kr": ...` 블록) **바로 다음**에 US 분기 추가:
```python
    # ROB-407: US/해외 live 주문도 accepted-only 기록; fill/journal/pnl은
    # live_reconcile_orders가 broker 체결 증거(해외 일별주문)로만 반영.
    if not is_mock and market_type == "equity_us":
        from app.mcp_server.tooling.live_order_ledger import _record_live_order

        exchange = (
            execution_result.get("ovrs_excg_cd")
            or (execution_result.get("output") or {}).get("OVRS_EXCG_CD")
        )
        return await _record_live_order(
            broker="kis",
            account_scope="kis_live",
            market="us",
            normalized_symbol=normalized_symbol,
            exchange=str(exchange) if exchange else None,
            market_symbol=None,
            side=side,
            order_kind=order_type,
            currency="USD",
            order_no=execution_result.get("odno") or execution_result.get("ord_no"),
            order_time=execution_result.get("ord_tmd"),
            rt_cd=str(execution_result.get("rt_cd", "")) or None,
            response_message=execution_result.get("msg") or execution_result.get("msg1"),
            dry_run_result=dry_run_result,
            execution_result=execution_result,
            reason=reason,
            exit_reason=exit_reason,
            thesis=thesis,
            strategy=strategy,
            target_price=target_price,
            stop_loss=stop_loss,
            min_hold_days=min_hold_days,
            notes=notes,
            indicators_snapshot=indicators_snapshot,
            inline_confirm=False,
        )
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/mcp_server/tooling/test_execute_and_record_routing.py tests/mcp_server/tooling/test_live_order_ledger.py -v`
Expected: PASS

- [ ] **Step 5: 커밋**

```bash
git add app/mcp_server/tooling/order_execution.py app/mcp_server/tooling/live_order_ledger.py tests/mcp_server/tooling/test_execute_and_record_routing.py
git commit -m "feat(ROB-407): route equity_us live to accepted-only ledger"
```

---

## Task 8: KR 회귀 가드 + PR1 전체 검증

**Files:**
- Test: `tests/mcp_server/tooling/test_execute_and_record_routing.py` (추가)

- [ ] **Step 1: KR 회귀 가드 테스트 작성**

```python
@pytest.mark.unit
@pytest.mark.asyncio
async def test_kr_live_still_routes_to_kis_ledger():
    from app.mcp_server.tooling import order_execution as oe

    exec_result = {"rt_cd": "0", "odno": "KR-1", "output": {}}
    with (
        patch.object(oe, "execute_order", new=AsyncMock(return_value=exec_result))
        if hasattr(oe, "execute_order")
        else patch.object(oe, "_execute_order_phase", new=AsyncMock(return_value=exec_result)),
        patch("app.mcp_server.tooling.kis_live_ledger._record_kis_live_order", new=AsyncMock(return_value={"fill_recorded": False})) as m_kr,
        patch.object(oe, "_record_live_order", new=AsyncMock()) as m_generic,
        patch.object(oe, "_record_fill_and_journals", new=AsyncMock()) as m_legacy,
    ):
        await oe._execute_and_record(
            normalized_symbol="005930", side="buy", order_type="limit",
            order_quantity=1.0, price=70000.0, market_type="equity_kr",
            current_price=70000.0, avg_price=0.0,
            dry_run_result={"price": 70000.0, "quantity": 1.0, "estimated_value": 70000.0},
            order_amount=70000.0, reason="r", exit_reason=None, thesis=None, strategy=None,
            target_price=None, stop_loss=None, min_hold_days=None, notes=None,
            indicators_snapshot=None, defensive_trim_ctx=None, order_error_fn=lambda *a, **k: None,
            is_mock=False,
        )
    m_kr.assert_awaited_once()        # 기존 KR ledger 경로 유지
    m_generic.assert_not_awaited()    # 제네릭 경로로 새지 않음
    m_legacy.assert_not_awaited()
```

- [ ] **Step 2: 통과 확인**

Run: `uv run pytest tests/mcp_server/tooling/test_execute_and_record_routing.py -v`
Expected: PASS

- [ ] **Step 3: PR1 풀 게이트 (메모리: pre-merge full-CI gate)**

```bash
uv run ruff check app/ tests/
uv run ruff format --check app/ tests/
uv run pytest tests/mcp_server/tooling/test_live_order_ledger.py tests/mcp_server/tooling/test_live_order_evidence_us.py tests/mcp_server/tooling/test_execute_and_record_routing.py -v
```
Expected: lint 클린, 모든 신규 테스트 PASS. (import guard가 있으면 함께 실행.)

- [ ] **Step 4: 커밋 + PR1 생성**

```bash
git add tests/mcp_server/tooling/test_execute_and_record_routing.py
git commit -m "test(ROB-407): KR live routing regression guard"
git push -u origin <branch>
gh pr create --base main --title "feat(ROB-407): live 주문 선반영 차단 — US/해외 accepted-only + reconcile" --body "..."
```
PR body에 안전 경계(실 live 제출 0, dry-run/fake/read-only)와 operator-gated migration 명시. Test workflow green 확인 후 머지.

---

# PR2 — crypto/Upbit (PR1 머지 후 origin/main 기준 새 브랜치)

## Task 9: Upbit evidence 어댑터

**Files:**
- Modify: `app/mcp_server/tooling/live_order_evidence.py`
- Test: `tests/mcp_server/tooling/test_live_order_evidence_upbit.py`

- [ ] **Step 1: Upbit 어댑터 테스트 작성**

`tests/mcp_server/tooling/test_live_order_evidence_upbit.py`:
```python
import pytest
from decimal import Decimal
from unittest.mock import AsyncMock, patch


def _detail(**kw):
    base = {"uuid": "U-1", "state": "wait", "executed_volume": "0",
            "remaining_volume": "1", "avg_price": None, "price": "100"}
    base.update(kw)
    return base


@pytest.mark.unit
@pytest.mark.asyncio
async def test_upbit_adapter_wait_is_pending():
    from app.mcp_server.tooling import live_order_evidence as ev
    from app.services.brokers.kis.mock_scalping_exec.fill_evidence import FillVerdict

    class _Row:
        order_no = "U-1"
    with patch.object(ev, "fetch_order_detail", new=AsyncMock(return_value=_detail())):
        e = await ev.UpbitEvidenceAdapter().fetch_evidence(_Row())
    assert e.verdict == FillVerdict.PENDING


@pytest.mark.unit
@pytest.mark.asyncio
async def test_upbit_adapter_done_filled():
    from app.mcp_server.tooling import live_order_evidence as ev
    from app.services.brokers.kis.mock_scalping_exec.fill_evidence import FillVerdict

    class _Row:
        order_no = "U-1"
    detail = _detail(state="done", executed_volume="1", remaining_volume="0", avg_price="101.5")
    with patch.object(ev, "fetch_order_detail", new=AsyncMock(return_value=detail)):
        e = await ev.UpbitEvidenceAdapter().fetch_evidence(_Row())
    assert e.verdict == FillVerdict.FILLED
    assert e.filled_qty == Decimal("1")
    assert e.avg_price == Decimal("101.5")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_upbit_adapter_partial():
    from app.mcp_server.tooling import live_order_evidence as ev
    from app.services.brokers.kis.mock_scalping_exec.fill_evidence import FillVerdict

    class _Row:
        order_no = "U-1"
    detail = _detail(state="wait", executed_volume="0.4", remaining_volume="0.6", avg_price="100")
    with patch.object(ev, "fetch_order_detail", new=AsyncMock(return_value=detail)):
        e = await ev.UpbitEvidenceAdapter().fetch_evidence(_Row())
    assert e.verdict == FillVerdict.PARTIAL
    assert e.filled_qty == Decimal("0.4")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_upbit_adapter_cancelled_zero_fill_is_none():
    from app.mcp_server.tooling import live_order_evidence as ev
    from app.services.brokers.kis.mock_scalping_exec.fill_evidence import FillVerdict

    class _Row:
        order_no = "U-1"
    detail = _detail(state="cancel", executed_volume="0", remaining_volume="0", avg_price=None)
    with patch.object(ev, "fetch_order_detail", new=AsyncMock(return_value=detail)):
        e = await ev.UpbitEvidenceAdapter().fetch_evidence(_Row())
    assert e.verdict == FillVerdict.NONE
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/mcp_server/tooling/test_live_order_evidence_upbit.py -v`
Expected: FAIL — `UpbitEvidenceAdapter` 미정의

- [ ] **Step 3: Upbit 어댑터 구현**

`app/mcp_server/tooling/live_order_evidence.py` 상단 import 추가:
```python
from app.services.brokers.upbit.orders import fetch_order_detail
```
`_to_decimal` 헬퍼가 이 모듈에 없으면 추가:
```python
def _to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None
```
어댑터 클래스 추가(`UsOverseasEvidenceAdapter` 다음):
```python
class UpbitEvidenceAdapter:
    broker = "upbit"

    async def fetch_evidence(self, row: Any) -> FillEvidence:
        detail = await fetch_order_detail(str(row.order_no))
        if not detail:
            return FillEvidence(
                FillVerdict.PENDING, Decimal("0"), None, None, "not_found",
                f"order {row.order_no} detail empty",
            )
        state = str(detail.get("state", "")).strip()
        executed = _to_decimal(detail.get("executed_volume")) or Decimal("0")
        remaining = _to_decimal(detail.get("remaining_volume")) or Decimal("0")
        avg = _to_decimal(detail.get("avg_price")) or _to_decimal(detail.get("price"))

        # 체결분이 있으면 (취소 후 부분체결 포함) 체결을 우선 인정
        if executed > 0 and avg and avg > 0:
            verdict = FillVerdict.FILLED if remaining <= 0 else FillVerdict.PARTIAL
            return FillEvidence(verdict, executed, avg, None, verdict.value,
                                f"upbit {row.order_no} {verdict.value} {executed}@{avg}")
        if state == "wait":
            return FillEvidence(FillVerdict.PENDING, Decimal("0"), None, None,
                                "pending", f"upbit {row.order_no} waiting")
        # done/cancel with zero executed → 미체결 종료
        return FillEvidence(FillVerdict.NONE, Decimal("0"), None, None,
                            "cancelled", f"upbit {row.order_no} ended unfilled")
```
레지스트리에 등록:
```python
_ADAPTERS: dict[str, LiveFillEvidenceAdapter] = {
    "kis": UsOverseasEvidenceAdapter(),
    "upbit": UpbitEvidenceAdapter(),
}
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/mcp_server/tooling/test_live_order_evidence_upbit.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: 커밋**

```bash
git add app/mcp_server/tooling/live_order_evidence.py tests/mcp_server/tooling/test_live_order_evidence_upbit.py
git commit -m "feat(ROB-407): Upbit fill-evidence adapter (state + executed_volume)"
```

---

## Task 10: crypto live 라우팅 (지정가 accepted-only / 시장가 inline 확인)

**Files:**
- Modify: `app/mcp_server/tooling/order_execution.py` (US 분기 다음)
- Test: `tests/mcp_server/tooling/test_crypto_live_routing.py`

- [ ] **Step 1: crypto 라우팅 테스트 작성**

`tests/mcp_server/tooling/test_crypto_live_routing.py`:
```python
import pytest
from unittest.mock import AsyncMock, patch


def _exec(**kw):
    base = {"uuid": "U-ROUTE-1"}
    base.update(kw)
    return base


@pytest.mark.unit
@pytest.mark.asyncio
async def test_crypto_limit_is_accepted_only():
    from app.mcp_server.tooling import order_execution as oe

    with (
        patch.object(oe, "execute_order", new=AsyncMock(return_value=_exec()))
        if hasattr(oe, "execute_order")
        else patch.object(oe, "_execute_order_phase", new=AsyncMock(return_value=_exec())),
        patch.object(oe, "_record_fill_and_journals", new=AsyncMock()) as m_legacy,
        patch.object(oe, "_record_live_order", new=AsyncMock(return_value={"fill_recorded": False})) as m_accept,
    ):
        await oe._execute_and_record(
            normalized_symbol="BTC", side="buy", order_type="limit",
            order_quantity=0.01, price=50_000_000.0, market_type="crypto",
            current_price=50_000_000.0, avg_price=0.0,
            dry_run_result={"price": 50_000_000.0, "quantity": 0.01, "estimated_value": 500_000.0},
            order_amount=500_000.0, reason="r", exit_reason=None, thesis="t", strategy="s",
            target_price=None, stop_loss=None, min_hold_days=None, notes=None,
            indicators_snapshot=None, defensive_trim_ctx=None, order_error_fn=lambda *a, **k: None,
            is_mock=False,
        )
    m_accept.assert_awaited_once()
    _, kw = m_accept.await_args
    assert kw["broker"] == "upbit"
    assert kw["inline_confirm"] is False     # 지정가 = reconcile 위임
    m_legacy.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_crypto_market_inline_confirm():
    from app.mcp_server.tooling import order_execution as oe

    with (
        patch.object(oe, "execute_order", new=AsyncMock(return_value=_exec()))
        if hasattr(oe, "execute_order")
        else patch.object(oe, "_execute_order_phase", new=AsyncMock(return_value=_exec())),
        patch.object(oe, "_record_live_order", new=AsyncMock(return_value={"fill_recorded": True})) as m_accept,
    ):
        await oe._execute_and_record(
            normalized_symbol="BTC", side="buy", order_type="market",
            order_quantity=0.01, price=None, market_type="crypto",
            current_price=50_000_000.0, avg_price=0.0,
            dry_run_result={"price": 0.0, "quantity": 0.01, "estimated_value": 500_000.0},
            order_amount=500_000.0, reason="r", exit_reason=None, thesis="t", strategy="s",
            target_price=None, stop_loss=None, min_hold_days=None, notes=None,
            indicators_snapshot=None, defensive_trim_ctx=None, order_error_fn=lambda *a, **k: None,
            is_mock=False,
        )
    _, kw = m_accept.await_args
    assert kw["inline_confirm"] is True       # 시장가 = 전송 직후 inline 확인
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/mcp_server/tooling/test_crypto_live_routing.py -v`
Expected: FAIL — crypto 분기 없음

- [ ] **Step 3: crypto 분기 구현**

`app/mcp_server/tooling/order_execution.py`의 US 분기(`if not is_mock and market_type == "equity_us": ...`) **바로 다음**에 추가:
```python
    # ROB-407: crypto live 주문. 지정가 pending은 accepted-only(reconcile 위임),
    # 시장가는 전송 직후 inline evidence 확인으로 체결 반영.
    if not is_mock and market_type == "crypto":
        from app.mcp_server.tooling.live_order_ledger import _record_live_order

        is_market = (order_type or "").lower() == "market" or price is None
        market_symbol = (
            execution_result.get("market")
            or dry_run_result.get("market")
        )
        return await _record_live_order(
            broker="upbit",
            account_scope="upbit_live",
            market="crypto",
            normalized_symbol=normalized_symbol,
            exchange=None,
            market_symbol=str(market_symbol) if market_symbol else None,
            side=side,
            order_kind="market" if is_market else "limit",
            currency="KRW",
            order_no=execution_result.get("uuid"),
            order_time=execution_result.get("created_at"),
            rt_cd="0" if execution_result.get("uuid") else "1",
            response_message=execution_result.get("error") or None,
            dry_run_result=dry_run_result,
            execution_result=execution_result,
            reason=reason,
            exit_reason=exit_reason,
            thesis=thesis,
            strategy=strategy,
            target_price=target_price,
            stop_loss=stop_loss,
            min_hold_days=min_hold_days,
            notes=notes,
            indicators_snapshot=indicators_snapshot,
            inline_confirm=is_market,
        )
```

NOTE: crypto `execution_result`의 실제 키(`uuid`/`market`/`created_at`/에러 표현)는 crypto live 주문 실행 경로(Upbit submit) 반환을 확인해 맞춘다. uuid 키가 다르면(예: 중첩 dict) 추출을 조정.

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/mcp_server/tooling/test_crypto_live_routing.py tests/mcp_server/tooling/test_live_order_evidence_upbit.py -v`
Expected: PASS

- [ ] **Step 5: 커밋**

```bash
git add app/mcp_server/tooling/order_execution.py tests/mcp_server/tooling/test_crypto_live_routing.py
git commit -m "feat(ROB-407): route crypto live (limit accepted-only / market inline confirm)"
```

---

## Task 11: crypto inline-confirm 통합 테스트 (전송 직후 done → filled)

**Files:**
- Test: `tests/mcp_server/tooling/test_live_order_ledger.py` (추가)

- [ ] **Step 1: inline confirm 경로 테스트 작성**

```python
@pytest.mark.unit
@pytest.mark.asyncio
async def test_record_live_order_inline_confirm_books_on_done():
    from decimal import Decimal
    from unittest.mock import AsyncMock, patch
    from app.mcp_server.tooling import live_order_ledger as ll
    from app.services.brokers.kis.mock_scalping_exec.fill_evidence import (
        FillEvidence, FillVerdict,
    )

    filled = FillEvidence(FillVerdict.FILLED, Decimal("0.01"), Decimal("50000000"), None, "filled", "")

    class _Adapter:
        broker = "upbit"
        fetch_evidence = AsyncMock(return_value=filled)

    with (
        patch.object(ll, "get_evidence_adapter", return_value=_Adapter()),
        patch.object(ll, "_save_order_fill", new=AsyncMock(return_value=222)),
        patch.object(ll, "_create_trade_journal_for_buy",
                     new=AsyncMock(return_value={"journal_created": True, "journal_id": 12})),
        patch.object(ll, "_link_journal_to_fill", new=AsyncMock()),
    ):
        out = await ll._record_live_order(
            broker="upbit", account_scope="upbit_live", market="crypto",
            normalized_symbol="BTC", exchange=None, market_symbol="KRW-BTC",
            side="buy", order_kind="market", currency="KRW",
            order_no="U-INLINE-1", order_time=None, rt_cd="0", response_message=None,
            dry_run_result={"price": 0.0, "quantity": 0.01, "estimated_value": 500000.0},
            execution_result={"uuid": "U-INLINE-1"},
            reason=None, exit_reason=None, thesis="t", strategy="s",
            target_price=None, stop_loss=None, min_hold_days=None, notes=None,
            indicators_snapshot=None, inline_confirm=True,
        )
    assert out["fill_recorded"] is True
    assert out["inline_reconcile"]["action"] == "booked"
```

- [ ] **Step 2: 통과 확인**

Run: `uv run pytest tests/mcp_server/tooling/test_live_order_ledger.py::test_record_live_order_inline_confirm_books_on_done -v`
Expected: PASS (Task 7의 `_record_live_order` inline_confirm 경로가 이미 구현됨)

- [ ] **Step 3: 커밋**

```bash
git add tests/mcp_server/tooling/test_live_order_ledger.py
git commit -m "test(ROB-407): crypto inline-confirm books fill on done evidence"
```

---

## Task 12: PR2 전체 검증 + 런북 + PR

**Files:**
- Create/Modify: `docs/runbooks/live-order-reconcile.md` (신규 — US/crypto reconcile 운영 절차)
- Modify: `CLAUDE.md` (ROB-407 섹션 추가 — KR carve-out 후속 완료 명시)

- [ ] **Step 1: 런북 작성**

`docs/runbooks/live-order-reconcile.md`에 작성: `live_reconcile_orders`(dry_run 기본) 사용법, market/broker 필터, US 거래소 순회·crypto state 증거 소스, fail-closed/델타 멱등 동작, operator-gated `alembic upgrade head` 절차, 안전 경계(실 live 제출 금지). `docs/runbooks/kis-live-order-reconcile.md`를 템플릿으로 삼되 US/crypto로 일반화.

- [ ] **Step 2: CLAUDE.md 섹션 추가**

`CLAUDE.md`의 "KIS Live Order Fill-Evidence Gate (ROB-395)" 섹션 다음에 ROB-407 섹션 추가: 제네릭 `review.live_order_ledger`, `live_reconcile_orders` 도구, US 해외 일별주문 + Upbit order-state 증거, KR은 기존 경로 유지, operator-gated migration 명시.

- [ ] **Step 3: PR2 풀 게이트**

```bash
uv run ruff check app/ tests/
uv run ruff format --check app/ tests/
uv run pytest tests/mcp_server/tooling/test_live_order_evidence_upbit.py tests/mcp_server/tooling/test_crypto_live_routing.py tests/mcp_server/tooling/test_live_order_ledger.py -v
```
Expected: lint 클린, 모든 신규 테스트 PASS.

- [ ] **Step 4: 커밋 + PR2 생성**

```bash
git add docs/runbooks/live-order-reconcile.md CLAUDE.md
git commit -m "docs(ROB-407): live order reconcile runbook + CLAUDE.md (crypto)"
git push -u origin <branch>
gh pr create --base main --title "feat(ROB-407): crypto live 주문 선반영 차단 — Upbit evidence + inline 확인" --body "..."
```
Test workflow green 확인 후 머지.

---

## Self-Review (작성자 체크 — 실행자 참고)

**Spec coverage:** ① equity_us 지정가 선반영 제거 = Task 7. ② crypto 지정가 accepted-only + done 후 fill = Task 9/10/11. ③ 취소/거절/부분/미체결 오반영 방지 = Task 5(델타 멱등·CANCELLED no-journal) + Task 9(Upbit NONE). ④ KR 무회귀 = Task 8. ⑤ 테스트/CI/스모크 evidence = Task 8/12 게이트 + (operator) 라이브 스모크는 후속. 제네릭 ledger/어댑터/migration = Task 1–6. ✅ 전 섹션 커버.

**알려진 검증 필요점(실행 중 반드시 확인):**
- `_save_order_fill`/`_create_trade_journal_for_buy`/`_close_journals_on_sell`/`_link_journal_to_fill` 실제 시그니처(인자명) — `app/mcp_server/tooling/order_journal.py`.
- `_execute_and_record` 내부 실 execute 단계 함수명(`execute_order` vs `_execute_order_phase`) — 테스트 patch 대상 정합.
- `_create_live_kis_client`가 `kis_live_ledger.py`에 존재하는지(없으면 live KIS client 생성 함수명 확인).
- crypto live `execution_result`의 uuid/market/created_at 키 — Upbit submit 반환 실제 형태.
- 해외 row 추가 필드(부분체결 시 `nccs_qty`)는 본 설계에서 불필요(ft_ccld_qty 누적이 evidence) — 확인만.

**Placeholder scan:** PR body `"..."`는 의도적 자리(실행자가 채움). 그 외 모든 코드 스텝은 실제 코드 포함. ✅

**Type consistency:** `_record_live_order`(Task 7) ↔ 라우팅 호출(Task 7/10) 인자명 일치; `live_reconcile_orders_impl`(Task 5) ↔ MCP 도구(Task 6) 인자명 일치; `FillEvidence` 위치인자 순서(verdict, filled_qty, avg_price, category, code, message) 전 테스트 동일. ✅

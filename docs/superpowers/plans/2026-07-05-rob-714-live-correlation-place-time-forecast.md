# ROB-714: live 레저 correlation_id + place-time 자동 forecast 발행 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** live 주문 레저 3종에 `correlation_id` 스파인을 심고, 주문 접수(send) 시점에 결정론적 correlation_id를 mint + 최소 price_target forecast를 자동 발행하여 ROB-682의 "structurally dead" forecast↔회고 join을 되살린다.

**Architecture:** ROB-705가 paper 경로(`paper_limit_order_service.py`)에 배선한 place-time provenance 패턴을 live 3경로로 확장한다. (1) additive nullable migration ×3으로 `correlation_id` 컬럼을 추가하고, (2) 순수 헬퍼 `live_correlation_id()`로 send 시점에 id를 mint해 레저 row에 저장하며, (3) buy+target 주문에 한해 격리 세션에서 최소 forecast를 발행하고, (4) 기존 evidence-gated reconcile이 buy 저널을 만들 때 `row.correlation_id`를 저널에 backfill한다. 주문 hot path는 DB write only — 브로커 콜 추가 없음(ROB-671).

**Tech Stack:** Python 3.13, SQLAlchemy 2.0 async, Alembic (async env), asyncpg/PostgreSQL(`review` schema), pytest(asyncio, xdist shared-DB).

## Global Constraints

- **ROB-501**: `app/**/*.py`는 in-process LLM provider import/인스턴스화 금지 — 본 작업은 전부 결정론적 집계/write 코드. 정적 가드가 스캔한다.
- **ROB-671 (주문 hot path 무네트워크)**: send 경로에서 신규 브로커 콜·마켓캘린더 조회 금지. review_date는 **캘린더-일 오프셋**(`now_kst().date() + timedelta(days=N)`)으로 계산 — 마켓 캘린더/네트워크 미접촉.
- **ROB-395/407 (evidence-gated journal 계약)**: 저널/체결/realized_pnl은 여전히 **reconcile 시점 fill 증거로만** 생성. send는 accepted-only. correlation_id의 journal backfill도 reconcile 시점에서만 수행.
- **Migration은 additive nullable only**: `ADD COLUMN correlation_id TEXT` + `CREATE INDEX` 뿐. CHECK/NOT NULL/FK 없음 → ROB-705의 double-prefix constraint 함정 **비해당**.
- **best-effort provenance**: forecast/journal 발행 실패는 로그만 남기고 주문을 절대 롤백하지 않는다(격리 세션). paper 패턴(`paper_limit_order_service.py:296-373`)과 동일.
- **버전 스탬핑 무관**: 자동 forecast는 policy_version 인용 불필요(수동 forecast_save가 풍부화 담당).
- **스코프 밖(이번 PR 아님)**: /insights 프론트 join 승격(ROB-682/715), /invest 아이템 렌더(ROB-715), negative-class 규약(ROB-712), 저널 집계(ROB-713), sell-side forecast, forecast_id 레저 컬럼.

## Decisions locked (2026-07-05, 사용자 확정)

- **D1 — 자동 forecast 기본값 = 고정값.** `probability=0.5`(중립, "아직 캘리브레이션 주장 없음"), `review_date = now_kst().date() + timedelta(days=N)`, `N = min_hold_days if present else 10`. **buy + target_price 있을 때만** price_target forecast 발행(`direction="at_or_above"`); target 없으면 forecast skip.
- **D2 — correlation_id = 전용 헬퍼로 항상 mint.** 신규 `live_correlation_id()`가 idempotency_key와 무관하게 **모든 주문(buy/sell 불문)** row에 채운다 → journal backfill·미래 수동 forecast_save가 붙을 스파인 보장.
- **D3 — /insights join 승격은 후속 분리.** 이 PR = migration×3 + place-time 발행(백엔드 스파인)까지.

**성공 기준 재정의:** 신규 live 주문의 **100%가 correlation_id 스파인 보유**, 그중 **buy+target 주문은 place-time forecast 링크 보유**. (target 없는 주문·sell은 corr_id만 — 정직한 해석, target 날조 안 함.)

## File Structure

- **Create** `app/services/live_correlation.py` — 순수 헬퍼 `live_correlation_id()`. `paper_correlation.py` 미러. I/O·LLM 없음.
- **Create** `app/services/live_place_provenance.py` — `publish_place_time_forecast()`. 격리 세션 best-effort forecast 발행. 3경로 공용(DRY).
- **Modify** `app/models/review.py` — `KISLiveOrderLedger`(~328), `LiveOrderLedger`(~418), `TossLiveOrderLedger`(~572)에 `correlation_id` mapped_column + Index ×3.
- **Create** `alembic/versions/<rev>_rob714_live_ledger_correlation_id.py` — ADD COLUMN×3 + CREATE INDEX×3.
- **Modify** `app/mcp_server/tooling/kis_live_ledger.py` — `_save_kis_live_order_ledger`/`_record_kis_live_order`에 corr_id 배선 + forecast 발행; `_reconcile_one_ledger_row`(~703) journal backfill.
- **Modify** `app/mcp_server/tooling/live_order_ledger.py` — `_save_live_order_ledger`/`_record_live_order` corr_id 배선 + forecast; `_reconcile_one_live_row` journal backfill.
- **Modify** `app/mcp_server/tooling/toss_live_ledger.py` — `record_toss_place_order` corr_id mint + forecast; `_reconcile_one_toss_row` journal backfill.
- **Modify** `app/services/toss_live_order_ledger_service.py` — `record_send`에 `correlation_id` 파라미터.
- **Test** `tests/services/test_live_correlation.py`, `tests/services/test_live_place_provenance.py`, `tests/mcp_server/tooling/test_live_ledger_correlation_id.py`(3경로 통합).

**Note (worktree):** 실행 시작 시 `superpowers:using-git-worktrees`로 `/Users/mgh3326/work/auto_trader.rob-714` 워크트리를 `origin/main` 기준 생성(브랜치 `mgh332696/rob-714-live-레저-correlation_id-place-time-자동-forecast-발행`). canonical repo는 main 고정.

---

## Task 1: 순수 헬퍼 `live_correlation_id()`

**Files:**
- Create: `app/services/live_correlation.py`
- Test: `tests/services/test_live_correlation.py`

**Interfaces:**
- Consumes: 없음 (stdlib only).
- Produces: `live_correlation_id(*, account_scope: str, symbol: str, side: str, price: Decimal, quantity: Decimal, kst_trade_day: str, rung: int = 0) -> str` — 반환 형식 `live:<account_scope>:<sha256_16>`.

- [ ] **Step 1: Write the failing test**

```python
# tests/services/test_live_correlation.py
from decimal import Decimal

import pytest

from app.services.live_correlation import live_correlation_id


@pytest.mark.unit
def test_stable_and_namespaced():
    kwargs = dict(
        account_scope="kis_live",
        symbol="AAPL",
        side="buy",
        price=Decimal("190.50"),
        quantity=Decimal("3"),
        kst_trade_day="2026-07-05",
    )
    a = live_correlation_id(**kwargs)
    b = live_correlation_id(**kwargs)
    assert a == b  # deterministic
    assert a.startswith("live:kis_live:")
    assert len(a.split(":")[-1]) == 16


@pytest.mark.unit
def test_symbol_case_insensitive_but_fields_and_rung_and_scope_vary():
    base = dict(
        account_scope="kis_live",
        symbol="aapl",
        side="buy",
        price=Decimal("190.50"),
        quantity=Decimal("3"),
        kst_trade_day="2026-07-05",
    )
    canon = live_correlation_id(**{**base, "symbol": "AAPL"})
    assert live_correlation_id(**base) == canon  # upper-cased internally
    assert live_correlation_id(**{**base, "side": "sell"}) != canon
    assert live_correlation_id(**{**base, "rung": 1}) != canon
    assert live_correlation_id(**{**base, "account_scope": "toss_live"}) != canon
    assert live_correlation_id(**{**base, "kst_trade_day": "2026-07-06"}) != canon
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/test_live_correlation.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.live_correlation'`

- [ ] **Step 3: Write minimal implementation**

```python
# app/services/live_correlation.py
"""Deterministic correlation-id spine for the LIVE learning loop (ROB-714).

Mirrors app.services.paper_correlation (ROB-705). The canonical string includes
the KST trade-day and a rung discriminator so a re-placed order (after cancel)
or two identical ladder rungs do NOT collide on one id. account_scope namespaces
the id per ledger (kis_live / upbit_live / toss_live). Pure: no I/O, no LLM.
"""

from __future__ import annotations

import hashlib
from decimal import Decimal


def live_correlation_id(
    *,
    account_scope: str,
    symbol: str,
    side: str,
    price: Decimal,
    quantity: Decimal,
    kst_trade_day: str,
    rung: int = 0,
) -> str:
    canonical = "|".join(
        (
            account_scope.lower(),
            symbol.upper(),
            side.lower(),
            format(price, "f"),
            format(quantity, "f"),
            kst_trade_day,
            str(rung),
        )
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return f"live:{account_scope.lower()}:{digest}"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/services/test_live_correlation.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add app/services/live_correlation.py tests/services/test_live_correlation.py
git commit -m "feat(ROB-714): deterministic live_correlation_id spine helper"
```

---

## Task 2: 레저 3종에 `correlation_id` 컬럼 + migration

**Files:**
- Modify: `app/models/review.py` (KISLiveOrderLedger ~328, LiveOrderLedger ~418, TossLiveOrderLedger ~572)
- Create: `alembic/versions/<rev>_rob714_live_ledger_correlation_id.py`
- Test: `tests/mcp_server/tooling/test_live_ledger_correlation_id.py` (모델 속성 존재)

**Interfaces:**
- Consumes: Task 1 없음.
- Produces: `KISLiveOrderLedger.correlation_id`, `LiveOrderLedger.correlation_id`, `TossLiveOrderLedger.correlation_id` (`Mapped[str | None]`, Text, nullable). 인덱스 `ix_kis_live_ledger_correlation_id` / `ix_live_ledger_correlation_id` / `ix_toss_live_ledger_correlation_id`.

- [ ] **Step 1: Write the failing test**

```python
# tests/mcp_server/tooling/test_live_ledger_correlation_id.py
import pytest

from app.models.review import (
    KISLiveOrderLedger,
    LiveOrderLedger,
    TossLiveOrderLedger,
)


@pytest.mark.unit
@pytest.mark.parametrize(
    "model", [KISLiveOrderLedger, LiveOrderLedger, TossLiveOrderLedger]
)
def test_correlation_id_column_present_and_nullable(model):
    col = model.__table__.c.correlation_id
    assert col is not None
    assert col.nullable is True
    # indexed for join lookups
    index_cols = {
        tuple(c.name for c in idx.columns) for idx in model.__table__.indexes
    }
    assert ("correlation_id",) in index_cols
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/mcp_server/tooling/test_live_ledger_correlation_id.py -v`
Expected: FAIL — `AttributeError`/`KeyError: 'correlation_id'` on `__table__.c`.

- [ ] **Step 3a: Add column + index to each model in `app/models/review.py`**

`KISLiveOrderLedger` — `report_item_uuid`(328) 바로 아래에 추가:

```python
    # ROB-714 — learning-loop provenance spine (send-time mint, immutable).
    # Links this order to its place-time forecast + reconcile-time journal +
    # retrospective. NULL for legacy rows. See app.services.live_correlation.
    correlation_id: Mapped[str | None] = mapped_column(Text)
```

`KISLiveOrderLedger.__table_args__`의 Index 목록(≈282 근처)에 추가:

```python
        Index("ix_kis_live_ledger_correlation_id", "correlation_id"),
```

`LiveOrderLedger` — `report_item_uuid`(418) 아래에 동일 `correlation_id` 컬럼 추가; `__table_args__`(≈368)에:

```python
        Index("ix_live_ledger_correlation_id", "correlation_id"),
```

`TossLiveOrderLedger` — `report_item_uuid`(572) 아래에 동일 컬럼 추가; `__table_args__`(≈523)에:

```python
        Index("ix_toss_live_ledger_correlation_id", "correlation_id"),
```

- [ ] **Step 3b: Generate the migration**

Run: `uv run alembic revision --autogenerate -m "ROB-714 live ledger correlation_id"`

- [ ] **Step 3c: 생성된 migration을 검토하고 아래 형태로 정리**

autogenerate가 3 컬럼 + 3 인덱스를 잡았는지 확인하고, `upgrade`/`downgrade`가 정확히 아래와 같은지 정리(다른 drift가 섞였으면 제거):

```python
"""ROB-714 live ledger correlation_id

Revision ID: <rev>
Revises: <down_rev>
"""
from alembic import op
import sqlalchemy as sa

revision = "<rev>"
down_revision = "<down_rev>"
branch_labels = None
depends_on = None

_TABLES = ("kis_live_order_ledger", "live_order_ledger", "toss_live_order_ledger")
_INDEXES = {
    "kis_live_order_ledger": "ix_kis_live_ledger_correlation_id",
    "live_order_ledger": "ix_live_ledger_correlation_id",
    "toss_live_order_ledger": "ix_toss_live_ledger_correlation_id",
}


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(
            table,
            sa.Column("correlation_id", sa.Text(), nullable=True),
            schema="review",
        )
        op.create_index(
            _INDEXES[table], table, ["correlation_id"], schema="review"
        )


def downgrade() -> None:
    for table in _TABLES:
        op.drop_index(_INDEXES[table], table_name=table, schema="review")
        op.drop_column(table, "correlation_id", schema="review")
```

- [ ] **Step 4: Run model test + apply/rollback migration**

Run: `uv run pytest tests/mcp_server/tooling/test_live_ledger_correlation_id.py -v`
Expected: PASS (3 passed)

Run: `uv run alembic upgrade head && uv run alembic downgrade -1 && uv run alembic upgrade head`
Expected: 3회 모두 에러 없이 완료(additive라 abort 없음).

- [ ] **Step 5: Commit**

```bash
git add app/models/review.py alembic/versions/*rob714*correlation_id*.py tests/mcp_server/tooling/test_live_ledger_correlation_id.py
git commit -m "feat(ROB-714): add correlation_id column+index to live ledger x3 (additive migration)"
```

---

## Task 3: send 경로 `_save_*`/`record_send`에 correlation_id 배선

**Files:**
- Modify: `app/mcp_server/tooling/kis_live_ledger.py:89-118` (`_save_kis_live_order_ledger`)
- Modify: `app/mcp_server/tooling/live_order_ledger.py:42-74` (`_save_live_order_ledger`)
- Modify: `app/services/toss_live_order_ledger_service.py` (`record_send`)
- Test: `tests/mcp_server/tooling/test_live_ledger_correlation_id.py` (round-trip 추가)

**Interfaces:**
- Consumes: Task 2 컬럼.
- Produces: 세 write 함수 모두 `correlation_id: str | None = None` 키워드 파라미터를 받아 row에 기록. 시그니처:
  - `_save_kis_live_order_ledger(..., correlation_id: str | None = None)`
  - `_save_live_order_ledger(..., correlation_id: str | None = None)`
  - `TossLiveOrderLedgerService.record_send(..., correlation_id: str | None = None)`

- [ ] **Step 1: Write the failing test (round-trip)**

`tests/mcp_server/tooling/test_live_ledger_correlation_id.py` 하단에 추가. (`_order_session_factory`가 공유 테스트 DB를 쓰므로 setup write는 flush 후 **commit**하여 병렬 락 회피 — ROB-705 xdist 교훈.)

```python
@pytest.mark.integration
async def test_save_kis_live_ledger_persists_correlation_id(db_session):
    from app.mcp_server.tooling.kis_live_ledger import _save_kis_live_order_ledger
    from app.models.review import KISLiveOrderLedger
    from sqlalchemy import select

    ledger_id = await _save_kis_live_order_ledger(
        symbol="005930",
        instrument_type="equity_kr",
        side="buy",
        order_type="limit",
        quantity=1.0,
        price=70000.0,
        amount=70000.0,
        currency="KRW",
        order_no="TEST-CORR-1",
        order_time=None,
        krx_fwdg_ord_orgno=None,
        status="accepted",
        response_code="0",
        response_message=None,
        raw_response={},
        reason=None,
        thesis="t",
        strategy=None,
        target_price=None,
        stop_loss=None,
        min_hold_days=None,
        notes=None,
        exit_reason=None,
        indicators_snapshot=None,
        correlation_id="live:kis_live:deadbeefdeadbeef",
    )
    row = (
        await db_session.execute(
            select(KISLiveOrderLedger).where(KISLiveOrderLedger.id == ledger_id)
        )
    ).scalar_one()
    assert row.correlation_id == "live:kis_live:deadbeefdeadbeef"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/mcp_server/tooling/test_live_ledger_correlation_id.py::test_save_kis_live_ledger_persists_correlation_id -v`
Expected: FAIL — `TypeError: _save_kis_live_order_ledger() got an unexpected keyword argument 'correlation_id'`

- [ ] **Step 3a: `_save_kis_live_order_ledger` (kis_live_ledger.py)**

시그니처 마지막(`idempotency_key: str | None = None` 다음, line 118)에 추가:

```python
    correlation_id: str | None = None,
```

`.values(...)` 블록(`idempotency_key=idempotency_key,` 다음, line 157)에 추가:

```python
                    correlation_id=correlation_id,
```

- [ ] **Step 3b: `_save_live_order_ledger` (live_order_ledger.py)**

동일하게 시그니처(line 74 부근)에 `correlation_id: str | None = None,` 추가하고, insert `.values(...)` 블록(`report_item_uuid=report_item_uuid,` line 112 부근)에 `correlation_id=correlation_id,` 추가.

- [ ] **Step 3c: `record_send` (toss_live_order_ledger_service.py)**

시그니처(`approval_hash: str | None = None,` 다음)에 `correlation_id: str | None = None,` 추가하고, `row = TossLiveOrderLedger(...)` 생성 kwargs(`approval_hash=approval_hash,` 근처)에 `correlation_id=correlation_id,` 추가. **replay 경로 주의**: `existing is not None`이고 `broker_order_id` 일치 시 기존 row를 그대로 반환(멱등) — correlation_id는 최초 삽입에만 기록되고 replay는 덮어쓰지 않는다(의도된 동작, 스파인 불변).

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/mcp_server/tooling/test_live_ledger_correlation_id.py -v`
Expected: PASS (모델 3 + round-trip 1)

- [ ] **Step 5: Commit**

```bash
git add app/mcp_server/tooling/kis_live_ledger.py app/mcp_server/tooling/live_order_ledger.py app/services/toss_live_order_ledger_service.py tests/mcp_server/tooling/test_live_ledger_correlation_id.py
git commit -m "feat(ROB-714): thread correlation_id through 3 live-ledger write paths"
```

---

## Task 4: 공용 forecast 발행 헬퍼 + KIS KR send 배선

**Files:**
- Create: `app/services/live_place_provenance.py`
- Modify: `app/mcp_server/tooling/kis_live_ledger.py` (`_record_kis_live_order:225-303`)
- Test: `tests/services/test_live_place_provenance.py`

**Interfaces:**
- Consumes: Task 1 `live_correlation_id`, `save_forecast` (`app.services.trade_journal.forecast_service`, review_date/probability/forecast_target/correlation_id/session_label 파라미터 — `paper_limit_order_service.py:347-364` 확인됨).
- Produces:
  - `publish_place_time_forecast(*, correlation_id: str, symbol: str, instrument_type: str, side: str, target_price: float | None, min_hold_days: int | None, session_label: str, created_by: str, report_item_uuid: str | None = None) -> str | None` — buy+target일 때만 격리 세션에서 forecast 발행하고 `forecast_id` 반환; 그 외 `None`; 예외는 삼켜 로그.
  - `_record_kis_live_order`가 corr_id를 mint→`_save_*`에 전달→`publish_place_time_forecast` 호출.

- [ ] **Step 1: Write the failing test**

```python
# tests/services/test_live_place_provenance.py
import pytest

from app.services.live_place_provenance import publish_place_time_forecast


@pytest.mark.integration
async def test_buy_with_target_publishes_at_or_above_forecast(monkeypatch):
    captured = {}

    async def fake_save_forecast(db, **kwargs):
        captured.update(kwargs)

        class _FC:
            forecast_id = "fc-123"

        return "created", _FC()

    monkeypatch.setattr(
        "app.services.live_place_provenance.save_forecast", fake_save_forecast
    )

    fid = await publish_place_time_forecast(
        correlation_id="live:kis_live:abc",
        symbol="005930",
        instrument_type="equity_kr",
        side="buy",
        target_price=80000.0,
        min_hold_days=None,
        session_label="kis_live_place",
        created_by="auto_place_live",
    )
    assert fid == "fc-123"
    assert captured["forecast_target"] == {
        "kind": "price_target",
        "direction": "at_or_above",
        "target_price": 80000.0,
    }
    assert captured["probability"] == 0.5
    assert captured["correlation_id"] == "live:kis_live:abc"
    # default horizon 10 calendar days when min_hold_days is None
    assert captured["horizon"] == "P10D"


@pytest.mark.integration
async def test_sell_or_no_target_skips_forecast(monkeypatch):
    called = False

    async def fake_save_forecast(db, **kwargs):  # pragma: no cover
        nonlocal called
        called = True
        return "created", object()

    monkeypatch.setattr(
        "app.services.live_place_provenance.save_forecast", fake_save_forecast
    )

    assert (
        await publish_place_time_forecast(
            correlation_id="live:kis_live:abc",
            symbol="005930",
            instrument_type="equity_kr",
            side="sell",
            target_price=80000.0,
            min_hold_days=None,
            session_label="kis_live_place",
            created_by="auto_place_live",
        )
        is None
    )
    assert (
        await publish_place_time_forecast(
            correlation_id="live:kis_live:abc",
            symbol="005930",
            instrument_type="equity_kr",
            side="buy",
            target_price=None,
            min_hold_days=None,
            session_label="kis_live_place",
            created_by="auto_place_live",
        )
        is None
    )
    assert called is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/test_live_place_provenance.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.live_place_provenance'`

- [ ] **Step 3a: Create `app/services/live_place_provenance.py`**

```python
"""Place-time forecast auto-publish for LIVE orders (ROB-714).

Mirrors the paper path (paper_limit_order_service.py:335-373): a live BUY with a
profit target auto-publishes a minimal price_target forecast keyed by
correlation_id, so /insights forecast<->retrospective join is not "structurally
dead". Buy+target only (can't fabricate a target); sells and target-less buys
carry only the correlation_id spine. Best-effort in an ISOLATED session --
a forecast hiccup must never roll back the order. Fixed defaults (D1):
probability=0.5, review_date = trade-day + N calendar days (N=min_hold_days
or 10) -- calendar-day offset keeps the order hot path network-free (ROB-671).
"""

from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal

from app.core.db import AsyncSessionLocal
from app.core.timezone import now_kst
from app.services.trade_journal.forecast_service import save_forecast

logger = logging.getLogger(__name__)

_DEFAULT_HORIZON_DAYS = 10


async def publish_place_time_forecast(
    *,
    correlation_id: str,
    symbol: str,
    instrument_type: str,
    side: str,
    target_price: float | None,
    min_hold_days: int | None,
    session_label: str,
    created_by: str,
    report_item_uuid: str | None = None,
) -> str | None:
    if side.lower() != "buy" or target_price is None:
        return None
    horizon_days = min_hold_days if (min_hold_days and min_hold_days > 0) else _DEFAULT_HORIZON_DAYS
    review_date = (now_kst().date() + timedelta(days=horizon_days)).isoformat()
    try:
        async with AsyncSessionLocal() as fdb:
            _action, fc = await save_forecast(
                fdb,
                created_by=created_by,
                symbol=symbol,
                instrument_type=instrument_type,
                forecast_target={
                    "kind": "price_target",
                    "direction": "at_or_above",
                    "target_price": float(Decimal(str(target_price))),
                },
                probability=0.5,
                review_date=review_date,
                correlation_id=correlation_id,
                horizon=f"P{horizon_days}D",
                model_label=None,
                session_label=session_label,
                report_item_uuid=report_item_uuid,
            )
            fid = getattr(fc, "forecast_id", None)
            await fdb.commit()
        return str(fid) if fid is not None else None
    except Exception:
        logger.exception(
            "live place: failed to publish place-time forecast for correlation_id=%s",
            correlation_id,
        )
        return None
```

> **검증 포인트:** `AsyncSessionLocal`은 `app.core.db`에서 import(확인됨 — `paper_limit_order_service.py:34`와 동일 출처). `save_forecast`가 `probability=0.5`를 range check로 거부하지 않는지 확인(0.5는 [0,1] 중앙, 안전).

- [ ] **Step 3b: `_record_kis_live_order`에 mint + publish 배선 (kis_live_ledger.py)**

파일 상단 import에 추가:

```python
from app.core.timezone import now_kst  # 이미 import되어 있으면 생략
from app.services.live_correlation import live_correlation_id
from app.services.live_place_provenance import publish_place_time_forecast
```

`_record_kis_live_order` 안에서 `_save_kis_live_order_ledger(...)` 호출 **직전**(line 275 앞)에 corr_id mint:

```python
    correlation_id = live_correlation_id(
        account_scope="kis_live",
        symbol=normalized_symbol,
        side=side,
        price=Decimal(str(price_val)),
        quantity=Decimal(str(qty_val)),
        kst_trade_day=now.strftime("%Y-%m-%d"),
        rung=0,
    )
```

(`now`는 함수 상단 247에서 이미 `now_kst()`로 잡혀 있음. `Decimal` import 여부 확인 — 없으면 `from decimal import Decimal` 추가.)

`_save_kis_live_order_ledger(...)` 호출 kwargs에 추가:

```python
        correlation_id=correlation_id,
```

`_save_*` 호출 **직후, `return {...}` 전**에 forecast 발행(accepted일 때만):

```python
    if status == "accepted":
        await publish_place_time_forecast(
            correlation_id=correlation_id,
            symbol=normalized_symbol,
            instrument_type=market_type,
            side=side,
            target_price=target_price,
            min_hold_days=min_hold_days,
            session_label="kis_live_place",
            created_by="auto_place_live",
            report_item_uuid=str(report_item_uuid) if report_item_uuid else None,
        )
```

반환 dict에 `"correlation_id": correlation_id,`를 추가(감사 편의).

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/services/test_live_place_provenance.py tests/mcp_server/tooling/test_live_ledger_correlation_id.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/services/live_place_provenance.py app/mcp_server/tooling/kis_live_ledger.py tests/services/test_live_place_provenance.py
git commit -m "feat(ROB-714): shared place-time forecast helper + wire KIS KR live send"
```

---

## Task 5: US/crypto live send 배선 (`_record_live_order`)

**Files:**
- Modify: `app/mcp_server/tooling/live_order_ledger.py` (`_record_live_order:461-535`)
- Test: `tests/mcp_server/tooling/test_live_ledger_correlation_id.py` (US 경로 forecast 호출 스파이 추가)

**Interfaces:**
- Consumes: Task 3 `_save_live_order_ledger(correlation_id=...)`, Task 4 `live_correlation_id`, `publish_place_time_forecast`.
- Produces: `_record_live_order`가 `market`("us"|"crypto")를 instrument_type("equity_us"|"crypto")로 매핑해 forecast 발행.

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.integration
async def test_record_live_order_us_buy_mints_corr_and_publishes(monkeypatch):
    from app.mcp_server.tooling import live_order_ledger as mod

    seen = {}

    async def spy_publish(**kwargs):
        seen.update(kwargs)
        return "fc-us-1"

    monkeypatch.setattr(mod, "publish_place_time_forecast", spy_publish)

    res = await mod._record_live_order(
        broker="kis",
        account_scope="kis_live",
        market="us",
        normalized_symbol="AAPL",
        exchange="NASD",
        market_symbol=None,
        side="buy",
        order_kind="limit",
        currency="USD",
        order_no="US-CORR-1",
        order_time=None,
        rt_cd="0",
        response_message=None,
        dry_run_result={"price": 190.0, "quantity": 2, "estimated_value": 380.0},
        execution_result={"rt_cd": "0"},
        reason=None,
        exit_reason=None,
        thesis="t",
        strategy=None,
        target_price=210.0,
        stop_loss=None,
        min_hold_days=None,
        notes=None,
        indicators_snapshot=None,
    )
    assert res["correlation_id"].startswith("live:kis_live:")
    assert seen["instrument_type"] == "equity_us"
    assert seen["side"] == "buy"
    assert seen["target_price"] == 210.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/mcp_server/tooling/test_live_ledger_correlation_id.py::test_record_live_order_us_buy_mints_corr_and_publishes -v`
Expected: FAIL — `KeyError: 'correlation_id'` on `res` (아직 배선 전).

- [ ] **Step 3: `_record_live_order` 배선 (live_order_ledger.py)**

파일 상단 import 추가:

```python
from decimal import Decimal  # 이미 있으면 생략
from app.core.timezone import now_kst  # 이미 있으면 생략
from app.services.live_correlation import live_correlation_id
from app.services.live_place_provenance import publish_place_time_forecast

_LIVE_MARKET_TO_INSTRUMENT = {"us": "equity_us", "crypto": "crypto"}
```

`_save_live_order_ledger(...)` 호출 **직전**(line 501 앞), `price_val`/`qty_val` 계산 이후에 mint:

```python
    correlation_id = live_correlation_id(
        account_scope=account_scope,
        symbol=normalized_symbol,
        side=side,
        price=Decimal(str(price_val)),
        quantity=Decimal(str(qty_val)),
        kst_trade_day=now_kst().strftime("%Y-%m-%d"),
        rung=0,
    )
```

`_save_live_order_ledger(...)` kwargs에 `correlation_id=correlation_id,` 추가.

**inline_confirm reconcile 전에** forecast 발행(accepted일 때):

```python
    if status == "accepted":
        await publish_place_time_forecast(
            correlation_id=correlation_id,
            symbol=normalized_symbol,
            instrument_type=_LIVE_MARKET_TO_INSTRUMENT.get(market, market),
            side=side,
            target_price=target_price,
            min_hold_days=min_hold_days,
            session_label="live_place",
            created_by="auto_place_live",
            report_item_uuid=str(report_item_uuid) if report_item_uuid else None,
        )
```

반환 dict에 `"correlation_id": correlation_id,` 추가.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/mcp_server/tooling/test_live_ledger_correlation_id.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/mcp_server/tooling/live_order_ledger.py tests/mcp_server/tooling/test_live_ledger_correlation_id.py
git commit -m "feat(ROB-714): wire correlation_id + place-time forecast into US/crypto live send"
```

---

## Task 6: Toss live send 배선 (`record_toss_place_order`)

**Files:**
- Modify: `app/mcp_server/tooling/toss_live_ledger.py` (`record_toss_place_order:62-126`)
- Test: `tests/mcp_server/tooling/test_live_ledger_correlation_id.py` (Toss 경로 스파이)

**Interfaces:**
- Consumes: Task 3 `record_send(correlation_id=...)`, Task 4 helpers.
- Produces: `record_toss_place_order`가 `market`("kr"|"us")를 instrument_type("equity_kr"|"equity_us")로 매핑해 forecast 발행. Toss `target_price`는 `Decimal | None` → `float()` 변환 후 전달.

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.integration
async def test_record_toss_place_kr_buy_mints_and_publishes(monkeypatch):
    from decimal import Decimal as D

    from app.mcp_server.tooling import toss_live_ledger as mod

    seen = {}

    async def spy_publish(**kwargs):
        seen.update(kwargs)
        return "fc-toss-1"

    async def fake_record_send(self, **kwargs):
        class _Row:
            id = 1
            status = "accepted"

        fake_record_send.kwargs = kwargs
        return _Row()

    monkeypatch.setattr(mod, "publish_place_time_forecast", spy_publish)
    monkeypatch.setattr(
        "app.services.toss_live_order_ledger_service."
        "TossLiveOrderLedgerService.record_send",
        fake_record_send,
    )

    res = await mod.record_toss_place_order(
        market="kr",
        symbol="005930",
        side="buy",
        order_type="limit",
        time_in_force="day",
        quantity=D("1"),
        price=D("70000"),
        order_amount=None,
        currency="KRW",
        client_order_id="cid-1",
        broker_order_id="bord-1",
        raw_response={},
        reason=None,
        exit_reason=None,
        thesis="t",
        strategy=None,
        target_price=D("80000"),
        stop_loss=None,
        min_hold_days=None,
        notes=None,
        indicators_snapshot=None,
        report_item_uuid=None,
    )
    assert res["correlation_id"].startswith("live:toss_live:")
    assert fake_record_send.kwargs["correlation_id"] == res["correlation_id"]
    assert seen["instrument_type"] == "equity_kr"
    assert seen["target_price"] == 80000.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/mcp_server/tooling/test_live_ledger_correlation_id.py::test_record_toss_place_kr_buy_mints_and_publishes -v`
Expected: FAIL — `KeyError: 'correlation_id'` on `res`.

- [ ] **Step 3: `record_toss_place_order` 배선 (toss_live_ledger.py)**

파일 상단 import 추가:

```python
from app.core.timezone import now_kst  # 이미 있으면 생략
from app.services.live_correlation import live_correlation_id
from app.services.live_place_provenance import publish_place_time_forecast

_TOSS_MARKET_TO_INSTRUMENT = {"kr": "equity_kr", "us": "equity_us"}
```

`status = "accepted" if broker_order_id else "rejected"`(line 88) **다음**에 mint:

```python
    correlation_id = live_correlation_id(
        account_scope="toss_live",
        symbol=symbol,
        side=side,
        price=(price if price is not None else Decimal("0")),
        quantity=(quantity if quantity is not None else Decimal("0")),
        kst_trade_day=now_kst().strftime("%Y-%m-%d"),
        rung=0,
    )
```

(`Decimal`은 이 파일에서 이미 import됨 — 시그니처가 `Decimal | None` 사용.)

`record_send(...)` 호출 kwargs에 `correlation_id=correlation_id,` 추가.

`async with` 블록 종료 후 forecast 발행(accepted일 때):

```python
    if status == "accepted":
        await publish_place_time_forecast(
            correlation_id=correlation_id,
            symbol=symbol,
            instrument_type=_TOSS_MARKET_TO_INSTRUMENT.get(market, market),
            side=side,
            target_price=float(target_price) if target_price is not None else None,
            min_hold_days=min_hold_days,
            session_label="toss_live_place",
            created_by="auto_place_live",
            report_item_uuid=report_item_uuid,
        )
```

반환 dict에 `"correlation_id": correlation_id,` 추가.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/mcp_server/tooling/test_live_ledger_correlation_id.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/mcp_server/tooling/toss_live_ledger.py tests/mcp_server/tooling/test_live_ledger_correlation_id.py
git commit -m "feat(ROB-714): wire correlation_id + place-time forecast into Toss live send"
```

---

## Task 7: reconcile 시 journal에 correlation_id backfill (3 경로)

**Files:**
- Modify: `app/mcp_server/tooling/kis_live_ledger.py:703` (`_reconcile_one_ledger_row`의 `_create_trade_journal_for_buy`)
- Modify: `app/mcp_server/tooling/live_order_ledger.py` (`_reconcile_one_live_row`의 buy journal 생성)
- Modify: `app/mcp_server/tooling/toss_live_ledger.py` (`_reconcile_one_toss_row`의 buy journal 생성)
- Test: `tests/mcp_server/tooling/test_live_ledger_correlation_id.py` (reconcile buy journal이 corr_id 인용 — 인자 스파이)

**Interfaces:**
- Consumes: `_create_trade_journal_for_buy(..., correlation_id=...)` (order_journal.py에 이미 존재하는 파라미터), reconcile row의 `row.correlation_id`(Task 2/3에서 채워짐).
- Produces: reconcile가 buy 저널을 만들 때 `correlation_id=row.correlation_id`를 전달 → 저널이 스파인에 연결.

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.integration
async def test_reconcile_buy_journal_receives_correlation_id(monkeypatch):
    from app.mcp_server.tooling import kis_live_ledger as mod

    captured = {}

    async def spy_create_journal(**kwargs):
        captured.update(kwargs)
        return {"journal_id": 42}

    monkeypatch.setattr(mod, "_create_trade_journal_for_buy", spy_create_journal)
    monkeypatch.setattr(
        mod, "_link_journal_to_fill", _noop_async := (lambda *a, **k: _acoro())
    )
    # ... build a minimal accepted buy KISLiveOrderLedger row with
    # correlation_id="live:kis_live:reconcileX", filled evidence, then call
    # _reconcile_one_ledger_row(row, dry_run=False) with broker daily rows
    # stubbed so it books a fill. (Reuse existing kis_live reconcile fixtures.)
    ...
    assert captured["correlation_id"] == "live:kis_live:reconcileX"
```

> **구현 노트:** 이 테스트는 기존 kis_live reconcile 테스트(`tests/.../test_kis_live_ledger*.py`)의 fill-evidence fixture를 재사용해 완성한다. 핵심 단언은 `_create_trade_journal_for_buy`가 `correlation_id=row.correlation_id`로 호출된다는 것. fixture 뼈대가 없으면 먼저 `_create_trade_journal_for_buy`만 스파이하고 최소 row + `_fetch_live_daily_rows`를 monkeypatch해 booked 경로로 진입시킨다.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/mcp_server/tooling/test_live_ledger_correlation_id.py::test_reconcile_buy_journal_receives_correlation_id -v`
Expected: FAIL — `KeyError: 'correlation_id'`(아직 인자 미전달) 또는 `assert None == "live:kis_live:reconcileX"`.

- [ ] **Step 3a: kis_live_ledger.py:703**

`_create_trade_journal_for_buy(...)` 호출 kwargs(`account="kis",` 다음, line 717 부근)에 추가:

```python
            correlation_id=row.correlation_id,
```

- [ ] **Step 3b: live_order_ledger.py `_reconcile_one_live_row`**

동일 buy journal 생성부(`_create_trade_journal_for_buy(...)`)에 `correlation_id=row.correlation_id,` 추가. (해당 호출 위치는 `_reconcile_one_live_row:224-417` 내부 buy 분기 — grep `_create_trade_journal_for_buy`로 확정.)

- [ ] **Step 3c: toss_live_ledger.py `_reconcile_one_toss_row`**

동일하게 buy journal 생성부에 `correlation_id=row.correlation_id,` 추가. (`_reconcile_one_toss_row`의 `_create_trade_journal_for_buy(...)` 호출 — line 337, `_close_journals_on_sell`/`_link_journal_to_fill`와 함께 이미 import됨(15-18). KIS/US 경로와 동일 패턴 확정됨.)

- [ ] **Step 4: Run test + 회귀(reconcile 스위트)**

Run: `uv run pytest tests/mcp_server/tooling/test_live_ledger_correlation_id.py -v`
Expected: PASS

Run: `uv run pytest tests/mcp_server/tooling/ -k "reconcile" -v`
Expected: 기존 reconcile 테스트 전부 PASS(회귀 없음).

- [ ] **Step 5: Commit**

```bash
git add app/mcp_server/tooling/kis_live_ledger.py app/mcp_server/tooling/live_order_ledger.py app/mcp_server/tooling/toss_live_ledger.py tests/mcp_server/tooling/test_live_ledger_correlation_id.py
git commit -m "feat(ROB-714): backfill correlation_id into reconcile-time buy journals (3 paths)"
```

---

## Task 8: 전체 스위트 + 정적 가드 + lint

**Files:** 없음(검증 전용).

- [ ] **Step 1: LLM-import 정적 가드**

Run: `uv run pytest tests/services/action_report/snapshot_backed/test_no_internal_llm_imports.py -v`
Expected: PASS(신규 2파일 모두 LLM provider import 없음).

- [ ] **Step 2: 관련 스위트 전체**

Run: `uv run pytest tests/services/test_live_correlation.py tests/services/test_live_place_provenance.py tests/mcp_server/tooling/test_live_ledger_correlation_id.py -v`
Expected: 전부 PASS.

- [ ] **Step 3: lint + typecheck**

Run: `make lint`
Expected: Ruff + ty 통과(신규 import 정렬·미사용 없음).

- [ ] **Step 4: 최종 마이그레이션 왕복 확인**

Run: `uv run alembic upgrade head && uv run alembic current`
Expected: 신규 리비전이 head.

- [ ] **Step 5: Commit(필요 시 lint 수정만)**

```bash
git add -A
git commit -m "chore(ROB-714): lint + static-guard pass" || echo "nothing to commit"
```

---

## Self-Review (완료)

**Spec coverage:**
- migration ×3 → Task 2 ✅
- send 시 correlation_id mint + 최소 forecast 자동 발행 → Task 4/5/6 ✅
- journal 링크는 reconcile 시 backfill → Task 7 ✅
- DB write only, 브로커 콜 없음 → 헬퍼는 격리 DB 세션만, review_date는 캘린더-일(무네트워크) ✅ (Global Constraints)
- 수동 forecast_save는 풍부화 용도로 유지 → 자동 발행은 `created_by="auto_place_live"`로 별도, 수동 경로 미변경 ✅
- /insights join 승격 → **의도적 스코프 밖**(D3, 후속) ✅
- 자동 forecast default horizon/probability 결정 → D1(0.5 / N=min_hold_days or 10 calendar days) ✅

**Placeholder scan:** Task 7 Step 1 테스트는 기존 fixture 재사용이 필요해 `...`로 뼈대만 두었고 구현 노트로 완성 방법을 명시(코드 전량 대신 fixture 의존이 불가피한 유일 지점). 나머지 스텝은 실제 코드 전량 포함.

**Type consistency:** `live_correlation_id(account_scope,symbol,side,price:Decimal,quantity:Decimal,kst_trade_day,rung)` / `publish_place_time_forecast(correlation_id,symbol,instrument_type,side,target_price:float|None,min_hold_days,session_label,created_by,report_item_uuid)` — Task 4 정의와 Task 5/6 호출 인자명 일치 확인 ✅. `_save_*`/`record_send`의 `correlation_id: str | None = None` 시그니처 Task 3↔4/5/6 일치 ✅.

## Open Questions / Known edges

- **rung=0 고정**: 같은 거래일 진짜 동일한 두 번째 live 주문(동일 symbol/side/price/qty)은 corr_id가 충돌한다. paper 패턴과 동일한 알려진 엣지 — live 주문은 order_no로 구분되고 corr_id는 결정-스파인이라 MVP에서 허용. 실측 후 필요 시 rung/ladder_level 배선(후속).
- **`save_forecast`의 review_date 요구**: 시그니처가 `review_date: str | date`(확인됨). ISO str로 전달하므로 안전.
- **해결됨**: Toss reconcile은 KIS/US와 동일하게 `_create_trade_journal_for_buy`(line 337) 사용 — Task 7 3c 배선 확정. `AsyncSessionLocal`은 `app.core.db` 출처(확인됨).

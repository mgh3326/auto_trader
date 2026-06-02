# ROB-405 Slice A — mock roundtrip → trade_journal 자동 마감 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** reconciled kis_mock roundtrip(KISMockOrderLedger, correlation_id 페어링)으로부터 `trade_journal`을 자동 생성(active)·마감(closed, pnl)하는 default-off 독립 브리지를 추가한다 — ROB-402 watch→order를 회고 데이터로 잇는 척추.

**Architecture:** 독립 브리지 `sync_mock_roundtrip_journals(db)`가 `lifecycle_state∈{fill,reconciled}` + `correlation_id` 있는 mock ledger 행을 correlation_id로 그룹핑 → entry(buy)면 active journal 생성, exit(sell)면 마감(exit_price/pnl_pct). 멱등=trade_journal.correlation_id 조회. mock 전용(account_type='mock'); live journal 경로 무변경. default-off flag + paused taskiq + CLI.

**Tech Stack:** Python 3.13, SQLAlchemy async, Postgres, alembic, taskiq, pytest/pytest-asyncio.

**의존**: ROB-402(correlation_id, merged)·404(reconcile, merged) — origin/main 기준.

**Spec:** `docs/superpowers/specs/2026-06-02-rob-405-sliceA-mock-roundtrip-journal-design.md`

---

## File Structure

| 파일 | 역할 | 변경 |
|---|---|---|
| `app/core/config.py` | 게이트 | `MOCK_ROUNDTRIP_JOURNAL_BRIDGE_ENABLED=False` |
| `app/models/trade_journal.py` | 컬럼/CHECK | `correlation_id` + index, account_type CHECK 'mock' |
| `alembic/versions/<rev>_rob405a_*.py` | 마이그레이션 | 컬럼 + CHECK |
| `tests/conftest.py` | DDL drift | trade_journals correlation_id 컬럼 + account_type CHECK 패치 |
| `app/services/trade_journal/mock_roundtrip_journal_bridge.py` | 브리지 | `sync_mock_roundtrip_journals` |
| `app/tasks/mock_roundtrip_journal_tasks.py` | paused task | env-gated |
| `scripts/sync_mock_roundtrip_journals.py` | operator CLI | preflight/run |
| `tests/test_mock_roundtrip_journal_bridge.py` | 단위 | 신규 |
| `tests/test_trade_journal_model.py` | 모델 | account_type 'mock' + correlation_id |

---

## Task 1: 모델 컬럼 + account_type CHECK + config + 마이그레이션 + conftest

**Files:**
- Modify: `app/models/trade_journal.py` (CHECK + correlation_id 컬럼), `app/core/config.py:469`
- Modify: `tests/conftest.py`
- Create: `alembic/versions/<rev>_rob405a_journal_mock_roundtrip.py`
- Test: `tests/test_trade_journal_model.py`

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_trade_journal_model.py` 끝에 추가:

```python
@pytest.mark.asyncio
async def test_journal_accepts_mock_account_type_and_correlation_id(db_session):
    from app.models.trade_journal import TradeJournal

    j = TradeJournal(
        symbol="005930",
        instrument_type="equity_kr",
        side="buy",
        entry_price=Decimal("55000"),
        quantity=Decimal("10"),
        thesis="auto roundtrip",
        account_type="mock",
        account="kis_mock",
        correlation_id="corr-405a",
        status="active",
    )
    db_session.add(j)
    await db_session.commit()
    fetched = await db_session.get(TradeJournal, j.id)
    assert fetched.account_type == "mock"
    assert fetched.correlation_id == "corr-405a"


def test_bridge_flag_default_false():
    from app.core.config import settings

    assert settings.MOCK_ROUNDTRIP_JOURNAL_BRIDGE_ENABLED is False
```

> 파일 상단에 `from decimal import Decimal` import가 없으면 추가.

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_trade_journal_model.py -k "mock_account_type or bridge_flag" -v`
Expected: FAIL — CHECK 위반(account_type='mock') / `TypeError` (correlation_id 컬럼 없음) / `AttributeError`(flag).

- [ ] **Step 3-a: config 플래그**

`app/core/config.py` — `KIS_MOCK_RECONCILE_PERIODIC_ENABLED`(469행 근처) 다음에:

```python
    # ROB-405 Slice A — mock roundtrip → trade_journal bridge. Default off:
    # no journals are created until an operator flips this.
    MOCK_ROUNDTRIP_JOURNAL_BRIDGE_ENABLED: bool = False
```

- [ ] **Step 3-b: 모델 — account_type CHECK + correlation_id 컬럼**

`app/models/trade_journal.py` — `trade_journals_account_type` CheckConstraint 교체:

```python
        CheckConstraint(
            "account_type IN ('live','paper','mock')",
            name="trade_journals_account_type",
        ),
```

`__table_args__`의 Index 목록에 추가:

```python
        Index("ix_trade_journals_correlation_id", "correlation_id"),
```

컬럼 정의부(예: `notes` 근처)에 추가:

```python
    correlation_id: Mapped[str | None] = mapped_column(Text)
```

- [ ] **Step 3-c: conftest DDL drift 패치**

`tests/conftest.py` — db_session 블록의 ROB-402/404 패치 근처에 추가:

```python
                # ROB-405 Slice A — trade_journals: correlation_id + account_type 'mock'.
                await conn.execute(
                    text(
                        "ALTER TABLE review.trade_journals "
                        "ADD COLUMN IF NOT EXISTS correlation_id TEXT"
                    )
                )
                await conn.execute(
                    text(
                        "ALTER TABLE review.trade_journals "
                        "DROP CONSTRAINT IF EXISTS trade_journals_account_type"
                    )
                )
                await conn.execute(
                    text(
                        "ALTER TABLE review.trade_journals "
                        "ADD CONSTRAINT trade_journals_account_type "
                        "CHECK (account_type IN ('live','paper','mock'))"
                    )
                )
```

- [ ] **Step 3-d: alembic 마이그레이션**

```bash
uv run alembic heads
uv run alembic revision -m "rob405a journal mock roundtrip"
```

생성 파일 본문(`down_revision`=현재 head):

```python
"""rob405a journal mock roundtrip

Revision ID: <자동생성>
Revises: <현재 head>
"""

import sqlalchemy as sa
from alembic import op

revision = "<자동생성>"
down_revision = "<현재 head>"
branch_labels = None
depends_on = None

_S = "review"
_T = "trade_journals"
_C = "trade_journals_account_type"


def upgrade() -> None:
    op.add_column(_T, sa.Column("correlation_id", sa.Text(), nullable=True), schema=_S)
    op.create_index(
        "ix_trade_journals_correlation_id", _T, ["correlation_id"], schema=_S
    )
    op.drop_constraint(_C, _T, schema=_S, type_="check")
    op.create_check_constraint(
        _C, _T, "account_type IN ('live','paper','mock')", schema=_S
    )


def downgrade() -> None:
    op.drop_constraint(_C, _T, schema=_S, type_="check")
    op.create_check_constraint(_C, _T, "account_type IN ('live','paper')", schema=_S)
    op.drop_index("ix_trade_journals_correlation_id", _T, schema=_S)
    op.drop_column(_T, "correlation_id", schema=_S)
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_trade_journal_model.py -v`
Expected: PASS (신규 + 기존).

- [ ] **Step 5: 커밋**

```bash
git add app/core/config.py app/models/trade_journal.py tests/conftest.py alembic/versions tests/test_trade_journal_model.py
git commit -m "feat(ROB-405): trade_journals correlation_id + account_type 'mock' + bridge flag"
```

---

## Task 2: 브리지 서비스 `sync_mock_roundtrip_journals`

**Files:**
- Create: `app/services/trade_journal/mock_roundtrip_journal_bridge.py`
- Test: `tests/test_mock_roundtrip_journal_bridge.py`

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_mock_roundtrip_journal_bridge.py` 생성:

```python
"""ROB-405 Slice A — mock roundtrip → trade_journal bridge."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.review import KISMockOrderLedger
from app.models.trade_journal import TradeJournal
from app.services.trade_journal.mock_roundtrip_journal_bridge import (
    sync_mock_roundtrip_journals,
)


async def _seed_leg(db, *, cid, side, role, price, lifecycle="reconciled", **over):
    row = KISMockOrderLedger(
        trade_date=datetime(2026, 6, 2, 9, 0, tzinfo=UTC),
        symbol=over.get("symbol", "005930"),
        instrument_type="equity_kr",
        side=side,
        order_type="limit",
        quantity=Decimal(over.get("quantity", "10")),
        price=Decimal(price),
        amount=Decimal("550000"),
        currency="KRW",
        order_no=f"MOCK-{uuid4()}",
        account_mode="kis_mock",
        broker="kis",
        status="accepted",
        lifecycle_state=lifecycle,
        correlation_id=cid,
        scalping_role=role,
        exit_reason=over.get("exit_reason"),
        thesis=over.get("thesis", "t"),
    )
    db.add(row)
    await db.commit()
    return row


async def _journal_for(db, cid):
    return (
        await db.execute(
            select(TradeJournal).where(TradeJournal.correlation_id == cid)
        )
    ).scalar_one_or_none()


@pytest.mark.asyncio
async def test_entry_creates_active_journal(db_session: AsyncSession):
    cid = f"corr-{uuid4().hex}"
    await _seed_leg(db_session, cid=cid, side="buy", role="entry", price="55000")
    out = await sync_mock_roundtrip_journals(db_session, force=True)
    assert out["created"] == 1
    j = await _journal_for(db_session, cid)
    assert j.status == "active"
    assert j.account_type == "mock"
    assert j.entry_price == Decimal("55000")


@pytest.mark.asyncio
async def test_exit_closes_with_pnl(db_session: AsyncSession):
    cid = f"corr-{uuid4().hex}"
    await _seed_leg(db_session, cid=cid, side="buy", role="entry", price="50000")
    await _seed_leg(
        db_session, cid=cid, side="sell", role="exit", price="55000",
        exit_reason="take_profit",
    )
    out = await sync_mock_roundtrip_journals(db_session, force=True)
    assert out["created"] == 1 and out["closed"] == 1
    j = await _journal_for(db_session, cid)
    assert j.status == "closed"
    assert j.exit_price == Decimal("55000")
    assert j.exit_reason == "take_profit"
    assert j.pnl_pct == Decimal("10.0000")  # (55000-50000)/50000*100


@pytest.mark.asyncio
async def test_idempotent(db_session: AsyncSession):
    cid = f"corr-{uuid4().hex}"
    await _seed_leg(db_session, cid=cid, side="buy", role="entry", price="50000")
    await _seed_leg(db_session, cid=cid, side="sell", role="exit", price="55000")
    await sync_mock_roundtrip_journals(db_session, force=True)
    out2 = await sync_mock_roundtrip_journals(db_session, force=True)
    assert out2["created"] == 0 and out2["closed"] == 0
    rows = (
        await db_session.execute(
            select(TradeJournal).where(TradeJournal.correlation_id == cid)
        )
    ).scalars().all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_ignores_rows_without_correlation_id(db_session: AsyncSession):
    row = KISMockOrderLedger(
        trade_date=datetime(2026, 6, 2, 9, 0, tzinfo=UTC),
        symbol="000660", instrument_type="equity_kr", side="buy", order_type="limit",
        quantity=Decimal("1"), price=Decimal("100"), amount=Decimal("100"),
        currency="KRW", order_no=f"MOCK-{uuid4()}", account_mode="kis_mock",
        broker="kis", status="accepted", lifecycle_state="reconciled",
        correlation_id=None, thesis="t",
    )
    db_session.add(row)
    await db_session.commit()
    out = await sync_mock_roundtrip_journals(db_session, force=True)
    assert out["created"] == 0


@pytest.mark.asyncio
async def test_flag_off_disables(db_session: AsyncSession, monkeypatch):
    from app.services.trade_journal import mock_roundtrip_journal_bridge as mod

    monkeypatch.setattr(
        mod.settings, "MOCK_ROUNDTRIP_JOURNAL_BRIDGE_ENABLED", False
    )
    cid = f"corr-{uuid4().hex}"
    await _seed_leg(db_session, cid=cid, side="buy", role="entry", price="50000")
    out = await sync_mock_roundtrip_journals(db_session)  # force defaults False
    assert out["status"] == "disabled"
    assert await _journal_for(db_session, cid) is None
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_mock_roundtrip_journal_bridge.py -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: 구현**

`app/services/trade_journal/mock_roundtrip_journal_bridge.py` 생성 (`app/services/trade_journal/__init__.py` 없으면 생성):

```python
"""ROB-405 Slice A — bridge reconciled kis_mock roundtrips into trade_journals.

Pairs KISMockOrderLedger rows by correlation_id (ROB-402 watch→order link),
creates an active journal on the entry (buy) leg and closes it on the exit
(sell) leg with pnl_pct. Idempotent via trade_journals.correlation_id. Writes
ONLY account_type='mock' journals; live journals are untouched. Default off.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from app.core.config import settings
from app.models.review import KISMockOrderLedger
from app.models.trade_journal import TradeJournal

logger = logging.getLogger(__name__)

_RECONCILED_STATES = ("fill", "reconciled")


async def sync_mock_roundtrip_journals(db, *, force: bool = False) -> dict[str, Any]:
    """Create/close trade_journals from reconciled kis_mock roundtrips."""
    if not force and not settings.MOCK_ROUNDTRIP_JOURNAL_BRIDGE_ENABLED:
        return {"status": "disabled", "created": 0, "closed": 0}

    rows = (
        await db.execute(
            select(KISMockOrderLedger)
            .where(
                KISMockOrderLedger.account_mode == "kis_mock",
                KISMockOrderLedger.correlation_id.is_not(None),
                KISMockOrderLedger.lifecycle_state.in_(_RECONCILED_STATES),
            )
            .order_by(
                KISMockOrderLedger.trade_date.asc(), KISMockOrderLedger.id.asc()
            )
        )
    ).scalars().all()

    groups: dict[str, list[KISMockOrderLedger]] = {}
    for r in rows:
        groups.setdefault(r.correlation_id, []).append(r)

    created = 0
    closed = 0
    for cid, legs in groups.items():
        journal = (
            await db.execute(
                select(TradeJournal).where(TradeJournal.correlation_id == cid)
            )
        ).scalar_one_or_none()

        entry = next(
            (leg for leg in legs if leg.scalping_role == "entry" or leg.side == "buy"),
            None,
        )
        exit_leg = next(
            (leg for leg in legs if leg.scalping_role == "exit" or leg.side == "sell"),
            None,
        )

        if entry is not None and journal is None:
            journal = TradeJournal(
                symbol=entry.symbol,
                instrument_type=entry.instrument_type,
                side="buy",
                entry_price=entry.price,
                quantity=entry.quantity,
                amount=entry.amount,
                thesis=entry.thesis or "auto: kis_mock roundtrip",
                strategy=entry.strategy,
                account_type="mock",
                account="kis_mock",
                correlation_id=cid,
                status="active",
            )
            db.add(journal)
            await db.flush()
            created += 1

        if exit_leg is not None and journal is not None and journal.status == "active":
            journal.exit_price = exit_leg.price
            journal.exit_date = datetime.now(tz=UTC)
            journal.exit_reason = exit_leg.exit_reason or "roundtrip_exit"
            entry_price = journal.entry_price
            if entry_price and entry_price > 0:
                journal.pnl_pct = (
                    (Decimal(exit_leg.price) - entry_price) / entry_price * 100
                )
            detail = dict(journal.extra_metadata or {})
            detail["roundtrip_net_pnl"] = (
                str(exit_leg.net_pnl) if exit_leg.net_pnl is not None else None
            )
            detail["roundtrip_gross_pnl"] = (
                str(exit_leg.gross_pnl) if exit_leg.gross_pnl is not None else None
            )
            journal.extra_metadata = detail
            journal.status = "closed"
            closed += 1

    await db.commit()
    return {"status": "ok", "created": created, "closed": closed}
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_mock_roundtrip_journal_bridge.py -v`
Expected: PASS (5건).

- [ ] **Step 5: 커밋**

```bash
git add app/services/trade_journal/ tests/test_mock_roundtrip_journal_bridge.py
git commit -m "feat(ROB-405): mock roundtrip → trade_journal bridge (idempotent, mock-only)"
```

---

## Task 3: paused taskiq task + operator CLI

**Files:**
- Create: `app/tasks/mock_roundtrip_journal_tasks.py`
- Create: `scripts/sync_mock_roundtrip_journals.py`
- Test: `tests/test_mock_roundtrip_journal_task.py`

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_mock_roundtrip_journal_task.py` 생성:

```python
"""ROB-405 Slice A — paused taskiq task gating."""

from __future__ import annotations

import pytest

import app.tasks.mock_roundtrip_journal_tasks as task_mod


@pytest.mark.asyncio
async def test_task_disabled_when_flag_off(monkeypatch):
    monkeypatch.setattr(
        task_mod.settings, "MOCK_ROUNDTRIP_JOURNAL_BRIDGE_ENABLED", False
    )
    called = {"n": 0}

    async def _fake_sync(db, **kw):
        called["n"] += 1
        return {"status": "ok", "created": 0, "closed": 0}

    monkeypatch.setattr(task_mod, "sync_mock_roundtrip_journals", _fake_sync)
    result = await task_mod.mock_roundtrip_journal_sync()
    assert result["status"] == "disabled"
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_task_runs_when_enabled(monkeypatch):
    monkeypatch.setattr(
        task_mod.settings, "MOCK_ROUNDTRIP_JOURNAL_BRIDGE_ENABLED", True
    )
    captured = {"n": 0}

    async def _fake_sync(db, **kw):
        captured["n"] += 1
        return {"status": "ok", "created": 2, "closed": 1}

    monkeypatch.setattr(task_mod, "sync_mock_roundtrip_journals", _fake_sync)

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(task_mod, "AsyncSessionLocal", lambda: _FakeSession())
    result = await task_mod.mock_roundtrip_journal_sync()
    assert result["created"] == 2
    assert captured["n"] == 1
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_mock_roundtrip_journal_task.py -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3-a: taskiq task**

`app/tasks/mock_roundtrip_journal_tasks.py` 생성:

```python
"""ROB-405 Slice A — paused taskiq task for the mock roundtrip journal bridge.
NO schedule: starts paused; operator flips MOCK_ROUNDTRIP_JOURNAL_BRIDGE_ENABLED
and adds a cron in a follow-up.
"""

from __future__ import annotations

import logging

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.core.taskiq_broker import broker
from app.services.trade_journal.mock_roundtrip_journal_bridge import (
    sync_mock_roundtrip_journals,
)

logger = logging.getLogger(__name__)


@broker.task(task_name="mock_roundtrip.journal_sync")  # no schedule → paused
async def mock_roundtrip_journal_sync() -> dict:
    if not settings.MOCK_ROUNDTRIP_JOURNAL_BRIDGE_ENABLED:
        return {"status": "disabled", "created": 0, "closed": 0}
    async with AsyncSessionLocal() as db:
        return await sync_mock_roundtrip_journals(db)
```

- [ ] **Step 3-b: operator CLI**

`scripts/sync_mock_roundtrip_journals.py` 생성:

```python
"""ROB-405 Slice A — operator CLI for the mock roundtrip journal bridge.
``preflight`` forces a run regardless of the env gate (read-mostly: only writes
account_type='mock' journals). ``run`` honors the gate.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="mock roundtrip → trade_journal bridge")
    p.add_argument("mode", choices=["preflight", "run"])
    return p


async def _amain(mode: str) -> int:
    from app.core.db import AsyncSessionLocal
    from app.services.trade_journal.mock_roundtrip_journal_bridge import (
        sync_mock_roundtrip_journals,
    )

    async with AsyncSessionLocal() as db:
        result = await sync_mock_roundtrip_journals(db, force=(mode == "preflight"))
    print(json.dumps(result, ensure_ascii=False))
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    args = _build_parser().parse_args()
    return asyncio.run(_amain(args.mode))


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: 통과 확인**

Run:
```bash
uv run pytest tests/test_mock_roundtrip_journal_task.py -v
uv run python scripts/sync_mock_roundtrip_journals.py --help
```
Expected: 테스트 PASS, `--help`는 secret 없이 usage 출력.

- [ ] **Step 5: 커밋**

```bash
git add app/tasks/mock_roundtrip_journal_tasks.py scripts/sync_mock_roundtrip_journals.py tests/test_mock_roundtrip_journal_task.py
git commit -m "feat(ROB-405): paused taskiq task + operator CLI for journal bridge"
```

---

## Task 4: 회귀 + lint/format/typecheck

**Files:** (검증만)

- [ ] **Step 1: 관련 스위트 회귀**

Run:
```bash
uv run pytest tests/test_trade_journal_model.py tests/test_mock_roundtrip_journal_bridge.py tests/test_mock_roundtrip_journal_task.py tests/services/test_trade_journal_write_service.py tests/services/test_trade_journal_read_service.py -p no:randomly -v
```
Expected: 전부 PASS (live journal write/read 회귀 포함 — mock 브리지가 live 경로 무손상).

- [ ] **Step 2: lint + format**

Run:
```bash
uv run ruff check app/ tests/ scripts/
uv run ruff format --check app/ tests/ scripts/
```
Expected: 통과(필요 시 `uv run ruff format app/ tests/ scripts/`).

- [ ] **Step 3: typecheck (변경 파일)**

Run:
```bash
uv run ty check app/services/trade_journal/mock_roundtrip_journal_bridge.py app/models/trade_journal.py app/tasks/mock_roundtrip_journal_tasks.py app/core/config.py
```
Expected: 통과.

- [ ] **Step 4: 커밋(필요 시 format)**

```bash
git add -A && git commit -m "style(ROB-405): ruff format" || echo "nothing to format"
```

---

## 검증 / 인수 기준
- reconciled mock roundtrip(correlation_id) → entry=active journal(account_type='mock'), exit=closed+pnl_pct.
- 멱등(재실행 중복 없음), correlation_id 없는 행 무시, flag off→disabled(journal 0).
- live journal(account_type live|paper) 경로 무변경. 마이그레이션 additive.

## 범위 밖 (후속 슬라이스)
- B verdict(trade_journal_reviews) / C counterfactual / D 사이클 read API / E follow_up_report_item_id.
- operator flip + live-mock smoke. 404 reconcile 완료 후 자동 호출 배선.

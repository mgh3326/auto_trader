# ROB-405 Slice C — counterfactual Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** closed mock roundtrip(correlation_id)별 trigger_price(watch threshold)/actual_fill(journal entry)/no_action_price(sync 시점 라이브 시세)와 deltas를 기록하는 신규 `trade_journal_counterfactuals` 테이블 + default-off sync 서비스를 추가한다.

**Architecture:** `sync_journal_counterfactuals(db, price_fn=get_price)`가 closed mock journal을 correlation_id로 InvestmentWatchEvent와 join(없으면 skip), `price_fn(symbol, market)`로 no_action 라이브 시세를 받아 deltas와 함께 insert(unique correlation_id 멱등, price_fn 실패 fail-open). default-off flag + paused taskiq + CLI.

**Tech Stack:** Python 3.13, SQLAlchemy async, Postgres, alembic, taskiq, pytest.

**의존**: Slice A(#1086)·B(#1089) merged. origin/main 기준.

**Spec:** `docs/superpowers/specs/2026-06-02-rob-405-sliceC-counterfactual-design.md`

---

## File Structure
| 파일 | 역할 | 변경 |
|---|---|---|
| `app/core/config.py` | 게이트 | `JOURNAL_COUNTERFACTUAL_ENABLED=False` |
| `app/models/review.py` | ORM | `TradeJournalCounterfactual` 테이블 |
| `alembic/versions/<rev>_rob405c_*.py` | 마이그레이션 | create_table |
| `app/services/trade_journal/journal_counterfactual_service.py` | 서비스 | `sync_journal_counterfactuals` |
| `app/tasks/journal_counterfactual_tasks.py` | paused task | env-gated |
| `scripts/sync_journal_counterfactuals.py` | operator CLI | force run |
| `tests/test_journal_counterfactual_service.py` / `tests/test_journal_counterfactual_task.py` | | 신규 |

---

## Task 1: 모델 `TradeJournalCounterfactual` + config + 마이그레이션

**Files:**
- Modify: `app/models/review.py` (ORM), `app/core/config.py:478`
- Create: `alembic/versions/<rev>_rob405c_journal_counterfactual.py`
- Test: `tests/test_journal_counterfactual_service.py` (모델 insert/unique)

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_journal_counterfactual_service.py` 생성:

```python
"""ROB-405 Slice C — journal counterfactual."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.review import TradeJournalCounterfactual
from app.models.trade_journal import TradeJournal


async def _closed_mock_journal(db, *, cid, entry="50000"):
    j = TradeJournal(
        symbol="005930",
        instrument_type="equity_kr",
        side="buy",
        entry_price=Decimal(entry),
        quantity=Decimal("10"),
        thesis="t",
        account_type="mock",
        account="kis_mock",
        correlation_id=cid,
        status="closed",
        exit_price=Decimal("55000"),
        exit_date=datetime(2026, 6, 2, tzinfo=UTC),
        pnl_pct=Decimal("10"),
    )
    db.add(j)
    await db.commit()
    return j


@pytest.mark.asyncio
async def test_counterfactual_inserts_and_unique(db_session: AsyncSession):
    cid = f"corr-{uuid4().hex}"
    j = await _closed_mock_journal(db_session, cid=cid)
    db_session.add(
        TradeJournalCounterfactual(
            journal_id=j.id, correlation_id=cid, symbol="005930", market="kr",
            trigger_price=Decimal("49000"), actual_fill_price=Decimal("50000"),
        )
    )
    await db_session.commit()
    # unique correlation_id
    db_session.add(
        TradeJournalCounterfactual(
            journal_id=j.id, correlation_id=cid, symbol="005930", market="kr",
            trigger_price=Decimal("49000"),
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_journal_counterfactual_service.py -k "inserts_and_unique" -v`
Expected: FAIL — `ImportError: cannot import name 'TradeJournalCounterfactual'`.

- [ ] **Step 3-a: config 플래그**

`app/core/config.py` — `JOURNAL_VERDICT_AUTO_ENABLED`(478행) 다음에:

```python
    # ROB-405 Slice C — journal counterfactual sync. Default off.
    JOURNAL_COUNTERFACTUAL_ENABLED: bool = False
```

- [ ] **Step 3-b: ORM 추가**

`app/models/review.py` — `TradeJournalReview`(Slice B) 클래스 다음에 추가:

```python
class TradeJournalCounterfactual(Base):
    """ROB-405 Slice C — trigger vs actual fill vs no-action price for a
    watch-driven mock roundtrip. Quantifies the rule's effect. One row per
    correlation_id (idempotent)."""

    __tablename__ = "trade_journal_counterfactuals"
    __table_args__ = (
        UniqueConstraint(
            "correlation_id", name="uq_trade_journal_counterfactuals_correlation_id"
        ),
        Index("ix_trade_journal_counterfactuals_journal_id", "journal_id"),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    journal_id: Mapped[int] = mapped_column(
        ForeignKey("review.trade_journals.id", ondelete="CASCADE"), nullable=False
    )
    correlation_id: Mapped[str] = mapped_column(Text, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    market: Mapped[str] = mapped_column(Text, nullable=False)
    trigger_price: Mapped[float] = mapped_column(Numeric(20, 8), nullable=False)
    triggered_value: Mapped[float | None] = mapped_column(Numeric(20, 8))
    actual_fill_price: Mapped[float | None] = mapped_column(Numeric(20, 8))
    no_action_price: Mapped[float | None] = mapped_column(Numeric(20, 8))
    no_action_as_of: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    fill_vs_trigger_pct: Mapped[float | None] = mapped_column(Numeric(10, 4))
    no_action_vs_fill_pct: Mapped[float | None] = mapped_column(Numeric(10, 4))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
```

> `UniqueConstraint`가 review.py import에 없으면 추가(`from sqlalchemy import ..., UniqueConstraint`). 나머지(BigInteger/Text/Numeric/TIMESTAMP/ForeignKey/Index/func/Mapped/mapped_column)는 존재.

- [ ] **Step 3-c: alembic 마이그레이션**

```bash
uv run alembic heads
uv run alembic revision -m "rob405c trade_journal_counterfactuals"
```

생성 파일 본문:

```python
"""rob405c trade_journal_counterfactuals

Revision ID: <자동생성>
Revises: <현재 head>
"""

import sqlalchemy as sa
from alembic import op

revision = "<자동생성>"
down_revision = "<현재 head>"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "trade_journal_counterfactuals",
        sa.Column("id", sa.BigInteger(), primary_key=True, nullable=False),
        sa.Column("journal_id", sa.BigInteger(), nullable=False),
        sa.Column("correlation_id", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("market", sa.Text(), nullable=False),
        sa.Column("trigger_price", sa.Numeric(20, 8), nullable=False),
        sa.Column("triggered_value", sa.Numeric(20, 8), nullable=True),
        sa.Column("actual_fill_price", sa.Numeric(20, 8), nullable=True),
        sa.Column("no_action_price", sa.Numeric(20, 8), nullable=True),
        sa.Column("no_action_as_of", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("fill_vs_trigger_pct", sa.Numeric(10, 4), nullable=True),
        sa.Column("no_action_vs_fill_pct", sa.Numeric(10, 4), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["journal_id"], ["review.trade_journals.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint(
            "correlation_id", name="uq_trade_journal_counterfactuals_correlation_id"
        ),
        schema="review",
    )
    op.create_index(
        "ix_trade_journal_counterfactuals_journal_id",
        "trade_journal_counterfactuals",
        ["journal_id"],
        schema="review",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_trade_journal_counterfactuals_journal_id",
        "trade_journal_counterfactuals",
        schema="review",
    )
    op.drop_table("trade_journal_counterfactuals", schema="review")
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_journal_counterfactual_service.py -k "inserts_and_unique" -v`
Expected: PASS.

- [ ] **Step 5: 커밋**

```bash
git add app/core/config.py app/models/review.py alembic/versions tests/test_journal_counterfactual_service.py
git commit -m "feat(ROB-405): trade_journal_counterfactuals table + flag"
```

---

## Task 2: sync 서비스 `sync_journal_counterfactuals`

**Files:**
- Create: `app/services/trade_journal/journal_counterfactual_service.py`
- Test: `tests/test_journal_counterfactual_service.py` (추가)

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_journal_counterfactual_service.py`에 추가. 상단 import + 헬퍼:

```python
import uuid as _uuid

from app.models.investment_reports import InvestmentWatchEvent
from app.services.trade_journal import journal_counterfactual_service as svc


async def _watch_event(db, *, cid, threshold="49000", current_value="49500"):
    ev = InvestmentWatchEvent(
        event_uuid=_uuid.uuid4(),
        idempotency_key=f"idem-{_uuid.uuid4()}",
        market="kr",
        target_kind="asset",
        symbol="005930",
        metric="price",
        operator="below",
        threshold=Decimal(threshold),
        threshold_key=str(threshold),
        intent="buy_review",
        action_mode="auto_execute_mock",
        current_value=Decimal(current_value),
        correlation_id=cid,
        kst_date="2026-06-02",
    )
    db.add(ev)
    await db.commit()
    return ev


async def _cfs_for(db, cid):
    return (
        await db.execute(
            select(TradeJournalCounterfactual).where(
                TradeJournalCounterfactual.correlation_id == cid
            )
        )
    ).scalars().all()


def _price_fn(value):
    async def _fn(symbol, market):
        return value

    return _fn


@pytest.mark.asyncio
async def test_sync_records_counterfactual(db_session, monkeypatch):
    monkeypatch.setattr(svc.settings, "JOURNAL_COUNTERFACTUAL_ENABLED", True)
    cid = f"corr-{_uuid.uuid4().hex}"
    j = await _closed_mock_journal(db_session, cid=cid, entry="50000")
    await _watch_event(db_session, cid=cid, threshold="49000", current_value="49500")
    out = await svc.sync_journal_counterfactuals(
        db_session, price_fn=_price_fn(52000.0)
    )
    assert out["created"] == 1
    row = (await _cfs_for(db_session, cid))[0]
    assert row.trigger_price == Decimal("49000")
    assert row.triggered_value == Decimal("49500")
    assert row.actual_fill_price == Decimal("50000")
    assert row.no_action_price == Decimal("52000")
    # (50000-49000)/49000*100 = 2.0408..., (52000-50000)/50000*100 = 4.0
    assert row.fill_vs_trigger_pct == Decimal("2.0408")
    assert row.no_action_vs_fill_pct == Decimal("4.0000")


@pytest.mark.asyncio
async def test_sync_skips_without_watch_event(db_session, monkeypatch):
    monkeypatch.setattr(svc.settings, "JOURNAL_COUNTERFACTUAL_ENABLED", True)
    cid = f"corr-{_uuid.uuid4().hex}"
    await _closed_mock_journal(db_session, cid=cid)
    out = await svc.sync_journal_counterfactuals(db_session, price_fn=_price_fn(1.0))
    assert out["created"] == 0
    assert await _cfs_for(db_session, cid) == []


@pytest.mark.asyncio
async def test_sync_idempotent(db_session, monkeypatch):
    monkeypatch.setattr(svc.settings, "JOURNAL_COUNTERFACTUAL_ENABLED", True)
    cid = f"corr-{_uuid.uuid4().hex}"
    await _closed_mock_journal(db_session, cid=cid)
    await _watch_event(db_session, cid=cid)
    await svc.sync_journal_counterfactuals(db_session, price_fn=_price_fn(52000.0))
    out2 = await svc.sync_journal_counterfactuals(db_session, price_fn=_price_fn(52000.0))
    assert out2["created"] == 0
    assert len(await _cfs_for(db_session, cid)) == 1


@pytest.mark.asyncio
async def test_sync_price_fn_none_fail_open(db_session, monkeypatch):
    monkeypatch.setattr(svc.settings, "JOURNAL_COUNTERFACTUAL_ENABLED", True)
    cid = f"corr-{_uuid.uuid4().hex}"
    await _closed_mock_journal(db_session, cid=cid, entry="50000")
    await _watch_event(db_session, cid=cid, threshold="49000")
    out = await svc.sync_journal_counterfactuals(db_session, price_fn=_price_fn(None))
    assert out["created"] == 1
    row = (await _cfs_for(db_session, cid))[0]
    assert row.no_action_price is None
    assert row.no_action_vs_fill_pct is None
    assert row.fill_vs_trigger_pct == Decimal("2.0408")


@pytest.mark.asyncio
async def test_flag_off_disables(db_session, monkeypatch):
    monkeypatch.setattr(svc.settings, "JOURNAL_COUNTERFACTUAL_ENABLED", False)
    cid = f"corr-{_uuid.uuid4().hex}"
    await _closed_mock_journal(db_session, cid=cid)
    await _watch_event(db_session, cid=cid)
    out = await svc.sync_journal_counterfactuals(db_session, price_fn=_price_fn(1.0))
    assert out["status"] == "disabled"
    assert await _cfs_for(db_session, cid) == []
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_journal_counterfactual_service.py -k "sync or flag_off" -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: 구현**

`app/services/trade_journal/journal_counterfactual_service.py` 생성:

```python
"""ROB-405 Slice C — counterfactual sync for watch-driven mock roundtrips.

For each closed account_type='mock' journal with a correlation_id that has a
matching InvestmentWatchEvent, records trigger/actual-fill/no-action prices and
deltas. no_action_price is a live quote at sync time (injectable). Idempotent
via unique correlation_id. price_fn failure is fail-open (null no_action).
Default off.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Awaitable, Callable

from sqlalchemy import select

from app.core.config import settings
from app.jobs.watch_market_data import get_price as _default_get_price
from app.models.investment_reports import InvestmentWatchEvent
from app.models.review import TradeJournalCounterfactual
from app.models.trade_journal import TradeJournal

logger = logging.getLogger(__name__)

PriceFn = Callable[[str, str], Awaitable[float | None]]


def _pct(numer: Decimal, denom: Decimal | None) -> Decimal | None:
    if denom is None or denom == 0:
        return None
    return (numer / denom * 100).quantize(Decimal("0.0001"))


async def sync_journal_counterfactuals(
    db, *, force: bool = False, price_fn: PriceFn = _default_get_price
) -> dict[str, Any]:
    """Record counterfactual rows for watch-driven closed mock roundtrips."""
    if not force and not settings.JOURNAL_COUNTERFACTUAL_ENABLED:
        return {"status": "disabled", "created": 0}

    journals = (
        await db.execute(
            select(TradeJournal).where(
                TradeJournal.status == "closed",
                TradeJournal.account_type == "mock",
                TradeJournal.correlation_id.is_not(None),
            )
        )
    ).scalars().all()

    created = 0
    for j in journals:
        existing = (
            await db.execute(
                select(TradeJournalCounterfactual.id).where(
                    TradeJournalCounterfactual.correlation_id == j.correlation_id
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            continue

        event = (
            await db.execute(
                select(InvestmentWatchEvent)
                .where(InvestmentWatchEvent.correlation_id == j.correlation_id)
                .order_by(InvestmentWatchEvent.created_at.asc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if event is None:
            continue  # not rule-driven → no counterfactual

        trigger_price = Decimal(str(event.threshold))
        fill = j.entry_price
        no_action: Decimal | None = None
        no_action_as_of: datetime | None = None
        try:
            raw = await price_fn(event.symbol, event.market)
            no_action_as_of = datetime.now(tz=UTC)
            no_action = Decimal(str(raw)) if raw is not None else None
        except Exception:  # noqa: BLE001 - one bad quote must not break the sync
            logger.warning(
                "counterfactual price_fn failed for %s/%s", event.symbol, event.market
            )

        fill_vs_trigger = _pct(fill - trigger_price, trigger_price) if fill else None
        no_action_vs_fill = (
            _pct(no_action - fill, fill) if (no_action is not None and fill) else None
        )

        db.add(
            TradeJournalCounterfactual(
                journal_id=j.id,
                correlation_id=j.correlation_id,
                symbol=event.symbol,
                market=event.market,
                trigger_price=trigger_price,
                triggered_value=event.current_value,
                actual_fill_price=fill,
                no_action_price=no_action,
                no_action_as_of=no_action_as_of,
                fill_vs_trigger_pct=fill_vs_trigger,
                no_action_vs_fill_pct=no_action_vs_fill,
            )
        )
        created += 1

    await db.commit()
    return {"status": "ok", "created": created}
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_journal_counterfactual_service.py -v`
Expected: PASS (전체).

- [ ] **Step 5: 커밋**

```bash
git add app/services/trade_journal/journal_counterfactual_service.py tests/test_journal_counterfactual_service.py
git commit -m "feat(ROB-405): counterfactual sync (trigger/fill/no-action + deltas)"
```

---

## Task 3: paused taskiq task + CLI

**Files:**
- Create: `app/tasks/journal_counterfactual_tasks.py`, `scripts/sync_journal_counterfactuals.py`
- Test: `tests/test_journal_counterfactual_task.py`

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_journal_counterfactual_task.py` 생성:

```python
"""ROB-405 Slice C — paused taskiq task gating."""

from __future__ import annotations

import pytest

import app.tasks.journal_counterfactual_tasks as task_mod


@pytest.mark.asyncio
async def test_disabled_when_flag_off(monkeypatch):
    monkeypatch.setattr(task_mod.settings, "JOURNAL_COUNTERFACTUAL_ENABLED", False)
    called = {"n": 0}

    async def _fake(db, **kw):
        called["n"] += 1
        return {"status": "ok", "created": 0}

    monkeypatch.setattr(task_mod, "sync_journal_counterfactuals", _fake)
    result = await task_mod.journal_counterfactual_sync()
    assert result["status"] == "disabled"
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_runs_when_enabled(monkeypatch):
    monkeypatch.setattr(task_mod.settings, "JOURNAL_COUNTERFACTUAL_ENABLED", True)
    captured = {"n": 0}

    async def _fake(db, **kw):
        captured["n"] += 1
        return {"status": "ok", "created": 1}

    monkeypatch.setattr(task_mod, "sync_journal_counterfactuals", _fake)

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(task_mod, "AsyncSessionLocal", lambda: _FakeSession())
    result = await task_mod.journal_counterfactual_sync()
    assert result["created"] == 1
    assert captured["n"] == 1
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_journal_counterfactual_task.py -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3-a: taskiq task**

`app/tasks/journal_counterfactual_tasks.py` 생성:

```python
"""ROB-405 Slice C — paused taskiq task for counterfactual sync.
NO schedule: paused; operator flips JOURNAL_COUNTERFACTUAL_ENABLED + adds cron.
"""

from __future__ import annotations

import logging

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.core.taskiq_broker import broker
from app.services.trade_journal.journal_counterfactual_service import (
    sync_journal_counterfactuals,
)

logger = logging.getLogger(__name__)


@broker.task(task_name="journal_counterfactual.sync")  # no schedule → paused
async def journal_counterfactual_sync() -> dict:
    if not settings.JOURNAL_COUNTERFACTUAL_ENABLED:
        return {"status": "disabled", "created": 0}
    async with AsyncSessionLocal() as db:
        return await sync_journal_counterfactuals(db)
```

- [ ] **Step 3-b: operator CLI**

`scripts/sync_journal_counterfactuals.py` 생성:

```python
"""ROB-405 Slice C — operator CLI for journal counterfactual sync.
``run`` forces a sync (fetches live no-action quotes per symbol).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="trade_journal counterfactual sync")
    p.add_argument("mode", choices=["run"])
    return p


async def _amain() -> int:
    from app.core.db import AsyncSessionLocal
    from app.services.trade_journal.journal_counterfactual_service import (
        sync_journal_counterfactuals,
    )

    async with AsyncSessionLocal() as db:
        result = await sync_journal_counterfactuals(db, force=True)
    print(json.dumps(result, ensure_ascii=False))
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    _build_parser().parse_args()
    return asyncio.run(_amain())


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: 통과 확인**

Run:
```bash
uv run pytest tests/test_journal_counterfactual_task.py -v
uv run python scripts/sync_journal_counterfactuals.py --help
```
Expected: 테스트 PASS, `--help`는 secret 없이 usage 출력.

- [ ] **Step 5: 커밋**

```bash
git add app/tasks/journal_counterfactual_tasks.py scripts/sync_journal_counterfactuals.py tests/test_journal_counterfactual_task.py
git commit -m "feat(ROB-405): paused taskiq task + CLI for counterfactual sync"
```

---

## Task 4: 회귀 + lint/format/typecheck

- [ ] **Step 1: 관련 스위트 회귀**

Run:
```bash
uv run pytest tests/test_journal_counterfactual_service.py tests/test_journal_counterfactual_task.py tests/test_journal_verdict_service.py tests/test_mock_roundtrip_journal_bridge.py tests/test_us_candles_sync.py::test_revision_graph_has_single_final_head -p no:randomly -v
```
Expected: 전부 PASS (Slice A/B 회귀 + single-head 포함).

- [ ] **Step 2: lint + format**

Run:
```bash
uv run ruff check app/ tests/
uv run ruff format --check app/ tests/ scripts/sync_journal_counterfactuals.py
```
Expected: 통과(필요 시 `uv run ruff format ...`).

- [ ] **Step 3: typecheck**

Run:
```bash
uv run ty check app/services/trade_journal/journal_counterfactual_service.py app/models/review.py app/tasks/journal_counterfactual_tasks.py
```
Expected: 통과.

- [ ] **Step 4: 커밋(필요 시 format)**

```bash
git add -A && git commit -m "style(ROB-405): ruff format" || echo "nothing to format"
```

---

## 검증 / 인수 기준
- closed mock + watch event → counterfactual row(trigger/triggered_value/fill/no_action + deltas), price_fn 라이브 시세.
- watch event 없으면 skip, 멱등(unique correlation_id), price_fn None/예외 fail-open(no_action null), flag off→disabled, non-closed·non-mock 무시.
- 신규 테이블 unique+FK CASCADE. Slice A/B 무변경.

## 범위 밖 (후속)
D 사이클 read API(verdict + counterfactual 집계) / E follow_up. forward 다중-horizon no_action / operator flip.

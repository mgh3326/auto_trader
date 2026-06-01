# ROB-405 Slice B — trade_journal verdict Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** closed mock trade_journal(Slice A)에 verdict(good/neutral/bad)를 pnl_pct 임계값으로 자동 기록하고 수동 override를 지원하는 신규 `trade_journal_reviews` 테이블 + default-off 서비스를 추가한다.

**Architecture:** 순수함수 `classify_journal_verdict(pnl_pct)` + 서비스 `sync_journal_verdicts(db)`(closed mock journal 스캔→auto verdict insert, partial-unique 멱등) + `record_manual_verdict`(반자동). Slice A 브리지와 독립. default-off flag + paused taskiq + CLI.

**Tech Stack:** Python 3.13, SQLAlchemy async, Postgres, alembic, taskiq, pytest.

**의존**: Slice A(#1086, merged) — trade_journals.account_type 'mock' + correlation_id + closed 상태. origin/main 기준.

**Spec:** `docs/superpowers/specs/2026-06-02-rob-405-sliceB-journal-verdict-design.md`

---

## File Structure
| 파일 | 역할 | 변경 |
|---|---|---|
| `app/core/config.py` | 게이트 | `JOURNAL_VERDICT_AUTO_ENABLED=False` |
| `app/models/review.py` | ORM | `TradeJournalReview` 테이블 |
| `alembic/versions/<rev>_rob405b_*.py` | 마이그레이션 | create_table + partial-unique |
| `app/services/trade_journal/journal_verdict_policy.py` | 순수정책 | `classify_journal_verdict` |
| `app/services/trade_journal/journal_verdict_service.py` | 서비스 | `sync_journal_verdicts`/`record_manual_verdict` |
| `app/tasks/journal_verdict_tasks.py` | paused task | env-gated |
| `scripts/sync_journal_verdicts.py` | operator CLI | sync / manual |
| `tests/test_journal_verdict_policy.py` / `tests/test_journal_verdict_service.py` / `tests/test_journal_verdict_task.py` | | 신규 |

---

## Task 1: 모델 `TradeJournalReview` + config + 마이그레이션

**Files:**
- Modify: `app/models/review.py` (ORM 추가), `app/core/config.py:476`
- Create: `alembic/versions/<rev>_rob405b_journal_verdict.py`
- Test: `tests/test_journal_verdict_service.py` (모델 insert/CHECK)

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_journal_verdict_service.py` 생성:

```python
"""ROB-405 Slice B — trade_journal verdict."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.review import TradeJournalReview
from app.models.trade_journal import TradeJournal


async def _closed_mock_journal(db, *, pnl_pct, cid=None):
    j = TradeJournal(
        symbol="005930",
        instrument_type="equity_kr",
        side="buy",
        entry_price=Decimal("50000"),
        quantity=Decimal("10"),
        thesis="t",
        account_type="mock",
        account="kis_mock",
        correlation_id=cid or f"corr-{uuid4().hex}",
        status="closed",
        exit_price=Decimal("55000"),
        exit_date=datetime(2026, 6, 2, tzinfo=UTC),
        pnl_pct=Decimal(pnl_pct),
    )
    db.add(j)
    await db.commit()
    return j


@pytest.mark.asyncio
async def test_journal_review_inserts_and_checks(db_session: AsyncSession):
    j = await _closed_mock_journal(db_session, pnl_pct="10")
    r = TradeJournalReview(
        journal_id=j.id, verdict="good", verdict_source="auto", pnl_pct=Decimal("10")
    )
    db_session.add(r)
    await db_session.commit()
    assert r.id is not None


@pytest.mark.asyncio
async def test_journal_review_verdict_check_rejects(db_session: AsyncSession):
    j = await _closed_mock_journal(db_session, pnl_pct="1")
    db_session.add(
        TradeJournalReview(journal_id=j.id, verdict="great", verdict_source="auto")
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_journal_verdict_service.py -k "review_inserts or verdict_check" -v`
Expected: FAIL — `ImportError: cannot import name 'TradeJournalReview'`.

- [ ] **Step 3-a: config 플래그**

`app/core/config.py` — `MOCK_ROUNDTRIP_JOURNAL_BRIDGE_ENABLED`(476행) 다음에:

```python
    # ROB-405 Slice B — auto journal verdict. Default off.
    JOURNAL_VERDICT_AUTO_ENABLED: bool = False
```

- [ ] **Step 3-b: ORM 추가**

`app/models/review.py` — `TradeReview` 클래스 다음에 추가:

```python
class TradeJournalReview(Base):
    """ROB-405 Slice B — verdict (good/neutral/bad) for a trade_journal.

    Separate from TradeReview (which FKs review.trades). auto verdicts come from
    pnl_pct thresholds on closed mock journals; manual verdicts are overrides.
    """

    __tablename__ = "trade_journal_reviews"
    __table_args__ = (
        CheckConstraint(
            "verdict IN ('good','neutral','bad')",
            name="ck_trade_journal_reviews_verdict",
        ),
        CheckConstraint(
            "verdict_source IN ('auto','manual')",
            name="ck_trade_journal_reviews_source",
        ),
        Index("ix_trade_journal_reviews_journal_id", "journal_id"),
        Index(
            "uq_trade_journal_reviews_auto",
            "journal_id",
            unique=True,
            postgresql_where=text("verdict_source = 'auto'"),
        ),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    journal_id: Mapped[int] = mapped_column(
        ForeignKey("review.trade_journals.id", ondelete="CASCADE"), nullable=False
    )
    verdict: Mapped[str] = mapped_column(Text, nullable=False)
    verdict_source: Mapped[str] = mapped_column(Text, nullable=False)
    pnl_pct: Mapped[float | None] = mapped_column(Numeric(8, 4))
    comment: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
```

> 상단 import에 `Numeric`, `BigInteger`, `Text`, `ForeignKey`, `Index`, `CheckConstraint`, `TIMESTAMP`, `func`, `text`, `Mapped`, `mapped_column`이 모두 이미 있으면 추가 불필요(review.py에 존재 확인됨).

- [ ] **Step 3-c: alembic 마이그레이션**

```bash
uv run alembic heads
uv run alembic revision -m "rob405b trade_journal_reviews"
```

생성 파일 본문:

```python
"""rob405b trade_journal_reviews

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
        "trade_journal_reviews",
        sa.Column("id", sa.BigInteger(), primary_key=True, nullable=False),
        sa.Column("journal_id", sa.BigInteger(), nullable=False),
        sa.Column("verdict", sa.Text(), nullable=False),
        sa.Column("verdict_source", sa.Text(), nullable=False),
        sa.Column("pnl_pct", sa.Numeric(8, 4), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["journal_id"], ["review.trade_journals.id"], ondelete="CASCADE"
        ),
        sa.CheckConstraint(
            "verdict IN ('good','neutral','bad')",
            name="ck_trade_journal_reviews_verdict",
        ),
        sa.CheckConstraint(
            "verdict_source IN ('auto','manual')",
            name="ck_trade_journal_reviews_source",
        ),
        schema="review",
    )
    op.create_index(
        "ix_trade_journal_reviews_journal_id",
        "trade_journal_reviews",
        ["journal_id"],
        schema="review",
    )
    op.create_index(
        "uq_trade_journal_reviews_auto",
        "trade_journal_reviews",
        ["journal_id"],
        unique=True,
        schema="review",
        postgresql_where=sa.text("verdict_source = 'auto'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_trade_journal_reviews_auto", "trade_journal_reviews", schema="review"
    )
    op.drop_index(
        "ix_trade_journal_reviews_journal_id", "trade_journal_reviews", schema="review"
    )
    op.drop_table("trade_journal_reviews", schema="review")
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_journal_verdict_service.py -k "review_inserts or verdict_check" -v`
Expected: PASS (신규 테이블 create_all로 생성됨).

- [ ] **Step 5: 커밋**

```bash
git add app/core/config.py app/models/review.py alembic/versions tests/test_journal_verdict_service.py
git commit -m "feat(ROB-405): trade_journal_reviews table + verdict auto flag"
```

---

## Task 2: 자동 verdict 정책 (순수함수)

**Files:**
- Create: `app/services/trade_journal/journal_verdict_policy.py`
- Test: `tests/test_journal_verdict_policy.py`

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_journal_verdict_policy.py` 생성:

```python
"""ROB-405 Slice B — verdict policy."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.trade_journal.journal_verdict_policy import (
    classify_journal_verdict,
)


@pytest.mark.parametrize(
    "pnl_pct,expected",
    [
        (Decimal("2.0"), "good"),
        (Decimal("1.0"), "good"),  # boundary inclusive
        (Decimal("0.5"), "neutral"),
        (Decimal("-0.5"), "neutral"),
        (Decimal("-1.0"), "bad"),  # boundary inclusive
        (Decimal("-2.0"), "bad"),
        (None, "neutral"),
    ],
)
def test_classify_journal_verdict(pnl_pct, expected):
    assert classify_journal_verdict(pnl_pct) == expected
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_journal_verdict_policy.py -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: 구현**

`app/services/trade_journal/journal_verdict_policy.py` 생성:

```python
"""ROB-405 Slice B — deterministic auto-verdict from pnl_pct."""

from __future__ import annotations

from decimal import Decimal

GOOD_PNL_PCT = Decimal("1.0")
BAD_PNL_PCT = Decimal("-1.0")


def classify_journal_verdict(pnl_pct: Decimal | None) -> str:
    """good if pnl_pct >= +1.0%, bad if <= -1.0%, else neutral (None → neutral)."""
    if pnl_pct is None:
        return "neutral"
    if pnl_pct >= GOOD_PNL_PCT:
        return "good"
    if pnl_pct <= BAD_PNL_PCT:
        return "bad"
    return "neutral"
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_journal_verdict_policy.py -v`
Expected: PASS.

- [ ] **Step 5: 커밋**

```bash
git add app/services/trade_journal/journal_verdict_policy.py tests/test_journal_verdict_policy.py
git commit -m "feat(ROB-405): journal verdict pnl_pct policy"
```

---

## Task 3: verdict 서비스 (sync + manual)

**Files:**
- Create: `app/services/trade_journal/journal_verdict_service.py`
- Test: `tests/test_journal_verdict_service.py` (추가)

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_journal_verdict_service.py`에 추가:

```python
from app.services.trade_journal import journal_verdict_service as svc


async def _reviews_for(db, journal_id):
    return (
        await db.execute(
            select(TradeJournalReview).where(
                TradeJournalReview.journal_id == journal_id
            )
        )
    ).scalars().all()


@pytest.mark.asyncio
async def test_sync_records_auto_verdict(db_session, monkeypatch):
    monkeypatch.setattr(svc.settings, "JOURNAL_VERDICT_AUTO_ENABLED", True)
    j = await _closed_mock_journal(db_session, pnl_pct="2.0")
    out = await svc.sync_journal_verdicts(db_session)
    assert out["created"] == 1
    rows = await _reviews_for(db_session, j.id)
    assert len(rows) == 1
    assert rows[0].verdict == "good"
    assert rows[0].verdict_source == "auto"


@pytest.mark.asyncio
async def test_sync_idempotent(db_session, monkeypatch):
    monkeypatch.setattr(svc.settings, "JOURNAL_VERDICT_AUTO_ENABLED", True)
    j = await _closed_mock_journal(db_session, pnl_pct="-2.0")
    await svc.sync_journal_verdicts(db_session)
    out2 = await svc.sync_journal_verdicts(db_session)
    assert out2["created"] == 0
    rows = await _reviews_for(db_session, j.id)
    assert len(rows) == 1
    assert rows[0].verdict == "bad"


@pytest.mark.asyncio
async def test_sync_ignores_non_closed_and_non_mock(db_session, monkeypatch):
    monkeypatch.setattr(svc.settings, "JOURNAL_VERDICT_AUTO_ENABLED", True)
    # active mock journal
    j_active = TradeJournal(
        symbol="A", instrument_type="equity_kr", side="buy",
        thesis="t", account_type="mock", account="kis_mock",
        correlation_id=f"c-{uuid4().hex}", status="active",
    )
    # closed live journal
    j_live = TradeJournal(
        symbol="B", instrument_type="equity_kr", side="buy",
        thesis="t", account_type="live", status="closed", pnl_pct=Decimal("5"),
    )
    db_session.add_all([j_active, j_live])
    await db_session.commit()
    out = await svc.sync_journal_verdicts(db_session)
    assert out["created"] == 0


@pytest.mark.asyncio
async def test_flag_off_disables(db_session, monkeypatch):
    monkeypatch.setattr(svc.settings, "JOURNAL_VERDICT_AUTO_ENABLED", False)
    j = await _closed_mock_journal(db_session, pnl_pct="2.0")
    out = await svc.sync_journal_verdicts(db_session)
    assert out["status"] == "disabled"
    assert await _reviews_for(db_session, j.id) == []


@pytest.mark.asyncio
async def test_record_manual_verdict(db_session):
    j = await _closed_mock_journal(db_session, pnl_pct="0.0")
    out = await svc.record_manual_verdict(
        db_session, journal_id=j.id, verdict="bad", comment="thesis broke"
    )
    assert out["status"] == "ok"
    rows = await _reviews_for(db_session, j.id)
    assert len(rows) == 1
    assert rows[0].verdict_source == "manual"
    with pytest.raises(ValueError):
        await svc.record_manual_verdict(db_session, journal_id=j.id, verdict="great")
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_journal_verdict_service.py -k "sync or manual or flag_off" -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: 구현**

`app/services/trade_journal/journal_verdict_service.py` 생성:

```python
"""ROB-405 Slice B — record verdicts for closed mock trade_journals.

Auto verdicts (pnl_pct policy) for closed account_type='mock' journals lacking
one; manual verdicts are operator overrides. Idempotent via partial-unique
(journal_id) WHERE verdict_source='auto'. Default off.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select

from app.core.config import settings
from app.models.review import TradeJournalReview
from app.models.trade_journal import TradeJournal
from app.services.trade_journal.journal_verdict_policy import (
    classify_journal_verdict,
)

logger = logging.getLogger(__name__)

_VALID_VERDICTS = frozenset({"good", "neutral", "bad"})


async def sync_journal_verdicts(db, *, force: bool = False) -> dict[str, Any]:
    """Record auto verdicts for closed mock journals without one."""
    if not force and not settings.JOURNAL_VERDICT_AUTO_ENABLED:
        return {"status": "disabled", "created": 0}

    journals = (
        await db.execute(
            select(TradeJournal).where(
                TradeJournal.status == "closed",
                TradeJournal.account_type == "mock",
            )
        )
    ).scalars().all()

    created = 0
    for j in journals:
        existing = (
            await db.execute(
                select(TradeJournalReview.id).where(
                    TradeJournalReview.journal_id == j.id,
                    TradeJournalReview.verdict_source == "auto",
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            continue
        db.add(
            TradeJournalReview(
                journal_id=j.id,
                verdict=classify_journal_verdict(j.pnl_pct),
                verdict_source="auto",
                pnl_pct=j.pnl_pct,
            )
        )
        created += 1
    await db.commit()
    return {"status": "ok", "created": created}


async def record_manual_verdict(
    db, *, journal_id: int, verdict: str, comment: str | None = None
) -> dict[str, Any]:
    """Record an operator (manual) verdict override."""
    if verdict not in _VALID_VERDICTS:
        raise ValueError(f"invalid verdict: {verdict!r}")
    db.add(
        TradeJournalReview(
            journal_id=journal_id,
            verdict=verdict,
            verdict_source="manual",
            comment=comment,
        )
    )
    await db.commit()
    return {"status": "ok"}
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_journal_verdict_service.py -v`
Expected: PASS (전체).

- [ ] **Step 5: 커밋**

```bash
git add app/services/trade_journal/journal_verdict_service.py tests/test_journal_verdict_service.py
git commit -m "feat(ROB-405): journal verdict service (auto sync + manual override)"
```

---

## Task 4: paused taskiq task + CLI

**Files:**
- Create: `app/tasks/journal_verdict_tasks.py`, `scripts/sync_journal_verdicts.py`
- Test: `tests/test_journal_verdict_task.py`

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_journal_verdict_task.py` 생성:

```python
"""ROB-405 Slice B — paused taskiq task gating."""

from __future__ import annotations

import pytest

import app.tasks.journal_verdict_tasks as task_mod


@pytest.mark.asyncio
async def test_disabled_when_flag_off(monkeypatch):
    monkeypatch.setattr(task_mod.settings, "JOURNAL_VERDICT_AUTO_ENABLED", False)
    called = {"n": 0}

    async def _fake(db, **kw):
        called["n"] += 1
        return {"status": "ok", "created": 0}

    monkeypatch.setattr(task_mod, "sync_journal_verdicts", _fake)
    result = await task_mod.journal_verdict_sync()
    assert result["status"] == "disabled"
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_runs_when_enabled(monkeypatch):
    monkeypatch.setattr(task_mod.settings, "JOURNAL_VERDICT_AUTO_ENABLED", True)
    captured = {"n": 0}

    async def _fake(db, **kw):
        captured["n"] += 1
        return {"status": "ok", "created": 3}

    monkeypatch.setattr(task_mod, "sync_journal_verdicts", _fake)

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(task_mod, "AsyncSessionLocal", lambda: _FakeSession())
    result = await task_mod.journal_verdict_sync()
    assert result["created"] == 3
    assert captured["n"] == 1
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_journal_verdict_task.py -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3-a: taskiq task**

`app/tasks/journal_verdict_tasks.py` 생성:

```python
"""ROB-405 Slice B — paused taskiq task for auto journal verdicts.
NO schedule: paused; operator flips JOURNAL_VERDICT_AUTO_ENABLED + adds cron.
"""

from __future__ import annotations

import logging

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.core.taskiq_broker import broker
from app.services.trade_journal.journal_verdict_service import (
    sync_journal_verdicts,
)

logger = logging.getLogger(__name__)


@broker.task(task_name="journal_verdict.sync")  # no schedule → paused
async def journal_verdict_sync() -> dict:
    if not settings.JOURNAL_VERDICT_AUTO_ENABLED:
        return {"status": "disabled", "created": 0}
    async with AsyncSessionLocal() as db:
        return await sync_journal_verdicts(db)
```

- [ ] **Step 3-b: operator CLI**

`scripts/sync_journal_verdicts.py` 생성:

```python
"""ROB-405 Slice B — operator CLI for journal verdicts.
``sync`` runs auto verdicts (force). ``manual`` records an override.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="trade_journal verdict bridge")
    sub = p.add_subparsers(dest="mode", required=True)
    sub.add_parser("sync")
    m = sub.add_parser("manual")
    m.add_argument("--journal-id", type=int, required=True)
    m.add_argument("--verdict", choices=["good", "neutral", "bad"], required=True)
    m.add_argument("--comment", default=None)
    return p


async def _amain(args) -> int:
    from app.core.db import AsyncSessionLocal
    from app.services.trade_journal.journal_verdict_service import (
        record_manual_verdict,
        sync_journal_verdicts,
    )

    async with AsyncSessionLocal() as db:
        if args.mode == "sync":
            result = await sync_journal_verdicts(db, force=True)
        else:
            result = await record_manual_verdict(
                db,
                journal_id=args.journal_id,
                verdict=args.verdict,
                comment=args.comment,
            )
    print(json.dumps(result, ensure_ascii=False))
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    return asyncio.run(_amain(_build_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: 통과 확인**

Run:
```bash
uv run pytest tests/test_journal_verdict_task.py -v
uv run python scripts/sync_journal_verdicts.py --help
```
Expected: 테스트 PASS, `--help`는 secret 없이 usage 출력.

- [ ] **Step 5: 커밋**

```bash
git add app/tasks/journal_verdict_tasks.py scripts/sync_journal_verdicts.py tests/test_journal_verdict_task.py
git commit -m "feat(ROB-405): paused taskiq task + CLI for journal verdicts"
```

---

## Task 5: 회귀 + lint/format/typecheck

- [ ] **Step 1: 관련 스위트 회귀**

Run:
```bash
uv run pytest tests/test_journal_verdict_policy.py tests/test_journal_verdict_service.py tests/test_journal_verdict_task.py tests/test_trade_journal_model.py tests/test_mock_roundtrip_journal_bridge.py -p no:randomly -v
```
Expected: 전부 PASS (Slice A 회귀 포함).

- [ ] **Step 2: lint + format**

Run:
```bash
uv run ruff check app/ tests/
uv run ruff format --check app/ tests/ scripts/sync_journal_verdicts.py
```
Expected: 통과(필요 시 `uv run ruff format ...`).

- [ ] **Step 3: typecheck**

Run:
```bash
uv run ty check app/services/trade_journal/journal_verdict_policy.py app/services/trade_journal/journal_verdict_service.py app/models/review.py app/tasks/journal_verdict_tasks.py
```
Expected: 통과.

- [ ] **Step 4: 커밋(필요 시 format)**

```bash
git add -A && git commit -m "style(ROB-405): ruff format" || echo "nothing to format"
```

---

## 검증 / 인수 기준
- closed mock journal → auto verdict(pnl_pct: good ≥+1%, bad ≤-1%, else neutral), 멱등(partial-unique).
- non-closed/non-mock 무시, flag off→disabled. manual override 기록(복수), 잘못된 verdict 거부.
- 신규 trade_journal_reviews 테이블 CHECK + FK CASCADE. Slice A 무변경.

## 범위 밖 (후속)
C counterfactual / D 사이클 read API(verdict 집계) / E follow_up. MCP verdict 도구 / thesis-target 보정 / operator flip.

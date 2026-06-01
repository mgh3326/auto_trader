# ROB-405 Slice E — follow_up_report_item_id 자동 채움 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** closed+verdict watch event(FK null)에 대해 회고 review item을 thin follow-up 리포트(ingestion 서비스)로 만들고 `investment_watch_events.follow_up_report_item_id`를 설정하는 default-off 서비스를 추가한다 — 루프 환류의 마지막 고리.

**Architecture:** `sync_watch_follow_up_items(db)`가 eligible event(correlation_id→closed mock journal→verdict, FK null)를 (kst_date,market) 그룹별로 thin follow-up 리포트로 ingest(review item, client_item_key=correlation_id, evidence_snapshot에 correlation_id)한 뒤 evidence_snapshot.correlation_id로 event↔item.id 매핑 → 신규 repo writer `update_event_follow_up`로 FK 설정. 멱등(FK null 필터 + report_key). 마이그레이션 없음.

**Tech Stack:** Python 3.13, SQLAlchemy async, pytest, taskiq.

**의존**: A(#1086)·B(#1089)·C(#1091) merged. origin/main 기준.

**Spec:** `docs/superpowers/specs/2026-06-02-rob-405-sliceE-followup-link-design.md`

---

## File Structure
| 파일 | 역할 | 변경 |
|---|---|---|
| `app/core/config.py` | 게이트 | `WATCH_FOLLOW_UP_LINK_ENABLED=False` |
| `app/services/investment_reports/repository.py` | repo writer | `update_event_follow_up` |
| `app/services/trade_journal/watch_follow_up_service.py` | 서비스 | `sync_watch_follow_up_items` |
| `app/tasks/watch_follow_up_tasks.py` | paused task | env-gated |
| `scripts/sync_watch_follow_up_items.py` | operator CLI | force run |
| `tests/test_watch_follow_up_service.py` / `tests/test_watch_follow_up_task.py` | | 신규 |

---

## Task 1: config 플래그 + repo writer `update_event_follow_up`

**Files:**
- Modify: `app/core/config.py:480`, `app/services/investment_reports/repository.py`
- Test: `tests/test_watch_follow_up_service.py` (repo writer 단위)

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_watch_follow_up_service.py` 생성:

```python
"""ROB-405 Slice E — watch follow-up link."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.investment_reports import InvestmentReportItem, InvestmentWatchEvent
from app.models.review import TradeJournalReview
from app.models.trade_journal import TradeJournal
from app.services.investment_reports.repository import InvestmentReportsRepository

# Seeds + commits investment_reports / watch_events on the shared test DB —
# hold the cleanup lock so a concurrent xdist TRUNCATE can't wipe rows mid-test
# (ROB-375 / Slice D lesson).
pytestmark = pytest.mark.usefixtures("investment_reports_cleanup_lock")


async def _event(db, *, cid, kst_date="2099-01-02", market="kr", symbol="005930"):
    ev = InvestmentWatchEvent(
        event_uuid=uuid4(),
        idempotency_key=f"idem-{uuid4()}",
        market=market,
        target_kind="asset",
        symbol=symbol,
        metric="price",
        operator="below",
        threshold=Decimal("49000"),
        threshold_key="49000",
        intent="buy_review",
        action_mode="auto_execute_mock",
        outcome="executed",
        current_value=Decimal("49500"),
        correlation_id=cid,
        kst_date=kst_date,
    )
    db.add(ev)
    await db.commit()
    return ev


async def _closed_mock_journal_with_verdict(db, *, cid, pnl="5", verdict="good"):
    j = TradeJournal(
        symbol="005930",
        instrument_type="equity_kr",
        side="buy",
        entry_price=Decimal("50000"),
        quantity=Decimal("10"),
        thesis="t",
        account_type="mock",
        account="kis_mock",
        correlation_id=cid,
        status="closed",
        exit_price=Decimal("52500"),
        exit_date=datetime(2099, 1, 2, tzinfo=UTC),
        pnl_pct=Decimal(pnl),
    )
    db.add(j)
    await db.commit()
    db.add(TradeJournalReview(journal_id=j.id, verdict=verdict, verdict_source="auto"))
    await db.commit()
    return j


@pytest.mark.asyncio
async def test_repo_update_event_follow_up(db_session: AsyncSession):
    cid = f"corr-{uuid4().hex}"
    j = await _closed_mock_journal_with_verdict(db_session, cid=cid)
    ev = await _event(db_session, cid=cid)
    # need any item id; reuse the journal's report-less context via a raw item is
    # not possible (report_id NOT NULL) — instead assert FK set to an existing
    # item created in the service test. Here assert the writer issues the update.
    repo = InvestmentReportsRepository(db_session)
    # create a placeholder report+item via repo to get a valid item id
    report = await repo.insert_report(
        report_uuid=uuid4(),
        idempotency_key=f"rk-{uuid4()}",
        report_type="mock_loop_followup",
        market="kr",
        execution_mode="mock_preview",
        account_scope="kis_mock",
        created_by_profile="t",
        title="t",
        summary="s",
        kst_date="2099-01-02",
        status="draft",
    )
    item = await repo.insert_item(
        item_uuid=uuid4(),
        idempotency_key=f"ik-{uuid4()}",
        report_id=report.id,
        item_kind="watch",
        operation="review",
        symbol="005930",
        intent="trend_recovery_review",
        target_kind="asset",
        rationale="r",
        evidence_snapshot={"correlation_id": cid},
    )
    await db_session.commit()
    await repo.update_event_follow_up(ev.id, follow_up_report_item_id=item.id)
    await db_session.commit()
    refreshed = await db_session.get(InvestmentWatchEvent, ev.id)
    assert refreshed.follow_up_report_item_id == item.id
```

> `insert_report`/`insert_item`의 필수 kwargs는 모델 NOT NULL 컬럼에 맞춘다(위가 최소 집합). 누락 시 IntegrityError로 드러나며 보강한다.

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_watch_follow_up_service.py -k "repo_update_event_follow_up" -v`
Expected: FAIL — `AttributeError: 'InvestmentReportsRepository' object has no attribute 'update_event_follow_up'` (또는 config flag 부재).

- [ ] **Step 3-a: config 플래그**

`app/core/config.py` — `JOURNAL_COUNTERFACTUAL_ENABLED`(480행) 다음에:

```python
    # ROB-405 Slice E — watch follow-up report-item link. Default off.
    WATCH_FOLLOW_UP_LINK_ENABLED: bool = False
```

- [ ] **Step 3-b: repo writer**

`app/services/investment_reports/repository.py` — `update_event_delivery`(348행) 다음에 추가:

```python
    async def update_event_follow_up(
        self, event_id: int, *, follow_up_report_item_id: int
    ) -> None:
        """ROB-405 Slice E — link a watch event to its follow-up report item."""
        await self._session.execute(
            sa.update(InvestmentWatchEvent)
            .where(InvestmentWatchEvent.id == event_id)
            .values(follow_up_report_item_id=follow_up_report_item_id)
        )
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_watch_follow_up_service.py -k "repo_update_event_follow_up" -v`
Expected: PASS.

- [ ] **Step 5: 커밋**

```bash
git add app/core/config.py app/services/investment_reports/repository.py tests/test_watch_follow_up_service.py
git commit -m "feat(ROB-405): WATCH_FOLLOW_UP_LINK_ENABLED flag + repo update_event_follow_up"
```

---

## Task 2: 서비스 `sync_watch_follow_up_items`

**Files:**
- Create: `app/services/trade_journal/watch_follow_up_service.py`
- Test: `tests/test_watch_follow_up_service.py` (추가)

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_watch_follow_up_service.py`에 추가:

```python
from app.services.trade_journal import watch_follow_up_service as svc


async def _item_for_event(db, ev):
    refreshed = await db.get(InvestmentWatchEvent, ev.id)
    if refreshed.follow_up_report_item_id is None:
        return None
    return await db.get(InvestmentReportItem, refreshed.follow_up_report_item_id)


@pytest.mark.asyncio
async def test_sync_links_eligible_event(db_session, monkeypatch):
    monkeypatch.setattr(svc.settings, "WATCH_FOLLOW_UP_LINK_ENABLED", True)
    cid = f"corr-{uuid4().hex}"
    await _closed_mock_journal_with_verdict(db_session, cid=cid, verdict="good")
    ev = await _event(db_session, cid=cid)
    out = await svc.sync_watch_follow_up_items(db_session)
    assert out["linked"] == 1
    item = await _item_for_event(db_session, ev)
    assert item is not None
    assert item.operation == "review"
    assert item.evidence_snapshot["correlation_id"] == cid


@pytest.mark.asyncio
async def test_sync_skips_event_without_verdict(db_session, monkeypatch):
    monkeypatch.setattr(svc.settings, "WATCH_FOLLOW_UP_LINK_ENABLED", True)
    cid = f"corr-{uuid4().hex}"
    # closed mock journal but NO review
    j = TradeJournal(
        symbol="005930", instrument_type="equity_kr", side="buy",
        entry_price=Decimal("50000"), quantity=Decimal("10"), thesis="t",
        account_type="mock", account="kis_mock", correlation_id=cid,
        status="closed", pnl_pct=Decimal("5"),
    )
    db_session.add(j)
    await db_session.commit()
    ev = await _event(db_session, cid=cid)
    out = await svc.sync_watch_follow_up_items(db_session)
    assert out["linked"] == 0
    assert await _item_for_event(db_session, ev) is None


@pytest.mark.asyncio
async def test_sync_idempotent(db_session, monkeypatch):
    monkeypatch.setattr(svc.settings, "WATCH_FOLLOW_UP_LINK_ENABLED", True)
    cid = f"corr-{uuid4().hex}"
    await _closed_mock_journal_with_verdict(db_session, cid=cid)
    ev = await _event(db_session, cid=cid)
    await svc.sync_watch_follow_up_items(db_session)
    first_item = await _item_for_event(db_session, ev)
    out2 = await svc.sync_watch_follow_up_items(db_session)
    assert out2["linked"] == 0  # already linked → skipped
    second_item = await _item_for_event(db_session, ev)
    assert second_item.id == first_item.id


@pytest.mark.asyncio
async def test_flag_off_disables(db_session, monkeypatch):
    monkeypatch.setattr(svc.settings, "WATCH_FOLLOW_UP_LINK_ENABLED", False)
    cid = f"corr-{uuid4().hex}"
    await _closed_mock_journal_with_verdict(db_session, cid=cid)
    ev = await _event(db_session, cid=cid)
    out = await svc.sync_watch_follow_up_items(db_session)
    assert out["status"] == "disabled"
    assert await _item_for_event(db_session, ev) is None
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_watch_follow_up_service.py -k "sync or flag_off" -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: 구현**

`app/services/trade_journal/watch_follow_up_service.py` 생성:

```python
"""ROB-405 Slice E — link closed+verdict watch events to a follow-up report
item so the retrospective feeds the next cycle. Builds a thin follow-up report
via the ingestion service (atomic + idempotent report_key); sets the event FK
via repository.update_event_follow_up. Idempotent (FK-null filter). Default off.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

from sqlalchemy import select

from app.core.config import settings
from app.models.investment_reports import InvestmentWatchEvent
from app.models.review import TradeJournalCounterfactual, TradeJournalReview
from app.models.trade_journal import TradeJournal
from app.schemas.investment_reports import IngestReportItem, IngestReportRequest
from app.services.investment_reports.ingestion import InvestmentReportIngestionService
from app.services.investment_reports.repository import InvestmentReportsRepository

logger = logging.getLogger(__name__)


async def sync_watch_follow_up_items(db, *, force: bool = False) -> dict[str, Any]:
    if not force and not settings.WATCH_FOLLOW_UP_LINK_ENABLED:
        return {"status": "disabled", "linked": 0}

    events = (
        await db.execute(
            select(InvestmentWatchEvent).where(
                InvestmentWatchEvent.follow_up_report_item_id.is_(None),
                InvestmentWatchEvent.correlation_id.is_not(None),
            )
        )
    ).scalars().all()
    if not events:
        return {"status": "ok", "linked": 0}

    cids = list({e.correlation_id for e in events})
    journals = (
        await db.execute(
            select(TradeJournal).where(
                TradeJournal.account_type == "mock",
                TradeJournal.status == "closed",
                TradeJournal.correlation_id.in_(cids),
            )
        )
    ).scalars().all()
    journal_by_cid = {j.correlation_id: j for j in journals}

    verdict_by_jid: dict[int, str] = {}
    if journals:
        for r in (
            await db.execute(
                select(TradeJournalReview).where(
                    TradeJournalReview.journal_id.in_([j.id for j in journals])
                )
            )
        ).scalars().all():
            verdict_by_jid.setdefault(r.journal_id, r.verdict)

    cf_by_cid: dict[str, TradeJournalCounterfactual] = {}
    for c in (
        await db.execute(
            select(TradeJournalCounterfactual).where(
                TradeJournalCounterfactual.correlation_id.in_(cids)
            )
        )
    ).scalars().all():
        cf_by_cid[c.correlation_id] = c

    groups: dict[tuple[str, str], list] = defaultdict(list)
    for e in events:
        j = journal_by_cid.get(e.correlation_id)
        if j is None:
            continue
        verdict = verdict_by_jid.get(j.id)
        if verdict is None:
            continue
        groups[(e.kst_date, e.market)].append((e, j, verdict, cf_by_cid.get(e.correlation_id)))

    if not groups:
        return {"status": "ok", "linked": 0}

    ingest = InvestmentReportIngestionService(db)
    repo = InvestmentReportsRepository(db)
    linked = 0
    for (kst_date, market), tuples in groups.items():
        items = []
        for e, j, verdict, cf in tuples:
            rationale = f"auto follow-up: verdict={verdict}, pnl_pct={j.pnl_pct}"
            if cf is not None:
                rationale += (
                    f", fill_vs_trigger={cf.fill_vs_trigger_pct}, "
                    f"no_action_vs_fill={cf.no_action_vs_fill_pct}"
                )
            items.append(
                IngestReportItem(
                    client_item_key=e.correlation_id,
                    item_kind="watch",
                    operation="review",
                    symbol=e.symbol,
                    intent="trend_recovery_review",
                    target_kind="asset",
                    rationale=rationale,
                    evidence_snapshot={
                        "correlation_id": e.correlation_id,
                        "verdict": verdict,
                        "pnl_pct": (str(j.pnl_pct) if j.pnl_pct is not None else None),
                        "fill_vs_trigger_pct": (
                            str(cf.fill_vs_trigger_pct)
                            if cf and cf.fill_vs_trigger_pct is not None
                            else None
                        ),
                        "no_action_vs_fill_pct": (
                            str(cf.no_action_vs_fill_pct)
                            if cf and cf.no_action_vs_fill_pct is not None
                            else None
                        ),
                    },
                )
            )
        req = IngestReportRequest(
            report_type="mock_loop_followup",
            market=market,
            account_scope="kis_mock",
            execution_mode="mock_preview",
            created_by_profile="rob405_followup",
            title=f"mock loop follow-up {kst_date}",
            summary="auto-generated retrospective follow-up",
            kst_date=kst_date,
            status="draft",
            items=items,
        )
        report, _reused, _count = await ingest.ingest_with_outcome(req)
        item_id_by_cid: dict[str, int] = {}
        for it in await repo.list_items_for_report(report.id):
            cid = (it.evidence_snapshot or {}).get("correlation_id")
            if cid:
                item_id_by_cid[cid] = it.id
        for e, _j, _v, _cf in tuples:
            item_id = item_id_by_cid.get(e.correlation_id)
            if item_id is not None:
                await repo.update_event_follow_up(
                    e.id, follow_up_report_item_id=item_id
                )
                linked += 1
    await db.commit()
    return {"status": "ok", "linked": linked}
```

> `ingest_with_outcome`이 내부 commit하면 그 뒤 update + 최종 commit이 별도 트랜잭션. 그래도 동일 db 세션이라 일관. report_type/market/kst_date 동일 그룹은 report_key 멱등 → 재실행 시 기존 report/item 재사용.

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_watch_follow_up_service.py -v`
Expected: PASS (전체).

- [ ] **Step 5: 커밋**

```bash
git add app/services/trade_journal/watch_follow_up_service.py tests/test_watch_follow_up_service.py
git commit -m "feat(ROB-405): watch follow-up link service (review item + event FK)"
```

---

## Task 3: paused taskiq task + CLI

**Files:**
- Create: `app/tasks/watch_follow_up_tasks.py`, `scripts/sync_watch_follow_up_items.py`
- Test: `tests/test_watch_follow_up_task.py`

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_watch_follow_up_task.py` 생성:

```python
"""ROB-405 Slice E — paused taskiq task gating."""

from __future__ import annotations

import pytest

import app.tasks.watch_follow_up_tasks as task_mod


@pytest.mark.asyncio
async def test_disabled_when_flag_off(monkeypatch):
    monkeypatch.setattr(task_mod.settings, "WATCH_FOLLOW_UP_LINK_ENABLED", False)
    called = {"n": 0}

    async def _fake(db, **kw):
        called["n"] += 1
        return {"status": "ok", "linked": 0}

    monkeypatch.setattr(task_mod, "sync_watch_follow_up_items", _fake)
    result = await task_mod.watch_follow_up_sync()
    assert result["status"] == "disabled"
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_runs_when_enabled(monkeypatch):
    monkeypatch.setattr(task_mod.settings, "WATCH_FOLLOW_UP_LINK_ENABLED", True)
    captured = {"n": 0}

    async def _fake(db, **kw):
        captured["n"] += 1
        return {"status": "ok", "linked": 2}

    monkeypatch.setattr(task_mod, "sync_watch_follow_up_items", _fake)

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(task_mod, "AsyncSessionLocal", lambda: _FakeSession())
    result = await task_mod.watch_follow_up_sync()
    assert result["linked"] == 2
    assert captured["n"] == 1
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_watch_follow_up_task.py -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3-a: taskiq task**

`app/tasks/watch_follow_up_tasks.py` 생성:

```python
"""ROB-405 Slice E — paused taskiq task for watch follow-up linking.
NO schedule: paused; operator flips WATCH_FOLLOW_UP_LINK_ENABLED + adds cron.
"""

from __future__ import annotations

import logging

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.core.taskiq_broker import broker
from app.services.trade_journal.watch_follow_up_service import (
    sync_watch_follow_up_items,
)

logger = logging.getLogger(__name__)


@broker.task(task_name="watch_follow_up.sync")  # no schedule → paused
async def watch_follow_up_sync() -> dict:
    if not settings.WATCH_FOLLOW_UP_LINK_ENABLED:
        return {"status": "disabled", "linked": 0}
    async with AsyncSessionLocal() as db:
        return await sync_watch_follow_up_items(db)
```

- [ ] **Step 3-b: operator CLI**

`scripts/sync_watch_follow_up_items.py` 생성:

```python
"""ROB-405 Slice E — operator CLI for watch follow-up linking.
``run`` forces a sync (creates follow-up report items + links events)."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="watch follow-up link sync")
    p.add_argument("mode", choices=["run"])
    return p


async def _amain() -> int:
    from app.core.db import AsyncSessionLocal
    from app.services.trade_journal.watch_follow_up_service import (
        sync_watch_follow_up_items,
    )

    async with AsyncSessionLocal() as db:
        result = await sync_watch_follow_up_items(db, force=True)
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
uv run pytest tests/test_watch_follow_up_task.py -v
uv run python scripts/sync_watch_follow_up_items.py --help
```
Expected: 테스트 PASS, `--help`는 secret 없이 usage 출력.

- [ ] **Step 5: 커밋**

```bash
git add app/tasks/watch_follow_up_tasks.py scripts/sync_watch_follow_up_items.py tests/test_watch_follow_up_task.py
git commit -m "feat(ROB-405): paused taskiq task + CLI for watch follow-up linking"
```

---

## Task 4: 회귀 + lint/format/typecheck

- [ ] **Step 1: 관련 스위트 회귀**

Run:
```bash
uv run pytest tests/test_watch_follow_up_service.py tests/test_watch_follow_up_task.py tests/test_investment_reports_ingestion.py tests/test_investment_reports_repository.py -p no:randomly -v
```
Expected: 전부 PASS (ingestion/repo 회귀 포함 — follow-up이 기존 ingest 경로 무손상).

- [ ] **Step 2: lint + format**

Run:
```bash
uv run ruff check app/ tests/
uv run ruff format --check app/ tests/ scripts/sync_watch_follow_up_items.py
```
Expected: 통과(필요 시 `uv run ruff format ...`).

- [ ] **Step 3: typecheck**

Run:
```bash
uv run ty check app/services/trade_journal/watch_follow_up_service.py app/services/investment_reports/repository.py app/tasks/watch_follow_up_tasks.py
```
Expected: 통과.

- [ ] **Step 4: 커밋(필요 시 format)**

```bash
git add -A && git commit -m "style(ROB-405): ruff format" || echo "nothing to format"
```

---

## 검증 / 인수 기준
- eligible(closed mock+verdict, FK null) event → follow-up 리포트+review item(operation='review', evidence_snapshot.correlation_id) 생성 + event.follow_up_report_item_id 설정.
- verdict 없음/FK 이미 set → skip. 멱등(재실행 추가 생성·중복 link 없음). (kst_date,market) 그룹 분리. flag off→disabled.
- repo/item 쓰기는 ingestion 서비스 경유. event FK만 신규 writer. 마이그레이션 0. A/B/C/D 무변경.

## 범위 밖 (후속)
다음-사이클 리포트의 follow-up item 소비(생성측). HTTP 표면. operator flip + smoke. **ROB-405 closure = A~E 코드완성 + operator 활성화.**

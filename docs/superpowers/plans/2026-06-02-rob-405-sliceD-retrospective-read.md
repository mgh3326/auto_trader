# ROB-405 Slice D — 사이클 회고 read API (MCP) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** kst_date 범위를 일별 cycle로 쪼개 per-day {armed, triggered, filled, PnL, hit/miss, verdict, counterfactual} 집계를 반환하는 read-only MCP 도구 `get_mock_loop_retrospective`를 추가한다.

**Architecture:** 집계 서비스 `build_mock_loop_retrospective(db, kst_date_from, kst_date_to, market)`가 InvestmentWatchEvent.kst_date를 사이클 앵커로 삼아 그 날 correlation_id로 trade_journals/reviews/counterfactuals를 join 집계(armed만 alert created_at KST 버킷). MCP 도구가 paper_analytics 패턴으로 래핑. read-only, 마이그레이션·flag·task 없음.

**Tech Stack:** Python 3.13, SQLAlchemy async, FastMCP, pytest.

**의존**: A(#1086)·B(#1089)·C(#1091) merged. origin/main 기준.

**Spec:** `docs/superpowers/specs/2026-06-02-rob-405-sliceD-retrospective-read-design.md`

---

## File Structure
| 파일 | 역할 | 변경 |
|---|---|---|
| `app/services/trade_journal/mock_loop_retrospective_service.py` | 집계 | `build_mock_loop_retrospective` |
| `app/mcp_server/tooling/mock_loop_retro_registration.py` | MCP 도구 | `get_mock_loop_retrospective` + `MOCK_LOOP_RETRO_TOOL_NAMES` |
| `app/mcp_server/tooling/registry.py` | 등록 | always-read-only 블록에 호출 추가 |
| `app/mcp_server/__init__.py` | 공개목록 | `AVAILABLE_TOOL_NAMES`에 추가 |
| `tests/test_mock_loop_retrospective_service.py` / `tests/test_mock_loop_retro_tool.py` | | 신규 |

---

## Task 1: 집계 서비스 `build_mock_loop_retrospective`

**Files:**
- Create: `app/services/trade_journal/mock_loop_retrospective_service.py`
- Test: `tests/test_mock_loop_retrospective_service.py`

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_mock_loop_retrospective_service.py` 생성:

```python
"""ROB-405 Slice D — mock loop retrospective aggregation."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.investment_reports import InvestmentWatchEvent
from app.models.review import TradeJournalCounterfactual, TradeJournalReview
from app.models.trade_journal import TradeJournal
from app.services.trade_journal.mock_loop_retrospective_service import (
    build_mock_loop_retrospective,
)


async def _seed_cycle(db, *, day, cid, pnl="5", verdict="good", market="kr"):
    db.add(
        InvestmentWatchEvent(
            event_uuid=uuid4(),
            idempotency_key=f"idem-{uuid4()}",
            market=market,
            target_kind="asset",
            symbol="005930",
            metric="price",
            operator="below",
            threshold=Decimal("49000"),
            threshold_key="49000",
            intent="buy_review",
            action_mode="auto_execute_mock",
            outcome="executed",
            current_value=Decimal("49500"),
            correlation_id=cid,
            kst_date=day,
        )
    )
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
        exit_date=datetime(2026, 6, 2, tzinfo=UTC),
        pnl_pct=Decimal(pnl),
    )
    db.add(j)
    await db.commit()
    db.add(TradeJournalReview(journal_id=j.id, verdict=verdict, verdict_source="auto"))
    db.add(
        TradeJournalCounterfactual(
            journal_id=j.id, correlation_id=cid, symbol="005930", market=market,
            trigger_price=Decimal("49000"), actual_fill_price=Decimal("50000"),
            fill_vs_trigger_pct=Decimal("2.0408"),
            no_action_vs_fill_pct=Decimal("4.0000"),
        )
    )
    await db.commit()
    return j


@pytest.mark.asyncio
async def test_cycle_summary_hit(db_session: AsyncSession):
    day = "2026-06-02"
    cid = f"corr-{uuid4().hex}"
    await _seed_cycle(db_session, day=day, cid=cid, pnl="5", verdict="good")
    cycles = await build_mock_loop_retrospective(
        db_session, kst_date_from=day, kst_date_to=day
    )
    assert len(cycles) == 1
    c = cycles[0]
    assert c["kst_date"] == day
    assert c["triggered"] == 1
    assert c["by_outcome"] == {"executed": 1}
    assert c["filled"] == 1
    assert c["hits"] == 1 and c["misses"] == 0
    assert c["hit_ratio"] == 1.0
    assert c["avg_pnl_pct"] == 5.0
    assert c["verdict"]["good"] == 1
    assert c["counterfactual"]["count"] == 1
    assert c["counterfactual"]["avg_fill_vs_trigger_pct"] == 2.0408


@pytest.mark.asyncio
async def test_cycle_summary_miss(db_session: AsyncSession):
    day = "2026-06-03"
    cid = f"corr-{uuid4().hex}"
    await _seed_cycle(db_session, day=day, cid=cid, pnl="-3", verdict="bad")
    cycles = await build_mock_loop_retrospective(
        db_session, kst_date_from=day, kst_date_to=day
    )
    c = cycles[0]
    assert c["hits"] == 0 and c["misses"] == 1
    assert c["hit_ratio"] == 0.0
    assert c["verdict"]["bad"] == 1


@pytest.mark.asyncio
async def test_market_filter_excludes(db_session: AsyncSession):
    day = "2026-06-04"
    await _seed_cycle(db_session, day=day, cid=f"c-{uuid4().hex}", market="us")
    cycles = await build_mock_loop_retrospective(
        db_session, kst_date_from=day, kst_date_to=day, market="kr"
    )
    assert cycles[0]["triggered"] == 0
    assert cycles[0]["filled"] == 0


@pytest.mark.asyncio
async def test_multi_day_range_separates(db_session: AsyncSession):
    await _seed_cycle(db_session, day="2026-06-05", cid=f"c-{uuid4().hex}", pnl="5")
    await _seed_cycle(db_session, day="2026-06-06", cid=f"c-{uuid4().hex}", pnl="-1")
    cycles = await build_mock_loop_retrospective(
        db_session, kst_date_from="2026-06-05", kst_date_to="2026-06-06"
    )
    by_day = {c["kst_date"]: c for c in cycles}
    assert by_day["2026-06-05"]["hits"] == 1
    assert by_day["2026-06-06"]["misses"] == 1
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_mock_loop_retrospective_service.py -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: 구현**

`app/services/trade_journal/mock_loop_retrospective_service.py` 생성:

```python
"""ROB-405 Slice D — per-cycle (kst_date) retrospective aggregation for the
mock autonomous loop. Read-only. Anchors triggered/filled/PnL/verdict/CF to
each day's watch-event correlation_ids; armed is bucketed by alert created_at
(KST) as a newly-armed proxy (alerts carry no correlation_id/kst_date)."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import func as safunc
from sqlalchemy import select

from app.core.timezone import to_kst_naive
from app.models.investment_reports import InvestmentWatchAlert, InvestmentWatchEvent
from app.models.review import TradeJournalCounterfactual, TradeJournalReview
from app.models.trade_journal import TradeJournal


def _iter_days(from_str: str, to_str: str):
    d0 = date.fromisoformat(from_str)
    d1 = date.fromisoformat(to_str)
    cur = d0
    while cur <= d1:
        yield cur.isoformat()
        cur += timedelta(days=1)


def _avg(values: list) -> float | None:
    nums: list[Decimal] = []
    for v in values:
        if v is None:
            continue
        try:
            nums.append(Decimal(str(v)))
        except (InvalidOperation, TypeError, ValueError):
            continue
    if not nums:
        return None
    return float(sum(nums) / len(nums))


async def build_mock_loop_retrospective(
    db, *, kst_date_from: str, kst_date_to: str, market: str | None = None
) -> list[dict[str, Any]]:
    """Per-day cycle summary over [kst_date_from, kst_date_to] (inclusive)."""
    # armed: bucket all (market-filtered) alerts by created_at KST date once.
    al_stmt = select(InvestmentWatchAlert)
    if market:
        al_stmt = al_stmt.where(InvestmentWatchAlert.market == market)
    armed_by_day: dict[str, int] = {}
    for a in (await db.execute(al_stmt)).scalars().all():
        d = to_kst_naive(a.created_at).date().isoformat()
        armed_by_day[d] = armed_by_day.get(d, 0) + 1

    cycles: list[dict[str, Any]] = []
    for day in _iter_days(kst_date_from, kst_date_to):
        ev_stmt = select(InvestmentWatchEvent).where(
            InvestmentWatchEvent.kst_date == day
        )
        if market:
            ev_stmt = ev_stmt.where(InvestmentWatchEvent.market == market)
        events = (await db.execute(ev_stmt)).scalars().all()
        corr_ids = [e.correlation_id for e in events if e.correlation_id]
        by_outcome: dict[str, int] = {}
        for e in events:
            by_outcome[e.outcome] = by_outcome.get(e.outcome, 0) + 1

        journals: list[TradeJournal] = []
        if corr_ids:
            journals = (
                await db.execute(
                    select(TradeJournal).where(
                        TradeJournal.account_type == "mock",
                        TradeJournal.correlation_id.in_(corr_ids),
                        TradeJournal.status.in_(("active", "closed")),
                    )
                )
            ).scalars().all()
        closed = [
            j for j in journals if j.status == "closed" and j.pnl_pct is not None
        ]
        hits = sum(1 for j in closed if j.pnl_pct > 0)
        misses = sum(1 for j in closed if j.pnl_pct <= 0)
        hit_ratio = (hits / (hits + misses)) if (hits + misses) > 0 else None

        verdict = {"good": 0, "neutral": 0, "bad": 0}
        journal_ids = [j.id for j in journals]
        if journal_ids:
            for v, cnt in (
                await db.execute(
                    select(TradeJournalReview.verdict, safunc.count())
                    .where(TradeJournalReview.journal_id.in_(journal_ids))
                    .group_by(TradeJournalReview.verdict)
                )
            ).all():
                if v in verdict:
                    verdict[v] = int(cnt)

        cf_ft: list = []
        cf_naf: list = []
        cf_count = 0
        if corr_ids:
            cfs = (
                await db.execute(
                    select(TradeJournalCounterfactual).where(
                        TradeJournalCounterfactual.correlation_id.in_(corr_ids)
                    )
                )
            ).scalars().all()
            cf_count = len(cfs)
            cf_ft = [c.fill_vs_trigger_pct for c in cfs]
            cf_naf = [c.no_action_vs_fill_pct for c in cfs]

        cycles.append(
            {
                "kst_date": day,
                "armed": armed_by_day.get(day, 0),
                "triggered": len(events),
                "by_outcome": by_outcome,
                "filled": len(journals),
                "closed": len(closed),
                "avg_pnl_pct": _avg([j.pnl_pct for j in closed]),
                "hits": hits,
                "misses": misses,
                "hit_ratio": hit_ratio,
                "verdict": verdict,
                "counterfactual": {
                    "count": cf_count,
                    "avg_fill_vs_trigger_pct": _avg(cf_ft),
                    "avg_no_action_vs_fill_pct": _avg(cf_naf),
                },
            }
        )
    return cycles
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_mock_loop_retrospective_service.py -v`
Expected: PASS (4건).

- [ ] **Step 5: 커밋**

```bash
git add app/services/trade_journal/mock_loop_retrospective_service.py tests/test_mock_loop_retrospective_service.py
git commit -m "feat(ROB-405): mock loop retrospective aggregation service"
```

---

## Task 2: MCP 도구 + 등록

**Files:**
- Create: `app/mcp_server/tooling/mock_loop_retro_registration.py`
- Modify: `app/mcp_server/tooling/registry.py` (always-read-only 블록), `app/mcp_server/__init__.py` (AVAILABLE_TOOL_NAMES)
- Test: `tests/test_mock_loop_retro_tool.py`

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_mock_loop_retro_tool.py` 생성:

```python
"""ROB-405 Slice D — get_mock_loop_retrospective MCP tool."""

from __future__ import annotations

import pytest

from tests._mcp_tooling_support import build_tools


def test_tool_registered():
    tools = build_tools()
    assert "get_mock_loop_retrospective" in tools


def test_tool_in_available_names():
    from app.mcp_server import AVAILABLE_TOOL_NAMES

    assert "get_mock_loop_retrospective" in AVAILABLE_TOOL_NAMES


@pytest.mark.asyncio
async def test_tool_returns_cycles(monkeypatch):
    import app.mcp_server.tooling.mock_loop_retro_registration as mod

    async def _fake_build(db, *, kst_date_from, kst_date_to, market=None):
        return [{"kst_date": kst_date_from, "triggered": 0}]

    monkeypatch.setattr(mod, "build_mock_loop_retrospective", _fake_build)

    class _Ctx:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(mod, "_session_factory", lambda: (lambda: _Ctx()))

    tools = build_tools()
    result = await tools["get_mock_loop_retrospective"](
        kst_date_from="2026-06-02", kst_date_to="2026-06-02"
    )
    assert result["success"] is True
    assert result["cycles"] == [{"kst_date": "2026-06-02", "triggered": 0}]
```

> `tests/_mcp_tooling_support.build_tools()`는 `register_all_tools`를 DummyMCP에 적용해 name→handler dict를 반환(기존 `tests/test_paper_analytics_tools.py` 패턴). 시그니처가 다르면 그 파일의 호출 형태를 그대로 따른다.

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_mock_loop_retro_tool.py -v`
Expected: FAIL — `"get_mock_loop_retrospective" not in tools` / ImportError.

- [ ] **Step 3-a: MCP 도구 모듈**

`app/mcp_server/tooling/mock_loop_retro_registration.py` 생성 (paper_analytics_registration.py 미러):

```python
"""ROB-405 Slice D — read-only MCP tool: mock loop per-cycle retrospective."""

from __future__ import annotations

from typing import Any
from typing import cast as typing_cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import AsyncSessionLocal
from app.core.timezone import now_kst
from app.services.trade_journal.mock_loop_retrospective_service import (
    build_mock_loop_retrospective,
)

MOCK_LOOP_RETRO_TOOL_NAMES: set[str] = {"get_mock_loop_retrospective"}


def _session_factory() -> async_sessionmaker[AsyncSession]:
    return typing_cast(
        async_sessionmaker[AsyncSession], typing_cast(object, AsyncSessionLocal)
    )


def register_mock_loop_retro_tools(mcp: Any) -> None:
    @mcp.tool(
        name="get_mock_loop_retrospective",
        description=(
            "Per-cycle (KST date) retrospective for the mock autonomous loop: "
            "armed/triggered/filled/PnL/hit-miss + verdict + counterfactual "
            "aggregates over a KST date range. Read-only, mock accounts only."
        ),
    )
    async def get_mock_loop_retrospective(
        kst_date_from: str | None = None,
        kst_date_to: str | None = None,
        market: str | None = None,
    ) -> dict[str, Any]:
        today = now_kst().date().isoformat()
        date_from = kst_date_from or today
        date_to = kst_date_to or date_from
        async with _session_factory()() as db:
            cycles = await build_mock_loop_retrospective(
                db, kst_date_from=date_from, kst_date_to=date_to, market=market
            )
        return {
            "success": True,
            "kst_date_from": date_from,
            "kst_date_to": date_to,
            "market": market,
            "cycles": cycles,
        }


__all__ = ["MOCK_LOOP_RETRO_TOOL_NAMES", "register_mock_loop_retro_tools"]
```

- [ ] **Step 3-b: registry 등록**

`app/mcp_server/tooling/registry.py` — import에 추가(다른 `register_*` import 근처):

```python
from app.mcp_server.tooling.mock_loop_retro_registration import (
    register_mock_loop_retro_tools,
)
```

always-read-only 블록(`register_paper_journal_tools(mcp)` 다음, 약 124행)에 추가:

```python
    register_mock_loop_retro_tools(mcp)
```

- [ ] **Step 3-c: AVAILABLE_TOOL_NAMES 추가**

`app/mcp_server/__init__.py` — `AVAILABLE_TOOL_NAMES` 리스트에 추가(적절한 위치, 예: trade journal/analytics 항목 근처):

```python
    "get_mock_loop_retrospective",
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_mock_loop_retro_tool.py -v`
Expected: PASS (3건).

- [ ] **Step 5: 커밋**

```bash
git add app/mcp_server/tooling/mock_loop_retro_registration.py app/mcp_server/tooling/registry.py app/mcp_server/__init__.py tests/test_mock_loop_retro_tool.py
git commit -m "feat(ROB-405): get_mock_loop_retrospective MCP tool + registration"
```

---

## Task 3: 회귀 + lint/format/typecheck

- [ ] **Step 1: 관련 스위트 회귀**

Run:
```bash
uv run pytest tests/test_mock_loop_retrospective_service.py tests/test_mock_loop_retro_tool.py tests/test_paper_analytics_tools.py tests/test_mcp_profiles.py -p no:randomly -v
```
Expected: 전부 PASS (MCP 프로필 회귀 포함 — 신규 도구가 등록 set과 일치).

- [ ] **Step 2: lint + format**

Run:
```bash
uv run ruff check app/ tests/
uv run ruff format --check app/ tests/
```
Expected: 통과(필요 시 `uv run ruff format app/ tests/`).

- [ ] **Step 3: typecheck**

Run:
```bash
uv run ty check app/services/trade_journal/mock_loop_retrospective_service.py app/mcp_server/tooling/mock_loop_retro_registration.py
```
Expected: 통과.

- [ ] **Step 4: 커밋(필요 시 format)**

```bash
git add -A && git commit -m "style(ROB-405): ruff format" || echo "nothing to format"
```

---

## 검증 / 인수 기준
- `get_mock_loop_retrospective(kst_date_from, kst_date_to, market)` → per-day cycle 리스트(armed/triggered/by_outcome/filled/avg_pnl_pct/hits/misses/hit_ratio/verdict/counterfactual).
- 사이클 앵커=event.kst_date; triggered→filled→PnL→verdict→CF는 correlation_id 일관; armed=alert created_at(KST) 버킷. market 필터, 다중 날 분리, hit/miss(pnl>0).
- read-only(마이그레이션·flag·task 없음), 등록 2곳(registry + AVAILABLE_TOOL_NAMES). A/B/C 무변경.

## 범위 밖 (후속)
E follow_up_report_item_id(ROB-405 마지막). HTTP 라우터(대시보드). armed point-in-time 정확도.

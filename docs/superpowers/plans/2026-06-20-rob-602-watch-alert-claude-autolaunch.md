# watch-alert → Claude Code 컨텍스트-보존 자동 기동 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** watch 알림 발화 시 운영자-호스트 poller가 신선한 `claude -p`를 하드 read-only로 깨워, 지속 아티팩트에서 매매 맥락을 부트스트랩해 분석+dry_run 제안을 내고 Discord 회신 + session_context 적재까지 자동화한다.

**Architecture:** 기존 watch-alert 파이프라인(scanner→Hermes→Discord)은 그대로. 그 위에 ① delivered 이벤트를 노출하는 read-only 표면(repo CLI + MCP 도구) ② 운영자-호스트 poller(레포밖 ops) ③ `/crypto-alert-triage` 슬래시 커맨드(부트스트랩) ④ read-only deny-list 설정 ⑤ session_context 적재+Discord 회신 글루를 얹는다. 레포 변경은 전부 read-only(브로커/주문/감시 mutation 없음, 마이그레이션 없음).

**Tech Stack:** Python 3.13 / uv / SQLAlchemy async / FastAPI MCP(FastMCP) / pytest(+marker) / Claude Code CLI(`claude -p`) / bash + jq + launchd.

## Global Constraints

- **Read-only 추가만**: 신규 레포 코드는 순수 read. 브로커/주문/감시/리포트 mutation 도달 금지. DB 쓰기 없음.
- **마이그레이션 없음**: 기존 `InvestmentWatchEvent`(`app/models/investment_reports.py:577-718`) + 인덱스 `ix_investment_watch_events_delivery_status_created` 재사용.
- **DB 접근은 repository 경유**: 신규 조회는 `InvestmentReportsRepository`에 메서드 추가, 직접 SQL 금지.
- **delivered 게이트**: 폴러/표면은 `delivery_status='delivered'` 이벤트만 노출(skipped/failed는 미전달 → 잘못 기동 방지).
- **디듀프 키**: `event_uuid`(자연 유니크). 멱등은 `idempotency_key=alert_uuid:kst_date:threshold_key`로 하루/threshold당 1 fire 보장.
- **하드 read-only 기동**: 트리아지 `claude -p`는 `--permission-mode bypassPermissions --settings .claude/settings.readonly.json`. deny-list는 26개 주문/리포트 mutation MCP 도구 + `Bash`/`Edit`/`Write`/`MultiEdit`/`NotebookEdit` 차단. `session_context_append`/`session_context_get_recent` 및 read/preview 도구는 **허용**(deny 안 함).
- **실주문은 사람**: 트리아지는 제안만. 실행은 운영자가 인터랙티브 세션에서 확정.
- **CC 사실(검증됨)**: deny는 bypassPermissions에서도 enforce. `mcp__<server>__<tool>` 패턴. 슬래시 커맨드 인자는 `$ARGUMENTS` 단일 블롭. `--output-format json`→`.result`/`.session_id`/`.cost_usd`/`.duration_ms`/`.num_turns`.
- **커밋 트레일러**:
  ```
  Co-authored-by: Hermes Agent <hermes-agent@users.noreply.github.com>
  Co-authored-by: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
  ```
- **테스트 실행**: `uv run pytest <path> -v`.

**참고 spec**: `docs/superpowers/specs/2026-06-20-rob-602-watch-alert-claude-autolaunch-design.md`

## File Structure

| 파일 | 책임 | Task |
|---|---|---|
| `app/services/investment_reports/repository.py` (수정) | `list_events_by_delivery_status` read 메서드 | 1 |
| `tests/services/investment_reports/test_watch_events_recent_repo.py` (생성) | 메서드 단위 테스트 | 1 |
| `scripts/list_recent_watch_events.py` (생성) | 폴러용 read-only CLI(JSON stdout) | 2 |
| `tests/scripts/test_list_recent_watch_events_cli.py` (생성) | CLI 테스트 | 2 |
| `app/mcp_server/tooling/investment_reports_handlers.py` (수정) | `investment_watch_events_list_recent` MCP 도구 + 등록 | 3 |
| `tests/mcp_server/test_watch_events_list_recent_tool.py` (생성) | MCP 도구 테스트 | 3 |
| `.claude/settings.readonly.json` (생성) | 트리아지 deny-list | 4 |
| `tests/test_watch_triage_readonly_settings.py` (생성) | deny-list 완전성 가드 | 4 |
| `.claude/commands/crypto-alert-triage.md` (생성) | 부트스트랩 슬래시 커맨드 | 5 |
| `tests/test_crypto_alert_triage_command.py` (생성) | 커맨드 구조 가드 | 5 |
| `docs/runbooks/watch-alert-claude-triage.md` (생성) | poller 스크립트 + launchd + 스모크 + Q3 검증 프로토콜 | 6 |

> 정확한 import/세션 패턴은 각 수정 파일의 기존 형제 코드(아래 명시)를 열어 그대로 따른다. 추측 금지.

---

### Task 1: repository read 메서드 `list_events_by_delivery_status`

**Files:**
- Modify: `app/services/investment_reports/repository.py` (기존 `list_events_for_source_reports` 바로 뒤, ~line 488)
- Test: `tests/services/investment_reports/test_watch_events_recent_repo.py`

**Interfaces:**
- Consumes: 기존 `InvestmentWatchEvent` 모델, `InvestmentReportsRepository._session`(AsyncSession), `import sqlalchemy as sa`(파일 상단에 이미 존재 — 확인).
- Produces:
  ```python
  async def list_events_by_delivery_status(
      self,
      *,
      delivery_status: str = "delivered",
      delivered_since: datetime | None = None,
      market: str | None = None,
      limit: int = 50,
  ) -> list[InvestmentWatchEvent]
  ```

- [ ] **Step 1: 기존 패턴 확인**

`app/services/investment_reports/repository.py`에서 `list_events_for_source_reports`(~line 472)를 열어 (a) 세션 속성명(`self._session` 등), (b) `sa.select` 사용, (c) `await self._session.scalars(stmt)` 반환 패턴을 확인한다. `datetime`이 상단에 import되어 있는지 확인하고 없으면 `from datetime import datetime` 추가.

- [ ] **Step 2: 실패 테스트 작성**

`tests/services/investment_reports/test_watch_events_recent_repo.py`:

```python
from datetime import datetime, timedelta, timezone

import pytest

from app.core.db import AsyncSessionLocal
from app.models.investment_reports import InvestmentWatchEvent
from app.services.investment_reports.repository import InvestmentReportsRepository

pytestmark = pytest.mark.asyncio


def _utc(offset_min: int) -> datetime:
    return datetime(2026, 6, 20, 12, 0, tzinfo=timezone.utc) + timedelta(minutes=offset_min)


async def _mk_event(db, *, symbol, market, delivery_status, delivered_at, kst_date="2026-06-20"):
    ev = InvestmentWatchEvent(
        market=market,
        target_kind="asset",
        symbol=symbol,
        metric="price",
        operator="below",
        threshold=100,
        threshold_key=f"{symbol}:price:below:100",
        intent="buy_review",
        action_mode="notify_only",
        outcome="notified",
        kst_date=kst_date,
        correlation_id=f"corr-{symbol}-{delivered_at.isoformat()}",
        idempotency_key=f"event:{symbol}:{kst_date}:{symbol}:price:below:100:{delivered_at.isoformat()}",
        delivery_status=delivery_status,
        delivered_at=delivered_at if delivery_status == "delivered" else None,
    )
    db.add(ev)
    await db.flush()
    return ev


async def test_returns_only_delivered_after_since_ordered_asc():
    async with AsyncSessionLocal() as db:
        await _mk_event(db, symbol="KRW-AAA", market="crypto", delivery_status="delivered", delivered_at=_utc(0))
        e1 = await _mk_event(db, symbol="KRW-BBB", market="crypto", delivery_status="delivered", delivered_at=_utc(10))
        e2 = await _mk_event(db, symbol="KRW-CCC", market="crypto", delivery_status="delivered", delivered_at=_utc(20))
        await _mk_event(db, symbol="KRW-DDD", market="crypto", delivery_status="pending", delivered_at=_utc(15))
        await db.commit()

        repo = InvestmentReportsRepository(db)
        rows = await repo.list_events_by_delivery_status(
            delivery_status="delivered", delivered_since=_utc(5), market="crypto", limit=50
        )

    symbols = [r.symbol for r in rows]
    assert symbols == ["KRW-BBB", "KRW-CCC"]  # delivered, >= since, asc, pending excluded
    assert {r.event_uuid for r in rows} == {e1.event_uuid, e2.event_uuid}


async def test_market_filter_and_limit_clamp():
    async with AsyncSessionLocal() as db:
        await _mk_event(db, symbol="005930", market="kr", delivery_status="delivered", delivered_at=_utc(0))
        await _mk_event(db, symbol="KRW-EEE", market="crypto", delivery_status="delivered", delivered_at=_utc(0))
        await db.commit()
        repo = InvestmentReportsRepository(db)
        kr = await repo.list_events_by_delivery_status(market="kr", limit=0)  # clamp -> >=1
    assert all(r.market == "kr" for r in kr)
    assert len(kr) >= 1
```

> 만약 레포에 표준 async DB fixture(예: `db_session`)가 있으면 `AsyncSessionLocal()` 대신 그것을 사용하도록 맞춘다. 기존 `tests/services/investment_reports/`의 형제 테스트를 먼저 확인.

- [ ] **Step 3: 실패 확인**

Run: `uv run pytest tests/services/investment_reports/test_watch_events_recent_repo.py -v`
Expected: FAIL — `AttributeError: ... has no attribute 'list_events_by_delivery_status'`

- [ ] **Step 4: 메서드 구현**

`repository.py`의 `list_events_for_source_reports` 바로 뒤에 추가(세션 속성명은 Step 1에서 확인한 실제 이름으로):

```python
    async def list_events_by_delivery_status(
        self,
        *,
        delivery_status: str = "delivered",
        delivered_since: datetime | None = None,
        market: str | None = None,
        limit: int = 50,
    ) -> list[InvestmentWatchEvent]:
        """List watch events by delivery status, newest-fire-last (asc).

        Primary use: external poller discovering newly-DELIVERED watch fires.
        Read-only; no mutation.
        """
        stmt = sa.select(InvestmentWatchEvent).where(
            InvestmentWatchEvent.delivery_status == delivery_status
        )
        if delivered_since is not None:
            stmt = stmt.where(InvestmentWatchEvent.delivered_at >= delivered_since)
        if market is not None:
            stmt = stmt.where(InvestmentWatchEvent.market == market)
        stmt = stmt.order_by(InvestmentWatchEvent.delivered_at.asc()).limit(
            max(1, min(int(limit), 500))
        )
        result = await self._session.scalars(stmt)
        return list(result.all())
```

- [ ] **Step 5: 통과 확인**

Run: `uv run pytest tests/services/investment_reports/test_watch_events_recent_repo.py -v`
Expected: PASS (2 passed)

- [ ] **Step 6: 커밋**

```bash
git add app/services/investment_reports/repository.py tests/services/investment_reports/test_watch_events_recent_repo.py
git commit -m "feat(ROB-602): list_events_by_delivery_status read 메서드 (delivered watch 이벤트 폴러용)

Refs ROB-602.

Co-authored-by: Hermes Agent <hermes-agent@users.noreply.github.com>
Co-authored-by: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: 폴러용 read-only CLI `scripts/list_recent_watch_events.py`

bash 폴러가 새 fire를 알기 위해 JSON을 stdout으로 받는 read-only CLI. Task 1 메서드 호출.

**Files:**
- Create: `scripts/list_recent_watch_events.py`
- Test: `tests/scripts/test_list_recent_watch_events_cli.py`

**Interfaces:**
- Consumes: `InvestmentReportsRepository.list_events_by_delivery_status` (Task 1), `AsyncSessionLocal`.
- Produces: `async def collect(market, since, limit) -> dict` 와 `main(argv)` 진입점. stdout JSON 스키마: `{"success": true, "count": N, "events": [{event_uuid, symbol, market, source_report_uuid, metric, operator, threshold, current_value, delivered_at, kst_date}, ...]}`.

- [ ] **Step 1: 실패 테스트 작성**

`tests/scripts/test_list_recent_watch_events_cli.py`:

```python
import json

import pytest

from app.core.db import AsyncSessionLocal
from scripts.list_recent_watch_events import collect
from tests.services.investment_reports.test_watch_events_recent_repo import _mk_event, _utc

pytestmark = pytest.mark.asyncio


async def test_collect_returns_serializable_delivered_events():
    async with AsyncSessionLocal() as db:
        await _mk_event(db, symbol="KRW-XYZ", market="crypto", delivery_status="delivered", delivered_at=_utc(0))
        await db.commit()

    out = await collect(market="crypto", since=None, limit=50)
    assert out["success"] is True
    assert out["count"] >= 1
    # JSON 직렬화 가능 (bash가 jq로 파싱)
    blob = json.dumps(out)
    assert "KRW-XYZ" in blob
    ev = next(e for e in out["events"] if e["symbol"] == "KRW-XYZ")
    assert set(ev) >= {"event_uuid", "symbol", "market", "source_report_uuid", "delivered_at"}
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/scripts/test_list_recent_watch_events_cli.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'scripts.list_recent_watch_events'`

- [ ] **Step 3: CLI 구현**

`scripts/list_recent_watch_events.py`:

```python
"""Read-only CLI: 최근 DELIVERED watch 이벤트를 JSON으로 stdout 출력.

운영자-호스트 alert poller가 새 fire를 감지하는 데이터 소스(ROB-602).
브로커/주문/감시 mutation 없음. DB 쓰기 없음.

사용:
    uv run python -m scripts.list_recent_watch_events --market crypto --since 2026-06-20T12:00:00Z --limit 50
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime

from app.core.db import AsyncSessionLocal
from app.services.investment_reports.repository import InvestmentReportsRepository


def _parse_since(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


async def collect(*, market: str | None, since: str | datetime | None, limit: int) -> dict:
    parsed = since if isinstance(since, datetime) else _parse_since(since)
    async with AsyncSessionLocal() as db:
        repo = InvestmentReportsRepository(db)
        events = await repo.list_events_by_delivery_status(
            delivery_status="delivered",
            delivered_since=parsed,
            market=market,
            limit=limit,
        )
    return {
        "success": True,
        "count": len(events),
        "events": [
            {
                "event_uuid": str(e.event_uuid),
                "symbol": e.symbol,
                "market": e.market,
                "source_report_uuid": str(e.source_report_uuid) if e.source_report_uuid else None,
                "metric": e.metric,
                "operator": e.operator,
                "threshold": str(e.threshold) if e.threshold is not None else None,
                "current_value": str(e.current_value) if e.current_value is not None else None,
                "delivered_at": e.delivered_at.isoformat() if e.delivered_at else None,
                "kst_date": e.kst_date,
            }
            for e in events
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="최근 delivered watch 이벤트(read-only JSON)")
    parser.add_argument("--market", default=None, help="kr|us|crypto (기본 전체)")
    parser.add_argument("--since", default=None, help="ISO8601, delivered_at >= since")
    parser.add_argument("--limit", type=int, default=50)
    args = parser.parse_args(argv)
    out = asyncio.run(collect(market=args.market, since=args.since, limit=args.limit))
    json.dump(out, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

> `from app.core.db import AsyncSessionLocal` 경로가 실제와 다르면(다른 스크립트의 import를 확인) 맞춘다.

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/scripts/test_list_recent_watch_events_cli.py -v`
Expected: PASS

- [ ] **Step 5: 커밋**

```bash
git add scripts/list_recent_watch_events.py tests/scripts/test_list_recent_watch_events_cli.py
git commit -m "feat(ROB-602): 폴러용 read-only CLI list_recent_watch_events (delivered 이벤트 JSON)

Refs ROB-602.

Co-authored-by: Hermes Agent <hermes-agent@users.noreply.github.com>
Co-authored-by: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: MCP 도구 `investment_watch_events_list_recent` (수동/claude 편의)

폴러는 Task 2 CLI를 쓰지만, claude/운영자가 대화 중 직접 조회할 수 있게 동일 메서드를 MCP 도구로도 노출(순수 read).

**Files:**
- Modify: `app/mcp_server/tooling/investment_reports_handlers.py` (`investment_report_context_get` 핸들러·등록부 인근, :936-968 및 `register_investment_report_tools`)
- Test: `tests/mcp_server/test_watch_events_list_recent_tool.py`

**Interfaces:**
- Consumes: Task 1 메서드, 기존 import `AsyncSessionLocal`, `InvestmentReportsRepository`, `InvestmentWatchEventResponse`(이 파일이 이미 사용 — 확인).
- Produces:
  ```python
  async def investment_watch_events_list_recent_impl(
      market: str | None = None,
      since_timestamp: str | None = None,
      limit: int = 50,
  ) -> dict
  ```
  반환: `{success, count, events: [InvestmentWatchEventResponse(by_alias json)]}` 또는 `{success: False, error: "invalid_timestamp", hint}`.

- [ ] **Step 1: 기존 핸들러/등록 패턴 확인**

`investment_reports_handlers.py`에서 (a) `investment_report_context_get`류 핸들러가 `async with AsyncSessionLocal() as db:` + `InvestmentReportsRepository(db)`를 쓰는 형태, (b) `register_investment_report_tools(mcp)`에서 `mcp.tool(name=..., description=...)(impl)` 등록 형태, (c) `InvestmentWatchEventResponse`가 import되어 있는지 확인.

- [ ] **Step 2: 실패 테스트 작성**

`tests/mcp_server/test_watch_events_list_recent_tool.py`:

```python
import pytest

from app.core.db import AsyncSessionLocal
from app.mcp_server.tooling.investment_reports_handlers import (
    investment_watch_events_list_recent_impl,
)
from tests.services.investment_reports.test_watch_events_recent_repo import _mk_event, _utc

pytestmark = pytest.mark.asyncio


async def test_tool_returns_delivered_events_json():
    async with AsyncSessionLocal() as db:
        await _mk_event(db, symbol="KRW-TOOL", market="crypto", delivery_status="delivered", delivered_at=_utc(0))
        await db.commit()
    out = await investment_watch_events_list_recent_impl(market="crypto", limit=50)
    assert out["success"] is True
    assert any(e["symbol"] == "KRW-TOOL" for e in out["events"])


async def test_tool_rejects_bad_timestamp():
    out = await investment_watch_events_list_recent_impl(since_timestamp="not-a-date")
    assert out["success"] is False
    assert out["error"] == "invalid_timestamp"
```

- [ ] **Step 3: 실패 확인**

Run: `uv run pytest tests/mcp_server/test_watch_events_list_recent_tool.py -v`
Expected: FAIL — ImportError(`investment_watch_events_list_recent_impl` 없음)

- [ ] **Step 4: 핸들러 + 등록 구현**

핸들러 함수 추가(파일 상단에 `from datetime import datetime`, `from typing import Any`가 없으면 확인 후 사용):

```python
async def investment_watch_events_list_recent_impl(
    market: str | None = None,
    since_timestamp: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """최근 DELIVERED watch 트리거 이벤트 조회(운영자 poller/수동용, read-only).

    delivery_status='delivered' 이벤트만, delivered_at>=since_timestamp, delivered_at 오름차순.
    디듀프는 event_uuid. 브로커/주문/감시 mutation 없음.
    """
    parsed_since = None
    if since_timestamp:
        try:
            parsed_since = datetime.fromisoformat(since_timestamp.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return {
                "success": False,
                "error": "invalid_timestamp",
                "hint": "ISO8601, e.g. 2026-06-20T12:34:56Z",
            }
    capped = max(1, min(int(limit), 500))
    async with AsyncSessionLocal() as db:
        repo = InvestmentReportsRepository(db)
        events = await repo.list_events_by_delivery_status(
            delivery_status="delivered",
            delivered_since=parsed_since,
            market=market,
            limit=capped,
        )
    return {
        "success": True,
        "count": len(events),
        "events": [
            InvestmentWatchEventResponse.model_validate(e).model_dump(mode="json", by_alias=True)
            for e in events
        ],
    }
```

`register_investment_report_tools(mcp)` 안, 기존 watch/이벤트 read 도구 등록 옆에 추가:

```python
    mcp.tool(
        name="investment_watch_events_list_recent",
        description=(
            "최근 DELIVERED watch 트리거 이벤트 목록(운영자 poller/수동 조회용). "
            "market 필터 + since_timestamp(ISO8601, delivered_at>=) + limit(1..500). "
            "delivered만 노출(skipped/failed 제외). 디듀프=event_uuid. "
            "Read-only. 브로커/주문/감시 mutation 없음."
        ),
    )(investment_watch_events_list_recent_impl)
```

> `InvestmentWatchEventResponse`가 미import면 schema 모듈에서 import 추가(`app/schemas/investment_reports.py`).

- [ ] **Step 5: 통과 확인**

Run: `uv run pytest tests/mcp_server/test_watch_events_list_recent_tool.py -v`
Expected: PASS (2 passed)

- [ ] **Step 6: 커밋**

```bash
git add app/mcp_server/tooling/investment_reports_handlers.py tests/mcp_server/test_watch_events_list_recent_tool.py
git commit -m "feat(ROB-602): investment_watch_events_list_recent MCP 도구 (read-only delivered 이벤트)

Refs ROB-602.

Co-authored-by: Hermes Agent <hermes-agent@users.noreply.github.com>
Co-authored-by: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: 하드 read-only deny-list `.claude/settings.readonly.json` + 완전성 가드

트리아지 `claude -p` 전용 권한 파일. 26개 주문/리포트 mutation MCP 도구 + 파일시스템/Bash 차단. deny가 누락되면 안전구멍이므로 가드 테스트로 완전성을 잠근다.

**Files:**
- Create: `.claude/settings.readonly.json`
- Test: `tests/test_watch_triage_readonly_settings.py`

- [ ] **Step 1: 실패 테스트 먼저 작성**

`tests/test_watch_triage_readonly_settings.py`:

```python
import json
import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[1]
SETTINGS = REPO / ".claude" / "settings.readonly.json"
TOOLING = REPO / "app" / "mcp_server" / "tooling"

# spec §6 deny-list (논리 도구명). 새 mutation 도구가 생기면 여기 + JSON에 추가해야 테스트 통과.
KNOWN_MUTATION_TOOLS = frozenset(
    {
        "place_order", "cancel_order", "modify_order",
        "kis_live_place_order", "kis_live_cancel_order", "kis_live_modify_order",
        "kis_live_reconcile_orders",
        "kis_mock_place_order", "kis_mock_cancel_order", "kis_mock_modify_order",
        "toss_place_order", "toss_modify_order", "toss_cancel_order", "toss_reconcile_orders",
        "alpaca_paper_submit_order", "alpaca_paper_cancel_order",
        "kiwoom_mock_place_order", "kiwoom_mock_cancel_order", "kiwoom_mock_modify_order",
        "live_reconcile_orders",
        "investment_report_create", "investment_report_add_items", "investment_report_update",
        "investment_report_decide_item", "investment_report_activate_watch",
        "investment_report_set_status",
    }
)

# read/preview/handoff 도구는 mutation 이름패턴과 무관하므로 스캔에서 제외(허용 대상).
ORDER_MUTATION_RE = re.compile(
    r'name\s*=\s*["\']([a-z0-9_]*(?:place_order|cancel_order|modify_order|submit_order|reconcile_orders))["\']'
)


def _deny() -> list[str]:
    data = json.loads(SETTINGS.read_text(encoding="utf-8"))
    return data["permissions"]["deny"]


def _denied_mcp_suffixes() -> set[str]:
    return {e.split("__")[-1] for e in _deny() if e.startswith("mcp__")}


def test_settings_file_is_valid_json_with_deny_array():
    assert isinstance(_deny(), list) and len(_deny()) > 0


def test_denies_all_known_mutation_tools():
    missing = KNOWN_MUTATION_TOOLS - _denied_mcp_suffixes()
    assert not missing, f"deny-list 누락 mutation 도구: {sorted(missing)}"


def test_denies_filesystem_and_bash_builtins():
    deny = set(_deny())
    assert {"Bash", "Edit", "Write", "MultiEdit", "NotebookEdit"} <= deny


def test_session_context_append_is_NOT_denied():
    # 자가치유 핸드오프 적재는 의도적 허용 — deny되면 출력 경로가 막힌다.
    assert not any(e.endswith("__session_context_append") for e in _deny())


def test_no_new_order_mutation_tool_escapes_known_set():
    found: set[str] = set()
    for p in TOOLING.glob("*.py"):
        found |= set(ORDER_MUTATION_RE.findall(p.read_text(encoding="utf-8")))
    escaped = found - KNOWN_MUTATION_TOOLS
    assert not escaped, (
        f"새 주문 mutation 도구가 deny-list/KNOWN_MUTATION_TOOLS에 없음: {sorted(escaped)} "
        "→ .claude/settings.readonly.json deny + KNOWN_MUTATION_TOOLS 둘 다 갱신"
    )
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_watch_triage_readonly_settings.py -v`
Expected: FAIL — `FileNotFoundError`(`.claude/settings.readonly.json` 없음)

- [ ] **Step 3: deny-list 파일 작성**

`.claude/settings.readonly.json` (서버명은 Step 4에서 확정 — 일단 `auto_trader` 가정):

```json
{
  "permissions": {
    "deny": [
      "Bash",
      "Edit",
      "Write",
      "MultiEdit",
      "NotebookEdit",
      "mcp__auto_trader__place_order",
      "mcp__auto_trader__cancel_order",
      "mcp__auto_trader__modify_order",
      "mcp__auto_trader__kis_live_place_order",
      "mcp__auto_trader__kis_live_cancel_order",
      "mcp__auto_trader__kis_live_modify_order",
      "mcp__auto_trader__kis_live_reconcile_orders",
      "mcp__auto_trader__kis_mock_place_order",
      "mcp__auto_trader__kis_mock_cancel_order",
      "mcp__auto_trader__kis_mock_modify_order",
      "mcp__auto_trader__toss_place_order",
      "mcp__auto_trader__toss_modify_order",
      "mcp__auto_trader__toss_cancel_order",
      "mcp__auto_trader__toss_reconcile_orders",
      "mcp__auto_trader__alpaca_paper_submit_order",
      "mcp__auto_trader__alpaca_paper_cancel_order",
      "mcp__auto_trader__kiwoom_mock_place_order",
      "mcp__auto_trader__kiwoom_mock_cancel_order",
      "mcp__auto_trader__kiwoom_mock_modify_order",
      "mcp__auto_trader__live_reconcile_orders",
      "mcp__auto_trader__investment_report_create",
      "mcp__auto_trader__investment_report_add_items",
      "mcp__auto_trader__investment_report_update",
      "mcp__auto_trader__investment_report_decide_item",
      "mcp__auto_trader__investment_report_activate_watch",
      "mcp__auto_trader__investment_report_set_status"
    ]
  }
}
```

- [ ] **Step 4: 실제 MCP 서버명 확정 (안전 임계)**

Run: `claude mcp list`
auto_trader MCP 서버의 **실제 등록명**을 확인한다. `auto_trader`가 아니면(예: `auto-trader`) JSON의 모든 `mcp__auto_trader__` prefix를 실제 서버명으로 치환한다.
⚠️ prefix가 틀리면 deny가 매칭되지 않아 mutation이 허용된다 — Task 6 라이브 스모크에서 실제 차단을 반드시 증명한다.

- [ ] **Step 5: 통과 확인**

Run: `uv run pytest tests/test_watch_triage_readonly_settings.py -v`
Expected: PASS (5 passed)

- [ ] **Step 6: 커밋**

```bash
git add .claude/settings.readonly.json tests/test_watch_triage_readonly_settings.py
git commit -m "feat(ROB-602): 트리아지 하드 read-only deny-list + 완전성 가드 테스트

26 주문/리포트 mutation MCP 도구 + Bash/Edit/Write 차단. 새 mutation 도구가
deny-list 밖으로 새면 소스 스캔 테스트가 실패시킨다.

Refs ROB-602.

Co-authored-by: Hermes Agent <hermes-agent@users.noreply.github.com>
Co-authored-by: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: 부트스트랩 슬래시 커맨드 `.claude/commands/crypto-alert-triage.md` + 구조 가드

poller가 `claude -p "/crypto-alert-triage <payload>"`로 호출. 맥락 복원→분석→dry_run→출력 시퀀스를 박는다.

**Files:**
- Create: `.claude/commands/crypto-alert-triage.md`
- Test: `tests/test_crypto_alert_triage_command.py`

- [ ] **Step 1: 실패 테스트 먼저 작성**

`tests/test_crypto_alert_triage_command.py`:

```python
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[1]
CMD = REPO / ".claude" / "commands" / "crypto-alert-triage.md"


def test_command_exists():
    assert CMD.is_file()


def test_command_invokes_full_bootstrap_sequence():
    body = CMD.read_text(encoding="utf-8")
    for token in (
        "$ARGUMENTS",
        "get_operating_briefing",
        "investment_report_get",
        "session_context_get_recent",
        "session_context_append",
    ):
        assert token in body, f"커맨드에 {token} 누락"


def test_command_states_readonly_contract():
    body = CMD.read_text(encoding="utf-8").lower()
    assert "dry_run" in body
    assert ("read-only" in body) or ("주문" in body and "금지" in body)
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_crypto_alert_triage_command.py -v`
Expected: FAIL — `assert CMD.is_file()` False

- [ ] **Step 3: 커맨드 작성**

`.claude/commands/crypto-alert-triage.md`:

```markdown
---
description: watch 알림 발화 시 컨텍스트-보존 트리아지 (read-only 분석 + dry_run 제안). ROB-602.
---

# crypto-alert-triage

watch 알림이 발화해 너를 깨웠다. 너는 **신선한 세션**이지만, 매매 맥락은 지속 아티팩트에 산다.
아래 순서로 맥락을 복원하고, 발화를 분석하고, dry_run 제안까지만 낸다. **실주문 금지.**

## 입력

`$ARGUMENTS` 에 공백구분 key=value 로 이벤트 요약이 온다:
`event_uuid=... symbol=... market=... source_report_uuid=... metric=... operator=... threshold=... current_value=...`
먼저 이를 파싱한다. `market`이 비면 `crypto`로 간주.

## 1. 맥락 복원 (반드시 이 순서)

1. `get_operating_briefing(market=<market>)` — 현재 보유 / 미체결 주문(만료시각) / 활성 watch / 최신 리포트 / 최근 session_context.
2. `investment_report_get(report_uuid=<source_report_uuid>)` — 발화 item의 rationale / evidence_snapshot / trigger_checklist / max_action / watch_condition. "왜 이 watch를 걸었나"를 복원.
3. `session_context_get_recent(market=<market>, limit=10)` — 직전 트리아지·결정 핸드오프(decision/next_action/open_question). "지난번에 뭘 보고 뭘 미뤘나".

## 2. 분석

- 트리거가 여전히 유효한가? (현재가 vs threshold, 노이즈 여부)
- `trigger_checklist` 항목을 하나씩 점검.
- `max_action`(side/qty·notional/limit/ladder_level)이 지금도 타당한가? 포트폴리오 제약(미체결/현금/중복 반대주문) 반영.
- 손실매도 가드·현금 정책 등 memory/CLAUDE.md 정책과 충돌 없는가.

## 3. dry_run 미리보기 (read-only)

- 필요하면 `buy_ladder_fill_preview` / `sell_ladder_fill_preview` 등 **read-only preview**로 실행안을 시뮬레이션.
- **실주문 절대 금지**: place/modify/cancel/reconcile 도구는 권한으로 차단되어 있고, 호출해서도 안 된다. 너는 제안만 한다.

## 4. 출력 (둘 다 수행)

1. `session_context_append(entries=[{ "market": "<market>", "entry_type": "decision", "title": "<symbol> watch 트리아지", "body": "<핵심 판단 + 제안 dry_run + 다음 액션>", "refs": { "report_uuid": "<source_report_uuid>", "event_uuid": "<event_uuid>", "symbols": ["<symbol>"] }, "created_by": "crypto-alert-triage", "session_label": "alert-triage" }])` — 다음 신선 런이 읽을 핸드오프.
2. 마지막 assistant 메시지로 **Discord용 간결 요약**을 낸다(이게 `--output-format json`의 `.result`로 회신된다):
   - 한 줄 결론(예: "BTC 매수 트리거 유효 — 1차 트랜치 dry_run 권장"),
   - 핵심 근거 2~3개,
   - 제안 dry_run 실행안(side/수량/지정가),
   - 운영자 확인 필요 사항(실주문은 사람이 확정).

## 안전 계약

- READ-ONLY 분석 + dry_run 제안까지만. 실주문/리포트 mutation 금지(권한 차단됨).
- 불확실하면 보수적으로(no-action) 제안하고 그 이유를 적는다.
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_crypto_alert_triage_command.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: 커밋**

```bash
git add .claude/commands/crypto-alert-triage.md tests/test_crypto_alert_triage_command.py
git commit -m "feat(ROB-602): /crypto-alert-triage 부트스트랩 커맨드 + 구조 가드

맥락복원(briefing→report→session_context)→분석→dry_run→출력(session_context append
+ Discord 요약). read-only 계약 명문화.

Refs ROB-602.

Co-authored-by: Hermes Agent <hermes-agent@users.noreply.github.com>
Co-authored-by: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: 운영자-호스트 poller + launchd + 스모크 + Q3 검증 (runbook)

레포밖 ops(운영자 머신, 운영자 `~/.claude` 환경 사용). poller 스크립트·launchd·스모크·검증 프로토콜을 runbook에 박는다. pytest 없음 — 검증은 문서화된 스모크.

**Files:**
- Create: `docs/runbooks/watch-alert-claude-triage.md`

- [ ] **Step 1: runbook 작성 (poller 스크립트 포함)**

`docs/runbooks/watch-alert-claude-triage.md` 에 아래를 포함:

**(a) poller 스크립트** (운영자 머신, 예: `~/ops/watch-alert-triage/poller.sh`):

```bash
#!/usr/bin/env bash
# watch-alert → claude 트리아지 poller (운영자-호스트, 레포밖). ROB-602.
set -euo pipefail

REPO="${AUTO_TRADER_REPO:-$HOME/work/auto_trader}"
SETTINGS="$REPO/.claude/settings.readonly.json"
MARKET="${TRIAGE_MARKET:-crypto}"
DISCORD_WEBHOOK="${DISCORD_TRIAGE_WEBHOOK:?DISCORD_TRIAGE_WEBHOOK 미설정}"
STATE_DIR="${TRIAGE_STATE_DIR:-$HOME/.local/state/watch-alert-triage}"
WATERMARK="$STATE_DIR/last_delivered_at"
SEEN="$STATE_DIR/seen_event_uuids"     # 최근 처리 uuid(동시각 동률 대비)
VLOG="$STATE_DIR/validation.jsonl"     # Q3 검증 로그
DRY_RUN="${DRY_RUN:-0}"                 # 1이면 claude 호출 대신 명령만 출력

mkdir -p "$STATE_DIR"; touch "$SEEN"
since="$(cat "$WATERMARK" 2>/dev/null || true)"

cd "$REPO"
events="$(uv run python -m scripts.list_recent_watch_events \
            --market "$MARKET" ${since:+--since "$since"} --limit 50 \
          | jq -c '.events // []')"

echo "$events" | jq -c '.[]' | while read -r ev; do
  uuid="$(jq -r '.event_uuid' <<<"$ev")"
  grep -qxF "$uuid" "$SEEN" && continue   # 이미 처리

  payload="$(jq -r '"event_uuid=\(.event_uuid) symbol=\(.symbol) market=\(.market) source_report_uuid=\(.source_report_uuid) metric=\(.metric) operator=\(.operator) threshold=\(.threshold) current_value=\(.current_value)"' <<<"$ev")"

  if [[ "$DRY_RUN" == "1" ]]; then
    echo "[dry-run] claude -p \"/crypto-alert-triage $payload\" --permission-mode bypassPermissions --settings $SETTINGS --output-format json"
  else
    res="$(claude -p "/crypto-alert-triage $payload" \
            --permission-mode bypassPermissions \
            --settings "$SETTINGS" \
            --output-format json)" || { echo "claude 실패: $uuid" >&2; continue; }
    text="$(jq -r '.result' <<<"$res")"
    # Discord 회신
    curl -fsS -H 'Content-Type: application/json' \
      -d "$(jq -nc --arg c "**[watch triage] $(jq -r .symbol <<<"$ev")**"$'\n'"$text" '{content:$c}')" \
      "$DISCORD_WEBHOOK" >/dev/null || echo "discord post 실패: $uuid" >&2
    # Q3 검증 로그
    jq -nc --arg u "$uuid" --argjson r "$res" \
      '{event:$u, session_id:$r.session_id, cost_usd:$r.cost_usd, duration_ms:$r.duration_ms, num_turns:$r.num_turns}' >> "$VLOG"
  fi

  # 디듀프 + 워터마크 전진 (성공 처리한 이벤트만)
  echo "$uuid" >> "$SEEN"; tail -n 500 "$SEEN" > "$SEEN.tmp" && mv "$SEEN.tmp" "$SEEN"
  d="$(jq -r '.delivered_at' <<<"$ev")"; [[ -n "$d" && "$d" != "null" ]] && echo "$d" > "$WATERMARK"
done
```

**(b) launchd plist** (`~/Library/LaunchAgents/com.operator.watch-alert-triage.plist`, ~1분 주기):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.operator.watch-alert-triage</string>
  <key>ProgramArguments</key>
  <array><string>/bin/bash</string><string>/Users/USERNAME/ops/watch-alert-triage/poller.sh</string></array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>AUTO_TRADER_REPO</key><string>/Users/USERNAME/work/auto_trader</string>
    <key>TRIAGE_MARKET</key><string>crypto</string>
    <key>DISCORD_TRIAGE_WEBHOOK</key><string>https://discord.com/api/webhooks/...</string>
  </dict>
  <key>StartInterval</key><integer>60</integer>
  <key>StandardOutPath</key><string>/Users/USERNAME/.local/state/watch-alert-triage/stdout.log</string>
  <key>StandardErrorPath</key><string>/Users/USERNAME/.local/state/watch-alert-triage/stderr.log</string>
</dict></plist>
```

**(c) 검증 프로토콜(Q3)** 섹션: `validation.jsonl`의 `cost_usd`/`duration_ms`/`num_turns` 집계 + 정성 평가(신선 런 결론 vs 인터랙티브 판단 일치도, 빠진 맥락). 수용기준=운영자가 맥락 재설명 거의 불필요. 미달 시 Q2 강화(인터랙티브 스냅샷 표준화) 또는 B/C 재검토.

- [ ] **Step 2: 스모크 — CLI read 경로**

Run:
```bash
cd "$REPO" && uv run python -m scripts.list_recent_watch_events --market crypto --limit 5 | jq .
```
Expected: `{"success": true, "count": N, "events": [...]}`. (실 delivered 이벤트 없으면 count 0 — 정상)

- [ ] **Step 3: 스모크 — poller dry-run (claude 미호출)**

Run: `DRY_RUN=1 DISCORD_TRIAGE_WEBHOOK=x AUTO_TRADER_REPO="$REPO" bash ~/ops/watch-alert-triage/poller.sh`
Expected: 새 이벤트마다 `[dry-run] claude -p "/crypto-alert-triage event_uuid=..."` 명령 출력(실제 claude/Discord 호출 없음).

- [ ] **Step 4: 스모크 — 안전 차단 증명 (안전 임계)**

트리아지 권한으로 mutation이 실제 차단되는지(=deny prefix가 맞는지) 증명:
```bash
claude -p "place_order MCP 도구를 호출해 005930 1주 시장가 매수를 시도해줘. 차단되면 차단됐다고만 답해." \
  --permission-mode bypassPermissions --settings "$REPO/.claude/settings.readonly.json" \
  --output-format json | jq -r '.result'
```
Expected: 주문 도구가 **권한으로 거부**됨(실행 안 됨). 만약 실행되면 deny prefix(서버명)가 틀린 것 → Task 4 Step 4로 돌아가 서버명 수정.

- [ ] **Step 5: 스모크 — 실 트리아지 1건(선택, 실 delivered 이벤트 존재 시)**

Run: `DRY_RUN=0` 로 poller 1회 실행. Expected: Discord에 `[watch triage] <symbol>` 회신 + `session_context_get_recent`로 적재 확인 + `validation.jsonl`에 1행.

- [ ] **Step 6: 커밋**

```bash
git add docs/runbooks/watch-alert-claude-triage.md
git commit -m "docs(ROB-602): watch-alert claude 트리아지 runbook (poller+launchd+스모크+Q3 검증)

Refs ROB-602.

Co-authored-by: Hermes Agent <hermes-agent@users.noreply.github.com>
Co-authored-by: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## 최종 검증 (전체 구현 후)

- [ ] 전체 신규 테스트: `uv run pytest tests/services/investment_reports/test_watch_events_recent_repo.py tests/scripts/test_list_recent_watch_events_cli.py tests/mcp_server/test_watch_events_list_recent_tool.py tests/test_watch_triage_readonly_settings.py tests/test_crypto_alert_triage_command.py -v` → all PASS
- [ ] `make lint` (ruff + ty) green
- [ ] Task 6 Step 4 안전 차단 스모크 통과(실 주문 도구 거부 증명)
- [ ] operator: launchd 등록(`launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.operator.watch-alert-triage.plist`) 후 dry-run 관찰 → 실모드 arm

## Self-Review (spec 대조)

- **spec ① read surface** → Task 1(repo) + Task 2(CLI, 폴러 실소비자) + Task 3(MCP, 수동). delivered 게이트/디듀프/페이로드 한계 반영. ✓
- **spec ② poller** → Task 6(runbook 스크립트+launchd+디듀프 워터마크). ✓
- **spec ③ /crypto-alert-triage** → Task 5(맥락복원 시퀀스 §6 그대로). ✓
- **spec ④ read-only deny-list** → Task 4(26 도구+빌트인 차단+완전성 가드+서버명 확정+차단 스모크). ✓
- **spec ⑤ 출력(Discord+session_context)** → Task 5(append) + Task 6(Discord 회신·검증로그). ✓
- **spec §9 Q3 검증** → Task 6 Step 1(c)+Step 5(validation.jsonl). ✓
- **마이그레이션 없음 / read-only / repository 경유** → 전 Task 준수. ✓
- Placeholder 스캔: 환경값(서버명·USERNAME·webhook)은 discovery 스텝/명시 치환으로 처리, 모호 placeholder 없음. ✓
- 타입 일관성: `list_events_by_delivery_status` 시그니처가 Task 1→2→3에서 동일. CLI/MCP 둘 다 동일 메서드 호출. ✓

# ROB-398 Slice 2 — 모멘텀 랭킹 read-model + kr_market_ranking collector Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 기존 `InvestMomentumEventSnapshot`(Naver 랭킹) 위에 freshness 명시 read-only query_service와 `kr_market_ranking` 번들 collector(optional/non-blocking)를 추가하고, snapshot_kind CHECK를 additive 확장한다.

**Architecture:** read-model 로직(freshness 파생·row 매핑)은 repository DI로 DB-free 테스트하는 `query_service.py`에 둔다. collector는 query_service를 통해 읽어 `SnapshotCollectResult`를 만들고 registry/policy에 optional로 등록한다. 새 테이블/적재 경로 없음(기존 momentum job 재사용); snapshot_kind 확장만 additive alembic migration.

**Tech Stack:** Python 3.13, `@dataclass(frozen=True)`, pytest, SQLAlchemy(기존 `InvestMomentumEventsRepository`), alembic. 새 의존성 없음.

**참조 스펙:** `docs/superpowers/specs/2026-06-02-rob398-slice2-momentum-ranking-readmodel-design.md`

기존 시그니처(확인됨):
- `InvestMomentumEventsRepository.list_momentum_events(*, trading_date=None, surface=None, order_type=None, trade_type=None, limit=50) -> list[InvestMomentumEventSnapshot]` (최신 snapshot_at 행, rank 오름차순) — `app/services/invest_momentum_events/repository.py:116`
- `InvestMomentumEventSnapshot` 필드: `snapshot_at: datetime`, `trading_date: date`, `order_type: str`, `rank: int`, `symbol: str`, `name: str|None`, `price/change_rate/trade_value/market_cap: Decimal|None`, `volume: int|None` — `app/models/invest_momentum_event_snapshot.py`
- collector 헬퍼 `build_result(*, snapshot_kind, market, account_scope, payload, origin, as_of, freshness_status="fresh", symbol=None, coverage=None, errors=None)`, `unavailable_result(*, snapshot_kind, market, account_scope, origin, reason, as_of)` — `.../collectors/_base.py`
- `CollectorRequest(market, account_scope=None, symbols=None, candidate_limit=None, policy_snapshot)` — `app/services/investment_snapshots/collectors.py:77`
- 등록: `production_collector_registry(session)` → `registry.register(Collector(session))` — `.../collectors/registry.py:284`
- policy: `SnapshotKindPolicy(snapshot_kind, freshness=FreshnessPolicy(soft_ttl, hard_ttl), required, collector_timeout)`, `_seconds(n)` — `app/services/investment_snapshots/policy.py`
- migration 템플릿: `alembic/versions/20260527_rob329_extend_snapshot_kind_run_card.py`

---

## File Structure

- Create `app/services/invest_momentum_events/query_service.py` — `MomentumRankingQueryService` + 계약 dataclass(`RankingRow`/`Freshness`/`MomentumRanking`) + 상수
- Create `app/services/action_report/snapshot_backed/collectors/kr_market_ranking.py` — `KrMarketRankingSnapshotCollector`
- Modify `app/services/action_report/snapshot_backed/collectors/registry.py` — collector 등록
- Modify `app/services/investment_snapshots/policy.py` — `kr_market_ranking` optional 항목
- Modify `app/models/investment_snapshots.py` — snapshot_kind CHECK 문자열 + `kr_market_ranking`
- Create `alembic/versions/20260602_rob398s2_extend_snapshot_kind_kr_market_ranking.py` — additive CHECK 확장
- Create tests: `tests/test_momentum_ranking_query_service.py`, `tests/test_kr_market_ranking_collector.py`, `tests/test_snapshot_kind_kr_market_ranking.py`

---

## Task 1: read-model query_service (`query_service.py`)

**Files:**
- Create: `app/services/invest_momentum_events/query_service.py`
- Test: `tests/test_momentum_ranking_query_service.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_momentum_ranking_query_service.py
import datetime as dt
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.services.invest_momentum_events.query_service import (
    MomentumRankingQueryService,
)

KST = ZoneInfo("Asia/Seoul")
NOW = dt.datetime(2026, 6, 2, 10, 0, tzinfo=KST)
TODAY = NOW.date()


def _row(rank, symbol, *, snapshot_at, trading_date, order_type="up"):
    return SimpleNamespace(
        rank=rank,
        symbol=symbol,
        name=f"name-{symbol}",
        price=Decimal("1000"),
        change_rate=Decimal("3.5"),
        volume=12345,
        trade_value=Decimal("9999"),
        market_cap=Decimal("100000"),
        order_type=order_type,
        snapshot_at=snapshot_at,
        trading_date=trading_date,
    )


class _FakeRepo:
    def __init__(self, rows):
        self._rows = rows
        self.calls = []

    async def list_momentum_events(self, *, order_type=None, limit=50, **kw):
        self.calls.append((order_type, limit))
        return [r for r in self._rows if r.order_type == order_type][:limit]


@pytest.mark.asyncio
async def test_fresh_ranking_today_recent_snapshot():
    snap = NOW - dt.timedelta(minutes=5)
    repo = _FakeRepo([_row(1, "005930", snapshot_at=snap, trading_date=TODAY),
                      _row(2, "000660", snapshot_at=snap, trading_date=TODAY)])
    svc = MomentumRankingQueryService(repo)
    out = await svc.get_ranking(order_type="up", limit=10, now=NOW)
    assert out.order_type == "up"
    assert [r.symbol for r in out.rows] == ["005930", "000660"]
    assert out.rows[0].price == 1000.0  # Decimal→float
    assert out.freshness.overall == "fresh"


@pytest.mark.asyncio
async def test_stale_when_trading_date_past():
    snap = NOW - dt.timedelta(minutes=5)
    repo = _FakeRepo([_row(1, "005930", snapshot_at=snap, trading_date=TODAY - dt.timedelta(days=1))])
    svc = MomentumRankingQueryService(repo)
    out = await svc.get_ranking(order_type="up", limit=10, now=NOW)
    assert out.freshness.overall == "stale"
    assert out.freshness.stale_reason == "older_trading_date"


@pytest.mark.asyncio
async def test_stale_when_snapshot_older_than_ttl():
    snap = NOW - dt.timedelta(minutes=30)  # > 15min TTL
    repo = _FakeRepo([_row(1, "005930", snapshot_at=snap, trading_date=TODAY)])
    svc = MomentumRankingQueryService(repo)
    out = await svc.get_ranking(order_type="up", limit=10, now=NOW, ttl_minutes=15)
    assert out.freshness.overall == "stale"
    assert out.freshness.stale_reason == "older_than_ttl"


@pytest.mark.asyncio
async def test_unavailable_when_no_rows():
    repo = _FakeRepo([])
    svc = MomentumRankingQueryService(repo)
    out = await svc.get_ranking(order_type="up", limit=10, now=NOW)
    assert out.rows == ()
    assert out.freshness.overall == "unavailable"
    assert out.freshness.stale_reason == "no_ranking_rows"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/mgh3326/work/auto_trader.rob-398-s2 && uv run pytest tests/test_momentum_ranking_query_service.py -v`
Expected: FAIL — `ModuleNotFoundError: ...query_service`.

- [ ] **Step 3: Write minimal implementation**

```python
# app/services/invest_momentum_events/query_service.py
"""모멘텀(Naver 랭킹) read-only read-model query_service (ROB-398 Slice 2).

기존 InvestMomentumEventsRepository 위 thin 래퍼. freshness 를 명시한다
(ROB-388/389 정직성 계승). write 없음.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from zoneinfo import ZoneInfo

_KST = ZoneInfo("Asia/Seoul")
RANKING_TTL_MINUTES: int = 15  # 모멘텀 job 주기 */10 기준


@dataclass(frozen=True)
class RankingRow:
    rank: int
    symbol: str
    name: str | None
    price: float | None
    change_rate: float | None
    volume: int | None
    trade_value: float | None
    market_cap: float | None


@dataclass(frozen=True)
class Freshness:
    overall: str               # "fresh" | "stale" | "unavailable"
    latest_snapshot_at: dt.datetime | None
    stale_reason: str | None


@dataclass(frozen=True)
class MomentumRanking:
    market: str
    order_type: str
    trading_date: dt.date | None
    rows: tuple[RankingRow, ...]
    freshness: Freshness


def _to_float(value: Decimal | float | None) -> float | None:
    return float(value) if value is not None else None


def _map_row(row: object) -> RankingRow:
    return RankingRow(
        rank=row.rank,
        symbol=row.symbol,
        name=getattr(row, "name", None),
        price=_to_float(getattr(row, "price", None)),
        change_rate=_to_float(getattr(row, "change_rate", None)),
        volume=getattr(row, "volume", None),
        trade_value=_to_float(getattr(row, "trade_value", None)),
        market_cap=_to_float(getattr(row, "market_cap", None)),
    )


def _derive_freshness(
    rows: Sequence[object], *, now: dt.datetime, ttl_minutes: int
) -> tuple[Freshness, dt.date | None]:
    if not rows:
        return Freshness("unavailable", None, "no_ranking_rows"), None
    latest = max(r.snapshot_at for r in rows)
    trading_date = rows[0].trading_date
    if trading_date != now.astimezone(_KST).date():
        return Freshness("stale", latest, "older_trading_date"), trading_date
    if now - latest > dt.timedelta(minutes=ttl_minutes):
        return Freshness("stale", latest, "older_than_ttl"), trading_date
    return Freshness("fresh", latest, None), trading_date


class MomentumRankingQueryService:
    def __init__(self, repository: object) -> None:
        self._repo = repository

    async def get_ranking(
        self,
        *,
        order_type: str = "up",
        market: str = "kr",
        limit: int = 50,
        now: dt.datetime,
        ttl_minutes: int = RANKING_TTL_MINUTES,
    ) -> MomentumRanking:
        rows = await self._repo.list_momentum_events(order_type=order_type, limit=limit)
        freshness, trading_date = _derive_freshness(
            rows, now=now, ttl_minutes=ttl_minutes
        )
        return MomentumRanking(
            market=market,
            order_type=order_type,
            trading_date=trading_date,
            rows=tuple(_map_row(r) for r in rows),
            freshness=freshness,
        )
```

Note: `now` 비교는 tz-aware 가정(테스트가 KST aware 전달). collector(Task 2)는 `utcnow()`(UTC aware)를 넘기며 `astimezone(_KST)`로 KST 영업일 비교 — aware끼리 안전.

- [ ] **Step 4: Run test to verify it passes + lint**

Run: `cd /Users/mgh3326/work/auto_trader.rob-398-s2 && uv run pytest tests/test_momentum_ranking_query_service.py -v && uv run ruff check app/services/invest_momentum_events/query_service.py tests/test_momentum_ranking_query_service.py`
Expected: PASS (4 passed); ruff clean.

- [ ] **Step 5: Commit**

```bash
cd /Users/mgh3326/work/auto_trader.rob-398-s2
git add app/services/invest_momentum_events/query_service.py tests/test_momentum_ranking_query_service.py
git commit -m "feat(ROB-398): 모멘텀 랭킹 read-model query_service + freshness

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: kr_market_ranking collector + registry + policy

**Files:**
- Create: `app/services/action_report/snapshot_backed/collectors/kr_market_ranking.py`
- Modify: `app/services/action_report/snapshot_backed/collectors/registry.py`
- Modify: `app/services/investment_snapshots/policy.py`
- Test: `tests/test_kr_market_ranking_collector.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_kr_market_ranking_collector.py
import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from app.services.action_report.snapshot_backed.collectors.kr_market_ranking import (
    KrMarketRankingSnapshotCollector,
)
from app.services.invest_momentum_events.query_service import (
    Freshness,
    MomentumRanking,
    RankingRow,
)
from app.services.investment_snapshots.collectors import CollectorRequest

KST = ZoneInfo("Asia/Seoul")


def _request():
    return CollectorRequest(market="kr", account_scope=None, policy_snapshot={})


class _FakeQuery:
    def __init__(self, *, by_order, raises=False):
        self._by_order = by_order
        self._raises = raises

    async def get_ranking(self, *, order_type, market="kr", limit=50, now, ttl_minutes=15):
        if self._raises:
            raise RuntimeError("query failed")
        return self._by_order[order_type]


def _fresh_ranking(order_type):
    return MomentumRanking(
        market="kr",
        order_type=order_type,
        trading_date=dt.date(2026, 6, 2),
        rows=(RankingRow(1, "005930", "삼성전자", 1000.0, 3.5, 100, 9999.0, 1e5),),
        freshness=Freshness("fresh", dt.datetime(2026, 6, 2, 10, tzinfo=KST), None),
    )


@pytest.mark.asyncio
async def test_collect_returns_kr_market_ranking_payload():
    query = _FakeQuery(by_order={"up": _fresh_ranking("up"), "quantTop": _fresh_ranking("quantTop")})
    collector = KrMarketRankingSnapshotCollector(session=None, query_service=query)
    assert collector.snapshot_kind == "kr_market_ranking"
    results = await collector.collect(_request())
    assert len(results) == 1
    r = results[0]
    assert r.snapshot_kind == "kr_market_ranking"
    assert set(r.payload_json["order_types"]) == {"up", "quantTop"}
    assert r.payload_json["order_types"]["up"]["rows"][0]["symbol"] == "005930"
    assert r.freshness_status == "fresh"


@pytest.mark.asyncio
async def test_collect_unavailable_when_all_unavailable():
    empty = MomentumRanking("kr", "up", None, (), Freshness("unavailable", None, "no_ranking_rows"))
    query = _FakeQuery(by_order={"up": empty, "quantTop": empty})
    collector = KrMarketRankingSnapshotCollector(session=None, query_service=query)
    results = await collector.collect(_request())
    assert results[0].freshness_status == "unavailable"


@pytest.mark.asyncio
async def test_collect_degrades_on_query_error():
    query = _FakeQuery(by_order={}, raises=True)
    collector = KrMarketRankingSnapshotCollector(session=None, query_service=query)
    results = await collector.collect(_request())
    assert results[0].freshness_status == "unavailable"
    assert "reason" in results[0].errors_json
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/mgh3326/work/auto_trader.rob-398-s2 && uv run pytest tests/test_kr_market_ranking_collector.py -v`
Expected: FAIL — `ModuleNotFoundError: ...kr_market_ranking`.

- [ ] **Step 3: Write collector**

```python
# app/services/action_report/snapshot_backed/collectors/kr_market_ranking.py
"""kr_market_ranking collector — Naver 모멘텀 랭킹을 번들 evidence 로 노출 (ROB-398 Slice 2).

read-only: 모멘텀 스냅샷을 query_service 로 읽기만 한다. optional/non-blocking.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.action_report.snapshot_backed.collectors._base import (
    build_result,
    unavailable_result,
    utcnow,
)
from app.services.invest_momentum_events.query_service import MomentumRankingQueryService
from app.services.invest_momentum_events.repository import InvestMomentumEventsRepository
from app.services.investment_snapshots.collectors import (
    CollectorRequest,
    SnapshotCollectResult,
)

_DEFAULT_ORDER_TYPES: tuple[str, ...] = ("up", "quantTop")  # 상승 + 거래량
_RANKING_LIMIT = 30


class KrMarketRankingSnapshotCollector:
    """Optional ``kr_market_ranking`` collector backed by momentum snapshots."""

    snapshot_kind: str = "kr_market_ranking"

    def __init__(
        self,
        session: AsyncSession | None,
        *,
        query_service: MomentumRankingQueryService | None = None,
        order_types: tuple[str, ...] = _DEFAULT_ORDER_TYPES,
    ) -> None:
        self._query = query_service or MomentumRankingQueryService(
            InvestMomentumEventsRepository(session)
        )
        self._order_types = order_types

    async def collect(self, request: CollectorRequest) -> list[SnapshotCollectResult]:
        now = utcnow()
        if request.market != "kr":
            return [
                unavailable_result(
                    snapshot_kind=self.snapshot_kind,
                    market=request.market,
                    account_scope=request.account_scope,
                    origin="auto_trader_db",
                    reason=f"kr_market_ranking unsupported for market={request.market}",
                    as_of=now,
                )
            ]
        try:
            order_payloads: dict[str, Any] = {}
            statuses: list[str] = []
            for order_type in self._order_types:
                ranking = await self._query.get_ranking(
                    order_type=order_type, market="kr", limit=_RANKING_LIMIT, now=now
                )
                order_payloads[order_type] = {
                    "trading_date": (
                        ranking.trading_date.isoformat()
                        if ranking.trading_date
                        else None
                    ),
                    "freshness": asdict(ranking.freshness),
                    "rows": [asdict(r) for r in ranking.rows],
                }
                statuses.append(ranking.freshness.overall)
        except Exception as exc:  # noqa: BLE001 — degrade rather than crash
            return [
                unavailable_result(
                    snapshot_kind=self.snapshot_kind,
                    market=request.market,
                    account_scope=request.account_scope,
                    origin="auto_trader_db",
                    reason=f"momentum ranking query failed: {type(exc).__name__}: {exc}",
                    as_of=now,
                )
            ]

        # overall: 하나라도 fresh면 fresh, 전부 unavailable이면 unavailable, 그 외 soft_stale.
        if any(s == "fresh" for s in statuses):
            freshness_status = "fresh"
        elif statuses and all(s == "unavailable" for s in statuses):
            freshness_status = "unavailable"
        else:
            freshness_status = "soft_stale"

        if freshness_status == "unavailable":
            return [
                unavailable_result(
                    snapshot_kind=self.snapshot_kind,
                    market=request.market,
                    account_scope=request.account_scope,
                    origin="auto_trader_db",
                    reason="no_ranking_rows",
                    as_of=now,
                )
            ]

        payload = {"market": "kr", "order_types": order_payloads}
        return [
            build_result(
                snapshot_kind=self.snapshot_kind,
                market=request.market,
                account_scope=request.account_scope,
                payload=payload,
                origin="auto_trader_db",
                as_of=now,
                freshness_status=freshness_status,
            )
        ]
```

- [ ] **Step 4: Register in registry + policy**

`app/services/action_report/snapshot_backed/collectors/registry.py` — import 추가:

```python
from app.services.action_report.snapshot_backed.collectors.kr_market_ranking import (
    KrMarketRankingSnapshotCollector,
)
```

`production_collector_registry(session)` 안, `registry.register(CandidateUniverseSnapshotCollector(session))` 다음 줄에 추가:

```python
    registry.register(KrMarketRankingSnapshotCollector(session))
```

`app/services/investment_snapshots/policy.py` — `candidate_universe` SnapshotKindPolicy 항목 뒤에 추가:

```python
        SnapshotKindPolicy(
            snapshot_kind="kr_market_ranking",
            freshness=FreshnessPolicy(soft_ttl=_seconds(900), hard_ttl=_seconds(3600)),
            required=False,
            collector_timeout=_seconds(15),
        ),
```

- [ ] **Step 5: Run test to verify it passes + lint**

Run: `cd /Users/mgh3326/work/auto_trader.rob-398-s2 && uv run pytest tests/test_kr_market_ranking_collector.py -v && uv run ruff check app/services/action_report/snapshot_backed/collectors/kr_market_ranking.py app/services/action_report/snapshot_backed/collectors/registry.py app/services/investment_snapshots/policy.py tests/test_kr_market_ranking_collector.py`
Expected: PASS (3 passed); ruff clean.

- [ ] **Step 6: Commit**

```bash
cd /Users/mgh3326/work/auto_trader.rob-398-s2
git add app/services/action_report/snapshot_backed/collectors/kr_market_ranking.py app/services/action_report/snapshot_backed/collectors/registry.py app/services/investment_snapshots/policy.py tests/test_kr_market_ranking_collector.py
git commit -m "feat(ROB-398): kr_market_ranking collector + registry/policy optional 등록

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: snapshot_kind CHECK additive 확장 (model + migration)

**Files:**
- Modify: `app/models/investment_snapshots.py` (CHECK 문자열)
- Create: `alembic/versions/20260602_rob398s2_extend_snapshot_kind_kr_market_ranking.py`
- Test: `tests/test_snapshot_kind_kr_market_ranking.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_snapshot_kind_kr_market_ranking.py
import pytest

from app.models.investment_snapshots import InvestmentSnapshot


def _snapshot_kind_check_sql() -> str:
    for c in InvestmentSnapshot.__table__.constraints:
        if getattr(c, "name", "") == "ck_investment_snapshots_snapshot_kind":
            return str(c.sqltext)
    raise AssertionError("snapshot_kind CHECK not found")


@pytest.mark.unit
def test_check_includes_kr_market_ranking_and_preserves_old_kinds():
    sql = _snapshot_kind_check_sql()
    assert "kr_market_ranking" in sql
    for kind in (
        "portfolio", "market", "news", "symbol", "candidate_universe",
        "browser_probe", "invest_page", "journal", "watch_context",
        "naver_remote_debug", "toss_remote_debug", "llm_input_frozen",
        "pending_orders", "validated_run_card",
    ):
        assert kind in sql, f"existing kind dropped: {kind}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/mgh3326/work/auto_trader.rob-398-s2 && uv run pytest tests/test_snapshot_kind_kr_market_ranking.py -v`
Expected: FAIL — `kr_market_ranking` not in CHECK sqltext.

- [ ] **Step 3: Update model CHECK**

`app/models/investment_snapshots.py` 의 `ck_investment_snapshots_snapshot_kind` CheckConstraint 문자열에 `,'kr_market_ranking'` 추가 (`'validated_run_card'` 다음):

```python
        CheckConstraint(
            "snapshot_kind IN ('portfolio','market','news','symbol',"
            "'candidate_universe','browser_probe','invest_page',"
            "'journal','watch_context','naver_remote_debug',"
            "'toss_remote_debug','llm_input_frozen','pending_orders',"
            "'validated_run_card','kr_market_ranking')",
            name="ck_investment_snapshots_snapshot_kind",
        ),
```

- [ ] **Step 4: Create migration (현재 head 확인 후 down_revision 설정)**

먼저 현재 head 확인:
```bash
cd /Users/mgh3326/work/auto_trader.rob-398-s2 && uv run alembic heads
```
출력된 단일 revision 을 아래 `down_revision` 에 넣는다(예시 placeholder `<CURRENT_HEAD>`).

`alembic/versions/20260602_rob398s2_extend_snapshot_kind_kr_market_ranking.py`:

```python
"""ROB-398 Slice 2 — add 'kr_market_ranking' to investment_snapshots.snapshot_kind CHECK.

Pure additive CHECK extension (no data backfill, no new column). The
kr_market_ranking collector emits snapshot rows with this kind; existing rows
are unaffected. Mirrors 20260527_rob329 (validated_run_card).

Operator-gated: ships in the PR, applied separately via ``alembic upgrade head``.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260602_rob398s2"
down_revision: str | None = "<CURRENT_HEAD>"  # Step 4 의 `alembic heads` 값으로 교체
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SNAPSHOT_KIND_CHECK = "ck_investment_snapshots_snapshot_kind"
_SNAPSHOT_KIND_EXPANDED = (
    "ck_investment_snapshots_ck_investment_snapshots_snapshot_kind"
)

_OLD_KINDS = (
    "'portfolio','market','news','symbol',"
    "'candidate_universe','browser_probe','invest_page','journal',"
    "'watch_context','naver_remote_debug','toss_remote_debug',"
    "'llm_input_frozen','pending_orders','validated_run_card'"
)
_NEW_KINDS = _OLD_KINDS + ",'kr_market_ranking'"


def _drop_snapshot_kind_check_if_exists() -> None:
    op.execute(
        f'ALTER TABLE review.investment_snapshots DROP CONSTRAINT IF EXISTS "{_SNAPSHOT_KIND_EXPANDED}"'
    )
    op.execute(
        f'ALTER TABLE review.investment_snapshots DROP CONSTRAINT IF EXISTS "{_SNAPSHOT_KIND_CHECK}"'
    )


def upgrade() -> None:
    _drop_snapshot_kind_check_if_exists()
    op.create_check_constraint(
        op.f(_SNAPSHOT_KIND_CHECK),
        "investment_snapshots",
        f"snapshot_kind IN ({_NEW_KINDS})",
        schema="review",
    )


def downgrade() -> None:
    _drop_snapshot_kind_check_if_exists()
    op.create_check_constraint(
        op.f(_SNAPSHOT_KIND_CHECK),
        "investment_snapshots",
        f"snapshot_kind IN ({_OLD_KINDS})",
        schema="review",
    )
```

- [ ] **Step 5: Run test to verify it passes + single-head check**

Run:
```bash
cd /Users/mgh3326/work/auto_trader.rob-398-s2
uv run pytest tests/test_snapshot_kind_kr_market_ranking.py -v
uv run alembic heads            # 단일 head 확인
uv run pytest tests/test_us_candles_sync.py::test_revision_graph_has_single_final_head -v
uv run ruff check app/models/investment_snapshots.py alembic/versions/20260602_rob398s2_extend_snapshot_kind_kr_market_ranking.py
```
Expected: 테스트 PASS; `alembic heads` 단일; single-head 테스트 PASS; ruff clean.

- [ ] **Step 6: Commit**

```bash
cd /Users/mgh3326/work/auto_trader.rob-398-s2
git add app/models/investment_snapshots.py alembic/versions/20260602_rob398s2_extend_snapshot_kind_kr_market_ranking.py tests/test_snapshot_kind_kr_market_ranking.py
git commit -m "feat(ROB-398): snapshot_kind CHECK에 kr_market_ranking additive 확장 (operator-gated)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: 전체 검증

**Files:** (검증/회귀만)

- [ ] **Step 1: Slice 2 테스트 + registry/policy 회귀**

Run:
```bash
cd /Users/mgh3326/work/auto_trader.rob-398-s2
uv run pytest tests/test_momentum_ranking_query_service.py tests/test_kr_market_ranking_collector.py tests/test_snapshot_kind_kr_market_ranking.py -v
uv run pytest tests/ -k "collector_registry or snapshot_kind or invest_momentum or action_report" -q
```
Expected: 전부 PASS.

- [ ] **Step 2: lint/format + import-contracts**

Run:
```bash
cd /Users/mgh3326/work/auto_trader.rob-398-s2
uv run ruff check app/ tests/
uv run ruff format --check app/ tests/
uv run pytest tests/test_import_contracts.py -q
```
Expected: PASS; ruff clean. (collector→query_service→repository 는 services 내부; registry 는 collector import; 위반 없음. **format --check 는 수정한 모든 파일 포함되도록 app/ tests/ 전체로 실행** — ROB-398 Slice 1 의 format 누락 교훈.)

- [ ] **Step 3: (변경 없으면) 커밋 불필요 / format 수정 시 커밋**

format 위반 발견 시:
```bash
cd /Users/mgh3326/work/auto_trader.rob-398-s2
uv run ruff format app/ tests/
git add -A && git commit -m "style(ROB-398): ruff format Slice 2

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review (작성자 체크)

**Spec coverage:**
- §3 read-model query_service + freshness(fresh/stale/unavailable + reason) → Task 1 ✅
- §4 kr_market_ranking collector + registry + policy optional → Task 2 ✅
- §5 additive CHECK migration + 모델 CHECK 동기 → Task 3 ✅
- §6 candidate_universe 미변경 → 어떤 Task도 candidate_universe collector 수정 안 함 ✅
- §7 테스트(query freshness 4케이스 / collector fresh·unavailable·degrade / CHECK 회귀) → Task 1/2/3 ✅
- §8 비목표(새 테이블 없음, 적재경로 재사용, priceTop/searchTop 기본 미수집) → 준수 ✅

**Placeholder scan:** migration `down_revision`만 `<CURRENT_HEAD>` placeholder인데, Step 4가 `alembic heads`로 실값 채우는 명령을 명시 → 의도된 런타임 치환(라인 드리프트 회피). 그 외 placeholder 없음.

**Type consistency:** `MomentumRanking`/`RankingRow`/`Freshness`(Task1) → collector(Task2)에서 `asdict`로 직렬화·`.freshness.overall` 사용 일관. `MomentumRankingQueryService.get_ranking(order_type, market, limit, now, ttl_minutes)` 시그니처가 collector 호출과 일치. `build_result`/`unavailable_result` 인자(snapshot_kind/market/account_scope/payload/origin/as_of/freshness_status) 정확. `CollectorRequest(market, account_scope, policy_snapshot)` 정확.

**검증 시 주의:**
- Task 3 Step 4: `down_revision`을 반드시 `alembic heads` 실값으로 교체(미교체 시 멀티헤드 → test_revision_graph_has_single_final_head 실패).
- `build_result`의 `freshness_status`는 모델 CHECK(`fresh/soft_stale/hard_stale/partial/unavailable`) 값만 허용 → collector는 `fresh`/`soft_stale`/`unavailable`만 emit(스펙 freshness의 `stale`은 payload 내부 표기, snapshot row status는 soft_stale로 매핑).

# ROB-398 Slice 3 — 투자자 플로우 read-model + investor_flow collector Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 기존 `InvestorFlowSnapshot`(외인/기관 순매수·double_buy·연속일) 위에 freshness read-only query_service와 `investor_flow` 번들 collector를 추가하고, 새 snapshot_kind를 6곳 동기 + additive CHECK migration으로 도입한다(397 FlowData 피드).

**Architecture:** read-model 로직(freshness·row 매핑)은 repository DI로 DB-free 테스트하는 `query_service.py`. collector는 query_service로 읽어 `SnapshotCollectResult`를 만든다. **새 `investor_flow` kind 도입은 drift-guard(런타임 registry ↔ contract ↔ doc matrix)를 원자적으로 만족하도록 registry 등록·contract 엔트리·doc 재렌더·migration을 한 task(Task 3)에 묶는다** (Slice 2에서 contract 누락이 CI에서 적발된 교훈).

**Tech Stack:** Python 3.13, `@dataclass(frozen=True)`, pytest, 기존 `InvestorFlowSnapshotsRepository`, alembic. 새 의존성 없음. 체결강도 제외.

**참조 스펙:** `docs/superpowers/specs/2026-06-02-rob398-slice3-investor-flow-readmodel-design.md`

기존 시그니처(확인됨):
- `InvestorFlowSnapshotsRepository.latest_by_symbols(*, market: str, symbols: Iterable[str], as_of: date|None=None) -> list[InvestorFlowSnapshot]` — `app/services/investor_flow_snapshots/repository.py:118`
- `InvestorFlowSnapshot`: `symbol, snapshot_date(date), foreign_net/institution_net/individual_net(int|None), double_buy/double_sell(bool), foreign_consecutive_buy_days/foreign_consecutive_sell_days/institution_consecutive_buy_days/institution_consecutive_sell_days(int|None), source` — `app/models/investor_flow_snapshot.py`
- collector 헬퍼 `build_result(...)`/`unavailable_result(...)`, `CollectorRequest(market, account_scope, symbols, candidate_limit, policy_snapshot)`, `production_collector_registry(session)` → `registry.register(...)` — Slice 2 plan 참조
- contract 엔트리(투자자 플로우, `collector_snapshot_kind=None`) — `invest_data_source_contract.py` `source_name="investor_flow_snapshots"` 행
- migration 템플릿: `alembic/versions/20260527_rob329_extend_snapshot_kind_run_card.py`; 모델 CHECK 현재값은 `...,'validated_run_card','kr_market_ranking'`(Slice 2 반영)

---

## File Structure

- Create `app/services/investor_flow_snapshots/query_service.py` — `InvestorFlowQueryService` + dataclass(`InvestorFlowRow`/`Freshness`/`InvestorFlow`) + 상수
- Create `app/services/action_report/snapshot_backed/collectors/investor_flow.py` — `InvestorFlowSnapshotCollector`
- Modify `registry.py` (등록), `policy.py` (optional 항목), `app/models/investment_snapshots.py` (CHECK), `app/schemas/investment_snapshots.py` (Literal), `app/services/invest_data_source_contract.py` (엔트리 collector_snapshot_kind), `docs/invest/data-source-contract.md` (matrix 재렌더)
- Create `alembic/versions/20260602_rob398s3_extend_snapshot_kind_investor_flow.py`
- Create tests: `tests/test_investor_flow_query_service.py`, `tests/test_investor_flow_collector.py`, `tests/test_snapshot_kind_investor_flow.py`

---

## Task 1: read-model query_service (`query_service.py`)

**Files:**
- Create: `app/services/investor_flow_snapshots/query_service.py`
- Test: `tests/test_investor_flow_query_service.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_investor_flow_query_service.py
import datetime as dt
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.services.investor_flow_snapshots.query_service import InvestorFlowQueryService

KST = ZoneInfo("Asia/Seoul")
NOW = dt.datetime(2026, 6, 2, 18, 0, tzinfo=KST)
TODAY = NOW.date()


def _row(symbol, *, snapshot_date, foreign_net=100, institution_net=50, double_buy=True):
    return SimpleNamespace(
        symbol=symbol,
        snapshot_date=snapshot_date,
        foreign_net=foreign_net,
        institution_net=institution_net,
        individual_net=-150,
        double_buy=double_buy,
        double_sell=False,
        foreign_consecutive_buy_days=3,
        foreign_consecutive_sell_days=0,
        institution_consecutive_buy_days=2,
        institution_consecutive_sell_days=0,
    )


class _FakeRepo:
    def __init__(self, rows):
        self._rows = rows

    async def latest_by_symbols(self, *, market, symbols, as_of=None):
        wanted = {s.upper() for s in symbols}
        return [r for r in self._rows if r.symbol.upper() in wanted]


@pytest.mark.asyncio
async def test_fresh_today():
    repo = _FakeRepo([_row("005930", snapshot_date=TODAY)])
    svc = InvestorFlowQueryService(repo)
    out = await svc.get_investor_flow(symbols=["005930"], now=NOW, ttl_days=1)
    assert out.rows[0].symbol == "005930"
    assert out.rows[0].double_buy is True
    assert out.rows[0].foreign_consecutive_buy_days == 3
    assert out.freshness.overall == "fresh"


@pytest.mark.asyncio
async def test_yesterday_still_fresh_with_ttl_1():
    repo = _FakeRepo([_row("005930", snapshot_date=TODAY - dt.timedelta(days=1))])
    svc = InvestorFlowQueryService(repo)
    out = await svc.get_investor_flow(symbols=["005930"], now=NOW, ttl_days=1)
    assert out.freshness.overall == "fresh"
    assert out.freshness.age_days == 1


@pytest.mark.asyncio
async def test_old_is_stale():
    repo = _FakeRepo([_row("005930", snapshot_date=TODAY - dt.timedelta(days=3))])
    svc = InvestorFlowQueryService(repo)
    out = await svc.get_investor_flow(symbols=["005930"], now=NOW, ttl_days=1)
    assert out.freshness.overall == "stale"
    assert out.freshness.stale_reason == "older_than_ttl"
    assert out.freshness.age_days == 3


@pytest.mark.asyncio
async def test_no_rows_unavailable():
    svc = InvestorFlowQueryService(_FakeRepo([]))
    out = await svc.get_investor_flow(symbols=["005930"], now=NOW)
    assert out.rows == ()
    assert out.freshness.overall == "unavailable"
    assert out.freshness.stale_reason == "no_flow_rows"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/mgh3326/work/auto_trader.rob-398-s3 && uv run pytest tests/test_investor_flow_query_service.py -v`
Expected: FAIL — `ModuleNotFoundError: ...query_service`.

- [ ] **Step 3: Write minimal implementation**

```python
# app/services/investor_flow_snapshots/query_service.py
"""투자자 플로우 read-only read-model query_service (ROB-398 Slice 3).

기존 InvestorFlowSnapshotsRepository 위 thin freshness 래퍼. write 없음.
체결강도(trade strength)는 본 슬라이스 범위 밖.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from zoneinfo import ZoneInfo

_KST = ZoneInfo("Asia/Seoul")
FLOW_TTL_DAYS: int = 1  # EOD 일별 데이터 — 당일/전일 fresh


@dataclass(frozen=True)
class InvestorFlowRow:
    symbol: str
    foreign_net: int | None
    institution_net: int | None
    individual_net: int | None
    double_buy: bool
    double_sell: bool
    foreign_consecutive_buy_days: int | None
    foreign_consecutive_sell_days: int | None
    institution_consecutive_buy_days: int | None
    institution_consecutive_sell_days: int | None


@dataclass(frozen=True)
class Freshness:
    overall: str               # "fresh" | "stale" | "unavailable"
    snapshot_date: dt.date | None
    stale_reason: str | None
    age_days: int | None


@dataclass(frozen=True)
class InvestorFlow:
    market: str
    snapshot_date: dt.date | None
    rows: tuple[InvestorFlowRow, ...]
    freshness: Freshness


def _map_row(row: object) -> InvestorFlowRow:
    return InvestorFlowRow(
        symbol=row.symbol,
        foreign_net=getattr(row, "foreign_net", None),
        institution_net=getattr(row, "institution_net", None),
        individual_net=getattr(row, "individual_net", None),
        double_buy=bool(getattr(row, "double_buy", False)),
        double_sell=bool(getattr(row, "double_sell", False)),
        foreign_consecutive_buy_days=getattr(row, "foreign_consecutive_buy_days", None),
        foreign_consecutive_sell_days=getattr(row, "foreign_consecutive_sell_days", None),
        institution_consecutive_buy_days=getattr(
            row, "institution_consecutive_buy_days", None
        ),
        institution_consecutive_sell_days=getattr(
            row, "institution_consecutive_sell_days", None
        ),
    )


def _derive_freshness(
    rows: Sequence[object], *, now: dt.datetime, ttl_days: int
) -> tuple[Freshness, dt.date | None]:
    if not rows:
        return Freshness("unavailable", None, "no_flow_rows", None), None
    snapshot_date = max(r.snapshot_date for r in rows)
    age_days = (now.astimezone(_KST).date() - snapshot_date).days
    if age_days <= ttl_days:
        return Freshness("fresh", snapshot_date, None, age_days), snapshot_date
    return Freshness("stale", snapshot_date, "older_than_ttl", age_days), snapshot_date


class InvestorFlowQueryService:
    def __init__(self, repository: object) -> None:
        self._repo = repository

    async def get_investor_flow(
        self,
        *,
        symbols: Iterable[str],
        market: str = "kr",
        now: dt.datetime,
        ttl_days: int = FLOW_TTL_DAYS,
    ) -> InvestorFlow:
        rows = await self._repo.latest_by_symbols(market=market, symbols=list(symbols))
        freshness, snapshot_date = _derive_freshness(
            rows, now=now, ttl_days=ttl_days
        )
        return InvestorFlow(
            market=market,
            snapshot_date=snapshot_date,
            rows=tuple(_map_row(r) for r in rows),
            freshness=freshness,
        )
```

- [ ] **Step 4: Run test to verify it passes + lint**

Run: `cd /Users/mgh3326/work/auto_trader.rob-398-s3 && uv run pytest tests/test_investor_flow_query_service.py -v && uv run ruff check app/services/investor_flow_snapshots/query_service.py tests/test_investor_flow_query_service.py`
Expected: PASS (4 passed); ruff clean.

- [ ] **Step 5: Commit**

```bash
cd /Users/mgh3326/work/auto_trader.rob-398-s3
git add app/services/investor_flow_snapshots/query_service.py tests/test_investor_flow_query_service.py
git commit -m "feat(ROB-398): 투자자 플로우 read-model query_service + freshness(일단위)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: investor_flow collector (class only, 아직 미등록)

**Files:**
- Create: `app/services/action_report/snapshot_backed/collectors/investor_flow.py`
- Test: `tests/test_investor_flow_collector.py`

Note: 이 task는 collector 클래스만 만든다. registry 등록은 Task 3에서 contract/doc/migration과 함께(drift-guard 원자성).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_investor_flow_collector.py
import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from app.services.action_report.snapshot_backed.collectors.investor_flow import (
    InvestorFlowSnapshotCollector,
)
from app.services.investor_flow_snapshots.query_service import (
    Freshness,
    InvestorFlow,
    InvestorFlowRow,
)
from app.services.investment_snapshots.collectors import CollectorRequest

KST = ZoneInfo("Asia/Seoul")


def _request(symbols=("005930",)):
    return CollectorRequest(
        market="kr", account_scope=None, symbols=list(symbols), policy_snapshot={}
    )


def _flow(overall="fresh"):
    return InvestorFlow(
        market="kr",
        snapshot_date=dt.date(2026, 6, 2),
        rows=(
            InvestorFlowRow(
                "005930", 100, 50, -150, True, False, 3, 0, 2, 0
            ),
        ),
        freshness=Freshness(overall, dt.date(2026, 6, 2), None, 0),
    )


class _FakeQuery:
    def __init__(self, *, flow=None, raises=False):
        self._flow = flow
        self._raises = raises

    async def get_investor_flow(self, *, symbols, market="kr", now, ttl_days=1):
        if self._raises:
            raise RuntimeError("boom")
        return self._flow


@pytest.mark.asyncio
async def test_collect_returns_investor_flow_payload():
    collector = InvestorFlowSnapshotCollector(session=None, query_service=_FakeQuery(flow=_flow("fresh")))
    assert collector.snapshot_kind == "investor_flow"
    results = await collector.collect(_request())
    assert len(results) == 1
    r = results[0]
    assert r.snapshot_kind == "investor_flow"
    assert r.payload_json["rows"][0]["symbol"] == "005930"
    assert r.payload_json["rows"][0]["double_buy"] is True
    assert r.freshness_status == "fresh"


@pytest.mark.asyncio
async def test_collect_stale_maps_to_soft_stale():
    collector = InvestorFlowSnapshotCollector(session=None, query_service=_FakeQuery(flow=_flow("stale")))
    results = await collector.collect(_request())
    assert results[0].freshness_status == "soft_stale"


@pytest.mark.asyncio
async def test_collect_unavailable_when_no_rows():
    empty = InvestorFlow("kr", None, (), Freshness("unavailable", None, "no_flow_rows", None))
    collector = InvestorFlowSnapshotCollector(session=None, query_service=_FakeQuery(flow=empty))
    results = await collector.collect(_request())
    assert results[0].freshness_status == "unavailable"


@pytest.mark.asyncio
async def test_collect_degrades_on_error():
    collector = InvestorFlowSnapshotCollector(session=None, query_service=_FakeQuery(raises=True))
    results = await collector.collect(_request())
    assert results[0].freshness_status == "unavailable"
    assert "reason" in results[0].errors_json


@pytest.mark.asyncio
async def test_collect_unavailable_when_no_symbols():
    collector = InvestorFlowSnapshotCollector(session=None, query_service=_FakeQuery(flow=_flow()))
    results = await collector.collect(_request(symbols=()))
    assert results[0].freshness_status == "unavailable"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/mgh3326/work/auto_trader.rob-398-s3 && uv run pytest tests/test_investor_flow_collector.py -v`
Expected: FAIL — `ModuleNotFoundError: ...investor_flow`.

- [ ] **Step 3: Write collector**

```python
# app/services/action_report/snapshot_backed/collectors/investor_flow.py
"""investor_flow collector — 투자자 매매동향을 번들 evidence로 노출 (ROB-398 Slice 3).

read-only: InvestorFlowSnapshot을 query_service로 읽기만 한다. optional/non-blocking.
"""

from __future__ import annotations

from dataclasses import asdict

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.action_report.snapshot_backed.collectors._base import (
    build_result,
    unavailable_result,
    utcnow,
)
from app.services.investment_snapshots.collectors import (
    CollectorRequest,
    SnapshotCollectResult,
)
from app.services.investor_flow_snapshots.query_service import InvestorFlowQueryService
from app.services.investor_flow_snapshots.repository import (
    InvestorFlowSnapshotsRepository,
)


class InvestorFlowSnapshotCollector:
    """Optional ``investor_flow`` collector backed by investor_flow_snapshots."""

    snapshot_kind: str = "investor_flow"

    def __init__(
        self,
        session: AsyncSession | None,
        *,
        query_service: InvestorFlowQueryService | None = None,
    ) -> None:
        self._query = query_service or InvestorFlowQueryService(
            InvestorFlowSnapshotsRepository(session)
        )

    async def collect(self, request: CollectorRequest) -> list[SnapshotCollectResult]:
        now = utcnow()
        if request.market != "kr":
            return [
                unavailable_result(
                    snapshot_kind=self.snapshot_kind,
                    market=request.market,
                    account_scope=request.account_scope,
                    origin="auto_trader_db",
                    reason=f"investor_flow unsupported for market={request.market}",
                    as_of=now,
                )
            ]
        symbols = request.symbols or []
        if not symbols:
            return [
                unavailable_result(
                    snapshot_kind=self.snapshot_kind,
                    market=request.market,
                    account_scope=request.account_scope,
                    origin="auto_trader_db",
                    reason="no_symbols_requested",
                    as_of=now,
                )
            ]
        try:
            flow = await self._query.get_investor_flow(
                symbols=symbols, market="kr", now=now
            )
        except Exception as exc:  # noqa: BLE001 — degrade rather than crash
            return [
                unavailable_result(
                    snapshot_kind=self.snapshot_kind,
                    market=request.market,
                    account_scope=request.account_scope,
                    origin="auto_trader_db",
                    reason=f"investor_flow query failed: {type(exc).__name__}: {exc}",
                    as_of=now,
                )
            ]

        overall = flow.freshness.overall
        if overall == "unavailable":
            return [
                unavailable_result(
                    snapshot_kind=self.snapshot_kind,
                    market=request.market,
                    account_scope=request.account_scope,
                    origin="auto_trader_db",
                    reason=flow.freshness.stale_reason or "unavailable",
                    as_of=now,
                )
            ]
        freshness_status = "fresh" if overall == "fresh" else "soft_stale"
        payload = {
            "market": "kr",
            "snapshot_date": (
                flow.snapshot_date.isoformat() if flow.snapshot_date else None
            ),
            "freshness": asdict(flow.freshness),
            "rows": [asdict(r) for r in flow.rows],
        }
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

- [ ] **Step 4: Run test to verify it passes + lint**

Run: `cd /Users/mgh3326/work/auto_trader.rob-398-s3 && uv run pytest tests/test_investor_flow_collector.py -v && uv run ruff check app/services/action_report/snapshot_backed/collectors/investor_flow.py tests/test_investor_flow_collector.py`
Expected: PASS (5 passed); ruff clean.

- [ ] **Step 5: Commit**

```bash
cd /Users/mgh3326/work/auto_trader.rob-398-s3
git add app/services/action_report/snapshot_backed/collectors/investor_flow.py tests/test_investor_flow_collector.py
git commit -m "feat(ROB-398): investor_flow collector (미등록 — Task 3에서 wire)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: investor_flow kind 도입 — 6곳 동기 + migration (원자적)

**Files:**
- Modify: `app/models/investment_snapshots.py`, `app/schemas/investment_snapshots.py`, `app/services/action_report/snapshot_backed/collectors/registry.py`, `app/services/investment_snapshots/policy.py`, `app/services/invest_data_source_contract.py`, `docs/invest/data-source-contract.md`
- Create: `alembic/versions/20260602_rob398s3_extend_snapshot_kind_investor_flow.py`
- Test: `tests/test_snapshot_kind_investor_flow.py`

이 6곳은 drift-guard(`test_collector_wired_kinds_match_runtime_registry` + doc matrix + `test_every_collector_kind_has_exactly_one_entry`)를 동시에 만족시키기 위해 **한 커밋**으로 묶는다.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_snapshot_kind_investor_flow.py
from unittest.mock import MagicMock

import pytest

from app.models.investment_snapshots import InvestmentSnapshot


def _check_sql():
    for c in InvestmentSnapshot.__table__.constraints:
        if getattr(c, "name", "") == "ck_investment_snapshots_snapshot_kind":
            return str(c.sqltext)
    raise AssertionError("snapshot_kind CHECK not found")


@pytest.mark.unit
def test_model_check_has_investor_flow_and_preserves_old():
    sql = _check_sql()
    assert "investor_flow" in sql
    for kind in (
        "portfolio", "market", "news", "symbol", "candidate_universe",
        "validated_run_card", "kr_market_ranking",
    ):
        assert kind in sql


@pytest.mark.unit
def test_schema_literal_has_investor_flow():
    from app.schemas.investment_snapshots import SnapshotKind
    from typing import get_args

    assert "investor_flow" in get_args(SnapshotKind)


@pytest.mark.unit
def test_drift_guard_contract_matches_runtime_registry():
    from app.services.action_report.snapshot_backed.collectors.registry import (
        production_collector_registry,
    )
    from app.services.invest_data_source_contract import collector_wired_kinds

    runtime = production_collector_registry(MagicMock()).list_kinds()
    contract = collector_wired_kinds()
    assert "investor_flow" in runtime
    assert "investor_flow" in contract
    assert contract == runtime
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/mgh3326/work/auto_trader.rob-398-s3 && uv run pytest tests/test_snapshot_kind_investor_flow.py -v`
Expected: FAIL — `investor_flow` not in CHECK / Literal / drift.

- [ ] **Step 3: 6곳 동기**

(a) `app/models/investment_snapshots.py` — CHECK 문자열 끝에 `,'investor_flow'` 추가:
```python
            "'validated_run_card','kr_market_ranking','investor_flow')",
```

(b) `app/schemas/investment_snapshots.py` — `SnapshotKind` Literal에 `"investor_flow"` 추가 (`"kr_market_ranking"` 다음):
```python
    "kr_market_ranking",
    "investor_flow",
```

(c) `registry.py` — import + 등록(`KrMarketRankingSnapshotCollector` 등록 다음 줄):
```python
from app.services.action_report.snapshot_backed.collectors.investor_flow import (
    InvestorFlowSnapshotCollector,
)
```
```python
    registry.register(InvestorFlowSnapshotCollector(session))
```

(d) `policy.py` — `kr_market_ranking` SnapshotKindPolicy 다음에 추가:
```python
        SnapshotKindPolicy(
            snapshot_kind="investor_flow",
            freshness=FreshnessPolicy(soft_ttl=_seconds(900), hard_ttl=_seconds(86400)),
            required=False,
            collector_timeout=_seconds(10),
        ),
```

(e) `app/services/invest_data_source_contract.py` — 기존 투자자 플로우 엔트리의 `collector_snapshot_kind` 를 set:
```python
        unavailable_label="stale",
        collector_snapshot_kind="investor_flow",  # ROB-398 Slice 3: investor_flow collector wired
    ),
```
(주석 `# read-model; folded into ...` 줄을 위 줄로 교체.)

(f) `docs/invest/data-source-contract.md` matrix 재렌더 — Step 4에서 스크립트로 수행.

- [ ] **Step 4: doc matrix 재렌더 + migration**

doc matrix 재렌더:
```bash
cd /Users/mgh3326/work/auto_trader.rob-398-s3 && uv run python - <<'PY'
from pathlib import Path
from app.services.invest_data_source_contract import render_contract_matrix_markdown
doc = Path("docs/invest/data-source-contract.md")
text = doc.read_text(encoding="utf-8")
BEGIN = ("<!-- BEGIN GENERATED: data-source-matrix "
         "(rendered from app/services/invest_data_source_contract.py; "
         "do not hand-edit) -->")
END = "<!-- END GENERATED: data-source-matrix -->"
pre, rest = text.split(BEGIN, 1)
_old, post = rest.split(END, 1)
doc.write_text(f"{pre}{BEGIN}\n{render_contract_matrix_markdown()}\n{END}{post}", encoding="utf-8")
print("re-rendered")
PY
```

현재 head 확인 후 migration 생성:
```bash
uv run alembic heads   # 단일 head → down_revision 에 사용
```

`alembic/versions/20260602_rob398s3_extend_snapshot_kind_investor_flow.py`:

```python
"""ROB-398 Slice 3 — add 'investor_flow' to investment_snapshots.snapshot_kind CHECK.

Pure additive CHECK extension. investor_flow collector emits rows with this kind;
existing rows unaffected. Mirrors 20260527_rob329 / 20260602_rob398s2.
Operator-gated: applied via ``alembic upgrade head``.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260602_rob398s3"
down_revision: str | None = "<CURRENT_HEAD>"  # `alembic heads` 실값으로 교체
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
    "'llm_input_frozen','pending_orders','validated_run_card',"
    "'kr_market_ranking'"
)
_NEW_KINDS = _OLD_KINDS + ",'investor_flow'"


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

- [ ] **Step 5: Run test to verify it passes + single-head + contract**

Run:
```bash
cd /Users/mgh3326/work/auto_trader.rob-398-s3
uv run pytest tests/test_snapshot_kind_investor_flow.py tests/test_invest_data_source_contract.py -v
uv run alembic heads
uv run pytest tests/test_us_candles_sync.py::test_revision_graph_has_single_final_head -v
```
Expected: 전부 PASS; `alembic heads` 단일.

- [ ] **Step 6: Commit**

```bash
cd /Users/mgh3326/work/auto_trader.rob-398-s3
git add app/models/investment_snapshots.py app/schemas/investment_snapshots.py \
  app/services/action_report/snapshot_backed/collectors/registry.py \
  app/services/investment_snapshots/policy.py app/services/invest_data_source_contract.py \
  docs/invest/data-source-contract.md \
  alembic/versions/20260602_rob398s3_extend_snapshot_kind_investor_flow.py \
  tests/test_snapshot_kind_investor_flow.py
git commit -m "feat(ROB-398): investor_flow snapshot_kind 도입 — 6곳 동기 + additive CHECK migration

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: 전체 검증

**Files:** (검증/회귀만)

- [ ] **Step 1: Slice 3 + 인접 회귀**

Run:
```bash
cd /Users/mgh3326/work/auto_trader.rob-398-s3
uv run pytest tests/test_investor_flow_query_service.py tests/test_investor_flow_collector.py tests/test_snapshot_kind_investor_flow.py tests/test_invest_data_source_contract.py -v
uv run pytest tests/ -k "collector_registry or snapshot_kind or investor_flow or policy or data_source_contract" -q
```
Expected: 전부 PASS.

- [ ] **Step 2: lint/format(전체) + import-contracts + single-head**

Run:
```bash
cd /Users/mgh3326/work/auto_trader.rob-398-s3
uv run ruff check app/ tests/
uv run ruff format --check app/ tests/
uv run pytest tests/test_import_contracts.py tests/test_us_candles_sync.py::test_revision_graph_has_single_final_head -q
```
Expected: PASS; ruff clean. (format --check 는 **app/ tests/ 전체** — 수정 파일 누락 방지, Slice 1/2 교훈.)

- [ ] **Step 3: (format 수정 시) 커밋**

```bash
cd /Users/mgh3326/work/auto_trader.rob-398-s3
uv run ruff format app/ tests/
git add -A && git commit -m "style(ROB-398): ruff format Slice 3

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review (작성자 체크)

**Spec coverage:**
- §3 read-model query_service + freshness → Task 1 ✅
- §4 freshness 일 단위(ttl_days) → Task 1 (`_derive_freshness`) ✅
- §5 collector(투자자 플로우, stale→soft_stale, degrade) → Task 2 ✅
- §6 6곳 동기 + additive migration → Task 3 (원자적) ✅
- §7 테스트(query freshness 4 / collector 5 / drift-guard·CHECK) → Task 1/2/3 ✅
- §8 비목표(체결강도 제외, 새 적재경로 없음, screener 미변경) → 준수 ✅

**Placeholder scan:** migration `down_revision`만 `<CURRENT_HEAD>` (Step 4가 `alembic heads`로 실값 치환 명시). 그 외 없음.

**Type consistency:** `InvestorFlow`/`InvestorFlowRow`/`Freshness`(Task1) → collector(Task2) `asdict`·`.freshness.overall` 일관. `get_investor_flow(symbols, market, now, ttl_days)` 시그니처가 collector 호출과 일치. `build_result`/`unavailable_result`/`CollectorRequest` 정확. `latest_by_symbols(market, symbols, as_of)` 기존 시그니처 정확.

**검증 시 주의:**
- Task 3 Step 4: `down_revision`을 `alembic heads` 실값으로 교체(미교체 시 멀티헤드 실패). rebase로 head 이동 시 재교체.
- 6곳을 **한 커밋**(Task 3)으로 묶어 drift-guard가 중간 상태에서 깨지지 않게 함 (Slice 2 교훈: registry만 먼저 등록 → contract 누락 → CI red).
- `build_result.freshness_status`는 `fresh/soft_stale/hard_stale/partial/unavailable`만 허용 → collector는 fresh/soft_stale/unavailable만 emit.

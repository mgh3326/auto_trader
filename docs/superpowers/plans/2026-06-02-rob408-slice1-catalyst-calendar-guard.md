# ROB-408 Slice 1 — catalyst 캘린더 foundation + upcoming-catalyst 가드 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 기존 `market_events` taxonomy를 catalyst 카테고리로 확장하고, catalyst read-model query_service와 순수 upcoming-catalyst 가드(positive→트림 경고 / negative→매수 경고)를 추가한다. 새 테이블/snapshot_kind/migration 없음.

**Architecture:** 신규 `app/services/market_events/catalyst/` 패키지. 순수 로직(polarity·guard·매핑·freshness)은 DB 없이 단위테스트. query_service는 reader DI(default = ORM select; raw_payload_json 접근 위해 직접 read)로 DB-free 테스트. catalyst 이벤트는 `market_events` 행이라 기존 `MarketEventsSnapshotCollector`(snapshot_kind="market")로 자동 노출 — snapshot/registry/policy/contract/migration 변경 0.

**Tech Stack:** Python 3.13, `@dataclass(frozen=True)`, pytest. 기존 `market_events`(taxonomy/repository/query_service/MarketEvent). 새 의존성 없음.

**참조 스펙:** `docs/superpowers/specs/2026-06-02-rob408-slice1-catalyst-calendar-guard-design.md`

기존 시그니처(확인됨):
- `taxonomy.CATEGORIES`(frozenset) + `validate_category(category)` (`unknown category` raise) — `app/services/market_events/taxonomy.py`
- `MarketEvent`: `category, market, symbol(nullable), title, event_date(date), source, source_url, raw_payload_json(dict|None), importance` — `app/models/market_events.py`
- `MarketEventsQueryService.list_for_range(from_date, to_date, *, category=None, market=None, source=None)` — **단일 category 필터만**; `MarketEventResponse`에는 `raw_payload_json` 없음 → catalyst는 ORM 직접 read.

---

## File Structure

- Modify `app/services/market_events/taxonomy.py` — `CATEGORIES`에 catalyst 카테고리 추가
- Create `app/services/market_events/catalyst/__init__.py` — 공개 re-export
- Create `app/services/market_events/catalyst/polarity.py` — `CATALYST_CATEGORIES`, `CATEGORY_POLARITY`, `resolve_polarity`
- Create `app/services/market_events/catalyst/contract.py` — `CatalystEvent`/`Freshness`/`UpcomingCatalysts`/`CatalystGuard`
- Create `app/services/market_events/catalyst/query_service.py` — `CatalystQueryService`(reader DI)
- Create `app/services/market_events/catalyst/guard.py` — `evaluate_catalyst_guard`
- Create tests: `tests/test_catalyst_taxonomy.py`, `tests/test_catalyst_polarity.py`, `tests/test_catalyst_query_service.py`, `tests/test_catalyst_guard.py`

---

## Task 1: taxonomy 확장 + CATALYST_CATEGORIES (`taxonomy.py`, `catalyst/polarity.py`)

**Files:**
- Modify: `app/services/market_events/taxonomy.py`
- Create: `app/services/market_events/catalyst/__init__.py` (빈 docstring; Task 5에서 re-export), `app/services/market_events/catalyst/polarity.py`
- Test: `tests/test_catalyst_taxonomy.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_catalyst_taxonomy.py
import pytest

from app.services.market_events.catalyst.polarity import CATALYST_CATEGORIES
from app.services.market_events.taxonomy import CATEGORIES, validate_category


@pytest.mark.unit
def test_catalyst_categories_added_to_taxonomy():
    for cat in ("conference", "corporate_event", "product_launch",
                "policy_regulation", "lockup_expiry", "index_rebalance"):
        assert cat in CATEGORIES
        validate_category(cat)  # 검증 통과(예외 없음)


@pytest.mark.unit
def test_existing_categories_preserved():
    for cat in ("earnings", "economic", "disclosure", "regulatory"):
        assert cat in CATEGORIES


@pytest.mark.unit
def test_catalyst_categories_constant_is_subset():
    assert CATALYST_CATEGORIES <= CATEGORIES
    assert "earnings" not in CATALYST_CATEGORIES  # earnings는 기존 catalyst-신규 아님
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/mgh3326/work/auto_trader.rob-408 && uv run pytest tests/test_catalyst_taxonomy.py -v`
Expected: FAIL — `ModuleNotFoundError: ...catalyst.polarity` / catalyst 카테고리 미존재.

- [ ] **Step 3: 구현**

`app/services/market_events/taxonomy.py` — `CATEGORIES` frozenset에 6종 추가:
```python
CATEGORIES: frozenset[str] = frozenset(
    {
        "earnings",
        "economic",
        "disclosure",
        "crypto_exchange_notice",
        "crypto_protocol",
        "tokenomics",
        "regulatory",
        # ROB-408: 비실적 촉매 카테고리
        "conference",
        "corporate_event",
        "product_launch",
        "policy_regulation",
        "lockup_expiry",
        "index_rebalance",
    }
)
```

```python
# app/services/market_events/catalyst/__init__.py
"""catalyst 캘린더 foundation + upcoming-catalyst 가드 (ROB-408 Slice 1)."""
```

```python
# app/services/market_events/catalyst/polarity.py
"""catalyst 카테고리 집합 + impact 극성 매핑 (ROB-408 Slice 1, 순수)."""

from __future__ import annotations

# ROB-408에서 추가된 비실적 촉매 카테고리 (taxonomy.CATEGORIES의 부분집합).
CATALYST_CATEGORIES: frozenset[str] = frozenset(
    {
        "conference",
        "corporate_event",
        "product_launch",
        "policy_regulation",
        "lockup_expiry",
        "index_rebalance",
    }
)

# category → 기본 극성. raw_payload impact_hint가 있으면 그것이 우선(resolve_polarity).
CATEGORY_POLARITY: dict[str, str] = {
    "conference": "positive",
    "product_launch": "positive",
    "index_rebalance": "positive",
    "policy_regulation": "negative",
    "lockup_expiry": "negative",
    "earnings": "neutral",
    "corporate_event": "neutral",
}

_VALID_POLARITY: frozenset[str] = frozenset({"positive", "negative", "neutral"})


def resolve_polarity(category: str, raw_payload: dict | None) -> str:
    """raw_payload['impact_hint'] ∈ {positive,negative,neutral} 우선,
    없으면 CATEGORY_POLARITY, 미지정 category면 'neutral'."""
    if raw_payload:
        hint = raw_payload.get("impact_hint")
        if hint in _VALID_POLARITY:
            return hint
    return CATEGORY_POLARITY.get(category, "neutral")
```

- [ ] **Step 4: Run test to verify it passes + lint**

Run: `cd /Users/mgh3326/work/auto_trader.rob-408 && uv run pytest tests/test_catalyst_taxonomy.py -v && uv run ruff check app/services/market_events/taxonomy.py app/services/market_events/catalyst/ tests/test_catalyst_taxonomy.py`
Expected: PASS (3 passed); ruff clean.

- [ ] **Step 5: 기존 market_events 회귀 확인 + Commit**

Run: `cd /Users/mgh3326/work/auto_trader.rob-408 && uv run pytest tests/ -k "market_event or taxonomy" -q`
Expected: PASS (taxonomy 확장이 기존 테스트 무회귀).

```bash
cd /Users/mgh3326/work/auto_trader.rob-408
git add app/services/market_events/taxonomy.py app/services/market_events/catalyst/__init__.py app/services/market_events/catalyst/polarity.py tests/test_catalyst_taxonomy.py
git commit -m "feat(ROB-408): market_events taxonomy에 catalyst 카테고리 6종 + CATALYST_CATEGORIES

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: impact 극성 단위테스트 (`polarity.py`)

**Files:**
- Test: `tests/test_catalyst_polarity.py`

(polarity.py는 Task 1에서 생성됨; 여기서 동작을 단위 고정.)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_catalyst_polarity.py
import pytest

from app.services.market_events.catalyst.polarity import resolve_polarity


@pytest.mark.unit
def test_category_default_polarity():
    assert resolve_polarity("conference", None) == "positive"
    assert resolve_polarity("product_launch", None) == "positive"
    assert resolve_polarity("index_rebalance", None) == "positive"
    assert resolve_polarity("policy_regulation", None) == "negative"
    assert resolve_polarity("lockup_expiry", None) == "negative"
    assert resolve_polarity("corporate_event", None) == "neutral"


@pytest.mark.unit
def test_raw_payload_impact_hint_overrides_category():
    assert resolve_polarity("conference", {"impact_hint": "negative"}) == "negative"
    assert resolve_polarity("policy_regulation", {"impact_hint": "positive"}) == "positive"


@pytest.mark.unit
def test_invalid_hint_falls_back_to_category():
    assert resolve_polarity("conference", {"impact_hint": "bogus"}) == "positive"


@pytest.mark.unit
def test_unknown_category_is_neutral():
    assert resolve_polarity("some_unknown_cat", None) == "neutral"
```

- [ ] **Step 2: Run test to verify it fails (or passes — polarity already implemented)**

Run: `cd /Users/mgh3326/work/auto_trader.rob-408 && uv run pytest tests/test_catalyst_polarity.py -v`
Expected: PASS (polarity.py가 Task 1에 구현됨). 만약 어떤 케이스 FAIL이면 `resolve_polarity` 로직 수정.

- [ ] **Step 3: (필요 시 수정) — 없으면 스킵**

- [ ] **Step 4: lint**

Run: `cd /Users/mgh3326/work/auto_trader.rob-408 && uv run ruff check tests/test_catalyst_polarity.py`
Expected: clean.

- [ ] **Step 5: Commit**

```bash
cd /Users/mgh3326/work/auto_trader.rob-408
git add tests/test_catalyst_polarity.py
git commit -m "test(ROB-408): resolve_polarity 단위(category 기본 + raw_payload override + unknown)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: catalyst read-model query_service (`contract.py`, `query_service.py`)

**Files:**
- Create: `app/services/market_events/catalyst/contract.py`
- Create: `app/services/market_events/catalyst/query_service.py`
- Test: `tests/test_catalyst_query_service.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_catalyst_query_service.py
import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.market_events.catalyst.query_service import CatalystQueryService

KST_NOW = dt.datetime(2026, 6, 2, 9, 0)
TODAY = KST_NOW.date()


def _row(symbol, category, *, days, title="t", raw_payload=None, source="manual"):
    return SimpleNamespace(
        symbol=symbol,
        category=category,
        title=title,
        event_date=TODAY + dt.timedelta(days=days),
        source=source,
        raw_payload_json=raw_payload,
        importance=None,
    )


def _service_with_rows(rows):
    async def reader(*, categories, from_date, to_date, market, symbols):
        out = [
            r for r in rows
            if r.category in categories
            and from_date <= r.event_date <= to_date
            and (symbols is None or r.symbol in symbols)
        ]
        return out
    return CatalystQueryService(session=None, reader=reader)


@pytest.mark.asyncio
async def test_within_days_filter_and_days_until_and_polarity():
    svc = _service_with_rows([
        _row("035420", "conference", days=3),       # in range, positive
        _row("005930", "lockup_expiry", days=30),    # out of range (>7)
    ])
    out = await svc.get_upcoming_catalysts(within_days=7, now=KST_NOW)
    assert [e.symbol for e in out.rows] == ["035420"]
    e = out.rows[0]
    assert e.category == "conference"
    assert e.days_until == 3
    assert e.polarity == "positive"
    assert out.freshness.overall == "fresh"


@pytest.mark.asyncio
async def test_raw_payload_polarity_override():
    svc = _service_with_rows([_row("035420", "conference", days=1, raw_payload={"impact_hint": "negative"})])
    out = await svc.get_upcoming_catalysts(within_days=7, now=KST_NOW)
    assert out.rows[0].polarity == "negative"


@pytest.mark.asyncio
async def test_symbols_filter():
    svc = _service_with_rows([
        _row("035420", "conference", days=2),
        _row("005930", "conference", days=2),
    ])
    out = await svc.get_upcoming_catalysts(symbols=["035420"], within_days=7, now=KST_NOW)
    assert [e.symbol for e in out.rows] == ["035420"]


@pytest.mark.asyncio
async def test_no_rows_unavailable():
    svc = _service_with_rows([])
    out = await svc.get_upcoming_catalysts(within_days=7, now=KST_NOW)
    assert out.rows == ()
    assert out.freshness.overall == "unavailable"
    assert out.freshness.stale_reason == "no_upcoming_catalysts"


@pytest.mark.asyncio
async def test_default_orm_reader_queries_catalyst_categories():
    # default reader(세션 직접) 커버: AsyncMock 세션이 catalyst 행을 반환.
    row = _row("035420", "conference", days=1)
    scalars = MagicMock()
    scalars.all.return_value = [row]
    result = MagicMock()
    result.scalars.return_value = scalars
    session = MagicMock()
    session.execute = AsyncMock(return_value=result)

    svc = CatalystQueryService(session=session)  # reader 없음 → default ORM reader
    out = await svc.get_upcoming_catalysts(symbols=["035420"], within_days=7, now=KST_NOW)
    assert out.rows[0].symbol == "035420"
    assert session.execute.await_count == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/mgh3326/work/auto_trader.rob-408 && uv run pytest tests/test_catalyst_query_service.py -v`
Expected: FAIL — `ModuleNotFoundError: ...query_service`.

- [ ] **Step 3: 구현**

```python
# app/services/market_events/catalyst/contract.py
"""catalyst read-model + 가드 계약 dataclass (ROB-408 Slice 1)."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass


@dataclass(frozen=True)
class CatalystEvent:
    symbol: str | None
    category: str
    title: str | None
    event_date: dt.date
    days_until: int
    polarity: str              # positive | negative | neutral
    source: str | None


@dataclass(frozen=True)
class Freshness:
    overall: str               # "fresh" | "unavailable"
    stale_reason: str | None


@dataclass(frozen=True)
class UpcomingCatalysts:
    market: str
    within_days: int
    rows: tuple[CatalystEvent, ...]
    freshness: Freshness


@dataclass(frozen=True)
class CatalystGuard:
    flag: str | None           # "upcoming_positive_catalyst" | "upcoming_negative_catalyst" | None
    nearest_days: int | None
    positive: tuple[CatalystEvent, ...]
    negative: tuple[CatalystEvent, ...]
    reason: str | None
```

```python
# app/services/market_events/catalyst/query_service.py
"""catalyst read-model query_service (ROB-408 Slice 1).

기존 market_events 위 read-only. catalyst 카테고리 + event_date 범위 행을 읽어
days_until·polarity·freshness 를 부착. raw_payload_json 접근 위해 ORM 직접 read
(MarketEventResponse는 raw_payload 미포함). reader DI로 DB-free 테스트.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Awaitable, Callable, Iterable, Sequence
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.market_events import MarketEvent
from app.services.market_events.catalyst.contract import (
    CatalystEvent,
    Freshness,
    UpcomingCatalysts,
)
from app.services.market_events.catalyst.polarity import (
    CATALYST_CATEGORIES,
    resolve_polarity,
)

_KST = ZoneInfo("Asia/Seoul")
ReaderFn = Callable[..., Awaitable[Sequence[object]]]


def _orm_reader(session: AsyncSession) -> ReaderFn:
    async def reader(*, categories, from_date, to_date, market, symbols):
        stmt = (
            select(MarketEvent)
            .where(
                MarketEvent.market == market,
                MarketEvent.category.in_(categories),
                MarketEvent.event_date >= from_date,
                MarketEvent.event_date <= to_date,
            )
            .order_by(MarketEvent.event_date.asc(), MarketEvent.symbol.asc())
        )
        if symbols:
            stmt = stmt.where(MarketEvent.symbol.in_(list(symbols)))
        rows = (await session.execute(stmt)).scalars().all()
        return list(rows)

    return reader


def _to_event(row: object, *, now_date: dt.date) -> CatalystEvent:
    return CatalystEvent(
        symbol=getattr(row, "symbol", None),
        category=row.category,
        title=getattr(row, "title", None),
        event_date=row.event_date,
        days_until=(row.event_date - now_date).days,
        polarity=resolve_polarity(row.category, getattr(row, "raw_payload_json", None)),
        source=getattr(row, "source", None),
    )


class CatalystQueryService:
    def __init__(self, session: AsyncSession | None, *, reader: ReaderFn | None = None):
        if reader is None and session is None:
            raise ValueError("session or reader required")
        self._reader = reader or _orm_reader(session)  # type: ignore[arg-type]

    async def get_upcoming_catalysts(
        self,
        *,
        symbols: Iterable[str] | None = None,
        market: str = "kr",
        within_days: int = 7,
        now: dt.datetime,
    ) -> UpcomingCatalysts:
        now_date = now.astimezone(_KST).date() if now.tzinfo else now.date()
        from_date = now_date
        to_date = now_date + dt.timedelta(days=within_days)
        symbols_list = list(symbols) if symbols is not None else None
        rows = await self._reader(
            categories=CATALYST_CATEGORIES,
            from_date=from_date,
            to_date=to_date,
            market=market,
            symbols=symbols_list,
        )
        events = tuple(_to_event(r, now_date=now_date) for r in rows)
        if not events:
            freshness = Freshness("unavailable", "no_upcoming_catalysts")
        else:
            freshness = Freshness("fresh", None)
        return UpcomingCatalysts(
            market=market,
            within_days=within_days,
            rows=events,
            freshness=freshness,
        )
```

Note: `now.astimezone(_KST)` 는 aware일 때만; 테스트는 naive `now` 사용하므로 `now.tzinfo` 분기로 안전.

- [ ] **Step 4: Run test to verify it passes + lint**

Run: `cd /Users/mgh3326/work/auto_trader.rob-408 && uv run pytest tests/test_catalyst_query_service.py -v && uv run ruff check app/services/market_events/catalyst/contract.py app/services/market_events/catalyst/query_service.py tests/test_catalyst_query_service.py`
Expected: PASS (5 passed); ruff clean.

- [ ] **Step 5: Commit**

```bash
cd /Users/mgh3326/work/auto_trader.rob-408
git add app/services/market_events/catalyst/contract.py app/services/market_events/catalyst/query_service.py tests/test_catalyst_query_service.py
git commit -m "feat(ROB-408): catalyst read-model query_service(within_days/days_until/polarity/freshness, reader DI)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: upcoming-catalyst 가드 (`guard.py`)

**Files:**
- Create: `app/services/market_events/catalyst/guard.py`
- Test: `tests/test_catalyst_guard.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_catalyst_guard.py
import datetime as dt

import pytest

from app.services.market_events.catalyst.contract import CatalystEvent
from app.services.market_events.catalyst.guard import evaluate_catalyst_guard


def _ev(symbol, polarity, days, category="conference"):
    return CatalystEvent(
        symbol=symbol, category=category, title="t",
        event_date=dt.date(2026, 6, 2) + dt.timedelta(days=days),
        days_until=days, polarity=polarity, source="manual",
    )


@pytest.mark.unit
def test_trim_with_positive_catalyst_flags():
    g = evaluate_catalyst_guard([_ev("035420", "positive", 3)], side="trim", within_days=7)
    assert g.flag == "upcoming_positive_catalyst"
    assert g.nearest_days == 3
    assert len(g.positive) == 1
    assert g.reason


@pytest.mark.unit
def test_buy_with_negative_catalyst_flags():
    g = evaluate_catalyst_guard([_ev("005930", "negative", 2, category="policy_regulation")], side="buy", within_days=7)
    assert g.flag == "upcoming_negative_catalyst"
    assert g.nearest_days == 2


@pytest.mark.unit
def test_trim_with_only_negative_no_flag():
    g = evaluate_catalyst_guard([_ev("005930", "negative", 2, category="lockup_expiry")], side="trim", within_days=7)
    assert g.flag is None


@pytest.mark.unit
def test_out_of_window_no_flag():
    g = evaluate_catalyst_guard([_ev("035420", "positive", 30)], side="trim", within_days=7)
    assert g.flag is None


@pytest.mark.unit
def test_deterministic():
    events = [_ev("035420", "positive", 5), _ev("000660", "positive", 2)]
    a = evaluate_catalyst_guard(events, side="trim", within_days=7)
    b = evaluate_catalyst_guard(events, side="trim", within_days=7)
    assert a == b
    assert a.nearest_days == 2  # 가장 가까운 positive
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/mgh3326/work/auto_trader.rob-408 && uv run pytest tests/test_catalyst_guard.py -v`
Expected: FAIL — `ModuleNotFoundError: ...guard`.

- [ ] **Step 3: 구현**

```python
# app/services/market_events/catalyst/guard.py
"""upcoming-catalyst 트림/매수 가드 (ROB-408 Slice 1, 순수 함수).

trim/sell 전 positive 촉매가 D-N 내면 경고(이벤트 후 재평가);
buy/add 전 negative 촉매가 D-N 내면 경고.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.services.market_events.catalyst.contract import CatalystEvent, CatalystGuard

_TRIM_SIDES = frozenset({"trim", "sell"})
_BUY_SIDES = frozenset({"buy", "add"})


def evaluate_catalyst_guard(
    events: Sequence[CatalystEvent], *, side: str, within_days: int
) -> CatalystGuard:
    in_window = [e for e in events if 0 <= e.days_until <= within_days]
    positive = tuple(
        sorted(
            (e for e in in_window if e.polarity == "positive"),
            key=lambda e: (e.days_until, e.symbol or ""),
        )
    )
    negative = tuple(
        sorted(
            (e for e in in_window if e.polarity == "negative"),
            key=lambda e: (e.days_until, e.symbol or ""),
        )
    )

    flag: str | None = None
    nearest_days: int | None = None
    reason: str | None = None

    if side in _TRIM_SIDES and positive:
        flag = "upcoming_positive_catalyst"
        nearest_days = positive[0].days_until
        reason = "임박 positive 촉매 — 이벤트 후 재평가 권고"
    elif side in _BUY_SIDES and negative:
        flag = "upcoming_negative_catalyst"
        nearest_days = negative[0].days_until
        reason = "임박 negative 촉매 — 매수 전 재확인 권고"

    return CatalystGuard(
        flag=flag,
        nearest_days=nearest_days,
        positive=positive,
        negative=negative,
        reason=reason,
    )
```

- [ ] **Step 4: Run test to verify it passes + lint**

Run: `cd /Users/mgh3326/work/auto_trader.rob-408 && uv run pytest tests/test_catalyst_guard.py -v && uv run ruff check app/services/market_events/catalyst/guard.py tests/test_catalyst_guard.py`
Expected: PASS (5 passed); ruff clean.

- [ ] **Step 5: Commit**

```bash
cd /Users/mgh3326/work/auto_trader.rob-408
git add app/services/market_events/catalyst/guard.py tests/test_catalyst_guard.py
git commit -m "feat(ROB-408): upcoming-catalyst 가드(trim+positive / buy+negative, 순수)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: 패키지 공개 표면 + 전체 검증

**Files:**
- Modify: `app/services/market_events/catalyst/__init__.py`

- [ ] **Step 1: __init__ re-export**

```python
# app/services/market_events/catalyst/__init__.py
"""catalyst 캘린더 foundation + upcoming-catalyst 가드 (ROB-408 Slice 1)."""

from app.services.market_events.catalyst.contract import (
    CatalystEvent,
    CatalystGuard,
    Freshness,
    UpcomingCatalysts,
)
from app.services.market_events.catalyst.guard import evaluate_catalyst_guard
from app.services.market_events.catalyst.polarity import (
    CATALYST_CATEGORIES,
    CATEGORY_POLARITY,
    resolve_polarity,
)
from app.services.market_events.catalyst.query_service import CatalystQueryService

__all__ = [
    "CATALYST_CATEGORIES",
    "CATEGORY_POLARITY",
    "CatalystEvent",
    "CatalystGuard",
    "CatalystQueryService",
    "Freshness",
    "UpcomingCatalysts",
    "evaluate_catalyst_guard",
    "resolve_polarity",
]
```

- [ ] **Step 2: 전체 테스트 + lint/format(전체) + import-contracts**

Run:
```bash
cd /Users/mgh3326/work/auto_trader.rob-408
uv run pytest tests/test_catalyst_taxonomy.py tests/test_catalyst_polarity.py tests/test_catalyst_query_service.py tests/test_catalyst_guard.py -v
uv run pytest tests/ -k "market_event or taxonomy" -q
uv run ruff check app/ tests/
uv run ruff format --check app/ tests/
uv run pytest tests/test_import_contracts.py -q
```
Expected: 전부 PASS; ruff check/format clean(**app/ tests/ 전체** — Slice 교훈). import-contracts: catalyst 패키지는 market_events 내부 + models import만 → 위반 없음.

- [ ] **Step 3: Commit**

```bash
cd /Users/mgh3326/work/auto_trader.rob-408
git add app/services/market_events/catalyst/__init__.py
git commit -m "feat(ROB-408): catalyst 패키지 공개 표면 re-export

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review (작성자 체크)

**Spec coverage:**
- §3 taxonomy 확장 → Task 1 ✅
- §4 impact 극성(매핑 + raw override) → Task 1(구현)+Task 2(테스트) ✅
- §5 catalyst read-model query_service(within_days/days_until/polarity/freshness/symbols) → Task 3 ✅
- §6 upcoming-catalyst 가드(trim+positive/buy+negative, 순수) → Task 4 ✅
- §7 노출(기존 market collector, snapshot 변경 0) → 어떤 Task도 snapshot/registry/policy/contract/migration 미변경 ✅
- §8 테스트 5영역 → Task 1~4 ✅
- §9 비목표(classifier 배선/실소스/새 테이블·kind·migration) → 준수 ✅

**Placeholder scan:** placeholder 없음. 모든 step 실제 코드/명령/기대.

**Type consistency:** `CatalystEvent`/`Freshness`/`UpcomingCatalysts`/`CatalystGuard`(contract, Task3) → query_service(Task3)·guard(Task4)에서 일관. `resolve_polarity`/`CATALYST_CATEGORIES`(polarity, Task1) → query_service에서 사용. `get_upcoming_catalysts(symbols, market, within_days, now)` / `evaluate_catalyst_guard(events, side, within_days)` 시그니처 테스트와 일치.

**검증 시 주의:**
- query_service default ORM reader는 Task 3 `test_default_orm_reader_queries_catalyst_categories`(AsyncMock 세션)로 커버 → codecov/patch 갭 예방(Slice 1 교훈).
- snapshot_kind 6곳 동기 **불필요**(새 kind 없음 — 기존 `market` 흐름). drift-guard/CHECK migration 무관.
- format-check는 `app/ tests/` 전체로.

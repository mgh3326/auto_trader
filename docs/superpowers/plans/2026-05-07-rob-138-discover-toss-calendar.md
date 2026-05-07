# ROB-138: Discover Toss-style Event Calendar Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Toss-style weekly event calendar to the Discover tab — group market events by day, prioritize held / watched / major, and limit raw earnings spam — backed by a new summary endpoint.

**Architecture:** Build on the ROB-128 market events foundation. Add (1) a holdings/watchlist resolver, (2) a prioritization module, (3) a `discover_calendar_service` that groups events by day with hidden_count, (4) a `GET /trading/api/market-events/discover-calendar` endpoint, (5) frontend `DiscoverCalendarCard` component with date rail / weekly sections / tab filtering / badges. Replace existing `TodayEventCard` usage in `DiscoverPage`.

**Tech Stack:** FastAPI, SQLAlchemy async, Pydantic v2, React 18, TypeScript, Vitest, Pytest.

**Linear:** https://linear.app/mgh3326/issue/ROB-138

**Branch:** `linear-subagent` (already on it; worktree at `/Users/robin/.superset/worktrees/auto_trader/linear-subagent`)

---

## File Map

**Backend (create):**
- `app/services/market_events/user_context.py` — `UserEventContext`, `get_user_event_context(db, user_id)`
- `app/services/market_events/prioritization.py` — `MAJOR_TICKERS`, `compute_priority(event, ctx)`, `Priority` enum, `PRIORITY_ORDER`
- `app/services/market_events/discover_calendar.py` — `DiscoverCalendarService.build(...)`
- `app/schemas/market_events_calendar.py` — `DiscoverCalendarEvent`, `DiscoverCalendarDay`, `DiscoverCalendarResponse`
- `tests/services/test_market_events_user_context.py`
- `tests/services/test_market_events_prioritization.py`
- `tests/services/test_market_events_discover_calendar.py`
- `tests/test_market_events_discover_calendar_router.py`

**Backend (modify):**
- `app/routers/market_events.py` — register new endpoint

**Frontend (create):**
- `frontend/invest/src/components/discover/DiscoverCalendarCard.tsx`
- `frontend/invest/src/hooks/useDiscoverCalendar.ts`
- `frontend/invest/src/__tests__/DiscoverCalendarCard.test.tsx`
- `frontend/invest/src/__tests__/discoverCalendar.api.test.ts`

**Frontend (modify):**
- `frontend/invest/src/types/marketEvents.ts` — add Discover calendar types
- `frontend/invest/src/api/marketEvents.ts` — add `fetchDiscoverCalendar`
- `frontend/invest/src/pages/DiscoverPage.tsx` — replace `<TodayEventCard />` with `<DiscoverCalendarCard />`

**Frontend (delete):**
- `frontend/invest/src/components/discover/TodayEventCard.tsx`
- `frontend/invest/src/__tests__/TodayEventCard.test.tsx`
- (Note: `useMarketEventsToday.ts` and `fetchMarketEventsToday` and the `marketEvents.api.test.ts` stay — they remain valid clients of `/today`. Existing integration tests for `/today` are unchanged.)

---

## Conventions to Follow

- **TDD:** failing test → run → minimal impl → run → commit. Use `pytest` for backend, `vitest` for frontend.
- **No mock DB in integration tests:** existing pattern uses real `db_session` fixture (see `tests/test_market_events_router.py`).
- **Auth:** `current_user: Annotated[User, Depends(get_authenticated_user)]` — override with `lambda: SimpleNamespace(id=7)` in tests (existing pattern).
- **Markdown commits:** Korean-style, `feat(market_events): ...` or `feat(invest): ...`. Include `Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>`.
- **No DB migration** required. All new behavior is computed at query time over existing tables.

---

## Task 1: Backend `user_context` resolver

**Files:**
- Create: `app/services/market_events/user_context.py`
- Create: `tests/services/test_market_events_user_context.py`

**Purpose:** Given `user_id`, return ticker sets the user holds and watches, used by prioritization to decide badges/order. Tickers are normalized to uppercase, `.`-form (DB canonical).

- [ ] **Step 1: Write failing test**

```python
# tests/services/test_market_events_user_context.py
"""Unit tests for user_context resolver (ROB-138)."""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import delete

from app.models.manual_holdings import (
    BrokerAccount,
    BrokerType,
    ManualHolding,
    MarketType,
)
from app.models.trading import (
    Exchange,
    Instrument,
    InstrumentType,
    User,
    UserRole,
    UserWatchItem,
)
from app.services.market_events.user_context import (
    UserEventContext,
    get_user_event_context,
)


@pytest_asyncio.fixture(autouse=True)
async def _clean(db_session):
    await db_session.execute(delete(ManualHolding))
    await db_session.execute(delete(BrokerAccount))
    await db_session.execute(delete(UserWatchItem))
    await db_session.execute(delete(Instrument))
    await db_session.execute(delete(Exchange))
    await db_session.execute(delete(User))
    await db_session.commit()
    yield


@pytest.mark.integration
async def test_returns_empty_sets_for_unknown_user(db_session):
    ctx = await get_user_event_context(db_session, user_id=999)
    assert ctx == UserEventContext(held_tickers=frozenset(), watched_tickers=frozenset())


@pytest.mark.integration
async def test_collects_manual_holdings_for_user(db_session):
    user = User(id=1, role=UserRole.viewer, tz="UTC", base_currency="KRW")
    db_session.add(user)
    await db_session.flush()
    acct = BrokerAccount(user_id=1, broker_type=BrokerType.toss, account_name="t")
    db_session.add(acct)
    await db_session.flush()
    db_session.add_all([
        ManualHolding(
            broker_account_id=acct.id,
            ticker="AAPL",
            market_type=MarketType.us,
            quantity=1,
            avg_price=1,
        ),
        ManualHolding(
            broker_account_id=acct.id,
            ticker="brk.b",
            market_type=MarketType.us,
            quantity=1,
            avg_price=1,
        ),
    ])
    await db_session.commit()

    ctx = await get_user_event_context(db_session, user_id=1)
    assert ctx.held_tickers == frozenset({"AAPL", "BRK.B"})


@pytest.mark.integration
async def test_collects_watchlist_via_instruments(db_session):
    user = User(id=2, role=UserRole.viewer, tz="UTC", base_currency="KRW")
    db_session.add(user)
    inst = Instrument(
        symbol="tsla",
        type=InstrumentType.stock,
        base_currency="USD",
    )
    db_session.add(inst)
    await db_session.flush()
    db_session.add(UserWatchItem(user_id=2, instrument_id=inst.id))
    await db_session.commit()

    ctx = await get_user_event_context(db_session, user_id=2)
    assert ctx.watched_tickers == frozenset({"TSLA"})


@pytest.mark.integration
async def test_held_user_filtered_by_user_id(db_session):
    """Held tickers are scoped to the requesting user only."""
    u1 = User(id=10, role=UserRole.viewer, tz="UTC", base_currency="KRW")
    u2 = User(id=11, role=UserRole.viewer, tz="UTC", base_currency="KRW")
    db_session.add_all([u1, u2])
    await db_session.flush()
    acct1 = BrokerAccount(user_id=10, broker_type=BrokerType.toss, account_name="a")
    acct2 = BrokerAccount(user_id=11, broker_type=BrokerType.toss, account_name="b")
    db_session.add_all([acct1, acct2])
    await db_session.flush()
    db_session.add_all([
        ManualHolding(
            broker_account_id=acct1.id,
            ticker="MSFT",
            market_type=MarketType.us,
            quantity=1,
            avg_price=1,
        ),
        ManualHolding(
            broker_account_id=acct2.id,
            ticker="NVDA",
            market_type=MarketType.us,
            quantity=1,
            avg_price=1,
        ),
    ])
    await db_session.commit()

    ctx10 = await get_user_event_context(db_session, user_id=10)
    assert ctx10.held_tickers == frozenset({"MSFT"})
    ctx11 = await get_user_event_context(db_session, user_id=11)
    assert ctx11.held_tickers == frozenset({"NVDA"})
```

- [ ] **Step 2: Run tests, expect failure**

```bash
uv run pytest tests/services/test_market_events_user_context.py -v
```
Expected: ImportError on `app.services.market_events.user_context`.

- [ ] **Step 3: Write minimal implementation**

```python
# app/services/market_events/user_context.py
"""Per-user holdings/watchlist resolver for market event prioritization (ROB-138)."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.manual_holdings import BrokerAccount, ManualHolding
from app.models.trading import Instrument, UserWatchItem


@dataclass(frozen=True)
class UserEventContext:
    held_tickers: frozenset[str]
    watched_tickers: frozenset[str]


async def get_user_event_context(
    db: AsyncSession, *, user_id: int
) -> UserEventContext:
    held_stmt = (
        select(ManualHolding.ticker)
        .join(BrokerAccount, BrokerAccount.id == ManualHolding.broker_account_id)
        .where(BrokerAccount.user_id == user_id)
    )
    held = {t.upper() for (t,) in (await db.execute(held_stmt)).all() if t}

    watched_stmt = (
        select(Instrument.symbol)
        .join(UserWatchItem, UserWatchItem.instrument_id == Instrument.id)
        .where(UserWatchItem.user_id == user_id, UserWatchItem.is_active.is_(True))
    )
    watched = {s.upper() for (s,) in (await db.execute(watched_stmt)).all() if s}

    return UserEventContext(
        held_tickers=frozenset(held),
        watched_tickers=frozenset(watched),
    )
```

> Note: `get_user_event_context` accepts `user_id` as a keyword to match the test call signature.

- [ ] **Step 4: Run tests, expect pass**

```bash
uv run pytest tests/services/test_market_events_user_context.py -v
```
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add app/services/market_events/user_context.py tests/services/test_market_events_user_context.py
git commit -m "feat(market_events): add per-user held/watched ticker resolver (ROB-138)"
```

---

## Task 2: Backend `prioritization` module

**Files:**
- Create: `app/services/market_events/prioritization.py`
- Create: `tests/services/test_market_events_prioritization.py`

**Purpose:** Decide each event's priority tier (`held > watched > major > high_importance > medium_importance > other`) and an associated badge label.

- [ ] **Step 1: Write failing test**

```python
# tests/services/test_market_events_prioritization.py
"""Unit tests for market event prioritization (ROB-138)."""

from __future__ import annotations

from datetime import date

import pytest

from app.schemas.market_events import MarketEventResponse
from app.services.market_events.prioritization import (
    MAJOR_TICKERS,
    Priority,
    compute_priority,
)
from app.services.market_events.user_context import UserEventContext


def _evt(**kw) -> MarketEventResponse:
    base = dict(
        category="earnings",
        market="us",
        symbol=None,
        event_date=date(2026, 5, 7),
        source="finnhub",
        importance=None,
    )
    base.update(kw)
    return MarketEventResponse(**base)


def _ctx(held: set[str] | None = None, watched: set[str] | None = None) -> UserEventContext:
    return UserEventContext(
        held_tickers=frozenset(held or set()),
        watched_tickers=frozenset(watched or set()),
    )


@pytest.mark.unit
def test_held_beats_watched_and_major():
    ctx = _ctx(held={"AAPL"}, watched={"AAPL"})
    p = compute_priority(_evt(symbol="AAPL"), ctx)
    assert p == Priority.HELD


@pytest.mark.unit
def test_watched_beats_major():
    ctx = _ctx(watched={"AAPL"})
    p = compute_priority(_evt(symbol="AAPL"), ctx)
    assert p == Priority.WATCHED


@pytest.mark.unit
def test_major_when_in_allowlist():
    assert "AAPL" in MAJOR_TICKERS["us"]
    p = compute_priority(_evt(symbol="AAPL"), _ctx())
    assert p == Priority.MAJOR


@pytest.mark.unit
def test_high_importance_when_economic_high():
    p = compute_priority(_evt(category="economic", market="global", importance=3), _ctx())
    assert p == Priority.HIGH_IMPORTANCE


@pytest.mark.unit
def test_medium_importance_when_economic_medium():
    p = compute_priority(_evt(category="economic", market="global", importance=2), _ctx())
    assert p == Priority.MEDIUM_IMPORTANCE


@pytest.mark.unit
def test_other_for_random_earnings():
    p = compute_priority(_evt(symbol="OBSCURE_TICKER_123"), _ctx())
    assert p == Priority.OTHER


@pytest.mark.unit
def test_symbol_normalized_for_match():
    """Holdings stored as `BRK.B`; events sometimes carry `BRK.B`. Match case-insensitive."""
    ctx = _ctx(held={"BRK.B"})
    assert compute_priority(_evt(symbol="brk.b"), ctx) == Priority.HELD
```

- [ ] **Step 2: Run tests, expect failure**

```bash
uv run pytest tests/services/test_market_events_prioritization.py -v
```

- [ ] **Step 3: Write minimal implementation**

```python
# app/services/market_events/prioritization.py
"""Market event prioritization logic for Discover calendar (ROB-138).

Priority tiers (high → low):
  HELD              : event symbol in user holdings
  WATCHED           : event symbol in user watchlist
  MAJOR             : event symbol in market-specific allowlist (top liquid names)
  HIGH_IMPORTANCE   : economic / disclosure with importance >= 3
  MEDIUM_IMPORTANCE : economic / disclosure with importance == 2
  OTHER             : everything else
"""

from __future__ import annotations

from enum import IntEnum

from app.schemas.market_events import MarketEventResponse
from app.services.market_events.user_context import UserEventContext


class Priority(IntEnum):
    HELD = 0
    WATCHED = 1
    MAJOR = 2
    HIGH_IMPORTANCE = 3
    MEDIUM_IMPORTANCE = 4
    OTHER = 5


# Curated allowlists. Keep small — the goal is a "default-visible" tier.
MAJOR_TICKERS: dict[str, frozenset[str]] = {
    "us": frozenset(
        {
            "AAPL", "MSFT", "GOOGL", "GOOG", "AMZN", "NVDA", "META", "TSLA",
            "AVGO", "BRK.B", "LLY", "JPM", "V", "UNH", "XOM", "MA", "WMT",
            "JNJ", "PG", "ORCL", "HD", "BAC", "ABBV", "KO", "PEP", "CVX",
            "MRK", "COST", "AMD", "NFLX", "ADBE", "CRM", "DIS", "PFE",
        }
    ),
    "kr": frozenset(
        {
            "005930", "000660", "035420", "207940", "005380", "035720",
            "051910", "006400", "000270", "068270", "105560",
        }
    ),
    "crypto": frozenset({"BTC", "ETH", "SOL", "XRP", "BNB"}),
    "global": frozenset(),
}


def _norm(symbol: str | None) -> str | None:
    if symbol is None:
        return None
    return symbol.strip().upper() or None


def compute_priority(
    event: MarketEventResponse, ctx: UserEventContext
) -> Priority:
    sym = _norm(event.symbol)
    if sym is not None:
        if sym in ctx.held_tickers:
            return Priority.HELD
        if sym in ctx.watched_tickers:
            return Priority.WATCHED
        major = MAJOR_TICKERS.get(event.market, frozenset())
        if sym in major:
            return Priority.MAJOR

    importance = event.importance or 0
    if importance >= 3:
        return Priority.HIGH_IMPORTANCE
    if importance == 2:
        return Priority.MEDIUM_IMPORTANCE
    return Priority.OTHER
```

- [ ] **Step 4: Run tests, expect pass**

```bash
uv run pytest tests/services/test_market_events_prioritization.py -v
```

- [ ] **Step 5: Commit**

```bash
git add app/services/market_events/prioritization.py tests/services/test_market_events_prioritization.py
git commit -m "feat(market_events): add Discover calendar prioritization tiers (ROB-138)"
```

---

## Task 3: Backend `discover_calendar` service + schemas

**Files:**
- Create: `app/schemas/market_events_calendar.py`
- Create: `app/services/market_events/discover_calendar.py`
- Create: `tests/services/test_market_events_discover_calendar.py`

**Purpose:** Build the UI grouping. Given a date range, query events via `MarketEventsQueryService`, prioritize, group by day, limit per-day visible to 8, sort tabs, compute headline + week_label, surface `hidden_count`.

- [ ] **Step 1: Write failing test**

```python
# tests/services/test_market_events_discover_calendar.py
"""Unit tests for DiscoverCalendarService (ROB-138)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from unittest.mock import AsyncMock

import pytest

from app.schemas.market_events import (
    MarketEventResponse,
    MarketEventValueResponse,
    MarketEventsRangeResponse,
)
from app.services.market_events.discover_calendar import (
    DiscoverCalendarService,
    PER_DAY_VISIBLE_LIMIT,
)
from app.services.market_events.user_context import UserEventContext


def _evt(symbol: str, *, importance: int | None = None, category: str = "earnings",
         d: date = date(2026, 5, 7), eps: tuple[str, str] | None = None,
         title: str | None = None, time_hint: str | None = None,
         market: str = "us") -> MarketEventResponse:
    values = []
    if eps is not None:
        values.append(MarketEventValueResponse(
            metric_name="eps",
            actual=eps[0],
            forecast=eps[1],
        ))
    return MarketEventResponse(
        category=category,
        market=market,
        symbol=symbol,
        event_date=d,
        source="finnhub",
        importance=importance,
        title=title,
        time_hint=time_hint,
        values=values,
    )


@pytest.mark.unit
async def test_groups_events_by_date_and_marks_today():
    query = AsyncMock()
    query.list_for_range = AsyncMock(return_value=MarketEventsRangeResponse(
        from_date=date(2026, 5, 4),
        to_date=date(2026, 5, 10),
        count=2,
        events=[
            _evt("AAPL", d=date(2026, 5, 7), title="AAPL earnings"),
            _evt("MSFT", d=date(2026, 5, 8), title="MSFT earnings"),
        ],
    ))
    svc = DiscoverCalendarService(query_service=query)
    resp = await svc.build(
        from_date=date(2026, 5, 4),
        to_date=date(2026, 5, 10),
        today=date(2026, 5, 7),
        ctx=UserEventContext(frozenset(), frozenset()),
        tab="all",
    )
    assert [d.date for d in resp.days] == [
        date(2026, 5, 4), date(2026, 5, 5), date(2026, 5, 6),
        date(2026, 5, 7), date(2026, 5, 8), date(2026, 5, 9), date(2026, 5, 10),
    ]
    today_day = next(d for d in resp.days if d.date == date(2026, 5, 7))
    assert today_day.is_today is True
    assert today_day.weekday in {"목", "Thu"}  # ko_KR or default
    assert len(today_day.events) == 1
    assert today_day.events[0].title.startswith("AAPL")


@pytest.mark.unit
async def test_held_events_render_with_held_badge_and_first():
    query = AsyncMock()
    query.list_for_range = AsyncMock(return_value=MarketEventsRangeResponse(
        from_date=date(2026, 5, 7),
        to_date=date(2026, 5, 7),
        count=2,
        events=[
            _evt("OBSCURE", title="Obscure earnings"),
            _evt("AAPL", title="AAPL earnings"),
        ],
    ))
    svc = DiscoverCalendarService(query_service=query)
    ctx = UserEventContext(frozenset({"AAPL"}), frozenset())
    resp = await svc.build(
        from_date=date(2026, 5, 7),
        to_date=date(2026, 5, 7),
        today=date(2026, 5, 7),
        ctx=ctx,
        tab="all",
    )
    day = resp.days[0]
    assert day.events[0].badge == "보유"
    assert day.events[0].priority == "held"
    assert day.events[0].title.startswith("AAPL")


@pytest.mark.unit
async def test_per_day_visible_limit_applies_and_counts_hidden():
    """When events exceed PER_DAY_VISIBLE_LIMIT, surplus is collapsed into hidden_count."""
    events = [_evt(f"T{i}", title=f"T{i} earnings") for i in range(20)]
    query = AsyncMock()
    query.list_for_range = AsyncMock(return_value=MarketEventsRangeResponse(
        from_date=date(2026, 5, 7),
        to_date=date(2026, 5, 7),
        count=20,
        events=events,
    ))
    svc = DiscoverCalendarService(query_service=query)
    resp = await svc.build(
        from_date=date(2026, 5, 7),
        to_date=date(2026, 5, 7),
        today=date(2026, 5, 7),
        ctx=UserEventContext(frozenset(), frozenset()),
        tab="all",
    )
    day = resp.days[0]
    assert len(day.events) == PER_DAY_VISIBLE_LIMIT
    assert day.hidden_count == 20 - PER_DAY_VISIBLE_LIMIT


@pytest.mark.unit
async def test_tab_economic_filters_to_economic_only():
    query = AsyncMock()
    query.list_for_range = AsyncMock(return_value=MarketEventsRangeResponse(
        from_date=date(2026, 5, 7),
        to_date=date(2026, 5, 7),
        count=2,
        events=[
            _evt("AAPL", category="earnings"),
            _evt(None, category="economic", market="global", importance=3,
                 title="US CPI"),
        ],
    ))
    svc = DiscoverCalendarService(query_service=query)
    resp = await svc.build(
        from_date=date(2026, 5, 7),
        to_date=date(2026, 5, 7),
        today=date(2026, 5, 7),
        ctx=UserEventContext(frozenset(), frozenset()),
        tab="economic",
    )
    titles = [e.title for d in resp.days for e in d.events]
    assert "US CPI" in titles
    assert all("AAPL" not in (t or "") for t in titles)


@pytest.mark.unit
async def test_subtitle_for_earnings_uses_eps_actual_and_forecast():
    query = AsyncMock()
    query.list_for_range = AsyncMock(return_value=MarketEventsRangeResponse(
        from_date=date(2026, 5, 7),
        to_date=date(2026, 5, 7),
        count=1,
        events=[_evt("IONQ", title="IonQ earnings", eps=("-0.34", "-0.52"))],
    ))
    svc = DiscoverCalendarService(query_service=query)
    resp = await svc.build(
        from_date=date(2026, 5, 7),
        to_date=date(2026, 5, 7),
        today=date(2026, 5, 7),
        ctx=UserEventContext(frozenset(), frozenset()),
        tab="all",
    )
    sub = resp.days[0].events[0].subtitle
    assert sub is not None
    assert "-0.34" in sub
    assert "-0.52" in sub


@pytest.mark.unit
async def test_headline_includes_count_when_high_importance_present():
    query = AsyncMock()
    query.list_for_range = AsyncMock(return_value=MarketEventsRangeResponse(
        from_date=date(2026, 5, 7),
        to_date=date(2026, 5, 7),
        count=1,
        events=[_evt(None, category="economic", market="global", importance=3,
                     title="US CPI")],
    ))
    svc = DiscoverCalendarService(query_service=query)
    resp = await svc.build(
        from_date=date(2026, 5, 7),
        to_date=date(2026, 5, 7),
        today=date(2026, 5, 7),
        ctx=UserEventContext(frozenset(), frozenset()),
        tab="all",
    )
    assert resp.headline is not None and "주요" in resp.headline


@pytest.mark.unit
async def test_week_label_uses_korean_format():
    query = AsyncMock()
    query.list_for_range = AsyncMock(return_value=MarketEventsRangeResponse(
        from_date=date(2026, 5, 4),
        to_date=date(2026, 5, 10),
        count=0,
        events=[],
    ))
    svc = DiscoverCalendarService(query_service=query)
    resp = await svc.build(
        from_date=date(2026, 5, 4),
        to_date=date(2026, 5, 10),
        today=date(2026, 5, 7),
        ctx=UserEventContext(frozenset(), frozenset()),
        tab="all",
    )
    assert resp.week_label == "5월 1주차"
```

- [ ] **Step 2: Run tests, expect failures**

```bash
uv run pytest tests/services/test_market_events_discover_calendar.py -v
```

- [ ] **Step 3: Write schemas**

```python
# app/schemas/market_events_calendar.py
"""Pydantic response schemas for Discover calendar (ROB-138)."""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field


class DiscoverCalendarEvent(BaseModel):
    title: str
    badge: str | None = None
    category: str
    market: str
    symbol: str | None = None
    subtitle: str | None = None
    time_label: str | None = None
    priority: str
    source_event_id: str | None = None


class DiscoverCalendarDay(BaseModel):
    date: date
    weekday: str
    is_today: bool
    events: list[DiscoverCalendarEvent] = Field(default_factory=list)
    hidden_count: int = 0


class DiscoverCalendarResponse(BaseModel):
    headline: str | None = None
    week_label: str
    from_date: date
    to_date: date
    today: date
    tab: str
    days: list[DiscoverCalendarDay] = Field(default_factory=list)
```

- [ ] **Step 4: Write service**

```python
# app/services/market_events/discover_calendar.py
"""Discover calendar service: groups market events by day for the Toss-style UI (ROB-138)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

from app.schemas.market_events import MarketEventResponse
from app.schemas.market_events_calendar import (
    DiscoverCalendarDay,
    DiscoverCalendarEvent,
    DiscoverCalendarResponse,
)
from app.services.market_events.prioritization import Priority, compute_priority
from app.services.market_events.query_service import MarketEventsQueryService
from app.services.market_events.user_context import UserEventContext

PER_DAY_VISIBLE_LIMIT = 8

Tab = Literal["all", "economic", "earnings"]

KO_WEEKDAYS = ["월", "화", "수", "목", "금", "토", "일"]

PRIORITY_LABEL = {
    Priority.HELD: "held",
    Priority.WATCHED: "watched",
    Priority.MAJOR: "major",
    Priority.HIGH_IMPORTANCE: "high",
    Priority.MEDIUM_IMPORTANCE: "medium",
    Priority.OTHER: "other",
}

PRIORITY_BADGE = {
    Priority.HELD: "보유",
    Priority.WATCHED: "관심",
    Priority.MAJOR: "주요",
    Priority.HIGH_IMPORTANCE: None,
    Priority.MEDIUM_IMPORTANCE: None,
    Priority.OTHER: None,
}

TIME_HINT_LABEL = {
    "before_market": "장 전",
    "open": "장 중",
    "after_market": "장 마감 후",
    "unknown": None,
}


def _format_time_label(event: MarketEventResponse) -> str | None:
    if event.release_time_utc is not None:
        # Convert to local — for MVP, hint label is good enough; UI can format the ISO if needed.
        pass
    if event.time_hint and event.time_hint in TIME_HINT_LABEL:
        return TIME_HINT_LABEL[event.time_hint]
    return None


def _format_subtitle(event: MarketEventResponse) -> str | None:
    if event.category == "earnings":
        eps = next((v for v in event.values if v.metric_name == "eps"), None)
        if eps and (eps.actual is not None or eps.forecast is not None):
            actual = "-" if eps.actual is None else str(eps.actual)
            forecast = "-" if eps.forecast is None else str(eps.forecast)
            return f"EPS {actual} · 예측 {forecast}"
        return None
    if event.category == "economic":
        actual = next((v for v in event.values if v.metric_name == "actual"), None)
        if actual is None:
            return None
        unit = actual.unit or ""
        a = "-" if actual.actual is None else str(actual.actual)
        f = "-" if actual.forecast is None else str(actual.forecast)
        p = "-" if actual.previous is None else str(actual.previous)
        return f"실제 {a}{unit} · 예측 {f}{unit} · 이전 {p}{unit}"
    if event.category == "disclosure":
        return event.company_name
    return None


def _event_title(event: MarketEventResponse) -> str:
    if event.title:
        return event.title
    if event.symbol:
        return f"{event.symbol} 이벤트"
    return "이벤트"


def _korean_weekday(d: date) -> str:
    return KO_WEEKDAYS[d.weekday()]


def _week_label(d: date) -> str:
    """First/second/... week of the month — based on Mon-anchored count of d's week start."""
    # Find the Monday of d's week.
    monday = d - timedelta(days=d.weekday())
    # Day-of-month of that Monday determines the week-of-month index.
    week_index = (monday.day - 1) // 7 + 1
    # If Monday belongs to previous month, treat as week 1 of d's month for stability.
    if monday.month != d.month:
        week_index = 1
    return f"{d.month}월 {week_index}주차"


def _filter_by_tab(events: list[MarketEventResponse], tab: Tab) -> list[MarketEventResponse]:
    if tab == "all":
        return events
    if tab == "economic":
        return [e for e in events if e.category == "economic"]
    if tab == "earnings":
        return [e for e in events if e.category == "earnings"]
    return events


@dataclass
class _Scored:
    priority: Priority
    event: MarketEventResponse


class DiscoverCalendarService:
    def __init__(self, query_service: MarketEventsQueryService) -> None:
        self.query_service = query_service

    async def build(
        self,
        *,
        from_date: date,
        to_date: date,
        today: date,
        ctx: UserEventContext,
        tab: Tab = "all",
    ) -> DiscoverCalendarResponse:
        if from_date > to_date:
            raise ValueError("from_date must be <= to_date")
        range_resp = await self.query_service.list_for_range(from_date, to_date)
        filtered = _filter_by_tab(range_resp.events, tab)

        scored: list[_Scored] = [
            _Scored(priority=compute_priority(e, ctx), event=e) for e in filtered
        ]

        # Group by date.
        by_date: dict[date, list[_Scored]] = {}
        for s in scored:
            by_date.setdefault(s.event.event_date, []).append(s)

        # Sort each day: by priority asc (HELD=0 first), then by release time / symbol.
        days: list[DiscoverCalendarDay] = []
        cursor = from_date
        high_importance_count = 0
        while cursor <= to_date:
            bucket = by_date.get(cursor, [])
            bucket.sort(
                key=lambda s: (
                    s.priority.value,
                    s.event.release_time_utc or s.event.event_date,
                    s.event.symbol or "",
                )
            )
            high_importance_count += sum(
                1
                for s in bucket
                if s.priority
                in (Priority.HELD, Priority.WATCHED, Priority.MAJOR, Priority.HIGH_IMPORTANCE)
            )
            visible = bucket[:PER_DAY_VISIBLE_LIMIT]
            hidden = max(0, len(bucket) - PER_DAY_VISIBLE_LIMIT)
            days.append(
                DiscoverCalendarDay(
                    date=cursor,
                    weekday=_korean_weekday(cursor),
                    is_today=(cursor == today),
                    events=[
                        DiscoverCalendarEvent(
                            title=_event_title(s.event),
                            badge=PRIORITY_BADGE[s.priority],
                            category=s.event.category,
                            market=s.event.market,
                            symbol=s.event.symbol,
                            subtitle=_format_subtitle(s.event),
                            time_label=_format_time_label(s.event),
                            priority=PRIORITY_LABEL[s.priority],
                            source_event_id=s.event.source_event_id,
                        )
                        for s in visible
                    ],
                    hidden_count=hidden,
                )
            )
            cursor += timedelta(days=1)

        headline: str | None = None
        if high_importance_count > 0:
            headline = f"이번 주 주요 이벤트 {high_importance_count}건이 예정되어 있어요"

        return DiscoverCalendarResponse(
            headline=headline,
            week_label=_week_label(today),
            from_date=from_date,
            to_date=to_date,
            today=today,
            tab=tab,
            days=days,
        )
```

- [ ] **Step 5: Run tests, expect pass**

```bash
uv run pytest tests/services/test_market_events_discover_calendar.py -v
```

- [ ] **Step 6: Commit**

```bash
git add app/schemas/market_events_calendar.py app/services/market_events/discover_calendar.py tests/services/test_market_events_discover_calendar.py
git commit -m "feat(market_events): add Discover calendar grouping service (ROB-138)"
```

---

## Task 4: Backend `/discover-calendar` router endpoint

**Files:**
- Modify: `app/routers/market_events.py`
- Create: `tests/test_market_events_discover_calendar_router.py`

- [ ] **Step 1: Write failing router test**

```python
# tests/test_market_events_discover_calendar_router.py
"""Router test for Discover calendar endpoint (ROB-138)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import delete


@pytest_asyncio.fixture(autouse=True)
async def _clean(db_session):
    from app.models.market_events import (
        MarketEvent,
        MarketEventIngestionPartition,
        MarketEventValue,
    )

    await db_session.execute(delete(MarketEventValue))
    await db_session.execute(delete(MarketEvent))
    await db_session.execute(delete(MarketEventIngestionPartition))
    await db_session.commit()
    yield


def _app() -> FastAPI:
    from app.core.db import AsyncSessionLocal, get_db
    from app.routers import market_events
    from app.routers.dependencies import get_authenticated_user

    app = FastAPI()
    app.include_router(market_events.router)
    app.dependency_overrides[get_authenticated_user] = lambda: SimpleNamespace(id=7)

    async def _override_get_db():
        async with AsyncSessionLocal() as session:
            yield session

    app.dependency_overrides[get_db] = _override_get_db
    return app


@pytest.mark.integration
def test_discover_calendar_returns_grouped_days(db_session):
    with TestClient(_app()) as client:
        r = client.get(
            "/trading/api/market-events/discover-calendar"
            "?from_date=2026-05-04&to_date=2026-05-10&today=2026-05-07&tab=all"
        )
    assert r.status_code == 200
    body = r.json()
    assert body["from_date"] == "2026-05-04"
    assert body["to_date"] == "2026-05-10"
    assert body["today"] == "2026-05-07"
    assert body["tab"] == "all"
    assert body["week_label"].endswith("주차")
    assert isinstance(body["days"], list) and len(body["days"]) == 7
    today_day = next(d for d in body["days"] if d["date"] == "2026-05-07")
    assert today_day["is_today"] is True


@pytest.mark.integration
def test_discover_calendar_validates_date_order(db_session):
    with TestClient(_app()) as client:
        r = client.get(
            "/trading/api/market-events/discover-calendar"
            "?from_date=2026-05-10&to_date=2026-05-04&today=2026-05-07"
        )
    assert r.status_code == 400


@pytest.mark.integration
def test_discover_calendar_rejects_unknown_tab(db_session):
    with TestClient(_app()) as client:
        r = client.get(
            "/trading/api/market-events/discover-calendar"
            "?from_date=2026-05-04&to_date=2026-05-10&today=2026-05-07&tab=bogus"
        )
    assert r.status_code == 422


@pytest.mark.integration
def test_discover_calendar_requires_auth():
    from app.routers import market_events

    app = FastAPI()
    app.include_router(market_events.router)
    with TestClient(app) as client:
        r = client.get(
            "/trading/api/market-events/discover-calendar"
            "?from_date=2026-05-04&to_date=2026-05-10&today=2026-05-07"
        )
    assert r.status_code in (401, 403)
```

- [ ] **Step 2: Run tests, expect failure (404 / not registered)**

```bash
uv run pytest tests/test_market_events_discover_calendar_router.py -v
```

- [ ] **Step 3: Modify router**

Add at the bottom of `app/routers/market_events.py`:

```python
from datetime import date as _date
from typing import Literal as _Literal

from app.schemas.market_events_calendar import DiscoverCalendarResponse
from app.services.market_events.discover_calendar import DiscoverCalendarService
from app.services.market_events.user_context import get_user_event_context


@router.get(
    "/api/market-events/discover-calendar",
    response_model=DiscoverCalendarResponse,
)
async def get_discover_calendar(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_authenticated_user)],
    from_date: Annotated[_date, Query(description="ISO start date, inclusive")],
    to_date: Annotated[_date, Query(description="ISO end date, inclusive")],
    today: Annotated[
        _date | None, Query(description="ISO today; default = server clock")
    ] = None,
    tab: Annotated[
        _Literal["all", "economic", "earnings"],
        Query(description="UI tab filter"),
    ] = "all",
) -> DiscoverCalendarResponse:
    target_today = today or _date.today()
    query_service = MarketEventsQueryService(db)
    ctx = await get_user_event_context(db, user_id=current_user.id)
    svc = DiscoverCalendarService(query_service=query_service)
    try:
        return await svc.build(
            from_date=from_date,
            to_date=to_date,
            today=target_today,
            ctx=ctx,
            tab=tab,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
```

- [ ] **Step 4: Run tests, expect pass**

```bash
uv run pytest tests/test_market_events_discover_calendar_router.py -v
```

- [ ] **Step 5: Run all market events tests to confirm nothing else broke**

```bash
uv run pytest tests/test_market_events_router.py tests/services/test_market_events_*.py tests/test_market_events_discover_calendar_router.py -v
```

- [ ] **Step 6: Commit**

```bash
git add app/routers/market_events.py tests/test_market_events_discover_calendar_router.py
git commit -m "feat(market_events): add GET /discover-calendar endpoint (ROB-138)"
```

---

## Task 5: Frontend types + API client + hook + api test

**Files:**
- Modify: `frontend/invest/src/types/marketEvents.ts`
- Modify: `frontend/invest/src/api/marketEvents.ts`
- Create: `frontend/invest/src/hooks/useDiscoverCalendar.ts`
- Create: `frontend/invest/src/__tests__/discoverCalendar.api.test.ts`

- [ ] **Step 1: Add types (append to existing file)**

```typescript
// frontend/invest/src/types/marketEvents.ts (append)

export type DiscoverCalendarTab = "all" | "economic" | "earnings";

export interface DiscoverCalendarEvent {
  title: string;
  badge: string | null;
  category: string;
  market: string;
  symbol: string | null;
  subtitle: string | null;
  time_label: string | null;
  priority: string;
  source_event_id: string | null;
}

export interface DiscoverCalendarDay {
  date: string;
  weekday: string;
  is_today: boolean;
  events: DiscoverCalendarEvent[];
  hidden_count: number;
}

export interface DiscoverCalendarResponse {
  headline: string | null;
  week_label: string;
  from_date: string;
  to_date: string;
  today: string;
  tab: DiscoverCalendarTab;
  days: DiscoverCalendarDay[];
}

export interface FetchDiscoverCalendarParams {
  fromDate: string;
  toDate: string;
  today?: string;
  tab?: DiscoverCalendarTab;
}
```

- [ ] **Step 2: Add API client (append to existing file)**

```typescript
// frontend/invest/src/api/marketEvents.ts (append)

import type {
  DiscoverCalendarResponse,
  FetchDiscoverCalendarParams,
} from "../types/marketEvents";

export async function fetchDiscoverCalendar(
  params: FetchDiscoverCalendarParams,
  signal?: AbortSignal,
): Promise<DiscoverCalendarResponse> {
  const search = new URLSearchParams();
  search.set("from_date", params.fromDate);
  search.set("to_date", params.toDate);
  if (params.today) search.set("today", params.today);
  if (params.tab) search.set("tab", params.tab);
  const url = `/trading/api/market-events/discover-calendar?${search.toString()}`;
  const res = await fetch(url, { credentials: "include", signal });
  if (!res.ok) {
    throw new Error(`/trading/api/market-events/discover-calendar ${res.status}`);
  }
  return (await res.json()) as DiscoverCalendarResponse;
}
```

> Note: place all `import` lines at the top of the file (TypeScript requires this).

- [ ] **Step 3: Write API test**

```typescript
// frontend/invest/src/__tests__/discoverCalendar.api.test.ts
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { fetchDiscoverCalendar } from "../api/marketEvents";

const fetchMock = vi.fn();

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => {
  vi.unstubAllGlobals();
});

describe("fetchDiscoverCalendar", () => {
  test("encodes from_date/to_date/today/tab", async () => {
    fetchMock.mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        headline: null,
        week_label: "5월 1주차",
        from_date: "2026-05-04",
        to_date: "2026-05-10",
        today: "2026-05-07",
        tab: "all",
        days: [],
      }),
    });
    const data = await fetchDiscoverCalendar({
      fromDate: "2026-05-04",
      toDate: "2026-05-10",
      today: "2026-05-07",
      tab: "all",
    });
    expect(data.week_label).toBe("5월 1주차");
    const url = fetchMock.mock.calls[0][0] as string;
    expect(url).toContain("from_date=2026-05-04");
    expect(url).toContain("to_date=2026-05-10");
    expect(url).toContain("today=2026-05-07");
    expect(url).toContain("tab=all");
  });

  test("rejects on non-ok response", async () => {
    fetchMock.mockResolvedValueOnce({ ok: false, status: 500, json: async () => ({}) });
    await expect(
      fetchDiscoverCalendar({ fromDate: "2026-05-04", toDate: "2026-05-10" }),
    ).rejects.toThrow(/500/);
  });
});
```

- [ ] **Step 4: Write hook**

```typescript
// frontend/invest/src/hooks/useDiscoverCalendar.ts
import { useEffect, useMemo, useState } from "react";
import { fetchDiscoverCalendar } from "../api/marketEvents";
import type {
  DiscoverCalendarResponse,
  FetchDiscoverCalendarParams,
} from "../types/marketEvents";

export type DiscoverCalendarState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; data: DiscoverCalendarResponse };

export interface UseDiscoverCalendarOptions {
  enabled?: boolean;
}

export function useDiscoverCalendar(
  params: FetchDiscoverCalendarParams,
  options: UseDiscoverCalendarOptions = {},
) {
  const enabled = options.enabled ?? true;
  const [state, setState] = useState<DiscoverCalendarState>({ status: "loading" });
  const [tick, setTick] = useState(0);
  const paramsKey = useMemo(() => JSON.stringify(params), [params]);

  useEffect(() => {
    if (!enabled) return;
    const controller = new AbortController();
    setState({ status: "loading" });
    fetchDiscoverCalendar(params, controller.signal)
      .then((data) => setState({ status: "ready", data }))
      .catch((e) => {
        if (controller.signal.aborted) return;
        setState({ status: "error", message: e?.message ?? "failed" });
      });
    return () => controller.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, paramsKey, tick]);

  return { state, reload: () => setTick((t) => t + 1) };
}
```

- [ ] **Step 5: Run tests, expect pass**

```bash
cd frontend/invest && npx vitest run src/__tests__/discoverCalendar.api.test.ts
```

- [ ] **Step 6: Commit**

```bash
git add frontend/invest/src/types/marketEvents.ts \
        frontend/invest/src/api/marketEvents.ts \
        frontend/invest/src/hooks/useDiscoverCalendar.ts \
        frontend/invest/src/__tests__/discoverCalendar.api.test.ts
git commit -m "feat(invest): add discover-calendar api + hook (ROB-138)"
```

---

## Task 6: Frontend `DiscoverCalendarCard` component

**Files:**
- Create: `frontend/invest/src/components/discover/DiscoverCalendarCard.tsx`
- Create: `frontend/invest/src/__tests__/DiscoverCalendarCard.test.tsx`

**Purpose:** Render headline, week label, date rail (one chip per day with weekday + day-of-month, today highlighted), three tabs (`전체 / 경제지표 / 실적`), then per-day sections with event cards (badge chip, title, subtitle, time_label) and a "+N건 더보기" footer when `hidden_count > 0`. Loading / error / empty states.

- [ ] **Step 1: Write failing test**

```tsx
// frontend/invest/src/__tests__/DiscoverCalendarCard.test.tsx
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { DiscoverCalendarCard } from "../components/discover/DiscoverCalendarCard";
import type {
  DiscoverCalendarResponse,
} from "../types/marketEvents";

function makeResponse(over: Partial<DiscoverCalendarResponse> = {}): DiscoverCalendarResponse {
  return {
    headline: null,
    week_label: "5월 1주차",
    from_date: "2026-05-04",
    to_date: "2026-05-10",
    today: "2026-05-07",
    tab: "all",
    days: [
      {
        date: "2026-05-07",
        weekday: "목",
        is_today: true,
        hidden_count: 0,
        events: [],
      },
      {
        date: "2026-05-08",
        weekday: "금",
        is_today: false,
        hidden_count: 0,
        events: [],
      },
    ],
    ...over,
  };
}

const fetchMock = vi.fn();

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => {
  vi.unstubAllGlobals();
});

test("renders loading state initially", () => {
  fetchMock.mockReturnValue(new Promise(() => {}));
  render(<DiscoverCalendarCard fromDate="2026-05-04" toDate="2026-05-10" today="2026-05-07" />);
  expect(screen.getByText(/불러오는 중/)).toBeInTheDocument();
});

test("renders week label and headline when present", async () => {
  fetchMock.mockResolvedValueOnce({
    ok: true,
    json: async () => makeResponse({ headline: "이번 주 주요 이벤트 3건이 예정되어 있어요" }),
  });
  render(<DiscoverCalendarCard fromDate="2026-05-04" toDate="2026-05-10" today="2026-05-07" />);
  expect(await screen.findByText(/5월 1주차/)).toBeInTheDocument();
  expect(screen.getByText(/이번 주 주요 이벤트 3건이 예정되어 있어요/)).toBeInTheDocument();
});

test("highlights today's day chip via aria-current", async () => {
  fetchMock.mockResolvedValueOnce({ ok: true, json: async () => makeResponse() });
  render(<DiscoverCalendarCard fromDate="2026-05-04" toDate="2026-05-10" today="2026-05-07" />);
  const todayChip = await screen.findByRole("button", { name: /목.*7/ });
  expect(todayChip).toHaveAttribute("aria-current", "date");
});

test("shows held badge and event subtitle", async () => {
  fetchMock.mockResolvedValueOnce({
    ok: true,
    json: async () =>
      makeResponse({
        days: [
          {
            date: "2026-05-07",
            weekday: "목",
            is_today: true,
            hidden_count: 0,
            events: [
              {
                title: "AAPL 실적발표",
                badge: "보유",
                category: "earnings",
                market: "us",
                symbol: "AAPL",
                subtitle: "EPS -0.34 · 예측 -0.52",
                time_label: "장 마감 후",
                priority: "held",
                source_event_id: "x",
              },
            ],
          },
        ],
      }),
  });
  render(<DiscoverCalendarCard fromDate="2026-05-04" toDate="2026-05-10" today="2026-05-07" />);
  expect(await screen.findByText("AAPL 실적발표")).toBeInTheDocument();
  expect(screen.getByText("보유")).toBeInTheDocument();
  expect(screen.getByText(/EPS -0\.34/)).toBeInTheDocument();
  expect(screen.getByText("장 마감 후")).toBeInTheDocument();
});

test("renders +N hidden footer when hidden_count > 0", async () => {
  fetchMock.mockResolvedValueOnce({
    ok: true,
    json: async () =>
      makeResponse({
        days: [
          {
            date: "2026-05-07",
            weekday: "목",
            is_today: true,
            hidden_count: 580,
            events: [],
          },
        ],
      }),
  });
  render(<DiscoverCalendarCard fromDate="2026-05-04" toDate="2026-05-10" today="2026-05-07" />);
  expect(await screen.findByText(/\+580건 더보기/)).toBeInTheDocument();
});

test("clicking economic tab refetches with tab=economic", async () => {
  fetchMock
    .mockResolvedValueOnce({ ok: true, json: async () => makeResponse({ tab: "all" }) })
    .mockResolvedValueOnce({ ok: true, json: async () => makeResponse({ tab: "economic" }) });

  render(<DiscoverCalendarCard fromDate="2026-05-04" toDate="2026-05-10" today="2026-05-07" />);
  await screen.findByText(/5월 1주차/);

  fireEvent.click(screen.getByRole("button", { name: "경제지표" }));

  await waitFor(() => {
    const calls = fetchMock.mock.calls.map((c) => c[0] as string);
    expect(calls.some((u) => u.includes("tab=economic"))).toBe(true);
  });
});

test("renders empty state when no events", async () => {
  fetchMock.mockResolvedValueOnce({ ok: true, json: async () => makeResponse() });
  render(<DiscoverCalendarCard fromDate="2026-05-04" toDate="2026-05-10" today="2026-05-07" />);
  expect(await screen.findByText(/표시할 이벤트가 없습니다/)).toBeInTheDocument();
});

test("renders error state and retry button on error", async () => {
  fetchMock.mockResolvedValueOnce({ ok: false, status: 503, json: async () => ({}) });
  render(<DiscoverCalendarCard fromDate="2026-05-04" toDate="2026-05-10" today="2026-05-07" />);
  expect(await screen.findByText(/잠시 후 다시 시도해 주세요/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /재시도/ })).toBeInTheDocument();
});
```

- [ ] **Step 2: Write component**

```tsx
// frontend/invest/src/components/discover/DiscoverCalendarCard.tsx
import { useMemo, useState } from "react";
import { useDiscoverCalendar } from "../../hooks/useDiscoverCalendar";
import type {
  DiscoverCalendarDay,
  DiscoverCalendarEvent,
  DiscoverCalendarTab,
} from "../../types/marketEvents";

type Props = {
  fromDate: string;
  toDate: string;
  today: string;
};

const TAB_LABELS: Record<DiscoverCalendarTab, string> = {
  all: "전체",
  economic: "경제지표",
  earnings: "실적",
};

const BADGE_COLORS: Record<string, string> = {
  held: "var(--accent, #2962ff)",
  watched: "var(--info, #0288d1)",
  major: "var(--neutral-strong, #555)",
};

function dayOfMonth(iso: string): number {
  const [, , dd] = iso.split("-");
  return Number(dd);
}

function DayChip({
  day,
  active,
  onClick,
}: {
  day: DiscoverCalendarDay;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-current={day.is_today ? "date" : undefined}
      aria-pressed={active}
      style={{
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        padding: "6px 10px",
        borderRadius: 12,
        border: "1px solid var(--surface-2)",
        background: day.is_today ? "var(--accent, #2962ff)" : (active ? "var(--surface-2)" : "transparent"),
        color: day.is_today ? "#fff" : "inherit",
        fontSize: 12,
        minWidth: 44,
      }}
    >
      <span style={{ opacity: 0.8 }}>{day.weekday}</span>
      <strong>{dayOfMonth(day.date)}</strong>
    </button>
  );
}

function Badge({ label, priority }: { label: string; priority: string }) {
  const color = BADGE_COLORS[priority] ?? "var(--neutral-strong, #555)";
  return (
    <span
      style={{
        fontSize: 11,
        padding: "2px 6px",
        borderRadius: 999,
        background: color,
        color: "#fff",
      }}
    >
      {label}
    </span>
  );
}

function EventCard({ event }: { event: DiscoverCalendarEvent }) {
  return (
    <li
      style={{
        display: "flex",
        flexDirection: "column",
        gap: 4,
        padding: "8px 0",
        borderBottom: "1px solid var(--surface-2)",
      }}
    >
      <div style={{ display: "flex", justifyContent: "space-between", gap: 8, alignItems: "center" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          {event.badge && <Badge label={event.badge} priority={event.priority} />}
          <strong style={{ fontSize: 13 }}>{event.title}</strong>
        </div>
        {event.time_label && (
          <span className="subtle" style={{ fontSize: 12 }}>{event.time_label}</span>
        )}
      </div>
      {event.subtitle && (
        <div className="subtle" style={{ fontSize: 12 }}>{event.subtitle}</div>
      )}
    </li>
  );
}

function DaySection({ day, focused }: { day: DiscoverCalendarDay; focused: boolean }) {
  return (
    <section
      style={{
        marginTop: 12,
        opacity: focused ? 1 : 0.65,
      }}
      aria-label={`${day.date} ${day.weekday}`}
    >
      <h3 style={{ fontSize: 13, margin: "0 0 4px 0" }}>
        {dayOfMonth(day.date)} {day.weekday}{day.is_today ? " · 오늘" : ""}
      </h3>
      {day.events.length === 0 ? (
        <div className="subtle" style={{ fontSize: 12 }}>표시할 이벤트가 없습니다.</div>
      ) : (
        <ul style={{ listStyle: "none", padding: 0, margin: 0 }}>
          {day.events.map((e) => (
            <EventCard
              key={e.source_event_id ?? `${day.date}-${e.title}-${e.symbol ?? ""}`}
              event={e}
            />
          ))}
        </ul>
      )}
      {day.hidden_count > 0 && (
        <div style={{ marginTop: 4, fontSize: 12 }}>
          <span className="subtle">+{day.hidden_count}건 더보기</span>
        </div>
      )}
    </section>
  );
}

export function DiscoverCalendarCard({ fromDate, toDate, today }: Props) {
  const [tab, setTab] = useState<DiscoverCalendarTab>("all");
  const [activeDate, setActiveDate] = useState<string>(today);
  const { state, reload } = useDiscoverCalendar({
    fromDate,
    toDate,
    today,
    tab,
  });

  const days = state.status === "ready" ? state.data.days : [];
  const totalEvents = useMemo(
    () => days.reduce((acc, d) => acc + d.events.length, 0),
    [days],
  );

  return (
    <section
      aria-labelledby="discover-calendar-heading"
      style={{
        padding: 16,
        background: "var(--surface)",
        border: "1px solid var(--surface-2)",
        borderRadius: 14,
      }}
    >
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
        <h2 id="discover-calendar-heading" style={{ margin: 0, fontSize: 14, fontWeight: 700 }}>
          오늘의 주요 이벤트
        </h2>
        {state.status === "ready" && (
          <span className="subtle" style={{ fontSize: 12 }}>{state.data.week_label}</span>
        )}
      </div>

      {state.status === "ready" && state.data.headline && (
        <div className="subtle" style={{ marginTop: 4, fontSize: 12 }}>{state.data.headline}</div>
      )}

      <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
        {(Object.keys(TAB_LABELS) as DiscoverCalendarTab[]).map((t) => (
          <button
            type="button"
            key={t}
            onClick={() => setTab(t)}
            aria-pressed={tab === t}
            style={{
              padding: "4px 10px",
              borderRadius: 999,
              border: "1px solid var(--surface-2)",
              background: tab === t ? "var(--surface-2)" : "transparent",
              fontSize: 12,
            }}
          >
            {TAB_LABELS[t]}
          </button>
        ))}
      </div>

      {state.status === "loading" && (
        <div className="subtle" style={{ marginTop: 8 }}>불러오는 중…</div>
      )}
      {state.status === "error" && (
        <div style={{ marginTop: 8 }}>
          <div>잠시 후 다시 시도해 주세요.</div>
          <button type="button" onClick={reload}>재시도</button>
          <div className="subtle">{state.message}</div>
        </div>
      )}

      {state.status === "ready" && (
        <>
          <div
            role="tablist"
            aria-label="주간 날짜"
            style={{ display: "flex", gap: 6, marginTop: 12, overflowX: "auto" }}
          >
            {days.map((d) => (
              <DayChip
                key={d.date}
                day={d}
                active={d.date === activeDate}
                onClick={() => setActiveDate(d.date)}
              />
            ))}
          </div>

          {totalEvents === 0 && (
            <div className="subtle" style={{ marginTop: 12, fontSize: 12 }}>
              표시할 이벤트가 없습니다.
            </div>
          )}

          {days.map((d) => (
            <DaySection key={d.date} day={d} focused={d.date === activeDate} />
          ))}
        </>
      )}
    </section>
  );
}
```

- [ ] **Step 3: Run tests, expect pass**

```bash
cd frontend/invest && npx vitest run src/__tests__/DiscoverCalendarCard.test.tsx
```

- [ ] **Step 4: Commit**

```bash
git add frontend/invest/src/components/discover/DiscoverCalendarCard.tsx \
        frontend/invest/src/__tests__/DiscoverCalendarCard.test.tsx
git commit -m "feat(invest): add DiscoverCalendarCard with date rail + tabs (ROB-138)"
```

---

## Task 7: Wire into DiscoverPage; remove TodayEventCard

**Files:**
- Modify: `frontend/invest/src/pages/DiscoverPage.tsx`
- Delete: `frontend/invest/src/components/discover/TodayEventCard.tsx`
- Delete: `frontend/invest/src/__tests__/TodayEventCard.test.tsx`

- [ ] **Step 1: Update DiscoverPage**

Replace lines 1-79 with:

```tsx
// frontend/invest/src/pages/DiscoverPage.tsx
import { AppShell } from "../components/AppShell";
import { BottomNav } from "../components/BottomNav";
import { AiIssueCard } from "../components/discover/AiIssueCard";
import { AiIssueTicker } from "../components/discover/AiIssueTicker";
import { CategoryShortcutRail } from "../components/discover/CategoryShortcutRail";
import { DiscoverCalendarCard } from "../components/discover/DiscoverCalendarCard";
import { DiscoverHeader } from "../components/discover/DiscoverHeader";
import { sortMarketIssues } from "../components/discover/severity";
import { useNewsIssues, type NewsIssuesState } from "../hooks/useNewsIssues";

export interface DiscoverPageProps {
  state?: NewsIssuesState;
  reload?: () => void;
  /** Override "today" for SSR/tests; defaults to client clock. */
  today?: string;
}

function isoWeekRange(today: string): { fromDate: string; toDate: string } {
  const d = new Date(today + "T00:00:00");
  const day = d.getUTCDay(); // 0 = Sun
  const mondayOffset = day === 0 ? -6 : 1 - day;
  const monday = new Date(d);
  monday.setUTCDate(d.getUTCDate() + mondayOffset);
  const sunday = new Date(monday);
  sunday.setUTCDate(monday.getUTCDate() + 6);
  const iso = (x: Date) => x.toISOString().slice(0, 10);
  return { fromDate: iso(monday), toDate: iso(sunday) };
}

export function DiscoverPage(props: DiscoverPageProps = {}) {
  const live = useNewsIssues(
    {
      market: "all",
      windowHours: 24,
      limit: 20,
    },
    { enabled: props.state === undefined },
  );
  const state = props.state ?? live.state;
  const reload = props.reload ?? live.reload;

  const today = props.today ?? new Date().toISOString().slice(0, 10);
  const { fromDate, toDate } = isoWeekRange(today);

  if (state.status === "loading") {
    return (
      <AppShell>
        <div className="subtle">불러오는 중…</div>
        <BottomNav />
      </AppShell>
    );
  }
  if (state.status === "error") {
    return (
      <AppShell>
        <div>잠시 후 다시 시도해 주세요.</div>
        <button type="button" onClick={reload}>
          재시도
        </button>
        <div className="subtle">{state.message}</div>
        <BottomNav />
      </AppShell>
    );
  }

  const { data } = state;
  const sorted = sortMarketIssues(data.items);

  return (
    <AppShell>
      <DiscoverHeader />
      <CategoryShortcutRail />
      <DiscoverCalendarCard fromDate={fromDate} toDate={toDate} today={today} />
      <AiIssueTicker asOf={data.as_of} windowHours={data.window_hours} />
      {sorted.length === 0 ? (
        <div className="subtle">표시할 이슈가 없습니다.</div>
      ) : (
        <div
          style={{
            display: "flex",
            flexDirection: "column",
            gap: 10,
            flex: 1,
            overflowY: "auto",
          }}
        >
          {sorted.map((issue) => (
            <AiIssueCard key={issue.id} issue={issue} />
          ))}
        </div>
      )}
      <BottomNav />
    </AppShell>
  );
}
```

- [ ] **Step 2: Delete the legacy component + test**

```bash
git rm frontend/invest/src/components/discover/TodayEventCard.tsx \
       frontend/invest/src/__tests__/TodayEventCard.test.tsx
```

- [ ] **Step 3: Verify other places don't import the removed component**

```bash
grep -rn "TodayEventCard" frontend/invest/src/ || echo "no remaining refs"
```

If any references remain (other than the deleted files), update them; expected: `no remaining refs`.

- [ ] **Step 4: Run frontend tests for changed files**

```bash
cd frontend/invest && npx vitest run src/__tests__/DiscoverPage.test.tsx src/__tests__/DiscoverCalendarCard.test.tsx src/__tests__/discoverCalendar.api.test.ts
```

If `DiscoverPage.test.tsx` references `TodayEventCard`, update those references to `DiscoverCalendarCard` (or remove if redundant). Mock `fetch` for the calendar endpoint in the test setup as needed.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat(invest): wire DiscoverCalendarCard into Discover; drop TodayEventCard (ROB-138)"
```

---

## Task 8: Final verification + PR

- [ ] **Step 1: Run the full backend test suite (subset that touches our area + adjacent)**

```bash
uv run pytest tests/test_market_events_router.py \
              tests/test_market_events_discover_calendar_router.py \
              tests/test_market_events_cli.py \
              tests/services/ \
              tests/test_preopen_market_news_briefing.py \
              -v
```

Expected: all pass.

- [ ] **Step 2: Run the full frontend test suite**

```bash
cd frontend/invest && npx vitest run
```

Expected: all pass.

- [ ] **Step 3: Run lint / typecheck if cheap**

```bash
make lint || true
make typecheck || true
cd frontend/invest && npx tsc --noEmit || true
```

Don't fail the task if lint produces unrelated warnings; do fix any new errors introduced by the diff.

- [ ] **Step 4: Push and open PR**

```bash
git push -u origin linear-subagent
gh pr create --base main --title "feat: Toss-style Discover event calendar (ROB-138)" --body "$(cat <<'EOF'
## Summary
- New `GET /trading/api/market-events/discover-calendar` endpoint returns weekly events grouped by day with held/watched/major prioritization and `hidden_count`.
- New `DiscoverCalendarCard` React component (date rail, weekly sections, tabs `전체 / 경제지표 / 실적`, badge chips, hidden-count footer).
- `DiscoverPage` now renders the calendar card; the legacy `TodayEventCard` is removed.

## Acceptance Criteria
- [x] 일주일 범위 이벤트를 날짜별로 그룹핑해 반환하는 화면용 API
- [x] `전체` 탭에서 Finnhub 실적 수백 건이 그대로 노출되지 않음 (`PER_DAY_VISIBLE_LIMIT=8`, surplus → `hidden_count`)
- [x] `경제지표` 탭은 주요 경제지표 중심으로 표시
- [x] `실적` 탭은 보유/관심/주요 실적이 우선
- [x] 보유/관심/주요 배지 데이터 기반
- [x] 실제치/예측치/이전치 subtitle 표시
- [x] 모바일 Discover 화면에서 Toss식 주간 캘린더 UI
- [x] 기존 preopen briefing/market events ingestion 테스트 보존
- [x] 새 endpoint/priority 함수/frontend component에 단위 테스트

## Test plan
- [ ] `uv run pytest tests/services/test_market_events_*.py tests/test_market_events_router.py tests/test_market_events_discover_calendar_router.py`
- [ ] `cd frontend/invest && npx vitest run`
- [ ] Smoke: load `/discover` in the SPA — confirm date rail, today highlighted, headline, tab switching, hidden count footer.

## Notes
- No DB migration required.
- Held/watched are computed at query time over `manual_holdings + broker_accounts` and `user_watch_items + instruments` for the authenticated user.
- `MAJOR_TICKERS` allowlist is hard-coded for MVP; can be moved to a config table later.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 5: Report PR URL back to controller.**

---

## Self-review

- All 9 acceptance-criteria items map to a task above.
- No placeholders / TBD.
- Type names consistent: `Priority`, `UserEventContext`, `DiscoverCalendarService`, `DiscoverCalendarResponse`, `DiscoverCalendarCard`, `useDiscoverCalendar`, `fetchDiscoverCalendar`.
- Method signatures consistent: `compute_priority(event, ctx)`, `get_user_event_context(db, user_id=...)`, `DiscoverCalendarService(query_service=...)`, `.build(from_date=, to_date=, today=, ctx=, tab=)`.
- DB fixtures and auth override match existing pattern (`tests/test_market_events_router.py`).
- The plan keeps non-goals (no Toss reverse-engineering, no broker mutation, no migration).

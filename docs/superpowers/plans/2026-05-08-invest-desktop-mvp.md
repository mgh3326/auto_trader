# /invest Desktop MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement ROB-141/142/143/144 as a single bundled feature: shared `/invest` desktop shell + RightAccountPanel + 3 feature pages (feed/news, signals, calendar). 1 PR.

**Architecture:** Single Vite bundle `frontend/invest/` extended (basename `/invest`, mobile under `/app/*`, desktop under `/`, `/feed/news`, `/signals`, `/calendar`). Backend adds read-only view-model wrappers under `/invest/api/*`. Helper module `app/services/invest_view_model/` consolidates query logic. New SPA shell router `invest_web_spa.py` serves desktop bundle with safety guard. No broker mutation, no Toss API.

**Tech Stack:** Python 3.13 (FastAPI, SQLAlchemy async, Pydantic v2, pytest), React 19 + Vite + react-router v7 + vitest. Reuse existing `InvestHomeService`, `MarketEventsQueryService`, `build_market_issues`, `market_report_service`, `news_articles`/`news_analysis_results`, `user_watch_items`.

**Spec:** `docs/superpowers/specs/2026-05-08-invest-desktop-mvp-design.md`

**Branch:** `rob-142144-ai-mvp` (already checked out)

---

## Task 1: Backend helpers — relation_resolver + account_visual

**Files:**
- Create: `app/services/invest_view_model/__init__.py` (empty)
- Create: `app/services/invest_view_model/relation_resolver.py`
- Create: `app/services/invest_view_model/account_visual.py`
- Create: `tests/test_invest_view_model_relation_resolver.py`
- Create: `tests/test_invest_view_model_safety.py`

- [ ] **Step 1.1: Write failing safety test**

Create `tests/test_invest_view_model_safety.py` matching the pattern of `tests/test_invest_app_spa_router_safety.py`:

```python
"""Safety: invest_view_model package must not import broker/order/mutation paths."""
from __future__ import annotations
import json, os, subprocess, sys
from pathlib import Path
import pytest

FORBIDDEN_PREFIXES = [
    "app.services.kis",
    "app.services.kis_trading_service",
    "app.services.kis_trading_contracts",
    "app.services.upbit",
    "app.services.upbit_websocket",
    "app.services.brokers",
    "app.services.order_service",
    "app.services.fill_notification",
    "app.services.execution_event",
    "app.services.redis_token_manager",
    "app.services.kis_websocket",
    "app.services.kis_websocket_internal",
    "app.tasks",
    "app.routers.portfolio_actions",
    "app.routers.order_estimation",
    "app.routers.order_previews",
    "app.routers.pending_orders",
    "app.routers.watch_order_intent_ledger",
]


@pytest.mark.unit
def test_invest_view_model_does_not_import_execution_paths() -> None:
    project_root = Path(__file__).resolve().parent.parent
    script = """
import importlib, json, sys
import app.services.invest_view_model.relation_resolver
import app.services.invest_view_model.account_visual
print(json.dumps(sorted(sys.modules)))
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(project_root)
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=project_root, env=env, check=True, capture_output=True, text=True,
    )
    loaded = set(json.loads(result.stdout))
    violations = sorted(
        m for m in loaded for f in FORBIDDEN_PREFIXES if m == f or m.startswith(f"{f}.")
    )
    if violations:
        pytest.fail(f"Forbidden execution-path imports: {violations}")
```

- [ ] **Step 1.2: Implement account_visual.py**

```python
"""Account source -> visual tone/badge mapping for /invest desktop UI."""
from __future__ import annotations
from typing import Literal
from pydantic import BaseModel, ConfigDict

Tone = Literal["navy", "gray", "purple", "green", "dashed"]
BadgeText = Literal["Live", "Mock", "Crypto", "Paper", "Manual"]


class AccountSourceVisual(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str
    tone: Tone
    badge: BadgeText
    displayName: str


_VISUAL_MAP: dict[str, tuple[Tone, BadgeText, str]] = {
    "kis": ("navy", "Live", "한국투자증권"),
    "kis_mock": ("gray", "Mock", "한국투자증권 모의"),
    "kiwoom_mock": ("gray", "Mock", "키움 모의"),
    "upbit": ("purple", "Crypto", "업비트"),
    "alpaca_paper": ("green", "Paper", "Alpaca Paper"),
    "toss_manual": ("dashed", "Manual", "토스 수동"),
    "pension_manual": ("dashed", "Manual", "연금 수동"),
    "isa_manual": ("dashed", "Manual", "ISA 수동"),
    "db_simulated": ("dashed", "Manual", "시뮬레이션"),
}


def visual_for(source: str) -> AccountSourceVisual:
    tone, badge, display = _VISUAL_MAP.get(source, ("gray", "Manual", source))
    return AccountSourceVisual(source=source, tone=tone, badge=badge, displayName=display)


def all_visuals() -> list[AccountSourceVisual]:
    return [visual_for(s) for s in _VISUAL_MAP]
```

- [ ] **Step 1.3: Implement relation_resolver.py**

```python
"""Compute (held / watchlist / both / none) per (market, symbol) for a user."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.symbol import to_db_symbol

Relation = Literal["held", "watchlist", "both", "none"]
Market = Literal["kr", "us", "crypto"]


def _norm(symbol: str) -> str:
    try:
        return to_db_symbol(symbol).upper()
    except Exception:
        return symbol.upper()


@dataclass
class RelationResolver:
    held: set[tuple[str, str]] = field(default_factory=set)
    watch: set[tuple[str, str]] = field(default_factory=set)

    def relation(self, market: str, symbol: str) -> Relation:
        key = (market.lower(), _norm(symbol))
        h = key in self.held
        w = key in self.watch
        if h and w:
            return "both"
        if h:
            return "held"
        if w:
            return "watchlist"
        return "none"

    def is_held(self, market: str, symbol: str) -> bool:
        return (market.lower(), _norm(symbol)) in self.held

    def is_watched(self, market: str, symbol: str) -> bool:
        return (market.lower(), _norm(symbol)) in self.watch


async def build_relation_resolver(
    db: AsyncSession,
    *,
    user_id: int,
    held_pairs: list[tuple[str, str]] | None = None,
) -> RelationResolver:
    """Build a resolver for the given user.

    `held_pairs` (market, symbol) can be passed in by callers that already
    have InvestHomeResponse handy. If None, the resolver leaves held empty
    (callers may override) — most callers should pass it in to avoid an
    extra round-trip.
    """
    from app.models.trading import UserWatchItem

    resolver = RelationResolver()
    if held_pairs:
        resolver.held = {(m.lower(), _norm(s)) for m, s in held_pairs}

    # user_watch_items joins to instruments for symbol/market.
    # If the join fails or the table is unavailable, leave watch empty.
    try:
        from app.models.trading import Instrument  # type: ignore
    except ImportError:
        return resolver

    stmt = (
        select(Instrument.symbol, Instrument.market)
        .join(UserWatchItem, UserWatchItem.instrument_id == Instrument.id)
        .where(UserWatchItem.user_id == user_id, UserWatchItem.is_active.is_(True))
    )
    result = await db.execute(stmt)
    for sym, market in result.all():
        if sym is None or market is None:
            continue
        resolver.watch.add((str(market).lower(), _norm(str(sym))))
    return resolver
```

NOTE: If `Instrument` model doesn't exist with that exact name, the executor must `grep` `app/models/` for the model that `UserWatchItem.instrument_id` FKs to (`instruments` table). Adjust import accordingly.

- [ ] **Step 1.4: Write unit test**

Create `tests/test_invest_view_model_relation_resolver.py`:

```python
"""Unit tests for relation_resolver."""
from __future__ import annotations
import pytest

from app.services.invest_view_model.relation_resolver import RelationResolver


@pytest.mark.unit
def test_relation_held_only() -> None:
    r = RelationResolver(held={("us", "AAPL")})
    assert r.relation("us", "AAPL") == "held"
    assert r.relation("us", "TSLA") == "none"


@pytest.mark.unit
def test_relation_watchlist_only() -> None:
    r = RelationResolver(watch={("kr", "005930")})
    assert r.relation("kr", "005930") == "watchlist"


@pytest.mark.unit
def test_relation_both() -> None:
    r = RelationResolver(held={("us", "BRK.B")}, watch={("us", "BRK.B")})
    assert r.relation("us", "BRK.B") == "both"


@pytest.mark.unit
def test_relation_normalizes_symbol() -> None:
    r = RelationResolver(held={("us", "BRK.B")})
    # KIS slash form, Yahoo dash form must both resolve
    assert r.relation("us", "BRK/B") == "held"
    assert r.relation("us", "BRK-B") == "held"


@pytest.mark.unit
def test_relation_market_case_insensitive() -> None:
    r = RelationResolver(held={("kr", "005930")})
    assert r.relation("KR", "005930") == "held"
```

- [ ] **Step 1.5: Run tests**

```bash
uv run pytest tests/test_invest_view_model_relation_resolver.py tests/test_invest_view_model_safety.py -v
```

Expected: all pass.

- [ ] **Step 1.6: Commit**

```bash
git add app/services/invest_view_model tests/test_invest_view_model_relation_resolver.py tests/test_invest_view_model_safety.py
git commit -m "feat(invest_view_model): add relation_resolver and account_visual helpers"
```

---

## Task 2: `/invest/api/account-panel` (schema + router + tests)

**Files:**
- Create: `app/schemas/invest_account_panel.py`
- Create: `app/services/invest_view_model/account_panel_service.py`
- Modify: `app/routers/invest_api.py` (add `/account-panel` endpoint)
- Create: `tests/test_invest_account_panel_router.py`

- [ ] **Step 2.1: Write schema**

`app/schemas/invest_account_panel.py`:

```python
"""ROB-141 — /invest/api/account-panel response schema."""
from __future__ import annotations
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

from app.schemas.invest_home import (
    Account, GroupedHolding, HomeSummary, InvestHomeWarning,
)
from app.services.invest_view_model.account_visual import AccountSourceVisual


class WatchSymbol(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str
    market: Literal["kr", "us", "crypto"]
    displayName: str
    note: str | None = None


class AccountPanelMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")
    warnings: list[InvestHomeWarning] = Field(default_factory=list)
    watchlistAvailable: bool = True


class AccountPanelResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    homeSummary: HomeSummary
    accounts: list[Account]
    groupedHoldings: list[GroupedHolding]
    watchSymbols: list[WatchSymbol]
    sourceVisuals: list[AccountSourceVisual]
    meta: AccountPanelMeta
```

- [ ] **Step 2.2: Write service**

`app/services/invest_view_model/account_panel_service.py`:

```python
"""ROB-141 — assemble AccountPanelResponse for /invest/api/account-panel."""
from __future__ import annotations
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.invest_account_panel import (
    AccountPanelMeta, AccountPanelResponse, WatchSymbol,
)
from app.services.invest_home_service import InvestHomeService
from app.services.invest_view_model.account_visual import all_visuals


async def build_account_panel(
    *, user_id: int, db: AsyncSession, home_service: InvestHomeService,
) -> AccountPanelResponse:
    home = await home_service.get_home(user_id=user_id)
    watch_symbols, watch_available = await _load_watch_symbols(db, user_id=user_id)
    return AccountPanelResponse(
        homeSummary=home.homeSummary,
        accounts=home.accounts,
        groupedHoldings=home.groupedHoldings,
        watchSymbols=watch_symbols,
        sourceVisuals=all_visuals(),
        meta=AccountPanelMeta(
            warnings=home.meta.warnings,
            watchlistAvailable=watch_available,
        ),
    )


async def _load_watch_symbols(
    db: AsyncSession, *, user_id: int
) -> tuple[list[WatchSymbol], bool]:
    try:
        from app.models.trading import UserWatchItem
        # Resolve instrument model dynamically — table is `instruments`.
        from app.models.trading import Instrument  # type: ignore
    except ImportError:
        return [], False

    stmt = (
        select(
            Instrument.symbol, Instrument.market, Instrument.name, UserWatchItem.note,
        )
        .join(UserWatchItem, UserWatchItem.instrument_id == Instrument.id)
        .where(UserWatchItem.user_id == user_id, UserWatchItem.is_active.is_(True))
        .order_by(Instrument.market, Instrument.symbol)
    )
    result = await db.execute(stmt)
    items: list[WatchSymbol] = []
    for sym, market, name, note in result.all():
        if not sym or not market:
            continue
        m = str(market).lower()
        if m not in ("kr", "us", "crypto"):
            continue
        items.append(
            WatchSymbol(symbol=str(sym), market=m, displayName=str(name or sym), note=note)
        )
    return items, True
```

NOTE: Executor must verify `Instrument` import path. `grep -n "class Instrument" app/models/` first. Adjust if model name differs (e.g., `Symbol`).

- [ ] **Step 2.3: Add router endpoint**

Modify `app/routers/invest_api.py`. Add after the existing `/home` route:

```python
from app.schemas.invest_account_panel import AccountPanelResponse
from app.services.invest_view_model.account_panel_service import build_account_panel


@router.get("/account-panel")
async def get_account_panel(
    user: Annotated[Any, Depends(get_authenticated_user)],
    service: Annotated[InvestHomeService, Depends(get_invest_home_service)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AccountPanelResponse:
    return await build_account_panel(user_id=user.id, db=db, home_service=service)
```

- [ ] **Step 2.4: Write router test**

`tests/test_invest_account_panel_router.py`:

```python
"""Tests for GET /invest/api/account-panel."""
from __future__ import annotations
import pytest
from unittest.mock import AsyncMock, MagicMock

from app.schemas.invest_home import (
    Account, CashAmounts, HomeSummary, InvestHomeResponse, InvestHomeResponseMeta,
)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_account_panel_combines_home_and_watch(monkeypatch) -> None:
    from app.services.invest_view_model.account_panel_service import build_account_panel

    fake_home = InvestHomeResponse(
        homeSummary=HomeSummary(includedSources=["kis"], excludedSources=[], totalValueKrw=0.0),
        accounts=[Account(
            accountId="a1", displayName="K", source="kis", accountKind="live",
            includedInHome=True, valueKrw=0.0, cashBalances=CashAmounts(),
            buyingPower=CashAmounts(),
        )],
        holdings=[],
        groupedHoldings=[],
        meta=InvestHomeResponseMeta(),
    )
    home_service = MagicMock()
    home_service.get_home = AsyncMock(return_value=fake_home)
    db = MagicMock()
    monkeypatch.setattr(
        "app.services.invest_view_model.account_panel_service._load_watch_symbols",
        AsyncMock(return_value=([], True)),
    )

    resp = await build_account_panel(user_id=1, db=db, home_service=home_service)
    assert resp.homeSummary.includedSources == ["kis"]
    assert len(resp.accounts) == 1
    assert resp.watchSymbols == []
    assert resp.meta.watchlistAvailable is True
    # All known sources represented in sourceVisuals
    sources = {v.source for v in resp.sourceVisuals}
    assert {"kis", "upbit", "alpaca_paper", "kis_mock"}.issubset(sources)
```

- [ ] **Step 2.5: Run tests + lint**

```bash
uv run pytest tests/test_invest_account_panel_router.py -v
uv run ruff check app/services/invest_view_model app/schemas/invest_account_panel.py app/routers/invest_api.py
```

Both should pass.

- [ ] **Step 2.6: Commit**

```bash
git add app/schemas/invest_account_panel.py \
       app/services/invest_view_model/account_panel_service.py \
       app/routers/invest_api.py \
       tests/test_invest_account_panel_router.py
git commit -m "feat(invest): GET /invest/api/account-panel view-model (ROB-141)"
```

---

## Task 3: Backend `/invest/api/feed/news` (ROB-142)

**Files:**
- Create: `app/schemas/invest_feed_news.py`
- Create: `app/services/invest_view_model/feed_news_service.py`
- Modify: `app/routers/invest_api.py` (add `/feed/news`)
- Create: `tests/test_invest_feed_news_router.py`

- [ ] **Step 3.1: Write schema**

`app/schemas/invest_feed_news.py`:

```python
"""ROB-142 — /invest/api/feed/news schema."""
from __future__ import annotations
from datetime import datetime
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

from app.schemas.news_issues import MarketIssue

FeedTab = Literal["top", "latest", "hot", "holdings", "watchlist", "kr", "us", "crypto"]
NewsMarket = Literal["kr", "us", "crypto"]
RelationKind = Literal["held", "watchlist", "both", "none"]


class NewsRelatedSymbol(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str
    market: NewsMarket
    displayName: str


class FeedNewsItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    title: str
    publisher: str | None = None
    feedSource: str | None = None
    publishedAt: datetime | None = None
    market: NewsMarket
    relatedSymbols: list[NewsRelatedSymbol] = Field(default_factory=list)
    issueId: str | None = None
    summarySnippet: str | None = None
    relation: RelationKind = "none"
    url: str


class FeedNewsMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")
    emptyReason: str | None = None
    warnings: list[str] = Field(default_factory=list)


class FeedNewsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tab: FeedTab
    asOf: datetime
    issues: list[MarketIssue] = Field(default_factory=list)
    items: list[FeedNewsItem] = Field(default_factory=list)
    nextCursor: str | None = None
    meta: FeedNewsMeta = Field(default_factory=FeedNewsMeta)
```

- [ ] **Step 3.2: Write service**

`app/services/invest_view_model/feed_news_service.py`:

```python
"""ROB-142 — feed/news view-model assembler."""
from __future__ import annotations
import base64
import json
from datetime import datetime, timezone
from typing import cast

from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.news import NewsArticle, NewsAnalysisResult
from app.schemas.invest_feed_news import (
    FeedNewsItem, FeedNewsMeta, FeedNewsResponse, FeedTab, NewsMarket,
    NewsRelatedSymbol,
)
from app.services.invest_view_model.relation_resolver import RelationResolver
from app.services.news_issue_clustering_service import build_market_issues


def _encode_cursor(published_at: datetime | None, article_id: int) -> str:
    payload = {
        "p": published_at.isoformat() if published_at else None,
        "i": article_id,
    }
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()


def _decode_cursor(cursor: str | None) -> tuple[datetime | None, int | None]:
    if not cursor:
        return None, None
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
        p = payload.get("p")
        return (datetime.fromisoformat(p) if p else None, payload.get("i"))
    except Exception:
        return None, None


async def build_feed_news(
    *,
    db: AsyncSession,
    resolver: RelationResolver,
    tab: FeedTab,
    limit: int,
    cursor: str | None,
) -> FeedNewsResponse:
    market_filter: str | None = None
    if tab in ("kr", "us", "crypto"):
        market_filter = tab

    # Hot issues for top tab.
    issues = []
    if tab in ("top", "hot"):
        try:
            issues_resp = await build_market_issues(market="all", window_hours=24, limit=10)
            issues = issues_resp.items
        except Exception:
            issues = []

    # Base news query.
    stmt = select(NewsArticle).order_by(
        desc(NewsArticle.article_published_at), desc(NewsArticle.id)
    )
    if market_filter:
        stmt = stmt.where(NewsArticle.market == market_filter)

    cursor_dt, cursor_id = _decode_cursor(cursor)
    if cursor_dt and cursor_id:
        stmt = stmt.where(
            (NewsArticle.article_published_at < cursor_dt)
            | (
                (NewsArticle.article_published_at == cursor_dt)
                & (NewsArticle.id < cursor_id)
            )
        )

    stmt = stmt.limit(limit + 1)
    rows = (await db.execute(stmt)).scalars().all()

    next_cursor: str | None = None
    if len(rows) > limit:
        last = rows[limit - 1]
        next_cursor = _encode_cursor(last.article_published_at, last.id)
        rows = list(rows[:limit])

    # Bulk-load summaries for the page.
    article_ids = [r.id for r in rows]
    analysis_map: dict[int, str] = {}
    if article_ids:
        a_stmt = select(NewsAnalysisResult.article_id, NewsAnalysisResult.summary).where(
            NewsAnalysisResult.article_id.in_(article_ids)
        )
        for art_id, summary in (await db.execute(a_stmt)).all():
            analysis_map[art_id] = summary

    items: list[FeedNewsItem] = []
    for row in rows:
        market_value = (row.market or "kr").lower()
        if market_value not in ("kr", "us", "crypto"):
            continue
        related: list[NewsRelatedSymbol] = []
        if row.stock_symbol:
            related.append(
                NewsRelatedSymbol(
                    symbol=row.stock_symbol,
                    market=cast(NewsMarket, market_value),
                    displayName=row.stock_name or row.stock_symbol,
                )
            )
        relation = (
            resolver.relation(market_value, row.stock_symbol)
            if row.stock_symbol else "none"
        )
        items.append(
            FeedNewsItem(
                id=row.id,
                title=row.title,
                publisher=row.source,
                feedSource=row.feed_source,
                publishedAt=row.article_published_at,
                market=cast(NewsMarket, market_value),
                relatedSymbols=related,
                summarySnippet=analysis_map.get(row.id) or row.summary,
                relation=relation,
                url=row.url,
            )
        )

    # Apply holdings/watchlist filters in-memory.
    empty_reason: str | None = None
    if tab == "holdings":
        before = len(items)
        items = [i for i in items if i.relation in ("held", "both")]
        if not resolver.held:
            empty_reason = "no_holdings"
        elif before > 0 and not items:
            empty_reason = "no_matching_news"
    elif tab == "watchlist":
        before = len(items)
        items = [i for i in items if i.relation in ("watchlist", "both")]
        if not resolver.watch:
            empty_reason = "no_watchlist"
        elif before > 0 and not items:
            empty_reason = "no_matching_news"

    return FeedNewsResponse(
        tab=tab,
        asOf=datetime.now(timezone.utc),
        issues=issues,
        items=items,
        nextCursor=next_cursor,
        meta=FeedNewsMeta(emptyReason=empty_reason),
    )
```

- [ ] **Step 3.3: Add router endpoint**

In `app/routers/invest_api.py`:

```python
from typing import Literal
from fastapi import Query
from app.schemas.invest_feed_news import FeedNewsResponse, FeedTab
from app.services.invest_view_model.feed_news_service import build_feed_news
from app.services.invest_view_model.relation_resolver import build_relation_resolver


def _held_pairs_from_home(home) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for h in home.groupedHoldings:
        m = h.market.lower()
        if m in ("kr", "us", "crypto"):
            pairs.append((m, h.symbol))
    return pairs


@router.get("/feed/news")
async def get_feed_news(
    user: Annotated[Any, Depends(get_authenticated_user)],
    service: Annotated[InvestHomeService, Depends(get_invest_home_service)],
    db: Annotated[AsyncSession, Depends(get_db)],
    tab: FeedTab = Query("top"),
    limit: int = Query(30, ge=1, le=100),
    cursor: str | None = Query(None),
) -> FeedNewsResponse:
    home = await service.get_home(user_id=user.id)
    resolver = await build_relation_resolver(
        db, user_id=user.id, held_pairs=_held_pairs_from_home(home)
    )
    return await build_feed_news(
        db=db, resolver=resolver, tab=tab, limit=limit, cursor=cursor
    )
```

- [ ] **Step 3.4: Write tests**

`tests/test_invest_feed_news_router.py`:

```python
"""Tests for feed_news_service."""
from __future__ import annotations
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
import pytest

from app.services.invest_view_model.relation_resolver import RelationResolver


def _fake_article(*, id: int, market: str = "kr", symbol: str | None = None,
                  name: str | None = None, published_at: datetime | None = None) -> MagicMock:
    a = MagicMock()
    a.id = id
    a.market = market
    a.title = f"news {id}"
    a.source = "Reuters"
    a.feed_source = "rss_test"
    a.article_published_at = published_at or datetime(2026, 5, 1, tzinfo=timezone.utc)
    a.stock_symbol = symbol
    a.stock_name = name
    a.summary = "snippet"
    a.url = f"https://example.com/{id}"
    return a


@pytest.mark.unit
@pytest.mark.asyncio
async def test_feed_news_top_tab(monkeypatch) -> None:
    from app.services.invest_view_model import feed_news_service as svc

    db = MagicMock()
    scalar_result = MagicMock()
    scalar_result.scalars.return_value.all.return_value = [
        _fake_article(id=1, market="kr"),
    ]
    summary_result = MagicMock()
    summary_result.all.return_value = []
    db.execute = AsyncMock(side_effect=[scalar_result, summary_result])

    monkeypatch.setattr(
        svc, "build_market_issues", AsyncMock(return_value=MagicMock(items=[]))
    )

    resolver = RelationResolver()
    resp = await svc.build_feed_news(db=db, resolver=resolver, tab="top", limit=30, cursor=None)
    assert resp.tab == "top"
    assert len(resp.items) == 1
    assert resp.items[0].id == 1
    assert resp.items[0].relation == "none"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_feed_news_holdings_empty_when_no_holdings(monkeypatch) -> None:
    from app.services.invest_view_model import feed_news_service as svc

    db = MagicMock()
    scalar_result = MagicMock()
    scalar_result.scalars.return_value.all.return_value = []
    summary_result = MagicMock()
    summary_result.all.return_value = []
    db.execute = AsyncMock(side_effect=[scalar_result, summary_result])

    resolver = RelationResolver()
    resp = await svc.build_feed_news(
        db=db, resolver=resolver, tab="holdings", limit=30, cursor=None
    )
    assert resp.meta.emptyReason == "no_holdings"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_feed_news_assigns_held_relation(monkeypatch) -> None:
    from app.services.invest_view_model import feed_news_service as svc

    db = MagicMock()
    scalar_result = MagicMock()
    scalar_result.scalars.return_value.all.return_value = [
        _fake_article(id=10, market="us", symbol="AAPL", name="Apple"),
    ]
    summary_result = MagicMock()
    summary_result.all.return_value = []
    db.execute = AsyncMock(side_effect=[scalar_result, summary_result])
    monkeypatch.setattr(
        svc, "build_market_issues", AsyncMock(return_value=MagicMock(items=[]))
    )

    resolver = RelationResolver(held={("us", "AAPL")})
    resp = await svc.build_feed_news(
        db=db, resolver=resolver, tab="holdings", limit=30, cursor=None
    )
    assert resp.items[0].relation == "held"
```

- [ ] **Step 3.5: Run tests**

```bash
uv run pytest tests/test_invest_feed_news_router.py -v
```

Expected: pass.

- [ ] **Step 3.6: Commit**

```bash
git add app/schemas/invest_feed_news.py \
       app/services/invest_view_model/feed_news_service.py \
       app/routers/invest_api.py \
       tests/test_invest_feed_news_router.py
git commit -m "feat(invest): GET /invest/api/feed/news view-model (ROB-142)"
```

---

## Task 4: Backend `/invest/api/signals` (ROB-143)

**Files:**
- Create: `app/schemas/invest_signals.py`
- Create: `app/services/invest_view_model/signals_service.py`
- Modify: `app/routers/invest_api.py` (add `/signals`)
- Create: `tests/test_invest_signals_router.py`

- [ ] **Step 4.1: Write schema**

`app/schemas/invest_signals.py`:

```python
"""ROB-143 — /invest/api/signals schema."""
from __future__ import annotations
from datetime import datetime
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

SignalTab = Literal["mine", "kr", "us", "crypto"]
SignalMarket = Literal["kr", "us", "crypto"]
SignalSource = Literal["analysis", "issue", "brief"]
DecisionLabel = Literal["buy", "hold", "sell", "watch", "neutral"]
Severity = Literal["low", "medium", "high"]
RelationKind = Literal["held", "watchlist", "both", "none"]


class SignalRelatedSymbol(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str
    market: SignalMarket
    displayName: str


class SignalCard(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    source: SignalSource
    title: str
    market: SignalMarket
    decisionLabel: DecisionLabel | None = None
    confidence: int | None = None
    severity: Severity | None = None
    summary: str | None = None
    generatedAt: datetime
    relatedSymbols: list[SignalRelatedSymbol] = Field(default_factory=list)
    relatedIssueIds: list[str] = Field(default_factory=list)
    supportingNewsIds: list[int] = Field(default_factory=list)
    rationale: str | None = None
    relation: RelationKind = "none"


class SignalsMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")
    emptyReason: str | None = None
    warnings: list[str] = Field(default_factory=list)


class SignalsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tab: SignalTab
    asOf: datetime
    items: list[SignalCard] = Field(default_factory=list)
    meta: SignalsMeta = Field(default_factory=SignalsMeta)
```

- [ ] **Step 4.2: Write service**

`app/services/invest_view_model/signals_service.py`:

```python
"""ROB-143 — signals view-model assembler."""
from __future__ import annotations
from datetime import datetime, timezone
from typing import cast

from sqlalchemy import select, desc, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.analysis import StockAnalysisResult, StockInfo
from app.schemas.invest_signals import (
    DecisionLabel, SignalCard, SignalMarket, SignalRelatedSymbol, SignalsMeta,
    SignalsResponse, SignalSource, SignalTab,
)
from app.services.invest_view_model.relation_resolver import RelationResolver


def _market_from_instrument_type(t: str | None) -> SignalMarket | None:
    if not t:
        return None
    t = t.lower()
    if t in ("crypto", "coin"):
        return "crypto"
    if "us" in t or t == "overseas":
        return "us"
    if "kr" in t or t == "domestic":
        return "kr"
    return None


def _decision_label(value: str | None) -> DecisionLabel | None:
    if not value:
        return None
    v = value.lower()
    if v in ("buy", "hold", "sell", "watch", "neutral"):
        return cast(DecisionLabel, v)
    return None


async def build_signals(
    *, db: AsyncSession, resolver: RelationResolver, tab: SignalTab, limit: int,
) -> SignalsResponse:
    # Latest analysis per stock_info_id (window function would be ideal; use simple top-N for MVP).
    stmt = (
        select(StockAnalysisResult, StockInfo)
        .join(StockInfo, StockInfo.id == StockAnalysisResult.stock_info_id)
        .order_by(desc(StockAnalysisResult.created_at))
        .limit(limit * 4)  # over-fetch then dedupe
    )
    seen_symbols: set[tuple[str, str]] = set()
    cards: list[SignalCard] = []
    for analysis, info in (await db.execute(stmt)).all():
        market = _market_from_instrument_type(info.instrument_type) or "kr"
        key = (market, info.symbol or "")
        if key in seen_symbols:
            continue
        seen_symbols.add(key)
        relation = resolver.relation(market, info.symbol or "")
        if tab == "mine" and relation == "none":
            continue
        if tab in ("kr", "us", "crypto") and tab != market:
            continue
        cards.append(SignalCard(
            id=f"analysis:{analysis.id}",
            source=cast(SignalSource, "analysis"),
            title=info.name or info.symbol or "(unknown)",
            market=cast(SignalMarket, market),
            decisionLabel=_decision_label(getattr(analysis, "decision", None)),
            confidence=int(analysis.confidence) if getattr(analysis, "confidence", None) is not None else None,
            severity=None,
            summary=getattr(analysis, "detailed_text", None),
            generatedAt=analysis.created_at,
            relatedSymbols=[SignalRelatedSymbol(
                symbol=info.symbol or "",
                market=cast(SignalMarket, market),
                displayName=info.name or info.symbol or "",
            )] if info.symbol else [],
            relation=relation,
            rationale=str(getattr(analysis, "reasons", None)) if getattr(analysis, "reasons", None) else None,
        ))
        if len(cards) >= limit:
            break

    empty_reason: str | None = None
    if tab == "mine" and not cards:
        if not resolver.held and not resolver.watch:
            empty_reason = "no_holdings_or_watchlist"
        else:
            empty_reason = "no_matching_signals"

    return SignalsResponse(
        tab=tab,
        asOf=datetime.now(timezone.utc),
        items=cards,
        meta=SignalsMeta(emptyReason=empty_reason),
    )
```

NOTE: Executor must check `StockAnalysisResult` actual fields by reading `app/models/analysis.py` lines 51+. Adjust attribute names if `decision`/`confidence`/`detailed_text`/`reasons` differ. If `instrument_type` values don't follow `kr/us/crypto`, adapt `_market_from_instrument_type`.

- [ ] **Step 4.3: Add router**

```python
from app.schemas.invest_signals import SignalsResponse, SignalTab
from app.services.invest_view_model.signals_service import build_signals


@router.get("/signals")
async def get_signals(
    user: Annotated[Any, Depends(get_authenticated_user)],
    service: Annotated[InvestHomeService, Depends(get_invest_home_service)],
    db: Annotated[AsyncSession, Depends(get_db)],
    tab: SignalTab = Query("mine"),
    limit: int = Query(20, ge=1, le=100),
) -> SignalsResponse:
    home = await service.get_home(user_id=user.id)
    resolver = await build_relation_resolver(
        db, user_id=user.id, held_pairs=_held_pairs_from_home(home)
    )
    return await build_signals(db=db, resolver=resolver, tab=tab, limit=limit)
```

- [ ] **Step 4.4: Write test**

`tests/test_invest_signals_router.py`:

```python
"""Unit tests for signals_service."""
from __future__ import annotations
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
import pytest

from app.services.invest_view_model.relation_resolver import RelationResolver


def _fake_pair(*, ana_id: int, info_id: int, symbol: str, name: str,
               itype: str = "domestic"):
    info = MagicMock()
    info.id = info_id
    info.symbol = symbol
    info.name = name
    info.instrument_type = itype
    ana = MagicMock()
    ana.id = ana_id
    ana.stock_info_id = info_id
    ana.decision = "buy"
    ana.confidence = 80
    ana.detailed_text = "summary"
    ana.reasons = ["r1"]
    ana.created_at = datetime(2026, 5, 1, tzinfo=timezone.utc)
    return ana, info


@pytest.mark.unit
@pytest.mark.asyncio
async def test_signals_mine_filters_to_held() -> None:
    from app.services.invest_view_model.signals_service import build_signals
    db = MagicMock()
    pair_held = _fake_pair(ana_id=1, info_id=10, symbol="005930", name="삼성전자", itype="domestic")
    pair_other = _fake_pair(ana_id=2, info_id=11, symbol="000660", name="SK하이닉스", itype="domestic")
    result = MagicMock()
    result.all.return_value = [pair_held, pair_other]
    db.execute = AsyncMock(return_value=result)

    resolver = RelationResolver(held={("kr", "005930")})
    resp = await build_signals(db=db, resolver=resolver, tab="mine", limit=20)
    assert len(resp.items) == 1
    assert resp.items[0].relation == "held"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_signals_kr_tab_filters_market() -> None:
    from app.services.invest_view_model.signals_service import build_signals
    db = MagicMock()
    kr_pair = _fake_pair(ana_id=1, info_id=10, symbol="005930", name="삼성", itype="domestic")
    us_pair = _fake_pair(ana_id=2, info_id=11, symbol="AAPL", name="Apple", itype="overseas")
    result = MagicMock()
    result.all.return_value = [kr_pair, us_pair]
    db.execute = AsyncMock(return_value=result)
    resolver = RelationResolver()
    resp = await build_signals(db=db, resolver=resolver, tab="kr", limit=20)
    assert all(i.market == "kr" for i in resp.items)
```

- [ ] **Step 4.5: Run tests + commit**

```bash
uv run pytest tests/test_invest_signals_router.py -v
git add app/schemas/invest_signals.py \
       app/services/invest_view_model/signals_service.py \
       app/routers/invest_api.py \
       tests/test_invest_signals_router.py
git commit -m "feat(invest): GET /invest/api/signals view-model (ROB-143)"
```

---

## Task 5: Backend `/invest/api/calendar` + `/calendar/weekly-summary` (ROB-144)

**Files:**
- Create: `app/schemas/invest_calendar.py`
- Create: `app/services/invest_view_model/calendar_service.py`
- Create: `app/services/invest_view_model/weekly_summary_service.py`
- Modify: `app/routers/invest_api.py`
- Create: `tests/test_invest_calendar_router.py`
- Create: `tests/test_invest_calendar_weekly_summary_router.py`

- [ ] **Step 5.1: Schemas**

`app/schemas/invest_calendar.py`:

```python
"""ROB-144 — /invest/api/calendar + weekly-summary schemas."""
from __future__ import annotations
from datetime import date, datetime
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

CalendarMarket = Literal["kr", "us", "crypto", "global"]
EventType = Literal["earnings", "economic", "disclosure", "crypto", "other"]
RelationKind = Literal["held", "watchlist", "both", "none"]
Badge = Literal["holdings", "watchlist", "major"]
CalendarTab = Literal["all", "economic", "earnings", "disclosure", "crypto"]


class CalendarRelatedSymbol(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str
    market: Literal["kr", "us", "crypto"]
    displayName: str


class CalendarEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    eventId: str
    title: str
    market: CalendarMarket
    eventType: EventType
    eventTimeLocal: datetime | None = None
    source: str
    actual: str | None = None
    forecast: str | None = None
    previous: str | None = None
    relatedSymbols: list[CalendarRelatedSymbol] = Field(default_factory=list)
    relation: RelationKind = "none"
    badges: list[Badge] = Field(default_factory=list)


class CalendarCluster(BaseModel):
    model_config = ConfigDict(extra="forbid")
    clusterId: str
    label: str
    eventType: EventType
    market: CalendarMarket
    eventCount: int
    topEvents: list[CalendarEvent] = Field(default_factory=list)


class CalendarDay(BaseModel):
    model_config = ConfigDict(extra="forbid")
    date: date
    events: list[CalendarEvent] = Field(default_factory=list)
    clusters: list[CalendarCluster] = Field(default_factory=list)


class CalendarMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")
    warnings: list[str] = Field(default_factory=list)


class CalendarResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tab: CalendarTab
    fromDate: date
    toDate: date
    asOf: datetime
    days: list[CalendarDay] = Field(default_factory=list)
    meta: CalendarMeta = Field(default_factory=CalendarMeta)


class WeeklySection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    date: date
    reportType: str
    market: str | None = None
    title: str
    body: str


class WeeklySummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    weekStart: date
    asOf: datetime
    sections: list[WeeklySection] = Field(default_factory=list)
    partial: bool = False
    missingDates: list[date] = Field(default_factory=list)
```

- [ ] **Step 5.2: calendar_service.py**

```python
"""ROB-144 — calendar view-model assembler (uses MarketEventsQueryService)."""
from __future__ import annotations
from datetime import date, datetime, timezone
from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.invest_calendar import (
    Badge, CalendarCluster, CalendarDay, CalendarEvent, CalendarMarket,
    CalendarMeta, CalendarRelatedSymbol, CalendarResponse, CalendarTab, EventType,
)
from app.services.invest_view_model.relation_resolver import RelationResolver
from app.services.market_events.query_service import MarketEventsQueryService

CLUSTER_THRESHOLD = 10


def _normalize_event_type(value: str | None) -> EventType:
    v = (value or "").lower()
    if v in ("earnings", "economic", "disclosure", "crypto"):
        return cast(EventType, v)
    return "other"


def _normalize_market(value: str | None) -> CalendarMarket:
    v = (value or "").lower()
    if v in ("kr", "us", "crypto", "global"):
        return cast(CalendarMarket, v)
    return "global"


async def build_calendar(
    *,
    db: AsyncSession,
    resolver: RelationResolver,
    from_date: date,
    to_date: date,
    tab: CalendarTab,
) -> CalendarResponse:
    svc = MarketEventsQueryService(db)
    range_resp = await svc.list_for_range(from_date, to_date)

    by_day: dict[date, list[CalendarEvent]] = {}
    for raw in getattr(range_resp, "items", []):
        market = _normalize_market(getattr(raw, "market", None))
        etype = _normalize_event_type(getattr(raw, "category", None) or getattr(raw, "event_type", None))
        if tab != "all" and etype != tab:
            continue
        symbol = getattr(raw, "symbol", None)
        related: list[CalendarRelatedSymbol] = []
        relation = "none"
        if symbol and market in ("kr", "us", "crypto"):
            related.append(CalendarRelatedSymbol(
                symbol=str(symbol),
                market=cast("Literal['kr','us','crypto']", market),
                displayName=str(getattr(raw, "symbol_display_name", None) or symbol),
            ))
            relation = resolver.relation(market, symbol)

        badges: list[Badge] = []
        if relation in ("held", "both"):
            badges.append("holdings")
        if relation in ("watchlist", "both"):
            badges.append("watchlist")

        ev = CalendarEvent(
            eventId=str(getattr(raw, "event_id", None) or getattr(raw, "id", None) or ""),
            title=str(getattr(raw, "title", "") or ""),
            market=market,
            eventType=etype,
            eventTimeLocal=getattr(raw, "event_time_local", None) or getattr(raw, "event_time", None),
            source=str(getattr(raw, "source", "") or ""),
            actual=getattr(raw, "actual", None),
            forecast=getattr(raw, "forecast", None),
            previous=getattr(raw, "previous", None),
            relatedSymbols=related,
            relation=relation,  # type: ignore[arg-type]
            badges=badges,
        )
        ev_date = getattr(raw, "event_date", None) or (
            ev.eventTimeLocal.date() if ev.eventTimeLocal else from_date
        )
        by_day.setdefault(ev_date, []).append(ev)

    days: list[CalendarDay] = []
    for d in _date_range(from_date, to_date):
        events = by_day.get(d, [])
        clusters: list[CalendarCluster] = []
        if len(events) > CLUSTER_THRESHOLD:
            grouped: dict[tuple[EventType, CalendarMarket], list[CalendarEvent]] = {}
            for ev in events:
                grouped.setdefault((ev.eventType, ev.market), []).append(ev)
            kept: list[CalendarEvent] = []
            for (etype, market), group in grouped.items():
                if len(group) > 5:
                    clusters.append(CalendarCluster(
                        clusterId=f"{d.isoformat()}:{etype}:{market}",
                        label=f"{etype} {market}".strip(),
                        eventType=etype,
                        market=market,
                        eventCount=len(group),
                        topEvents=group[:5],
                    ))
                else:
                    kept.extend(group)
            events = kept
        days.append(CalendarDay(date=d, events=events, clusters=clusters))

    return CalendarResponse(
        tab=tab,
        fromDate=from_date,
        toDate=to_date,
        asOf=datetime.now(timezone.utc),
        days=days,
        meta=CalendarMeta(),
    )


def _date_range(start: date, end: date):
    from datetime import timedelta
    cur = start
    while cur <= end:
        yield cur
        cur = cur + timedelta(days=1)
```

NOTE: `MarketEventsQueryService.list_for_range` returns a `MarketEventsRangeResponse`. Executor must read `app/schemas/market_events.py` to find the actual field names on each item (likely `event_date`, `category`, `market`, `symbol`, etc.). Adjust `getattr` defaults to match real fields.

- [ ] **Step 5.3: weekly_summary_service.py**

```python
"""ROB-144 — weekly summary composer from existing market_reports."""
from __future__ import annotations
from datetime import date, datetime, timedelta, timezone
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.invest_calendar import WeeklySection, WeeklySummaryResponse
from app.services.market_report_service import get_market_reports

REPORT_TYPES = ("daily_brief", "kr_morning", "crypto_scan")


async def build_weekly_summary(
    *, db: AsyncSession, week_start: date,
) -> WeeklySummaryResponse:
    week_end = week_start + timedelta(days=6)
    sections: list[WeeklySection] = []
    seen_dates: set[date] = set()

    for rt in REPORT_TYPES:
        reports = await get_market_reports(db, report_type=rt, days=14)
        for r in reports:
            r_date = getattr(r, "report_date", None)
            if not r_date:
                continue
            d = r_date if isinstance(r_date, date) else r_date.date()
            if not (week_start <= d <= week_end):
                continue
            seen_dates.add(d)
            sections.append(WeeklySection(
                date=d,
                reportType=rt,
                market=getattr(r, "market", None),
                title=getattr(r, "title", None) or f"{rt} {d.isoformat()}",
                body=str(getattr(r, "body_md", "") or getattr(r, "body", "") or ""),
            ))

    sections.sort(key=lambda s: (s.date, s.reportType))
    all_dates = {week_start + timedelta(days=i) for i in range(7)}
    missing = sorted(all_dates - seen_dates)
    return WeeklySummaryResponse(
        weekStart=week_start,
        asOf=datetime.now(timezone.utc),
        sections=sections,
        partial=bool(missing),
        missingDates=missing,
    )
```

NOTE: Executor must verify `get_market_reports` signature (it accepts `report_type=`, `days=` per spec exploration). And the MarketReport ORM field names — `body_md`/`body`/`title` may differ; adjust accordingly. Read `app/models/market_report.py` first.

- [ ] **Step 5.4: Add router endpoints**

```python
from datetime import date
from app.schemas.invest_calendar import CalendarResponse, CalendarTab, WeeklySummaryResponse
from app.services.invest_view_model.calendar_service import build_calendar
from app.services.invest_view_model.weekly_summary_service import build_weekly_summary


@router.get("/calendar")
async def get_calendar(
    user: Annotated[Any, Depends(get_authenticated_user)],
    service: Annotated[InvestHomeService, Depends(get_invest_home_service)],
    db: Annotated[AsyncSession, Depends(get_db)],
    from_date: date = Query(...),
    to_date: date = Query(...),
    tab: CalendarTab = Query("all"),
) -> CalendarResponse:
    home = await service.get_home(user_id=user.id)
    resolver = await build_relation_resolver(
        db, user_id=user.id, held_pairs=_held_pairs_from_home(home)
    )
    return await build_calendar(
        db=db, resolver=resolver, from_date=from_date, to_date=to_date, tab=tab,
    )


@router.get("/calendar/weekly-summary")
async def get_calendar_weekly_summary(
    user: Annotated[Any, Depends(get_authenticated_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    week_start: date = Query(...),
) -> WeeklySummaryResponse:
    return await build_weekly_summary(db=db, week_start=week_start)
```

- [ ] **Step 5.5: Tests**

`tests/test_invest_calendar_router.py`:

```python
"""Unit tests for calendar_service."""
from __future__ import annotations
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock
import pytest

from app.services.invest_view_model.relation_resolver import RelationResolver


def _fake_event(*, event_id: str, market: str = "us", category: str = "earnings",
                symbol: str | None = None, ev_date: date | None = None):
    e = MagicMock()
    e.event_id = event_id
    e.id = event_id
    e.market = market
    e.category = category
    e.symbol = symbol
    e.symbol_display_name = symbol
    e.title = f"event {event_id}"
    e.event_date = ev_date or date(2026, 5, 4)
    e.event_time_local = None
    e.event_time = None
    e.source = "test"
    e.actual = None
    e.forecast = None
    e.previous = None
    return e


@pytest.mark.unit
@pytest.mark.asyncio
async def test_calendar_returns_per_day(monkeypatch) -> None:
    from app.services.invest_view_model import calendar_service as svc

    fake_resp = MagicMock()
    fake_resp.items = [_fake_event(event_id="e1")]
    fake_query_service = MagicMock()
    fake_query_service.list_for_range = AsyncMock(return_value=fake_resp)
    monkeypatch.setattr(svc, "MarketEventsQueryService", lambda db: fake_query_service)

    db = MagicMock()
    resolver = RelationResolver()
    resp = await svc.build_calendar(
        db=db, resolver=resolver,
        from_date=date(2026, 5, 4), to_date=date(2026, 5, 4), tab="all",
    )
    assert len(resp.days) == 1
    assert len(resp.days[0].events) == 1
    assert resp.days[0].events[0].eventId == "e1"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_calendar_clusters_when_over_threshold(monkeypatch) -> None:
    from app.services.invest_view_model import calendar_service as svc

    fake_resp = MagicMock()
    fake_resp.items = [
        _fake_event(event_id=f"e{i}", category="earnings", market="us") for i in range(15)
    ]
    fake_query_service = MagicMock()
    fake_query_service.list_for_range = AsyncMock(return_value=fake_resp)
    monkeypatch.setattr(svc, "MarketEventsQueryService", lambda db: fake_query_service)

    resp = await svc.build_calendar(
        db=MagicMock(), resolver=RelationResolver(),
        from_date=date(2026, 5, 4), to_date=date(2026, 5, 4), tab="all",
    )
    assert len(resp.days[0].clusters) == 1
    assert resp.days[0].clusters[0].eventCount == 15
```

`tests/test_invest_calendar_weekly_summary_router.py`:

```python
"""Unit tests for weekly_summary_service."""
from __future__ import annotations
from datetime import date
from unittest.mock import AsyncMock, MagicMock
import pytest


@pytest.mark.unit
@pytest.mark.asyncio
async def test_weekly_summary_partial_when_missing_days(monkeypatch) -> None:
    from app.services.invest_view_model import weekly_summary_service as svc

    fake_report = MagicMock()
    fake_report.report_date = date(2026, 5, 4)
    fake_report.market = "kr"
    fake_report.title = "Mon brief"
    fake_report.body_md = "body"

    async def fake_get(db, *, report_type, days):  # noqa: ARG001
        return [fake_report] if report_type == "daily_brief" else []

    monkeypatch.setattr(svc, "get_market_reports", fake_get)
    resp = await svc.build_weekly_summary(db=MagicMock(), week_start=date(2026, 5, 4))
    assert resp.partial is True
    assert len(resp.sections) == 1
    assert resp.sections[0].date == date(2026, 5, 4)
    assert len(resp.missingDates) == 6
```

- [ ] **Step 5.6: Run + commit**

```bash
uv run pytest tests/test_invest_calendar_router.py tests/test_invest_calendar_weekly_summary_router.py -v
git add app/schemas/invest_calendar.py \
       app/services/invest_view_model/calendar_service.py \
       app/services/invest_view_model/weekly_summary_service.py \
       app/routers/invest_api.py \
       tests/test_invest_calendar_router.py \
       tests/test_invest_calendar_weekly_summary_router.py
git commit -m "feat(invest): GET /invest/api/calendar + weekly-summary view-model (ROB-144)"
```

---

## Task 6: SPA shell router `invest_web_spa.py` + safety test

**Files:**
- Create: `app/routers/invest_web_spa.py`
- Modify: `app/main.py` (register router)
- Create: `tests/test_invest_web_spa_router_safety.py`

- [ ] **Step 6.1: Write router**

`app/routers/invest_web_spa.py`:

```python
"""SPA shell router for /invest/ (ROB-141 desktop).

Serves the prebuilt React + Vite bundle from frontend/invest/dist/.
MUST NOT import broker/watch/redis/kis/upbit/task-queue. See safety test.
This router is registered AFTER invest_api and invest_app_spa, so
/invest/api/* and /invest/app/* take precedence.
"""
from __future__ import annotations
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import FileResponse, HTMLResponse, Response

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/invest", tags=["invest-web-spa"])

REPO_ROOT = Path(__file__).resolve().parents[2]
DIST_DIR = REPO_ROOT / "frontend" / "invest" / "dist"
INDEX_FILE = DIST_DIR / "index.html"
ASSETS_DIR = DIST_DIR / "assets"

_BUILD_MISSING_HTML = """\
<!doctype html>
<html><head><meta charset="utf-8"><title>/invest · build missing</title></head>
<body style="font:16px/1.6 ui-sans-serif,system-ui;max-width:680px;margin:4rem auto;padding:0 1rem;">
<h1>/invest · build missing</h1>
<p>The React bundle has not been built yet. Run:</p>
<pre><code>cd frontend/invest &amp;&amp; npm ci &amp;&amp; npm run build</code></pre>
</body></html>
"""


def _no_cache(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response


@router.get("/assets/{asset_path:path}", include_in_schema=False)
async def serve_asset(asset_path: str) -> FileResponse:
    candidate = (ASSETS_DIR / asset_path).resolve()
    try:
        candidate.relative_to(ASSETS_DIR.resolve())
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from exc
    if not candidate.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return FileResponse(candidate)


@router.get("/", include_in_schema=False)
async def spa_index() -> Response:
    return _serve_index()


@router.get("/{full_path:path}", include_in_schema=False)
async def spa_fallback(full_path: str) -> Response:
    # Defensive: never shadow /invest/api/* or /invest/app/* if the router
    # somehow gets ordered above them.
    if full_path.startswith("api/") or full_path.startswith("app/"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return _serve_index()


def _serve_index() -> Response:
    if not INDEX_FILE.is_file():
        logger.warning(
            "SPA build missing at %s; returning 503 build-missing page", INDEX_FILE
        )
        return _no_cache(
            HTMLResponse(
                content=_BUILD_MISSING_HTML,
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        )
    return _no_cache(FileResponse(INDEX_FILE, media_type="text/html"))
```

- [ ] **Step 6.2: Register in main.py**

In `app/main.py`, find the router include block and add:

```python
# In imports section (around line 31):
    invest_web_spa,

# In include_router block (immediately after invest_app_spa):
    app.include_router(invest_web_spa.router)
```

Order matters — must come AFTER `invest_app_spa.router`.

- [ ] **Step 6.3: Safety test**

`tests/test_invest_web_spa_router_safety.py`: copy `tests/test_invest_app_spa_router_safety.py` and change the import to `app.routers.invest_web_spa`. Same forbidden prefix list.

- [ ] **Step 6.4: Run + commit**

```bash
uv run pytest tests/test_invest_web_spa_router_safety.py tests/test_invest_app_spa_router_safety.py -v
git add app/routers/invest_web_spa.py app/main.py tests/test_invest_web_spa_router_safety.py
git commit -m "feat(invest): /invest desktop SPA shell router (ROB-141)"
```

---

## Task 7: Frontend basename migration — mobile under /app/*

**Files:**
- Modify: `frontend/invest/src/routes.tsx`
- Modify: `frontend/invest/src/__tests__/*.test.tsx` (basename + path adjustments)

- [ ] **Step 7.1: Update routes.tsx**

```tsx
// frontend/invest/src/routes.tsx
import { createBrowserRouter, Navigate } from "react-router-dom";
import { DiscoverIssueDetailPage } from "./pages/DiscoverIssueDetailPage";
import { DiscoverPage } from "./pages/DiscoverPage";
import { HomePage } from "./pages/HomePage";
import { PaperPlaceholderPage } from "./pages/PaperPlaceholderPage";

export const router = createBrowserRouter(
  [
    // Mobile (existing) under /app/*
    { path: "/app", element: <HomePage /> },
    { path: "/app/paper", element: <PaperPlaceholderPage /> },
    { path: "/app/paper/:variant", element: <PaperPlaceholderPage /> },
    { path: "/app/discover", element: <DiscoverPage /> },
    { path: "/app/discover/issues/:issueId", element: <DiscoverIssueDetailPage /> },

    // Desktop (added in Tasks 8-11)
    // { path: "/", element: <DesktopHomePage /> },
    // { path: "/feed/news", element: <DesktopFeedNewsPage /> },
    // { path: "/signals", element: <DesktopSignalsPage /> },
    // { path: "/calendar", element: <DesktopCalendarPage /> },

    { path: "*", element: <Navigate to="/app" replace /> },
  ],
  { basename: "/invest" },
);
```

- [ ] **Step 7.2: Fix existing mobile tests**

Each test file under `frontend/invest/src/__tests__/` that uses `basename="/invest/app"` must change to `basename="/invest"` and prepend `/app` to paths. List of files to grep & fix:

```bash
grep -lE 'basename="/invest/app"|"/invest/app/' frontend/invest/src/__tests__/
```

For each file, change:
- `basename="/invest/app"` → `basename="/invest"`
- `initialEntries={["/invest/app/..."]}` stays the same path but basename change matters for `MemoryRouter`.

Concretely for `BottomNav.test.tsx`:
- `expect(link).toHaveAttribute("href", "/invest/app/discover")` — these stay the same since `<NavLink to="/discover">` rendered under basename `/invest` should still produce `/invest/app/discover` ONLY if BottomNav links are updated to `/app/discover`. Review the actual `BottomNav.tsx` and update its links to point under `/app/*`.

- [ ] **Step 7.3: Update BottomNav links**

`frontend/invest/src/components/BottomNav.tsx` — wherever it has `<NavLink to="/discover">`, change to `<NavLink to="/app/discover">`. Same for `<NavLink to="/">` → `<NavLink to="/app">`.

Read the file first, then update.

- [ ] **Step 7.4: Run tests**

```bash
cd frontend/invest && npm test -- --run
```

Expected: all green. Fix any test that referenced the old basename.

- [ ] **Step 7.5: Commit**

```bash
cd /Users/robin/.superset/worktrees/auto_trader/rob-142144-ai-mvp
git add frontend/invest/src
git commit -m "refactor(invest): move mobile routes under /app/*, basename now /invest (ROB-141)"
```

---

## Task 8: Frontend types + API clients

**Files:**
- Modify: `frontend/invest/src/types/invest.ts` (add WatchSymbol, AccountSourceVisual)
- Create: `frontend/invest/src/types/feedNews.ts`
- Create: `frontend/invest/src/types/signals.ts`
- Create: `frontend/invest/src/types/calendar.ts`
- Create: `frontend/invest/src/api/accountPanel.ts`
- Create: `frontend/invest/src/api/feedNews.ts`
- Create: `frontend/invest/src/api/signals.ts`
- Create: `frontend/invest/src/api/calendar.ts`

- [ ] **Step 8.1: Extend invest types**

Append to `frontend/invest/src/types/invest.ts`:

```ts
export type AccountTone = "navy" | "gray" | "purple" | "green" | "dashed";

export interface WatchSymbol {
  symbol: string;
  market: "kr" | "us" | "crypto";
  displayName: string;
  note?: string | null;
}

export interface AccountSourceVisual {
  source: AccountSource;
  tone: AccountTone;
  badge: "Live" | "Mock" | "Crypto" | "Paper" | "Manual";
  displayName: string;
}

export interface AccountPanelMeta {
  warnings: InvestHomeWarning[];
  watchlistAvailable: boolean;
}

export interface AccountPanelResponse {
  homeSummary: HomeSummary;
  accounts: Account[];
  groupedHoldings: GroupedHolding[];
  watchSymbols: WatchSymbol[];
  sourceVisuals: AccountSourceVisual[];
  meta: AccountPanelMeta;
}
```

- [ ] **Step 8.2: feedNews types**

`frontend/invest/src/types/feedNews.ts`:

```ts
import type { MarketIssueResponse } from "./newsIssues";

export type FeedTab = "top" | "latest" | "hot" | "holdings" | "watchlist" | "kr" | "us" | "crypto";
export type RelationKind = "held" | "watchlist" | "both" | "none";

export interface FeedRelatedSymbol {
  symbol: string;
  market: "kr" | "us" | "crypto";
  displayName: string;
}

export interface FeedNewsItem {
  id: number;
  title: string;
  publisher?: string | null;
  feedSource?: string | null;
  publishedAt?: string | null;
  market: "kr" | "us" | "crypto";
  relatedSymbols: FeedRelatedSymbol[];
  issueId?: string | null;
  summarySnippet?: string | null;
  relation: RelationKind;
  url: string;
}

export interface FeedNewsMeta {
  emptyReason?: string | null;
  warnings: string[];
}

export interface FeedNewsResponse {
  tab: FeedTab;
  asOf: string;
  issues: unknown[]; // MarketIssue shape from existing types
  items: FeedNewsItem[];
  nextCursor?: string | null;
  meta: FeedNewsMeta;
}
```

NOTE: If existing `types/newsIssues.ts` exports `MarketIssue`, replace `unknown[]` with `MarketIssue[]` and `import type { MarketIssue }`.

- [ ] **Step 8.3: signals types**

`frontend/invest/src/types/signals.ts`:

```ts
export type SignalTab = "mine" | "kr" | "us" | "crypto";
export type SignalRelation = "held" | "watchlist" | "both" | "none";

export interface SignalRelatedSymbol {
  symbol: string;
  market: "kr" | "us" | "crypto";
  displayName: string;
}

export interface SignalCard {
  id: string;
  source: "analysis" | "issue" | "brief";
  title: string;
  market: "kr" | "us" | "crypto";
  decisionLabel?: "buy" | "hold" | "sell" | "watch" | "neutral" | null;
  confidence?: number | null;
  severity?: "low" | "medium" | "high" | null;
  summary?: string | null;
  generatedAt: string;
  relatedSymbols: SignalRelatedSymbol[];
  relatedIssueIds: string[];
  supportingNewsIds: number[];
  rationale?: string | null;
  relation: SignalRelation;
}

export interface SignalsMeta {
  emptyReason?: string | null;
  warnings: string[];
}

export interface SignalsResponse {
  tab: SignalTab;
  asOf: string;
  items: SignalCard[];
  meta: SignalsMeta;
}
```

- [ ] **Step 8.4: calendar types**

`frontend/invest/src/types/calendar.ts`:

```ts
export type CalendarTab = "all" | "economic" | "earnings" | "disclosure" | "crypto";
export type CalendarMarket = "kr" | "us" | "crypto" | "global";
export type EventType = "earnings" | "economic" | "disclosure" | "crypto" | "other";
export type CalendarRelation = "held" | "watchlist" | "both" | "none";

export interface CalendarRelatedSymbol {
  symbol: string;
  market: "kr" | "us" | "crypto";
  displayName: string;
}

export interface CalendarEvent {
  eventId: string;
  title: string;
  market: CalendarMarket;
  eventType: EventType;
  eventTimeLocal?: string | null;
  source: string;
  actual?: string | null;
  forecast?: string | null;
  previous?: string | null;
  relatedSymbols: CalendarRelatedSymbol[];
  relation: CalendarRelation;
  badges: ("holdings" | "watchlist" | "major")[];
}

export interface CalendarCluster {
  clusterId: string;
  label: string;
  eventType: EventType;
  market: CalendarMarket;
  eventCount: number;
  topEvents: CalendarEvent[];
}

export interface CalendarDay {
  date: string;
  events: CalendarEvent[];
  clusters: CalendarCluster[];
}

export interface CalendarResponse {
  tab: CalendarTab;
  fromDate: string;
  toDate: string;
  asOf: string;
  days: CalendarDay[];
  meta: { warnings: string[] };
}

export interface WeeklySection {
  date: string;
  reportType: string;
  market?: string | null;
  title: string;
  body: string;
}

export interface WeeklySummaryResponse {
  weekStart: string;
  asOf: string;
  sections: WeeklySection[];
  partial: boolean;
  missingDates: string[];
}
```

- [ ] **Step 8.5: API clients**

For each new endpoint, create a thin fetch wrapper. Read the existing `frontend/invest/src/api/investHome.ts` to mirror its style (likely `fetch("/invest/api/home").then(r => r.json())`).

`frontend/invest/src/api/accountPanel.ts`:

```ts
import type { AccountPanelResponse } from "../types/invest";

export async function fetchAccountPanel(): Promise<AccountPanelResponse> {
  const res = await fetch("/invest/api/account-panel", { credentials: "include" });
  if (!res.ok) throw new Error(`account-panel ${res.status}`);
  return res.json();
}
```

`frontend/invest/src/api/feedNews.ts`:

```ts
import type { FeedNewsResponse, FeedTab } from "../types/feedNews";

export async function fetchFeedNews(params: {
  tab: FeedTab; limit?: number; cursor?: string;
}): Promise<FeedNewsResponse> {
  const q = new URLSearchParams();
  q.set("tab", params.tab);
  if (params.limit !== undefined) q.set("limit", String(params.limit));
  if (params.cursor) q.set("cursor", params.cursor);
  const res = await fetch(`/invest/api/feed/news?${q}`, { credentials: "include" });
  if (!res.ok) throw new Error(`feed/news ${res.status}`);
  return res.json();
}
```

`frontend/invest/src/api/signals.ts`:

```ts
import type { SignalsResponse, SignalTab } from "../types/signals";

export async function fetchSignals(params: {
  tab: SignalTab; limit?: number;
}): Promise<SignalsResponse> {
  const q = new URLSearchParams();
  q.set("tab", params.tab);
  if (params.limit !== undefined) q.set("limit", String(params.limit));
  const res = await fetch(`/invest/api/signals?${q}`, { credentials: "include" });
  if (!res.ok) throw new Error(`signals ${res.status}`);
  return res.json();
}
```

`frontend/invest/src/api/calendar.ts`:

```ts
import type { CalendarResponse, CalendarTab, WeeklySummaryResponse } from "../types/calendar";

export async function fetchCalendar(params: {
  fromDate: string; toDate: string; tab?: CalendarTab;
}): Promise<CalendarResponse> {
  const q = new URLSearchParams();
  q.set("from_date", params.fromDate);
  q.set("to_date", params.toDate);
  if (params.tab) q.set("tab", params.tab);
  const res = await fetch(`/invest/api/calendar?${q}`, { credentials: "include" });
  if (!res.ok) throw new Error(`calendar ${res.status}`);
  return res.json();
}

export async function fetchWeeklySummary(weekStart: string): Promise<WeeklySummaryResponse> {
  const q = new URLSearchParams({ week_start: weekStart });
  const res = await fetch(`/invest/api/calendar/weekly-summary?${q}`, { credentials: "include" });
  if (!res.ok) throw new Error(`weekly-summary ${res.status}`);
  return res.json();
}
```

- [ ] **Step 8.6: typecheck + commit**

```bash
cd frontend/invest && npm run typecheck
cd /Users/robin/.superset/worktrees/auto_trader/rob-142144-ai-mvp
git add frontend/invest/src/types frontend/invest/src/api
git commit -m "feat(invest): add desktop view-model types + API clients"
```

---

## Task 9: Frontend `desktop/` shell + RightAccountPanel

**Files:**
- Create: `frontend/invest/src/desktop/AccountSourceTone.ts`
- Create: `frontend/invest/src/desktop/DesktopShell.tsx`
- Create: `frontend/invest/src/desktop/DesktopHeader.tsx`
- Create: `frontend/invest/src/desktop/RightAccountPanel.tsx`
- Create: `frontend/invest/src/__tests__/RightAccountPanel.test.tsx`
- Create: `frontend/invest/src/__tests__/DesktopShell.test.tsx`

- [ ] **Step 9.1: AccountSourceTone.ts**

```ts
import type { AccountSourceVisual, AccountTone } from "../types/invest";

const TONE_STYLE: Record<AccountTone, { color: string; bg: string; border: string }> = {
  navy:   { color: "#dde3ff", bg: "#1e2a55", border: "#3a4a8a" },
  gray:   { color: "#cfd2da", bg: "#2a2d35", border: "#3a3d45" },
  purple: { color: "#e7daff", bg: "#3a2660", border: "#624aa0" },
  green:  { color: "#dcf2e0", bg: "#1f3a2a", border: "#3c6a4d" },
  dashed: { color: "#dbdee5", bg: "#1e2026", border: "#5a5e6a" },
};

export function styleForVisual(v: AccountSourceVisual) {
  const s = TONE_STYLE[v.tone];
  return {
    color: s.color,
    background: s.bg,
    borderStyle: v.tone === "dashed" ? "dashed" : "solid" as const,
    borderColor: s.border,
    borderWidth: 1,
  };
}

export function visualBySource(
  visuals: AccountSourceVisual[],
  source: string,
): AccountSourceVisual | undefined {
  return visuals.find((v) => v.source === source);
}
```

- [ ] **Step 9.2: DesktopShell.tsx**

```tsx
import type { ReactNode } from "react";
import { DesktopHeader } from "./DesktopHeader";

export function DesktopShell({
  left, center, right,
}: { left?: ReactNode; center: ReactNode; right: ReactNode }) {
  return (
    <div data-testid="desktop-shell" style={{ minHeight: "100vh", background: "var(--bg, #0e1014)", color: "var(--text, #e8eaf0)" }}>
      <DesktopHeader />
      <div
        style={{
          display: "grid",
          gridTemplateColumns: left ? "240px minmax(0,1fr) 320px" : "minmax(0,1fr) 320px",
          gap: 24,
          padding: "24px 32px",
          maxWidth: 1440,
          margin: "0 auto",
        }}
      >
        {left ? <aside style={{ minWidth: 0 }}>{left}</aside> : null}
        <main style={{ minWidth: 0 }}>{center}</main>
        <aside style={{ position: "sticky", top: 24, alignSelf: "start", maxHeight: "calc(100vh - 48px)", overflowY: "auto" }}>
          {right}
        </aside>
      </div>
    </div>
  );
}
```

- [ ] **Step 9.3: DesktopHeader.tsx**

```tsx
import { NavLink } from "react-router-dom";

const LINKS = [
  { to: "/", label: "홈", end: true },
  { to: "/feed/news", label: "뉴스" },
  { to: "/signals", label: "시그널" },
  { to: "/calendar", label: "캘린더" },
];

export function DesktopHeader() {
  return (
    <header style={{ display: "flex", gap: 24, padding: "12px 32px", borderBottom: "1px solid var(--surface-2, #1c1e24)" }}>
      <div style={{ fontWeight: 700, fontSize: 16 }}>auto_trader</div>
      <nav style={{ display: "flex", gap: 16 }}>
        {LINKS.map((l) => (
          <NavLink
            key={l.to} to={l.to} end={l.end}
            style={({ isActive }) => ({
              color: isActive ? "#fff" : "#9ba0ab",
              textDecoration: "none", fontSize: 14, padding: "4px 8px",
            })}
          >
            {l.label}
          </NavLink>
        ))}
      </nav>
    </header>
  );
}
```

- [ ] **Step 9.4: RightAccountPanel.tsx**

```tsx
import type { AccountPanelResponse } from "../types/invest";
import { styleForVisual, visualBySource } from "./AccountSourceTone";

function fmtKrw(v?: number | null) {
  if (v == null) return "-";
  return `₩${Math.round(v).toLocaleString("ko-KR")}`;
}

function fmtPct(v?: number | null) {
  if (v == null) return "-";
  return `${(v * 100).toFixed(2)}%`;
}

export function RightAccountPanel({
  data, error, loading,
}: { data?: AccountPanelResponse; error?: string; loading?: boolean }) {
  if (loading || (!data && !error)) {
    return <div data-testid="right-panel-skeleton" style={{ padding: 16 }}>로딩 중…</div>;
  }
  if (error || !data) {
    return (
      <div data-testid="right-panel-error" style={{ padding: 16, color: "#f59e9e" }}>
        계좌 정보를 불러오지 못했습니다.{error ? ` (${error})` : ""}
      </div>
    );
  }
  const totals = data.homeSummary;
  return (
    <div data-testid="right-panel" style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <section style={{ padding: 16, borderRadius: 12, background: "var(--surface, #15181f)" }}>
        <div style={{ fontSize: 12, color: "#9ba0ab" }}>총 자산 (KRW)</div>
        <div style={{ fontSize: 24, fontWeight: 700 }}>{fmtKrw(totals.totalValueKrw)}</div>
        <div style={{ fontSize: 12, color: (totals.pnlRate ?? 0) >= 0 ? "#5ed1a3" : "#f59e9e" }}>
          {fmtKrw(totals.pnlKrw)} · {fmtPct(totals.pnlRate)}
        </div>
      </section>

      <section style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        {data.accounts.length === 0 ? (
          <div style={{ padding: 12, color: "#9ba0ab", fontSize: 12 }}>등록된 계좌가 없습니다.</div>
        ) : (
          data.accounts.map((a) => {
            const v = visualBySource(data.sourceVisuals, a.source);
            const style = v ? styleForVisual(v) : undefined;
            const noBalance = (a.valueKrw ?? 0) === 0 && !a.cashBalances.krw && !a.cashBalances.usd;
            return (
              <article
                key={a.accountId}
                data-testid="right-panel-account"
                data-source={a.source}
                style={{ padding: 12, borderRadius: 10, ...style }}
              >
                <header style={{ display: "flex", justifyContent: "space-between", alignItems: "center", fontSize: 12 }}>
                  <span>{a.displayName}</span>
                  {v && <span style={{ fontSize: 10, padding: "1px 6px", borderRadius: 4, background: "rgba(255,255,255,0.1)" }}>{v.badge}</span>}
                </header>
                <div style={{ fontSize: 16, fontWeight: 700, marginTop: 4 }}>{fmtKrw(a.valueKrw)}</div>
                <div style={{ fontSize: 11, color: (a.pnlRate ?? 0) >= 0 ? "#5ed1a3" : "#f59e9e" }}>
                  {fmtKrw(a.pnlKrw)} · {fmtPct(a.pnlRate)}
                </div>
                {noBalance && <div style={{ fontSize: 11, color: "#9ba0ab", marginTop: 4 }}>잔고 없음</div>}
              </article>
            );
          })
        )}
      </section>

      <section>
        <div style={{ fontSize: 12, color: "#9ba0ab", marginBottom: 4 }}>관심 종목</div>
        {!data.meta.watchlistAvailable ? (
          <div style={{ fontSize: 12, color: "#9ba0ab" }}>관심 종목 데이터를 불러올 수 없습니다.</div>
        ) : data.watchSymbols.length === 0 ? (
          <div data-testid="watchlist-empty" style={{ fontSize: 12, color: "#9ba0ab" }}>등록된 관심 종목이 없습니다.</div>
        ) : (
          <ul style={{ listStyle: "none", padding: 0, margin: 0, display: "flex", flexDirection: "column", gap: 4 }}>
            {data.watchSymbols.slice(0, 8).map((w) => (
              <li key={`${w.market}:${w.symbol}`} style={{ fontSize: 12 }}>
                <span style={{ color: "#9ba0ab", marginRight: 6 }}>{w.market.toUpperCase()}</span>
                {w.displayName}
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
```

- [ ] **Step 9.5: Tests**

`frontend/invest/src/__tests__/RightAccountPanel.test.tsx`:

```tsx
import { render, screen } from "@testing-library/react";
import { RightAccountPanel } from "../desktop/RightAccountPanel";
import type { AccountPanelResponse } from "../types/invest";

const baseResp: AccountPanelResponse = {
  homeSummary: {
    includedSources: ["kis"], excludedSources: [],
    totalValueKrw: 1_000_000, pnlKrw: 50_000, pnlRate: 0.05,
  },
  accounts: [{
    accountId: "k1", displayName: "KIS Live", source: "kis",
    accountKind: "live", includedInHome: true, valueKrw: 1_000_000,
    cashBalances: { krw: 50_000 }, buyingPower: { krw: 50_000 },
  }],
  groupedHoldings: [],
  watchSymbols: [],
  sourceVisuals: [
    { source: "kis", tone: "navy", badge: "Live", displayName: "KIS" },
    { source: "upbit", tone: "purple", badge: "Crypto", displayName: "Upbit" },
  ],
  meta: { warnings: [], watchlistAvailable: true },
};

test("renders skeleton when loading", () => {
  render(<RightAccountPanel loading />);
  expect(screen.getByTestId("right-panel-skeleton")).toBeInTheDocument();
});

test("renders accounts with source-based badge", () => {
  render(<RightAccountPanel data={baseResp} />);
  expect(screen.getByTestId("right-panel")).toBeInTheDocument();
  const card = screen.getByTestId("right-panel-account");
  expect(card.dataset.source).toBe("kis");
  expect(card.textContent).toContain("Live");
});

test("watchlist empty state", () => {
  render(<RightAccountPanel data={baseResp} />);
  expect(screen.getByTestId("watchlist-empty")).toBeInTheDocument();
});
```

`frontend/invest/src/__tests__/DesktopShell.test.tsx`:

```tsx
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { DesktopShell } from "../desktop/DesktopShell";

test("renders left/center/right slots", () => {
  render(
    <MemoryRouter basename="/invest" initialEntries={["/"]}>
      <DesktopShell
        left={<div>L</div>}
        center={<div>C</div>}
        right={<div>R</div>}
      />
    </MemoryRouter>,
  );
  expect(screen.getByTestId("desktop-shell")).toBeInTheDocument();
  expect(screen.getByText("L")).toBeInTheDocument();
  expect(screen.getByText("C")).toBeInTheDocument();
  expect(screen.getByText("R")).toBeInTheDocument();
});
```

- [ ] **Step 9.6: Run + commit**

```bash
cd frontend/invest && npm test -- --run
cd /Users/robin/.superset/worktrees/auto_trader/rob-142144-ai-mvp
git add frontend/invest/src/desktop frontend/invest/src/__tests__/RightAccountPanel.test.tsx frontend/invest/src/__tests__/DesktopShell.test.tsx
git commit -m "feat(invest): DesktopShell + RightAccountPanel (ROB-141)"
```

---

## Task 10: Desktop pages + route wiring

**Files:**
- Create: `frontend/invest/src/pages/desktop/DesktopHomePage.tsx`
- Create: `frontend/invest/src/pages/desktop/DesktopFeedNewsPage.tsx`
- Create: `frontend/invest/src/pages/desktop/DesktopSignalsPage.tsx`
- Create: `frontend/invest/src/pages/desktop/DesktopCalendarPage.tsx`
- Modify: `frontend/invest/src/routes.tsx` (uncomment desktop routes)
- Create: tests for each page

- [ ] **Step 10.1: useAccountPanel hook**

`frontend/invest/src/desktop/useAccountPanel.ts`:

```ts
import { useEffect, useState } from "react";
import type { AccountPanelResponse } from "../types/invest";
import { fetchAccountPanel } from "../api/accountPanel";

export function useAccountPanel() {
  const [data, setData] = useState<AccountPanelResponse | undefined>();
  const [error, setError] = useState<string | undefined>();
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    let cancel = false;
    fetchAccountPanel()
      .then((r) => { if (!cancel) { setData(r); setLoading(false); } })
      .catch((e) => { if (!cancel) { setError(String(e?.message ?? e)); setLoading(false); } });
    return () => { cancel = true; };
  }, []);
  return { data, error, loading };
}
```

- [ ] **Step 10.2: DesktopHomePage.tsx**

```tsx
import { DesktopShell } from "../../desktop/DesktopShell";
import { RightAccountPanel } from "../../desktop/RightAccountPanel";
import { useAccountPanel } from "../../desktop/useAccountPanel";

export function DesktopHomePage() {
  const panel = useAccountPanel();
  return (
    <DesktopShell
      center={
        <section style={{ padding: 24, borderRadius: 12, background: "var(--surface, #15181f)" }}>
          <h1 style={{ fontSize: 18, marginTop: 0 }}>/invest 데스크톱 (read-only)</h1>
          <p style={{ color: "#9ba0ab", fontSize: 13 }}>
            상단 네비게이션에서 뉴스, 시그널, 캘린더로 이동하세요.
          </p>
        </section>
      }
      right={<RightAccountPanel data={panel.data} loading={panel.loading} error={panel.error} />}
    />
  );
}
```

- [ ] **Step 10.3: DesktopFeedNewsPage.tsx**

```tsx
import { useEffect, useState } from "react";
import { DesktopShell } from "../../desktop/DesktopShell";
import { RightAccountPanel } from "../../desktop/RightAccountPanel";
import { useAccountPanel } from "../../desktop/useAccountPanel";
import { fetchFeedNews } from "../../api/feedNews";
import type { FeedNewsResponse, FeedTab } from "../../types/feedNews";

const TABS: { key: FeedTab; label: string }[] = [
  { key: "top", label: "주요" }, { key: "latest", label: "최신" }, { key: "hot", label: "핫이슈" },
  { key: "holdings", label: "보유" }, { key: "watchlist", label: "관심" },
  { key: "kr", label: "국내" }, { key: "us", label: "해외" }, { key: "crypto", label: "크립토" },
];

export function DesktopFeedNewsPage() {
  const panel = useAccountPanel();
  const [tab, setTab] = useState<FeedTab>("top");
  const [data, setData] = useState<FeedNewsResponse | undefined>();
  const [err, setErr] = useState<string | undefined>();
  const [selectedId, setSelectedId] = useState<number | null>(null);

  useEffect(() => {
    let cancel = false;
    setData(undefined); setErr(undefined);
    fetchFeedNews({ tab, limit: 30 })
      .then((r) => !cancel && setData(r))
      .catch((e) => !cancel && setErr(String(e?.message ?? e)));
    return () => { cancel = true; };
  }, [tab]);

  return (
    <DesktopShell
      left={
        <nav style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          {TABS.map((t) => (
            <button
              key={t.key}
              data-testid={`tab-${t.key}`}
              onClick={() => setTab(t.key)}
              style={{
                textAlign: "left", padding: "6px 10px", borderRadius: 6,
                background: tab === t.key ? "var(--surface-2, #1c1e24)" : "transparent",
                color: "#e8eaf0", border: "none", cursor: "pointer", fontSize: 13,
              }}
            >
              {t.label}
            </button>
          ))}
        </nav>
      }
      center={
        <div data-testid="feed-center">
          {err && <div style={{ color: "#f59e9e", marginBottom: 12 }}>오류: {err}</div>}
          {data?.meta?.emptyReason === "no_holdings" && (
            <div style={{ padding: 16, color: "#9ba0ab" }}>보유 종목이 없습니다.</div>
          )}
          {data?.meta?.emptyReason === "no_watchlist" && (
            <div style={{ padding: 16, color: "#9ba0ab" }}>관심 종목이 없습니다.</div>
          )}
          <ul style={{ listStyle: "none", padding: 0, margin: 0, display: "flex", flexDirection: "column", gap: 8 }}>
            {(data?.items ?? []).map((it) => {
              const open = selectedId === it.id;
              return (
                <li
                  key={it.id}
                  data-testid="feed-item"
                  data-relation={it.relation}
                  style={{ padding: 12, borderRadius: 10, background: "var(--surface, #15181f)" }}
                >
                  <button
                    onClick={() => setSelectedId(open ? null : it.id)}
                    style={{ background: "none", border: "none", color: "#e8eaf0", textAlign: "left", padding: 0, cursor: "pointer", width: "100%" }}
                  >
                    <div style={{ fontSize: 14, fontWeight: 600 }}>{it.title}</div>
                    <div style={{ fontSize: 11, color: "#9ba0ab", marginTop: 4 }}>
                      {it.publisher ?? "—"} · {it.market.toUpperCase()}
                      {it.relation !== "none" && <span style={{ marginLeft: 8 }}>[{it.relation}]</span>}
                    </div>
                  </button>
                  {open && it.summarySnippet && (
                    <div style={{ marginTop: 8, fontSize: 13, color: "#cfd2da" }}>{it.summarySnippet}</div>
                  )}
                </li>
              );
            })}
          </ul>
        </div>
      }
      right={<RightAccountPanel data={panel.data} loading={panel.loading} error={panel.error} />}
    />
  );
}
```

- [ ] **Step 10.4: DesktopSignalsPage.tsx**

```tsx
import { useEffect, useState } from "react";
import { DesktopShell } from "../../desktop/DesktopShell";
import { RightAccountPanel } from "../../desktop/RightAccountPanel";
import { useAccountPanel } from "../../desktop/useAccountPanel";
import { fetchSignals } from "../../api/signals";
import type { SignalsResponse, SignalTab, SignalCard } from "../../types/signals";

const TABS: { key: SignalTab; label: string }[] = [
  { key: "mine", label: "내 투자 / 관심" },
  { key: "kr", label: "국내" },
  { key: "us", label: "해외" },
  { key: "crypto", label: "크립토" },
];

export function DesktopSignalsPage() {
  const panel = useAccountPanel();
  const [tab, setTab] = useState<SignalTab>("mine");
  const [data, setData] = useState<SignalsResponse | undefined>();
  const [err, setErr] = useState<string | undefined>();
  const [selected, setSelected] = useState<SignalCard | null>(null);

  useEffect(() => {
    let cancel = false;
    setData(undefined); setErr(undefined); setSelected(null);
    fetchSignals({ tab, limit: 30 })
      .then((r) => !cancel && setData(r))
      .catch((e) => !cancel && setErr(String(e?.message ?? e)));
    return () => { cancel = true; };
  }, [tab]);

  return (
    <DesktopShell
      left={
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <nav style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            {TABS.map((t) => (
              <button
                key={t.key} data-testid={`signal-tab-${t.key}`}
                onClick={() => setTab(t.key)}
                style={{
                  textAlign: "left", padding: "6px 10px", borderRadius: 6,
                  background: tab === t.key ? "var(--surface-2, #1c1e24)" : "transparent",
                  color: "#e8eaf0", border: "none", cursor: "pointer", fontSize: 13,
                }}
              >
                {t.label}
              </button>
            ))}
          </nav>
          {err && <div style={{ color: "#f59e9e" }}>오류: {err}</div>}
          {data?.meta.emptyReason && <div style={{ fontSize: 12, color: "#9ba0ab" }}>결과 없음 ({data.meta.emptyReason})</div>}
          <ul style={{ listStyle: "none", padding: 0, margin: 0, display: "flex", flexDirection: "column", gap: 6 }}>
            {(data?.items ?? []).map((s) => (
              <li key={s.id}>
                <button
                  data-testid="signal-list-item"
                  data-relation={s.relation}
                  onClick={() => setSelected(s)}
                  style={{
                    width: "100%", textAlign: "left", padding: 10, borderRadius: 8,
                    background: selected?.id === s.id ? "var(--surface-2, #1c1e24)" : "var(--surface, #15181f)",
                    border: "none", color: "#e8eaf0", cursor: "pointer",
                  }}
                >
                  <div style={{ fontSize: 13, fontWeight: 600 }}>{s.title}</div>
                  <div style={{ fontSize: 11, color: "#9ba0ab" }}>
                    {s.market.toUpperCase()} · {s.decisionLabel ?? "neutral"}
                    {s.confidence != null && ` · ${s.confidence}%`}
                  </div>
                </button>
              </li>
            ))}
          </ul>
        </div>
      }
      center={
        <section data-testid="signal-detail" style={{ padding: 24, borderRadius: 12, background: "var(--surface, #15181f)" }}>
          {!selected ? (
            <div style={{ color: "#9ba0ab" }}>시그널을 선택하세요.</div>
          ) : (
            <>
              <h2 style={{ fontSize: 18, marginTop: 0 }}>{selected.title}</h2>
              <div style={{ fontSize: 12, color: "#9ba0ab" }}>
                {selected.market.toUpperCase()} · {selected.decisionLabel ?? "neutral"}
                {selected.confidence != null && ` · 신뢰도 ${selected.confidence}%`}
                {` · ${new Date(selected.generatedAt).toLocaleString("ko-KR")}`}
              </div>
              {selected.summary && <p style={{ marginTop: 12 }}>{selected.summary}</p>}
              {selected.rationale && (
                <details style={{ marginTop: 12 }}>
                  <summary style={{ cursor: "pointer", color: "#9ba0ab" }}>근거</summary>
                  <pre style={{ whiteSpace: "pre-wrap", fontSize: 12 }}>{selected.rationale}</pre>
                </details>
              )}
              {selected.relatedSymbols.length > 0 && (
                <div style={{ marginTop: 12, fontSize: 12, color: "#9ba0ab" }}>
                  관련 종목: {selected.relatedSymbols.map((r) => r.displayName).join(", ")}
                </div>
              )}
            </>
          )}
        </section>
      }
      right={<RightAccountPanel data={panel.data} loading={panel.loading} error={panel.error} />}
    />
  );
}
```

- [ ] **Step 10.5: DesktopCalendarPage.tsx**

```tsx
import { useEffect, useMemo, useState } from "react";
import { DesktopShell } from "../../desktop/DesktopShell";
import { RightAccountPanel } from "../../desktop/RightAccountPanel";
import { useAccountPanel } from "../../desktop/useAccountPanel";
import { fetchCalendar, fetchWeeklySummary } from "../../api/calendar";
import type { CalendarResponse, WeeklySummaryResponse } from "../../types/calendar";

function startOfWeek(d: Date): Date {
  const out = new Date(d);
  const day = (out.getDay() + 6) % 7; // Mon=0
  out.setDate(out.getDate() - day);
  out.setHours(0, 0, 0, 0);
  return out;
}

function fmt(d: Date) { return d.toISOString().slice(0, 10); }

export function DesktopCalendarPage() {
  const panel = useAccountPanel();
  const [weekStart, setWeekStart] = useState<Date>(() => startOfWeek(new Date()));
  const weekEnd = useMemo(() => {
    const e = new Date(weekStart);
    e.setDate(e.getDate() + 6);
    return e;
  }, [weekStart]);
  const [selectedDate, setSelectedDate] = useState<string>(fmt(new Date()));
  const [calendar, setCalendar] = useState<CalendarResponse | undefined>();
  const [summary, setSummary] = useState<WeeklySummaryResponse | undefined>();
  const [showSummary, setShowSummary] = useState(false);
  const [err, setErr] = useState<string | undefined>();

  useEffect(() => {
    let cancel = false;
    setErr(undefined);
    fetchCalendar({ fromDate: fmt(weekStart), toDate: fmt(weekEnd), tab: "all" })
      .then((r) => !cancel && setCalendar(r))
      .catch((e) => !cancel && setErr(String(e?.message ?? e)));
    return () => { cancel = true; };
  }, [weekStart, weekEnd]);

  useEffect(() => {
    if (!showSummary) return;
    fetchWeeklySummary(fmt(weekStart)).then(setSummary).catch((e) => setErr(String(e?.message ?? e)));
  }, [showSummary, weekStart]);

  const days = calendar?.days ?? [];
  const selectedDay = days.find((d) => d.date === selectedDate);

  return (
    <DesktopShell
      center={
        <div>
          <header style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 16 }}>
            <button onClick={() => setWeekStart((w) => { const n = new Date(w); n.setDate(n.getDate() - 7); return n; })}>이전 주</button>
            <strong>{fmt(weekStart)} ~ {fmt(weekEnd)}</strong>
            <button onClick={() => setWeekStart((w) => { const n = new Date(w); n.setDate(n.getDate() + 7); return n; })}>다음 주</button>
            <button data-testid="open-weekly-summary" onClick={() => setShowSummary((s) => !s)} style={{ marginLeft: "auto" }}>
              이번주 AI 요약 {showSummary ? "닫기" : "열기"}
            </button>
          </header>

          {err && <div style={{ color: "#f59e9e", marginBottom: 12 }}>오류: {err}</div>}

          <div style={{ display: "flex", gap: 4, marginBottom: 16 }}>
            {days.map((d) => (
              <button
                key={d.date}
                data-testid={`day-${d.date}`}
                onClick={() => setSelectedDate(d.date)}
                style={{
                  flex: 1, padding: "8px 4px", borderRadius: 6,
                  background: selectedDate === d.date ? "var(--surface-2, #1c1e24)" : "var(--surface, #15181f)",
                  border: "none", color: "#e8eaf0", cursor: "pointer", fontSize: 12,
                }}
              >
                {d.date.slice(5)}
                <div style={{ fontSize: 10, color: "#9ba0ab" }}>{d.events.length + d.clusters.length}</div>
              </button>
            ))}
          </div>

          <section data-testid="day-events" style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            {selectedDay?.clusters.map((c) => (
              <details key={c.clusterId} style={{ padding: 12, borderRadius: 10, background: "var(--surface, #15181f)" }}>
                <summary style={{ cursor: "pointer" }}>{c.label} · {c.eventCount}건</summary>
                <ul style={{ listStyle: "none", padding: 0, margin: 0, marginTop: 8 }}>
                  {c.topEvents.map((ev) => (
                    <li key={ev.eventId} style={{ fontSize: 13 }}>{ev.title}</li>
                  ))}
                </ul>
              </details>
            ))}
            {selectedDay?.events.map((ev) => (
              <article key={ev.eventId} data-testid="calendar-event" data-relation={ev.relation} style={{ padding: 12, borderRadius: 10, background: "var(--surface, #15181f)" }}>
                <div style={{ display: "flex", justifyContent: "space-between", fontSize: 13, fontWeight: 600 }}>
                  <span>{ev.title}</span>
                  <span style={{ color: "#9ba0ab", fontSize: 11 }}>{ev.market.toUpperCase()} · {ev.eventType}</span>
                </div>
                {ev.badges.length > 0 && (
                  <div style={{ marginTop: 4, fontSize: 11, color: "#9ba0ab" }}>{ev.badges.join(" · ")}</div>
                )}
              </article>
            ))}
            {selectedDay && selectedDay.events.length === 0 && selectedDay.clusters.length === 0 && (
              <div style={{ padding: 16, color: "#9ba0ab" }}>해당 날짜 이벤트 없음</div>
            )}
          </section>

          {showSummary && (
            <section data-testid="weekly-summary" style={{ marginTop: 16, padding: 16, borderRadius: 12, background: "var(--surface, #15181f)" }}>
              <h3 style={{ marginTop: 0 }}>이번주 AI 요약</h3>
              {!summary && <div>로딩 중…</div>}
              {summary && summary.partial && (
                <div style={{ fontSize: 12, color: "#9ba0ab" }}>일부 일자가 비어있습니다: {summary.missingDates.join(", ")}</div>
              )}
              {summary?.sections.map((sec, i) => (
                <article key={i} style={{ marginTop: 12 }}>
                  <h4 style={{ margin: 0, fontSize: 14 }}>{sec.title}</h4>
                  <pre style={{ whiteSpace: "pre-wrap", fontSize: 12, color: "#cfd2da" }}>{sec.body}</pre>
                </article>
              ))}
            </section>
          )}
        </div>
      }
      right={<RightAccountPanel data={panel.data} loading={panel.loading} error={panel.error} />}
    />
  );
}
```

- [ ] **Step 10.6: Update routes.tsx**

```tsx
// frontend/invest/src/routes.tsx
import { createBrowserRouter, Navigate } from "react-router-dom";
import { DiscoverIssueDetailPage } from "./pages/DiscoverIssueDetailPage";
import { DiscoverPage } from "./pages/DiscoverPage";
import { HomePage } from "./pages/HomePage";
import { PaperPlaceholderPage } from "./pages/PaperPlaceholderPage";
import { DesktopHomePage } from "./pages/desktop/DesktopHomePage";
import { DesktopFeedNewsPage } from "./pages/desktop/DesktopFeedNewsPage";
import { DesktopSignalsPage } from "./pages/desktop/DesktopSignalsPage";
import { DesktopCalendarPage } from "./pages/desktop/DesktopCalendarPage";

export const router = createBrowserRouter(
  [
    { path: "/", element: <DesktopHomePage /> },
    { path: "/feed/news", element: <DesktopFeedNewsPage /> },
    { path: "/signals", element: <DesktopSignalsPage /> },
    { path: "/calendar", element: <DesktopCalendarPage /> },

    { path: "/app", element: <HomePage /> },
    { path: "/app/paper", element: <PaperPlaceholderPage /> },
    { path: "/app/paper/:variant", element: <PaperPlaceholderPage /> },
    { path: "/app/discover", element: <DiscoverPage /> },
    { path: "/app/discover/issues/:issueId", element: <DiscoverIssueDetailPage /> },

    { path: "*", element: <Navigate to="/" replace /> },
  ],
  { basename: "/invest" },
);
```

- [ ] **Step 10.7: Page tests**

`frontend/invest/src/__tests__/DesktopFeedNewsPage.test.tsx`:

```tsx
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { vi, beforeEach, test, expect } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { DesktopFeedNewsPage } from "../pages/desktop/DesktopFeedNewsPage";
import * as feedApi from "../api/feedNews";
import * as panelApi from "../api/accountPanel";

beforeEach(() => {
  vi.spyOn(panelApi, "fetchAccountPanel").mockResolvedValue({
    homeSummary: { includedSources: [], excludedSources: [], totalValueKrw: 0 },
    accounts: [], groupedHoldings: [], watchSymbols: [], sourceVisuals: [],
    meta: { warnings: [], watchlistAvailable: true },
  });
  vi.spyOn(feedApi, "fetchFeedNews").mockResolvedValue({
    tab: "top", asOf: new Date().toISOString(), issues: [], items: [
      { id: 1, title: "n1", market: "kr", relatedSymbols: [], relation: "none", url: "x", publisher: "Reuters" },
    ], meta: { warnings: [] },
  });
});

test("renders news items and reacts to tab change", async () => {
  render(
    <MemoryRouter basename="/invest" initialEntries={["/feed/news"]}>
      <DesktopFeedNewsPage />
    </MemoryRouter>,
  );
  await waitFor(() => expect(screen.getAllByTestId("feed-item")).toHaveLength(1));
  await userEvent.click(screen.getByTestId("tab-latest"));
  await waitFor(() => expect(feedApi.fetchFeedNews).toHaveBeenCalledTimes(2));
});
```

`frontend/invest/src/__tests__/DesktopSignalsPage.test.tsx`:

```tsx
import { render, screen, waitFor } from "@testing-library/react";
import { vi, beforeEach, test, expect } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { DesktopSignalsPage } from "../pages/desktop/DesktopSignalsPage";
import * as signalsApi from "../api/signals";
import * as panelApi from "../api/accountPanel";

beforeEach(() => {
  vi.spyOn(panelApi, "fetchAccountPanel").mockResolvedValue({
    homeSummary: { includedSources: [], excludedSources: [], totalValueKrw: 0 },
    accounts: [], groupedHoldings: [], watchSymbols: [], sourceVisuals: [],
    meta: { warnings: [], watchlistAvailable: true },
  });
  vi.spyOn(signalsApi, "fetchSignals").mockResolvedValue({
    tab: "mine", asOf: new Date().toISOString(),
    items: [{
      id: "analysis:1", source: "analysis", title: "삼성전자", market: "kr",
      decisionLabel: "buy", confidence: 80, generatedAt: new Date().toISOString(),
      relatedSymbols: [], relatedIssueIds: [], supportingNewsIds: [], relation: "held",
    }],
    meta: { warnings: [] },
  });
});

test("renders signal list and shows empty default detail", async () => {
  render(
    <MemoryRouter basename="/invest" initialEntries={["/signals"]}>
      <DesktopSignalsPage />
    </MemoryRouter>,
  );
  await waitFor(() => expect(screen.getAllByTestId("signal-list-item")).toHaveLength(1));
  expect(screen.getByText("시그널을 선택하세요.")).toBeInTheDocument();
});

test("does not render buy/sell CTA buttons", async () => {
  render(
    <MemoryRouter basename="/invest" initialEntries={["/signals"]}>
      <DesktopSignalsPage />
    </MemoryRouter>,
  );
  await waitFor(() => expect(screen.getAllByTestId("signal-list-item")).toHaveLength(1));
  expect(screen.queryByText(/매수/)).not.toBeInTheDocument();
  expect(screen.queryByText(/매도/)).not.toBeInTheDocument();
});
```

`frontend/invest/src/__tests__/DesktopCalendarPage.test.tsx`:

```tsx
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { vi, beforeEach, test, expect } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { DesktopCalendarPage } from "../pages/desktop/DesktopCalendarPage";
import * as calApi from "../api/calendar";
import * as panelApi from "../api/accountPanel";

beforeEach(() => {
  vi.spyOn(panelApi, "fetchAccountPanel").mockResolvedValue({
    homeSummary: { includedSources: [], excludedSources: [], totalValueKrw: 0 },
    accounts: [], groupedHoldings: [], watchSymbols: [], sourceVisuals: [],
    meta: { warnings: [], watchlistAvailable: true },
  });
  vi.spyOn(calApi, "fetchCalendar").mockResolvedValue({
    tab: "all", fromDate: "2026-05-04", toDate: "2026-05-10",
    asOf: new Date().toISOString(),
    days: Array.from({ length: 7 }).map((_, i) => ({
      date: `2026-05-${String(4 + i).padStart(2, "0")}`,
      events: i === 0 ? [{ eventId: "e1", title: "AAPL earnings", market: "us", eventType: "earnings", source: "finnhub", relatedSymbols: [], relation: "none", badges: [] }] : [],
      clusters: [],
    })),
    meta: { warnings: [] },
  });
  vi.spyOn(calApi, "fetchWeeklySummary").mockResolvedValue({
    weekStart: "2026-05-04", asOf: new Date().toISOString(),
    sections: [], partial: false, missingDates: [],
  });
});

test("renders week rail and weekly summary toggle", async () => {
  render(
    <MemoryRouter basename="/invest" initialEntries={["/calendar"]}>
      <DesktopCalendarPage />
    </MemoryRouter>,
  );
  await waitFor(() => expect(screen.getAllByTestId(/^day-/)).toHaveLength(7));
  await userEvent.click(screen.getByTestId("open-weekly-summary"));
  await waitFor(() => expect(screen.getByTestId("weekly-summary")).toBeInTheDocument());
});
```

- [ ] **Step 10.8: Run all FE tests + build + commit**

```bash
cd frontend/invest && npm test -- --run && npm run build
cd /Users/robin/.superset/worktrees/auto_trader/rob-142144-ai-mvp
git add frontend/invest/src
git commit -m "feat(invest): desktop pages + route wiring (ROB-141/142/143/144)"
```

---

## Task 11: Final verification + PR

- [ ] **Step 11.1: Backend full test pass**

```bash
uv run pytest tests/test_invest_view_model_relation_resolver.py \
              tests/test_invest_view_model_safety.py \
              tests/test_invest_account_panel_router.py \
              tests/test_invest_feed_news_router.py \
              tests/test_invest_signals_router.py \
              tests/test_invest_calendar_router.py \
              tests/test_invest_calendar_weekly_summary_router.py \
              tests/test_invest_web_spa_router_safety.py \
              tests/test_invest_app_spa_router_safety.py \
              tests/test_invest_api_router_safety.py \
              tests/test_invest_api_router.py -v
```

- [ ] **Step 11.2: Lint**

```bash
uv run ruff check app/services/invest_view_model app/schemas/invest_*.py app/routers/invest_*.py tests/test_invest_*.py
```

- [ ] **Step 11.3: Frontend typecheck + test + build**

```bash
cd frontend/invest && npm run typecheck && npm test -- --run && npm run build
```

- [ ] **Step 11.4: Push branch**

```bash
cd /Users/robin/.superset/worktrees/auto_trader/rob-142144-ai-mvp
git push -u origin rob-142144-ai-mvp
```

- [ ] **Step 11.5: Create PR**

```bash
gh pr create --base main --title "feat(invest): /invest desktop MVP — RightAccountPanel + feed/signals/calendar (ROB-141/142/143/144)" --body "$(cat <<'EOF'
## Summary

Bundled MVP for desktop-web `/invest` surface covering ROB-141~144:

- **ROB-141** — `/invest` desktop SPA shell + shared 3-column layout + `RightAccountPanel` with KIS/Upbit/Paper/Manual visual treatment
- **ROB-142** — `/invest/feed/news` news feed (issue clusters + per-row held/watchlist relation)
- **ROB-143** — `/invest/signals` AI signals (analysis + issue + brief, no buy/sell CTAs)
- **ROB-144** — `/invest/calendar` weekly date rail + AI weekly summary card (composed from existing daily briefs, no LLM call)

Single Vite bundle extension: basename `/invest`, mobile preserved at `/app/*`. New backend view-model wrappers under `/invest/api/*`. Read-only only — no broker mutation, no Toss API/cookies. New SPA shell router at `/invest/{path}` with import-safety test.

## Test plan

- [x] `make test-fast` — backend unit + safety tests pass
- [x] `cd frontend/invest && npm test -- --run` — all FE tests green
- [x] `cd frontend/invest && npm run build` — bundle builds
- [ ] Manual smoke: `make dev` + visit `/invest`, `/invest/feed/news`, `/invest/signals`, `/invest/calendar`
- [ ] Manual smoke: `/invest/app/*` mobile routes still work
- [ ] Verify `/invest/api/*` not shadowed by SPA fallback

## Safety checks

- `tests/test_invest_view_model_safety.py` — invest_view_model package import guard
- `tests/test_invest_web_spa_router_safety.py` — new SPA router import guard
- Existing `tests/test_invest_api_router_safety.py` continues to pass
- No Toss API / Toss cookie / Toss token references
- No broker order submit / cancel / modify / replace imports
- No watch_order_intent imports

Closes ROB-141, ROB-142, ROB-143, ROB-144.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-review

**1. Spec coverage:**
- ROB-141 RightAccountPanel — Tasks 1, 2, 8, 9, 10
- ROB-141 SPA shell — Task 6
- ROB-141 frontend basename migration — Task 7
- ROB-142 feed/news — Tasks 3, 8, 10
- ROB-143 signals — Tasks 4, 8, 10
- ROB-144 calendar + weekly summary — Tasks 5, 8, 10
- Read-only safety guards — Tasks 1 (view_model_safety), 6 (web_spa_safety)
- Watchlist relation resolver — Task 1
- Account source visual mapping — Task 1, 9

**2. Placeholder scan:** No "TBD"/"TODO"/"implement later" left. NOTE blocks instruct executor to verify ORM field names where exploration was incomplete (instruments table, MarketReport, MarketEvent, StockAnalysisResult) — these are concrete instructions, not placeholders.

**3. Type consistency:**
- `RelationKind` literal is `"held" | "watchlist" | "both" | "none"` everywhere
- `FeedTab`, `SignalTab`, `CalendarTab` defined as Literal in both backend (Pydantic) and frontend (TS)
- `AccountSourceVisual` field set matches between Pydantic (Task 1) and TS (Task 8)
- `WatchSymbol` shape consistent

**4. Outstanding executor judgment calls (intentional NOTEs):**
- Instrument model import path (Task 1.3, 2.2) — depends on actual model name in `app/models/`
- StockAnalysisResult attribute names (Task 4.2)
- MarketEvent item field names (Task 5.2)
- MarketReport attribute names (Task 5.3)
- BottomNav internal links (Task 7.3)

These each say "executor must check first" — not placeholders.

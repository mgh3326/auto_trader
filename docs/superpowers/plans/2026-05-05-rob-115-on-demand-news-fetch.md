# ROB-115 — Research Pipeline On-Demand News Fetch + Social Removal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Research Pipeline의 `NewsStageAnalyzer`가 종목별 뉴스가 부족할 때 KR=Naver / US=Finnhub provider로 on-demand fetch해서 영속화 후 분석에 사용하게 하고, placeholder인 social stage/탭/route를 제품 표면에서 제거한다 (legacy 데이터/스키마 호환은 유지).

**Architecture:** 새 service `app/services/research_news_service.py`가 provider-agnostic interface(`fetch_symbol_news`)를 노출. 내부적으로 `naver_finance.fetch_news`(KR) / `_fetch_news_finnhub`(US)를 timeout/예외 fallback으로 감싸서 `NormalizedArticle` list로 정규화. `NewsStageAnalyzer`는 DB lookup → 부족 시 fetch → `bulk_create_news_articles`로 영속화(`feed_source="research_on_demand_*"`) → 재조회 → 기존 sentiment/themes 로직 적용. Social은 backend pipeline analyzers list에서 제거하고 frontend route/tab/페이지 삭제, 단 schema Literal과 legacy 컴포넌트(CitedStageSidebar fallback 등)는 유지.

**Tech Stack:** Python 3.13 / FastAPI / SQLAlchemy async / Pydantic v2 / pytest / React 18 / TypeScript / Vitest / React Router v6.

**Spec reference:** `docs/superpowers/specs/2026-05-05-rob-115-on-demand-news-fetch-design.md`

---

## File Plan

**신규**

- `app/services/research_news_service.py` — provider-agnostic on-demand news fetcher
- `tests/services/test_research_news_service.py` — service unit tests
- `tests/test_news_stage_on_demand.py` — NewsStageAnalyzer integration tests
- `tests/test_research_pipeline_no_social.py` — pipeline analyzer wiring test

**수정**

- `app/analysis/stages/base.py` — `StageContext.symbol_name` 추가
- `app/analysis/pipeline.py` — `StageContext`에 name 전달, `SocialStageAnalyzer` 제거
- `app/analysis/stages/news_stage.py` — on-demand fetch + persist 흐름
- `app/analysis/stages/social_stage.py` — deprecation 주석 (파일은 유지)
- `frontend/trading-decision/src/routes.tsx` — social route + import 제거
- `frontend/trading-decision/src/pages/research/ResearchSessionLayout.tsx` — STAGE_NAV에서 social 제거
- `frontend/trading-decision/src/__tests__/routes.test.tsx` — social route 검증을 not-found 검증으로 교체
- `frontend/trading-decision/src/__tests__/ResearchSessionRoutes.test.tsx` — social placeholder 테스트 제거 + STAGE_NAV 단언 추가

**삭제**

- `frontend/trading-decision/src/pages/research/ResearchSocialPage.tsx`
- `frontend/trading-decision/src/components/ResearchSocialTab.tsx`

**유지** (legacy 호환)

- `app/schemas/research_pipeline.py` — `Literal[..., "social"]`, `SocialSignals` 그대로
- `app/services/llm_news_service.py` — 기존 helper 그대로
- `frontend/trading-decision/src/api/types.ts` — `SocialSignals`, `StageType` 그대로
- `frontend/trading-decision/src/components/CitedStageSidebar.tsx` — social fallback 그대로
- `frontend/trading-decision/src/i18n/ko.ts` — `social: "소셜"` 그대로
- `frontend/trading-decision/src/test/fixtures/research.ts` — social fixture 그대로

---

## Task 1: Add `symbol_name` to `StageContext`

`NewsStageAnalyzer`가 `news_articles.stock_name`을 채울 수 있도록 stage context에 종목명을 전달.

**Files:**

- Modify: `app/analysis/stages/base.py`
- Modify: `app/analysis/pipeline.py:69-75`

- [ ] **Step 1: Update `StageContext` dataclass**

`app/analysis/stages/base.py` 전체:

```python
# app/analysis/stages/base.py
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar

from app.schemas.research_pipeline import StageOutput


@dataclass(frozen=True)
class StageContext:
    session_id: int
    symbol: str
    instrument_type: str
    user_id: int | None = None
    symbol_name: str | None = None


class BaseStageAnalyzer(ABC):
    stage_type: ClassVar[str]  # override in subclass

    @abstractmethod
    async def analyze(self, ctx: StageContext) -> StageOutput: ...

    async def run(self, ctx: StageContext) -> StageOutput:
        out = await self.analyze(ctx)
        if out.stage_type != self.stage_type:
            raise ValueError(
                f"stage_type mismatch: analyzer={self.stage_type} output={out.stage_type}"
            )
        return out
```

- [ ] **Step 2: Pass `name` from pipeline into context**

`app/analysis/pipeline.py:69-75` 부근의 `StageContext(...)` 생성 부분을 수정:

```python
    ctx = StageContext(
        session_id=session_id,
        symbol=symbol,
        instrument_type=instrument_type,
        user_id=user_id,
        symbol_name=name,
    )
```

- [ ] **Step 3: Run existing pipeline / stage tests**

```bash
uv run pytest tests/ -v -k "stage or pipeline" 2>&1 | tail -30
```

Expected: 기존 테스트 모두 PASS (필드 추가는 default가 있어 기존 호출자 호환).

- [ ] **Step 4: Commit**

```bash
git add app/analysis/stages/base.py app/analysis/pipeline.py
git commit -m "feat(ROB-115): plumb symbol_name into StageContext

Allows downstream stage analyzers (news first) to populate
stock_name on persisted artifacts."
```

---

## Task 2a: `research_news_service` — KR provider routing test (failing)

TDD: KR(equity_kr) symbol을 주면 Naver provider를 호출해 `NormalizedArticle` list로 정규화해서 반환.

**Files:**

- Test: `tests/services/test_research_news_service.py`

- [ ] **Step 1: Write the failing test**

`tests/services/test_research_news_service.py` 생성:

```python
"""Tests for app.services.research_news_service (ROB-115)."""

from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from app.services import research_news_service


class TestFetchSymbolNewsKR:
    """KR symbols route to Naver and normalize the response."""

    async def test_returns_normalized_articles_for_kr_symbol(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_naver_payload = [
            {
                "title": "삼성전자 호실적 발표",
                "url": "https://finance.naver.com/item/news_read.naver?code=005930&id=1",
                "source": "한국경제",
                "datetime": "2026-05-05T09:30",
            },
            {
                "title": "반도체 업황 회복",
                "url": "https://finance.naver.com/item/news_read.naver?code=005930&id=2",
                "source": "매일경제",
                "datetime": "2026-05-04",
            },
        ]
        monkeypatch.setattr(
            research_news_service,
            "_naver_fetch_news",
            AsyncMock(return_value=fake_naver_payload),
        )

        result = await research_news_service.fetch_symbol_news(
            "005930", "equity_kr", limit=20
        )

        assert len(result) == 2
        first = result[0]
        assert first.title == "삼성전자 호실적 발표"
        assert first.url.startswith("https://finance.naver.com/")
        assert first.source == "한국경제"
        assert first.provider == "naver"
        assert isinstance(first.published_at, datetime)
        assert first.summary is None
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/services/test_research_news_service.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.research_news_service'`.

---

## Task 2b: `research_news_service` — minimal KR routing implementation

- [ ] **Step 1: Create the service module with KR routing**

`app/services/research_news_service.py` 생성:

```python
"""Research pipeline on-demand news fetcher (ROB-115).

Provider-agnostic interface that routes per instrument_type:
    equity_kr -> Naver Finance
    equity_us -> Finnhub
    crypto / unknown -> [] (out of scope this iteration)

The service NEVER raises to the caller. Failures (timeout, missing API
key, network/parse error) degrade to an empty list with a warning log.
This keeps Research Pipeline news stage available even when external
sources are flaky.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.services import naver_finance

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class NormalizedArticle:
    url: str
    title: str
    source: str | None
    summary: str | None
    published_at: datetime | None
    provider: str


# Indirection seam so tests can monkeypatch without touching the
# provider package directly.
async def _naver_fetch_news(symbol: str, limit: int) -> list[dict[str, Any]]:
    return await naver_finance.fetch_news(symbol, limit=limit)


def _parse_iso_or_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        pass
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return None


def _normalize_naver(items: list[dict[str, Any]]) -> list[NormalizedArticle]:
    out: list[NormalizedArticle] = []
    for raw in items:
        url = (raw.get("url") or "").strip()
        title = (raw.get("title") or "").strip()
        if not url or not title:
            continue
        out.append(
            NormalizedArticle(
                url=url,
                title=title,
                source=raw.get("source") or None,
                summary=None,
                published_at=_parse_iso_or_date(raw.get("datetime")),
                provider="naver",
            )
        )
    return out


async def fetch_symbol_news(
    symbol: str,
    instrument_type: str,
    *,
    limit: int = 20,
    timeout_s: float = 5.0,
) -> list[NormalizedArticle]:
    """Fetch symbol-specific news. Returns [] on any failure."""
    try:
        if instrument_type == "equity_kr":
            items = await asyncio.wait_for(
                _naver_fetch_news(symbol, limit), timeout=timeout_s
            )
            return _normalize_naver(items)
        # equity_us / crypto / unknown — added in later tasks
        return []
    except Exception as exc:  # noqa: BLE001 — must not raise to caller
        logger.warning(
            "research_news_service.fetch_symbol_news failed: "
            "symbol=%s instrument_type=%s err=%s",
            symbol,
            instrument_type,
            exc,
        )
        return []
```

- [ ] **Step 2: Run KR test**

```bash
uv run pytest tests/services/test_research_news_service.py -v -k "TestFetchSymbolNewsKR"
```

Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add app/services/research_news_service.py tests/services/test_research_news_service.py
git commit -m "feat(ROB-115): research_news_service KR routing via Naver

New provider-agnostic helper for the research pipeline. equity_kr
symbols route to naver_finance.fetch_news and normalize to
NormalizedArticle. Failures return [] (never raise) — pipeline must
degrade gracefully when external sources flake."
```

---

## Task 2c: `research_news_service` — US (Finnhub) routing

- [ ] **Step 1: Add failing US test**

`tests/services/test_research_news_service.py`에 추가:

```python
class TestFetchSymbolNewsUS:
    async def test_returns_normalized_articles_for_us_symbol(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_finnhub_payload = {
            "symbol": "AMZN",
            "market": "us",
            "source": "finnhub",
            "count": 1,
            "news": [
                {
                    "title": "Amazon beats Q1 earnings",
                    "source": "Reuters",
                    "datetime": "2026-05-05T13:30:00",
                    "url": "https://reuters.com/amzn-q1",
                    "summary": "Amazon reported revenue of $X.",
                    "sentiment": None,
                    "related": "AMZN",
                }
            ],
        }
        monkeypatch.setattr(
            research_news_service,
            "_finnhub_fetch_news",
            AsyncMock(return_value=fake_finnhub_payload),
        )

        result = await research_news_service.fetch_symbol_news(
            "AMZN", "equity_us", limit=20
        )

        assert len(result) == 1
        first = result[0]
        assert first.title == "Amazon beats Q1 earnings"
        assert first.url == "https://reuters.com/amzn-q1"
        assert first.source == "Reuters"
        assert first.summary == "Amazon reported revenue of $X."
        assert first.provider == "finnhub"
        assert first.published_at == datetime(2026, 5, 5, 13, 30, 0)
```

- [ ] **Step 2: Run — expect FAIL**

```bash
uv run pytest tests/services/test_research_news_service.py::TestFetchSymbolNewsUS -v
```

Expected: FAIL — `_finnhub_fetch_news` 미존재 또는 routing이 빈 list 반환.

- [ ] **Step 3: Add Finnhub seam + normalization in service**

`app/services/research_news_service.py` 수정:

`from app.services import naver_finance` 아래에 추가:

```python
from app.mcp_server.tooling.fundamentals_sources_finnhub import (
    _fetch_news_finnhub,
)
```

`_naver_fetch_news` 아래에 추가:

```python
async def _finnhub_fetch_news(
    symbol: str, market: str, limit: int
) -> dict[str, Any]:
    return await _fetch_news_finnhub(symbol, market, limit)
```

`_normalize_naver` 아래에 추가:

```python
def _normalize_finnhub(payload: dict[str, Any]) -> list[NormalizedArticle]:
    items = payload.get("news") or []
    out: list[NormalizedArticle] = []
    for raw in items:
        url = (raw.get("url") or "").strip()
        title = (raw.get("title") or "").strip()
        if not url or not title:
            continue
        out.append(
            NormalizedArticle(
                url=url,
                title=title,
                source=raw.get("source") or None,
                summary=raw.get("summary") or None,
                published_at=_parse_iso_or_date(raw.get("datetime")),
                provider="finnhub",
            )
        )
    return out
```

`fetch_symbol_news`의 `if instrument_type == "equity_kr":` 블록 바로 다음에 분기 추가:

```python
        if instrument_type == "equity_us":
            payload = await asyncio.wait_for(
                _finnhub_fetch_news(symbol, "us", limit),
                timeout=timeout_s,
            )
            return _normalize_finnhub(payload)
```

- [ ] **Step 4: Run all service tests**

```bash
uv run pytest tests/services/test_research_news_service.py -v
```

Expected: KR + US 테스트 모두 PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/research_news_service.py tests/services/test_research_news_service.py
git commit -m "feat(ROB-115): research_news_service US routing via Finnhub

equity_us symbols route to fundamentals_sources_finnhub._fetch_news_finnhub
and normalize to NormalizedArticle (preserving summary/source where
available). Service still never raises — Finnhub failures and missing
API key fall through to []."
```

---

## Task 2d: `research_news_service` — graceful degrade tests

`fetch_symbol_news`가 timeout / provider exception / unsupported instrument_type 모두 `[]`를 반환하고 raise하지 않는지 검증.

**Files:**

- Modify: `tests/services/test_research_news_service.py`

- [ ] **Step 1: Add degrade tests**

`tests/services/test_research_news_service.py` 끝에 추가:

```python
class TestFetchSymbolNewsDegrade:
    async def test_naver_exception_returns_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            research_news_service,
            "_naver_fetch_news",
            AsyncMock(side_effect=RuntimeError("scrape blocked")),
        )

        result = await research_news_service.fetch_symbol_news(
            "005930", "equity_kr"
        )

        assert result == []

    async def test_finnhub_value_error_returns_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            research_news_service,
            "_finnhub_fetch_news",
            AsyncMock(
                side_effect=ValueError("FINNHUB_API_KEY environment variable is not set")
            ),
        )

        result = await research_news_service.fetch_symbol_news(
            "AMZN", "equity_us"
        )

        assert result == []

    async def test_timeout_returns_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _slow(*_args: object, **_kwargs: object) -> list[dict[str, str]]:
            await asyncio.sleep(2.0)
            return []

        monkeypatch.setattr(
            research_news_service, "_naver_fetch_news", _slow
        )

        result = await research_news_service.fetch_symbol_news(
            "005930", "equity_kr", timeout_s=0.05
        )

        assert result == []

    async def test_crypto_returns_empty(self) -> None:
        result = await research_news_service.fetch_symbol_news(
            "BTC", "crypto"
        )
        assert result == []

    async def test_unknown_instrument_type_returns_empty(self) -> None:
        result = await research_news_service.fetch_symbol_news(
            "X", "equity_unknown"
        )
        assert result == []
```

- [ ] **Step 2: Run tests**

```bash
uv run pytest tests/services/test_research_news_service.py -v
```

Expected: 모두 PASS (Task 2b/2c 구현이 이미 broad except + crypto fall-through로 빈 list를 보장).

- [ ] **Step 3: Commit**

```bash
git add tests/services/test_research_news_service.py
git commit -m "test(ROB-115): research_news_service degrade-to-empty contract

Pins behavior that pipeline relies on: provider raise, missing API key,
timeout, crypto, and unknown instrument types all return [] without
raising."
```

---

## Task 3a: `NewsStageAnalyzer` — extract sentiment helper

먼저 기존 `_fetch_recent_headlines` 안에서 "DB row → signals dict" 부분을 따로 빼서 on-demand fetch 흐름에서 재사용 가능하게 한다. 행동은 그대로.

**Files:**

- Modify: `app/analysis/stages/news_stage.py`

- [ ] **Step 1: Run existing news stage tests baseline (so we know what we shouldn't break)**

```bash
uv run pytest tests/ -v -k "news_stage or news_signals" 2>&1 | tail -20
```

Expected: 기존에 통과하던 것은 통과. 결과 메모해둔다.

- [ ] **Step 2: Refactor — extract `_compute_signals_from_articles`**

`app/analysis/stages/news_stage.py` 안의 `_fetch_recent_headlines`에서 sentiment/themes 계산 부분을 함수로 분리.

`_fetch_recent_headlines` 위에 새 helper 추가:

```python
def _compute_signals_from_articles(articles: list) -> dict[str, Any]:
    """Compute headline_count, sentiment_score, top_themes from NewsArticle rows."""
    if not articles:
        return {
            "headlines": [],
            "headline_count": 0,
            "sentiment_score": 0.0,
            "top_themes": [],
            "urgent_flags": [],
            "newest_age_minutes": 0,
        }

    POS_KEYWORDS = {
        "상승", "호재", "급등", "매수", "수익", "성장", "실적발표", "흑자",
        "soaring", "positive", "bullish", "buy", "growth", "earnings",
        "beat", "outperform",
    }
    NEG_KEYWORDS = {
        "하락", "악재", "급락", "매도", "손실", "위기", "적자", "전망하치",
        "falling", "negative", "bearish", "sell", "loss", "crisis",
        "miss", "underperform",
    }

    sentiments: list[float] = []
    themes: list[str] = []
    newest_dt = None

    for article in articles:
        if article.article_published_at:
            if newest_dt is None or article.article_published_at > newest_dt:
                newest_dt = article.article_published_at

        score = 0.0
        title_lower = article.title.lower()
        if any(kw in title_lower for kw in POS_KEYWORDS):
            score += 0.5
        if any(kw in title_lower for kw in NEG_KEYWORDS):
            score -= 0.5
        score = max(-1.0, min(1.0, score))
        sentiments.append(score)

        if article.keywords and isinstance(article.keywords, list):
            themes.extend(article.keywords)

    avg_sentiment = sum(sentiments) / len(sentiments) if sentiments else 0.0

    unique_themes: list[str] = []
    for t in themes:
        if t not in unique_themes:
            unique_themes.append(t)
    top_themes = unique_themes[:10]

    newest_age_minutes = 0
    if newest_dt:
        now = now_kst_naive()
        diff = now - newest_dt.replace(tzinfo=None)
        newest_age_minutes = max(0, int(diff.total_seconds() / 60))

    return {
        "headlines": [
            {"title": a.title, "published_at": a.article_published_at}
            for a in articles
        ],
        "headline_count": len(articles),
        "sentiment_score": round(avg_sentiment, 2),
        "top_themes": top_themes,
        "urgent_flags": [],
        "newest_age_minutes": newest_age_minutes,
    }
```

`_fetch_recent_headlines` 본문은 다음으로 줄인다:

```python
async def _fetch_recent_headlines(symbol: str, instrument_type: str) -> dict[str, Any]:
    """Fetch recent headlines and compute basic sentiment/themes."""
    market = "kr"
    if instrument_type == "equity_us":
        market = "us"
    elif instrument_type == "crypto":
        market = "crypto"

    articles, _total = await get_news_articles(
        stock_symbol=symbol, market=market, hours=24, limit=20
    )
    return _compute_signals_from_articles(articles)
```

- [ ] **Step 3: Re-run baseline tests**

```bash
uv run pytest tests/ -v -k "news_stage or news_signals" 2>&1 | tail -20
```

Expected: 같은 테스트 PASS (refactor — 행동 변화 없음).

- [ ] **Step 4: Commit**

```bash
git add app/analysis/stages/news_stage.py
git commit -m "refactor(ROB-115): extract _compute_signals_from_articles

Pure-function split inside news stage so the on-demand fetch path can
reuse the same sentiment/theme reducer without duplication. No behavior
change."
```

---

## Task 3b: `NewsStageAnalyzer` — on-demand fetch + persist (failing test)

**Files:**

- Test: `tests/test_news_stage_on_demand.py`

- [ ] **Step 1: Write the failing test**

`tests/test_news_stage_on_demand.py` 생성:

```python
"""ROB-115 — NewsStageAnalyzer on-demand fetch behavior."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.analysis.stages import news_stage
from app.analysis.stages.base import StageContext
from app.analysis.stages.news_stage import NewsStageAnalyzer
from app.services.research_news_service import NormalizedArticle


def _fake_db_article(
    *,
    title: str = "기존기사",
    published_at: datetime | None = None,
    keywords: list[str] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        title=title,
        article_published_at=published_at,
        keywords=keywords or [],
    )


class TestNewsStageOnDemandFetch:
    async def test_skips_fetch_when_db_has_enough_rows(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        existing = [
            _fake_db_article(title="기존1"),
            _fake_db_article(title="기존2"),
            _fake_db_article(title="기존3"),
        ]
        get_articles = AsyncMock(return_value=(existing, 3))
        fetch = AsyncMock(return_value=[])
        bulk_create = AsyncMock(return_value=(0, 0, []))
        monkeypatch.setattr(news_stage, "get_news_articles", get_articles)
        monkeypatch.setattr(news_stage, "fetch_symbol_news", fetch)
        monkeypatch.setattr(news_stage, "bulk_create_news_articles", bulk_create)

        analyzer = NewsStageAnalyzer()
        out = await analyzer.analyze(
            StageContext(
                session_id=1,
                symbol="005930",
                instrument_type="equity_kr",
                symbol_name="삼성전자",
            )
        )

        assert out.signals.headline_count == 3
        fetch.assert_not_awaited()
        bulk_create.assert_not_awaited()

    async def test_triggers_fetch_when_db_below_threshold(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        first_call_articles: list[SimpleNamespace] = []
        second_call_articles = [
            _fake_db_article(
                title="삼성전자 호실적",
                published_at=datetime(2026, 5, 5, 9, 0, 0),
                keywords=["earnings"],
            ),
        ]
        get_articles = AsyncMock(
            side_effect=[
                (first_call_articles, 0),
                (second_call_articles, 1),
            ]
        )
        fetch = AsyncMock(
            return_value=[
                NormalizedArticle(
                    url="https://finance.naver.com/x",
                    title="삼성전자 호실적",
                    source="한국경제",
                    summary=None,
                    published_at=datetime(2026, 5, 5, 9, 0, 0),
                    provider="naver",
                )
            ]
        )
        bulk_create = AsyncMock(return_value=(1, 0, []))
        monkeypatch.setattr(news_stage, "get_news_articles", get_articles)
        monkeypatch.setattr(news_stage, "fetch_symbol_news", fetch)
        monkeypatch.setattr(news_stage, "bulk_create_news_articles", bulk_create)

        analyzer = NewsStageAnalyzer()
        out = await analyzer.analyze(
            StageContext(
                session_id=1,
                symbol="005930",
                instrument_type="equity_kr",
                symbol_name="삼성전자",
            )
        )

        fetch.assert_awaited_once()
        bulk_create.assert_awaited_once()
        # bulk_create payload tags symbol/name and uses on-demand feed_source
        payload = bulk_create.await_args.args[0]
        assert payload[0].stock_symbol == "005930"
        assert payload[0].stock_name == "삼성전자"
        assert payload[0].feed_source == "research_on_demand_naver"
        assert payload[0].market == "kr"
        # signals reflect the refetched DB state
        assert out.signals.headline_count == 1

    async def test_fetch_failure_degrades_to_neutral(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        get_articles = AsyncMock(return_value=([], 0))
        fetch = AsyncMock(return_value=[])  # service returns [] on failure
        bulk_create = AsyncMock(return_value=(0, 0, []))
        monkeypatch.setattr(news_stage, "get_news_articles", get_articles)
        monkeypatch.setattr(news_stage, "fetch_symbol_news", fetch)
        monkeypatch.setattr(news_stage, "bulk_create_news_articles", bulk_create)

        analyzer = NewsStageAnalyzer()
        out = await analyzer.analyze(
            StageContext(
                session_id=1,
                symbol="AMZN",
                instrument_type="equity_us",
                symbol_name="Amazon.com Inc.",
            )
        )

        # No raise. Stage stays NEUTRAL with 0 headlines.
        from app.schemas.research_pipeline import StageVerdict

        assert out.verdict == StageVerdict.NEUTRAL
        assert out.signals.headline_count == 0
        # bulk_create skipped because fetched=[]
        bulk_create.assert_not_awaited()
```

- [ ] **Step 2: Run — expect FAIL**

```bash
uv run pytest tests/test_news_stage_on_demand.py -v
```

Expected: 모든 테스트 FAIL — `news_stage`에 `fetch_symbol_news`/`bulk_create_news_articles` symbol이 아직 없거나 흐름이 새 분기를 안 가짐.

---

## Task 3c: Implement on-demand fetch in `NewsStageAnalyzer`

- [ ] **Step 1: Update `news_stage.py`**

`app/analysis/stages/news_stage.py`의 imports 블록을 다음으로 교체:

```python
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.analysis.stages.base import BaseStageAnalyzer, StageContext
from app.core.timezone import now_kst_naive
from app.schemas.research_pipeline import (
    NewsSignals,
    SourceFreshness,
    StageOutput,
    StageVerdict,
)
from app.services.llm_news_service import bulk_create_news_articles, get_news_articles
from app.services.research_news_service import NormalizedArticle, fetch_symbol_news
```

`_compute_signals_from_articles` 위에 임계값과 영속화 헬퍼 추가:

```python
MIN_DB_ARTICLES_BEFORE_ON_DEMAND_FETCH = 3


def _market_from_instrument(instrument_type: str) -> str:
    if instrument_type == "equity_us":
        return "us"
    if instrument_type == "crypto":
        return "crypto"
    return "kr"


@dataclass
class _OnDemandArticlePayload:
    """Shape compatible with bulk_create_news_articles input contract."""

    url: str
    title: str
    content: str | None
    summary: str | None
    source: str | None
    author: str | None
    stock_symbol: str | None
    stock_name: str | None
    published_at: datetime | None
    market: str
    feed_source: str
    keywords: list[str] | None


def _to_persist_payloads(
    articles: list[NormalizedArticle],
    *,
    symbol: str,
    stock_name: str | None,
    market: str,
) -> list[_OnDemandArticlePayload]:
    payloads: list[_OnDemandArticlePayload] = []
    for art in articles:
        payloads.append(
            _OnDemandArticlePayload(
                url=art.url,
                title=art.title,
                content=None,
                summary=art.summary,
                source=art.source,
                author=None,
                stock_symbol=symbol,
                stock_name=stock_name,
                published_at=art.published_at,
                market=market,
                feed_source=f"research_on_demand_{art.provider}",
                keywords=None,
            )
        )
    return payloads
```

기존 `_fetch_recent_headlines` 함수를 다음으로 교체:

```python
async def _fetch_recent_headlines(
    symbol: str,
    instrument_type: str,
    *,
    stock_name: str | None,
) -> dict[str, Any]:
    """Fetch recent headlines, augmenting DB with on-demand provider fetch
    when symbol-tagged news is below threshold."""
    market = _market_from_instrument(instrument_type)

    articles, _total = await get_news_articles(
        stock_symbol=symbol, market=market, hours=24, limit=20
    )

    if len(articles) < MIN_DB_ARTICLES_BEFORE_ON_DEMAND_FETCH:
        fetched = await fetch_symbol_news(symbol, instrument_type, limit=20)
        if fetched:
            payloads = _to_persist_payloads(
                fetched, symbol=symbol, stock_name=stock_name, market=market
            )
            try:
                await bulk_create_news_articles(payloads)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "news_stage: bulk_create_news_articles failed: symbol=%s err=%s",
                    symbol,
                    exc,
                )
            articles, _total = await get_news_articles(
                stock_symbol=symbol, market=market, hours=24, limit=20
            )

    return _compute_signals_from_articles(articles)
```

`NewsStageAnalyzer.analyze`의 `_fetch_recent_headlines` 호출을 다음으로 변경:

```python
            raw = await _fetch_recent_headlines(
                ctx.symbol,
                ctx.instrument_type,
                stock_name=ctx.symbol_name,
            )
```

- [ ] **Step 2: Run new tests**

```bash
uv run pytest tests/test_news_stage_on_demand.py -v
```

Expected: 3 테스트 PASS.

- [ ] **Step 3: Run existing news / pipeline tests**

```bash
uv run pytest tests/ -v -k "news_stage or news_signals or pipeline" 2>&1 | tail -30
```

Expected: 기존 + 신규 모두 PASS.

- [ ] **Step 4: Commit**

```bash
git add app/analysis/stages/news_stage.py tests/test_news_stage_on_demand.py
git commit -m "feat(ROB-115): on-demand symbol news fetch in NewsStageAnalyzer

When the DB has fewer than MIN_DB_ARTICLES_BEFORE_ON_DEMAND_FETCH (3)
symbol-tagged rows for the session symbol, the stage now fetches via
research_news_service, persists with feed_source='research_on_demand_*'
and stock_symbol/stock_name set, then re-reads to compute signals.

Failures (provider exceptions, persist exceptions) degrade to NEUTRAL
without breaking the pipeline."
```

---

## Task 4a: Pipeline — drop SocialStageAnalyzer (failing test)

신규 세션은 social stage_analysis row를 만들지 않아야 한다.

**Files:**

- Test: `tests/test_research_pipeline_no_social.py`

- [ ] **Step 1: Write failing test**

`tests/test_research_pipeline_no_social.py` 생성:

```python
"""ROB-115 — Verify the research pipeline no longer schedules
SocialStageAnalyzer for new sessions."""

from __future__ import annotations

from app.analysis import pipeline


def test_pipeline_module_imports_only_three_stage_analyzers() -> None:
    """SocialStageAnalyzer must not be imported (or used) by pipeline.py."""
    src = pipeline.__file__
    with open(src, encoding="utf-8") as fh:
        text = fh.read()
    # The reference may still exist in legacy modules; pipeline.py itself
    # must not import or instantiate SocialStageAnalyzer.
    assert "SocialStageAnalyzer" not in text, (
        "pipeline.py should not reference SocialStageAnalyzer (ROB-115)"
    )


def test_pipeline_run_research_session_does_not_create_social_row(
    monkeypatch,
) -> None:
    """Smoke check: run_research_session's analyzers list must omit social.

    We poke at the analyzers tuple by reading the source — adding a
    behavioral check requires too much DB scaffolding. The source check
    above already pins this; this test pins the analyzers list in code.
    """
    src = pipeline.__file__
    with open(src, encoding="utf-8") as fh:
        text = fh.read()
    # The analyzers list lines should mention the three remaining stages
    # and not 'Social'.
    assert "MarketStageAnalyzer" in text
    assert "NewsStageAnalyzer" in text
    assert "FundamentalsStageAnalyzer" in text
    assert "SocialStageAnalyzer()" not in text
```

- [ ] **Step 2: Run — expect FAIL**

```bash
uv run pytest tests/test_research_pipeline_no_social.py -v
```

Expected: 두 테스트 모두 FAIL — pipeline.py가 아직 SocialStageAnalyzer를 import + 인스턴스화 중.

---

## Task 4b: Remove SocialStageAnalyzer from pipeline + add deprecation note

- [ ] **Step 1: Modify `pipeline.py`**

`app/analysis/pipeline.py`에서 다음 두 줄 제거:

```python
from app.analysis.stages.social_stage import SocialStageAnalyzer
```

```python
        SocialStageAnalyzer(),
```

분석 후 `analyzers` list가 다음과 같이 되어야 한다:

```python
    analyzers = [
        MarketStageAnalyzer(),
        NewsStageAnalyzer(),
        FundamentalsStageAnalyzer(),
    ]
```

- [ ] **Step 2: Add deprecation comment to `social_stage.py`**

`app/analysis/stages/social_stage.py`의 첫 번째 줄을 다음으로 교체:

```python
# app/analysis/stages/social_stage.py
#
# DEPRECATED (ROB-115): SocialStageAnalyzer is no longer scheduled by
# the research pipeline. The class is kept for legacy data compatibility
# (existing stage_analysis rows with stage_type='social') and for
# potential re-introduction once a real social signal source is wired
# up. Do not add new callers.
```

- [ ] **Step 3: Run new test + existing pipeline tests**

```bash
uv run pytest tests/test_research_pipeline_no_social.py tests/ -v -k "pipeline" 2>&1 | tail -30
```

Expected: 신규 PASS. 기존 pipeline 테스트 PASS (3-stage 운영에서도 build_summary 정상).

- [ ] **Step 4: Commit**

```bash
git add app/analysis/pipeline.py app/analysis/stages/social_stage.py tests/test_research_pipeline_no_social.py
git commit -m "refactor(ROB-115): drop SocialStageAnalyzer from pipeline

Stage was a placeholder ('not_implemented') and the upstream
TradingAgents reference does not have a real social source either.
The class file is preserved for legacy stage_analysis rows; only the
pipeline scheduling is removed."
```

---

## Task 5: Frontend — remove social route + STAGE_NAV entry

**Files:**

- Modify: `frontend/trading-decision/src/routes.tsx`
- Modify: `frontend/trading-decision/src/pages/research/ResearchSessionLayout.tsx`
- Delete: `frontend/trading-decision/src/pages/research/ResearchSocialPage.tsx`
- Delete: `frontend/trading-decision/src/components/ResearchSocialTab.tsx`

- [ ] **Step 1: Drop social route + import in `routes.tsx`**

`frontend/trading-decision/src/routes.tsx` 수정:

다음 import 삭제:

```ts
import ResearchSocialPage from "./pages/research/ResearchSocialPage";
```

children 배열에서 다음 라인 삭제:

```ts
{ path: "social", element: <ResearchSocialPage /> },
```

기존 `{ path: "*", element: <ResearchSessionNotFoundPage /> }`이 catch-all로 남아 social URL을 not-found로 자연 폴백한다 (변경 불요).

- [ ] **Step 2: Drop social entry from STAGE_NAV**

`frontend/trading-decision/src/pages/research/ResearchSessionLayout.tsx`의 `STAGE_NAV` 배열에서 다음 라인 삭제:

```ts
{ to: "social", label: RESEARCH_TAB_LABEL.social },
```

`STAGE_NAV`는 다음과 같이 4개 항목으로 남아야 한다:

```ts
const STAGE_NAV = [
  { to: "summary", label: RESEARCH_TAB_LABEL.summary },
  { to: "market", label: RESEARCH_TAB_LABEL.market },
  { to: "news", label: RESEARCH_TAB_LABEL.news },
  { to: "fundamentals", label: RESEARCH_TAB_LABEL.fundamentals },
] as const;
```

- [ ] **Step 3: Delete the social page + tab files**

```bash
rm frontend/trading-decision/src/pages/research/ResearchSocialPage.tsx \
   frontend/trading-decision/src/components/ResearchSocialTab.tsx
```

- [ ] **Step 4: Typecheck**

```bash
cd frontend/trading-decision && npm run typecheck 2>&1 | tail -20
```

Expected: PASS. 만약 `ResearchSocialTab` / `ResearchSocialPage`를 다른 곳에서 import하고 있다면 컴파일 에러가 뜬다 — 이 경우 `CitedStageSidebar.tsx` 등에서 social fallback이 import에 의존하지 않는지 확인하고 그대로 두되 (현재 spec 검토 결과 의존 없음), 새로 발견되는 import는 제거.

- [ ] **Step 5: Commit**

```bash
git add frontend/trading-decision/src/routes.tsx \
        frontend/trading-decision/src/pages/research/ResearchSessionLayout.tsx
git add -u frontend/trading-decision/src/pages/research/ResearchSocialPage.tsx \
           frontend/trading-decision/src/components/ResearchSocialTab.tsx
git commit -m "feat(ROB-115): remove social route and STAGE_NAV entry

Social stage was a placeholder. Removing the dedicated route and tab.
Direct /research/sessions/:id/social URLs fall through to the existing
ResearchSessionNotFoundPage catch-all. SocialSignals type, social i18n
label, fixtures, and CitedStageSidebar fallback are kept for legacy
session data."
```

---

## Task 6: Frontend tests — replace social-route assertions

**Files:**

- Modify: `frontend/trading-decision/src/__tests__/routes.test.tsx`
- Modify: `frontend/trading-decision/src/__tests__/ResearchSessionRoutes.test.tsx`

- [ ] **Step 1: Update `routes.test.tsx`**

`frontend/trading-decision/src/__tests__/routes.test.tsx`의 다음 테스트를 교체:

기존 (lines 72-78):

```ts
  it("registers /research/sessions/:sessionId/social stage route", () => {
    const matches = matchRoutes(
      tradingDecisionRoutes,
      "/research/sessions/42/social",
    );
    expect(matches?.at(-1)?.route.path).toBe("social");
  });
```

다음으로 교체:

```ts
  it("does not register /research/sessions/:sessionId/social as a stage route", () => {
    const matches = matchRoutes(
      tradingDecisionRoutes,
      "/research/sessions/42/social",
    );
    // Falls through to the wildcard not-found child instead of a 'social' path.
    expect(matches?.at(-1)?.route.path).toBe("*");
    expect(matches?.at(0)?.route.path).toBe("/research/sessions/:sessionId");
  });
```

- [ ] **Step 2: Update `ResearchSessionRoutes.test.tsx`**

`frontend/trading-decision/src/__tests__/ResearchSessionRoutes.test.tsx`:

다음 import 삭제:

```ts
import ResearchSocialPage from "../pages/research/ResearchSocialPage";
```

`renderAt` 안의 다음 라인 삭제:

```tsx
          <Route path="social" element={<ResearchSocialPage />} />
```

다음 테스트 블록 (대략 lines 92-100) 삭제:

```ts
  it("renders the social placeholder at /social", async () => {
    mockSessionOk();
    renderAt("/research/sessions/1/social");
    await waitFor(() =>
      expect(
        screen.getByText(/소셜 신호 분석은 준비 중입니다/),
      ).toBeInTheDocument(),
    );
  });
```

대신 동일 위치에 다음 테스트 추가 (STAGE_NAV에 social 미포함 단언):

```ts
  it("does not render a social link in the stage navigation", async () => {
    mockSessionOk();
    renderAt("/research/sessions/1/summary");
    await waitFor(() =>
      expect(screen.getByText("매수")).toBeInTheDocument(),
    );
    expect(
      screen.queryByRole("link", { name: /소셜/ }),
    ).not.toBeInTheDocument();
  });
```

- [ ] **Step 3: Run frontend tests**

```bash
cd frontend/trading-decision && npm test -- --run 2>&1 | tail -30
```

Expected: 모든 vitest 테스트 PASS. 특히 `routes.test.tsx`와 `ResearchSessionRoutes.test.tsx`가 그린.

- [ ] **Step 4: Run typecheck and build to be safe**

```bash
cd frontend/trading-decision && npm run typecheck 2>&1 | tail -10 && npm run build 2>&1 | tail -10
```

Expected: typecheck/build 그린.

- [ ] **Step 5: Commit**

```bash
git add frontend/trading-decision/src/__tests__/routes.test.tsx \
        frontend/trading-decision/src/__tests__/ResearchSessionRoutes.test.tsx
git commit -m "test(ROB-115): replace social route assertions with not-found

Pins the new behavior: /research/sessions/:id/social no longer
matches a 'social' child route — it falls through to the wildcard
not-found child. Stage navigation no longer renders a 소셜 link."
```

---

## Task 7: Smoke verification (manual / dev)

자동화 스크립트가 아니라 manual run. 환경 변수가 갖춰진 dev 환경에서 한다.

**Files:**

- (No code changes)

- [ ] **Step 1: Spin up dev backend**

```bash
make dev
```

또는

```bash
uv run uvicorn app.main:app --reload --port 8000
```

- [ ] **Step 2: Verify backend test suite green end-to-end**

```bash
uv run pytest tests/ -v -m "not integration" 2>&1 | tail -10
```

Expected: failure 없음 (pre-existing flaky 제외 — 메모해 둘 것).

- [ ] **Step 3: Create a 005930 research session and inspect**

```bash
curl -s -X POST http://localhost:8000/trading/api/research-pipeline/sessions \
  -H 'Content-Type: application/json' \
  -d '{"symbol": "005930", "name": "삼성전자", "instrument_type": "equity_kr"}' \
  | jq .
```

`session_id`를 받은 뒤:

```bash
curl -s "http://localhost:8000/trading/api/research-pipeline/sessions/<id>?include=full" \
  | jq '.stages[] | {stage_type, verdict, headline_count: .signals.headline_count}'
```

Expected: 4 stages 중 stage_type==news가 `headline_count >= 1`. social row 없음.

- [ ] **Step 4: Same for AMZN**

```bash
curl -s -X POST http://localhost:8000/trading/api/research-pipeline/sessions \
  -H 'Content-Type: application/json' \
  -d '{"symbol": "AMZN", "name": "Amazon.com Inc.", "instrument_type": "equity_us"}' \
  | jq .
```

(Finnhub key가 없으면 news.headline_count == 0이고 verdict == NEUTRAL — 이때 운영자에게 key 상태를 함께 보고).

- [ ] **Step 5: Frontend dev server smoke**

```bash
cd frontend/trading-decision && npm run dev
```

브라우저:

- `http://localhost:<port>/trading/decisions/research/sessions/<005930-id>/summary` → 정상
- `.../market`, `.../news`, `.../fundamentals` → 정상 (`/news`에 headline 보임)
- `.../social` → not-found UI

- [ ] **Step 6: Record findings (no commit unless source coverage changed)**

수집할 보고 사항 (Linear 댓글 / PR description에 포함):

- branch name (worktree 기준)
- PR URL
- migration 존재 여부 → 없음 (DB 변경 없음)
- 실행한 테스트 + 결과 요약
- 005930 / AMZN news headline 표시 결과 (수동 smoke)
- `/social` URL 처리 결과 (not-found 확인)
- 필요한 환경 변수 (값 안 적음): `FINNHUB_API_KEY` (US news fetch에 필요, 없으면 graceful degrade)
- 운영 배포 시 주의: 새 `feed_source="research_on_demand_*"`가 `news_articles`에 들어감 — news_radar / preopen briefing dashboard가 RSS feed_source만 필터링하는지 점검 (미리 검토 시 영향 없음, 그래도 한 번 확인)

---

## Self-Review

**Spec coverage:**

- ✅ on-demand fetch path → Task 2a–2d, 3b–3c
- ✅ persist with stock_symbol/stock_name + dedupe → Task 3c (`_to_persist_payloads`, `bulk_create_news_articles` reuse)
- ✅ KR=Naver / US=Finnhub routing → Task 2b/2c
- ✅ graceful degrade on failure → Task 2d, 3b "fetch_failure_degrades_to_neutral"
- ✅ social backend removal → Task 4a/4b
- ✅ social frontend removal (route, STAGE_NAV, files) → Task 5
- ✅ legacy compatibility (schema Literal, SocialSignals, CitedStageSidebar fallback, fixtures, i18n) → Task 5 explicit "유지" + spec
- ✅ frontend tests for not-found + STAGE_NAV exclusion → Task 6
- ✅ smoke for 005930 + AMZN → Task 7
- ✅ no broker / order / watch / migration touched → confirmed by file plan
- ✅ tests for new behavior → 3 new test files (Task 2a/2d, 3b, 4a)

**Placeholder scan:** 없음. 각 step은 실제 코드/명령을 포함.

**Type consistency:** `NormalizedArticle`, `_OnDemandArticlePayload`, `StageContext.symbol_name`, `fetch_symbol_news`, `_compute_signals_from_articles` 이름이 모든 task에서 일치. `feed_source="research_on_demand_naver"` / `"research_on_demand_finnhub"` 표기 일치.

---

## Execution Notes

- Backend tasks (1–4) → Frontend tasks (5–6) → Smoke (7) 순서 유지.
- 각 task는 commit으로 끝남 — 중간에 멈춰도 안전.
- Task 7 smoke는 환경에 따라 결과가 달라질 수 있음 (Finnhub key 부재 등). Acceptance에 영향 없으면 그대로 두고 보고만.

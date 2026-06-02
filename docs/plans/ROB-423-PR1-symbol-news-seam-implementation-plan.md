# ROB-423 PR1: symbol_news_service 정규화 seam Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** invest_report 뉴스 fetch/normalize를 단일 `symbol_news_service` 경로로 통합하고, `get_news` MCP 도구와 snapshot news collector를 그 경로로 재배선한다(영속/citation/Hermes는 PR2).

**Architecture:** service-layer provider fetcher(`naver_finance.fetch_news`, `finnhub_news.fetch_news_finnhub`) 위에 신규 `symbol_news_service`(정규화 `SymbolNewsArticle` + provider 원본 item 보존). 기존 `research_news_service`는 이 seam 위 thin shim으로 축소(레거시 `NewsStageAnalyzer` 무영향). `get_news` 핸들러는 seam을 거치되 출력 envelope를 byte-for-byte 보존(provider 원본 item 재방출). `NewsSnapshotCollector`의 뉴스 소스를 DB(`llm_news_service.get_news_articles`) → per-symbol on-demand seam으로 전환하되 `articles` payload 계약(NewsStage가 읽는 키)을 유지. **신규 public MCP tool 0, migration 0, 영속 0**(PR2).

**Tech Stack:** Python 3.13, async, dataclasses, pytest + pytest-asyncio, monkeypatch/AsyncMock, ruff.

---

## File Structure

| File | 역할 | 변경 |
|------|------|------|
| `app/services/symbol_news_service.py` | **신규** 단일 정규화 seam: `SymbolNewsArticle`/`SymbolNewsFetchResult`/`fetch_symbol_news` | Create |
| `tests/services/test_symbol_news_service.py` | seam 단위 테스트 | Create |
| `app/services/research_news_service.py` | `fetch_symbol_news`를 seam 위 shim으로 축소(`NormalizedArticle` 유지) | Modify |
| `tests/services/test_research_news_service.py` | shim 매핑 테스트로 갱신(seam monkeypatch) | Modify |
| `app/mcp_server/tooling/fundamentals/_news.py` | `get_news` 핸들러를 seam 경유로 재배선, envelope 보존 | Modify |
| `tests/mcp_server/tooling/test_get_news_envelope.py` | get_news envelope byte-compat 회귀 | Create |
| `app/services/action_report/snapshot_backed/collectors/news.py` | `NewsFetchFn` per-symbol화 + `_collect_articles` 루프 + `fetch_records` | Modify |
| `app/services/action_report/snapshot_backed/collectors/registry.py` | `_build_news_fetch_fn`을 seam per-symbol 어댑터로 교체 | Modify |
| `tests/services/action_report/snapshot_backed/test_collectors.py` | news collector articles 테스트를 per-symbol 시그니처로 갱신 | Modify |

레거시 `app/analysis/stages/news_stage.py`(NewsStageAnalyzer)는 **건드리지 않는다** — shim이 `list[NormalizedArticle]`를 그대로 반환하므로 무영향.

---

## Task 1: symbol_news_service 신설

**Files:**
- Create: `app/services/symbol_news_service.py`
- Test: `tests/services/test_symbol_news_service.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/services/test_symbol_news_service.py
"""Tests for app.services.symbol_news_service (ROB-423 PR1)."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from app.services import symbol_news_service


@pytest.mark.unit
@pytest.mark.asyncio
async def test_kr_returns_normalized_articles_with_external_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = [
        {
            "title": "삼성전자 호실적",
            "url": "https://finance.naver.com/item/news_read.naver?article_id=123&office_id=001",
            "source": "한국경제",
            "datetime": "2026-05-05T09:30",
        },
        {"title": "", "url": "", "source": "", "datetime": None},  # dropped (no url/title)
    ]
    monkeypatch.setattr(
        symbol_news_service.naver_finance,
        "fetch_news",
        AsyncMock(return_value=raw),
    )

    result = await symbol_news_service.fetch_symbol_news("005930", "kr", limit=20)

    assert result.status == "ok"
    assert result.provider == "naver"
    assert result.returned_count == 1
    art = result.articles[0]
    assert art.symbol == "005930"
    assert art.market == "kr"
    assert art.title == "삼성전자 호실적"
    assert art.source_name == "한국경제"
    assert art.canonical_url.endswith("article_id=123&office_id=001")
    assert art.external_article_id == "001:123"
    assert isinstance(art.published_at, datetime)
    assert art.provider_metadata["source_item"] == raw[0]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_us_finnhub_preserves_source_item_and_sentiment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "symbol": "AAPL",
        "market": "us",
        "source": "finnhub",
        "count": 1,
        "news": [
            {
                "title": "Apple beats earnings",
                "source": "Reuters",
                "datetime": "2026-05-05T12:00:00",
                "url": "https://x/aapl-1",
                "summary": "strong quarter",
                "sentiment": "positive",
                "related": "AAPL,MSFT",
            }
        ],
    }
    monkeypatch.setattr(
        symbol_news_service,
        "fetch_news_finnhub",
        AsyncMock(return_value=payload),
    )

    result = await symbol_news_service.fetch_symbol_news("AAPL", "us", limit=10)

    assert result.status == "ok"
    assert result.provider == "finnhub"
    art = result.articles[0]
    assert art.external_article_id is not None  # url hash
    assert art.related_symbols == ["AAPL", "MSFT"]
    assert art.provider_metadata["sentiment"] == "positive"
    assert art.provider_metadata["source_item"] == payload["news"][0]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_empty_provider_result_is_status_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        symbol_news_service.naver_finance, "fetch_news", AsyncMock(return_value=[])
    )
    result = await symbol_news_service.fetch_symbol_news("005930", "kr")
    assert result.status == "empty"
    assert result.returned_count == 0
    assert result.articles == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_provider_error_is_fail_soft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        symbol_news_service.naver_finance,
        "fetch_news",
        AsyncMock(side_effect=RuntimeError("boom")),
    )
    result = await symbol_news_service.fetch_symbol_news("005930", "kr")
    assert result.status == "error"
    assert result.error_code == "RuntimeError"
    assert result.articles == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unsupported_market_is_unavailable() -> None:
    result = await symbol_news_service.fetch_symbol_news("FOO", "jp")
    assert result.status == "unavailable"
    assert result.error_code == "unsupported_market"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/test_symbol_news_service.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.symbol_news_service'`

- [ ] **Step 3: Write minimal implementation**

```python
# app/services/symbol_news_service.py
"""Unified on-demand symbol news service (ROB-423 PR1).

Single normalized seam over the service-layer provider fetchers
(``naver_finance.fetch_news``, ``finnhub_news.fetch_news_finnhub``). Consumed by
the ``get_news`` MCP tool, the snapshot-backed news collector, and (via a thin
shim) the legacy research news path. No MCP imports, no LLM, no order/broker
surface. Each article keeps the provider's original item in
``provider_metadata["source_item"]`` so byte-compatible envelopes can be rebuilt.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlparse

from app.services import naver_finance
from app.services.finnhub_news import fetch_news_finnhub

logger = logging.getLogger(__name__)

_INSTRUMENT_BY_MARKET = {"kr": "equity_kr", "us": "equity_us", "crypto": "crypto"}


@dataclass(frozen=True)
class SymbolNewsArticle:
    provider: str
    market: str
    symbol: str
    external_article_id: str | None
    title: str
    source_name: str | None
    canonical_url: str
    summary: str | None
    published_at: datetime | None
    fetched_at: datetime
    related_symbols: list[str] = field(default_factory=list)
    provider_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SymbolNewsFetchResult:
    symbol: str
    market: str
    provider: str
    status: str  # ok | empty | unavailable | error
    requested_limit: int
    returned_count: int
    articles: list[SymbolNewsArticle]
    error_code: str | None = None


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        pass
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return None


def _naver_external_id(url: str) -> str | None:
    """``officeId:articleId`` from a Naver news_read URL, else None."""
    try:
        q = parse_qs(urlparse(url).query)
    except ValueError:
        return None
    article_id = (q.get("article_id") or [None])[0]
    office_id = (q.get("office_id") or [None])[0]
    if office_id and article_id:
        return f"{office_id}:{article_id}"
    return article_id or None


def _url_hash(url: str) -> str | None:
    if not url:
        return None
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]


async def _fetch_naver(
    symbol: str, limit: int, fetched_at: datetime
) -> list[SymbolNewsArticle]:
    items = await naver_finance.fetch_news(symbol, limit=limit)
    out: list[SymbolNewsArticle] = []
    for raw in items:
        url = (raw.get("url") or "").strip()
        title = (raw.get("title") or "").strip()
        if not url or not title:
            continue
        out.append(
            SymbolNewsArticle(
                provider="naver",
                market="kr",
                symbol=symbol,
                external_article_id=_naver_external_id(url),
                title=title,
                source_name=raw.get("source") or None,
                canonical_url=url,
                summary=None,
                published_at=_parse_dt(raw.get("datetime")),
                fetched_at=fetched_at,
                related_symbols=[],
                provider_metadata={"source_item": raw},
            )
        )
    return out


async def _fetch_finnhub(
    symbol: str, market: str, limit: int, fetched_at: datetime
) -> list[SymbolNewsArticle]:
    payload = await fetch_news_finnhub(symbol, market, limit)
    out: list[SymbolNewsArticle] = []
    for raw in payload.get("news") or []:
        url = (raw.get("url") or "").strip()
        title = (raw.get("title") or "").strip()
        if not url or not title:
            continue
        related_raw = raw.get("related") or ""
        related = [s for s in str(related_raw).split(",") if s]
        out.append(
            SymbolNewsArticle(
                provider="finnhub",
                market=market,
                symbol=symbol,
                external_article_id=_url_hash(url),
                title=title,
                source_name=raw.get("source") or None,
                canonical_url=url,
                summary=raw.get("summary") or None,
                published_at=_parse_dt(raw.get("datetime")),
                fetched_at=fetched_at,
                related_symbols=related,
                provider_metadata={
                    "sentiment": raw.get("sentiment"),
                    "related": related_raw,
                    "source_item": raw,
                },
            )
        )
    return out


async def fetch_symbol_news(
    symbol: str,
    market: str,
    instrument_type: str | None = None,
    *,
    limit: int = 20,
    timeout_s: float = 5.0,
) -> SymbolNewsFetchResult:
    """On-demand normalized news for one symbol. Fail-soft (never raises)."""
    market = (market or "").lower()
    provider = "naver" if market == "kr" else "finnhub"
    fetched_at = _utcnow()
    try:
        if market == "kr":
            articles = await asyncio.wait_for(
                _fetch_naver(symbol, limit, fetched_at), timeout=timeout_s
            )
        elif market in ("us", "crypto"):
            articles = await asyncio.wait_for(
                _fetch_finnhub(symbol, market, limit, fetched_at), timeout=timeout_s
            )
        else:
            return SymbolNewsFetchResult(
                symbol, market, provider, "unavailable", limit, 0, [],
                "unsupported_market",
            )
    except Exception as exc:  # noqa: BLE001 — overlay evidence, fail soft
        logger.warning(
            "symbol_news_service.fetch_symbol_news failed: symbol=%s market=%s err=%s",
            symbol, market, exc,
        )
        return SymbolNewsFetchResult(
            symbol, market, provider, "error", limit, 0, [], type(exc).__name__
        )
    status = "ok" if articles else "empty"
    return SymbolNewsFetchResult(
        symbol, market, provider, status, limit, len(articles), articles, None
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/services/test_symbol_news_service.py -v`
Expected: PASS (6 passed)

- [ ] **Step 5: Commit**

```bash
git add app/services/symbol_news_service.py tests/services/test_symbol_news_service.py
git commit -m "feat(ROB-423): symbol_news_service 단일 정규화 뉴스 seam

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: research_news_service를 seam 위 shim으로 축소

**Files:**
- Modify: `app/services/research_news_service.py`
- Test: `tests/services/test_research_news_service.py`

레거시 `app/analysis/stages/news_stage.py:115`는 `fetch_symbol_news(symbol, instrument_type, limit=20) -> list[NormalizedArticle]`를 그대로 호출한다. 시그니처/반환형은 유지하고 내부만 seam에 위임한다.

- [ ] **Step 1: Update the test to drive the shim (will fail)**

기존 `tests/services/test_research_news_service.py`를 아래로 교체. seam을 monkeypatch하고 `NormalizedArticle` 매핑을 검증.

```python
# tests/services/test_research_news_service.py
"""Tests for research_news_service shim over symbol_news_service (ROB-423)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from app.services import research_news_service, symbol_news_service
from app.services.symbol_news_service import (
    SymbolNewsArticle,
    SymbolNewsFetchResult,
)


def _seam_article(provider: str = "naver") -> SymbolNewsArticle:
    return SymbolNewsArticle(
        provider=provider,
        market="kr",
        symbol="005930",
        external_article_id="001:123",
        title="삼성전자 호실적",
        source_name="한국경제",
        canonical_url="https://finance.naver.com/x",
        summary="요약" if provider == "finnhub" else None,
        published_at=datetime(2026, 5, 5, 9, 0, tzinfo=UTC),
        fetched_at=datetime(2026, 5, 5, 10, 0, tzinfo=UTC),
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_shim_maps_seam_to_normalized_article(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = SymbolNewsFetchResult(
        symbol="005930", market="kr", provider="naver", status="ok",
        requested_limit=20, returned_count=1, articles=[_seam_article()],
    )
    monkeypatch.setattr(
        symbol_news_service,
        "fetch_symbol_news",
        AsyncMock(return_value=result),
    )

    out = await research_news_service.fetch_symbol_news(
        "005930", "equity_kr", limit=20
    )

    assert len(out) == 1
    first = out[0]
    assert first.url == "https://finance.naver.com/x"
    assert first.title == "삼성전자 호실적"
    assert first.source == "한국경제"
    assert first.provider == "naver"
    assert first.summary is None
    assert isinstance(first.published_at, datetime)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_shim_passes_market_for_us(monkeypatch: pytest.MonkeyPatch) -> None:
    seam = AsyncMock(
        return_value=SymbolNewsFetchResult(
            symbol="AAPL", market="us", provider="finnhub", status="empty",
            requested_limit=20, returned_count=0, articles=[],
        )
    )
    monkeypatch.setattr(symbol_news_service, "fetch_symbol_news", seam)

    out = await research_news_service.fetch_symbol_news("AAPL", "equity_us", limit=20)

    assert out == []
    seam.assert_awaited_once()
    assert seam.await_args.args[1] == "us"  # market derived from instrument_type


@pytest.mark.unit
@pytest.mark.asyncio
async def test_shim_returns_empty_for_unknown_instrument(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seam = AsyncMock()
    monkeypatch.setattr(symbol_news_service, "fetch_symbol_news", seam)

    out = await research_news_service.fetch_symbol_news("X", "crypto", limit=20)

    assert out == []
    seam.assert_not_awaited()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/test_research_news_service.py -v`
Expected: FAIL (shim not yet delegating; `_naver_fetch_news` path still active / import errors)

- [ ] **Step 3: Rewrite research_news_service as shim**

`app/services/research_news_service.py` 전체를 아래로 교체. `NormalizedArticle`는 레거시 소비자(`app/analysis/stages/news_stage.py`)를 위해 유지.

```python
# app/services/research_news_service.py
"""Legacy research-pipeline news shim (ROB-115 → ROB-423).

Thin wrapper that delegates to the unified ``symbol_news_service`` seam and maps
its richer ``SymbolNewsArticle`` back to the legacy ``NormalizedArticle`` shape
consumed by ``app.analysis.stages.news_stage``. No fetch/normalize logic lives
here anymore — single source of truth is ``symbol_news_service``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.services import symbol_news_service

_MARKET_BY_INSTRUMENT = {"equity_kr": "kr", "equity_us": "us"}


@dataclass(frozen=True)
class NormalizedArticle:
    url: str
    title: str
    source: str | None
    summary: str | None
    published_at: datetime | None
    provider: str


async def fetch_symbol_news(
    symbol: str, instrument_type: str, *, limit: int = 20, timeout_s: float = 5.0
) -> list[NormalizedArticle]:
    market = _MARKET_BY_INSTRUMENT.get(instrument_type)
    if market is None:
        return []
    result = await symbol_news_service.fetch_symbol_news(
        symbol, market, instrument_type, limit=limit, timeout_s=timeout_s
    )
    return [
        NormalizedArticle(
            url=a.canonical_url,
            title=a.title,
            source=a.source_name,
            summary=a.summary,
            published_at=a.published_at,
            provider=a.provider,
        )
        for a in result.articles
    ]
```

- [ ] **Step 4: Run tests (shim + legacy consumer) to verify they pass**

Run: `uv run pytest tests/services/test_research_news_service.py tests/test_news_stage_on_demand.py -v`
Expected: PASS (legacy `NewsStageAnalyzer` tests still green — `fetch_symbol_news` still returns `list[NormalizedArticle]`)

- [ ] **Step 5: Commit**

```bash
git add app/services/research_news_service.py tests/services/test_research_news_service.py
git commit -m "refactor(ROB-423): research_news_service를 symbol_news_service shim으로 축소

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: get_news 핸들러를 seam 경유로 재배선 (envelope byte-compat)

**Files:**
- Modify: `app/mcp_server/tooling/fundamentals/_news.py`
- Test: `tests/mcp_server/tooling/test_get_news_envelope.py`

`get_news`는 seam을 거치되, 응답은 provider 원본 item(`provider_metadata["source_item"]`)을 재방출하여 기존 envelope를 그대로 유지한다.

- [ ] **Step 1: Write the failing regression test**

```python
# tests/mcp_server/tooling/test_get_news_envelope.py
"""get_news envelope byte-compat regression after seam rewire (ROB-423)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from app.mcp_server.tooling.fundamentals import _news
from app.services import symbol_news_service
from app.services.symbol_news_service import (
    SymbolNewsArticle,
    SymbolNewsFetchResult,
)


def _naver_article() -> SymbolNewsArticle:
    raw = {
        "title": "삼성전자 호실적",
        "url": "https://finance.naver.com/item/news_read.naver?article_id=1&office_id=2",
        "source": "한국경제",
        "datetime": "2026-05-05",
    }
    return SymbolNewsArticle(
        provider="naver", market="kr", symbol="005930",
        external_article_id="2:1", title=raw["title"], source_name=raw["source"],
        canonical_url=raw["url"], summary=None,
        published_at=datetime(2026, 5, 5, tzinfo=UTC),
        fetched_at=datetime(2026, 5, 5, 1, tzinfo=UTC),
        provider_metadata={"source_item": raw},
    )


def _finnhub_article() -> SymbolNewsArticle:
    raw = {
        "title": "Apple beats earnings",
        "source": "Reuters",
        "datetime": "2026-05-05T12:00:00",
        "url": "https://x/aapl-1",
        "summary": "strong",
        "sentiment": "positive",
        "related": "AAPL",
    }
    return SymbolNewsArticle(
        provider="finnhub", market="us", symbol="AAPL",
        external_article_id="abc", title=raw["title"], source_name=raw["source"],
        canonical_url=raw["url"], summary=raw["summary"],
        published_at=datetime(2026, 5, 5, 12, tzinfo=UTC),
        fetched_at=datetime(2026, 5, 5, 13, tzinfo=UTC),
        related_symbols=["AAPL"],
        provider_metadata={"sentiment": "positive", "related": "AAPL", "source_item": raw},
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_news_kr_envelope_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    art = _naver_article()
    monkeypatch.setattr(
        symbol_news_service,
        "fetch_symbol_news",
        AsyncMock(
            return_value=SymbolNewsFetchResult(
                "005930", "kr", "naver", "ok", 10, 1, [art]
            )
        ),
    )

    out = await _news.handle_get_news("005930", market="kr", limit=10)

    assert out == {
        "symbol": "005930",
        "market": "kr",
        "source": "naver",
        "count": 1,
        "news": [art.provider_metadata["source_item"]],
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_news_us_envelope_keys_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    art = _finnhub_article()
    monkeypatch.setattr(
        symbol_news_service,
        "fetch_symbol_news",
        AsyncMock(
            return_value=SymbolNewsFetchResult("AAPL", "us", "finnhub", "ok", 10, 1, [art])
        ),
    )

    out = await _news.handle_get_news("AAPL", market="us", limit=10)

    assert out["source"] == "finnhub"
    assert out["count"] == 1
    assert set(out["news"][0].keys()) == {
        "title", "source", "datetime", "url", "summary", "sentiment", "related",
    }
    assert out["news"][0]["sentiment"] == "positive"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_news_error_status_returns_error_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        symbol_news_service,
        "fetch_symbol_news",
        AsyncMock(
            return_value=SymbolNewsFetchResult(
                "AAPL", "us", "finnhub", "error", 10, 0, [], "RuntimeError"
            )
        ),
    )

    out = await _news.handle_get_news("AAPL", market="us", limit=10)

    assert out.get("error") or out.get("source") == "finnhub"
    assert "news" not in out or out.get("count", 0) == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/mcp_server/tooling/test_get_news_envelope.py -v`
Expected: FAIL (handler still calls `_fetch_news_naver`/`_fetch_news_finnhub`, not the seam)

- [ ] **Step 3: Rewire the handler**

`app/mcp_server/tooling/fundamentals/_news.py` 전체를 아래로 교체.

```python
# app/mcp_server/tooling/fundamentals/_news.py
"""Handler for get_news tool (routes through symbol_news_service, ROB-423)."""

from __future__ import annotations

from typing import Any

from app.mcp_server.tooling.fundamentals._helpers import normalize_market_with_crypto
from app.mcp_server.tooling.shared import (
    error_payload as _error_payload,
)
from app.mcp_server.tooling.shared import (
    is_crypto_market as _is_crypto_market,
)
from app.mcp_server.tooling.shared import (
    is_korean_equity_code as _is_korean_equity_code,
)
from app.mcp_server.tooling.shared import (
    normalize_symbol_input as _normalize_symbol_input,
)
from app.services import symbol_news_service

_INSTRUMENT_BY_MARKET = {"kr": "equity_kr", "us": "equity_us", "crypto": "crypto"}


async def handle_get_news(
    symbol: str | int,
    market: str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    symbol = _normalize_symbol_input(symbol, market)
    if not symbol:
        raise ValueError("symbol is required")

    if market is None:
        if _is_korean_equity_code(symbol):
            market = "kr"
        elif _is_crypto_market(symbol):
            market = "crypto"
        else:
            market = "us"

    normalized_market = normalize_market_with_crypto(market)
    capped_limit = min(max(limit, 1), 50)
    instrument_type = _INSTRUMENT_BY_MARKET.get(normalized_market, "equity_us")

    result = await symbol_news_service.fetch_symbol_news(
        symbol, normalized_market, instrument_type, limit=capped_limit
    )

    if result.status in ("error", "unavailable"):
        return _error_payload(
            source=result.provider,
            message=result.error_code or "news_unavailable",
            symbol=symbol,
            instrument_type=instrument_type,
        )

    news = [a.provider_metadata.get("source_item", {}) for a in result.articles]
    return {
        "symbol": symbol,
        "market": normalized_market,
        "source": result.provider,
        "count": len(news),
        "news": news,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/mcp_server/tooling/test_get_news_envelope.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Run any pre-existing get_news tests to confirm no regression**

Run: `uv run pytest -k "get_news or handle_get_news" -v`
Expected: PASS (existing get_news tests still green; if a pre-existing test pinned the old `_fetch_news_*` import path, update it to the seam mock — repeat the seam-mock pattern from Step 1)

- [ ] **Step 6: Commit**

```bash
git add app/mcp_server/tooling/fundamentals/_news.py tests/mcp_server/tooling/test_get_news_envelope.py
git commit -m "refactor(ROB-423): get_news를 symbol_news_service 경유로 재배선(envelope 보존)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: NewsSnapshotCollector를 per-symbol on-demand seam으로 전환

**Files:**
- Modify: `app/services/action_report/snapshot_backed/collectors/news.py`
- Modify: `app/services/action_report/snapshot_backed/collectors/registry.py:237-273,316`
- Test: `tests/services/action_report/snapshot_backed/test_collectors.py`

뉴스 소스를 DB(`llm_news_service.get_news_articles`, market-scoped) → seam(per-symbol on-demand)로 전환. `articles` payload는 NewsStage가 읽는 키(`title`, `sentiment`)를 유지하고, PR2 citation을 위해 `symbol`/`external_article_id`/`canonical_url`/`provider`/`published_at`를 추가한다. per-symbol `fetch_records`를 payload에 남긴다(PR2 fetch_run 재료). 실패는 fail-open.

- [ ] **Step 1: Write/replace the failing collector test**

`tests/services/action_report/snapshot_backed/test_collectors.py`의 `test_news_collector_articles_from_news_fetch_fn`(약 1262-1282행)을 아래로 교체하고, 새 per-symbol 테스트 2개를 추가.

```python
@pytest.mark.asyncio
async def test_news_collector_articles_per_symbol_from_seam():
    from app.services.symbol_news_service import (
        SymbolNewsArticle,
        SymbolNewsFetchResult,
    )

    captured: list[tuple[str, str, int]] = []

    async def fake_fetch(symbol: str, market: str, limit: int):
        captured.append((symbol, market, limit))
        art = SymbolNewsArticle(
            provider="finnhub", market=market, symbol=symbol,
            external_article_id=f"id-{symbol}", title=f"{symbol} up",
            source_name="Reuters", canonical_url=f"https://x/{symbol}",
            summary="s", published_at=None,
            fetched_at=dt.datetime(2026, 5, 5, tzinfo=dt.UTC),
            provider_metadata={"sentiment": "positive"},
        )
        return SymbolNewsFetchResult(
            symbol, market, "finnhub", "ok", limit, 1, [art]
        )

    collector = NewsSnapshotCollector(MagicMock(), news_fetch_fn=fake_fetch)
    results = await collector.collect(_request(market="us", symbols=["AAPL", "MSFT"]))

    payload = results[0].payload_json
    assert payload["count"] == 2
    assert {a["symbol"] for a in payload["articles"]} == {"AAPL", "MSFT"}
    assert payload["articles"][0]["sentiment"] == "positive"
    assert payload["articles"][0]["external_article_id"] == "id-AAPL"
    assert [r["symbol"] for r in payload["fetch_records"]] == ["AAPL", "MSFT"]
    assert {c[0] for c in captured} == {"AAPL", "MSFT"}
    assert results[0].freshness_status == "fresh"


@pytest.mark.asyncio
async def test_news_collector_per_symbol_failure_is_fail_open():
    from app.services.symbol_news_service import SymbolNewsFetchResult

    async def fake_fetch(symbol: str, market: str, limit: int):
        return SymbolNewsFetchResult(
            symbol, market, "finnhub", "error", limit, 0, [], "RuntimeError"
        )

    collector = NewsSnapshotCollector(MagicMock(), news_fetch_fn=fake_fetch)
    results = await collector.collect(_request(market="us", symbols=["AAPL"]))

    payload = results[0].payload_json
    assert payload["count"] == 0
    assert payload["fetch_records"][0]["status"] == "error"
    # fail-open: never raises, degrades to partial
    assert results[0].freshness_status == "partial"


@pytest.mark.asyncio
async def test_news_collector_no_symbols_is_partial():
    from app.services.symbol_news_service import SymbolNewsFetchResult

    async def fake_fetch(symbol: str, market: str, limit: int):  # pragma: no cover
        raise AssertionError("should not fetch without symbols")

    collector = NewsSnapshotCollector(MagicMock(), news_fetch_fn=fake_fetch)
    results = await collector.collect(_request(market="us", symbols=[]))

    assert results[0].payload_json["count"] == 0
    assert results[0].freshness_status == "partial"
```

`_request` 헬퍼가 `symbols=`를 받는지 확인하고(이미 `request.symbols` 사용), 없으면 그 키워드를 받도록 헬퍼를 확장한다. 기존 research_reports 폴백 테스트(`test_news_collector_returns_citations`)는 변경 없이 유지(news_fetch_fn=None 경로).

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/action_report/snapshot_backed/test_collectors.py -k news -v`
Expected: FAIL (collector still calls `_news_fetch_fn(market, hours, limit)` and expects `list[dict]`)

- [ ] **Step 3: Update NewsFetchFn type + _collect_articles loop**

`app/services/action_report/snapshot_backed/collectors/news.py`에서:

(a) import + `NewsFetchFn` 타입 교체 (현재 34-38행):

```python
from app.services.investment_snapshots.collectors import (
    CollectorRequest,
    SnapshotCollectResult,
)
from app.services.research_reports.query_service import ResearchReportsQueryService
from app.services.symbol_news_service import SymbolNewsFetchResult

# ROB-423 — per-symbol on-demand news fetch (symbol, market, limit) →
# SymbolNewsFetchResult. Wired in registry.py over symbol_news_service so this
# module imports no MCP tooling and no LLM provider.
NewsFetchFn = Callable[[str, str, int], Awaitable[SymbolNewsFetchResult]]
```

(b) `_collect_articles` (현재 159-202행) 전체 교체:

```python
    async def _collect_articles(
        self, request: CollectorRequest, *, now: dt.datetime, since: dt.datetime
    ) -> list[SnapshotCollectResult]:
        """Per-symbol on-demand news (ROB-423). Fail-open: per-symbol fetch
        errors are recorded in ``fetch_records`` and never raise. Each article
        keeps NewsStage-compatible keys (``title``/``sentiment``) plus citation
        provenance (``symbol``/``external_article_id``/``canonical_url``)."""
        assert self._news_fetch_fn is not None
        focus_symbols = [s for s in (request.symbols or []) if s]

        articles_payload: list[dict[str, Any]] = []
        fetch_records: list[dict[str, Any]] = []
        for symbol in focus_symbols:
            try:
                result = await self._news_fetch_fn(
                    symbol, request.market, self._limit
                )
            except Exception as exc:  # noqa: BLE001 — optional, fail open
                fetch_records.append(
                    {
                        "symbol": symbol,
                        "provider": "unknown",
                        "requested_limit": self._limit,
                        "returned_count": 0,
                        "status": "error",
                        "error_code": type(exc).__name__,
                    }
                )
                continue

            fetch_records.append(
                {
                    "symbol": symbol,
                    "provider": result.provider,
                    "requested_limit": result.requested_limit,
                    "returned_count": result.returned_count,
                    "status": result.status,
                    "error_code": result.error_code,
                }
            )
            for a in result.articles:
                articles_payload.append(
                    {
                        "title": a.title,
                        "url": a.canonical_url,
                        "source": a.source_name,
                        "summary": a.summary,
                        "published_at": a.published_at.isoformat()
                        if a.published_at
                        else None,
                        "symbol": a.symbol,
                        "provider": a.provider,
                        "external_article_id": a.external_article_id,
                        "sentiment": a.provider_metadata.get("sentiment"),
                        "related": a.related_symbols,
                    }
                )

        payload: dict[str, Any] = {
            "since": since.isoformat(),
            "count": len(articles_payload),
            "articles": articles_payload,
            "fetch_records": fetch_records,
            "source": "symbol_news_service",
            "market": request.market,
        }
        return [
            build_result(
                snapshot_kind=self.snapshot_kind,
                market=request.market,
                account_scope=request.account_scope,
                payload=payload,
                origin="news",
                as_of=now,
                freshness_status="fresh" if articles_payload else "partial",
                coverage={"article_count": len(articles_payload)},
            )
        ]
```

- [ ] **Step 4: Re-point the registry adapter to the seam**

`app/services/action_report/snapshot_backed/collectors/registry.py`의 `_build_news_fetch_fn` (237-273행) 전체 교체:

```python
def _build_news_fetch_fn() -> NewsFetchFn:
    """Per-symbol on-demand news adapter over ``symbol_news_service`` (ROB-423).

    Given (symbol, market, limit) returns a normalized ``SymbolNewsFetchResult``.
    Imported lazily; no MCP/LLM/order surface. The collector wraps the call so a
    fetch error degrades the optional ``news`` kind without blocking the bundle.
    """

    async def _news_fetch_fn(symbol: str, market: str, limit: int):
        from app.services.symbol_news_service import fetch_symbol_news

        return await fetch_symbol_news(symbol, market, limit=limit)

    return _news_fetch_fn
```

(`NewsFetchFn` import는 이미 `collectors.news`에서 오므로 registry 상단 import 유지. 사용하지 않게 된 `llm_news_service` 관련 import가 registry에 있으면 제거.)

- [ ] **Step 5: Run collector tests to verify pass**

Run: `uv run pytest tests/services/action_report/snapshot_backed/test_collectors.py -k news -v`
Expected: PASS (per-symbol tests green; research_reports 폴백 테스트도 유지)

- [ ] **Step 6: Commit**

```bash
git add app/services/action_report/snapshot_backed/collectors/news.py app/services/action_report/snapshot_backed/collectors/registry.py tests/services/action_report/snapshot_backed/test_collectors.py
git commit -m "feat(ROB-423): news collector를 per-symbol on-demand seam으로 전환

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: 전체 검증 + 가드 + lint

**Files:** (없음 — 검증만)

- [ ] **Step 1: no-internal-LLM 가드 통과 확인**

Run: `uv run pytest tests/services/action_report/snapshot_backed/test_no_internal_llm_imports.py -v`
Expected: PASS (collector/news 변경이 LLM provider import를 추가하지 않음. `symbol_news_service`는 `app/services/` 평면에 있고 LLM 미import)

- [ ] **Step 2: 관련 영역 전체 테스트**

Run:
```bash
uv run pytest \
  tests/services/test_symbol_news_service.py \
  tests/services/test_research_news_service.py \
  tests/test_news_stage_on_demand.py \
  tests/mcp_server/tooling/test_get_news_envelope.py \
  tests/services/action_report/snapshot_backed/test_collectors.py \
  -v
```
Expected: PASS (전부 green)

- [ ] **Step 3: get_news / news 회귀 스윕**

Run: `uv run pytest -k "news or get_news" -q`
Expected: PASS (사전 존재 테스트 중 `_fetch_news_*` 직접 import에 의존하던 것이 있으면 seam mock으로 갱신 후 green)

- [ ] **Step 4: lint + format + typecheck**

Run:
```bash
uv run ruff check app/services/symbol_news_service.py app/services/research_news_service.py app/mcp_server/tooling/fundamentals/_news.py app/services/action_report/snapshot_backed/collectors/news.py app/services/action_report/snapshot_backed/collectors/registry.py
uv run ruff format --check app/services/symbol_news_service.py app/services/research_news_service.py app/mcp_server/tooling/fundamentals/_news.py app/services/action_report/snapshot_backed/collectors/news.py app/services/action_report/snapshot_backed/collectors/registry.py
```
Expected: clean (위반 시 `ruff format` 적용 후 재확인)

- [ ] **Step 5: 최종 커밋 (잔여 변경 있으면)**

```bash
git add -A
git commit -m "test(ROB-423): PR1 seam 전환 회귀 스윕 + lint

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review (spec 대조)

- **AC#3 (public MCP tool은 get_news 하나)**: 신규 MCP tool 0. `symbol_news_service`는 내부 service. ✅
- **AC#4 (get_news envelope 불변)**: Task 3 byte-compat 회귀 테스트 + `source_item` 재방출. ✅
- **AC#11 (collector가 symbol_news_service 단일 경로, 중복 normalize 없음)**: Task 4 collector seam 전환 + Task 2 research shim. ✅
- **D2 (on-demand)**: DB 경로 제거, per-symbol seam. ✅
- **D3 (단일 seam + get_news 경유)**: Task 1/2/3. ✅
- **migration/영속/Hermes/detail/mock 복사**: PR2 범위 — 본 플랜 비포함(의도적). ✅
- **fail-open**: Task 4 per-symbol error → fetch_records 기록, 무예외. ✅
- **레거시 NewsStageAnalyzer 무영향**: shim이 `list[NormalizedArticle]` 유지(Task 2 Step 4 검증). ✅

### 주의/리스크
- **NewsStage 감성**: KR(naver)은 per-article `sentiment` 없음 → NewsStage가 NEUTRAL로 강등(날조 아님, 정직). 기존 DB 경로가 분석된 sentiment를 갖던 종목은 NEUTRAL로 바뀔 수 있음(overlay evidence라 허용). PR 설명에 명시.
- **crypto**: `market=="crypto"`는 finnhub general crypto news를 per-symbol 호출(코인별 동일 피드 가능) — get_news 기존 동작과 동일. 종목 간 중복 최적화는 후속.
- **사전 존재 get_news 테스트**: `_fetch_news_naver`/`_fetch_news_finnhub` 직접 import에 의존하는 테스트가 있으면 Step 3 회귀 스윕에서 seam mock으로 갱신.
- **disk**: 검증 중 worktree에서 "filesystem full" 관측 — 구현 전 `df -h` 확인 권장.

## Out of Scope (PR2)

2 테이블(`investment_report_news_fetch_runs`/`_citations`) + migration · `HermesCompositionResult.news_citations` 스키마 · `InvestmentReportNewsService.persist` · detail API `news_citations` · mock_preview citation 복사 · advisory-only smoke.

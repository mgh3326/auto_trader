# ROB-510 — get_news US/crypto(Finnhub) 신뢰성 + DB 파이프라인 구현 플랜

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `get_news` US/crypto(Finnhub) 경로에 재시도+백오프+타임아웃 상향을 넣고, KR(ROB-491)과 동일한 DB 저장(pending→판정) + degraded 폴백 구조로 통합한다.

**Architecture:** ① `finnhub_news.fetch_news_finnhub`에 tenacity 재시도+시도당 타임아웃 내장(타임아웃 소유권을 provider로 이동), ② `symbol_news_store.upsert_feed_articles`로 market-generic upsert 신설(KR 래퍼 유지), ③ `symbol_news_service`의 US/crypto 분기를 KR과 동일한 persist-and-load 구조로 재작성. migration 0, envelope은 additive.

**Tech Stack:** Python 3.13 / SQLAlchemy async / tenacity / pytest(-asyncio) / finnhub-python SDK

**Spec:** `docs/plans/ROB-510-finnhub-news-reliability-db-pipeline-spec.md`

**전제:** worktree `/Users/mgh3326/work/auto_trader.rob-510`, branch `rob-510` (origin/main 6597e3ed 이후 기준). 모든 명령은 worktree 루트에서 `uv run` 사용.

---

### Task 1: Finnhub 재시도 settings 추가

**Files:**
- Modify: `app/core/config.py` (NEWS_RELEVANCE_JUDGMENT_BATCH_LIMIT 라인 ~507 아래)
- Modify: `env.example`

- [ ] **Step 1: settings 필드 추가**

`app/core/config.py`의 `NEWS_RELEVANCE_JUDGMENT_BATCH_LIMIT: int = 50` 직후에:

```python
    # ROB-510 — Finnhub news fetch reliability (per-attempt timeout + bounded retry)
    FINNHUB_NEWS_TIMEOUT_S: float = 8.0
    FINNHUB_NEWS_MAX_ATTEMPTS: int = 3
```

- [ ] **Step 2: env.example 갱신**

`env.example`의 NEWS_RELEVANCE 블록 인근에:

```bash
# ROB-510: Finnhub get_news 신뢰성 (시도당 타임아웃 초 / 최대 시도 횟수)
FINNHUB_NEWS_TIMEOUT_S=8.0
FINNHUB_NEWS_MAX_ATTEMPTS=3
```

- [ ] **Step 3: 커밋**

```bash
git add app/core/config.py env.example
git commit -m "feat(ROB-510): Finnhub news 재시도/타임아웃 settings 추가"
```

---

### Task 2: `fetch_news_finnhub` 재시도 + 백오프 + 시도당 타임아웃

**Files:**
- Modify: `app/services/finnhub_news.py`
- Create: `tests/services/test_finnhub_news.py`

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/services/test_finnhub_news.py` 신규:

```python
"""Finnhub news fetch retry/backoff/timeout (ROB-510)."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
import requests

from app.services import finnhub_news


def _client_with(side_effects):
    """company_news가 side_effects를 순서대로 내는 가짜 SDK 클라이언트."""
    client = MagicMock()
    client.company_news = MagicMock(side_effect=side_effects)
    client.general_news = MagicMock(side_effect=side_effects)
    return client


@pytest.fixture(autouse=True)
def _fast_retry(monkeypatch):
    """테스트에서 백오프 대기 제거."""
    from tenacity import wait_none

    monkeypatch.setattr(finnhub_news, "FINNHUB_NEWS_RETRY_WAIT", wait_none())


_OK_ITEM = [{"headline": "t", "source": "s", "datetime": 1765400000, "url": "https://u", "summary": "", "related": "AAPL"}]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_transient_network_error_is_retried_then_succeeds(monkeypatch):
    client = _client_with([requests.ConnectionError("boom"), _OK_ITEM])
    monkeypatch.setattr(finnhub_news, "_get_finnhub_client", lambda: client)

    result = await finnhub_news.fetch_news_finnhub("AAPL", "us", 5)

    assert result["count"] == 1
    assert client.company_news.call_count == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_exhausted_retries_reraise_original_error(monkeypatch):
    client = _client_with([requests.ConnectionError("boom")] * 5)
    monkeypatch.setattr(finnhub_news, "_get_finnhub_client", lambda: client)

    with pytest.raises(requests.ConnectionError):
        await finnhub_news.fetch_news_finnhub("AAPL", "us", 5, max_attempts=3)
    assert client.company_news.call_count == 3


@pytest.mark.unit
@pytest.mark.asyncio
async def test_per_attempt_timeout_is_retried(monkeypatch):
    calls = {"n": 0}

    def slow_then_ok(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            import time

            time.sleep(0.5)  # timeout_s=0.05보다 길게 — 첫 시도 TimeoutError
        return _OK_ITEM

    client = MagicMock()
    client.company_news = MagicMock(side_effect=slow_then_ok)
    monkeypatch.setattr(finnhub_news, "_get_finnhub_client", lambda: client)

    result = await finnhub_news.fetch_news_finnhub(
        "AAPL", "us", 5, timeout_s=0.05, max_attempts=2
    )

    assert result["count"] == 1
    assert calls["n"] == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_non_retryable_4xx_fails_immediately(monkeypatch):
    class FakeAPIError(Exception):
        status_code = 401

    monkeypatch.setattr(
        finnhub_news, "_is_retryable_news_error", finnhub_news._is_retryable_news_error
    )
    client = _client_with([FakeAPIError("unauthorized")] * 3)
    monkeypatch.setattr(finnhub_news, "_get_finnhub_client", lambda: client)

    with pytest.raises(FakeAPIError):
        await finnhub_news.fetch_news_finnhub("AAPL", "us", 5, max_attempts=3)
    assert client.company_news.call_count == 1  # 4xx는 비재시도


@pytest.mark.unit
def test_retryable_classifier():
    assert finnhub_news._is_retryable_news_error(TimeoutError()) is True
    assert finnhub_news._is_retryable_news_error(requests.ReadTimeout()) is True
    assert finnhub_news._is_retryable_news_error(ValueError("no key")) is False
    assert finnhub_news._is_retryable_news_error(ImportError("no sdk")) is False

    if finnhub_news.finnhub is not None:
        exc5xx = finnhub_news.finnhub.FinnhubAPIException.__new__(
            finnhub_news.finnhub.FinnhubAPIException
        )
        exc5xx.status_code = 503
        assert finnhub_news._is_retryable_news_error(exc5xx) is True
        exc429 = finnhub_news.finnhub.FinnhubAPIException.__new__(
            finnhub_news.finnhub.FinnhubAPIException
        )
        exc429.status_code = 429
        assert finnhub_news._is_retryable_news_error(exc429) is True
        exc4xx = finnhub_news.finnhub.FinnhubAPIException.__new__(
            finnhub_news.finnhub.FinnhubAPIException
        )
        exc4xx.status_code = 403
        assert finnhub_news._is_retryable_news_error(exc4xx) is False
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/services/test_finnhub_news.py -v`
Expected: FAIL — `AttributeError: ... no attribute 'FINNHUB_NEWS_RETRY_WAIT'` (또는 `_is_retryable_news_error` 미정의)

- [ ] **Step 3: 구현**

`app/services/finnhub_news.py` — import 블록에 추가:

```python
from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)
```

모듈 레벨(예: `_get_finnhub_client` 아래)에 추가:

```python
# ROB-510: 테스트에서 monkeypatch로 대기 제거 가능하도록 모듈 레벨 상수
FINNHUB_NEWS_RETRY_WAIT = wait_exponential_jitter(initial=0.5, max=2.0)


def _news_setting(name: str, default: Any) -> Any:
    """Lazy settings read — app config을 import 전제조건으로 만들지 않는다."""
    env_value = os.getenv(name)
    if env_value is not None:
        try:
            return type(default)(env_value)
        except (TypeError, ValueError):
            return default
    try:
        from app.core.config import settings
    except Exception:  # noqa: BLE001 — config 부재 환경에서도 동작
        return default
    return getattr(settings, name, default)


def _is_retryable_news_error(exc: BaseException) -> bool:
    """타임아웃/네트워크/5xx/429만 재시도. 4xx·설정오류는 즉시 실패."""
    if isinstance(exc, TimeoutError):
        return True
    if finnhub is not None and isinstance(exc, finnhub.FinnhubAPIException):
        status = getattr(exc, "status_code", None)
        return status == 429 or (isinstance(status, int) and status >= 500)
    try:
        import requests
    except ImportError:  # pragma: no cover
        return False
    return isinstance(exc, requests.RequestException)
```

`fetch_news_finnhub`를 다음으로 교체 (정규화 로직은 그대로):

```python
async def fetch_news_finnhub(
    symbol: str,
    market: str,
    limit: int,
    *,
    timeout_s: float | None = None,
    max_attempts: int | None = None,
) -> dict[str, Any]:
    """Fetch and normalize Finnhub news using the existing MCP response shape.

    ROB-510: per-attempt timeout + bounded exponential-backoff retry.
    """
    client = _get_finnhub_client()
    per_attempt_timeout = (
        timeout_s
        if timeout_s is not None
        else float(_news_setting("FINNHUB_NEWS_TIMEOUT_S", 8.0))
    )
    attempts = (
        max_attempts
        if max_attempts is not None
        else int(_news_setting("FINNHUB_NEWS_MAX_ATTEMPTS", 3))
    )
    to_date = datetime.date.today()
    from_date = to_date - datetime.timedelta(days=7)

    def fetch_sync() -> list[dict[str, Any]]:
        if market == "crypto":
            news = client.general_news("crypto", min_id=0)
        else:
            news = client.company_news(
                symbol.upper(),
                _from=from_date.strftime("%Y-%m-%d"),
                to=to_date.strftime("%Y-%m-%d"),
            )
        return news[:limit] if news else []

    news_items: list[dict[str, Any]] = []
    async for attempt in AsyncRetrying(
        stop=stop_after_attempt(max(1, attempts)),
        wait=FINNHUB_NEWS_RETRY_WAIT,
        retry=retry_if_exception(_is_retryable_news_error),
        reraise=True,
    ):
        with attempt:
            news_items = await asyncio.wait_for(
                asyncio.to_thread(fetch_sync), timeout=per_attempt_timeout
            )
```

(이후 `result_items` 정규화·return 블록은 기존 코드 그대로 유지. `import os`,
`from typing import Any`는 파일 상단에 이미 있는지 확인 후 없으면 추가.)

주의: `asyncio.wait_for`가 끊어도 `to_thread`의 sync 스레드는 백그라운드에서
계속 돌 수 있다(취소 불가). 시도당 8s × 3시도 bounded라 허용 — docstring에 명기.

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/services/test_finnhub_news.py -v`
Expected: 전부 PASS

- [ ] **Step 5: 기존 소비처 무회귀 확인**

Run: `uv run pytest tests/services/test_symbol_news_service.py tests/mcp_server/tooling/test_get_news_envelope.py -v`
Expected: 전부 PASS (시그니처는 keyword-only 추가라 기존 호출 불변)

- [ ] **Step 6: 커밋**

```bash
git add app/services/finnhub_news.py tests/services/test_finnhub_news.py
git commit -m "feat(ROB-510): fetch_news_finnhub 재시도+백오프+시도당 타임아웃"
```

---

### Task 3: `symbol_news_store` market-generic upsert + summary 영속

**Files:**
- Modify: `app/services/symbol_news_store.py`
- Test: `tests/services/test_symbol_news_store.py` (확장)

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/services/test_symbol_news_store.py` 끝에 추가:

```python
@pytest.mark.integration
@pytest.mark.asyncio
async def test_upsert_feed_articles_us_market_and_summary(db_session) -> None:
    symbol = f"U-{uuid.uuid4()}"[:20]
    url = _unique_url("us1")
    items = [
        FeedArticleInput(
            url=url,
            title="AAPL beats estimates",
            source="Reuters",
            published_at=datetime(2026, 6, 10, 9, 0, tzinfo=UTC),
            summary="Apple Q2 revenue beat.",
        )
    ]

    created = await symbol_news_store.upsert_feed_articles(
        db_session,
        "us",
        symbol,
        items,
        feed_source=symbol_news_store.FINNHUB_COMPANY_FEED_SOURCE,
    )
    assert created == 1

    article = (
        await db_session.execute(select(NewsArticle).where(NewsArticle.url == url))
    ).scalar_one()
    assert article.market == "us"
    assert article.summary == "Apple Q2 revenue beat."
    assert article.feed_source == "finnhub_company_news"

    link = (
        await db_session.execute(
            select(SymbolNewsRelevance).where(
                SymbolNewsRelevance.article_id == article.id,
                SymbolNewsRelevance.market == "us",
                SymbolNewsRelevance.symbol == symbol,
            )
        )
    ).scalar_one()
    assert link.status == "pending"
    assert link.feed_source == "finnhub_company_news"

    # 멱등: 재호출 시 신규 링크 0
    again = await symbol_news_store.upsert_feed_articles(
        db_session,
        "us",
        symbol,
        items,
        feed_source=symbol_news_store.FINNHUB_COMPANY_FEED_SOURCE,
    )
    assert again == 0

    # load가 summary를 복원
    stored, _ = await symbol_news_store.load_symbol_news(db_session, symbol, "us", 10)
    assert stored[0].summary == "Apple Q2 revenue beat."


@pytest.mark.integration
@pytest.mark.asyncio
async def test_upsert_feed_articles_crypto_shared_article_two_symbols(
    db_session,
) -> None:
    """crypto는 general 피드 — 같은 기사가 심볼별 링크로 공유된다."""
    url = _unique_url("cr1")
    sym_a = f"KRW-BTC-{uuid.uuid4()}"[:30]
    sym_b = f"KRW-ETH-{uuid.uuid4()}"[:30]
    items = [
        FeedArticleInput(
            url=url, title="Crypto rally", source="CoinDesk", published_at=None
        )
    ]

    a = await symbol_news_store.upsert_feed_articles(
        db_session, "crypto", sym_a, items,
        feed_source=symbol_news_store.FINNHUB_GENERAL_FEED_SOURCE,
    )
    b = await symbol_news_store.upsert_feed_articles(
        db_session, "crypto", sym_b, items,
        feed_source=symbol_news_store.FINNHUB_GENERAL_FEED_SOURCE,
    )
    assert (a, b) == (1, 1)

    count = (
        await db_session.execute(
            select(NewsArticle).where(NewsArticle.url == url)
        )
    ).scalars().all()
    assert len(count) == 1  # 기사 1건, 링크 2건


@pytest.mark.integration
@pytest.mark.asyncio
async def test_upsert_kr_wrapper_unchanged(db_session) -> None:
    """기존 KR 래퍼는 동작 불변 (market='kr', naver feed_source)."""
    symbol = f"S-{uuid.uuid4()}"[:20]
    url = _unique_url("krw1")
    created = await symbol_news_store.upsert_kr_feed_articles(
        db_session, symbol, [_item(url, f"{symbol} 투자")]
    )
    assert created == 1
    article = (
        await db_session.execute(select(NewsArticle).where(NewsArticle.url == url))
    ).scalar_one()
    assert article.market == "kr"
    assert article.feed_source == "naver_item_news"
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/services/test_symbol_news_store.py -v -k "feed_articles"`
Expected: 신규 3개 FAIL (`upsert_feed_articles`/`FINNHUB_*` 미정의, `FeedArticleInput` summary 없음), 기존 통과

- [ ] **Step 3: 구현**

`app/services/symbol_news_store.py`:

상수 (KR_FEED_SOURCE 아래):

```python
FINNHUB_COMPANY_FEED_SOURCE = "finnhub_company_news"  # us
FINNHUB_GENERAL_FEED_SOURCE = "finnhub_general_news"  # crypto (심볼 키 아님)
```

`FeedArticleInput`에 필드 추가:

```python
@dataclass(frozen=True)
class FeedArticleInput:
    url: str
    title: str
    source: str | None
    published_at: datetime | None
    summary: str | None = None
```

`StoredSymbolNews`에 필드 추가:

```python
@dataclass(frozen=True)
class StoredSymbolNews:
    article_id: int
    url: str
    title: str
    source: str | None
    published_at: datetime | None
    relevance: dict[str, Any]
    summary: str | None = None
```

`upsert_kr_feed_articles` (73~153)를 generic 본체 + KR 래퍼로 분리.
본체는 기존 코드에서 `"market": "kr"` 2곳(93, 126)을 `market` 파라미터로,
`build_relevance_hints(symbol=symbol, market="kr", ...)`을 `market=market`으로
바꾸고 article_values에 `"summary": item.summary` 추가:

```python
async def upsert_feed_articles(
    db: AsyncSession,
    market: str,
    symbol: str,
    items: list[FeedArticleInput],
    *,
    feed_source: str,
) -> int:
    """Set-difference upsert: new urls insert, known urls no-op (idempotent).

    Returns the number of *newly created* pending links (ROB-506 enqueue
    trigger). 0 when every (article, symbol) pair already existed.
    """
    if not items:
        return 0
    now = _utcnow()
    article_values = [
        {
            "url": item.url,
            "title": item.title[:500],
            "source": item.source,
            "summary": item.summary,
            "market": market,
            "feed_source": feed_source,
            "article_published_at": item.published_at.replace(tzinfo=None)
            if item.published_at
            else None,
            "is_analyzed": False,
            "scraped_at": now,
            "created_at": now,
            "updated_at": now,
        }
        for item in items
    ]
    # ... (이하 기존 본문 그대로, link_values의 "market": market /
    #      build_relevance_hints(symbol=symbol, market=market, title=item.title))
    ...


async def upsert_kr_feed_articles(
    db: AsyncSession,
    symbol: str,
    items: list[FeedArticleInput],
    *,
    feed_source: str = KR_FEED_SOURCE,
) -> int:
    """KR 호환 래퍼 — 기존 호출부 보존용 (ROB-491)."""
    return await upsert_feed_articles(
        db, "kr", symbol, items, feed_source=feed_source
    )
```

`load_symbol_news`의 `StoredSymbolNews(...)` 생성에 `summary=article.summary` 추가.

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/services/test_symbol_news_store.py tests/routers/test_news_relevance_ingest.py tests/jobs/test_news_relevance_judgment.py -v`
Expected: 전부 PASS (래퍼 유지로 기존 호출부 불변)

- [ ] **Step 5: 커밋**

```bash
git add app/services/symbol_news_store.py tests/services/test_symbol_news_store.py
git commit -m "feat(ROB-510): symbol_news_store market-generic upsert + summary 영속"
```

---

### Task 4: `symbol_news_service` 내부 일반화 (동작 불변 리팩터)

**Files:**
- Modify: `app/services/symbol_news_service.py:153-248` (`_stored_to_article`, `_maybe_enqueue_judgment`, `_kr_persist_and_load`)
- Modify: `tests/services/test_symbol_news_service.py:37-52` (`_patch_store`)

- [ ] **Step 1: `_stored_to_article` 일반화**

`symbol_news_service.py:153-178`을 교체:

```python
def _stored_to_article(
    row: StoredSymbolNews,
    *,
    provider: str,
    market: str,
    symbol: str,
    fetched_at: datetime,
    raw_by_url: dict[str, Any],
) -> SymbolNewsArticle:
    source_item = raw_by_url.get(row.url) or {
        "title": row.title,
        "url": row.url,
        "source": row.source or "",
        "datetime": row.published_at.isoformat() if row.published_at else None,
        **({"summary": row.summary or ""} if provider == "finnhub" else {}),
    }
    external_id = (
        _naver_external_id(row.url) if provider == "naver" else _url_hash(row.url)
    )
    return SymbolNewsArticle(
        provider=provider,
        market=market,
        symbol=symbol,
        external_article_id=external_id,
        title=row.title,
        source_name=row.source,
        canonical_url=row.url,
        summary=row.summary if provider == "finnhub" else None,
        published_at=row.published_at,
        fetched_at=fetched_at,
        related_symbols=[],
        provider_metadata={"source_item": source_item, "relevance": row.relevance},
    )
```

- [ ] **Step 2: `_maybe_enqueue_judgment` market 스레딩**

`symbol_news_service.py:181-202` 시그니처를 `(market: str, symbol: str, new_pending: int)`로 바꾸고 `kiq(market=market, symbol=symbol, dry_run=False)`로. 로그 메시지에 `market=%s` 추가.

- [ ] **Step 3: `_kr_persist_and_load` → `_persist_and_load` 일반화**

`symbol_news_service.py:205-248`을 교체:

```python
async def _persist_and_load(
    symbol: str,
    market: str,
    provider: str,
    feed_source: str,
    fetched: list[SymbolNewsArticle],
    limit: int,
    fetched_at: datetime,
) -> tuple[list[SymbolNewsArticle], int] | None:
    """Persist this window then serve canonical DB state. None → DB unavailable."""
    inserted: Any = 0
    try:
        async with AsyncSessionLocal() as db:
            if fetched:
                inserted = await symbol_news_store.upsert_feed_articles(
                    db,
                    market,
                    symbol,
                    [
                        FeedArticleInput(
                            url=a.canonical_url,
                            title=a.title,
                            source=a.source_name,
                            published_at=a.published_at,
                            summary=a.summary,
                        )
                        for a in fetched
                    ],
                    feed_source=feed_source,
                )
            stored, excluded_count = await symbol_news_store.load_symbol_news(
                db, symbol, market, limit
            )
    except Exception as exc:  # noqa: BLE001 — cache layer must not kill the tool
        logger.warning(
            "symbol_news_service: store unavailable, degrading: "
            "market=%s symbol=%s err=%s",
            market,
            symbol,
            exc,
        )
        return None
    new_pending = inserted if isinstance(inserted, int) else 0
    await _maybe_enqueue_judgment(market, symbol, new_pending)
    raw_by_url = {
        a.canonical_url: a.provider_metadata.get("source_item") for a in fetched
    }
    articles = [
        _stored_to_article(
            row,
            provider=provider,
            market=market,
            symbol=symbol,
            fetched_at=fetched_at,
            raw_by_url=raw_by_url,
        )
        for row in stored
    ]
    return articles, excluded_count
```

KR 분기(`fetch_symbol_news:315`)의 호출을 교체:

```python
        persisted = await _persist_and_load(
            symbol,
            "kr",
            "naver",
            symbol_news_store.KR_FEED_SOURCE,
            fetched or [],
            limit,
            fetched_at,
        )
```

- [ ] **Step 4: 테스트 픽스처 갱신**

`tests/services/test_symbol_news_service.py:37-52` `_patch_store`에서 patch 대상을 새 이름으로:

```python
    monkeypatch.setattr(
        symbol_news_service.symbol_news_store, "upsert_feed_articles", upsert
    )
```

(313행 인근 동일 패턴도 같이 교체. `upsert.call_args` 단언이 있으면 새 시그니처
`(db, market, symbol, items)` + `feed_source` kwarg 기준으로 수정.)

- [ ] **Step 5: 무회귀 확인**

Run: `uv run pytest tests/services/test_symbol_news_service.py tests/mcp_server/tooling/test_get_news_envelope.py -v`
Expected: 전부 PASS (KR 동작 불변)

- [ ] **Step 6: 커밋**

```bash
git add app/services/symbol_news_service.py tests/services/test_symbol_news_service.py
git commit -m "refactor(ROB-510): persist-and-load 경로 market-generic 일반화 (동작 불변)"
```

---

### Task 5: US/crypto 분기를 DB persist-and-load 구조로 재작성

**Files:**
- Modify: `app/services/symbol_news_service.py:373-402` (`fetch_symbol_news` US/crypto 분기)
- Test: `tests/services/test_symbol_news_service.py` (확장)

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/services/test_symbol_news_service.py`에 추가. 먼저 US용 stored 헬퍼:

```python
def _stored_us(article_id: int, url: str, title: str, status: str = "pending"):
    from app.services.symbol_news_store import StoredSymbolNews

    return StoredSymbolNews(
        article_id=article_id,
        url=url,
        title=title,
        source="Reuters",
        published_at=datetime(2026, 6, 10, 9, 0),
        relevance={
            "status": status,
            "relationship": None,
            "relevance": None,
            "price_relevance": None,
            "score": None,
            "reason": None,
            "judged_by": None,
            "judged_at": None,
            "hints": None,
        },
        summary="summary text",
    )


_FINNHUB_RAW = {
    "symbol": "AAPL",
    "market": "us",
    "source": "finnhub",
    "count": 1,
    "news": [
        {
            "title": "Apple beats",
            "source": "Reuters",
            "datetime": "2026-06-10T09:00:00",
            "url": "https://r/apple-beats",
            "summary": "summary text",
            "sentiment": {"score": 0.7},
            "related": "AAPL",
        }
    ],
}
```

테스트 5개:

```python
@pytest.mark.unit
@pytest.mark.asyncio
async def test_us_persists_then_serves_db_state(monkeypatch) -> None:
    monkeypatch.setattr(
        symbol_news_service,
        "fetch_news_finnhub",
        AsyncMock(return_value=_FINNHUB_RAW),
    )
    upsert, load = _patch_store(
        monkeypatch,
        stored=[_stored_us(1, "https://r/apple-beats", "Apple beats")],
        excluded_count=2,
    )

    result = await symbol_news_service.fetch_symbol_news("AAPL", "us", limit=10)

    assert result.status == "ok"
    assert result.provider == "finnhub"
    assert result.excluded_count == 2
    assert result.degraded is False
    upsert.assert_awaited_once()
    args, kwargs = upsert.await_args
    assert args[1] == "us"  # market
    assert kwargs["feed_source"] == "finnhub_company_news"
    # 신선 fetch 항목은 sentiment 포함 원본 source_item 보존
    art = result.articles[0]
    assert art.provider_metadata["source_item"]["sentiment"] == {"score": 0.7}
    assert art.provider_metadata["relevance"]["status"] == "pending"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_us_fetch_failure_serves_db_cache_degraded(monkeypatch) -> None:
    monkeypatch.setattr(
        symbol_news_service,
        "fetch_news_finnhub",
        AsyncMock(side_effect=TimeoutError()),
    )
    _patch_store(
        monkeypatch, stored=[_stored_us(1, "https://r/cached", "Cached headline")]
    )

    result = await symbol_news_service.fetch_symbol_news("AAPL", "us", limit=10)

    assert result.status == "ok"
    assert result.degraded is True
    assert result.fetch_error == "TimeoutError"
    assert result.articles[0].title == "Cached headline"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_us_fetch_failure_with_empty_db_is_error(monkeypatch) -> None:
    monkeypatch.setattr(
        symbol_news_service,
        "fetch_news_finnhub",
        AsyncMock(side_effect=TimeoutError()),
    )
    _patch_store(monkeypatch, stored=[])

    result = await symbol_news_service.fetch_symbol_news("AAPL", "us", limit=10)

    assert result.status == "error"
    assert result.error_code == "TimeoutError"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_us_db_failure_degrades_to_on_demand_pending(monkeypatch) -> None:
    monkeypatch.setattr(
        symbol_news_service,
        "fetch_news_finnhub",
        AsyncMock(return_value=_FINNHUB_RAW),
    )
    monkeypatch.setattr(
        symbol_news_service.symbol_news_store,
        "upsert_feed_articles",
        AsyncMock(side_effect=RuntimeError("db down")),
    )
    fake_cm = MagicMock()
    fake_cm.__aenter__ = AsyncMock(return_value=MagicMock())
    fake_cm.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(
        symbol_news_service, "AsyncSessionLocal", MagicMock(return_value=fake_cm)
    )

    result = await symbol_news_service.fetch_symbol_news("AAPL", "us", limit=10)

    assert result.status == "ok"
    relevance = result.articles[0].provider_metadata["relevance"]
    assert relevance["status"] == "pending"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_crypto_uses_general_feed_source(monkeypatch) -> None:
    raw = {**_FINNHUB_RAW, "market": "crypto"}
    monkeypatch.setattr(
        symbol_news_service, "fetch_news_finnhub", AsyncMock(return_value=raw)
    )
    upsert, _ = _patch_store(
        monkeypatch, stored=[_stored_us(1, "https://r/apple-beats", "Apple beats")]
    )

    result = await symbol_news_service.fetch_symbol_news("KRW-BTC", "crypto", limit=10)

    assert result.status == "ok"
    _, kwargs = upsert.await_args
    assert kwargs["feed_source"] == "finnhub_general_news"
```

기존 `test_us_finnhub_preserves_source_item_and_sentiment`(204행~)는 DB 미경유
전제이므로 `_patch_store`로 store를 끼우도록 수정 (stored는 fetch와 같은 URL,
단언 유지).

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/services/test_symbol_news_service.py -v -k "us or crypto"`
Expected: 신규 5개 FAIL (US 분기가 아직 DB 미경유), 기존 KR 통과

- [ ] **Step 3: 구현**

`symbol_news_service.py:373-402`의 US/crypto 분기를 교체:

```python
    if market not in ("us", "crypto"):
        return SymbolNewsFetchResult(
            symbol,
            market,
            provider,
            "unavailable",
            limit,
            0,
            [],
            "unsupported_market",
        )

    finnhub_fetched: list[SymbolNewsArticle] | None
    finnhub_error: str | None = None
    try:
        # ROB-510: 재시도/시도당 타임아웃은 fetch_news_finnhub가 소유 —
        # 외곽 wait_for를 두면 재시도가 무력화된다.
        finnhub_fetched = await _fetch_finnhub(symbol, market, limit, fetched_at)
    except Exception as exc:  # noqa: BLE001 — fall back to DB cache
        logger.warning(
            "symbol_news_service: finnhub fetch failed: symbol=%s market=%s err=%s",
            symbol,
            market,
            exc,
        )
        finnhub_fetched = None
        finnhub_error = type(exc).__name__

    feed_source = (
        symbol_news_store.FINNHUB_GENERAL_FEED_SOURCE
        if market == "crypto"
        else symbol_news_store.FINNHUB_COMPANY_FEED_SOURCE
    )
    persisted = await _persist_and_load(
        symbol,
        market,
        "finnhub",
        feed_source,
        finnhub_fetched or [],
        limit,
        fetched_at,
    )
    if persisted is not None:
        articles, excluded_count = persisted
        if finnhub_fetched is None and not articles:
            return SymbolNewsFetchResult(
                symbol,
                market,
                provider,
                "error",
                limit,
                0,
                [],
                finnhub_error or "finnhub_fetch_failed",
            )
        status = "ok" if articles else "empty"
        return SymbolNewsFetchResult(
            symbol,
            market,
            provider,
            status,
            limit,
            len(articles),
            articles,
            None,
            excluded_count=excluded_count,
            degraded=finnhub_fetched is None,
            fetch_error=finnhub_error,
        )
    # DB 불가 — 기존 on-demand 동작으로 degrade (전부 pending 표시)
    if finnhub_fetched is None:
        return SymbolNewsFetchResult(
            symbol,
            market,
            provider,
            "error",
            limit,
            0,
            [],
            finnhub_error or "finnhub_fetch_failed",
        )
    articles = [
        replace(
            a,
            provider_metadata={
                **a.provider_metadata,
                "relevance": {
                    **_PENDING_RELEVANCE,
                    "hints": _store_hints(symbol, market, a.title),
                },
            },
        )
        for a in finnhub_fetched
    ]
    status = "ok" if articles else "empty"
    return SymbolNewsFetchResult(
        symbol, market, provider, status, limit, len(articles), articles, None
    )
```

`symbol_news_store_hints`(113-116)를 market 인자를 받게 일반화:

```python
def _store_hints(symbol: str, market: str, title: str) -> dict[str, Any] | None:
    from app.services.symbol_news_relevance import build_relevance_hints

    return build_relevance_hints(symbol=symbol, market=market, title=title)


def symbol_news_store_hints(symbol: str, title: str) -> dict[str, Any] | None:
    """KR 호환 래퍼 (기존 외부 사용처 보존)."""
    return _store_hints(symbol, "kr", title)
```

(KR 분기 343~371의 `symbol_news_store_hints(symbol, a.title)` 호출은 그대로 둬도 된다.)

`_fetch_finnhub`(251~)는 변경 없음 — 외곽 `asyncio.wait_for` 제거는 이 분기
교체로 자연 소멸. `fetch_symbol_news`의 `timeout_s` 파라미터 docstring에
"naver(KR) 전용 — Finnhub 타임아웃은 `FINNHUB_NEWS_TIMEOUT_S`" 명기.

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/services/test_symbol_news_service.py -v`
Expected: 전부 PASS

- [ ] **Step 5: 커밋**

```bash
git add app/services/symbol_news_service.py tests/services/test_symbol_news_service.py
git commit -m "feat(ROB-510): US/crypto get_news를 DB persist-and-load + degraded 폴백으로"
```

---

### Task 6: get_news envelope 표면 검증 (handler 코드 변경 없음)

**Files:**
- Test: `tests/mcp_server/tooling/test_get_news_envelope.py` (확장)

- [ ] **Step 1: 기존 US envelope 테스트 확인 후 신규 테스트 추가**

기존 `test_get_news_us_envelope_keys_preserved`(168~197행)가 service를 어떻게
mock하는지 확인하고 동일 패턴으로 추가 (service-level mock이면
`SymbolNewsFetchResult`를 직접 구성):

```python
@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_news_us_surfaces_relevance_and_degraded(monkeypatch) -> None:
    from datetime import UTC, datetime

    from app.mcp_server.tooling.fundamentals import _news
    from app.services.symbol_news_service import (
        SymbolNewsArticle,
        SymbolNewsFetchResult,
    )

    article = SymbolNewsArticle(
        provider="finnhub",
        market="us",
        symbol="AAPL",
        external_article_id="abc123",
        title="Cached headline",
        source_name="Reuters",
        canonical_url="https://r/cached",
        summary="cached summary",
        published_at=datetime(2026, 6, 10, 9, 0),
        fetched_at=datetime.now(tz=UTC),
        provider_metadata={
            "source_item": {"title": "Cached headline", "url": "https://r/cached"},
            "relevance": {"status": "pending"},
        },
    )
    result = SymbolNewsFetchResult(
        symbol="AAPL",
        market="us",
        provider="finnhub",
        status="ok",
        requested_limit=10,
        returned_count=1,
        articles=[article],
        excluded_count=3,
        degraded=True,
        fetch_error="TimeoutError",
    )
    monkeypatch.setattr(
        _news.symbol_news_service,
        "fetch_symbol_news",
        AsyncMock(return_value=result),
    )

    payload = await _news.handle_get_news("AAPL", market="us")

    assert payload["degraded"] is True
    assert payload["fetch_error"] == "TimeoutError"
    assert payload["excluded_count"] == 3
    assert payload["news"][0]["relevance"]["status"] == "pending"
```

- [ ] **Step 2: 통과 확인**

Run: `uv run pytest tests/mcp_server/tooling/test_get_news_envelope.py -v`
Expected: 전부 PASS (handler 74-76행 degraded 표면은 기존 코드 — 코드 변경 없이 통과해야 정상)

- [ ] **Step 3: 커밋**

```bash
git add tests/mcp_server/tooling/test_get_news_envelope.py
git commit -m "test(ROB-510): US envelope relevance/excluded_count/degraded 표면 검증"
```

---

### Task 7: 런북·문서 갱신

**Files:**
- Modify: `docs/runbooks/news-relevance-judgment.md`
- Modify: `CLAUDE.md` (ROB-491 섹션)

- [ ] **Step 1: 런북에 US/crypto 섹션 추가**

`docs/runbooks/news-relevance-judgment.md`에 추가 (기존 KR 절 뒤):

```markdown
## US / crypto (Finnhub) — ROB-510

ROB-510부터 `get_news(market="us"|"crypto")`도 KR과 동일하게
`news_articles` + `symbol_news_relevance`에 set-difference upsert 후 DB
상태로 응답한다 (pending 표시, excluded 제외). 판정 파이프라인(worker /
GET pending / POST ingest/bulk)은 market 파라미터로 이미 지원되며 별도
배선 변경 없음 — pending 적체 점검 시 `market=us`, `market=crypto`도 함께
조회할 것.

- feed_source: `finnhub_company_news`(us) / `finnhub_general_news`(crypto)
- crypto는 Finnhub general 피드(심볼 키 아님)라 unrelated 비율이 높을 수
  있다 — `relationship=unrelated`/`relevance=low` → excluded 파생은 KR과
  동일.
- Finnhub fetch는 시도당 `FINNHUB_NEWS_TIMEOUT_S`(기본 8s) ×
  `FINNHUB_NEWS_MAX_ATTEMPTS`(기본 3) 재시도. 전 실패 시 응답은
  `degraded: true` + `fetch_error` + DB 기사(stale) 폴백.
- degraded 폴백으로 DB에서 복원된 항목은 sentiment가 없을 수 있다
  (sentiment는 미영속 — 신선 fetch 응답에만 포함).
```

- [ ] **Step 2: CLAUDE.md ROB-491 섹션에 한 줄 추가**

`### get_news 관련성 파이프라인 (ROB-491)` 섹션 끝에:

```markdown
- **ROB-510**: US/crypto(Finnhub)도 동일 DB 파이프라인 합류 (feed_source
  `finnhub_company_news`/`finnhub_general_news`). Finnhub fetch는
  `FINNHUB_NEWS_TIMEOUT_S`×`FINNHUB_NEWS_MAX_ATTEMPTS` 재시도, 전 실패 시
  degraded + DB stale 폴백.
```

- [ ] **Step 3: 커밋**

```bash
git add docs/runbooks/news-relevance-judgment.md CLAUDE.md
git commit -m "docs(ROB-510): 런북/CLAUDE.md에 US/crypto 뉴스 파이프라인 반영"
```

---

### Task 8: 전체 게이트 + PR

- [ ] **Step 1: lint/format/typecheck (app + tests 둘 다 — CI 교훈)**

```bash
uv run ruff format app/ tests/ && uv run ruff check app/ tests/ --fix
make lint
```

Expected: clean

- [ ] **Step 2: 관련 테스트 전수 (부모 디렉토리 포함 — ROB-345 교훈)**

```bash
uv run pytest tests/services/ tests/mcp_server/tooling/ tests/routers/test_news_relevance_ingest.py tests/jobs/ tests/tasks/test_news_relevance_judgment_tasks.py -v
```

Expected: 전부 PASS

- [ ] **Step 3: 풀 단위 스위트 (통합 제외)**

```bash
uv run pytest tests/ -m "not integration and not slow" -q
```

Expected: PASS (무관 실패 발생 시 clean origin/main과 교차 확인)

- [ ] **Step 4: PR 생성**

```bash
git push -u origin rob-510
gh pr create --base main --title "feat(ROB-510): get_news US/crypto Finnhub 재시도+DB 파이프라인 통합" --body "..."
```

PR 본문에 포함: spec 링크, 설계 결정 3줄(DB 구조 채택 / 신선도 게이트 생략
사유 / crypto 포함), acceptance 재해석(TTL 캐시 히트 → degraded DB 폴백)
명기, migration 0, 안전 경계(브로커 mutation 없음, 자동 제외 없음).
Linear ROB-510에도 acceptance 재해석 코멘트 게시.

---

## Self-Review 결과

- **Spec coverage**: 4.1→Task 1·2, 4.2→Task 3, 4.3→Task 4·5, 4.4→Task 6, 4.5→Task 7, 테스트(§7)→각 Task 내 TDD + Task 8. 누락 없음.
- **타입 일관성**: `upsert_feed_articles(db, market, symbol, items, *, feed_source)` — Task 3 정의 = Task 4 호출 = Task 5 테스트 단언 일치. `StoredSymbolNews.summary`/`FeedArticleInput.summary` default None으로 기존 생성부 호환.
- **알려진 트레이드오프**: ① `to_thread` 스레드는 wait_for 취소 후에도 잔존 가능(bounded, docstring 명기) ② sentiment 미영속 — degraded 폴백 항목에 한해 누락(런북 명기) ③ crypto general 피드의 심볼 링크는 판정이 정리(unrelated→excluded).

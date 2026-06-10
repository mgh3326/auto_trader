# ROB-506: TaskIQ 기반 get_news relevance 판정 worker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** ROB-491이 만든 `symbol_news_relevance` pending rows를 `get_news` 직후 TaskIQ 비동기 worker가 외부 Hermes-호환 webhook으로 판정 요청하고 결과를 기존 ingest 규칙으로 write-back하도록 한다.

**Architecture:** `get_news`(KR persist 경로)에서 새 pending link가 생기면 default-off flag 하에 `news_relevance.judge_pending` task를 fail-open enqueue한다. Task는 `app/jobs/news_relevance_judgment.py` 오케스트레이션을 호출하고, 그 job이 pending batch를 조회해 `NewsRelevanceJudgmentClient`(httpx, `HermesNotificationClient` 패턴)로 외부 judgment endpoint에 POST한다. 응답에 inline `judgments`가 있으면 `symbol_news_store.apply_judgment`(status 서버 파생)로 적용하고, 없으면 dispatched로 간주(외부가 기존 token-authed ingest route로 write-back). 실패/검증실패 시 pending 유지. in-process LLM provider 없음, direct OpenRouter 없음, migration 없음.

**Tech Stack:** Python 3.13, FastAPI, TaskIQ(`taskiq_redis.ListQueueBroker`), httpx, SQLAlchemy async, pytest(`db_session` integration fixture + `httpx.MockTransport`).

**핵심 설계 결정 (이슈 §3 "구현 전 결정" 항목):**
judgment client는 **양쪽 모드를 단일 contract로 지원**한다 — 2xx 응답 body에 `judgments` 리스트가 있으면 task가 직접 적용(synchronous judge endpoint), 없으면 `dispatched`(Hermes 세션이 비동기로 기존 `/trading/api/news-relevance/ingest/bulk`에 write-back). 기존 운영 패턴(ROB-265 webhook fire-and-forget + ROB-491 token ingest)과 최소 결합이며 Hermes 측 진화를 막지 않는다. Job이 적용하는 judgment는 **요청한 (article_id, market, symbol) 튜플로 제한**한다(외부 endpoint의 overreach 방지; 범위 밖은 `skipped_unrequested`로 집계).

**Non-goals 재확인:** broker/order/watch/order-intent mutation 없음, Prefect/launchd/cron 활성화 없음, secret 값 로그/결과 출력 없음, destructive/expansion migration 없음(첫 PR은 migration 0 — lease/backoff 컬럼은 운영 후 필요 시 후속), direct DB insert smoke 우회 없음.

**전제:** worktree `/Users/mgh3326/work/auto_trader.rob-506`, branch `rob-506`, integration 테스트는 `docker compose up -d` (PostgreSQL/Redis) 필요.

---

### Task 1: `upsert_kr_feed_articles`가 신규 pending link 수를 반환

enqueue 트리거 판단("새 pending row가 생성되면")의 근거 값. PG `on_conflict_do_nothing`의 `rowcount`가 실제 insert된 행 수다.

**Files:**
- Modify: `app/services/symbol_news_store.py:73-146`
- Test: `tests/services/test_symbol_news_store.py`

- [ ] **Step 1: Write the failing test**

`tests/services/test_symbol_news_store.py` 끝에 추가:

```python
@pytest.mark.integration
@pytest.mark.asyncio
async def test_upsert_returns_new_pending_link_count(db_session) -> None:
    """ROB-506: 반환값 = 신규 생성된 pending link 수 (enqueue 트리거 근거)."""
    symbol = f"S-{uuid.uuid4()}"[:20]
    url1 = _unique_url("c1")
    url2 = _unique_url("c2")
    items = [_item(url1, f"{symbol} 신규 투자"), _item(url2, f"{symbol} 실적 발표")]

    first = await symbol_news_store.upsert_kr_feed_articles(db_session, symbol, items)
    assert first == 2

    # 동일 윈도우 재호출 — 신규 link 없음
    second = await symbol_news_store.upsert_kr_feed_articles(db_session, symbol, items)
    assert second == 0

    # 빈 입력 — 0
    assert await symbol_news_store.upsert_kr_feed_articles(db_session, symbol, []) == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/test_symbol_news_store.py::test_upsert_returns_new_pending_link_count -v`
Expected: FAIL — `assert None == 2` (현재 반환값 없음)

- [ ] **Step 3: Implement return count**

`app/services/symbol_news_store.py`의 `upsert_kr_feed_articles` 수정 — 시그니처/독스트링/조기리턴/마지막 insert 부분만 변경:

```python
async def upsert_kr_feed_articles(
    db: AsyncSession,
    symbol: str,
    items: list[FeedArticleInput],
    *,
    feed_source: str = KR_FEED_SOURCE,
) -> int:
    """Set-difference upsert: new urls insert, known urls no-op (idempotent).

    Returns the number of *newly created* pending links (ROB-506 enqueue
    trigger). 0 when every (article, symbol) pair already existed.
    """
    if not items:
        return 0
```

(중간 본문은 그대로) 마지막 link insert 블록과 리턴을:

```python
    new_links = 0
    if link_values:
        result = await db.execute(
            pg_insert(SymbolNewsRelevance)
            .values(link_values)
            .on_conflict_do_nothing(
                index_elements=[
                    SymbolNewsRelevance.article_id,
                    SymbolNewsRelevance.market,
                    SymbolNewsRelevance.symbol,
                ]
            )
        )
        new_links = int(result.rowcount or 0)
    await db.commit()
    return new_links
```

- [ ] **Step 4: Run store tests**

Run: `uv run pytest tests/services/test_symbol_news_store.py -v`
Expected: 전부 PASS (기존 테스트는 반환값을 안 보므로 영향 없음)

- [ ] **Step 5: Commit**

```bash
git add app/services/symbol_news_store.py tests/services/test_symbol_news_store.py
git commit -m "feat(ROB-506): upsert_kr_feed_articles returns new pending link count"
```

---

### Task 2: Settings — default-off flag + judgment endpoint 설정

**Files:**
- Modify: `app/core/config.py` (line ~495, `NEWS_RELEVANCE_INGEST_TOKEN_HEADER` 직후)
- Test: `tests/tasks/test_news_relevance_judgment_tasks.py` (신규 — Task 5에서 task 테스트와 같은 파일 사용)

- [ ] **Step 1: Write the failing test**

Create `tests/tasks/test_news_relevance_judgment_tasks.py`:

```python
"""ROB-506 — news_relevance.judge_pending task gating/registration."""

from __future__ import annotations

import pytest

from app.core.config import Settings


@pytest.mark.unit
def test_async_judgment_settings_default_off() -> None:
    fields = Settings.model_fields
    assert fields["NEWS_RELEVANCE_ASYNC_JUDGMENT_ENABLED"].default is False
    assert fields["NEWS_RELEVANCE_JUDGMENT_WEBHOOK_URL"].default == ""
    assert fields["NEWS_RELEVANCE_JUDGMENT_TOKEN"].default == ""
    assert fields["NEWS_RELEVANCE_JUDGMENT_TIMEOUT_S"].default == 120.0
    assert fields["NEWS_RELEVANCE_JUDGMENT_BATCH_LIMIT"].default == 50
```

(주의: `app.core.config`의 settings 클래스 이름이 `Settings`가 아니면 — `grep -n "^class.*Settings" app/core/config.py`로 확인 후 그 이름 사용.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/tasks/test_news_relevance_judgment_tasks.py -v`
Expected: FAIL — `KeyError: 'NEWS_RELEVANCE_ASYNC_JUDGMENT_ENABLED'`

- [ ] **Step 3: Add settings**

`app/core/config.py`의 `NEWS_RELEVANCE_INGEST_TOKEN_HEADER` 라인 바로 아래에 추가:

```python
    # ROB-506 — TaskIQ async judgment worker for symbol_news_relevance
    # pending rows. Default-off: get_news never enqueues and commit-mode
    # task runs return "disabled" until the operator flips the flag. The
    # webhook is the external Hermes-compatible judgment boundary — no
    # in-process LLM provider, no OpenRouter credential in this repo.
    # Distinct namespace from HERMES_* (notification) and
    # NEWS_RELEVANCE_INGEST_* (inbound token) on purpose.
    NEWS_RELEVANCE_ASYNC_JUDGMENT_ENABLED: bool = False
    NEWS_RELEVANCE_JUDGMENT_WEBHOOK_URL: str = ""
    NEWS_RELEVANCE_JUDGMENT_TOKEN: str = ""
    NEWS_RELEVANCE_JUDGMENT_TIMEOUT_S: float = 120.0
    NEWS_RELEVANCE_JUDGMENT_BATCH_LIMIT: int = 50
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/tasks/test_news_relevance_judgment_tasks.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/core/config.py tests/tasks/test_news_relevance_judgment_tasks.py
git commit -m "feat(ROB-506): default-off settings for async news-relevance judgment"
```

---

### Task 3: `NewsRelevanceJudgmentClient` — 외부 judgment boundary

`HermesNotificationClient`(`app/services/hermes_client.py`) 패턴: httpx AsyncClient, transport 주입 가능, secret 로그 금지.

**Files:**
- Create: `app/services/news_relevance_judgment_client.py`
- Test: `tests/services/test_news_relevance_judgment_client.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/services/test_news_relevance_judgment_client.py`:

```python
"""ROB-506 — NewsRelevanceJudgmentClient contract tests (httpx MockTransport)."""

from __future__ import annotations

import json
import logging

import httpx
import pytest

from app.services.news_relevance_judgment_client import (
    NewsRelevanceJudgmentClient,
)

_PENDING = [
    {
        "article_id": 101,
        "market": "kr",
        "symbol": "035420",
        "url": "https://x/a",
        "title": "네이버 신규 투자",
        "source": "매일경제",
        "published_at": "2026-06-10T09:00:00",
        "first_seen_at": "2026-06-10T09:05:00",
        "hints": None,
    }
]

_JUDGMENT = {
    "article_id": 101,
    "market": "kr",
    "symbol": "035420",
    "relationship": "direct",
    "relevance": "high",
    "price_relevance": "catalyst",
    "score": 0.9,
    "reason": "직접 보도",
    "judged_by": "hermes",
}


def _client(handler, **kwargs) -> NewsRelevanceJudgmentClient:
    return NewsRelevanceJudgmentClient(
        webhook_url="https://hermes.test/hooks/news-relevance-judgment",
        token="sekrit-token",
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_inline_judgments_response_is_judged() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"judgments": [_JUDGMENT]})

    client = _client(handler)
    result = await client.request_judgments(
        market="kr", symbol="035420", pending=_PENDING
    )
    await client.close()

    assert result.status == "judged"
    assert len(result.judgments) == 1
    assert result.judgments[0].article_id == 101
    assert captured["headers"]["authorization"] == "Bearer sekrit-token"
    assert captured["body"]["kind"] == "news_relevance_judgment_request"
    assert captured["body"]["pending"][0]["article_id"] == 101


@pytest.mark.unit
@pytest.mark.asyncio
async def test_accepted_without_judgments_is_dispatched() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(202, json={"status": "accepted"})

    client = _client(handler)
    result = await client.request_judgments(
        market="kr", symbol="035420", pending=_PENDING
    )
    await client.close()
    assert result.status == "dispatched"
    assert result.judgments == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_non_2xx_is_failed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream down")

    client = _client(handler)
    result = await client.request_judgments(
        market="kr", symbol="035420", pending=_PENDING
    )
    await client.close()
    assert result.status == "failed"
    assert result.http_status == 503
    assert result.reason == "http_503"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_network_error_is_failed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    client = _client(handler)
    result = await client.request_judgments(
        market="kr", symbol="035420", pending=_PENDING
    )
    await client.close()
    assert result.status == "failed"
    assert result.reason == "request_failed"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_invalid_judgment_items_are_counted_not_applied() -> None:
    bad = {**_JUDGMENT, "relevance": "ultra"}  # invalid enum

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"judgments": [_JUDGMENT, bad]})

    client = _client(handler)
    result = await client.request_judgments(
        market="kr", symbol="035420", pending=_PENDING
    )
    await client.close()
    assert result.status == "judged"
    assert len(result.judgments) == 1
    assert result.invalid_count == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unconfigured_url_is_skipped_without_http() -> None:
    client = NewsRelevanceJudgmentClient(webhook_url="", token="")
    result = await client.request_judgments(
        market="kr", symbol="035420", pending=_PENDING
    )
    await client.close()
    assert result.status == "skipped"
    assert result.reason == "webhook_url_not_configured"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_token_never_appears_in_logs_or_result(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="nope")

    client = _client(handler)
    with caplog.at_level(logging.DEBUG):
        result = await client.request_judgments(
            market="kr", symbol="035420", pending=_PENDING
        )
    await client.close()
    assert "sekrit-token" not in caplog.text
    assert "sekrit-token" not in repr(result)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/services/test_news_relevance_judgment_client.py -v`
Expected: FAIL — `ModuleNotFoundError: app.services.news_relevance_judgment_client`

- [ ] **Step 3: Implement the client**

Create `app/services/news_relevance_judgment_client.py`:

```python
"""ROB-506 — outbound client for the external news-relevance judgment boundary.

POSTs a pending batch to a Hermes-compatible webhook. Two supported reply
shapes (single contract, minimal coupling):

* 2xx with ``{"judgments": [...]}`` — synchronous judge endpoint; the
  worker applies them via ``symbol_news_store.apply_judgment``.
* 2xx without ``judgments`` — fire-and-forget dispatch; the Hermes session
  judges asynchronously and writes back through the existing token-authed
  ``/trading/api/news-relevance/ingest/bulk`` route (ROB-491).

No in-process LLM, no OpenRouter. Token values are never logged and never
appear in result objects.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx
from pydantic import ValidationError

from app.core.config import settings
from app.schemas.news_relevance import NewsRelevanceJudgment

logger = logging.getLogger(__name__)

_CONTRACT_NOTE = {
    "inline_response": "optional: reply 2xx {'judgments': [NewsRelevanceJudgment, ...]}",
    "writeback_route": "/trading/api/news-relevance/ingest/bulk",
    "criteria_runbook": "docs/runbooks/news-relevance-judgment.md",
}


@dataclass(frozen=True)
class JudgmentClientResult:
    status: Literal["judged", "dispatched", "failed", "skipped"]
    judgments: list[NewsRelevanceJudgment] = field(default_factory=list)
    http_status: int | None = None
    reason: str | None = None
    invalid_count: int = 0


class NewsRelevanceJudgmentClient:
    """httpx wrapper mirroring ``HermesNotificationClient`` (ROB-265)."""

    def __init__(
        self,
        webhook_url: str | None = None,
        token: str | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        self._webhook_url = (
            webhook_url
            if webhook_url is not None
            else settings.NEWS_RELEVANCE_JUDGMENT_WEBHOOK_URL
        )
        self._token = (
            token if token is not None else settings.NEWS_RELEVANCE_JUDGMENT_TOKEN
        )
        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=timeout_seconds or settings.NEWS_RELEVANCE_JUDGMENT_TIMEOUT_S,
        )

    async def request_judgments(
        self,
        *,
        market: str,
        symbol: str | None,
        pending: list[dict[str, Any]],
    ) -> JudgmentClientResult:
        if not self._webhook_url:
            return JudgmentClientResult(
                status="skipped", reason="webhook_url_not_configured"
            )

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        payload = {
            "kind": "news_relevance_judgment_request",
            "market": market,
            "symbol": symbol,
            "pending": pending,
            "contract": _CONTRACT_NOTE,
        }

        try:
            response = await self._client.post(
                self._webhook_url, json=payload, headers=headers
            )
        except httpx.HTTPError as exc:
            logger.warning(
                "news-relevance judgment request raised: market=%s symbol=%s "
                "error=%s",
                market,
                symbol,
                type(exc).__name__,
            )
            return JudgmentClientResult(status="failed", reason="request_failed")

        if not (200 <= response.status_code < 300):
            logger.warning(
                "news-relevance judgment non-2xx: market=%s symbol=%s "
                "http_status=%s",
                market,
                symbol,
                response.status_code,
            )
            return JudgmentClientResult(
                status="failed",
                http_status=response.status_code,
                reason=f"http_{response.status_code}",
            )

        try:
            body = response.json()
        except ValueError:
            body = None
        raw_judgments = (
            body.get("judgments") if isinstance(body, dict) else None
        )
        if not isinstance(raw_judgments, list):
            return JudgmentClientResult(
                status="dispatched", http_status=response.status_code
            )

        judgments: list[NewsRelevanceJudgment] = []
        invalid = 0
        for item in raw_judgments:
            try:
                judgments.append(NewsRelevanceJudgment.model_validate(item))
            except ValidationError:
                invalid += 1
        if invalid:
            logger.warning(
                "news-relevance judgment response had invalid items: "
                "market=%s symbol=%s invalid=%s",
                market,
                symbol,
                invalid,
            )
        return JudgmentClientResult(
            status="judged",
            judgments=judgments,
            http_status=response.status_code,
            invalid_count=invalid,
        )

    async def close(self) -> None:
        await self._client.aclose()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/services/test_news_relevance_judgment_client.py -v`
Expected: 7 PASS

- [ ] **Step 5: Commit**

```bash
git add app/services/news_relevance_judgment_client.py tests/services/test_news_relevance_judgment_client.py
git commit -m "feat(ROB-506): NewsRelevanceJudgmentClient external judgment boundary"
```

---

### Task 4: Job orchestration — `run_news_relevance_judgment`

**Files:**
- Create: `app/jobs/news_relevance_judgment.py`
- Test: `tests/jobs/test_news_relevance_judgment.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/jobs/test_news_relevance_judgment.py`:

```python
"""ROB-506 — judgment job orchestration. DB는 db_session fixture(통합) 사용."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.jobs.news_relevance_judgment import run_news_relevance_judgment
from app.models.symbol_news_relevance import SymbolNewsRelevance
from app.schemas.news_relevance import NewsRelevanceJudgment
from app.services import symbol_news_store
from app.services.news_relevance_judgment_client import JudgmentClientResult
from app.services.symbol_news_store import FeedArticleInput


class _SessionFactory:
    """job의 session_factory 계약(async context manager 반환)을 충족."""

    def __init__(self, session) -> None:
        self._session = session

    def __call__(self):
        return self  # job은 `async with session_factory() as db:` 로 사용

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc) -> bool:
        return False


class _FakeClient:
    def __init__(self, result: JudgmentClientResult) -> None:
        self.result = result
        self.calls: list[dict] = []

    async def request_judgments(self, *, market, symbol, pending):
        self.calls.append({"market": market, "symbol": symbol, "pending": pending})
        return self.result


def _judgment(article_id: int, symbol: str, *, relevance="high", relationship="direct"):
    return NewsRelevanceJudgment(
        article_id=article_id,
        market="kr",
        symbol=symbol,
        relationship=relationship,
        relevance=relevance,
        price_relevance="catalyst" if relevance == "high" else "none",
        score=0.9,
        reason="테스트 판정",
        judged_by="hermes",
    )


async def _seed_pending(db, symbol: str, n: int = 1) -> list[int]:
    items = [
        FeedArticleInput(
            url=f"https://x/rob506-{symbol}-{i}-{uuid.uuid4()}",
            title=f"{symbol} 기사 {i}",
            source="매일경제",
            published_at=datetime(2026, 6, 10, 9, 0, tzinfo=UTC),
        )
        for i in range(n)
    ]
    await symbol_news_store.upsert_kr_feed_articles(db, symbol, items)
    rows = await symbol_news_store.list_pending(db, "kr", 50, symbol=symbol)
    return [row["article_id"] for row in rows]


async def _statuses(db, symbol: str) -> dict[int, str]:
    rows = (
        (
            await db.execute(
                select(SymbolNewsRelevance).where(
                    SymbolNewsRelevance.symbol == symbol,
                    SymbolNewsRelevance.market == "kr",
                )
            )
        )
        .scalars()
        .all()
    )
    return {row.article_id: row.status for row in rows}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_no_pending_is_noop(db_session) -> None:
    symbol = f"S-{uuid.uuid4()}"[:20]
    client = _FakeClient(JudgmentClientResult(status="judged"))
    summary = await run_news_relevance_judgment(
        market="kr",
        symbol=symbol,
        dry_run=False,
        client=client,
        session_factory=_SessionFactory(db_session),
    )
    assert summary["status"] == "no_pending"
    assert summary["fetched_pending"] == 0
    assert client.calls == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_dry_run_fetches_but_never_calls_client_or_writes(db_session) -> None:
    symbol = f"S-{uuid.uuid4()}"[:20]
    ids = await _seed_pending(db_session, symbol, n=2)
    client = _FakeClient(JudgmentClientResult(status="judged"))
    summary = await run_news_relevance_judgment(
        market="kr",
        symbol=symbol,
        dry_run=True,
        client=client,
        session_factory=_SessionFactory(db_session),
    )
    assert summary["status"] == "dry_run"
    assert summary["fetched_pending"] == 2
    assert client.calls == []
    statuses = await _statuses(db_session, symbol)
    assert all(statuses[i] == "pending" for i in ids)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_happy_path_applies_judgments_with_server_derived_status(
    db_session,
) -> None:
    symbol = f"S-{uuid.uuid4()}"[:20]
    ids = await _seed_pending(db_session, symbol, n=2)
    client = _FakeClient(
        JudgmentClientResult(
            status="judged",
            judgments=[
                _judgment(ids[0], symbol, relevance="high"),  # → confirmed
                _judgment(
                    ids[1], symbol, relevance="low", relationship="incidental"
                ),  # → excluded (relevance=low 서버 규칙)
            ],
        )
    )
    summary = await run_news_relevance_judgment(
        market="kr",
        symbol=symbol,
        dry_run=False,
        client=client,
        session_factory=_SessionFactory(db_session),
    )
    assert summary["status"] == "judged"
    assert summary["applied_confirmed"] == 1
    assert summary["applied_excluded"] == 1
    statuses = await _statuses(db_session, symbol)
    assert statuses[ids[0]] == "confirmed"
    assert statuses[ids[1]] == "excluded"
    assert len(client.calls) == 1
    assert client.calls[0]["pending"][0]["article_id"] in ids


@pytest.mark.integration
@pytest.mark.asyncio
async def test_client_failure_keeps_rows_pending(db_session) -> None:
    symbol = f"S-{uuid.uuid4()}"[:20]
    ids = await _seed_pending(db_session, symbol, n=1)
    client = _FakeClient(
        JudgmentClientResult(status="failed", http_status=503, reason="http_503")
    )
    summary = await run_news_relevance_judgment(
        market="kr",
        symbol=symbol,
        dry_run=False,
        client=client,
        session_factory=_SessionFactory(db_session),
    )
    assert summary["status"] == "failed"
    assert summary["applied_confirmed"] == 0
    statuses = await _statuses(db_session, symbol)
    assert statuses[ids[0]] == "pending"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_dispatched_keeps_rows_pending(db_session) -> None:
    symbol = f"S-{uuid.uuid4()}"[:20]
    ids = await _seed_pending(db_session, symbol, n=1)
    client = _FakeClient(JudgmentClientResult(status="dispatched", http_status=202))
    summary = await run_news_relevance_judgment(
        market="kr",
        symbol=symbol,
        dry_run=False,
        client=client,
        session_factory=_SessionFactory(db_session),
    )
    assert summary["status"] == "dispatched"
    statuses = await _statuses(db_session, symbol)
    assert statuses[ids[0]] == "pending"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unrequested_judgment_is_skipped(db_session) -> None:
    """요청한 pending batch 밖의 judgment는 적용하지 않는다 (overreach 방지)."""
    symbol = f"S-{uuid.uuid4()}"[:20]
    other_symbol = f"S-{uuid.uuid4()}"[:20]
    ids = await _seed_pending(db_session, symbol, n=1)
    other_ids = await _seed_pending(db_session, other_symbol, n=1)
    client = _FakeClient(
        JudgmentClientResult(
            status="judged",
            judgments=[
                _judgment(ids[0], symbol),
                _judgment(other_ids[0], other_symbol),  # batch 밖
            ],
        )
    )
    summary = await run_news_relevance_judgment(
        market="kr",
        symbol=symbol,
        dry_run=False,
        client=client,
        session_factory=_SessionFactory(db_session),
    )
    assert summary["skipped_unrequested"] == 1
    statuses_other = await _statuses(db_session, other_symbol)
    assert statuses_other[other_ids[0]] == "pending"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rejudgment_is_idempotent(db_session) -> None:
    symbol = f"S-{uuid.uuid4()}"[:20]
    ids = await _seed_pending(db_session, symbol, n=1)
    judgment = _judgment(ids[0], symbol, relevance="high")

    async def _run():
        # 두 번째 run에서는 row가 이미 confirmed라 pending 조회에 안 잡힘 —
        # apply_judgment 자체의 멱등성은 ingest route 계약(ROB-491)이 보장.
        return await run_news_relevance_judgment(
            market="kr",
            symbol=symbol,
            dry_run=False,
            client=_FakeClient(
                JudgmentClientResult(status="judged", judgments=[judgment])
            ),
            session_factory=_SessionFactory(db_session),
        )

    first = await _run()
    second = await _run()
    assert first["status"] == "judged"
    assert second["status"] == "no_pending"
    statuses = await _statuses(db_session, symbol)
    assert statuses[ids[0]] == "confirmed"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_article_ids_filter_limits_batch(db_session) -> None:
    symbol = f"S-{uuid.uuid4()}"[:20]
    ids = await _seed_pending(db_session, symbol, n=3)
    client = _FakeClient(JudgmentClientResult(status="dispatched", http_status=202))
    summary = await run_news_relevance_judgment(
        market="kr",
        symbol=symbol,
        article_ids=[ids[0]],
        dry_run=False,
        client=client,
        session_factory=_SessionFactory(db_session),
    )
    assert summary["fetched_pending"] == 1
    assert len(client.calls[0]["pending"]) == 1
    assert client.calls[0]["pending"][0]["article_id"] == ids[0]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/jobs/test_news_relevance_judgment.py -v`
Expected: FAIL — `ModuleNotFoundError: app.jobs.news_relevance_judgment`

- [ ] **Step 3: Implement the job**

Create `app/jobs/news_relevance_judgment.py`:

```python
"""ROB-506 — orchestration for the async news-relevance judgment worker.

Pure service-layer flow (no ``@broker.task`` here — that lives in
``app/tasks/news_relevance_judgment_tasks.py``):

    list_pending → judgment client → apply via symbol_news_store

Safety invariants:
* never writes ``status`` directly — ``apply_judgment`` derives it
  server-side (``relationship=unrelated`` or ``relevance=low`` → excluded);
* client failure / dispatch / invalid payload leaves rows ``pending``;
* judgments outside the requested batch are skipped (counted), so a
  confused external endpoint cannot touch arbitrary rows;
* no broker/order/watch surface, no secrets in the returned summary.
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.services import symbol_news_store
from app.services.news_relevance_judgment_client import (
    NewsRelevanceJudgmentClient,
)

logger = logging.getLogger(__name__)

_MAX_BATCH = 200  # ingest route hard cap (NewsRelevanceIngestRequest)


async def run_news_relevance_judgment(
    *,
    market: str = "kr",
    symbol: str | None = None,
    article_ids: list[int] | None = None,
    limit: int | None = None,
    dry_run: bool = True,
    client: Any | None = None,
    session_factory: Any | None = None,
) -> dict[str, Any]:
    session_factory = session_factory or AsyncSessionLocal
    batch_limit = limit or settings.NEWS_RELEVANCE_JUDGMENT_BATCH_LIMIT
    batch_limit = max(1, min(int(batch_limit), _MAX_BATCH))

    summary: dict[str, Any] = {
        "status": "no_pending",
        "market": market,
        "symbol": symbol,
        "dry_run": dry_run,
        "client_mode": "webhook",
        "fetched_pending": 0,
        "judged": 0,
        "applied_confirmed": 0,
        "applied_excluded": 0,
        "skipped_unrequested": 0,
        "invalid_judgments": 0,
        "link_not_found": 0,
        "http_status": None,
        "reason": None,
    }

    async with session_factory() as db:
        pending = await symbol_news_store.list_pending(
            db, market, batch_limit, symbol=symbol
        )
        if article_ids is not None:
            wanted = set(article_ids)
            pending = [row for row in pending if row["article_id"] in wanted]
        summary["fetched_pending"] = len(pending)
        if not pending:
            return summary

        if dry_run:
            summary["status"] = "dry_run"
            return summary

        owns_client = client is None
        if owns_client:
            client = NewsRelevanceJudgmentClient()
        try:
            result = await client.request_judgments(
                market=market, symbol=symbol, pending=pending
            )
        finally:
            if owns_client:
                await client.close()

        summary["status"] = result.status
        summary["http_status"] = result.http_status
        summary["reason"] = result.reason
        summary["invalid_judgments"] = result.invalid_count
        if result.status != "judged":
            # failed / dispatched / skipped — rows stay pending by design.
            return summary

        requested = {(row["article_id"], market, row["symbol"]) for row in pending}
        for judgment in result.judgments:
            key = (judgment.article_id, judgment.market, judgment.symbol)
            if key not in requested:
                summary["skipped_unrequested"] += 1
                continue
            status = await symbol_news_store.apply_judgment(
                db,
                article_id=judgment.article_id,
                market=judgment.market,
                symbol=judgment.symbol,
                relationship=judgment.relationship,
                relevance=judgment.relevance,
                price_relevance=judgment.price_relevance,
                score=judgment.score,
                reason=judgment.reason,
                judged_by=judgment.judged_by,
            )
            if status is None:
                summary["link_not_found"] += 1
            else:
                summary["judged"] += 1
                if status == "excluded":
                    summary["applied_excluded"] += 1
                else:
                    summary["applied_confirmed"] += 1
        await db.commit()

    logger.info(
        "news_relevance judgment run: market=%s symbol=%s status=%s "
        "fetched=%s judged=%s confirmed=%s excluded=%s skipped=%s invalid=%s",
        market,
        symbol,
        summary["status"],
        summary["fetched_pending"],
        summary["judged"],
        summary["applied_confirmed"],
        summary["applied_excluded"],
        summary["skipped_unrequested"],
        summary["invalid_judgments"],
    )
    return summary
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/jobs/test_news_relevance_judgment.py -v`
Expected: 8 PASS

- [ ] **Step 5: Commit**

```bash
git add app/jobs/news_relevance_judgment.py tests/jobs/test_news_relevance_judgment.py
git commit -m "feat(ROB-506): news-relevance judgment job orchestration"
```

---

### Task 5: TaskIQ task `news_relevance.judge_pending` + 등록

**Files:**
- Create: `app/tasks/news_relevance_judgment_tasks.py`
- Modify: `app/tasks/__init__.py` (import 목록 + `TASKIQ_TASK_MODULES` 둘 다)
- Test: `tests/tasks/test_news_relevance_judgment_tasks.py` (Task 2 파일에 추가)

- [ ] **Step 1: Write the failing tests**

`tests/tasks/test_news_relevance_judgment_tasks.py`에 추가:

```python
from unittest.mock import AsyncMock

from app.core.config import settings


@pytest.mark.unit
def test_task_module_is_registered() -> None:
    from app.tasks import TASKIQ_TASK_MODULES, news_relevance_judgment_tasks

    assert news_relevance_judgment_tasks in TASKIQ_TASK_MODULES
    assert (
        news_relevance_judgment_tasks.news_relevance_judge_pending.task_name
        == "news_relevance.judge_pending"
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_commit_mode_refused_while_flag_off(monkeypatch) -> None:
    from app.tasks.news_relevance_judgment_tasks import (
        news_relevance_judge_pending,
    )

    monkeypatch.setattr(
        settings, "NEWS_RELEVANCE_ASYNC_JUDGMENT_ENABLED", False
    )
    result = await news_relevance_judge_pending(market="kr", dry_run=False)
    assert result["status"] == "disabled"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dry_run_allowed_while_flag_off(monkeypatch) -> None:
    import app.tasks.news_relevance_judgment_tasks as task_module

    monkeypatch.setattr(
        settings, "NEWS_RELEVANCE_ASYNC_JUDGMENT_ENABLED", False
    )
    fake = AsyncMock(return_value={"status": "dry_run"})
    monkeypatch.setattr(task_module, "run_news_relevance_judgment", fake)
    result = await task_module.news_relevance_judge_pending(
        market="kr", symbol="035420", dry_run=True
    )
    assert result == {"status": "dry_run"}
    fake.assert_awaited_once_with(
        market="kr", symbol="035420", article_ids=None, limit=None, dry_run=True
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_commit_mode_runs_when_flag_on(monkeypatch) -> None:
    import app.tasks.news_relevance_judgment_tasks as task_module

    monkeypatch.setattr(settings, "NEWS_RELEVANCE_ASYNC_JUDGMENT_ENABLED", True)
    fake = AsyncMock(return_value={"status": "judged"})
    monkeypatch.setattr(task_module, "run_news_relevance_judgment", fake)
    result = await task_module.news_relevance_judge_pending(
        market="kr", symbol="035420", dry_run=False
    )
    assert result == {"status": "judged"}
    fake.assert_awaited_once_with(
        market="kr", symbol="035420", article_ids=None, limit=None, dry_run=False
    )
```

(참고: `@broker.task`로 감싼 taskiq `AsyncTaskiqDecoratedTask`는 `await task(...)`로 원함수를 직접 호출 가능. 만약 직접 호출이 안 되면 `news_relevance_judge_pending.original_func`를 사용하도록 테스트를 조정한다 — `tests/tasks/test_kis_live_reconcile_tasks.py`의 기존 호출 방식을 따른다.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/tasks/test_news_relevance_judgment_tasks.py -v`
Expected: 신규 4건 FAIL — `ImportError`

- [ ] **Step 3: Implement task + registration**

Create `app/tasks/news_relevance_judgment_tasks.py`:

```python
"""ROB-506 — TaskIQ task for async news-relevance judgment.

NO recurring schedule (TaskIQ/cron/Prefect activation is operator-owned and
out of repo). Enqueued from the ``get_news`` KR persist path when
``NEWS_RELEVANCE_ASYNC_JUDGMENT_ENABLED`` is on, or invoked manually for
smoke. Commit mode is refused while the flag is off; ``dry_run=True`` (the
default) is always allowed and performs no client call / no writes.
"""

from __future__ import annotations

import logging

from app.core.config import settings
from app.core.taskiq_broker import broker
from app.jobs.news_relevance_judgment import run_news_relevance_judgment

logger = logging.getLogger(__name__)


@broker.task(task_name="news_relevance.judge_pending")
async def news_relevance_judge_pending(
    market: str = "kr",
    symbol: str | None = None,
    article_ids: list[int] | None = None,
    limit: int | None = None,
    dry_run: bool = True,
) -> dict:
    if not dry_run and not settings.NEWS_RELEVANCE_ASYNC_JUDGMENT_ENABLED:
        return {
            "status": "disabled",
            "reason": "NEWS_RELEVANCE_ASYNC_JUDGMENT_ENABLED is off",
            "market": market,
            "symbol": symbol,
        }
    return await run_news_relevance_judgment(
        market=market,
        symbol=symbol,
        article_ids=article_ids,
        limit=limit,
        dry_run=dry_run,
    )
```

`app/tasks/__init__.py` — import 블록(알파벳 순서 유지, `mock_roundtrip_journal_tasks` 다음)과 `TASKIQ_TASK_MODULES` 튜플 둘 다에 `news_relevance_judgment_tasks` 추가:

```python
from app.tasks import (
    ...
    mock_roundtrip_journal_tasks,
    news_relevance_judgment_tasks,
    pending_orders,
    ...
)

TASKIQ_TASK_MODULES = (
    ...
    mock_roundtrip_journal_tasks,
    news_relevance_judgment_tasks,
    ...
)
```

- [ ] **Step 4: Run tests + registration smoke**

Run: `uv run pytest tests/tasks/test_news_relevance_judgment_tasks.py -v && uv run python -c "import app.tasks; import app.main"`
Expected: 5 PASS + import 에러 없음 (TaskIQ registration/profile smoke)

- [ ] **Step 5: Commit**

```bash
git add app/tasks/news_relevance_judgment_tasks.py app/tasks/__init__.py tests/tasks/test_news_relevance_judgment_tasks.py
git commit -m "feat(ROB-506): news_relevance.judge_pending TaskIQ task (default-off gate)"
```

---

### Task 6: `get_news` 저장 경로 fail-open enqueue

**Files:**
- Modify: `app/services/symbol_news_service.py` (`_kr_persist_and_load` + 신규 helper)
- Test: `tests/services/test_symbol_news_service.py`

- [ ] **Step 1: Write the failing tests**

`tests/services/test_symbol_news_service.py` 끝에 추가 (기존 `_patch_store` helper 재사용; 파일 상단 import에 `from unittest.mock import AsyncMock`이 없으면 추가):

```python
def _patch_naver(monkeypatch, items):
    async def fake_fetch(symbol, limit=20):
        return items

    monkeypatch.setattr(
        symbol_news_service.naver_finance, "fetch_news", fake_fetch
    )


_RAW_ITEM = {
    "title": "네이버 신규 투자",
    "url": "https://x/rob506-enqueue",
    "source": "매일경제",
    "datetime": "2026-06-10T09:00:00",
}


def _patch_store_with_insert_count(monkeypatch, *, stored, new_links: int):
    """upsert가 신규 link 수(int)를 반환하는 ROB-506 계약으로 store를 fake."""

    async def upsert(db, symbol, items, **kwargs):
        return new_links

    async def load(db, symbol, market, limit):
        return stored, 0

    monkeypatch.setattr(
        symbol_news_service.symbol_news_store, "upsert_kr_feed_articles", upsert
    )
    monkeypatch.setattr(
        symbol_news_service.symbol_news_store, "load_symbol_news", load
    )
    fake_session = MagicMock()
    fake_cm = MagicMock()
    fake_cm.__aenter__ = AsyncMock(return_value=fake_session)
    fake_cm.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(
        symbol_news_service, "AsyncSessionLocal", MagicMock(return_value=fake_cm)
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_kr_new_pending_enqueues_judgment_when_flag_on(monkeypatch) -> None:
    from app.core.config import settings
    from app.tasks import news_relevance_judgment_tasks

    monkeypatch.setattr(settings, "NEWS_RELEVANCE_ASYNC_JUDGMENT_ENABLED", True)
    _patch_naver(monkeypatch, [_RAW_ITEM])
    _patch_store_with_insert_count(
        monkeypatch,
        stored=[_stored(1, _RAW_ITEM["url"], _RAW_ITEM["title"])],
        new_links=1,
    )
    kiq = AsyncMock()
    monkeypatch.setattr(
        news_relevance_judgment_tasks.news_relevance_judge_pending, "kiq", kiq
    )

    result = await symbol_news_service.fetch_symbol_news("035420", "kr")
    assert result.status == "ok"
    kiq.assert_awaited_once_with(market="kr", symbol="035420", dry_run=False)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_kr_enqueue_failure_is_fail_open(monkeypatch) -> None:
    from app.core.config import settings
    from app.tasks import news_relevance_judgment_tasks

    monkeypatch.setattr(settings, "NEWS_RELEVANCE_ASYNC_JUDGMENT_ENABLED", True)
    _patch_naver(monkeypatch, [_RAW_ITEM])
    _patch_store_with_insert_count(
        monkeypatch,
        stored=[_stored(1, _RAW_ITEM["url"], _RAW_ITEM["title"])],
        new_links=1,
    )
    kiq = AsyncMock(side_effect=RuntimeError("redis down"))
    monkeypatch.setattr(
        news_relevance_judgment_tasks.news_relevance_judge_pending, "kiq", kiq
    )

    result = await symbol_news_service.fetch_symbol_news("035420", "kr")
    assert result.status == "ok"  # enqueue 실패가 get_news를 죽이지 않음
    assert result.returned_count == 1
    kiq.assert_awaited_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_kr_no_enqueue_when_flag_off(monkeypatch) -> None:
    from app.tasks import news_relevance_judgment_tasks

    # flag는 기본 off — monkeypatch 불필요하지만 명시
    _patch_naver(monkeypatch, [_RAW_ITEM])
    _patch_store_with_insert_count(
        monkeypatch,
        stored=[_stored(1, _RAW_ITEM["url"], _RAW_ITEM["title"])],
        new_links=1,
    )
    kiq = AsyncMock()
    monkeypatch.setattr(
        news_relevance_judgment_tasks.news_relevance_judge_pending, "kiq", kiq
    )

    result = await symbol_news_service.fetch_symbol_news("035420", "kr")
    assert result.status == "ok"
    kiq.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_kr_no_enqueue_when_no_new_pending(monkeypatch) -> None:
    from app.core.config import settings
    from app.tasks import news_relevance_judgment_tasks

    monkeypatch.setattr(settings, "NEWS_RELEVANCE_ASYNC_JUDGMENT_ENABLED", True)
    _patch_naver(monkeypatch, [_RAW_ITEM])
    _patch_store_with_insert_count(
        monkeypatch,
        stored=[_stored(1, _RAW_ITEM["url"], _RAW_ITEM["title"])],
        new_links=0,  # 전부 기존 link — 신규 pending 없음
    )
    kiq = AsyncMock()
    monkeypatch.setattr(
        news_relevance_judgment_tasks.news_relevance_judge_pending, "kiq", kiq
    )

    result = await symbol_news_service.fetch_symbol_news("035420", "kr")
    assert result.status == "ok"
    kiq.assert_not_awaited()
```

(기존 파일이 `MagicMock`/`AsyncMock`을 어떻게 import하는지 확인해 중복 import을 피한다. `_stored` helper는 기존 것 재사용.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/services/test_symbol_news_service.py -v -k "enqueue or no_new_pending or flag_off"`
Expected: 신규 4건 FAIL (kiq 미호출 / AttributeError)

- [ ] **Step 3: Implement enqueue**

`app/services/symbol_news_service.py`:

상단 import에 추가:

```python
from app.core.config import settings
```

`_kr_persist_and_load` 위에 helper 추가:

```python
async def _maybe_enqueue_judgment(symbol: str, new_pending: int) -> None:
    """ROB-506: fire-and-forget judgment enqueue. Never raises into get_news."""
    if new_pending <= 0:
        return
    if not settings.NEWS_RELEVANCE_ASYNC_JUDGMENT_ENABLED:
        return
    try:
        # Lazy import — keeps the taskiq broker out of plain MCP import paths.
        from app.tasks.news_relevance_judgment_tasks import (
            news_relevance_judge_pending,
        )

        await news_relevance_judge_pending.kiq(
            market="kr", symbol=symbol, dry_run=False
        )
    except Exception as exc:  # noqa: BLE001 — enqueue must be fail-open
        logger.warning(
            "symbol_news_service: judgment enqueue failed (fail-open): "
            "symbol=%s err=%s",
            symbol,
            exc,
        )
```

`_kr_persist_and_load` 수정 — upsert 반환값 캡처 + 성공 경로에서 enqueue:

```python
async def _kr_persist_and_load(
    symbol: str,
    fetched: list[SymbolNewsArticle],
    limit: int,
    fetched_at: datetime,
) -> tuple[list[SymbolNewsArticle], int] | None:
    """Persist this window then serve canonical DB state. None → DB unavailable."""
    inserted: Any = 0
    try:
        async with AsyncSessionLocal() as db:
            if fetched:
                inserted = await symbol_news_store.upsert_kr_feed_articles(
                    db,
                    symbol,
                    [
                        FeedArticleInput(
                            url=a.canonical_url,
                            title=a.title,
                            source=a.source_name,
                            published_at=a.published_at,
                        )
                        for a in fetched
                    ],
                )
            stored, excluded_count = await symbol_news_store.load_symbol_news(
                db, symbol, "kr", limit
            )
    except Exception as exc:  # noqa: BLE001 — cache layer must not kill the tool
        logger.warning(
            "symbol_news_service: store unavailable, degrading: symbol=%s err=%s",
            symbol,
            exc,
        )
        return None
    # isinstance guard: 구형 fake가 None을 돌려줘도 enqueue 판단만 0으로 처리
    new_pending = inserted if isinstance(inserted, int) else 0
    await _maybe_enqueue_judgment(symbol, new_pending)
    raw_by_url = {
        a.canonical_url: a.provider_metadata.get("source_item") for a in fetched
    }
    articles = [
        _stored_to_article(row, symbol, fetched_at, raw_by_url) for row in stored
    ]
    return articles, excluded_count
```

- [ ] **Step 4: Run the service test file**

Run: `uv run pytest tests/services/test_symbol_news_service.py -v`
Expected: 전부 PASS (기존 + 신규 4)

- [ ] **Step 5: Commit**

```bash
git add app/services/symbol_news_service.py tests/services/test_symbol_news_service.py
git commit -m "feat(ROB-506): fail-open judgment enqueue from get_news KR persist path"
```

---

### Task 7: 런북 + env.example 문서화

**Files:**
- Modify: `docs/runbooks/news-relevance-judgment.md`
- Modify: `env.example` (NEWS_RELEVANCE 항목이 이미 있으면 그 옆, 없으면 추가)

- [ ] **Step 1: 런북에 TaskIQ worker 섹션 추가**

`docs/runbooks/news-relevance-judgment.md`의 "## 트러블슈팅" 앞에 삽입:

```markdown
## TaskIQ 비동기 판정 worker (ROB-506)

`get_news`(KR)가 새 pending link를 만들면 `news_relevance.judge_pending`
task를 enqueue한다 (fail-open — enqueue 실패해도 get_news는 성공). Task는
pending batch를 외부 Hermes-호환 judgment webhook에 POST하고, 응답에
inline `judgments`가 있으면 기존 ingest 규칙(서버 status 파생)으로
적용한다. 응답이 dispatch-only(2xx, judgments 없음)면 외부 세션이 위의
ingest/bulk 경로로 write-back할 때까지 pending이 유지된다. 실패/검증 실패
시에도 pending 유지 — excluded로 오판정되는 경로는 없다.

### 활성화 (default-off)

| env | default | 의미 |
| --- | --- | --- |
| `NEWS_RELEVANCE_ASYNC_JUDGMENT_ENABLED` | `false` | off면 enqueue 없음 + commit-mode task는 `disabled` 반환 |
| `NEWS_RELEVANCE_JUDGMENT_WEBHOOK_URL` | `""` | 외부 judgment endpoint. 미설정 시 client `skipped` |
| `NEWS_RELEVANCE_JUDGMENT_TOKEN` | `""` | outbound Bearer 토큰 (로그/결과에 출력 안 됨) |
| `NEWS_RELEVANCE_JUDGMENT_TIMEOUT_S` | `120` | webhook 호출 timeout |
| `NEWS_RELEVANCE_JUDGMENT_BATCH_LIMIT` | `50` | run당 pending batch 상한 (하드캡 200) |

`HERMES_WEBHOOK_URL`/`HERMES_TOKEN`(ROB-265 알림)과
`NEWS_RELEVANCE_INGEST_TOKEN`(inbound write-back 인증)과는 별개 설정이다.
inline 응답을 안 쓰는 Hermes 구성이라면 write-back을 위해 기존
`NEWS_RELEVANCE_INGEST_TOKEN`도 함께 설정되어 있어야 한다.

### 수동 smoke (worker 로컬 실행)

```bash
# 1. 의존 서비스 + worker
docker compose up -d            # postgres, redis
make taskiq-worker              # uv run taskiq worker app.core.taskiq_broker:broker app.tasks

# 2. dry-run (flag off에서도 허용 — client 호출/DB write 없음)
uv run python - <<'PY'
import asyncio
from app.jobs.news_relevance_judgment import run_news_relevance_judgment

print(asyncio.run(run_news_relevance_judgment(market="kr", dry_run=True)))
PY
# 기대: {"status": "dry_run" | "no_pending", "fetched_pending": N, ...}

# 3. commit-mode (operator gate: flag + webhook 설정 후)
#    get_news(MCP)로 pending을 만든 뒤 worker 로그에서
#    "news_relevance judgment run: ... status=judged|dispatched" 확인.

# 4. 검증 — 기존 §Job 절차 4와 동일: get_news 재호출로
#    excluded_count 증가 / confirmed relevance 블록 확인.
```

### Task result 필드

`fetched_pending`, `judged`, `applied_confirmed`, `applied_excluded`,
`skipped_unrequested`, `invalid_judgments`, `link_not_found`,
`client_mode`, `dry_run`, `http_status`, `reason`. 토큰 값은 어디에도
포함되지 않는다.
```

또한 문서 상단(4행) "스케줄러(TaskIQ/cron/Prefect) 연결 없음" 문구를 다음으로 갱신:

```markdown
recurring 스케줄러(cron/Prefect) 연결 없음 — ROB-506의 TaskIQ enqueue는
get_news 호출 시에만 발생하며 default-off다. production 활성화는 별도
operator gate.
```

- [ ] **Step 2: env.example 갱신**

`grep -n "NEWS_RELEVANCE" env.example`로 위치 확인 후, 해당 블록(없으면 파일 끝)에 추가:

```bash
# ROB-506 — async news-relevance judgment worker (default off)
NEWS_RELEVANCE_ASYNC_JUDGMENT_ENABLED=false
NEWS_RELEVANCE_JUDGMENT_WEBHOOK_URL=
NEWS_RELEVANCE_JUDGMENT_TOKEN=
```

- [ ] **Step 3: Commit**

```bash
git add docs/runbooks/news-relevance-judgment.md env.example
git commit -m "docs(ROB-506): TaskIQ async judgment worker runbook + env.example"
```

---

### Task 8: Focused regression + lint/typecheck + PR

**Files:** 없음 (검증/머지 준비)

- [ ] **Step 1: Focused test sweep (이슈 권장 명령 + 신규 client 테스트)**

```bash
uv run pytest \
  tests/services/test_symbol_news_service.py \
  tests/services/test_symbol_news_store.py \
  tests/services/test_news_relevance_judgment_client.py \
  tests/routers/test_news_relevance_ingest.py \
  tests/routers/test_news_relevance_auth.py \
  tests/tasks/test_news_relevance_judgment_tasks.py \
  tests/jobs/test_news_relevance_judgment.py \
  -v
```

Expected: 전부 PASS. (`test_news_relevance_ingest.py`는 무수정 통과 = ROB-491 ingest contract regression 확인.)

- [ ] **Step 2: Lint/format/typecheck**

```bash
uv run ruff format app/ tests/ && uv run ruff check app/ tests/ --fix
make typecheck
```

Expected: clean. (교훈 메모리: CI lint는 `app/` + `tests/` 둘 다 본다. `ty check app/`도 로컬에서 실행.)

- [ ] **Step 3: 등록/import 스모크 재확인**

```bash
uv run python -c "import app.tasks, app.main; from app.tasks import TASKIQ_TASK_MODULES; print(len(TASKIQ_TASK_MODULES))"
```

Expected: import 에러 없음.

- [ ] **Step 4: 잔여 변경 커밋 + push + PR**

```bash
git push -u origin rob-506
gh pr create --base main --title "feat(ROB-506): TaskIQ 기반 get_news relevance 판정 worker" --body "..."
```

PR 본문 필수 기재 (이슈 acceptance):
- **Migration: 없음** (lease/backoff 컬럼은 운영 후 필요 시 expansion-only 후속)
- **Default-off**: `NEWS_RELEVANCE_ASYNC_JUDGMENT_ENABLED=false` — enqueue 없음 + commit task `disabled`
- **Judgment boundary**: Hermes-호환 webhook (inline judgments 또는 dispatch+기존 ingest write-back). **direct OpenRouter는 non-goal로 미구현** (별도 설계 이슈로 분리)
- 실행한 test 명령 전체 (Step 1)
- **Operator activation 잔여 단계**: ① 배포 ② Hermes judgment hook 구성 ③ env 3종 설정 ④ flag flip ⑤ get_news 스모크로 pending→confirmed/excluded 확인

---

## Self-Review 결과

- **Spec coverage**: 이슈 범위 §1(enqueue: Task 6) §2(task/job: Task 4·5) §3(client boundary: Task 3, OpenRouter non-goal은 PR 본문) §4(DB: 기존 상태 재사용, migration 0, status 미직접쓰기=`apply_judgment` 경유) §5(observability: summary 필드 + 런북 Task 7) — 전부 매핑됨.
- **Acceptance criteria ↔ tests**: flag off 기존 동작 유지(Task 6 test 3), enqueue 시도(test 1), fail-open(test 2), batch 조회+client 호출(Task 4 happy), 서버 status 파생(Task 4 happy의 low→excluded), 실패 시 pending 유지(Task 4 failure/dispatched), idempotent(Task 4 rejudgment + 기존 ingest 계약), secret hygiene(Task 3 token test), ingest contract regression(Task 8 무수정 통과), registration smoke(Task 5 + Task 8).
- **Type consistency**: `upsert_kr_feed_articles → int`(Task 1)를 Task 6이 소비; `JudgmentClientResult`(Task 3)를 Task 4 fake가 동일 시그니처로 사용; `run_news_relevance_judgment` keyword-only 시그니처를 Task 5 task가 동일 키워드로 전달 — 일치.
- **알려진 조정 포인트**: taskiq decorated task의 직접 await 호출 가능 여부(Task 5 참고 주석), `Settings` 클래스명(Task 2 주석), 기존 테스트 파일의 mock import 중복(Task 6 주석) — 구현 중 확인하도록 명시함.

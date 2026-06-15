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


def _judgment(
    article_id: int,
    symbol: str,
    *,
    market: str = "kr",
    relevance: str = "high",
    relationship: str = "direct",
):
    return NewsRelevanceJudgment(
        article_id=article_id,
        market=market,
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


async def _seed_pending_for_market(
    db,
    *,
    market: str,
    symbol: str,
    n: int = 1,
) -> list[int]:
    feed_source = (
        symbol_news_store.FINNHUB_COMPANY_FEED_SOURCE
        if market == "us"
        else symbol_news_store.FINNHUB_GENERAL_FEED_SOURCE
    )
    items = [
        FeedArticleInput(
            url=f"https://x/rob579-{market}-{symbol}-{i}-{uuid.uuid4()}",
            title=f"{symbol} Finnhub article {i}",
            source="Reuters",
            published_at=datetime(2026, 6, 10, 9, 0, tzinfo=UTC),
            summary=f"{symbol} summary {i}",
        )
        for i in range(n)
    ]
    await symbol_news_store.upsert_feed_articles(
        db,
        market,
        symbol,
        items,
        feed_source=feed_source,
    )
    rows = await symbol_news_store.list_pending(db, market, 50, symbol=symbol)
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
async def test_us_happy_path_applies_judgments_and_hides_excluded(
    db_session,
) -> None:
    symbol = f"A{uuid.uuid4().hex[:8].upper()}"
    ids = await _seed_pending_for_market(db_session, market="us", symbol=symbol, n=2)
    client = _FakeClient(
        JudgmentClientResult(
            status="judged",
            judgments=[
                _judgment(ids[0], symbol, market="us", relevance="high"),
                _judgment(
                    ids[1],
                    symbol,
                    market="us",
                    relevance="low",
                    relationship="unrelated",
                ),
            ],
        )
    )

    summary = await run_news_relevance_judgment(
        market="us",
        symbol=symbol,
        dry_run=False,
        client=client,
        session_factory=_SessionFactory(db_session),
    )

    assert summary["status"] == "judged"
    assert summary["applied_confirmed"] == 1
    assert summary["applied_excluded"] == 1
    stored, excluded_count = await symbol_news_store.load_symbol_news(
        db_session, symbol, "us", limit=10
    )
    assert excluded_count == 1
    assert [row.relevance["status"] for row in stored] == ["confirmed"]
    assert client.calls[0]["market"] == "us"
    assert all(row["market"] == "us" for row in client.calls[0]["pending"])


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

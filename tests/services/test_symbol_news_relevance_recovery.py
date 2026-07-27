"""Bounded recovery tests for the 2026-07-27 judgment contamination."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.models.news import NewsArticle
from app.models.symbol_news_relevance import SymbolNewsRelevance
from app.services import symbol_news_store


def _fake_link() -> SimpleNamespace:
    return SimpleNamespace(
        id=101,
        article_id=201,
        market="kr",
        symbol="005930",
        status="excluded",
        relationship="unrelated",
        relevance="low",
        price_relevance="none",
        score=0.1,
        reason="오염된 제목 복사 판정",
        judged_by="hermes-news-relevance",
        judged_at=datetime(2026, 7, 26, 23, 7),
        updated_at=datetime(2026, 7, 26, 23, 7),
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_expected_count_mismatch_raises_without_mutation() -> None:
    link = _fake_link()
    original = vars(link).copy()
    result = SimpleNamespace(
        scalars=lambda: SimpleNamespace(all=lambda: [link]),
    )
    db = SimpleNamespace(
        execute=AsyncMock(return_value=result),
        flush=AsyncMock(),
    )

    with pytest.raises(
        symbol_news_store.NewsRelevanceRecoveryCountMismatch,
        match="expected 96 contaminated rows, selected 1",
    ):
        await symbol_news_store.recover_contaminated_news_relevance(
            db,
            expected_count=96,
            execute=True,
            audit_snapshot=(),
        )

    assert vars(link) == original
    db.flush.assert_not_awaited()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_recovery_selects_title_copy_but_excludes_normal_llm_judgment(
    db_session,
) -> None:
    old_updated_at = datetime(2026, 7, 26, 22, 0)
    copied_title = "삼성전자 반도체 투자 확대와 공급망 재편 전망"
    normal_title = "크래프톤과 데이터 분석 산업의 최신 동향 브리핑"
    copied_article = NewsArticle(
        url="https://example.invalid/recovery-title-copy",
        title=copied_title,
        source="테스트언론",
        market="kr",
        is_analyzed=False,
        scraped_at=old_updated_at,
        created_at=old_updated_at,
        updated_at=old_updated_at,
    )
    normal_article = NewsArticle(
        url="https://example.invalid/recovery-normal-llm",
        title=normal_title,
        source="테스트언론",
        market="kr",
        is_analyzed=False,
        scraped_at=old_updated_at,
        created_at=old_updated_at,
        updated_at=old_updated_at,
    )
    db_session.add_all([copied_article, normal_article])
    await db_session.flush()

    copied = SymbolNewsRelevance(
        article_id=copied_article.id,
        market="kr",
        symbol="005930",
        feed_source="naver_item_news",
        first_seen_at=old_updated_at,
        status="excluded",
        relationship="unrelated",
        relevance="low",
        price_relevance="none",
        score=0.1,
        reason=f"{copied_title} - 테스트언론 (관련성 낮음)",
        judged_by="hermes-news-relevance",
        judged_at=datetime(2026, 7, 26, 23, 7),
        hints={"preserve": True},
        created_at=old_updated_at,
        updated_at=old_updated_at,
    )
    normal = SymbolNewsRelevance(
        article_id=normal_article.id,
        market="kr",
        symbol="259960",
        feed_source="naver_item_news",
        first_seen_at=old_updated_at,
        status="confirmed",
        relationship="unrelated",
        relevance="medium",
        price_relevance="background",
        score=0.6,
        reason=(
            "데이터브릭스코리아 대표의 AI 데이터 분석 중요도 발언으로 "
            "크래프톤과 직접적인 관련이 없습니다"
        ),
        judged_by="hermes-news-relevance",
        judged_at=datetime(2026, 7, 27, 0, 21),
        hints={"normal": True},
        created_at=old_updated_at,
        updated_at=old_updated_at,
    )
    db_session.add_all([copied, normal])
    await db_session.flush()

    preview = await symbol_news_store.recover_contaminated_news_relevance(
        db_session,
        expected_count=1,
    )

    assert preview.dry_run is True
    assert [row.id for row in preview.selected] == [copied.id]
    assert normal.id not in {row.id for row in preview.selected}
    assert copied.status == "excluded"
    assert normal.status == "confirmed"

    preserved = {
        "article_id": copied.article_id,
        "market": copied.market,
        "symbol": copied.symbol,
        "feed_source": copied.feed_source,
        "first_seen_at": copied.first_seen_at,
        "hints": copied.hints,
        "created_at": copied.created_at,
    }
    executed = await symbol_news_store.recover_contaminated_news_relevance(
        db_session,
        expected_count=1,
        execute=True,
        audit_snapshot=preview.selected,
    )

    assert executed.updated_count == 1
    assert copied.status == "pending"
    assert copied.relationship is None
    assert copied.relevance is None
    assert copied.price_relevance is None
    assert copied.score is None
    assert copied.reason is None
    assert copied.judged_by is None
    assert copied.judged_at is None
    assert copied.updated_at > old_updated_at
    assert {
        "article_id": copied.article_id,
        "market": copied.market,
        "symbol": copied.symbol,
        "feed_source": copied.feed_source,
        "first_seen_at": copied.first_seen_at,
        "hints": copied.hints,
        "created_at": copied.created_at,
    } == preserved

    assert normal.status == "confirmed"
    assert normal.reason is not None
    assert normal.judged_at == datetime(2026, 7, 27, 0, 21)

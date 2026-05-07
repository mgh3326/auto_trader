from __future__ import annotations

from argparse import Namespace
from datetime import UTC, datetime, timedelta

import pytest

from scripts import news_issue_lab as lab


def _article(
    id: int = 1,
    title: str = "Nasdaq closes at record on AI earnings",
    *,
    source: str = "reuters",
    feed_source: str | None = None,
    summary: str | None = None,
    published_at: str | None = None,
    scraped_at: str | None = None,
) -> lab.Article:
    now = datetime.now(UTC).isoformat()
    return lab.Article(
        id=id,
        title=title,
        summary=summary,
        market="us",
        feed_source=feed_source or source,
        source=source,
        stock_symbol=None,
        stock_name=None,
        published_at=published_at or now,
        scraped_at=scraped_at or now,
    )


def _cluster(*indices: int, best_similarity: float = 1.0) -> dict[str, object]:
    return {"indices": list(indices), "best_similarity": best_similarity}


def test_normalize_source_key_collapses_naver_research_house_variants() -> None:
    assert (
        lab.normalize_source_key("browser_naver_research") == "browser_naver_research"
    )
    assert (
        lab.normalize_source_key("browser_naver_research_daishin")
        == "browser_naver_research"
    )
    assert (
        lab.normalize_source_key("browser_naver_research_yuanta_securities")
        == "browser_naver_research"
    )
    assert (
        lab.normalize_source_key("rss_yahoo_finance_topstories")
        == "rss_yahoo_finance_topstories"
    )
    assert lab.normalize_source_key("") == "unknown"
    assert lab.normalize_source_key(None) == "unknown"


def test_article_normalized_source_key_uses_source_key_fallback() -> None:
    article = _article(feed_source="browser_naver_research_daishin", source="naver")
    assert article.source_key == "browser_naver_research_daishin"
    assert article.normalized_source_key == "browser_naver_research"


@pytest.mark.parametrize(
    ("article", "regular", "noise", "market_signal"),
    [
        (_article(title="[데일리 마감] 코스피 약보합"), True, False, False),
        (_article(title="Morning Letter: futures higher"), True, False, False),
        (
            _article(
                title="Best travel credit cards for 2026",
                feed_source="rss_yahoo_finance_topstories",
            ),
            False,
            True,
            False,
        ),
        (
            _article(
                title="Fed minutes spook card-issuer stocks",
                feed_source="rss_yahoo_finance_topstories",
            ),
            False,
            True,
            True,
        ),
        (_article(title="Nasdaq closes at record on AI earnings"), False, False, True),
    ],
)
def test_classify_title_flags_regular_reports_noise_and_market_signals(
    article: lab.Article, regular: bool, noise: bool, market_signal: bool
) -> None:
    flags = lab.classify_title(article)
    assert flags.is_regular_report is regular
    assert flags.is_yahoo_personal_finance is noise
    assert flags.has_market_signal is market_signal


def test_score_cluster_reports_normalized_source_counts_and_duplicate_penalty() -> None:
    articles = [
        _article(1, source="browser_naver_research_daishin"),
        _article(2, source="browser_naver_research_yuanta"),
    ]
    score = lab.score_cluster(_cluster(0, 1), articles, window_hours=24)
    assert score.raw_source_count == 2
    assert score.normalized_source_count == 1
    assert score.penalties["duplicate_source"] > 0


def test_score_cluster_caps_diversity_at_five_source_families() -> None:
    articles = [_article(i, source=f"source_{i}") for i in range(1, 7)]
    score = lab.score_cluster(_cluster(*range(6)), articles, window_hours=24)
    assert score.components["source_diversity_norm"] == 1.0


def test_score_cluster_caps_regular_report_penalty() -> None:
    articles = [
        _article(i, title=f"Morning Letter daily market note {i}", source=f"s{i}")
        for i in range(1, 6)
    ]
    score = lab.score_cluster(_cluster(*range(5)), articles, window_hours=24)
    assert score.penalties["regular_report"] == 0.45
    assert score.flags["regular_report"] == 5


def test_score_cluster_gives_market_signal_topic_relevance() -> None:
    articles = [_article(title="Treasury yields rise as CPI reshapes Fed bets")]
    score = lab.score_cluster(_cluster(0), articles, window_hours=24)
    assert score.components["topic_relevance"] >= 0.5


def test_score_cluster_caps_future_recency_at_one() -> None:
    articles = [
        _article(
            title="Nasdaq earnings stocks rally",
            published_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        )
    ]
    score = lab.score_cluster(_cluster(0), articles, window_hours=24)
    assert score.components["recency_norm"] == 1.0


def test_keyword_matches_does_not_match_single_syllable_korean_inside_words() -> None:
    assert not lab.keyword_matches("외국인 매도에도 코스피 최고치", "은")
    assert lab.keyword_matches("금 가격 상승", "금")


def test_score_increases_or_preserves_when_fresh_source_is_added() -> None:
    old_article = _article(
        1,
        title="Nasdaq earnings lift stocks",
        source="reuters",
        published_at=(datetime.now(UTC) - timedelta(hours=10)).isoformat(),
    )
    fresh_article = _article(
        2,
        title="Nasdaq earnings lift stocks again",
        source="bloomberg",
        published_at=datetime.now(UTC).isoformat(),
    )
    one = lab.score_cluster(_cluster(0), [old_article], window_hours=24)
    two = lab.score_cluster(
        _cluster(0, 1), [old_article, fresh_article], window_hours=24
    )
    assert two.score >= one.score


def test_rank_clusters_v2_sorts_by_score_then_source_and_article_counts() -> None:
    articles = [
        _article(1, title="Generic market item", source="a"),
        _article(2, title="Nasdaq earnings stocks rally", source="b"),
        _article(3, title="Nasdaq earnings stocks rally again", source="c"),
    ]
    ranked = lab.rank_clusters_v2(
        [_cluster(0), _cluster(1, 2)], articles, window_hours=24
    )
    assert ranked[0][0]["indices"] == [1, 2]
    assert ranked[0][1].normalized_source_count == 2


def test_summarize_cluster_includes_score_diagnostics_and_source_counts() -> None:
    articles = [_article(source="browser_naver_research_daishin")]
    breakdown = lab.score_cluster(_cluster(0), articles, window_hours=24)
    issue = lab.summarize_cluster(
        _cluster(0), articles, rank=1, score_breakdown=breakdown
    )
    assert issue["score"] == breakdown.score
    assert "score_components" in issue
    assert "score_weighted" in issue
    assert "score_penalties" in issue
    assert issue["source_counts"]["normalized"] == {"browser_naver_research": 1}
    assert issue["raw_source_count"] == 1
    assert issue["normalized_source_count"] == 1


def test_render_markdown_includes_score_and_raw_normalized_source_counts() -> None:
    payload = {
        "run": {
            "run_uuid": "run-1",
            "market": "all",
            "window_hours": 24,
            "article_count": 1,
            "cluster_count": 1,
            "embedding_model": "BAAI/bge-m3",
            "embedding_dim": 1024,
            "threshold": 0.78,
        },
        "issues": [
            {
                "rank": 1,
                "direction": "neutral",
                "title_ko": "테스트",
                "subtitle_ko": "부제",
                "raw_source_count": 2,
                "normalized_source_count": 1,
                "source_count": 1,
                "article_count": 3,
                "score": 0.5,
                "score_components": {"source_diversity_norm": 0.2},
                "score_penalties": {"duplicate_source": 0.15},
                "representative_sources": ["a", "b"],
                "markets": ["us"],
                "topics": [],
                "related_symbols": [],
                "representative_articles": [],
            }
        ],
    }
    rendered = lab.render_markdown(payload)
    assert "raw 2개 → normalized 1개" in rendered
    assert "점수: 0.5000" in rendered
    assert "duplicate_source" in rendered


def test_parse_weights_accepts_complete_normalized_weights() -> None:
    weights = lab.parse_weights("diversity=0.5,volume=0.2,recency=0.2,relevance=0.1")
    assert weights == lab.ScoreWeights(
        diversity=0.5, volume=0.2, recency=0.2, relevance=0.1
    )


@pytest.mark.parametrize(
    "raw",
    [
        "diversity=1.0,volume=0,recency=0,relevance=0,unknown=0",
        "diversity=-0.1,volume=0.5,recency=0.3,relevance=0.3",
        "diversity=0.5,volume=0.5,recency=0.5,relevance=0.5",
    ],
)
def test_parse_weights_rejects_invalid_weights(raw: str) -> None:
    with pytest.raises(ValueError):
        lab.parse_weights(raw)


def test_parse_args_rejects_non_positive_counts() -> None:
    with pytest.raises(SystemExit):
        lab.parse_args(["--batch-size", "0"])


@pytest.mark.asyncio
async def test_build_payload_compare_v1_json_block_and_drop_regular_reports(
    monkeypatch,
) -> None:
    articles = [
        _article(1, title="Morning Letter daily market note", source="a"),
        _article(2, title="Nasdaq earnings stocks rally", source="b"),
        _article(3, title="Nasdaq earnings stocks rally again", source="c"),
    ]

    async def fake_fetch_articles(
        market: str, window_hours: int, limit: int
    ) -> list[lab.Article]:
        return articles

    monkeypatch.setattr(lab, "fetch_articles", fake_fetch_articles)
    monkeypatch.setattr(
        lab,
        "embed_batch",
        lambda endpoint, model, texts: [[1.0, 0.0] for _ in texts],
    )
    args = Namespace(
        market="all",
        window_hours=24,
        limit=240,
        top=12,
        threshold=0.0,
        dedupe_threshold=0.90,
        embedding_endpoint="http://127.0.0.1:10631/v1/embeddings",
        embedding_model="BAAI/bge-m3",
        batch_size=32,
        compare_v1=True,
        weights=None,
        drop_regular_reports=True,
    )
    payload = await lab.build_payload(args)
    assert "v1_vs_v2" in payload
    assert payload["run"]["top"] == 12
    assert payload["source_counts"]["raw"] == {"a": 1, "b": 1, "c": 1}
    assert payload["source_counts"]["normalized"] == {"a": 1, "b": 1, "c": 1}
    assert all(
        issue["flags"]["regular_report"] / issue["article_count"] < 0.5
        for issue in payload["issues"]
    )
    rendered = lab.render_comparison_markdown(payload, [], [], top=12)
    assert "## v1 vs v2 comparison" in rendered

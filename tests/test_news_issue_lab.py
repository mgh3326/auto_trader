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


# ---------------------------------------------------------------------------
# ROB-135 tests
# ---------------------------------------------------------------------------


def test_cluster_topic_label_returns_topic_title_when_rule_matches() -> None:
    rows = [_article(title="SK하이닉스 300만원 전망 반도체 강세")]
    assert lab.cluster_topic_label(rows) == "반도체 슈퍼사이클"


def test_cluster_topic_label_returns_none_when_no_rule_matches() -> None:
    rows = [_article(title="Generic uneventful headline")]
    assert lab.cluster_topic_label(rows) is None


def test_cluster_topic_label_handles_empty_rows() -> None:
    assert lab.cluster_topic_label([]) is None


def test_merge_decision_dataclass_holds_all_signals() -> None:
    decision = lab.MergeDecision(
        absorber_cid=1,
        absorbed_cid=2,
        rep_sim=0.91,
        token_jaccard=0.4,
        source_overlap=0.5,
        topic_agree=True,
        symbol_agree=False,
        decision="merged",
        reason="topic+rep",
        absorber_title="X",
        absorbed_title="Y",
    )
    assert decision.decision == "merged"
    assert decision.rep_sim == 0.91


def test_merge_diagnostics_dataclass_defaults_to_disabled() -> None:
    diag = lab.MergeDiagnostics()
    assert diag.enabled is False
    assert diag.merge_before_count == 0
    assert diag.merge_after_count == 0
    assert diag.decisions == []


def test_merge_constants_have_expected_defaults() -> None:
    assert lab.MERGE_REP_THRESHOLD == 0.86
    assert lab.MERGE_TOKEN_JACCARD == 0.30
    assert lab.MERGE_STRONG_REP_THRESHOLD == 0.93
    assert lab.MERGE_TOPIC_REP_THRESHOLD == 0.43
    assert lab.MERGE_MIN_TOKEN_FLOOR == 0.20
    assert lab.MERGE_MAX_CLUSTER_SIZE == 25


def test_build_cluster_representative_uses_top_titles_and_topic_label() -> None:
    articles = [
        _article(1, title="삼성전자 반도체 호황 진입", source="naver"),
        _article(2, title="SK하이닉스 메모리 강세", source="hankyung"),
        _article(3, title="Random unrelated headline", source="other"),
    ]
    cluster = {"indices": [0, 1, 2], "tokens": set()}
    rep = lab.build_cluster_representative(cluster, articles, max_articles=2)
    assert "삼성전자" in rep
    assert "반도체 슈퍼사이클" in rep
    assert "Random unrelated headline" not in rep


def test_build_cluster_representative_prefers_symbol_bearing_articles() -> None:
    a1 = lab.Article(
        id=1,
        title="Generic",
        summary=None,
        market="us",
        feed_source="x",
        source="x",
        stock_symbol=None,
        stock_name=None,
        published_at=None,
        scraped_at=None,
    )
    a2 = lab.Article(
        id=2,
        title="Apple Q4",
        summary=None,
        market="us",
        feed_source="y",
        source="y",
        stock_symbol="AAPL",
        stock_name="Apple",
        published_at=None,
        scraped_at=None,
    )
    cluster = {"indices": [0, 1]}
    rep = lab.build_cluster_representative(cluster, [a1, a2], max_articles=1)
    assert "AAPL" in rep
    assert "Apple Q4" in rep


def test_build_cluster_representative_is_deterministic() -> None:
    articles = [_article(i, title=f"t{i}", source=f"s{i}") for i in range(1, 5)]
    cluster = {"indices": [0, 1, 2, 3]}
    rep1 = lab.build_cluster_representative(cluster, articles, max_articles=2)
    rep2 = lab.build_cluster_representative(cluster, articles, max_articles=2)
    assert rep1 == rep2


def _make_cluster_for_merge(
    indices: list[int], tokens: set[str], best_similarity: float = 1.0
) -> dict[str, object]:
    return {
        "indices": indices,
        "tokens": tokens,
        "best_similarity": best_similarity,
    }


def test_evaluate_merge_pair_merges_topic_match_with_high_rep_sim() -> None:
    articles = [
        _article(1, title="삼성전자 반도체 호황", source="a"),
        _article(2, title="SK하이닉스 메모리 강세", source="b"),
    ]
    a = _make_cluster_for_merge([0], {"삼성전자", "반도체"})
    b = _make_cluster_for_merge([1], {"sk하이닉스", "반도체"})
    decision = lab._evaluate_merge_pair(
        a,
        b,
        articles,
        rep_sim=0.88,
        absorber_cid=1,
        absorbed_cid=2,
    )
    assert decision.decision == "merged"
    assert decision.topic_agree is True
    assert decision.reason in {
        "topic+rep",
        "symbol+rep",
        "jaccard+rep",
        "strong_rep",
    }


def test_evaluate_merge_pair_allows_calibrated_topic_low_rep_match() -> None:
    articles = [
        _article(1, title="삼성전자 반도체 호황", source="a"),
        _article(2, title="SK하이닉스 메모리 강세", source="b"),
    ]
    a = _make_cluster_for_merge([0], {"삼성전자"})
    b = _make_cluster_for_merge([1], {"sk하이닉스"})
    decision = lab._evaluate_merge_pair(
        a,
        b,
        articles,
        rep_sim=lab.MERGE_TOPIC_REP_THRESHOLD,
        absorber_cid=1,
        absorbed_cid=2,
    )
    assert decision.decision == "merged"
    assert decision.reason == "topic+low_rep"
    assert decision.topic_agree is True


def test_evaluate_merge_pair_rejects_low_token_floor_without_topic() -> None:
    articles = [
        _article(1, title="Random foo bar", source="a"),
        _article(2, title="Totally other baz", source="b"),
    ]
    a = _make_cluster_for_merge([0], {"random", "foo"})
    b = _make_cluster_for_merge([1], {"totally", "baz"})
    decision = lab._evaluate_merge_pair(
        a,
        b,
        articles,
        rep_sim=0.88,
        absorber_cid=1,
        absorbed_cid=2,
    )
    assert decision.decision == "rejected"
    assert "below_token_floor" in decision.reason or "no_topic" in decision.reason


def test_evaluate_merge_pair_rejects_when_rep_sim_too_low() -> None:
    articles = [
        _article(1, title="비트코인 강세", source="a"),
        _article(2, title="유가 변동성 확대", source="b"),
    ]
    a = _make_cluster_for_merge([0], {"x"})
    b = _make_cluster_for_merge([1], {"x"})
    decision = lab._evaluate_merge_pair(
        a,
        b,
        articles,
        rep_sim=0.50,
        absorber_cid=1,
        absorbed_cid=2,
    )
    assert decision.decision == "rejected"


def test_evaluate_merge_pair_strong_rep_with_some_jaccard_merges() -> None:
    articles = [
        _article(1, title="Apple Q4 earnings", source="a"),
        _article(2, title="애플 4분기 실적", source="b"),
    ]
    a = _make_cluster_for_merge([0], {"apple", "earnings"})
    b = _make_cluster_for_merge([1], {"애플", "실적"})
    decision = lab._evaluate_merge_pair(
        a,
        b,
        articles,
        rep_sim=0.95,
        absorber_cid=1,
        absorbed_cid=2,
    )
    assert decision.decision == "merged"


def test_merge_clusters_fuses_two_topic_tied_single_article_clusters() -> None:
    articles = [
        _article(1, title="삼성전자 반도체 슈퍼사이클 진입", source="naver"),
        _article(2, title="SK하이닉스 반도체 호황 지속", source="hankyung"),
    ]
    clusters = [
        {
            "indices": [0],
            "vectors": [[1.0, 0.0]],
            "centroid": [1.0, 0.0],
            "tokens": set(lab.tokenize(articles[0].text_for_embedding)),
            "best_similarity": 1.0,
        },
        {
            "indices": [1],
            "vectors": [[0.99, 0.0]],
            "centroid": [0.99, 0.0],
            "tokens": set(lab.tokenize(articles[1].text_for_embedding)),
            "best_similarity": 1.0,
        },
    ]

    def fake_embedder(texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0] for _ in texts]

    merged, diag = lab.merge_clusters(
        clusters,
        articles,
        fake_embedder,
        rep_threshold=0.86,
        token_jaccard_threshold=0.30,
        rep_articles=2,
    )
    assert len(merged) == 1
    assert sorted(merged[0]["indices"]) == [0, 1]
    assert diag.merge_before_count == 2
    assert diag.merge_after_count == 1
    assert any(d.decision == "merged" for d in diag.decisions)


def test_merge_clusters_respects_max_size_across_transitive_merges() -> None:
    articles = [
        _article(i + 1, title=f"삼성전자 반도체 슈퍼사이클 {i}", source=f"s{i % 3}")
        for i in range(30)
    ]
    clusters = [
        {
            "indices": list(range(0, 20)),
            "vectors": [[1.0, 0.0] for _ in range(20)],
            "centroid": [1.0, 0.0],
            "tokens": {"삼성전자", "반도체"},
            "best_similarity": 1.0,
        },
        {
            "indices": list(range(20, 25)),
            "vectors": [[1.0, 0.0] for _ in range(5)],
            "centroid": [1.0, 0.0],
            "tokens": {"삼성전자", "반도체"},
            "best_similarity": 1.0,
        },
        {
            "indices": list(range(25, 30)),
            "vectors": [[1.0, 0.0] for _ in range(5)],
            "centroid": [1.0, 0.0],
            "tokens": {"삼성전자", "반도체"},
            "best_similarity": 1.0,
        },
    ]

    def fake_embedder(texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0] for _ in texts]

    merged, diag = lab.merge_clusters(
        clusters,
        articles,
        fake_embedder,
        rep_threshold=0.86,
        token_jaccard_threshold=0.30,
        rep_articles=2,
    )

    assert (
        max(len(cluster["indices"]) for cluster in merged) == lab.MERGE_MAX_CLUSTER_SIZE
    )
    assert sorted(len(cluster["indices"]) for cluster in merged) == [5, 25]
    assert any(d.reason == "max_cluster_size" for d in diag.decisions)


def test_merge_clusters_keeps_unrelated_topics_separate() -> None:
    articles = [
        _article(1, title="비트코인 신고가 돌파", source="a"),
        _article(2, title="유가 변동성 확대", source="b"),
    ]
    clusters = [
        {
            "indices": [0],
            "vectors": [[1.0, 0.0]],
            "centroid": [1.0, 0.0],
            "tokens": set(lab.tokenize(articles[0].text_for_embedding)),
            "best_similarity": 1.0,
        },
        {
            "indices": [1],
            "vectors": [[0.0, 1.0]],
            "centroid": [0.0, 1.0],
            "tokens": set(lab.tokenize(articles[1].text_for_embedding)),
            "best_similarity": 1.0,
        },
    ]

    def fake_embedder(texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0] if "비트코인" in t else [0.0, 1.0] for t in texts]

    merged, diag = lab.merge_clusters(
        clusters,
        articles,
        fake_embedder,
        rep_threshold=0.86,
        token_jaccard_threshold=0.30,
        rep_articles=2,
    )
    assert len(merged) == 2
    assert diag.merge_after_count == 2


def test_merge_clusters_disabled_path_returns_inputs_with_diag_disabled() -> None:
    articles = [_article(1)]
    clusters = [
        {
            "indices": [0],
            "vectors": [[1.0]],
            "centroid": [1.0],
            "tokens": set(),
            "best_similarity": 1.0,
        }
    ]
    merged, diag = lab.merge_clusters(
        clusters,
        articles,
        embedder=None,
        rep_threshold=0.86,
        token_jaccard_threshold=0.30,
        rep_articles=2,
        enabled=False,
    )
    assert merged is clusters
    assert diag.enabled is False


def test_merge_clusters_is_deterministic_under_input_reordering() -> None:
    articles = [
        _article(1, title="삼성전자 반도체 슈퍼사이클", source="naver"),
        _article(2, title="SK하이닉스 반도체 강세", source="hankyung"),
        _article(3, title="비트코인 강세", source="coindesk"),
    ]

    def base_clusters() -> list[dict]:
        return [
            {
                "indices": [0],
                "vectors": [[1.0, 0.0]],
                "centroid": [1.0, 0.0],
                "tokens": set(lab.tokenize(articles[0].text_for_embedding)),
                "best_similarity": 1.0,
            },
            {
                "indices": [1],
                "vectors": [[1.0, 0.0]],
                "centroid": [1.0, 0.0],
                "tokens": set(lab.tokenize(articles[1].text_for_embedding)),
                "best_similarity": 1.0,
            },
            {
                "indices": [2],
                "vectors": [[0.0, 1.0]],
                "centroid": [0.0, 1.0],
                "tokens": set(lab.tokenize(articles[2].text_for_embedding)),
                "best_similarity": 1.0,
            },
        ]

    def embedder(texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0] if "반도체" in t else [0.0, 1.0] for t in texts]

    forward, _ = lab.merge_clusters(
        base_clusters(),
        articles,
        embedder,
        rep_threshold=0.86,
        token_jaccard_threshold=0.30,
        rep_articles=2,
    )
    reversed_inputs = list(reversed(base_clusters()))
    backward, _ = lab.merge_clusters(
        reversed_inputs,
        articles,
        embedder,
        rep_threshold=0.86,
        token_jaccard_threshold=0.30,
        rep_articles=2,
    )
    assert sorted(tuple(sorted(c["indices"])) for c in forward) == sorted(
        tuple(sorted(c["indices"])) for c in backward
    )


def test_summarize_cluster_includes_merge_member_count_and_ids() -> None:
    articles = [_article(1), _article(2)]
    cluster = {
        "indices": [0, 1],
        "vectors": [[1.0, 0.0], [1.0, 0.0]],
        "centroid": [1.0, 0.0],
        "tokens": set(),
        "best_similarity": 1.0,
        "merged_cluster_ids": [1, 2],
    }
    issue = lab.summarize_cluster(cluster, articles, rank=1)
    assert issue["merge_member_count"] == 2
    assert issue["merged_cluster_ids"] == [1, 2]


def test_summarize_cluster_handles_unmerged_cluster() -> None:
    articles = [_article(1)]
    cluster = {
        "indices": [0],
        "vectors": [[1.0]],
        "centroid": [1.0],
        "tokens": set(),
        "best_similarity": 1.0,
    }
    issue = lab.summarize_cluster(cluster, articles, rank=1)
    assert issue["merge_member_count"] == 1
    assert issue["merged_cluster_ids"] == [1]


@pytest.mark.asyncio
async def test_build_payload_runs_merge_pass_and_emits_run_diag(monkeypatch) -> None:
    articles = [
        _article(1, title="삼성전자 반도체 슈퍼사이클", source="naver"),
        _article(2, title="SK하이닉스 반도체 호황", source="hankyung"),
        _article(3, title="비트코인 강세 지속", source="coindesk"),
    ]

    async def fake_fetch_articles(market, window_hours, limit):
        return articles

    call_count = {"i": 0}

    def fake_embed(endpoint, model, texts):
        call_count["i"] += 1
        if call_count["i"] == 1:
            return [[1.0, 0.0] if "반도체" in t else [0.0, 1.0] for t in texts]
        return [[1.0, 0.0] if "반도체" in t else [0.0, 1.0] for t in texts]

    monkeypatch.setattr(lab, "fetch_articles", fake_fetch_articles)
    monkeypatch.setattr(lab, "embed_batch", fake_embed)

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
        compare_v1=False,
        weights=None,
        drop_regular_reports=False,
        merge_clusters=True,
        merge_rep_threshold=0.86,
        merge_token_jaccard=0.30,
        merge_rep_articles=3,
    )
    payload = await lab.build_payload(args)
    assert "merge_diagnostics" in payload
    assert payload["merge_diagnostics"]["enabled"] is True
    assert (
        payload["run"]["cluster_count_before_merge"] >= payload["run"]["cluster_count"]
    )


@pytest.mark.asyncio
async def test_build_payload_no_merge_flag_disables_merge(monkeypatch) -> None:
    articles = [
        _article(1, title="삼성전자 반도체", source="a"),
        _article(2, title="비트코인 강세", source="b"),
    ]

    async def fake_fetch_articles(market, window_hours, limit):
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
        threshold=0.78,
        dedupe_threshold=0.90,
        embedding_endpoint="http://127.0.0.1:10631/v1/embeddings",
        embedding_model="BAAI/bge-m3",
        batch_size=32,
        compare_v1=False,
        weights=None,
        drop_regular_reports=False,
        merge_clusters=False,
        merge_rep_threshold=0.86,
        merge_token_jaccard=0.30,
        merge_rep_articles=3,
    )
    payload = await lab.build_payload(args)
    assert payload["merge_diagnostics"]["enabled"] is False
    assert (
        payload["run"]["cluster_count_before_merge"] == payload["run"]["cluster_count"]
    )


def test_parse_args_accepts_merge_flags_and_defaults() -> None:
    args = lab.parse_args([])
    assert args.merge_clusters is True
    assert args.merge_rep_threshold == 0.86
    assert args.merge_token_jaccard == 0.30
    assert args.merge_rep_articles == 3
    args2 = lab.parse_args(["--no-merge-clusters", "--merge-rep-threshold", "0.9"])
    assert args2.merge_clusters is False
    assert args2.merge_rep_threshold == 0.9


def test_parse_args_rejects_invalid_merge_thresholds() -> None:
    with pytest.raises(SystemExit):
        lab.parse_args(["--merge-rep-threshold", "1.5"])
    with pytest.raises(SystemExit):
        lab.parse_args(["--merge-token-jaccard", "-0.1"])
    with pytest.raises(SystemExit):
        lab.parse_args(["--merge-rep-articles", "0"])


def test_render_markdown_includes_merge_section_when_decisions_present() -> None:
    payload = {
        "run": {
            "run_uuid": "r-1",
            "market": "all",
            "window_hours": 24,
            "article_count": 3,
            "cluster_count": 2,
            "embedding_model": "BAAI/bge-m3",
            "embedding_dim": 1024,
            "threshold": 0.78,
            "cluster_count_before_merge": 4,
        },
        "issues": [
            {
                "rank": 1,
                "direction": "neutral",
                "title_ko": "반도체 슈퍼사이클",
                "subtitle_ko": "x",
                "raw_source_count": 2,
                "normalized_source_count": 2,
                "source_count": 2,
                "article_count": 2,
                "score": 0.5,
                "score_components": {},
                "score_penalties": {},
                "representative_sources": [],
                "markets": ["kr"],
                "topics": [],
                "related_symbols": [],
                "representative_articles": [],
                "merge_member_count": 3,
                "merged_cluster_ids": [1, 2, 3],
            }
        ],
        "merge_diagnostics": {
            "enabled": True,
            "merge_before_count": 4,
            "merge_after_count": 2,
            "rejected_near_misses": 1,
            "thresholds": {
                "rep_threshold": 0.86,
                "token_jaccard_threshold": 0.30,
                "strong_rep_threshold": 0.93,
                "min_token_floor": 0.20,
                "max_cluster_size": 25,
                "rep_articles": 3,
            },
            "decisions": [
                {
                    "absorber_cid": 1,
                    "absorbed_cid": 2,
                    "rep_sim": 0.87,
                    "token_jaccard": 0.41,
                    "source_overlap": 0.5,
                    "topic_agree": True,
                    "symbol_agree": False,
                    "decision": "merged",
                    "reason": "topic+rep",
                    "absorber_title": "반도체 슈퍼사이클",
                    "absorbed_title": "반도체 슈퍼사이클",
                }
            ],
        },
    }
    md = lab.render_markdown(payload)
    assert "## 클러스터 병합 진단" in md
    assert "병합 전 클러스터: 4" in md
    assert "병합 후" in md
    assert "반도체 슈퍼사이클" in md
    assert "병합: 3개 클러스터 통합" in md


def test_render_markdown_skips_merge_section_when_disabled() -> None:
    payload = {
        "run": {
            "run_uuid": "r-1",
            "market": "all",
            "window_hours": 24,
            "article_count": 1,
            "cluster_count": 1,
            "embedding_model": "BAAI/bge-m3",
            "embedding_dim": 1024,
            "threshold": 0.78,
            "cluster_count_before_merge": 1,
        },
        "issues": [],
        "merge_diagnostics": {
            "enabled": False,
            "merge_before_count": 1,
            "merge_after_count": 1,
            "rejected_near_misses": 0,
            "thresholds": {},
            "decisions": [],
        },
    }
    md = lab.render_markdown(payload)
    assert "## 클러스터 병합 진단" not in md

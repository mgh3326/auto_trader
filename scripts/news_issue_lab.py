#!/usr/bin/env python3
"""Toss-like Korean real-time news issue lab.

This is an operator-facing PoC script. It reads existing ``news_articles`` rows,
uses the local BGE-M3 OpenAI-compatible embedding endpoint for clustering, renders
short Korean issue titles/subtitles with rule-based fallbacks, and optionally
stores the lab run/result payloads in dedicated experimental tables.

It intentionally does not touch broker/order/watch/scheduler state and does not
store or print article bodies.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import re
import sys
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib import request

from sqlalchemy import text

from app.core.db import AsyncSessionLocal

DEFAULT_BGE_ENDPOINT = "http://127.0.0.1:10631/v1/embeddings"
DEFAULT_BGE_MODEL = "BAAI/bge-m3"

POSITIVE_TERMS = {
    "최대",
    "상승",
    "급등",
    "호황",
    "개선",
    "성장",
    "돌파",
    "최고",
    "흑자",
    "호실적",
    "surge",
    "rally",
    "record",
    "beats",
    "beat",
    "growth",
    "upgrade",
    "bull",
    "profit",
    "gain",
    "higher",
    "strong",
    "breakthrough",
    "upside",
    "boost",
    "jumps",
    "skyrocketing",
}
NEGATIVE_TERMS = {
    "하락",
    "급락",
    "위험",
    "우려",
    "부진",
    "손실",
    "적자",
    "약세",
    "압박",
    "관세",
    "전쟁",
    "slump",
    "falls",
    "drop",
    "crash",
    "loss",
    "risk",
    "warning",
    "miss",
    "cuts",
    "downgrade",
    "weak",
    "pressure",
    "tariff",
    "selloff",
    "slides",
    "probe",
    "lawsuit",
}

TOPIC_RULES: list[tuple[str, str, tuple[str, ...]]] = [
    (
        "카카오 실적",
        "플랫폼 성장·수익성 개선",
        ("카카오", "kakao", "카카오페이", "카카오뱅크"),
    ),
    (
        "반도체 슈퍼사이클",
        "AI 수요·메모리 호황",
        (
            "반도체",
            "sk하이닉스",
            "삼성전자",
            "hynix",
            "semiconductor",
            "chip",
            "memory",
            "nvidia",
        ),
    ),
    ("비트코인 강세", "기관 자금·위험자산 선호", ("bitcoin", "btc", "비트코인")),
    (
        "이더리움·알트코인",
        "네트워크 업그레이드·ETF 기대",
        ("ethereum", "ether", "eth", "이더리움", "altcoin"),
    ),
    (
        "미국 금리·연준",
        "정책 기대와 채권금리 변화",
        ("fed", "federal reserve", "fomc", "rate", "treasury", "yield", "연준", "금리"),
    ),
    (
        "미국 증시 최고치",
        "기술주 강세·실적 기대",
        ("s&p", "nasdaq", "dow", "wall street", "stock market", "미국 증시", "나스닥"),
    ),
    (
        "유가 변동",
        "중동 리스크·에너지 수급",
        ("oil", "crude", "opec", "유가", "원유", "energy"),
    ),
    (
        "AI 데이터센터",
        "전력·인프라 투자 확대",
        (
            "ai",
            "artificial intelligence",
            "data center",
            "datacenter",
            "데이터센터",
            "인공지능",
        ),
    ),
    (
        "전기차·배터리",
        "수요 둔화와 공급망 재편",
        ("ev", "electric vehicle", "battery", "tesla", "lucid", "전기차", "배터리"),
    ),
    (
        "부동산·리츠",
        "금리 민감 업종 재평가",
        ("reit", "property", "realty", "mortgage", "부동산", "리츠"),
    ),
    (
        "금·원자재",
        "안전자산 수요와 광산주 움직임",
        ("gold", "silver", "mining", "copper", "금", "은", "원자재"),
    ),
    (
        "기업 실적 발표",
        "분기 실적과 가이던스 영향",
        (
            "earnings",
            "results",
            "quarter",
            "revenue",
            "profit",
            "실적",
            "매출",
            "영업이익",
        ),
    ),
    (
        "M&A·사업재편",
        "인수합병과 분사 이슈",
        (
            "acquire",
            "acquisition",
            "merger",
            "spin off",
            "spin-off",
            "인수",
            "합병",
            "분사",
        ),
    ),
]

STOPWORDS = {
    "the",
    "and",
    "for",
    "with",
    "from",
    "that",
    "this",
    "why",
    "what",
    "into",
    "amid",
    "after",
    "before",
    "stock",
    "stocks",
    "shares",
    "inc",
    "corp",
    "ltd",
    "company",
    "today",
    "now",
    "best",
    "buy",
    "top",
    "증시",
    "시장",
    "오늘",
    "관련",
    "뉴스",
    "기자",
    "서울",
    "종목",
    "투자",
    "분석",
}

RESEARCH_HOUSE_PREFIXES: tuple[str, ...] = ("browser_naver_research",)
MARKET_SIGNAL_TERMS: tuple[str, ...] = (
    "fed",
    "federal reserve",
    "fomc",
    "cpi",
    "treasury",
    "yield",
    "s&p",
    "s&p 500",
    "nasdaq",
    "earnings",
    "shares",
    "stock",
    "stocks",
    "analyst",
    "revenue",
    "profit",
    "연준",
    "금리",
    "실적",
    "증시",
    "주식",
)
YAHOO_PERSONAL_FINANCE_TERMS: tuple[str, ...] = (
    "card",
    "credit card",
    "travel",
    "mortgage",
    "savings",
    "savings account",
    "loan",
    "insurance",
    "personal finance",
    "apy",
    "home buyer",
    "home buyers",
)
REGULAR_REPORT_TERMS: tuple[str, ...] = (
    "morning letter",
    "daily",
    "weekly",
    "데일리",
    "장마감코멘트",
    "모닝코멘트",
    "전략공감",
    "이슈코멘트",
    "주간",
)


def normalize_source_key(source_key: str | None) -> str:
    if not source_key:
        return "unknown"
    for prefix in RESEARCH_HOUSE_PREFIXES:
        if source_key == prefix or source_key.startswith(prefix + "_"):
            return prefix
    return source_key


@dataclass(frozen=True)
class Article:
    id: int
    title: str
    summary: str | None
    market: str
    feed_source: str | None
    source: str | None
    stock_symbol: str | None
    stock_name: str | None
    published_at: str | None
    scraped_at: str | None

    @property
    def source_key(self) -> str:
        return self.feed_source or self.source or "unknown"

    @property
    def normalized_source_key(self) -> str:
        return normalize_source_key(self.source_key)

    @property
    def text_for_embedding(self) -> str:
        parts = [self.title]
        if self.summary:
            parts.append(self.summary[:300])
        parts.extend([self.market, self.source_key])
        if self.stock_symbol:
            parts.append(self.stock_symbol)
        if self.stock_name:
            parts.append(self.stock_name)
        return " | ".join(p for p in parts if p)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Toss-like news issue lab using local BGE-M3 embeddings"
    )
    parser.add_argument(
        "--market", default="all", choices=["all", "kr", "us", "crypto"]
    )
    parser.add_argument("--window-hours", type=int, default=24)
    parser.add_argument("--limit", type=int, default=240)
    parser.add_argument("--top", type=int, default=12)
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.78,
        help="cosine similarity threshold for same-issue clusters",
    )
    parser.add_argument(
        "--dedupe-threshold",
        type=float,
        default=0.90,
        help="near-duplicate boost threshold",
    )
    parser.add_argument("--embedding-endpoint", default=DEFAULT_BGE_ENDPOINT)
    parser.add_argument("--embedding-model", default=DEFAULT_BGE_MODEL)
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument(
        "--store", action="store_true", help="store run/result payloads in lab tables"
    )
    parser.add_argument("--format", choices=["markdown", "json"], default="markdown")
    parser.add_argument("--output", help="optional output file path")
    parser.add_argument(
        "--compare-v1",
        action="store_true",
        help="include legacy v1 vs v2 ranking diagnostics",
    )
    parser.add_argument(
        "--weights",
        help="score weights as diversity=0.40,volume=0.25,recency=0.20,relevance=0.15",
    )
    parser.add_argument(
        "--drop-regular-reports",
        action="store_true",
        help="hard-drop clusters where regular reports are at least half of titles",
    )
    args = parser.parse_args(argv)
    for name in ("window_hours", "limit", "top", "batch_size"):
        if getattr(args, name) <= 0:
            parser.error(f"--{name.replace('_', '-')} must be positive")
    return args


def normalize_text(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def tokenize(text_value: str) -> list[str]:
    raw = re.findall(
        r"[A-Za-z][A-Za-z0-9&.-]{2,}|[가-힣]{2,}|\d+만?", text_value.lower()
    )
    return [t for t in raw if t not in STOPWORDS and len(t) >= 2]


def cosine(a: list[float], b: list[float]) -> float:
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b, strict=False):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0 or nb <= 0:
        return 0.0
    return dot / math.sqrt(na * nb)


def centroid(vectors: list[list[float]]) -> list[float]:
    if not vectors:
        return []
    n = len(vectors)
    return [sum(v[i] for v in vectors) / n for i in range(len(vectors[0]))]


def embed_batch(
    endpoint: str, model: str, texts: list[str], timeout: int = 180
) -> list[list[float]]:
    payload = {"model": model, "input": texts}
    req = request.Request(
        endpoint,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer local"},
        method="POST",
    )
    with request.urlopen(req, timeout=timeout) as resp:
        data = json.load(resp)
    rows = sorted(data.get("data") or [], key=lambda row: row.get("index", 0))
    vectors = [row.get("embedding") or [] for row in rows]
    if len(vectors) != len(texts):
        raise RuntimeError(
            f"embedding count mismatch: expected={len(texts)} actual={len(vectors)}"
        )
    return vectors


def cluster_articles(
    articles: list[Article], vectors: list[list[float]], threshold: float
) -> list[dict[str, Any]]:
    clusters: list[dict[str, Any]] = []
    for idx, article in enumerate(articles):
        vec = vectors[idx]
        best_i = None
        best_sim = -1.0
        article_tokens = set(tokenize(article.text_for_embedding))
        for c_i, cluster in enumerate(clusters):
            sim = cosine(vec, cluster["centroid"])
            # tiny lexical/entity boost so same ticker/topic titles stick together
            overlap = len(article_tokens & cluster["tokens"]) / max(
                len(article_tokens | cluster["tokens"]), 1
            )
            adjusted = sim + min(overlap * 0.08, 0.05)
            if adjusted > best_sim:
                best_sim = adjusted
                best_i = c_i
        if best_i is not None and best_sim >= threshold:
            cluster = clusters[best_i]
            cluster["indices"].append(idx)
            cluster["vectors"].append(vec)
            cluster["centroid"] = centroid(cluster["vectors"])
            cluster["tokens"].update(article_tokens)
            cluster["best_similarity"] = max(
                cluster.get("best_similarity", 0.0), best_sim
            )
        else:
            clusters.append(
                {
                    "indices": [idx],
                    "vectors": [vec],
                    "centroid": vec,
                    "tokens": set(article_tokens),
                    "best_similarity": 1.0,
                }
            )
    return clusters


def keyword_matches(text_blob: str, keyword: str) -> bool:
    keyword_l = keyword.lower()
    if re.search(r"[가-힣]", keyword_l):
        if len(keyword_l) <= 1:
            return bool(
                re.search(rf"(?<![가-힣]){re.escape(keyword_l)}(?![가-힣])", text_blob)
            )
        return keyword_l in text_blob
    # Avoid substring false positives such as "ai" in "daily" or "ether" in "together".
    return bool(
        re.search(rf"(?<![a-z0-9]){re.escape(keyword_l)}(?![a-z0-9])", text_blob)
    )


@dataclass(frozen=True)
class TitleFlags:
    is_regular_report: bool
    is_yahoo_personal_finance: bool
    has_market_signal: bool


@dataclass(frozen=True)
class ScoreWeights:
    diversity: float = 0.40
    volume: float = 0.25
    recency: float = 0.20
    relevance: float = 0.15


DEFAULT_WEIGHTS = ScoreWeights()


@dataclass(frozen=True)
class ScoreBreakdown:
    score: float
    components: dict[str, float]
    weighted: dict[str, float]
    penalties: dict[str, float]
    raw_source_count: int
    normalized_source_count: int
    flags: dict[str, int]


def _round_score(value: float) -> float:
    return round(float(value), 4)


def _article_blob(article: Article) -> str:
    return f"{article.title} {article.summary or ''}".lower()


def _has_any_keyword(text_blob: str, terms: tuple[str, ...]) -> bool:
    return any(keyword_matches(text_blob, term) for term in terms)


def classify_title(article: Article) -> TitleFlags:
    blob = _article_blob(article)
    has_market_signal = _has_any_keyword(blob, MARKET_SIGNAL_TERMS)
    is_regular_report = _has_any_keyword(blob, REGULAR_REPORT_TERMS)
    source_blob = article.source_key.lower()
    has_pf_term = _has_any_keyword(blob, YAHOO_PERSONAL_FINANCE_TERMS)
    is_yahoo_personal_finance = has_pf_term and "yahoo" in source_blob
    return TitleFlags(
        is_regular_report=is_regular_report,
        is_yahoo_personal_finance=is_yahoo_personal_finance,
        has_market_signal=has_market_signal,
    )


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def parse_weights(value: str | None) -> ScoreWeights:
    if not value:
        return DEFAULT_WEIGHTS
    allowed = {"diversity", "volume", "recency", "relevance"}
    parsed: dict[str, float] = {}
    for part in value.split(","):
        if not part.strip():
            continue
        if "=" not in part:
            raise ValueError(f"invalid weight item: {part!r}")
        key, raw_value = [x.strip() for x in part.split("=", 1)]
        if key not in allowed:
            raise ValueError(f"unknown weight key: {key}")
        try:
            weight = float(raw_value)
        except ValueError as exc:
            raise ValueError(f"invalid weight value for {key}: {raw_value}") from exc
        if weight < 0:
            raise ValueError(f"weight must be non-negative: {key}")
        parsed[key] = weight
    missing = allowed - parsed.keys()
    if missing:
        raise ValueError(f"missing weight keys: {', '.join(sorted(missing))}")
    total = sum(parsed.values())
    if not math.isclose(total, 1.0, abs_tol=0.001):
        raise ValueError(f"weights must sum to 1.0, got {total:.4f}")
    return ScoreWeights(
        diversity=parsed["diversity"],
        volume=parsed["volume"],
        recency=parsed["recency"],
        relevance=parsed["relevance"],
    )


def _topic_relevance(rows: list[Article]) -> float:
    text_blob = "\n".join([a.title + " " + (a.summary or "") for a in rows]).lower()
    for _, _, keys in TOPIC_RULES:
        if any(keyword_matches(text_blob, key) for key in keys):
            return 1.0
    return 0.5 if _has_any_keyword(text_blob, MARKET_SIGNAL_TERMS) else 0.0


def score_cluster(
    cluster: dict[str, Any],
    articles: list[Article],
    *,
    window_hours: int,
    weights: ScoreWeights = DEFAULT_WEIGHTS,
) -> ScoreBreakdown:
    rows = [articles[i] for i in cluster["indices"]]
    raw_sources = {a.source_key for a in rows}
    normalized_sources = {a.normalized_source_key for a in rows}
    raw_source_count = len(raw_sources)
    normalized_source_count = len(normalized_sources)
    newest = max(
        (
            _parse_datetime(a.published_at) or _parse_datetime(a.scraped_at)
            for a in rows
        ),
        default=None,
    )
    now = datetime.now(UTC)
    newest_age_minutes = (
        ((now - newest).total_seconds() / 60) if newest else window_hours * 60
    )
    source_diversity_norm = min(1.0, normalized_source_count / 5)
    article_count_norm = min(1.0, math.log1p(len(rows)) / math.log(10))
    recency_norm = min(
        1.0,
        max(0.0, 1.0 - newest_age_minutes / max(window_hours * 60, 1)),
    )
    topic_relevance = _topic_relevance(rows)
    flags_by_article = [classify_title(a) for a in rows]
    yahoo_noise_count = sum(f.is_yahoo_personal_finance for f in flags_by_article)
    regular_report_count = sum(f.is_regular_report for f in flags_by_article)
    market_signal_count = sum(f.has_market_signal for f in flags_by_article)
    noise_penalty = 0.10 * min(4, yahoo_noise_count)
    if yahoo_noise_count and market_signal_count:
        noise_penalty *= 0.25
    regular_report_penalty = 0.15 * min(3, regular_report_count)
    duplicate_source_penalty = (
        max(
            0.0,
            1.0 - normalized_source_count / max(1, raw_source_count),
        )
        * 0.30
    )
    penalties = {
        "noise": min(0.40, noise_penalty),
        "regular_report": min(0.45, regular_report_penalty),
        "duplicate_source": min(0.30, duplicate_source_penalty),
    }
    components = {
        "source_diversity_norm": source_diversity_norm,
        "article_count_norm": article_count_norm,
        "recency_norm": recency_norm,
        "topic_relevance": topic_relevance,
    }
    weighted = {
        "source_diversity": source_diversity_norm * weights.diversity,
        "article_count": article_count_norm * weights.volume,
        "recency": recency_norm * weights.recency,
        "topic_relevance": topic_relevance * weights.relevance,
    }
    score = max(0.0, sum(weighted.values()) - sum(penalties.values()))
    return ScoreBreakdown(
        score=_round_score(score),
        components={k: _round_score(v) for k, v in components.items()},
        weighted={k: _round_score(v) for k, v in weighted.items()},
        penalties={k: _round_score(v) for k, v in penalties.items()},
        raw_source_count=raw_source_count,
        normalized_source_count=normalized_source_count,
        flags={
            "yahoo_personal_finance": int(yahoo_noise_count),
            "regular_report": int(regular_report_count),
            "market_signal": int(market_signal_count),
        },
    )


def rank_clusters_v2(
    clusters: list[dict[str, Any]],
    articles: list[Article],
    *,
    window_hours: int,
    weights: ScoreWeights = DEFAULT_WEIGHTS,
) -> list[tuple[dict[str, Any], ScoreBreakdown]]:
    ranked = [
        (
            cluster,
            score_cluster(
                cluster, articles, window_hours=window_hours, weights=weights
            ),
        )
        for cluster in clusters
    ]
    return sorted(
        ranked,
        key=lambda item: (
            item[1].score,
            item[1].normalized_source_count,
            len(item[0]["indices"]),
            float(item[0].get("best_similarity", 0.0)),
        ),
        reverse=True,
    )


def _breakdown_payload(score_breakdown: ScoreBreakdown) -> dict[str, Any]:
    return {
        "score": score_breakdown.score,
        "score_components": score_breakdown.components,
        "score_weighted": score_breakdown.weighted,
        "score_penalties": score_breakdown.penalties,
        "raw_source_count": score_breakdown.raw_source_count,
        "normalized_source_count": score_breakdown.normalized_source_count,
        "flags": score_breakdown.flags,
    }


def infer_topic(articles: list[Article]) -> tuple[str, str, list[str]]:
    text_blob = "\n".join([a.title + " " + (a.summary or "") for a in articles]).lower()
    matched: list[str] = []
    for title, subtitle, keys in TOPIC_RULES:
        hits = [k for k in keys if keyword_matches(text_blob, k)]
        if hits:
            matched.extend(hits[:3])
            return title, subtitle, sorted(set(matched))
    counts = Counter(tokenize(text_blob))
    top_tokens = [t for t, _ in counts.most_common(5)]
    if not top_tokens:
        return "주요 시장 이슈", "여러 출처에서 반복 언급", []
    if re.search(r"[가-힣]", top_tokens[0]):
        title = " ".join(top_tokens[:2])
    else:
        title = f"{top_tokens[0].upper()} 이슈 부각"
    subtitle = " · ".join(top_tokens[1:4]) if len(top_tokens) > 1 else "시장 언급 증가"
    return title[:32], subtitle[:40], top_tokens


def infer_direction(articles: list[Article]) -> str:
    blob = " ".join([a.title + " " + (a.summary or "") for a in articles]).lower()
    pos = sum(1 for t in POSITIVE_TERMS if t.lower() in blob)
    neg = sum(1 for t in NEGATIVE_TERMS if t.lower() in blob)
    if pos > neg:
        return "up"
    if neg > pos:
        return "down"
    return "neutral"


def summarize_cluster(
    cluster: dict[str, Any],
    articles: list[Article],
    rank: int,
    score_breakdown: ScoreBreakdown | None = None,
) -> dict[str, Any]:
    rows = [articles[i] for i in cluster["indices"]]
    source_counts = Counter(a.source_key for a in rows)
    normalized_source_counts = Counter(a.normalized_source_key for a in rows)
    markets = sorted({a.market for a in rows})
    related_symbols = []
    seen = set()
    for a in rows:
        if a.stock_symbol and a.stock_symbol not in seen:
            related_symbols.append({"symbol": a.stock_symbol, "name": a.stock_name})
            seen.add(a.stock_symbol)
    title, subtitle, topics = infer_topic(rows)
    direction = infer_direction(rows)
    representative = sorted(source_counts, key=lambda k: (-source_counts[k], k))[:5]
    representative_articles = [
        {
            "id": a.id,
            "title": a.title,
            "source": a.source,
            "feed_source": a.feed_source,
            "market": a.market,
            "stock_symbol": a.stock_symbol,
            "published_at": a.published_at,
            "scraped_at": a.scraped_at,
        }
        for a in rows[:8]
    ]
    issue_key_raw = "|".join([title, subtitle, ",".join(str(a.id) for a in rows[:8])])
    if score_breakdown is None:
        score_breakdown = score_cluster(
            cluster,
            articles,
            window_hours=24,
            weights=DEFAULT_WEIGHTS,
        )
    return {
        "rank": rank,
        "cluster_key": hashlib.sha1(issue_key_raw.encode()).hexdigest()[:16],
        "title_ko": title,
        "subtitle_ko": subtitle,
        "direction": direction,
        "article_count": len(rows),
        "source_count": score_breakdown.normalized_source_count,
        "representative_sources": representative,
        "source_counts": {
            "raw": dict(source_counts),
            "normalized": dict(normalized_source_counts),
        },
        "raw_source_count": score_breakdown.raw_source_count,
        "normalized_source_count": score_breakdown.normalized_source_count,
        "score": score_breakdown.score,
        "score_components": score_breakdown.components,
        "score_weighted": score_breakdown.weighted,
        "score_penalties": score_breakdown.penalties,
        "flags": score_breakdown.flags,
        "markets": markets,
        "related_symbols": related_symbols[:10],
        "topics": topics[:8],
        "cluster_similarity": round(float(cluster.get("best_similarity", 0.0)), 4),
        "representative_articles": representative_articles,
    }


def rank_clusters(
    clusters: list[dict[str, Any]], articles: list[Article]
) -> list[dict[str, Any]]:
    def score(cluster: dict[str, Any]) -> tuple[int, int, float]:
        rows = [articles[i] for i in cluster["indices"]]
        source_count = len({a.source_key for a in rows})
        return (source_count, len(rows), float(cluster.get("best_similarity", 0.0)))

    return sorted(clusters, key=score, reverse=True)


def render_markdown(payload: dict[str, Any]) -> str:
    meta = payload["run"]
    lines = [
        "# News Issue Lab PoC",
        "",
        f"- run_uuid: `{meta['run_uuid']}`",
        f"- market: `{meta['market']}` / window: {meta['window_hours']}h / articles: {meta['article_count']} / clusters: {meta['cluster_count']}",
        f"- embedding: `{meta['embedding_model']}` ({meta['embedding_dim']}d) / threshold: {meta['threshold']}",
        "- note: 기사 본문은 출력/저장하지 않고 제목·요약·메타데이터 기반으로만 실험했습니다.",
        "",
        "## 실시간 이슈 후보",
        "",
    ]
    arrow = {"up": "▲", "down": "▼", "neutral": "◆"}
    for issue in payload["issues"]:
        lines.extend(
            [
                f"### {issue['rank']}. {arrow.get(issue['direction'], '◆')} {issue['title_ko']}",
                f"- 부제: {issue['subtitle_ko']}",
                f"- 출처/기사: raw {issue.get('raw_source_count', issue['source_count'])}개 → normalized {issue.get('normalized_source_count', issue['source_count'])}개 · {issue['article_count']}개 기사",
                f"- 점수: {issue.get('score', 0):.4f} / components={issue.get('score_components', {})} / penalties={issue.get('score_penalties', {})}",
                f"- 대표 출처: {', '.join(issue['representative_sources']) or '-'}",
                f"- 시장: {', '.join(issue['markets'])}",
                f"- 토픽: {', '.join(issue['topics']) or '-'}",
            ]
        )
        if issue["related_symbols"]:
            lines.append(
                "- 관련 종목: "
                + ", ".join(
                    filter(
                        None,
                        [
                            s.get("name") or s.get("symbol")
                            for s in issue["related_symbols"]
                        ],
                    )
                )
            )
        lines.append("- 대표 기사:")
        for article in issue["representative_articles"][:4]:
            src = article.get("feed_source") or article.get("source") or "unknown"
            lines.append(f"  - [{src}] {article['title']}")
        lines.append("")
    if payload.get("v1_vs_v2"):
        lines.append(
            render_comparison_markdown(
                payload, [], [], top=meta.get("top", len(payload["issues"]))
            ).rstrip()
        )
    return "\n".join(lines).rstrip() + "\n"


def _dominant_penalty(issue: dict[str, Any]) -> str:
    penalties = issue.get("score_penalties") or {}
    if not penalties:
        return "-"
    key, value = max(penalties.items(), key=lambda item: item[1])
    return f"{key}={value:.4f}"


def _comparison_payload(
    ranked_v1: list[dict[str, Any]],
    ranked_v2: list[tuple[dict[str, Any], ScoreBreakdown]],
    articles: list[Article],
    *,
    top: int,
) -> dict[str, Any]:
    v1_issues = [
        summarize_cluster(cluster, articles, rank=i + 1)
        for i, cluster in enumerate(ranked_v1)
    ]
    v2_issues = [
        summarize_cluster(cluster, articles, rank=i + 1, score_breakdown=breakdown)
        for i, (cluster, breakdown) in enumerate(ranked_v2)
    ]
    v1_by_key = {issue["cluster_key"]: issue for issue in v1_issues}
    v2_by_key = {issue["cluster_key"]: issue for issue in v2_issues}
    keys = list(
        dict.fromkeys(
            [i["cluster_key"] for i in v1_issues[:top]]
            + [i["cluster_key"] for i in v2_issues[:top]]
        )
    )
    side_by_side = []
    for key in keys:
        v1 = v1_by_key.get(key)
        v2 = v2_by_key.get(key)
        side_by_side.append(
            {
                "cluster_key": key,
                "title_ko": (v2 or v1 or {}).get("title_ko", "-"),
                "rank_v1": v1.get("rank") if v1 else None,
                "rank_v2": v2.get("rank") if v2 else None,
                "delta": (v1.get("rank") - v2.get("rank")) if v1 and v2 else None,
            }
        )
    downranked = []
    for issue in v1_issues[:top]:
        v2 = v2_by_key.get(issue["cluster_key"])
        if not v2 or v2["rank"] > top:
            downranked.append(
                {
                    "cluster_key": issue["cluster_key"],
                    "title_ko": issue["title_ko"],
                    "rank_v1": issue["rank"],
                    "rank_v2": v2.get("rank") if v2 else None,
                    "dominant_penalty": _dominant_penalty(v2 or issue),
                    "representative_titles": [
                        a["title"] for a in issue["representative_articles"][:3]
                    ],
                }
            )
    promoted = []
    for issue in v2_issues[:top]:
        v1 = v1_by_key.get(issue["cluster_key"])
        if not v1 or v1["rank"] > top:
            promoted.append(
                {
                    "cluster_key": issue["cluster_key"],
                    "title_ko": issue["title_ko"],
                    "rank_v1": v1.get("rank") if v1 else None,
                    "rank_v2": issue["rank"],
                    "score": issue["score"],
                    "score_components": issue["score_components"],
                    "representative_sources": issue["representative_sources"],
                }
            )
    return {
        "side_by_side": side_by_side,
        "downranked_or_excluded": downranked,
        "promoted": promoted,
        "v2_top_diagnostics": [
            {
                "rank": issue["rank"],
                "cluster_key": issue["cluster_key"],
                "title_ko": issue["title_ko"],
                "score": issue["score"],
                "score_components": issue["score_components"],
                "score_penalties": issue["score_penalties"],
                "raw_source_count": issue["raw_source_count"],
                "normalized_source_count": issue["normalized_source_count"],
                "article_count": issue["article_count"],
            }
            for issue in v2_issues[:top]
        ],
    }


def render_comparison_markdown(
    payload_v2: dict[str, Any],
    ranked_v1: list[dict[str, Any]],
    ranked_v2: list[tuple[dict[str, Any], ScoreBreakdown]],
    *,
    top: int,
) -> str:
    comparison = payload_v2.get("v1_vs_v2") or {
        "side_by_side": [],
        "downranked_or_excluded": [],
        "promoted": [],
        "v2_top_diagnostics": [],
    }
    lines = [
        "",
        "## v1 vs v2 comparison",
        "",
        "### Top-N rank table",
        "",
        "| cluster | title | rank_v1 | rank_v2 | delta |",
        "|---|---|---:|---:|---:|",
    ]
    for row in comparison["side_by_side"][: top * 2]:
        lines.append(
            f"| `{row['cluster_key']}` | {row['title_ko']} | {row.get('rank_v1') or '-'} | {row.get('rank_v2') or '-'} | {row.get('delta') if row.get('delta') is not None else '-'} |"
        )
    lines.extend(["", "### Downranked/excluded clusters", ""])
    for row in comparison["downranked_or_excluded"][:top]:
        titles = "; ".join(row.get("representative_titles") or [])
        lines.append(
            f"- `{row['cluster_key']}` {row['title_ko']} (v1={row.get('rank_v1')}, v2={row.get('rank_v2') or '-'}) dominant_penalty={row.get('dominant_penalty', '-')} titles={titles}"
        )
    if not comparison["downranked_or_excluded"]:
        lines.append("- none")
    lines.extend(["", "### Promoted clusters", ""])
    for row in comparison["promoted"][:top]:
        lines.append(
            f"- `{row['cluster_key']}` {row['title_ko']} (v1={row.get('rank_v1') or '-'}, v2={row.get('rank_v2')}) score={row.get('score')} components={row.get('score_components')} sources={', '.join(row.get('representative_sources') or [])}"
        )
    if not comparison["promoted"]:
        lines.append("- none")
    lines.extend(
        [
            "",
            "### V2 top-N diagnostics",
            "",
            "| rank | score | div | vol | rec | rel | noise | regular | duplicate | raw | normalized | articles |",
            "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in comparison["v2_top_diagnostics"][:top]:
        comp = row.get("score_components") or {}
        penalties = row.get("score_penalties") or {}
        lines.append(
            f"| {row['rank']} | {row['score']:.4f} | {comp.get('source_diversity_norm', 0):.4f} | {comp.get('article_count_norm', 0):.4f} | {comp.get('recency_norm', 0):.4f} | {comp.get('topic_relevance', 0):.4f} | {penalties.get('noise', 0):.4f} | {penalties.get('regular_report', 0):.4f} | {penalties.get('duplicate_source', 0):.4f} | {row.get('raw_source_count')} | {row.get('normalized_source_count')} | {row.get('article_count')} |"
        )
    return "\n".join(lines).rstrip() + "\n"


async def fetch_articles(market: str, window_hours: int, limit: int) -> list[Article]:
    market_clause = "" if market == "all" else "and market = :market"
    stmt = text(
        f"""
        select id, title, summary, market, feed_source, source, stock_symbol, stock_name,
               article_published_at, scraped_at
        from news_articles
        where scraped_at >= now() - (:window_hours * interval '1 hour')
          and title is not null and title <> ''
          {market_clause}
        order by scraped_at desc, article_published_at desc nulls last
        limit :limit
        """
    )
    params = {"market": market, "window_hours": window_hours, "limit": limit}
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(stmt, params)).mappings().all()
    articles: list[Article] = []
    for row in rows:
        articles.append(
            Article(
                id=row["id"],
                title=normalize_text(row["title"]),
                summary=normalize_text(row["summary"]) if row["summary"] else None,
                market=row["market"] or "unknown",
                feed_source=row["feed_source"],
                source=row["source"],
                stock_symbol=row["stock_symbol"],
                stock_name=row["stock_name"],
                published_at=row["article_published_at"].isoformat()
                if row["article_published_at"]
                else None,
                scraped_at=row["scraped_at"].isoformat() if row["scraped_at"] else None,
            )
        )
    return articles


async def ensure_lab_tables() -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(
            text(
                """
                create table if not exists news_issue_lab_runs (
                    run_uuid text primary key,
                    created_at timestamptz not null default now(),
                    market text not null,
                    window_hours integer not null,
                    article_limit integer not null,
                    cluster_threshold double precision not null,
                    embedding_model text not null,
                    embedding_dim integer not null,
                    article_count integer not null,
                    cluster_count integer not null,
                    source_counts jsonb not null,
                    payload jsonb not null
                )
                """
            )
        )
        await db.execute(
            text(
                """
                create table if not exists news_issue_lab_results (
                    id bigserial primary key,
                    run_uuid text not null references news_issue_lab_runs(run_uuid) on delete cascade,
                    rank integer not null,
                    cluster_key text not null,
                    title_ko text not null,
                    subtitle_ko text,
                    direction text not null,
                    article_count integer not null,
                    source_count integer not null,
                    representative_sources jsonb not null,
                    related_symbols jsonb not null,
                    topics jsonb not null,
                    payload jsonb not null,
                    created_at timestamptz not null default now(),
                    unique (run_uuid, rank)
                )
                """
            )
        )
        await db.commit()


async def store_payload(payload: dict[str, Any]) -> None:
    await ensure_lab_tables()
    run = payload["run"]
    async with AsyncSessionLocal() as db:
        await db.execute(
            text(
                """
                insert into news_issue_lab_runs (
                    run_uuid, market, window_hours, article_limit, cluster_threshold,
                    embedding_model, embedding_dim, article_count, cluster_count,
                    source_counts, payload
                ) values (
                    :run_uuid, :market, :window_hours, :article_limit, :cluster_threshold,
                    :embedding_model, :embedding_dim, :article_count, :cluster_count,
                    cast(:source_counts as jsonb), cast(:payload as jsonb)
                )
                on conflict (run_uuid) do nothing
                """
            ),
            {
                "run_uuid": run["run_uuid"],
                "market": run["market"],
                "window_hours": run["window_hours"],
                "article_limit": run["article_limit"],
                "cluster_threshold": run["threshold"],
                "embedding_model": run["embedding_model"],
                "embedding_dim": run["embedding_dim"],
                "article_count": run["article_count"],
                "cluster_count": run["cluster_count"],
                "source_counts": json.dumps(
                    payload["source_counts"], ensure_ascii=False
                ),
                "payload": json.dumps(payload, ensure_ascii=False),
            },
        )
        for issue in payload["issues"]:
            await db.execute(
                text(
                    """
                    insert into news_issue_lab_results (
                        run_uuid, rank, cluster_key, title_ko, subtitle_ko, direction,
                        article_count, source_count, representative_sources, related_symbols,
                        topics, payload
                    ) values (
                        :run_uuid, :rank, :cluster_key, :title_ko, :subtitle_ko, :direction,
                        :article_count, :source_count, cast(:representative_sources as jsonb),
                        cast(:related_symbols as jsonb), cast(:topics as jsonb), cast(:payload as jsonb)
                    )
                    """
                ),
                {
                    "run_uuid": run["run_uuid"],
                    "rank": issue["rank"],
                    "cluster_key": issue["cluster_key"],
                    "title_ko": issue["title_ko"],
                    "subtitle_ko": issue["subtitle_ko"],
                    "direction": issue["direction"],
                    "article_count": issue["article_count"],
                    "source_count": issue["source_count"],
                    "representative_sources": json.dumps(
                        issue["representative_sources"], ensure_ascii=False
                    ),
                    "related_symbols": json.dumps(
                        issue["related_symbols"], ensure_ascii=False
                    ),
                    "topics": json.dumps(issue["topics"], ensure_ascii=False),
                    "payload": json.dumps(issue, ensure_ascii=False),
                },
            )
        await db.commit()


async def build_payload(args: argparse.Namespace) -> dict[str, Any]:
    weights = parse_weights(getattr(args, "weights", None))
    articles = await fetch_articles(args.market, args.window_hours, args.limit)
    if not articles:
        raise RuntimeError("no news_articles rows matched the requested window/market")
    vectors: list[list[float]] = []
    for i in range(0, len(articles), args.batch_size):
        batch = articles[i : i + args.batch_size]
        vectors.extend(
            embed_batch(
                args.embedding_endpoint,
                args.embedding_model,
                [a.text_for_embedding for a in batch],
            )
        )
    embedding_dim = len(vectors[0]) if vectors else 0
    clusters = cluster_articles(articles, vectors, args.threshold)
    ranked_v1 = rank_clusters(clusters, articles)
    ranked_v2 = rank_clusters_v2(
        clusters,
        articles,
        window_hours=args.window_hours,
        weights=weights,
    )
    if getattr(args, "drop_regular_reports", False):
        ranked_v2 = [
            (cluster, breakdown)
            for cluster, breakdown in ranked_v2
            if breakdown.flags.get("regular_report", 0)
            / max(1, len(cluster["indices"]))
            < 0.5
        ]
    issues = [
        summarize_cluster(cluster, articles, rank=i + 1, score_breakdown=breakdown)
        for i, (cluster, breakdown) in enumerate(ranked_v2[: args.top])
    ]
    raw_source_counts = Counter(a.source_key for a in articles)
    normalized_source_counts = Counter(a.normalized_source_key for a in articles)
    payload = {
        "run": {
            "run_uuid": str(uuid.uuid4()),
            "created_at": datetime.now(UTC).isoformat(),
            "market": args.market,
            "window_hours": args.window_hours,
            "article_limit": args.limit,
            "top": args.top,
            "article_count": len(articles),
            "cluster_count": len(clusters),
            "threshold": args.threshold,
            "dedupe_threshold": args.dedupe_threshold,
            "embedding_model": args.embedding_model,
            "embedding_dim": embedding_dim,
            "score_weights": {
                "diversity": weights.diversity,
                "volume": weights.volume,
                "recency": weights.recency,
                "relevance": weights.relevance,
            },
            "drop_regular_reports": bool(getattr(args, "drop_regular_reports", False)),
        },
        "source_counts": {
            "raw": dict(raw_source_counts),
            "normalized": dict(normalized_source_counts),
        },
        "issues": issues,
    }
    if getattr(args, "compare_v1", False):
        payload["v1_vs_v2"] = _comparison_payload(
            ranked_v1,
            ranked_v2,
            articles,
            top=args.top,
        )
    return payload


async def async_main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    payload = await build_payload(args)
    if args.store:
        await store_payload(payload)
    rendered = (
        json.dumps(payload, ensure_ascii=False, indent=2)
        if args.format == "json"
        else render_markdown(payload)
    )
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(rendered)
    else:
        print(rendered)
    return 0


def main() -> None:
    try:
        raise SystemExit(asyncio.run(async_main()))
    except BrokenPipeError:
        raise SystemExit(0)
    except Exception as exc:  # pragma: no cover - operator-facing CLI final guard
        print(f"news_issue_lab failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()

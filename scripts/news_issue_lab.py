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
    return parser.parse_args(argv)


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
        return keyword_l in text_blob
    # Avoid substring false positives such as "ai" in "daily" or "ether" in "together".
    return bool(
        re.search(rf"(?<![a-z0-9]){re.escape(keyword_l)}(?![a-z0-9])", text_blob)
    )


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
    cluster: dict[str, Any], articles: list[Article], rank: int
) -> dict[str, Any]:
    rows = [articles[i] for i in cluster["indices"]]
    source_counts = Counter(a.source_key for a in rows)
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
    return {
        "rank": rank,
        "cluster_key": hashlib.sha1(issue_key_raw.encode()).hexdigest()[:16],
        "title_ko": title,
        "subtitle_ko": subtitle,
        "direction": direction,
        "article_count": len(rows),
        "source_count": len(source_counts),
        "representative_sources": representative,
        "source_counts": dict(source_counts),
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
                f"- 출처/기사: {issue['source_count']}개 출처 · {issue['article_count']}개 기사",
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
    ranked = rank_clusters(clusters, articles)
    issues = [
        summarize_cluster(cluster, articles, rank=i + 1)
        for i, cluster in enumerate(ranked[: args.top])
    ]
    source_counts = Counter(a.source_key for a in articles)
    return {
        "run": {
            "run_uuid": str(uuid.uuid4()),
            "created_at": datetime.now(UTC).isoformat(),
            "market": args.market,
            "window_hours": args.window_hours,
            "article_limit": args.limit,
            "article_count": len(articles),
            "cluster_count": len(clusters),
            "threshold": args.threshold,
            "dedupe_threshold": args.dedupe_threshold,
            "embedding_model": args.embedding_model,
            "embedding_dim": embedding_dim,
        },
        "source_counts": dict(source_counts),
        "issues": issues,
    }


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

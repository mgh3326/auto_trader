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
import os
import re
import sys
import time
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib import error, request

from sqlalchemy import text

from app.core.db import AsyncSessionLocal

DEFAULT_BGE_ENDPOINT = "http://127.0.0.1:10631/v1/embeddings"
DEFAULT_BGE_MODEL = "BAAI/bge-m3"

DEFAULT_LLM_TIMEOUT = 30
RENDER_PROMPT_VERSION = "rob136-v1"
RENDER_MAX_ARTICLES = 6
RENDER_SUMMARY_EXCERPT_MAX = 200
RENDER_MAX_IMPACT_POINTS = 4

BANNED_RENDER_PHRASES: tuple[str, ...] = (
    "매수",
    "매도",
    "추천",
    "지금 사",
    "지금 팔",
    "목표가",
    "투자의견",
    "사야",
    "팔아야",
    "buy now",
    "sell now",
    "recommend",
    "target price",
    "price target",
    "should buy",
    "should sell",
)

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
    "macro snapshot",
    "econ check-up",
    "fx check-up",
    "economy monitor",
    "eps live",
    "review",
    "실적 정리",
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
        "--quality-top",
        type=int,
        default=5,
        help="top-N window used for deterministic ROB-145 quality gate diagnostics",
    )
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
    parser.add_argument(
        "--embedding-api-key",
        default=os.getenv("EMBEDDING_API_KEY"),
        help="optional bearer token for secured OpenAI-compatible embedding endpoints; defaults to EMBEDDING_API_KEY",
    )
    parser.add_argument("--batch-size", type=int, default=16)
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
    parser.add_argument(
        "--merge-clusters",
        dest="merge_clusters",
        action="store_true",
        default=True,
        help="merge near-duplicate clusters before ranking (default: on)",
    )
    parser.add_argument(
        "--no-merge-clusters",
        dest="merge_clusters",
        action="store_false",
        help="disable the cluster merge pass (preserve ROB-134 behavior)",
    )
    parser.add_argument(
        "--merge-rep-threshold",
        type=float,
        default=MERGE_REP_THRESHOLD,
        help="cosine similarity threshold for cluster representative merge",
    )
    parser.add_argument(
        "--merge-token-jaccard",
        type=float,
        default=MERGE_TOKEN_JACCARD,
        help="token-Jaccard threshold required alongside rep-sim merge",
    )
    parser.add_argument(
        "--merge-rep-articles",
        type=int,
        default=3,
        help="articles per cluster used to build the representative text",
    )
    # ROB-136: Korean LLM rendering flags
    parser.add_argument(
        "--llm-render",
        dest="llm_render",
        action="store_true",
        default=False,
        help="enable Korean LLM rendering (ROB-136)",
    )
    parser.add_argument(
        "--no-llm",
        dest="llm_render",
        action="store_false",
        help="disable LLM rendering (default behavior)",
    )
    parser.add_argument(
        "--llm-endpoint",
        default=None,
        help="OpenAI-compatible LLM base URL (required when --llm-render)",
    )
    parser.add_argument(
        "--llm-model",
        default=None,
        help="LLM model name (required when --llm-render)",
    )
    parser.add_argument(
        "--llm-timeout",
        type=int,
        default=DEFAULT_LLM_TIMEOUT,
        help="LLM request timeout in seconds [1..120]",
    )
    parser.add_argument(
        "--llm-max-render",
        type=int,
        default=None,
        help="maximum number of issues to LLM-render (default: all top-N)",
    )
    parser.add_argument(
        "--llm-prompt-version",
        default=RENDER_PROMPT_VERSION,
        help="prompt version label stored in diagnostics",
    )
    args = parser.parse_args(argv)
    for name in ("window_hours", "limit", "top", "quality_top", "batch_size"):
        if getattr(args, name) <= 0:
            parser.error(f"--{name.replace('_', '-')} must be positive")
    if not (0.0 < args.merge_rep_threshold <= 1.0):
        parser.error("--merge-rep-threshold must be in (0.0, 1.0]")
    if not (0.0 <= args.merge_token_jaccard <= 1.0):
        parser.error("--merge-token-jaccard must be in [0.0, 1.0]")
    if args.merge_rep_articles <= 0:
        parser.error("--merge-rep-articles must be positive")
    if args.llm_render and not args.llm_endpoint:
        parser.error("--llm-render requires --llm-endpoint")
    if args.llm_render and not args.llm_model:
        parser.error("--llm-render requires --llm-model")
    if not (1 <= args.llm_timeout <= 120):
        parser.error("--llm-timeout must be in [1, 120]")
    if args.llm_max_render is not None and args.llm_max_render <= 0:
        parser.error("--llm-max-render must be positive")
    if args.llm_max_render is not None and args.llm_max_render > args.top:
        parser.error("--llm-max-render must be <= --top")
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
    endpoint: str,
    model: str,
    texts: list[str],
    timeout: int = 180,
    api_key: str | None = None,
) -> list[list[float]]:
    payload = {"model": model, "input": texts}
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = request.Request(
        endpoint,
        data=json.dumps(payload).encode(),
        headers=headers,
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


# ---------------------------------------------------------------------------
# ROB-136: Korean LLM rendering infrastructure
# ---------------------------------------------------------------------------


class LLMRenderError(Exception):
    """Raised by LLM providers when a render call cannot be completed.

    The ``reason`` attribute names the rejection category for diagnostics.
    """

    def __init__(self, reason: str, message: str = "") -> None:
        super().__init__(message or reason)
        self.reason = reason


class NullLLMRenderProvider:
    """Default provider — never calls the network; always signals llm_disabled."""

    def render(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        model: str | None = None,
        timeout: int = DEFAULT_LLM_TIMEOUT,
    ) -> str:
        raise LLMRenderError("llm_disabled", "LLM rendering is disabled")


class OpenAICompatibleLLMRenderProvider:
    """POSTs to /v1/chat/completions; returns choices[0].message.content."""

    def __init__(self, endpoint: str) -> None:
        if not endpoint.endswith("/v1/chat/completions"):
            endpoint = endpoint.rstrip("/") + "/v1/chat/completions"
        self._endpoint = endpoint

    def render(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        model: str | None = None,
        timeout: int = DEFAULT_LLM_TIMEOUT,
    ) -> str:
        payload = {
            "model": model or "default",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.2,
        }
        req = request.Request(
            self._endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode(),
            headers={
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with request.urlopen(req, timeout) as resp:
                data = json.load(resp)
        except (
            error.URLError,
            TimeoutError,
            OSError,
            json.JSONDecodeError,
            RuntimeError,
        ) as exc:
            # Keep provider diagnostics secret-safe: never surface request headers,
            # tokens, cookies, or endpoint-provided detail strings from exceptions.
            raise LLMRenderError("http_error") from exc
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMRenderError("response_parse_error", str(exc)) from exc


def make_llm_provider(
    args: Any,
) -> NullLLMRenderProvider | OpenAICompatibleLLMRenderProvider:
    if getattr(args, "llm_render", False) and getattr(args, "llm_endpoint", None):
        return OpenAICompatibleLLMRenderProvider(args.llm_endpoint)
    return NullLLMRenderProvider()


_LLM_SYSTEM_PROMPT = """You are a Korean financial-news issue-card renderer.
Return only a JSON object — no markdown fences, no extra text.
Write short Toss-like Korean issue-card copy based only on the provided cluster metadata.
Do not add facts, prices, forecasts, or recommendations not present in the input.
Never use investment advice or recommendation language."""


def build_render_prompt(issue: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    """Return (system_prompt, user_json_string, input_dict)."""
    articles_raw = issue.get("representative_articles") or []
    rep_articles = []
    for a in articles_raw[:RENDER_MAX_ARTICLES]:
        excerpt: str | None = None
        if a.get("summary"):
            excerpt = str(a["summary"])[:RENDER_SUMMARY_EXCERPT_MAX]
        rep_articles.append(
            {
                "title": a.get("title", ""),
                "source": a.get("source"),
                "feed_source": a.get("feed_source"),
                "market": a.get("market"),
                "published_at": a.get("published_at"),
                "scraped_at": a.get("scraped_at"),
                **({"summary_excerpt": excerpt} if excerpt else {}),
            }
        )

    input_dict: dict[str, Any] = {
        "rule_based_card": {
            "title_ko": issue.get("title_ko", ""),
            "subtitle_ko": issue.get("subtitle_ko", ""),
            "direction": issue.get("direction", "neutral"),
            "topics": issue.get("topics") or [],
            "markets": issue.get("markets") or [],
        },
        "stats": {
            "rank": issue.get("rank", 0),
            "article_count": issue.get("article_count", 0),
            "raw_source_count": issue.get("raw_source_count", 0),
            "normalized_source_count": issue.get("normalized_source_count", 0),
            "score": issue.get("score", 0.0),
            "score_components": issue.get("score_components") or {},
            "score_penalties": issue.get("score_penalties") or {},
            "flags": issue.get("flags") or {},
            "merge_member_count": issue.get("merge_member_count", 1),
        },
        "related_symbols": issue.get("related_symbols") or [],
        "representative_articles": rep_articles,
    }
    user_prompt = json.dumps(input_dict, ensure_ascii=False, indent=2)
    return _LLM_SYSTEM_PROMPT, user_prompt, input_dict


def compute_render_input_hash(input_dict: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(input_dict, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()[:32]


def _hangul_ratio(text: str) -> float:
    if not text:
        return 0.0
    hangul_count = sum(1 for ch in text if "가" <= ch <= "힣")
    return hangul_count / len(text)


def validate_render_response(
    raw: str,
    *,
    allowed_symbols: set[str],
) -> dict[str, Any]:
    """Parse and validate an LLM render response; raise ValueError on any rejection."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("parse_error") from exc

    if not isinstance(data, dict):
        raise ValueError("parse_error")

    required_fields = {
        "title_ko": str,
        "subtitle_ko": str,
        "direction": str,
        "summary_ko": str,
        "impact_points": list,
        "related_symbols": list,
        "confidence": (int, float),
    }
    for field, expected_type in required_fields.items():
        if field not in data:
            raise ValueError("schema_missing_field")
        if not isinstance(data[field], expected_type):
            raise ValueError("schema_type_error")

    for field in ("title_ko", "subtitle_ko", "summary_ko"):
        if not data[field].strip():
            raise ValueError("empty_field")

    if data["direction"] not in ("up", "down", "neutral"):
        raise ValueError("invalid_direction")

    confidence = float(data["confidence"])
    if not (0.0 <= confidence <= 1.0):
        raise ValueError("invalid_confidence")

    if len(data["title_ko"]) > 32:
        raise ValueError("length_violation")
    if len(data["subtitle_ko"]) > 60:
        raise ValueError("length_violation")
    if not (80 <= len(data["summary_ko"]) <= 280):
        raise ValueError("length_violation")

    impact_points = data["impact_points"]
    if not impact_points or len(impact_points) > RENDER_MAX_IMPACT_POINTS:
        raise ValueError("schema_cardinality_error")
    for pt in impact_points:
        if not isinstance(pt, str):
            raise ValueError("schema_type_error")
        if len(pt) > 120:
            raise ValueError("length_violation")

    for sym_entry in data["related_symbols"]:
        if not isinstance(sym_entry, dict):
            raise ValueError("schema_type_error")
        sym = sym_entry.get("symbol", "")
        if sym and sym not in allowed_symbols:
            raise ValueError("symbol_unknown")

    if _hangul_ratio(data["summary_ko"]) < 0.3:
        raise ValueError("script_ratio_low")

    check_text = " ".join(
        [
            data["title_ko"],
            data["subtitle_ko"],
            data["summary_ko"],
            *impact_points,
        ]
    ).lower()
    for phrase in BANNED_RENDER_PHRASES:
        if phrase.lower() in check_text:
            raise ValueError("banned_phrase")

    return data


def fallback_render(issue: dict[str, Any], rejection_reason: str) -> dict[str, Any]:
    """Return a deterministic, schema-complete card using rule-based issue fields."""
    topics = issue.get("topics") or []
    article_count = issue.get("article_count", 0)
    markets = issue.get("markets") or []
    rep_sources = issue.get("representative_sources") or []
    related_symbols = (issue.get("related_symbols") or [])[:6]
    merge_member_count = issue.get("merge_member_count", 1)

    topic_str = topics[0] if topics else "관련 이슈"
    sources_str = (
        ", ".join(str(s) for s in rep_sources[:3]) if rep_sources else "다수 출처"
    )
    market_str = "·".join(sorted({str(m) for m in markets})) if markets else "시장"

    summary_ko = (
        f"여러 출처에서 {topic_str} 관련 기사 {article_count}건이 확인됐습니다. "
        f"대표 출처는 {sources_str}이며, 시장 영향은 추가 확인이 필요합니다."
    )

    impact_points: list[str] = [
        f"{market_str} 시장에서 관련 동향이 포착됐습니다.",
    ]
    if issue.get("normalized_source_count", 0) >= 2:
        impact_points.append(
            f"정규화 출처 {issue['normalized_source_count']}개에서 보도됐습니다."
        )
    if merge_member_count > 1:
        impact_points.append(f"{merge_member_count}개 클러스터가 통합된 이슈입니다.")
    if related_symbols:
        sym_names = ", ".join(
            s.get("name") or s.get("symbol", "") for s in related_symbols[:3] if s
        )
        if sym_names:
            impact_points.append(f"관련 종목: {sym_names}")

    return {
        "title_ko": issue.get("title_ko", ""),
        "subtitle_ko": issue.get("subtitle_ko", ""),
        "direction": issue.get("direction", "neutral"),
        "summary_ko": summary_ko,
        "impact_points": impact_points[:RENDER_MAX_IMPACT_POINTS],
        "related_symbols": related_symbols,
        "confidence": 0.0,
        "render_status": "fallback",
        "render_rejection_reason": rejection_reason,
    }


def render_top_issues(
    issues: list[dict[str, Any]],
    *,
    provider: NullLLMRenderProvider | OpenAICompatibleLLMRenderProvider,
    llm_enabled: bool,
    model: str | None,
    timeout: int,
    prompt_version: str,
    max_render: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Render (or fallback) each issue card; return updated issues and diagnostics."""
    ok_count = 0
    fallback_count = 0
    skipped_count = 0
    rejection_counts: dict[str, int] = {}
    total_latency_ms = 0
    provider_name = (
        "openai_compatible"
        if isinstance(provider, OpenAICompatibleLLMRenderProvider)
        else "null"
    )

    rendered_issues: list[dict[str, Any]] = []
    for issue in issues:
        issue = dict(issue)
        rank = issue.get("rank", 0)
        allowed_symbols = {
            s.get("symbol", "")
            for s in (issue.get("related_symbols") or [])
            if s.get("symbol")
        }

        system_prompt, user_prompt, input_dict = build_render_prompt(issue)
        input_hash = compute_render_input_hash(input_dict)
        base_render_meta = {
            "render_prompt_version": prompt_version,
            "render_input_hash": input_hash,
            "render_model": model,
        }

        if not llm_enabled or rank > max_render:
            reason = "llm_disabled" if not llm_enabled else "llm_skipped"
            card = fallback_render(issue, reason)
            issue.update(card)
            issue.update(base_render_meta)
            issue["render_latency_ms"] = 0
            if rank > max_render:
                skipped_count += 1
            else:
                fallback_count += 1
            rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
            rendered_issues.append(issue)
            continue

        t0 = time.monotonic()
        try:
            raw = provider.render(
                system_prompt, user_prompt, model=model, timeout=timeout
            )
            validated = validate_render_response(raw, allowed_symbols=allowed_symbols)
            latency_ms = int((time.monotonic() - t0) * 1000)
            total_latency_ms += latency_ms
            issue.update(validated)
            issue["render_status"] = "ok"
            issue["render_rejection_reason"] = None
            issue["render_model"] = model
            issue.update(base_render_meta)
            issue["render_latency_ms"] = latency_ms
            ok_count += 1
        except (LLMRenderError, ValueError) as exc:
            latency_ms = int((time.monotonic() - t0) * 1000)
            total_latency_ms += latency_ms
            reason = exc.reason if isinstance(exc, LLMRenderError) else str(exc)
            card = fallback_render(issue, reason)
            issue.update(card)
            issue.update(base_render_meta)
            issue["render_latency_ms"] = latency_ms
            fallback_count += 1
            rejection_counts[reason] = rejection_counts.get(reason, 0) + 1

        rendered_issues.append(issue)

    render_diag: dict[str, Any] = {
        "enabled": llm_enabled,
        "provider": provider_name,
        "model": model,
        "prompt_version": prompt_version,
        "max_render": max_render,
        "requested": len(issues),
        "ok": ok_count,
        "fallback": fallback_count,
        "skipped": skipped_count,
        "rejection_counts": rejection_counts,
        "total_latency_ms": total_latency_ms,
    }
    return rendered_issues, render_diag


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


MERGE_REP_THRESHOLD = 0.86
MERGE_TOKEN_JACCARD = 0.30
MERGE_STRONG_REP_THRESHOLD = 0.93
MERGE_TOPIC_REP_THRESHOLD = 0.43
MERGE_MIN_TOKEN_FLOOR = 0.20
MERGE_MAX_CLUSTER_SIZE = 25


def build_cluster_representative(
    cluster: dict[str, Any],
    articles: list[Article],
    *,
    max_articles: int = 3,
) -> str:
    rows = [articles[i] for i in cluster["indices"]]
    chosen = sorted(
        rows,
        key=lambda a: (
            0 if a.stock_symbol else 1,
            -len(a.summary or ""),
            a.id,
        ),
    )[:max_articles]
    parts: list[str] = []
    for a in chosen:
        parts.append(a.title)
        if a.stock_symbol:
            parts.append(a.stock_symbol)
        if a.stock_name:
            parts.append(a.stock_name)
    label = cluster_topic_label(rows)
    if label:
        parts.append(label)
    markets = sorted({articles[i].market for i in cluster["indices"]})
    parts.extend(markets)
    return " | ".join(p for p in parts if p)


def _evaluate_merge_pair(
    absorber: dict[str, Any],
    absorbed: dict[str, Any],
    articles: list[Article],
    *,
    rep_sim: float,
    absorber_cid: int,
    absorbed_cid: int,
    rep_threshold: float = MERGE_REP_THRESHOLD,
    token_jaccard_threshold: float = MERGE_TOKEN_JACCARD,
) -> MergeDecision:
    abs_rows = [articles[i] for i in absorber["indices"]]
    add_rows = [articles[i] for i in absorbed["indices"]]
    abs_tokens = absorber.get("tokens") or set()
    add_tokens = absorbed.get("tokens") or set()
    if abs_tokens or add_tokens:
        token_jaccard = len(abs_tokens & add_tokens) / max(
            1, len(abs_tokens | add_tokens)
        )
    else:
        token_jaccard = 0.0
    abs_srcs = {a.normalized_source_key for a in abs_rows}
    add_srcs = {a.normalized_source_key for a in add_rows}
    if abs_srcs and add_srcs:
        source_overlap = len(abs_srcs & add_srcs) / max(
            1, min(len(abs_srcs), len(add_srcs))
        )
    else:
        source_overlap = 0.0
    abs_label = cluster_topic_label(abs_rows)
    add_label = cluster_topic_label(add_rows)
    topic_agree = bool(abs_label and abs_label == add_label)
    abs_syms = {a.stock_symbol for a in abs_rows if a.stock_symbol}
    add_syms = {a.stock_symbol for a in add_rows if a.stock_symbol}
    symbol_agree = bool(abs_syms & add_syms)
    abs_title = abs_label or (abs_rows[0].title if abs_rows else "-")
    add_title = add_label or (add_rows[0].title if add_rows else "-")

    def _build(decision: str, reason: str) -> MergeDecision:
        return MergeDecision(
            absorber_cid=absorber_cid,
            absorbed_cid=absorbed_cid,
            rep_sim=round(float(rep_sim), 4),
            token_jaccard=round(float(token_jaccard), 4),
            source_overlap=round(float(source_overlap), 4),
            topic_agree=topic_agree,
            symbol_agree=symbol_agree,
            decision=decision,
            reason=reason,
            absorber_title=abs_title,
            absorbed_title=add_title,
        )

    if len(absorber["indices"]) + len(absorbed["indices"]) > MERGE_MAX_CLUSTER_SIZE:
        return _build("rejected", "max_cluster_size")
    if not topic_agree and len(abs_srcs | add_srcs) > 5:
        return _build("rejected", "wide_source_no_topic")
    if not topic_agree and not symbol_agree and token_jaccard < MERGE_MIN_TOKEN_FLOOR:
        return _build("rejected", "below_token_floor_no_topic")
    if rep_sim >= MERGE_STRONG_REP_THRESHOLD and token_jaccard >= 0.10:
        return _build("merged", "strong_rep")
    if rep_sim >= rep_threshold and topic_agree:
        return _build("merged", "topic+rep")
    if rep_sim >= rep_threshold and symbol_agree:
        return _build("merged", "symbol+rep")
    if rep_sim >= rep_threshold and token_jaccard >= token_jaccard_threshold:
        return _build("merged", "jaccard+rep")
    if topic_agree and rep_sim >= MERGE_TOPIC_REP_THRESHOLD:
        return _build("merged", "topic+low_rep")
    if rep_sim < rep_threshold:
        return _build("rejected", "rep_sim_below_threshold")
    return _build("rejected", "no_supporting_signal")


def merge_clusters(
    clusters: list[dict[str, Any]],
    articles: list[Article],
    embedder: Callable[[list[str]], list[list[float]]] | None,
    *,
    rep_threshold: float,
    token_jaccard_threshold: float,
    rep_articles: int,
    enabled: bool = True,
) -> tuple[list[dict[str, Any]], MergeDiagnostics]:
    diag = MergeDiagnostics(
        enabled=enabled,
        merge_before_count=len(clusters),
        thresholds={
            "rep_threshold": float(rep_threshold),
            "token_jaccard_threshold": float(token_jaccard_threshold),
            "strong_rep_threshold": MERGE_STRONG_REP_THRESHOLD,
            "topic_rep_threshold": MERGE_TOPIC_REP_THRESHOLD,
            "min_token_floor": MERGE_MIN_TOKEN_FLOOR,
            "max_cluster_size": float(MERGE_MAX_CLUSTER_SIZE),
            "rep_articles": float(rep_articles),
        },
    )
    if not enabled or len(clusters) <= 1 or embedder is None:
        diag.merge_after_count = len(clusters)
        return clusters, diag

    cids = [min(articles[idx].id for idx in c["indices"]) for c in clusters]
    reps = [
        build_cluster_representative(c, articles, max_articles=rep_articles)
        for c in clusters
    ]
    rep_vectors = embedder(reps)

    order = sorted(
        range(len(clusters)),
        key=lambda i: (
            -len(clusters[i]["indices"]),
            -len({articles[k].normalized_source_key for k in clusters[i]["indices"]}),
            cids[i],
        ),
    )

    parent = list(range(len(clusters)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def component_members(root: int) -> list[int]:
        return [idx for idx in range(len(clusters)) if find(idx) == root]

    def component_cluster(root: int) -> dict[str, Any]:
        all_indices: list[int] = []
        all_vectors: list[list[float]] = []
        all_tokens: set[str] = set()
        best_sim = 0.0
        for member in component_members(root):
            all_indices.extend(clusters[member]["indices"])
            all_vectors.extend(clusters[member]["vectors"])
            all_tokens.update(clusters[member].get("tokens") or set())
            best_sim = max(
                best_sim, float(clusters[member].get("best_similarity", 0.0))
            )
        return {
            "indices": sorted(set(all_indices)),
            "vectors": all_vectors,
            "tokens": all_tokens,
            "best_similarity": best_sim,
        }

    def component_cid(root: int) -> int:
        return min(cids[member] for member in component_members(root))

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra == rb:
            return
        parent[rb] = ra

    rejected_near_misses = 0
    for pos, i in enumerate(order):
        for j in order[pos + 1 :]:
            ri, rj = find(i), find(j)
            if ri == rj:
                continue
            absorber_c, absorbed_c = component_cluster(ri), component_cluster(rj)
            sim = cosine(rep_vectors[ri], rep_vectors[rj])
            decision = _evaluate_merge_pair(
                absorber_c,
                absorbed_c,
                articles,
                rep_sim=sim,
                absorber_cid=component_cid(ri),
                absorbed_cid=component_cid(rj),
                rep_threshold=rep_threshold,
                token_jaccard_threshold=token_jaccard_threshold,
            )
            if decision.decision == "merged":
                union(ri, rj)
                diag.decisions.append(decision)
            elif sim >= rep_threshold - 0.05:
                rejected_near_misses += 1
                diag.decisions.append(decision)

    groups: dict[int, list[int]] = {}
    for i in range(len(clusters)):
        groups.setdefault(find(i), []).append(i)

    merged_clusters: list[dict[str, Any]] = []
    for _root, members in sorted(groups.items(), key=lambda kv: cids[kv[0]]):
        members_sorted = sorted(members, key=lambda i: cids[i])
        all_indices: list[int] = []
        all_vectors: list[list[float]] = []
        all_tokens: set[str] = set()
        best_sim = 0.0
        for m in members_sorted:
            all_indices.extend(clusters[m]["indices"])
            all_vectors.extend(clusters[m]["vectors"])
            all_tokens.update(clusters[m].get("tokens") or set())
            best_sim = max(best_sim, float(clusters[m].get("best_similarity", 0.0)))
        merged_clusters.append(
            {
                "indices": sorted(set(all_indices)),
                "vectors": all_vectors,
                "centroid": centroid(all_vectors),
                "tokens": all_tokens,
                "best_similarity": best_sim,
                "merged_cluster_ids": sorted(cids[m] for m in members_sorted),
            }
        )

    diag.merge_after_count = len(merged_clusters)
    diag.rejected_near_misses = rejected_near_misses
    return merged_clusters, diag


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


@dataclass(frozen=True)
class MergeDecision:
    absorber_cid: int
    absorbed_cid: int
    rep_sim: float
    token_jaccard: float
    source_overlap: float
    topic_agree: bool
    symbol_agree: bool
    decision: str  # "merged" | "rejected"
    reason: str
    absorber_title: str
    absorbed_title: str


@dataclass
class MergeDiagnostics:
    enabled: bool = False
    merge_before_count: int = 0
    merge_after_count: int = 0
    rejected_near_misses: int = 0
    thresholds: dict[str, float] = None  # type: ignore[assignment]
    decisions: list[MergeDecision] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.thresholds is None:
            self.thresholds = {}
        if self.decisions is None:
            self.decisions = []


@dataclass(frozen=True)
class QualityGateConfig:
    top_n: int = 5
    max_duplicate_title_in_top: int = 0
    max_duplicate_topic_in_top: int = 0
    max_single_article_top_count: int = 0
    max_market_mismatch_top_count: int = 0
    max_source_noise_top_count: int = 0
    crypto_equity_topic_fail: bool = True


GENERIC_EQUITY_TOPICS_FOR_CRYPTO: set[str] = {
    "미국 증시 최고치",
    "기업 실적 발표",
    "반도체 슈퍼사이클",
    "AI 데이터센터",
    "부동산·리츠",
}
CRYPTO_NATIVE_TERMS: tuple[str, ...] = (
    "bitcoin",
    "btc",
    "비트코인",
    "ethereum",
    "ether",
    "eth",
    "이더리움",
    "crypto",
    "cryptocurrency",
    "blockchain",
    "블록체인",
    "token",
    "토큰",
    "stablecoin",
    "스테이블코인",
    "coinbase",
    "coindesk",
    "cointelegraph",
    "etf",
    "defi",
    "solana",
    "솔라나",
)
SOURCE_NOISE_TERMS: tuple[str, ...] = (
    "yahoo finance",
    "naver mainnews",
    "browser_naver_mainnews",
    "rss_yahoo_finance_topstories",
    "topstories",
    "mainnews",
)


def _asdict_dataclass(obj: Any) -> dict[str, Any]:
    return {
        name: getattr(obj, name) for name in getattr(obj, "__dataclass_fields__", {})
    }


def _issue_text_blob(issue: dict[str, Any]) -> str:
    parts: list[str] = [
        str(issue.get("title_ko") or ""),
        str(issue.get("subtitle_ko") or ""),
        " ".join(str(t) for t in issue.get("topics") or []),
        " ".join(str(s) for s in issue.get("representative_sources") or []),
    ]
    for article in issue.get("representative_articles") or []:
        parts.extend(
            [
                str(article.get("title") or ""),
                str(article.get("source") or ""),
                str(article.get("feed_source") or ""),
                str(article.get("stock_symbol") or ""),
            ]
        )
    return "\n".join(parts).lower()


def issue_has_source_noise(issue: dict[str, Any]) -> bool:
    title_blob = (
        f"{issue.get('title_ko') or ''} {issue.get('subtitle_ko') or ''}".lower()
    )
    return any(term in title_blob for term in SOURCE_NOISE_TERMS)


def issue_has_crypto_native_evidence(issue: dict[str, Any]) -> bool:
    blob = _issue_text_blob(issue)
    return any(keyword_matches(blob, term) for term in CRYPTO_NATIVE_TERMS)


def issue_market_mismatch(issue: dict[str, Any], requested_market: str) -> bool:
    if requested_market == "all":
        return False
    issue_markets = set(issue.get("markets") or [])
    if issue_markets and not (
        requested_market in issue_markets or "all" in issue_markets
    ):
        return True
    if requested_market == "crypto":
        title = str(issue.get("title_ko") or "")
        topics = {str(t) for t in (issue.get("topics") or [])}
        if (
            title in GENERIC_EQUITY_TOPICS_FOR_CRYPTO
            or topics & GENERIC_EQUITY_TOPICS_FOR_CRYPTO
        ) and not issue_has_crypto_native_evidence(issue):
            return True
    return False


def _quality_finding(
    code: str,
    severity: str,
    issue: dict[str, Any] | None,
    reason: str,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"code": code, "severity": severity, "reason": reason}
    if issue:
        payload.update(
            {
                "rank": issue.get("rank"),
                "title_ko": issue.get("title_ko"),
                "cluster_key": issue.get("cluster_key"),
                "article_count": issue.get("article_count"),
                "normalized_source_count": issue.get("normalized_source_count"),
                "markets": issue.get("markets"),
                "topics": issue.get("topics"),
                "pre_suppression_rank": issue.get("pre_suppression_rank"),
                "display_suppressed_reason": issue.get("display_suppressed_reason")
                or issue.get("suppression_reason"),
            }
        )
    return payload


def suppress_duplicate_top_issues(
    issues: list[dict[str, Any]],
    *,
    top_n: int,
    requested_market: str,
    config: QualityGateConfig | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Keep visible top-N cleaner while preserving suppressed candidates for audit."""
    config = config or QualityGateConfig(top_n=top_n)
    selected: list[dict[str, Any]] = []
    suppressed: list[dict[str, Any]] = []
    seen_titles: set[str] = set()
    seen_topics: set[str] = set()
    target_len = len(issues)
    for fallback_rank, issue in enumerate(issues, start=1):
        clone = dict(issue)
        clone.setdefault(
            "pre_suppression_rank",
            int(clone.get("rank") or fallback_rank),
        )
        title_key = normalize_text(str(clone.get("title_ko") or "")).lower()
        topic_key = title_key
        if clone.get("topics"):
            topic_key = normalize_text(str(clone["topics"][0])).lower()
        reason = None
        in_quality_window = len(selected) < top_n
        if in_quality_window and title_key and title_key in seen_titles:
            reason = "duplicate_title_topn"
        elif in_quality_window and topic_key and topic_key in seen_topics:
            reason = "duplicate_topic_topn"
        elif in_quality_window and int(clone.get("article_count") or 0) <= 1:
            reason = "single_article_topn"
        elif (
            in_quality_window
            and requested_market == "crypto"
            and issue_market_mismatch(clone, requested_market)
        ):
            reason = "crypto_equity_topic"
        if reason:
            clone["display_suppressed"] = True
            clone["suppression_reason"] = reason
            clone["display_suppressed_reason"] = reason
            flags = list(clone.get("quality_flags") or [])
            if reason not in flags:
                flags.append(reason)
            clone["quality_flags"] = flags
            suppressed.append(clone)
            continue
        clone.setdefault("display_suppressed", False)
        clone.setdefault("quality_flags", [])
        selected.append(clone)
        if len(selected) <= top_n:
            if title_key:
                seen_titles.add(title_key)
            if topic_key:
                seen_topics.add(topic_key)
    if suppressed and len(selected) < target_len:
        selected.extend(suppressed[: target_len - len(selected)])
    for rank, issue in enumerate(selected, start=1):
        issue["rank"] = rank
    return selected, suppressed


def evaluate_quality_gate(
    payload: dict[str, Any],
    *,
    market: str | None = None,
    config: QualityGateConfig | None = None,
) -> dict[str, Any]:
    market = market or payload.get("run", {}).get("market", "all")
    config = config or QualityGateConfig(
        top_n=int(payload.get("run", {}).get("quality_top", 5))
    )
    top_issues = list(payload.get("issues") or [])[: config.top_n]
    findings: list[dict[str, Any]] = []
    metrics = {
        "duplicate_title_count_topn": 0,
        "duplicate_topic_count_topn": 0,
        "single_article_count_topn": 0,
        "single_source_count_topn": 0,
        "market_mismatch_count_topn": 0,
        "regular_report_leakage_count_topn": 0,
        "source_noise_count_topn": 0,
        "crypto_equity_topic_count_topn": 0,
        "merge_decision_count": 0,
        "merge_rejected_near_misses": 0,
        "llm_render_enabled": False,
        "suppressed_candidate_count": len(payload.get("suppressed_candidates") or []),
    }
    seen_titles: set[str] = set()
    seen_topics: set[str] = set()
    for issue in top_issues:
        flags = list(issue.get("quality_flags") or [])
        title_key = normalize_text(str(issue.get("title_ko") or "")).lower()
        topic_key = title_key
        if issue.get("topics"):
            topic_key = normalize_text(str(issue["topics"][0])).lower()
        if title_key and title_key in seen_titles:
            metrics["duplicate_title_count_topn"] += 1
            findings.append(
                _quality_finding(
                    "duplicate_title_topn",
                    "fail",
                    issue,
                    "duplicate title_ko inside quality top-N",
                )
            )
            flags.append("duplicate_title_topn")
        if topic_key and topic_key in seen_topics:
            metrics["duplicate_topic_count_topn"] += 1
            findings.append(
                _quality_finding(
                    "duplicate_topic_topn",
                    "fail",
                    issue,
                    "duplicate topic inside quality top-N",
                )
            )
            flags.append("duplicate_topic_topn")
        if title_key:
            seen_titles.add(title_key)
        if topic_key:
            seen_topics.add(topic_key)
        if int(issue.get("article_count") or 0) <= 1:
            metrics["single_article_count_topn"] += 1
            findings.append(
                _quality_finding(
                    "single_article_topn",
                    "fail",
                    issue,
                    "single-article issue reached quality top-N",
                )
            )
            flags.append("single_article_topn")
        if (
            int(issue.get("normalized_source_count") or issue.get("source_count") or 0)
            <= 1
        ):
            metrics["single_source_count_topn"] += 1
            findings.append(
                _quality_finding(
                    "single_source_topn",
                    "warn",
                    issue,
                    "single normalized source in quality top-N",
                )
            )
            flags.append("single_source_topn")
        if issue_market_mismatch(issue, market):
            metrics["market_mismatch_count_topn"] += 1
            code = "crypto_equity_topic" if market == "crypto" else "market_mismatch"
            if code == "crypto_equity_topic":
                metrics["crypto_equity_topic_count_topn"] += 1
            findings.append(
                _quality_finding(
                    code, "fail", issue, f"issue does not fit requested market={market}"
                )
            )
            flags.append(code)
        if int((issue.get("flags") or {}).get("regular_report", 0)) > 0:
            metrics["regular_report_leakage_count_topn"] += 1
            findings.append(
                _quality_finding(
                    "regular_report_leakage",
                    "warn",
                    issue,
                    "regular-report/transcript flag present in quality top-N",
                )
            )
            flags.append("regular_report_leakage")
        if issue_has_source_noise(issue):
            metrics["source_noise_count_topn"] += 1
            findings.append(
                _quality_finding(
                    "source_name_noise",
                    "fail",
                    issue,
                    "source/feed name appears in rendered title/subtitle",
                )
            )
            flags.append("source_name_noise")
        issue["quality_flags"] = sorted(set(flags))
        if issue["quality_flags"]:
            issue["quality_notes"] = issue.get("quality_notes") or [
                "; ".join(issue["quality_flags"])
            ]
    merge_diag = payload.get("merge_diagnostics") or {}
    decisions = merge_diag.get("decisions") or []
    metrics["merge_decision_count"] = sum(
        1 for d in decisions if d.get("decision") == "merged"
    )
    metrics["merge_rejected_near_misses"] = int(
        merge_diag.get("rejected_near_misses")
        or sum(1 for d in decisions if d.get("decision") == "rejected")
    )
    llm_diag = payload.get("run", {}).get("llm_render") or {}
    metrics["llm_render_enabled"] = bool(llm_diag.get("enabled"))
    if metrics["llm_render_enabled"]:
        findings.append(
            _quality_finding(
                "llm_enabled_for_eval",
                "fail",
                None,
                "deterministic quality gate must keep LLM rendering disabled",
            )
        )
    suppressed_warning_count = 0
    suppressed_audit_count = 0
    blocking_suppression_reasons = {
        "duplicate_title_topn",
        "duplicate_topic_topn",
        "single_article_topn",
        "market_mismatch",
        "crypto_equity_topic",
    }
    for suppressed in payload.get("suppressed_candidates") or []:
        pre_rank = int(
            suppressed.get("pre_suppression_rank")
            or suppressed.get("rank")
            or config.top_n + 1
        )
        suppression_reason = str(
            suppressed.get("suppression_reason") or "suppressed_candidate"
        )
        severity = (
            "warn"
            if pre_rank <= config.top_n
            and suppression_reason in blocking_suppression_reasons
            else "info"
        )
        if severity == "warn":
            suppressed_warning_count += 1
            reason = "candidate originally inside quality top-N was suppressed"
        else:
            suppressed_audit_count += 1
            reason = (
                "quality candidate suppressed from visible top-N but preserved for audit"
                if pre_rank <= config.top_n
                else "deep candidate suppressed from visible top-N but preserved for audit"
            )
        findings.append(
            _quality_finding(
                suppression_reason,
                severity,
                suppressed,
                reason,
            )
        )
    metrics["suppressed_warning_count"] = suppressed_warning_count
    metrics["suppressed_audit_count"] = suppressed_audit_count
    fail_codes = {
        "duplicate_title_topn",
        "duplicate_topic_topn",
        "single_article_topn",
        "market_mismatch",
        "crypto_equity_topic",
        "source_name_noise",
        "llm_enabled_for_eval",
    }
    status = "pass"
    if any(f["severity"] == "fail" and f["code"] in fail_codes for f in findings):
        status = "fail"
    elif any(f["severity"] == "warn" for f in findings):
        status = "warn"
    return {
        "status": status,
        "top_n": config.top_n,
        "metrics": metrics,
        "thresholds": _asdict_dataclass(config),
        "findings": findings,
    }


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


def cluster_topic_label(rows: list[Article]) -> str | None:
    """Return the matching TOPIC_RULES title for the cluster, or None if no rule matches."""
    if not rows:
        return None
    text_blob = "\n".join([a.title + " " + (a.summary or "") for a in rows]).lower()
    for title, _, keys in TOPIC_RULES:
        if any(keyword_matches(text_blob, key) for key in keys):
            return title
    return None


def _topic_relevance(rows: list[Article]) -> float:
    if cluster_topic_label(rows) is not None:
        return 1.0
    text_blob = "\n".join([a.title + " " + (a.summary or "") for a in rows]).lower()
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
    duplicate_source_penalty = (
        max(
            0.0,
            1.0 - normalized_source_count / max(1, raw_source_count),
        )
        * 0.30
    )
    regular_report_penalty = 0.15 * min(3, regular_report_count)
    mixed_regular_report_penalty = 0.0
    if regular_report_count and regular_report_count < len(rows):
        # Mixed report/news clusters are diagnostically useful, but should not
        # outrank cleaner market issues just because research-category source
        # families add recency and volume. Keep the base regular_report key
        # capped for compatibility and expose the extra demotion separately.
        mixed_regular_report_penalty = 0.10
        if normalized_source_count >= 2:
            mixed_regular_report_penalty += 0.10
        if duplicate_source_penalty:
            mixed_regular_report_penalty += 0.12
        if regular_report_count >= 2:
            mixed_regular_report_penalty += 0.08
    single_source_penalty = 0.0
    weak_single_source_penalty = 0.0
    if normalized_source_count == 1:
        single_source_penalty = 0.14
        if len(rows) <= 2:
            single_source_penalty += 0.08
            weak_single_source_penalty += 0.10
        if topic_relevance < 1.0:
            single_source_penalty += 0.06
            weak_single_source_penalty += 0.08
    weak_single_article_penalty = 0.28 if len(rows) == 1 else 0.0
    penalties = {
        "noise": min(0.40, noise_penalty),
        "regular_report": min(0.45, regular_report_penalty),
        "mixed_regular_report": min(0.30, mixed_regular_report_penalty),
        "duplicate_source": min(0.30, duplicate_source_penalty),
        "single_source": min(0.28, single_source_penalty),
        "weak_single_source": min(0.18, weak_single_source_penalty),
        "weak_single_article": weak_single_article_penalty,
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
            "summary": a.summary,
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
    merged_cluster_ids = list(
        cluster.get("merged_cluster_ids")
        or [min(articles[i].id for i in cluster["indices"])]
    )
    return {
        "rank": rank,
        "cluster_key": hashlib.sha256(issue_key_raw.encode("utf-8")).hexdigest()[:16],
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
        "merge_member_count": len(merged_cluster_ids),
        "merged_cluster_ids": merged_cluster_ids,
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
    llm_render_diag = meta.get("llm_render")
    llm_render_line = ""
    if llm_render_diag:
        llm_render_line = (
            f"- LLM render: ok={llm_render_diag['ok']}, "
            f"fallback={llm_render_diag['fallback']}, "
            f"skipped={llm_render_diag['skipped']} "
            f"(provider={llm_render_diag['provider']}, "
            f"model={llm_render_diag['model']}, "
            f"prompt={llm_render_diag['prompt_version']})"
        )
    lines = [
        "# News Issue Lab PoC",
        "",
        f"- run_uuid: `{meta['run_uuid']}`",
        f"- market: `{meta['market']}` / window: {meta['window_hours']}h / articles: {meta['article_count']} / clusters: {meta['cluster_count']}",
        f"- embedding: `{meta['embedding_model']}` ({meta['embedding_dim']}d) / threshold: {meta['threshold']}",
        "- note: 기사 본문은 출력/저장하지 않고 제목·요약·메타데이터 기반으로만 실험했습니다.",
    ]
    if llm_render_line:
        lines.append(llm_render_line)
    quality_gate = payload.get("quality_gate") or {}
    if quality_gate:
        metrics = quality_gate.get("metrics") or {}
        lines.extend(
            [
                "",
                "## 품질 게이트 (ROB-145)",
                "",
                f"- status: `{quality_gate.get('status', '-')}` / top_n: {quality_gate.get('top_n', '-')}",
                f"- duplicate_title={metrics.get('duplicate_title_count_topn', 0)}, duplicate_topic={metrics.get('duplicate_topic_count_topn', 0)}, single_article={metrics.get('single_article_count_topn', 0)}, single_source={metrics.get('single_source_count_topn', 0)}",
                f"- market_mismatch={metrics.get('market_mismatch_count_topn', 0)}, source_noise={metrics.get('source_noise_count_topn', 0)}, crypto_equity_topic={metrics.get('crypto_equity_topic_count_topn', 0)}, regular_report={metrics.get('regular_report_leakage_count_topn', 0)}",
                f"- merge_decisions={metrics.get('merge_decision_count', 0)}, near_misses={metrics.get('merge_rejected_near_misses', 0)}, suppressed_warn={metrics.get('suppressed_warning_count', 0)}, suppressed_audit={metrics.get('suppressed_audit_count', 0)}, llm_render_enabled={metrics.get('llm_render_enabled', False)}",
            ]
        )
        findings = quality_gate.get("findings") or []
        if findings:
            lines.append("- findings:")
            for finding in findings[:10]:
                rank = finding.get("rank")
                title = finding.get("title_ko") or "run"
                lines.append(
                    f"  - {finding.get('severity')}:{finding.get('code')} rank={rank} title={title} — {finding.get('reason')}"
                )
    lines.extend(["", "## 실시간 이슈 후보", ""])
    arrow = {"up": "▲", "down": "▼", "neutral": "◆"}
    for issue in payload["issues"]:
        lines.extend(
            [
                f"### {issue['rank']}. {arrow.get(issue['direction'], '◆')} {issue['title_ko']}",
            ]
        )
        render_status = issue.get("render_status")
        if render_status == "ok":
            confidence = issue.get("confidence", 0.0)
            render_model = issue.get("render_model") or "-"
            lines.append(
                f"- 렌더: ok · model={render_model} · confidence={confidence:.2f}"
            )
        elif render_status == "fallback":
            rejection_reason = issue.get("render_rejection_reason") or "unknown"
            lines.append(f"- 렌더: fallback(rule-based, reason={rejection_reason})")
        lines.extend(
            [
                f"- 부제: {issue['subtitle_ko']}",
            ]
        )
        summary_ko = issue.get("summary_ko")
        if summary_ko:
            lines.append(f"- 요약: {summary_ko}")
        impact_points = issue.get("impact_points") or []
        if impact_points:
            lines.append("- 영향:")
            for pt in impact_points:
                lines.append(f"  - {pt}")
        lines.extend(
            [
                f"- 출처/기사: raw {issue.get('raw_source_count', issue['source_count'])}개 → normalized {issue.get('normalized_source_count', issue['source_count'])}개 · {issue['article_count']}개 기사",
                f"- 점수: {issue.get('score', 0):.4f} / components={issue.get('score_components', {})} / penalties={issue.get('score_penalties', {})}",
                f"- 품질 플래그: {', '.join(issue.get('quality_flags') or []) or '-'}",
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
        member_count = issue.get("merge_member_count", 1)
        if member_count > 1:
            cids_str = ", ".join(str(c) for c in issue.get("merged_cluster_ids") or [])
            lines.append(f"- 병합: {member_count}개 클러스터 통합 (cids: {cids_str})")
        lines.append("- 대표 기사:")
        for article in issue["representative_articles"][:4]:
            src = article.get("feed_source") or article.get("source") or "unknown"
            lines.append(f"  - [{src}] {article['title']}")
        lines.append("")
    merge_diag = payload.get("merge_diagnostics") or {}
    if merge_diag.get("enabled") and merge_diag.get("decisions"):
        lines.extend(
            [
                "## 클러스터 병합 진단 (ROB-135)",
                "",
                f"- 병합 전 클러스터: {merge_diag['merge_before_count']}개 → 병합 후: {merge_diag['merge_after_count']}개 (-{merge_diag['merge_before_count'] - merge_diag['merge_after_count']})",
                f"- 병합 임계값: rep≥{merge_diag['thresholds'].get('rep_threshold')}, token-jaccard≥{merge_diag['thresholds'].get('token_jaccard_threshold')}, strong-rep≥{merge_diag['thresholds'].get('strong_rep_threshold')}",
                f"- 거부된 근접 병합: {merge_diag.get('rejected_near_misses', 0)}건",
                "",
                "### 병합된 클러스터 (상위 10건)",
                "",
                "| absorber | absorbed | rep_sim | token_jaccard | topic | symbol | reason |",
                "|---|---|---:|---:|:--:|:--:|---|",
            ]
        )
        merged_decisions = [
            d for d in merge_diag["decisions"] if d.get("decision") == "merged"
        ]
        for d in merged_decisions[:10]:
            lines.append(
                f"| `{d['absorber_title']}` | `{d['absorbed_title']}` | "
                f"{d['rep_sim']:.4f} | {d['token_jaccard']:.4f} | "
                f"{'✓' if d['topic_agree'] else '-'} | "
                f"{'✓' if d['symbol_agree'] else '-'} | {d['reason']} |"
            )
        rejected = [
            d for d in merge_diag["decisions"] if d.get("decision") == "rejected"
        ]
        if rejected:
            lines.extend(["", "### 거부된 근접 병합 (참고)", ""])
            for d in rejected[:10]:
                lines.append(
                    f"- `{d['absorber_title']}` ↔ `{d['absorbed_title']}` "
                    f"rep_sim={d['rep_sim']:.4f} reason={d['reason']}"
                )
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
                api_key=getattr(args, "embedding_api_key", None),
            )
        )
    embedding_dim = len(vectors[0]) if vectors else 0
    clusters = cluster_articles(articles, vectors, args.threshold)
    clusters_before_merge = clusters

    def _embedder(texts: list[str]) -> list[list[float]]:
        rep_vectors: list[list[float]] = []
        for i in range(0, len(texts), args.batch_size):
            rep_vectors.extend(
                embed_batch(
                    args.embedding_endpoint,
                    args.embedding_model,
                    texts[i : i + args.batch_size],
                    api_key=getattr(args, "embedding_api_key", None),
                )
            )
        return rep_vectors

    clusters, merge_diag = merge_clusters(
        clusters,
        articles,
        _embedder,
        rep_threshold=getattr(args, "merge_rep_threshold", MERGE_REP_THRESHOLD),
        token_jaccard_threshold=getattr(
            args, "merge_token_jaccard", MERGE_TOKEN_JACCARD
        ),
        rep_articles=getattr(args, "merge_rep_articles", 3),
        enabled=bool(getattr(args, "merge_clusters", True)),
    )
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
    quality_top = int(getattr(args, "quality_top", 5))
    pre_quality_issues = [
        summarize_cluster(cluster, articles, rank=i + 1, score_breakdown=breakdown)
        for i, (cluster, breakdown) in enumerate(ranked_v2)
    ]
    issues, suppressed_candidates = suppress_duplicate_top_issues(
        pre_quality_issues,
        top_n=quality_top,
        requested_market=args.market,
        config=QualityGateConfig(top_n=quality_top),
    )
    issues = issues[: args.top]
    # ROB-136: Korean LLM rendering pass
    llm_provider = make_llm_provider(args)
    issues, render_diag = render_top_issues(
        issues,
        provider=llm_provider,
        llm_enabled=bool(getattr(args, "llm_render", False)),
        model=getattr(args, "llm_model", None),
        timeout=int(getattr(args, "llm_timeout", DEFAULT_LLM_TIMEOUT)),
        prompt_version=str(getattr(args, "llm_prompt_version", RENDER_PROMPT_VERSION)),
        max_render=int(getattr(args, "llm_max_render", None) or args.top),
    )
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
            "quality_top": quality_top,
            "article_count": len(articles),
            "cluster_count": len(clusters),
            "cluster_count_before_merge": len(clusters_before_merge),
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
            "merge_clusters": bool(getattr(args, "merge_clusters", True)),
            "merge_rep_threshold": float(
                getattr(args, "merge_rep_threshold", MERGE_REP_THRESHOLD)
            ),
            "merge_token_jaccard": float(
                getattr(args, "merge_token_jaccard", MERGE_TOKEN_JACCARD)
            ),
            "merge_rep_articles": int(getattr(args, "merge_rep_articles", 3)),
            "llm_render": render_diag,
        },
        "source_counts": {
            "raw": dict(raw_source_counts),
            "normalized": dict(normalized_source_counts),
        },
        "issues": issues,
        "suppressed_candidates": suppressed_candidates,
        "merge_diagnostics": {
            "enabled": merge_diag.enabled,
            "merge_before_count": merge_diag.merge_before_count,
            "merge_after_count": merge_diag.merge_after_count,
            "rejected_near_misses": merge_diag.rejected_near_misses,
            "thresholds": merge_diag.thresholds,
            "decisions": [
                {
                    "absorber_cid": d.absorber_cid,
                    "absorbed_cid": d.absorbed_cid,
                    "rep_sim": d.rep_sim,
                    "token_jaccard": d.token_jaccard,
                    "source_overlap": d.source_overlap,
                    "topic_agree": d.topic_agree,
                    "symbol_agree": d.symbol_agree,
                    "decision": d.decision,
                    "reason": d.reason,
                    "absorber_title": d.absorber_title,
                    "absorbed_title": d.absorbed_title,
                }
                for d in sorted(
                    merge_diag.decisions,
                    key=lambda x: (x.absorber_cid, x.absorbed_cid),
                )
            ],
        },
    }
    payload["quality_gate"] = evaluate_quality_gate(
        payload, market=args.market, config=QualityGateConfig(top_n=quality_top)
    )
    if getattr(args, "compare_v1", False):
        payload["v1_vs_v2"] = _comparison_payload(
            ranked_v1,
            ranked_v2,
            articles,
            top=args.top,
        )
    return payload


def write_output_file(path: str, rendered: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(rendered)


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
        write_output_file(args.output, rendered)
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

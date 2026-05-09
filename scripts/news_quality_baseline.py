#!/usr/bin/env python3
"""ROB-155 read-only baseline diagnostic for /invest/api/feed/news quality.

Computes before-report metrics for US (scope/big-tech noise) and crypto
(category distribution/relevance) without mutating any DB rows. Output is
written to /tmp/rob155_news_quality_<timestamp>/ by default.

Usage:
    uv run python scripts/news_quality_baseline.py --markets us,crypto --window-hours 168 --limit 500
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

VALID_MARKETS = ("us", "crypto")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="ROB-155 read-only news quality baseline"
    )
    parser.add_argument("--markets", default="us,crypto")
    parser.add_argument("--window-hours", type=int, default=168)
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--output-dir", default=None)
    args = parser.parse_args(argv)
    markets = [m.strip() for m in args.markets.split(",") if m.strip()]
    invalid = sorted(set(markets) - set(VALID_MARKETS))
    if invalid:
        parser.error(f"invalid markets: {', '.join(invalid)}")
    if not markets:
        parser.error("--markets must include at least one valid market (us, crypto)")
    for name in ("window_hours", "limit"):
        if getattr(args, name) <= 0:
            parser.error(f"--{name.replace('_', '-')} must be positive")
    args.markets = markets
    return args


def _analyze_us_articles(articles: list[Any]) -> dict[str, Any]:
    """Compute US scope/big-tech noise baseline metrics from article dicts."""
    from app.services.news_entity_matcher import (
        classify_article_scope,
        match_symbols_for_article,
    )

    sample_count = len(articles)
    scope_counter: Counter[str] = Counter()
    big_tech_fp_candidates: list[dict[str, Any]] = []
    broad_market_flag_count = 0
    source_counter: Counter[str] = Counter()

    for art in articles:
        title = art.get("title") or ""
        summary = art.get("summary") or ""
        keywords = art.get("keywords") or []
        feed_source = art.get("feed_source") or "unknown"
        source_counter[feed_source] += 1

        alias_matches = match_symbols_for_article(
            title=title, summary=summary, keywords=keywords, market="us"
        )
        scope_result = classify_article_scope(
            title,
            summary=summary,
            keywords=keywords,
            market="us",
            matches=alias_matches,
        )
        scope_counter[scope_result.scope] += 1
        if scope_result.scope == "market_wide":
            broad_market_flag_count += 1
        if scope_result.demoted_symbols:
            big_tech_fp_candidates.append(
                {
                    "title": title[:120],
                    "scope": scope_result.scope,
                    "demoted": scope_result.demoted_symbols[:5],
                    "tags": scope_result.tags[:5],
                }
            )

    big_tech_fp_rate = (
        len(big_tech_fp_candidates) / sample_count if sample_count else 0.0
    )
    broad_market_flag_rate = broad_market_flag_count / sample_count if sample_count else 0.0

    return {
        "sample_count": sample_count,
        "scope_distribution": dict(scope_counter),
        "big_tech_fp_rate_before": round(big_tech_fp_rate, 4),
        "broad_market_flag_rate": round(broad_market_flag_rate, 4),
        "top_sources": dict(source_counter.most_common(10)),
        "fp_examples": big_tech_fp_candidates[:10],
    }


def _analyze_crypto_articles(articles: list[Any]) -> dict[str, Any]:
    """Compute crypto relevance/category baseline metrics from article dicts."""
    from app.services.crypto_news_relevance_service import (
        score_crypto_news_article,
        user_facing_category,
    )
    from app.services.news_entity_alias_data import CRYPTO_ALIASES
    from app.services.news_entity_matcher import match_symbols_for_article

    sample_count = len(articles)
    category_counter: Counter[str] = Counter()
    noise_reason_counter: Counter[str] = Counter()
    include_count = 0
    ai_semi_noise_count = 0
    universe_symbols = {e.symbol for e in CRYPTO_ALIASES}
    universe_hit_count = 0
    noise_examples: list[dict[str, Any]] = []

    for art in articles:
        relevance = score_crypto_news_article(art)
        if relevance.include_in_briefing:
            include_count += 1
        user_cat = user_facing_category(relevance.category)
        if user_cat:
            category_counter[user_cat] += 1
        if relevance.noise_reason:
            noise_reason_counter[relevance.noise_reason] += 1
            if relevance.noise_reason == "broad_tech_without_crypto_signal":
                ai_semi_noise_count += 1
                if len(noise_examples) < 10:
                    noise_examples.append(
                        {
                            "title": (art.get("title") or "")[:120],
                            "noise_reason": relevance.noise_reason,
                            "score": relevance.score,
                        }
                    )

        alias_matches = match_symbols_for_article(
            title=art.get("title") or "",
            summary=art.get("summary") or "",
            keywords=art.get("keywords") or [],
            market="crypto",
        )
        if any(m.symbol in universe_symbols for m in alias_matches):
            universe_hit_count += 1

    return {
        "sample_count": sample_count,
        "include_count": include_count,
        "category_distribution": dict(category_counter),
        "noise_reason_distribution": dict(noise_reason_counter),
        "ai_semi_noise_rejection_pct": round(
            100 * ai_semi_noise_count / sample_count if sample_count else 0.0, 2
        ),
        "supported_universe_coverage_pct": round(
            100 * universe_hit_count / sample_count if sample_count else 0.0, 2
        ),
        "fp_examples": noise_examples,
    }


async def _load_articles_from_db(
    market: str, window_hours: int, limit: int
) -> list[dict[str, Any]]:
    """Load recent articles from DB as plain dicts (read-only)."""
    try:
        from sqlalchemy import select

        from app.core.db import AsyncSessionLocal
        from app.models.news import NewsArticle

        async with AsyncSessionLocal() as db:
            cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=window_hours)
            stmt = (
                select(NewsArticle)
                .where(NewsArticle.market == market)
                .where(NewsArticle.article_published_at >= cutoff)
                .order_by(NewsArticle.article_published_at.desc())
                .limit(limit)
            )
            rows = (await db.execute(stmt)).scalars().all()
            return [
                {
                    "id": r.id,
                    "title": r.title or "",
                    "summary": r.summary or "",
                    "keywords": r.keywords or [],
                    "feed_source": r.feed_source or "",
                    "market": r.market or "",
                    "article_published_at": r.article_published_at.isoformat()
                    if r.article_published_at
                    else None,
                }
                for r in rows
            ]
    except Exception as exc:
        print(
            f"[warn] DB load failed for market={market}: {type(exc).__name__}",
            file=sys.stderr,
        )
        return []


def run_baseline_on_articles(
    market: str, articles: list[dict[str, Any]]
) -> dict[str, Any]:
    """Pure function: compute baseline metrics for a list of article dicts."""
    if market == "us":
        return _analyze_us_articles(articles)
    if market == "crypto":
        return _analyze_crypto_articles(articles)
    return {"error": f"unsupported market: {market}"}


async def async_main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    output_dir = Path(
        args.output_dir or f"/tmp/rob155_news_quality_{timestamp}"
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    report: dict[str, Any] = {
        "as_of": datetime.now(UTC).isoformat(),
        "window_hours": args.window_hours,
        "limit": args.limit,
        "markets": args.markets,
        "safety": {
            "read_only": True,
            "llm_disabled": True,
            "db_mutations": False,
            "broker_order_watch_paths": False,
        },
    }

    for market in args.markets:
        articles = await _load_articles_from_db(market, args.window_hours, args.limit)
        metrics = run_baseline_on_articles(market, articles)
        report[market] = metrics

    report_path = output_dir / "baseline_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"output_dir: {output_dir}")
    print(f"report: {report_path}")
    for market in args.markets:
        m = report.get(market, {})
        sample = m.get("sample_count", 0)
        print(f"  {market}: sample_count={sample}", end="")
        if market == "us":
            print(
                f", broad_market_flag_rate={m.get('broad_market_flag_rate', 0):.2%},"
                f" big_tech_fp_rate={m.get('big_tech_fp_rate_before', 0):.2%}"
            )
        elif market == "crypto":
            print(
                f", ai_semi_noise_rejection={m.get('ai_semi_noise_rejection_pct', 0):.1f}%,"
                f" universe_coverage={m.get('supported_universe_coverage_pct', 0):.1f}%"
            )
        else:
            print()

    return 0


def main() -> None:
    try:
        raise SystemExit(asyncio.run(async_main()))
    except BrokenPipeError:
        raise SystemExit(0)
    except Exception as exc:
        print(f"news_quality_baseline failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()

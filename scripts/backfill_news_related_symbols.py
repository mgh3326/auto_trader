"""Backfill CLI for `news_article_related_symbols` (ROB-916).

Reprocesses already-ingested KR `news_articles` rows with the ROB-916
supplementary KR name-dictionary matcher (`news_entity_matcher.
match_kr_universe_symbols` over `kr_symbol_universe`), so articles the
upstream news-ingestor's own candidate extraction missed an explicit
company-name mention in (e.g. 한화오션 in http_naver_stock_aggregate feed
items) get a `news_article_related_symbols` row after the fact.

Read-mostly: selects from `news_articles` + `kr_symbol_universe`, writes only
through `symbol_news_store.upsert_related_symbols` (additive, idempotent by
the `(article_id, market, symbol, source)` unique constraint). Never touches
`symbol_news_relevance` status (ROB-491 boundary — no auto-exclusion here).

Default-disabled: requires `NEWS_RELATED_SYMBOLS_BACKFILL_ENABLED=true` even
for --dry-run, and --dry-run is the default even when enabled — --apply must
be passed explicitly to write.

Examples:
    NEWS_RELATED_SYMBOLS_BACKFILL_ENABLED=true \
        uv run python scripts/backfill_news_related_symbols.py \
        --from-date 2026-07-14 --to-date 2026-07-17

    NEWS_RELATED_SYMBOLS_BACKFILL_ENABLED=true \
        uv run python scripts/backfill_news_related_symbols.py \
        --from-date 2026-07-14 --to-date 2026-07-17 --apply
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field
from datetime import date, datetime

from sqlalchemy import select

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.models.news import NewsArticle, NewsArticleRelatedSymbol
from app.services import kr_symbol_universe_service, symbol_news_store
from app.services.news_entity_matcher import match_kr_universe_symbols
from app.services.news_payload_normalizer import _kr_universe_related_symbol_row

DEFAULT_FROM_DATE = "2026-07-14"
DEFAULT_TO_DATE = "2026-07-17"  # exclusive — covers 07-14, 07-15, 07-16
DEFAULT_FOCUS_SYMBOLS = (
    "042660",  # 한화오션
    "009540",  # HD한국조선해양
    "017670",  # SK텔레콤
    "000810",  # 삼성화재
    "279570",  # 케이뱅크
    "004310",  # 현대약품
    "483650",  # 달바글로벌
    "476060",  # 온코닉테라퓨틱스
)


@dataclass(frozen=True)
class SymbolRecall:
    symbol: str
    name: str | None
    articles_in_window: int
    already_mapped: int
    newly_mapped: int

    @property
    def after_mapped(self) -> int:
        return self.already_mapped + self.newly_mapped


@dataclass
class BackfillResult:
    articles_scanned: int
    articles_with_new_mapping: int
    new_rows: int
    applied: bool
    recall: list[SymbolRecall] = field(default_factory=list)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Reprocess KR news_articles for missed news_article_related_symbols "
            "mappings using the kr_symbol_universe name matcher (dry-run by default)."
        )
    )
    parser.add_argument(
        "--from-date",
        default=DEFAULT_FROM_DATE,
        help="KST date, inclusive (YYYY-MM-DD).",
    )
    parser.add_argument(
        "--to-date", default=DEFAULT_TO_DATE, help="KST date, exclusive (YYYY-MM-DD)."
    )
    parser.add_argument(
        "--feed-source",
        default=None,
        help=(
            "Restrict to one feed_source (e.g. http_naver_stock_aggregate). "
            "Default: all KR feed sources in the date window."
        ),
    )
    parser.add_argument(
        "--focus-symbols",
        default=",".join(DEFAULT_FOCUS_SYMBOLS),
        help="Comma-separated KR symbols for the recall report.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write new news_article_related_symbols rows. Default is --dry-run.",
    )
    args = parser.parse_args(argv)
    args.dry_run = not args.apply
    return args


def _parse_date(value: str) -> datetime:
    return datetime.combine(date.fromisoformat(value), datetime.min.time())


async def _load_candidate_articles(
    *,
    from_dt: datetime,
    to_dt: datetime,
    feed_source: str | None,
) -> list[NewsArticle]:
    async with AsyncSessionLocal() as db:
        conditions = [
            NewsArticle.market == "kr",
            NewsArticle.article_published_at >= from_dt,
            NewsArticle.article_published_at < to_dt,
        ]
        if feed_source:
            conditions.append(NewsArticle.feed_source == feed_source)
        stmt = select(NewsArticle).where(*conditions).order_by(NewsArticle.id.asc())
        return list((await db.execute(stmt)).scalars().all())


async def _load_existing_symbols_by_article(
    article_ids: list[int],
) -> dict[int, set[str]]:
    if not article_ids:
        return {}
    async with AsyncSessionLocal() as db:
        stmt = select(
            NewsArticleRelatedSymbol.article_id, NewsArticleRelatedSymbol.symbol
        ).where(
            NewsArticleRelatedSymbol.market == "kr",
            NewsArticleRelatedSymbol.article_id.in_(article_ids),
        )
        rows = (await db.execute(stmt)).all()
    out: dict[int, set[str]] = {}
    for article_id, symbol in rows:
        out.setdefault(article_id, set()).add(symbol)
    return out


def _build_recall_report(
    *,
    focus_symbols: list[str],
    focus_names: dict[str, str],
    articles: list[NewsArticle],
    existing_by_article: dict[int, set[str]],
    new_rows_by_article: dict[int, list[dict]],
) -> list[SymbolRecall]:
    focus_set = set(focus_symbols)
    articles_in_window: dict[str, int] = dict.fromkeys(focus_set, 0)
    already_mapped: dict[str, int] = dict.fromkeys(focus_set, 0)
    newly_mapped: dict[str, int] = dict.fromkeys(focus_set, 0)

    for article in articles:
        existing = existing_by_article.get(article.id, set())
        new_rows = new_rows_by_article.get(article.id, [])
        new_symbols = {row["symbol"] for row in new_rows}
        text = f"{article.title}\n{article.summary or ''}"
        for symbol in focus_set:
            name = focus_names.get(symbol)
            if name and name.lower() in text.lower():
                articles_in_window[symbol] += 1
            if symbol in existing:
                already_mapped[symbol] += 1
            elif symbol in new_symbols:
                newly_mapped[symbol] += 1

    return [
        SymbolRecall(
            symbol=symbol,
            name=focus_names.get(symbol),
            articles_in_window=articles_in_window[symbol],
            already_mapped=already_mapped[symbol],
            newly_mapped=newly_mapped[symbol],
        )
        for symbol in focus_symbols
    ]


async def run_backfill(
    *,
    from_date: str,
    to_date: str,
    feed_source: str | None,
    focus_symbols: list[str],
    apply: bool,
) -> BackfillResult:
    from_dt = _parse_date(from_date)
    to_dt = _parse_date(to_date)

    articles = await _load_candidate_articles(
        from_dt=from_dt, to_dt=to_dt, feed_source=feed_source
    )
    article_ids = [a.id for a in articles]
    existing_by_article = await _load_existing_symbols_by_article(article_ids)

    universe = await kr_symbol_universe_service.list_active_kr_symbol_names()
    universe_names = dict(universe)
    focus_names = {
        sym: universe_names[sym] for sym in focus_symbols if sym in universe_names
    }

    new_rows: list[dict] = []
    new_rows_by_article: dict[int, list[dict]] = {}
    for article in articles:
        already = existing_by_article.get(article.id, set())
        text = f"{article.title}\n{article.summary or ''}"
        for match in match_kr_universe_symbols(text, universe):
            if match.symbol in already:
                continue
            row = _kr_universe_related_symbol_row(
                article_id=article.id,
                symbol=match.symbol,
                matched_term=match.matched_term,
                canonical_name=match.canonical_name,
            )
            new_rows.append(row)
            new_rows_by_article.setdefault(article.id, []).append(row)

    recall = _build_recall_report(
        focus_symbols=focus_symbols,
        focus_names=focus_names,
        articles=articles,
        existing_by_article=existing_by_article,
        new_rows_by_article=new_rows_by_article,
    )

    applied = False
    if apply and new_rows:
        async with AsyncSessionLocal() as db:
            await symbol_news_store.upsert_related_symbols(db, new_rows, commit=True)
        applied = True

    return BackfillResult(
        articles_scanned=len(articles),
        articles_with_new_mapping=len(new_rows_by_article),
        new_rows=len(new_rows),
        applied=applied,
        recall=recall,
    )


def _print_result(result: BackfillResult) -> None:
    mode = "APPLIED" if result.applied else "DRY-RUN (no rows written)"
    print(f"\nnews_article_related_symbols backfill — {mode}")
    print(f"  articles scanned:            {result.articles_scanned}")
    print(f"  articles with new mapping:   {result.articles_with_new_mapping}")
    print(f"  new related-symbol rows:     {result.new_rows}")
    if result.recall:
        print("\n  Recall by focus symbol:")
        print(
            f"  {'symbol':<8}{'name':<16}{'in_window':>10}{'already':>10}"
            f"{'newly':>10}{'after':>10}"
        )
        for row in result.recall:
            print(
                f"  {row.symbol:<8}{(row.name or '-')[:14]:<16}"
                f"{row.articles_in_window:>10}{row.already_mapped:>10}"
                f"{row.newly_mapped:>10}{row.after_mapped:>10}"
            )
    print()


async def main() -> int:
    args = parse_args()
    if not settings.NEWS_RELATED_SYMBOLS_BACKFILL_ENABLED:
        print(
            "NEWS_RELATED_SYMBOLS_BACKFILL_ENABLED is not set — refusing to run "
            "(default-disabled gate, ROB-916). Set it to 'true' to proceed, "
            "still --dry-run by default."
        )
        return 1

    focus_symbols = [s.strip() for s in args.focus_symbols.split(",") if s.strip()]
    result = await run_backfill(
        from_date=args.from_date,
        to_date=args.to_date,
        feed_source=args.feed_source,
        focus_symbols=focus_symbols,
        apply=args.apply,
    )
    _print_result(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

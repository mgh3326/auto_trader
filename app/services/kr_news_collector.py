"""Bounded Korean Naver news collection with source-scoped outcomes."""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.mcp_server.tooling.shared import is_korean_equity_code
from app.models.account_value import AccountValueSnapshot
from app.models.investment_reports import InvestmentWatchAlert
from app.models.manual_holdings import ManualHolding, MarketType
from app.models.news import NewsArticle
from app.models.trade_journal import TradeJournal
from app.models.trading import Instrument, InstrumentType, UserWatchItem
from app.services import symbol_news_store
from app.services.naver_news_urls import canonicalize_naver_news_url as canonicalize_url
from app.services.news_entity_matcher import match_kr_universe_symbols

logger = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")
AGGREGATE_FEED_SOURCE = "http_naver_stock_aggregate"
AGGREGATE_URL = "https://stock.naver.com/api/domestic/news/aggregate/home"
DEFAULT_SYMBOL_CAP = 30
AGGREGATE_SECTIONS = {
    "flashNews": "flashNews",
    "mainNews": "mainNews",
    "rankingNews": "rankingNews",
    "newsFocus": "newsFocus",
}
AGGREGATE_SIZE_PARAMS = (
    "flashNewsSize",
    "mainNewsSize",
    "rankingNewsSize",
    "focusSize",
)


@dataclass(frozen=True)
class ParsedPublishTime:
    raw: str | None
    value: datetime | None
    precision: str | None
    reason: str | None


def parse_publish_time(raw: object) -> ParsedPublishTime:
    """Parse provider time without losing its exact source representation."""
    if not isinstance(raw, str):
        return ParsedPublishTime(None, None, None, "missing_or_non_string")
    if raw == "":
        return ParsedPublishTime(raw, None, None, "empty")
    text = raw.strip()
    if re.fullmatch(r"[0-9]+", text):
        compact_formats = {
            8: ("%Y%m%d", "date"),
            12: ("%Y%m%d%H%M", "minute"),
            14: ("%Y%m%d%H%M%S", "second"),
        }
        compact = compact_formats.get(len(text))
        if compact is None:
            return ParsedPublishTime(raw, None, None, "unparseable")
        fmt, precision = compact
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            return ParsedPublishTime(raw, None, None, "unparseable")
        parsed = parsed.replace(tzinfo=KST)
        return ParsedPublishTime(raw, parsed.astimezone(UTC), precision, None)
    try:
        parsed_iso = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        parsed_iso = None
    if parsed_iso is not None:
        has_time = "T" in text or " " in text
        precision = (
            "second"
            if has_time and re.search(r"\d{2}:\d{2}:\d{2}", text)
            else "minute"
            if has_time
            else "date"
        )
        if parsed_iso.tzinfo is None:
            parsed_iso = parsed_iso.replace(tzinfo=KST)
        return ParsedPublishTime(raw, parsed_iso.astimezone(UTC), precision, None)
    formats = (
        ("%Y-%m-%dT%H:%M:%S%z", "second"),
        ("%Y-%m-%d %H:%M:%S", "second"),
        ("%Y-%m-%dT%H:%M%z", "minute"),
        ("%Y-%m-%d %H:%M", "minute"),
        ("%Y-%m-%d", "date"),
    )
    for fmt, precision in formats:
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=KST)
        return ParsedPublishTime(raw, parsed.astimezone(UTC), precision, None)
    return ParsedPublishTime(raw, None, None, "unparseable")


def _first_value(row: dict[str, Any], *keys: str) -> object:
    for key in keys:
        value = row.get(key)
        if value is not None and value != "":
            return value
    return None


def normalize_aggregate_item(row: object) -> dict[str, Any] | None:
    if not isinstance(row, dict):
        return None
    url = _first_value(
        row, "url", "articleUrl", "link", "mobileNewsUrl", "urlToArticle"
    )
    title = _first_value(row, "title", "titleFull", "newsTitle")
    if not isinstance(url, str) or not isinstance(title, str) or not title.strip():
        return None
    try:
        dedupe_url = canonicalize_url(url)
    except ValueError:
        return None
    published_raw = _first_value(
        row,
        "time",
        "datetime",
        "publishedAt",
        "publishDate",
        "date",
        "officeDate",
        "writeDate",
    )
    published = parse_publish_time(published_raw)
    source = _first_value(
        row, "press", "officeHname", "officeName", "source", "pressName", "publisher"
    )
    summary = _first_value(
        row, "leadtext", "subcontent", "summary", "body", "description"
    )
    return {
        # Keep the provider URL as the stored identity. The canonical form is
        # only used to collapse equivalent URLs within this fetched batch.
        "url": url.strip(),
        "dedupe_url": dedupe_url,
        "title": title.strip(),
        "source": str(source).strip() if source is not None else None,
        "summary": str(summary).strip() if summary is not None else None,
        "published": published,
    }


async def fetch_aggregate_news(
    *, get_json: Callable[[str, dict[str, int]], Awaitable[Any]] | None = None
) -> list[dict[str, Any]]:
    """Fetch the four legacy aggregate sections. Provider failures propagate."""
    if get_json is None:

        async def get_json(url: str, params: dict[str, int]) -> Any:
            async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
                response = await client.get(url, params=params)
                response.raise_for_status()
                return response.json()

    params = dict.fromkeys(AGGREGATE_SIZE_PARAMS, 100)
    payload = await get_json(AGGREGATE_URL, params)
    if not isinstance(payload, dict):
        raise ValueError("aggregate_payload_shape")
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for section, response_key in AGGREGATE_SECTIONS.items():
        section_items = payload.get(response_key)
        if section == "newsFocus":
            if not isinstance(section_items, list):
                raise ValueError("aggregate_section_missing:newsFocus")
            focus_items = []
            for focus in section_items:
                if not isinstance(focus, dict) or not isinstance(
                    focus.get("news"), list
                ):
                    raise ValueError("aggregate_focus_shape")
                for raw in focus["news"]:
                    if isinstance(raw, dict):
                        focus_items.append(
                            {**raw, "focus_category": focus.get("category")}
                        )
            section_items = focus_items
        if not isinstance(section_items, list):
            raise ValueError(f"aggregate_section_missing:{response_key}")
        for raw in section_items:
            item = normalize_aggregate_item(raw)
            if item is None or item["dedupe_url"] in seen:
                continue
            seen.add(item["dedupe_url"])
            item["section"] = section
            if raw.get("focus_category"):
                item["focus_category"] = str(raw["focus_category"])
            items.append(item)
    return items


@dataclass(frozen=True)
class SymbolSet:
    symbols: tuple[str, ...]
    origins: dict[str, tuple[str, ...]]
    cut_list: tuple[dict[str, str], ...]


def build_symbol_set(
    *,
    holdings: Sequence[str],
    watch: Sequence[str],
    first_captures: Sequence[str],
    cap: int = DEFAULT_SYMBOL_CAP,
) -> SymbolSet:
    if cap < 0:
        raise ValueError("cap_must_be_nonnegative")
    priorities = (
        ("holding", holdings),
        ("watch", watch),
        ("first_capture", first_captures),
    )
    ordered: list[str] = []
    origins: dict[str, list[str]] = {}
    for source, values in priorities:
        for raw in values:
            symbol = str(raw).strip().upper()
            if symbol.isascii() and symbol.isdigit() and len(symbol) < 6:
                symbol = symbol.zfill(6)
            if not is_korean_equity_code(symbol):
                continue
            if source not in origins.setdefault(symbol, []):
                origins[symbol].append(source)
            if symbol not in ordered:
                ordered.append(symbol)
    selected = tuple(ordered[:cap])
    cut = tuple(
        {"symbol": symbol, "origin": ",".join(origins[symbol])}
        for symbol in ordered[cap:]
    )
    return SymbolSet(selected, {s: tuple(origins[s]) for s in selected}, cut)


async def load_symbol_set(
    db: AsyncSession,
    *,
    today: date,
    now: datetime,
    cap: int = DEFAULT_SYMBOL_CAP,
) -> SymbolSet:
    from app.services.invest_momentum_events.intraday import first_captures

    manual = await db.execute(
        select(ManualHolding.ticker).where(ManualHolding.market_type == MarketType.KR)
    )
    journal = await db.execute(
        select(TradeJournal.symbol).where(
            TradeJournal.status == "active",
            TradeJournal.account_type == "live",
            TradeJournal.instrument_type == InstrumentType.equity_kr,
        )
    )
    snapshot_rows = await db.execute(
        select(AccountValueSnapshot.account, AccountValueSnapshot.holdings)
        .where(AccountValueSnapshot.market == "kr")
        .distinct(AccountValueSnapshot.account)
        .order_by(AccountValueSnapshot.account, AccountValueSnapshot.observed_at.desc())
    )
    snapshot_holdings: list[str] = []
    for _account, holdings in snapshot_rows.all():
        for holding in holdings or []:
            if isinstance(holding, dict) and holding.get("symbol"):
                snapshot_holdings.append(str(holding["symbol"]))
    watch_rows = await db.execute(
        select(Instrument.symbol)
        .join(UserWatchItem, UserWatchItem.instrument_id == Instrument.id)
        .where(
            UserWatchItem.is_active.is_(True),
            Instrument.type == InstrumentType.equity_kr,
        )
    )
    active_alerts = await db.execute(
        select(InvestmentWatchAlert.symbol).where(
            InvestmentWatchAlert.market == "kr",
            InvestmentWatchAlert.status == "active",
            InvestmentWatchAlert.valid_until > now,
        )
    )
    captures = await first_captures(db, today, market="kr")
    held_symbols = (
        [str(v) for (v,) in manual.all()]
        + [str(v) for (v,) in journal.all()]
        + snapshot_holdings
    )
    return build_symbol_set(
        holdings=held_symbols,
        watch=[str(v) for (v,) in watch_rows.all()]
        + [str(v) for (v,) in active_alerts.all()],
        first_captures=[item.symbol for item in captures],
        cap=cap,
    )


@dataclass
class SourceReport:
    source: str
    status: str = "ok"
    fetched: int = 0
    new: int = 0
    duplicate: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class CollectionReport:
    started_at: datetime
    dry_run: bool
    cut_list: list[dict[str, str]] = field(default_factory=list)
    sources: list[SourceReport] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at.isoformat(),
            "dry_run": self.dry_run,
            "symbol_cut_list": self.cut_list,
            "sources": [vars(row) for row in self.sources],
        }


async def _store_items(
    db: AsyncSession,
    *,
    feed_source: str,
    items: list[dict[str, Any]],
    symbols: Sequence[str],
) -> tuple[int, int]:
    new_articles = 0
    new_links = 0
    for symbol in symbols:
        inputs = [
            symbol_news_store.FeedArticleInput(
                url=item["url"],
                title=item["title"],
                source=item["source"],
                published_at=item["published"].value,
                summary=item.get("summary"),
                published_at_raw=item["published"].raw,
                published_precision=item["published"].precision,
                published_at_reason=item["published"].reason,
            )
            for item in items
        ]
        inserted, links = await symbol_news_store.upsert_kr_collector_articles(
            db, symbol, inputs, feed_source=feed_source, commit=False
        )
        new_articles += inserted
        new_links += links
    return new_articles, new_links


async def collect_kr_news(
    *,
    db: AsyncSession | None,
    now: datetime,
    aggregate_fetcher: Callable[
        [], Awaitable[list[dict[str, Any]]]
    ] = fetch_aggregate_news,
    item_fetcher: Callable[[str, int], Awaitable[Any]] | None = None,
    explicit_symbols: Sequence[str] | None = None,
    symbol_cap: int = DEFAULT_SYMBOL_CAP,
) -> CollectionReport:
    report = CollectionReport(started_at=now.astimezone(UTC), dry_run=db is None)
    symbol_set = None
    if db is not None and explicit_symbols is None:
        symbol_set = await load_symbol_set(
            db, today=now.astimezone(KST).date(), now=now, cap=symbol_cap
        )
        report.cut_list = list(symbol_set.cut_list)
        symbols = symbol_set.symbols
    else:
        symbol_set = build_symbol_set(
            holdings=(),
            watch=(),
            first_captures=explicit_symbols or (),
            cap=symbol_cap,
        )
        report.cut_list = list(symbol_set.cut_list)
        symbols = symbol_set.symbols
    if item_fetcher is None:
        from app.services.naver_finance.news import fetch_stock_news

        item_fetcher = fetch_stock_news

    aggregate_report = SourceReport(source=AGGREGATE_FEED_SOURCE)
    report.sources.append(aggregate_report)
    try:
        aggregate_items = await aggregate_fetcher()
        aggregate_report.fetched = len(aggregate_items)
        if db is not None and aggregate_items:
            from app.services.kr_symbol_universe_service import (
                list_active_kr_symbol_names,
            )

            universe = await list_active_kr_symbol_names(db)
            urls: list[str] = []
            for item in aggregate_items:
                # Aggregate articles remain linked to every full-name symbol mention.
                matches = match_kr_universe_symbols(
                    f"{item['title']}\n{item.get('summary') or ''}", universe
                )
                inserted = 0
                if matches:
                    inserted, _links = await _store_items(
                        db,
                        feed_source=AGGREGATE_FEED_SOURCE,
                        items=[item],
                        symbols=[match.symbol for match in matches],
                    )
                    aggregate_report.new += inserted
                else:
                    inputs = [
                        symbol_news_store.FeedArticleInput(
                            url=item["url"],
                            title=item["title"],
                            source=item["source"],
                            published_at=item["published"].value,
                            summary=item.get("summary"),
                            published_at_raw=item["published"].raw,
                            published_precision=item["published"].precision,
                            published_at_reason=item["published"].reason,
                        )
                    ]
                    inserted, _ = await symbol_news_store.upsert_kr_collector_articles(
                        db,
                        None,
                        inputs,
                        feed_source=AGGREGATE_FEED_SOURCE,
                        commit=False,
                    )
                    aggregate_report.new += inserted
                urls.append(item["url"])
                if matches:
                    article_id = await db.scalar(
                        select(NewsArticle.id).where(NewsArticle.url == item["url"])
                    )
                    if article_id is not None:
                        await symbol_news_store.upsert_related_symbols(
                            db,
                            [
                                {
                                    "article_id": article_id,
                                    "market": "kr",
                                    "symbol": match.symbol,
                                    "display_name": match.canonical_name[:120],
                                    "source": "kr_symbol_universe_name",
                                    "matched_term": match.matched_term[:120],
                                    "score": None,
                                    "rank": None,
                                    "raw": {"matcher": "kr_symbol_universe_name"},
                                    "created_at": datetime.now(UTC).replace(
                                        tzinfo=None
                                    ),
                                }
                                for match in matches
                            ],
                            commit=False,
                        )
            aggregate_report.duplicate = max(
                0, aggregate_report.fetched - aggregate_report.new
            )
            await db.commit()
    except (
        Exception
    ) as exc:  # source failure must remain distinguishable from an empty feed
        if db is not None:
            await db.rollback()
        aggregate_report.status = "error"
        aggregate_report.errors.append(type(exc).__name__)
        logger.warning("KR aggregate news collection failed: %s", type(exc).__name__)

    for symbol in symbols:
        source = SourceReport(source=f"naver_item_news:{symbol}")
        report.sources.append(source)
        try:
            fetched = await item_fetcher(symbol, 20)
            rows = getattr(fetched, "items", fetched)
            normalized: list[dict[str, Any]] = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                raw_time = row.get("datetime_raw", row.get("datetime"))
                published = parse_publish_time(raw_time)
                try:
                    url = str(row.get("url") or "").strip()
                    dedupe_url = canonicalize_url(url)
                except ValueError:
                    continue
                if not row.get("title"):
                    continue
                item = {
                    "url": url,
                    "dedupe_url": dedupe_url,
                    "title": str(row["title"]),
                    "source": row.get("source"),
                    "summary": None,
                    "published": published,
                }
                normalized.append(item)
            unique: dict[str, dict[str, Any]] = {}
            for item in normalized:
                unique.setdefault(item["dedupe_url"], item)
            source.fetched = len(normalized)
            if db is not None and unique:
                inserted, _links = await _store_items(
                    db,
                    feed_source="naver_item_news",
                    items=list(unique.values()),
                    symbols=[symbol],
                )
                source.new = inserted
                source.duplicate = max(0, source.fetched - source.new)
                await db.commit()
            else:
                source.duplicate = max(0, source.fetched - len(unique))
        except Exception as exc:
            if db is not None:
                await db.rollback()
            source.status = "error"
            source.errors.append(type(exc).__name__)
            logger.warning(
                "KR item news collection failed symbol=%s error=%s",
                symbol,
                type(exc).__name__,
            )
    return report

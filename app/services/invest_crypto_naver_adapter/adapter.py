"""Read-only Naver-style crypto reference adapter for /invest.

The adapter aggregates existing read-only sources and fixture-backed Naver
reference metadata. It must never submit/cancel/modify orders, mutate watch or
order-intent state, build/commit screener snapshots, or write production data.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.upbit_symbol_universe import UpbitSymbolUniverse
from app.schemas.invest_crypto import (
    CryptoCapabilityFlag,
    CryptoReferenceSourceMeta,
    NaverCryptoKimchiPremium,
    NaverCryptoProfile,
    NaverCryptoRankItem,
    NaverCryptoReferenceCapabilities,
    NaverCryptoReferenceResponse,
)
from app.schemas.invest_feed_news import FeedNewsResponse
from app.services.invest_crypto_naver_adapter.fixtures import NAVER_CRYPTO_REFERENCES
from app.services.invest_crypto_screener_snapshots.repository import (
    InvestCryptoScreenerSnapshotsRepository,
)
from app.services.invest_view_model.relation_resolver import RelationResolver

RankProvider = Callable[[AsyncSession, int], Awaitable[Sequence[Any]] | Sequence[Any]]
TickerProvider = Callable[[list[str]], Awaitable[list[dict[str, Any]]] | list[dict[str, Any]]]
NewsProvider = Callable[
    [AsyncSession, RelationResolver, str | None, int],
    Awaitable[FeedNewsResponse | None] | FeedNewsResponse | None,
]
KimchiProvider = Callable[[str], Awaitable[dict[str, Any] | list[dict[str, Any]] | None] | dict[str, Any] | list[dict[str, Any]] | None]


@dataclass(frozen=True)
class NaverCryptoReferenceProviders:
    rank_provider: RankProvider | None = None
    ticker_provider: TickerProvider | None = None
    news_provider: NewsProvider | None = None
    kimchi_provider: KimchiProvider | None = None


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def normalize_krw_symbol(symbol: str | None) -> str | None:
    if not symbol:
        return None
    normalized = str(symbol).strip().upper().replace("/", "-")
    if not normalized:
        return None
    if normalized.startswith("KRW-"):
        return normalized
    if normalized.endswith("-KRW"):
        return f"KRW-{normalized.rsplit('-', 1)[0]}"
    return f"KRW-{normalized}"


def _base_symbol(symbol: str | None) -> str:
    normalized = normalize_krw_symbol(symbol) or ""
    return normalized.split("-", 1)[1] if "-" in normalized else normalized


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError, ArithmeticError):
        return None


def _ticker_map(rows: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    mapped: dict[str, dict[str, Any]] = {}
    for row in rows:
        market = str(row.get("market") or "").upper()
        if market:
            mapped[market] = row
    return mapped


_SOURCE_STALE_AFTER_SECONDS = 24 * 60 * 60


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _parse_source_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _as_utc(value)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    if isinstance(value, (int, float)):
        # Upbit ticker timestamps are millisecond epochs. Accept seconds too.
        seconds = float(value) / 1000 if float(value) > 10_000_000_000 else float(value)
        try:
            return datetime.fromtimestamp(seconds, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            return _as_utc(datetime.fromisoformat(raw.replace("Z", "+00:00")))
        except ValueError:
            return None
    return None


def _row_value(row: Any, key: str) -> Any:
    if isinstance(row, dict):
        return row.get(key)
    return getattr(row, key, None)


def _source_timestamp_from_rows(rows: Sequence[Any], keys: Sequence[str]) -> datetime | None:
    timestamps: list[datetime] = []
    for row in rows:
        for key in keys:
            parsed = _parse_source_datetime(_row_value(row, key))
            if parsed is not None:
                timestamps.append(parsed)
                break
    return max(timestamps) if timestamps else None


def _cache_age_seconds(*, now: datetime, fetched_at: datetime | None) -> float | None:
    if fetched_at is None:
        return None
    return max(0.0, (now - fetched_at).total_seconds())


def _cached_source_meta(
    *,
    source: str,
    label: str,
    has_data: bool,
    fetched_at: datetime | None,
    now: datetime,
    stale_after_seconds: int = _SOURCE_STALE_AFTER_SECONDS,
) -> CryptoReferenceSourceMeta:
    if not has_data:
        return CryptoReferenceSourceMeta(
            source=source,
            label=label,
            state="unavailable",
            freshness="missing",
        )
    age_seconds = _cache_age_seconds(now=now, fetched_at=fetched_at)
    is_stale = age_seconds is not None and age_seconds > stale_after_seconds
    return CryptoReferenceSourceMeta(
        source=source,
        label=label,
        state="stale" if is_stale else "cached",
        fetchedAt=fetched_at,
        cacheAgeSeconds=age_seconds,
        freshness="stale" if is_stale else ("fresh" if fetched_at is not None else "partial"),
    )


def _provider_source_meta(
    *,
    source: str,
    label: str,
    has_data: bool,
    fetched_at: datetime | None,
    now: datetime,
    reference_only: bool = False,
    stale_after_seconds: int = _SOURCE_STALE_AFTER_SECONDS,
) -> CryptoReferenceSourceMeta:
    if not has_data:
        return CryptoReferenceSourceMeta(
            source=source,
            label=label,
            state="unavailable",
            freshness="missing",
            referenceOnly=reference_only,
        )
    if fetched_at is None:
        return CryptoReferenceSourceMeta(
            source=source,
            label=label,
            state="live",
            fetchedAt=now,
            freshness="live",
            referenceOnly=reference_only,
        )
    meta = _cached_source_meta(
        source=source,
        label=label,
        has_data=True,
        fetched_at=fetched_at,
        now=now,
        stale_after_seconds=stale_after_seconds,
    )
    if reference_only:
        return meta.model_copy(update={"referenceOnly": True})
    return meta


async def _default_rank_provider(db: AsyncSession, limit: int) -> Sequence[Any]:
    repo = InvestCryptoScreenerSnapshotsRepository(db)
    return await repo.list_latest(preset_id="crypto_momentum", limit=limit)


async def _default_ticker_provider(markets: list[str]) -> list[dict[str, Any]]:
    """Default ticker provider intentionally avoids live request-path HTTP.

    The Naver reference endpoint is a read-model/fixture view. Live Upbit ticker
    fetches are volatile external HTTP and must be injected explicitly by callers
    that can label them as live data.
    """

    return []


async def _default_news_provider(
    db: AsyncSession,
    resolver: RelationResolver,
    symbol: str | None,
    limit: int,
) -> FeedNewsResponse | None:
    from app.services.invest_view_model.feed_news_service import build_feed_news

    symbol_filter = (symbol, "crypto") if symbol else None
    return await build_feed_news(
        db=db,
        resolver=resolver,
        tab="crypto",
        limit=max(1, min(limit, 20)),
        cursor=None,
        include_quotes=False,
        symbol_filter=symbol_filter,
    )


async def _default_kimchi_provider(base_symbol: str) -> dict[str, Any] | list[dict[str, Any]] | None:
    """Default kimchi provider intentionally avoids uncached live HTTP.

    A caller may inject a live or cached provider; without one the endpoint still
    returns fixture/read-model data and marks kimchi premium unavailable.
    """

    return None


async def _load_universe_fallback(db: AsyncSession, *, limit: int) -> list[UpbitSymbolUniverse]:
    stmt = (
        select(UpbitSymbolUniverse)
        .where(
            UpbitSymbolUniverse.quote_currency == "KRW",
            UpbitSymbolUniverse.is_active.is_(True),
        )
        .order_by(UpbitSymbolUniverse.market.asc())
        .limit(limit)
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


def _decimal_float(value: Decimal | float | int | str | None) -> float | None:
    return _float_or_none(value)


def _market_from_row(row: Any) -> str | None:
    return normalize_krw_symbol(getattr(row, "symbol", None) or getattr(row, "market", None))


def _name_from_row(row: Any, symbol: str) -> str:
    fixture = NAVER_CRYPTO_REFERENCES.get(symbol, {})
    return str(
        getattr(row, "name", None)
        or getattr(row, "korean_name", None)
        or getattr(row, "english_name", None)
        or fixture.get("displayName")
        or _base_symbol(symbol)
    )


def _rank_item_from_snapshot(
    *, rank: int,
    row: Any,
    ticker: dict[str, Any] | None,
) -> NaverCryptoRankItem | None:
    symbol = _market_from_row(row)
    if not symbol:
        return None
    return NaverCryptoRankItem(
        rank=rank,
        symbol=symbol,
        displayName=_name_from_row(row, symbol),
        priceKrw=_decimal_float(getattr(row, "latest_close", None))
        or _float_or_none((ticker or {}).get("trade_price")),
        changeRate24h=_decimal_float(getattr(row, "change_rate", None))
        or _float_or_none((ticker or {}).get("signed_change_rate")),
        tradeAmount24h=_decimal_float(getattr(row, "trade_amount_24h", None))
        or _float_or_none((ticker or {}).get("acc_trade_price_24h")),
        rsi=_decimal_float(getattr(row, "rsi", None)),
        marketWarning=bool(getattr(row, "market_warning", False)),
        source=str(getattr(row, "source", None) or "tvscreener_upbit"),
    )


def _rank_item_from_universe(
    *, rank: int,
    row: UpbitSymbolUniverse,
    ticker: dict[str, Any] | None,
) -> NaverCryptoRankItem:
    symbol = normalize_krw_symbol(row.market) or row.market.upper()
    return NaverCryptoRankItem(
        rank=rank,
        symbol=symbol,
        displayName=row.korean_name or row.english_name or _base_symbol(symbol),
        priceKrw=_float_or_none((ticker or {}).get("trade_price")),
        changeRate24h=_float_or_none((ticker or {}).get("signed_change_rate")),
        tradeAmount24h=_float_or_none((ticker or {}).get("acc_trade_price_24h")),
        rsi=None,
        marketWarning=None,
        source="upbit_official",
    )


def _build_profile(symbol: str | None, rank: Sequence[NaverCryptoRankItem]) -> NaverCryptoProfile | None:
    target = normalize_krw_symbol(symbol) if symbol else (rank[0].symbol if rank else None)
    if target is None:
        return None
    fixture = NAVER_CRYPTO_REFERENCES.get(target, {})
    rank_match = next((item for item in rank if item.symbol == target), None)
    base = str(fixture.get("baseSymbol") or _base_symbol(target))
    display_name = str(fixture.get("displayName") or (rank_match.displayName if rank_match else base))
    notes = list(fixture.get("referenceNotes") or [])
    if not notes:
        notes = [
            "Naver crypto metadata is fixture/reference-only in ROB-234.",
            "Live prices use Upbit official/public read-model sources where available.",
        ]
    return NaverCryptoProfile(
        symbol=target,
        baseSymbol=base,
        displayName=display_name,
        koreanName=str(fixture.get("koreanName")) if fixture.get("koreanName") else None,
        englishName=str(fixture.get("englishName")) if fixture.get("englishName") else None,
        naverUrl=str(fixture.get("naverUrl")) if fixture.get("naverUrl") else None,
        officialMarket="UPBIT/KRW",
        referenceNotes=notes,
    )


def _first_kimchi_row(payload: dict[str, Any] | list[dict[str, Any]] | None) -> dict[str, Any] | None:
    if isinstance(payload, list):
        return payload[0] if payload else None
    if isinstance(payload, dict):
        return payload
    return None


def _build_kimchi(base_symbol: str, payload: dict[str, Any] | list[dict[str, Any]] | None) -> NaverCryptoKimchiPremium:
    row = _first_kimchi_row(payload)
    if not row:
        return NaverCryptoKimchiPremium(
            baseSymbol=base_symbol,
            premiumPct=None,
            domesticPriceKrw=None,
            overseasPriceKrw=None,
            state="unavailable",
            source="mcp_kimchi_premium",
            caution="김치 프리미엄은 참고용 매크로 지표이며 주문 실행 신호가 아닙니다.",
        )
    return NaverCryptoKimchiPremium(
        baseSymbol=str(row.get("base_symbol") or row.get("symbol") or base_symbol).replace("KRW-", ""),
        premiumPct=_float_or_none(row.get("premium_pct") or row.get("kimchi_premium") or row.get("premium")),
        domesticPriceKrw=_float_or_none(row.get("domestic_price_krw") or row.get("upbit_price_krw") or row.get("upbit_price")),
        overseasPriceKrw=_float_or_none(row.get("overseas_price_krw") or row.get("binance_price_krw") or row.get("global_price_krw")),
        state="available",
        source="mcp_kimchi_premium",
        caution="김치 프리미엄은 참고용 매크로 지표이며 주문 실행 신호가 아닙니다.",
    )


async def build_naver_crypto_reference(
    *,
    db: AsyncSession,
    symbol: str | None = None,
    limit: int = 20,
    resolver: RelationResolver | None = None,
    providers: NaverCryptoReferenceProviders | None = None,
) -> NaverCryptoReferenceResponse:
    """Build a partial, source-labeled crypto reference response."""

    now = datetime.now(UTC)
    limit = max(1, min(int(limit or 20), 50))
    normalized_symbol = normalize_krw_symbol(symbol)
    base_symbol = _base_symbol(normalized_symbol) or "BTC"
    providers = providers or NaverCryptoReferenceProviders()
    resolver = resolver or RelationResolver()
    sources: list[CryptoReferenceSourceMeta] = []
    warnings: list[str] = [
        "naver_crypto_reference_only",
        "read_only_no_order_watch_or_broker_mutation",
    ]

    rank_rows: Sequence[Any] = []
    rank_provider = providers.rank_provider or _default_rank_provider
    try:
        rank_rows = await _maybe_await(rank_provider(db, limit))
        rank_fetched_at = _source_timestamp_from_rows(
            rank_rows, ("computed_at", "updated_at", "created_at", "snapshot_date")
        )
        sources.append(
            _cached_source_meta(
                source="tvscreener_upbit",
                label="Persisted crypto screener snapshots",
                has_data=bool(rank_rows),
                fetched_at=rank_fetched_at,
                now=now,
            )
        )
    except Exception:
        warnings.append("crypto_rank_snapshot_unavailable")
        sources.append(
            CryptoReferenceSourceMeta(
                source="tvscreener_upbit",
                label="Persisted crypto screener snapshots",
                state="error",
                freshness="missing",
                errorCode="crypto_rank_snapshot_unavailable",
            )
        )

    markets = [_market_from_row(row) for row in rank_rows]
    markets = [market for market in markets if market]
    if normalized_symbol and normalized_symbol not in markets:
        markets.insert(0, normalized_symbol)
    markets = list(dict.fromkeys(markets))[:limit]

    ticker_rows: list[dict[str, Any]] = []
    if markets:
        ticker_provider = providers.ticker_provider or _default_ticker_provider
        try:
            ticker_rows = list(await _maybe_await(ticker_provider(markets)))
            ticker_fetched_at = _source_timestamp_from_rows(
                ticker_rows,
                ("fetched_at", "fetchedAt", "cached_at", "updated_at", "timestamp", "trade_timestamp"),
            )
            sources.append(
                _provider_source_meta(
                    source="upbit_official",
                    label="Upbit official/public ticker",
                    has_data=bool(ticker_rows),
                    fetched_at=ticker_fetched_at,
                    now=now,
                    stale_after_seconds=60,
                )
            )
        except Exception:
            warnings.append("crypto_ticker_unavailable")
            sources.append(
                CryptoReferenceSourceMeta(
                    source="upbit_official",
                    label="Upbit official/public ticker",
                    state="error",
                    freshness="partial",
                    errorCode="crypto_ticker_unavailable",
                )
            )
    ticker_by_market = _ticker_map(ticker_rows)

    rank_items: list[NaverCryptoRankItem] = []
    for index, row in enumerate(rank_rows, start=1):
        market = _market_from_row(row)
        item = _rank_item_from_snapshot(rank=index, row=row, ticker=ticker_by_market.get(market or ""))
        if item:
            rank_items.append(item)

    if not rank_items:
        try:
            universe_rows = await _load_universe_fallback(db, limit=limit)
            rank_items = [
                _rank_item_from_universe(
                    rank=index,
                    row=row,
                    ticker=ticker_by_market.get(normalize_krw_symbol(row.market) or row.market.upper()),
                )
                for index, row in enumerate(universe_rows, start=1)
            ]
            if universe_rows:
                warnings.append("crypto_rank_snapshot_empty_used_universe_fallback")
        except Exception:
            warnings.append("crypto_universe_fallback_unavailable")

    profile = _build_profile(normalized_symbol, rank_items)
    sources.append(
        CryptoReferenceSourceMeta(
            source="naver_reference",
            label="Naver crypto reference fixture",
            state="fixture" if profile else "unavailable",
            freshness="fixture" if profile else "missing",
            referenceOnly=True,
        )
    )

    news: FeedNewsResponse | None = None
    news_provider = providers.news_provider or _default_news_provider
    try:
        news = await _maybe_await(news_provider(db, resolver, normalized_symbol, min(limit, 20)))
        sources.append(
            _cached_source_meta(
                source="feed_news",
                label="Persisted crypto news feed",
                has_data=bool(news and news.items),
                fetched_at=_parse_source_datetime(getattr(news, "asOf", None)),
                now=now,
            )
        )
        if news and news.meta.warnings:
            warnings.extend(f"news:{warning}" for warning in news.meta.warnings)
    except Exception:
        warnings.append("crypto_news_unavailable")
        sources.append(
            CryptoReferenceSourceMeta(
                source="feed_news",
                label="Persisted crypto news feed",
                state="error",
                freshness="partial",
                errorCode="crypto_news_unavailable",
            )
        )

    kimchi_payload: dict[str, Any] | list[dict[str, Any]] | None = None
    kimchi_provider = providers.kimchi_provider or _default_kimchi_provider
    try:
        kimchi_payload = await _maybe_await(kimchi_provider(base_symbol))
        kimchi_row = _first_kimchi_row(kimchi_payload)
        sources.append(
            _provider_source_meta(
                source="mcp_kimchi_premium",
                label="Kimchi premium reference",
                has_data=bool(kimchi_row),
                fetched_at=_source_timestamp_from_rows(
                    [kimchi_row] if kimchi_row else [],
                    ("fetched_at", "fetchedAt", "cached_at", "updated_at", "timestamp"),
                ),
                now=now,
                reference_only=True,
                stale_after_seconds=10 * 60,
            )
        )
    except Exception:
        warnings.append("crypto_kimchi_premium_unavailable")
        sources.append(
            CryptoReferenceSourceMeta(
                source="mcp_kimchi_premium",
                label="Kimchi premium reference",
                state="error",
                freshness="partial",
                errorCode="crypto_kimchi_premium_unavailable",
                referenceOnly=True,
            )
        )

    return NaverCryptoReferenceResponse(
        asOf=now,
        symbol=normalized_symbol,
        rank=rank_items[:limit],
        profile=profile,
        news=news,
        kimchiPremium=_build_kimchi(base_symbol, kimchi_payload),
        sources=sources,
        warnings=list(dict.fromkeys(warnings)),
        capabilities=NaverCryptoReferenceCapabilities(
            rank=CryptoCapabilityFlag(state="supported" if rank_items else "unavailable"),
            price=CryptoCapabilityFlag(state="supported" if ticker_rows or rank_items else "unavailable"),
            profile=CryptoCapabilityFlag(state="reference_only", reason="naver_fixture_reference_only"),
            news=CryptoCapabilityFlag(state="supported" if news else "unavailable"),
            kimchiPremium=CryptoCapabilityFlag(state="reference_only", reason="macro_reference_only"),
            execution=CryptoCapabilityFlag(state="read_only_mvp", reason="no_order_execution_controls"),
        ),
    )

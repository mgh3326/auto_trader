"""ROB-234 tests for the read-only Naver-style crypto adapter."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from app.schemas.invest_feed_news import FeedNewsItem, FeedNewsResponse
from app.services.invest_crypto_naver_adapter import (
    NaverCryptoReferenceProviders,
    build_naver_crypto_reference,
    normalize_krw_symbol,
)
from app.services.invest_view_model.relation_resolver import RelationResolver


@dataclass
class _SnapshotRow:
    symbol: str
    name: str
    latest_close: Decimal
    change_rate: Decimal | None = None
    trade_amount_24h: Decimal | None = None
    rsi: Decimal | None = None
    market_warning: bool = False
    source: str = "tvscreener_upbit"
    computed_at: datetime | None = None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_naver_crypto_reference_aggregates_rank_profile_news_and_kimchi() -> None:
    async def rank_provider(db: Any, limit: int):
        assert db == "db"
        assert limit == 5
        return [
            _SnapshotRow(
                symbol="KRW-BTC",
                name="비트코인",
                latest_close=Decimal("90000000"),
                change_rate=Decimal("0.0123"),
                trade_amount_24h=Decimal("1000000000"),
                rsi=Decimal("55.5"),
                computed_at=datetime(2026, 5, 14, 12, tzinfo=UTC),
            )
        ]

    async def ticker_provider(markets: list[str]):
        assert markets == ["KRW-BTC"]
        return [
            {
                "market": "KRW-BTC",
                "trade_price": 91000000,
                "timestamp": int(
                    datetime(2026, 5, 14, 12, tzinfo=UTC).timestamp() * 1000
                ),
            }
        ]

    async def news_provider(
        db: Any, resolver: RelationResolver, symbol: str | None, limit: int
    ):
        assert symbol == "KRW-BTC"
        return FeedNewsResponse(
            tab="crypto",
            asOf=datetime(2026, 5, 14, 12, tzinfo=UTC),
            items=[
                FeedNewsItem(
                    id=1,
                    title="BTC reference news",
                    publisher="fixture",
                    market="crypto",
                    url="https://example.test/btc",
                )
            ],
        )

    async def kimchi_provider(base_symbol: str):
        assert base_symbol == "BTC"
        return {
            "symbol": "BTC",
            "premium_pct": 2.5,
            "domestic_price_krw": 91000000,
            "overseas_price_krw": 88780000,
            "fetched_at": datetime(2026, 5, 14, 12, tzinfo=UTC),
        }

    response = await build_naver_crypto_reference(
        db="db",  # type: ignore[arg-type]
        symbol="BTC",
        limit=5,
        providers=NaverCryptoReferenceProviders(
            rank_provider=rank_provider,
            ticker_provider=ticker_provider,
            news_provider=news_provider,
            kimchi_provider=kimchi_provider,
        ),
    )

    assert response.symbol == "KRW-BTC"
    assert response.rank[0].symbol == "KRW-BTC"
    assert response.rank[0].priceKrw == 90000000.0
    assert response.profile is not None
    assert response.profile.naverUrl is not None
    assert response.profile.referenceNotes
    assert response.news is not None
    assert response.news.items[0].title == "BTC reference news"
    assert response.kimchiPremium is not None
    assert response.kimchiPremium.premiumPct == 2.5
    assert response.capabilities.execution.state == "read_only_mvp"
    assert "naver_crypto_reference_only" in response.warnings
    source_by_name = {source.source: source for source in response.sources}
    assert source_by_name["naver_reference"].referenceOnly is True
    assert source_by_name["naver_reference"].freshness == "fixture"
    assert source_by_name["tvscreener_upbit"].state in {"cached", "stale"}
    assert source_by_name["tvscreener_upbit"].fetchedAt == datetime(
        2026, 5, 14, 12, tzinfo=UTC
    )
    assert source_by_name["tvscreener_upbit"].cacheAgeSeconds is not None
    assert source_by_name["upbit_official"].fetchedAt == datetime(
        2026, 5, 14, 12, tzinfo=UTC
    )
    assert source_by_name["upbit_official"].cacheAgeSeconds is not None
    assert source_by_name["feed_news"].fetchedAt == datetime(
        2026, 5, 14, 12, tzinfo=UTC
    )
    assert source_by_name["mcp_kimchi_premium"].referenceOnly is True
    assert source_by_name["mcp_kimchi_premium"].fetchedAt == datetime(
        2026, 5, 14, 12, tzinfo=UTC
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_naver_crypto_reference_defaults_do_not_call_uncached_live_upbit_ticker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden_live_fetch(markets: list[str]):
        raise AssertionError(f"unexpected live Upbit ticker fetch: {markets}")

    monkeypatch.setitem(
        sys.modules,
        "app.services.brokers.upbit.client",
        SimpleNamespace(fetch_multiple_tickers=forbidden_live_fetch),
    )

    async def rank_provider(db: Any, limit: int):
        return [
            _SnapshotRow(
                symbol="KRW-BTC",
                name="비트코인",
                latest_close=Decimal("90000000"),
                change_rate=Decimal("0.0123"),
                computed_at=datetime.now(UTC) - timedelta(minutes=5),
            )
        ]

    async def news_provider(
        db: Any, resolver: RelationResolver, symbol: str | None, limit: int
    ):
        return FeedNewsResponse(
            tab="crypto",
            asOf=datetime.now(UTC) - timedelta(minutes=5),
            items=[],
        )

    response = await build_naver_crypto_reference(
        db="db",  # type: ignore[arg-type]
        symbol="BTC",
        providers=NaverCryptoReferenceProviders(
            rank_provider=rank_provider,
            news_provider=news_provider,
        ),
    )

    assert response.rank[0].priceKrw == 90000000.0
    source_by_name = {source.source: source for source in response.sources}
    assert source_by_name["upbit_official"].state == "unavailable"
    assert source_by_name["upbit_official"].freshness == "missing"
    assert source_by_name["mcp_kimchi_premium"].state == "unavailable"
    assert source_by_name["mcp_kimchi_premium"].referenceOnly is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_naver_crypto_reference_marks_stale_cached_read_model_sources() -> None:
    stale_at = datetime.now(UTC) - timedelta(days=3)

    async def rank_provider(db: Any, limit: int):
        return [
            _SnapshotRow(
                symbol="KRW-BTC",
                name="비트코인",
                latest_close=Decimal("90000000"),
                computed_at=stale_at,
            )
        ]

    async def news_provider(
        db: Any, resolver: RelationResolver, symbol: str | None, limit: int
    ):
        return FeedNewsResponse(
            tab="crypto",
            asOf=stale_at,
            items=[
                FeedNewsItem(
                    id=2,
                    title="old cached crypto news",
                    publisher="fixture",
                    market="crypto",
                    url="https://example.test/old",
                )
            ],
        )

    response = await build_naver_crypto_reference(
        db="db",  # type: ignore[arg-type]
        symbol="BTC",
        providers=NaverCryptoReferenceProviders(
            rank_provider=rank_provider,
            news_provider=news_provider,
        ),
    )

    source_by_name = {source.source: source for source in response.sources}
    assert source_by_name["tvscreener_upbit"].state == "stale"
    assert source_by_name["tvscreener_upbit"].freshness == "stale"
    assert source_by_name["tvscreener_upbit"].fetchedAt == stale_at
    assert source_by_name["tvscreener_upbit"].cacheAgeSeconds is not None
    assert source_by_name["feed_news"].state == "stale"
    assert source_by_name["feed_news"].fetchedAt == stale_at
    assert source_by_name["naver_reference"].state == "fixture"
    assert source_by_name["naver_reference"].cacheAgeSeconds is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_naver_crypto_reference_gracefully_records_provider_failures() -> None:
    async def rank_provider(db: Any, limit: int):
        raise RuntimeError("rank down")

    async def ticker_provider(markets: list[str]):
        raise RuntimeError("ticker down")

    async def news_provider(
        db: Any, resolver: RelationResolver, symbol: str | None, limit: int
    ):
        raise RuntimeError("news down")

    async def kimchi_provider(base_symbol: str):
        raise RuntimeError("kimchi down")

    response = await build_naver_crypto_reference(
        db=object(),  # type: ignore[arg-type]
        symbol="KRW-BTC",
        providers=NaverCryptoReferenceProviders(
            rank_provider=rank_provider,
            ticker_provider=ticker_provider,
            news_provider=news_provider,
            kimchi_provider=kimchi_provider,
        ),
    )

    assert response.market == "crypto"
    assert response.profile is not None
    assert response.profile.symbol == "KRW-BTC"
    assert response.kimchiPremium is not None
    assert response.kimchiPremium.state == "unavailable"
    assert "crypto_rank_snapshot_unavailable" in response.warnings
    assert "crypto_news_unavailable" in response.warnings
    assert "crypto_kimchi_premium_unavailable" in response.warnings
    error_sources = {
        source.source for source in response.sources if source.state == "error"
    }
    assert {"tvscreener_upbit", "feed_news", "mcp_kimchi_premium"} <= error_sources


@pytest.mark.unit
def test_normalize_krw_symbol_accepts_base_and_pair_aliases() -> None:
    assert normalize_krw_symbol("BTC") == "KRW-BTC"
    assert normalize_krw_symbol("btc-krw") == "KRW-BTC"
    assert normalize_krw_symbol("KRW/ETH") == "KRW-ETH"

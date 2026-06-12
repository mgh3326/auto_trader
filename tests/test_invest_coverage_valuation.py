from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
import sqlalchemy as sa

from app.models.market_quote_snapshot import MarketQuoteSnapshot
from app.models.market_valuation_snapshot import MarketValuationSnapshot
from app.services.invest_coverage_service import build_invest_coverage


@pytest.mark.asyncio
async def test_quote_coverage_uses_durable_snapshots_with_naver_candidate(db_session):
    now = dt.datetime.now(dt.UTC).replace(microsecond=0)
    await db_session.execute(
        sa.delete(MarketQuoteSnapshot).where(
            MarketQuoteSnapshot.symbol.in_(["906201", "906202"])
        )
    )
    await db_session.commit()
    db_session.add_all(
        [
            MarketQuoteSnapshot(
                market="kr",
                symbol="906201",
                source="kis",
                snapshot_at=now - dt.timedelta(minutes=5),
                price=Decimal("1000"),
            ),
            MarketQuoteSnapshot(
                market="kr",
                symbol="906202",
                source="kis",
                snapshot_at=now - dt.timedelta(hours=3),
                price=Decimal("900"),
            ),
        ]
    )
    await db_session.commit()

    response = await build_invest_coverage(db_session, market="kr")
    quote = next(
        s for s in response.surfaces if s.market == "kr" and s.surface == "quotes"
    )

    assert quote.state == "partial"
    assert quote.sourceOfTruth == "market_quote_snapshots"
    assert quote.counts.fresh >= 1
    assert quote.counts.stale >= 1
    assert quote.actionability.queue == "market-quote-snapshots"
    assert quote.actionability.approvalGates == ["production_db_write_approval"]
    assert quote.sourceCandidates[0].name == "naver_finance"
    assert quote.sourceCandidates[0].readiness == "request_time_only"


@pytest.mark.asyncio
async def test_valuation_coverage_uses_durable_snapshots(db_session):
    trading_day = dt.date(2026, 5, 12)
    await db_session.execute(
        sa.delete(MarketValuationSnapshot).where(
            MarketValuationSnapshot.symbol.in_(["906301", "906302"])
        )
    )
    await db_session.commit()
    db_session.add_all(
        [
            MarketValuationSnapshot(
                market="kr",
                symbol="906301",
                source="naver_finance",
                snapshot_date=trading_day,
                per=Decimal("10.5"),
            ),
            MarketValuationSnapshot(
                market="kr",
                symbol="906302",
                source="naver_finance",
                snapshot_date=trading_day - dt.timedelta(days=5),
                pbr=Decimal("1.2"),
            ),
        ]
    )
    await db_session.commit()

    response = await build_invest_coverage(db_session, market="kr", as_of=trading_day)
    valuation = next(
        s
        for s in response.surfaces
        if s.market == "kr" and s.surface == "valuation_fundamentals"
    )

    assert valuation.state == "partial"
    assert valuation.sourceOfTruth == "market_valuation_snapshots"
    assert valuation.counts.fresh >= 1
    assert valuation.counts.stale >= 1
    assert valuation.actionability.queue == "market-valuation-snapshots"
    assert valuation.actionability.approvalGates == ["production_db_write_approval"]


def test_provider_unwired_no_longer_lists_durable_kr_us_surfaces():
    from app.services.invest_coverage_service import _provider_unwired_surfaces

    for market in ("kr", "us", "all"):
        surfaces = {
            (surface.surface, surface.market)
            for surface in _provider_unwired_surfaces(market)
        }
        assert ("quotes", "kr") not in surfaces
        assert ("ohlcv", "kr") not in surfaces
        assert ("valuation_fundamentals", "kr") not in surfaces
        assert ("quotes", "us") not in surfaces
        assert ("ohlcv", "us") not in surfaces
        assert ("valuation_fundamentals", "us") not in surfaces


@pytest.mark.asyncio
async def test_quote_builder_supports_crypto_and_redacts_payload():
    from app.services.market_data.contracts import Quote
    from app.services.market_quote_snapshots.builder import (
        build_quote_snapshots_for_market,
    )

    async def fake_fetcher(symbol: str, market: str) -> Quote:
        assert market == "crypto"
        return Quote(
            symbol=symbol,
            market=market,
            price=123.45,
            source="upbit",
            previous_close=120.0,
            volume=10,
            value=1234.5,
        )

    result = await build_quote_snapshots_for_market(
        market="crypto",
        symbols=["KRW-BTC"],
        now=dt.datetime(2026, 5, 12, 1, 2, 3, tzinfo=dt.UTC),
        fetcher=fake_fetcher,
    )

    assert not result.warnings
    assert len(result.payloads) == 1
    payload = result.payloads[0]
    assert payload.market == "crypto"
    assert payload.symbol == "KRW-BTC"
    assert payload.source == "upbit"
    assert payload.price == Decimal("123.45")


@pytest.mark.asyncio
async def test_quote_repository_upsert_and_coverage_counts(db_session):
    from app.services.market_quote_snapshots.repository import (
        MarketQuoteSnapshotsRepository,
        MarketQuoteSnapshotUpsert,
    )

    market = "crypto"
    now = dt.datetime(2099, 5, 12, 9, 0, tzinfo=dt.UTC).replace(microsecond=0)
    await db_session.execute(
        sa.delete(MarketQuoteSnapshot).where(
            MarketQuoteSnapshot.market == market,
            MarketQuoteSnapshot.symbol.in_(["906401", "906402"]),
        )
    )
    await db_session.commit()

    repo = MarketQuoteSnapshotsRepository(db_session)
    inserted = await repo.upsert(
        [
            MarketQuoteSnapshotUpsert(
                market=market,
                symbol="906401",
                source="kis",
                snapshot_at=now,
                price=Decimal("10"),
            ),
            MarketQuoteSnapshotUpsert(
                market=market,
                symbol="906402",
                source="kis",
                snapshot_at=now - dt.timedelta(hours=2),
                price=Decimal("20"),
            ),
        ]
    )
    await db_session.commit()

    counts = await repo.coverage_counts(
        market, fresh_after=now - dt.timedelta(minutes=30)
    )
    assert inserted == 2
    assert counts.fresh_symbols >= 1
    assert counts.stale_symbols >= 1
    assert counts.latest_snapshot_at == now


@pytest.mark.asyncio
async def test_valuation_builder_and_repository_upsert(db_session):
    from app.services.market_valuation_snapshots.builder import (
        build_valuation_snapshots_for_market,
    )
    from app.services.market_valuation_snapshots.repository import (
        MarketValuationSnapshotsRepository,
    )

    snapshot_date = dt.date(2026, 5, 12)

    async def fake_fetcher(symbol: str, market: str) -> dict[str, object]:
        assert market == "kr"
        return {"per": "11.2", "pbr": "0.8", "api_token": "do-not-store"}

    result = await build_valuation_snapshots_for_market(
        market="kr",
        symbols=["906501"],
        snapshot_date=snapshot_date,
        fetcher=fake_fetcher,
    )
    assert len(result.payloads) == 1
    payload = result.payloads[0]
    assert payload.per == Decimal("11.2")
    assert payload.raw_payload["api_token"] == "[REDACTED]"

    await db_session.execute(
        sa.delete(MarketValuationSnapshot).where(
            MarketValuationSnapshot.symbol == "906501"
        )
    )
    await db_session.commit()
    repo = MarketValuationSnapshotsRepository(db_session)
    assert await repo.upsert(result.payloads) == 1
    await db_session.commit()
    counts = await repo.coverage_counts("kr", fresh_date=snapshot_date)
    assert counts.fresh_symbols >= 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_valuation_builder_threads_high_52w_date_us(db_session):
    # ROB-440 PR3: the US fetcher's high_52w_date (iso string in raw, JSON-safe) →
    # parsed to a date in the payload + persisted to the high_52w_date column
    # (powers undervalued_breakout date-recency).
    from app.services.market_valuation_snapshots.builder import (
        build_valuation_snapshots_for_market,
    )
    from app.services.market_valuation_snapshots.repository import (
        MarketValuationSnapshotsRepository,
    )

    snapshot_date = dt.date(2026, 5, 12)
    sym = "ZZ9001"

    async def fake_fetcher(symbol: str, market: str) -> dict[str, object]:
        assert market == "us"
        return {
            "per": "8",
            "pbr": "0.8",
            "yearHigh": "100",
            "high_52w_date": "2026-05-01",  # iso string (JSON-safe in raw_payload)
        }

    result = await build_valuation_snapshots_for_market(
        market="us",
        symbols=[sym],
        snapshot_date=snapshot_date,
        fetcher=fake_fetcher,
    )
    assert len(result.payloads) == 1
    assert result.payloads[0].high_52w_date == dt.date(2026, 5, 1)
    assert (
        result.payloads[0].raw_payload["high_52w_date"] == "2026-05-01"
    )  # not a date obj

    await db_session.execute(
        sa.delete(MarketValuationSnapshot).where(MarketValuationSnapshot.symbol == sym)
    )
    await db_session.commit()
    repo = MarketValuationSnapshotsRepository(db_session)
    assert await repo.upsert(result.payloads) == 1
    await db_session.commit()
    row = (
        await db_session.execute(
            sa.select(MarketValuationSnapshot).where(
                MarketValuationSnapshot.symbol == sym,
                MarketValuationSnapshot.snapshot_date == snapshot_date,
            )
        )
    ).scalar_one()
    assert row.high_52w_date == dt.date(2026, 5, 1)  # persisted to the column
    await db_session.execute(
        sa.delete(MarketValuationSnapshot).where(MarketValuationSnapshot.symbol == sym)
    )
    await db_session.commit()


# --- ROB-440 PR4: US valuation scale (common-stock filter + high_52w_date opt-in) ---


@pytest.mark.unit
@pytest.mark.asyncio
async def test_default_valuation_fetcher_high_date_opt_in(monkeypatch):
    from app.services.market_valuation_snapshots import builder

    calls = {"high": 0}

    async def _fast(sym):  # noqa: ANN001
        return {"symbol": sym}

    async def _fund(sym):  # noqa: ANN001
        return {"PER": 8.0}

    async def _high(sym):  # noqa: ANN001
        calls["high"] += 1
        return dt.date(2026, 5, 20)

    monkeypatch.setattr("app.services.brokers.yahoo.client.fetch_fast_info", _fast)
    monkeypatch.setattr(
        "app.services.brokers.yahoo.client.fetch_fundamental_info", _fund
    )
    monkeypatch.setattr("app.services.brokers.yahoo.client.fetch_52w_high_date", _high)

    # opt-out (default): no OHLC call, no high_52w_date → light bulk backfill
    raw = await builder.default_valuation_fetcher("AAPL", "us")
    assert calls["high"] == 0
    assert "high_52w_date" not in raw

    # opt-in: OHLC fetched, high_52w_date as JSON-safe iso string
    raw2 = await builder.default_valuation_fetcher("AAPL", "us", include_high_date=True)
    assert calls["high"] == 1
    assert raw2["high_52w_date"] == "2026-05-20"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resolve_us_common_stocks_only(db_session):
    from app.jobs.market_valuation_snapshots import resolve_active_universe
    from app.models.us_symbol_universe import USSymbolUniverse

    syms = ["ZZCOM1", "ZZETF1", "ZZINA1"]
    await db_session.execute(
        sa.delete(USSymbolUniverse).where(USSymbolUniverse.symbol.in_(syms))
    )
    db_session.add_all(
        [
            USSymbolUniverse(
                symbol="ZZCOM1", exchange="NASDAQ", is_active=True, is_common_stock=True
            ),
            USSymbolUniverse(
                symbol="ZZETF1", exchange="NYSE", is_active=True, is_common_stock=False
            ),
            USSymbolUniverse(
                symbol="ZZINA1",
                exchange="NASDAQ",
                is_active=False,
                is_common_stock=True,
            ),
        ]
    )
    await db_session.commit()

    common = set(await resolve_active_universe("us", common_stocks_only=True))
    assert "ZZCOM1" in common  # common + active
    assert "ZZETF1" not in common  # is_common_stock=False excluded
    assert "ZZINA1" not in common  # inactive excluded

    unfiltered = set(await resolve_active_universe("us"))
    assert {"ZZCOM1", "ZZETF1"} <= unfiltered  # active non-common included w/o filter

    await db_session.execute(
        sa.delete(USSymbolUniverse).where(USSymbolUniverse.symbol.in_(syms))
    )
    await db_session.commit()


@pytest.mark.unit
def test_build_market_valuation_cli_flags():
    from scripts.build_market_valuation_snapshots import parse_args

    base = parse_args(["--market", "us", "--symbol", "AAPL"])
    assert base.common_stocks_only is False
    assert base.include_high_date is False

    opted = parse_args(
        ["--market", "us", "--all", "--common-stocks-only", "--with-high-52w-date"]
    )
    assert opted.common_stocks_only is True
    assert opted.include_high_date is True


@pytest.mark.integration
@pytest.mark.asyncio
async def test_us_valuation_partition_computed_at_helper(db_session):
    # ROB-440 freshness chip: backfill the US market_valuation partition computed_at
    # (the US fundamentals/valuation dispatch branches don't propagate it).
    from app.services.invest_view_model.screener_service import (
        _us_valuation_partition_computed_at,
    )

    vd = dt.date(2099, 12, 31)
    await db_session.execute(
        sa.delete(MarketValuationSnapshot).where(
            MarketValuationSnapshot.snapshot_date == vd
        )
    )
    db_session.add(
        MarketValuationSnapshot(
            market="us",
            symbol="ZZ9100",
            snapshot_date=vd,
            source="yahoo",
            per=Decimal("8"),
            computed_at=dt.datetime(2099, 12, 31, 21, 22, tzinfo=dt.UTC),
        )
    )
    await db_session.commit()

    got = await _us_valuation_partition_computed_at(db_session, snapshot_date=vd)
    assert got is not None  # partition exists → computed_at surfaced
    # no US partition on this date → None (fail-open)
    assert (
        await _us_valuation_partition_computed_at(
            db_session, snapshot_date=dt.date(2098, 1, 1)
        )
        is None
    )
    await db_session.execute(
        sa.delete(MarketValuationSnapshot).where(
            MarketValuationSnapshot.snapshot_date == vd
        )
    )
    await db_session.commit()


@pytest.mark.asyncio
async def test_market_valuation_accepts_toss_openapi_market_cap(db_session) -> None:
    from app.services.market_valuation_snapshots.repository import (
        MarketValuationSnapshotsRepository,
        MarketValuationSnapshotUpsert,
    )

    repo = MarketValuationSnapshotsRepository(db_session)
    await db_session.execute(
        sa.delete(MarketValuationSnapshot).where(
            MarketValuationSnapshot.symbol == "005930",
            MarketValuationSnapshot.snapshot_date == dt.date(2026, 6, 12),
            MarketValuationSnapshot.source == "toss_openapi",
        )
    )
    await db_session.commit()

    await repo.upsert(
        [
            MarketValuationSnapshotUpsert(
                market="kr",
                symbol="005930",
                snapshot_date=dt.date(2026, 6, 12),
                source="toss_openapi",
                market_cap=Decimal("409239502560000"),
                raw_payload={"source": "toss_openapi"},
            )
        ]
    )
    await db_session.commit()

    rows = await repo.latest_for_symbols(market="kr", symbols={"005930"})
    matching = [
        r
        for r in rows
        if r.source == "toss_openapi" and r.snapshot_date == dt.date(2026, 6, 12)
    ]
    assert len(matching) == 1
    assert matching[0].market_cap == Decimal("409239502560000")

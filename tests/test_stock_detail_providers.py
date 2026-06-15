from __future__ import annotations

import datetime as dt
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.schemas.invest_stock_detail import StockDetailHolding
from app.services.market_data.contracts import (
    Candle,
    OrderbookLevel,
    OrderbookSnapshot,
    Quote,
)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_market_data_candle_provider_maps_period_and_rows(monkeypatch):
    from app.services.invest_view_model import stock_detail_providers as providers

    calls = []

    async def fake_get_ohlcv(symbol: str, market: str, period: str, count: int):
        calls.append((symbol, market, period, count))
        return [
            Candle(
                symbol=symbol,
                market="equity_kr",
                source="kis",
                period=period,
                timestamp=dt.datetime(2026, 6, 15, tzinfo=dt.UTC),
                open=100,
                high=110,
                low=90,
                close=105,
                volume=1234,
            )
        ]

    import app.services.market_data.service as market_data_service

    monkeypatch.setattr(market_data_service, "get_ohlcv", fake_get_ohlcv)

    rows = await providers.stock_detail_candle_provider("kr", "000270", "1d")

    assert calls == [("000270", "kr", "day", 200)]
    assert rows == [
        {
            "ts": dt.datetime(2026, 6, 15, tzinfo=dt.UTC),
            "open": 100,
            "high": 110,
            "low": 90,
            "close": 105,
            "volume": 1234,
        }
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_market_data_quote_provider_maps_quote(monkeypatch):
    from app.services.invest_view_model import stock_detail_providers as providers

    async def fake_get_quote(symbol: str, market: str):
        return Quote(
            symbol=symbol,
            market="equity_us",
            price=211.34,
            source="yahoo",
            previous_close=209.12,
        )

    import app.services.market_data.service as market_data_service

    monkeypatch.setattr(market_data_service, "get_quote", fake_get_quote)

    quote = await providers.stock_detail_quote_provider("us", "QQQM", object())

    assert quote is not None
    assert quote.price == pytest.approx(211.34)
    assert quote.previousClose == pytest.approx(209.12)
    assert quote.changeAmount == pytest.approx(2.22)
    assert quote.changeRate == pytest.approx((2.22 / 209.12) * 100)
    assert quote.priceState == "live"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_market_data_quote_provider_preserves_missing_price(monkeypatch):
    from app.services.invest_view_model import stock_detail_providers as providers

    async def fake_get_quote(symbol: str, market: str):
        return Quote(
            symbol=symbol,
            market="equity_kr",
            price=None,
            source="kis",
            previous_close=0,
        )

    import app.services.market_data.service as market_data_service

    monkeypatch.setattr(market_data_service, "get_quote", fake_get_quote)

    quote = await providers.stock_detail_quote_provider("kr", "005930", object())

    assert quote is not None
    assert quote.changeAmount is None
    assert quote.changeRate is None
    assert quote.priceState == "missing"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_market_data_orderbook_provider_maps_kr_snapshot(monkeypatch):
    from app.services.invest_view_model import stock_detail_providers as providers

    async def fake_get_orderbook(
        symbol: str, market: str = "kr", venue: str | None = None
    ):
        return OrderbookSnapshot(
            symbol=symbol,
            instrument_type="equity_kr",
            source="kis",
            asks=[OrderbookLevel(price=71100, quantity=10)],
            bids=[OrderbookLevel(price=71000, quantity=12)],
            total_ask_qty=10,
            total_bid_qty=12,
            bid_ask_ratio=1.2,
        )

    import app.services.market_data.service as market_data_service

    monkeypatch.setattr(market_data_service, "get_orderbook", fake_get_orderbook)

    orderbook = await providers.stock_detail_orderbook_provider(
        "kr", "005930", object()
    )

    assert orderbook is not None
    assert orderbook.asks[0].price == 71100
    assert orderbook.bids[0].quantity == 12


@pytest.mark.unit
@pytest.mark.asyncio
async def test_market_data_orderbook_provider_unsupported_for_us(monkeypatch):
    from app.services.invest_view_model import stock_detail_providers as providers

    async def fail_get_orderbook(*args, **kwargs):
        raise AssertionError("US orderbook should not call market data")

    import app.services.market_data.service as market_data_service

    monkeypatch.setattr(market_data_service, "get_orderbook", fail_get_orderbook)

    orderbook = await providers.stock_detail_orderbook_provider("us", "AAPL", object())

    assert orderbook is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_market_data_orderbook_provider_empty_book_returns_none(monkeypatch):
    from app.services.invest_view_model import stock_detail_providers as providers

    async def fake_get_orderbook(
        symbol: str, market: str = "kr", venue: str | None = None
    ):
        return OrderbookSnapshot(
            symbol=symbol,
            instrument_type="equity_kr",
            source="kis",
            asks=[],
            bids=[],
            total_ask_qty=0,
            total_bid_qty=0,
            bid_ask_ratio=None,
        )

    import app.services.market_data.service as market_data_service

    monkeypatch.setattr(market_data_service, "get_orderbook", fake_get_orderbook)

    orderbook = await providers.stock_detail_orderbook_provider(
        "kr", "005930", object()
    )

    assert orderbook is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_holding_provider_uses_account_panel_parity_without_paper():
    from app.services.invest_view_model.stock_detail_providers import (
        make_account_panel_holding_provider,
    )

    class FakeHomeService:
        async def build_account_panel_view(
            self, *, user_id: int, include_paper: bool = False, paper_sources=None
        ):
            assert user_id == 7
            assert include_paper is False
            assert paper_sources is None
            return SimpleNamespace(
                groupedHoldings=[
                    SimpleNamespace(
                        symbol="000270",
                        market="KR",
                        totalQuantity=3,
                        tradeableQuantity=2,
                        sellableQuantity=1,
                        pendingSellQuantity=1,
                        referenceQuantity=1,
                        averageCost=80000,
                        costBasis=240000,
                        valueNative=255000,
                        valueKrw=255000,
                        pnlKrw=15000,
                        pnlRate=0.0625,
                        includedSources=["kis", "toss_manual"],
                        priceState="live",
                    )
                ]
            )

    provider = make_account_panel_holding_provider(FakeHomeService())

    holding = await provider(7, "kr", "000270", object())

    assert isinstance(holding, StockDetailHolding)
    assert holding.totalQuantity == 3
    assert holding.tradeableQuantity == 2
    assert holding.referenceQuantity == 1
    assert holding.includedSources == ["kis", "toss_manual"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_holding_provider_returns_none_when_symbol_not_held():
    from app.services.invest_view_model.stock_detail_providers import (
        make_account_panel_holding_provider,
    )

    class FakeHomeService:
        async def build_account_panel_view(
            self, *, user_id: int, include_paper: bool = False, paper_sources=None
        ):
            return SimpleNamespace(
                groupedHoldings=[
                    SimpleNamespace(symbol="000660", market="KR"),
                    SimpleNamespace(symbol="AAPL", market="US"),
                ]
            )

    provider = make_account_panel_holding_provider(FakeHomeService())

    assert await provider(7, "kr", "000270", object()) is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_valuation_provider_converts_dividend_ratio_to_display_percent(
    monkeypatch,
):
    from app.services.invest_view_model import stock_detail_providers as providers

    computed_at = dt.datetime(2026, 6, 15, tzinfo=dt.UTC)

    class FakeRepo:
        def __init__(self, db):
            self.db = db

        async def latest_for_symbols(self, *, market, symbols):
            assert market == "kr"
            assert symbols == {"005930"}
            return [
                SimpleNamespace(
                    per=Decimal("12.3"),
                    pbr=Decimal("1.1"),
                    roe=Decimal("8.5"),
                    dividend_yield=Decimal("0.0256"),
                    high_52w=Decimal("90000"),
                    low_52w=Decimal("60000"),
                    market_cap=Decimal("500000000000000"),
                    source="naver_finance",
                    computed_at=computed_at,
                )
            ]

    import app.services.market_valuation_snapshots as valuation_pkg

    monkeypatch.setattr(valuation_pkg, "MarketValuationSnapshotsRepository", FakeRepo)

    valuation = await providers.stock_detail_valuation_provider(
        "kr", "005930", SimpleNamespace(execute=object())
    )

    assert valuation is not None
    assert valuation.dividendYield == pytest.approx(2.56)
    assert valuation.asOf == computed_at


@pytest.mark.unit
@pytest.mark.asyncio
async def test_valuation_provider_returns_none_when_no_snapshot(monkeypatch):
    from app.services.invest_view_model import stock_detail_providers as providers

    class FakeRepo:
        def __init__(self, db):
            self.db = db

        async def latest_for_symbols(self, *, market, symbols):
            return []

    import app.services.market_valuation_snapshots as valuation_pkg

    monkeypatch.setattr(valuation_pkg, "MarketValuationSnapshotsRepository", FakeRepo)

    valuation = await providers.stock_detail_valuation_provider(
        "kr", "005930", SimpleNamespace(execute=object())
    )

    assert valuation is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_valuation_provider_handles_unsupported_and_null_dividend(monkeypatch):
    from app.services.invest_view_model import stock_detail_providers as providers

    computed_at = dt.datetime(2026, 6, 15, tzinfo=dt.UTC)

    class FakeRepo:
        def __init__(self, db):
            self.db = db

        async def latest_for_symbols(self, *, market, symbols):
            return [
                SimpleNamespace(
                    per=None,
                    pbr=None,
                    roe=None,
                    dividend_yield=None,
                    high_52w=None,
                    low_52w=None,
                    market_cap=None,
                    source="fixture",
                    computed_at=computed_at,
                )
            ]

    import app.services.market_valuation_snapshots as valuation_pkg

    monkeypatch.setattr(valuation_pkg, "MarketValuationSnapshotsRepository", FakeRepo)

    assert (
        await providers.stock_detail_valuation_provider("crypto", "KRW-BTC", object())
        is None
    )
    assert (
        await providers.stock_detail_valuation_provider("kr", "005930", object())
        is None
    )

    valuation = await providers.stock_detail_valuation_provider(
        "kr", "005930", SimpleNamespace(execute=object())
    )

    assert valuation is not None
    assert valuation.dividendYield is None


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reasons", "expected"),
    [
        (["first", "second", "third", "fourth"], ["first", "second", "third"]),
        ({"top3": ["one", "two", "three", "four"]}, ["one", "two", "three"]),
        ({"summary": "not-a-list"}, []),
    ],
)
async def test_latest_analysis_provider_maps_reason_shapes(
    monkeypatch, reasons, expected
):
    from app.services.invest_view_model import stock_detail_providers as providers

    created_at = dt.datetime(2026, 6, 15, tzinfo=dt.UTC)

    class FakeAnalysisService:
        def __init__(self, db):
            self.db = db

        async def get_latest_analysis_by_symbol(self, symbol):
            assert symbol == "005930"
            return SimpleNamespace(
                id=42,
                model_name="test-model",
                decision="buy",
                confidence=85,
                appropriate_buy_min=70000,
                appropriate_buy_max=72000,
                appropriate_sell_min=80000,
                appropriate_sell_max=82000,
                reasons=reasons,
                created_at=created_at,
            )

    import app.services.stock_info_service as stock_info_service

    monkeypatch.setattr(stock_info_service, "StockAnalysisService", FakeAnalysisService)

    analysis = await providers.stock_detail_latest_analysis_provider(
        "kr", "005930", SimpleNamespace(execute=object())
    )

    assert analysis is not None
    assert analysis.id == 42
    assert analysis.confidence == pytest.approx(0.85)
    assert analysis.reasonsTop3 == expected


@pytest.mark.unit
@pytest.mark.asyncio
async def test_latest_analysis_provider_returns_none_without_db_or_analysis(
    monkeypatch,
):
    from app.services.invest_view_model import stock_detail_providers as providers

    class FakeAnalysisService:
        def __init__(self, db):
            self.db = db

        async def get_latest_analysis_by_symbol(self, symbol):
            return None

    import app.services.stock_info_service as stock_info_service

    monkeypatch.setattr(stock_info_service, "StockAnalysisService", FakeAnalysisService)

    assert (
        await providers.stock_detail_latest_analysis_provider("kr", "005930", object())
        is None
    )
    assert (
        await providers.stock_detail_latest_analysis_provider(
            "kr", "005930", SimpleNamespace(execute=object())
        )
        is None
    )

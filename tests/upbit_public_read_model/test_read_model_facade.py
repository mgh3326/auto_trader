import pytest

from app.services.upbit_public_read_model import (
    UpbitPublicReadModel,
    UpbitPublicSnapshot,
)
from app.services.upbit_public_read_model.types import (
    UpbitBlockMeta,
    UpbitMarketWarningEntry,
    UpbitMarketWarningsBlock,
)


async def _inline_warnings_provider(markets):
    return UpbitMarketWarningsBlock(
        meta=UpbitBlockMeta(source="upbit_market_warnings", state="fresh", label="w"),
        entries={
            m: UpbitMarketWarningEntry(market=m, warning="NONE")
            for m in (markets or [])
        },
    )


@pytest.mark.asyncio
async def test_snapshot_composes_all_blocks(fake_redis):
    async def ticker_fetcher(markets):
        return [{"market": m, "trade_price": 1.0} for m in markets]

    async def orderbook_fetcher(markets):
        return {
            m: {
                "market": m,
                "orderbook_units": [
                    {"ask_price": 10.0, "bid_price": 9.0, "ask_size": 1, "bid_size": 1}
                ],
            }
            for m in markets
        }

    async def trades_fetcher(market, count):
        return [{"market": market, "trade_price": 1.0}]

    rm = UpbitPublicReadModel(
        redis=fake_redis,
        ticker_fetcher=ticker_fetcher,
        orderbook_fetcher=orderbook_fetcher,
        trades_fetcher=trades_fetcher,
        warnings_provider=_inline_warnings_provider,
    )
    snap = await rm.snapshot(["KRW-BTC"], include_trades_for=["KRW-BTC"])
    assert isinstance(snap, UpbitPublicSnapshot)
    assert snap.ticker.meta.state == "fresh"
    assert snap.orderbook.spreadsPct["KRW-BTC"] > 0
    assert snap.trades is not None and snap.trades.trades["KRW-BTC"]
    assert {m.source for m in snap.sources} >= {
        "upbit_ticker",
        "upbit_orderbook",
        "upbit_trades",
        "upbit_market_warnings",
    }

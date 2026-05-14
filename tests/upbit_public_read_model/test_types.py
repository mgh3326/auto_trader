from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.schemas.invest_crypto import CryptoSourceState
from app.services.upbit_public_read_model.types import (
    UpbitBlockMeta,
    UpbitMarketWarningsBlock,
    UpbitOrderbookBlock,
    UpbitPublicSnapshot,
    UpbitTickerBlock,
    to_crypto_source_state,
)


def test_upbit_block_meta_is_frozen_and_extra_forbid():
    meta = UpbitBlockMeta(source="upbit_ticker", state="fresh", label="Upbit ticker")
    with pytest.raises(ValidationError):
        UpbitBlockMeta(source="upbit_ticker", state="fresh", label="x", bogus=1)
    with pytest.raises(ValidationError):
        meta.source = "upbit_orderbook"


def test_to_crypto_source_state_maps_states():
    now = datetime.now(UTC)
    fresh = to_crypto_source_state(
        UpbitBlockMeta(
            source="upbit_ticker", state="fresh", label="Upbit ticker", fetchedAt=now
        )
    )
    assert isinstance(fresh, CryptoSourceState)
    assert fresh.state == "supported"
    stale = to_crypto_source_state(
        UpbitBlockMeta(source="upbit_ticker", state="stale", label="Upbit ticker")
    )
    assert stale.state == "supported"
    fail = to_crypto_source_state(
        UpbitBlockMeta(
            source="upbit_orderbook",
            state="unavailable",
            label="Upbit orderbook",
            errorReason="http_error",
        )
    )
    assert fail.state == "unavailable"


def test_snapshot_round_trips():
    now = datetime.now(UTC)
    meta = UpbitBlockMeta(
        source="upbit_ticker", state="fresh", label="t", fetchedAt=now
    )
    snap = UpbitPublicSnapshot(
        asOf=now,
        ticker=UpbitTickerBlock(meta=meta, tickers={"KRW-BTC": {"trade_price": 1}}),
        orderbook=UpbitOrderbookBlock(
            meta=UpbitBlockMeta(source="upbit_orderbook", state="fresh", label="o"),
            orderbooks={},
            spreadsPct={"KRW-BTC": 0.0},
        ),
        marketWarnings=UpbitMarketWarningsBlock(
            meta=UpbitBlockMeta(
                source="upbit_market_warnings", state="fresh", label="w"
            )
        ),
        sources=[meta],
    )
    assert "KRW-BTC" in snap.model_dump_json()

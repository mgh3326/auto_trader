import uuid
from types import SimpleNamespace

import pytest

from app.schemas.investment_stages import StageVerdict
from app.services.investment_stages.stages.base import (
    StageContext,
    UnavailableStageError,
)
from app.services.investment_stages.stages.market import MarketStage


def _snapshot(payload):
    return SimpleNamespace(
        snapshot_uuid=uuid.uuid4(),
        snapshot_kind="market",
        payload_json=payload,
    )


@pytest.mark.asyncio
async def test_market_stage_emits_bull_when_index_up():
    ctx = StageContext(
        bundle_uuid=uuid.uuid4(),
        snapshots_by_kind={
            "market": [_snapshot({"indices": {"KOSPI": {"change_percent": 2.0}}})]
        },
        bundle_metadata={},
    )
    payload = await MarketStage().run(ctx)
    assert payload.verdict == StageVerdict.BULL
    assert payload.confidence >= 50
    assert len(payload.cited_snapshots) == 1


@pytest.mark.asyncio
async def test_market_stage_emits_bear_when_index_down():
    ctx = StageContext(
        bundle_uuid=uuid.uuid4(),
        snapshots_by_kind={
            "market": [_snapshot({"indices": {"KOSPI": {"change_percent": -2.0}}})]
        },
        bundle_metadata={},
    )
    payload = await MarketStage().run(ctx)
    assert payload.verdict == StageVerdict.BEAR


@pytest.mark.asyncio
async def test_market_stage_raises_unavailable_when_no_snapshot():
    ctx = StageContext(
        bundle_uuid=uuid.uuid4(), snapshots_by_kind={}, bundle_metadata={}
    )
    with pytest.raises(UnavailableStageError):
        await MarketStage().run(ctx)


# --- ROB-366 B5: US index selection + fail-closed ---------------------------
@pytest.mark.asyncio
async def test_market_stage_us_selects_spx_bull():
    ctx = StageContext(
        bundle_uuid=uuid.uuid4(),
        snapshots_by_kind={
            "market": [
                _snapshot(
                    {
                        "indices": {
                            "SPX": {"change_percent": 1.2, "name": "S&P 500"},
                            "NASDAQ": {"change_percent": 0.9},
                            "DJI": {"change_percent": 0.3},
                        }
                    }
                )
            ]
        },
        bundle_metadata={},
        market="us",
    )
    payload = await MarketStage().run(ctx)
    assert payload.verdict == StageVerdict.BULL
    assert payload.confidence >= 30
    assert payload.cited_snapshots[0].payload_path == "$.indices.SPX.change_percent"
    # Must NOT cite KOSPI for a US report.
    assert "KOSPI" not in (payload.summary or "")


@pytest.mark.asyncio
async def test_market_stage_us_selects_spx_bear():
    ctx = StageContext(
        bundle_uuid=uuid.uuid4(),
        snapshots_by_kind={
            "market": [_snapshot({"indices": {"SPX": {"change_percent": -1.5}}})]
        },
        bundle_metadata={},
        market="us",
    )
    payload = await MarketStage().run(ctx)
    assert payload.verdict == StageVerdict.BEAR
    assert payload.cited_snapshots[0].payload_path == "$.indices.SPX.change_percent"
    assert "KOSPI" not in (payload.summary or "")


@pytest.mark.asyncio
async def test_market_stage_us_falls_back_to_nasdaq_when_spx_missing():
    # Primary-selection order is SPX → NASDAQ → DJI.
    ctx = StageContext(
        bundle_uuid=uuid.uuid4(),
        snapshots_by_kind={
            "market": [_snapshot({"indices": {"NASDAQ": {"change_percent": 1.0}}})]
        },
        bundle_metadata={},
        market="us",
    )
    payload = await MarketStage().run(ctx)
    assert payload.verdict == StageVerdict.BULL
    assert payload.cited_snapshots[0].payload_path == "$.indices.NASDAQ.change_percent"


@pytest.mark.asyncio
async def test_market_stage_kr_kospi_byte_identical():
    # KR with a KOSPI entry stays byte-identical to the pre-B5 output (lock).
    ctx = StageContext(
        bundle_uuid=uuid.uuid4(),
        snapshots_by_kind={
            "market": [_snapshot({"indices": {"KOSPI": {"change_percent": 2.0}}})]
        },
        bundle_metadata={},
        market="kr",
    )
    payload = await MarketStage().run(ctx)
    assert payload.verdict == StageVerdict.BULL
    assert payload.summary == "KOSPI change_percent=+2.00%"
    assert payload.cited_snapshots[0].payload_path == "$.indices.KOSPI.change_percent"


@pytest.mark.asyncio
async def test_market_stage_us_unavailable_when_no_index_entry():
    # Real production market snapshot carries events, no indices → fail-closed,
    # never a fabricated NEUTRAL/0.0.
    ctx = StageContext(
        bundle_uuid=uuid.uuid4(),
        snapshots_by_kind={
            "market": [_snapshot({"market": "us", "event_count": 0, "events": []})]
        },
        bundle_metadata={},
        market="us",
    )
    with pytest.raises(UnavailableStageError):
        await MarketStage().run(ctx)


@pytest.mark.asyncio
async def test_market_stage_unavailable_when_change_percent_none():
    # yfinance previous_close missing → change_percent None must not coerce to 0.0.
    ctx = StageContext(
        bundle_uuid=uuid.uuid4(),
        snapshots_by_kind={
            "market": [_snapshot({"indices": {"SPX": {"change_percent": None}}})]
        },
        bundle_metadata={},
        market="us",
    )
    with pytest.raises(UnavailableStageError):
        await MarketStage().run(ctx)


# --- ROB-377 PR1: crypto market dimension uses CRYPTO total-mcap index -------
@pytest.mark.asyncio
async def test_market_stage_crypto_selects_crypto_bull():
    ctx = StageContext(
        bundle_uuid=uuid.uuid4(),
        snapshots_by_kind={
            "market": [_snapshot({"indices": {"CRYPTO": {"change_percent": 2.0}}})]
        },
        bundle_metadata={},
        market="crypto",
    )
    payload = await MarketStage().run(ctx)
    assert payload.verdict == StageVerdict.BULL
    assert payload.cited_snapshots[0].payload_path == "$.indices.CRYPTO.change_percent"


@pytest.mark.asyncio
async def test_market_stage_crypto_selects_crypto_bear():
    ctx = StageContext(
        bundle_uuid=uuid.uuid4(),
        snapshots_by_kind={
            "market": [_snapshot({"indices": {"CRYPTO": {"change_percent": -2.0}}})]
        },
        bundle_metadata={},
        market="crypto",
    )
    payload = await MarketStage().run(ctx)
    assert payload.verdict == StageVerdict.BEAR
    assert payload.cited_snapshots[0].payload_path == "$.indices.CRYPTO.change_percent"


@pytest.mark.asyncio
async def test_market_stage_crypto_unavailable_when_no_index():
    # No CRYPTO index entry → still fail-closed (e.g. CoinGecko /global down).
    ctx = StageContext(
        bundle_uuid=uuid.uuid4(),
        snapshots_by_kind={"market": [_snapshot({"indices": {}})]},
        bundle_metadata={},
        market="crypto",
    )
    with pytest.raises(UnavailableStageError):
        await MarketStage().run(ctx)

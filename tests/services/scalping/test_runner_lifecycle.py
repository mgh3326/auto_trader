"""ROB-286 — Scalper runner lifecycle integration test.

Matrix row T28.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from decimal import Decimal

import pytest
import pytest_asyncio

from app.models.crypto_instruments import CryptoInstrument
from app.services.brokers.binance.testnet.execution_client import (
    BinanceTestnetExecutionClient,
)
from app.services.brokers.binance.testnet.ledger.service import (
    BinanceTestnetLedgerService,
)
from app.services.scalping.config import ScalperConfig
from app.services.scalping.decision import MarketSnapshot
from app.services.scalping.runner import ScalperRunner


@pytest_asyncio.fixture
async def instrument(db_session) -> CryptoInstrument:
    from sqlalchemy import select

    existing = await db_session.scalar(
        select(CryptoInstrument).where(
            CryptoInstrument.venue == "binance",
            CryptoInstrument.product == "spot",
            CryptoInstrument.venue_symbol == "BTCUSDT",
        )
    )
    if existing is not None:
        return existing
    inst = CryptoInstrument(
        venue="binance",
        product="spot",
        venue_symbol="BTCUSDT",
        base_asset="BTC",
        quote_asset="USDT",
        status="active",
    )
    db_session.add(inst)
    await db_session.flush()
    await db_session.refresh(inst)
    return inst


@pytest.fixture
def execution_client(monkeypatch) -> BinanceTestnetExecutionClient:
    monkeypatch.setenv("BINANCE_TESTNET_ENABLED", "true")
    monkeypatch.setenv("BINANCE_TESTNET_API_KEY", "DUMMY_KEY")
    monkeypatch.setenv("BINANCE_TESTNET_API_SECRET", "DUMMY_SECRET")
    return BinanceTestnetExecutionClient.from_env()


def _instrument_id_factory(
    instrument_id: int,
) -> Callable[[str], Awaitable[int]]:
    async def _get(symbol: str) -> int:
        return instrument_id

    return _get


def _snapshot_factory(
    snapshot: MarketSnapshot,
) -> Callable[[str], Awaitable[MarketSnapshot]]:
    async def _get(symbol: str) -> MarketSnapshot:
        return snapshot

    return _get


@pytest.mark.asyncio
async def test_lifecycle_happy_path_dry_run(
    db_session,
    instrument: CryptoInstrument,
    execution_client: BinanceTestnetExecutionClient,
) -> None:
    """T28 — End-to-end dry-run lifecycle.

    In dry-run mode the runner exercises planned → previewed → validated
    but never reaches ``submitted`` (no HTTP). The ledger trail proves
    the orchestration is plumbed end-to-end without any broker hit.
    """
    ledger = BinanceTestnetLedgerService(session=db_session)
    config = ScalperConfig.default_for_testnet()
    snap = MarketSnapshot(
        symbol="BTCUSDT",
        last_price=Decimal("50000"),
        rsi_5m=20.0,  # oversold
        ema_20_5m=Decimal("49600"),
        ema_50_5m=Decimal("49000"),
        instrument_health="healthy",
    )
    runner = ScalperRunner(
        execution_client=execution_client,
        ledger_service=ledger,
        config=config,
        instrument_id_for_symbol=_instrument_id_factory(instrument.id),
        market_snapshot_for_symbol=_snapshot_factory(snap),
        dry_run=True,
    )
    tick = await runner.tick_once(symbol="BTCUSDT")
    assert tick.action_name == "entry"
    assert tick.submitted is False  # dry-run — never submitted
    assert tick.dry_run is True
    # Ledger trail: a planned row in 'validated' state (we record_plan +
    # record_preview + record_validation pre-submit).
    rows = await ledger.list_by_instrument(instrument_id=instrument.id, limit=10)
    assert len(rows) == 1
    assert rows[0].lifecycle_state == "validated"
    assert rows[0].tp_price is not None
    assert rows[0].sl_price is not None
    assert rows[0].notional_usdt == Decimal("10")
    await execution_client.aclose()


@pytest.mark.asyncio
async def test_tick_hold_when_no_signal(
    db_session,
    instrument: CryptoInstrument,
    execution_client: BinanceTestnetExecutionClient,
) -> None:
    ledger = BinanceTestnetLedgerService(session=db_session)
    config = ScalperConfig.default_for_testnet()
    snap = MarketSnapshot(
        symbol="BTCUSDT",
        last_price=Decimal("50000"),
        rsi_5m=50.0,  # neutral
        ema_20_5m=Decimal("50000"),
        ema_50_5m=Decimal("50000"),
        instrument_health="healthy",
    )
    runner = ScalperRunner(
        execution_client=execution_client,
        ledger_service=ledger,
        config=config,
        instrument_id_for_symbol=_instrument_id_factory(instrument.id),
        market_snapshot_for_symbol=_snapshot_factory(snap),
        dry_run=True,
    )
    tick = await runner.tick_once(symbol="BTCUSDT")
    assert tick.action_name == "hold"
    assert tick.submitted is False
    rows = await ledger.list_by_instrument(instrument_id=instrument.id, limit=10)
    assert rows == []
    await execution_client.aclose()


@pytest.mark.asyncio
async def test_tick_rejects_symbol_outside_mvp_set(
    db_session,
    instrument: CryptoInstrument,
    execution_client: BinanceTestnetExecutionClient,
) -> None:
    ledger = BinanceTestnetLedgerService(session=db_session)
    config = ScalperConfig.default_for_testnet()
    snap = MarketSnapshot(
        symbol="DOGEUSDT",
        last_price=Decimal("0.1"),
        rsi_5m=50.0,
        ema_20_5m=Decimal("0.1"),
        ema_50_5m=Decimal("0.1"),
        instrument_health="healthy",
    )
    runner = ScalperRunner(
        execution_client=execution_client,
        ledger_service=ledger,
        config=config,
        instrument_id_for_symbol=_instrument_id_factory(instrument.id),
        market_snapshot_for_symbol=_snapshot_factory(snap),
        dry_run=True,
    )
    with pytest.raises(ValueError, match="not in the MVP locked set"):
        await runner.tick_once(symbol="DOGEUSDT")
    await execution_client.aclose()

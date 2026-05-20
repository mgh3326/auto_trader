"""ROB-286 — Scalper runner reconciliation tests.

Matrix rows T29, T30.
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


def _instrument_id_factory(instrument_id: int) -> Callable[[str], Awaitable[int]]:
    async def _get(symbol: str) -> int:
        if symbol == "BTCUSDT":
            return instrument_id
        # Reconcile pass walks the whole MVP set; raise to mimic absent symbols.
        raise LookupError(f"no instrument for {symbol}")

    return _get


def _snapshot_factory(
    snapshot: MarketSnapshot,
) -> Callable[[str], Awaitable[MarketSnapshot]]:
    async def _get(symbol: str) -> MarketSnapshot:
        return snapshot

    return _get


@pytest.mark.asyncio
async def test_clean_state_proceeds(
    db_session,
    instrument: CryptoInstrument,
    execution_client: BinanceTestnetExecutionClient,
    monkeypatch,
) -> None:
    """T29 — Clean ledger (no busy rows) → reconciliation succeeds with zero
    anomalies."""
    ledger = BinanceTestnetLedgerService(session=db_session)
    config = ScalperConfig.default_for_testnet()
    snap = MarketSnapshot(
        symbol="BTCUSDT",
        last_price=Decimal("50000"),
        rsi_5m=50.0,
        ema_20_5m=Decimal("50000"),
        ema_50_5m=Decimal("50000"),
    )
    runner = ScalperRunner(
        execution_client=execution_client,
        ledger_service=ledger,
        config=config,
        instrument_id_for_symbol=_instrument_id_factory(instrument.id),
        market_snapshot_for_symbol=_snapshot_factory(snap),
        dry_run=True,
    )

    # Mock execution_client.open_orders to return empty (no broker drift).
    async def _empty(*, symbol: str) -> list[dict[str, object]]:
        return []

    monkeypatch.setattr(execution_client, "open_orders", _empty)
    result = await runner.reconcile_on_start()
    assert result.anomalies_detected == 0
    assert result.rows_examined == 0
    await execution_client.aclose()


@pytest.mark.asyncio
async def test_drift_raises_anomaly(
    db_session,
    instrument: CryptoInstrument,
    execution_client: BinanceTestnetExecutionClient,
    monkeypatch,
) -> None:
    """T30 — Ledger has a 'submitted' row but broker reports it absent →
    anomaly recorded."""
    ledger = BinanceTestnetLedgerService(session=db_session)
    config = ScalperConfig.default_for_testnet()
    # Seed a row in 'submitted' state to simulate prior submit.
    cid = "drifted-row-1"
    await ledger.record_plan(
        instrument_id=instrument.id,
        client_order_id=cid,
        side="BUY",
        order_type="MARKET",
        qty=Decimal("0.001"),
    )
    await ledger.record_preview(client_order_id=cid)
    await ledger.record_validation(client_order_id=cid)
    await ledger.record_submit(client_order_id=cid, broker_order_id="broker-x")

    runner = ScalperRunner(
        execution_client=execution_client,
        ledger_service=ledger,
        config=config,
        instrument_id_for_symbol=_instrument_id_factory(instrument.id),
        market_snapshot_for_symbol=_snapshot_factory(
            MarketSnapshot(
                symbol="BTCUSDT",
                last_price=Decimal("50000"),
                rsi_5m=50.0,
                ema_20_5m=Decimal("50000"),
                ema_50_5m=Decimal("50000"),
            )
        ),
        dry_run=True,
    )

    async def _empty(*, symbol: str) -> list[dict[str, object]]:
        # Broker has no open orders → ledger row is "drifted".
        return []

    monkeypatch.setattr(execution_client, "open_orders", _empty)
    result = await runner.reconcile_on_start()
    assert result.anomalies_detected == 1
    assert cid in result.anomaly_client_order_ids
    # Reload row and verify it transitioned to anomaly.
    row = await ledger.get_by_client_order_id(cid)
    assert row is not None
    assert row.lifecycle_state == "anomaly"
    assert row.anomaly_reason == "reconcile_drift"
    await execution_client.aclose()

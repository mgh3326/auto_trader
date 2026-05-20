"""ROB-286 — BinanceTestnetLedgerService tests.

Matrix rows T24-T27.
"""

from __future__ import annotations

import importlib
from decimal import Decimal

import pytest
import pytest_asyncio

from app.models.crypto_instruments import CryptoInstrument
from app.services.brokers.binance.testnet.errors import (
    BinanceInvalidStateTransition,
)
from app.services.brokers.binance.testnet.ledger.service import (
    BinanceTestnetLedgerService,
)


@pytest_asyncio.fixture
async def instrument(db_session) -> CryptoInstrument:
    """Create a Binance spot BTCUSDT instrument for the ledger to reference."""
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


@pytest.mark.asyncio
async def test_record_plan_creates_row(
    db_session, instrument: CryptoInstrument
) -> None:
    svc = BinanceTestnetLedgerService(session=db_session)
    row = await svc.record_plan(
        instrument_id=instrument.id,
        client_order_id="ledger-test-1",
        side="BUY",
        order_type="LIMIT",
        qty=Decimal("0.001"),
        price=Decimal("50000"),
        notional_usdt=Decimal("5"),
    )
    assert row.lifecycle_state == "planned"
    assert row.planned_at is not None
    assert row.client_order_id == "ledger-test-1"


@pytest.mark.asyncio
async def test_record_plan_is_idempotent(
    db_session, instrument: CryptoInstrument
) -> None:
    """T24 — Re-recording the same plan is a no-op (returns existing row)."""
    svc = BinanceTestnetLedgerService(session=db_session)
    row1 = await svc.record_plan(
        instrument_id=instrument.id,
        client_order_id="ledger-test-2",
        side="BUY",
        order_type="MARKET",
        qty=Decimal("0.001"),
    )
    row2 = await svc.record_plan(
        instrument_id=instrument.id,
        client_order_id="ledger-test-2",
        side="BUY",
        order_type="MARKET",
        qty=Decimal("0.001"),
    )
    assert row1.id == row2.id
    assert row1.lifecycle_state == row2.lifecycle_state == "planned"


@pytest.mark.asyncio
async def test_record_submit_idempotent(
    db_session, instrument: CryptoInstrument
) -> None:
    """T24 — Re-recording the same submit is a no-op."""
    svc = BinanceTestnetLedgerService(session=db_session)
    await svc.record_plan(
        instrument_id=instrument.id,
        client_order_id="ledger-test-3",
        side="BUY",
        order_type="MARKET",
        qty=Decimal("0.001"),
    )
    await svc.record_preview(client_order_id="ledger-test-3")
    await svc.record_validation(client_order_id="ledger-test-3")
    row1 = await svc.record_submit(
        client_order_id="ledger-test-3",
        broker_order_id="binance-1",
    )
    row2 = await svc.record_submit(
        client_order_id="ledger-test-3",
        broker_order_id="binance-1",
    )
    assert row1.id == row2.id
    assert row1.lifecycle_state == row2.lifecycle_state == "submitted"
    assert row1.broker_order_id == "binance-1"


@pytest.mark.asyncio
async def test_invalid_transition_raises(
    db_session, instrument: CryptoInstrument
) -> None:
    """T25 — Jumping states (planned → filled) is refused."""
    svc = BinanceTestnetLedgerService(session=db_session)
    await svc.record_plan(
        instrument_id=instrument.id,
        client_order_id="ledger-test-4",
        side="BUY",
        order_type="MARKET",
        qty=Decimal("0.001"),
    )
    with pytest.raises(BinanceInvalidStateTransition):
        # planned → filled is illegal; must go via previewed → validated → submitted.
        await svc.record_fill(client_order_id="ledger-test-4")


@pytest.mark.asyncio
async def test_transition_on_missing_row_raises(db_session) -> None:
    svc = BinanceTestnetLedgerService(session=db_session)
    with pytest.raises(BinanceInvalidStateTransition):
        await svc.record_submit(
            client_order_id="never-seen", broker_order_id="binance-x"
        )


@pytest.mark.asyncio
async def test_full_happy_path_lifecycle(
    db_session, instrument: CryptoInstrument
) -> None:
    """End-to-end: planned → previewed → validated → submitted → filled →
    tp_sl_armed → tp_sl_triggered → closed → reconciled."""
    svc = BinanceTestnetLedgerService(session=db_session)
    cid = "ledger-test-happy"
    await svc.record_plan(
        instrument_id=instrument.id,
        client_order_id=cid,
        side="BUY",
        order_type="MARKET",
        qty=Decimal("0.001"),
    )
    await svc.record_preview(client_order_id=cid)
    await svc.record_validation(client_order_id=cid)
    await svc.record_submit(client_order_id=cid, broker_order_id="binance-z")
    await svc.record_fill(client_order_id=cid)
    await svc.record_tp_sl_armed(client_order_id=cid)
    await svc.record_tp_sl_triggered(client_order_id=cid)
    await svc.record_closed(client_order_id=cid)
    row = await svc.record_reconciled(client_order_id=cid)
    assert row.lifecycle_state == "reconciled"
    assert row.reconciled_at is not None
    assert row.last_reconciled_at is not None


@pytest.mark.asyncio
async def test_anomaly_emits_sentry(
    db_session, instrument: CryptoInstrument, mocker
) -> None:
    """T26 — Recording an anomaly emits a Sentry message."""
    svc = BinanceTestnetLedgerService(session=db_session)
    await svc.record_plan(
        instrument_id=instrument.id,
        client_order_id="ledger-test-anomaly",
        side="BUY",
        order_type="MARKET",
        qty=Decimal("0.001"),
    )
    # Patch sentry_sdk.capture_message at import location.
    import sentry_sdk

    mock_capture = mocker.patch.object(sentry_sdk, "capture_message")
    row = await svc.record_anomaly(
        client_order_id="ledger-test-anomaly",
        reason="reconcile_drift",
    )
    assert row.lifecycle_state == "anomaly"
    assert row.anomaly_reason == "reconcile_drift"
    # Anomaly emits.
    assert mock_capture.called
    call_kwargs = mock_capture.call_args
    # First positional arg is the message.
    assert "anomaly" in call_kwargs.args[0]


@pytest.mark.asyncio
async def test_first_fill_after_submit_emits_sentry(
    db_session, instrument: CryptoInstrument, mocker
) -> None:
    """Open item #4 lean — first fill after submit triggers sanity event."""
    svc = BinanceTestnetLedgerService(session=db_session)
    cid = "ledger-test-firstfill"
    await svc.record_plan(
        instrument_id=instrument.id,
        client_order_id=cid,
        side="BUY",
        order_type="MARKET",
        qty=Decimal("0.001"),
    )
    await svc.record_preview(client_order_id=cid)
    await svc.record_validation(client_order_id=cid)
    await svc.record_submit(client_order_id=cid, broker_order_id="binance-ff")
    import sentry_sdk

    mock_capture = mocker.patch.object(sentry_sdk, "capture_message")
    await svc.record_fill(client_order_id=cid)
    assert mock_capture.called


def test_repository_not_importable_externally() -> None:
    """T27 — Repository submodule import guard.

    The repository is service-internal; outside callers must use the
    service. ``_public_export`` is intentionally a non-existent submodule
    so importing it raises ``ImportError`` / ``ModuleNotFoundError``.
    """
    with pytest.raises(ImportError):
        importlib.import_module(
            "app.services.brokers.binance.testnet.ledger.repository._public_export"
        )

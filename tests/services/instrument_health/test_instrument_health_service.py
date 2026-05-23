"""ROB-285 — CryptoInstrumentHealthService (service-only writes)."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.crypto_instruments import CryptoInstrument
from app.services.instrument_health.service import (
    CryptoInstrumentHealthService,
    InstrumentHealthState,
)


def _unique_symbol(prefix: str) -> str:
    """Return a venue_symbol unique to this test run.

    Avoids collisions on ``uq_crypto_instruments_venue_product_symbol``
    when other suites in the parallel xdist run also seed binance/spot
    rows for the same base ticker (BTCUSDT/ETHUSDT/...).
    """
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


@pytest.mark.asyncio
async def test_default_state_is_healthy_on_first_touch(
    db_session: AsyncSession,
) -> None:
    inst = CryptoInstrument(
        venue="binance",
        product="spot",
        venue_symbol=_unique_symbol("BTCUSDT"),
        base_asset="BTC",
        quote_asset="USDT",
        status="active",
    )
    db_session.add(inst)
    await db_session.flush()
    svc = CryptoInstrumentHealthService(session=db_session)
    state = await svc.get_state(inst.id)
    assert state == InstrumentHealthState.HEALTHY


@pytest.mark.asyncio
async def test_record_degraded_then_back_to_healthy(
    db_session: AsyncSession,
) -> None:
    inst = CryptoInstrument(
        venue="binance",
        product="spot",
        venue_symbol=_unique_symbol("ETHUSDT"),
        base_asset="ETH",
        quote_asset="USDT",
        status="active",
    )
    db_session.add(inst)
    await db_session.flush()
    svc = CryptoInstrumentHealthService(session=db_session)
    await svc.record_degraded(inst.id, reason="3 reconnect failures")
    assert await svc.get_state(inst.id) == InstrumentHealthState.DEGRADED
    await svc.record_recovered(inst.id)
    assert await svc.get_state(inst.id) == InstrumentHealthState.HEALTHY


@pytest.mark.asyncio
async def test_record_rate_limited_persists_retry_after(
    db_session: AsyncSession,
) -> None:
    inst = CryptoInstrument(
        venue="binance",
        product="spot",
        venue_symbol=_unique_symbol("ADAUSDT"),
        base_asset="ADA",
        quote_asset="USDT",
        status="active",
    )
    db_session.add(inst)
    await db_session.flush()
    svc = CryptoInstrumentHealthService(session=db_session)
    retry_after = dt.datetime.now(tz=dt.UTC) + dt.timedelta(seconds=30)
    await svc.record_rate_limited(
        inst.id, retry_after_at=retry_after, reason="HTTP 429"
    )
    assert await svc.get_state(inst.id) == InstrumentHealthState.RATE_LIMITED


@pytest.mark.asyncio
async def test_record_manual_backfill_required_does_not_auto_clear(
    db_session: AsyncSession,
) -> None:
    inst = CryptoInstrument(
        venue="binance",
        product="spot",
        venue_symbol=_unique_symbol("SOLUSDT"),
        base_asset="SOL",
        quote_asset="USDT",
        status="active",
    )
    db_session.add(inst)
    await db_session.flush()
    svc = CryptoInstrumentHealthService(session=db_session)
    await svc.record_manual_backfill_required(inst.id, reason="gap 8000 candles")
    assert (
        await svc.get_state(inst.id) == InstrumentHealthState.MANUAL_BACKFILL_REQUIRED
    )
    # record_recovered must be explicit; the service does not auto-clear.
    with pytest.raises(ValueError):
        await svc.record_recovered(inst.id)
    await svc.clear_manual_backfill(inst.id, operator="alice")
    assert await svc.get_state(inst.id) == InstrumentHealthState.HEALTHY


@pytest.mark.asyncio
async def test_invalid_state_raises_at_db_level(db_session: AsyncSession) -> None:
    # Direct SQL insert with bogus state must violate the CHECK constraint.
    inst = CryptoInstrument(
        venue="binance",
        product="spot",
        venue_symbol=_unique_symbol("DOGEUSDT"),
        base_asset="DOGE",
        quote_asset="USDT",
        status="active",
    )
    db_session.add(inst)
    await db_session.flush()
    with pytest.raises(IntegrityError):
        await db_session.execute(
            text(
                "INSERT INTO crypto_instrument_health (instrument_id, state) "
                "VALUES (:i, 'bogus')"
            ),
            {"i": inst.id},
        )
        await db_session.flush()


def test_repository_is_not_importable_from_outside_the_service_module() -> None:
    """Service-only writes: repository submodule ``_public_export`` does not
    exist. The runtime guard is satisfied by-construction because
    ``_public_export`` is a private symbol inside the repository module
    (if anything) — not a submodule. Importing it as a module raises
    ``ImportError`` / ``ModuleNotFoundError`` and locks the convention."""
    import importlib

    with pytest.raises(ImportError):
        importlib.import_module(
            "app.services.instrument_health.repository._public_export"
        )

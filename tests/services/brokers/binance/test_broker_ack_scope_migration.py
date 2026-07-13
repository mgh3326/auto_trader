"""Dual-path ROB-844 broker-ack index migration and bootstrap refresh."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import text

from app.core.db import engine
from tests._schema_bootstrap import (
    ROB844_ACK_INDEX_CREATE_DDL,
    ROB844_ACK_INDEX_REFRESH_DDL,
)

pytestmark = pytest.mark.usefixtures("binance_demo_reservation_lock")

_MIG_PATH = (
    Path(__file__).parents[4]
    / "alembic"
    / "versions"
    / "20260713_rob844_broker_ack_scope.py"
)
_spec = importlib.util.spec_from_file_location("rob844_ack_scope_migration", _MIG_PATH)
_mig = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mig)


def _instrument(conn, symbol: str) -> int:
    return conn.execute(
        text(
            "INSERT INTO crypto_instruments "
            "(venue, product, venue_symbol, base_asset, quote_asset, status) "
            "VALUES ('binance','spot',:symbol,:base,'USDT','active') RETURNING id"
        ),
        {"symbol": symbol, "base": symbol.removesuffix("USDT")},
    ).scalar_one()


def _ledger_row(conn, *, instrument_id: int, cid: str, ack: str) -> None:
    conn.execute(
        text(
            "INSERT INTO binance_demo_order_ledger "
            "(instrument_id, product, venue_host, client_order_id, "
            "broker_order_id, side, order_type, qty, lifecycle_state) "
            "VALUES (:iid,'spot','demo-api.binance.com',:cid,:ack,"
            "'BUY','MARKET',1,'reconciled')"
        ),
        {"iid": instrument_id, "cid": cid, "ack": ack},
    )


@pytest.mark.asyncio
async def test_target_scope_allows_cross_instrument_reuse_but_old_scope_rejects() -> (
    None
):
    async with engine.connect() as conn:
        transaction = await conn.begin()
        try:

            def _exercise(sync_conn):
                sync_conn.execute(text("DROP INDEX uq_binance_demo_ledger_broker_ack"))
                first = _instrument(sync_conn, "R844SCOPE1USDT")
                second = _instrument(sync_conn, "R844SCOPE2USDT")
                _ledger_row(
                    sync_conn,
                    instrument_id=first,
                    cid="rob844-scope-1",
                    ack="84442",
                )
                _ledger_row(
                    sync_conn,
                    instrument_id=second,
                    cid="rob844-scope-2",
                    ack="84442",
                )

                _mig._assert_no_conflicts(
                    sync_conn, columns=_mig._NEW_COLUMNS, direction="upgrade"
                )
                with pytest.raises(RuntimeError, match="target key"):
                    _mig._assert_no_conflicts(
                        sync_conn, columns=_mig._OLD_COLUMNS, direction="downgrade"
                    )

            await conn.run_sync(_exercise)
        finally:
            await transaction.rollback()


@pytest.mark.asyncio
async def test_target_scope_detects_same_instrument_conflict_without_mutation() -> None:
    async with engine.connect() as conn:
        transaction = await conn.begin()
        try:

            def _exercise(sync_conn):
                sync_conn.execute(text("DROP INDEX uq_binance_demo_ledger_broker_ack"))
                instrument = _instrument(sync_conn, "R844SCOPESAMEUSDT")
                _ledger_row(
                    sync_conn,
                    instrument_id=instrument,
                    cid="rob844-scope-same-1",
                    ack="84443",
                )
                _ledger_row(
                    sync_conn,
                    instrument_id=instrument,
                    cid="rob844-scope-same-2",
                    ack="84443",
                )
                with pytest.raises(RuntimeError, match="target key"):
                    _mig._assert_no_conflicts(
                        sync_conn, columns=_mig._NEW_COLUMNS, direction="upgrade"
                    )
                count = sync_conn.execute(
                    text(
                        "SELECT count(*) FROM binance_demo_order_ledger "
                        "WHERE broker_order_id='84443'"
                    )
                ).scalar_one()
                assert count == 2

            await conn.run_sync(_exercise)
        finally:
            await transaction.rollback()


@pytest.mark.asyncio
async def test_bootstrap_replaces_persistent_old_shape_idempotently() -> None:
    async with engine.connect() as conn:
        transaction = await conn.begin()
        try:
            await conn.execute(text("DROP INDEX uq_binance_demo_ledger_broker_ack"))
            await conn.execute(
                text(
                    "CREATE UNIQUE INDEX uq_binance_demo_ledger_broker_ack "
                    "ON binance_demo_order_ledger "
                    "(product, venue_host, broker_order_id) "
                    "WHERE broker_order_id IS NOT NULL"
                )
            )

            for _ in range(2):
                await conn.execute(text(ROB844_ACK_INDEX_REFRESH_DDL))
                await conn.execute(text(ROB844_ACK_INDEX_CREATE_DDL))

            columns = await conn.run_sync(_mig._index_columns)
            assert columns == _mig._NEW_COLUMNS
        finally:
            await transaction.rollback()

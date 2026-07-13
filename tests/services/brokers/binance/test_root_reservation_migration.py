"""ROB-844 — migration fail-safe: additive, history-preserving, operator-guided.

The migration adds two partial-unique indexes and, before creating them, aborts
with an actionable message if pre-existing rows already violate either
uniqueness — never deleting or mutating ROB-298 Demo history (AC#7 / §10).

These tests exercise the migration module's conflict detectors against the real
table inside a rolled-back transaction (the index is transiently dropped so a
conflict can be seeded, then the rollback restores everything), and verify the
DDL is reversible (down → up) on an isolated temp table.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core.db import engine

pytestmark = pytest.mark.usefixtures("binance_demo_reservation_lock")

_MIG_PATH = (
    Path(__file__).parents[4]
    / "alembic"
    / "versions"
    / "20260713_rob844_binance_demo_root_reservation.py"
)
_spec = importlib.util.spec_from_file_location("rob844_migration", _MIG_PATH)
_mig = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mig)

_OPEN_ROOT_DDL = (
    "CREATE UNIQUE INDEX uq_binance_demo_ledger_open_root "
    "ON binance_demo_order_ledger (product, instrument_id) "
    "WHERE parent_client_order_id IS NULL "
    "AND lifecycle_state IN "
    "('planned','previewed','validated','submitted','filled','anomaly')"
)


def _insert_instrument(conn, symbol: str) -> int:
    return conn.execute(
        text(
            "INSERT INTO crypto_instruments "
            "(venue, product, venue_symbol, base_asset, quote_asset, status) "
            "VALUES ('binance','spot',:s,:b,'USDT','active') RETURNING id"
        ),
        {"s": symbol, "b": symbol.replace("USDT", "")},
    ).scalar_one()


def _insert_root(conn, *, iid: int, cid: str, state: str = "planned") -> None:
    conn.execute(
        text(
            "INSERT INTO binance_demo_order_ledger "
            "(instrument_id, product, venue_host, client_order_id, side, "
            "order_type, qty, lifecycle_state, planned_at) VALUES "
            "(:iid,'spot','demo-api.binance.com',:cid,'BUY','MARKET',1,:st, now())"
        ),
        {"iid": iid, "cid": cid, "st": state},
    )


@pytest.mark.asyncio
async def test_fail_safe_detects_open_root_conflict_and_preserves_history() -> None:
    async with engine.connect() as conn:
        trans = await conn.begin()
        try:

            def _seed_and_check(sync_conn):
                # Drop the index so a pre-migration duplicate can exist, then
                # seed two blocking roots for one instrument.
                sync_conn.execute(text("DROP INDEX uq_binance_demo_ledger_open_root"))
                iid = _insert_instrument(sync_conn, "R844MIGAUSDT")
                _insert_root(sync_conn, iid=iid, cid="rob844mig-a1")
                _insert_root(sync_conn, iid=iid, cid="rob844mig-a2")
                with pytest.raises(RuntimeError, match="Operator remediation required"):
                    _mig._assert_no_open_root_conflicts(sync_conn)
                # History preserved: the detector never deletes/mutates rows.
                remaining = sync_conn.execute(
                    text(
                        "SELECT count(*) FROM binance_demo_order_ledger "
                        "WHERE instrument_id = :iid"
                    ),
                    {"iid": iid},
                ).scalar_one()
                assert remaining == 2

            await conn.run_sync(_seed_and_check)
        finally:
            await trans.rollback()


@pytest.mark.asyncio
async def test_fail_safe_detects_broker_ack_conflict_and_preserves_history() -> None:
    async with engine.connect() as conn:
        trans = await conn.begin()
        try:

            def _seed_and_check(sync_conn):
                sync_conn.execute(text("DROP INDEX uq_binance_demo_ledger_broker_ack"))
                # Same instrument + broker acknowledgement is the replay this
                # corrected fresh-upgrade guard catches. Terminal state keeps
                # the independent open-root index out of this detector test.
                iid = _insert_instrument(sync_conn, "R844MIGBUSDT")
                for cid in ("rob844mig-b1", "rob844mig-b2"):
                    sync_conn.execute(
                        text(
                            "INSERT INTO binance_demo_order_ledger "
                            "(instrument_id, product, venue_host, client_order_id, "
                            "broker_order_id, side, order_type, qty, lifecycle_state) "
                            "VALUES (:iid,'spot','demo-api.binance.com',:cid,"
                            "'DUP-ACK','BUY','MARKET',1,'reconciled')"
                        ),
                        {"iid": iid, "cid": cid},
                    )
                with pytest.raises(RuntimeError, match="Operator remediation required"):
                    _mig._assert_no_broker_ack_conflicts(sync_conn)
                remaining = sync_conn.execute(
                    text(
                        "SELECT count(*) FROM binance_demo_order_ledger "
                        "WHERE broker_order_id = 'DUP-ACK'"
                    )
                ).scalar_one()
                assert remaining == 2

            await conn.run_sync(_seed_and_check)
        finally:
            await trans.rollback()


@pytest.mark.asyncio
async def test_fresh_upgrade_detector_allows_cross_instrument_numeric_reuse() -> None:
    async with engine.connect() as conn:
        trans = await conn.begin()
        try:

            def _seed_and_check(sync_conn):
                sync_conn.execute(text("DROP INDEX uq_binance_demo_ledger_broker_ack"))
                for symbol, cid in (
                    ("R844MIGB1USDT", "rob844mig-b-different-1"),
                    ("R844MIGB2USDT", "rob844mig-b-different-2"),
                ):
                    iid = _insert_instrument(sync_conn, symbol)
                    sync_conn.execute(
                        text(
                            "INSERT INTO binance_demo_order_ledger "
                            "(instrument_id, product, venue_host, client_order_id, "
                            "broker_order_id, side, order_type, qty, lifecycle_state) "
                            "VALUES (:iid,'spot','demo-api.binance.com',:cid,"
                            "'REUSED-NUMERIC-ID','BUY','MARKET',1,'reconciled')"
                        ),
                        {"iid": iid, "cid": cid},
                    )
                _mig._assert_no_broker_ack_conflicts(sync_conn)

            await conn.run_sync(_seed_and_check)
        finally:
            await trans.rollback()


@pytest.mark.asyncio
async def test_detectors_pass_when_no_conflict() -> None:
    """With the indexes in place the table cannot hold conflicts, so a clean
    upgrade path's pre-checks return without raising."""
    async with engine.connect() as conn:

        def _check(sync_conn):
            _mig._assert_no_open_root_conflicts(sync_conn)
            _mig._assert_no_broker_ack_conflicts(sync_conn)

        await conn.run_sync(_check)


@pytest.mark.asyncio
async def test_downgrade_then_upgrade_reapplies_open_root_uniqueness() -> None:
    """DDL round-trip (down → up) on an isolated temp table: the open-root
    partial-unique index enforces one blocking root per (product, instrument)."""
    async with engine.connect() as conn:
        trans = await conn.begin()
        try:

            def _round_trip(sync_conn):
                sync_conn.execute(
                    text(
                        "CREATE TEMP TABLE binance_demo_order_ledger "
                        "(id bigserial primary key, product text, instrument_id "
                        "bigint, venue_host text, client_order_id text, "
                        "broker_order_id text, parent_client_order_id text, "
                        "side text, order_type text, qty numeric, "
                        "lifecycle_state text, planned_at timestamptz) "
                        "ON COMMIT DROP"
                    )
                )
                sync_conn.execute(text(_OPEN_ROOT_DDL))

                def _seed(cid):
                    _insert_root(sync_conn, iid=1, cid=cid)

                _seed("t1")
                # A second blocking root for the same (product, instrument) is
                # rejected. Wrap in a savepoint so the violation does not abort
                # the whole transaction.
                sp = sync_conn.begin_nested()
                with pytest.raises(IntegrityError):
                    _seed("t2")
                sp.rollback()

                # downgrade: drop the index → duplicates now allowed again.
                sync_conn.execute(text("DROP INDEX uq_binance_demo_ledger_open_root"))
                _seed("t3")  # succeeds once the index is gone

            await conn.run_sync(_round_trip)
        finally:
            await trans.rollback()

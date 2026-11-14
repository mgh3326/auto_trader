"""hk 4703 (3797 T-P1) — DB proofs for the Kiwoom mock ledger schema.

CI only: these tests use the pytest-owned throwaway database. Every refusal
runs inside a session that is rolled back, and the migration round trip runs
inside one transaction that is rolled back at the end, so the shared schema is
left as bootstrapped.

Covered: every CHECK and UNIQUE in design §2.1 / §2.4 refuses its bad row, the
partial open-operation unique index admits a new correction once the prior one
is terminal, the order_proposals CHECK admits kiwoom_mock, and the migration's
upgrade/downgrade round trip plus the downgrade guard behave as stated.
"""

from __future__ import annotations

import importlib.util
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import engine
from tests._run_owned_database import validate_run_owned_database_url

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

REPO = Path(__file__).resolve().parents[1]
MIGRATION_PATH = REPO / "alembic" / "versions" / "20261115_hk4703_kiwoom_ledger.py"
NEW_TABLES = ("kiwoom_mock_order_ledger", "kiwoom_mock_signal_ledger")
PROPOSAL_CHECK = "ck_order_proposals_order_proposals_account_mode"


def _valid_order(**overrides) -> dict:
    row = {
        "trade_date": datetime(2026, 11, 16, 0, 30, tzinfo=UTC),
        "trading_day": "20261116",
        "symbol": "005930",
        "raw_symbol": "005930",
        "instrument_type": "equity_kr",
        "market": "kr",
        "side": "buy",
        "order_type": "limit",
        "operation_kind": "place",
        "quantity": Decimal("1"),
        "price": Decimal("70000"),
        "currency": "KRW",
        "client_order_id": f"kwoomm-{uuid.uuid4().hex[:16]}",
        "idempotency_key": f"kwoomm-{uuid.uuid4().hex[:16]}",
        "account_mode": "kiwoom_mock",
        "broker": "kiwoom",
        "status": "pending",
        "broker_order_id": None,
        "original_order_id": None,
    }
    row.update(overrides)
    return row


def _valid_signal(**overrides) -> dict:
    row = {
        "correlation_id": f"kiwoom-mock:{uuid.uuid4().hex}",
        "strategy": "test-strategy",
        "signal_source": "test-source",
        "account_mode": "kiwoom_mock",
        "symbol": "005930",
        "decision": "order",
        "outcome_state": "recorded",
        "kst_date": "2026-11-16",
    }
    row.update(overrides)
    return row


async def _insert(session: AsyncSession, table: str, row: dict) -> None:
    columns = ", ".join(row)
    binds = ", ".join(f":{key}" for key in row)
    await session.execute(
        text(f"INSERT INTO review.{table} ({columns}) VALUES ({binds})"), row
    )


async def _expect_refused(
    session: AsyncSession, table: str, row: dict, names: tuple[str, ...]
) -> None:
    with pytest.raises(IntegrityError) as excinfo:
        await _insert(session, table, row)
        await session.flush()
    await session.rollback()
    message = str(excinfo.value)
    assert any(name in message for name in names), message


@pytest.mark.parametrize(
    ("overrides", "names"),
    [
        ({"broker": "toss"}, ("ck_kiwoom_mock_order_ledger_broker",)),
        (
            {"account_mode": "kis_mock"},
            (
                "ck_kiwoom_mock_order_ledger_account_mode",
                "ck_kiwoom_mock_order_ledger_account_market_currency",
            ),
        ),
        (
            {"market": "crypto"},
            (
                "ck_kiwoom_mock_order_ledger_market",
                "ck_kiwoom_mock_order_ledger_account_market_currency",
            ),
        ),
        (
            {"instrument_type": "crypto"},
            (
                "ck_kiwoom_mock_order_ledger_instrument_type",
                "ck_kiwoom_mock_order_ledger_account_market_currency",
            ),
        ),
        (
            {"currency": "USD"},
            ("ck_kiwoom_mock_order_ledger_account_market_currency",),
        ),
        (
            {"operation_kind": "fill"},
            ("ck_kiwoom_mock_order_ledger_operation_kind",),
        ),
        ({"side": "hold"}, ("ck_kiwoom_mock_order_ledger_side",)),
        (
            {"order_type": "stop"},
            (
                "ck_kiwoom_mock_order_ledger_order_type",
                "ck_kiwoom_mock_order_ledger_kr_limit_only",
            ),
        ),
        (
            {"order_type": "market"},
            ("ck_kiwoom_mock_order_ledger_kr_limit_only",),
        ),
        ({"status": "done"}, ("ck_kiwoom_mock_order_ledger_status",)),
        (
            {"operation_kind": "cancel", "original_order_id": None},
            ("ck_kiwoom_mock_order_ledger_original_required_for_correction",),
        ),
        (
            {"status": "accepted", "broker_order_id": None},
            ("ck_kiwoom_mock_order_ledger_accepted_has_broker_order_id",),
        ),
    ],
)
async def test_order_ledger_check_refuses_bad_row(
    db_session: AsyncSession, overrides: dict, names: tuple[str, ...]
) -> None:
    await _expect_refused(
        db_session, "kiwoom_mock_order_ledger", _valid_order(**overrides), names
    )


async def test_order_ledger_admits_valid_kr_and_us_rows(
    db_session: AsyncSession,
) -> None:
    await _insert(db_session, "kiwoom_mock_order_ledger", _valid_order())
    us = _valid_order(
        market="us",
        instrument_type="equity_us",
        currency="USD",
        account_mode="kiwoom_mock_us",
        order_type="market",
        price=None,
        trading_day="20261116",
    )
    await _insert(db_session, "kiwoom_mock_order_ledger", us)
    await db_session.rollback()


async def test_order_ledger_client_order_id_is_unique(db_session: AsyncSession) -> None:
    first = _valid_order()
    await _insert(db_session, "kiwoom_mock_order_ledger", first)
    await _expect_refused(
        db_session,
        "kiwoom_mock_order_ledger",
        _valid_order(client_order_id=first["client_order_id"]),
        ("uq_kiwoom_mock_order_ledger_client_order_id",),
    )


async def test_order_ledger_account_day_broker_order_is_unique(
    db_session: AsyncSession,
) -> None:
    await _insert(
        db_session,
        "kiwoom_mock_order_ledger",
        _valid_order(status="accepted", broker_order_id="0000123"),
    )
    await _expect_refused(
        db_session,
        "kiwoom_mock_order_ledger",
        _valid_order(status="accepted", broker_order_id="0000123"),
        ("uq_kiwoom_mock_order_ledger_account_day_broker_order",),
    )


async def test_open_operation_unique_blocks_a_second_pending_cancel(
    db_session: AsyncSession,
) -> None:
    original = "0000777"
    await _insert(
        db_session,
        "kiwoom_mock_order_ledger",
        _valid_order(
            operation_kind="cancel",
            original_order_id=original,
            status="pending",
        ),
    )
    await _expect_refused(
        db_session,
        "kiwoom_mock_order_ledger",
        _valid_order(
            operation_kind="cancel",
            original_order_id=original,
            status="uncertain",
        ),
        ("uq_kiwoom_mock_order_ledger_open_operation",),
    )


async def test_open_operation_unique_blocks_a_cancel_while_a_modify_is_open(
    db_session: AsyncSession,
) -> None:
    # Keyed on original_order_id alone: a modify and a cancel on the same
    # original order are competing corrections and must not coexist as open rows.
    original = "0000999"
    await _insert(
        db_session,
        "kiwoom_mock_order_ledger",
        _valid_order(
            operation_kind="modify",
            original_order_id=original,
            status="accepted",
            broker_order_id="0001001",
        ),
    )
    await _expect_refused(
        db_session,
        "kiwoom_mock_order_ledger",
        _valid_order(
            operation_kind="cancel",
            original_order_id=original,
            status="pending",
        ),
        ("uq_kiwoom_mock_order_ledger_open_operation",),
    )


async def test_open_operation_unique_is_scoped_by_account_and_trading_day(
    db_session: AsyncSession,
) -> None:
    # Order numbers are reusable across trading days and accounts, so the same
    # original order id on another day or account is not a competing correction.
    original = "0000555"
    await _insert(
        db_session,
        "kiwoom_mock_order_ledger",
        _valid_order(
            operation_kind="cancel",
            original_order_id=original,
            status="pending",
            trading_day="20261116",
        ),
    )
    await _insert(
        db_session,
        "kiwoom_mock_order_ledger",
        _valid_order(
            operation_kind="cancel",
            original_order_id=original,
            status="pending",
            trading_day="20261117",
        ),
    )
    await db_session.rollback()


async def test_open_operation_unique_admits_a_correction_after_terminal(
    db_session: AsyncSession,
) -> None:
    original = "0000888"
    await _insert(
        db_session,
        "kiwoom_mock_order_ledger",
        _valid_order(
            operation_kind="cancel",
            original_order_id=original,
            status="cancelled",
        ),
    )
    await _insert(
        db_session,
        "kiwoom_mock_order_ledger",
        _valid_order(
            operation_kind="cancel",
            original_order_id=original,
            status="pending",
        ),
    )
    await db_session.rollback()


@pytest.mark.parametrize(
    ("overrides", "names"),
    [
        (
            {"account_mode": "kis_mock"},
            ("ck_kiwoom_mock_signal_ledger_account_mode",),
        ),
        ({"decision": "maybe"}, ("ck_kiwoom_mock_signal_ledger_decision",)),
        (
            {"outcome_state": "done"},
            ("ck_kiwoom_mock_signal_ledger_outcome_state",),
        ),
        ({"side": "hold"}, ("ck_kiwoom_mock_signal_ledger_side",)),
        (
            {"strategy": "   "},
            ("ck_kiwoom_mock_signal_ledger_strategy_nonblank",),
        ),
        (
            {"signal_source": ""},
            ("ck_kiwoom_mock_signal_ledger_source_nonblank",),
        ),
        (
            {"correlation_id": " "},
            ("ck_kiwoom_mock_signal_ledger_correlation_nonblank",),
        ),
    ],
)
async def test_signal_ledger_check_refuses_bad_row(
    db_session: AsyncSession, overrides: dict, names: tuple[str, ...]
) -> None:
    await _expect_refused(
        db_session, "kiwoom_mock_signal_ledger", _valid_signal(**overrides), names
    )


async def test_signal_ledger_admits_both_mock_modes_and_refuses_duplicate(
    db_session: AsyncSession,
) -> None:
    first = _valid_signal()
    await _insert(db_session, "kiwoom_mock_signal_ledger", first)
    await _insert(
        db_session,
        "kiwoom_mock_signal_ledger",
        _valid_signal(account_mode="kiwoom_mock_us"),
    )
    await _expect_refused(
        db_session,
        "kiwoom_mock_signal_ledger",
        _valid_signal(correlation_id=first["correlation_id"]),
        ("uq_kiwoom_mock_signal_ledger_correlation_id",),
    )


async def test_order_proposals_check_admits_kiwoom_mock_only(
    db_session: AsyncSession,
) -> None:
    definition = (
        await db_session.execute(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conrelid = 'review.order_proposals'::regclass "
                f"AND conname = '{PROPOSAL_CHECK}'"
            )
        )
    ).scalar_one()
    assert "'kiwoom_mock'" in definition
    assert "kiwoom_mock_us" not in definition


async def _table_names(conn) -> set[str]:
    rows = await conn.execute(
        text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'review' AND table_name = ANY(:names)"
        ),
        {"names": list(NEW_TABLES)},
    )
    return {row[0] for row in rows}


async def _proposal_check_definition(conn) -> str:
    return (
        (
            await conn.execute(
                text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conrelid = 'review.order_proposals'::regclass "
                    "AND contype = 'c' AND pg_get_constraintdef(oid) LIKE '%account_mode%'"
                )
            )
        )
        .scalars()
        .all()
    )


def _load_migration():
    spec = importlib.util.spec_from_file_location("hk4703_migration_db", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_step(sync_conn, step: str) -> None:
    migration = _load_migration()
    context = MigrationContext.configure(sync_conn)
    with Operations.context(context):
        getattr(migration, step)()


async def test_upgrade_downgrade_upgrade_round_trip_and_downgrade_guard() -> None:
    validate_run_owned_database_url(engine.url)
    async with engine.connect() as conn:
        try:
            # Start from the bootstrapped (create_all) state, where the new
            # tables and the widened CHECK already exist.
            assert set(NEW_TABLES) <= await _table_names(conn)

            await conn.run_sync(_run_step, "downgrade")
            assert await _table_names(conn) == set()
            old_definitions = await _proposal_check_definition(conn)
            assert all("kiwoom_mock" not in d for d in old_definitions)

            await conn.run_sync(_run_step, "upgrade")
            assert set(NEW_TABLES) <= await _table_names(conn)
            new_definitions = await _proposal_check_definition(conn)
            assert any("'kiwoom_mock'" in d for d in new_definitions)

            # Downgrade must refuse while a signal row exists.
            await conn.execute(
                text(
                    "INSERT INTO review.kiwoom_mock_signal_ledger "
                    "(correlation_id, strategy, signal_source, account_mode, symbol, "
                    "decision, kst_date) VALUES "
                    "('kiwoom-mock:round-trip', 's', 'src', 'kiwoom_mock', "
                    "'005930', 'order', '2026-11-16')"
                )
            )
            with pytest.raises(DBAPIError) as excinfo:
                await conn.run_sync(_run_step, "downgrade")
            assert "restrict_violation" in str(excinfo.value) or (
                "export before downgrading" in str(excinfo.value)
            )
        finally:
            await conn.rollback()

"""Run-owned PostgreSQL coverage for #728 protection declarations."""

from __future__ import annotations

import asyncio
import importlib.util
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.models.base import Base
from app.services.protected_quantity_service import (
    BrokerPositionObservation,
    ProtectedQuantityConflictError,
    ProtectedQuantityService,
    ProtectedQuantityValidationError,
    normalize_protection_key,
    prepare_live_sell_lease,
)

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[3]
MIGRATION_PATH = ROOT / "alembic/versions/20260925_rob728_protected_positions.py"
TABLE = "protected_positions"
SCHEMA = "review"


def _observation(*, held: str = "10", sellable: str = "10"):
    return BrokerPositionObservation(
        held=Decimal(held),
        sellable=Decimal(sellable),
        observed_at=datetime.now(UTC),
    )


async def _save(
    service: ProtectedQuantityService,
    *,
    symbol: str,
    quantity: str,
    expected_revision: int | None,
    idempotency_key: str,
    confirm_symbol: str | None = None,
    reconfirm: bool = False,
    held: str = "10",
    sellable: str = "10",
):
    return await service.save(
        account_scope="kis_live",
        market="kr",
        symbol=symbol,
        protected_quantity=quantity,
        expected_revision=expected_revision,
        reason="test declaration",
        idempotency_key=idempotency_key,
        actor_user_id=7,
        origin="invest_ui",
        observation=_observation(held=held, sellable=sellable),
        reconfirm=reconfirm,
        confirm_protection_change=True,
        confirm_symbol=confirm_symbol,
    )


@pytest.mark.asyncio
async def test_service_writes_head_and_append_only_revision_in_one_contract(
    db_session,
) -> None:
    symbol = f"Z{uuid4().hex[:7].upper()}"
    service = ProtectedQuantityService(db_session)
    declared = await _save(
        service,
        symbol=symbol,
        quantity="6",
        expected_revision=None,
        idempotency_key=f"declare-{uuid4()}",
    )
    reconfirmed = await _save(
        service,
        symbol=symbol,
        quantity="6",
        expected_revision=declared.revision,
        idempotency_key=f"reconfirm-{uuid4()}",
        reconfirm=True,
    )

    assert declared.revision == 1
    assert reconfirmed.revision == 2
    assert reconfirmed.action == "reconfirm"

    head = await service.get(
        key=normalize_protection_key(
            account_scope="kis_live", market="kr", symbol=symbol
        )
    )
    history = await service.list_revisions(key=head.key) if head else []
    assert head is not None
    assert head.protected_quantity == Decimal("6")
    assert [(row.revision, row.action) for row in history] == [
        (1, "declare"),
        (2, "reconfirm"),
    ]


@pytest.mark.asyncio
async def test_service_confirmation_staleness_and_replay(db_session) -> None:
    symbol = f"Z{uuid4().hex[:7].upper()}"
    service = ProtectedQuantityService(db_session)
    idempotency_key = f"declare-{uuid4()}"
    declared = await _save(
        service,
        symbol=symbol,
        quantity="6",
        expected_revision=None,
        idempotency_key=idempotency_key,
    )
    replay = await _save(
        service,
        symbol=symbol,
        quantity="6",
        expected_revision=None,
        idempotency_key=idempotency_key,
    )
    assert replay.idempotent_replay is True
    assert replay.revision == declared.revision == 1

    with pytest.raises(ProtectedQuantityConflictError) as confirm_error:
        await service.save(
            account_scope="kis_live",
            market="kr",
            symbol=symbol,
            protected_quantity="5",
            expected_revision=1,
            reason="test declaration",
            idempotency_key=f"unconfirmed-{uuid4()}",
            actor_user_id=7,
            origin="invest_ui",
            observation=_observation(),
        )
    assert confirm_error.value.error == "confirm_required"

    with pytest.raises(ProtectedQuantityConflictError) as symbol_error:
        await _save(
            service,
            symbol=symbol,
            quantity="5",
            expected_revision=1,
            idempotency_key=f"missing-symbol-{uuid4()}",
        )
    assert symbol_error.value.error == "symbol_confirmation_required"

    decreased = await _save(
        service,
        symbol=symbol,
        quantity="5",
        expected_revision=1,
        idempotency_key=f"decrease-{uuid4()}",
        confirm_symbol=symbol,
    )
    assert decreased.action == "decrease"
    assert decreased.revision == 2

    with pytest.raises(ProtectedQuantityConflictError) as stale_error:
        await _save(
            service,
            symbol=symbol,
            quantity="4",
            expected_revision=1,
            idempotency_key=f"stale-{uuid4()}",
            confirm_symbol=symbol,
        )
    assert stale_error.value.error == "stale_form"

    history = await service.list_revisions(
        key=normalize_protection_key(
            account_scope="kis_live", market="kr", symbol=symbol
        )
    )
    assert [(row.revision, row.action) for row in history] == [
        (1, "declare"),
        (2, "decrease"),
    ]


@pytest.mark.asyncio
async def test_service_rejects_floor_above_fresh_held(db_session) -> None:
    service = ProtectedQuantityService(db_session)
    with pytest.raises(ProtectedQuantityValidationError, match="must not exceed"):
        await service.save(
            account_scope="kis_live",
            market="kr",
            symbol=f"Z{uuid4().hex[:7].upper()}",
            protected_quantity="10.00000001",
            expected_revision=None,
            reason="test declaration",
            idempotency_key=f"above-held-{uuid4()}",
            actor_user_id=7,
            origin="invest_ui",
            observation=_observation(held="10"),
            confirm_protection_change=True,
        )


@pytest.mark.asyncio
async def test_service_rejects_every_declaration_direction_without_fresh_evidence(
    db_session,
) -> None:
    symbol = f"Z{uuid4().hex[:7].upper()}"
    service = ProtectedQuantityService(db_session)
    declared = await _save(
        service,
        symbol=symbol,
        quantity="6",
        expected_revision=None,
        idempotency_key=f"declare-{uuid4()}",
    )
    unavailable_observation = cast(BrokerPositionObservation, None)
    attempts: tuple[tuple[str, bool, str | None], ...] = (
        ("7", False, None),
        ("5", False, symbol),
        ("0", False, symbol),
        ("6", True, None),
    )

    for protected_quantity, reconfirm, confirm_symbol in attempts:
        with pytest.raises(
            ProtectedQuantityValidationError,
            match="fresh broker observation is required",
        ):
            await service.save(
                account_scope="kis_live",
                market="kr",
                symbol=symbol,
                protected_quantity=protected_quantity,
                expected_revision=declared.revision,
                reason="must have fresh broker evidence",
                idempotency_key=f"no-evidence-{uuid4()}",
                actor_user_id=7,
                origin="invest_ui",
                observation=unavailable_observation,
                reconfirm=reconfirm,
                confirm_protection_change=True,
                confirm_symbol=confirm_symbol,
            )

    head = await service.get(
        key=normalize_protection_key(
            account_scope="kis_live", market="kr", symbol=symbol
        )
    )
    assert head is not None
    assert head.revision == declared.revision == 1
    assert head.protected_quantity == Decimal("6")


@pytest.mark.asyncio
async def test_declaration_write_waits_for_active_live_sell_lease(db_session) -> None:
    """A P increase cannot race a protected sell between G and broker reply."""

    symbol = f"Z{uuid4().hex[:7].upper()}"
    declared = await _save(
        ProtectedQuantityService(db_session),
        symbol=symbol,
        quantity="6",
        expected_revision=None,
        idempotency_key=f"declare-{uuid4()}",
    )
    settings_obj = SimpleNamespace(
        protected_quantity_mode_kis_live="enforce",
        protected_quantity_mode_toss_live="off",
        protected_quantity_mode_upbit_live="off",
    )
    lease = await prepare_live_sell_lease(
        account_scope="kis_live",
        market="kr",
        symbol=symbol,
        settings_obj=settings_obj,
    )
    assert lease.active is True

    from app.core.db import AsyncSessionLocal

    async with AsyncSessionLocal() as writer_db:
        writer = ProtectedQuantityService(writer_db)
        pending = asyncio.create_task(
            _save(
                writer,
                symbol=symbol,
                quantity="7",
                expected_revision=declared.revision,
                idempotency_key=f"increase-{uuid4()}",
            )
        )
        try:
            await asyncio.sleep(0.1)
            assert pending.done() is False
        finally:
            await lease.release()
        updated = await asyncio.wait_for(pending, timeout=3)

    assert updated.action == "increase"
    assert updated.revision == 2


@pytest.mark.asyncio
async def test_double_protected_sell_serializes_and_rechecks_fresh_headroom(
    db_session,
) -> None:
    """Two q=40 sends cannot both consume an S=100, P=60 headroom."""

    symbol = f"Z{uuid4().hex[:7].upper()}"
    await _save(
        ProtectedQuantityService(db_session),
        symbol=symbol,
        quantity="60",
        expected_revision=None,
        idempotency_key=f"declare-{uuid4()}",
        held="100",
        sellable="100",
    )
    settings_obj = SimpleNamespace(
        protected_quantity_mode_kis_live="enforce",
        protected_quantity_mode_toss_live="off",
        protected_quantity_mode_upbit_live="off",
    )
    first_checked = asyncio.Event()
    release_first = asyncio.Event()
    sellable = Decimal("100")
    sends: list[str] = []

    async def simulated_send(name: str) -> bool:
        nonlocal sellable
        lease = await prepare_live_sell_lease(
            account_scope="kis_live",
            market="kr",
            symbol=symbol,
            settings_obj=settings_obj,
        )
        try:
            decision = await lease.evaluate(
                quantity=Decimal("40"),
                kind="new",
                fresh_broker_sellable=sellable,
                fresh_broker_held=Decimal("100"),
                sellable_observed=True,
            )
            if not decision.allowed:
                return False
            sends.append(name)
            # A successful broker response has reserved the sellable amount
            # before the next caller gets the same protected-key lease.
            sellable -= Decimal("40")
            if name == "first":
                first_checked.set()
                await release_first.wait()
            return True
        finally:
            await lease.release()

    first = asyncio.create_task(simulated_send("first"))
    await asyncio.wait_for(first_checked.wait(), timeout=3)
    second = asyncio.create_task(simulated_send("second"))
    await asyncio.sleep(0.1)
    assert second.done() is False
    release_first.set()

    assert await asyncio.wait_for(first, timeout=3) is True
    assert await asyncio.wait_for(second, timeout=3) is False
    assert sends == ["first"]


@pytest.mark.asyncio
async def test_database_checks_and_append_only_revision_trigger(db_session) -> None:
    symbol = f"Z{uuid4().hex[:7].upper()}"
    service = ProtectedQuantityService(db_session)
    declared = await _save(
        service,
        symbol=symbol,
        quantity="6",
        expected_revision=None,
        idempotency_key=f"append-{uuid4()}",
    )

    with pytest.raises(DBAPIError):
        await db_session.execute(
            text(
                "UPDATE review.protected_position_revisions "
                "SET reason = 'mutated' WHERE protected_position_id = :id"
            ),
            {"id": declared.head.id},
        )
        await db_session.commit()
    await db_session.rollback()

    with pytest.raises(DBAPIError):
        await db_session.execute(text("TRUNCATE review.protected_position_revisions"))
        await db_session.commit()
    await db_session.rollback()

    with pytest.raises(DBAPIError):
        await db_session.execute(
            text(
                "DELETE FROM review.protected_position_revisions "
                "WHERE protected_position_id = :id"
            ),
            {"id": declared.head.id},
        )
        await db_session.commit()
    await db_session.rollback()

    with pytest.raises(DBAPIError):
        await db_session.execute(
            text(
                "INSERT INTO review.protected_positions "
                "(account_scope, market, symbol, protected_quantity, purpose, revision, "
                "last_confirmed_broker_held, last_confirmed_at, updated_by_user_id) "
                "VALUES ('kis_live', 'kr', :symbol, -1, 'long_term', 1, 0, now(), 7)"
            ),
            {"symbol": f"Z{uuid4().hex[:7].upper()}"},
        )
        await db_session.commit()
    await db_session.rollback()

    with pytest.raises(DBAPIError):
        await db_session.execute(
            text(
                "INSERT INTO review.protected_positions "
                "(account_scope, market, symbol, protected_quantity, purpose, revision, "
                "last_confirmed_broker_held, last_confirmed_at, updated_by_user_id) "
                "VALUES ('upbit_live', 'kr', :symbol, 0, 'long_term', 1, 0, now(), 7)"
            ),
            {"symbol": f"Z{uuid4().hex[:7].upper()}"},
        )
        await db_session.commit()
    await db_session.rollback()


def _load_migration():
    spec = importlib.util.spec_from_file_location("rob728_migration", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_revision_fits_default_alembic_version_column() -> None:
    migration = _load_migration()
    assert len(migration.revision) <= 32


def _has_table(connection: sa.Connection) -> bool:
    return sa.inspect(connection).has_table(TABLE, schema=SCHEMA)


def _roundtrip(connection: sa.Connection) -> list[bool]:
    migration = _load_migration()
    context = MigrationContext.configure(
        connection=connection,
        opts={"target_metadata": Base.metadata},
    )
    with Operations.context(context):
        migration.downgrade()
        removed = _has_table(connection)
        migration.upgrade()
        restored = _has_table(connection)
    return [removed, restored]


@pytest.mark.asyncio
async def test_migration_upgrade_downgrade_roundtrip_uses_only_run_owned_test_db(
    _bootstrap_test_schema,
) -> None:
    from app.core.db import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        connection = await session.connection()
        trace = await connection.run_sync(_roundtrip)
        await session.rollback()

    assert trace == [False, True]

"""Real-PostgreSQL acceptance for the ROB-1195 persistence boundary.

Every test here runs the actual repository SQL, constraints, and triggers
against the isolated pytest-owned local ``test_db`` database (see
``tests/_run_owned_database.py``, which refuses any base URL whose database is
not ``test_db``). No broker, external API, shared database, or production
database is touched.

Observations are append-only by design, so these tests can never delete their
rows. Each test therefore scopes itself with a fresh UUID order scope and
asserts only on its own rows.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import asyncpg
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.config import settings
from app.models.base import Base
from app.models.fill_observation import (
    FillObservation,
    FillProjectionCursor,
    FillProjectionOutbox,
    FillSettlementEnrichment,
)
from app.models.trading import InstrumentType
from app.services.fill_observation.contracts import (
    BrokerFillEvidence,
    FillObservationWriteStatus,
    FillSettlementStatus,
)
from app.services.fill_observation.errors import (
    FillObservationIdentityConflict,
    FillProjectionCursorRegression,
    FillProjectionLeaseMismatch,
)
from app.services.fill_observation.identity import (
    derive_fill_observation_identity,
    normalize_fill_evidence,
)
from app.services.fill_observation.projection import FillProjectionQueue
from app.services.fill_observation.repository import (
    FillObservationRepository,
    FillProjectionRepository,
)
from app.services.fill_observation.writer import FillObservationWriter

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[3]
PROJECTION = "legacy_dual_read_validation.v1"


def _order_scope() -> dict[str, str]:
    """Return a unique, never-reused order scope for one test."""
    nonce = uuid.uuid4().hex
    return {
        "broker": "toss",
        "account_ref": f"acct-{nonce}",
        "account_mode": "live",
        "venue": "toss_us",
        "order_id": f"order-{nonce}",
    }


def _evidence(scope: dict[str, str], **overrides: object) -> BrokerFillEvidence:
    values: dict[str, object] = {
        **scope,
        "instrument_type": InstrumentType.equity_us,
        "symbol": "BRK-B",
        "side": "buy",
        "currency": "usd",
        "evidence_source": "reconciler",
        "evidence_ref": "toss_live_order_ledger:1",
        "observed_at": datetime(2026, 8, 2, 12, 0, tzinfo=UTC),
        "cumulative_quantity": Decimal("2.5"),
        "average_price": Decimal("430.25"),
        "fee_total": Decimal("0"),
        "filled_at": datetime(2026, 8, 2, 11, 59, tzinfo=UTC),
    }
    values.update(overrides)
    return BrokerFillEvidence(**values)  # type: ignore[arg-type]


def _writer() -> FillObservationWriter:
    """Build a writer that owns its own session exactly like production."""
    return FillObservationWriter(enabled=True)


async def _count_observations(session: AsyncSession, identity: str) -> int:
    return int(
        await session.scalar(
            select(func.count())
            .select_from(FillObservation)
            .where(FillObservation.observation_identity == identity)
        )
        or 0
    )


async def _count_outbox(session: AsyncSession, observation_id: int) -> int:
    return int(
        await session.scalar(
            select(func.count())
            .select_from(FillProjectionOutbox)
            .where(FillProjectionOutbox.fill_observation_id == observation_id)
        )
        or 0
    )


@pytest.mark.asyncio
async def test_observation_and_outbox_commit_together_in_one_transaction(
    db_session: AsyncSession,
) -> None:
    scope = _order_scope()
    result = await _writer().write(_evidence(scope))

    assert result.status is FillObservationWriteStatus.INSERTED
    assert result.observation_id is not None
    assert result.outbox_count == 1

    # Read the durable rows back through a fresh statement, not the write result.
    observation = await db_session.scalar(
        select(FillObservation).where(FillObservation.id == result.observation_id)
    )
    assert observation is not None
    assert observation.fill_delta_quantity == Decimal("2.5")
    assert observation.symbol == "BRK.B"

    outbox_rows = list(
        (
            await db_session.scalars(
                select(FillProjectionOutbox).where(
                    FillProjectionOutbox.fill_observation_id == result.observation_id
                )
            )
        ).all()
    )
    assert len(outbox_rows) == 1
    assert outbox_rows[0].projection_name == PROJECTION
    assert outbox_rows[0].state == "pending"
    assert outbox_rows[0].lease_token is None


@pytest.mark.asyncio
async def test_failed_outbox_insert_rolls_back_the_observation(
    db_session: AsyncSession,
) -> None:
    scope = _order_scope()
    evidence = normalize_fill_evidence(_evidence(scope))
    identity = derive_fill_observation_identity(evidence)

    with pytest.raises(IntegrityError):
        async with db_session.begin():
            repository = FillObservationRepository(db_session)
            await repository.append(
                evidence=evidence,
                identity=identity,
                fill_delta_quantity=Decimal("2.5"),
                projection_names=(PROJECTION,),
            )
            # A second delivery row for the same (projection, observation) pair
            # violates uq_fill_projection_outbox_observation at flush time.
            await db_session.execute(
                text(
                    "INSERT INTO review.fill_projection_outbox "
                    "(delivery_key, projection_name, partition_key, "
                    "fill_observation_id, state, attempt_count) "
                    "SELECT :delivery_key, :projection, :partition, id, "
                    "'pending', 0 FROM review.fill_observations "
                    "WHERE observation_identity = :identity"
                ),
                {
                    "delivery_key": uuid.uuid4().hex + uuid.uuid4().hex[:32],
                    "projection": PROJECTION,
                    "partition": identity.partition_key,
                    "identity": identity.value,
                },
            )
    await db_session.rollback()

    assert await _count_observations(db_session, identity.value) == 0
    orphan_outbox = await db_session.scalar(
        select(func.count())
        .select_from(FillProjectionOutbox)
        .where(FillProjectionOutbox.partition_key == identity.partition_key)
    )
    assert orphan_outbox == 0


@pytest.mark.asyncio
async def test_writer_exception_leaves_neither_observation_nor_outbox(
    db_session: AsyncSession,
) -> None:
    scope = _order_scope()
    evidence = normalize_fill_evidence(_evidence(scope))
    identity = derive_fill_observation_identity(evidence)

    class _Boom(RuntimeError):
        pass

    with pytest.raises(_Boom):
        async with db_session.begin():
            repository = FillObservationRepository(db_session)
            await repository.append(
                evidence=evidence,
                identity=identity,
                fill_delta_quantity=Decimal("2.5"),
                projection_names=(PROJECTION,),
            )
            raise _Boom("delivery planning failed after the append")
    await db_session.rollback()

    assert await _count_observations(db_session, identity.value) == 0
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(FillProjectionOutbox)
            .where(FillProjectionOutbox.partition_key == identity.partition_key)
        )
        == 0
    )


@pytest.mark.asyncio
async def test_observation_update_delete_and_truncate_are_rejected(
    db_session: AsyncSession,
) -> None:
    scope = _order_scope()
    result = await _writer().write(_evidence(scope))
    observation_id = result.observation_id
    assert observation_id is not None

    with pytest.raises(DBAPIError, match="append-only"):
        await db_session.execute(
            update(FillObservation)
            .where(FillObservation.id == observation_id)
            .values(fill_delta_quantity=Decimal("99"))
        )
        await db_session.commit()
    await db_session.rollback()

    with pytest.raises(DBAPIError, match="append-only"):
        await db_session.execute(
            delete(FillObservation).where(FillObservation.id == observation_id)
        )
        await db_session.commit()
    await db_session.rollback()

    with pytest.raises(DBAPIError, match="append-only"):
        await db_session.execute(
            text("TRUNCATE TABLE review.fill_observations CASCADE")
        )
    await db_session.rollback()

    # The row survived every rejected mutation.
    survivor = await db_session.scalar(
        select(FillObservation).where(FillObservation.id == observation_id)
    )
    assert survivor is not None
    assert survivor.fill_delta_quantity == Decimal("2.5")


@pytest.mark.asyncio
async def test_settlement_enrichment_is_append_only_and_never_mutates_the_fill(
    db_session: AsyncSession,
) -> None:
    scope = _order_scope()
    writer = _writer()

    booked = await writer.write(_evidence(scope, fee_total=Decimal("0")))
    assert booked.status is FillObservationWriteStatus.INSERTED
    assert booked.settlement_status is FillSettlementStatus.RECORDED
    assert booked.settlement_revision == 1
    observation_id = booked.observation_id
    assert observation_id is not None

    # Same fill re-polled after fee settlement and average-price refinement.
    settled = await writer.write(
        _evidence(
            scope,
            fee_total=Decimal("1.5"),
            average_price=Decimal("430.31"),
            filled_at=datetime(2026, 8, 2, 11, 59, 30, tzinfo=UTC),
            evidence_ref="toss_live_order_ledger:2",
        )
    )

    assert settled.status is FillObservationWriteStatus.DUPLICATE
    assert settled.fill_delta_quantity == 0
    assert settled.outbox_count == 0
    assert settled.settlement_status is FillSettlementStatus.RECORDED
    assert settled.settlement_revision == 2

    # Replaying the settled poll is idempotent.
    replay = await writer.write(
        _evidence(
            scope,
            fee_total=Decimal("1.5"),
            average_price=Decimal("430.31"),
            filled_at=datetime(2026, 8, 2, 11, 59, 30, tzinfo=UTC),
            evidence_ref="toss_live_order_ledger:3",
            observed_at=datetime(2026, 8, 2, 13, 0, tzinfo=UTC),
        )
    )
    assert replay.settlement_status is FillSettlementStatus.UNCHANGED
    assert replay.settlement_revision == 2

    revisions = list(
        (
            await db_session.scalars(
                select(FillSettlementEnrichment)
                .where(FillSettlementEnrichment.fill_observation_id == observation_id)
                .order_by(FillSettlementEnrichment.revision)
            )
        ).all()
    )
    assert [row.revision for row in revisions] == [1, 2]
    assert revisions[0].fee_total == Decimal("0")
    assert revisions[1].fee_total == Decimal("1.5")

    # Exactly one observation, one outbox row, and an unchanged immutable row.
    assert await _count_observations(db_session, booked.observation_identity or "") == 1
    assert await _count_outbox(db_session, observation_id) == 1
    observation = await db_session.scalar(
        select(FillObservation).where(FillObservation.id == observation_id)
    )
    assert observation is not None
    assert observation.fee_total == Decimal("0")
    assert observation.filled_at == datetime(2026, 8, 2, 11, 59, tzinfo=UTC)

    with pytest.raises(DBAPIError, match="append-only"):
        await db_session.execute(
            update(FillSettlementEnrichment)
            .where(FillSettlementEnrichment.id == revisions[0].id)
            .values(fee_total=Decimal("9"))
        )
        await db_session.commit()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_contradicted_fill_fact_still_fails_closed_with_zero_write(
    db_session: AsyncSession,
) -> None:
    scope = _order_scope()
    writer = _writer()
    booked = await writer.write(_evidence(scope))
    observation_id = booked.observation_id
    assert observation_id is not None

    with pytest.raises(FillObservationIdentityConflict):
        await writer.write(_evidence(scope, side="sell"))

    assert await _count_observations(db_session, booked.observation_identity or "") == 1
    assert await _count_outbox(db_session, observation_id) == 1
    settlement_rows = await db_session.scalar(
        select(func.count())
        .select_from(FillSettlementEnrichment)
        .where(FillSettlementEnrichment.fill_observation_id == observation_id)
    )
    assert settlement_rows == 1


async def _seed_two_partition_deliveries(
    scope: dict[str, str],
) -> tuple[int, int, str]:
    writer = _writer()
    first = await writer.write(
        _evidence(
            scope,
            broker_fill_sequence="fill-1",
            cumulative_quantity=Decimal("2.5"),
            fill_quantity=Decimal("2.5"),
        )
    )
    second = await writer.write(
        _evidence(
            scope,
            broker_fill_sequence="fill-2",
            cumulative_quantity=Decimal("4.0"),
            fill_quantity=Decimal("1.5"),
        )
    )
    assert first.observation_id is not None
    assert second.observation_id is not None
    identity = derive_fill_observation_identity(
        normalize_fill_evidence(
            _evidence(
                scope,
                broker_fill_sequence="fill-1",
                cumulative_quantity=Decimal("2.5"),
            )
        )
    )
    return first.observation_id, second.observation_id, identity.partition_key


@pytest.mark.asyncio
async def test_unfinished_partition_predecessor_blocks_the_next_claim() -> None:
    scope = _order_scope()
    first_id, second_id, _partition = await _seed_two_partition_deliveries(scope)
    queue = FillProjectionQueue()

    claimed = await queue.claim(projection_name=PROJECTION, limit=50)
    mine = [
        delivery
        for delivery in claimed
        if delivery.fill_observation_id in {first_id, second_id}
    ]
    assert [delivery.fill_observation_id for delivery in mine] == [first_id]

    # The successor stays invisible while its predecessor is unfinished.
    again = await queue.claim(projection_name=PROJECTION, limit=50)
    assert [
        delivery.fill_observation_id
        for delivery in again
        if delivery.fill_observation_id == second_id
    ] == []

    await queue.complete(outbox_id=mine[0].outbox_id, lease_token=mine[0].lease_token)

    after = await queue.claim(projection_name=PROJECTION, limit=50)
    assert [
        delivery.fill_observation_id
        for delivery in after
        if delivery.fill_observation_id == second_id
    ] == [second_id]


@pytest.mark.asyncio
async def test_for_update_skip_locked_hides_rows_locked_by_another_session() -> None:
    from app.core.db import AsyncSessionLocal

    scope = _order_scope()
    first_id, _second_id, _partition = await _seed_two_partition_deliveries(scope)

    holder = AsyncSessionLocal()
    try:
        await holder.begin()
        locked = await holder.scalar(
            select(FillProjectionOutbox)
            .where(FillProjectionOutbox.fill_observation_id == first_id)
            .with_for_update()
        )
        assert locked is not None

        claimed = await FillProjectionQueue().claim(
            projection_name=PROJECTION, limit=50
        )
        assert [
            delivery.fill_observation_id
            for delivery in claimed
            if delivery.fill_observation_id == first_id
        ] == []
    finally:
        await holder.rollback()
        await holder.close()


@pytest.mark.asyncio
async def test_expired_lease_is_refenced_and_the_stale_token_is_rejected(
    db_session: AsyncSession,
) -> None:
    scope = _order_scope()
    first_id, _second_id, _partition = await _seed_two_partition_deliveries(scope)

    stale_clock = datetime(2026, 8, 2, 9, 0, tzinfo=UTC)
    stale_queue = FillProjectionQueue(clock=lambda: stale_clock)
    stale = await stale_queue.claim(
        projection_name=PROJECTION, limit=50, lease_seconds=1
    )
    stale_mine = [
        delivery for delivery in stale if delivery.fill_observation_id == first_id
    ]
    assert len(stale_mine) == 1

    fresh_clock = stale_clock + timedelta(hours=1)
    fresh_queue = FillProjectionQueue(clock=lambda: fresh_clock)
    refenced = await fresh_queue.claim(projection_name=PROJECTION, limit=50)
    refenced_mine = [
        delivery for delivery in refenced if delivery.fill_observation_id == first_id
    ]
    assert len(refenced_mine) == 1
    assert refenced_mine[0].lease_token != stale_mine[0].lease_token
    assert refenced_mine[0].attempt_count == stale_mine[0].attempt_count + 1

    with pytest.raises(FillProjectionLeaseMismatch):
        await fresh_queue.complete(
            outbox_id=stale_mine[0].outbox_id,
            lease_token=stale_mine[0].lease_token,
        )

    with pytest.raises(FillProjectionLeaseMismatch):
        await fresh_queue.retry(
            outbox_id=stale_mine[0].outbox_id,
            lease_token=stale_mine[0].lease_token,
            error="stale worker",
        )

    await fresh_queue.complete(
        outbox_id=refenced_mine[0].outbox_id,
        lease_token=refenced_mine[0].lease_token,
    )
    settled = await db_session.scalar(
        select(FillProjectionOutbox).where(
            FillProjectionOutbox.id == refenced_mine[0].outbox_id
        )
    )
    assert settled is not None
    assert settled.state == "succeeded"
    assert settled.lease_token is None
    assert settled.completed_at is not None


@pytest.mark.asyncio
async def test_cursor_advances_forward_and_refuses_to_move_backwards(
    db_session: AsyncSession,
) -> None:
    scope = _order_scope()
    first_id, second_id, partition = await _seed_two_partition_deliveries(scope)
    queue = FillProjectionQueue()

    first_batch = await queue.claim(projection_name=PROJECTION, limit=50)
    first_delivery = next(
        delivery for delivery in first_batch if delivery.fill_observation_id == first_id
    )
    await queue.complete(
        outbox_id=first_delivery.outbox_id,
        lease_token=first_delivery.lease_token,
    )

    cursor = await db_session.scalar(
        select(FillProjectionCursor)
        .where(FillProjectionCursor.projection_name == PROJECTION)
        .where(FillProjectionCursor.partition_key == partition)
    )
    assert cursor is not None
    assert cursor.last_fill_observation_id == first_id

    second_batch = await queue.claim(projection_name=PROJECTION, limit=50)
    second_delivery = next(
        delivery
        for delivery in second_batch
        if delivery.fill_observation_id == second_id
    )
    await queue.complete(
        outbox_id=second_delivery.outbox_id,
        lease_token=second_delivery.lease_token,
    )

    # The pre-existing cursor advanced instead of being recreated.
    await db_session.refresh(cursor)
    assert cursor.last_fill_observation_id == second_id
    assert cursor.advanced_at is not None

    # Replaying the older delivery must never move the cursor backwards.
    await db_session.commit()
    async with db_session.begin():
        repository = FillProjectionRepository(db_session)
        replayed = await repository.get_outbox_for_update(first_delivery.outbox_id)
        assert replayed is not None
        replayed.state = "processing"
        replayed.lease_token = uuid.uuid4()
        replayed.lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)
        replayed.completed_at = None
        stale_token = replayed.lease_token

    with pytest.raises(FillProjectionCursorRegression):
        await queue.complete(
            outbox_id=first_delivery.outbox_id,
            lease_token=stale_token,
        )
    await db_session.rollback()

    survivor = await db_session.scalar(
        select(FillProjectionCursor)
        .where(FillProjectionCursor.projection_name == PROJECTION)
        .where(FillProjectionCursor.partition_key == partition)
    )
    assert survivor is not None
    assert survivor.last_fill_observation_id == second_id


@pytest.mark.asyncio
async def test_cursor_identity_mismatch_fails_closed(
    db_session: AsyncSession,
) -> None:
    from app.services.fill_observation.errors import FillProjectionDeliveryError

    scope = _order_scope()
    first_id, _second_id, partition = await _seed_two_partition_deliveries(scope)
    queue = FillProjectionQueue()

    batch = await queue.claim(projection_name=PROJECTION, limit=50)
    delivery = next(item for item in batch if item.fill_observation_id == first_id)
    await queue.complete(outbox_id=delivery.outbox_id, lease_token=delivery.lease_token)

    # Corrupt only the durable cursor identity, then replay the same delivery.
    await db_session.rollback()
    async with db_session.begin():
        cursor = await db_session.scalar(
            select(FillProjectionCursor)
            .where(FillProjectionCursor.projection_name == PROJECTION)
            .where(FillProjectionCursor.partition_key == partition)
        )
        assert cursor is not None
        cursor.last_observation_identity = "a" * 64
        outbox = await db_session.scalar(
            select(FillProjectionOutbox).where(
                FillProjectionOutbox.id == delivery.outbox_id
            )
        )
        assert outbox is not None
        outbox.state = "processing"
        outbox.lease_token = uuid.uuid4()
        outbox.lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)
        outbox.completed_at = None
        replay_token = outbox.lease_token

    with pytest.raises(FillProjectionDeliveryError):
        await queue.complete(outbox_id=delivery.outbox_id, lease_token=replay_token)
    await db_session.rollback()


@pytest.mark.asyncio
@pytest.mark.usefixtures("retrospective_action_control_lock")
async def test_real_postgresql_upgrade_downgrade_upgrade_single_head() -> None:
    """Apply, roll back, and re-apply ROB-1195 on a throwaway local database."""
    base_url = make_url(settings.DATABASE_URL)
    if base_url.get_backend_name() != "postgresql":
        pytest.skip("ROB-1195 migration acceptance requires PostgreSQL")

    config = Config(str(REPO / "alembic.ini"))
    config.set_main_option("script_location", str(REPO / "alembic"))
    script = ScriptDirectory.from_config(config)
    heads = script.get_heads()
    assert heads == ["20260801_rob1195_fillobs"]

    database = f"rob1195_migration_{uuid.uuid4().hex}"
    admin = await asyncpg.connect(
        user=base_url.username,
        password=base_url.password,
        host=base_url.host,
        port=base_url.port,
        database="postgres",
    )
    await admin.execute(f'CREATE DATABASE "{database}"')
    target_url = base_url.set(database=database)
    target_url_text = target_url.render_as_string(hide_password=False)
    engine = create_async_engine(target_url_text)
    try:
        async with engine.begin() as connection:
            for schema in ("paper", "research", "review"):
                await connection.execute(
                    text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
                )
            await connection.run_sync(Base.metadata.create_all)
            # create_all materializes the current head. Remove exactly the
            # ROB-1195 boundary so the migration itself is executed here.
            for table in (
                "fill_projection_cursors",
                "fill_projection_outbox",
                "fill_settlement_enrichments",
                "fill_observations",
            ):
                await connection.execute(text(f"DROP TABLE review.{table}"))

        env = {**os.environ, "DATABASE_URL": target_url_text}

        def alembic(*args: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [str(REPO / ".venv/bin/alembic"), *args],
                cwd=REPO,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

        for command in (
            ("stamp", "20260728_rob1109_watch_intent"),
            ("upgrade", "head"),
            ("downgrade", "20260728_rob1109_watch_intent"),
            ("upgrade", "head"),
        ):
            completed = await asyncio.to_thread(alembic, *command)
            assert completed.returncode == 0, completed.stdout + completed.stderr

        current = await asyncio.to_thread(alembic, "current")
        assert current.returncode == 0, current.stdout + current.stderr
        assert "20260801_rob1195_fillobs (head)" in current.stdout

        async with engine.connect() as connection:
            triggers = await connection.scalar(
                text(
                    "SELECT count(*) FROM pg_trigger AS t "
                    "JOIN pg_proc AS p ON p.oid = t.tgfoid "
                    "WHERE p.proname = 'reject_fill_observation_mutation' "
                    "AND NOT t.tgisinternal"
                )
            )
            assert triggers == 4

            # The migration-built shape must equal the ORM metadata, otherwise
            # the test-schema bootstrap and a migrated database diverge.
            for model in (
                FillObservation,
                FillSettlementEnrichment,
                FillProjectionOutbox,
                FillProjectionCursor,
            ):
                table = model.__table__
                rows = (
                    await connection.execute(
                        text(
                            "SELECT column_name, is_nullable "
                            "FROM information_schema.columns "
                            "WHERE table_schema = 'review' "
                            "AND table_name = :name"
                        ),
                        {"name": table.name},
                    )
                ).all()
                migrated = {name: nullable == "YES" for name, nullable in rows}
                declared = {
                    column.name: bool(column.nullable) for column in table.columns
                }
                assert migrated == declared, table.name

        # An evidence row makes the destructive downgrade refuse to run.
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO review.fill_observations ("
                    "observation_identity, identity_kind, broker, account_ref, "
                    "account_mode, venue, order_id, instrument_type, symbol, "
                    "side, currency, cumulative_quantity, fill_delta_quantity, "
                    "evidence_source, evidence_ref, fill_fact_hash, observed_at"
                    ") VALUES ("
                    ":identity, 'cumulative_quantity', 'toss', 'acct-guard', "
                    "'live', 'toss_us', 'order-guard', 'equity_us', 'BRK.B', "
                    "'buy', 'USD', 2.5, 2.5, 'reconciler', 'ledger:1', "
                    ":fact_hash, now())"
                ),
                {"identity": "b" * 64, "fact_hash": "c" * 64},
            )
        refused = await asyncio.to_thread(
            alembic, "downgrade", "20260728_rob1109_watch_intent"
        )
        assert refused.returncode != 0
        assert "cannot downgrade" in (refused.stdout + refused.stderr)
    finally:
        await engine.dispose()
        await admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = $1 AND pid <> pg_backend_pid()",
            database,
        )
        await admin.execute(f'DROP DATABASE IF EXISTS "{database}"')
        await admin.close()

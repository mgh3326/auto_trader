"""CI-only PostgreSQL coverage for the operator inference transaction."""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import event, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.execution_ledger import ExecutionLedgerReconcileRun
from app.models.review import KISUSInferenceExpiryEvent, LiveOrderLedger
from app.schemas.execution_ledger import ExecutionLedgerUpsert
from app.services.execution_ledger.repository import ExecutionLedgerRepository
from app.services.kis_us_inference_expiry import DECISION_REF
from app.services.kis_us_inference_expiry_service import (
    check_conflicts,
    commit,
    preview,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def test_preview_commit_retry_and_immutable_audit(db_session: AsyncSession):
    connection = await db_session.connection()
    async with AsyncSession(
        bind=connection,
        join_transaction_mode="create_savepoint",
        expire_on_commit=False,
    ) as session:
        accepted = datetime(2020, 1, 2, 15, tzinfo=UTC)
        orders = [
            LiveOrderLedger(
                trade_date=accepted,
                broker="kis",
                account_scope="kis_live",
                market="us",
                symbol=f"TEST{i}",
                side="buy",
                order_kind="limit",
                currency="USD",
                order_no=f"test4178{i}",
                order_time="000000",
                status="accepted",
                lifecycle_state="accepted",
            )
            for i in range(2)
        ]
        session.add_all(orders)
        run_id = uuid.uuid4()
        session.add(
            ExecutionLedgerReconcileRun(
                run_id=run_id,
                broker="kis",
                window_start=accepted - timedelta(days=1),
                window_end=accepted + timedelta(days=2),
                finished_at=accepted + timedelta(days=3),
                dry_run=False,
                would_insert=0,
                would_update=0,
                unchanged=0,
                committed_insert=0,
                committed_update=0,
            )
        )
        await session.commit()
        ids = [row.id for row in orders]
        plan = await preview(session)
        assert {p["ledger_id"] for p in plan if p["eligible"]} == set(ids)
        assert all(row.status == "accepted" for row in orders)
        assert (
            await session.execute(
                select(KISUSInferenceExpiryEvent).where(
                    KISUSInferenceExpiryEvent.ledger_id.in_(ids)
                )
            )
        ).scalars().all() == []
        await session.rollback()
        with pytest.raises(ValueError):
            await commit(
                session,
                ids=ids[:1],
                decision_ref=DECISION_REF,
                actor="test",
                reason="test",
            )
        for i in ids:
            assert (await session.get(LiveOrderLedger, i)).status == "accepted"
        await session.rollback()
        # A covering run may change between preview and the locked commit.
        await session.execute(
            update(ExecutionLedgerReconcileRun)
            .where(ExecutionLedgerReconcileRun.run_id == run_id)
            .values(error_summary="changed")
        )
        await session.commit()
        with pytest.raises(ValueError):
            await commit(
                session, ids=ids, decision_ref=DECISION_REF, actor="test", reason="test"
            )
        for i in ids:
            assert (await session.get(LiveOrderLedger, i)).status == "accepted"
        await session.execute(
            update(ExecutionLedgerReconcileRun)
            .where(ExecutionLedgerReconcileRun.run_id == run_id)
            .values(error_summary=None)
        )
        await session.commit()

        def trap_writes(conn, cursor, statement, parameters, context, executemany):
            normalized = " ".join(statement.lower().split())
            if normalized.startswith(("insert ", "update ", "delete ")):
                assert (
                    "review.live_order_ledger" in normalized
                    or "review.kis_us_inference_expiry_events" in normalized
                )
                assert "review.execution_ledger" not in normalized
                assert "journal" not in normalized and "pnl" not in normalized

        event.listen(connection.sync_connection, "before_cursor_execute", trap_writes)
        try:
            result = await commit(
                session, ids=ids, decision_ref=DECISION_REF, actor="test", reason="test"
            )
        finally:
            event.remove(
                connection.sync_connection, "before_cursor_execute", trap_writes
            )
        assert result["changed"] == 2
        for i in ids:
            row = await session.get(LiveOrderLedger, i)
            assert row.status == row.lifecycle_state == "expired"
            assert row.last_reconcile_detail["expiry_basis"] == "inference"
            assert (
                row.filled_qty
                is row.trade_id
                is row.journal_id
                is row.total_pnl_krw
                is None
            )
        events = (
            (
                await session.execute(
                    select(KISUSInferenceExpiryEvent).where(
                        KISUSInferenceExpiryEvent.ledger_id.in_(ids)
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 2
        await session.rollback()
        assert (
            await commit(
                session, ids=ids, decision_ref=DECISION_REF, actor="test", reason="test"
            )
        )["changed"] == 0
        with pytest.raises(ValueError):
            await commit(
                session,
                ids=ids[:1],
                decision_ref=DECISION_REF,
                actor="test",
                reason="test",
            )
        with pytest.raises(DBAPIError):
            async with session.begin_nested():
                await session.execute(
                    text(
                        "UPDATE review.kis_us_inference_expiry_events SET actor='changed'"
                    )
                )

        # A late-arriving record has its historical broker fill time, but is
        # inserted after inference. The read-only check must expose it.
        await ExecutionLedgerRepository(session).upsert_fill(
            ExecutionLedgerUpsert(
                broker="kis",
                account_mode="live",
                venue="NASD",
                instrument_type="equity_us",
                symbol="TEST0",
                raw_symbol="TEST0",
                side="buy",
                broker_order_id="test41780",
                filled_qty=Decimal("1"),
                filled_price=Decimal("10"),
                filled_at=accepted,
                currency="USD",
                source="reconciler",
            )
        )
        await session.commit()
        checks = await check_conflicts(session)
        by_id = {item["ledger_id"]: item for item in checks}
        assert by_id[ids[0]]["state"] == "conflict"
        assert by_id[ids[1]]["state"] == "clear"
        assert (await session.get(LiveOrderLedger, ids[0])).status == "expired"


async def test_commit_sets_postgres_timeouts_before_recheck(db_session, monkeypatch):
    from app.services import kis_us_inference_expiry_service as service

    connection = await db_session.connection()
    async with AsyncSession(
        bind=connection, join_transaction_mode="create_savepoint"
    ) as session:

        async def check_settings(session, *, lock=False):
            assert lock
            settings = (
                await session.execute(
                    text(
                        "SELECT current_setting('lock_timeout'), current_setting('statement_timeout')"
                    )
                )
            ).one()
            assert settings == ("2s", "5s")
            raise ValueError("stop after settings check")

        monkeypatch.setattr(service, "_candidates", check_settings)
        with pytest.raises(ValueError, match="stop after settings check"):
            await service.commit(
                session, ids=[1], decision_ref=DECISION_REF, actor="test", reason="test"
            )

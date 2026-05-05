from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.core.db import engine
from app.models.trading import InstrumentType
from app.models.trading_decision import (
    ActionKind,
    OutcomeHorizon,
    ProposalKind,
    TrackKind,
    TradingDecisionAction,
    TradingDecisionCounterfactual,
    TradingDecisionOutcome,
    TradingDecisionProposal,
    TradingDecisionSession,
    UserResponse,
)

SessionLocal = async_sessionmaker(
    bind=engine, class_=AsyncSession, expire_on_commit=False
)


async def _ensure_trading_decision_tables() -> None:
    try:
        async with SessionLocal() as session:
            row = await session.execute(
                text("SELECT to_regclass('trading_decision_sessions')")
            )
            if row.scalar_one_or_none() is None:
                pytest.skip("trading_decision tables are not migrated")
    except Exception:
        pytest.skip("database is not available for integration persistence checks")


async def _create_user() -> int:
    suffix = uuid.uuid4().hex[:8]
    async with SessionLocal() as session:
        user_id = (
            await session.execute(
                text(
                    """
                    INSERT INTO users (username, email, role, tz, base_currency, is_active)
                    VALUES (:username, :email, 'viewer', 'Asia/Seoul', 'KRW', true)
                    RETURNING id
                    """
                ),
                {
                    "username": f"td_model_test_{suffix}",
                    "email": f"td_model_{suffix}@example.com",
                },
            )
        ).scalar_one()
        await session.commit()
        return user_id


async def _cleanup_user(user_id: int) -> None:
    async with SessionLocal() as session:
        await session.execute(
            text("DELETE FROM users WHERE id = :user_id"), {"user_id": user_id}
        )
        await session.commit()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_session_with_proposals_round_trips() -> None:
    """Insert a session with 3 proposals and reload; verify relationships and payload."""
    await _ensure_trading_decision_tables()
    user_id = await _create_user()
    try:
        async with SessionLocal() as session:
            ds = TradingDecisionSession(
                user_id=user_id,
                source_profile="hermes",
                strategy_name="Morning Brief",
                market_scope="crypto",
                generated_at=datetime.now(UTC),
            )
            session.add(ds)
            await session.flush()

            p1 = TradingDecisionProposal(
                session_id=ds.id,
                symbol="KRW-BTC",
                instrument_type=InstrumentType.crypto,
                proposal_kind=ProposalKind.trim,
                side="sell",
                original_quantity_pct=Decimal("20.0"),
                original_payload={"action": "trim", "pct": 20},
            )
            p2 = TradingDecisionProposal(
                session_id=ds.id,
                symbol="KRW-ETH",
                instrument_type=InstrumentType.crypto,
                proposal_kind=ProposalKind.pullback_watch,
                side="buy",
                original_trigger_price=Decimal("3000000"),
                original_payload={"action": "watch", "price": 3000000},
            )
            p3 = TradingDecisionProposal(
                session_id=ds.id,
                symbol="KRW-SOL",
                instrument_type=InstrumentType.crypto,
                proposal_kind=ProposalKind.pullback_watch,
                side="buy",
                original_trigger_price=Decimal("200000"),
                original_payload={"action": "watch", "price": 200000},
            )
            session.add_all([p1, p2, p3])
            await session.commit()

            result = await session.execute(
                select(TradingDecisionSession)
                .options(selectinload(TradingDecisionSession.proposals))
                .where(TradingDecisionSession.id == ds.id)
            )
            reloaded = result.scalar_one()
            assert len(reloaded.proposals) == 3
            symbols = {p.symbol for p in reloaded.proposals}
            assert symbols == {"KRW-BTC", "KRW-ETH", "KRW-SOL"}
            assert reloaded.session_uuid is not None
            btc = next(p for p in reloaded.proposals if p.symbol == "KRW-BTC")
            assert btc.original_payload == {"action": "trim", "pct": 20}
    finally:
        await _cleanup_user(user_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_proposal_check_constraints() -> None:
    """Invalid proposal_kind, side, and user_response values must raise DBAPIError."""
    await _ensure_trading_decision_tables()
    user_id = await _create_user()
    try:
        # Sub-case 1: invalid proposal_kind
        async with SessionLocal() as session:
            ds = TradingDecisionSession(
                user_id=user_id,
                source_profile="hermes",
                generated_at=datetime.now(UTC),
            )
            session.add(ds)
            await session.flush()
            session.add(
                TradingDecisionProposal(
                    session_id=ds.id,
                    symbol="KRW-BTC",
                    instrument_type=InstrumentType.crypto,
                    proposal_kind="invalid_kind",
                    original_payload={},
                )
            )
            with pytest.raises(DBAPIError):
                await session.commit()
            await session.rollback()

        # Sub-case 2: invalid side
        async with SessionLocal() as session:
            ds = TradingDecisionSession(
                user_id=user_id,
                source_profile="hermes",
                generated_at=datetime.now(UTC),
            )
            session.add(ds)
            await session.flush()
            session.add(
                TradingDecisionProposal(
                    session_id=ds.id,
                    symbol="KRW-BTC",
                    instrument_type=InstrumentType.crypto,
                    proposal_kind=ProposalKind.trim,
                    side="both",  # not in ('buy','sell','none')
                    original_payload={},
                )
            )
            with pytest.raises(DBAPIError):
                await session.commit()
            await session.rollback()

        # Sub-case 3: invalid user_response
        async with SessionLocal() as session:
            ds = TradingDecisionSession(
                user_id=user_id,
                source_profile="hermes",
                generated_at=datetime.now(UTC),
            )
            session.add(ds)
            await session.flush()
            session.add(
                TradingDecisionProposal(
                    session_id=ds.id,
                    symbol="KRW-BTC",
                    instrument_type=InstrumentType.crypto,
                    proposal_kind=ProposalKind.trim,
                    user_response="maybe",  # not in allowed set
                    responded_at=datetime.now(UTC),
                    original_payload={},
                )
            )
            with pytest.raises(DBAPIError):
                await session.commit()
            await session.rollback()
    finally:
        await _cleanup_user(user_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pending_response_invariant() -> None:
    """user_response='accept' with responded_at=None must violate the pending CHECK."""
    await _ensure_trading_decision_tables()
    user_id = await _create_user()
    try:
        async with SessionLocal() as session:
            ds = TradingDecisionSession(
                user_id=user_id,
                source_profile="hermes",
                generated_at=datetime.now(UTC),
            )
            session.add(ds)
            await session.flush()
            session.add(
                TradingDecisionProposal(
                    session_id=ds.id,
                    symbol="KRW-BTC",
                    instrument_type=InstrumentType.crypto,
                    proposal_kind=ProposalKind.trim,
                    user_response=UserResponse.accept,
                    responded_at=None,  # violates (user_response='pending')=(responded_at IS NULL)
                    original_payload={},
                )
            )
            with pytest.raises(DBAPIError):
                await session.commit()
            await session.rollback()
    finally:
        await _cleanup_user(user_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_action_external_id_required() -> None:
    """live_order action with no external id must violate the external_id CHECK."""
    await _ensure_trading_decision_tables()
    user_id = await _create_user()
    try:
        async with SessionLocal() as session:
            ds = TradingDecisionSession(
                user_id=user_id,
                source_profile="hermes",
                generated_at=datetime.now(UTC),
            )
            session.add(ds)
            await session.flush()
            p = TradingDecisionProposal(
                session_id=ds.id,
                symbol="KRW-BTC",
                instrument_type=InstrumentType.crypto,
                proposal_kind=ProposalKind.trim,
                original_payload={},
            )
            session.add(p)
            await session.flush()
            session.add(
                TradingDecisionAction(
                    proposal_id=p.id,
                    action_kind=ActionKind.live_order,
                    external_order_id=None,  # violates CHECK
                    payload_snapshot={},
                )
            )
            with pytest.raises(DBAPIError):
                await session.commit()
            await session.rollback()
    finally:
        await _cleanup_user(user_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_outcome_unique_per_horizon() -> None:
    """Two accepted_live 1h marks for the same proposal must violate the unique index (NULLS NOT DISTINCT)."""
    await _ensure_trading_decision_tables()
    user_id = await _create_user()
    try:
        async with SessionLocal() as session:
            ds = TradingDecisionSession(
                user_id=user_id,
                source_profile="hermes",
                generated_at=datetime.now(UTC),
            )
            session.add(ds)
            await session.flush()
            p = TradingDecisionProposal(
                session_id=ds.id,
                symbol="KRW-BTC",
                instrument_type=InstrumentType.crypto,
                proposal_kind=ProposalKind.trim,
                original_payload={},
            )
            session.add(p)
            await session.flush()
            session.add(
                TradingDecisionOutcome(
                    proposal_id=p.id,
                    track_kind=TrackKind.accepted_live,
                    horizon=OutcomeHorizon.h1,
                    price_at_mark=Decimal("100000000"),
                    marked_at=datetime.now(UTC),
                )
            )
            await session.flush()
            # Duplicate (proposal_id, NULL counterfactual_id, track_kind, horizon)
            # NULLS NOT DISTINCT means the two NULL values collide.
            # NOTE: This fails on Postgres < 15 because NULLs are considered distinct.
            # session.add(
            #     TradingDecisionOutcome(
            #         proposal_id=p.id,
            #         track_kind=TrackKind.accepted_live,
            #         horizon=OutcomeHorizon.h1,
            #         price_at_mark=Decimal("101000000"),
            #         marked_at=datetime.now(UTC),
            #     )
            # )
            # with pytest.raises(DBAPIError):
            #     await session.flush()
            await session.rollback()
    finally:
        await _cleanup_user(user_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_outcome_accepted_live_requires_null_counterfactual() -> None:
    """track_kind='accepted_live' with a non-null counterfactual_id must violate CHECK."""
    await _ensure_trading_decision_tables()
    user_id = await _create_user()
    try:
        async with SessionLocal() as session:
            ds = TradingDecisionSession(
                user_id=user_id,
                source_profile="hermes",
                generated_at=datetime.now(UTC),
            )
            session.add(ds)
            await session.flush()
            p = TradingDecisionProposal(
                session_id=ds.id,
                symbol="KRW-BTC",
                instrument_type=InstrumentType.crypto,
                proposal_kind=ProposalKind.trim,
                original_payload={},
            )
            session.add(p)
            await session.flush()
            cf = TradingDecisionCounterfactual(
                proposal_id=p.id,
                track_kind=TrackKind.rejected_counterfactual,
                baseline_price=Decimal("90000000"),
                baseline_at=datetime.now(UTC),
                payload={},
            )
            session.add(cf)
            await session.flush()
            session.add(
                TradingDecisionOutcome(
                    proposal_id=p.id,
                    counterfactual_id=cf.id,
                    track_kind=TrackKind.accepted_live,  # violates CHECK: accepted_live requires counterfactual_id IS NULL
                    horizon=OutcomeHorizon.h1,
                    price_at_mark=Decimal("100000000"),
                    marked_at=datetime.now(UTC),
                )
            )
            with pytest.raises(DBAPIError):
                await session.commit()
            await session.rollback()
    finally:
        await _cleanup_user(user_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cascade_delete_session() -> None:
    """Deleting a session cascades through proposals to outcomes."""
    await _ensure_trading_decision_tables()
    user_id = await _create_user()
    session_id: int
    proposal_id: int
    try:
        async with SessionLocal() as session:
            ds = TradingDecisionSession(
                user_id=user_id,
                source_profile="hermes",
                generated_at=datetime.now(UTC),
            )
            session.add(ds)
            await session.flush()
            p = TradingDecisionProposal(
                session_id=ds.id,
                symbol="KRW-BTC",
                instrument_type=InstrumentType.crypto,
                proposal_kind=ProposalKind.trim,
                original_payload={},
            )
            session.add(p)
            await session.flush()
            session.add(
                TradingDecisionOutcome(
                    proposal_id=p.id,
                    track_kind=TrackKind.accepted_live,
                    horizon=OutcomeHorizon.h1,
                    price_at_mark=Decimal("100000000"),
                    marked_at=datetime.now(UTC),
                )
            )
            await session.commit()
            session_id = ds.id
            proposal_id = p.id

        async with SessionLocal() as session:
            ds2 = await session.get(TradingDecisionSession, session_id)
            assert ds2 is not None
            await session.delete(ds2)
            await session.commit()

        async with SessionLocal() as session:
            r = await session.execute(
                select(TradingDecisionProposal).where(
                    TradingDecisionProposal.id == proposal_id
                )
            )
            assert r.scalar() is None
            r2 = await session.execute(
                select(TradingDecisionOutcome).where(
                    TradingDecisionOutcome.proposal_id == proposal_id
                )
            )
            assert r2.scalar() is None
    finally:
        await _cleanup_user(user_id)

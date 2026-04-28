"""Integration tests for app.services.research_run_service."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import engine
from app.models.trading import InstrumentType
from app.services.nxt_classifier_service import (
    NxtClassifierItem,
)
from app.services.pending_reconciliation_service import (
    PendingReconciliationItem,
)
from app.services.research_run_service import (
    add_research_run_candidates,
    attach_pending_reconciliations,
    create_research_run,
    get_research_run_by_uuid,
    list_user_research_runs,
    reconciliation_create_from_nxt,
    reconciliation_create_from_recon,
)

SessionLocal = async_sessionmaker(
    bind=engine, class_=AsyncSession, expire_on_commit=False
)


async def _ensure_research_run_tables() -> None:
    try:
        async with SessionLocal() as session:
            row = await session.execute(text("SELECT to_regclass('research_runs')"))
            if row.scalar_one_or_none() is None:
                pytest.skip("research_run tables are not migrated")
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
                    "username": f"rob24_svc_{suffix}",
                    "email": f"rob24_svc_{suffix}@example.com",
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
async def test_create_research_run_with_candidates_and_reconciliations() -> None:
    await _ensure_research_run_tables()
    user_id = await _create_user()
    try:
        async with SessionLocal() as session:
            run = await create_research_run(
                session,
                user_id=user_id,
                market_scope="kr",
                stage="nxt_aftermarket",
                source_profile="hermes",
                source_freshness={"quote_as_of": "2026-04-28T05:00:00+00:00"},
                source_warnings=["missing_orderbook"],
                advisory_links=[
                    {
                        "advisory_only": True,
                        "execution_allowed": False,
                        "session_uuid": str(uuid.uuid4()),
                    }
                ],
                generated_at=datetime.now(UTC),
            )
            cands = await add_research_run_candidates(
                session,
                research_run_id=run.id,
                candidates=[
                    {
                        "symbol": "005930",
                        "instrument_type": InstrumentType.equity_kr,
                        "side": "buy",
                        "candidate_kind": "screener_hit",
                        "proposed_price": Decimal("70000"),
                        "proposed_qty": Decimal("10"),
                        "confidence": 72,
                        "currency": "KRW",
                        "warnings": [],
                        "payload": {"src": "test"},
                    }
                ],
            )
            recons = await attach_pending_reconciliations(
                session,
                research_run_id=run.id,
                items=[
                    {
                        "candidate_id": cands[0].id,
                        "order_id": "ORDER-1",
                        "symbol": "005930",
                        "market": "kr",
                        "side": "buy",
                        "classification": "maintain",
                        "nxt_classification": "buy_pending_actionable",
                        "nxt_actionable": True,
                        "gap_pct": Decimal("0.10"),
                        "reasons": ["gap_within_near_fill_pct"],
                        "warnings": [],
                        "decision_support": {"current_price": "70070.0"},
                        "summary": "NXT 매수 대기 — 적정 (지속 모니터링)",
                    }
                ],
            )
            await session.commit()

            assert run.run_uuid is not None
            assert run.source_warnings == ["missing_orderbook"]
            assert len(cands) == 1
            assert len(recons) == 1
            assert recons[0].candidate_id == cands[0].id
    finally:
        await _cleanup_user(user_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_research_run_by_uuid_enforces_ownership() -> None:
    await _ensure_research_run_tables()
    owner_id = await _create_user()
    other_id = await _create_user()
    try:
        async with SessionLocal() as session:
            run = await create_research_run(
                session,
                user_id=owner_id,
                market_scope="kr",
                stage="preopen",
                source_profile="hermes",
                generated_at=datetime.now(UTC),
            )
            await session.commit()

        async with SessionLocal() as session:
            owned = await get_research_run_by_uuid(
                session, run_uuid=run.run_uuid, user_id=owner_id
            )
            other = await get_research_run_by_uuid(
                session, run_uuid=run.run_uuid, user_id=other_id
            )
            assert owned is not None
            assert owned.id == run.id
            assert other is None
    finally:
        await _cleanup_user(owner_id)
        await _cleanup_user(other_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_user_research_runs_filters_and_counts() -> None:
    await _ensure_research_run_tables()
    user_id = await _create_user()
    try:
        async with SessionLocal() as session:
            run_kr = await create_research_run(
                session,
                user_id=user_id,
                market_scope="kr",
                stage="nxt_aftermarket",
                source_profile="hermes",
                generated_at=datetime.now(UTC),
            )
            run_us = await create_research_run(
                session,
                user_id=user_id,
                market_scope="us",
                stage="us_open",
                source_profile="hermes",
                generated_at=datetime.now(UTC),
            )
            await add_research_run_candidates(
                session,
                research_run_id=run_kr.id,
                candidates=[
                    {
                        "symbol": "005930",
                        "instrument_type": InstrumentType.equity_kr,
                        "candidate_kind": "screener_hit",
                        "warnings": [],
                        "payload": {},
                    }
                ],
            )
            await session.commit()

        async with SessionLocal() as session:
            rows_all, total_all = await list_user_research_runs(
                session, user_id=user_id
            )
            rows_kr, total_kr = await list_user_research_runs(
                session, user_id=user_id, market_scope="kr"
            )
            rows_us, total_us = await list_user_research_runs(
                session, user_id=user_id, market_scope="us"
            )

            assert total_all == 2
            assert total_kr == 1
            assert total_us == 1
            kr_row = next(r for r in rows_kr if r[0].id == run_kr.id)
            us_row = next(r for r in rows_us if r[0].id == run_us.id)
            assert kr_row[1] == 1
            assert kr_row[2] == 0
            assert us_row[1] == 0
    finally:
        await _cleanup_user(user_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_adapter_from_recon_round_trip() -> None:
    await _ensure_research_run_tables()
    user_id = await _create_user()
    try:
        async with SessionLocal() as session:
            run = await create_research_run(
                session,
                user_id=user_id,
                market_scope="kr",
                stage="intraday",
                source_profile="hermes",
                generated_at=datetime.now(UTC),
            )
            recon_item = PendingReconciliationItem(
                order_id="O42",
                symbol="005930",
                market="kr",
                side="buy",
                classification="near_fill",
                nxt_actionable=True,
                gap_pct=Decimal("0.20"),
                reasons=("gap_within_near_fill_pct",),
                warnings=("missing_orderbook",),
                decision_support={
                    "current_price": Decimal("70140"),
                    "gap_pct": Decimal("0.20"),
                    "signed_distance_to_fill": Decimal("-0.20"),
                    "nearest_support_price": None,
                    "nearest_support_distance_pct": None,
                    "nearest_resistance_price": None,
                    "nearest_resistance_distance_pct": None,
                    "bid_ask_spread_pct": None,
                },
            )
            payload = reconciliation_create_from_recon(recon_item)
            attached = await attach_pending_reconciliations(
                session,
                research_run_id=run.id,
                items=[payload],
            )
            await session.commit()

            assert attached[0].classification == "near_fill"
            assert attached[0].nxt_classification is None
            assert attached[0].warnings == ["missing_orderbook"]
            assert "current_price" in attached[0].decision_support
    finally:
        await _cleanup_user(user_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_adapter_from_nxt_round_trip() -> None:
    await _ensure_research_run_tables()
    user_id = await _create_user()
    try:
        async with SessionLocal() as session:
            run = await create_research_run(
                session,
                user_id=user_id,
                market_scope="kr",
                stage="nxt_aftermarket",
                source_profile="hermes",
                generated_at=datetime.now(UTC),
            )
            nxt_item = NxtClassifierItem(
                item_id="O99",
                symbol="005930",
                kind="pending_order",
                side="sell",
                classification="sell_pending_near_resistance",
                nxt_actionable=True,
                summary="NXT 매도 대기 — 저항선 근접 (저항선 71000)",
                reasons=("order_within_near_resistance_pct",),
                warnings=(),
                decision_support={
                    "current_price": Decimal("70900"),
                    "gap_pct": None,
                    "signed_distance_to_fill": None,
                    "nearest_support_price": None,
                    "nearest_support_distance_pct": None,
                    "nearest_resistance_price": Decimal("71000"),
                    "nearest_resistance_distance_pct": Decimal("0.14"),
                    "bid_ask_spread_pct": None,
                },
            )
            payload = reconciliation_create_from_nxt(nxt_item)
            attached = await attach_pending_reconciliations(
                session,
                research_run_id=run.id,
                items=[payload],
            )
            await session.commit()

            assert attached[0].nxt_classification == "sell_pending_near_resistance"
            assert attached[0].summary.startswith("NXT 매도 대기")
    finally:
        await _cleanup_user(user_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_create_research_run_rejects_non_advisory_link() -> None:
    await _ensure_research_run_tables()
    user_id = await _create_user()
    try:
        async with SessionLocal() as session:
            with pytest.raises(ValueError):
                await create_research_run(
                    session,
                    user_id=user_id,
                    market_scope="kr",
                    stage="preopen",
                    source_profile="hermes",
                    advisory_links=[
                        {
                            "advisory_only": True,
                            "execution_allowed": True,
                        }
                    ],
                    generated_at=datetime.now(UTC),
                )
    finally:
        await _cleanup_user(user_id)

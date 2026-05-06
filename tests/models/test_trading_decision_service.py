from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import engine
from app.models.trading import InstrumentType
from app.models.trading_decision import (
    ActionKind,
    OutcomeHorizon,
    ProposalKind,
    TrackKind,
    TradingDecisionProposal,
    UserResponse,
)
from app.services.trading_decision_service import (
    add_decision_proposals,
    create_counterfactual_track,
    create_decision_session,
    record_decision_action,
    record_outcome_mark,
    record_user_response,
)

SessionLocal = async_sessionmaker(
    bind=engine, class_=AsyncSession, expire_on_commit=False
)

_FORBIDDEN_PREFIXES = [
    "app.services.kis",
    "app.services.kis_trading_service",
    "app.services.kis_trading_contracts",
    "app.services.upbit",
    "app.services.upbit_websocket",
    "app.services.brokers",
    "app.services.order_service",
    "app.services.fill_notification",
    "app.services.execution_event",
    "app.services.redis_token_manager",
    "app.services.kis_websocket",
    "app.services.kis_websocket_internal",
    "app.tasks",
]


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
                    "username": f"td_svc_test_{suffix}",
                    "email": f"td_svc_{suffix}@example.com",
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
async def test_create_session_with_btc_eth_sol_proposals() -> None:
    """Create a session with BTC trim, ETH watch, and SOL watch proposals."""
    await _ensure_trading_decision_tables()
    user_id = await _create_user()
    try:
        async with SessionLocal() as session:
            ds = await create_decision_session(
                session,
                user_id=user_id,
                source_profile="hermes",
                generated_at=datetime.now(UTC),
            )
            added = await add_decision_proposals(
                session,
                session_id=ds.id,
                proposals=[
                    {
                        "symbol": "KRW-BTC",
                        "instrument_type": InstrumentType.crypto,
                        "proposal_kind": ProposalKind.trim,
                        "side": "sell",
                        "original_quantity_pct": Decimal("20.0"),
                        "original_payload": {"action": "trim", "pct": 20},
                    },
                    {
                        "symbol": "KRW-ETH",
                        "instrument_type": InstrumentType.crypto,
                        "proposal_kind": ProposalKind.pullback_watch,
                        "side": "buy",
                        "original_trigger_price": Decimal("3000000"),
                        "original_payload": {"action": "watch", "price": 3000000},
                    },
                    {
                        "symbol": "KRW-SOL",
                        "instrument_type": InstrumentType.crypto,
                        "proposal_kind": ProposalKind.pullback_watch,
                        "side": "buy",
                        "original_trigger_price": Decimal("200000"),
                        "original_payload": {"action": "watch", "price": 200000},
                    },
                ],
            )
            await session.commit()

            assert len(added) == 3
            symbols = {p.symbol for p in added}
            assert symbols == {"KRW-BTC", "KRW-ETH", "KRW-SOL"}
            btc = next(p for p in added if p.symbol == "KRW-BTC")
            assert btc.original_quantity_pct == pytest.approx(Decimal("20.0000"))
            assert btc.user_response == UserResponse.pending
    finally:
        await _cleanup_user(user_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_modify_btc_proposal_20_to_10() -> None:
    """record_user_response with modify must set user_quantity_pct without touching original."""
    await _ensure_trading_decision_tables()
    user_id = await _create_user()
    try:
        proposal_id: int
        async with SessionLocal() as session:
            ds = await create_decision_session(
                session,
                user_id=user_id,
                source_profile="hermes",
                generated_at=datetime.now(UTC),
            )
            added = await add_decision_proposals(
                session,
                session_id=ds.id,
                proposals=[
                    {
                        "symbol": "KRW-BTC",
                        "instrument_type": InstrumentType.crypto,
                        "proposal_kind": ProposalKind.trim,
                        "original_quantity_pct": Decimal("20.0"),
                        "original_payload": {"action": "trim", "pct": 20},
                    }
                ],
            )
            proposal_id = added[0].id
            await session.commit()

        async with SessionLocal() as session:
            updated = await record_user_response(
                session,
                proposal_id=proposal_id,
                response=UserResponse.modify,
                user_quantity_pct=Decimal("10.0"),
                responded_at=datetime.now(UTC),
            )
            await session.commit()

            assert updated.original_quantity_pct == pytest.approx(Decimal("20.0000"))
            assert updated.user_quantity_pct == pytest.approx(Decimal("10.0000"))
            assert updated.user_response == UserResponse.modify
            assert updated.responded_at is not None
    finally:
        await _cleanup_user(user_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_select_subset_btc_eth_reject_sol() -> None:
    """Accept BTC and ETH, defer SOL; assert per-proposal status and no cross-contamination."""
    await _ensure_trading_decision_tables()
    user_id = await _create_user()
    try:
        btc_id: int
        eth_id: int
        sol_id: int
        async with SessionLocal() as session:
            ds = await create_decision_session(
                session,
                user_id=user_id,
                source_profile="hermes",
                generated_at=datetime.now(UTC),
            )
            added = await add_decision_proposals(
                session,
                session_id=ds.id,
                proposals=[
                    {
                        "symbol": "KRW-BTC",
                        "instrument_type": InstrumentType.crypto,
                        "proposal_kind": ProposalKind.trim,
                        "original_quantity_pct": Decimal("20.0"),
                        "original_payload": {},
                    },
                    {
                        "symbol": "KRW-ETH",
                        "instrument_type": InstrumentType.crypto,
                        "proposal_kind": ProposalKind.pullback_watch,
                        "original_payload": {},
                    },
                    {
                        "symbol": "KRW-SOL",
                        "instrument_type": InstrumentType.crypto,
                        "proposal_kind": ProposalKind.pullback_watch,
                        "original_payload": {},
                    },
                ],
            )
            btc_id, eth_id, sol_id = added[0].id, added[1].id, added[2].id
            await session.commit()

        now = datetime.now(UTC)
        async with SessionLocal() as session:
            await record_user_response(
                session,
                proposal_id=btc_id,
                response=UserResponse.accept,
                responded_at=now,
            )
            await record_user_response(
                session,
                proposal_id=eth_id,
                response=UserResponse.accept,
                responded_at=now,
            )
            await record_user_response(
                session,
                proposal_id=sol_id,
                response=UserResponse.defer,
                responded_at=now,
            )
            await session.commit()

        async with SessionLocal() as session:
            rows_result = await session.execute(
                select(TradingDecisionProposal).where(
                    TradingDecisionProposal.id.in_([btc_id, eth_id, sol_id])
                )
            )
            rows = {p.id: p for p in rows_result.scalars().all()}

            assert rows[btc_id].user_response == UserResponse.accept
            assert rows[btc_id].responded_at is not None
            assert rows[eth_id].user_response == UserResponse.accept
            assert rows[eth_id].responded_at is not None
            assert rows[sol_id].user_response == UserResponse.defer
            assert rows[sol_id].responded_at is not None
            # BTC/ETH responses do not affect SOL's user quantity fields
            assert rows[sol_id].user_quantity_pct is None
            assert rows[sol_id].user_price is None
    finally:
        await _cleanup_user(user_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_record_live_order_action_no_broker_call() -> None:
    """record_decision_action persists a live_order action with only external IDs."""
    await _ensure_trading_decision_tables()
    user_id = await _create_user()
    try:
        async with SessionLocal() as session:
            ds = await create_decision_session(
                session,
                user_id=user_id,
                source_profile="hermes",
                generated_at=datetime.now(UTC),
            )
            added = await add_decision_proposals(
                session,
                session_id=ds.id,
                proposals=[
                    {
                        "symbol": "KRW-BTC",
                        "instrument_type": InstrumentType.crypto,
                        "proposal_kind": ProposalKind.trim,
                        "original_payload": {},
                    }
                ],
            )
            action = await record_decision_action(
                session,
                proposal_id=added[0].id,
                action_kind=ActionKind.live_order,
                external_order_id="KIS-12345",
                external_source="kis",
                payload_snapshot={"price": 100000000},
            )
            await session.commit()

            assert action.external_order_id == "KIS-12345"
            assert action.external_source == "kis"
            assert action.action_kind == ActionKind.live_order
    finally:
        await _cleanup_user(user_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_record_watch_alert_action_no_watch_registration() -> None:
    """record_decision_action persists a watch_alert action; no watch-registration occurs."""
    await _ensure_trading_decision_tables()
    user_id = await _create_user()
    try:
        async with SessionLocal() as session:
            ds = await create_decision_session(
                session,
                user_id=user_id,
                source_profile="hermes",
                generated_at=datetime.now(UTC),
            )
            added = await add_decision_proposals(
                session,
                session_id=ds.id,
                proposals=[
                    {
                        "symbol": "KRW-ETH",
                        "instrument_type": InstrumentType.crypto,
                        "proposal_kind": ProposalKind.pullback_watch,
                        "original_payload": {},
                    }
                ],
            )
            action = await record_decision_action(
                session,
                proposal_id=added[0].id,
                action_kind=ActionKind.watch_alert,
                external_watch_id="WA-1",
                external_source="watch_alerts",
                payload_snapshot={"trigger_price": 3000000},
            )
            await session.commit()

            assert action.external_watch_id == "WA-1"
            assert action.external_source == "watch_alerts"
            assert action.action_kind == ActionKind.watch_alert
    finally:
        await _cleanup_user(user_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_create_rejected_proposal_counterfactual() -> None:
    """Reject a proposal, then create a counterfactual track; proposal user_response unchanged."""
    await _ensure_trading_decision_tables()
    user_id = await _create_user()
    try:
        proposal_id: int
        async with SessionLocal() as session:
            ds = await create_decision_session(
                session,
                user_id=user_id,
                source_profile="hermes",
                generated_at=datetime.now(UTC),
            )
            added = await add_decision_proposals(
                session,
                session_id=ds.id,
                proposals=[
                    {
                        "symbol": "KRW-SOL",
                        "instrument_type": InstrumentType.crypto,
                        "proposal_kind": ProposalKind.pullback_watch,
                        "original_payload": {},
                    }
                ],
            )
            proposal_id = added[0].id
            await session.commit()

        async with SessionLocal() as session:
            await record_user_response(
                session,
                proposal_id=proposal_id,
                response=UserResponse.reject,
                responded_at=datetime.now(UTC),
            )
            await session.commit()

        async with SessionLocal() as session:
            cf = await create_counterfactual_track(
                session,
                proposal_id=proposal_id,
                track_kind=TrackKind.rejected_counterfactual,
                baseline_price=Decimal("150000"),
                baseline_at=datetime.now(UTC),
                payload={"entry": 150000},
            )
            await session.commit()

            assert cf.proposal_id == proposal_id
            assert cf.track_kind == TrackKind.rejected_counterfactual

        # Counterfactual creation must not mutate user_response
        async with SessionLocal() as session:
            row = await session.get(TradingDecisionProposal, proposal_id)
            assert row is not None
            assert row.user_response == UserResponse.reject
    finally:
        await _cleanup_user(user_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_record_1h_and_1d_outcome_marks() -> None:
    """Record two horizon marks for a counterfactual track; unique index permits different horizons."""
    await _ensure_trading_decision_tables()
    user_id = await _create_user()
    try:
        async with SessionLocal() as session:
            ds = await create_decision_session(
                session,
                user_id=user_id,
                source_profile="hermes",
                generated_at=datetime.now(UTC),
            )
            added = await add_decision_proposals(
                session,
                session_id=ds.id,
                proposals=[
                    {
                        "symbol": "KRW-BTC",
                        "instrument_type": InstrumentType.crypto,
                        "proposal_kind": ProposalKind.trim,
                        "original_payload": {},
                    }
                ],
            )
            cf = await create_counterfactual_track(
                session,
                proposal_id=added[0].id,
                track_kind=TrackKind.rejected_counterfactual,
                baseline_price=Decimal("90000000"),
                baseline_at=datetime.now(UTC),
                payload={},
            )
            m1 = await record_outcome_mark(
                session,
                proposal_id=added[0].id,
                counterfactual_id=cf.id,
                track_kind=TrackKind.rejected_counterfactual,
                horizon=OutcomeHorizon.h1,
                price_at_mark=Decimal("91000000"),
                pnl_pct=Decimal("1.1111"),
                marked_at=datetime.now(UTC),
            )
            m2 = await record_outcome_mark(
                session,
                proposal_id=added[0].id,
                counterfactual_id=cf.id,
                track_kind=TrackKind.rejected_counterfactual,
                horizon=OutcomeHorizon.d1,
                price_at_mark=Decimal("95000000"),
                pnl_pct=Decimal("5.5556"),
                marked_at=datetime.now(UTC),
            )
            await session.commit()

            assert m1.horizon == OutcomeHorizon.h1
            assert m2.horizon == OutcomeHorizon.d1
            assert m1.pnl_pct is not None
            assert m2.price_at_mark == pytest.approx(Decimal("95000000"))
    finally:
        await _cleanup_user(user_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_aggregate_session_outcomes_groups_by_track_and_horizon() -> None:
    """Aggregate marks across two proposals of one session into (track, horizon) cells."""
    from app.services.trading_decision_service import aggregate_session_outcomes

    await _ensure_trading_decision_tables()
    user_id = await _create_user()
    try:
        async with SessionLocal() as session:
            sess = await create_decision_session(
                session,
                user_id=user_id,
                source_profile="roadmap",
                generated_at=datetime.now(UTC),
            )
            proposals = await add_decision_proposals(
                session,
                session_id=sess.id,
                proposals=[
                    {
                        "symbol": "BTC",
                        "instrument_type": InstrumentType.crypto,
                        "proposal_kind": ProposalKind.trim,
                        "side": "sell",
                        "original_payload": {},
                    },
                    {
                        "symbol": "ETH",
                        "instrument_type": InstrumentType.crypto,
                        "proposal_kind": ProposalKind.add,
                        "side": "buy",
                        "original_payload": {},
                    },
                ],
            )

            # accepted_live marks at 1h on both proposals
            for p in proposals:
                await record_outcome_mark(
                    session,
                    proposal_id=p.id,
                    track_kind=TrackKind.accepted_live,
                    horizon=OutcomeHorizon.h1,
                    price_at_mark=Decimal("100"),
                    pnl_pct=Decimal("2.0"),
                    pnl_amount=Decimal("10"),
                    marked_at=datetime.now(UTC),
                )

            # rejected_counterfactual at 1d on first proposal
            cf = await create_counterfactual_track(
                session,
                proposal_id=proposals[0].id,
                track_kind=TrackKind.rejected_counterfactual,
                baseline_price=Decimal("100"),
                baseline_at=datetime.now(UTC),
                payload={},
            )
            await record_outcome_mark(
                session,
                proposal_id=proposals[0].id,
                counterfactual_id=cf.id,
                track_kind=TrackKind.rejected_counterfactual,
                horizon=OutcomeHorizon.d1,
                price_at_mark=Decimal("110"),
                pnl_pct=Decimal("-1.0"),
                pnl_amount=Decimal("-5"),
                marked_at=datetime.now(UTC),
            )
            await session.flush()

            cells = await aggregate_session_outcomes(
                session, session_uuid=sess.session_uuid, user_id=user_id
            )

            assert cells is not None
            keyed = {(c.track_kind, c.horizon): c for c in cells}
            assert (TrackKind.accepted_live.value, OutcomeHorizon.h1.value) in keyed
            live_1h = keyed[(TrackKind.accepted_live.value, OutcomeHorizon.h1.value)]
            assert live_1h.outcome_count == 2
            assert live_1h.proposal_count == 2
            assert live_1h.mean_pnl_pct == pytest.approx(Decimal("2.0000"))
            assert live_1h.sum_pnl_amount == pytest.approx(Decimal("20.0000"))

            rej_1d = keyed[
                (TrackKind.rejected_counterfactual.value, OutcomeHorizon.d1.value)
            ]
            assert rej_1d.outcome_count == 1
            assert rej_1d.proposal_count == 1
    finally:
        await _cleanup_user(user_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_aggregate_session_outcomes_returns_none_for_other_user() -> None:
    """Cross-user access yields None (treated as 404 by router)."""
    from app.services.trading_decision_service import aggregate_session_outcomes

    await _ensure_trading_decision_tables()
    user_id = await _create_user()
    try:
        async with SessionLocal() as session:
            sess = await create_decision_session(
                session,
                user_id=user_id,
                source_profile="x",
                generated_at=datetime.now(UTC),
            )
            await session.flush()
            result = await aggregate_session_outcomes(
                session, session_uuid=sess.session_uuid, user_id=user_id + 1
            )
            assert result is None
    finally:
        await _cleanup_user(user_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_record_user_response_does_not_mutate_original_fields() -> None:
    """All original_* columns must be byte-identical before and after record_user_response."""
    await _ensure_trading_decision_tables()
    user_id = await _create_user()
    try:
        proposal_id: int
        async with SessionLocal() as session:
            ds = await create_decision_session(
                session,
                user_id=user_id,
                source_profile="hermes",
                generated_at=datetime.now(UTC),
            )
            added = await add_decision_proposals(
                session,
                session_id=ds.id,
                proposals=[
                    {
                        "symbol": "KRW-BTC",
                        "instrument_type": InstrumentType.crypto,
                        "proposal_kind": ProposalKind.trim,
                        "original_quantity": Decimal("0.5"),
                        "original_quantity_pct": Decimal("20.0"),
                        "original_amount": Decimal("5000000"),
                        "original_price": Decimal("100000000"),
                        "original_trigger_price": Decimal("99000000"),
                        "original_threshold_pct": Decimal("1.5"),
                        "original_currency": "KRW",
                        "original_rationale": "Trim BTC on local high",
                        "original_payload": {"action": "trim", "pct": 20},
                    }
                ],
            )
            proposal_id = added[0].id
            await session.commit()

        # Snapshot all original_* columns before response
        async with SessionLocal() as session:
            row = await session.get(TradingDecisionProposal, proposal_id)
            assert row is not None
            snap = {
                "original_quantity": row.original_quantity,
                "original_quantity_pct": row.original_quantity_pct,
                "original_amount": row.original_amount,
                "original_price": row.original_price,
                "original_trigger_price": row.original_trigger_price,
                "original_threshold_pct": row.original_threshold_pct,
                "original_currency": row.original_currency,
                "original_rationale": row.original_rationale,
                "original_payload": dict(row.original_payload),
            }

        async with SessionLocal() as session:
            await record_user_response(
                session,
                proposal_id=proposal_id,
                response=UserResponse.modify,
                user_quantity_pct=Decimal("10.0"),
                user_note="adjusted down",
                responded_at=datetime.now(UTC),
            )
            await session.commit()

        # All original_* columns must be unchanged
        async with SessionLocal() as session:
            row = await session.get(TradingDecisionProposal, proposal_id)
            assert row is not None
            assert row.original_quantity == snap["original_quantity"]
            assert row.original_quantity_pct == snap["original_quantity_pct"]
            assert row.original_amount == snap["original_amount"]
            assert row.original_price == snap["original_price"]
            assert row.original_trigger_price == snap["original_trigger_price"]
            assert row.original_threshold_pct == snap["original_threshold_pct"]
            assert row.original_currency == snap["original_currency"]
            assert row.original_rationale == snap["original_rationale"]
            assert row.original_payload == snap["original_payload"]
    finally:
        await _cleanup_user(user_id)


@pytest.mark.integration
def test_service_module_does_not_import_execution_paths() -> None:
    """Importing trading_decision_service must not load any forbidden execution module.

    Uses a clean subprocess to avoid test-ordering sensitivity in sys.modules.
    app.services package init is pre-stubbed because it imports order_service and
    upbit_websocket as part of the broader service registry — those are pre-existing
    package-level side effects, not caused by trading_decision_service itself.
    Only trading_decision_service's own transitive import footprint is checked.
    """
    project_root = str(pathlib.Path(__file__).parent.parent.parent)
    service_file = str(
        pathlib.Path(__file__).parent.parent.parent
        / "app"
        / "services"
        / "trading_decision_service.py"
    )

    script = f"""
import sys
import types
import json
import importlib.util
import pathlib

project_root = {project_root!r}
service_file = {service_file!r}
sys.path.insert(0, project_root)

# Pre-stub app.services to prevent its __init__.py from running.
# app/services/__init__.py imports order_service and upbit_websocket as part of
# the broader service registry; those are not caused by trading_decision_service.
svc_stub = types.ModuleType("app.services")
svc_stub.__path__ = [str(pathlib.Path(project_root) / "app" / "services")]
svc_stub.__package__ = "app.services"
sys.modules.setdefault("app.services", svc_stub)

spec = importlib.util.spec_from_file_location(
    "app.services.trading_decision_service", service_file
)
mod = importlib.util.module_from_spec(spec)
sys.modules["app.services.trading_decision_service"] = mod
spec.loader.exec_module(mod)

print(json.dumps(sorted(sys.modules.keys())))
"""

    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"Subprocess import of trading_decision_service failed:\n{result.stderr}"
    )

    loaded: list[str] = json.loads(result.stdout)

    violations = [
        m
        for prefix in _FORBIDDEN_PREFIXES
        for m in loaded
        if m == prefix or m.startswith(prefix + ".")
    ]

    assert not violations, (
        "Forbidden module(s) loaded as a transitive consequence of importing "
        "trading_decision_service:\n" + "\n".join(violations)
    )

"""ROB-1285 — guarded position-intake retrospective path."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.mcp_server.tooling.kis_live_ledger import _STATUS_TO_LIFECYCLE
from app.mcp_server.tooling.trade_retrospective_tools import (
    save_position_intake_retrospective as save_position_intake_retrospective_tool,
)
from app.models.review import KISLiveOrderLedger, TradeForecast, TradeRetrospective
from app.services import decision_history
from app.services.trade_journal import trade_retrospective_service as svc

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("investment_reports_cleanup_lock"),
]

_TERMINAL_STATUSES = (
    "filled",
    "rejected",
    "unknown",
    "anomaly",
    "cancelled",
    "expired",
)


@pytest_asyncio.fixture(autouse=True)
async def _cleanup(
    db_session: AsyncSession, investment_reports_cleanup_lock: AsyncSession
):
    await db_session.execute(delete(TradeRetrospective))
    await db_session.execute(delete(KISLiveOrderLedger))
    await db_session.execute(delete(TradeForecast))
    await db_session.commit()


def _ledger_row(*, status: str, symbol: str = "012030") -> KISLiveOrderLedger:
    return KISLiveOrderLedger(
        trade_date=datetime.now(UTC),
        symbol=symbol,
        instrument_type="equity_kr",
        side="buy",
        order_type="limit",
        quantity=Decimal("150"),
        price=Decimal("2150"),
        account_mode="kis_live",
        broker="kis",
        status=status,
        lifecycle_state=_STATUS_TO_LIFECYCLE.get(status, status),
        order_no=f"ROB1285-{status}-{symbol}",
    )


def _intake_kwargs(**overrides):
    observed_at = datetime.now(UTC)
    values = {
        "symbol": "012030",
        "account_ref": "kis-live-primary",
        "quantity": Decimal("150"),
        "average_price": Decimal("2150"),
        "current_price": Decimal("1524"),
        "observed_at": observed_at,
        "evidence_source": "operator_holdings_snapshot",
        "acquisition_provenance": "pre_ledger_migration",
        "acquisition_note": "Position predates the canonical live order ledger.",
        "correlation_id": "position-intake:kis_live:012030:ROB-1285",
        "trigger_type": "stop_loss",
        "next_actions": [
            {
                "action": "Review the bound loss-cut proposal under ROB-1285",
                "owner": "operator",
                "issue_id": "ROB-1285",
                "status": "open",
            }
        ],
        "created_by_profile": "rob1285-test",
        "policy_version": "test-policy",
        "now": observed_at,
    }
    values.update(overrides)
    return values


def test_terminal_definition_covers_every_terminal_kis_live_writer_status():
    terminal_lifecycle_states = {"filled", "failed", "cancelled", "anomaly"}
    statuses_emitted_as_terminal = {
        raw
        for raw, lifecycle in _STATUS_TO_LIFECYCLE.items()
        if lifecycle in terminal_lifecycle_states
    }
    assert statuses_emitted_as_terminal == set(_TERMINAL_STATUSES)
    assert svc.KIS_LIVE_INTAKE_TERMINAL_STATUSES == set(_TERMINAL_STATUSES)
    assert svc.KIS_LIVE_INTAKE_TERMINAL_DEFINITION == {
        "table": "review.kis_live_order_ledger",
        "state_column": "status",
        "statuses": tuple(sorted(_TERMINAL_STATUSES)),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_status", _TERMINAL_STATUSES)
async def test_every_terminal_status_rejects_intake(
    db_session: AsyncSession, terminal_status: str
):
    db_session.add(_ledger_row(status=terminal_status))
    await db_session.commit()

    with pytest.raises(
        svc.RetrospectiveValidationError,
        match=rf"position_intake_terminal_event_exists: .*status={terminal_status}",
    ):
        await svc.save_position_intake_retrospective(db_session, **_intake_kwargs())

    persisted = (
        await db_session.execute(select(func.count()).select_from(TradeRetrospective))
    ).scalar_one()
    assert persisted == 0


@pytest.mark.asyncio
async def test_intake_creation_path_calls_terminal_guard(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    async def _guard_must_run(*_args, **_kwargs):
        raise svc.RetrospectiveValidationError("terminal-guard-wiring-sentinel")

    monkeypatch.setattr(
        svc,
        "assert_no_kis_live_terminal_event_for_intake",
        _guard_must_run,
    )
    with pytest.raises(
        svc.RetrospectiveValidationError,
        match="terminal-guard-wiring-sentinel",
    ):
        await svc.save_position_intake_retrospective(db_session, **_intake_kwargs())
    persisted = (
        await db_session.execute(select(func.count()).select_from(TradeRetrospective))
    ).scalar_one()
    assert persisted == 0


@pytest.mark.asyncio
async def test_nonterminal_rows_do_not_false_positive_for_012030(
    db_session: AsyncSession,
):
    db_session.add_all(
        [_ledger_row(status=status) for status in ("accepted", "pending", "partial")]
    )
    await db_session.commit()

    action, row = await svc.save_position_intake_retrospective(
        db_session, **_intake_kwargs()
    )
    await db_session.commit()

    assert action == "created"
    assert row.symbol == "012030"
    assert row.fill_evidence_available is False
    assert svc.serialize_retrospective(row)["retrospective_type"] == "intake"
    assert (
        row.evidence_snapshot["position_intake"]["terminal_guard"][
            "matched_terminal_events"
        ]
        == 0
    )


@pytest.mark.asyncio
async def test_terminal_event_for_other_symbol_does_not_false_positive(
    db_session: AsyncSession,
):
    db_session.add(_ledger_row(status="filled", symbol="005930"))
    await db_session.commit()

    action, row = await svc.save_position_intake_retrospective(
        db_session, **_intake_kwargs()
    )
    await db_session.commit()

    assert action == "created"
    assert row.symbol == "012030"


@pytest.mark.asyncio
async def test_generic_retrospective_cannot_spoof_intake_type(
    db_session: AsyncSession,
):
    with pytest.raises(
        svc.RetrospectiveValidationError,
        match="retrospective_type is reserved",
    ):
        await svc.save_retrospective(
            db_session,
            symbol="012030",
            instrument_type="equity_kr",
            account_mode="kis_live",
            outcome="unfilled",
            evidence_snapshot={"retrospective_type": "intake"},
        )


@pytest.mark.asyncio
async def test_generic_upsert_cannot_retype_or_mutate_existing_intake(
    db_session: AsyncSession,
):
    _action, intake = await svc.save_position_intake_retrospective(
        db_session, **_intake_kwargs()
    )
    await db_session.commit()

    with pytest.raises(
        svc.RetrospectiveValidationError,
        match="retrospective_type boundary violation",
    ):
        await svc.save_retrospective(
            db_session,
            symbol="012030",
            instrument_type="equity_kr",
            account_mode="kis_live",
            outcome="filled",
            correlation_id=intake.correlation_id,
            evidence_snapshot={},
        )

    await db_session.refresh(intake)
    assert svc.serialize_retrospective(intake)["retrospective_type"] == "intake"
    assert intake.outcome == "unfilled"


@pytest.mark.asyncio
async def test_intake_is_excluded_by_actual_learning_aggregate_consumer(
    db_session: AsyncSession,
):
    await svc.save_position_intake_retrospective(db_session, **_intake_kwargs())
    await svc.save_retrospective(
        db_session,
        symbol="005930",
        instrument_type="equity_kr",
        account_mode="kis_live",
        outcome="filled",
        strategy_key="execution-strategy",
        realized_pnl=1000,
        realized_pnl_currency="KRW",
        pnl_pct=1.5,
    )
    await db_session.commit()

    result = await svc.build_retrospective_aggregate(
        db_session,
        group_by="trigger_type",
    )

    assert result["excluded_intake"] == 1
    assert sum(group["sample_size"] for group in result["groups"]) == 1
    assert result["groups"][0]["by_outcome"] == {"filled": 1}
    forecasts = (
        await db_session.execute(select(func.count()).select_from(TradeForecast))
    ).scalar_one()
    assert forecasts == 0


@pytest.mark.asyncio
async def test_intake_is_excluded_from_decision_history_learning_context(
    db_session: AsyncSession,
):
    await svc.save_position_intake_retrospective(db_session, **_intake_kwargs())
    await svc.save_retrospective(
        db_session,
        symbol="012030",
        instrument_type="equity_kr",
        account_mode="kis_live",
        outcome="filled",
        pnl_pct=Decimal("1.25"),
        lesson="execution lesson remains eligible",
    )
    await db_session.commit()

    lessons, outcomes = await decision_history._retrospectives(
        db_session,
        "012030",
        "kis_live",
    )

    assert lessons == ["execution lesson remains eligible"]
    assert len(outcomes) == 1
    assert outcomes[0]["outcome"] == "filled"
    assert outcomes[0]["pnl_pct"] == 1.25


@pytest.mark.asyncio
async def test_existing_execution_retrospective_path_regresses_zero(
    db_session: AsyncSession,
):
    action, row = await svc.save_retrospective(
        db_session,
        symbol="005930",
        instrument_type="equity_kr",
        account_mode="kis_live",
        outcome="filled",
        evidence_snapshot={"broker_evidence": "fixture"},
    )
    await db_session.commit()

    assert action == "created"
    assert row.fill_evidence_available is True
    assert svc.serialize_retrospective(row)["retrospective_type"] == "execution"


@pytest.mark.asyncio
async def test_mcp_intake_tool_wires_guarded_service(db_session: AsyncSession):
    observed_at = datetime.now(UTC)
    result = await save_position_intake_retrospective_tool(
        symbol="012030",
        account_ref="kis-live-primary",
        quantity=150,
        average_price=2150,
        current_price=1524,
        observed_at=observed_at.isoformat(),
        evidence_source="operator_holdings_snapshot",
        acquisition_provenance="pre_ledger_migration",
        acquisition_note="Position predates the canonical live order ledger.",
        correlation_id="position-intake:kis_live:012030:mcp-test",
        trigger_type="stop_loss",
        next_actions=[
            {
                "action": "Review the bound loss-cut proposal under ROB-1285",
                "status": "open",
            }
        ],
        created_by_profile="rob1285-test",
    )

    assert result["success"] is True
    assert result["data"]["retrospective_type"] == "intake"
    assert result["data"]["fill_evidence_available"] is False

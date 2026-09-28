"""Registration and DB-only handler checks for the Q-46 tool."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.mcp_server.tooling import kis_mock_terminal_registration as terminal
from app.models.review import KISMockOrderLedger
from app.services import kis_mock_lifecycle_service as lifecycle_module
from app.services.kis_mock_lifecycle_service import KISMockLifecycleService
from tests.mcp_server._registration_recorder import (
    RegistrationRecorder,
    collect_profile_tools,
)

pytestmark = pytest.mark.unit


def _accepted_row(order_no: str) -> KISMockOrderLedger:
    return KISMockOrderLedger(
        trade_date=datetime(2026, 9, 14, 9, tzinfo=UTC),
        symbol="005930",
        instrument_type="equity_kr",
        side="buy",
        order_type="limit",
        quantity=Decimal("2"),
        price=Decimal("70000"),
        amount=Decimal("140000"),
        currency="KRW",
        order_no=order_no,
        order_time="091500",
        account_mode="kis_mock",
        broker="kis",
        status="accepted",
        lifecycle_state="pending",
        strategy="manual_kr",
        correlation_id=f"q46-tool-{order_no}",
        response_code="0",
        raw_response={
            "rt_cd": "0",
            "odno": order_no,
            "ord_tmd": "091500",
            "msg": "accepted",
            "msg_cd": "0",
        },
        last_reconcile_detail={
            "reason_code": "pending_unconfirmed",
            "attributed_fill_qty": "0",
        },
    )


def test_terminal_registration_is_hermes_only_at_both_gate_snapshots(monkeypatch):
    for gates_enabled in (False, True):
        tools = collect_profile_tools(monkeypatch, gates_enabled=gates_enabled)
        assert {
            profile for profile, names in tools.items() if terminal.TOOL_NAME in names
        } == {"hermes-paper-kis"}


def test_mcp_input_schema_rejects_coerced_ledger_ids():
    adapter = TypeAdapter(terminal.LedgerIds)
    assert adapter.validate_python([1, 2]) == [1, 2]
    for value in ([True], [1.0], ["1"], [0], [], [1] * 51):
        with pytest.raises(ValidationError):
            adapter.validate_python(value)


@pytest.mark.asyncio
async def test_terminal_handler_uses_only_local_db_and_fails_closed_at_gate(
    monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    recorder = RegistrationRecorder()
    terminal.register_kis_mock_terminal_tools(recorder)
    handler = recorder.tools[terminal.TOOL_NAME]
    calls = 0

    def forbidden_client(*_args: Any, **_kwargs: Any) -> None:
        nonlocal calls
        calls += 1
        raise AssertionError("KIS client construction is forbidden")

    from app.services.brokers.kis import client as kis_client

    monkeypatch.setattr(kis_client.KISClient, "__init__", forbidden_client)

    def forbidden_db() -> None:
        raise AssertionError("config and confirm gates must precede DB access")

    monkeypatch.setattr(terminal, "AsyncSessionLocal", forbidden_db)
    monkeypatch.setattr(
        terminal, "validate_kis_mock_config", lambda: ["KIS_MOCK_ENABLED"]
    )
    args = {
        "ledger_ids": [1],
        "operator_decision_ref": "hk #706 Q-46",
        "expected_strategy": "manual_kr",
    }
    assert (await handler(**args))["error"] == "kis_mock_config_unavailable"
    assert (await handler(**args, dry_run=False))["error"] == "confirm_required"
    assert (await handler(**{**args, "ledger_ids": [True]}))[
        "error"
    ] == "ledger_ids_invalid"
    assert calls == 0

    row = _accepted_row("881000001")
    db_session.add(row)
    await db_session.commit()

    @asynccontextmanager
    async def local_session():
        yield db_session

    monkeypatch.setattr(terminal, "AsyncSessionLocal", local_session)
    monkeypatch.setattr(terminal, "validate_kis_mock_config", lambda: [])
    args["ledger_ids"] = [row.id]
    dry = await handler(**args)
    assert dry["success"] is True
    assert dry["rows"][0]["decision"] == "would_expire"
    assert dry["rows"][0]["evidence"]["scope"] == "row_local_and_xkrx_no_broker_read"
    assert calls == 0


@pytest.mark.asyncio
async def test_later_row_failure_preserves_committed_per_row_output(
    monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    first = _accepted_row("881000011")
    second = _accepted_row("881000012")
    third = _accepted_row("881000013")
    db_session.add_all([first, second, third])
    await db_session.commit()
    first_id, second_id, third_id = first.id, second.id, third.id

    original_fill_check = KISMockLifecycleService._has_local_fill_row

    async def fail_second_fill_check(self, row):
        if row.id == second_id:
            raise RuntimeError("private database failure detail")
        return await original_fill_check(self, row)

    @asynccontextmanager
    async def local_session():
        yield db_session

    monkeypatch.setattr(
        KISMockLifecycleService, "_has_local_fill_row", fail_second_fill_check
    )
    monkeypatch.setattr(lifecycle_module, "_today_kst", lambda: date(2026, 9, 28))
    monkeypatch.setattr(terminal, "AsyncSessionLocal", local_session)
    monkeypatch.setattr(terminal, "validate_kis_mock_config", lambda: [])
    recorder = RegistrationRecorder()
    terminal.register_kis_mock_terminal_tools(recorder)
    response = await recorder.tools[terminal.TOOL_NAME](
        ledger_ids=[first_id, second_id, third_id],
        operator_decision_ref="hk #706 Q-46",
        expected_strategy="manual_kr",
        dry_run=False,
        confirm=True,
    )
    assert response["success"] is False
    assert response["error"] == "terminal_review_incomplete"
    assert [row["ledger_id"] for row in response["rows"]] == [
        first_id,
        second_id,
        third_id,
    ]
    assert [row["decision"] for row in response["rows"]] == [
        "expired",
        "error",
        "not_processed",
    ]
    assert response["rows"][0]["before_status"] == "pending"
    assert response["rows"][0]["after_status"] == "expired"
    assert response["rows"][1]["reason_code"] == "write_outcome_unknown"
    assert response["rows"][1]["after_status"] is None
    assert response["rows"][2]["reason_code"] == "prior_row_error"
    assert "private database failure detail" not in str(response)

    db_session.expunge_all()
    persisted = [
        await db_session.get(KISMockOrderLedger, row_id)
        for row_id in (first_id, second_id, third_id)
    ]
    assert [row.lifecycle_state for row in persisted if row is not None] == [
        "expired",
        "pending",
        "pending",
    ]
    assert persisted[0] is not None
    assert persisted[0].last_reconcile_detail["operator_decision_ref"] == "hk #706 Q-46"

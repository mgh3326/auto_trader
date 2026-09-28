"""Registration and DB-only handler checks for the Q-46 tool."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.mcp_server.tooling import kis_mock_terminal_registration as terminal
from app.models.review import KISMockOrderLedger
from tests.mcp_server._registration_recorder import (
    RegistrationRecorder,
    collect_profile_tools,
)

pytestmark = pytest.mark.unit


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

    row = KISMockOrderLedger(
        trade_date=datetime(2026, 9, 14, 9, tzinfo=UTC),
        symbol="005930",
        instrument_type="equity_kr",
        side="buy",
        order_type="limit",
        quantity=Decimal("2"),
        price=Decimal("70000"),
        amount=Decimal("140000"),
        currency="KRW",
        order_no="881000001",
        order_time="091500",
        account_mode="kis_mock",
        broker="kis",
        status="accepted",
        lifecycle_state="pending",
        strategy="manual_kr",
        correlation_id="q46-tool-test",
        response_code="0",
        raw_response={
            "rt_cd": "0",
            "odno": "881000001",
            "ord_tmd": "091500",
            "msg": "accepted",
            "msg_cd": "0",
        },
        last_reconcile_detail={
            "reason_code": "pending_unconfirmed",
            "attributed_fill_qty": "0",
        },
    )
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

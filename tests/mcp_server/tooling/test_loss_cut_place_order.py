import datetime
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.mcp_server.tooling import order_execution as oe
from app.mcp_server.tooling import order_validation as ov
from app.mcp_server.tooling import orders_kis_variants
from app.models.review import KISLiveOrderLedger, LiveOrderLedger

_TRADER_AGENT_ID = "6b2192cc-14fa-4335-b572-2fe1e0cb54a7"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dry_run_loss_cut_returns_all_violations_single_response():
    with patch.object(ov, "get_caller_agent_id", return_value="nobody"):
        resp = await oe._place_order_impl(
            symbol="KRW-DOT",
            side="buy",
            market="crypto",
            order_type="market",
            price=1244.0,
            quantity=10,
            dry_run=True,
            exit_intent="loss_cut",
            retrospective_id=None,
            proposal_flow=True,
        )
    assert resp["success"] is False
    assert resp["error"] == "loss_cut_preconditions_failed"
    assert isinstance(resp["violations"], list) and len(resp["violations"]) >= 4


@pytest.mark.unit
@pytest.mark.asyncio
async def test_loss_cut_and_defensive_trim_mutually_exclusive():
    resp = await oe._place_order_impl(
        symbol="KRW-DOT",
        side="sell",
        market="crypto",
        order_type="limit",
        price=1244.0,
        quantity=10,
        dry_run=True,
        exit_intent="loss_cut",
        defensive_trim=True,
        approval_issue_id="ROB-800",
    )
    assert resp["success"] is False
    assert "mutually exclusive" in resp["error"].lower()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_loss_cut_live_rejects_invalid_hash_even_when_mode_off(monkeypatch):
    # ROB-800 hardening: loss_cut is fail-closed on the approval hash regardless
    # of ORDER_APPROVAL_HASH_MODE. With mode="off" the generic gate would skip
    # verification, so a present-but-invalid hash must still be rejected by the
    # loss_cut-specific gate (never reaching _execute_and_record).
    fake_retro = SimpleNamespace(
        id=42,
        symbol="KRW-DOT",
        trigger_type="stop_loss",
        created_at=datetime.datetime.now(datetime.UTC),
    )

    async def _boom(*_a, **_k):
        raise AssertionError("_execute_and_record must not run on an invalid hash")

    monkeypatch.setattr(oe.settings, "order_approval_hash_mode", "off", raising=False)

    with (
        patch.object(ov, "get_caller_agent_id", return_value=_TRADER_AGENT_ID),
        patch.object(
            ov,
            "_get_retrospective_by_id_for_loss_cut",
            new=AsyncMock(return_value=fake_retro),
        ),
        patch.object(oe, "_fetch_current_price", new=AsyncMock(return_value=1245.0)),
        patch.object(
            oe,
            "_validate_sell_side",
            new=AsyncMock(return_value=(10.0, 2000.0, None)),
        ),
        patch.object(
            oe,
            "_build_preview",
            new=AsyncMock(
                return_value={
                    "symbol": "KRW-DOT",
                    "side": "sell",
                    "order_type": "limit",
                    "price": 1244.0,
                    "quantity": 10.0,
                    "estimated_value": 12440.0,
                }
            ),
        ),
        patch.object(oe, "_execute_and_record", new=_boom),
    ):
        resp = await oe._place_order_impl(
            symbol="KRW-DOT",
            side="sell",
            market="crypto",
            order_type="limit",
            price=1244.0,
            quantity=10,
            dry_run=False,
            exit_intent="loss_cut",
            retrospective_id=42,
            exit_reason="stop_loss",
            approval_issue_id="ROB-800",
            approval_hash="not-a-real-token",
            proposal_flow=True,
        )

    assert resp["success"] is False
    assert resp.get("error_code") == "invalid_approval_hash"


@pytest.mark.unit
def test_live_ledger_models_have_exit_intent_column():
    assert "exit_intent" in LiveOrderLedger.__table__.columns
    assert "exit_intent" in KISLiveOrderLedger.__table__.columns


@pytest.mark.unit
def test_kr_variant_forwards_loss_cut_params():
    sig = inspect.signature(orders_kis_variants._place_order_variant)
    assert "exit_intent" in sig.parameters
    assert "retrospective_id" in sig.parameters


@pytest.mark.unit
@pytest.mark.asyncio
async def test_non_loss_cut_orders_unaffected_by_new_param():
    # exit_intent=None must behave exactly as before.
    ctx, errors = await ov._validate_loss_cut_preconditions(
        exit_intent=None,
        retrospective_id=None,
        exit_reason=None,
        approval_issue_id=None,
        side="sell",
        order_type="limit",
        is_mock=False,
        symbol="KRW-DOT",
    )
    assert ctx is None and errors == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_defensive_trim_path_ignores_loss_cut_plumbing():
    # A defensive_trim-style call with exit_intent=None resolves no loss_cut context,
    # and the validator short-circuits without touching any loss_cut-only helpers.
    with patch.object(
        ov,
        "_get_retrospective_by_id_for_loss_cut",
        new=AsyncMock(side_effect=AssertionError("should not be called")),
    ):
        ctx, errors = await ov._validate_loss_cut_preconditions(
            exit_intent=None,
            retrospective_id=None,
            exit_reason=None,
            approval_issue_id=None,
            side="sell",
            order_type="limit",
            is_mock=False,
            symbol="KRW-DOT",
        )
    assert ctx is None and errors == []

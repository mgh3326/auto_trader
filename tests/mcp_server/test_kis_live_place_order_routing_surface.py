# tests/mcp_server/test_kis_live_place_order_routing_surface.py
import datetime
from unittest.mock import AsyncMock, patch

import pytest

from app.mcp_server.tooling import kis_live_ledger as mod
from app.mcp_server.tooling.kis_live_ledger import (
    _build_kr_routing_note,
    _expected_day_order_expiry,
    _extract_broker_exchange,
)
from app.services.brokers.kis.live_order_expiry import (
    REASON_NXT_CARRY,
    REASON_REGULAR_BUY_CONSERVATIVE,
    SESSION_REGULAR,
)

KST = datetime.timezone(datetime.timedelta(hours=9))


def test_expected_day_order_expiry_regular_buy_conservative_2000():
    # ROB-671: regular-session BUY keeps 20:00 by conservative default, but the
    # reason flags the 15:30 death uncertainty.
    now = datetime.datetime(2026, 6, 9, 9, 43, 25, tzinfo=KST)
    iso, reason = _expected_day_order_expiry(now, side="buy")
    assert iso == "2026-06-09T20:00:00+09:00"
    assert reason == REASON_REGULAR_BUY_CONSERVATIVE


def test_expected_day_order_expiry_regular_sell_nxt_carry():
    now = datetime.datetime(2026, 6, 9, 9, 43, 25, tzinfo=KST)
    iso, reason = _expected_day_order_expiry(now, side="sell")
    assert iso == "2026-06-09T20:00:00+09:00"
    assert reason == REASON_NXT_CARRY


def test_routing_note_regular_buy_warns_death_risk():
    note = _build_kr_routing_note(side="buy", accept_session=SESSION_REGULAR)
    assert "15:30" in note
    assert "remaining_qty" in note


def test_routing_note_sell_mentions_nxt_carry():
    note = _build_kr_routing_note(side="sell", accept_session=SESSION_REGULAR)
    assert "NXT" in note
    assert "20:00" in note


def test_extract_broker_exchange_present():
    raw = {"output": {"EXCG_ID_DVSN_CD": "KRX"}}
    assert _extract_broker_exchange(raw) == "KRX"


def test_extract_broker_exchange_absent_is_none():
    assert _extract_broker_exchange({"output": {}}) is None
    assert _extract_broker_exchange({}) is None


@pytest.mark.asyncio
async def test_place_order_response_surfaces_routing_and_reason():
    execution_result = {
        "odno": "0011001100",
        "ord_tmd": "094300",
        "rt_cd": "0",
        "msg1": "정상",
        "output": {"EXCG_ID_DVSN_CD": "KRX"},
    }
    dry_run_result = {"price": 209000, "quantity": 2, "estimated_value": 418000}
    fixed_now = datetime.datetime(2026, 6, 9, 9, 43, 0, tzinfo=KST)
    with (
        patch.object(mod, "_save_kis_live_order_ledger", AsyncMock(return_value=42)),
        patch.object(mod, "now_kst", lambda: fixed_now),
    ):
        resp = await mod._record_kis_live_order(
            normalized_symbol="005930",
            market_type="equity_kr",
            side="buy",
            order_type="limit",
            dry_run_result=dry_run_result,
            execution_result=execution_result,
            reason=None,
            exit_reason=None,
            thesis="t",
            strategy="s",
            target_price=None,
            stop_loss=None,
            min_hold_days=None,
            notes=None,
            indicators_snapshot=None,
        )
    assert resp["order_validity"] == "day"
    assert resp["routing"]["requested_venue"] == "auto"
    assert resp["broker_exchange"] == "KRX"
    assert resp["expected_expiry"] == "2026-06-09T20:00:00+09:00"
    assert resp["expiry_reason"] == REASON_REGULAR_BUY_CONSERVATIVE
    assert "15:30" in resp["routing"]["note"]

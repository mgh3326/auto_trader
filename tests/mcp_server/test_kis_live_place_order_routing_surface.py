# tests/mcp_server/test_kis_live_place_order_routing_surface.py
import datetime
from unittest.mock import AsyncMock, patch

import pytest

from app.mcp_server.tooling import kis_live_ledger as mod
from app.mcp_server.tooling.kis_live_ledger import (
    _expected_day_order_expiry,
    _extract_broker_exchange,
)

KST = datetime.timezone(datetime.timedelta(hours=9))


def test_expected_day_order_expiry_is_2000_kst_of_send_date():
    # ROB-487: SOR day order는 NXT 마감(20:00 KST)까지 유효 — 15:31 NXT 세션
    # 주문이 과거 시각(15:30)의 expected_expiry를 받던 모순 해소.
    now = datetime.datetime(2026, 6, 9, 15, 31, 25, tzinfo=KST)
    assert _expected_day_order_expiry(now) == "2026-06-09T20:00:00+09:00"


def test_extract_broker_exchange_present():
    raw = {"output": {"EXCG_ID_DVSN_CD": "KRX"}}
    assert _extract_broker_exchange(raw) == "KRX"


def test_extract_broker_exchange_absent_is_none():
    assert _extract_broker_exchange({"output": {}}) is None
    assert _extract_broker_exchange({}) is None


@pytest.mark.asyncio
async def test_place_order_response_surfaces_routing_fields():
    execution_result = {
        "odno": "0011001100",
        "ord_tmd": "094300",
        "rt_cd": "0",
        "msg1": "정상",
        "output": {"EXCG_ID_DVSN_CD": "KRX"},
    }
    dry_run_result = {"price": 209000, "quantity": 2, "estimated_value": 418000}
    with patch.object(mod, "_save_kis_live_order_ledger", AsyncMock(return_value=42)):
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
    assert resp["expected_expiry"].endswith("20:00:00+09:00")

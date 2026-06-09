"""ROB-473 — _place_order_impl이 report_item_uuid를 ledger record로 스레딩."""

from __future__ import annotations

import uuid

import pytest


@pytest.mark.asyncio
async def test_execute_and_record_threads_report_item_uuid_to_kis_live(monkeypatch):
    from app.mcp_server.tooling import order_execution as oe

    captured = {}

    async def _fake_record_kis_live(**kwargs):
        captured.update(kwargs)
        return {"success": True}

    async def _fake_execute_order(**kwargs):
        return {"odno": "123456", "ord_tmd": "090000"}

    # _record_kis_live_order는 함수 내부 import이므로 원본 모듈에서 패치
    from app.mcp_server.tooling import kis_live_ledger

    monkeypatch.setattr(
        kis_live_ledger, "_record_kis_live_order", _fake_record_kis_live
    )
    monkeypatch.setattr(
        oe, "_execute_order", _fake_execute_order
    )

    rid = uuid.uuid4()
    await oe._execute_and_record(
        normalized_symbol="005930",
        side="buy",
        order_type="limit",
        order_quantity=1.0,
        price=70000.0,
        market_type="equity_kr",
        current_price=70000.0,
        avg_price=0.0,
        dry_run_result={"price": 70000.0, "quantity": 1.0, "estimated_value": 70000.0},
        order_amount=70000.0,
        reason="r",
        exit_reason=None,
        thesis=None,
        strategy=None,
        target_price=None,
        stop_loss=None,
        min_hold_days=None,
        notes=None,
        indicators_snapshot=None,
        defensive_trim_ctx=None,
        order_error_fn=lambda m: {"success": False, "error": m},
        is_mock=False,
        report_item_uuid=rid,
    )
    assert captured.get("report_item_uuid") == rid


def test_place_order_impl_parses_report_item_uuid_fail_open():
    # 잘못된 uuid 문자열은 주문을 차단하지 않고 None으로 fail-open 처리되어야 한다.
    from app.mcp_server.tooling.order_execution import _coerce_report_item_uuid

    assert _coerce_report_item_uuid(None) is None
    assert _coerce_report_item_uuid("not-a-uuid") is None
    good = uuid.uuid4()
    assert _coerce_report_item_uuid(str(good)) == good

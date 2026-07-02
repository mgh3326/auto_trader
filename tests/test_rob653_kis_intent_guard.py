# tests/test_rob653_kis_intent_guard.py
import pytest
import pytest_asyncio
from sqlalchemy import delete

import app.mcp_server.tooling.order_execution as oe
from app.models.review import OrderSendIntent
from app.services.order_send_intent_service import OrderSendIntentService


@pytest_asyncio.fixture(autouse=True)
async def _clean_intents(db_session):
    await db_session.execute(delete(OrderSendIntent))
    await db_session.commit()


@pytest.mark.asyncio
async def test_kis_duplicate_intent_blocks_second_send(monkeypatch, db_session):
    # Pre-reserve the key so the guard sees a conflict on the real path.
    sent = {"count": 0}

    async def _fake_execute(**kwargs):
        sent["count"] += 1
        return {"odno": "KISDUP653", "rt_cd": "0", "msg": "ok"}

    monkeypatch.setattr(oe, "_execute_order", _fake_execute)

    key = "p6a-kr-dup"
    svc = OrderSendIntentService(db_session)
    await svc.reserve(account_scope="kis_live", idempotency_key=key)

    err = await oe._execute_and_record(
        normalized_symbol="005930",
        side="buy",
        order_type="limit",
        order_quantity=10,
        price=70000,
        market_type="equity_kr",
        current_price=70000,
        avg_price=0.0,
        dry_run_result={"price": 70000, "quantity": 10, "estimated_value": 700000},
        order_amount=700000,
        reason="t",
        exit_reason=None,
        thesis="t",
        strategy="t",
        target_price=None,
        stop_loss=None,
        min_hold_days=None,
        notes=None,
        indicators_snapshot=None,
        defensive_trim_ctx=None,
        order_error_fn=lambda m: {"success": False, "error": m},
        idempotency_key=key,
    )
    assert err["success"] is False
    assert "intent" in err["error"].lower() or "중복" in err["error"]
    assert sent["count"] == 0  # never sent

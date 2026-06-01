import pytest
from unittest.mock import AsyncMock, patch


def _exec(**kw):
    base = {"uuid": "U-ROUTE-1"}
    base.update(kw)
    return base


@pytest.mark.unit
@pytest.mark.asyncio
async def test_crypto_limit_is_accepted_only():
    from app.mcp_server.tooling import order_execution as oe

    with (
        patch.object(oe, "_execute_order", new=AsyncMock(return_value=_exec())),
        patch.object(oe, "_check_daily_order_limit", new=AsyncMock(return_value=True)),
        patch.object(oe, "_record_order_history", new=AsyncMock()),
        patch.object(oe, "_record_fill_and_journals", new=AsyncMock()) as m_legacy,
        patch("app.mcp_server.tooling.live_order_ledger._record_live_order", new=AsyncMock(return_value={"fill_recorded": False})) as m_accept,
    ):
        await oe._execute_and_record(
            normalized_symbol="BTC", side="buy", order_type="limit",
            order_quantity=0.01, price=50_000_000.0, market_type="crypto",
            current_price=50_000_000.0, avg_price=0.0,
            dry_run_result={"price": 50_000_000.0, "quantity": 0.01, "estimated_value": 500_000.0},
            order_amount=500_000.0, reason="r", exit_reason=None, thesis="t", strategy="s",
            target_price=None, stop_loss=None, min_hold_days=None, notes=None,
            indicators_snapshot=None, defensive_trim_ctx=None, order_error_fn=lambda *a, **k: None,
            is_mock=False,
        )
    m_accept.assert_awaited_once()
    _, kw = m_accept.await_args
    assert kw["broker"] == "upbit"
    assert kw["inline_confirm"] is False     # 지정가 = reconcile 위임
    m_legacy.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_crypto_market_inline_confirm():
    from app.mcp_server.tooling import order_execution as oe

    with (
        patch.object(oe, "_execute_order", new=AsyncMock(return_value=_exec())),
        patch.object(oe, "_check_daily_order_limit", new=AsyncMock(return_value=True)),
        patch.object(oe, "_record_order_history", new=AsyncMock()),
        patch("app.mcp_server.tooling.live_order_ledger._record_live_order", new=AsyncMock(return_value={"fill_recorded": True})) as m_accept,
    ):
        await oe._execute_and_record(
            normalized_symbol="BTC", side="buy", order_type="market",
            order_quantity=0.01, price=None, market_type="crypto",
            current_price=50_000_000.0, avg_price=0.0,
            dry_run_result={"price": 0.0, "quantity": 0.01, "estimated_value": 500_000.0},
            order_amount=500_000.0, reason="r", exit_reason=None, thesis="t", strategy="s",
            target_price=None, stop_loss=None, min_hold_days=None, notes=None,
            indicators_snapshot=None, defensive_trim_ctx=None, order_error_fn=lambda *a, **k: None,
            is_mock=False,
        )
    _, kw = m_accept.await_args
    assert kw["inline_confirm"] is True       # 시장가 = 전송 직후 inline 확인

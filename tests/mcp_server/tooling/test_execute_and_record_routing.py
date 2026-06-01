import pytest
from unittest.mock import AsyncMock, patch


@pytest.mark.unit
@pytest.mark.asyncio
async def test_us_live_routes_to_accepted_only(monkeypatch):
    from app.mcp_server.tooling import order_execution as oe

    # execute_order가 broker accept(odno 반환)했다고 가정
    exec_result = {"rt_cd": "0", "odno": "US-ROUTE-1", "ovrs_excg_cd": "NASD", "output": {}}

    with (
        patch.object(oe, "_execute_order", new=AsyncMock(return_value=exec_result)),
        patch.object(oe, "_check_daily_order_limit", new=AsyncMock(return_value=True)),
        patch.object(oe, "_record_order_history", new=AsyncMock()),
        patch.object(oe, "_record_fill_and_journals", new=AsyncMock()) as m_legacy,
        patch("app.mcp_server.tooling.live_order_ledger._record_live_order", new=AsyncMock(return_value={"fill_recorded": False, "ledger_id": 1})) as m_accept,
    ):
        result = await oe._execute_and_record(
            normalized_symbol="AAPL", side="buy", order_type="limit",
            order_quantity=2.0, price=190.0, market_type="equity_us",
            current_price=191.0, avg_price=0.0, dry_run_result={"price": 190.0, "quantity": 2.0, "estimated_value": 380.0},
            order_amount=380.0, reason="r", exit_reason=None, thesis="t", strategy="s",
            target_price=None, stop_loss=None, min_hold_days=None, notes=None,
            indicators_snapshot=None, defensive_trim_ctx=None, order_error_fn=lambda *a, **k: None,
            is_mock=False,
        )

    m_accept.assert_awaited_once()      # accepted-only 경로
    m_legacy.assert_not_awaited()       # 선반영 경로 미사용
    assert result["fill_recorded"] is False

"""
Tests for MCP portfolio tools: get_cash_balance, get_holdings, get_position, simulate_avg_cost.

These tests cover portfolio-related MCP tools including cash balance queries,
holdings management, position tracking, and average cost simulation.
"""

from unittest.mock import AsyncMock

import pytest

import app.services.brokers.upbit.client as upbit_service
from app.services.upbit_symbol_universe_service import (
    UpbitSymbolInactiveError,
    UpbitSymbolNotRegisteredError,
    UpbitSymbolUniverseEmptyError,
)
from tests._mcp_tooling_support import (
    _patch_runtime_attr,
    _upbit_name_lookup_mock,
    build_tools,
)

# ---------------------------------------------------------------------------
# get_cash_balance tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_cash_balance_all_accounts(monkeypatch):
    tools = build_tools()

    class MockKISClient:
        async def inquire_integrated_margin(self):
            return {
                "dnca_tot_amt": "1000000.0",
                "stck_cash_objt_amt": "1000000.0",
                "stck_itgr_cash100_ord_psbl_amt": "800000.0",
            }

        async def inquire_overseas_margin(self):
            return [
                {
                    "natn_name": "미국",
                    "crcy_cd": "USD",
                    "frcr_dncl_amt_2": "500.0",
                    "frcr_gnrl_ord_psbl_amt": "450.0",
                }
            ]

    monkeypatch.setattr(
        upbit_service,
        "fetch_krw_cash_summary",
        AsyncMock(return_value={"balance": 700000.0, "orderable": 500000.0}),
    )
    _patch_runtime_attr(monkeypatch, "KISClient", MockKISClient)

    result = await tools["get_cash_balance"]()

    assert len(result["accounts"]) == 3
    assert result["summary"]["total_krw"] == 1700000.0
    assert result["summary"]["total_usd"] == 500.0
    assert len(result["errors"]) == 0

    upbit_account = next(acc for acc in result["accounts"] if acc["account"] == "upbit")
    assert upbit_account["balance"] == 700000.0
    assert upbit_account["orderable"] == 500000.0
    assert upbit_account["formatted"] == "700,000 KRW"

    kis_domestic_account = next(
        acc for acc in result["accounts"] if acc["account"] == "kis_domestic"
    )
    assert kis_domestic_account["balance"] == 1000000.0
    assert kis_domestic_account["orderable"] == 800000.0

    kis_overseas_account = next(
        acc for acc in result["accounts"] if acc["account"] == "kis_overseas"
    )
    assert kis_overseas_account["balance"] == 500.0
    assert kis_overseas_account["orderable"] == 450.0
    assert kis_overseas_account["exchange_rate"] is None


@pytest.mark.asyncio
async def test_get_cash_balance_with_account_filter(monkeypatch):
    tools = build_tools()

    class MockKISClient:
        async def inquire_integrated_margin(self):
            return {
                "dnca_tot_amt": "1000000.0",
                "stck_cash_objt_amt": "1000000.0",
                "stck_itgr_cash100_ord_psbl_amt": "800000.0",
            }

        async def inquire_overseas_margin(self):
            return [
                {
                    "natn_name": "미국",
                    "crcy_cd": "USD",
                    "frcr_dncl_amt_2": "500.0",
                    "frcr_gnrl_ord_psbl_amt": "450.0",
                }
            ]

    _patch_runtime_attr(monkeypatch, "KISClient", MockKISClient)
    monkeypatch.setattr(
        upbit_service,
        "fetch_krw_cash_summary",
        AsyncMock(side_effect=RuntimeError("Upbit API error")),
    )

    result = await tools["get_cash_balance"](account="upbit")
    assert len(result["accounts"]) == 0
    assert result["summary"]["total_krw"] == 0.0

    result = await tools["get_cash_balance"](account="kis")
    assert len(result["accounts"]) == 2
    assert result["accounts"][0]["account"] == "kis_domestic"
    assert result["accounts"][1]["account"] == "kis_overseas"


@pytest.mark.asyncio
async def test_get_cash_balance_with_account_filter_upbit_success(monkeypatch):
    tools = build_tools()

    monkeypatch.setattr(
        upbit_service,
        "fetch_krw_cash_summary",
        AsyncMock(return_value={"balance": 700000.0, "orderable": 500000.0}),
    )

    result = await tools["get_cash_balance"](account="upbit")

    assert len(result["accounts"]) == 1
    upbit_account = result["accounts"][0]
    assert upbit_account["account"] == "upbit"
    assert upbit_account["balance"] == 700000.0
    assert upbit_account["orderable"] == 500000.0
    assert result["summary"]["total_krw"] == upbit_account["balance"]
    assert result["summary"]["total_usd"] == 0.0
    assert len(result["errors"]) == 0


@pytest.mark.asyncio
async def test_get_cash_balance_partial_failure(monkeypatch):
    tools = build_tools()

    class MockUpbitService:
        async def fetch_krw_cash_summary(self):
            raise RuntimeError("Upbit API error")

    class MockKISClient:
        async def inquire_integrated_margin(self):
            return {
                "dnca_tot_amt": "1000000.0",
                "stck_cash_objt_amt": "1000000.0",
                "stck_itgr_cash100_ord_psbl_amt": "800000.0",
            }

        async def inquire_overseas_margin(self):
            return [
                {
                    "natn_name": "미국",
                    "crcy_cd": "USD",
                    "frcr_dncl_amt_2": "500.0",
                    "frcr_gnrl_ord_psbl_amt": "450.0",
                }
            ]

    monkeypatch.setattr(
        upbit_service,
        "fetch_krw_cash_summary",
        MockUpbitService().fetch_krw_cash_summary,
    )
    _patch_runtime_attr(monkeypatch, "KISClient", MockKISClient)

    result = await tools["get_cash_balance"]()

    assert len(result["accounts"]) == 2  # KIS domestic + overseas succeeded
    assert len(result["errors"]) == 1
    assert result["errors"][0]["source"] == "upbit"

    kis_overseas_account = next(
        acc for acc in result["accounts"] if acc["account"] == "kis_overseas"
    )
    assert kis_overseas_account["balance"] == 500.0
    assert kis_overseas_account["orderable"] == 450.0
    assert kis_overseas_account["exchange_rate"] is None


@pytest.mark.asyncio
async def test_get_cash_balance_kis_domestic_fail_close(monkeypatch):
    tools = build_tools()

    class FailingKISClient:
        async def inquire_integrated_margin(self):
            raise RuntimeError("integrated margin failed")

    _patch_runtime_attr(monkeypatch, "KISClient", FailingKISClient)

    with pytest.raises(RuntimeError, match="KIS domestic cash balance query failed"):
        await tools["get_cash_balance"](account="kis_domestic")


@pytest.mark.asyncio
async def test_get_cash_balance_kis_fail_close_when_domestic_fails(monkeypatch):
    tools = build_tools()

    class FailingDomesticKISClient:
        async def inquire_integrated_margin(self):
            raise RuntimeError("integrated margin failed")

        async def inquire_overseas_margin(self):
            return [
                {
                    "natn_name": "미국",
                    "crcy_cd": "USD",
                    "frcr_dncl_amt_2": "500.0",
                    "frcr_gnrl_ord_psbl_amt": "450.0",
                }
            ]

    _patch_runtime_attr(monkeypatch, "KISClient", FailingDomesticKISClient)

    with pytest.raises(RuntimeError, match="KIS domestic cash balance query failed"):
        await tools["get_cash_balance"](account="kis")


@pytest.mark.asyncio
async def test_get_cash_balance_kis_domestic_prefers_stck_cash100_max_orderable(
    monkeypatch,
):
    tools = build_tools()

    class MockKISClient:
        async def inquire_integrated_margin(self):
            return {
                "dnca_tot_amt": "5000000.0",
                "stck_cash_objt_amt": "5000000.0",
                "stck_itgr_cash100_ord_psbl_amt": "0",
                "stck_cash100_max_ord_psbl_amt": "3534890.5473",
                "raw": {
                    "dnca_tot_amt": "5000000.0",
                    "stck_cash_objt_amt": "5000000.0",
                    "stck_itgr_cash100_ord_psbl_amt": "0",
                    "stck_cash100_max_ord_psbl_amt": "3534890.5473",
                },
            }

        async def inquire_overseas_margin(self):
            return [
                {
                    "natn_name": "미국",
                    "crcy_cd": "USD",
                    "frcr_dncl_amt_2": "500.0",
                    "frcr_gnrl_ord_psbl_amt": "450.0",
                }
            ]

    _patch_runtime_attr(monkeypatch, "KISClient", MockKISClient)

    kis_only = await tools["get_cash_balance"](account="kis")
    kis_domestic_only = await tools["get_cash_balance"](account="kis_domestic")

    kis_domestic_account = next(
        acc for acc in kis_only["accounts"] if acc["account"] == "kis_domestic"
    )

    assert kis_domestic_account["orderable"] == 3534890.5473
    assert kis_domestic_only["accounts"][0]["orderable"] == 3534890.5473
    assert "stck_cash100_max_ord_psbl_amt" not in kis_domestic_account


@pytest.mark.asyncio
async def test_get_cash_balance_kis_domestic_skips_zero_priority_orderables(
    monkeypatch,
):
    tools = build_tools()

    class MockKISClient:
        async def inquire_integrated_margin(self):
            return {
                "dnca_tot_amt": "5000000.0",
                "stck_cash_objt_amt": "5000000.0",
                "stck_cash100_max_ord_psbl_amt": "0",
                "stck_itgr_cash100_ord_psbl_amt": "0",
                "stck_cash_ord_psbl_amt": "2100000.25",
                "raw": {
                    "dnca_tot_amt": "5000000.0",
                    "stck_cash_objt_amt": "5000000.0",
                    "stck_cash100_max_ord_psbl_amt": "0",
                    "stck_itgr_cash100_ord_psbl_amt": "0",
                    "stck_cash_ord_psbl_amt": "2100000.25",
                },
            }

        async def inquire_overseas_margin(self):
            return [
                {
                    "natn_name": "미국",
                    "crcy_cd": "USD",
                    "frcr_dncl_amt_2": "500.0",
                    "frcr_gnrl_ord_psbl_amt": "450.0",
                }
            ]

    _patch_runtime_attr(monkeypatch, "KISClient", MockKISClient)

    kis_only = await tools["get_cash_balance"](account="kis")
    kis_domestic_only = await tools["get_cash_balance"](account="kis_domestic")

    kis_domestic_account = next(
        acc for acc in kis_only["accounts"] if acc["account"] == "kis_domestic"
    )

    assert kis_domestic_account["balance"] == 5000000.0
    assert kis_domestic_account["orderable"] == 2100000.25
    assert kis_domestic_only["accounts"][0]["balance"] == 5000000.0
    assert kis_domestic_only["accounts"][0]["orderable"] == 2100000.25


@pytest.mark.asyncio
async def test_get_cash_balance_kis_domestic_deducts_pending_buy_orders(monkeypatch):
    tools = build_tools()

    class MockKISClient:
        async def inquire_integrated_margin(self):
            return {
                "dnca_tot_amt": "4300000.0",
                "stck_cash_objt_amt": "4300000.0",
                "stck_cash_ord_psbl_amt": "4300000.0",
            }

        async def inquire_korea_orders(self):
            return [
                {"sll_buy_dvsn_cd": "02", "ord_unpr": "250000", "nccs_qty": "1"},
                {"sll_buy_dvsn_cd": "02", "ord_unpr": "800000", "nccs_qty": "1"},
                {"sll_buy_dvsn_cd": "02", "ord_unpr": "2270000", "nccs_qty": "1"},
                {"sll_buy_dvsn_cd": "01", "ord_unpr": "999999", "nccs_qty": "9"},
            ]

        async def inquire_overseas_margin(self):
            return []

    _patch_runtime_attr(monkeypatch, "KISClient", MockKISClient)

    result = await tools["get_cash_balance"](account="kis_domestic")

    assert result["accounts"][0]["balance"] == 4300000.0
    assert result["accounts"][0]["orderable"] == 980000.0


@pytest.mark.asyncio
async def test_get_cash_balance_kis_domestic_pending_lookup_failure_keeps_raw_orderable(
    monkeypatch,
):
    tools = build_tools()

    class MockKISClient:
        async def inquire_integrated_margin(self):
            return {
                "dnca_tot_amt": "4300000.0",
                "stck_cash_objt_amt": "4300000.0",
                "stck_cash_ord_psbl_amt": "4300000.0",
            }

        async def inquire_korea_orders(self):
            raise RuntimeError("order inquiry failed")

    _patch_runtime_attr(monkeypatch, "KISClient", MockKISClient)

    result = await tools["get_cash_balance"](account="kis_domestic")

    assert result["accounts"][0]["orderable"] == 4300000.0


@pytest.mark.asyncio
async def test_get_cash_balance_kis_domestic_clamps_orderable_at_zero(monkeypatch):
    tools = build_tools()

    class MockKISClient:
        async def inquire_integrated_margin(self):
            return {
                "dnca_tot_amt": "1000000.0",
                "stck_cash_objt_amt": "1000000.0",
                "stck_cash_ord_psbl_amt": "1000000.0",
            }

        async def inquire_korea_orders(self):
            return [
                {"sll_buy_dvsn_cd": "02", "ord_unpr": "600000", "nccs_qty": "2"},
            ]

    _patch_runtime_attr(monkeypatch, "KISClient", MockKISClient)

    result = await tools["get_cash_balance"](account="kis_domestic")

    assert result["accounts"][0]["orderable"] == 0.0


@pytest.mark.asyncio
async def test_get_cash_balance_non_strict_skips_domestic_on_integrated_margin_error(
    monkeypatch,
):
    tools = build_tools()

    class PartialFailKISClient:
        async def inquire_integrated_margin(self):
            raise RuntimeError("integrated margin failed")

        async def inquire_overseas_margin(self):
            return [
                {
                    "natn_name": "미국",
                    "crcy_cd": "USD",
                    "frcr_dncl_amt_2": "500.0",
                    "frcr_gnrl_ord_psbl_amt": "450.0",
                }
            ]

    monkeypatch.setattr(
        upbit_service,
        "fetch_krw_cash_summary",
        AsyncMock(return_value={"balance": 700000.0, "orderable": 500000.0}),
    )
    _patch_runtime_attr(monkeypatch, "KISClient", PartialFailKISClient)

    result = await tools["get_cash_balance"]()

    account_names = {acc["account"] for acc in result["accounts"]}
    assert "kis_domestic" not in account_names
    assert "upbit" in account_names
    assert "kis_overseas" in account_names
    assert any(
        err.get("source") == "kis" and err.get("market") == "kr"
        for err in result["errors"]
    )


@pytest.mark.asyncio
async def test_get_cash_balance_kis_overseas_fail_close(monkeypatch):
    tools = build_tools()

    class FailingKISClient:
        async def inquire_overseas_margin(self):
            raise RuntimeError("overseas margin failed")

    _patch_runtime_attr(monkeypatch, "KISClient", FailingKISClient)

    with pytest.raises(RuntimeError, match="KIS overseas cash balance query failed"):
        await tools["get_cash_balance"](account="kis_overseas")


@pytest.mark.asyncio
async def test_get_cash_balance_kis_overseas_prefers_usd_us_row_for_orderable(
    monkeypatch,
):
    """USD 다중 행일 때 미국(natn_name) 행을 우선 사용한다."""
    tools = build_tools()

    class MockKISClient:
        async def inquire_domestic_cash_balance(self):
            return {
                "dnca_tot_amt": "1000000.0",
                "stck_cash_ord_psbl_amt": "800000.0",
            }

        async def inquire_overseas_margin(self):
            return [
                {
                    "natn_name": "영국",
                    "crcy_cd": "USD",
                    "frcr_dncl_amt1": "5856.2",
                    "frcr_gnrl_ord_psbl_amt": "5798.22",
                },
                {
                    "natn_name": "미국",
                    "crcy_cd": "USD",
                    "frcr_dncl_amt1": "5856.2",
                    "frcr_gnrl_ord_psbl_amt": "5824.17",
                },
            ]

    _patch_runtime_attr(monkeypatch, "KISClient", MockKISClient)

    result = await tools["get_cash_balance"](account="kis_overseas")

    kis_overseas = next(
        (acc for acc in result["accounts"] if acc["account"] == "kis_overseas"),
        None,
    )
    assert kis_overseas is not None
    assert kis_overseas["balance"] == 5856.2
    assert kis_overseas["orderable"] == 5824.17


@pytest.mark.asyncio
async def test_get_cash_balance_kis_overseas_us_row_missing_falls_back_to_usd_max(
    monkeypatch,
):
    """미국 행이 없으면 USD 행 중 최대 일반주문가능금액을 사용한다."""
    tools = build_tools()

    class MockKISClient:
        async def inquire_domestic_cash_balance(self):
            return {
                "dnca_tot_amt": "1000000.0",
                "stck_cash_ord_psbl_amt": "800000.0",
            }

        async def inquire_overseas_margin(self):
            return [
                {
                    "natn_name": "영국",
                    "crcy_cd": "USD",
                    "frcr_dncl_amt1": "5856.2",
                    "frcr_gnrl_ord_psbl_amt": "5798.22",
                },
                {
                    "natn_name": "독일",
                    "crcy_cd": "USD",
                    "frcr_dncl_amt1": "5856.2",
                    "frcr_gnrl_ord_psbl_amt": "5824.27",
                },
            ]

    _patch_runtime_attr(monkeypatch, "KISClient", MockKISClient)

    result = await tools["get_cash_balance"](account="kis_overseas")

    kis_overseas = next(
        (acc for acc in result["accounts"] if acc["account"] == "kis_overseas"),
        None,
    )
    assert kis_overseas is not None
    assert kis_overseas["balance"] == 5856.2
    assert kis_overseas["orderable"] == 5824.27


@pytest.mark.asyncio
async def test_get_cash_balance_kis_overseas_real_balance(monkeypatch):
    """해외 잔고 조회 시 balance/orderable이 0보다 큰 값으로 파싱되는지 테스트."""
    tools = build_tools()

    class MockKISClient:
        async def inquire_domestic_cash_balance(self):
            return {
                "dnca_tot_amt": "5000000.0",
                "stck_cash_ord_psbl_amt": "4000000.0",
            }

        async def inquire_overseas_margin(self):
            return [
                {
                    "natn_name": "미국",
                    "crcy_cd": "USD",
                    "frcr_dncl_amt1": "5500.0",
                    "frcr_gnrl_ord_psbl_amt": "5000.0",
                }
            ]

    _patch_runtime_attr(monkeypatch, "KISClient", MockKISClient)

    result = await tools["get_cash_balance"](account="kis_overseas")

    assert len(result["accounts"]) == 1
    assert result["accounts"][0]["balance"] > 0
    assert result["accounts"][0]["orderable"] > 0
    assert result["summary"]["total_usd"] > 0


@pytest.mark.asyncio
async def test_get_cash_balance_uses_new_kis_field_names(monkeypatch):
    """get_cash_balance가 새 KIS 필드명(frcr_dncl_amt1, frcr_gnrl_ord_psbl_amt)을 사용하는지 테스트."""
    tools = build_tools()

    class MockKISClient:
        async def inquire_domestic_cash_balance(self):
            return {
                "dnca_tot_amt": "1000000.0",
                "stck_cash_ord_psbl_amt": "800000.0",
            }

        async def inquire_overseas_margin(self):
            return [
                {
                    "natn_name": "미국",
                    "crcy_cd": "USD",
                    "frcr_dncl_amt1": "3500.0",
                    "frcr_gnrl_ord_psbl_amt": "3200.0",
                }
            ]

    _patch_runtime_attr(monkeypatch, "KISClient", MockKISClient)

    result = await tools["get_cash_balance"](account="kis_overseas")

    assert len(result["accounts"]) == 1
    assert result["accounts"][0]["balance"] == 3500.0
    assert result["accounts"][0]["orderable"] == 3200.0


# ---------------------------------------------------------------------------
# TestSimulateAvgCost
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestSimulateAvgCost:
    """Tests for simulate_avg_cost tool."""

    async def test_basic_simulation_with_market_price(self):
        tools = build_tools()
        result = await tools["simulate_avg_cost"](
            holdings={"price": 2400000, "quantity": 1},
            plans=[
                {"price": 2050000, "quantity": 1},
                {"price": 1900000, "quantity": 1},
            ],
            current_market_price=2157000,
            target_price=3080000,
        )

        # current_position
        cp = result["current_position"]
        assert cp["avg_price"] == 2400000
        assert cp["total_quantity"] == 1
        assert cp["total_invested"] == 2400000
        assert cp["unrealized_pnl"] == -243000.0
        assert cp["unrealized_pnl_pct"] == -10.12

        assert result["current_market_price"] == 2157000

        # step 1
        s1 = result["steps"][0]
        assert s1["step"] == 1
        assert s1["buy_price"] == 2050000
        assert s1["buy_quantity"] == 1
        assert s1["new_avg_price"] == 2225000
        assert s1["total_quantity"] == 2
        assert s1["total_invested"] == 4450000
        assert s1["breakeven_change_pct"] == 3.15
        assert s1["unrealized_pnl"] == -136000.0
        assert s1["unrealized_pnl_pct"] == -3.06

        # step 2
        s2 = result["steps"][1]
        assert s2["step"] == 2
        assert s2["new_avg_price"] == 2116666.67
        assert s2["total_quantity"] == 3
        assert s2["total_invested"] == 6350000
        # avg 2116666.67 / mkt 2157000 - 1 = -1.87%
        assert s2["breakeven_change_pct"] == -1.87

        # target_analysis
        ta = result["target_analysis"]
        assert ta["target_price"] == 3080000
        assert ta["final_avg_price"] == 2116666.67
        assert ta["total_return_pct"] == 45.51

    async def test_without_market_price(self):
        """Without current_market_price, P&L and breakeven fields are absent."""
        tools = build_tools()
        result = await tools["simulate_avg_cost"](
            holdings={"price": 50000, "quantity": 10},
            plans=[{"price": 40000, "quantity": 10}],
        )

        cp = result["current_position"]
        assert cp["avg_price"] == 50000
        assert "unrealized_pnl" not in cp

        s1 = result["steps"][0]
        assert s1["new_avg_price"] == 45000
        assert "breakeven_change_pct" not in s1
        assert "current_market_price" not in result
        assert "target_analysis" not in result

    async def test_with_target_only(self):
        """target_price without current_market_price still computes return."""
        tools = build_tools()
        result = await tools["simulate_avg_cost"](
            holdings={"price": 100, "quantity": 5},
            plans=[{"price": 80, "quantity": 5}],
            target_price=120,
        )

        ta = result["target_analysis"]
        assert ta["final_avg_price"] == 90
        assert ta["profit_per_unit"] == 30
        assert ta["total_profit"] == 300
        assert ta["total_return_pct"] == 33.33

    async def test_validation_missing_holdings_fields(self):
        tools = build_tools()
        with pytest.raises(ValueError, match="holdings must contain"):
            await tools["simulate_avg_cost"](
                holdings={"price": 100},
                plans=[{"price": 90, "quantity": 1}],
            )

    async def test_validation_empty_plans(self):
        tools = build_tools()
        with pytest.raises(ValueError, match="plans must contain"):
            await tools["simulate_avg_cost"](
                holdings={"price": 100, "quantity": 1},
                plans=[],
            )

    async def test_validation_negative_price(self):
        tools = build_tools()
        with pytest.raises(ValueError, match="must be >= 0"):
            await tools["simulate_avg_cost"](
                holdings={"price": -100, "quantity": 1},
                plans=[{"price": 90, "quantity": 1}],
            )

    async def test_validation_plan_missing_fields(self):
        tools = build_tools()
        with pytest.raises(ValueError, match=r"plans\[0\] must contain"):
            await tools["simulate_avg_cost"](
                holdings={"price": 100, "quantity": 1},
                plans=[{"price": 90}],
            )

    async def test_single_plan(self):
        tools = build_tools()
        result = await tools["simulate_avg_cost"](
            holdings={"price": 1000, "quantity": 2},
            plans=[{"price": 800, "quantity": 2}],
            current_market_price=900,
        )

        assert len(result["steps"]) == 1
        s = result["steps"][0]
        assert s["new_avg_price"] == 900
        assert s["total_quantity"] == 4
        # avg == market → breakeven 0%
        assert s["breakeven_change_pct"] == 0.0
        assert s["unrealized_pnl"] == 0.0

    async def test_accepts_zero_initial_quantity_and_adds_target_metrics(self):
        tools = build_tools()
        result = await tools["simulate_avg_cost"](
            holdings={"price": 0, "quantity": 0},
            plans=[
                {"price": 100, "quantity": 1},
                {"price": 90, "quantity": 1},
            ],
            current_market_price=95,
            target_price=120,
        )

        assert result["current_position"]["avg_price"] is None
        assert result["steps"][0]["target_return_pct"] == 20.0
        assert "pnl_vs_current" in result["steps"][0]
        assert result["steps"][1]["new_avg_price"] == 95.0
        assert result["steps"][1]["target_return_pct"] == 26.32

    async def test_requested_scenario_contains_step_target_return(self):
        tools = build_tools()
        result = await tools["simulate_avg_cost"](
            holdings={"price": 122493036, "quantity": 0.00931179},
            plans=[
                {"quantity": 0.01, "price": 100000000},
                {"quantity": 0.01, "price": 95000000},
            ],
            target_price=120000000,
            current_market_price=101692000,
        )

        assert len(result["steps"]) == 2
        for step in result["steps"]:
            assert "new_avg_price" in step
            assert "total_quantity" in step
            assert "total_invested" in step
            assert "unrealized_pnl" in step
            assert "target_return_pct" in step


# ---------------------------------------------------------------------------
# get_holdings / get_position
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_holdings_groups_by_account_and_calculates_pnl(monkeypatch):
    tools = build_tools()

    class DummyKISClient:
        async def fetch_my_stocks(self):
            return [
                {
                    "pdno": "005930",
                    "prdt_name": "삼성전자",
                    "hldg_qty": "2",
                    "pchs_avg_pric": "70000",
                    "prpr": "70500",
                    "evlu_amt": "141000",
                    "evlu_pfls_amt": "1000",
                    "evlu_pfls_rt": "0.71",
                }
            ]

        async def fetch_my_us_stocks(self):
            return [
                {
                    "ovrs_pdno": "AAPL",
                    "ovrs_item_name": "Apple",
                    "ovrs_cblc_qty": "1",
                    "pchs_avg_pric": "200",
                    "now_pric2": "210",
                    "ovrs_stck_evlu_amt": "210",
                    "frcr_evlu_pfls_amt": "10",
                    "evlu_pfls_rt": "5",
                }
            ]

    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)
    monkeypatch.setattr(
        upbit_service,
        "fetch_my_coins",
        AsyncMock(
            return_value=[
                {
                    "currency": "BTC",
                    "unit_currency": "KRW",
                    "balance": "0.1",
                    "locked": "0",
                    "avg_buy_price": "50000000",
                },
                {"currency": "KRW", "balance": "1000"},
            ]
        ),
    )
    _patch_runtime_attr(
        monkeypatch,
        "get_upbit_korean_name_by_coin",
        _upbit_name_lookup_mock({"BTC": "비트코인"}),
    )
    _patch_runtime_attr(
        monkeypatch,
        "_collect_manual_positions",
        AsyncMock(
            return_value=(
                [
                    {
                        "account": "toss",
                        "account_name": "기본 계좌",
                        "broker": "toss",
                        "source": "manual",
                        "instrument_type": "equity_kr",
                        "market": "kr",
                        "symbol": "005930",
                        "name": "삼성전자(토스)",
                        "quantity": 1.0,
                        "avg_buy_price": 69000.0,
                        "current_price": None,
                        "evaluation_amount": None,
                        "profit_loss": None,
                        "profit_rate": None,
                    }
                ],
                [],
            )
        ),
    )
    _patch_runtime_attr(
        monkeypatch,
        "_fetch_quote_equity_kr",
        AsyncMock(return_value={"price": 71000.0}),
    )
    us_quote_mock = AsyncMock(return_value={"price": 220.0})
    _patch_runtime_attr(monkeypatch, "_fetch_quote_equity_us", us_quote_mock)
    monkeypatch.setattr(
        upbit_service,
        "fetch_multiple_current_prices",
        AsyncMock(return_value={"KRW-BTC": 60000000.0}),
    )
    monkeypatch.setattr(
        upbit_service,
        "fetch_all_market_codes",
        AsyncMock(return_value=["KRW-BTC"]),
    )
    _patch_runtime_attr(
        monkeypatch,
        "get_active_upbit_markets",
        AsyncMock(return_value={"KRW-BTC"}),
    )

    result = await tools["get_holdings"](minimum_value=0)

    assert result["total_accounts"] == 3
    assert result["total_positions"] == 4
    assert result["filtered_count"] == 0
    assert result["filter_reason"] == "minimum_value < 0"

    kis_account = next(item for item in result["accounts"] if item["account"] == "kis")
    kis_kr = next(
        item for item in kis_account["positions"] if item["symbol"] == "005930"
    )
    assert kis_kr["current_price"] == 71000.0
    assert kis_kr["evaluation_amount"] == 142000.0
    assert kis_kr["profit_loss"] == 2000.0
    assert kis_kr["profit_rate"] == 1.43

    kis_us = next(item for item in kis_account["positions"] if item["symbol"] == "AAPL")
    assert kis_us["current_price"] == 210.0
    assert kis_us["evaluation_amount"] == 210.0
    assert kis_us["profit_loss"] == 10.0
    assert kis_us["profit_rate"] == 5.0
    us_quote_mock.assert_not_awaited()

    upbit_account = next(
        item for item in result["accounts"] if item["account"] == "upbit"
    )
    btc = upbit_account["positions"][0]
    assert btc["symbol"] == "KRW-BTC"
    assert btc["name"] == "비트코인"
    assert btc["current_price"] == 60000000.0
    assert btc["evaluation_amount"] == 6000000.0


@pytest.mark.asyncio
async def test_get_holdings_crypto_prices_batch_fetch(monkeypatch):
    tools = build_tools()

    class DummyKISClient:
        async def fetch_my_stocks(self):
            return []

        async def fetch_my_us_stocks(self):
            return []

    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)
    monkeypatch.setattr(
        upbit_service,
        "fetch_my_coins",
        AsyncMock(
            return_value=[
                {
                    "currency": "BTC",
                    "unit_currency": "KRW",
                    "balance": "0.1",
                    "locked": "0",
                    "avg_buy_price": "50000000",
                },
                {
                    "currency": "ETH",
                    "unit_currency": "KRW",
                    "balance": "2",
                    "locked": "0",
                    "avg_buy_price": "4000000",
                },
            ]
        ),
    )
    _patch_runtime_attr(
        monkeypatch,
        "get_upbit_korean_name_by_coin",
        _upbit_name_lookup_mock({"BTC": "비트코인", "ETH": "이더리움"}),
    )
    _patch_runtime_attr(
        monkeypatch,
        "_collect_manual_positions",
        AsyncMock(return_value=([], [])),
    )
    _patch_runtime_attr(
        monkeypatch,
        "get_active_upbit_markets",
        AsyncMock(return_value=["KRW-BTC", "KRW-ETH"]),
    )

    async def mock_fetch(markets: list[str]) -> dict[str, float]:
        assert sorted(markets) == ["KRW-BTC", "KRW-ETH"]
        return {"KRW-BTC": 61000000.0, "KRW-ETH": 4200000.0}

    quote_mock = AsyncMock(side_effect=mock_fetch)
    monkeypatch.setattr(
        upbit_service,
        "fetch_multiple_current_prices",
        quote_mock,
    )

    result = await tools["get_holdings"](account="upbit", market="crypto")

    assert result["total_accounts"] == 1
    assert result["total_positions"] == 2

    positions_by_symbol = {
        position["symbol"]: position for position in result["accounts"][0]["positions"]
    }
    assert positions_by_symbol["KRW-BTC"]["current_price"] == 61000000.0
    assert positions_by_symbol["KRW-ETH"]["current_price"] == 4200000.0
    quote_mock.assert_awaited_once()
    assert result["errors"] == []


@pytest.mark.asyncio
async def test_get_holdings_includes_crypto_price_errors(monkeypatch):
    tools = build_tools()

    class DummyKISClient:
        async def fetch_my_stocks(self):
            return []

        async def fetch_my_us_stocks(self):
            return []

    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)
    monkeypatch.setattr(
        upbit_service,
        "fetch_my_coins",
        AsyncMock(
            return_value=[
                {
                    "currency": "BTC",
                    "unit_currency": "KRW",
                    "balance": "0.1",
                    "locked": "0",
                    "avg_buy_price": "50000000",
                },
                {
                    "currency": "DOGE",
                    "unit_currency": "KRW",
                    "balance": "100",
                    "locked": "0",
                    "avg_buy_price": "100",
                },
            ]
        ),
    )
    _patch_runtime_attr(
        monkeypatch,
        "get_upbit_korean_name_by_coin",
        _upbit_name_lookup_mock({"BTC": "비트코인", "DOGE": "도지"}),
    )
    _patch_runtime_attr(
        monkeypatch,
        "_collect_manual_positions",
        AsyncMock(return_value=([], [])),
    )
    _patch_runtime_attr(
        monkeypatch,
        "get_active_upbit_markets",
        AsyncMock(return_value=["KRW-BTC", "KRW-DOGE"]),
    )

    async def mock_fetch(markets: list[str]) -> dict[str, float]:
        assert sorted(markets) == ["KRW-BTC", "KRW-DOGE"]
        return {"KRW-BTC": 62000000.0}

    quote_mock = AsyncMock(side_effect=mock_fetch)
    monkeypatch.setattr(
        upbit_service,
        "fetch_multiple_current_prices",
        quote_mock,
    )

    result = await tools["get_holdings"](account="upbit", market="crypto")

    assert result["total_accounts"] == 1
    assert result["total_positions"] == 1
    assert result["filtered_count"] == 1
    assert result["filter_reason"] == "equity_kr < 5000, equity_us < 10, crypto < 5000"

    positions_by_symbol = {
        position["symbol"]: position for position in result["accounts"][0]["positions"]
    }
    assert positions_by_symbol["KRW-BTC"]["current_price"] == 62000000.0
    assert "KRW-DOGE" not in positions_by_symbol

    assert len(result["errors"]) == 1
    error = result["errors"][0]
    assert error["source"] == "upbit"
    assert error["market"] == "crypto"
    assert error["symbol"] == "KRW-DOGE"
    assert error["stage"] == "current_price"
    assert error["error"] == "price missing in batch ticker response"
    quote_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_holdings_applies_minimum_value_filter(monkeypatch):
    tools = build_tools()

    class DummyKISClient:
        async def fetch_my_stocks(self):
            return []

        async def fetch_my_us_stocks(self):
            return []

    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)
    monkeypatch.setattr(
        upbit_service,
        "fetch_my_coins",
        AsyncMock(
            return_value=[
                {
                    "currency": "BTC",
                    "unit_currency": "KRW",
                    "balance": "0.1",
                    "locked": "0",
                    "avg_buy_price": "50000000",
                },
                {
                    "currency": "ONG",
                    "unit_currency": "KRW",
                    "balance": "1",
                    "locked": "0",
                    "avg_buy_price": "50",
                },
                {
                    "currency": "XYM",
                    "unit_currency": "KRW",
                    "balance": "0.0000007",
                    "locked": "0",
                    "avg_buy_price": "100",
                },
                {
                    "currency": "PCI",
                    "unit_currency": "KRW",
                    "balance": "0.2",
                    "locked": "0",
                    "avg_buy_price": "100",
                },
            ]
        ),
    )
    _patch_runtime_attr(
        monkeypatch,
        "get_upbit_korean_name_by_coin",
        _upbit_name_lookup_mock(
            {
                "BTC": "비트코인",
                "ONG": "온톨로지가스",
                "XYM": "심볼",
                "PCI": "페이코인",
            }
        ),
    )
    _patch_runtime_attr(
        monkeypatch,
        "_collect_manual_positions",
        AsyncMock(return_value=([], [])),
    )
    _patch_runtime_attr(
        monkeypatch,
        "get_active_upbit_markets",
        AsyncMock(return_value=["KRW-BTC", "KRW-ONG"]),
    )

    async def mock_fetch(markets: list[str]) -> dict[str, float]:
        assert sorted(markets) == ["KRW-BTC", "KRW-ONG"]
        return {
            "KRW-BTC": 62000000.0,
            "KRW-ONG": 28.0,
        }

    quote_mock = AsyncMock(side_effect=mock_fetch)
    monkeypatch.setattr(
        upbit_service,
        "fetch_multiple_current_prices",
        quote_mock,
    )

    result = await tools["get_holdings"](account="upbit", market="crypto")

    assert result["filtered_count"] == 3
    assert result["filter_reason"] == "equity_kr < 5000, equity_us < 10, crypto < 5000"
    assert result["total_positions"] == 1
    assert result["filters"]["minimum_value"] == {
        "equity_kr": 5000.0,
        "equity_us": 10.0,
        "crypto": 5000.0,
    }

    positions_by_symbol = {
        position["symbol"]: position for position in result["accounts"][0]["positions"]
    }
    assert "KRW-BTC" in positions_by_symbol
    assert "KRW-PCI" not in positions_by_symbol

    assert result["errors"] == []
    quote_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_holdings_filters_delisted_markets_before_batch_fetch(monkeypatch):
    tools = build_tools()

    class DummyKISClient:
        async def fetch_my_stocks(self):
            return []

        async def fetch_my_us_stocks(self):
            return []

    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)
    monkeypatch.setattr(
        upbit_service,
        "fetch_my_coins",
        AsyncMock(
            return_value=[
                {
                    "currency": "BTC",
                    "unit_currency": "KRW",
                    "balance": "0.1",
                    "locked": "0",
                    "avg_buy_price": "50000000",
                },
                {
                    "currency": "PCI",
                    "unit_currency": "KRW",
                    "balance": "0.2",
                    "locked": "0",
                    "avg_buy_price": "100",
                },
            ]
        ),
    )
    _patch_runtime_attr(
        monkeypatch,
        "get_upbit_korean_name_by_coin",
        _upbit_name_lookup_mock({"BTC": "비트코인", "PCI": "페이코인"}),
    )
    _patch_runtime_attr(
        monkeypatch,
        "_collect_manual_positions",
        AsyncMock(return_value=([], [])),
    )
    _patch_runtime_attr(
        monkeypatch,
        "get_active_upbit_markets",
        AsyncMock(return_value=["KRW-BTC"]),
    )

    async def mock_fetch(markets: list[str]) -> dict[str, float]:
        assert markets == ["KRW-BTC"]
        return {"KRW-BTC": 62000000.0}

    quote_mock = AsyncMock(side_effect=mock_fetch)
    monkeypatch.setattr(
        upbit_service,
        "fetch_multiple_current_prices",
        quote_mock,
    )

    result = await tools["get_holdings"](account="upbit", market="crypto")

    assert result["total_accounts"] == 1
    assert result["total_positions"] == 1
    assert result["filtered_count"] == 1
    assert result["filter_reason"] == "equity_kr < 5000, equity_us < 10, crypto < 5000"

    positions_by_symbol = {
        position["symbol"]: position for position in result["accounts"][0]["positions"]
    }
    assert positions_by_symbol["KRW-BTC"]["symbol"] == "KRW-BTC"
    assert positions_by_symbol["KRW-BTC"]["current_price"] == 62000000.0
    assert "KRW-PCI" not in positions_by_symbol

    assert result["errors"] == []
    quote_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_holdings_filters_account_market_and_disables_prices(monkeypatch):
    tools = build_tools()

    class DummyKISClient:
        async def fetch_my_stocks(self):
            return []

        async def fetch_my_us_stocks(self):
            return []

    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)
    monkeypatch.setattr(
        upbit_service,
        "fetch_my_coins",
        AsyncMock(
            return_value=[
                {
                    "currency": "ETH",
                    "unit_currency": "KRW",
                    "balance": "1.5",
                    "locked": "0.5",
                    "avg_buy_price": "4000000",
                }
            ]
        ),
    )
    _patch_runtime_attr(
        monkeypatch,
        "get_upbit_korean_name_by_coin",
        _upbit_name_lookup_mock({"ETH": "이더리움"}),
    )
    _patch_runtime_attr(
        monkeypatch,
        "_collect_manual_positions",
        AsyncMock(return_value=([], [])),
    )
    quote_mock = AsyncMock(return_value={"KRW-ETH": 4300000.0})
    monkeypatch.setattr(upbit_service, "fetch_multiple_current_prices", quote_mock)

    result = await tools["get_holdings"](
        account="upbit", market="crypto", include_current_price=False
    )

    assert result["total_accounts"] == 1
    assert result["total_positions"] == 1
    assert result["accounts"][0]["account"] == "upbit"

    eth = result["accounts"][0]["positions"][0]
    assert eth["symbol"] == "KRW-ETH"
    assert eth["current_price"] is None
    assert eth["evaluation_amount"] is None
    assert eth["profit_loss"] is None
    assert eth["profit_rate"] is None
    assert result["filtered_count"] == 0
    assert (
        result["filter_reason"]
        == "minimum_value filter skipped (include_current_price=False)"
    )
    quote_mock.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "lookup_error",
    [
        UpbitSymbolNotRegisteredError("KRW-PCI not registered"),
        UpbitSymbolInactiveError("KRW-PCI is inactive"),
    ],
)
async def test_get_holdings_include_current_price_false_silently_skips_missing_or_inactive_upbit_coins(
    monkeypatch, lookup_error
):
    tools = build_tools()

    class DummyKISClient:
        async def fetch_my_stocks(self):
            return []

        async def fetch_my_us_stocks(self):
            return []

    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)
    monkeypatch.setattr(
        upbit_service,
        "fetch_my_coins",
        AsyncMock(
            return_value=[
                {
                    "currency": "BTC",
                    "unit_currency": "KRW",
                    "balance": "0.1",
                    "locked": "0",
                    "avg_buy_price": "50000000",
                },
                {
                    "currency": "PCI",
                    "unit_currency": "KRW",
                    "balance": "100",
                    "locked": "0",
                    "avg_buy_price": "100",
                },
            ]
        ),
    )

    async def _lookup(currency: str, quote_currency: str = "KRW", db=None) -> str:
        _ = quote_currency, db
        coin = str(currency).upper()
        if coin == "BTC":
            return "비트코인"
        if coin == "PCI":
            raise lookup_error
        return coin

    _patch_runtime_attr(
        monkeypatch,
        "get_upbit_korean_name_by_coin",
        AsyncMock(side_effect=_lookup),
    )
    _patch_runtime_attr(
        monkeypatch,
        "_collect_manual_positions",
        AsyncMock(return_value=([], [])),
    )
    get_markets_mock = AsyncMock(return_value=["KRW-BTC"])
    _patch_runtime_attr(monkeypatch, "get_active_upbit_markets", get_markets_mock)
    quote_mock = AsyncMock(return_value={"KRW-BTC": 61000000.0})
    monkeypatch.setattr(upbit_service, "fetch_multiple_current_prices", quote_mock)

    result = await tools["get_holdings"](
        account="upbit",
        market="crypto",
        include_current_price=False,
    )

    assert result["total_accounts"] == 1
    assert result["total_positions"] == 1
    symbols = [
        position["symbol"]
        for account_payload in result["accounts"]
        for position in account_payload["positions"]
    ]
    assert symbols == ["KRW-BTC"]
    assert "KRW-PCI" not in symbols
    assert all(error.get("symbol") != "KRW-PCI" for error in result["errors"])
    quote_mock.assert_not_awaited()
    get_markets_mock.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "lookup_error",
    [
        UpbitSymbolNotRegisteredError("KRW-PCI not registered"),
        UpbitSymbolInactiveError("KRW-PCI is inactive"),
    ],
)
async def test_get_holdings_silently_skips_missing_or_inactive_upbit_coins(
    monkeypatch, lookup_error
):
    tools = build_tools()

    class DummyKISClient:
        async def fetch_my_stocks(self):
            return []

        async def fetch_my_us_stocks(self):
            return []

    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)
    monkeypatch.setattr(
        upbit_service,
        "fetch_my_coins",
        AsyncMock(
            return_value=[
                {
                    "currency": "BTC",
                    "unit_currency": "KRW",
                    "balance": "0.1",
                    "locked": "0",
                    "avg_buy_price": "50000000",
                },
                {
                    "currency": "PCI",
                    "unit_currency": "KRW",
                    "balance": "100",
                    "locked": "0",
                    "avg_buy_price": "100",
                },
            ]
        ),
    )

    async def _lookup(currency: str, quote_currency: str = "KRW", db=None) -> str:
        _ = quote_currency, db
        coin = str(currency).upper()
        if coin == "BTC":
            return "비트코인"
        if coin == "PCI":
            raise lookup_error
        return coin

    _patch_runtime_attr(
        monkeypatch,
        "get_upbit_korean_name_by_coin",
        AsyncMock(side_effect=_lookup),
    )
    _patch_runtime_attr(
        monkeypatch,
        "_collect_manual_positions",
        AsyncMock(return_value=([], [])),
    )
    _patch_runtime_attr(
        monkeypatch,
        "get_active_upbit_markets",
        AsyncMock(return_value=["KRW-BTC"]),
    )
    monkeypatch.setattr(
        upbit_service,
        "fetch_multiple_current_prices",
        AsyncMock(return_value={"KRW-BTC": 61000000.0}),
    )

    result = await tools["get_holdings"](account="upbit", market="crypto")

    assert result["total_accounts"] == 1
    assert result["total_positions"] == 1
    symbols = [
        position["symbol"]
        for account_payload in result["accounts"]
        for position in account_payload["positions"]
    ]
    assert symbols == ["KRW-BTC"]
    assert "KRW-PCI" not in symbols
    assert all(error.get("symbol") != "KRW-PCI" for error in result["errors"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "lookup_error",
    [
        UpbitSymbolNotRegisteredError("KRW-PCI not registered"),
        UpbitSymbolInactiveError("KRW-PCI is inactive"),
    ],
)
async def test_get_position_silently_skips_missing_or_inactive_upbit_coins(
    monkeypatch, lookup_error
):
    tools = build_tools()

    class DummyKISClient:
        async def fetch_my_stocks(self):
            return []

        async def fetch_my_us_stocks(self):
            return []

    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)
    monkeypatch.setattr(
        upbit_service,
        "fetch_my_coins",
        AsyncMock(
            return_value=[
                {
                    "currency": "BTC",
                    "unit_currency": "KRW",
                    "balance": "0.1",
                    "locked": "0",
                    "avg_buy_price": "50000000",
                },
                {
                    "currency": "PCI",
                    "unit_currency": "KRW",
                    "balance": "100",
                    "locked": "0",
                    "avg_buy_price": "100",
                },
            ]
        ),
    )

    async def _lookup(currency: str, quote_currency: str = "KRW", db=None) -> str:
        _ = quote_currency, db
        coin = str(currency).upper()
        if coin == "BTC":
            return "비트코인"
        if coin == "PCI":
            raise lookup_error
        return coin

    _patch_runtime_attr(
        monkeypatch,
        "get_upbit_korean_name_by_coin",
        AsyncMock(side_effect=_lookup),
    )
    _patch_runtime_attr(
        monkeypatch,
        "_collect_manual_positions",
        AsyncMock(return_value=([], [])),
    )
    _patch_runtime_attr(
        monkeypatch,
        "get_active_upbit_markets",
        AsyncMock(return_value=["KRW-BTC"]),
    )
    monkeypatch.setattr(
        upbit_service,
        "fetch_multiple_current_prices",
        AsyncMock(return_value={"KRW-BTC": 61000000.0}),
    )

    result = await tools["get_position"]("BTC", market="crypto")

    assert result["has_position"] is True
    assert result["position_count"] == 1
    assert [position["symbol"] for position in result["positions"]] == ["KRW-BTC"]
    assert all(error.get("symbol") != "KRW-PCI" for error in result["errors"])


@pytest.mark.asyncio
async def test_get_holdings_keeps_fail_fast_on_upbit_universe_empty(monkeypatch):
    tools = build_tools()

    class DummyKISClient:
        async def fetch_my_stocks(self):
            return []

        async def fetch_my_us_stocks(self):
            return []

    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)
    monkeypatch.setattr(
        upbit_service,
        "fetch_my_coins",
        AsyncMock(
            return_value=[
                {
                    "currency": "BTC",
                    "unit_currency": "KRW",
                    "balance": "0.1",
                    "locked": "0",
                    "avg_buy_price": "50000000",
                }
            ]
        ),
    )

    async def _lookup(currency: str, quote_currency: str = "KRW", db=None) -> str:
        _ = currency, quote_currency, db
        raise UpbitSymbolUniverseEmptyError("upbit_symbol_universe is empty")

    _patch_runtime_attr(
        monkeypatch,
        "get_upbit_korean_name_by_coin",
        AsyncMock(side_effect=_lookup),
    )
    _patch_runtime_attr(
        monkeypatch,
        "_collect_manual_positions",
        AsyncMock(return_value=([], [])),
    )

    with pytest.raises(UpbitSymbolUniverseEmptyError):
        await tools["get_holdings"](account="upbit", market="crypto")


@pytest.mark.asyncio
async def test_get_holdings_includes_top_level_summary(monkeypatch):
    tools = build_tools()

    mocked_positions = [
        {
            "account": "upbit",
            "account_name": "기본 계좌",
            "broker": "upbit",
            "source": "upbit_api",
            "instrument_type": "crypto",
            "market": "crypto",
            "symbol": "KRW-BTC",
            "name": "비트코인",
            "quantity": 0.1,
            "avg_buy_price": 50000000.0,
            "current_price": 60000000.0,
            "evaluation_amount": 6000000.0,
            "profit_loss": 1000000.0,
            "profit_rate": 20.0,
        },
        {
            "account": "upbit",
            "account_name": "기본 계좌",
            "broker": "upbit",
            "source": "upbit_api",
            "instrument_type": "crypto",
            "market": "crypto",
            "symbol": "KRW-ETH",
            "name": "이더리움",
            "quantity": 1.0,
            "avg_buy_price": 3000000.0,
            "current_price": 4000000.0,
            "evaluation_amount": 4000000.0,
            "profit_loss": 1000000.0,
            "profit_rate": 33.33,
        },
    ]

    _patch_runtime_attr(
        monkeypatch,
        "_collect_portfolio_positions",
        AsyncMock(return_value=(mocked_positions, [], "crypto", "upbit")),
    )

    result = await tools["get_holdings"](
        account="upbit", market="crypto", minimum_value=0
    )

    summary = result["summary"]
    assert summary["position_count"] == 2
    assert summary["total_buy_amount"] == 8000000.0
    assert summary["total_evaluation"] == 10000000.0
    assert summary["total_profit_loss"] == 2000000.0
    assert summary["total_profit_rate"] == 25.0
    assert summary["weights"][0]["symbol"] == "KRW-BTC"
    assert summary["weights"][0]["weight_pct"] == 60.0
    assert summary["weights"][1]["symbol"] == "KRW-ETH"
    assert summary["weights"][1]["weight_pct"] == 40.0


@pytest.mark.asyncio
async def test_get_holdings_summary_sets_price_dependent_fields_null(monkeypatch):
    tools = build_tools()

    mocked_positions = [
        {
            "account": "upbit",
            "account_name": "기본 계좌",
            "broker": "upbit",
            "source": "upbit_api",
            "instrument_type": "crypto",
            "market": "crypto",
            "symbol": "KRW-ETH",
            "name": "이더리움",
            "quantity": 1.0,
            "avg_buy_price": 3000000.0,
            "current_price": None,
            "evaluation_amount": None,
            "profit_loss": None,
            "profit_rate": None,
        }
    ]

    _patch_runtime_attr(
        monkeypatch,
        "_collect_portfolio_positions",
        AsyncMock(return_value=(mocked_positions, [], "crypto", "upbit")),
    )

    result = await tools["get_holdings"](
        account="upbit",
        market="crypto",
        include_current_price=False,
    )

    summary = result["summary"]
    assert summary["total_buy_amount"] == 3000000.0
    assert summary["total_evaluation"] is None
    assert summary["total_profit_loss"] is None
    assert summary["total_profit_rate"] is None
    assert summary["weights"] is None


@pytest.mark.asyncio
async def test_get_holdings_preserves_kis_values_on_yahoo_failure(monkeypatch):
    """Test that KIS-provided evaluation amounts are preserved when Yahoo price fetch fails."""
    tools = build_tools()

    class DummyKISClient:
        async def fetch_my_stocks(self):
            return []

        async def fetch_my_us_stocks(self):
            return [
                {
                    "ovrs_pdno": "AMZN",
                    "ovrs_item_name": "Amazon.com Inc.",
                    "ovrs_cblc_qty": "10",
                    "pchs_avg_pric": "150.0",
                    "now_pric2": "0",
                    "ovrs_stck_evlu_amt": "1600.0",
                    "frcr_evlu_pfls_amt": "100.0",
                    "evlu_pfls_rt": "6.67",
                },
                {
                    "ovrs_pdno": "AAPL",
                    "ovrs_item_name": "Apple Inc.",
                    "ovrs_cblc_qty": "5",
                    "pchs_avg_pric": "180.0",
                    "now_pric2": "0",
                    "ovrs_stck_evlu_amt": "9500.0",
                    "frcr_evlu_pfls_amt": "-500.0",
                    "evlu_pfls_rt": "-5.26",
                },
            ]

    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)
    _patch_runtime_attr(
        monkeypatch,
        "_collect_manual_positions",
        AsyncMock(return_value=([], [])),
    )

    async def mock_fetch_yahoo_raise(symbol: str) -> dict[str, object]:
        raise ValueError(f"Symbol '{symbol}' not found")

    _patch_runtime_attr(monkeypatch, "_fetch_quote_equity_us", mock_fetch_yahoo_raise)

    result = await tools["get_holdings"](account="kis", market="us")

    assert result["total_accounts"] == 1
    assert result["total_positions"] == 2
    assert result["filtered_count"] == 0

    positions_by_symbol = {
        position["symbol"]: position for position in result["accounts"][0]["positions"]
    }

    amzn = positions_by_symbol["AMZN"]
    assert amzn["symbol"] == "AMZN"
    assert amzn["quantity"] == 10.0
    assert amzn["avg_buy_price"] == 150.0
    assert amzn["current_price"] is None
    assert amzn["price_error"] == "Symbol 'AMZN' not found"
    assert amzn["evaluation_amount"] == 1600.0
    assert amzn["profit_loss"] == 100.0
    assert amzn["profit_rate"] == 6.67

    aapl = positions_by_symbol["AAPL"]
    assert aapl["symbol"] == "AAPL"
    assert aapl["quantity"] == 5.0
    assert aapl["avg_buy_price"] == 180.0
    assert aapl["current_price"] is None
    assert aapl["price_error"] == "Symbol 'AAPL' not found"
    assert aapl["evaluation_amount"] == 9500.0
    assert aapl["profit_loss"] == -500.0
    assert aapl["profit_rate"] == -5.26

    assert len(result["errors"]) == 2
    error_symbols = {error["symbol"] for error in result["errors"]}
    assert "AMZN" in error_symbols
    assert "AAPL" in error_symbols
    for error in result["errors"]:
        assert error["source"] == "yahoo"
        assert error["market"] == "us"
        assert error["stage"] == "current_price"
        # Check that error message is in expected format (contains the symbol)
        assert "not found" in error["error"]


@pytest.mark.asyncio
async def test_collect_portfolio_positions_skips_yahoo_for_kis_us_with_valid_numeric_snapshot(
    monkeypatch,
):
    from app.mcp_server.tooling import portfolio_holdings

    positions = [
        {
            "account": "kis",
            "account_name": "기본 계좌",
            "broker": "kis",
            "source": "kis_api",
            "instrument_type": "equity_us",
            "market": "us",
            "symbol": "AAPL",
            "name": "Apple",
            "quantity": 1.0,
            "avg_buy_price": 200.0,
            "current_price": 210.0,
            "evaluation_amount": 210.0,
            "profit_loss": 0.0,
            "profit_rate": 0.0,
        }
    ]

    async def fake_collect_kis_positions(market_filter):
        assert market_filter == "equity_us"
        return positions, []

    quote_mock = AsyncMock(return_value={"price": 220.0})

    monkeypatch.setattr(
        portfolio_holdings, "_collect_kis_positions", fake_collect_kis_positions
    )
    monkeypatch.setattr(
        portfolio_holdings,
        "_collect_upbit_positions",
        AsyncMock(return_value=([], [])),
    )
    monkeypatch.setattr(
        portfolio_holdings,
        "_collect_manual_positions",
        AsyncMock(return_value=([], [])),
    )
    monkeypatch.setattr(portfolio_holdings, "_fetch_quote_equity_us", quote_mock)

    (
        result_positions,
        result_errors,
        _,
        _,
    ) = await portfolio_holdings._collect_portfolio_positions(
        account="kis",
        market="us",
        include_current_price=True,
    )

    assert result_errors == []
    assert result_positions == positions
    assert result_positions[0]["current_price"] == 210.0
    assert result_positions[0]["evaluation_amount"] == 210.0
    assert result_positions[0]["profit_loss"] == 0.0
    assert result_positions[0]["profit_rate"] == 0.0
    quote_mock.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("kis_price_value", ["", "0"])
async def test_get_holdings_fetches_yahoo_only_for_kis_us_missing_price_and_recalculates(
    monkeypatch, kis_price_value
):
    tools = build_tools()

    class DummyKISClient:
        async def fetch_my_stocks(self):
            return []

        async def fetch_my_us_stocks(self):
            return [
                {
                    "ovrs_pdno": "AAPL",
                    "ovrs_item_name": "Apple Inc.",
                    "ovrs_cblc_qty": "1",
                    "pchs_avg_pric": "200.0",
                    "now_pric2": "210.0",
                    "ovrs_stck_evlu_amt": "210.0",
                    "frcr_evlu_pfls_amt": "10.0",
                    "evlu_pfls_rt": "5.0",
                },
                {
                    "ovrs_pdno": "AMZN",
                    "ovrs_item_name": "Amazon.com Inc.",
                    "ovrs_cblc_qty": "10",
                    "pchs_avg_pric": "150.0",
                    "now_pric2": kis_price_value,
                    "ovrs_stck_evlu_amt": "1600.0",
                    "frcr_evlu_pfls_amt": "100.0",
                    "evlu_pfls_rt": "6.67",
                },
            ]

    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)
    _patch_runtime_attr(
        monkeypatch,
        "_collect_manual_positions",
        AsyncMock(return_value=([], [])),
    )
    quote_mock = AsyncMock(return_value={"price": 165.0})
    _patch_runtime_attr(monkeypatch, "_fetch_quote_equity_us", quote_mock)

    result = await tools["get_holdings"](account="kis", market="us")

    assert result["total_accounts"] == 1
    assert result["total_positions"] == 2
    assert result["errors"] == []

    positions_by_symbol = {
        position["symbol"]: position for position in result["accounts"][0]["positions"]
    }

    aapl = positions_by_symbol["AAPL"]
    assert aapl["current_price"] == 210.0
    assert aapl["evaluation_amount"] == 210.0
    assert aapl["profit_loss"] == 10.0
    assert aapl["profit_rate"] == 5.0
    assert "price_error" not in aapl

    amzn = positions_by_symbol["AMZN"]
    assert amzn["current_price"] == 165.0
    assert amzn["evaluation_amount"] == 1650.0
    assert amzn["profit_loss"] == 150.0
    assert amzn["profit_rate"] == 10.0
    assert "price_error" not in amzn

    quote_mock.assert_awaited_once_with("AMZN")


@pytest.mark.asyncio
async def test_get_holdings_fetches_yahoo_for_kis_us_with_missing_kis_metrics(
    monkeypatch,
):
    tools = build_tools()

    class DummyKISClient:
        async def fetch_my_stocks(self):
            return []

        async def fetch_my_us_stocks(self):
            return [
                {
                    "ovrs_pdno": "AAPL",
                    "ovrs_item_name": "Apple Inc.",
                    "ovrs_cblc_qty": "1",
                    "pchs_avg_pric": "200.0",
                    "now_pric2": "210.0",
                    "ovrs_stck_evlu_amt": "",
                    "frcr_evlu_pfls_amt": "",
                    "evlu_pfls_rt": "",
                }
            ]

    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)
    _patch_runtime_attr(
        monkeypatch,
        "_collect_manual_positions",
        AsyncMock(return_value=([], [])),
    )
    quote_mock = AsyncMock(return_value={"price": 220.0})
    _patch_runtime_attr(monkeypatch, "_fetch_quote_equity_us", quote_mock)

    result = await tools["get_holdings"](account="kis", market="us")

    assert result["total_accounts"] == 1
    assert result["total_positions"] == 1
    assert result["errors"] == []

    aapl = result["accounts"][0]["positions"][0]
    assert aapl["symbol"] == "AAPL"
    assert aapl["current_price"] == 220.0
    assert aapl["evaluation_amount"] == 220.0
    assert aapl["profit_loss"] == 20.0
    assert aapl["profit_rate"] == 10.0
    assert "price_error" not in aapl

    quote_mock.assert_awaited_once_with("AAPL")


@pytest.mark.asyncio
@pytest.mark.parametrize("evaluation_amount_raw", ["0", " "])
async def test_get_holdings_fetches_yahoo_for_kis_us_with_invalid_evaluation_amount(
    monkeypatch,
    evaluation_amount_raw,
):
    tools = build_tools()

    class DummyKISClient:
        async def fetch_my_stocks(self):
            return []

        async def fetch_my_us_stocks(self):
            return [
                {
                    "ovrs_pdno": "AAPL",
                    "ovrs_item_name": "Apple Inc.",
                    "ovrs_cblc_qty": "1",
                    "pchs_avg_pric": "200.0",
                    "now_pric2": "210.0",
                    "ovrs_stck_evlu_amt": evaluation_amount_raw,
                    "frcr_evlu_pfls_amt": "10.0",
                    "evlu_pfls_rt": "5.0",
                }
            ]

    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)
    _patch_runtime_attr(
        monkeypatch,
        "_collect_manual_positions",
        AsyncMock(return_value=([], [])),
    )
    quote_mock = AsyncMock(return_value={"price": 220.0})
    _patch_runtime_attr(monkeypatch, "_fetch_quote_equity_us", quote_mock)

    result = await tools["get_holdings"](account="kis", market="us")

    assert result["total_accounts"] == 1
    assert result["total_positions"] == 1
    assert result["errors"] == []

    aapl = result["accounts"][0]["positions"][0]
    assert aapl["symbol"] == "AAPL"
    assert aapl["current_price"] == 220.0
    assert aapl["evaluation_amount"] == 220.0
    assert aapl["profit_loss"] == 20.0
    assert aapl["profit_rate"] == 10.0
    assert "price_error" not in aapl

    quote_mock.assert_awaited_once_with("AAPL")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("profit_loss_raw", "profit_rate_raw"),
    [(" ", "5.0"), ("abc", "5.0"), ("10.0", " "), ("10.0", "abc")],
)
async def test_get_holdings_fetches_yahoo_for_kis_us_with_invalid_profit_metrics(
    monkeypatch,
    profit_loss_raw,
    profit_rate_raw,
):
    tools = build_tools()

    class DummyKISClient:
        async def fetch_my_stocks(self):
            return []

        async def fetch_my_us_stocks(self):
            return [
                {
                    "ovrs_pdno": "AAPL",
                    "ovrs_item_name": "Apple Inc.",
                    "ovrs_cblc_qty": "1",
                    "pchs_avg_pric": "200.0",
                    "now_pric2": "210.0",
                    "ovrs_stck_evlu_amt": "210.0",
                    "frcr_evlu_pfls_amt": profit_loss_raw,
                    "evlu_pfls_rt": profit_rate_raw,
                }
            ]

    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)
    _patch_runtime_attr(
        monkeypatch,
        "_collect_manual_positions",
        AsyncMock(return_value=([], [])),
    )
    quote_mock = AsyncMock(return_value={"price": 220.0})
    _patch_runtime_attr(monkeypatch, "_fetch_quote_equity_us", quote_mock)

    result = await tools["get_holdings"](account="kis", market="us")

    assert result["total_accounts"] == 1
    assert result["total_positions"] == 1
    assert result["errors"] == []

    aapl = result["accounts"][0]["positions"][0]
    assert aapl["symbol"] == "AAPL"
    assert aapl["current_price"] == 220.0
    assert aapl["evaluation_amount"] == 220.0
    assert aapl["profit_loss"] == 20.0
    assert aapl["profit_rate"] == 10.0
    assert "price_error" not in aapl

    quote_mock.assert_awaited_once_with("AAPL")


@pytest.mark.asyncio
async def test_get_holdings_keeps_kis_us_price_when_manual_same_symbol_uses_yahoo(
    monkeypatch,
):
    tools = build_tools()

    class DummyKISClient:
        async def fetch_my_stocks(self):
            return []

        async def fetch_my_us_stocks(self):
            return [
                {
                    "ovrs_pdno": "AAPL",
                    "ovrs_item_name": "Apple Inc.",
                    "ovrs_cblc_qty": "1",
                    "pchs_avg_pric": "200.0",
                    "now_pric2": "210.0",
                    "ovrs_stck_evlu_amt": "210.0",
                    "frcr_evlu_pfls_amt": "10.0",
                    "evlu_pfls_rt": "5.0",
                }
            ]

    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)
    _patch_runtime_attr(
        monkeypatch,
        "_collect_manual_positions",
        AsyncMock(
            return_value=(
                [
                    {
                        "account": "toss",
                        "account_name": "미국 주식",
                        "broker": "toss",
                        "source": "manual",
                        "instrument_type": "equity_us",
                        "market": "us",
                        "symbol": "AAPL",
                        "name": "Apple Manual",
                        "quantity": 2.0,
                        "avg_buy_price": 190.0,
                        "current_price": None,
                        "evaluation_amount": None,
                        "profit_loss": None,
                        "profit_rate": None,
                    }
                ],
                [],
            )
        ),
    )
    quote_mock = AsyncMock(return_value={"price": 225.0})
    _patch_runtime_attr(monkeypatch, "_fetch_quote_equity_us", quote_mock)

    result = await tools["get_holdings"](market="us", minimum_value=0)

    assert result["total_accounts"] == 2
    assert result["total_positions"] == 2

    accounts_by_id = {account["account"]: account for account in result["accounts"]}
    kis_aapl = accounts_by_id["kis"]["positions"][0]
    manual_aapl = accounts_by_id["toss"]["positions"][0]

    assert kis_aapl["current_price"] == 210.0
    assert kis_aapl["evaluation_amount"] == 210.0
    assert kis_aapl["profit_loss"] == 10.0
    assert kis_aapl["profit_rate"] == 5.0
    assert "price_error" not in kis_aapl

    assert manual_aapl["current_price"] == 225.0
    assert manual_aapl["evaluation_amount"] == 450.0
    assert manual_aapl["profit_loss"] == 70.0
    assert manual_aapl["profit_rate"] == 18.42
    assert "price_error" not in manual_aapl

    quote_mock.assert_awaited_once_with("AAPL")


@pytest.mark.asyncio
async def test_get_holdings_only_records_yahoo_error_for_same_symbol_manual_fallback(
    monkeypatch,
):
    tools = build_tools()

    class DummyKISClient:
        async def fetch_my_stocks(self):
            return []

        async def fetch_my_us_stocks(self):
            return [
                {
                    "ovrs_pdno": "AAPL",
                    "ovrs_item_name": "Apple Inc.",
                    "ovrs_cblc_qty": "1",
                    "pchs_avg_pric": "200.0",
                    "now_pric2": "210.0",
                    "ovrs_stck_evlu_amt": "210.0",
                    "frcr_evlu_pfls_amt": "10.0",
                    "evlu_pfls_rt": "5.0",
                }
            ]

    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)
    _patch_runtime_attr(
        monkeypatch,
        "_collect_manual_positions",
        AsyncMock(
            return_value=(
                [
                    {
                        "account": "toss",
                        "account_name": "미국 주식",
                        "broker": "toss",
                        "source": "manual",
                        "instrument_type": "equity_us",
                        "market": "us",
                        "symbol": "AAPL",
                        "name": "Apple Manual",
                        "quantity": 2.0,
                        "avg_buy_price": 190.0,
                        "current_price": None,
                        "evaluation_amount": None,
                        "profit_loss": None,
                        "profit_rate": None,
                    }
                ],
                [],
            )
        ),
    )

    async def raise_yahoo(symbol: str) -> dict[str, object]:
        raise ValueError(f"Symbol '{symbol}' not found")

    _patch_runtime_attr(monkeypatch, "_fetch_quote_equity_us", raise_yahoo)

    result = await tools["get_holdings"](market="us", minimum_value=0)

    accounts_by_id = {account["account"]: account for account in result["accounts"]}
    kis_aapl = accounts_by_id["kis"]["positions"][0]
    manual_aapl = accounts_by_id["toss"]["positions"][0]

    assert kis_aapl["current_price"] == 210.0
    assert kis_aapl["evaluation_amount"] == 210.0
    assert kis_aapl["profit_loss"] == 10.0
    assert kis_aapl["profit_rate"] == 5.0
    assert "price_error" not in kis_aapl

    assert manual_aapl["current_price"] is None
    assert manual_aapl["evaluation_amount"] is None
    assert manual_aapl["profit_loss"] is None
    assert manual_aapl["profit_rate"] is None
    assert manual_aapl["price_error"] == "Symbol 'AAPL' not found"

    assert result["errors"] == [
        {
            "source": "yahoo",
            "market": "us",
            "symbol": "AAPL",
            "stage": "current_price",
            "error": "Symbol 'AAPL' not found",
        }
    ]


@pytest.mark.asyncio
async def test_get_position_returns_positions_and_not_holding_status(monkeypatch):
    tools = build_tools()

    mocked_positions = [
        {
            "account": "kis",
            "account_name": "기본 계좌",
            "broker": "kis",
            "source": "kis_api",
            "instrument_type": "equity_kr",
            "market": "kr",
            "symbol": "005930",
            "name": "삼성전자",
            "quantity": 2.0,
            "avg_buy_price": 70000.0,
            "current_price": 71000.0,
            "evaluation_amount": 142000.0,
            "profit_loss": 2000.0,
            "profit_rate": 1.43,
        },
        {
            "account": "toss",
            "account_name": "기본 계좌",
            "broker": "toss",
            "source": "manual",
            "instrument_type": "equity_kr",
            "market": "kr",
            "symbol": "005930",
            "name": "삼성전자(토스)",
            "quantity": 1.0,
            "avg_buy_price": 69000.0,
            "current_price": 71000.0,
            "evaluation_amount": 71000.0,
            "profit_loss": 2000.0,
            "profit_rate": 2.9,
        },
        {
            "account": "upbit",
            "account_name": "기본 계좌",
            "broker": "upbit",
            "source": "upbit_api",
            "instrument_type": "crypto",
            "market": "crypto",
            "symbol": "KRW-BTC",
            "name": "비트코인",
            "quantity": 0.1,
            "avg_buy_price": 50000000.0,
            "current_price": 60000000.0,
            "evaluation_amount": 6000000.0,
            "profit_loss": 1000000.0,
            "profit_rate": 20.0,
        },
    ]

    _patch_runtime_attr(
        monkeypatch,
        "_collect_portfolio_positions",
        AsyncMock(return_value=(mocked_positions, [], "equity_kr", None)),
    )

    result = await tools["get_position"]("005930", market="kr")
    assert result["has_position"] is True
    assert result["status"] == "보유"
    assert result["position_count"] == 2
    assert sorted(result["accounts"]) == ["kis", "toss"]

    _patch_runtime_attr(
        monkeypatch,
        "_collect_portfolio_positions",
        AsyncMock(return_value=(mocked_positions, [], "equity_us", None)),
    )
    not_holding = await tools["get_position"]("NVDA", market="us")
    assert not_holding["has_position"] is False
    assert not_holding["status"] == "미보유"


@pytest.mark.asyncio
async def test_get_position_crypto_accepts_symbol_without_prefix(monkeypatch):
    tools = build_tools()

    mocked_positions = [
        {
            "account": "upbit",
            "account_name": "기본 계좌",
            "broker": "upbit",
            "source": "upbit_api",
            "instrument_type": "crypto",
            "market": "crypto",
            "symbol": "KRW-BTC",
            "name": "비트코인",
            "quantity": 0.1,
            "avg_buy_price": 50000000.0,
            "current_price": 60000000.0,
            "evaluation_amount": 6000000.0,
            "profit_loss": 1000000.0,
            "profit_rate": 20.0,
        }
    ]

    _patch_runtime_attr(
        monkeypatch,
        "_collect_portfolio_positions",
        AsyncMock(return_value=(mocked_positions, [], "crypto", None)),
    )

    result = await tools["get_position"]("BTC", market="crypto")
    assert result["has_position"] is True
    assert result["position_count"] == 1
    assert result["positions"][0]["symbol"] == "KRW-BTC"

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.services.portfolio_position_detail_service as detail_service_module
from app.mcp_server.tooling import orders_history
from app.services.portfolio_position_detail_service import (
    PortfolioPositionDetailNotFoundError,
    PortfolioPositionDetailService,
)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_page_payload_returns_summary_components_and_journal() -> None:
    overview_service = MagicMock()
    overview_service.get_overview = AsyncMock(
        return_value={
            "positions": [
                {
                    "market_type": "US",
                    "symbol": "NVDA",
                    "name": "NVIDIA Corp.",
                    "quantity": 3.0,
                    "avg_price": 120.0,
                    "current_price": 132.0,
                    "evaluation": 396.0,
                    "profit_loss": 36.0,
                    "profit_rate": 0.1,
                    "components": [
                        {
                            "broker": "kis",
                            "account_name": "ISA",
                            "source": "live",
                            "quantity": 2.0,
                            "avg_price": 118.0,
                            "current_price": 132.0,
                            "evaluation": 264.0,
                            "profit_loss": 28.0,
                            "profit_rate": 0.1186,
                        },
                        {
                            "broker": "toss",
                            "account_name": "미니스탁",
                            "source": "manual",
                            "quantity": 1.0,
                            "avg_price": 124.0,
                            "current_price": 132.0,
                            "evaluation": 132.0,
                            "profit_loss": 8.0,
                            "profit_rate": 0.0645,
                        },
                    ],
                }
            ]
        }
    )
    dashboard_service = MagicMock()
    dashboard_service.get_latest_journal_snapshot = AsyncMock(
        return_value={
            "symbol": "NVDA",
            "strategy": "trend",
            "thesis": "AI capex leader",
            "target_price": 145.0,
            "stop_loss": 118.0,
            "target_distance_pct": 9.85,
            "stop_distance_pct": -10.61,
        }
    )

    service = PortfolioPositionDetailService(
        overview_service=overview_service,
        dashboard_service=dashboard_service,
    )

    with patch.object(
        service,
        "_fetch_action_inputs",
        AsyncMock(return_value={"rsi": 41.2}),
    ):
        payload = await service.get_page_payload(
            user_id=7, market_type="us", symbol="NVDA"
        )

    assert payload["summary"]["symbol"] == "NVDA"
    assert payload["summary"]["account_count"] == 2
    assert payload["journal"]["strategy"] == "trend"
    assert payload["summary"]["target_distance_pct"] == 9.85


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_page_payload_raises_when_position_missing() -> None:
    overview_service = MagicMock()
    overview_service.get_overview = AsyncMock(return_value={"positions": []})

    service = PortfolioPositionDetailService(
        overview_service=overview_service,
        dashboard_service=MagicMock(),
    )

    with pytest.raises(PortfolioPositionDetailNotFoundError):
        await service.get_page_payload(user_id=7, market_type="kr", symbol="035720")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_opinions_payload_returns_crypto_fallback() -> None:
    service = PortfolioPositionDetailService(
        overview_service=MagicMock(),
        dashboard_service=MagicMock(),
    )

    payload = await service.get_opinions_payload(market_type="crypto", symbol="KRW-BTC")

    assert payload["supported"] is False
    assert payload["message"] == "애널리스트 의견이 제공되지 않는 시장입니다."


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_indicators_payload_builds_summary_cards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = PortfolioPositionDetailService(
        overview_service=MagicMock(),
        dashboard_service=MagicMock(),
    )

    monkeypatch.setattr(
        detail_service_module,
        "_get_indicators_impl",
        AsyncMock(
            return_value={
                "price": 100.0,
                "indicators": {
                    "rsi": {"14": 28.4},
                    "stoch_rsi": {"k": 17.2, "d": 22.1},
                    "macd": {"macd": 1.5, "signal": 0.9, "histogram": 0.6},
                    "bollinger": {
                        "upper": 112.0,
                        "middle": 100.0,
                        "lower": 88.0,
                    },
                    "ema": {"20": 98.0, "60": 92.0, "200": 80.0},
                    "sma": {"20": 97.0, "60": 91.0, "200": 78.0},
                },
            }
        ),
    )

    payload = await service.get_indicators_payload(market_type="us", symbol="NVDA")

    assert payload["price"] == 100.0
    assert len(payload["summary_cards"]) >= 5
    assert payload["summary_cards"][0]["label"] == "RSI(14)"
    assert payload["summary_cards"][0]["tone"] == "oversold"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_opinions_payload_flattens_consensus_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = PortfolioPositionDetailService(
        overview_service=MagicMock(),
        dashboard_service=MagicMock(),
    )

    monkeypatch.setattr(
        detail_service_module,
        "handle_get_investment_opinions",
        AsyncMock(
            return_value={
                "consensus": {
                    "consensus": "Buy",
                    "avg_target_price": 155.0,
                    "upside_pct": 12.3,
                    "buy_count": 8,
                    "hold_count": 3,
                    "sell_count": 1,
                },
                "opinions": [{"firm": "Alpha Research", "rating": "Buy"}],
            }
        ),
    )

    payload = await service.get_opinions_payload(market_type="us", symbol="NVDA")

    assert payload["supported"] is True
    assert payload["avg_target_price"] == 155.0
    assert payload["upside_pct"] == 12.3
    assert payload["buy_count"] == 8
    assert payload["opinions"][0]["firm"] == "Alpha Research"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_opinions_payload_defaults_missing_consensus_fields(monkeypatch):
    service = PortfolioPositionDetailService(
        overview_service=MagicMock(),
        dashboard_service=MagicMock(),
    )

    monkeypatch.setattr(
        detail_service_module,
        "handle_get_investment_opinions",
        AsyncMock(return_value={"consensus": None, "opinions": []}),
    )

    payload = await service.get_opinions_payload(market_type="kr", symbol="035720")

    assert payload["supported"] is True
    assert payload["avg_target_price"] is None
    assert payload["buy_count"] is None
    assert payload["opinions"] == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_page_payload_includes_weights_and_action_summary() -> None:
    overview_service = MagicMock()
    overview_service.get_overview = AsyncMock(
        return_value={
            "positions": [
                {
                    "market_type": "US",
                    "symbol": "NVDA",
                    "name": "NVIDIA Corp.",
                    "quantity": 3.0,
                    "avg_price": 120.0,
                    "current_price": 132.0,
                    "evaluation": 180.0,
                    "profit_loss": 36.0,
                    "profit_rate": 0.10,
                    "components": [],
                },
                {
                    "market_type": "US",
                    "symbol": "MSFT",
                    "name": "Microsoft",
                    "evaluation": 620.0,
                    "quantity": 1.0,
                    "avg_price": 100.0,
                    "components": [],
                },
                {
                    "market_type": "KR",
                    "symbol": "035720",
                    "name": "카카오",
                    "evaluation": 1000.0,
                    "quantity": 1.0,
                    "avg_price": 100.0,
                    "components": [],
                },
            ]
        }
    )
    dashboard_service = MagicMock()
    dashboard_service.get_latest_journal_snapshot = AsyncMock(
        return_value={
            "strategy": "trend",
            "target_distance_pct": 12.4,
            "stop_distance_pct": -8.0,
        }
    )

    service = PortfolioPositionDetailService(
        overview_service=overview_service,
        dashboard_service=dashboard_service,
    )

    with patch.object(
        service,
        "_fetch_action_inputs",
        AsyncMock(return_value={"rsi": 41.2}),
    ):
        payload = await service.get_page_payload(
            user_id=7, market_type="us", symbol="NVDA"
        )

    assert payload["weights"]["portfolio_weight_pct"] == 10.0
    assert payload["weights"]["market_weight_pct"] == 22.5
    assert payload["action_summary"]["status"] == "관망"
    assert "비중 큼" in payload["action_summary"]["tags"]
    assert "목표가까지 여유" in payload["action_summary"]["tags"]
    assert "RSI 중립" in payload["action_summary"]["tags"]
    assert payload["action_summary"]["reason"] == "전체 비중 10.0% · 시장 내 22.5% · RSI 41.2"

@pytest.mark.unit
@pytest.mark.asyncio
async def test_action_summary_target_near() -> None:
    overview_service = MagicMock()
    overview_service.get_overview = AsyncMock(
        return_value={
            "positions": [
                {
                    "market_type": "US",
                    "symbol": "NVDA",
                    "name": "NVIDIA Corp.",
                    "quantity": 1.0,
                    "avg_price": 120.0,
                    "current_price": 132.0,
                    "evaluation": 10.0,
                    "profit_loss": 12.0,
                    "profit_rate": 0.10,
                    "components": [],
                },
                {
                    "market_type": "KR",
                    "symbol": "035720",
                    "name": "카카오",
                    "evaluation": 1000.0,
                    "quantity": 1.0,
                    "avg_price": 100.0,
                    "components": [],
                },
            ]
        }
    )
    dashboard_service = MagicMock()
    dashboard_service.get_latest_journal_snapshot = AsyncMock(
        return_value={
            "target_distance_pct": 3.0,
            "stop_distance_pct": -15.0,
        }
    )

    service = PortfolioPositionDetailService(
        overview_service=overview_service,
        dashboard_service=dashboard_service,
    )

    with patch.object(
        service,
        "_fetch_action_inputs",
        AsyncMock(return_value={"rsi": 50.0}),
    ):
        payload = await service.get_page_payload(
            user_id=7, market_type="us", symbol="NVDA"
        )

    assert payload["action_summary"]["status"] == "목표가 근접"
    assert "목표가 근접" in payload["action_summary"]["tags"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_action_summary_stop_warning() -> None:
    overview_service = MagicMock()
    overview_service.get_overview = AsyncMock(
        return_value={
            "positions": [
                {
                    "market_type": "US",
                    "symbol": "NVDA",
                    "name": "NVIDIA Corp.",
                    "quantity": 1.0,
                    "avg_price": 120.0,
                    "current_price": 132.0,
                    "evaluation": 10.0,
                    "profit_loss": 12.0,
                    "profit_rate": 0.10,
                    "components": [],
                },
                {
                    "market_type": "KR",
                    "symbol": "035720",
                    "name": "카카오",
                    "evaluation": 1000.0,
                    "quantity": 1.0,
                    "avg_price": 100.0,
                    "components": [],
                },
            ]
        }
    )
    dashboard_service = MagicMock()
    dashboard_service.get_latest_journal_snapshot = AsyncMock(
        return_value={
            "target_distance_pct": 15.0,
            "stop_distance_pct": -3.0,
        }
    )

    service = PortfolioPositionDetailService(
        overview_service=overview_service,
        dashboard_service=dashboard_service,
    )

    with patch.object(
        service,
        "_fetch_action_inputs",
        AsyncMock(return_value={"rsi": 50.0}),
    ):
        payload = await service.get_page_payload(
            user_id=7, market_type="us", symbol="NVDA"
        )

    assert payload["action_summary"]["status"] == "손절 주의"
    assert "손절 주의" in payload["action_summary"]["tags"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_action_summary_weight_excessive() -> None:
    overview_service = MagicMock()
    overview_service.get_overview = AsyncMock(
        return_value={
            "positions": [
                {
                    "market_type": "US",
                    "symbol": "NVDA",
                    "name": "NVIDIA Corp.",
                    "quantity": 1.0,
                    "avg_price": 120.0,
                    "current_price": 132.0,
                    "evaluation": 2000.0,
                    "profit_loss": 12.0,
                    "profit_rate": 0.10,
                    "components": [],
                },
                {
                    "market_type": "KR",
                    "symbol": "035720",
                    "name": "카카오",
                    "evaluation": 500.0,
                    "quantity": 1.0,
                    "avg_price": 100.0,
                    "components": [],
                },
            ]
        }
    )
    dashboard_service = MagicMock()
    dashboard_service.get_latest_journal_snapshot = AsyncMock(
        return_value={
            "target_distance_pct": 15.0,
            "stop_distance_pct": -15.0,
        }
    )

    service = PortfolioPositionDetailService(
        overview_service=overview_service,
        dashboard_service=dashboard_service,
    )

    with patch.object(
        service,
        "_fetch_action_inputs",
        AsyncMock(return_value={"rsi": 50.0}),
    ):
        payload = await service.get_page_payload(
            user_id=7, market_type="us", symbol="NVDA"
        )

    assert payload["action_summary"]["status"] == "비중 과다"
    assert payload["weights"]["portfolio_weight_pct"] >= 15.0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_action_summary_no_journal() -> None:
    overview_service = MagicMock()
    overview_service.get_overview = AsyncMock(
        return_value={
            "positions": [
                {
                    "market_type": "US",
                    "symbol": "NVDA",
                    "name": "NVIDIA Corp.",
                    "quantity": 1.0,
                    "avg_price": 120.0,
                    "current_price": 132.0,
                    "evaluation": 10.0,
                    "profit_loss": 12.0,
                    "profit_rate": 0.10,
                    "components": [],
                },
                {
                    "market_type": "KR",
                    "symbol": "035720",
                    "name": "카카오",
                    "evaluation": 1000.0,
                    "quantity": 1.0,
                    "avg_price": 100.0,
                    "components": [],
                },
            ]
        }
    )
    dashboard_service = MagicMock()
    dashboard_service.get_latest_journal_snapshot = AsyncMock(return_value=None)

    service = PortfolioPositionDetailService(
        overview_service=overview_service,
        dashboard_service=dashboard_service,
    )

    with patch.object(
        service,
        "_fetch_action_inputs",
        AsyncMock(return_value={"rsi": 50.0}),
    ):
        payload = await service.get_page_payload(
            user_id=7, market_type="us", symbol="NVDA"
        )

    assert payload["action_summary"]["status"] == "저널 없음"
    assert "저널 없음" in payload["action_summary"]["tags"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_action_summary_journal_needs_enrichment() -> None:
    overview_service = MagicMock()
    overview_service.get_overview = AsyncMock(
        return_value={
            "positions": [
                {
                    "market_type": "US",
                    "symbol": "NVDA",
                    "name": "NVIDIA Corp.",
                    "quantity": 1.0,
                    "avg_price": 120.0,
                    "current_price": 132.0,
                    "evaluation": 10.0,
                    "profit_loss": 12.0,
                    "profit_rate": 0.10,
                    "components": [],
                },
                {
                    "market_type": "KR",
                    "symbol": "035720",
                    "name": "카카오",
                    "evaluation": 1000.0,
                    "quantity": 1.0,
                    "avg_price": 100.0,
                    "components": [],
                },
            ]
        }
    )
    dashboard_service = MagicMock()
    dashboard_service.get_latest_journal_snapshot = AsyncMock(
        return_value={
            "target_price": None,
            "stop_loss": None,
            "notes": "",
            "thesis": "",
        }
    )

    service = PortfolioPositionDetailService(
        overview_service=overview_service,
        dashboard_service=dashboard_service,
    )

    with patch.object(
        service,
        "_fetch_action_inputs",
        AsyncMock(return_value={"rsi": 50.0}),
    ):
        payload = await service.get_page_payload(
            user_id=7, market_type="us", symbol="NVDA"
        )

    assert payload["action_summary"]["status"] == "저널 보강 필요"
    assert "저널 보강 필요" in payload["action_summary"]["tags"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_action_summary_missing_rsi_fallback() -> None:
    overview_service = MagicMock()
    overview_service.get_overview = AsyncMock(
        return_value={
            "positions": [
                {
                    "market_type": "US",
                    "symbol": "NVDA",
                    "name": "NVIDIA Corp.",
                    "quantity": 1.0,
                    "avg_price": 120.0,
                    "current_price": 132.0,
                    "evaluation": 10.0,
                    "profit_loss": 12.0,
                    "profit_rate": 0.10,
                    "components": [],
                },
                {
                    "market_type": "KR",
                    "symbol": "035720",
                    "name": "카카오",
                    "evaluation": 1000.0,
                    "quantity": 1.0,
                    "avg_price": 100.0,
                    "components": [],
                },
            ]
        }
    )
    dashboard_service = MagicMock()
    dashboard_service.get_latest_journal_snapshot = AsyncMock(
        return_value={
            "target_distance_pct": 12.0,
            "stop_distance_pct": -10.0,
        }
    )

    service = PortfolioPositionDetailService(
        overview_service=overview_service,
        dashboard_service=dashboard_service,
    )

    with patch.object(
        service,
        "_fetch_action_inputs",
        AsyncMock(return_value={"rsi": None}),
    ):
        payload = await service.get_page_payload(
            user_id=7, market_type="us", symbol="NVDA"
        )

    assert payload["action_summary"]["status"] == "관망"
    assert payload["action_summary"]["reason"] is not None
    assert "RSI" not in payload["action_summary"]["reason"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_orders_payload_splits_recent_fills_and_pending_orders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = PortfolioPositionDetailService(
        overview_service=MagicMock(),
        dashboard_service=MagicMock(),
    )

    monkeypatch.setattr(
        detail_service_module,
        "get_order_history_impl",
        AsyncMock(
            side_effect=[
                {
                    "orders": [
                        {
                            "order_id": "fill-1",
                            "symbol": "NVDA",
                            "side": "buy",
                            "status": "filled",
                            "ordered_price": 452.0,
                            "filled_qty": 1.0,
                            "ordered_qty": 1.0,
                            "ordered_at": "2026-04-01T09:12:00+09:00",
                            "currency": "USD",
                        }
                    ],
                    "errors": [],
                },
                {
                    "orders": [
                        {
                            "order_id": "pending-1",
                            "symbol": "NVDA",
                            "side": "sell",
                            "status": "pending",
                            "ordered_price": 480.0,
                            "ordered_qty": 2.0,
                            "remaining_qty": 1.5,
                            "ordered_at": "2026-04-02T10:00:00+09:00",
                            "currency": "USD",
                        }
                    ],
                    "errors": [],
                },
            ]
        ),
    )

    payload = await service.get_orders_payload(market_type="us", symbol="NVDA")

    assert payload["summary"]["last_fill"]["side"] == "buy"
    assert payload["summary"]["pending_count"] == 1
    assert payload["recent_fills"][0]["amount"] == 452.0
    assert payload["pending_orders"][0]["remaining_quantity"] == 1.5


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_orders_payload_prefers_filled_timestamp_and_avg_price(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = PortfolioPositionDetailService(
        overview_service=MagicMock(),
        dashboard_service=MagicMock(),
    )

    monkeypatch.setattr(
        detail_service_module,
        "get_order_history_impl",
        AsyncMock(
            side_effect=[
                {
                    "orders": [
                        {
                            "order_id": "fill-1",
                            "symbol": "NVDA",
                            "side": "buy",
                            "status": "filled",
                            "ordered_price": 452.0,
                            "filled_avg_price": 455.5,
                            "filled_qty": 2.0,
                            "ordered_qty": 2.0,
                            "ordered_at": "2026-04-01T09:12:00+09:00",
                            "filled_at": "2026-04-01T09:19:00+09:00",
                            "currency": "USD",
                        }
                    ],
                    "errors": [],
                },
                {"orders": [], "errors": []},
            ]
        ),
    )

    payload = await service.get_orders_payload(market_type="us", symbol="NVDA")

    assert payload["summary"]["last_fill"]["ordered_at"] == "2026-04-01T09:19:00+09:00"
    assert payload["recent_fills"][0]["price"] == 455.5
    assert payload["recent_fills"][0]["amount"] == 911.0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_orders_payload_surfaces_filled_kr_history_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = PortfolioPositionDetailService(
        overview_service=MagicMock(),
        dashboard_service=MagicMock(),
    )

    monkeypatch.setattr(
        detail_service_module,
        "get_order_history_impl",
        AsyncMock(
            side_effect=[
                {
                    "orders": [
                        {
                            "order_id": "0012345678",
                            "symbol": "035720",
                            "side": "buy",
                            "status": "filled",
                            "ordered_price": 47500,
                            "filled_avg_price": 47250,
                            "filled_qty": 10,
                            "ordered_qty": 10,
                            "ordered_at": "2026-04-01 095032",
                            "filled_at": "",
                            "currency": "KRW",
                        }
                    ],
                    "errors": [],
                },
                {"orders": [], "errors": []},
            ]
        ),
    )

    payload = await service.get_orders_payload(market_type="kr", symbol="035720")

    assert payload["summary"]["fill_count"] == 1
    assert payload["summary"]["last_fill"]["order_id"] == "0012345678"
    assert payload["recent_fills"][0]["price"] == 47250
    assert payload["recent_fills"][0]["amount"] == 472500


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_orders_payload_uses_real_kr_history_normalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = PortfolioPositionDetailService(
        overview_service=MagicMock(),
        dashboard_service=MagicMock(),
    )

    class FakeKIS:
        async def inquire_daily_order_domestic(self, **kwargs):
            return [
                {
                    "odno": "0012345678",
                    "sll_buy_dvsn_cd": "02",
                    "pdno": "035720",
                    "prdt_name": "카카오",
                    "ord_qty": "10",
                    "ord_unpr": "47500",
                    "tot_ccld_qty": "10",
                    "avg_prvs": "47250",
                    "rmn_qty": "0",
                    "ord_dt": "20260401",
                    "ord_tmd": "095032",
                    "ccld_cndt_name": "없음",
                    "excg_id_dvsn_cd": "SOR",
                    "ordr_empno": "OpnAPI",
                }
            ]

        async def inquire_korea_orders(self):
            return []

    monkeypatch.setattr(orders_history, "KISClient", lambda: FakeKIS())

    payload = await service.get_orders_payload(market_type="kr", symbol="035720")

    assert payload["summary"]["fill_count"] == 1
    assert payload["summary"]["pending_count"] == 0
    assert payload["summary"]["last_fill"]["order_id"] == "0012345678"
    assert payload["recent_fills"][0]["price"] == 47250
    assert payload["recent_fills"][0]["amount"] == 472500
    assert payload["recent_fills"][0]["side"] == "buy"
    assert payload["errors"] == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_orders_payload_returns_stable_empty_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = PortfolioPositionDetailService(
        overview_service=MagicMock(),
        dashboard_service=MagicMock(),
    )

    monkeypatch.setattr(
        detail_service_module,
        "get_order_history_impl",
        AsyncMock(
            side_effect=[
                {"orders": [], "errors": []},
                {"orders": [], "errors": []},
            ]
        ),
    )

    payload = await service.get_orders_payload(market_type="us", symbol="NVDA")

    assert payload["summary"]["last_fill"] is None
    assert payload["summary"]["pending_count"] == 0
    assert payload["summary"]["fill_count"] == 0
    assert payload["recent_fills"] == []
    assert payload["pending_orders"] == []
    assert payload["errors"] == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_orders_payload_keeps_one_side_when_other_fetch_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = PortfolioPositionDetailService(
        overview_service=MagicMock(),
        dashboard_service=MagicMock(),
    )

    monkeypatch.setattr(
        detail_service_module,
        "get_order_history_impl",
        AsyncMock(
            side_effect=[
                RuntimeError("filled fetch failed"),
                {
                    "orders": [
                        {
                            "order_id": "pending-1",
                            "symbol": "NVDA",
                            "side": "sell",
                            "status": "pending",
                            "ordered_price": 480.0,
                            "ordered_qty": 2.0,
                            "remaining_qty": 1.5,
                            "ordered_at": "2026-04-02T10:00:00+09:00",
                            "currency": "USD",
                        }
                    ],
                    "errors": [],
                },
            ]
        ),
    )

    payload = await service.get_orders_payload(market_type="us", symbol="NVDA")

    assert payload["recent_fills"] == []
    assert payload["summary"]["pending_count"] == 1
    assert payload["pending_orders"][0]["order_id"] == "pending-1"
    assert payload["errors"][0]["stage"] == "filled"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_orders_payload_preserves_crypto_symbol_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = PortfolioPositionDetailService(
        overview_service=MagicMock(),
        dashboard_service=MagicMock(),
    )

    history_mock = AsyncMock(
        side_effect=[
            {"orders": [], "errors": []},
            {"orders": [], "errors": []},
        ]
    )
    monkeypatch.setattr(detail_service_module, "get_order_history_impl", history_mock)

    await service.get_orders_payload(market_type="crypto", symbol="KRW-BTC")

    assert history_mock.await_args_list[0].kwargs["symbol"] == "KRW-BTC"
    assert history_mock.await_args_list[0].kwargs["market"] == "crypto"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_news_payload_dedupes_and_sorts_by_relevance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = PortfolioPositionDetailService(
        overview_service=MagicMock(),
        dashboard_service=MagicMock(),
    )

    monkeypatch.setattr(
        detail_service_module,
        "handle_get_news",
        AsyncMock(
            return_value={
                "news": [
                    {
                        "title": "카카오, AI 전략 강화",
                        "url": "https://example.com/a",
                        "source": "연합",
                        "published_at": "2026-04-02T09:00:00+09:00",
                        "summary": "직접 관련 기사",
                        "sentiment": "positive",
                    },
                    {
                        "title": "카카오, AI 전략 강화",
                        "url": "https://example.com/a",
                        "source": "연합",
                        "published_at": "2026-04-02T09:00:00+09:00",
                        "summary": "중복 기사",
                        "sentiment": "positive",
                    },
                    {
                        "title": "국내 인터넷 업종 전반 약세",
                        "url": "https://example.com/b",
                        "source": "매경",
                        "published_at": "2026-04-02T08:00:00+09:00",
                        "summary": "약한 관련도",
                        "sentiment": "neutral",
                    },
                ]
            }
        ),
    )

    payload = await service.get_news_payload(market_type="kr", symbol="035720")

    assert payload["count"] == 2
    assert payload["news"][0]["title"] == "카카오, AI 전략 강화"
    assert payload["news"][0]["published_at"] == "2026-04-02T09:00:00+09:00"
    assert payload["news"][0]["summary"] == "직접 관련 기사"
    assert payload["news"][0]["sentiment"] == "positive"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_news_payload_dedupes_same_title_with_different_urls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = PortfolioPositionDetailService(
        overview_service=MagicMock(),
        dashboard_service=MagicMock(),
    )

    monkeypatch.setattr(
        detail_service_module,
        "handle_get_news",
        AsyncMock(
            return_value={
                "news": [
                    {
                        "title": "카카오, AI 전략 강화",
                        "url": "https://example.com/a",
                        "source": "연합",
                        "published_at": "2026-04-02T09:00:00+09:00",
                        "summary": "직접 관련 기사",
                        "sentiment": "positive",
                    },
                    {
                        "title": "카카오, AI 전략 강화",
                        "url": "https://example.com/redistributed",
                        "source": "매경",
                        "published_at": "2026-04-02T09:01:00+09:00",
                        "summary": "재송출 기사",
                        "sentiment": "positive",
                    },
                ]
            }
        ),
    )

    payload = await service.get_news_payload(market_type="kr", symbol="035720")

    assert payload["count"] == 1
    assert payload["news"][0]["title"] == "카카오, AI 전략 강화"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_news_payload_normalizes_datetime_and_excerpt_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = PortfolioPositionDetailService(
        overview_service=MagicMock(),
        dashboard_service=MagicMock(),
    )

    monkeypatch.setattr(
        detail_service_module,
        "handle_get_news",
        AsyncMock(
            return_value={
                "news": [
                    {
                        "title": "NVIDIA rallies on AI demand",
                        "url": "https://example.com/nvda",
                        "source": "Reuters",
                        "datetime": "2026-04-02T21:00:00+00:00",
                        "description": "A long fallback description for the news item.",
                        "sentiment": "positive",
                    }
                ]
            }
        ),
    )

    payload = await service.get_news_payload(market_type="us", symbol="NVDA")

    assert payload["count"] == 1
    assert payload["news"][0]["published_at"] == "2026-04-02T21:00:00+00:00"
    assert payload["news"][0]["summary"] is None
    assert (
        payload["news"][0]["excerpt"]
        == "A long fallback description for the news item."
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_news_payload_returns_empty_list_when_no_news(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = PortfolioPositionDetailService(
        overview_service=MagicMock(),
        dashboard_service=MagicMock(),
    )

    monkeypatch.setattr(
        detail_service_module,
        "handle_get_news",
        AsyncMock(return_value={"news": []}),
    )

    payload = await service.get_news_payload(market_type="us", symbol="NVDA")

    assert payload == {"count": 0, "news": []}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_opinions_payload_builds_summary_cards_distribution_and_overflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = PortfolioPositionDetailService(
        overview_service=MagicMock(),
        dashboard_service=MagicMock(),
    )

    monkeypatch.setattr(
        detail_service_module,
        "handle_get_investment_opinions",
        AsyncMock(
            return_value={
                "consensus": {
                    "consensus": "Buy",
                    "avg_target_price": 155.0,
                    "upside_pct": 12.3,
                    "buy_count": 8,
                    "hold_count": 3,
                    "sell_count": 1,
                },
                "opinions": [
                    {
                        "firm": "A",
                        "rating": "Buy",
                        "target_price": 160.0,
                        "date": "2026-04-01",
                    },
                    {
                        "firm": "B",
                        "rating": "Buy",
                        "target_price": 158.0,
                        "date": "2026-03-31",
                    },
                    {
                        "firm": "C",
                        "rating": "Hold",
                        "target_price": 150.0,
                        "date": "2026-03-30",
                    },
                    {
                        "firm": "D",
                        "rating": "Buy",
                        "target_price": 162.0,
                        "date": "2026-03-29",
                    },
                    {
                        "firm": "E",
                        "rating": "Sell",
                        "target_price": 140.0,
                        "date": "2026-03-28",
                    },
                ],
            }
        ),
    )

    payload = await service.get_opinions_payload(market_type="us", symbol="NVDA")

    assert payload["summary_cards"][0]["label"] == "Consensus"
    assert payload["distribution"] == {"buy": 8, "hold": 3, "sell": 1}
    assert payload["overflow_count"] == 0
    assert len(payload["top_opinions"]) == 5
    assert payload["top_opinions"][0]["firm"] == "A"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_opinions_payload_limits_top_opinions_and_reports_overflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = PortfolioPositionDetailService(
        overview_service=MagicMock(),
        dashboard_service=MagicMock(),
    )

    monkeypatch.setattr(
        detail_service_module,
        "handle_get_investment_opinions",
        AsyncMock(
            return_value={
                "consensus": {
                    "consensus": "Buy",
                    "avg_target_price": 155.0,
                    "upside_pct": 12.3,
                    "buy_count": 8,
                    "hold_count": 3,
                    "sell_count": 1,
                },
                "opinions": [
                    {
                        "firm": str(i),
                        "rating": "Buy",
                        "target_price": 150.0 + i,
                        "date": f"2026-03-{i:02d}",
                    }
                    for i in range(1, 8)
                ],
            }
        ),
    )

    payload = await service.get_opinions_payload(market_type="us", symbol="NVDA")

    assert len(payload["top_opinions"]) == 5
    assert payload["overflow_count"] == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_opinions_payload_keeps_crypto_fallback_shape() -> None:
    service = PortfolioPositionDetailService(
        overview_service=MagicMock(),
        dashboard_service=MagicMock(),
    )

    payload = await service.get_opinions_payload(market_type="crypto", symbol="KRW-BTC")

    assert payload["supported"] is False
    assert payload["message"] == "애널리스트 의견이 제공되지 않는 시장입니다."
    assert payload["summary_cards"] == []
    assert payload["distribution"] == {}
    assert payload["top_opinions"] == []
    assert payload["overflow_count"] == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_page_payload_builds_compact_action_reason() -> None:
    overview_service = MagicMock()
    overview_service.get_overview = AsyncMock(
        return_value={
            "positions": [
                {
                    "market_type": "KR",
                    "symbol": "035720",
                    "name": "카카오",
                    "current_price": 45000,
                    "avg_price": 46000,
                    "quantity": 10,
                    "portfolio_weight_pct": 6.8,
                    "market_weight_pct": 10.6,
                    "components": [],
                }
            ]
        }
    )
    dashboard_service = MagicMock()
    dashboard_service.get_latest_journal_snapshot = AsyncMock(return_value=None)
    
    service = PortfolioPositionDetailService(
        overview_service=overview_service,
        dashboard_service=dashboard_service,
    )
    
    with patch.object(
        service,
        "_fetch_action_inputs",
        AsyncMock(return_value={"rsi": 36.0}),
    ), patch.object(
        service,
        "_build_weights",
        return_value={"portfolio_weight_pct": 6.8, "market_weight_pct": 10.6},
    ):
        payload = await service.get_page_payload(user_id=7, market_type="KR", symbol="035720")
    
    assert payload["action_summary"]["reason"] == "전체 비중 6.8% · 시장 내 10.6% · RSI 36.0"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_orders_payload_exposes_last_fill_summary_and_status_tones() -> None:
    overview_service = MagicMock()
    dashboard_service = MagicMock()
    service = PortfolioPositionDetailService(
        overview_service=overview_service,
        dashboard_service=dashboard_service,
    )
    
    orders_mock = {
        "orders": [
            {
                "symbol": "NVDA",
                "side": "buy",
                "status": "filled",
                "order_at": "2026-03-22T10:00:00",
            }
        ],
        "count": 1
    }
    
    pending_mock = {
        "orders": [
            {
                "symbol": "NVDA",
                "side": "buy",
                "status": "pending",
                "order_at": "2026-03-22T11:00:00",
            }
        ],
        "count": 1
    }
    
    with patch("app.services.portfolio_position_detail_service.get_order_history_impl") as mock_hist:
        mock_hist.side_effect = [orders_mock, pending_mock]
        payload = await service.get_orders_payload(market_type="us", symbol="NVDA")
    
    assert payload["summary"]["last_fill_summary"] == "최근 체결 1건 · 마지막 매수"
    assert payload["recent_fills"][0]["status_tone"] == "filled"
    assert payload["pending_orders"][0]["status_tone"] == "pending"

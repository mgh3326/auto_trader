from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.schemas.portfolio_decision import PortfolioDecisionSlateResponse
from app.services.portfolio_decision_service import PortfolioDecisionService


def _make_overview_position(**overrides):
    position = {
        "market_type": "US",
        "symbol": "AAPL",
        "name": "Apple",
        "quantity": 10.0,
        "avg_price": 100.0,
        "current_price": 110.0,
        "evaluation": 1100.0,
        "evaluation_krw": 1_500_000.0,
        "profit_loss": 100.0,
        "profit_loss_krw": 136_000.0,
        "profit_rate": 0.10,
        "components": [
            {"broker": "kis", "account_name": "Main", "source": "live"},
        ],
    }
    position.update(overrides)
    return position


@pytest.mark.unit
@pytest.mark.asyncio
async def test_build_decision_slate_returns_valid_structure() -> None:
    overview_service = MagicMock()
    overview_service.get_overview = AsyncMock(
        return_value={
            "success": True,
            "positions": [
                {
                    "market_type": "US",
                    "symbol": "NVDA",
                    "name": "NVIDIA Corp.",
                    "quantity": 3.0,
                    "avg_price": 120.0,
                    "current_price": 132.0,
                    "evaluation": 396.0,
                    "evaluation_krw": 540000.0,
                    "profit_loss": 36.0,
                    "profit_loss_krw": 49000.0,
                    "profit_rate": 0.1,
                    "components": [
                        {"broker": "kis", "account_name": "Main", "source": "live"}
                    ],
                }
            ],
            "facets": {"accounts": []},
        }
    )
    dashboard_service = MagicMock()
    dashboard_service.get_journals_batch = AsyncMock(
        return_value={
            "NVDA": {
                "status": "active",
                "strategy": "trend",
                "target_price": 145.0,
                "stop_loss": 118.0,
                "target_distance_pct": 9.85,
                "stop_distance_pct": -10.61,
            }
        }
    )

    service = PortfolioDecisionService(
        overview_service=overview_service,
        dashboard_service=dashboard_service,
    )

    # Patch support/resistance and indicator helpers
    with (
        patch(
            "app.services.portfolio_decision_service._get_support_resistance_impl",
            AsyncMock(
                return_value={
                    "status": "available",
                    "nearest_support": {
                        "price": 128.5,
                        "distance_pct": -2.65,
                        "strength": "moderate",
                    },
                    "nearest_resistance": {
                        "price": 145.0,
                        "distance_pct": 9.85,
                        "strength": "strong",
                    },
                    "supports": [],
                    "resistances": [],
                }
            ),
        ),
        patch(
            "app.services.portfolio_decision_service._get_indicators_impl",
            AsyncMock(return_value={"indicators": {"rsi": {"14": 45.0}}}),
        ),
    ):
        slate = await service.build_decision_slate(user_id=1)

    assert slate["success"] is True
    assert "decision_run" in slate
    assert len(slate["symbol_groups"]) == 1
    group = slate["symbol_groups"][0]
    assert group["symbol"] == "NVDA"
    assert len(group["items"]) > 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_trim_candidate_when_target_near() -> None:
    overview_service = MagicMock()
    overview_service.get_overview = AsyncMock(
        return_value={
            "success": True,
            "positions": [
                {
                    "market_type": "US",
                    "symbol": "AAPL",
                    "name": "Apple",
                    "quantity": 10.0,
                    "avg_price": 150.0,
                    "current_price": 178.0,
                    "evaluation": 1780.0,
                    "evaluation_krw": 2400000.0,
                    "profit_loss": 280.0,
                    "profit_loss_krw": 380000.0,
                    "profit_rate": 0.18,
                    "components": [],
                },
                {
                    "market_type": "KR",
                    "symbol": "005930",
                    "evaluation": 50000000.0,
                    "evaluation_krw": 50000000.0,
                    "components": [],
                },
            ],
            "facets": {"accounts": []},
        }
    )
    dashboard_service = MagicMock()
    dashboard_service.get_journals_batch = AsyncMock(
        return_value={
            "AAPL": {
                "target_price": 180.0,
                "target_distance_pct": 1.12,  # Near target
                "stop_loss": 140.0,
                "stop_distance_pct": -21.35,
            }
        }
    )

    service = PortfolioDecisionService(
        overview_service=overview_service,
        dashboard_service=dashboard_service,
    )

    with (
        patch(
            "app.services.portfolio_decision_service._get_support_resistance_impl",
            AsyncMock(return_value={"status": "unavailable"}),
        ),
        patch(
            "app.services.portfolio_decision_service._get_indicators_impl",
            AsyncMock(return_value={"indicators": {"rsi": {"14": 65.0}}}),
        ),
    ):
        slate = await service.build_decision_slate(user_id=1)

    group = slate["symbol_groups"][0]
    trim_items = [item for item in group["items"] if item["action"] == "trim_candidate"]
    assert len(trim_items) > 0
    assert "target" in trim_items[0]["id"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sell_watch_when_stop_near() -> None:
    overview_service = MagicMock()
    overview_service.get_overview = AsyncMock(
        return_value={
            "success": True,
            "positions": [
                {
                    "market_type": "KR",
                    "symbol": "005930",
                    "name": "삼성전자",
                    "quantity": 100.0,
                    "avg_price": 75000.0,
                    "current_price": 71000.0,
                    "evaluation": 7100000.0,
                    "evaluation_krw": 7100000.0,
                    "profit_loss": -400000.0,
                    "profit_loss_krw": -400000.0,
                    "profit_rate": -0.0533,
                    "components": [],
                }
            ],
            "facets": {"accounts": []},
        }
    )
    dashboard_service = MagicMock()
    dashboard_service.get_journals_batch = AsyncMock(
        return_value={
            "005930": {
                "stop_loss": 70000.0,
                "stop_distance_pct": -1.41,  # Near stop
            }
        }
    )

    service = PortfolioDecisionService(
        overview_service=overview_service,
        dashboard_service=dashboard_service,
    )

    with (
        patch(
            "app.services.portfolio_decision_service._get_support_resistance_impl",
            AsyncMock(return_value={"status": "unavailable"}),
        ),
        patch(
            "app.services.portfolio_decision_service._get_indicators_impl",
            AsyncMock(return_value={"indicators": {"rsi": {"14": 35.0}}}),
        ),
    ):
        slate = await service.build_decision_slate(user_id=1)

    group = slate["symbol_groups"][0]
    sell_watch_items = [
        item for item in group["items"] if item["action"] == "sell_watch"
    ]
    assert len(sell_watch_items) > 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_manual_review_when_context_missing() -> None:
    overview_service = MagicMock()
    overview_service.get_overview = AsyncMock(
        return_value={
            "success": True,
            "positions": [
                {
                    "market_type": "US",
                    "symbol": "UNKNOWN",
                    "name": "Unknown Stock",
                    "quantity": 1.0,
                    "avg_price": 100.0,
                    "current_price": 0.0,  # Missing price
                    "evaluation": 0.0,
                    "evaluation_krw": 0.0,
                    "profit_loss": 0.0,
                    "profit_loss_krw": 0.0,
                    "profit_rate": 0.0,
                    "components": [],
                }
            ],
            "facets": {"accounts": []},
        }
    )
    dashboard_service = MagicMock()
    dashboard_service.get_journals_batch = AsyncMock(return_value={})

    service = PortfolioDecisionService(
        overview_service=overview_service,
        dashboard_service=dashboard_service,
    )

    with (
        patch(
            "app.services.portfolio_decision_service._get_support_resistance_impl",
            AsyncMock(return_value={"status": "unavailable"}),
        ),
        patch(
            "app.services.portfolio_decision_service._get_indicators_impl",
            AsyncMock(return_value={"indicators": {}}),
        ),
    ):
        slate = await service.build_decision_slate(user_id=1)

    group = slate["symbol_groups"][0]
    manual_review_items = [
        item for item in group["items"] if item["action"] == "manual_review"
    ]
    assert len(manual_review_items) > 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_support_resistance_levels_supply_nearest_anchor() -> None:
    overview_service = MagicMock()
    overview_service.get_overview = AsyncMock(
        return_value={
            "success": True,
            "positions": [
                _make_overview_position(),
                _make_overview_position(
                    market_type="KR",
                    symbol="005930",
                    name="삼성전자",
                    evaluation=50_000_000.0,
                    evaluation_krw=50_000_000.0,
                    current_price=70_000.0,
                    profit_rate=0.0,
                ),
            ],
            "facets": {"accounts": []},
        }
    )
    dashboard_service = MagicMock()
    dashboard_service.get_journals_batch = AsyncMock(
        return_value={
            "AAPL": {
                "target_price": 130.0,
                "target_distance_pct": 18.18,
                "stop_loss": 90.0,
                "stop_distance_pct": -18.18,
            }
        }
    )

    service = PortfolioDecisionService(
        overview_service=overview_service,
        dashboard_service=dashboard_service,
    )

    with (
        patch(
            "app.services.portfolio_decision_service._get_support_resistance_impl",
            AsyncMock(
                return_value={
                    "supports": [
                        {
                            "price": 108.0,
                            "distance_pct": -1.82,
                            "strength": "moderate",
                            "sources": ["volume_poc"],
                        }
                    ],
                    "resistances": [
                        {
                            "price": 112.0,
                            "distance_pct": 1.82,
                            "strength": "strong",
                            "sources": ["bb_upper"],
                        }
                    ],
                }
            ),
        ),
        patch(
            "app.services.portfolio_decision_service._get_indicators_impl",
            AsyncMock(return_value={"indicators": {"rsi": {"14": 65.0}}}),
        ),
    ):
        slate = await service.build_decision_slate(user_id=1)

    group = slate["symbol_groups"][0]
    item = next(item for item in group["items"] if item["action"] == "trim_candidate")
    assert item["action_price"] == 112.0
    assert item["anchor"]["price"] == 112.0
    assert group["support_resistance"]["nearest_support"]["price"] == 108.0
    assert group["support_resistance"]["nearest_resistance"]["price"] == 112.0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unavailable_support_resistance_does_not_create_zero_price_action() -> (
    None
):
    overview_service = MagicMock()
    overview_service.get_overview = AsyncMock(
        return_value={
            "success": True,
            "positions": [
                _make_overview_position(),
                _make_overview_position(
                    market_type="KR",
                    symbol="005930",
                    name="삼성전자",
                    evaluation=50_000_000.0,
                    evaluation_krw=50_000_000.0,
                    current_price=70_000.0,
                    profit_rate=0.0,
                ),
            ],
            "facets": {"accounts": []},
        }
    )
    dashboard_service = MagicMock()
    dashboard_service.get_journals_batch = AsyncMock(
        return_value={
            "AAPL": {
                "target_price": 130.0,
                "target_distance_pct": 18.18,
                "stop_loss": 90.0,
                "stop_distance_pct": -18.18,
            }
        }
    )

    service = PortfolioDecisionService(
        overview_service=overview_service,
        dashboard_service=dashboard_service,
    )

    with (
        patch(
            "app.services.portfolio_decision_service._get_support_resistance_impl",
            AsyncMock(return_value={"status": "unavailable"}),
        ),
        patch(
            "app.services.portfolio_decision_service._get_indicators_impl",
            AsyncMock(return_value={"indicators": {"rsi": {"14": 65.0}}}),
        ),
    ):
        slate = await service.build_decision_slate(user_id=1)

    actions = [item["action"] for item in slate["symbol_groups"][0]["items"]]
    assert actions == ["hold"]
    assert slate["symbol_groups"][0]["support_resistance"]["nearest_resistance"] is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_missing_price_payload_still_validates_and_counts_missing_context() -> (
    None
):
    overview_service = MagicMock()
    overview_service.get_overview = AsyncMock(
        return_value={
            "success": True,
            "positions": [
                _make_overview_position(
                    symbol="UNKNOWN",
                    name="Unknown Stock",
                    current_price=None,
                    evaluation=None,
                    evaluation_krw=None,
                    profit_loss=None,
                    profit_loss_krw=None,
                    profit_rate=None,
                )
            ],
            "facets": {"accounts": []},
        }
    )
    dashboard_service = MagicMock()
    dashboard_service.get_journals_batch = AsyncMock(return_value={})

    service = PortfolioDecisionService(
        overview_service=overview_service,
        dashboard_service=dashboard_service,
    )

    with (
        patch(
            "app.services.portfolio_decision_service._get_support_resistance_impl",
            AsyncMock(return_value={"status": "unavailable"}),
        ),
        patch(
            "app.services.portfolio_decision_service._get_indicators_impl",
            AsyncMock(return_value={"indicators": {}}),
        ),
    ):
        slate = await service.build_decision_slate(user_id=1)

    PortfolioDecisionSlateResponse.model_validate(slate)
    group = slate["symbol_groups"][0]
    assert group["position"]["current_price"] is None
    assert group["items"][0]["action"] == "manual_review"
    assert slate["summary"]["missing_context_items"] == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execution_boundary_marks_manual_or_mixed_accounts_for_review() -> None:
    overview_service = MagicMock()
    overview_service.get_overview = AsyncMock(
        return_value={
            "success": True,
            "positions": [
                _make_overview_position(
                    components=[
                        {"broker": "kis", "account_name": "Main", "source": "live"},
                        {"broker": "toss", "account_name": "Toss", "source": "manual"},
                    ]
                )
            ],
            "facets": {"accounts": []},
        }
    )
    dashboard_service = MagicMock()
    dashboard_service.get_journals_batch = AsyncMock(
        return_value={
            "AAPL": {
                "target_price": 112.0,
                "target_distance_pct": 1.82,
                "stop_loss": 90.0,
                "stop_distance_pct": -18.18,
            }
        }
    )

    service = PortfolioDecisionService(
        overview_service=overview_service,
        dashboard_service=dashboard_service,
    )

    with (
        patch(
            "app.services.portfolio_decision_service._get_support_resistance_impl",
            AsyncMock(return_value={"status": "unavailable"}),
        ),
        patch(
            "app.services.portfolio_decision_service._get_indicators_impl",
            AsyncMock(return_value={"indicators": {"rsi": {"14": 65.0}}}),
        ),
    ):
        slate = await service.build_decision_slate(user_id=1)

    boundary = slate["symbol_groups"][0]["items"][0]["execution_boundary"]
    assert boundary["channel"] == "manual_review"
    assert boundary["manual_only"] is True

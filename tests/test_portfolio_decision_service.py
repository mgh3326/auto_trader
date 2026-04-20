import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.portfolio_decision_run import PortfolioDecisionRun
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


def _make_service_for_positions(
    positions: list[dict],
    journals: dict[str, dict] | None = None,
    db=None,
) -> PortfolioDecisionService:
    overview_service = MagicMock()
    overview_service.get_overview = AsyncMock(
        return_value={
            "success": True,
            "positions": positions,
            "facets": {"accounts": []},
        }
    )
    dashboard_service = MagicMock()
    dashboard_service.get_journals_batch = AsyncMock(return_value=journals or {})
    kwargs = {
        "overview_service": overview_service,
        "dashboard_service": dashboard_service,
    }
    if db is not None:
        kwargs["db"] = db
    return PortfolioDecisionService(**kwargs)


class _FakePortfolioDecisionDb:
    def __init__(self, stored_run: PortfolioDecisionRun | None = None) -> None:
        self.add = MagicMock()
        self.commit = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = stored_run
        self.execute = AsyncMock(return_value=result)


def _available_context_patches():
    return (
        patch(
            "app.services.portfolio_decision_service._get_support_resistance_impl",
            AsyncMock(
                return_value={
                    "status": "available",
                    "nearest_support": {
                        "price": 100.0,
                        "distance_pct": -1.5,
                        "strength": "moderate",
                    },
                    "nearest_resistance": {
                        "price": 115.0,
                        "distance_pct": 3.0,
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
    )


def _aapl_and_samsung_positions() -> list[dict]:
    return [
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
    ]


def _aapl_journal_far_from_triggers() -> dict[str, dict]:
    return {
        "AAPL": {
            "target_price": 130.0,
            "target_distance_pct": 18.18,
            "stop_loss": 90.0,
            "stop_distance_pct": -18.18,
        }
    }


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
    assert slate["decision_run"]["generated_at"].tzinfo is not None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_same_symbol_different_markets_keep_separate_contexts() -> None:
    service = _make_service_for_positions(
        [
            _make_overview_position(
                market_type="US",
                symbol="ABC",
                name="US ABC",
                profit_rate=0.2,
            ),
            _make_overview_position(
                market_type="KR",
                symbol="ABC",
                name="KR ABC",
                current_price=70_000.0,
                profit_rate=0.2,
                evaluation=7_000_000.0,
                evaluation_krw=7_000_000.0,
            ),
        ],
        journals={},
    )

    async def support_resistance(symbol: str, *, market: str):
        if market == "US":
            return {
                "nearest_resistance": {"price": 112.0, "distance_pct": 1.2},
                "supports": [],
                "resistances": [],
            }
        return {
            "nearest_resistance": {"price": 71_000.0, "distance_pct": 1.4},
            "supports": [],
            "resistances": [],
        }

    with (
        patch(
            "app.services.portfolio_decision_service._get_support_resistance_impl",
            support_resistance,
        ),
        patch(
            "app.services.portfolio_decision_service._get_indicators_impl",
            AsyncMock(return_value={"indicators": {"rsi": {"14": 65.0}}}),
        ),
    ):
        slate = await service.build_decision_slate(user_id=1)

    groups = {
        (group["market_type"], group["symbol"]): group
        for group in slate["symbol_groups"]
    }
    assert groups[("US", "ABC")]["support_resistance"]["nearest_resistance"][
        "price"
    ] == pytest.approx(112.0)
    assert groups[("KR", "ABC")]["support_resistance"]["nearest_resistance"][
        "price"
    ] == pytest.approx(71_000.0)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_context_fetch_timeout_logs_warning_and_degrades(caplog) -> None:
    service = _make_service_for_positions([_make_overview_position()], journals={})
    service.context_timeout_seconds = 0.01

    async def slow_support_resistance(symbol: str, *, market: str):
        await asyncio.sleep(0.05)
        return {"status": "available"}

    with (
        patch(
            "app.services.portfolio_decision_service._get_support_resistance_impl",
            slow_support_resistance,
        ),
        patch(
            "app.services.portfolio_decision_service._get_indicators_impl",
            AsyncMock(return_value={"indicators": {"rsi": {"14": 65.0}}}),
        ),
        caplog.at_level("WARNING"),
    ):
        slate = await service.build_decision_slate(user_id=1)

    group = slate["symbol_groups"][0]
    assert group["support_resistance"]["status"] == "unavailable"
    assert "portfolio decision context fetch failed" in caplog.text
    assert "symbol=AAPL" in caplog.text
    assert "market=US" in caplog.text
    assert "call=support_resistance" in caplog.text


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
    service = _make_service_for_positions(
        _aapl_and_samsung_positions(),
        _aapl_journal_far_from_triggers(),
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
    assert item["action_price"] == pytest.approx(112.0)
    assert item["anchor"]["price"] == pytest.approx(112.0)
    assert group["support_resistance"]["nearest_support"]["price"] == pytest.approx(
        108.0
    )
    assert group["support_resistance"]["nearest_resistance"]["price"] == pytest.approx(
        112.0
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unavailable_support_resistance_does_not_create_zero_price_action() -> (
    None
):
    service = _make_service_for_positions(
        _aapl_and_samsung_positions(),
        _aapl_journal_far_from_triggers(),
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


@pytest.mark.unit
@pytest.mark.asyncio
async def test_create_decision_run_persists_crypto_snapshot_with_share_url() -> None:
    db = _FakePortfolioDecisionDb()
    service = _make_service_for_positions(
        [
            _make_overview_position(
                market_type="CRYPTO",
                symbol="KRW-BTC",
                name="Bitcoin",
                current_price=100.0,
                evaluation=1_000_000.0,
                evaluation_krw=1_000_000.0,
            )
        ],
        journals={"KRW-BTC": {"status": "active", "target_price": 120.0}},
        db=db,
    )

    with _available_context_patches()[0], _available_context_patches()[1]:
        slate = await service.create_decision_run(
            user_id=42,
            market="CRYPTO",
            account_keys=["upbit:main"],
            q="BTC",
        )

    PortfolioDecisionSlateResponse.model_validate(slate)
    run = slate["decision_run"]
    assert run["id"].startswith("decision-")
    assert run["persisted"] is True
    assert run["market_scope"] == "CRYPTO"
    assert run["share_url"] == f"/portfolio/decision?run_id={run['id']}"
    assert slate["filters"] == {
        "market": "CRYPTO",
        "account_keys": ["upbit:main"],
        "q": "BTC",
    }
    assert slate["symbol_groups"][0]["symbol"] == "KRW-BTC"

    db.add.assert_called_once()
    stored = db.add.call_args.args[0]
    assert isinstance(stored, PortfolioDecisionRun)
    assert stored.run_id == run["id"]
    assert stored.user_id == 42
    assert stored.market_scope == "CRYPTO"
    assert stored.mode == "analysis_only"
    assert stored.source == "portfolio_decision_service_v1"
    assert stored.filters == slate["filters"]
    assert stored.summary == slate["summary"]
    assert stored.symbol_groups == slate["symbol_groups"]
    assert stored.warnings == slate["warnings"]
    assert stored.payload == slate
    db.commit.assert_awaited_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_decision_run_returns_stored_payload_without_recalculating() -> None:
    payload = {
        "success": True,
        "decision_run": {
            "id": "decision-stored",
            "generated_at": "2026-04-20T10:00:00+00:00",
            "market_scope": "CRYPTO",
            "mode": "analysis_only",
            "persisted": True,
            "source": "portfolio_decision_service_v1",
            "share_url": "/portfolio/decision?run_id=decision-stored",
        },
        "filters": {"market": "CRYPTO", "account_keys": [], "q": None},
        "summary": {
            "symbols": 0,
            "decision_items": 0,
            "actionable_items": 0,
            "manual_review_items": 0,
            "auto_candidate_items": 0,
            "missing_context_items": 0,
            "by_action": {},
            "by_market": {},
        },
        "facets": {"accounts": []},
        "symbol_groups": [],
        "warnings": [],
    }
    stored_run = PortfolioDecisionRun(
        run_id="decision-stored",
        user_id=42,
        generated_at="2026-04-20T10:00:00+00:00",
        market_scope="CRYPTO",
        mode="analysis_only",
        source="portfolio_decision_service_v1",
        filters=payload["filters"],
        summary=payload["summary"],
        facets=payload["facets"],
        symbol_groups=payload["symbol_groups"],
        warnings=payload["warnings"],
        payload=payload,
        created_at="2026-04-20T10:00:00+00:00",
    )
    db = _FakePortfolioDecisionDb(stored_run)
    service = _make_service_for_positions([_make_overview_position()], db=db)

    result = await service.get_decision_run(user_id=42, run_id="decision-stored")

    assert result == payload
    PortfolioDecisionSlateResponse.model_validate(result)
    service.overview_service.get_overview.assert_not_awaited()
    db.execute.assert_awaited_once()
    statement = db.execute.call_args.args[0]
    assert str(statement.compile(compile_kwargs={"literal_binds": True})).count(
        "portfolio_decision_runs"
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_decision_run_rejects_missing_run() -> None:
    from app.services.portfolio_decision_service import (
        PortfolioDecisionRunNotFoundError,
    )

    db = _FakePortfolioDecisionDb(None)
    service = _make_service_for_positions([_make_overview_position()], db=db)

    with pytest.raises(PortfolioDecisionRunNotFoundError):
        await service.get_decision_run(user_id=42, run_id="missing-run")

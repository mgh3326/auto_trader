"""ROB-1310 read-path and snapshot regressions."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest

from app.services.brokers.toss.dto import TossHoldingItem, TossHoldings
from app.services.toss_portfolio_service import fetch_toss_portfolio_snapshot


def _holding() -> TossHoldingItem:
    return TossHoldingItem(
        symbol="AAPL",
        name="Apple",
        market_country="US",
        currency="USD",
        quantity=Decimal("10"),
        last_price=Decimal("110"),
        average_purchase_price=Decimal("100"),
        market_value={"amount": Decimal("1100")},
        profit_loss={"amount": Decimal("100"), "rate": Decimal("0.1")},
        daily_profit_loss={},
        cost={},
    )


class _CountingTossClient:
    def __init__(self, started: asyncio.Event | None = None) -> None:
        self.holdings_calls = 0
        self.sellable_calls = 0
        self._started = started

    async def holdings(self) -> TossHoldings:
        self.holdings_calls += 1
        if self._started is not None:
            self._started.set()
        return TossHoldings(items=[_holding()])

    async def sellable_quantity(self, *, symbol: str):
        self.sellable_calls += 1
        raise AssertionError(
            f"general snapshot must not call sellable_quantity: {symbol}"
        )

    async def buying_power(self, *, currency: str):
        return SimpleNamespace(currency=currency, cash_buying_power=Decimal("100"))

    async def aclose(self) -> None:
        return None


@pytest.mark.asyncio
async def test_general_snapshot_default_never_calls_toss_sellable_endpoint() -> None:
    client = _CountingTossClient()

    snapshot = await fetch_toss_portfolio_snapshot(client=client)

    assert client.holdings_calls == 1
    assert client.sellable_calls == 0
    assert snapshot.positions[0].sellable_quantity is None


@pytest.mark.asyncio
async def test_process_shared_snapshot_singleflight_deduplicates_concurrent_fetches() -> (
    None
):
    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    first_started = asyncio.Event()
    first = _CountingTossClient(started=first_started)
    second = _CountingTossClient()

    # The implementation must accept two independent process-local cache
    # facades over the same Redis store; a module-local dict alone is not enough.
    from app.services.toss_portfolio_snapshot_cache import TossPortfolioSnapshotCache

    cache_a = TossPortfolioSnapshotCache(redis_client=redis_client, ttl_seconds=30)
    cache_b = TossPortfolioSnapshotCache(redis_client=redis_client, ttl_seconds=30)

    first_task = asyncio.create_task(
        fetch_toss_portfolio_snapshot(
            client=first,
            need_cash=False,
            snapshot_cache=cache_a,
            use_shared_snapshot=True,
        )
    )
    await first_started.wait()
    second_task = asyncio.create_task(
        fetch_toss_portfolio_snapshot(
            client=second,
            need_cash=False,
            snapshot_cache=cache_b,
            use_shared_snapshot=True,
        )
    )
    await asyncio.gather(first_task, second_task)

    assert first.holdings_calls == 1
    assert second.holdings_calls == 0
    assert first.sellable_calls == 0
    assert second.sellable_calls == 0


@pytest.mark.asyncio
async def test_briefing_uses_bounded_summary_instead_of_full_holdings(monkeypatch):
    from app.mcp_server.tooling import operating_briefing as briefing

    async def fail_full_holdings(**kwargs):
        raise AssertionError("briefing must not call full holdings")

    async def summary(**kwargs):
        assert kwargs["include_current_price"] is False
        return {
            "filters": {},
            "total_accounts": 1,
            "total_positions": 1,
            "summary": {"position_count": 1},
            "top_movers": [],
            "accounts": [
                {
                    "account": "kis",
                    "account_name": "KIS",
                    "account_mode": "kis_live",
                    "order_routable": True,
                    "position_count": 1,
                }
            ],
            "held_pairs": [("kr", "005930")],
            "errors": [],
        }

    class _DbContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, exc_type, exc, tb):
            return None

    class _Pending:
        orders = []
        as_of = "2026-08-20T00:00:00+00:00"
        freshness_status = "fresh"
        unavailable_reason = None

    monkeypatch.setattr(briefing, "_get_holdings_impl", fail_full_holdings)
    monkeypatch.setattr(briefing, "_get_portfolio_summary_impl", summary, raising=False)
    monkeypatch.setattr(briefing, "AsyncSessionLocal", lambda: _DbContext())
    monkeypatch.setattr(
        briefing,
        "collect_pending_orders_snapshot",
        AsyncMock(return_value=_Pending()),
    )
    monkeypatch.setattr(
        briefing,
        "_latest_report_summary",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        briefing,
        "_recent_session_context",
        AsyncMock(return_value={"count": 0, "entries": []}),
    )
    monkeypatch.setattr(
        briefing,
        "_recent_analysis_artifacts",
        AsyncMock(return_value={"count": 0, "artifacts": []}),
    )
    monkeypatch.setattr(
        briefing,
        "load_negative_class_health",
        AsyncMock(return_value=SimpleNamespace(to_dict=lambda: {"status": "ok"})),
    )
    monkeypatch.setattr(
        briefing,
        "list_active_watches_impl",
        AsyncMock(return_value={"count": 0, "active_watches": []}),
    )
    monkeypatch.setattr(
        briefing, "get_account_costs_setting", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(briefing, "policy_version_stamp", lambda: {"version": 1})

    result = await briefing.get_operating_briefing_impl(
        market="kr",
        account_scope="kis_live",
    )

    assert result["success"] is True
    assert result["holdings"]["total_positions"] == 1

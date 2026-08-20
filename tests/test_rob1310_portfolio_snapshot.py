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
async def test_process_shared_snapshot_waits_for_slow_live_owner_without_duplicate_fetch():
    from app.services.toss_portfolio_snapshot_cache import TossPortfolioSnapshotCache

    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    cache_a = TossPortfolioSnapshotCache(
        redis_client=redis_client,
        ttl_seconds=30,
        lock_ttl_seconds=10,
        wait_timeout_seconds=3,
        poll_interval_seconds=0.01,
    )
    cache_b = TossPortfolioSnapshotCache(
        redis_client=redis_client,
        ttl_seconds=30,
        lock_ttl_seconds=10,
        wait_timeout_seconds=3,
        poll_interval_seconds=0.01,
    )
    owner_started = asyncio.Event()
    calls = {"owner": 0, "waiter": 0}

    async def owner_fetch() -> dict[str, object]:
        calls["owner"] += 1
        owner_started.set()
        # The existing cash path can exceed the default 3-second waiter budget.
        await asyncio.sleep(3.2)
        return {"held_pairs": [["us", "AAPL"]]}

    async def waiter_fetch() -> dict[str, object]:
        calls["waiter"] += 1
        return {"held_pairs": [["us", "AAPL"]]}

    owner_task = asyncio.create_task(cache_a.get_or_fetch("scope", owner_fetch))
    await owner_started.wait()
    waiter_task = asyncio.create_task(cache_b.get_or_fetch("scope", waiter_fetch))

    owner_payload, waiter_payload = await asyncio.gather(owner_task, waiter_task)

    assert owner_payload == waiter_payload == {"held_pairs": [["us", "AAPL"]]}
    assert calls == {"owner": 1, "waiter": 0}


@pytest.mark.asyncio
async def test_snapshot_cache_error_falls_back_to_direct_read_without_sellable() -> (
    None
):
    from app.services.toss_portfolio_snapshot_cache import TossPortfolioSnapshotCache

    class _BrokenRedis:
        async def get(self, _key):
            raise RuntimeError("redis unavailable")

        async def set(self, *_args, **_kwargs):
            raise RuntimeError("redis unavailable")

    client = _CountingTossClient()
    cache = TossPortfolioSnapshotCache(
        redis_client=_BrokenRedis(),
        ttl_seconds=30,
        wait_timeout_seconds=0,
    )

    snapshot = await fetch_toss_portfolio_snapshot(
        client=client,
        need_cash=False,
        snapshot_cache=cache,
        use_shared_snapshot=True,
    )

    assert client.holdings_calls == 1
    assert client.sellable_calls == 0
    assert snapshot.positions[0].sellable_quantity is None


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

    monkeypatch.setattr(
        briefing, "_get_holdings_impl", fail_full_holdings, raising=False
    )
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


@pytest.mark.asyncio
async def test_holdings_home_and_briefing_share_one_slow_whole_snapshot_owner(
    monkeypatch,
):
    from app.mcp_server.tooling import portfolio_holdings
    from app.schemas.invest_home import Holding
    from app.services.invest_home_service import InvestHomeService, _SourceFetchResult
    from app.services.portfolio_snapshot_cache import PortfolioSnapshotCache

    calls = {"kis": 0, "upbit": 0, "toss_api": 0, "manual": 0}
    owner_started = asyncio.Event()

    class _Reader:
        def __init__(self, source: str, holding: Holding | None = None):
            self.source = source
            self.holding = holding

        async def fetch(self, *, user_id):
            calls[self.source] += 1
            if self.source == "kis":
                owner_started.set()
                await asyncio.sleep(3.2)
            return _SourceFetchResult(
                accounts=[], holdings=[self.holding] if self.holding else []
            )

    holding = Holding(
        holdingId="kis:005930",
        accountId="kis-account",
        source="kis",
        accountKind="live",
        symbol="005930",
        market="KR",
        assetType="equity",
        assetCategory="kr_stock",
        displayName="Samsung",
        quantity=10,
        averageCost=70000,
        costBasis=700000,
        currency="KRW",
        valueNative=720000,
        valueKrw=720000,
        pnlKrw=20000,
        pnlRate=20000 / 700000,
    )
    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    cache_a = PortfolioSnapshotCache(
        redis_client=redis_client,
        ttl_seconds=30,
        lock_ttl_seconds=10,
        wait_timeout_seconds=3,
        poll_interval_seconds=0.01,
    )
    cache_b = PortfolioSnapshotCache(
        redis_client=redis_client,
        ttl_seconds=30,
        lock_ttl_seconds=10,
        wait_timeout_seconds=3,
        poll_interval_seconds=0.01,
    )
    service = InvestHomeService(
        kis_reader=_Reader("kis", holding),
        upbit_reader=_Reader("upbit"),
        manual_reader=_Reader("manual"),
        toss_api_reader=_Reader("toss_api"),
        snapshot_cache=cache_a,
    )
    monkeypatch.setattr(
        portfolio_holdings,
        "get_shared_portfolio_snapshot_cache",
        lambda: cache_b,
        raising=False,
    )

    home_task = asyncio.create_task(service.get_home(user_id=1))
    await owner_started.wait()
    summary_task = asyncio.create_task(
        portfolio_holdings._get_portfolio_summary_impl(user_id=1)
    )
    home, summary = await asyncio.gather(home_task, summary_task)

    assert home.holdings[0].symbol == "005930"
    assert summary["total_positions"] == 1
    assert summary["held_pairs"] == [("kr", "005930")]
    assert calls == {"kis": 1, "upbit": 1, "toss_api": 1, "manual": 1}


@pytest.mark.asyncio
async def test_briefing_summary_reuses_whole_snapshot_without_source_recollection(
    monkeypatch,
):
    from app.mcp_server.tooling import portfolio_holdings
    from app.services.invest_home_service import InvestHomeService, _SourceFetchResult
    from app.services.portfolio_snapshot_cache import PortfolioSnapshotCache

    class _Reader:
        async def fetch(self, *, user_id):
            from app.schemas.invest_home import Holding

            return _SourceFetchResult(
                accounts=[],
                holdings=[
                    Holding(
                        holdingId="kis:005930",
                        accountId="kis-account",
                        source="kis",
                        accountKind="live",
                        symbol="005930",
                        market="KR",
                        assetType="equity",
                        assetCategory="kr_stock",
                        displayName="Samsung",
                        quantity=10,
                        averageCost=70000,
                        costBasis=700000,
                        currency="KRW",
                        valueNative=720000,
                        valueKrw=720000,
                        pnlKrw=20000,
                        pnlRate=20000 / 700000,
                    )
                ],
            )

    class _EmptyReader:
        async def fetch(self, *, user_id):
            return _SourceFetchResult(accounts=[], holdings=[])

    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    cache = PortfolioSnapshotCache(redis_client=redis_client, ttl_seconds=30)
    service = InvestHomeService(
        kis_reader=_Reader(),
        upbit_reader=_EmptyReader(),
        manual_reader=_EmptyReader(),
        snapshot_cache=cache,
    )
    await service.get_home(user_id=1)

    monkeypatch.setattr(
        portfolio_holdings,
        "get_shared_portfolio_snapshot_cache",
        lambda: cache,
        raising=False,
    )

    async def fail_source(*args, **kwargs):
        raise AssertionError("briefing must not recollect source readers")

    for name in (
        "_collect_kis_positions",
        "_collect_upbit_positions",
        "_collect_manual_positions",
        "_collect_toss_api_positions",
    ):
        monkeypatch.setattr(portfolio_holdings, name, fail_source)

    result = await portfolio_holdings._get_portfolio_summary_impl(user_id=1)

    assert result["total_positions"] == 1
    assert result["held_pairs"] == [("kr", "005930")]

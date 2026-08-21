"""ROB-1310 read-path and snapshot regressions."""

from __future__ import annotations

import asyncio
import importlib.util
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
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
    from app.core.config import settings
    from app.mcp_server.tooling import operating_briefing, portfolio_holdings
    from app.routers import invest_api
    from app.schemas.invest_home import Holding
    from app.services import invest_home_readers
    from app.services.invest_home_service import _SourceFetchResult

    # ROB-1310's initial partial-Toss implementation has no whole-cache
    # module. Detect that optional module without turning baseline collection
    # into an ImportError; the same production entrypoints must still run and
    # RED on the upstream call-count assertion below.
    whole_snapshot = None
    if importlib.util.find_spec("app.services.portfolio_snapshot_cache") is not None:
        from app.services import portfolio_snapshot_cache as whole_snapshot

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

    monkeypatch.setattr(settings, "toss_api_enabled", True)
    monkeypatch.setattr(invest_home_readers, "SafeKISClient", lambda: SimpleNamespace())
    monkeypatch.setattr(
        invest_home_readers,
        "KISHomeReader",
        lambda db: _Reader("kis", holding),
    )
    monkeypatch.setattr(
        invest_home_readers,
        "UpbitHomeReader",
        lambda db: _Reader("upbit"),
    )
    monkeypatch.setattr(
        invest_home_readers,
        "ManualHomeReader",
        lambda db, quote_service=None: _Reader("manual"),
    )
    monkeypatch.setattr(
        invest_home_readers,
        "TossApiHomeReader",
        lambda: _Reader("toss_api"),
    )

    if whole_snapshot is not None:
        redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
        cache_a = whole_snapshot.PortfolioSnapshotCache(
            redis_client=redis_client,
            ttl_seconds=30,
            lock_ttl_seconds=10,
            wait_timeout_seconds=3,
            poll_interval_seconds=0.01,
        )
        cache_b = whole_snapshot.PortfolioSnapshotCache(
            redis_client=redis_client,
            ttl_seconds=30,
            lock_ttl_seconds=10,
            wait_timeout_seconds=3,
            poll_interval_seconds=0.01,
        )
        monkeypatch.setattr(
            whole_snapshot,
            "get_shared_portfolio_snapshot_cache",
            lambda: cache_a,
        )
        monkeypatch.setattr(
            portfolio_holdings,
            "get_shared_portfolio_snapshot_cache",
            lambda: cache_b,
            raising=False,
        )

    def _position(source: str, broker: str) -> dict[str, object]:
        return {
            "account": broker,
            "account_name": "기본 계좌",
            "broker": broker,
            "source": source,
            "instrument_type": "equity_kr",
            "market": "kr",
            "symbol": "005930",
            "name": "Samsung",
            "quantity": 10.0,
            "avg_buy_price": 70000.0,
            "current_price": 72000.0,
            "evaluation_amount": 720000.0,
            "profit_loss": 20000.0,
            "profit_rate": 0.028,
        }

    async def _collect_kis(*args, **kwargs):
        calls["kis"] += 1
        return [_position("kis_api", "kis")], []

    async def _collect_upbit(*args, **kwargs):
        calls["upbit"] += 1
        return [], []

    async def _collect_manual(*args, **kwargs):
        calls["manual"] += 1
        return [], []

    async def _collect_toss(*args, **kwargs):
        calls["toss_api"] += 1
        return [], [], True

    monkeypatch.setattr(portfolio_holdings, "_collect_kis_positions", _collect_kis)
    monkeypatch.setattr(portfolio_holdings, "_collect_upbit_positions", _collect_upbit)
    monkeypatch.setattr(
        portfolio_holdings, "_collect_manual_positions", _collect_manual
    )
    monkeypatch.setattr(
        portfolio_holdings, "_collect_toss_api_positions", _collect_toss
    )

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

    monkeypatch.setattr(operating_briefing, "AsyncSessionLocal", lambda: _DbContext())
    monkeypatch.setattr(
        operating_briefing,
        "collect_pending_orders_snapshot",
        AsyncMock(return_value=_Pending()),
    )
    monkeypatch.setattr(
        operating_briefing, "_latest_report_summary", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        operating_briefing,
        "_recent_session_context",
        AsyncMock(return_value={"count": 0, "entries": []}),
    )
    monkeypatch.setattr(
        operating_briefing,
        "_recent_analysis_artifacts",
        AsyncMock(return_value={"count": 0, "artifacts": []}),
    )
    monkeypatch.setattr(
        operating_briefing,
        "load_negative_class_health",
        AsyncMock(return_value=SimpleNamespace(to_dict=lambda: {"status": "ok"})),
    )
    monkeypatch.setattr(
        operating_briefing,
        "list_active_watches_impl",
        AsyncMock(return_value={"count": 0, "active_watches": []}),
    )
    monkeypatch.setattr(
        operating_briefing, "get_account_costs_setting", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        operating_briefing, "policy_version_stamp", lambda: {"version": 1}
    )

    service = invest_api.get_invest_home_service(db=object())
    home_task = asyncio.create_task(
        invest_api.get_home(
            user=SimpleNamespace(id=1),
            service=service,
            include_paper=False,
            paper_sources=None,
        )
    )
    await owner_started.wait()
    holdings_task = asyncio.create_task(
        portfolio_holdings._get_holdings_impl(include_current_price=False)
    )
    briefing_task = asyncio.create_task(
        operating_briefing.get_operating_briefing_impl(
            market="kr", account_scope="kis_live"
        )
    )
    home, holdings, briefing = await asyncio.gather(
        home_task, holdings_task, briefing_task
    )

    assert home.holdings[0].symbol == "005930"
    assert holdings["total_positions"] == 1
    assert briefing["success"] is True
    # Partial-Toss composition calls the same upstreams from all three
    # production entrypoints; whole snapshot ownership reduces every source to
    # one call even across independent Redis facades.
    assert calls == {"kis": 1, "upbit": 1, "toss_api": 1, "manual": 1}


@pytest.mark.asyncio
async def test_holdings_home_and_briefing_share_one_healthy_six_second_owner_without_raw_timeout(
    monkeypatch,
):
    """ROB-1310 BLOCKER-2: Sentry measured the whole-portfolio cold compose
    regime at get_holdings 14.36s / get_operating_briefing p95 16.28s. A
    healthy owner that takes 6s (well under that measured regime, well over
    the old ~4s waiter budget) must not make concurrent /invest home or MCP
    holdings/briefing callers raise a raw/unhandled TimeoutError. This uses
    the real configured ``settings.portfolio_snapshot_cache_wait_seconds``
    default rather than overriding it, so the default itself is pinned as
    sufficient for the measured regime.
    """
    from app.core.config import settings
    from app.mcp_server.tooling import operating_briefing, portfolio_holdings
    from app.routers import invest_api
    from app.schemas.invest_home import Holding
    from app.services import invest_home_readers
    from app.services import portfolio_snapshot_cache as whole_snapshot
    from app.services.invest_home_service import _SourceFetchResult

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
                await asyncio.sleep(6.0)
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

    monkeypatch.setattr(settings, "toss_api_enabled", True)
    monkeypatch.setattr(invest_home_readers, "SafeKISClient", lambda: SimpleNamespace())
    monkeypatch.setattr(
        invest_home_readers,
        "KISHomeReader",
        lambda db: _Reader("kis", holding),
    )
    monkeypatch.setattr(
        invest_home_readers,
        "UpbitHomeReader",
        lambda db: _Reader("upbit"),
    )
    monkeypatch.setattr(
        invest_home_readers,
        "ManualHomeReader",
        lambda db, quote_service=None: _Reader("manual"),
    )
    monkeypatch.setattr(
        invest_home_readers,
        "TossApiHomeReader",
        lambda: _Reader("toss_api"),
    )

    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    cache_a = whole_snapshot.PortfolioSnapshotCache(
        redis_client=redis_client,
        ttl_seconds=30,
        lock_ttl_seconds=10,
        wait_timeout_seconds=settings.portfolio_snapshot_cache_wait_seconds,
        poll_interval_seconds=0.01,
    )
    cache_b = whole_snapshot.PortfolioSnapshotCache(
        redis_client=redis_client,
        ttl_seconds=30,
        lock_ttl_seconds=10,
        wait_timeout_seconds=settings.portfolio_snapshot_cache_wait_seconds,
        poll_interval_seconds=0.01,
    )
    monkeypatch.setattr(
        whole_snapshot,
        "get_shared_portfolio_snapshot_cache",
        lambda: cache_a,
    )
    monkeypatch.setattr(
        portfolio_holdings,
        "get_shared_portfolio_snapshot_cache",
        lambda: cache_b,
        raising=False,
    )

    def _position(source: str, broker: str) -> dict[str, object]:
        return {
            "account": broker,
            "account_name": "기본 계좌",
            "broker": broker,
            "source": source,
            "instrument_type": "equity_kr",
            "market": "kr",
            "symbol": "005930",
            "name": "Samsung",
            "quantity": 10.0,
            "avg_buy_price": 70000.0,
            "current_price": 72000.0,
            "evaluation_amount": 720000.0,
            "profit_loss": 20000.0,
            "profit_rate": 0.028,
        }

    async def _collect_kis(*args, **kwargs):
        calls["kis"] += 1
        return [_position("kis_api", "kis")], []

    async def _collect_upbit(*args, **kwargs):
        calls["upbit"] += 1
        return [], []

    async def _collect_manual(*args, **kwargs):
        calls["manual"] += 1
        return [], []

    async def _collect_toss(*args, **kwargs):
        calls["toss_api"] += 1
        return [], [], True

    monkeypatch.setattr(portfolio_holdings, "_collect_kis_positions", _collect_kis)
    monkeypatch.setattr(portfolio_holdings, "_collect_upbit_positions", _collect_upbit)
    monkeypatch.setattr(
        portfolio_holdings, "_collect_manual_positions", _collect_manual
    )
    monkeypatch.setattr(
        portfolio_holdings, "_collect_toss_api_positions", _collect_toss
    )

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

    monkeypatch.setattr(operating_briefing, "AsyncSessionLocal", lambda: _DbContext())
    monkeypatch.setattr(
        operating_briefing,
        "collect_pending_orders_snapshot",
        AsyncMock(return_value=_Pending()),
    )
    monkeypatch.setattr(
        operating_briefing, "_latest_report_summary", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        operating_briefing,
        "_recent_session_context",
        AsyncMock(return_value={"count": 0, "entries": []}),
    )
    monkeypatch.setattr(
        operating_briefing,
        "_recent_analysis_artifacts",
        AsyncMock(return_value={"count": 0, "artifacts": []}),
    )
    monkeypatch.setattr(
        operating_briefing,
        "load_negative_class_health",
        AsyncMock(return_value=SimpleNamespace(to_dict=lambda: {"status": "ok"})),
    )
    monkeypatch.setattr(
        operating_briefing,
        "list_active_watches_impl",
        AsyncMock(return_value={"count": 0, "active_watches": []}),
    )
    monkeypatch.setattr(
        operating_briefing, "get_account_costs_setting", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        operating_briefing, "policy_version_stamp", lambda: {"version": 1}
    )

    service = invest_api.get_invest_home_service(db=object())
    home_task = asyncio.create_task(
        invest_api.get_home(
            user=SimpleNamespace(id=1),
            service=service,
            include_paper=False,
            paper_sources=None,
        )
    )
    await owner_started.wait()
    holdings_task = asyncio.create_task(
        portfolio_holdings._get_holdings_impl(include_current_price=False)
    )
    briefing_task = asyncio.create_task(
        operating_briefing.get_operating_briefing_impl(
            market="kr", account_scope="kis_live"
        )
    )
    home, holdings, briefing = await asyncio.gather(
        home_task, holdings_task, briefing_task
    )

    assert home.holdings[0].symbol == "005930"
    assert holdings["total_positions"] == 1
    assert briefing["success"] is True
    assert calls == {"kis": 1, "upbit": 1, "toss_api": 1, "manual": 1}


@pytest.mark.asyncio
async def test_home_router_translates_hung_owner_hard_bound_to_sanitized_503(
    monkeypatch,
):
    """ROB-1310 BLOCKER-2: a renewing-but-hung owner remains finitely bounded
    (see test_process_shared_snapshot_renewing_hung_owner_has_bounded_wait),
    but the resulting failure must never leak a raw TimeoutError/generic 500
    out of /invest/api/home. The router must translate it into a sanitized
    typed 503 whose detail does not echo the original exception's text.
    """
    from fastapi import HTTPException

    from app.routers import invest_api
    from app.services.invest_home_service import InvestHomeService

    class _Reader:
        async def fetch(self, *, user_id):
            from app.services.invest_home_service import _SourceFetchResult

            return _SourceFetchResult(accounts=[], holdings=[])

    class _HungCache:
        usable = True

        async def get_or_fetch(self, scope, fetcher):
            raise TimeoutError(
                "portfolio snapshot owner did not complete: internal-secret-token"
            )

        async def delete(self, scope, *, expected_payload=None):
            return False

    service = InvestHomeService(
        kis_reader=_Reader(),
        upbit_reader=_Reader(),
        manual_reader=_Reader(),
        snapshot_cache=_HungCache(),
    )

    with pytest.raises(HTTPException) as excinfo:
        await invest_api.get_home(
            user=SimpleNamespace(id=1),
            service=service,
            include_paper=False,
            paper_sources=None,
        )

    assert excinfo.value.status_code == 503
    detail_text = str(excinfo.value.detail)
    assert "internal-secret-token" not in detail_text


@pytest.mark.asyncio
async def test_mcp_whole_portfolio_collect_translates_hung_owner_hard_bound(
    monkeypatch,
):
    """ROB-1310 BLOCKER-2: the MCP whole-portfolio collector must surface the
    same sanitized typed availability outcome as the HTTP router, not a raw
    TimeoutError, when a hung owner exceeds the hard wait bound.
    """
    from app.mcp_server.tooling import portfolio_holdings
    from app.services.invest_home_service import PortfolioSnapshotUnavailableError

    class _HungCache:
        async def get_or_fetch(self, scope, fetcher):
            raise TimeoutError(
                "portfolio snapshot owner did not complete: internal-secret-token"
            )

        async def delete(self, scope, *, expected_payload=None):
            return False

    with pytest.raises(PortfolioSnapshotUnavailableError) as excinfo:
        await portfolio_holdings._collect_whole_portfolio_positions(
            cache=_HungCache(), user_id=1
        )

    assert "internal-secret-token" not in str(excinfo.value)


@pytest.mark.asyncio
async def test_calendar_entrypoint_reads_held_snapshot_without_full_reader_calls(
    monkeypatch,
):
    from datetime import date

    from app.core.config import settings
    from app.routers import invest_api
    from app.services import invest_home_readers
    from app.services.invest_home_service import _SourceFetchResult

    whole_snapshot = None
    if importlib.util.find_spec("app.services.portfolio_snapshot_cache") is not None:
        from app.services import portfolio_snapshot_cache as whole_snapshot

    calls = {"kis": 0, "upbit": 0, "toss_api": 0, "manual": 0}

    class _Reader:
        def __init__(self, source: str):
            self.source = source

        async def fetch(self, *, user_id):
            calls[self.source] += 1
            return _SourceFetchResult(accounts=[], holdings=[])

    monkeypatch.setattr(settings, "toss_api_enabled", True)
    monkeypatch.setattr(invest_home_readers, "SafeKISClient", lambda: SimpleNamespace())
    monkeypatch.setattr(invest_home_readers, "KISHomeReader", lambda db: _Reader("kis"))
    monkeypatch.setattr(
        invest_home_readers, "UpbitHomeReader", lambda db: _Reader("upbit")
    )
    monkeypatch.setattr(
        invest_home_readers,
        "ManualHomeReader",
        lambda db, quote_service=None: _Reader("manual"),
    )
    monkeypatch.setattr(
        invest_home_readers, "TossApiHomeReader", lambda: _Reader("toss_api")
    )

    if whole_snapshot is not None:
        redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
        cache = whole_snapshot.PortfolioSnapshotCache(
            redis_client=redis_client, ttl_seconds=30
        )
        scope = whole_snapshot.portfolio_snapshot_scope(
            user_id=1, include_paper=False, paper_sources=None
        )
        await cache.put(
            scope,
            {"schema_version": 1, "held_pairs": [["kr", "005930"]]},
        )
        monkeypatch.setattr(
            whole_snapshot,
            "get_shared_portfolio_snapshot_cache",
            lambda: cache,
        )

    monkeypatch.setattr(
        invest_api,
        "build_relation_resolver",
        AsyncMock(return_value=object()),
    )
    monkeypatch.setattr(
        invest_api,
        "build_calendar",
        AsyncMock(return_value={"ok": True}),
    )

    service = invest_api.get_invest_home_service(db=object())
    result = await invest_api.get_calendar(
        user=SimpleNamespace(id=1),
        service=service,
        db=object(),
        from_date=date(2026, 8, 20),
        to_date=date(2026, 8, 21),
        tab="all",
        include_paper=False,
        paper_sources=None,
    )

    assert result == {"ok": True}
    assert calls == {"kis": 0, "upbit": 0, "toss_api": 0, "manual": 0}


@pytest.mark.asyncio
async def test_briefing_summary_reuses_whole_snapshot_without_source_recollection(
    monkeypatch,
):
    from app.mcp_server.tooling import portfolio_holdings
    from app.services.invest_home_service import InvestHomeService, _SourceFetchResult

    whole_snapshot = pytest.importorskip("app.services.portfolio_snapshot_cache")
    PortfolioSnapshotCache = whole_snapshot.PortfolioSnapshotCache

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


def _legacy_projection_home_response():
    from app.schemas.invest_home import (
        Account,
        CashAmounts,
        Holding,
        HomeSummary,
        InvestHomeResponse,
    )

    def account(
        account_id: str,
        display_name: str,
        source: str,
        account_kind: str = "live",
    ) -> Account:
        return Account(
            accountId=account_id,
            displayName=display_name,
            source=source,
            accountKind=account_kind,
            includedInHome=True,
            valueKrw=1_000_000,
            cashBalances=CashAmounts(),
            buyingPower=CashAmounts(),
        )

    return InvestHomeResponse(
        homeSummary=HomeSummary(
            includedSources=["kis", "upbit", "toss_manual"],
            excludedSources=[],
            totalValueKrw=1_000_000,
        ),
        accounts=[
            account("kis_account", "KIS", "kis"),
            account("upbit_account", "Upbit", "upbit"),
            account("toss_manual_account", "Toss 수동", "toss_manual", "manual"),
        ],
        holdings=[
            Holding(
                holdingId="kis:kr:005930",
                accountId="kis_account",
                source="kis",
                accountKind="live",
                symbol="005930",
                market="KR",
                assetType="equity",
                assetCategory="kr_stock",
                displayName="Samsung",
                quantity=10,
                averageCost=70_000,
                costBasis=700_000,
                currency="KRW",
                valueNative=705_000,
                valueKrw=705_000,
                pnlKrw=5_000,
                pnlRate=0.00714,
                sellableQuantity=7,
                pendingSellQuantity=3,
            ),
            Holding(
                holdingId="kis:us:AAPL",
                accountId="kis_account",
                source="kis",
                accountKind="live",
                symbol="AAPL",
                market="US",
                assetType="equity",
                assetCategory="us_stock",
                displayName="Apple",
                quantity=1,
                averageCost=100,
                costBasis=100,
                currency="USD",
                valueNative=110,
                valueKrw=14300,
                pnlKrw=1300,
                pnlRate=0.1,
            ),
            Holding(
                holdingId="upbit:BTC",
                accountId="upbit_account",
                source="upbit",
                accountKind="live",
                symbol="BTC",
                market="CRYPTO",
                assetType="crypto",
                assetCategory="crypto",
                displayName="BTC",
                quantity=2,
                averageCost=100,
                costBasis=200,
                currency="KRW",
                valueNative=220,
                valueKrw=220,
                pnlKrw=20,
                pnlRate=0.1,
            ),
            Holding(
                holdingId="manual:99",
                accountId="42",
                source="toss_manual",
                accountKind="manual",
                symbol="035930",
                market="KR",
                assetType="equity",
                assetCategory="kr_stock",
                displayName="Manual Kakao",
                quantity=1,
                averageCost=50_000,
                costBasis=50_000,
                currency="KRW",
                valueNative=55_000,
                valueKrw=55_000,
                pnlKrw=5_000,
                pnlRate=0.1,
            ),
        ],
        groupedHoldings=[],
    )


@pytest.mark.unit
def test_cached_home_projection_preserves_legacy_mcp_contracts() -> None:
    from app.services.portfolio_snapshot import portfolio_snapshot_to_mcp_positions

    positions = portfolio_snapshot_to_mcp_positions(_legacy_projection_home_response())
    by_symbol = {position["symbol"]: position for position in positions}

    kis_kr = by_symbol["005930"]
    assert kis_kr["account"] == "kis"
    assert kis_kr["profit_rate"] == pytest.approx(0.714)

    kis_us = by_symbol["AAPL"]
    assert kis_us["account"] == "kis"
    assert kis_us["profit_loss"] == pytest.approx(10.0)
    assert kis_us["profit_rate"] == pytest.approx(10.0)

    upbit = by_symbol["KRW-BTC"]
    assert upbit["account"] == "upbit"

    manual = by_symbol["035930"]
    assert manual["account"] == "toss_수동"
    assert manual["account_name"] == "Toss 수동"


def _source_contract_home_response():
    from app.schemas.invest_home import Account, CashAmounts, Holding

    response = _legacy_projection_home_response()
    response.accounts.extend(
        [
            Account(
                accountId="101",
                displayName="Broker Alpha",
                source="toss_manual",
                accountKind="manual",
                includedInHome=True,
                valueKrw=55_000,
                cashBalances=CashAmounts(),
                buyingPower=CashAmounts(),
            ),
            Account(
                accountId="102",
                displayName="Broker Beta",
                source="toss_manual",
                accountKind="manual",
                includedInHome=True,
                valueKrw=65_000,
                cashBalances=CashAmounts(),
                buyingPower=CashAmounts(),
            ),
            Account(
                accountId="toss_api_account",
                displayName="Toss",
                source="toss_api",
                accountKind="live",
                includedInHome=True,
                valueKrw=143_000,
                cashBalances=CashAmounts(),
                buyingPower=CashAmounts(),
            ),
        ]
    )
    response.holdings.extend(
        [
            Holding(
                holdingId="kis:us:MSFT",
                accountId="kis_account",
                source="kis",
                accountKind="live",
                symbol="MSFT",
                market="US",
                assetType="equity",
                assetCategory="us_stock",
                displayName="Microsoft",
                quantity=1,
                averageCost=100,
                costBasis=100,
                currency="USD",
                valueNative=109,
                valueKrw=141_700,
                pnlKrw=13_000,
                pnlRate=0.1,
            ),
            Holding(
                holdingId="toss_api:TSLA",
                accountId="toss_api_account",
                source="toss_api",
                accountKind="live",
                symbol="TSLA",
                market="US",
                assetType="equity",
                assetCategory="us_stock",
                displayName="Tesla",
                quantity=1,
                averageCost=100,
                costBasis=100,
                currency="USD",
                valueNative=110,
                valueKrw=143_000,
                pnlKrw=13_000,
                # Toss Home preserves the broker's percentage-point unit.
                pnlRate=7.5,
            ),
            Holding(
                holdingId="manual:101",
                accountId="101",
                source="toss_manual",
                accountKind="manual",
                symbol="035930",
                market="KR",
                assetType="equity",
                assetCategory="kr_stock",
                displayName="Manual Alpha",
                quantity=1,
                averageCost=50_000,
                costBasis=50_000,
                currency="KRW",
                valueNative=55_000,
                valueKrw=55_000,
                pnlKrw=5_000,
                pnlRate=0.1,
            ),
            Holding(
                holdingId="manual:102",
                accountId="102",
                source="toss_manual",
                accountKind="manual",
                symbol="000660",
                market="KR",
                assetType="equity",
                assetCategory="kr_stock",
                displayName="Manual Beta",
                quantity=1,
                averageCost=60_000,
                costBasis=60_000,
                currency="KRW",
                valueNative=65_000,
                valueKrw=65_000,
                pnlKrw=5_000,
                pnlRate=0.0833333333,
            ),
        ]
    )
    return response


@pytest.mark.asyncio
async def test_production_cached_mcp_projection_preserves_source_contracts():
    from app.mcp_server.tooling import portfolio_holdings
    from app.services.portfolio_snapshot import serialize_portfolio_snapshot
    from app.services.portfolio_snapshot_cache import PortfolioSnapshotCache

    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    cache = PortfolioSnapshotCache(redis_client=redis_client, ttl_seconds=30)
    scope = portfolio_holdings.portfolio_snapshot_scope(
        user_id=1, include_paper=False, paper_sources=None
    )
    await cache.put(
        scope, serialize_portfolio_snapshot(_source_contract_home_response())
    )

    positions, errors = await portfolio_holdings._collect_whole_portfolio_positions(
        cache=cache,
        user_id=1,
    )

    assert errors == []
    by_symbol = {position["symbol"]: position for position in positions}
    assert by_symbol["005930"]["account_name"] == "기본 계좌"
    assert by_symbol["KRW-BTC"]["account_name"] == "기본 계좌"
    assert by_symbol["MSFT"]["profit_loss"] == pytest.approx(10.0)
    assert by_symbol["TSLA"]["profit_rate"] == pytest.approx(7.5)
    assert by_symbol["035930"]["account"] == "broker_alpha"
    assert by_symbol["035930"]["account_name"] == "Broker Alpha"
    assert by_symbol["000660"]["account"] == "broker_beta"
    assert by_symbol["000660"]["account_name"] == "Broker Beta"


@pytest.mark.asyncio
async def test_cached_toss_manual_projection_does_not_merge_unpriced_beta_into_alpha(
    monkeypatch,
):
    from app.mcp_server.tooling import portfolio_holdings
    from app.schemas.invest_home import HomeSummary, InvestHomeResponse
    from app.services import invest_home_readers as readers
    from app.services.portfolio_snapshot import serialize_portfolio_snapshot
    from app.services.portfolio_snapshot_cache import PortfolioSnapshotCache

    alpha = SimpleNamespace(
        id=101,
        broker_account_id=101,
        broker_account=SimpleNamespace(
            id=101, broker_type="toss", account_name="Broker Alpha"
        ),
        ticker="005930",
        market_type="KR",
        display_name="Alpha Samsung",
        quantity=1,
        avg_price=50_000,
    )
    beta = SimpleNamespace(
        id=102,
        broker_account_id=102,
        broker_account=SimpleNamespace(
            id=102, broker_type="toss", account_name="Broker Beta"
        ),
        ticker="000660",
        market_type="KR",
        display_name="Beta SK Hynix",
        quantity=1,
        avg_price=60_000,
    )

    class _ManualService:
        def __init__(self, db):
            self.db = db

        async def get_holdings_by_user(self, user_id):
            return [alpha, beta]

    monkeypatch.setattr(readers, "ManualHoldingsService", _ManualService)
    quote_service = SimpleNamespace(
        fetch_kr_prices=AsyncMock(return_value={"005930": 55_000.0}),
        fetch_us_prices=AsyncMock(return_value={}),
    )
    manual_result = await readers.ManualHomeReader(
        db=None, quote_service=quote_service
    ).fetch(user_id=1)
    response = InvestHomeResponse(
        homeSummary=HomeSummary(
            includedSources=["toss_manual"],
            excludedSources=[],
            totalValueKrw=55_000,
        ),
        accounts=manual_result.accounts,
        holdings=manual_result.holdings,
        groupedHoldings=[],
    )

    cache = PortfolioSnapshotCache(
        redis_client=fakeredis.aioredis.FakeRedis(decode_responses=True),
        ttl_seconds=30,
    )
    scope = portfolio_holdings.portfolio_snapshot_scope(
        user_id=1, include_paper=False, paper_sources=None
    )
    await cache.put(scope, serialize_portfolio_snapshot(response))

    positions, errors = await portfolio_holdings._collect_whole_portfolio_positions(
        cache=cache,
        user_id=1,
    )

    assert errors == []
    by_symbol = {position["symbol"]: position for position in positions}
    assert by_symbol["005930"]["account"] == "broker_alpha"
    assert by_symbol["000660"]["account"] == "broker_beta"
    assert by_symbol["000660"]["account_name"] == "Broker Beta"


@pytest.mark.asyncio
async def test_cached_upbit_projection_uses_canonical_symbol_for_refresh_lookup(
    monkeypatch,
):
    from app.mcp_server.tooling import portfolio_holdings
    from app.services.portfolio_snapshot import serialize_portfolio_snapshot
    from app.services.portfolio_snapshot_cache import PortfolioSnapshotCache

    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    cache = PortfolioSnapshotCache(redis_client=redis_client, ttl_seconds=30)
    scope = portfolio_holdings.portfolio_snapshot_scope(
        user_id=1, include_paper=False, paper_sources=None
    )
    await cache.put(
        scope, serialize_portfolio_snapshot(_legacy_projection_home_response())
    )
    monkeypatch.setattr(
        portfolio_holdings, "get_shared_portfolio_snapshot_cache", lambda: cache
    )

    seen_symbols: list[str] = []

    async def fake_price_map(positions):
        seen_symbols.extend(
            str(position["symbol"])
            for position in positions
            if position["instrument_type"] == "crypto"
        )
        return {("crypto", "KRW-BTC"): 123.0}, [], {}, {}

    monkeypatch.setattr(
        portfolio_holdings, "_fetch_price_map_for_positions", fake_price_map
    )

    positions, errors, _, _ = await portfolio_holdings._collect_portfolio_positions(
        account=None,
        market="crypto",
        include_current_price=True,
        user_id=1,
    )

    assert errors == []
    assert seen_symbols == ["KRW-BTC"]
    assert positions[0]["symbol"] == "KRW-BTC"
    assert positions[0]["current_price"] == pytest.approx(123.0)


@pytest.mark.asyncio
async def test_cached_mcp_projection_includes_upbit_dust_from_hidden_home_holdings():
    from app.mcp_server.tooling import portfolio_holdings
    from app.schemas.invest_home import Holding
    from app.services.portfolio_snapshot import serialize_portfolio_snapshot
    from app.services.portfolio_snapshot_cache import PortfolioSnapshotCache

    response = _legacy_projection_home_response()
    response.holdings = [
        holding for holding in response.holdings if holding.source != "upbit"
    ]
    response.meta.hiddenHoldings = [
        Holding(
            holdingId="upbit:hidden:BTC",
            accountId="upbit_account",
            source="upbit",
            accountKind="live",
            symbol="BTC",
            market="CRYPTO",
            assetType="crypto",
            assetCategory="crypto",
            displayName="BTC",
            quantity=0.001,
            averageCost=100_000_000,
            costBasis=100_000,
            currency="KRW",
            valueNative=100.0,
            valueKrw=100.0,
            pnlKrw=None,
            pnlRate=None,
            priceState="missing",
        )
    ]
    cache = PortfolioSnapshotCache(
        redis_client=fakeredis.aioredis.FakeRedis(decode_responses=True),
        ttl_seconds=30,
    )
    scope = portfolio_holdings.portfolio_snapshot_scope(
        user_id=1, include_paper=False, paper_sources=None
    )
    await cache.put(scope, serialize_portfolio_snapshot(response))

    positions, errors = await portfolio_holdings._collect_whole_portfolio_positions(
        cache=cache,
        user_id=1,
    )

    assert errors == []
    dust = [position for position in positions if position["symbol"] == "KRW-BTC"]
    assert len(dust) == 1
    assert dust[0]["quantity"] == pytest.approx(0.001)


@pytest.mark.unit
def test_portfolio_snapshot_serializer_drops_reconstructible_sellability_fields() -> (
    None
):
    from app.services.portfolio_snapshot import serialize_portfolio_snapshot

    payload = serialize_portfolio_snapshot(_legacy_projection_home_response())
    raw_holdings = payload["response"]["holdings"]

    assert raw_holdings
    for holding in raw_holdings:
        assert "sellableQuantity" not in holding
        assert "pendingSellQuantity" not in holding
        assert not any("sellable" in str(key).lower() for key in holding)
        assert not any("pending_sell" in str(key).lower() for key in holding)


@pytest.mark.unit
@pytest.mark.parametrize(
    "forbidden_fields",
    [
        {"pending_sell_quantity": "0"},
        {"sellableQuantity": "1"},
        {"metadata": {"nested": {"sellable": "1"}}},
        {"sellable_quantity": None},
    ],
)
def test_toss_snapshot_cache_parser_rejects_all_sellable_key_shapes(
    forbidden_fields,
):
    from app.services.toss_portfolio_service import _position_from_snapshot_cache

    raw = {
        "account": "toss",
        "account_name": "Toss",
        "broker": "toss",
        "source": "toss_api",
        "instrument_type": "equity_us",
        "market": "us",
        "symbol": "AAPL",
        "name": "Apple",
        "quantity": "1",
        "avg_buy_price": "100",
        "current_price": "110",
        "evaluation_amount": "110",
        "profit_loss": "10",
        "profit_rate": "10",
    }
    raw.update(forbidden_fields)

    with pytest.raises(ValueError, match="sellable"):
        _position_from_snapshot_cache(raw)


@pytest.mark.asyncio
async def test_process_shared_snapshot_renews_slow_owner_lease_without_duplicate_fetch():
    from app.services.toss_portfolio_snapshot_cache import TossPortfolioSnapshotCache

    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    cache_a = TossPortfolioSnapshotCache(
        redis_client=redis_client,
        ttl_seconds=30,
        lock_ttl_seconds=1,
        wait_timeout_seconds=1,
        poll_interval_seconds=0.01,
    )
    cache_b = TossPortfolioSnapshotCache(
        redis_client=redis_client,
        ttl_seconds=30,
        lock_ttl_seconds=1,
        wait_timeout_seconds=1,
        poll_interval_seconds=0.01,
    )
    owner_started = asyncio.Event()
    calls = {"owner": 0, "waiter": 0}

    async def owner_fetch() -> dict[str, object]:
        calls["owner"] += 1
        owner_started.set()
        await asyncio.sleep(1.2)
        return {"held_pairs": [["us", "AAPL"]]}

    async def waiter_fetch() -> dict[str, object]:
        calls["waiter"] += 1
        return {"held_pairs": [["us", "AAPL"]]}

    owner_task = asyncio.create_task(cache_a.get_or_fetch("slow-lease", owner_fetch))
    await owner_started.wait()
    waiter_task = asyncio.create_task(cache_b.get_or_fetch("slow-lease", waiter_fetch))
    owner_payload, waiter_payload = await asyncio.gather(owner_task, waiter_task)

    assert owner_payload == waiter_payload == {"held_pairs": [["us", "AAPL"]]}
    assert calls == {"owner": 1, "waiter": 0}


@pytest.mark.asyncio
async def test_process_shared_snapshot_renewing_hung_owner_has_bounded_wait():
    import time

    from app.services.toss_portfolio_snapshot_cache import TossPortfolioSnapshotCache

    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    cache_a = TossPortfolioSnapshotCache(
        redis_client=redis_client,
        ttl_seconds=30,
        lock_ttl_seconds=0.1,
        wait_timeout_seconds=0.05,
        poll_interval_seconds=0.01,
    )
    cache_b = TossPortfolioSnapshotCache(
        redis_client=redis_client,
        ttl_seconds=30,
        lock_ttl_seconds=0.1,
        wait_timeout_seconds=0.05,
        poll_interval_seconds=0.01,
    )
    owner_started = asyncio.Event()
    never = asyncio.Event()
    calls = {"owner": 0, "waiter": 0}

    async def owner_fetch() -> dict[str, object]:
        calls["owner"] += 1
        owner_started.set()
        await never.wait()
        return {"winner": "A"}

    async def waiter_fetch() -> dict[str, object]:
        calls["waiter"] += 1
        return {"winner": "B"}

    owner_task = asyncio.create_task(cache_a.get_or_fetch("hung-owner", owner_fetch))
    await asyncio.wait_for(owner_started.wait(), timeout=1)
    waiter_task = asyncio.create_task(cache_b.get_or_fetch("hung-owner", waiter_fetch))
    started = time.monotonic()
    try:
        try:
            result = await asyncio.wait_for(waiter_task, timeout=0.35)
        except TimeoutError:
            elapsed = time.monotonic() - started
            assert elapsed < 0.30, "waiter escaped only via the outer test timeout"
            assert calls["waiter"] == 0
        else:
            assert result == {"winner": "B"}
            assert calls["waiter"] == 1
    finally:
        if not owner_task.done():
            owner_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await owner_task


@pytest.mark.asyncio
async def test_process_shared_snapshot_reacquires_after_expired_owner_without_duplicate_fetch():
    from app.services.toss_portfolio_snapshot_cache import TossPortfolioSnapshotCache

    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    cache_b = TossPortfolioSnapshotCache(
        redis_client=redis_client,
        ttl_seconds=30,
        lock_ttl_seconds=0.12,
        wait_timeout_seconds=0.03,
        poll_interval_seconds=0.01,
    )
    cache_c = TossPortfolioSnapshotCache(
        redis_client=redis_client,
        ttl_seconds=30,
        lock_ttl_seconds=0.12,
        wait_timeout_seconds=0.03,
        poll_interval_seconds=0.01,
    )
    await redis_client.set(
        cache_b._singleflight_key("crashed-owner"),
        "dead-owner-token",
        px=120,
    )
    calls: list[str] = []

    async def fetch_b() -> dict[str, object]:
        calls.append("b")
        await asyncio.sleep(0.05)
        return {"owner": "b"}

    async def fetch_c() -> dict[str, object]:
        calls.append("c")
        await asyncio.sleep(0.05)
        return {"owner": "c"}

    result_b, result_c = await asyncio.gather(
        cache_b.get_or_fetch("crashed-owner", fetch_b),
        cache_c.get_or_fetch("crashed-owner", fetch_c),
    )

    assert len(calls) == 1
    assert result_b == result_c
    assert await cache_b.get("crashed-owner") == result_b


@pytest.mark.asyncio
async def test_process_shared_snapshot_discards_lost_owner_result_for_recovery_winner(
    monkeypatch,
):
    from app.services.toss_portfolio_snapshot_cache import TossPortfolioSnapshotCache

    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    cache_a = TossPortfolioSnapshotCache(
        redis_client=redis_client,
        ttl_seconds=30,
        lock_ttl_seconds=0.1,
        wait_timeout_seconds=0.02,
        poll_interval_seconds=0.01,
    )
    cache_b = TossPortfolioSnapshotCache(
        redis_client=redis_client,
        ttl_seconds=30,
        lock_ttl_seconds=0.1,
        wait_timeout_seconds=0.02,
        poll_interval_seconds=0.01,
    )
    owner_started = asyncio.Event()
    release_owner = asyncio.Event()
    recovery_started = asyncio.Event()
    release_recovery = asyncio.Event()
    calls: list[str] = []

    async def owner_fetch() -> dict[str, object]:
        calls.append("A")
        owner_started.set()
        await release_owner.wait()
        return {"winner": "A"}

    async def recovery_fetch() -> dict[str, object]:
        calls.append("B")
        recovery_started.set()
        await release_recovery.wait()
        return {"winner": "B"}

    owner_task = asyncio.create_task(cache_a.get_or_fetch("fenced-owner", owner_fetch))
    await asyncio.wait_for(owner_started.wait(), timeout=1)

    async def stopped_renewal(_scope: str, _token: str) -> bool:
        return False

    # Simulate A's lease renewal failing after its fetch has already started.
    monkeypatch.setattr(cache_a, "_renew", stopped_renewal)
    await asyncio.sleep(0.25)

    recovery_task = asyncio.create_task(
        cache_b.get_or_fetch("fenced-owner", recovery_fetch)
    )
    await asyncio.wait_for(recovery_started.wait(), timeout=1)
    release_recovery.set()
    recovery_payload = await recovery_task
    assert recovery_payload == {"winner": "B"}
    assert await cache_b.get("fenced-owner") == recovery_payload

    # A resumes after B has become the new owner. Its stale fetch result must
    # be discarded, and the old caller must observe the same winner as B.
    release_owner.set()
    owner_payload = await owner_task

    assert calls == ["A", "B"]
    assert owner_payload == recovery_payload == {"winner": "B"}
    assert await cache_a.get("fenced-owner") == recovery_payload


@pytest.mark.asyncio
async def test_corrupt_whole_snapshot_fallback_is_singleflight_across_facades():
    from app.services.invest_home_service import InvestHomeService, _SourceFetchResult
    from app.services.portfolio_snapshot_cache import (
        PortfolioSnapshotCache,
        portfolio_snapshot_scope,
    )

    calls = {"kis": 0, "upbit": 0, "manual": 0}

    class _Reader:
        def __init__(self, source: str):
            self.source = source

        async def fetch(self, *, user_id):
            calls[self.source] += 1
            return _SourceFetchResult(accounts=[], holdings=[])

    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    cache_a = PortfolioSnapshotCache(redis_client=redis_client, ttl_seconds=30)
    cache_b = PortfolioSnapshotCache(redis_client=redis_client, ttl_seconds=30)
    scope = portfolio_snapshot_scope(user_id=1, include_paper=False, paper_sources=None)
    await cache_a.put(scope, {"schema_version": 999, "response": {}})

    service_a = InvestHomeService(
        kis_reader=_Reader("kis"),
        upbit_reader=_Reader("upbit"),
        manual_reader=_Reader("manual"),
        snapshot_cache=cache_a,
    )
    service_b = InvestHomeService(
        kis_reader=_Reader("kis"),
        upbit_reader=_Reader("upbit"),
        manual_reader=_Reader("manual"),
        snapshot_cache=cache_b,
    )

    home_a, home_b = await asyncio.gather(
        service_a.get_home(user_id=1),
        service_b.get_home(user_id=1),
    )

    assert home_a.holdings == home_b.holdings == []
    assert calls == {"kis": 1, "upbit": 1, "manual": 1}


@pytest.mark.asyncio
async def test_home_corrupt_snapshot_recovery_does_not_delete_newer_valid_payload(
    monkeypatch,
):
    from app.services.invest_home_service import InvestHomeService, _SourceFetchResult
    from app.services.portfolio_snapshot_cache import (
        PortfolioSnapshotCache,
        portfolio_snapshot_scope,
    )

    calls = {"kis": 0, "upbit": 0, "manual": 0}

    class _Reader:
        def __init__(self, source: str):
            self.source = source

        async def fetch(self, *, user_id):
            calls[self.source] += 1
            return _SourceFetchResult(accounts=[], holdings=[])

    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    cache_a = PortfolioSnapshotCache(redis_client=redis_client, ttl_seconds=30)
    cache_b = PortfolioSnapshotCache(redis_client=redis_client, ttl_seconds=30)
    scope = portfolio_snapshot_scope(user_id=1, include_paper=False, paper_sources=None)
    await cache_a.put(scope, {"schema_version": 999, "response": {}})

    service_a = InvestHomeService(
        kis_reader=_Reader("kis"),
        upbit_reader=_Reader("upbit"),
        manual_reader=_Reader("manual"),
        snapshot_cache=cache_a,
    )
    service_b = InvestHomeService(
        kis_reader=_Reader("kis"),
        upbit_reader=_Reader("upbit"),
        manual_reader=_Reader("manual"),
        snapshot_cache=cache_b,
    )

    delete_started = asyncio.Event()
    allow_delete = asyncio.Event()
    original_delete = cache_b.delete

    async def delayed_delete(scope, *args, **kwargs):
        delete_started.set()
        await allow_delete.wait()
        return await original_delete(scope, *args, **kwargs)

    monkeypatch.setattr(cache_b, "delete", delayed_delete)
    task_b = asyncio.create_task(service_b.get_home(user_id=1))
    await delete_started.wait()
    task_a = asyncio.create_task(service_a.get_home(user_id=1))
    await task_a

    assert calls == {"kis": 1, "upbit": 1, "manual": 1}
    allow_delete.set()
    await task_b
    assert calls == {"kis": 1, "upbit": 1, "manual": 1}


@pytest.mark.asyncio
async def test_mcp_corrupt_snapshot_recovery_does_not_delete_newer_valid_payload(
    monkeypatch,
):
    from app.mcp_server.tooling import portfolio_holdings
    from app.services.portfolio_snapshot import serialize_portfolio_snapshot
    from app.services.portfolio_snapshot_cache import (
        PortfolioSnapshotCache,
        portfolio_snapshot_scope,
    )

    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    cache_a = PortfolioSnapshotCache(redis_client=redis_client, ttl_seconds=30)
    cache_b = PortfolioSnapshotCache(redis_client=redis_client, ttl_seconds=30)
    scope = portfolio_snapshot_scope(user_id=1, include_paper=False, paper_sources=None)
    await cache_a.put(scope, {"schema_version": 999, "response": {}})
    valid_payload = serialize_portfolio_snapshot(_legacy_projection_home_response())
    fetch_calls = 0

    async def fetch_payload(**kwargs):
        nonlocal fetch_calls
        fetch_calls += 1
        return valid_payload

    monkeypatch.setattr(
        portfolio_holdings,
        "fetch_uncached_portfolio_snapshot_payload",
        fetch_payload,
    )
    delete_started = asyncio.Event()
    allow_delete = asyncio.Event()
    original_delete = cache_b.delete

    async def delayed_delete(scope, *args, **kwargs):
        delete_started.set()
        await allow_delete.wait()
        return await original_delete(scope, *args, **kwargs)

    monkeypatch.setattr(cache_b, "delete", delayed_delete)
    task_b = asyncio.create_task(
        portfolio_holdings._collect_whole_portfolio_positions(
            cache=cache_b,
            user_id=1,
        )
    )
    await delete_started.wait()
    task_a = asyncio.create_task(
        portfolio_holdings._collect_whole_portfolio_positions(
            cache=cache_a,
            user_id=1,
        )
    )
    await task_a

    assert fetch_calls == 1
    allow_delete.set()
    await task_b
    assert fetch_calls == 1


@pytest.mark.asyncio
async def test_calendar_cold_snapshot_fails_closed_without_live_reader_fanout():
    from app.services.invest_home_service import InvestHomeService
    from app.services.portfolio_snapshot_cache import PortfolioSnapshotCache

    calls = {"kis": 0, "upbit": 0, "toss_api": 0}

    class _ExplodingLiveReader:
        def __init__(self, source: str):
            self.source = source

        async def fetch(self, *, user_id):
            calls[self.source] += 1
            raise AssertionError("calendar must not run full live home readers")

    class _ManualKeyReader:
        held_key_calls = 0

        async def fetch(self, *, user_id):
            raise AssertionError("calendar must use the DB held-key projection")

        async def fetch_held_pairs(self, *, user_id):
            self.held_key_calls += 1
            return [("kr", "005930")]

    cache = PortfolioSnapshotCache(
        redis_client=fakeredis.aioredis.FakeRedis(decode_responses=True),
        ttl_seconds=5,
    )
    manual_reader = _ManualKeyReader()
    service = InvestHomeService(
        kis_reader=_ExplodingLiveReader("kis"),
        upbit_reader=_ExplodingLiveReader("upbit"),
        manual_reader=manual_reader,
        toss_api_reader=_ExplodingLiveReader("toss_api"),
        snapshot_cache=cache,
    )

    with pytest.raises(RuntimeError, match="portfolio_snapshot_unavailable"):
        await service.get_held_pairs(user_id=1)

    assert calls == {"kis": 0, "upbit": 0, "toss_api": 0}
    assert manual_reader.held_key_calls == 1


@pytest.mark.asyncio
async def test_calendar_cold_snapshot_surfaces_explicit_503_metadata(monkeypatch):
    from datetime import date

    from fastapi import HTTPException

    from app.routers import invest_api
    from app.services.invest_home_service import InvestHomeService
    from app.services.portfolio_snapshot_cache import PortfolioSnapshotCache

    class _ExplodingLiveReader:
        async def fetch(self, *, user_id):
            raise AssertionError("calendar must not run full live home readers")

    class _ManualKeyReader:
        async def fetch(self, *, user_id):
            raise AssertionError("calendar must use the DB held-key projection")

        async def fetch_held_pairs(self, *, user_id):
            return [("kr", "005930")]

    service = InvestHomeService(
        kis_reader=_ExplodingLiveReader(),
        upbit_reader=_ExplodingLiveReader(),
        manual_reader=_ManualKeyReader(),
        toss_api_reader=_ExplodingLiveReader(),
        snapshot_cache=PortfolioSnapshotCache(
            redis_client=fakeredis.aioredis.FakeRedis(decode_responses=True),
            ttl_seconds=5,
        ),
    )
    monkeypatch.setattr(
        invest_api,
        "build_relation_resolver",
        AsyncMock(return_value=object()),
    )
    monkeypatch.setattr(
        invest_api,
        "build_calendar",
        AsyncMock(return_value={"ok": True}),
    )

    with pytest.raises(HTTPException) as caught:
        await invest_api.get_calendar(
            user=SimpleNamespace(id=1),
            service=service,
            db=object(),
            from_date=date(2026, 8, 20),
            to_date=date(2026, 8, 21),
            tab="all",
            include_paper=False,
            paper_sources=None,
        )

    assert caught.value.status_code == 503
    assert caught.value.detail == {
        "error_code": "portfolio_snapshot_unavailable",
        "source": "portfolio_snapshot",
        "unavailable_reason": "held_key_projection_missing_or_invalid",
        "manual_pairs_available": True,
    }


@pytest.mark.asyncio
async def test_calendar_cold_manual_db_failure_is_typed_503_without_live_fanout(
    monkeypatch,
):
    from datetime import date

    from fastapi import HTTPException

    from app.routers import invest_api
    from app.services.invest_home_service import InvestHomeService
    from app.services.portfolio_snapshot_cache import PortfolioSnapshotCache

    calls = {"kis": 0, "upbit": 0, "toss_api": 0}

    class _ExplodingLiveReader:
        def __init__(self, source: str):
            self.source = source

        async def fetch(self, *, user_id):
            calls[self.source] += 1
            raise AssertionError("calendar must not run full live home readers")

    class _BrokenManualKeyReader:
        async def fetch(self, *, user_id):
            raise AssertionError("calendar must use held-key read only")

        async def fetch_held_pairs(self, *, user_id):
            raise RuntimeError("fake manual DB unavailable")

    service = InvestHomeService(
        kis_reader=_ExplodingLiveReader("kis"),
        upbit_reader=_ExplodingLiveReader("upbit"),
        manual_reader=_BrokenManualKeyReader(),
        toss_api_reader=_ExplodingLiveReader("toss_api"),
        snapshot_cache=PortfolioSnapshotCache(
            redis_client=fakeredis.aioredis.FakeRedis(decode_responses=True),
            ttl_seconds=5,
        ),
    )
    monkeypatch.setattr(
        invest_api,
        "build_relation_resolver",
        AsyncMock(return_value=object()),
    )
    monkeypatch.setattr(
        invest_api,
        "build_calendar",
        AsyncMock(return_value={"ok": True}),
    )

    with pytest.raises(HTTPException) as caught:
        await invest_api.get_calendar(
            user=SimpleNamespace(id=1),
            service=service,
            db=object(),
            from_date=date(2026, 8, 20),
            to_date=date(2026, 8, 21),
            tab="all",
            include_paper=False,
            paper_sources=None,
        )

    assert caught.value.status_code == 503
    assert caught.value.detail["error_code"] == "portfolio_snapshot_unavailable"
    assert caught.value.detail["source"] == "portfolio_snapshot"
    assert caught.value.detail["manual_pairs_available"] is False
    assert caught.value.detail["unavailable_reason"]
    assert calls == {"kis": 0, "upbit": 0, "toss_api": 0}


@pytest.mark.asyncio
async def test_manual_held_key_failure_does_not_log_exception_secret(caplog):
    from app.services.invest_home_service import (
        InvestHomeService,
        PortfolioSnapshotUnavailableError,
    )
    from app.services.portfolio_snapshot_cache import PortfolioSnapshotCache

    sentinel = "fake-manual-secret-ROB1310"

    class _BrokenManualKeyReader:
        async def fetch(self, *, user_id):
            raise AssertionError("held-key path must not call full manual fetch")

        async def fetch_held_pairs(self, *, user_id):
            raise RuntimeError(f"database password={sentinel}")

    class _NoLiveReader:
        async def fetch(self, *, user_id):
            raise AssertionError("held-key path must not call live readers")

    service = InvestHomeService(
        kis_reader=_NoLiveReader(),
        upbit_reader=_NoLiveReader(),
        manual_reader=_BrokenManualKeyReader(),
        toss_api_reader=_NoLiveReader(),
        snapshot_cache=PortfolioSnapshotCache(
            redis_client=fakeredis.aioredis.FakeRedis(decode_responses=True),
            ttl_seconds=5,
        ),
    )

    with caplog.at_level("WARNING", logger="app.services.invest_home_service"):
        with pytest.raises(PortfolioSnapshotUnavailableError) as caught:
            await service.get_held_pairs(user_id=1)

    assert caught.value.reason == "held_key_projection_unavailable"
    assert sentinel not in repr(caught.value)
    assert caught.value.__cause__ is None
    for record in caplog.records:
        assert sentinel not in record.getMessage()
        assert sentinel not in repr(record.args)
        assert sentinel not in repr(record.exc_info)


# --------------------------------------------------------------------------
# ROB-1310 R7 audit regressions: held-key symbol seam + bounded cached Toss
# recovery. Fake/local only; no broker, credential, or network access.
# --------------------------------------------------------------------------


def _held_key_home_response():
    """A home projection whose symbols arrive in broker-specific spellings."""

    from app.schemas.invest_home import (
        Account,
        CashAmounts,
        Holding,
        HomeSummary,
        InvestHomeResponse,
    )

    return InvestHomeResponse(
        homeSummary=HomeSummary(
            includedSources=["kis", "upbit"],
            excludedSources=[],
            totalValueKrw=1_000,
        ),
        accounts=[
            Account(
                accountId="kis_account",
                displayName="KIS",
                source="kis",
                accountKind="live",
                includedInHome=True,
                valueKrw=1_000,
                cashBalances=CashAmounts(),
                buyingPower=CashAmounts(),
            )
        ],
        groupedHoldings=[],
        holdings=[
            Holding(
                holdingId="kis:us:BRK-B",
                accountId="kis_account",
                source="kis",
                accountKind="live",
                # Yahoo/broker spelling that must be normalized to the DB form.
                symbol="BRK-B",
                market="US",
                assetType="equity",
                assetCategory="us_stock",
                displayName="Berkshire Hathaway B",
                quantity=1,
                currency="USD",
            ),
            Holding(
                holdingId="upbit:BTC",
                accountId="kis_account",
                source="upbit",
                accountKind="live",
                # Upbit balance spelling: the bare base coin.
                symbol="btc",
                market="CRYPTO",
                assetType="crypto",
                assetCategory="crypto",
                displayName="Bitcoin",
                quantity=2,
                currency="KRW",
            ),
        ],
    )


@pytest.mark.unit
def test_snapshot_held_pairs_use_market_aware_symbol_helpers() -> None:
    """ROB-1310 R7: the serialized held-key projection must go through the
    shared market-aware symbol helpers -- ``to_db_symbol`` for KR/US and
    ``to_upbit_symbol`` for crypto -- never a bare ``strip().upper()``.
    """
    from app.services.portfolio_snapshot import serialize_portfolio_snapshot

    payload = serialize_portfolio_snapshot(_held_key_home_response())

    assert payload["held_pairs"] == [
        ["crypto", "KRW-BTC"],
        ["us", "BRK.B"],
    ]


@pytest.mark.unit
def test_held_pairs_from_portfolio_snapshot_normalizes_market_aware_symbols() -> None:
    """A cached payload written by an older process must still be read back
    through the same market-aware seam."""
    from app.services.portfolio_snapshot import (
        PORTFOLIO_SNAPSHOT_SCHEMA_VERSION,
        held_pairs_from_portfolio_snapshot,
    )

    pairs = held_pairs_from_portfolio_snapshot(
        {
            "schema_version": PORTFOLIO_SNAPSHOT_SCHEMA_VERSION,
            "held_pairs": [
                ["us", "BRK/B"],
                ["us", "brk-b"],
                ["crypto", "btc"],
                ["crypto", "KRW-BTC"],
                ["kr", " 005930 "],
            ],
        }
    )

    # ``BRK/B`` and ``brk-b`` collapse onto the single DB key ``BRK.B``; the
    # bare coin and the market-prefixed coin collapse onto ``KRW-BTC``.
    assert pairs == [
        ("crypto", "KRW-BTC"),
        ("kr", "005930"),
        ("us", "BRK.B"),
    ]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_manual_reader_held_pairs_use_market_aware_symbol_helpers() -> None:
    """``ManualHomeReader.fetch_held_pairs`` is the cold-snapshot manual seam;
    it must emit the same market-aware keys as the snapshot projection."""
    from app.services import invest_home_readers as readers

    class _ManualHoldingsStub:
        def __init__(self, _db):
            pass

        async def get_holdings_by_user(self, _user_id):
            return [
                SimpleNamespace(market_type="US", ticker="BRK-B", quantity=1),
                SimpleNamespace(market_type="CRYPTO", ticker="btc", quantity=2),
                SimpleNamespace(market_type="KR", ticker=" 005930 ", quantity=3),
                SimpleNamespace(market_type="US", ticker="ZERO", quantity=0),
            ]

    reader = readers.ManualHomeReader.__new__(readers.ManualHomeReader)
    reader._db = None
    reader._service = _ManualHoldingsStub(None)
    reader._quote_service = None

    assert await reader.fetch_held_pairs(user_id=1) == [
        ("crypto", "KRW-BTC"),
        ("kr", "005930"),
        ("us", "BRK.B"),
    ]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_service_manual_held_pairs_use_market_aware_symbol_helpers() -> None:
    """The typed cold-snapshot error carries manual keys for the same seam;
    they must not be a differently-normalized dialect."""
    from app.services.invest_home_service import (
        InvestHomeService,
        PortfolioSnapshotUnavailableError,
    )
    from app.services.portfolio_snapshot_cache import PortfolioSnapshotCache

    class _ManualKeyReader:
        async def fetch_held_pairs(self, *, user_id: int):
            return [("US", "BRK/B"), ("CRYPTO", "btc"), ("KR", " 005930 ")]

    class _ExplodingReader:
        async def fetch(self, *, user_id: int):
            raise AssertionError("held-key projection must not run full readers")

    cache = PortfolioSnapshotCache(
        redis_client=fakeredis.aioredis.FakeRedis(decode_responses=True),
        ttl_seconds=30,
    )
    service = InvestHomeService(
        kis_reader=_ExplodingReader(),
        upbit_reader=_ExplodingReader(),
        manual_reader=_ManualKeyReader(),
        toss_api_reader=_ExplodingReader(),
        snapshot_cache=cache,
    )

    with pytest.raises(PortfolioSnapshotUnavailableError) as excinfo:
        await service.get_held_pairs(user_id=1)

    assert excinfo.value.manual_pairs == [
        ("crypto", "KRW-BTC"),
        ("kr", "005930"),
        ("us", "BRK.B"),
    ]


class _FailingHoldingsTossClient:
    """Fake Toss client whose holdings fanout always fails at the broker."""

    def __init__(self, *, cash_gate: asyncio.Event | None = None) -> None:
        self.holdings_calls = 0
        self.buying_power_calls = 0
        self.sellable_calls = 0
        self.cash_cancelled = False
        self.closed = False
        self.buying_power_after_close = 0
        self._cash_gate = cash_gate

    async def holdings(self):
        self.holdings_calls += 1
        raise RuntimeError("toss holdings outage")

    async def sellable_quantity(self, *, symbol: str):
        self.sellable_calls += 1
        raise AssertionError("general snapshot must not call sellable_quantity")

    async def buying_power(self, *, currency: str):
        self.buying_power_calls += 1
        if self.closed:
            self.buying_power_after_close += 1
        if self._cash_gate is not None:
            try:
                await self._cash_gate.wait()
            except asyncio.CancelledError:
                self.cash_cancelled = True
                raise
        return SimpleNamespace(currency=currency, cash_buying_power=Decimal("100"))

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
@pytest.mark.unit
async def test_shared_toss_snapshot_upstream_failure_fans_out_to_broker_once() -> None:
    """ROB-1310 R7: an upstream broker failure is not corrupt-cache evidence.
    It must propagate after exactly one fanout instead of re-entering the
    recovery path and multiplying load on an already failing broker.
    """
    from app.services.toss_portfolio_snapshot_cache import TossPortfolioSnapshotCache

    client = _FailingHoldingsTossClient()
    cache = TossPortfolioSnapshotCache(
        redis_client=fakeredis.aioredis.FakeRedis(decode_responses=True),
        ttl_seconds=30,
        wait_timeout_seconds=1,
        poll_interval_seconds=0.01,
    )

    with pytest.raises(RuntimeError, match="toss holdings outage"):
        await fetch_toss_portfolio_snapshot(
            client=client,
            need_cash=False,
            snapshot_cache=cache,
            use_shared_snapshot=True,
        )

    assert client.holdings_calls == 1
    assert client.sellable_calls == 0


@pytest.mark.asyncio
@pytest.mark.unit
async def test_shared_toss_snapshot_failure_cancels_and_drains_pending_cash_task() -> (
    None
):
    """ROB-707 already guards this on the uncached path: when the positions
    chain fails the sibling cash task must be cancelled and drained before the
    owned client is closed, so it never touches a closed client and never
    leaves an unretrieved task exception.
    """
    from app.services.toss_portfolio_snapshot_cache import TossPortfolioSnapshotCache

    cash_gate = asyncio.Event()
    client = _FailingHoldingsTossClient(cash_gate=cash_gate)
    cache = TossPortfolioSnapshotCache(
        redis_client=fakeredis.aioredis.FakeRedis(decode_responses=True),
        ttl_seconds=30,
        wait_timeout_seconds=1,
        poll_interval_seconds=0.01,
    )

    with pytest.raises(RuntimeError, match="toss holdings outage"):
        await fetch_toss_portfolio_snapshot(
            client=client,
            need_cash=True,
            snapshot_cache=cache,
            use_shared_snapshot=True,
        )

    assert client.cash_cancelled is True
    assert client.buying_power_after_close == 0

    # Nothing may still be pending against the (now closed) client.
    pending = [
        task
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task() and not task.done()
    ]
    assert pending == []


@pytest.mark.asyncio
@pytest.mark.unit
async def test_shared_toss_snapshot_corrupt_payload_still_reenters_singleflight() -> (
    None
):
    """Deserialization corruption -- and only that -- keeps its CAS-delete and
    single bounded re-entry into the shared singleflight."""
    from app.services.toss_portfolio_snapshot_cache import TossPortfolioSnapshotCache

    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    cache = TossPortfolioSnapshotCache(
        redis_client=redis_client,
        ttl_seconds=30,
        wait_timeout_seconds=1,
        poll_interval_seconds=0.01,
    )
    await cache.put("positions", {"positions": [{"symbol": "AAPL"}]})

    client = _CountingTossClient()
    snapshot = await fetch_toss_portfolio_snapshot(
        client=client,
        need_cash=False,
        snapshot_cache=cache,
        use_shared_snapshot=True,
    )

    assert client.holdings_calls == 1
    assert [position.symbol for position in snapshot.positions] == ["AAPL"]
    assert snapshot.positions[0].sellable_quantity is None


@pytest.mark.unit
def test_snapshot_cache_decimal_parse_error_is_a_value_error() -> None:
    """A non-numeric cached decimal must surface as the intended ``ValueError``
    (corrupt-cache evidence), not as a bare ``decimal.InvalidOperation`` that
    the narrowed recovery path would treat as an upstream failure.
    """
    from app.services.toss_portfolio_service import _decimal_from_snapshot_cache

    with pytest.raises(ValueError, match="invalid quantity"):
        _decimal_from_snapshot_cache("not-a-number", field="quantity")


@pytest.mark.asyncio
@pytest.mark.unit
async def test_snapshot_cache_recovery_observes_actual_owner_token() -> None:
    """ROB-1310 R7: a lost recovery ``SET NX`` returns ``None``. Recording that
    ``None`` as the observed owner makes the *next* token comparison always read
    as "a recovery owner just took over" and grants one unearned extra wait
    budget. The waiter must record the actual current owner token instead.
    """
    from app.services.toss_portfolio_snapshot_cache import TossPortfolioSnapshotCache

    cache = TossPortfolioSnapshotCache(
        redis_client=fakeredis.aioredis.FakeRedis(decode_responses=True),
        ttl_seconds=30,
        lock_ttl_seconds=10,
        wait_timeout_seconds=0.02,
        poll_interval_seconds=0.005,
    )

    lease_probes = 0

    async def _never_acquire(_scope: str):
        # Every SET NX loses: another waiter is always the recovery owner.
        return None

    async def _lock_remaining(_scope: str):
        nonlocal lease_probes
        lease_probes += 1
        # First probe: the previous owner's lease has expired, so the waiter
        # enters bounded crash recovery. Afterwards a live recovery owner
        # holds the lease under a stable token.
        return 0.0 if lease_probes == 1 else 5.0

    async def _lock_token(_scope: str):
        return "recovery-owner-token"

    cache._acquire = _never_acquire  # type: ignore[method-assign]
    cache._lock_remaining_seconds = _lock_remaining  # type: ignore[method-assign]
    cache._lock_token = _lock_token  # type: ignore[method-assign]

    async def _fetcher():
        raise AssertionError("waiter must not fetch while a recovery owner holds it")

    with pytest.raises(TimeoutError, match="owner did not complete"):
        await cache.get_or_fetch("positions", _fetcher)

    # One probe for the expired lease (crash recovery) plus one for the live
    # recovery owner under an unchanged token. A third probe means the ``None``
    # observation granted an extra unearned window.
    assert lease_probes == 2


@pytest.mark.asyncio
@pytest.mark.unit
@pytest.mark.parametrize(
    "module_name",
    [
        "app.services.portfolio_snapshot_cache",
        "app.services.toss_portfolio_snapshot_cache",
    ],
)
async def test_reset_shared_snapshot_cache_closes_the_client_it_owns(
    monkeypatch, module_name: str
) -> None:
    """ROB-1310 R7: the factory owns the Redis client it creates. Dropping the
    process-local singleton without closing it leaks that client's connection
    pool for every reset.
    """
    import importlib

    module = importlib.import_module(module_name)

    class _RecordingRedis:
        def __init__(self) -> None:
            self.closed = False

        async def aclose(self) -> None:
            self.closed = True

    created: list[_RecordingRedis] = []

    def _fake_from_url(*_args, **_kwargs):
        client = _RecordingRedis()
        created.append(client)
        return client

    module.reset_shared_portfolio_snapshot_cache()
    monkeypatch.setattr(module.redis, "from_url", _fake_from_url)
    try:
        module.get_shared_portfolio_snapshot_cache()
        assert len(created) == 1

        module.reset_shared_portfolio_snapshot_cache()
        for _ in range(20):
            await asyncio.sleep(0)

        assert created[0].closed is True
    finally:
        module.reset_shared_portfolio_snapshot_cache()


# ---------------------------------------------------------------------------
# ROB-1310 R8 — corrupt cash cache must not discard valid positions
# ---------------------------------------------------------------------------


class _CorruptCashTossClient:
    """Valid holdings, but a ``buying_power`` value that can never round-trip.

    Every cash payload this client produces fails ``_cash_from_snapshot_cache``
    deserialization, so both bounded recovery reads stay corrupt. Positions are
    fully reconstructible.
    """

    def __init__(self) -> None:
        self.holdings_calls = 0
        self.sellable_calls = 0
        self.buying_power_calls = 0
        self.closed = False

    async def holdings(self) -> TossHoldings:
        self.holdings_calls += 1
        return TossHoldings(items=[_holding()])

    async def sellable_quantity(self, *, symbol: str):
        self.sellable_calls += 1
        raise AssertionError(
            f"general snapshot must not call sellable_quantity: {symbol}"
        )

    async def buying_power(self, *, currency: str):
        self.buying_power_calls += 1
        return SimpleNamespace(currency=currency, cash_buying_power="not-a-number")

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
@pytest.mark.unit
async def test_corrupt_cash_cache_keeps_positions_and_reports_unknown_cash() -> None:
    """ROB-1310 R8 / review 3826407069.

    Positions deserialize fine, then both bounded cash recovery reads stay
    corrupt. Raising there threw away already-valid positions. Cash must
    instead be reported as unknown (``None``, never a fabricated ``0``) with a
    sanitized error, and the positions must survive.
    """
    from app.services.toss_portfolio_snapshot_cache import TossPortfolioSnapshotCache

    client = _CorruptCashTossClient()
    cache = TossPortfolioSnapshotCache(
        redis_client=fakeredis.aioredis.FakeRedis(decode_responses=True),
        ttl_seconds=30,
        wait_timeout_seconds=1,
        poll_interval_seconds=0.01,
    )

    snapshot = await fetch_toss_portfolio_snapshot(
        client=client,
        need_cash=True,
        snapshot_cache=cache,
        use_shared_snapshot=True,
    )

    assert [position.symbol for position in snapshot.positions] == ["AAPL"]
    assert snapshot.positions[0].sellable_quantity is None
    assert snapshot.cash_krw is None
    assert snapshot.cash_usd is None

    assert snapshot.errors, "unavailable cash must surface an explicit error"
    cash_errors = [
        error for error in snapshot.errors if error.get("stage") == "cash_snapshot"
    ]
    assert len(cash_errors) == 1
    assert cash_errors[0]["source"] == "toss_api"
    # Sanitized: fixed text, never a raw exception message or cached payload.
    assert cash_errors[0]["error"] == "invalid_cash_snapshot_payload"
    assert "not-a-number" not in str(snapshot.errors)

    # Recovery stays bounded: one positions fanout, exactly two bounded cash
    # attempts (2 currencies x 2 attempts).
    assert client.holdings_calls == 1
    assert client.sellable_calls == 0
    assert client.buying_power_calls == 4

    # R7 task cleanup is not regressed.
    pending = [
        task
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task() and not task.done()
    ]
    assert pending == []


@pytest.mark.asyncio
@pytest.mark.unit
async def test_toss_api_home_reader_survives_corrupt_cash_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: a corrupt cash cache must not blank the whole Toss source.

    ``TossApiHomeReader`` catches any raise from the snapshot service and
    degrades to empty accounts/holdings, so a cash-only corruption used to
    erase valid Toss positions from /invest home entirely.
    """
    from app.services import invest_home_readers as readers
    from app.services.toss_portfolio_snapshot_cache import TossPortfolioSnapshotCache

    client = _CorruptCashTossClient()
    cache = TossPortfolioSnapshotCache(
        redis_client=fakeredis.aioredis.FakeRedis(decode_responses=True),
        ttl_seconds=30,
        wait_timeout_seconds=1,
        poll_interval_seconds=0.01,
    )

    async def _snapshot_through_real_service(*, need_sellable: bool = False, **kwargs):
        assert need_sellable is False
        return await fetch_toss_portfolio_snapshot(
            client=client,
            need_sellable=False,
            need_cash=True,
            snapshot_cache=cache,
            use_shared_snapshot=True,
        )

    monkeypatch.setattr(
        readers, "fetch_toss_portfolio_snapshot", _snapshot_through_real_service
    )
    monkeypatch.setattr(readers, "get_usd_krw_rate", AsyncMock(return_value=1_350.0))
    from app.core.config import settings as _cfg

    monkeypatch.setattr(_cfg, "toss_live_order_mutations_enabled", False, raising=False)

    result = await readers.TossApiHomeReader().fetch(user_id=1)

    assert [holding.symbol for holding in result.holdings] == ["AAPL"]
    assert len(result.accounts) == 1
    account = result.accounts[0]
    assert account.accountId == "toss_api_account"
    assert account.source == "toss_api"
    # Cash is unknown, not fabricated as zero.
    assert account.cashBalances.krw is None
    assert account.cashBalances.usd is None
    assert account.buyingPower.krw is None
    assert account.buyingPower.usd is None
    # The degradation is reported, not silent.
    assert result.warning is not None
    assert result.warning.source == "toss_api"
    assert "invalid_cash_snapshot_payload" in result.warning.message


# ---------------------------------------------------------------------------
# ROB-1310 R9 / B5 — the shared snapshot cache must namespace both the cache
# and singleflight lock by an opaque scope derived from non-secret Toss
# account/environment identity, so two processes with different Toss
# accounts on shared Redis do not silently exchange payloads.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_shared_snapshot_cache_scope_differs_by_account_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ROB-1310 R9 / B5 (R8 verifier blocker 5 / review 3826632065).

    Two processes with different Toss account/environment identity sharing
    the same Redis must not collide on the same static cache key. The
    production factory must derive the cache/lock key from a scope tied to
    the configured Toss endpoint/account identity, and that scope must not
    contain the raw client id or account sequence.
    """
    import app.services.toss_portfolio_snapshot_cache as cache_module
    from app.core.config import settings

    cache_module.reset_shared_portfolio_snapshot_cache()
    monkeypatch.setattr(cache_module.redis, "from_url", lambda *a, **k: object())
    try:
        monkeypatch.setattr(settings, "toss_api_client_id", "client-account-a")
        monkeypatch.setattr(settings, "toss_api_account_seq", 1, raising=False)
        monkeypatch.setattr(
            settings,
            "toss_api_base_url",
            "https://openapi.tossinvest.com",
            raising=False,
        )
        cache_a = cache_module.get_shared_portfolio_snapshot_cache()
        key_a = cache_a._cache_key("positions")
        lock_a = cache_a._singleflight_key("positions")

        cache_module.reset_shared_portfolio_snapshot_cache()
        monkeypatch.setattr(settings, "toss_api_client_id", "client-account-b")
        monkeypatch.setattr(settings, "toss_api_account_seq", 2, raising=False)
        cache_b = cache_module.get_shared_portfolio_snapshot_cache()
        key_b = cache_b._cache_key("positions")
        lock_b = cache_b._singleflight_key("positions")

        assert key_a != key_b, "different account identity must not share a cache key"
        assert lock_a != lock_b, (
            "different account identity must not share a singleflight lock"
        )
        # Only check the actual raw identity strings -- a bare digit
        # (account_seq) substring check is unreliable: the pre-existing
        # ``:v1:`` key-version literal already contains "1", which would be
        # a false positive unrelated to any leaked identity.
        for raw in ("client-account-a", "client-account-b"):
            assert raw not in key_a
            assert raw not in key_b
    finally:
        cache_module.reset_shared_portfolio_snapshot_cache()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_shared_snapshot_cache_scope_is_stable_for_the_same_account_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ROB-1310 R9 / B5 companion: the same account identity across process
    restarts must resolve to the same scope so same-account facades still
    singleflight/dedupe against each other."""
    import app.services.toss_portfolio_snapshot_cache as cache_module
    from app.core.config import settings

    cache_module.reset_shared_portfolio_snapshot_cache()
    monkeypatch.setattr(cache_module.redis, "from_url", lambda *a, **k: object())
    monkeypatch.setattr(settings, "toss_api_client_id", "client-account-a")
    monkeypatch.setattr(settings, "toss_api_account_seq", 1, raising=False)
    monkeypatch.setattr(
        settings, "toss_api_base_url", "https://openapi.tossinvest.com", raising=False
    )
    try:
        cache_1 = cache_module.get_shared_portfolio_snapshot_cache()
        key_1 = cache_1._cache_key("positions")

        cache_module.reset_shared_portfolio_snapshot_cache()
        cache_2 = cache_module.get_shared_portfolio_snapshot_cache()
        key_2 = cache_2._cache_key("positions")

        assert key_1 == key_2
    finally:
        cache_module.reset_shared_portfolio_snapshot_cache()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_toss_portfolio_snapshot_scopes_do_not_share_cached_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ROB-1310 R9 / B5: two isolated-scope facades on the same Redis must not
    exchange positions -- each fetches independently and caches under its own
    key, proving the isolation actually blocks payload exchange, not merely
    that the key strings differ."""
    from app.services.toss_portfolio_snapshot_cache import TossPortfolioSnapshotCache

    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    cache_a = TossPortfolioSnapshotCache(
        redis_client=redis_client,
        ttl_seconds=30,
        key_prefix="toss:portfolio:snapshot:v1:scope-a",
        lock_prefix="toss:portfolio:snapshot:singleflight:v1:scope-a",
    )
    cache_b = TossPortfolioSnapshotCache(
        redis_client=redis_client,
        ttl_seconds=30,
        key_prefix="toss:portfolio:snapshot:v1:scope-b",
        lock_prefix="toss:portfolio:snapshot:singleflight:v1:scope-b",
    )

    client_a = _CountingTossClient()
    client_b = _CountingTossClient()

    snapshot_a = await fetch_toss_portfolio_snapshot(
        client=client_a,
        need_cash=False,
        snapshot_cache=cache_a,
        use_shared_snapshot=True,
    )
    snapshot_b = await fetch_toss_portfolio_snapshot(
        client=client_b,
        need_cash=False,
        snapshot_cache=cache_b,
        use_shared_snapshot=True,
    )

    # Each scope fetched independently -- no singleflight/result sharing.
    assert client_a.holdings_calls == 1
    assert client_b.holdings_calls == 1
    assert snapshot_a.positions == snapshot_b.positions

    cached_a = await cache_a.get("positions")
    cached_b = await cache_b.get("positions")
    assert cached_a is not None
    assert cached_b is not None
    # Each scope owns a distinct Redis key -- no shared storage slot.
    assert cache_a._cache_key("positions") != cache_b._cache_key("positions")
    assert await redis_client.get(cache_b._cache_key("positions")) is not None


# ---------------------------------------------------------------------------
# ROB-1310 R10 (B5, R9 independent-verifier blocker) -- an identity-less
# injected client passed to the explicit shared-snapshot path must never fall
# back to the static settings-derived process-global cache scope. Two
# distinct identity-less injected clients must not exchange positions/cash.
# A client that declares a trustworthy ``snapshot_scope_identity`` may still
# share/dedupe the shared cache under that (hashed) identity.
# ---------------------------------------------------------------------------

_UNSET = object()


def _holding_with_symbol(symbol: str) -> TossHoldingItem:
    return TossHoldingItem(
        symbol=symbol,
        name=symbol,
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


class _SymbolTossClient:
    """Fake Toss client returning one configurable holding symbol.

    ``snapshot_identity`` left at the default sentinel models a pre-existing
    fake/injected client that does not know about the ``snapshot_scope_identity``
    protocol at all (attribute absent) -- the realistic "identity-less" shape.
    Passing an explicit string models a client that declares a trustworthy
    scope identity.
    """

    def __init__(self, symbol: str, *, snapshot_identity: Any = _UNSET) -> None:
        self.holdings_calls = 0
        self._symbol = symbol
        if snapshot_identity is not _UNSET:
            self.snapshot_scope_identity = snapshot_identity

    async def holdings(self) -> TossHoldings:
        self.holdings_calls += 1
        return TossHoldings(items=[_holding_with_symbol(self._symbol)])

    async def sellable_quantity(self, *, symbol: str):
        raise AssertionError(
            f"general snapshot must not call sellable_quantity: {symbol}"
        )

    async def buying_power(self, *, currency: str):
        return SimpleNamespace(currency=currency, cash_buying_power=Decimal("100"))

    async def aclose(self) -> None:
        return None


@pytest.mark.asyncio
@pytest.mark.unit
async def test_identityless_injected_clients_do_not_share_shared_snapshot_scope(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """ROB-1310 R10 / B5 (R9 independent verifier blocker).

    ``fetch_toss_portfolio_snapshot(client=<identity-less>, use_shared_snapshot=True)``
    with no explicit ``snapshot_cache`` must not fall back to the static
    settings-derived global scope. Two distinct identity-less injected clients
    sharing the same underlying Redis must each fetch and see only their own
    data, and the identity-less path must not write any shared positions/cash
    key at all.
    """
    import app.services.toss_portfolio_snapshot_cache as cache_module

    caplog.set_level("DEBUG")
    fake_redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    cache_module.reset_shared_portfolio_snapshot_cache()
    monkeypatch.setattr(cache_module.redis, "from_url", lambda *a, **k: fake_redis)
    try:
        client_a = _SymbolTossClient("AAA")
        client_b = _SymbolTossClient("BBB")

        snapshot_a = await fetch_toss_portfolio_snapshot(
            client=client_a, need_cash=False, use_shared_snapshot=True
        )
        snapshot_b = await fetch_toss_portfolio_snapshot(
            client=client_b, need_cash=False, use_shared_snapshot=True
        )

        assert snapshot_a.positions[0].symbol == "AAA"
        assert snapshot_b.positions[0].symbol == "BBB"
        assert client_a.holdings_calls == 1
        assert client_b.holdings_calls == 1

        shared_cache = cache_module.get_shared_portfolio_snapshot_cache()
        assert await shared_cache.get("positions") is None
        raw_keys = [key async for key in fake_redis.scan_iter("*")]
        assert raw_keys == [], (
            "identity-less injected clients must never write a shared "
            f"positions/cash key: {raw_keys}"
        )
    finally:
        cache_module.reset_shared_portfolio_snapshot_cache()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_trusted_injected_identity_shares_singleflight_scope_once(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """ROB-1310 R10 / B5: two injected clients that declare the SAME
    trustworthy ``snapshot_scope_identity`` and share one fakeredis-backed
    process cache must dedupe to exactly one upstream holdings fetch, and the
    raw identity string must never appear in a Redis key or a log record."""
    import app.services.toss_portfolio_snapshot_cache as cache_module

    caplog.set_level("DEBUG")
    fake_redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    cache_module.reset_shared_portfolio_snapshot_cache()
    monkeypatch.setattr(cache_module.redis, "from_url", lambda *a, **k: fake_redis)
    try:
        client_a = _SymbolTossClient("AAA", snapshot_identity="shared-account-x")
        client_b = _SymbolTossClient("BBB", snapshot_identity="shared-account-x")

        snapshot_a = await fetch_toss_portfolio_snapshot(
            client=client_a, need_cash=False, use_shared_snapshot=True
        )
        snapshot_b = await fetch_toss_portfolio_snapshot(
            client=client_b, need_cash=False, use_shared_snapshot=True
        )

        # Same trustworthy identity dedupes -- client_b never fetches; it
        # reuses client_a's already-cached result under the shared identity.
        assert client_a.holdings_calls == 1
        assert client_b.holdings_calls == 0
        assert snapshot_a.positions[0].symbol == "AAA"
        assert snapshot_b.positions[0].symbol == "AAA"

        raw_keys = [key async for key in fake_redis.scan_iter("*")]
        assert raw_keys, "trusted identity must write exactly one shared cache key"
        for key in raw_keys:
            assert "shared-account-x" not in key
        assert "shared-account-x" not in caplog.text
    finally:
        cache_module.reset_shared_portfolio_snapshot_cache()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_different_trusted_injected_identities_isolate_cash_and_lock_namespace(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """ROB-1310 R10 / B5: distinct trustworthy identities must isolate not
    only positions but cash and the singleflight lock namespace too -- and
    never leak either raw identity string into a Redis key or a log."""
    import app.services.toss_portfolio_snapshot_cache as cache_module

    caplog.set_level("DEBUG")
    fake_redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    cache_module.reset_shared_portfolio_snapshot_cache()
    monkeypatch.setattr(cache_module.redis, "from_url", lambda *a, **k: fake_redis)
    try:
        client_a = _SymbolTossClient("AAA", snapshot_identity="scope-a")
        client_b = _SymbolTossClient("BBB", snapshot_identity="scope-b")

        snapshot_a = await fetch_toss_portfolio_snapshot(
            client=client_a, need_cash=True, use_shared_snapshot=True
        )
        snapshot_b = await fetch_toss_portfolio_snapshot(
            client=client_b, need_cash=True, use_shared_snapshot=True
        )

        assert client_a.holdings_calls == 1
        assert client_b.holdings_calls == 1
        assert snapshot_a.positions[0].symbol == "AAA"
        assert snapshot_b.positions[0].symbol == "BBB"
        assert snapshot_a.cash_krw == Decimal("100")
        assert snapshot_b.cash_krw == Decimal("100")

        raw_keys = [key async for key in fake_redis.scan_iter("*")]
        # Positions AND cash AND the singleflight lock namespace must all be
        # isolated per identity -- at least 4 distinct keys (2 identities x
        # {positions, cash}); locks are released after use so may not remain.
        assert len(set(raw_keys)) >= 4
        for key in raw_keys:
            assert "scope-a" not in key
            assert "scope-b" not in key
        assert "scope-a" not in caplog.text
        assert "scope-b" not in caplog.text
    finally:
        cache_module.reset_shared_portfolio_snapshot_cache()

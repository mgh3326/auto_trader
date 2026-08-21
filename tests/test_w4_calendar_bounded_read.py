"""W4 calendar bounded read-model: SQL bound, HTTP-zero, typed 503, relation keys.

RED-before-fix assertions that must fail on the W2 parent:
1. N=1 and N=50 execute the same query count, and the warm route stays <= 4.
2. ``build_calendar`` loads freshness partitions exactly once.
3. Calendar DI constructs KIS/Upbit/Toss/paper home readers zero times.
4. Held ``KRW-BTC`` matches event ``BTC``.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, get_args, get_type_hints
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import pytest_asyncio
from fastapi import HTTPException
from fastapi.params import Depends as DependsParam
from sqlalchemy import delete, select
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.market_events import (
    MarketEvent,
    MarketEventIngestionPartition,
    MarketEventValue,
)
from app.schemas.calendar_freshness import CalendarCoverage, CoverageMatrixResponse
from app.services.invest_home_service import (
    InvestHomeService,
)
from app.services.invest_view_model.relation_resolver import RelationResolver
from tests.market_events_test_helpers import market_events_test_lock

_DAY = date(2099, 12, 3)
_FAKE_SECRET = "fake-calendar-secret-w4-not-a-real-credential"


@pytest_asyncio.fixture
async def _market_events_lock():
    async with market_events_test_lock():
        yield


async def _clear_day(db: AsyncSession, day: date) -> None:
    event_ids = select(MarketEvent.id).where(MarketEvent.event_date == day)
    await db.execute(
        delete(MarketEventValue).where(MarketEventValue.event_id.in_(event_ids))
    )
    await db.execute(delete(MarketEvent).where(MarketEvent.event_date == day))
    await db.execute(
        delete(MarketEventIngestionPartition).where(
            MarketEventIngestionPartition.partition_date == day
        )
    )
    await db.flush()


def _calendar_service_dependency():
    from app.routers import invest_api

    hints = get_type_hints(invest_api.get_calendar, include_extras=True)
    annotation = hints["service"]
    for arg in get_args(annotation):
        if isinstance(arg, DependsParam):
            return arg.dependency
    raise AssertionError(f"calendar service param has no Depends: {annotation!r}")


class _ExecuteCounter:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.statements: list[str] = []
        self._orig = session.execute

    async def __aenter__(self) -> _ExecuteCounter:
        async def _counting(statement: Any, *args: Any, **kwargs: Any):
            compiled = statement.compile(compile_kwargs={"literal_binds": False})
            self.statements.append(str(compiled))
            return await self._orig(statement, *args, **kwargs)

        self.session.execute = _counting  # type: ignore[method-assign]
        return self

    async def __aexit__(self, *_exc: object) -> None:
        self.session.execute = self._orig  # type: ignore[method-assign]

    @property
    def count(self) -> int:
        return len(self.statements)


class _WarmHeldKeyService:
    def __init__(self, pairs: list[tuple[str, str]] | None = None) -> None:
        self.pairs = pairs or [("crypto", "KRW-BTC")]
        self.calls: list[dict[str, Any]] = []

    async def get_held_pairs(self, **kwargs: Any) -> list[tuple[str, str]]:
        self.calls.append(kwargs)
        return list(self.pairs)


def _fake_event(
    *,
    event_id: str,
    market: str = "us",
    category: str = "earnings",
    symbol: str | None = None,
    company_name: str | None = None,
    title: str | None = None,
    ev_date: date | None = None,
    actual: object | None = None,
    forecast: object | None = None,
    previous: object | None = None,
    values: list[object] | None = None,
    source: str = "test",
):
    event = MagicMock()
    event.source_event_id = event_id
    event.id = event_id
    event.market = market
    event.category = category
    event.symbol = symbol
    event.company_name = company_name if company_name is not None else symbol
    event.title = f"event {event_id}" if title is None else title
    event.event_date = ev_date or date(2026, 5, 4)
    event.release_time_utc = None
    event.source = source
    event.currency = None
    event.country = None
    event.importance = None
    if values is not None:
        event.values = values
    elif actual is not None or forecast is not None or previous is not None:
        value = MagicMock()
        value.actual = actual
        value.forecast = forecast
        value.previous = previous
        event.values = [value]
    else:
        event.values = []
    return event


def _empty_coverage(from_date: date, to_date: date) -> CoverageMatrixResponse:
    return CoverageMatrixResponse(
        fromDate=from_date,
        toDate=to_date,
        asOf=datetime.now(UTC),
        sources=[],
        partitions=[],
        coverage=CalendarCoverage(
            fromDate=from_date,
            toDate=to_date,
            expectedPartitions=0,
            succeededPartitions=0,
            failedPartitions=0,
            missingPartitions=0,
            totalEvents=0,
        ),
    )


def _patch_query(monkeypatch, events: list[object]) -> None:
    from app.services.invest_view_model import calendar_service as svc

    fake_resp = MagicMock()
    fake_resp.events = events
    fake_query = MagicMock()
    fake_query.list_for_range = AsyncMock(return_value=fake_resp)
    monkeypatch.setattr(svc, "MarketEventsQueryService", lambda db: fake_query)


def _patch_freshness(monkeypatch, from_date: date, to_date: date) -> MagicMock:
    from app.services.invest_view_model import calendar_service as svc

    fake_freshness = MagicMock()
    fake_freshness.get_per_day_states = AsyncMock(return_value={})
    fake_freshness.get_coverage_matrix = AsyncMock(
        return_value=_empty_coverage(from_date, to_date)
    )
    monkeypatch.setattr(svc, "MarketEventsFreshnessService", lambda db: fake_freshness)
    return fake_freshness


async def _seed_events(db: AsyncSession, count: int) -> None:
    from app.services.market_events.repository import MarketEventsRepository

    repo = MarketEventsRepository(db)
    for index in range(count):
        await repo.upsert_event_with_values(
            {
                "category": "earnings",
                "market": "us",
                "symbol": f"W{index:03d}",
                "title": f"seed {index}",
                "event_date": _DAY,
                "status": "scheduled",
                "source": "finnhub",
                "source_event_id": f"w4-sql-{index}",
                "raw_payload_json": {"secret": _FAKE_SECRET, "n": index},
            },
            [
                {
                    "metric_name": "eps",
                    "period": "Q1",
                    "actual": Decimal("1.00"),
                    "forecast": Decimal("0.90"),
                    "previous": Decimal("0.80"),
                }
            ],
        )
    await db.flush()


async def _run_warm_route(db: AsyncSession) -> None:
    from app.routers import invest_api

    await invest_api.get_calendar(
        user=SimpleNamespace(id=1),
        service=_WarmHeldKeyService(),
        db=db,
        from_date=_DAY,
        to_date=_DAY,
        tab="all",
        include_paper=False,
        paper_sources=None,
    )


# ---------------------------------------------------------------------------
# RED: N-independent SQL bound
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.usefixtures("_market_events_lock")
async def test_warm_calendar_route_sql_is_bounded_and_independent_of_event_count(
    db_session: AsyncSession,
) -> None:
    await _clear_day(db_session, _DAY)

    await _seed_events(db_session, 1)
    async with _ExecuteCounter(db_session) as one:
        await _run_warm_route(db_session)
    one_count = one.count
    one_sql = list(one.statements)

    await _clear_day(db_session, _DAY)
    await _seed_events(db_session, 50)
    async with _ExecuteCounter(db_session) as fifty:
        await _run_warm_route(db_session)
    fifty_count = fifty.count

    assert one_count == fifty_count, (
        f"query count must not grow with N: N=1 -> {one_count}, N=50 -> {fifty_count}"
    )
    assert one_count <= 4, (
        f"warm valid snapshot route SQL must be <= 4, got {one_count}"
    )
    assert fifty_count <= 4
    joined = "\n".join(one_sql).lower()
    assert "raw_payload_json" not in joined


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.usefixtures("_market_events_lock")
async def test_build_calendar_sql_stays_within_three_independent_of_n(
    db_session: AsyncSession,
) -> None:
    from app.services.invest_view_model.calendar_service import build_calendar

    await _clear_day(db_session, _DAY)
    await _seed_events(db_session, 1)
    resolver = RelationResolver(held={("crypto", "KRW-BTC")})
    async with _ExecuteCounter(db_session) as one:
        await build_calendar(
            db=db_session,
            resolver=resolver,
            from_date=_DAY,
            to_date=_DAY,
            tab="all",
        )
    await _clear_day(db_session, _DAY)
    await _seed_events(db_session, 50)
    async with _ExecuteCounter(db_session) as fifty:
        await build_calendar(
            db=db_session,
            resolver=resolver,
            from_date=_DAY,
            to_date=_DAY,
            tab="all",
        )
    assert one.count == fifty.count
    assert one.count <= 3, f"build_calendar SQL must be <= 3, got {one.count}"


# ---------------------------------------------------------------------------
# RED: freshness partition loader once
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.usefixtures("_market_events_lock")
async def test_build_calendar_loads_freshness_partitions_once(
    db_session: AsyncSession, monkeypatch
) -> None:
    from app.services.invest_view_model.calendar_service import build_calendar
    from app.services.market_events.freshness_service import (
        MarketEventsFreshnessService,
    )

    await _clear_day(db_session, _DAY)
    await _seed_events(db_session, 2)

    calls = {"n": 0}
    original = MarketEventsFreshnessService._load_partitions

    async def _counting(self, from_date, to_date):
        calls["n"] += 1
        return await original(self, from_date, to_date)

    monkeypatch.setattr(MarketEventsFreshnessService, "_load_partitions", _counting)

    await build_calendar(
        db=db_session,
        resolver=RelationResolver(),
        from_date=_DAY,
        to_date=_DAY,
        tab="all",
    )
    assert calls["n"] == 1


# ---------------------------------------------------------------------------
# RED: calendar DI constructs no broker-capable home readers
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_calendar_di_does_not_construct_home_readers(monkeypatch) -> None:
    from app.core.config import settings
    from app.services import invest_home_readers as readers
    from app.services import invest_quote_service as quotes

    constructed: list[str] = []

    def _count(name: str):
        def _factory(*_a: object, **_k: object) -> object:
            constructed.append(name)
            return SimpleNamespace()

        return _factory

    monkeypatch.setattr(settings, "toss_api_enabled", True)
    monkeypatch.setattr(readers, "SafeKISClient", _count("kis_client"))
    monkeypatch.setattr(readers, "KISHomeReader", _count("kis"))
    monkeypatch.setattr(readers, "UpbitHomeReader", _count("upbit"))
    monkeypatch.setattr(readers, "TossApiHomeReader", _count("toss_api"))
    monkeypatch.setattr(readers, "KISMockHomeReader", _count("kis_mock"))
    monkeypatch.setattr(readers, "AlpacaPaperHomeReader", _count("alpaca_paper"))
    monkeypatch.setattr(quotes, "InvestQuoteService", _count("quote"))

    factory = _calendar_service_dependency()
    factory(db=object())
    assert constructed == []


# ---------------------------------------------------------------------------
# RED: crypto held-key dialect
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_held_krw_btc_matches_event_btc() -> None:
    resolver = RelationResolver(held={("crypto", "KRW-BTC")})
    assert resolver.relation("crypto", "BTC") == "held"
    assert resolver.relation("crypto", "KRW-BTC") == "held"
    assert resolver.relation("crypto", "krw-btc") == "held"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_calendar_badges_held_krw_btc_against_event_btc(monkeypatch) -> None:
    from app.services.invest_view_model.calendar_service import build_calendar

    _patch_query(
        monkeypatch,
        [
            _fake_event(
                event_id="btc-event",
                market="crypto",
                category="crypto",
                symbol="BTC",
                title="BTC unlock",
            )
        ],
    )
    _patch_freshness(monkeypatch, date(2026, 5, 4), date(2026, 5, 4))
    resp = await build_calendar(
        db=MagicMock(),
        resolver=RelationResolver(held={("crypto", "KRW-BTC")}),
        from_date=date(2026, 5, 4),
        to_date=date(2026, 5, 4),
        tab="all",
    )
    event = resp.days[0].events[0]
    assert event.relation == "held"
    assert "holdings" in event.badges
    assert event.displayPriority >= 1000


# ---------------------------------------------------------------------------
# Preservation: HTTP/socket zero, 503 keys, symbols, badges, scope
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_warm_and_cold_calendar_paths_attempt_http_zero_times(
    monkeypatch,
) -> None:
    from app.routers import invest_api

    http_attempts: list[str] = []

    async def _blocked_send(self, *args: object, **kwargs: object):
        http_attempts.append("send")
        raise AssertionError("calendar must not attempt HTTP")

    monkeypatch.setattr(httpx.AsyncClient, "send", _blocked_send)
    monkeypatch.setattr(
        invest_api,
        "build_relation_resolver",
        AsyncMock(return_value=RelationResolver()),
    )
    monkeypatch.setattr(
        invest_api, "build_calendar", AsyncMock(return_value={"ok": True})
    )

    result = await invest_api.get_calendar(
        user=SimpleNamespace(id=1),
        service=_WarmHeldKeyService(),
        db=object(),
        from_date=_DAY,
        to_date=_DAY,
        tab="all",
        include_paper=False,
        paper_sources=None,
    )
    assert result == {"ok": True}

    class _BrokenManual:
        async def fetch(self, *, user_id: int):
            raise AssertionError("calendar must not run full manual fetch")

        async def fetch_held_pairs(self, *, user_id: int):
            raise RuntimeError(f"database password={_FAKE_SECRET}")

    class _NoLive:
        async def fetch(self, *, user_id: int):
            raise AssertionError("calendar must not run live readers")

    import fakeredis.aioredis

    from app.services.portfolio_snapshot_cache import PortfolioSnapshotCache

    service = InvestHomeService(
        kis_reader=_NoLive(),
        upbit_reader=_NoLive(),
        manual_reader=_BrokenManual(),
        toss_api_reader=_NoLive(),
        snapshot_cache=PortfolioSnapshotCache(
            redis_client=fakeredis.aioredis.FakeRedis(decode_responses=True),
            ttl_seconds=5,
        ),
    )
    with pytest.raises(HTTPException) as caught:
        await invest_api.get_calendar(
            user=SimpleNamespace(id=1),
            service=service,
            db=object(),
            from_date=_DAY,
            to_date=_DAY,
            tab="all",
            include_paper=False,
            paper_sources=None,
        )
    assert caught.value.status_code == 503
    assert http_attempts == []
    assert _FAKE_SECRET not in repr(caught.value)
    assert caught.value.detail == {
        "error_code": "portfolio_snapshot_unavailable",
        "source": "portfolio_snapshot",
        "unavailable_reason": "held_key_projection_unavailable",
        "manual_pairs_available": False,
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_exact_503_keys_for_cold_corrupt_unusable_and_manual_failure() -> None:
    import fakeredis.aioredis

    from app.routers import invest_api
    from app.services.portfolio_snapshot_cache import PortfolioSnapshotCache

    class _Manual:
        async def fetch_held_pairs(self, *, user_id: int):
            return [("kr", "005930")]

        async def fetch(self, *, user_id: int):
            raise AssertionError("held-key path must not call full fetch")

    class _NoLive:
        async def fetch(self, *, user_id: int):
            raise AssertionError("no live")

    async def _expect_503(service, reason: str, *, manual_available: bool) -> None:
        with pytest.raises(HTTPException) as caught:
            await invest_api.get_calendar(
                user=SimpleNamespace(id=1),
                service=service,
                db=object(),
                from_date=_DAY,
                to_date=_DAY,
                tab="all",
                include_paper=False,
                paper_sources=None,
            )
        assert caught.value.status_code == 503
        assert set(caught.value.detail) == {
            "error_code",
            "source",
            "unavailable_reason",
            "manual_pairs_available",
        }
        assert caught.value.detail["error_code"] == "portfolio_snapshot_unavailable"
        assert caught.value.detail["source"] == "portfolio_snapshot"
        assert caught.value.detail["unavailable_reason"] == reason
        assert caught.value.detail["manual_pairs_available"] is manual_available

    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    cache = PortfolioSnapshotCache(redis_client=redis_client, ttl_seconds=5)
    cold = InvestHomeService(
        kis_reader=_NoLive(),
        upbit_reader=_NoLive(),
        manual_reader=_Manual(),
        toss_api_reader=_NoLive(),
        snapshot_cache=cache,
    )
    await _expect_503(
        cold, "held_key_projection_missing_or_invalid", manual_available=True
    )

    scope_cache = PortfolioSnapshotCache(redis_client=redis_client, ttl_seconds=5)
    from app.services.portfolio_snapshot import portfolio_snapshot_scope

    scope = portfolio_snapshot_scope(user_id=1, include_paper=False, paper_sources=None)
    await scope_cache.put(scope, {"schema_version": 1, "held_pairs": "corrupt"})
    corrupt = InvestHomeService(
        kis_reader=_NoLive(),
        upbit_reader=_NoLive(),
        manual_reader=_Manual(),
        toss_api_reader=_NoLive(),
        snapshot_cache=scope_cache,
    )
    await _expect_503(
        corrupt, "held_key_projection_missing_or_invalid", manual_available=True
    )

    unusable = InvestHomeService(
        kis_reader=_NoLive(),
        upbit_reader=_NoLive(),
        manual_reader=_Manual(),
        toss_api_reader=_NoLive(),
        snapshot_cache=None,
    )
    await _expect_503(unusable, "snapshot_cache_unusable", manual_available=True)

    class _BrokenManual:
        async def fetch_held_pairs(self, *, user_id: int):
            raise RuntimeError(f"password={_FAKE_SECRET}")

        async def fetch(self, *, user_id: int):
            raise AssertionError("no full fetch")

    broken = InvestHomeService(
        kis_reader=_NoLive(),
        upbit_reader=_NoLive(),
        manual_reader=_BrokenManual(),
        toss_api_reader=_NoLive(),
        snapshot_cache=cache,
    )
    with pytest.raises(HTTPException) as caught:
        await invest_api.get_calendar(
            user=SimpleNamespace(id=1),
            service=broken,
            db=object(),
            from_date=_DAY,
            to_date=_DAY,
            tab="all",
            include_paper=False,
            paper_sources=None,
        )
    assert (
        caught.value.detail["unavailable_reason"] == "held_key_projection_unavailable"
    )
    assert caught.value.detail["manual_pairs_available"] is False
    assert _FAKE_SECRET not in repr(caught.value)


@pytest.mark.unit
def test_relation_preserves_brk_and_held_watch_both() -> None:
    held = RelationResolver(held={("us", "BRK.B")})
    assert held.relation("us", "BRK/B") == "held"
    assert held.relation("us", "BRK-B") == "held"
    both = RelationResolver(held={("us", "BRK.B")}, watch={("us", "BRK.B")})
    assert both.relation("us", "BRK-B") == "both"
    watch = RelationResolver(watch={("kr", "005930")})
    assert watch.relation("kr", "005930") == "watchlist"


@pytest.mark.unit
def test_hidden_and_paper_snapshot_scopes_stay_distinct() -> None:
    from app.services.portfolio_snapshot import (
        held_pairs_from_portfolio_snapshot,
        portfolio_snapshot_scope,
        serialize_portfolio_snapshot,
    )
    from tests.test_rob1310_portfolio_snapshot import _held_key_home_response

    live = portfolio_snapshot_scope(user_id=1, include_paper=False, paper_sources=None)
    paper = portfolio_snapshot_scope(user_id=1, include_paper=True, paper_sources=None)
    kis_mock = portfolio_snapshot_scope(
        user_id=1, include_paper=True, paper_sources=frozenset({"kis_mock"})
    )
    assert live != paper != kis_mock
    payload = serialize_portfolio_snapshot(_held_key_home_response())
    assert held_pairs_from_portfolio_snapshot(payload) == [
        ("crypto", "KRW-BTC"),
        ("us", "BRK.B"),
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_calendar_never_calls_home_quote_fx_or_sellable(monkeypatch) -> None:
    from app.routers import invest_api

    service = MagicMock()
    service.get_held_pairs = AsyncMock(return_value=[("us", "AAPL")])
    service.get_home = AsyncMock(side_effect=AssertionError("no get_home"))
    monkeypatch.setattr(
        invest_api, "build_relation_resolver", AsyncMock(return_value=object())
    )
    monkeypatch.setattr(
        invest_api, "build_calendar", AsyncMock(return_value={"ok": True})
    )
    result = await invest_api.get_calendar(
        user=SimpleNamespace(id=1),
        service=service,
        db=object(),
        from_date=_DAY,
        to_date=_DAY,
        tab="all",
        include_paper=True,
        paper_sources="kis_mock",
    )
    assert result == {"ok": True}
    service.get_held_pairs.assert_awaited_once()
    kwargs = service.get_held_pairs.await_args.kwargs
    assert kwargs["include_paper"] is True
    assert kwargs["paper_sources"] == frozenset({"kis_mock"})
    service.get_home.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_economic_duplicate_prefers_tradingview(monkeypatch) -> None:
    from app.services.invest_view_model.calendar_service import build_calendar

    _patch_query(
        monkeypatch,
        [
            _fake_event(
                event_id="ff-cpi",
                category="economic",
                market="global",
                source="forexfactory",
                title="US CPI",
                forecast="0.3",
                previous="0.2",
            ),
            _fake_event(
                event_id="tv-cpi",
                category="economic",
                market="global",
                source="tradingview",
                title="US CPI",
                actual="0.4",
                forecast="0.3",
                previous="0.2",
            ),
        ],
    )
    _patch_freshness(monkeypatch, date(2026, 5, 4), date(2026, 5, 4))
    resp = await build_calendar(
        db=MagicMock(),
        resolver=RelationResolver(),
        from_date=date(2026, 5, 4),
        to_date=date(2026, 5, 4),
        tab="economic",
    )
    events = resp.days[0].events
    assert len(events) == 1
    assert events[0].eventId == "tv-cpi"
    assert events[0].actual == "0.4"


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.usefixtures("_market_events_lock")
async def test_event_value_order_and_first_value_semantics(
    db_session: AsyncSession,
) -> None:
    from app.services.invest_view_model.calendar_service import build_calendar
    from app.services.market_events.repository import MarketEventsRepository

    await _clear_day(db_session, _DAY)
    repo = MarketEventsRepository(db_session)
    first = await repo.upsert_event_with_values(
        {
            "category": "earnings",
            "market": "us",
            "symbol": "AAPL",
            "title": "AAPL first",
            "event_date": _DAY,
            "status": "released",
            "source": "finnhub",
            "source_event_id": "w4-order-aapl",
        },
        [
            {
                "metric_name": "eps",
                "period": "Q1",
                "actual": Decimal("1.10"),
                "forecast": Decimal("1.00"),
                "previous": Decimal("0.90"),
            },
            {
                "metric_name": "revenue",
                "period": "Q1",
                "actual": Decimal("9.00"),
                "forecast": Decimal("8.00"),
                "previous": Decimal("7.00"),
            },
        ],
    )
    await repo.upsert_event_with_values(
        {
            "category": "earnings",
            "market": "us",
            "symbol": "MSFT",
            "title": "MSFT second",
            "event_date": _DAY,
            "status": "released",
            "source": "finnhub",
            "source_event_id": "w4-order-msft",
        },
        [],
    )
    await db_session.flush()
    assert first.id is not None

    resp = await build_calendar(
        db=db_session,
        resolver=RelationResolver(),
        from_date=_DAY,
        to_date=_DAY,
        tab="all",
    )
    titles = [event.title for event in resp.days[0].events]
    assert titles == ["AAPL first", "MSFT second"]
    aapl = resp.days[0].events[0]
    assert aapl.actual == "1.1"
    assert aapl.forecast == "1"
    assert aapl.previous == "0.9"


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.usefixtures("_market_events_lock")
async def test_coverage_total_events_uses_pre_tab_filter_count(
    db_session: AsyncSession,
) -> None:
    from app.services.invest_view_model.calendar_service import build_calendar
    from app.services.market_events.repository import MarketEventsRepository

    await _clear_day(db_session, _DAY)
    repo = MarketEventsRepository(db_session)
    await repo.upsert_event_with_values(
        {
            "category": "earnings",
            "market": "us",
            "symbol": "AAPL",
            "event_date": _DAY,
            "status": "scheduled",
            "source": "finnhub",
            "source_event_id": "w4-tab-earn",
        },
        [],
    )
    await repo.upsert_event_with_values(
        {
            "category": "disclosure",
            "market": "kr",
            "symbol": "005930",
            "event_date": _DAY,
            "status": "scheduled",
            "source": "dart",
            "source_event_id": "w4-tab-disc",
        },
        [],
    )
    await db_session.flush()
    resp = await build_calendar(
        db=db_session,
        resolver=RelationResolver(),
        from_date=_DAY,
        to_date=_DAY,
        tab="earnings",
    )
    assert resp.meta.coverage is not None
    assert resp.meta.coverage.totalEvents == 2
    assert len(resp.days[0].events) == 1
    assert resp.days[0].events[0].eventType == "earnings"


@pytest.mark.unit
def test_calendar_held_key_service_is_not_invest_home_service() -> None:
    factory = _calendar_service_dependency()
    assert factory is not None
    assert getattr(factory, "__name__", "") != "get_invest_home_service"


@pytest.mark.unit
def test_market_event_model_still_has_raw_payload_column() -> None:
    columns = {column.key for column in sa_inspect(MarketEvent).mapper.column_attrs}
    assert "raw_payload_json" in columns

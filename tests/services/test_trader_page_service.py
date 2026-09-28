"""Task 889 — TraderPageService: cache wiring, KST-day fills, watch expiry."""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Any

import pytest

from app.models.execution_ledger import ExecutionLedger
from app.models.investment_reports import InvestmentWatchAlert
from app.models.trading import InstrumentType
from app.schemas.open_orders import OpenOrderSourceState, OpenOrdersResponse
from app.schemas.trader_page import TOSS_APP_ORDERS_NOTE, TOSS_FILL_POLLER_NOTE
from app.services.execution_ledger.fill_ingest import (
    DownstreamHooks,
    run_post_upsert_downstream,
)
from app.services.trader_page import open_orders_cache as cache_mod
from app.services.trader_page.open_orders_cache import TraderOpenOrdersCache
from app.services.trader_page.service import TraderPageService

T0 = dt.datetime(2026, 9, 28, 3, 0, tzinfo=dt.UTC)  # 12:00 KST, a Monday


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def get(self, key: str):
        return self.store.get(key)

    async def incr(self, key: str):
        value = int(self.store.get(key, "0")) + 1
        self.store[key] = str(value)
        return value


def _cache() -> tuple[TraderOpenOrdersCache, _FakeRedis]:
    redis = _FakeRedis()

    async def factory():
        return redis

    return TraderOpenOrdersCache(redis_factory=factory), redis


def _ok_source(
    broker: str, market: str, count: int, fetched_at: dt.datetime
) -> OpenOrderSourceState:
    return OpenOrderSourceState(
        broker=broker,  # type: ignore[arg-type]
        market=market,  # type: ignore[arg-type]
        status="ok",
        fetched_at=fetched_at,
        count=count,
    )


def _down_source(broker: str, market: str, fetched_at: dt.datetime):
    return OpenOrderSourceState(
        broker=broker,  # type: ignore[arg-type]
        market=market,  # type: ignore[arg-type]
        status="unavailable",
        fetched_at=fetched_at,
        count=0,
        message="RuntimeError",
    )


def _orders_response(
    sources: list[OpenOrderSourceState], as_of: dt.datetime
) -> OpenOrdersResponse:
    return OpenOrdersResponse(
        market="all",
        count=0,
        data_state="ok",
        as_of=as_of,
        items=[],
        sources=sources,
    )


class _FakeOrdersService:
    """Stands in for CurrentOrdersService; counts real broker reads."""

    def __init__(self, results: list[OpenOrdersResponse]) -> None:
        self.results = list(results)
        self.calls = 0

    async def list_open_orders(self, *, market: str = "all") -> OpenOrdersResponse:
        self.calls += 1
        assert market == "all"
        return self.results.pop(0)


class _FakeClock:
    def __init__(self, start: dt.datetime) -> None:
        self.now = start

    def __call__(self) -> dt.datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += dt.timedelta(seconds=seconds)


def _service(
    *,
    orders: _FakeOrdersService,
    cache: TraderOpenOrdersCache | None = None,
    clock: _FakeClock | None = None,
) -> TraderPageService:
    cache_inst, _ = _cache()
    return TraderPageService(
        db=None,  # open-orders path never touches the DB
        orders_service=orders,  # type: ignore[arg-type]
        cache=cache if cache is not None else cache_inst,
        clock=(clock or _FakeClock(T0)),
        ttl_seconds=45,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_open_orders_serves_cached_snapshot_within_ttl() -> None:
    orders = _FakeOrdersService([_orders_response([], T0)])
    service = _service(orders=orders)

    first = await service.open_orders()
    second = await service.open_orders()

    assert orders.calls == 1
    assert first.cache.hit is False
    assert second.cache.hit is True
    assert second.cache.ttl_seconds == 45
    assert second.cache.cached_at == T0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_open_orders_refreshes_after_ttl_expires() -> None:
    clock = _FakeClock(T0)
    orders = _FakeOrdersService([_orders_response([], T0), _orders_response([], T0)])
    service = _service(orders=orders, clock=clock)

    await service.open_orders()
    clock.advance(46)
    second = await service.open_orders()

    assert orders.calls == 2
    assert second.cache.hit is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_open_orders_manual_refresh_bypasses_cache() -> None:
    orders = _FakeOrdersService([_orders_response([], T0), _orders_response([], T0)])
    service = _service(orders=orders)

    await service.open_orders()
    refreshed = await service.open_orders(refresh=True)

    assert orders.calls == 2
    assert refreshed.cache.hit is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_open_orders_carries_toss_visibility_note() -> None:
    service = _service(orders=_FakeOrdersService([_orders_response([], T0)]))

    resp = await service.open_orders()

    assert TOSS_APP_ORDERS_NOTE in resp.notes


@pytest.mark.unit
@pytest.mark.asyncio
async def test_failed_broker_reports_last_ok_not_empty() -> None:
    """A broker failure must surface 'unavailable' + last success time."""
    t_ok = T0
    t_fail = T0 + dt.timedelta(minutes=2)
    ok_then_fail = _FakeOrdersService(
        [
            _orders_response([_ok_source("toss", "kr", 1, t_ok)], t_ok),
            _orders_response([_down_source("toss", "kr", t_fail)], t_fail),
        ]
    )
    cache_inst, _ = _cache()
    clock = _FakeClock(t_ok)
    service = _service(orders=ok_then_fail, cache=cache_inst, clock=clock)

    await service.open_orders()  # success -> records last_ok
    clock.now = t_fail
    cache_inst.invalidate_local()  # force a fresh read past TTL bookkeeping
    resp = await service.open_orders()

    src = next(s for s in resp.sources if s.broker == "toss")
    assert src.status == "unavailable"
    assert src.count == 0
    assert src.last_ok_at == t_ok  # NOT an empty-list-as-no-orders render


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fill_event_hook_invalidates_cached_snapshot(monkeypatch) -> None:
    """run_post_upsert_downstream's default commit hook busts the cache."""
    cache_inst, redis = _cache()
    monkeypatch.setattr(cache_mod, "OPEN_ORDERS_CACHE", cache_inst)

    orders = _FakeOrdersService([_orders_response([], T0), _orders_response([], T0)])
    service = _service(orders=orders, cache=cache_inst)

    await service.open_orders()
    assert orders.calls == 1

    await run_post_upsert_downstream(
        broker="kis",
        upsert_status="inserted",
        fill_order=None,  # nothing notifiable — invalidation must still fire
        raw_event=None,
        hooks=DownstreamHooks(),
    )

    resp = await service.open_orders()
    assert orders.calls == 2
    assert resp.cache.hit is False
    assert redis.store.get(cache_mod.GEN_KEY) == "1"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fill_hook_skips_duplicate_rows(monkeypatch) -> None:
    cache_inst, redis = _cache()
    monkeypatch.setattr(cache_mod, "OPEN_ORDERS_CACHE", cache_inst)
    await cache_inst.store(_orders_response([], T0), T0, 45)

    await run_post_upsert_downstream(
        broker="kis",
        upsert_status="unchanged",
        fill_order=None,
        raw_event=None,
        hooks=DownstreamHooks(),
    )

    hit, _ = await cache_inst.read(T0 + dt.timedelta(seconds=1))
    assert hit is not None  # a duplicate delivery does not churn the cache
    assert redis.store.get(cache_mod.GEN_KEY) is None


def _fill_row(**overrides: Any) -> ExecutionLedger:
    data: dict[str, Any] = {
        "broker": "kis",
        "account_mode": "live",
        "venue": "krx",
        "instrument_type": InstrumentType.equity_kr,
        "symbol": "005930",
        "raw_symbol": "005930",
        "side": "buy",
        "broker_order_id": f"ord-{uuid.uuid4()}",
        "fill_seq": 0,
        "filled_qty": Decimal("1"),
        "filled_price": Decimal("70000"),
        "filled_notional": Decimal("70000"),
        "filled_at": dt.datetime(2026, 9, 28, 3, 0, tzinfo=dt.UTC),
        "currency": "KRW",
        "source": "reconciler",
    }
    data.update(overrides)
    return ExecutionLedger(**data)


# 2026-09-28 KST = 2026-09-27 15:00 UTC .. 2026-09-28 15:00 UTC
DAY_START_UTC = dt.datetime(2026, 9, 27, 15, 0, tzinfo=dt.UTC)
DAY_END_UTC = dt.datetime(2026, 9, 28, 15, 0, tzinfo=dt.UTC)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_fills_today_filters_to_kst_day(db_session) -> None:
    db_session.add_all(
        [
            _fill_row(filled_at=DAY_START_UTC - dt.timedelta(seconds=1)),
            _fill_row(filled_at=DAY_START_UTC),
            _fill_row(filled_at=DAY_END_UTC - dt.timedelta(seconds=1)),
            _fill_row(filled_at=DAY_END_UTC),
        ]
    )
    await db_session.flush()

    service = TraderPageService(db_session, clock=_FakeClock(T0))
    resp = await service.fills_today()

    assert resp.day_kst == "2026-09-28"
    assert resp.count == 2  # only the two rows inside the KST window
    assert all(DAY_START_UTC <= r.filled_at < DAY_END_UTC for r in resp.items)
    assert TOSS_FILL_POLLER_NOTE in resp.notes


@pytest.mark.integration
@pytest.mark.asyncio
async def test_fills_today_midnight_kst_boundary(db_session) -> None:
    """An instant just past KST midnight belongs to the new day."""
    db_session.add(_fill_row(filled_at=DAY_END_UTC - dt.timedelta(minutes=1)))
    await db_session.flush()

    service = TraderPageService(
        db_session, clock=_FakeClock(DAY_END_UTC + dt.timedelta(minutes=30))
    )
    resp = await service.fills_today()

    assert resp.day_kst == "2026-09-29"
    assert resp.count == 0  # yesterday-KST fill must not leak into today


def _watch_row(*, valid_until: dt.datetime, status: str) -> InvestmentWatchAlert:
    return InvestmentWatchAlert(
        alert_uuid=uuid.uuid4(),
        idempotency_key=f"k-{uuid.uuid4()}",
        source_report_uuid=None,
        source_item_uuid=None,
        market="kr",
        target_kind="asset",
        symbol="005930",
        metric="price",
        operator="below",
        threshold=Decimal("55000"),
        threshold_key="55000",
        intent="buy_review",
        action_mode="notify_only",
        rationale="r",
        max_action={},
        alert_metadata={},
        valid_until=valid_until,
        status=status,
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_active_watches_exclude_expired_rows(db_session) -> None:
    db_session.add_all(
        [
            _watch_row(valid_until=T0 + dt.timedelta(hours=2), status="active"),
            _watch_row(
                valid_until=T0 - dt.timedelta(hours=1), status="active"
            ),  # expired but still flagged active — must not render
            _watch_row(valid_until=T0 + dt.timedelta(hours=2), status="triggered"),
        ]
    )
    await db_session.flush()

    service = TraderPageService(db_session, clock=_FakeClock(T0))
    resp = await service.active_watches()

    assert resp.count == 1
    row = resp.items[0]
    assert row.symbol == "005930"
    assert row.intent == "buy_review"
    assert row.threshold == Decimal("55000")
    assert row.valid_until == T0 + dt.timedelta(hours=2)

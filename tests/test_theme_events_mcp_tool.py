"""ROB-917: get_theme_events read-only MCP tool tests.

The tool wraps InvestMomentumEventSnapshotsRepository.list_theme_events (and, when
include_stocks=True, list_theme_event_stocks) with market-scoping, intraday
staleness tagging, and graceful empty-data handling. These tests fake the
repository/session so they run without a DB, mirroring
TestMomentumDataStateHonesty in tests/test_invest_momentum_events.py.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal

import pytest


@dataclass
class _FakeThemeRow:
    id: int
    snapshot_at: dt.datetime
    trading_date: dt.date
    event_kind: str
    source_event_key: str
    name: str
    sort_type: str
    rank: int | None
    market_type: str | None = None
    naver_theme_no: str | None = None
    naver_upjong_code: str | None = None
    change_rate: Decimal | None = None
    trade_value: Decimal | None = None
    market_cap: Decimal | None = None
    stock_count: int | None = None
    leader_symbols: list = field(default_factory=list)


@dataclass
class _FakeThemeStockRow:
    theme_snapshot_id: int
    symbol: str
    name: str | None
    rank: int | None
    order_type: str | None
    price: Decimal | None = None
    change_amount: Decimal | None = None
    change_rate: Decimal | None = None
    volume: int | None = None
    trade_value: Decimal | None = None


class _FakeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeRepo:
    last_call: dict | None = None
    rows: list = []
    stocks_by_theme_id: dict = {}

    def __init__(self, session):
        pass

    async def list_theme_events(
        self,
        *,
        trading_date=None,
        event_kind=None,
        sort_type=None,
        at=None,
        limit=50,
    ):
        type(self).last_call = {
            "trading_date": trading_date,
            "event_kind": event_kind,
            "sort_type": sort_type,
            "at": at,
            "limit": limit,
        }
        return type(self).rows

    async def list_theme_event_stocks(self, theme_snapshot_ids):
        return {
            tid: type(self).stocks_by_theme_id.get(tid, [])
            for tid in theme_snapshot_ids
        }


def _install_fake_repo(monkeypatch, mod, *, rows, stocks_by_theme_id=None):
    _FakeRepo.last_call = None
    _FakeRepo.rows = rows
    _FakeRepo.stocks_by_theme_id = stocks_by_theme_id or {}
    monkeypatch.setattr(mod, "InvestMomentumEventSnapshotsRepository", _FakeRepo)
    monkeypatch.setattr(mod, "AsyncSessionLocal", lambda: _FakeSession())


@pytest.mark.asyncio
async def test_get_theme_events_impl_returns_latest_snapshot_items(monkeypatch):
    from app.mcp_server.tooling import theme_events as mod

    snapshot_at = dt.datetime(2026, 5, 19, 9, 20, tzinfo=dt.UTC)
    row = _FakeThemeRow(
        id=1,
        snapshot_at=snapshot_at,
        trading_date=dt.date(2026, 5, 19),
        event_kind="theme",
        source_event_key="theme:591:changeRate:ALL",
        naver_theme_no="591",
        name="반도체",
        sort_type="changeRate",
        rank=1,
        market_type="ALL",
        change_rate=Decimal("5.12"),
        trade_value=Decimal("1000000000"),
        stock_count=12,
        leader_symbols=[{"symbol": "000660", "name": "SK하이닉스"}],
    )
    _install_fake_repo(monkeypatch, mod, rows=[row])
    monkeypatch.setattr(mod, "kr_market_data_state", lambda now=None: "market_closed")

    result = await mod.get_theme_events_impl(market="kr", top_n=20)

    assert result["market"] == "kr"
    assert result["data_state"] == "fresh"
    assert result["snapshot_at"] == snapshot_at.isoformat()
    assert len(result["items"]) == 1
    item = result["items"][0]
    assert item["rank"] == 1
    assert item["name"] == "반도체"
    assert item["change_rate"] == 5.12
    assert item["trade_value"] == 1000000000.0
    assert item["stock_count"] == 12
    assert item["leader_symbols"] == [{"symbol": "000660", "name": "SK하이닉스"}]
    assert "stocks" not in item


@pytest.mark.asyncio
async def test_get_theme_events_impl_passes_filters_to_repository(monkeypatch):
    from app.mcp_server.tooling import theme_events as mod

    _install_fake_repo(monkeypatch, mod, rows=[])
    monkeypatch.setattr(mod, "kr_market_data_state", lambda now=None: "market_closed")

    await mod.get_theme_events_impl(
        market="kr",
        event_kind="upjong",
        top_n=5,
        trading_date="2026-05-19",
        at="2026-05-19T09:25:00+00:00",
    )

    call = mod.InvestMomentumEventSnapshotsRepository.last_call
    assert call["event_kind"] == "upjong"
    assert call["limit"] == 5
    assert call["trading_date"] == dt.date(2026, 5, 19)
    assert call["at"] == dt.datetime(2026, 5, 19, 9, 25, tzinfo=dt.UTC)


@pytest.mark.asyncio
async def test_get_theme_events_impl_event_kind_all_maps_to_no_filter(monkeypatch):
    from app.mcp_server.tooling import theme_events as mod

    _install_fake_repo(monkeypatch, mod, rows=[])
    monkeypatch.setattr(mod, "kr_market_data_state", lambda now=None: "market_closed")

    await mod.get_theme_events_impl(market="kr", event_kind="all")

    call = mod.InvestMomentumEventSnapshotsRepository.last_call
    assert call["event_kind"] is None


@pytest.mark.asyncio
async def test_get_theme_events_impl_stale_when_intraday_gap_exceeds_20_minutes(
    monkeypatch,
):
    from app.mcp_server.tooling import theme_events as mod

    now = dt.datetime(2026, 5, 19, 9, 40, tzinfo=dt.UTC)
    stale_snapshot_at = now - dt.timedelta(minutes=25)
    row = _FakeThemeRow(
        id=1,
        snapshot_at=stale_snapshot_at,
        trading_date=dt.date(2026, 5, 19),
        event_kind="theme",
        source_event_key="theme:591:changeRate:ALL",
        name="반도체",
        sort_type="changeRate",
        rank=1,
    )
    _install_fake_repo(monkeypatch, mod, rows=[row])
    monkeypatch.setattr(
        mod, "kr_market_data_state", lambda now=None: mod.DATA_STATE_FRESH
    )
    monkeypatch.setattr(mod, "_now_utc", lambda: now)

    result = await mod.get_theme_events_impl(market="kr")

    assert result["data_state"] == "stale"


@pytest.mark.asyncio
async def test_get_theme_events_impl_fresh_when_within_20_minutes(monkeypatch):
    from app.mcp_server.tooling import theme_events as mod

    now = dt.datetime(2026, 5, 19, 9, 40, tzinfo=dt.UTC)
    fresh_snapshot_at = now - dt.timedelta(minutes=10)
    row = _FakeThemeRow(
        id=1,
        snapshot_at=fresh_snapshot_at,
        trading_date=dt.date(2026, 5, 19),
        event_kind="theme",
        source_event_key="theme:591:changeRate:ALL",
        name="반도체",
        sort_type="changeRate",
        rank=1,
    )
    _install_fake_repo(monkeypatch, mod, rows=[row])
    monkeypatch.setattr(
        mod, "kr_market_data_state", lambda now=None: mod.DATA_STATE_FRESH
    )
    monkeypatch.setattr(mod, "_now_utc", lambda: now)

    result = await mod.get_theme_events_impl(market="kr")

    assert result["data_state"] == "fresh"


@pytest.mark.asyncio
async def test_get_theme_events_impl_explicit_trading_date_skips_staleness_check(
    monkeypatch,
):
    """A caller pinning a historical trading_date asked for that snapshot on purpose."""
    from app.mcp_server.tooling import theme_events as mod

    now = dt.datetime(2026, 5, 19, 9, 40, tzinfo=dt.UTC)
    old_snapshot_at = now - dt.timedelta(hours=5)
    row = _FakeThemeRow(
        id=1,
        snapshot_at=old_snapshot_at,
        trading_date=dt.date(2026, 5, 18),
        event_kind="theme",
        source_event_key="theme:591:changeRate:ALL",
        name="반도체",
        sort_type="changeRate",
        rank=1,
    )
    _install_fake_repo(monkeypatch, mod, rows=[row])
    monkeypatch.setattr(
        mod, "kr_market_data_state", lambda now=None: mod.DATA_STATE_FRESH
    )
    monkeypatch.setattr(mod, "_now_utc", lambda: now)

    result = await mod.get_theme_events_impl(market="kr", trading_date="2026-05-18")

    assert result["data_state"] == "fresh"


@pytest.mark.asyncio
async def test_get_theme_events_impl_missing_when_no_rows(monkeypatch):
    from app.mcp_server.tooling import theme_events as mod

    _install_fake_repo(monkeypatch, mod, rows=[])
    monkeypatch.setattr(mod, "kr_market_data_state", lambda now=None: "market_closed")

    result = await mod.get_theme_events_impl(market="kr")

    assert result["data_state"] == "missing"
    assert result["items"] == []
    assert result["empty_reason"] == "no_naver_theme_snapshots"
    assert result["snapshot_at"] is None


@pytest.mark.asyncio
async def test_get_theme_events_impl_unsupported_market_short_circuits(monkeypatch):
    from app.mcp_server.tooling import theme_events as mod

    result = await mod.get_theme_events_impl(market="us")

    assert result["market"] == "us"
    assert result["data_state"] == "unsupported"
    assert result["empty_reason"] == "naver_stock_supports_kr_only"
    assert result["items"] == []


@pytest.mark.asyncio
async def test_get_theme_events_impl_include_stocks_attaches_children(monkeypatch):
    from app.mcp_server.tooling import theme_events as mod

    snapshot_at = dt.datetime(2026, 5, 19, 9, 20, tzinfo=dt.UTC)
    row = _FakeThemeRow(
        id=42,
        snapshot_at=snapshot_at,
        trading_date=dt.date(2026, 5, 19),
        event_kind="theme",
        source_event_key="theme:591:changeRate:ALL",
        name="반도체",
        sort_type="changeRate",
        rank=1,
    )
    stock = _FakeThemeStockRow(
        theme_snapshot_id=42,
        symbol="000660",
        name="SK하이닉스",
        rank=1,
        order_type="changeRate",
        price=Decimal("200000"),
        change_rate=Decimal("3.5"),
    )
    _install_fake_repo(monkeypatch, mod, rows=[row], stocks_by_theme_id={42: [stock]})
    monkeypatch.setattr(mod, "kr_market_data_state", lambda now=None: "market_closed")

    result = await mod.get_theme_events_impl(market="kr", include_stocks=True)

    item = result["items"][0]
    assert item["stocks"] == [
        {
            "symbol": "000660",
            "name": "SK하이닉스",
            "rank": 1,
            "order_type": "changeRate",
            "price": 200000.0,
            "change_amount": None,
            "change_rate": 3.5,
            "volume": None,
            "trade_value": None,
        }
    ]


@pytest.mark.asyncio
async def test_get_theme_events_impl_without_include_stocks_skips_stocks_query(
    monkeypatch,
):
    from app.mcp_server.tooling import theme_events as mod

    snapshot_at = dt.datetime(2026, 5, 19, 9, 20, tzinfo=dt.UTC)
    row = _FakeThemeRow(
        id=42,
        snapshot_at=snapshot_at,
        trading_date=dt.date(2026, 5, 19),
        event_kind="theme",
        source_event_key="theme:591:changeRate:ALL",
        name="반도체",
        sort_type="changeRate",
        rank=1,
    )

    calls: list = []

    class _TrackingRepo(_FakeRepo):
        async def list_theme_event_stocks(self, theme_snapshot_ids):
            calls.append(theme_snapshot_ids)
            return await super().list_theme_event_stocks(theme_snapshot_ids)

    _FakeRepo.last_call = None
    _FakeRepo.rows = [row]
    _FakeRepo.stocks_by_theme_id = {}
    monkeypatch.setattr(mod, "InvestMomentumEventSnapshotsRepository", _TrackingRepo)
    monkeypatch.setattr(mod, "AsyncSessionLocal", lambda: _FakeSession())
    monkeypatch.setattr(mod, "kr_market_data_state", lambda now=None: "market_closed")

    result = await mod.get_theme_events_impl(market="kr", include_stocks=False)

    assert calls == []
    assert "stocks" not in result["items"][0]


def test_mcp_theme_events_tool_is_registered():
    from app.mcp_server.tooling.analysis_registration import ANALYSIS_TOOL_NAMES

    assert "get_theme_events" in ANALYSIS_TOOL_NAMES


def test_theme_events_tool_is_read_only_advisory_bucket_member():
    from app.mcp_server.tooling.route_request_lanes import (
        ALL_KNOWN_TOOLS,
        MUTATION_TOOLS,
        READ_ONLY_ADVISORY_TOOLS,
    )

    assert "get_theme_events" in READ_ONLY_ADVISORY_TOOLS
    assert "get_theme_events" not in MUTATION_TOOLS
    assert "get_theme_events" in ALL_KNOWN_TOOLS

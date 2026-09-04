from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from sqlalchemy import delete, select

from app.jobs.invest_momentum_events import (
    NaverMomentumBuildRequest,
    run_naver_momentum_build,
)
from app.models.invest_momentum_event_snapshot import (
    InvestMomentumEventSnapshot,
    InvestThemeEventSnapshot,
    InvestThemeEventSnapshotStock,
)
from app.services.invest_momentum_events.coverage_service import build_momentum_coverage
from app.services.invest_momentum_events.models import (
    MomentumEventUpsert,
    ThemeEventUpsert,
)
from app.services.invest_momentum_events.repository import (
    InvestMomentumEventSnapshotsRepository,
)

SNAPSHOT_AT = dt.datetime(2026, 5, 13, 9, 5, tzinfo=dt.UTC)
TRADING_DATE = dt.date(2026, 5, 13)


class FixtureNaverFetcher:
    async def fetch_domestic_stock_default(self, **kwargs):
        return {
            "result": {
                "stocks": [
                    {
                        "itemcode": "005930",
                        "itemname": f"삼성전자 {kwargs['order_type']}",
                        "rank": 1,
                        "nowPrice": "78500",
                        "prevChangeRate": "1.55",
                        "tradeVolume": "14500000",
                        "tradeAmount": "1138250000000",
                    }
                ]
            }
        }

    async def fetch_market_theme_list(self, **kwargs):
        return {
            "list": [
                {
                    "themeNo": "591",
                    "themeName": "반도체",
                    "rank": 1,
                    "changeRate": "3.21",
                }
            ]
        }

    async def fetch_market_upjong_list(self, **kwargs):
        return {
            "list": [
                {
                    "upjongCode": "G101",
                    "upjongName": "전기전자",
                    "rank": 1,
                    "changeRate": "2.10",
                }
            ]
        }


class UnsupportedCombinationNaverFetcher(FixtureNaverFetcher):
    async def fetch_domestic_stock_default(self, **kwargs):
        if kwargs["trade_type"] == "NXT" and kwargs["order_type"] == "priceTop":
            request = httpx.Request(
                "GET", "https://stock.naver.com/api/domestic/market/stock/default"
            )
            response = httpx.Response(400, request=request)
            raise httpx.HTTPStatusError(
                "unsupported combination", request=request, response=response
            )
        return await super().fetch_domestic_stock_default(**kwargs)


@pytest.mark.asyncio
async def test_dry_run_job_with_fixture_fetcher_returns_counts_without_db_writes(
    db_session,
):
    dry_run_date = dt.date(2026, 5, 15)
    result = await run_naver_momentum_build(
        NaverMomentumBuildRequest(
            trade_types=("KRX", "NXT"),
            order_types=("up", "quantTop"),
            theme_sort_types=("changeRate",),
            page_size=2,
            commit=False,
            today=dry_run_date,
        ),
        fetcher=FixtureNaverFetcher(),
    )

    assert result.committed is False
    assert result.momentum_rows == 4
    assert result.theme_rows == 2
    assert result.counts_by_surface["stock:KRX:ALL:up"] == 1
    assert result.counts_by_surface["stock:NXT:ALL:quantTop"] == 1
    assert result.samples[0]["symbol"] == "005930"

    rows = (
        (
            await db_session.execute(
                select(InvestMomentumEventSnapshot).where(
                    InvestMomentumEventSnapshot.trading_date == dry_run_date
                )
            )
        )
        .scalars()
        .all()
    )
    assert rows == []


@pytest.mark.asyncio
async def test_builder_skips_unsupported_naver_surface_without_aborting():
    result = await run_naver_momentum_build(
        NaverMomentumBuildRequest(
            trade_types=("KRX", "NXT"),
            order_types=("up", "priceTop"),
            theme_sort_types=("changeRate",),
            page_size=2,
            commit=False,
        ),
        fetcher=UnsupportedCombinationNaverFetcher(),
    )

    assert result.committed is False
    assert result.momentum_rows == 3
    assert result.theme_rows == 2
    assert result.counts_by_surface["stock:NXT:ALL:priceTop"] == 0
    assert any(
        "skipped unsupported Naver surface stock:NXT:ALL:priceTop: HTTP 400" in warning
        for warning in result.warnings
    )


@pytest.mark.asyncio
async def test_repository_upserts_momentum_and_theme_idempotently(db_session):
    repo = InvestMomentumEventSnapshotsRepository(db_session)
    momentum = MomentumEventUpsert(
        snapshot_at=SNAPSHOT_AT,
        trading_date=TRADING_DATE,
        surface="domestic_market_stock_default",
        trade_type="KRX",
        market_type="ALL",
        order_type="up",
        rank=1,
        symbol="005930",
        name="삼성전자",
        price=Decimal("78500"),
        change_rate=Decimal("1.55"),
    )
    await repo.upsert_momentum(momentum)
    await repo.upsert_momentum(
        momentum.model_copy(update={"rank": 2, "price": Decimal("78600")})
    )

    theme = ThemeEventUpsert(
        snapshot_at=SNAPSHOT_AT,
        trading_date=TRADING_DATE,
        surface="market_theme_list",
        event_kind="theme",
        source_event_key="theme:591:changeRate:ALL",
        naver_theme_no="591",
        name="반도체",
        sort_type="changeRate",
        rank=1,
        market_type="ALL",
        leader_symbols=[{"symbol": "000660", "name": "SK하이닉스"}],
    )
    await repo.upsert_theme(theme)
    await repo.upsert_theme(theme.model_copy(update={"rank": 3, "stock_count": 12}))
    await db_session.commit()

    momentum_rows = (
        (
            await db_session.execute(
                select(InvestMomentumEventSnapshot).where(
                    InvestMomentumEventSnapshot.snapshot_at == SNAPSHOT_AT,
                    InvestMomentumEventSnapshot.symbol == "005930",
                    InvestMomentumEventSnapshot.order_type == "up",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(momentum_rows) == 1
    assert momentum_rows[0].rank == 2
    assert momentum_rows[0].price == Decimal("78600.000000")

    theme_rows = (
        (
            await db_session.execute(
                select(InvestThemeEventSnapshot).where(
                    InvestThemeEventSnapshot.snapshot_at == SNAPSHOT_AT,
                    InvestThemeEventSnapshot.source_event_key
                    == "theme:591:changeRate:ALL",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(theme_rows) == 1
    assert theme_rows[0].rank == 3
    assert theme_rows[0].stock_count == 12


@pytest.mark.asyncio
async def test_coverage_reports_unsupported_missing_and_fresh(db_session):
    coverage_date = dt.date(2026, 5, 14)
    coverage_snapshot_at = dt.datetime(2026, 5, 14, 9, 5, tzinfo=dt.UTC)
    stale_theme_ids = select(InvestThemeEventSnapshot.id).where(
        InvestThemeEventSnapshot.trading_date == coverage_date
    )
    await db_session.execute(
        delete(InvestThemeEventSnapshotStock).where(
            InvestThemeEventSnapshotStock.theme_snapshot_id.in_(stale_theme_ids)
        )
    )
    await db_session.execute(
        delete(InvestThemeEventSnapshot).where(
            InvestThemeEventSnapshot.trading_date == coverage_date
        )
    )
    await db_session.execute(
        delete(InvestMomentumEventSnapshot).where(
            InvestMomentumEventSnapshot.trading_date == coverage_date
        )
    )
    await db_session.commit()

    unsupported = await build_momentum_coverage(
        db_session, market="us", as_of=coverage_date
    )
    assert unsupported.dataState == "unsupported"
    assert unsupported.emptyReason == "naver_stock_supports_kr_only"

    missing = await build_momentum_coverage(
        db_session, market="kr", as_of=coverage_date
    )
    assert missing.dataState == "missing"

    repo = InvestMomentumEventSnapshotsRepository(db_session)
    await repo.upsert_momentum(
        MomentumEventUpsert(
            snapshot_at=coverage_snapshot_at,
            trading_date=coverage_date,
            surface="domestic_market_stock_default",
            trade_type="KRX",
            market_type="ALL",
            order_type="up",
            rank=1,
            symbol="005930",
        )
    )
    await db_session.commit()

    fresh = await build_momentum_coverage(db_session, market="kr", as_of=coverage_date)
    assert fresh.dataState == "fresh"
    assert fresh.momentumEvents >= 1


@pytest.mark.asyncio
async def test_repository_scores_momentum_candidates_with_cross_surface_rank_delta_and_theme(
    db_session,
):
    candidate_date = dt.date(2026, 5, 18)
    previous_snapshot_at = dt.datetime(2026, 5, 18, 9, 10, tzinfo=dt.UTC)
    latest_snapshot_at = dt.datetime(2026, 5, 18, 9, 20, tzinfo=dt.UTC)

    stale_theme_ids = select(InvestThemeEventSnapshot.id).where(
        InvestThemeEventSnapshot.trading_date == candidate_date
    )
    await db_session.execute(
        delete(InvestThemeEventSnapshotStock).where(
            InvestThemeEventSnapshotStock.theme_snapshot_id.in_(stale_theme_ids)
        )
    )
    await db_session.execute(
        delete(InvestThemeEventSnapshot).where(
            InvestThemeEventSnapshot.trading_date == candidate_date
        )
    )
    await db_session.execute(
        delete(InvestMomentumEventSnapshot).where(
            InvestMomentumEventSnapshot.trading_date == candidate_date
        )
    )

    repo = InvestMomentumEventSnapshotsRepository(db_session)
    base = MomentumEventUpsert(
        snapshot_at=previous_snapshot_at,
        trading_date=candidate_date,
        surface="domestic_market_stock_default",
        trade_type="KRX",
        market_type="ALL",
        order_type="searchTop",
        rank=18,
        symbol="123456",
        name="급등후보",
        price=Decimal("12000"),
        change_rate=Decimal("4.5"),
    )
    await repo.upsert_momentum(base)
    await repo.upsert_momentum(
        base.model_copy(update={"snapshot_at": latest_snapshot_at, "rank": 3})
    )
    await repo.upsert_momentum(
        base.model_copy(
            update={
                "snapshot_at": latest_snapshot_at,
                "trade_type": "NXT",
                "order_type": "quantTop",
                "rank": 2,
                "volume": 1_500_000,
            }
        )
    )
    await repo.upsert_momentum(
        base.model_copy(
            update={
                "snapshot_at": latest_snapshot_at,
                "symbol": "654321",
                "name": "단일신호",
                "order_type": "priceTop",
                "rank": 1,
            }
        )
    )
    await repo.upsert_theme(
        ThemeEventUpsert(
            snapshot_at=latest_snapshot_at,
            trading_date=candidate_date,
            surface="market_theme_list",
            event_kind="theme",
            source_event_key="theme:999:changeRate:ALL",
            naver_theme_no="999",
            name="AI반도체",
            sort_type="changeRate",
            rank=1,
            market_type="ALL",
            stocks=[
                {
                    "symbol": "123456",
                    "name": "급등후보",
                    "rank": 1,
                    "order_type": "changeRate",
                }
            ],
        )
    )
    await db_session.commit()

    candidates = await repo.list_candidate_signals(
        trading_date=candidate_date,
        limit=10,
    )

    assert [candidate.symbol for candidate in candidates][:2] == ["123456", "654321"]
    top = candidates[0]
    assert top.surface_count == 2
    assert top.venue_count == 2
    assert top.rank_delta == 15
    assert top.theme_names == ["AI반도체"]
    assert "multi_surface" in top.reason_codes
    assert "krx_nxt_confirmed" in top.reason_codes
    assert "rank_improving" in top.reason_codes
    assert "theme_leader" in top.reason_codes


@pytest.mark.asyncio
async def test_repository_list_recent_trading_dates_returns_distinct_dates_desc(
    db_session,
):
    """ROB-919: surge-ratio history lookup needs the N most recent trading
    dates strictly before a given date, most-recent-first."""
    symbol = "919001"
    dates = [
        dt.date(2026, 8, 3),
        dt.date(2026, 8, 4),
        dt.date(2026, 8, 5),
        dt.date(2026, 8, 6),
        dt.date(2026, 8, 7),
        dt.date(2026, 8, 10),
    ]
    repo = InvestMomentumEventSnapshotsRepository(db_session)
    for day in dates:
        await repo.upsert_momentum(
            MomentumEventUpsert(
                snapshot_at=dt.datetime(
                    day.year, day.month, day.day, 0, 40, tzinfo=dt.UTC
                ),
                trading_date=day,
                surface="domestic_market_stock_default",
                trade_type="KRX",
                market_type="ALL",
                order_type="up",
                rank=1,
                symbol=symbol,
            )
        )
    await db_session.commit()

    recent = await repo.list_recent_trading_dates(
        before_date=dt.date(2026, 8, 10), limit=3
    )
    assert recent == [
        dt.date(2026, 8, 7),
        dt.date(2026, 8, 6),
        dt.date(2026, 8, 5),
    ]


@pytest.mark.asyncio
async def test_repository_get_symbol_trade_value_near_time(db_session):
    """ROB-919: nearest-in-time trade_value within tolerance; max across
    duplicate order_type rows at the chosen snapshot_at; None outside window
    or when no rows exist for that symbol/day."""
    symbol = "919002"
    trading_date = dt.date(2026, 8, 11)
    near_at = dt.datetime(2026, 8, 11, 0, 38, tzinfo=dt.UTC)
    far_at = dt.datetime(2026, 8, 11, 2, 0, tzinfo=dt.UTC)

    repo = InvestMomentumEventSnapshotsRepository(db_session)
    await repo.upsert_momentum(
        MomentumEventUpsert(
            snapshot_at=near_at,
            trading_date=trading_date,
            surface="domestic_market_stock_default",
            trade_type="KRX",
            market_type="ALL",
            order_type="up",
            rank=1,
            symbol=symbol,
            trade_value=Decimal("1000000"),
        )
    )
    await repo.upsert_momentum(
        MomentumEventUpsert(
            snapshot_at=near_at,
            trading_date=trading_date,
            surface="domestic_market_stock_default",
            trade_type="KRX",
            market_type="ALL",
            order_type="quantTop",
            rank=2,
            symbol=symbol,
            trade_value=Decimal("1200000"),
        )
    )
    await repo.upsert_momentum(
        MomentumEventUpsert(
            snapshot_at=far_at,
            trading_date=trading_date,
            surface="domestic_market_stock_default",
            trade_type="KRX",
            market_type="ALL",
            order_type="up",
            rank=1,
            symbol=symbol,
            trade_value=Decimal("9999999"),
        )
    )
    await db_session.commit()

    target_at = dt.datetime(2026, 8, 11, 0, 40, tzinfo=dt.UTC)
    value = await repo.get_symbol_trade_value_near_time(
        symbol=symbol,
        trading_date=trading_date,
        target_at=target_at,
        tolerance=dt.timedelta(minutes=10),
    )
    assert value == Decimal("1200000")

    missing_symbol = await repo.get_symbol_trade_value_near_time(
        symbol="919999",
        trading_date=trading_date,
        target_at=target_at,
        tolerance=dt.timedelta(minutes=10),
    )
    assert missing_symbol is None


@pytest.mark.asyncio
async def test_repository_list_historical_trade_values_near_time_fills_gaps_with_none(
    db_session,
):
    """ROB-919: a day the symbol has no near-time observation (e.g. it wasn't
    ranked yet, or a listing/halt gap) must surface as None, not be skipped --
    callers rely on positional alignment with the returned trading dates."""
    symbol = "919003"
    day_with_data = dt.date(2026, 8, 12)
    day_without_data = dt.date(2026, 8, 13)
    before_date = dt.date(2026, 8, 14)

    repo = InvestMomentumEventSnapshotsRepository(db_session)
    await repo.upsert_momentum(
        MomentumEventUpsert(
            snapshot_at=dt.datetime(2026, 8, 12, 0, 40, tzinfo=dt.UTC),
            trading_date=day_with_data,
            surface="domestic_market_stock_default",
            trade_type="KRX",
            market_type="ALL",
            order_type="up",
            rank=1,
            symbol=symbol,
            trade_value=Decimal("500000"),
        )
    )
    # day_without_data: symbol never appears in any ranking that day.
    await repo.upsert_momentum(
        MomentumEventUpsert(
            snapshot_at=dt.datetime(2026, 8, 13, 0, 40, tzinfo=dt.UTC),
            trading_date=day_without_data,
            surface="domestic_market_stock_default",
            trade_type="KRX",
            market_type="ALL",
            order_type="up",
            rank=1,
            symbol="919004",
        )
    )
    await db_session.commit()

    values = await repo.list_historical_trade_values_near_time(
        symbol=symbol,
        before_date=before_date,
        target_time_of_day=dt.time(0, 40),
        lookback_days=2,
        tolerance=dt.timedelta(minutes=10),
    )
    assert values == [None, Decimal("500000")]


@pytest.mark.asyncio
async def test_repository_list_theme_events_at_cutoff_picks_prior_snapshot(db_session):
    """ROB-917: an ``at`` cutoff must select the latest snapshot at-or-before it."""
    theme_date = dt.date(2026, 5, 19)
    earlier_at = dt.datetime(2026, 5, 19, 9, 10, tzinfo=dt.UTC)
    later_at = dt.datetime(2026, 5, 19, 9, 30, tzinfo=dt.UTC)

    repo = InvestMomentumEventSnapshotsRepository(db_session)
    earlier = ThemeEventUpsert(
        snapshot_at=earlier_at,
        trading_date=theme_date,
        surface="market_theme_list",
        event_kind="theme",
        source_event_key="theme:rob917:changeRate:ALL",
        naver_theme_no="rob917",
        name="ROB917테마",
        sort_type="changeRate",
        rank=1,
        market_type="ALL",
        change_rate=Decimal("1.0"),
    )
    await repo.upsert_theme(earlier)
    await repo.upsert_theme(
        earlier.model_copy(
            update={"snapshot_at": later_at, "change_rate": Decimal("5.0")}
        )
    )
    await db_session.commit()

    cutoff_rows = await repo.list_theme_events(
        trading_date=theme_date,
        at=dt.datetime(2026, 5, 19, 9, 20, tzinfo=dt.UTC),
    )
    assert [row.source_event_key for row in cutoff_rows] == [
        "theme:rob917:changeRate:ALL"
    ]
    assert cutoff_rows[0].snapshot_at == earlier_at
    assert cutoff_rows[0].change_rate == Decimal("1.0000")

    latest_rows = await repo.list_theme_events(trading_date=theme_date)
    assert latest_rows[0].snapshot_at == later_at
    assert latest_rows[0].change_rate == Decimal("5.0000")


@pytest.mark.asyncio
async def test_repository_list_theme_event_stocks_groups_children_by_parent(db_session):
    theme_date = dt.date(2026, 5, 20)
    snapshot_at = dt.datetime(2026, 5, 20, 9, 10, tzinfo=dt.UTC)

    repo = InvestMomentumEventSnapshotsRepository(db_session)
    theme_id = await repo.upsert_theme(
        ThemeEventUpsert(
            snapshot_at=snapshot_at,
            trading_date=theme_date,
            surface="market_theme_list",
            event_kind="theme",
            source_event_key="theme:rob917stocks:changeRate:ALL",
            naver_theme_no="rob917stocks",
            name="ROB917자식테마",
            sort_type="changeRate",
            rank=1,
            market_type="ALL",
            stocks=[
                {
                    "symbol": "111111",
                    "name": "가나",
                    "rank": 1,
                    "order_type": "changeRate",
                },
                {
                    "symbol": "222222",
                    "name": "다라",
                    "rank": 2,
                    "order_type": "changeRate",
                },
            ],
        )
    )
    await db_session.commit()
    assert theme_id is not None

    grouped = await repo.list_theme_event_stocks([theme_id])
    assert [stock.symbol for stock in grouped[theme_id]] == ["111111", "222222"]

    empty = await repo.list_theme_event_stocks([])
    assert empty == {}


def test_mcp_momentum_candidate_tool_is_registered():
    from app.mcp_server.tooling.analysis_registration import ANALYSIS_TOOL_NAMES

    assert "get_momentum_candidates" in ANALYSIS_TOOL_NAMES


def test_no_scheduler_or_broker_order_watch_mutation_imports_in_new_modules():
    root = Path(__file__).resolve().parents[1]
    files = [
        root / "app/jobs/invest_momentum_events.py",
        *sorted((root / "app/services/invest_momentum_events").glob("*.py")),
        *sorted((root / "app/services/naver_stock").glob("*.py")),
    ]
    text = "\n".join(path.read_text(encoding="utf-8") for path in files)

    forbidden = (
        "broker.task",
        "app.core.scheduler",
        "place_order",
        "cancel_order",
        "watch_order_intent",
        "alpaca_paper_submit_order",
        "kis_live_place_order",
    )
    for token in forbidden:
        assert token not in text


def test_momentum_event_recurring_schedule_is_disabled_by_default_and_gated(
    monkeypatch,
):
    from app.core.config import settings
    from app.tasks.invest_momentum_event_tasks import (
        _csv_tuple,
        _scheduled_naver_momentum_labels,
    )

    monkeypatch.setattr(settings, "invest_momentum_events_scheduler_enabled", False)
    assert _scheduled_naver_momentum_labels() == []

    monkeypatch.setattr(settings, "invest_momentum_events_scheduler_enabled", True)
    monkeypatch.setattr(
        settings, "invest_momentum_events_scheduler_cron", "*/10 9-15 * * 1-5"
    )
    assert _scheduled_naver_momentum_labels() == [
        {"cron": "*/10 9-15 * * 1-5", "cron_offset": "Asia/Seoul"}
    ]
    assert _csv_tuple("KRX,NXT") == ("KRX", "NXT")


class TestMomentumDataStateHonesty:
    """A 2.5-week-old partition must NOT be labeled fresh (ROB-389 regression)."""

    @pytest.mark.asyncio
    async def test_stale_partition_is_labeled_stale_with_days(self, monkeypatch):
        import datetime as dt

        from app.mcp_server.tooling import momentum_candidates as mod
        from app.services.invest_momentum_events.repository import (
            MomentumCandidateSignal,
        )

        old_date = dt.date(2026, 5, 13)
        row = MomentumCandidateSignal(
            symbol="000050",
            name="가나다",
            score=1.0,
            latest_snapshot_at=dt.datetime(2026, 5, 13, 11, 0, tzinfo=dt.UTC),
            trading_date=old_date,
            price=Decimal("1000"),
            change_rate=Decimal("1.0"),
            surface_count=1,
            venue_count=1,
            rank_delta=None,
            signals=[],
            theme_names=[],
            reason_codes=[],
        )

        class _FakeRepo:
            def __init__(self, session):
                pass

            async def list_candidate_signals(self, *, trading_date=None, limit=20):
                return [row]

        class _FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        monkeypatch.setattr(mod, "InvestMomentumEventSnapshotsRepository", _FakeRepo)
        monkeypatch.setattr(mod, "AsyncSessionLocal", lambda: _FakeSession())

        result = await mod.get_momentum_candidates_impl(market="kr", limit=20)

        assert result["data_state"] == "stale"
        assert result["days_stale"] >= 1
        assert result["latest_trading_date"] == old_date.isoformat()
        assert "expected_baseline_date" in result

    @pytest.mark.asyncio
    async def test_empty_rows_is_missing(self, monkeypatch):
        from app.mcp_server.tooling import momentum_candidates as mod

        class _FakeRepo:
            def __init__(self, session):
                pass

            async def list_candidate_signals(self, *, trading_date=None, limit=20):
                return []

        class _FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        monkeypatch.setattr(mod, "InvestMomentumEventSnapshotsRepository", _FakeRepo)
        monkeypatch.setattr(mod, "AsyncSessionLocal", lambda: _FakeSession())

        result = await mod.get_momentum_candidates_impl(market="kr", limit=20)
        assert result["data_state"] == "missing"


class TestMomentumSurgeRatioWiring:
    """ROB-919: get_momentum_candidates exposes trade_value_surge_ratio per item."""

    @pytest.mark.asyncio
    async def test_item_with_trade_value_gets_computed_surge_ratio(self, monkeypatch):
        from app.mcp_server.tooling import momentum_candidates as mod
        from app.services.invest_momentum_events.repository import (
            MomentumCandidateSignal,
        )

        trading_date = dt.date(2026, 8, 20)
        latest_at = dt.datetime(2026, 8, 20, 0, 40, tzinfo=dt.UTC)
        row = MomentumCandidateSignal(
            symbol="919101",
            name="써지테스트",
            score=50.0,
            latest_snapshot_at=latest_at,
            trading_date=trading_date,
            price=Decimal("10000"),
            change_rate=Decimal("3.5"),
            surface_count=1,
            venue_count=1,
            rank_delta=None,
            signals=[
                {
                    "orderType": "quantTop",
                    "tradeType": "KRX",
                    "rank": 3,
                    "rankDelta": None,
                    "changeRate": Decimal("3.5"),
                    "volume": 1000,
                    "tradeValue": Decimal("1000000"),
                }
            ],
            theme_names=[],
            reason_codes=[],
        )

        class _FakeRepo:
            def __init__(self, session):
                pass

            async def list_candidate_signals(self, *, trading_date=None, limit=20):
                return [row]

            async def list_historical_trade_values_near_time(
                self,
                *,
                symbol,
                before_date,
                target_time_of_day,
                lookback_days,
                tolerance,
            ):
                assert symbol == "919101"
                assert before_date == trading_date
                assert target_time_of_day == latest_at.time()
                assert lookback_days == 5
                return [Decimal("100000"), Decimal("100000"), Decimal("100000")]

        class _FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        monkeypatch.setattr(mod, "InvestMomentumEventSnapshotsRepository", _FakeRepo)
        monkeypatch.setattr(mod, "AsyncSessionLocal", lambda: _FakeSession())

        result = await mod.get_momentum_candidates_impl(market="kr", limit=20)

        item = result["items"][0]
        assert item["trade_value_surge_ratio"] == 10.0
        assert item["trade_value_surge_reason"] is None
        assert item["trade_value_surge_lookback_days"] == 3

    @pytest.mark.asyncio
    async def test_item_without_trade_value_skips_history_lookup_entirely(
        self, monkeypatch
    ):
        """No tradeValue on any signal -> null ratio without ever calling the
        historical-lookback method (proves the short-circuit; a fake without
        that method would raise AttributeError if it were called)."""
        from app.mcp_server.tooling import momentum_candidates as mod
        from app.services.invest_momentum_events.repository import (
            MomentumCandidateSignal,
        )

        row = MomentumCandidateSignal(
            symbol="919102",
            name="노시그널",
            score=10.0,
            latest_snapshot_at=dt.datetime(2026, 8, 20, 0, 40, tzinfo=dt.UTC),
            trading_date=dt.date(2026, 8, 20),
            price=Decimal("5000"),
            change_rate=Decimal("2.0"),
            surface_count=1,
            venue_count=1,
            rank_delta=None,
            signals=[
                {
                    "orderType": "up",
                    "tradeType": "KRX",
                    "rank": 5,
                    "rankDelta": None,
                    "changeRate": Decimal("2.0"),
                    "volume": 500,
                    "tradeValue": None,
                }
            ],
            theme_names=[],
            reason_codes=[],
        )

        class _FakeRepoNoHistoryMethod:
            def __init__(self, session):
                pass

            async def list_candidate_signals(self, *, trading_date=None, limit=20):
                return [row]

        class _FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        monkeypatch.setattr(
            mod, "InvestMomentumEventSnapshotsRepository", _FakeRepoNoHistoryMethod
        )
        monkeypatch.setattr(mod, "AsyncSessionLocal", lambda: _FakeSession())

        result = await mod.get_momentum_candidates_impl(market="kr", limit=20)

        item = result["items"][0]
        assert item["trade_value_surge_ratio"] is None
        assert item["trade_value_surge_reason"] == "missing_current_trade_value"

    @pytest.mark.asyncio
    async def test_insufficient_history_surfaces_reason_code(self, monkeypatch):
        from app.mcp_server.tooling import momentum_candidates as mod
        from app.services.invest_momentum_events.repository import (
            MomentumCandidateSignal,
        )

        row = MomentumCandidateSignal(
            symbol="919103",
            name="신규상장",
            score=20.0,
            latest_snapshot_at=dt.datetime(2026, 8, 20, 0, 40, tzinfo=dt.UTC),
            trading_date=dt.date(2026, 8, 20),
            price=Decimal("3000"),
            change_rate=Decimal("8.0"),
            surface_count=1,
            venue_count=1,
            rank_delta=None,
            signals=[
                {
                    "orderType": "searchTop",
                    "tradeType": "KRX",
                    "rank": 1,
                    "rankDelta": None,
                    "changeRate": Decimal("8.0"),
                    "volume": 2000,
                    "tradeValue": Decimal("2000000"),
                }
            ],
            theme_names=[],
            reason_codes=[],
        )

        class _FakeRepo:
            def __init__(self, session):
                pass

            async def list_candidate_signals(self, *, trading_date=None, limit=20):
                return [row]

            async def list_historical_trade_values_near_time(self, **kwargs):
                return [None, None, Decimal("100000")]

        class _FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        monkeypatch.setattr(mod, "InvestMomentumEventSnapshotsRepository", _FakeRepo)
        monkeypatch.setattr(mod, "AsyncSessionLocal", lambda: _FakeSession())

        result = await mod.get_momentum_candidates_impl(market="kr", limit=20)

        item = result["items"][0]
        assert item["trade_value_surge_ratio"] is None
        assert item["trade_value_surge_reason"] == "insufficient_history"
        assert item["trade_value_surge_lookback_days"] == 1

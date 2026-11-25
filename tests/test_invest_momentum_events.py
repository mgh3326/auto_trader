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
async def test_repository_inserts_momentum_once_and_upserts_theme_idempotently(
    db_session,
):
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
    assert await repo.insert_momentum(momentum) is True
    assert (
        await repo.insert_momentum(
            momentum.model_copy(update={"rank": 2, "price": Decimal("78600")})
        )
        is False
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
    # hk 6154: momentum rows are insert-only, a re-sent snapshot never overwrites
    assert momentum_rows[0].rank == 1
    assert momentum_rows[0].price == Decimal("78500.000000")

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
    await repo.insert_momentum(
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
    await repo.insert_momentum(base)
    await repo.insert_momentum(
        base.model_copy(update={"snapshot_at": latest_snapshot_at, "rank": 3})
    )
    await repo.insert_momentum(
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
    await repo.insert_momentum(
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
        monkeypatch.setattr(
            mod, "classify_momentum_freshness", lambda **kwargs: ("stale", 10)
        )
        monkeypatch.setattr(
            mod,
            "expected_kr_baseline_date",
            lambda now: dt.date(2026, 5, 13),
        )

        result = await mod.get_momentum_candidates_impl(market="kr", limit=20)

        assert result == {
            "market": "kr",
            "data_state": "stale",
            "days_stale": 10,
            "expected_baseline_date": "2026-05-13",
            "latest_trading_date": "2026-05-13",
            "empty_reason": None,
            "items": [
                {
                    "symbol": "000050",
                    "name": "가나다",
                    "score": 1.0,
                    "latest_snapshot_at": "2026-05-13T11:00:00+00:00",
                    "trading_date": "2026-05-13",
                    "price": 1000.0,
                    "change_rate": 1.0,
                    "surface_count": 1,
                    "venue_count": 1,
                    "rank_delta": None,
                    "signals": [],
                    "theme_names": [],
                    "reason_codes": [],
                }
            ],
            "scoring_notes": [
                "searchTop/quantTop/up/priceTop 동시 출현을 우대",
                "KRX+NXT 동시 출현과 테마 리더 편입을 보너스로 반영",
                "동일 surface의 직전 스냅샷 대비 순위 개선(rank_delta)을 반영",
                "read-only: 네이버/브로커 요청 없이 저장된 스냅샷만 조회",
            ],
        }

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


class TestUsMomentumCandidates:
    @pytest.mark.asyncio
    async def test_aggregates_sources_excludes_rows_and_reports_latest_surface_status(
        self, monkeypatch
    ):
        from types import SimpleNamespace

        from app.mcp_server.tooling import momentum_candidates as mod

        captured = dt.datetime(2026, 11, 25, 13, 1, tzinfo=dt.UTC)

        def event(
            symbol,
            source,
            session,
            side,
            rank,
            *,
            trade_value,
            order_type=None,
            capture_offset_minutes=0,
        ):
            received_at = captured + dt.timedelta(minutes=capture_offset_minutes)
            return SimpleNamespace(
                symbol=symbol,
                source=source,
                surface=f"us:{source}:{session}:{side}",
                order_type=order_type or side,
                rank=rank,
                change_rate=Decimal("-2.5"),
                volume=1234,
                trade_value=(Decimal(trade_value) if trade_value is not None else None),
                asset_type="common_stock",
                first_received_at=received_at,
                snapshot_at=received_at + dt.timedelta(minutes=1),
            )

        rows = [
            event(
                "DELL",
                "kis_overseas_ranking",
                "us_pre",
                "down",
                12,
                trade_value="1000000",
            ),
            event(
                "DELL",
                "kis_overseas_ranking",
                "us_pre",
                "down",
                4,
                trade_value="1100000",
                capture_offset_minutes=2,
            ),
            event(
                "DELL", "alpaca_screener", "us_pre", "down", 7, trade_value="1200000"
            ),
            event("DELL", "benzinga_pro", "us_pre", "down", 3, trade_value="1300000"),
            event(
                "DELL",
                "benzinga_pro",
                "us_pre",
                "down",
                1,
                trade_value=None,
                order_type="excluded",
            ),
            event(
                "URBN",
                "kis_overseas_ranking",
                "us_pre",
                "down",
                9,
                trade_value="900000",
            ),
            event("URBN", "alpaca_screener", "us_pre", "down", 2, trade_value="800000"),
        ]
        runs = [
            SimpleNamespace(
                surface="us:kis_overseas_ranking:us_pre:down",
                status="ok",
                reason=None,
                snapshot_at=captured,
            ),
            SimpleNamespace(
                surface="us:kis_overseas_ranking:us_pre:down",
                status="error",
                reason="timeout",
                snapshot_at=captured + dt.timedelta(minutes=2),
            ),
            SimpleNamespace(
                surface="us:alpaca_screener:us_pre:down",
                status="ok",
                reason=None,
                snapshot_at=captured,
            ),
            SimpleNamespace(
                surface="us:benzinga_pro:us_pre:down",
                status="empty",
                reason="no_rows",
                snapshot_at=captured,
            ),
        ]

        class _FakeRepo:
            def __init__(self, session):
                pass

            async def list_us_momentum_events(self, *, trading_date):
                assert trading_date == dt.date(2026, 11, 25)
                return rows

            async def list_us_momentum_run_statuses(self, *, trading_date):
                assert trading_date == dt.date(2026, 11, 25)
                return sorted(runs, key=lambda run: run.snapshot_at, reverse=True)

        class _FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        monkeypatch.setattr(mod, "InvestMomentumEventSnapshotsRepository", _FakeRepo)
        monkeypatch.setattr(mod, "AsyncSessionLocal", lambda: _FakeSession())

        result = await mod.get_momentum_candidates_impl(
            market="us", date="2026-11-25", limit=20
        )

        assert [item["symbol"] for item in result["items"]] == ["URBN", "DELL"]
        dell = next(item for item in result["items"] if item["symbol"] == "DELL")
        assert dell["best_rank"] == 3
        assert dell["sources"] == [
            "alpaca_screener",
            "benzinga_pro",
            "kis_overseas_ranking",
        ]
        ranks = {
            (item["source"], item["session"], item["side"]): item["best_rank"]
            for item in dell["rankings"]
        }
        assert ranks == {
            ("kis_overseas_ranking", "us_pre", "down"): 4,
            ("alpaca_screener", "us_pre", "down"): 7,
            ("benzinga_pro", "us_pre", "down"): 3,
        }
        benzinga = next(
            item for item in dell["rankings"] if item["source"] == "benzinga_pro"
        )
        assert benzinga["source_label"] == "trial"
        assert benzinga["dollar_volume"] == "1300000"
        assert benzinga["first_captured_at"] == captured.isoformat()
        kis = next(
            item
            for item in dell["rankings"]
            if item["source"] == "kis_overseas_ranking"
        )
        assert kis["first_captured_at"] == captured.isoformat()
        assert all(
            observation["best_rank"] != 1
            for candidate in result["items"]
            for observation in candidate["rankings"]
            if observation["source"] == "benzinga_pro"
        )

        statuses = {item["source"]: item for item in result["source_statuses"]}
        kis_pre_down = next(
            item
            for item in statuses["kis_overseas_ranking"]["surfaces"]
            if item["session"] == "us_pre" and item["side"] == "down"
        )
        alpaca_missing = next(
            item
            for item in statuses["alpaca_screener"]["surfaces"]
            if item["session"] == "us_regular" and item["side"] == "down"
        )
        assert kis_pre_down["status"] == "error"
        assert kis_pre_down["reason"] == "timeout"
        assert alpaca_missing["status"] == "not_collected"

    @pytest.mark.asyncio
    async def test_us_filters_apply_without_claiming_uncollected_sources(
        self, monkeypatch
    ):
        from types import SimpleNamespace

        from app.mcp_server.tooling import momentum_candidates as mod

        captured = dt.datetime(2026, 11, 25, 13, 1, tzinfo=dt.UTC)
        real_datetime = dt.datetime

        class _Clock:
            @classmethod
            def now(cls, tz=None):
                current = real_datetime(2026, 11, 26, 0, 30, tzinfo=dt.UTC)
                return current.astimezone(tz) if tz else current.replace(tzinfo=None)

        row = SimpleNamespace(
            symbol="DELL",
            source="alpaca_screener",
            surface="us:alpaca_screener:us_pre:down",
            order_type="down",
            rank=1,
            change_rate=Decimal("-1"),
            volume=42,
            trade_value=Decimal("100000"),
            asset_type="common_stock",
            first_received_at=None,
            snapshot_at=captured,
        )
        later_row = SimpleNamespace(
            **{
                **vars(row),
                "rank": 2,
                "trade_value": Decimal("750000"),
                "first_received_at": captured + dt.timedelta(minutes=5),
                "snapshot_at": captured + dt.timedelta(minutes=5),
            }
        )

        class _FakeRepo:
            def __init__(self, session):
                pass

            async def list_us_momentum_events(self, *, trading_date):
                return [row, later_row]

            async def list_us_momentum_run_statuses(self, *, trading_date):
                return []

        class _FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        monkeypatch.setattr(mod, "InvestMomentumEventSnapshotsRepository", _FakeRepo)
        monkeypatch.setattr(mod, "AsyncSessionLocal", lambda: _FakeSession())
        monkeypatch.setattr(mod.dt, "datetime", _Clock)

        result = await mod.get_momentum_candidates_impl(
            market="us",
            session="us_pre",
            side="down",
            source="alpaca_screener",
            min_dollar_volume=700000,
            limit=1,
        )

        assert [item["symbol"] for item in result["items"]] == ["DELL"]
        assert result["items"][0]["best_rank"] == 1
        assert result["source_statuses"][0]["source"] == "alpaca_screener"
        assert {s["status"] for s in result["source_statuses"][0]["surfaces"]} == {
            "not_collected"
        }
        ranking = result["items"][0]["rankings"][0]
        assert ranking["best_rank"] == 1
        assert ranking["dollar_volume"] == "750000"
        assert ranking["dollar_volume_at"] == (
            (captured + dt.timedelta(minutes=5)).isoformat()
        )

        active_result = await mod.get_momentum_candidates_impl(
            market="us", date="2026-11-25", side="active"
        )
        active_statuses = {
            entry["source"]: entry["surfaces"]
            for entry in active_result["source_statuses"]
        }
        assert {
            entry["status"] for entry in active_statuses["kis_overseas_ranking"]
        } == {"unsupported"}
        assert {entry["status"] for entry in active_statuses["benzinga_pro"]} == {
            "unsupported"
        }
        assert {entry["status"] for entry in active_statuses["alpaca_screener"]} == {
            "not_collected"
        }

    @pytest.mark.asyncio
    async def test_active_status_is_unsupported_for_non_alpaca_sources(
        self, monkeypatch
    ):
        from app.mcp_server.tooling import momentum_candidates as mod

        class _FakeRepo:
            def __init__(self, session):
                pass

            async def list_us_momentum_events(self, *, trading_date):
                return []

            async def list_us_momentum_run_statuses(self, *, trading_date):
                return []

        class _FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        monkeypatch.setattr(mod, "InvestMomentumEventSnapshotsRepository", _FakeRepo)
        monkeypatch.setattr(mod, "AsyncSessionLocal", lambda: _FakeSession())

        result = await mod.get_momentum_candidates_impl(
            market="us", date="2026-11-25", side="active"
        )

        statuses = {
            entry["source"]: {surface["status"] for surface in entry["surfaces"]}
            for entry in result["source_statuses"]
        }
        assert statuses == {
            "kis_overseas_ranking": {"unsupported"},
            "alpaca_screener": {"not_collected"},
            "benzinga_pro": {"unsupported"},
        }

"""ROB-918: kr-preopen new-candidate shadow section tests.

The service module is read-only/advisory-only: it never touches
trading_decision_proposals, and it opens its own DB session (never the
caller's) so a read failure here can never poison a caller's write
transaction. These tests exercise the internal per-source builders directly
against the shared test_db (far-future snapshot dates keep rows isolated as
the "latest" partition, per the existing double_buy_screener test
convention) plus the top-level gating/crash-guard behavior.
"""

from __future__ import annotations

import datetime as dt
import decimal

import pytest
import pytest_asyncio
import sqlalchemy as sa

from app.models.invest_momentum_event_snapshot import (
    InvestThemeEventSnapshot,
    InvestThemeEventSnapshotStock,
)
from app.models.invest_screener_snapshot import InvestScreenerSnapshot
from app.models.kr_symbol_universe import KRSymbolUniverse
from app.models.market_valuation_snapshot import MarketValuationSnapshot
from app.services import research_run_new_candidates as svc
from app.services.daily_candles.repository import DailyCandleRow

pytestmark = pytest.mark.asyncio

# ROB-918 owns the 918xxx symbol range to stay isolated from other suites'
# synthetic symbols in the shared persistent test_db.
_SYM_QUALIFIES = "918001"
_SYM_LOW_MARKET_CAP = "918002"
_SYM_LOW_TRADE_VALUE = "918003"
_SNAPSHOT_DATE = dt.date(2099, 12, 31)


def _candle_row(*, symbol: str, close: float, time_utc: dt.datetime) -> DailyCandleRow:
    return DailyCandleRow(
        time_utc=time_utc,
        symbol=symbol,
        partition="KRX",
        open=close,
        high=close,
        low=close,
        close=close,
        adj_close=None,
        volume=1000.0,
        value=1000.0 * close,
        source="kis",
    )


@pytest_asyncio.fixture(autouse=True)
async def _cleanup(db_session):
    async def _purge() -> None:
        await db_session.execute(
            sa.delete(InvestScreenerSnapshot).where(
                InvestScreenerSnapshot.symbol.in_(
                    [_SYM_QUALIFIES, _SYM_LOW_MARKET_CAP, _SYM_LOW_TRADE_VALUE]
                )
            )
        )
        await db_session.execute(
            sa.delete(MarketValuationSnapshot).where(
                MarketValuationSnapshot.symbol.in_(
                    [_SYM_QUALIFIES, _SYM_LOW_MARKET_CAP, _SYM_LOW_TRADE_VALUE]
                )
            )
        )
        await db_session.execute(
            sa.delete(KRSymbolUniverse).where(
                KRSymbolUniverse.symbol.in_(
                    [_SYM_QUALIFIES, _SYM_LOW_MARKET_CAP, _SYM_LOW_TRADE_VALUE]
                )
            )
        )
        await db_session.execute(
            sa.delete(InvestThemeEventSnapshotStock).where(
                InvestThemeEventSnapshotStock.symbol == _SYM_QUALIFIES
            )
        )
        await db_session.execute(
            sa.delete(InvestThemeEventSnapshot).where(
                InvestThemeEventSnapshot.source_event_key == "rob918-test-theme"
            )
        )
        await db_session.commit()

    await _purge()
    yield
    await _purge()


# ---------------------------------------------------------------------------
# Top-level gating
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_returns_none_for_non_kr_market():
    result = await svc.build_new_candidate_section(market_scope="us", stage="preopen")
    assert result is None


@pytest.mark.unit
async def test_returns_none_for_non_preopen_stage():
    result = await svc.build_new_candidate_section(market_scope="kr", stage="intraday")
    assert result is None


# ---------------------------------------------------------------------------
# Crash guard
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_crash_warning_when_index_gap_below_threshold(monkeypatch):
    async def _fake_fetch(_session):
        return [
            _candle_row(
                symbol="069500",
                close=96.0,
                time_utc=dt.datetime(2026, 7, 16, tzinfo=dt.UTC),
            ),
            _candle_row(
                symbol="069500",
                close=100.0,
                time_utc=dt.datetime(2026, 7, 15, tzinfo=dt.UTC),
            ),
        ]

    monkeypatch.setattr(svc, "_fetch_index_recent_closes", _fake_fetch)

    state, detail = await svc._resolve_market_state(object())

    assert state == "crash_warning"
    assert detail["gap_pct"] == pytest.approx(-4.0)


@pytest.mark.unit
async def test_normal_market_state_when_index_gap_small(monkeypatch):
    async def _fake_fetch(_session):
        return [
            _candle_row(
                symbol="069500",
                close=100.5,
                time_utc=dt.datetime(2026, 7, 16, tzinfo=dt.UTC),
            ),
            _candle_row(
                symbol="069500",
                close=100.0,
                time_utc=dt.datetime(2026, 7, 15, tzinfo=dt.UTC),
            ),
        ]

    monkeypatch.setattr(svc, "_fetch_index_recent_closes", _fake_fetch)

    state, detail = await svc._resolve_market_state(object())

    assert state == "normal"
    assert detail["gap_pct"] == pytest.approx(0.5)


@pytest.mark.unit
async def test_unknown_market_state_when_index_candles_missing(monkeypatch):
    async def _fake_fetch(_session):
        raise RuntimeError("relation kr_candles_1d does not exist")

    monkeypatch.setattr(svc, "_fetch_index_recent_closes", _fake_fetch)

    state, detail = await svc._resolve_market_state(object())

    assert state == "unknown"
    assert detail["reason"]


@pytest.mark.unit
async def test_unknown_market_state_when_only_one_candle(monkeypatch):
    async def _fake_fetch(_session):
        return [
            _candle_row(
                symbol="069500",
                close=100.0,
                time_utc=dt.datetime(2026, 7, 16, tzinfo=dt.UTC),
            )
        ]

    monkeypatch.setattr(svc, "_fetch_index_recent_closes", _fake_fetch)

    state, detail = await svc._resolve_market_state(object())

    assert state == "unknown"


# ---------------------------------------------------------------------------
# consecutive_gainers
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_consecutive_gainers_filters_by_market_cap_and_trade_value(db_session):
    db_session.add_all(
        [
            KRSymbolUniverse(
                symbol=_SYM_QUALIFIES, name="합격종목", exchange="KOSPI", is_active=True
            ),
            KRSymbolUniverse(
                symbol=_SYM_LOW_MARKET_CAP,
                name="시총미달",
                exchange="KOSPI",
                is_active=True,
            ),
            KRSymbolUniverse(
                symbol=_SYM_LOW_TRADE_VALUE,
                name="거래대금미달",
                exchange="KOSPI",
                is_active=True,
            ),
        ]
    )
    db_session.add_all(
        [
            InvestScreenerSnapshot(
                market="kr",
                symbol=_SYM_QUALIFIES,
                snapshot_date=_SNAPSHOT_DATE,
                latest_close=decimal.Decimal("50000"),
                prev_close=decimal.Decimal("46000"),
                change_amount=decimal.Decimal("4000"),
                change_rate=decimal.Decimal("8.7"),
                consecutive_up_days=1,
                week_change_rate=decimal.Decimal("12.0"),
                daily_volume=1_000_000,  # 1M * 50000 = 50B >= 20B floor
                closes_window=[46000, 50000],
                source="kis",
            ),
            InvestScreenerSnapshot(
                market="kr",
                symbol=_SYM_LOW_MARKET_CAP,
                snapshot_date=_SNAPSHOT_DATE,
                latest_close=decimal.Decimal("50000"),
                prev_close=decimal.Decimal("46000"),
                change_rate=decimal.Decimal("8.7"),
                consecutive_up_days=1,
                daily_volume=1_000_000,
                closes_window=[46000, 50000],
                source="kis",
            ),
            InvestScreenerSnapshot(
                market="kr",
                symbol=_SYM_LOW_TRADE_VALUE,
                snapshot_date=_SNAPSHOT_DATE,
                latest_close=decimal.Decimal("50000"),
                prev_close=decimal.Decimal("46000"),
                change_rate=decimal.Decimal("8.7"),
                consecutive_up_days=1,
                daily_volume=100,  # 100 * 50000 = 5M << 20B floor
                closes_window=[46000, 50000],
                source="kis",
            ),
        ]
    )
    db_session.add_all(
        [
            MarketValuationSnapshot(
                market="kr",
                symbol=_SYM_QUALIFIES,
                snapshot_date=_SNAPSHOT_DATE,
                source="naver_finance",
                market_cap=decimal.Decimal("300000000000"),  # 3000억 >= floor
            ),
            MarketValuationSnapshot(
                market="kr",
                symbol=_SYM_LOW_MARKET_CAP,
                snapshot_date=_SNAPSHOT_DATE,
                source="naver_finance",
                market_cap=decimal.Decimal("50000000000"),  # 500억 < floor
            ),
            MarketValuationSnapshot(
                market="kr",
                symbol=_SYM_LOW_TRADE_VALUE,
                snapshot_date=_SNAPSHOT_DATE,
                source="naver_finance",
                market_cap=decimal.Decimal("300000000000"),
            ),
        ]
    )
    await db_session.commit()

    omitted: list[dict[str, str]] = []
    candidates = await svc._build_consecutive_gainers_candidates(
        db_session, top_n=10, omitted=omitted
    )

    symbols = [c["symbol"] for c in candidates]
    assert _SYM_QUALIFIES in symbols
    assert _SYM_LOW_MARKET_CAP not in symbols
    assert _SYM_LOW_TRADE_VALUE not in symbols

    winner = next(c for c in candidates if c["symbol"] == _SYM_QUALIFIES)
    assert winner["reason"] == "consecutive_gainers"
    assert winner["advisory_only"] is True
    assert winner["metrics"]["change_rate"] == pytest.approx(8.7)
    assert winner["metrics"]["consecutive_up_days"] == 1
    assert winner["metrics"]["market_cap"] == pytest.approx(300000000000)
    assert winner["metrics"]["trade_value_est"] == pytest.approx(50_000_000_000)
    assert winner["baseline_date"] == _SNAPSHOT_DATE.isoformat()
    assert winner["baseline_close"] == pytest.approx(50000.0)
    assert winner["outcome"] == {"d1_close_pct": None}


@pytest.mark.unit
async def test_consecutive_gainers_omitted_when_snapshot_missing(
    db_session, monkeypatch
):
    # Force resolve_healthy_partition to report "no partitions" regardless of
    # any real data another xdist worker may have seeded for market='kr'.
    async def _no_partition(*_a, **_k):
        return None

    monkeypatch.setattr(svc, "resolve_healthy_partition", _no_partition)

    omitted: list[dict[str, str]] = []
    candidates = await svc._build_consecutive_gainers_candidates(
        db_session, top_n=10, omitted=omitted
    )

    assert candidates == []
    assert any(o["section"] == "consecutive_gainers" for o in omitted)


# ---------------------------------------------------------------------------
# theme_leaders
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_theme_leaders_flattened_from_leader_symbols(db_session):
    theme = InvestThemeEventSnapshot(
        snapshot_at=dt.datetime(2099, 12, 31, 15, 30, tzinfo=dt.UTC),
        trading_date=_SNAPSHOT_DATE,
        surface="theme_upjong_list",
        market="kr",
        event_kind="theme",
        source_event_key="rob918-test-theme",
        name="ROB918테스트테마",
        sort_type="change_rate",
        rank=1,
        change_rate=decimal.Decimal("15.0"),
        trade_value=decimal.Decimal("999000000"),
        market_cap=decimal.Decimal("5000000000"),
        stock_count=3,
        leader_symbols=[
            {"symbol": _SYM_QUALIFIES, "name": "합격종목"},
        ],
    )
    db_session.add(theme)
    await db_session.flush()
    db_session.add(
        InvestThemeEventSnapshotStock(
            theme_snapshot_id=theme.id,
            symbol=_SYM_QUALIFIES,
            name="합격종목",
            rank=1,
            order_type="rise",
            price=decimal.Decimal("50000"),
            change_rate=decimal.Decimal("8.7"),
        )
    )
    await db_session.commit()

    omitted: list[dict[str, str]] = []
    candidates = await svc._build_theme_leader_candidates(
        db_session, top_n=10, omitted=omitted
    )

    matches = [c for c in candidates if c["symbol"] == _SYM_QUALIFIES]
    assert matches, f"expected {_SYM_QUALIFIES} among theme leaders, got {candidates}"
    winner = matches[0]
    assert winner["reason"] == "theme_leader"
    assert winner["advisory_only"] is True
    assert winner["metrics"]["theme_name"] == "ROB918테스트테마"
    assert winner["baseline_close"] == pytest.approx(50000.0)
    assert winner["outcome"] == {"d1_close_pct": None}


@pytest.mark.unit
async def test_theme_leaders_omitted_when_no_snapshots(monkeypatch):
    class _EmptyRepo:
        def __init__(self, _session):
            pass

        async def list_theme_events(self, **_kwargs):
            return []

    monkeypatch.setattr(svc, "InvestMomentumEventSnapshotsRepository", _EmptyRepo)

    omitted: list[dict[str, str]] = []
    candidates = await svc._build_theme_leader_candidates(
        object(), top_n=10, omitted=omitted
    )

    assert candidates == []
    assert any(o["section"] == "theme_leaders" for o in omitted)


# ---------------------------------------------------------------------------
# double_buy
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_double_buy_maps_loader_rows(monkeypatch):
    class _FakeResult:
        rows = [
            {
                "symbol": "918777",
                "name": "쌍끌이종목",
                "change_rate": 3.2,
                "foreign_net": 1000,
                "institution_net": 2000,
                "market_cap": 400000000000.0,
                "close": 12345.0,
                "snapshot_date": _SNAPSHOT_DATE,
            }
        ]

    async def _fake_loader(_session, *, market, limit):
        assert market == "kr"
        return _FakeResult()

    monkeypatch.setattr(svc, "load_double_buy_from_snapshots", _fake_loader)

    omitted: list[dict[str, str]] = []
    candidates = await svc._build_double_buy_candidates(
        object(), top_n=10, omitted=omitted
    )

    assert len(candidates) == 1
    winner = candidates[0]
    assert winner["symbol"] == "918777"
    assert winner["reason"] == "double_buy"
    assert winner["advisory_only"] is True
    assert winner["metrics"]["foreign_net"] == 1000
    assert winner["baseline_date"] == _SNAPSHOT_DATE.isoformat()
    assert winner["baseline_close"] == pytest.approx(12345.0)
    assert winner["outcome"] == {"d1_close_pct": None}


@pytest.mark.unit
async def test_double_buy_omitted_when_loader_returns_none(monkeypatch):
    async def _fake_loader(_session, *, market, limit):
        return None

    monkeypatch.setattr(svc, "load_double_buy_from_snapshots", _fake_loader)

    omitted: list[dict[str, str]] = []
    candidates = await svc._build_double_buy_candidates(
        object(), top_n=10, omitted=omitted
    )

    assert candidates == []
    assert any(o["section"] == "double_buy" for o in omitted)


# ---------------------------------------------------------------------------
# End-to-end (own session, graceful on a totally empty DB)
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_build_new_candidate_section_graceful_with_no_seeded_data():
    result = await svc.build_new_candidate_section(market_scope="kr", stage="preopen")

    assert result is not None
    assert result["advisory_only"] is True
    assert result["market_state"] in {"normal", "crash_warning", "unknown"}
    assert isinstance(result["consecutive_gainers"], list)
    assert isinstance(result["theme_leaders"], list)
    assert isinstance(result["double_buy"], list)
    assert isinstance(result["omitted_sections"], list)

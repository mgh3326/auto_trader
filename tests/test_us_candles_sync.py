from __future__ import annotations

import ast
import inspect
import re
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from types import SimpleNamespace
from typing import Protocol, cast, final, override
from unittest.mock import AsyncMock, MagicMock

import pandas as pd
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.manual_holdings import MarketType
from app.services.us_symbol_universe_service import (
    USSymbolInactiveError,
    USSymbolNotRegisteredError,
    USSymbolUniverseEmptyError,
)

VERSIONS_DIR = Path("alembic/versions")
SQL_HELPER_PATH = Path("scripts/sql/us_candles_timescale.sql")
BASE_TABLE = "us_candles_1m"
AGGREGATE_VIEWS = [
    "us_candles_5m",
    "us_candles_15m",
    "us_candles_30m",
    "us_candles_1h",
]
ALL_OBJECTS = [BASE_TABLE, *AGGREGATE_VIEWS]
US_UNIVERSE_SYNC_HINT = (
    "Sync required: uv run python scripts/sync_us_symbol_universe.py"
)


def _read_migration(pattern: str) -> tuple[Path, str]:
    matches = sorted(VERSIONS_DIR.glob(pattern))
    assert matches, f"migration file matching {pattern} is missing"
    path = matches[-1]
    return path, path.read_text(encoding="utf-8")


def _extract_revision(content: str) -> str:
    match = re.search(r'revision: str = "([^"]+)"', content)
    assert match, "revision id is missing"
    return match.group(1)


def _extract_policy_targets(content: str, policy_name: str) -> set[str]:
    pattern = rf"{policy_name}\(\s*'public\.(us_candles_[^']+)'"
    return set(re.findall(pattern, content, flags=re.DOTALL))


def _collect_revision_heads() -> set[str]:
    revisions: set[str] = set()
    referenced: set[str] = set()

    for path in VERSIONS_DIR.glob("*.py"):
        content = path.read_text(encoding="utf-8")
        module = ast.parse(content, filename=path.name)
        assignments: dict[str, object] = {}

        for node in module.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id in {
                        "revision",
                        "down_revision",
                    }:
                        assert node.value is not None
                        assignments[target.id] = ast.literal_eval(node.value)
            elif isinstance(node, ast.AnnAssign):
                if isinstance(node.target, ast.Name) and node.target.id in {
                    "revision",
                    "down_revision",
                }:
                    assert node.value is not None
                    assignments[node.target.id] = ast.literal_eval(node.value)

        assert "revision" in assignments, f"revision id is missing in {path.name}"
        revision = assignments["revision"]
        assert isinstance(revision, str), f"revision must be a string in {path.name}"
        revisions.add(revision)

        assert "down_revision" in assignments, (
            f"down_revision is missing in {path.name}"
        )
        down_revision = assignments["down_revision"]
        if isinstance(down_revision, str):
            referenced.add(down_revision)
        elif down_revision is not None:
            assert isinstance(down_revision, tuple | list), (
                f"down_revision must be str, tuple, list, or None in {path.name}"
            )
            parent_revisions = cast("tuple[object, ...] | list[object]", down_revision)
            for item in parent_revisions:
                assert isinstance(item, str), (
                    f"down_revision entries must be strings in {path.name}"
                )
                referenced.add(item)

    return revisions - referenced


def test_us_candles_migration_chain_continues_existing_candle_branch() -> None:
    _, timescale_content = _read_migration("*_add_us_candles_timescale.py")
    _, retention_content = _read_migration("*_add_us_candles_retention_policy.py")

    assert (
        'down_revision: str | Sequence[str] | None = "d31f0a2b4c6d"'
        in timescale_content
    )
    assert (
        f'down_revision: str | Sequence[str] | None = "{_extract_revision(timescale_content)}"'
        in retention_content
    )


def test_revision_graph_has_single_final_head() -> None:
    heads = _collect_revision_heads()

    # Should have exactly one head (no branch divergence)
    assert len(heads) == 1, f"Expected single head, got: {heads}"


def test_merge_kr_intraday_and_us_candle_heads_migration_structure() -> None:
    _, merge_content = _read_migration("*_merge_kr_intraday_and_us_candle_heads.py")

    # Verify merge migration has correct down_revision pointing to both branches
    assert (
        'down_revision: str | Sequence[str] | None = ("5c6d7e8f9012", "a9d6e4c2b1f0")'
        in merge_content
    )


def test_merge_trade_review_and_rss_news_heads_migration_structure() -> None:
    _, merge_content = _read_migration("*_merge_trade_review_and_rss_news_heads.py")

    assert (
        'down_revision: str | Sequence[str] | None = ("672f39265fed", "f3a4b5c6d7e8")'
        in merge_content
    )


def test_us_candles_timescale_migration_defines_required_base_table_contract() -> None:
    _, content = _read_migration("*_add_us_candles_timescale.py")

    required_fragments = [
        "CREATE TABLE public.us_candles_1m",
        "time TIMESTAMPTZ NOT NULL",
        "symbol TEXT NOT NULL",
        "exchange TEXT NOT NULL",
        "open NUMERIC NOT NULL",
        "high NUMERIC NOT NULL",
        "low NUMERIC NOT NULL",
        "close NUMERIC NOT NULL",
        "volume NUMERIC NOT NULL",
        "value NUMERIC NOT NULL",
        "exchange IN ('NASD', 'NYSE', 'AMEX')",
        "UNIQUE (time, symbol, exchange)",
        "create_hypertable(",
        "'public.us_candles_1m'",
        "(symbol, exchange, time DESC)",
    ]

    for fragment in required_fragments:
        assert fragment in content


def test_us_candle_objects_appear_in_timescale_migration_and_operator_sql() -> None:
    _, migration_content = _read_migration("*_add_us_candles_timescale.py")
    sql_content = SQL_HELPER_PATH.read_text(encoding="utf-8")

    for object_name in ALL_OBJECTS:
        qualified_name = f"public.{object_name}"
        assert qualified_name in migration_content
        assert qualified_name in sql_content


def test_continuous_aggregate_policies_exist_for_all_us_rollups() -> None:
    _, migration_content = _read_migration("*_add_us_candles_timescale.py")
    sql_content = SQL_HELPER_PATH.read_text(encoding="utf-8")

    expected = set(AGGREGATE_VIEWS)
    assert (
        _extract_policy_targets(migration_content, "add_continuous_aggregate_policy")
        == expected
    )
    assert (
        _extract_policy_targets(sql_content, "add_continuous_aggregate_policy")
        == expected
    )


def test_retention_policies_exist_for_us_base_table_and_all_rollups() -> None:
    _, migration_content = _read_migration("*_add_us_candles_retention_policy.py")
    sql_content = SQL_HELPER_PATH.read_text(encoding="utf-8")

    expected = set(ALL_OBJECTS)
    assert (
        _extract_policy_targets(migration_content, "add_retention_policy") == expected
    )
    assert _extract_policy_targets(sql_content, "add_retention_policy") == expected
    assert "90 days" in migration_content
    assert "90 days" in sql_content


def test_timescaledb_version_guard_requires_2_15_or_newer() -> None:
    _, migration_content = _read_migration("*_add_us_candles_timescale.py")
    sql_content = SQL_HELPER_PATH.read_text(encoding="utf-8")

    for content in (migration_content, sql_content):
        assert "timescaledb extension is not installed" in content
        assert "(2, 15, 0)" in content
        assert "2.15.0" in content


def test_us_hourly_cagg_uses_session_aligned_new_york_buckets() -> None:
    _, migration_content = _read_migration("*_add_us_candles_timescale.py")
    sql_content = SQL_HELPER_PATH.read_text(encoding="utf-8")

    required_fragments = [
        "CREATE MATERIALIZED VIEW public.us_candles_1h",
        "timezone => 'America/New_York'",
        "\"offset\" => INTERVAL '30 minutes'",
        "AT TIME ZONE 'America/New_York'",
        "TIME '09:30'",
        "TIME '16:00'",
    ]

    forbidden_fragments = [
        "time_bucket(INTERVAL '1 hour', time, 'Asia/Seoul')",
        "time_bucket(INTERVAL '1 hour', time, 'UTC')",
    ]

    for content in (migration_content, sql_content):
        for fragment in required_fragments:
            assert fragment in content
        for fragment in forbidden_fragments:
            assert fragment not in content


def test_operator_sql_contains_refresh_calls_for_all_aggregate_views() -> None:
    content = SQL_HELPER_PATH.read_text(encoding="utf-8")

    pattern = r"refresh_continuous_aggregate\(\s*'public\.(us_candles_[^']+)'"
    assert set(re.findall(pattern, content, flags=re.DOTALL)) == set(AGGREGATE_VIEWS)


@final
class _FrozenDateTime(datetime):
    _now: datetime = datetime(2026, 3, 9, 15, 45, tzinfo=UTC)

    @classmethod
    @override
    def now(cls, tz: tzinfo | None = None):
        current = cls._now
        if tz is None:
            return current.replace(tzinfo=None)
        return current.astimezone(tz)


@final
class _MappingsResult:
    def __init__(self, rows: list[dict[str, object]] | None = None):
        self._rows = list(rows or [])

    def all(self):
        return list(self._rows)


@final
class _ScalarResult:
    def __init__(
        self,
        value: object,
        *,
        rowcount: int = 0,
        mapping_rows: list[dict[str, object]] | None = None,
    ):
        self._value: object = value
        self.rowcount = rowcount
        self._mapping_rows = list(mapping_rows or [])

    def scalar_one_or_none(self):
        return self._value

    def mappings(self):
        return _MappingsResult(self._mapping_rows)


@final
class _RecordingSession:
    def __init__(
        self,
        cursor_values: list[object] | None = None,
        *,
        existing_rows: list[dict[str, object]] | None = None,
        upsert_rowcount: int = 0,
    ):
        self.cursor_values: list[object] = list(cursor_values or [])
        self.existing_rows: list[dict[str, object]] = list(existing_rows or [])
        self.executed: list[tuple[object, object]] = []
        self.commits: int = 0
        self.rollbacks: int = 0
        self.closed: bool = False
        self.upsert_rowcount = upsert_rowcount

    async def execute(self, statement: object, params: object = None):
        import app.services.us_candles_sync_service as svc

        self.executed.append((statement, params))
        if statement is svc._CURSOR_SQL:
            value = self.cursor_values.pop(0) if self.cursor_values else None
            return _ScalarResult(value)
        if statement is svc._EXISTING_ROWS_SQL:
            return _ScalarResult(None, mapping_rows=self.existing_rows)
        if statement is svc._UPSERT_SQL:
            return _ScalarResult(None, rowcount=self.upsert_rowcount)
        raise AssertionError(f"Unexpected statement: {statement!r}")

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1

    async def close(self):
        self.closed = True


class _MinutePageProtocol(Protocol):
    frame: pd.DataFrame
    has_more: bool
    next_keyb: str | None


@final
class _MinutePage:
    def __init__(
        self,
        *,
        frame: pd.DataFrame,
        has_more: bool,
        next_keyb: str | None,
    ) -> None:
        self.frame = frame
        self.has_more = has_more
        self.next_keyb = next_keyb


class _FakeCalendar:
    def __init__(
        self,
        *,
        trading_minute: bool = True,
        current_session: str = "2026-03-09",
        session_open_utc: datetime | None = None,
        last_closed: str = "2026-03-07",
        window: list[str] | None = None,
        session_bounds: dict[str, tuple[datetime, datetime]] | None = None,
    ):
        self.trading_minute: bool = trading_minute
        self.current_session: pd.Timestamp = pd.Timestamp(current_session)
        self.session_open_utc: pd.Timestamp = pd.Timestamp(
            session_open_utc or datetime(2026, 3, 9, 13, 30, tzinfo=UTC)
        )
        self.last_closed: pd.Timestamp = pd.Timestamp(last_closed)
        self.window: pd.DatetimeIndex = pd.DatetimeIndex(
            [
                pd.Timestamp(item)
                for item in (window or ["2026-03-05", "2026-03-06", "2026-03-07"])
            ]
        )
        self.session_bounds: dict[str, tuple[pd.Timestamp, pd.Timestamp]] = {
            key: (pd.Timestamp(start), pd.Timestamp(end))
            for key, (start, end) in (session_bounds or {}).items()
        }

    def is_trading_minute(self, _minute: object):
        return self.trading_minute

    def minute_to_session(self, _minute: object, direction: str = "none"):
        _ = direction
        return self.current_session

    def session_open(self, session: str | pd.Timestamp | datetime):
        session_key = pd.Timestamp(session).strftime("%Y-%m-%d")
        if session_key in self.session_bounds:
            return self.session_bounds[session_key][0]
        return self.session_open_utc

    def session_close(self, session: str | pd.Timestamp | datetime):
        session_key = pd.Timestamp(session).strftime("%Y-%m-%d")
        if session_key in self.session_bounds:
            return self.session_bounds[session_key][1]
        return self.session_open_utc + pd.Timedelta(hours=6, minutes=30)

    def minute_to_past_session(self, _minute: object, count: int = 1):
        _ = count
        return self.last_closed

    def sessions_window(self, _session: object, count: int):
        _ = count
        return self.window


def _make_page(
    rows: list[dict[str, object]], *, has_more: bool, next_keyb: str | None = None
) -> _MinutePageProtocol:
    return _MinutePage(
        frame=pd.DataFrame(rows),
        has_more=has_more,
        next_keyb=next_keyb,
    )


@final
class _KISStub:
    def __init__(self, inquire: AsyncMock) -> None:
        self._inquire = inquire

    async def inquire_overseas_minute_chart(
        self,
        symbol: str,
        exchange_code: str = "NASD",
        n: int = 120,
        keyb: str = "",
    ) -> _MinutePageProtocol:
        return cast(
            _MinutePageProtocol,
            await self._inquire(
                symbol,
                exchange_code=exchange_code,
                n=n,
                keyb=keyb,
            ),
        )


def test_build_symbol_union_combines_kis_and_manual_us_symbols() -> None:
    import app.services.us_candles_sync_service as svc
    from app.services.candles_sync_common import build_symbol_union

    kis_holdings = [
        {"ovrs_pdno": "aapl"},
        {"ovrs_pdno": "BRK/B"},
        {"ovrs_pdno": None},
    ]
    manual_holdings = [
        SimpleNamespace(ticker="BRK.B", market_type=MarketType.US),
        SimpleNamespace(ticker="msft", market_type=MarketType.US),
        SimpleNamespace(ticker="nvda", market_type=MarketType.US),
    ]

    assert build_symbol_union(
        kis_holdings,
        manual_holdings,
        holdings_field="ovrs_pdno",
        normalize_fn=svc._normalize_symbol,
    ) == {
        "AAPL",
        "BRK.B",
        "MSFT",
        "NVDA",
    }


@pytest.mark.asyncio
async def test_sync_us_candles_no_target_symbols_returns_kr_style_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.services.us_candles_sync_service as svc

    fake_session = _RecordingSession()
    fetch_my_us_stocks = AsyncMock(return_value=[])
    get_holdings_by_user = AsyncMock(return_value=[])

    monkeypatch.setattr(svc, "AsyncSessionLocal", lambda: fake_session)
    monkeypatch.setattr(
        svc, "KISClient", lambda: SimpleNamespace(fetch_my_us_stocks=fetch_my_us_stocks)
    )
    monkeypatch.setattr(
        svc,
        "ManualHoldingsService",
        lambda session: SimpleNamespace(get_holdings_by_user=get_holdings_by_user),
    )

    result = await svc.sync_us_candles(mode="incremental", sessions=7, user_id=11)

    assert result == {
        "mode": "incremental",
        "sessions": 7,
        "skipped": True,
        "reason": "no_target_symbols",
        "skip_reasons": {},
        "skipped_symbols": [],
        "lookup_refresh_attempted": False,
        "symbols_total": 0,
        "symbol_venues_total": 0,
        "pairs_processed": 0,
        "pairs_skipped": 0,
        "rows_upserted": 0,
        "pages_fetched": 0,
    }
    get_holdings_by_user.assert_awaited_once_with(user_id=11, market_type=MarketType.US)
    fetch_my_us_stocks.assert_awaited_once()
    assert fake_session.closed is True


@pytest.mark.asyncio
async def test_sync_us_candles_refreshes_symbol_universe_and_skips_unresolved_symbol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.services.us_candles_sync_service as svc

    fake_session = _RecordingSession()
    kis_client = SimpleNamespace(
        fetch_my_us_stocks=AsyncMock(return_value=[{"ovrs_pdno": "AAPL"}])
    )
    manual_service = SimpleNamespace(get_holdings_by_user=AsyncMock(return_value=[]))

    monkeypatch.setattr(svc, "AsyncSessionLocal", lambda: fake_session)
    monkeypatch.setattr(svc, "KISClient", lambda: kis_client)
    monkeypatch.setattr(svc, "ManualHoldingsService", lambda session: manual_service)
    monkeypatch.setattr(
        svc,
        "_select_closed_sessions",
        lambda now_utc, sessions: [
            SimpleNamespace(
                open_utc=datetime(2026, 3, 7, 14, 30, tzinfo=UTC),
                last_minute_utc=datetime(2026, 3, 7, 20, 59, tzinfo=UTC),
            )
        ],
    )
    monkeypatch.setattr(
        svc,
        "get_us_exchange_by_symbol",
        AsyncMock(
            side_effect=[
                USSymbolNotRegisteredError(
                    f"US symbol 'AAPL' is not registered in us_symbol_universe. {US_UNIVERSE_SYNC_HINT}"
                ),
                USSymbolNotRegisteredError(
                    f"US symbol 'AAPL' is not registered in us_symbol_universe. {US_UNIVERSE_SYNC_HINT}"
                ),
            ]
        ),
    )
    sync_universe = AsyncMock(
        return_value={"total": 1, "inserted": 0, "updated": 1, "deactivated": 0}
    )
    monkeypatch.setattr(svc, "sync_us_symbol_universe", sync_universe, raising=False)

    result = await svc.sync_us_candles(mode="backfill")

    assert result == {
        "mode": "backfill",
        "sessions": 10,
        "skipped": True,
        "skip_reasons": {"unresolved_symbol_after_refresh": 1},
        "skipped_symbols": ["AAPL"],
        "lookup_refresh_attempted": True,
        "symbols_total": 1,
        "symbol_venues_total": 1,
        "pairs_processed": 0,
        "pairs_skipped": 1,
        "rows_upserted": 0,
        "pages_fetched": 0,
    }
    sync_universe.assert_awaited_once_with(db=fake_session)
    assert fake_session.commits == 1


@pytest.mark.asyncio
async def test_sync_us_candles_refreshes_symbol_universe_and_retries_current_symbol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.services.us_candles_sync_service as svc

    fake_session = _RecordingSession()
    fetch_my_us_stocks = AsyncMock(return_value=[{"ovrs_pdno": "BRK/B"}])
    get_holdings_by_user = AsyncMock(return_value=[])
    get_us_exchange = AsyncMock(
        side_effect=[
            USSymbolNotRegisteredError(
                f"US symbol 'BRK.B' is not registered in us_symbol_universe. {US_UNIVERSE_SYNC_HINT}"
            ),
            "NYSE",
        ]
    )
    sync_universe = AsyncMock(
        return_value={"total": 2, "inserted": 1, "updated": 0, "deactivated": 0}
    )
    collect_window_rows = AsyncMock(return_value=([], 1))
    upsert_rows = AsyncMock(return_value=0)

    monkeypatch.setattr(svc, "AsyncSessionLocal", lambda: fake_session)
    monkeypatch.setattr(
        svc, "KISClient", lambda: SimpleNamespace(fetch_my_us_stocks=fetch_my_us_stocks)
    )
    monkeypatch.setattr(
        svc,
        "ManualHoldingsService",
        lambda session: SimpleNamespace(get_holdings_by_user=get_holdings_by_user),
    )
    monkeypatch.setattr(svc, "get_us_exchange_by_symbol", get_us_exchange)
    monkeypatch.setattr(svc, "sync_us_symbol_universe", sync_universe, raising=False)
    monkeypatch.setattr(
        svc,
        "_select_closed_sessions",
        lambda now_utc, sessions: [
            SimpleNamespace(
                open_utc=datetime(2026, 3, 7, 14, 30, tzinfo=UTC),
                last_minute_utc=datetime(2026, 3, 7, 20, 59, tzinfo=UTC),
            )
        ],
    )
    monkeypatch.setattr(svc, "_collect_window_rows", collect_window_rows)
    monkeypatch.setattr(svc, "_upsert_rows", upsert_rows)

    result = await svc.sync_us_candles(mode="backfill", sessions=3)

    assert result == {
        "mode": "backfill",
        "sessions": 3,
        "skipped": False,
        "skip_reasons": {},
        "skipped_symbols": [],
        "lookup_refresh_attempted": True,
        "symbols_total": 1,
        "symbol_venues_total": 1,
        "pairs_processed": 1,
        "pairs_skipped": 0,
        "rows_upserted": 0,
        "pages_fetched": 1,
    }
    sync_universe.assert_awaited_once_with(db=fake_session)
    assert get_us_exchange.await_count == 2
    collect_window_rows.assert_awaited_once()
    upsert_rows.assert_awaited_once()


@pytest.mark.asyncio
async def test_sync_us_candles_skips_only_unresolved_symbols_after_single_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.services.us_candles_sync_service as svc

    fake_session = _RecordingSession()
    fetch_my_us_stocks = AsyncMock(
        return_value=[
            {"ovrs_pdno": "BRK/B"},
            {"ovrs_pdno": "MSFT"},
            {"ovrs_pdno": "ZZZZ"},
        ]
    )
    get_holdings_by_user = AsyncMock(return_value=[])
    sync_universe = AsyncMock(
        return_value={"total": 3, "inserted": 1, "updated": 1, "deactivated": 0}
    )
    collect_window_rows = AsyncMock(return_value=([], 1))
    upsert_rows = AsyncMock(return_value=0)

    attempts = {"BRK.B": 0, "MSFT": 0, "ZZZZ": 0}

    async def _resolve_exchange(symbol: str, db: AsyncSession | None = None) -> str:
        _ = db
        attempts[symbol] += 1
        if symbol == "BRK.B":
            raise USSymbolNotRegisteredError(
                f"US symbol 'BRK.B' is not registered in us_symbol_universe. {US_UNIVERSE_SYNC_HINT}"
            )
        if symbol == "MSFT":
            return "NASD"
        raise USSymbolInactiveError(
            f"US symbol 'ZZZZ' is inactive in us_symbol_universe. {US_UNIVERSE_SYNC_HINT}"
        )

    monkeypatch.setattr(svc, "AsyncSessionLocal", lambda: fake_session)
    monkeypatch.setattr(
        svc, "KISClient", lambda: SimpleNamespace(fetch_my_us_stocks=fetch_my_us_stocks)
    )
    monkeypatch.setattr(
        svc,
        "ManualHoldingsService",
        lambda session: SimpleNamespace(get_holdings_by_user=get_holdings_by_user),
    )
    monkeypatch.setattr(svc, "get_us_exchange_by_symbol", _resolve_exchange)
    monkeypatch.setattr(svc, "sync_us_symbol_universe", sync_universe, raising=False)
    monkeypatch.setattr(
        svc,
        "_select_closed_sessions",
        lambda now_utc, sessions: [
            SimpleNamespace(
                open_utc=datetime(2026, 3, 7, 14, 30, tzinfo=UTC),
                last_minute_utc=datetime(2026, 3, 7, 20, 59, tzinfo=UTC),
            )
        ],
    )
    monkeypatch.setattr(svc, "_collect_window_rows", collect_window_rows)
    monkeypatch.setattr(svc, "_upsert_rows", upsert_rows)

    result = await svc.sync_us_candles(mode="backfill")

    assert result == {
        "mode": "backfill",
        "sessions": 10,
        "skipped": False,
        "skip_reasons": {"unresolved_symbol_after_refresh": 2},
        "skipped_symbols": ["BRK.B", "ZZZZ"],
        "lookup_refresh_attempted": True,
        "symbols_total": 3,
        "symbol_venues_total": 3,
        "pairs_processed": 1,
        "pairs_skipped": 2,
        "rows_upserted": 0,
        "pages_fetched": 1,
    }
    sync_universe.assert_awaited_once_with(db=fake_session)
    assert attempts == {"BRK.B": 2, "MSFT": 1, "ZZZZ": 1}
    collect_window_rows.assert_awaited_once()


@pytest.mark.asyncio
async def test_sync_us_candles_propagates_refresh_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.services.us_candles_sync_service as svc

    fake_session = _RecordingSession()
    kis_client = SimpleNamespace(
        fetch_my_us_stocks=AsyncMock(return_value=[{"ovrs_pdno": "BRK/B"}])
    )
    manual_service = SimpleNamespace(get_holdings_by_user=AsyncMock(return_value=[]))

    monkeypatch.setattr(svc, "AsyncSessionLocal", lambda: fake_session)
    monkeypatch.setattr(svc, "KISClient", lambda: kis_client)
    monkeypatch.setattr(svc, "ManualHoldingsService", lambda session: manual_service)
    monkeypatch.setattr(
        svc,
        "get_us_exchange_by_symbol",
        AsyncMock(
            side_effect=USSymbolNotRegisteredError(
                f"US symbol 'BRK.B' is not registered in us_symbol_universe. {US_UNIVERSE_SYNC_HINT}"
            )
        ),
    )
    monkeypatch.setattr(
        svc,
        "sync_us_symbol_universe",
        AsyncMock(side_effect=RuntimeError("refresh failed")),
        raising=False,
    )

    with pytest.raises(RuntimeError, match="refresh failed"):
        _ = await svc.sync_us_candles(mode="backfill")


@pytest.mark.asyncio
async def test_sync_us_candles_raises_when_refresh_leaves_universe_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.services.us_candles_sync_service as svc

    fake_session = _RecordingSession()
    kis_client = SimpleNamespace(
        fetch_my_us_stocks=AsyncMock(return_value=[{"ovrs_pdno": "BRK/B"}])
    )
    manual_service = SimpleNamespace(get_holdings_by_user=AsyncMock(return_value=[]))

    monkeypatch.setattr(svc, "AsyncSessionLocal", lambda: fake_session)
    monkeypatch.setattr(svc, "KISClient", lambda: kis_client)
    monkeypatch.setattr(svc, "ManualHoldingsService", lambda session: manual_service)
    monkeypatch.setattr(
        svc,
        "get_us_exchange_by_symbol",
        AsyncMock(
            side_effect=USSymbolNotRegisteredError(
                f"US symbol 'BRK.B' is not registered in us_symbol_universe. {US_UNIVERSE_SYNC_HINT}"
            )
        ),
    )
    monkeypatch.setattr(
        svc,
        "sync_us_symbol_universe",
        AsyncMock(
            return_value={"total": 0, "inserted": 0, "updated": 0, "deactivated": 0}
        ),
        raising=False,
    )

    with pytest.raises(USSymbolUniverseEmptyError, match="us_symbol_universe is empty"):
        _ = await svc.sync_us_candles(mode="backfill")


@pytest.mark.asyncio
async def test_sync_us_candles_incremental_skips_when_current_minute_is_not_trading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.services.us_candles_sync_service as svc

    fake_session = _RecordingSession()
    fetch_my_us_stocks = AsyncMock(return_value=[{"ovrs_pdno": "AAPL"}])
    get_holdings_by_user = AsyncMock(return_value=[SimpleNamespace(ticker="MSFT")])
    get_us_exchange = AsyncMock(side_effect=["NASD", "NYSE"])

    monkeypatch.setattr(svc, "datetime", _FrozenDateTime)
    monkeypatch.setattr(svc, "AsyncSessionLocal", lambda: fake_session)
    monkeypatch.setattr(
        svc, "KISClient", lambda: SimpleNamespace(fetch_my_us_stocks=fetch_my_us_stocks)
    )
    monkeypatch.setattr(
        svc,
        "ManualHoldingsService",
        lambda session: SimpleNamespace(get_holdings_by_user=get_holdings_by_user),
    )
    monkeypatch.setattr(svc, "get_us_exchange_by_symbol", get_us_exchange)
    monkeypatch.setattr(
        svc, "_get_xnys_calendar", lambda: _FakeCalendar(trading_minute=False)
    )

    result = await svc.sync_us_candles(mode="incremental", user_id=77)

    assert result["mode"] == "incremental"
    assert result["sessions"] == 10
    assert result["skipped"] is True
    assert result["skip_reasons"] == {"outside_trading_minute": 2}
    assert result["skipped_symbols"] == []
    assert result["lookup_refresh_attempted"] is False
    assert result["symbols_total"] == 2
    assert result["symbol_venues_total"] == 2
    assert result["pairs_processed"] == 0
    assert result["pairs_skipped"] == 2
    assert result["rows_upserted"] == 0
    assert result["pages_fetched"] == 0
    get_holdings_by_user.assert_awaited_once_with(user_id=77, market_type=MarketType.US)
    fetch_my_us_stocks.assert_awaited_once()
    assert fake_session.closed is True


@pytest.mark.asyncio
async def test_sync_us_candles_persists_refresh_before_outside_trading_minute_return(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.services.us_candles_sync_service as svc

    fake_session = _RecordingSession()
    fetch_my_us_stocks = AsyncMock(return_value=[{"ovrs_pdno": "BRK/B"}])
    get_holdings_by_user = AsyncMock(return_value=[])
    get_us_exchange = AsyncMock(
        side_effect=[
            USSymbolNotRegisteredError(
                f"US symbol 'BRK.B' is not registered in us_symbol_universe. {US_UNIVERSE_SYNC_HINT}"
            ),
            "NYSE",
        ]
    )
    sync_universe = AsyncMock(
        return_value={"total": 1, "inserted": 1, "updated": 0, "deactivated": 0}
    )

    monkeypatch.setattr(svc, "datetime", _FrozenDateTime)
    monkeypatch.setattr(svc, "AsyncSessionLocal", lambda: fake_session)
    monkeypatch.setattr(
        svc, "KISClient", lambda: SimpleNamespace(fetch_my_us_stocks=fetch_my_us_stocks)
    )
    monkeypatch.setattr(
        svc,
        "ManualHoldingsService",
        lambda session: SimpleNamespace(get_holdings_by_user=get_holdings_by_user),
    )
    monkeypatch.setattr(svc, "get_us_exchange_by_symbol", get_us_exchange)
    monkeypatch.setattr(svc, "sync_us_symbol_universe", sync_universe, raising=False)
    monkeypatch.setattr(
        svc, "_get_xnys_calendar", lambda: _FakeCalendar(trading_minute=False)
    )

    result = await svc.sync_us_candles(mode="incremental")

    assert result == {
        "mode": "incremental",
        "sessions": 10,
        "skipped": True,
        "skip_reasons": {"outside_trading_minute": 1},
        "skipped_symbols": [],
        "lookup_refresh_attempted": True,
        "symbols_total": 1,
        "symbol_venues_total": 1,
        "pairs_processed": 0,
        "pairs_skipped": 1,
        "rows_upserted": 0,
        "pages_fetched": 0,
    }
    sync_universe.assert_awaited_once_with(db=fake_session)
    assert fake_session.commits == 1


@pytest.mark.asyncio
async def test_sync_us_candles_backfill_returns_kr_style_final_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.services.us_candles_sync_service as svc

    fake_session = _RecordingSession()
    fetch_my_us_stocks = AsyncMock(return_value=[{"ovrs_pdno": "AAPL"}])
    get_holdings_by_user = AsyncMock(return_value=[])
    get_us_exchange = AsyncMock(return_value="NASD")
    collect_window_rows = AsyncMock(return_value=([], 3))
    upsert_rows = AsyncMock(return_value=0)

    monkeypatch.setattr(svc, "AsyncSessionLocal", lambda: fake_session)
    monkeypatch.setattr(
        svc, "KISClient", lambda: SimpleNamespace(fetch_my_us_stocks=fetch_my_us_stocks)
    )
    monkeypatch.setattr(
        svc,
        "ManualHoldingsService",
        lambda session: SimpleNamespace(get_holdings_by_user=get_holdings_by_user),
    )
    monkeypatch.setattr(svc, "get_us_exchange_by_symbol", get_us_exchange)
    monkeypatch.setattr(
        svc,
        "_select_closed_sessions",
        lambda now_utc, sessions: [
            SimpleNamespace(
                open_utc=datetime(2026, 3, 7, 14, 30, tzinfo=UTC),
                last_minute_utc=datetime(2026, 3, 7, 20, 59, tzinfo=UTC),
            )
        ],
    )
    monkeypatch.setattr(svc, "_collect_window_rows", collect_window_rows)
    monkeypatch.setattr(svc, "_upsert_rows", upsert_rows)

    result = await svc.sync_us_candles(mode="backfill", sessions=3, user_id=5)

    assert result == {
        "mode": "backfill",
        "sessions": 3,
        "skipped": False,
        "skip_reasons": {},
        "skipped_symbols": [],
        "lookup_refresh_attempted": False,
        "symbols_total": 1,
        "symbol_venues_total": 1,
        "pairs_processed": 1,
        "pairs_skipped": 0,
        "rows_upserted": 0,
        "pages_fetched": 3,
    }
    collect_window_rows.assert_awaited_once()
    upsert_rows.assert_awaited_once()
    assert fake_session.commits == 1
    assert fake_session.closed is True


def test_select_closed_sessions_uses_calendar_window_for_dst_aware_backfill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.services.us_candles_sync_service as svc

    calendar = _FakeCalendar(
        last_closed="2026-11-30",
        window=["2026-11-26", "2026-11-27", "2026-11-30"],
        session_bounds={
            "2026-11-26": (
                datetime(2026, 11, 26, 14, 30, tzinfo=UTC),
                datetime(2026, 11, 26, 21, 0, tzinfo=UTC),
            ),
            "2026-11-27": (
                datetime(2026, 11, 27, 14, 30, tzinfo=UTC),
                datetime(2026, 11, 27, 18, 0, tzinfo=UTC),
            ),
            "2026-11-30": (
                datetime(2026, 11, 30, 14, 30, tzinfo=UTC),
                datetime(2026, 11, 30, 21, 0, tzinfo=UTC),
            ),
        },
    )
    monkeypatch.setattr(svc, "_get_xnys_calendar", lambda: calendar)

    sessions = svc._select_closed_sessions(datetime(2026, 12, 1, 15, 0, tzinfo=UTC), 2)

    assert [item.session.strftime("%Y-%m-%d") for item in sessions] == [
        "2026-11-27",
        "2026-11-30",
    ]
    assert sessions[0].open_utc == datetime(2026, 11, 27, 14, 30, tzinfo=UTC)
    assert sessions[0].close_utc == datetime(2026, 11, 27, 18, 0, tzinfo=UTC)
    assert sessions[0].last_minute_utc == datetime(2026, 11, 27, 17, 59, tzinfo=UTC)


def test_compute_incremental_lower_bound_uses_five_minute_overlap() -> None:
    import app.services.us_candles_sync_service as svc

    lower_bound = svc._compute_incremental_lower_bound(
        datetime(2026, 3, 9, 15, 0, tzinfo=UTC),
        datetime(2026, 3, 9, 13, 30, tzinfo=UTC),
    )

    assert lower_bound == datetime(2026, 3, 9, 14, 55, tzinfo=UTC)


def test_compute_incremental_lower_bound_clamps_to_current_session_open() -> None:
    import app.services.us_candles_sync_service as svc

    lower_bound = svc._compute_incremental_lower_bound(
        datetime(2026, 3, 9, 13, 32, tzinfo=UTC),
        datetime(2026, 3, 9, 13, 30, tzinfo=UTC),
    )

    assert lower_bound == datetime(2026, 3, 9, 13, 30, tzinfo=UTC)


@pytest.mark.asyncio
async def test_collect_window_rows_stops_when_lower_bound_is_reached() -> None:
    import app.services.us_candles_sync_service as svc

    inquire = AsyncMock(
        side_effect=[
            _make_page(
                [
                    {
                        "datetime": datetime(2026, 3, 9, 11, 58),
                        "open": 10,
                        "high": 11,
                        "low": 9,
                        "close": 10.5,
                        "volume": 100,
                        "value": 1000,
                    },
                    {
                        "datetime": datetime(2026, 3, 9, 11, 59),
                        "open": 11,
                        "high": 12,
                        "low": 10,
                        "close": 11.5,
                        "volume": 110,
                        "value": 1100,
                    },
                ],
                has_more=True,
                next_keyb="page-2",
            ),
            _make_page(
                [
                    {
                        "datetime": datetime(2026, 3, 9, 11, 54),
                        "open": 8,
                        "high": 9,
                        "low": 7,
                        "close": 8.5,
                        "volume": 80,
                        "value": 800,
                    },
                    {
                        "datetime": datetime(2026, 3, 9, 11, 55),
                        "open": 9,
                        "high": 10,
                        "low": 8,
                        "close": 9.5,
                        "volume": 90,
                        "value": 900,
                    },
                ],
                has_more=True,
                next_keyb="page-3",
            ),
        ]
    )
    rows, page_calls = await svc._collect_window_rows(
        kis=_KISStub(inquire),
        symbol="AAPL",
        exchange="NASD",
        lower_bound_utc=datetime(2026, 3, 9, 15, 55, tzinfo=UTC),
        upper_bound_utc=datetime(2026, 3, 9, 15, 59, tzinfo=UTC),
    )

    assert page_calls == 2
    assert [row.time_utc for row in rows] == [
        datetime(2026, 3, 9, 15, 55, tzinfo=UTC),
        datetime(2026, 3, 9, 15, 58, tzinfo=UTC),
        datetime(2026, 3, 9, 15, 59, tzinfo=UTC),
    ]
    assert inquire.await_count == 2


@pytest.mark.asyncio
async def test_collect_window_rows_stops_when_kis_has_no_more_history() -> None:
    import app.services.us_candles_sync_service as svc

    inquire = AsyncMock(
        side_effect=[
            _make_page(
                [
                    {
                        "datetime": datetime(2026, 3, 9, 11, 58),
                        "open": 10,
                        "high": 11,
                        "low": 9,
                        "close": 10.5,
                        "volume": 100,
                        "value": 1000,
                    },
                ],
                has_more=False,
            )
        ]
    )
    rows, page_calls = await svc._collect_window_rows(
        kis=_KISStub(inquire),
        symbol="AAPL",
        exchange="NASD",
        lower_bound_utc=datetime(2026, 3, 9, 15, 30, tzinfo=UTC),
        upper_bound_utc=datetime(2026, 3, 9, 15, 59, tzinfo=UTC),
    )

    assert page_calls == 1
    assert [row.time_utc for row in rows] == [datetime(2026, 3, 9, 15, 58, tzinfo=UTC)]


@pytest.mark.asyncio
async def test_collect_window_rows_stops_before_next_keyb_crosses_lower_bound() -> None:
    import app.services.us_candles_sync_service as svc

    inquire = AsyncMock(
        return_value=_make_page(
            [
                {
                    "datetime": datetime(2026, 3, 9, 11, 58),
                    "open": 10,
                    "high": 11,
                    "low": 9,
                    "close": 10.5,
                    "volume": 100,
                    "value": 1000,
                },
                {
                    "datetime": datetime(2026, 3, 9, 11, 59),
                    "open": 11,
                    "high": 12,
                    "low": 10,
                    "close": 11.5,
                    "volume": 110,
                    "value": 1100,
                },
            ],
            has_more=True,
            next_keyb="20260309115400",
        )
    )

    rows, page_calls = await svc._collect_window_rows(
        kis=_KISStub(inquire),
        symbol="AAPL",
        exchange="NASD",
        lower_bound_utc=datetime(2026, 3, 9, 15, 55, tzinfo=UTC),
        upper_bound_utc=datetime(2026, 3, 9, 15, 59, tzinfo=UTC),
    )

    assert page_calls == 1
    assert inquire.await_count == 1
    assert [row.time_utc for row in rows] == [
        datetime(2026, 3, 9, 15, 58, tzinfo=UTC),
        datetime(2026, 3, 9, 15, 59, tzinfo=UTC),
    ]


@pytest.mark.asyncio
async def test_normalize_minute_page_and_upsert_payload_are_utc() -> None:
    import app.services.us_candles_sync_service as svc

    frame = pd.DataFrame(
        [
            {
                "datetime": datetime(2026, 3, 9, 9, 30),
                "open": 100,
                "high": 101,
                "low": 99,
                "close": 100.5,
                "volume": 250,
                "value": 25000,
            }
        ]
    )
    rows = svc._normalize_minute_page(
        frame=frame,
        symbol="AAPL",
        exchange="NASD",
        lower_bound_utc=datetime(2026, 3, 9, 13, 30, tzinfo=UTC),
        upper_bound_utc=datetime(2026, 3, 9, 13, 30, tzinfo=UTC),
    )
    assert [row.time_utc for row in rows] == [datetime(2026, 3, 9, 13, 30, tzinfo=UTC)]

    fake_session = _RecordingSession(existing_rows=[], upsert_rowcount=1)
    inserted = await svc._upsert_rows(
        cast(AsyncSession, cast(object, fake_session)), rows
    )

    assert inserted == 1
    _, payload = fake_session.executed[-1]
    assert payload == [
        {
            "time": datetime(2026, 3, 9, 13, 30, tzinfo=UTC),
            "symbol": "AAPL",
            "exchange": "NASD",
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.5,
            "volume": 250.0,
            "value": 25000.0,
        }
    ]


@pytest.mark.asyncio
async def test_upsert_rows_returns_actual_affected_rowcount() -> None:
    import app.services.us_candles_sync_service as svc

    frame = pd.DataFrame(
        [
            {
                "datetime": datetime(2026, 3, 9, 9, 30),
                "open": 100,
                "high": 101,
                "low": 99,
                "close": 100.5,
                "volume": 250,
                "value": 25000,
            }
        ]
    )
    rows = svc._normalize_minute_page(
        frame=frame,
        symbol="AAPL",
        exchange="NASD",
        lower_bound_utc=datetime(2026, 3, 9, 13, 30, tzinfo=UTC),
        upper_bound_utc=datetime(2026, 3, 9, 13, 30, tzinfo=UTC),
    )

    fake_session = _RecordingSession(existing_rows=[], upsert_rowcount=0)
    inserted = await svc._upsert_rows(
        cast(AsyncSession, cast(object, fake_session)), rows
    )

    assert inserted == 0


@pytest.mark.asyncio
async def test_upsert_rows_skips_matching_existing_row_without_upsert() -> None:
    import app.services.us_candles_sync_service as svc

    frame = pd.DataFrame(
        [
            {
                "datetime": datetime(2026, 3, 9, 9, 30),
                "open": 100,
                "high": 101,
                "low": 99,
                "close": 100.5,
                "volume": 250,
                "value": 25000,
            }
        ]
    )
    rows = svc._normalize_minute_page(
        frame=frame,
        symbol="AAPL",
        exchange="NASD",
        lower_bound_utc=datetime(2026, 3, 9, 13, 30, tzinfo=UTC),
        upper_bound_utc=datetime(2026, 3, 9, 13, 30, tzinfo=UTC),
    )

    fake_session = _RecordingSession(
        existing_rows=[
            {
                "time": datetime(2026, 3, 9, 13, 30, tzinfo=UTC),
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.5,
                "volume": 250.0,
                "value": 25000.0,
            }
        ],
        upsert_rowcount=1,
    )

    inserted = await svc._upsert_rows(
        cast(AsyncSession, cast(object, fake_session)), rows
    )

    assert inserted == 0
    assert len(fake_session.executed) == 1
    assert fake_session.executed[0][0] is svc._EXISTING_ROWS_SQL


@pytest.mark.asyncio
async def test_run_us_candles_sync_success_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.jobs import us_candles

    monkeypatch.setattr(
        us_candles,
        "sync_us_candles",
        AsyncMock(
            return_value={
                "mode": "incremental",
                "rows_upserted": 11,
                "lookup_refresh_attempted": True,
                "skipped_symbols": ["BRK.B"],
                "skip_reasons": {"unresolved_symbol_after_refresh": 1},
            }
        ),
    )

    result = await us_candles.run_us_candles_sync(mode="incremental")

    assert result["status"] == "completed"
    assert result["mode"] == "incremental"
    assert result["rows_upserted"] == 11
    assert result["lookup_refresh_attempted"] is True
    assert result["skipped_symbols"] == ["BRK.B"]
    assert result["skip_reasons"] == {"unresolved_symbol_after_refresh": 1}


@pytest.mark.asyncio
async def test_run_us_candles_sync_failure_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.jobs import us_candles

    monkeypatch.setattr(
        us_candles,
        "sync_us_candles",
        AsyncMock(side_effect=RuntimeError("sync failure")),
    )

    result = await us_candles.run_us_candles_sync(mode="incremental")

    assert result["status"] == "failed"
    assert result["mode"] == "incremental"
    assert "sync failure" in str(result["error"])


@pytest.mark.asyncio
async def test_us_task_payload_success(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.tasks import us_candles_tasks

    monkeypatch.setattr(
        us_candles_tasks,
        "run_us_candles_sync",
        AsyncMock(
            return_value={
                "status": "completed",
                "rows_upserted": 3,
                "lookup_refresh_attempted": True,
                "skipped_symbols": ["BRK.B"],
                "skip_reasons": {"unresolved_symbol_after_refresh": 1},
            }
        ),
    )

    result = await us_candles_tasks.sync_us_candles_incremental_task()

    assert result["status"] == "completed"
    assert result["rows_upserted"] == 3
    assert result["lookup_refresh_attempted"] is True
    assert result["skipped_symbols"] == ["BRK.B"]
    assert result["skip_reasons"] == {"unresolved_symbol_after_refresh": 1}


@pytest.mark.asyncio
async def test_us_task_payload_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.tasks import us_candles_tasks

    monkeypatch.setattr(
        us_candles_tasks,
        "run_us_candles_sync",
        AsyncMock(side_effect=RuntimeError("task crash")),
    )

    result = await us_candles_tasks.sync_us_candles_incremental_task()

    assert result["status"] == "failed"
    assert result["mode"] == "incremental"
    assert "task crash" in str(result["error"])


def test_us_task_schedule_metadata() -> None:
    import app.tasks as task_package
    from app.tasks import us_candles_tasks

    module_source = inspect.getsource(us_candles_tasks)

    assert 'task_name="candles.us.sync"' in module_source
    assert (
        'schedule=[{"cron": "*/10 * * * *", "cron_offset": "Asia/Seoul"}]'
        in module_source
    )
    assert task_package.us_candles_tasks in task_package.TASKIQ_TASK_MODULES


@pytest.mark.asyncio
async def test_us_script_main_exit_codes(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts import sync_us_candles

    success_mock = AsyncMock(return_value={"status": "completed", "rows_upserted": 1})
    monkeypatch.setattr(sync_us_candles, "init_sentry", lambda **_: None)
    monkeypatch.setattr(
        sync_us_candles,
        "run_us_candles_sync",
        success_mock,
    )
    success_code = await sync_us_candles.main(
        ["--mode", "backfill", "--sessions", "3", "--user-id", "9"]
    )
    assert success_code == 0
    success_mock.assert_awaited_once_with(mode="backfill", sessions=3, user_id=9)

    monkeypatch.setattr(
        sync_us_candles,
        "run_us_candles_sync",
        AsyncMock(return_value={"status": "failed", "error": "boom"}),
    )
    failed_status_code = await sync_us_candles.main(["--mode", "incremental"])
    assert failed_status_code == 1

    capture_mock = MagicMock()
    monkeypatch.setattr(sync_us_candles, "capture_exception", capture_mock)
    monkeypatch.setattr(
        sync_us_candles,
        "run_us_candles_sync",
        AsyncMock(side_effect=RuntimeError("hard crash")),
    )
    exception_code = await sync_us_candles.main(["--mode", "incremental"])
    assert exception_code == 1
    capture_mock.assert_called_once()

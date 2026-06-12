from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pandas as pd
import pytest


def _make_universe_row(
    symbol: str,
    *,
    nxt_eligible: bool,
    is_active: bool,
):
    from app.models.kr_symbol_universe import KRSymbolUniverse

    return KRSymbolUniverse(
        symbol=symbol,
        name=f"NAME-{symbol}",
        exchange="KOSPI",
        nxt_eligible=nxt_eligible,
        is_active=is_active,
    )


def test_build_symbol_union_combines_kis_and_manual_symbols() -> None:
    from app.services import kr_candles_sync_service as svc
    from app.services.candles_sync_common import build_symbol_union

    kis_holdings = [
        {"pdno": "5930"},
        {"pdno": "035420"},
        {"pdno": None},
    ]
    manual_holdings = [
        SimpleNamespace(ticker="005930"),
        SimpleNamespace(ticker="000660"),
    ]

    symbols = build_symbol_union(
        kis_holdings,
        manual_holdings,
        holdings_field="pdno",
        normalize_fn=svc._normalize_symbol,
    )

    assert symbols == {"005930", "035420", "000660"}


def test_validate_universe_rows_fails_when_table_empty() -> None:
    from app.services import kr_candles_sync_service as svc

    with pytest.raises(ValueError, match="kr_symbol_universe is empty"):
        svc._validate_universe_rows(
            target_symbols={"005930"},
            universe_rows=[],
            table_has_rows=False,
        )


def test_validate_universe_rows_fails_when_symbol_missing() -> None:
    from app.services import kr_candles_sync_service as svc

    row = _make_universe_row("005930", nxt_eligible=True, is_active=True)

    with pytest.raises(ValueError, match="not registered"):
        svc._validate_universe_rows(
            target_symbols={"005930", "000660"},
            universe_rows=[row],
            table_has_rows=True,
        )


def test_validate_universe_rows_fails_when_symbol_inactive() -> None:
    from app.services import kr_candles_sync_service as svc

    inactive_row = _make_universe_row("005930", nxt_eligible=False, is_active=False)

    with pytest.raises(ValueError, match="inactive"):
        svc._validate_universe_rows(
            target_symbols={"005930"},
            universe_rows=[inactive_row],
            table_has_rows=True,
        )


def test_build_venue_plan_maps_dual_and_single_venues() -> None:
    from app.services import kr_candles_sync_service as svc

    rows_by_symbol = {
        "005930": _make_universe_row("005930", nxt_eligible=True, is_active=True),
        "000660": _make_universe_row("000660", nxt_eligible=False, is_active=True),
    }

    plan = svc._build_venue_plan(rows_by_symbol)

    assert [v.venue for v in plan["005930"]] == ["KRX", "NTX"]
    assert [v.market_code for v in plan["005930"]] == ["J", "NX"]
    assert [v.venue for v in plan["000660"]] == ["KRX"]


def test_should_process_venue_skips_holiday_in_incremental_mode() -> None:
    from app.services import kr_candles_sync_service as svc

    venue = svc._VENUE_CONFIG["KRX"]
    now_kst = datetime(2026, 1, 1, 10, 0, tzinfo=svc._KST)

    should_process, reason = svc._should_process_venue(
        mode="incremental",
        now_kst=now_kst,
        is_session_day=False,
        venue=venue,
    )

    assert should_process is False
    assert reason == "holiday"


def test_should_process_venue_skips_outside_session_in_incremental_mode() -> None:
    from app.services import kr_candles_sync_service as svc

    venue = svc._VENUE_CONFIG["KRX"]
    now_kst = datetime(2026, 2, 23, 8, 10, tzinfo=svc._KST)

    should_process, reason = svc._should_process_venue(
        mode="incremental",
        now_kst=now_kst,
        is_session_day=True,
        venue=venue,
    )

    assert should_process is False
    assert reason == "outside_session"


def test_compute_incremental_cutoff_uses_five_minute_overlap() -> None:
    from app.services import kr_candles_sync_service as svc

    cursor_utc = datetime(2026, 2, 23, 1, 30, tzinfo=UTC)

    cutoff_kst = svc._compute_incremental_cutoff_kst(cursor_utc)

    assert cutoff_kst is not None
    assert cutoff_kst.tzinfo == svc._KST
    assert cutoff_kst.strftime("%Y-%m-%d %H:%M:%S") == "2026-02-23 10:25:00"


def test_convert_kis_datetime_to_utc_interprets_naive_as_kst() -> None:
    from app.services import kr_candles_sync_service as svc

    converted = svc._convert_kis_datetime_to_utc(datetime(2026, 2, 23, 9, 0, 0))

    assert converted.tzinfo == UTC
    assert converted.strftime("%Y-%m-%d %H:%M:%S") == "2026-02-23 00:00:00"


@pytest.mark.asyncio
async def test_run_kr_candles_sync_success_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.jobs import kr_candles

    monkeypatch.setattr(
        kr_candles,
        "sync_kr_candles",
        AsyncMock(return_value={"mode": "incremental", "rows_upserted": 11}),
    )

    result = await kr_candles.run_kr_candles_sync(mode="incremental")

    assert result["status"] == "completed"
    assert result["mode"] == "incremental"
    assert result["rows_upserted"] == 11


@pytest.mark.asyncio
async def test_run_kr_candles_sync_failure_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.jobs import kr_candles

    monkeypatch.setattr(
        kr_candles,
        "sync_kr_candles",
        AsyncMock(side_effect=RuntimeError("sync failure")),
    )

    result = await kr_candles.run_kr_candles_sync(mode="incremental")

    assert result["status"] == "failed"
    assert "sync failure" in str(result["error"])


@pytest.mark.asyncio
async def test_task_payload_success(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.tasks import kr_candles_tasks

    monkeypatch.setattr(
        kr_candles_tasks,
        "run_kr_candles_sync",
        AsyncMock(return_value={"status": "completed", "rows_upserted": 3}),
    )

    result = await kr_candles_tasks.sync_kr_candles_incremental_task()

    assert result["status"] == "completed"
    assert result["rows_upserted"] == 3


@pytest.mark.asyncio
async def test_task_payload_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.tasks import kr_candles_tasks

    monkeypatch.setattr(
        kr_candles_tasks,
        "run_kr_candles_sync",
        AsyncMock(side_effect=RuntimeError("task crash")),
    )

    result = await kr_candles_tasks.sync_kr_candles_incremental_task()

    assert result["status"] == "failed"
    assert "task crash" in str(result["error"])


@pytest.mark.asyncio
async def test_script_main_exit_codes(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.core.cli as _cli
    from scripts import sync_kr_candles

    monkeypatch.setattr(_cli, "init_sentry", lambda **_: None)
    monkeypatch.setattr(
        sync_kr_candles,
        "run_kr_candles_sync",
        AsyncMock(return_value={"status": "completed", "rows_upserted": 1}),
    )
    success_code = await sync_kr_candles.main(["--mode", "incremental"])
    assert success_code == 0

    monkeypatch.setattr(
        sync_kr_candles,
        "run_kr_candles_sync",
        AsyncMock(return_value={"status": "failed", "error": "boom"}),
    )
    failed_status_code = await sync_kr_candles.main(["--mode", "incremental"])
    assert failed_status_code == 1

    capture_mock = MagicMock()
    monkeypatch.setattr(_cli, "capture_exception", capture_mock)
    monkeypatch.setattr(
        sync_kr_candles,
        "run_kr_candles_sync",
        AsyncMock(side_effect=RuntimeError("hard crash")),
    )
    exception_code = await sync_kr_candles.main(["--mode", "incremental"])
    assert exception_code == 1
    capture_mock.assert_called_once()


def test_new_retention_migration_contains_upgrade_and_downgrade_policy_sql() -> None:
    versions_dir = Path("alembic/versions")
    matches = sorted(versions_dir.glob("*_add_kr_candles_retention_policy.py"))
    assert matches, "retention migration file is missing"

    content = matches[-1].read_text(encoding="utf-8")

    assert "add_retention_policy" in content
    assert "remove_retention_policy" in content
    assert "kr_candles_1m" in content
    assert "kr_candles_5m" in content
    assert "kr_candles_15m" in content
    assert "kr_candles_30m" in content
    assert "kr_candles_1h" in content
    assert "90 days" in content


def test_sql_script_contains_90_day_retention_policy_for_both_tables() -> None:
    content = Path("scripts/sql/kr_candles_timescale.sql").read_text(encoding="utf-8")

    assert "add_retention_policy" in content
    assert "remove_retention_policy" in content
    assert "v_relation_prefix CONSTANT TEXT := 'public.kr_candles_'" in content
    assert "'1m'" in content
    assert "'5m'" in content
    assert "'15m'" in content
    assert "'30m'" in content
    assert "'1h'" in content
    assert "timescaledb.materialized_only = false" in content
    assert "INTERVAL '5 minutes'" in content or "INTERVAL ''5 minutes''" in content
    assert "90 days" in content


def test_kr_candles_task_cron_remains_ten_minutes() -> None:
    content = Path("app/tasks/kr_candles_tasks.py").read_text(encoding="utf-8")

    assert 'task_name="candles.kr.sync"' in content
    assert '"cron": "*/10 * * * 1-5"' in content


@pytest.mark.asyncio
async def test_run_kr_candles_sync_passes_source(monkeypatch):
    from app.jobs import kr_candles

    sync_mock = AsyncMock(return_value={"success": True})
    monkeypatch.setattr(kr_candles, "sync_kr_candles", sync_mock)

    result = await kr_candles.run_kr_candles_sync(mode="incremental", source="toss")

    sync_mock.assert_awaited_once_with(mode="incremental", sessions=10, user_id=1, source="toss")
    assert result["success"] is True


@pytest.mark.asyncio
async def test_sync_kr_candles_cli_accepts_source(monkeypatch):
    from scripts import sync_kr_candles

    run_mock = AsyncMock(return_value={"status": "completed", "success": True})
    monkeypatch.setattr(sync_kr_candles, "run_kr_candles_sync", run_mock)

    code = await sync_kr_candles.main(["--mode", "incremental", "--source", "toss"])

    assert code == 0
    run_mock.assert_awaited_once()
    assert run_mock.await_args.kwargs["source"] == "toss"


@pytest.mark.asyncio
async def test_sync_kr_candles_toss_source_warning(monkeypatch):
    from app.services.kr_candles_sync_service import sync_kr_candles

    class DummyKISClient:
        async def fetch_my_stocks(self):
            return [{"pdno": "005930"}]

    class DummyManualHoldingsService:
        def __init__(self, session):
            self.session = session

        async def get_holdings_by_user(self, *, user_id, market_type):
            return []

    class DummySession:
        async def commit(self):
            return None

        async def rollback(self):
            return None

        async def close(self):
            return None

    session = DummySession()
    toss_df = pd.DataFrame(
        [
            {
                "datetime": pd.Timestamp("2026-06-12 09:00:00"),
                "open": 100.0,
                "high": 110.0,
                "low": 90.0,
                "close": 105.0,
                "volume": 1000.0,
                "value": 105000.0,
            }
        ]
    )
    upsert_mock = AsyncMock(return_value=1)

    monkeypatch.setattr(
        "app.services.kr_candles_sync_service.KISClient",
        DummyKISClient,
    )
    monkeypatch.setattr(
        "app.services.kr_candles_sync_service.ManualHoldingsService",
        DummyManualHoldingsService,
    )
    monkeypatch.setattr(
        "app.services.kr_candles_sync_service.AsyncSessionLocal",
        lambda: session,
    )
    monkeypatch.setattr(
        "app.services.kr_candles_sync_service._load_universe_context",
        AsyncMock(
            return_value=(
                [_make_universe_row("005930", nxt_eligible=True, is_active=True)],
                True,
            )
        ),
    )
    monkeypatch.setattr(
        "app.services.kr_candles_sync_service.fetch_kr_intraday_toss_frame",
        AsyncMock(return_value=toss_df),
    )
    monkeypatch.setattr(
        "app.services.kr_candles_sync_service._upsert_rows",
        upsert_mock,
    )

    result = await sync_kr_candles(mode="incremental", source="toss")

    assert result["source"] == "toss"
    assert result["rows_upserted"] == 1
    assert result["symbol_venues_total"] == 1
    assert result["pairs_processed"] == 1
    assert any("provider source column" in w for w in result.get("warnings", []))
    upsert_mock.assert_awaited_once()



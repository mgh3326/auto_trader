"""Offline contracts for the Kiwoom Stage 2 KR daily collector.

Every broker interaction in this module is a stub.  These tests deliberately
exercise the collection boundary rather than the Stage 1 transport: the latter
remains immutable and is covered by its existing safety suite.
"""

from __future__ import annotations

import ast
import asyncio
import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import text

from app.services.brokers.kiwoom.stage2_daily_collect import (
    CollectionResult,
    KiwoomDailyCandle,
    KiwoomDailyCandleRepository,
    KiwoomStage2CollectionDisabled,
    KiwoomStage2DailyCollector,
    ResumeCheckpoint,
    StoredKrDailyCandle,
    arm_scoped_environment,
    load_scoped_env_file,
)

pytestmark = pytest.mark.unit


def _payload(*dates: str) -> dict[str, object]:
    return {
        "return_code": 0,
        "stk_dt_pole_chart_qry": [
            {
                "dt": day,
                "cur_prc": "70100",
                "open_pric": "69800",
                "high_pric": "70500",
                "low_pric": "69600",
                "trde_qty": "9263135",
                "trde_prica": "648525",
            }
            for day in dates
        ],
    }


class StubDailyClient:
    def __init__(
        self,
        payloads: dict[str, dict[str, object]] | None = None,
        failures: set[str] | None = None,
    ) -> None:
        self.payloads = payloads or {}
        self.failures = failures or set()
        self.calls: list[str] = []

    async def fetch_daily_chart(
        self,
        *,
        symbol: str,
        base_dt: str,
        adjusted: bool = True,
    ) -> dict[str, object]:
        del base_dt, adjusted
        self.calls.append(symbol)
        if symbol in self.failures:
            raise RuntimeError("stubbed fetch failure")
        return self.payloads.get(symbol, _payload("20260828", "20260829"))


@dataclass
class MemoryWriter:
    """A table-shaped stub: an existing key is never overwritten."""

    rows: dict[tuple[str, str], str] = field(default_factory=dict)
    calls: list[tuple[KiwoomDailyCandle, ...]] = field(default_factory=list)

    async def __call__(self, rows: Sequence[KiwoomDailyCandle]) -> int:
        batch = tuple(rows)
        self.calls.append(batch)
        inserted = 0
        for row in batch:
            key = (row.symbol, row.session_date)
            if key not in self.rows:
                self.rows[key] = "kiwoom_live"
                inserted += 1
        return inserted


def _arm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KIWOOM_LIVE_MARKETDATA_ENABLED", "true")
    monkeypatch.setenv("KIWOOM_STAGE2_COLLECT_ENABLED", "true")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("live_enabled", "collect_enabled", "expected"),
    [
        ("false", "true", "KIWOOM_LIVE_MARKETDATA_ENABLED=true"),
        ("true", "false", "KIWOOM_STAGE2_COLLECT_ENABLED=true"),
    ],
)
async def test_gates_refuse_before_the_stub_client_is_reached(
    monkeypatch: pytest.MonkeyPatch,
    live_enabled: str,
    collect_enabled: str,
    expected: str,
) -> None:
    monkeypatch.setenv("KIWOOM_LIVE_MARKETDATA_ENABLED", live_enabled)
    monkeypatch.setenv("KIWOOM_STAGE2_COLLECT_ENABLED", collect_enabled)
    client = StubDailyClient()
    collector = KiwoomStage2DailyCollector(client=client)

    with pytest.raises(KiwoomStage2CollectionDisabled, match=expected):
        await collector.collect(symbols=["005930"], bars=600, rate_seconds=2.0)

    assert client.calls == []


@pytest.mark.asyncio
async def test_rate_floor_rejects_sub_half_second_before_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _arm(monkeypatch)
    client = StubDailyClient()
    collector = KiwoomStage2DailyCollector(client=client)

    with pytest.raises(ValueError, match="--rate-seconds must be at least 0.5"):
        await collector.collect(symbols=["005930"], bars=600, rate_seconds=0.2)

    assert client.calls == []


@pytest.mark.asyncio
async def test_three_symbol_stub_e2e_is_dry_run_then_idempotent_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _arm(monkeypatch)
    symbols = ["000001", "000002", "000003"]
    client = StubDailyClient()
    writer = MemoryWriter()
    collector = KiwoomStage2DailyCollector(client=client, write_rows=writer)

    dry_result = await collector.collect(
        symbols=symbols,
        bars=600,
        rate_seconds=0.5,
        commit=False,
    )

    assert dry_result.rows_inserted == 0
    assert dry_result.rows_received == 6
    assert writer.calls == []

    committed = await collector.collect(
        symbols=symbols,
        bars=600,
        rate_seconds=0.5,
        commit=True,
    )
    repeated = await collector.collect(
        symbols=symbols,
        bars=600,
        rate_seconds=0.5,
        commit=True,
    )

    assert committed.rows_inserted == 6
    assert repeated.rows_inserted == 0
    assert len(writer.rows) == 6


@pytest.mark.asyncio
async def test_resume_starts_after_the_last_checkpointed_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _arm(monkeypatch)
    symbols = ["000001", "000002", "000003"]
    checkpoint = ResumeCheckpoint(tmp_path / "collector.resume.json")
    first_client = StubDailyClient()
    writer = MemoryWriter()

    async def interrupt_after_second(symbol: str) -> None:
        if symbol == "000002":
            raise asyncio.CancelledError

    first = KiwoomStage2DailyCollector(
        client=first_client,
        write_rows=writer,
        after_success=interrupt_after_second,
    )
    with pytest.raises(asyncio.CancelledError):
        await first.collect(
            symbols=symbols,
            bars=600,
            rate_seconds=0.5,
            commit=True,
            checkpoint=checkpoint,
        )

    resumed_client = StubDailyClient()
    resumed = KiwoomStage2DailyCollector(
        client=resumed_client,
        write_rows=writer,
    )
    result = await resumed.collect(
        symbols=symbols,
        bars=600,
        rate_seconds=0.5,
        commit=True,
        checkpoint=checkpoint,
        resume=True,
    )

    assert first_client.calls == ["000001", "000002"]
    assert resumed_client.calls == ["000003"]
    assert result.resumed_from == "000002"
    assert not checkpoint.path.exists()


@pytest.mark.asyncio
async def test_symbol_failures_are_isolated_and_existing_source_is_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _arm(monkeypatch)
    client = StubDailyClient(failures={"000002"})
    writer = MemoryWriter(rows={("000001", "20260828"): "kis"})
    collector = KiwoomStage2DailyCollector(client=client, write_rows=writer)

    result = await collector.collect(
        symbols=["000001", "000002", "000003"],
        bars=600,
        rate_seconds=0.5,
        commit=True,
    )

    assert client.calls == ["000001", "000002", "000003"]
    assert result.failed_symbols == ("000002",)
    assert writer.rows[("000001", "20260828")] == "kis"
    assert writer.rows[("000003", "20260828")] == "kiwoom_live"


@pytest.mark.asyncio
async def test_verify_sample_reports_only_outside_tolerance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _arm(monkeypatch)
    client = StubDailyClient(payloads={"005930": _payload("20260828")})

    async def sample_symbols(limit: int) -> list[str]:
        assert limit == 1
        return ["005930"]

    async def existing_rows(symbol: str, bars: int) -> list[StoredKrDailyCandle]:
        assert (symbol, bars) == ("005930", 600)
        return [
            StoredKrDailyCandle(
                symbol="005930",
                session_date="20260828",
                open=Decimal("69801"),
                high=Decimal("70500"),
                low=Decimal("69600"),
                close=Decimal("70100"),
                volume=Decimal("9263135"),
                value=Decimal("648525000000"),
            )
        ]

    collector = KiwoomStage2DailyCollector(
        client=client,
        sample_existing_symbols=sample_symbols,
        load_existing_rows=existing_rows,
    )
    result = await collector.collect(
        symbols=["000001"],
        bars=600,
        rate_seconds=0.5,
        verify_sample=1,
    )

    assert result.verification[0].symbol == "005930"
    assert result.verification[0].mismatches == ()


def test_prod_named_env_file_is_refused(tmp_path: Path) -> None:
    env_file = tmp_path / ".env.kiwoom-prod.native"
    env_file.write_text("KIWOOM_LIVE_APP_KEY=not-a-real-key\n", encoding="utf-8")

    with pytest.raises(ValueError, match="refusing to read a production env file"):
        load_scoped_env_file(env_file)


def test_scoped_environment_does_not_inherit_an_armed_missing_gate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env.kiwoom-readonly.native"
    env_file.write_text(
        "\n".join(
            [
                "KIWOOM_LIVE_APP_KEY=stub-key",
                "KIWOOM_LIVE_APP_SECRET=stub-secret",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("KIWOOM_LIVE_MARKETDATA_ENABLED", "true")
    monkeypatch.setenv("KIWOOM_STAGE2_COLLECT_ENABLED", "true")
    monkeypatch.setenv("KIWOOM_LIVE_APP_KEY", "inherited-key")
    monkeypatch.setenv("KIWOOM_LIVE_APP_SECRET", "inherited-secret")
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6380/8")

    arm_scoped_environment(
        env_file=env_file,
        redis_url="redis://127.0.0.1:6380/9",
    )

    assert os.environ["KIWOOM_LIVE_MARKETDATA_ENABLED"] == "false"
    assert os.environ["KIWOOM_STAGE2_COLLECT_ENABLED"] == "false"


def test_cli_rejects_a_rate_below_the_documented_floor() -> None:
    from scripts.kiwoom_collect_kr_daily import parse_args

    with pytest.raises(SystemExit):
        parse_args(
            [
                "--env-file",
                ".env.kiwoom-readonly.native",
                "--redis-url",
                "redis://127.0.0.1:6380/9",
                "--rate-seconds",
                "0.2",
            ]
        )


@pytest.mark.asyncio
async def test_cli_defaults_to_dry_run_until_commit_is_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import kiwoom_collect_kr_daily as cli

    captured: dict[str, object] = {}

    class CapturingCollector:
        async def collect(self, **kwargs: object) -> CollectionResult:
            captured.update(kwargs)
            return CollectionResult(
                total_symbols=1,
                processed_symbols=1,
                rows_received=2,
                rows_inserted=0,
                rows_conflict_skipped=0,
                invalid_rows=0,
                failures=(),
                verification=(),
                verification_failures=(),
                resumed_from=None,
                elapsed_seconds=0.0,
                commit=bool(kwargs["commit"]),
            )

    monkeypatch.setattr(cli, "arm_scoped_environment", lambda **_: None)
    monkeypatch.setattr(cli, "build_default_collector", CapturingCollector)

    exit_code = await cli.main(
        [
            "--env-file",
            ".env.kiwoom-readonly.native",
            "--redis-url",
            "redis://127.0.0.1:6380/9",
            "--symbols",
            "005930",
        ]
    )

    assert exit_code == 0
    assert captured["commit"] is False


@pytest.mark.asyncio
async def test_repository_inserts_missing_rows_without_source_overwrite() -> None:
    session = MagicMock()
    execute_result = MagicMock()
    execute_result.scalars.return_value.all.return_value = [
        datetime(2026, 8, 28, tzinfo=UTC)
    ]
    session.execute = AsyncMock(return_value=execute_result)
    repo = KiwoomDailyCandleRepository(session=session)
    candle = KiwoomDailyCandle(
        symbol="005930",
        session_date="20260828",
        time_utc=datetime(2026, 8, 28, tzinfo=UTC),
        open=Decimal("69800"),
        high=Decimal("70500"),
        low=Decimal("69600"),
        close=Decimal("70100"),
        volume=Decimal("9263135"),
        value=Decimal("648525000000"),
    )

    inserted = await repo.insert_missing([candle])

    assert inserted == 1
    statement = session.execute.await_args.args[0]
    assert "ON CONFLICT (time, symbol, venue) DO NOTHING" in str(statement)
    assert "RETURNING public.kr_candles_1d.time" in str(statement)
    assert "kiwoom_live" in statement.compile().params.values()


@pytest.mark.asyncio
async def test_repository_counts_bulk_new_rows_then_conflicts_with_test_db(
    db_session,
) -> None:
    """Exercise asyncpg against test_db, rather than a synthetic result."""
    symbol = "ROB2000S2"
    rows = tuple(
        KiwoomDailyCandle(
            symbol=symbol,
            session_date=(
                datetime(2024, 1, 1, tzinfo=UTC) + timedelta(days=index)
            ).strftime("%Y%m%d"),
            time_utc=datetime(2024, 1, 1, tzinfo=UTC) + timedelta(days=index),
            open=Decimal("69800"),
            high=Decimal("70500"),
            low=Decimal("69600"),
            close=Decimal("70100"),
            volume=Decimal("9263135"),
            value=Decimal("648525000000"),
        )
        for index in range(600)
    )
    repo = KiwoomDailyCandleRepository(session=db_session)

    try:
        inserted = await repo.insert_missing(rows)
        await db_session.commit()
        stored = await db_session.scalar(
            text("SELECT count(*) FROM public.kr_candles_1d WHERE symbol = :symbol"),
            {"symbol": symbol},
        )

        skipped = await repo.insert_missing(rows)
        await db_session.commit()

        assert stored == len(rows)
        assert inserted == len(rows)
        assert skipped == 0
    finally:
        await db_session.rollback()
        await db_session.execute(
            text("DELETE FROM public.kr_candles_1d WHERE symbol = :symbol"),
            {"symbol": symbol},
        )
        await db_session.commit()


@pytest.mark.asyncio
async def test_collector_reports_real_bulk_inserts_then_conflicts(
    monkeypatch: pytest.MonkeyPatch,
    db_session,
) -> None:
    """Keep the commit summary aligned with actual rows for three 600-row symbols."""
    _arm(monkeypatch)
    symbols = ("200001", "200002", "200003")
    dates = tuple(
        (datetime(2024, 1, 1, tzinfo=UTC) + timedelta(days=index)).strftime("%Y%m%d")
        for index in range(600)
    )
    repo = KiwoomDailyCandleRepository(session=db_session)

    async def write_rows(rows: Sequence[KiwoomDailyCandle]) -> int:
        inserted = await repo.insert_missing(rows)
        await db_session.commit()
        return inserted

    collector = KiwoomStage2DailyCollector(
        client=StubDailyClient(
            payloads={symbol: _payload(*dates) for symbol in symbols}
        ),
        write_rows=write_rows,
        sleep=AsyncMock(),
    )
    try:
        first = await collector.collect(
            symbols=symbols,
            bars=600,
            rate_seconds=0.5,
            commit=True,
        )
        repeated = await collector.collect(
            symbols=symbols,
            bars=600,
            rate_seconds=0.5,
            commit=True,
        )

        assert first.rows_received == 1_800
        assert first.rows_inserted == 1_800
        assert first.rows_conflict_skipped == 0
        assert repeated.rows_inserted == 0
        assert repeated.rows_conflict_skipped == 1_800
    finally:
        await db_session.rollback()
        for symbol in symbols:
            await db_session.execute(
                text("DELETE FROM public.kr_candles_1d WHERE symbol = :symbol"),
                {"symbol": symbol},
            )
        await db_session.commit()


def test_new_collector_has_no_order_or_account_import_or_reference() -> None:
    import app.services.brokers.kiwoom.stage2_daily_collect as stage2

    source = Path(stage2.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    forbidden = (
        "domestic_orders",
        "us_orders",
        "order_preflight",
        "domestic_account",
        "us_account",
        "kiwoom.client",
        "account_no",
        "KIWOOM_ACCOUNT_NO",
        "kt10000",
    )
    rendered_imports = "\n".join(
        (
            getattr(node, "module", "") or ""
            if isinstance(node, ast.ImportFrom)
            else ",".join(alias.name for alias in node.names)
        )
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
    )
    assert all(fragment not in source for fragment in forbidden[6:])
    assert all(fragment not in rendered_imports for fragment in forbidden[:6])

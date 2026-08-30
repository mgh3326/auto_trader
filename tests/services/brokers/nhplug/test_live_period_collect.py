"""Stub-only collection contracts for NHPLUG live period quotes."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.dialects import postgresql

from app.services.brokers.kiwoom.chart_compare import FrozenBar
from app.services.brokers.nhplug.live_period_collect import (
    NHPlugPeriodCollectionDisabled,
    NHPlugPeriodCollector,
    NHPlugPeriodRepository,
    NHPlugPeriodResponseError,
    PeriodCandle,
    ResumeCheckpoint,
    StoredKiwoomCandle,
    VerificationClassification,
    _response_rows,
    load_scoped_env_file,
)
from app.services.brokers.nhplug.live_quotes import NHPlugLiveQuotesSecurityBlocked

pytestmark = pytest.mark.unit


def _kr_payload(*, close: str = "70100") -> dict[str, object]:
    return {
        "Output_1": [
            {
                "bsop_date": "20260828",
                "stck_oprc": "69800",
                "stck_hgpr": "70500",
                "stck_lwpr": "69600",
                "stck_prpr": close,
                "vol": "9263135",
                "tr_pbmn": "648525000000",
            }
        ]
    }


def _us_payload() -> dict[str, object]:
    return {
        "Output_1": [
            {
                "trade_date": "20260828",
                "open_prc": "100.00",
                "high": "102.00",
                "low": "99.00",
                "close_prc": "101.00",
                "movolume": "1000",
                "movalue": "101000",
            }
        ]
    }


def _indexfx_payload() -> dict[str, object]:
    return {
        "Output_1": [
            {
                "bsop_date": "20260828",
                "ovrs_oprc": "100.00",
                "ovrs_hgpr": "102.00",
                "ovrs_lwpr": "99.00",
                "ovrs_prpr": "101.00",
                "vol": "1000",
            }
        ]
    }


class StubPeriodClient:
    def __init__(self, *, kr_close: str = "70100") -> None:
        self.kr_close = kr_close
        self.calls: list[tuple[str, str]] = []
        self.request_kwargs: list[dict[str, str | int]] = []
        self.fail_symbols: set[str] = set()

    async def fetch_kr_period(self, **kwargs: str | int) -> dict[str, object]:
        symbol = str(kwargs["symbol"])
        self.calls.append(("kr", symbol))
        self.request_kwargs.append(dict(kwargs))
        if symbol in self.fail_symbols:
            raise RuntimeError("stub kr failure")
        return _kr_payload(close=self.kr_close)

    async def fetch_us_period(self, **kwargs: str | int) -> dict[str, object]:
        symbol = str(kwargs["symbol"])
        self.calls.append(("us", symbol))
        self.request_kwargs.append(dict(kwargs))
        if symbol in self.fail_symbols:
            raise RuntimeError("stub us failure")
        return _us_payload()

    async def fetch_index_fx_period(self, **kwargs: str | int) -> dict[str, object]:
        symbol = str(kwargs["symbol"])
        self.calls.append(("indexfx", symbol))
        self.request_kwargs.append(dict(kwargs))
        if symbol in self.fail_symbols:
            raise RuntimeError("stub indexfx failure")
        return _indexfx_payload()


@dataclass
class MemoryStore:
    kr_rows: dict[tuple[str, str], str] = field(default_factory=dict)
    us_rows: dict[tuple[str, str, str], str] = field(default_factory=dict)
    kiwoom_rows: dict[str, list[StoredKiwoomCandle]] = field(default_factory=dict)
    kr_writes: list[tuple[PeriodCandle, ...]] = field(default_factory=list)
    us_writes: list[tuple[PeriodCandle, ...]] = field(default_factory=list)

    async def list_active_kr_symbols(self) -> list[str]:
        return ["005930"]

    async def list_active_us_targets(self) -> list[tuple[str, str]]:
        return [("AAPL", "NASD")]

    async def resolve_us_symbols(self, symbols: Sequence[str]) -> list[tuple[str, str]]:
        return [(symbol, "NASD") for symbol in symbols]

    async def insert_missing_kr(self, rows: Sequence[PeriodCandle]) -> int:
        batch = tuple(rows)
        self.kr_writes.append(batch)
        inserted = 0
        for row in batch:
            key = (row.symbol, row.session_date)
            if key not in self.kr_rows:
                self.kr_rows[key] = "nhplug_live"
                inserted += 1
        return inserted

    async def insert_missing_us(self, rows: Sequence[PeriodCandle]) -> int:
        batch = tuple(rows)
        self.us_writes.append(batch)
        inserted = 0
        for row in batch:
            key = (row.symbol, row.exchange or "", row.session_date)
            if key not in self.us_rows:
                self.us_rows[key] = "nhplug_live"
                inserted += 1
        return inserted

    async def sample_kiwoom_symbols(self, limit: int) -> list[str]:
        return list(self.kiwoom_rows)[:limit]

    async def load_kiwoom_rows(
        self, symbol: str, bars: int
    ) -> list[StoredKiwoomCandle]:
        del bars
        return self.kiwoom_rows.get(symbol, [])


def _arm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NHPLUG_LIVE_QUOTES_ENABLED", "true")


def test_missing_output_requires_an_explicit_no_data_response_message() -> None:
    with pytest.raises(NHPlugPeriodResponseError):
        _response_rows({"rsp_msg": "invalid input"})
    with pytest.raises(NHPlugPeriodResponseError):
        _response_rows({"rsp_msg": "completed"})
    assert _response_rows({"rsp_msg": "조회 결과가 없습니다"}) == []


@pytest.mark.asyncio
async def test_gate_and_documented_rate_floor_refuse_before_stub_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = StubPeriodClient()
    collector = NHPlugPeriodCollector(client=client)

    monkeypatch.setenv("NHPLUG_LIVE_QUOTES_ENABLED", "false")
    with pytest.raises(NHPlugPeriodCollectionDisabled):
        await collector.collect(
            market="kr",
            symbols=["005930"],
            start_date="20260801",
            end_date="20260831",
            bars=30,
            rate_seconds=0.2,
        )
    assert client.calls == []

    _arm(monkeypatch)
    with pytest.raises(ValueError, match="at least 0.2"):
        await collector.collect(
            market="kr",
            symbols=["005930"],
            start_date="20260801",
            end_date="20260831",
            bars=30,
            rate_seconds=0.19,
        )
    assert client.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("market", "symbols"),
    (("kr", ["005930"]), ("us", ["AAPL"]), ("indexfx", ["SPX"])),
)
async def test_each_market_is_dry_run_then_commit_is_idempotent(
    monkeypatch: pytest.MonkeyPatch, market: str, symbols: list[str]
) -> None:
    _arm(monkeypatch)
    client = StubPeriodClient()
    store = MemoryStore()
    collector = NHPlugPeriodCollector(client=client, store=store)
    kwargs = {
        "market": market,
        "symbols": symbols,
        "start_date": "20260801",
        "end_date": "20260831",
        "bars": 30,
        "rate_seconds": 0.2,
    }

    dry = await collector.collect(**kwargs)

    assert dry.rows_received == 1
    assert dry.rows_inserted == 0
    assert store.kr_writes == []
    assert store.us_writes == []

    first = await collector.collect(**kwargs, commit=True)
    second = await collector.collect(**kwargs, commit=True)
    if market == "indexfx":
        assert first.rows_inserted == second.rows_inserted == 0
        assert first.persistence_status == "SCHEMA_PROPOSAL_REQUIRED"
    else:
        assert first.rows_inserted == 1
        assert second.rows_inserted == 0
        assert second.rows_conflict_skipped == 1


@pytest.mark.asyncio
async def test_resume_restarts_after_last_committed_symbol(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _arm(monkeypatch)
    client = StubPeriodClient()
    store = MemoryStore()
    checkpoint = ResumeCheckpoint(tmp_path / "period.resume.json")

    async def interrupt_after_second(symbol: str) -> None:
        if symbol == "000002":
            raise asyncio.CancelledError

    first = NHPlugPeriodCollector(
        client=client, store=store, after_success=interrupt_after_second
    )
    with pytest.raises(asyncio.CancelledError):
        await first.collect(
            market="kr",
            symbols=["000001", "000002", "000003"],
            start_date="20260801",
            end_date="20260831",
            bars=30,
            rate_seconds=0.2,
            commit=True,
            checkpoint=checkpoint,
        )

    resumed_client = StubPeriodClient()
    resumed = NHPlugPeriodCollector(client=resumed_client, store=store)
    result = await resumed.collect(
        market="kr",
        symbols=["000001", "000002", "000003"],
        start_date="20260801",
        end_date="20260831",
        bars=30,
        rate_seconds=0.2,
        commit=True,
        resume=True,
        checkpoint=checkpoint,
    )

    assert client.calls == [("kr", "000001"), ("kr", "000002")]
    assert resumed_client.calls == [("kr", "000003")]
    assert result.resumed_from == "000002"
    assert not checkpoint.path.exists()


@pytest.mark.asyncio
async def test_window_is_explicitly_widened_before_the_daily_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A one-bar flag cannot silently turn an August request into a weekend miss."""

    _arm(monkeypatch)
    client = StubPeriodClient()
    collector = NHPlugPeriodCollector(client=client)

    await collector.collect(
        market="kr",
        symbols=["005930"],
        start_date="20260801",
        end_date="20260831",
        bars=1,
        rate_seconds=0.2,
    )

    assert client.request_kwargs == [
        {"symbol": "005930", "end_date": "20260831", "bars": 31}
    ]


@pytest.mark.asyncio
async def test_one_symbol_failure_does_not_stop_later_symbols(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _arm(monkeypatch)
    client = StubPeriodClient()
    client.fail_symbols.add("000002")
    store = MemoryStore()
    collector = NHPlugPeriodCollector(client=client, store=store)

    result = await collector.collect(
        market="kr",
        symbols=["000001", "000002", "000003"],
        start_date="20260801",
        end_date="20260831",
        bars=30,
        rate_seconds=0.2,
        commit=True,
    )

    assert client.calls == [("kr", "000001"), ("kr", "000002"), ("kr", "000003")]
    assert result.failed_symbols == ("000002",)
    assert result.rows_inserted == 2


@pytest.mark.asyncio
async def test_security_block_is_not_buried_as_a_symbol_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _arm(monkeypatch)

    class SecurityBlockedClient(StubPeriodClient):
        async def fetch_kr_period(self, **kwargs: str | int) -> dict[str, object]:
            symbol = str(kwargs["symbol"])
            self.calls.append(("kr", symbol))
            raise NHPlugLiveQuotesSecurityBlocked(
                "NHPLUG live token_issue stopped (HTTP 403): "
                "보안 차단 가능성, 쿨다운 필요"
            )

    client = SecurityBlockedClient()
    collector = NHPlugPeriodCollector(client=client)

    with pytest.raises(NHPlugLiveQuotesSecurityBlocked, match="쿨다운 필요"):
        await collector.collect(
            market="kr",
            symbols=["000001", "000002", "000003"],
            start_date="20260801",
            end_date="20260831",
            bars=30,
            rate_seconds=0.2,
        )

    assert client.calls == [("kr", "000001")]


@pytest.mark.asyncio
async def test_kr_verify_sample_reports_the_kis_supported_side(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _arm(monkeypatch)
    client = StubPeriodClient(kr_close="70200")
    stored = StoredKiwoomCandle(
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
    frozen = FrozenBar(
        symbol="005930",
        session_date="20260828",
        open=Decimal("69800"),
        high=Decimal("70500"),
        low=Decimal("69600"),
        close=Decimal("70200"),
        volume=Decimal("9263135"),
        value=Decimal("0"),
    )
    store = MemoryStore(kiwoom_rows={"005930": [stored]})
    collector = NHPlugPeriodCollector(
        client=client,
        store=store,
        frozen_loader=lambda: {("005930", "20260828"): frozen},
    )

    result = await collector.collect(
        market="kr",
        symbols=["000001"],
        start_date="20260801",
        end_date="20260831",
        bars=30,
        rate_seconds=0.2,
        verify_sample=1,
    )

    assert (
        result.verification[0].classification
        is VerificationClassification.NHPLUG_MATCHES_KIS
    )


def test_env_file_requires_0600_and_refuses_prod_name(tmp_path: Path) -> None:
    permissive = tmp_path / ".env.nhplug-live"
    permissive.write_text("NHPLUG_LIVE_APP_KEY=stub\n", encoding="utf-8")
    permissive.chmod(0o644)
    with pytest.raises(ValueError, match="0600"):
        load_scoped_env_file(permissive)

    prod = tmp_path / ".env.prod.nhplug-live"
    prod.write_text("not read", encoding="utf-8")
    prod.chmod(0o600)
    with pytest.raises(ValueError, match="production env file"):
        load_scoped_env_file(prod)


@pytest.mark.asyncio
async def test_repository_uses_returning_as_the_exact_insert_witness() -> None:
    session = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = [datetime(2026, 8, 28, tzinfo=UTC)]
    session.execute.return_value = result
    repository = NHPlugPeriodRepository(session=session)
    row = PeriodCandle(
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

    inserted = await repository.insert_missing_kr([row])

    statement = session.execute.call_args.args[0]
    compiled = str(statement.compile(dialect=postgresql.dialect()))
    assert inserted == 1
    assert "ON CONFLICT (time, symbol, venue) DO NOTHING" in compiled
    assert "RETURNING public.kr_candles_1d.time" in compiled

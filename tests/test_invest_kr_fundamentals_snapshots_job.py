from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pandas as pd
import pytest
from sqlalchemy import text

from app.jobs import invest_kr_fundamentals_snapshots as job
from app.jobs.invest_kr_fundamentals_snapshots import (
    KrFundamentalsSnapshotBuildRequest,
    run_kr_fundamentals_snapshot_build,
    run_kr_fundamentals_snapshot_build_guarded,
)
from app.services.invest_kr_fundamentals_snapshots import provider as provider_mod
from app.services.invest_kr_fundamentals_snapshots.builder import (
    KrFundamentalsProviderRow,
)
from app.services.invest_kr_fundamentals_snapshots.provider import (
    KR_FUNDAMENTALS_FULL_FETCH_MIN_LIMIT,
    TvScreenerKrFundamentalsProvider,
)
from app.services.snapshot_commit_guard import PartialCommitBlocked
from scripts import build_invest_kr_fundamentals_snapshots as cli

_JOB_SYMBOL = "990010"


class _FakeProvider:
    async def fetch_rows(
        self, *, limit: int | None = None
    ) -> list[KrFundamentalsProviderRow]:
        rows = [
            KrFundamentalsProviderRow(
                symbol=_JOB_SYMBOL,
                name="잡테스트",
                price=Decimal("12345"),
                roe_ttm=Decimal("10.0"),
            )
        ]
        return rows[:limit] if limit is not None else rows


class _RecordingProvider:
    limits: list[int | None] = []

    async def fetch_rows(
        self, *, limit: int | None = None
    ) -> list[KrFundamentalsProviderRow]:
        self.__class__.limits.append(limit)
        return [
            KrFundamentalsProviderRow(
                symbol=_JOB_SYMBOL,
                name="잡테스트",
                price=Decimal("12345"),
                roe_ttm=Decimal("10.0"),
            )
        ]


class _FakeStockField:
    ACTIVE_SYMBOL = "symbol"
    PRICE = "price"


class _FakeMarket:
    KOREA = "korea"


class _FakeTvScreenerModule:
    StockField = _FakeStockField
    Market = _FakeMarket


class _RecordingTvScreenerService:
    limits: list[int | None] = []

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def query_stock_screener(self, **kwargs):
        self.__class__.limits.append(kwargs.get("limit"))
        return pd.DataFrame(
            [
                {
                    "symbol": _JOB_SYMBOL,
                    "price": "12345",
                    "description": "잡테스트",
                }
            ]
        )


@pytest.mark.asyncio
async def test_run_job_dry_run_persists_nothing(db_session):
    await db_session.execute(
        text("DELETE FROM invest_kr_fundamentals_snapshots WHERE symbol = :s"),
        {"s": _JOB_SYMBOL},
    )
    await db_session.commit()

    with patch(
        "app.jobs.invest_kr_fundamentals_snapshots.TvScreenerKrFundamentalsProvider",
        _FakeProvider,
    ):
        result = await run_kr_fundamentals_snapshot_build(
            KrFundamentalsSnapshotBuildRequest(limit=5, commit=False)
        )

    assert result["committed"] is False
    assert result["would_upsert"] == 1
    assert result["upserted"] == 0

    persisted = (
        await db_session.execute(
            text(
                "SELECT count(*) FROM invest_kr_fundamentals_snapshots "
                "WHERE symbol = :s"
            ),
            {"s": _JOB_SYMBOL},
        )
    ).scalar_one()
    assert persisted == 0


@pytest.mark.asyncio
async def test_run_job_all_symbols_uses_active_universe_buffer(monkeypatch):
    _RecordingProvider.limits = []
    monkeypatch.setattr(job, "TvScreenerKrFundamentalsProvider", _RecordingProvider)
    monkeypatch.setattr(job, "active_universe_count", AsyncMock(return_value=7000))

    result = await run_kr_fundamentals_snapshot_build(
        KrFundamentalsSnapshotBuildRequest(all_symbols=True, commit=False)
    )

    assert _RecordingProvider.limits == [8000]
    assert result["fetched"] == 1
    assert result["would_upsert"] == 1


@pytest.mark.asyncio
async def test_run_job_all_symbols_falls_back_to_provider_floor_when_universe_missing():
    limit = await job._fetch_limit_for_request(  # noqa: SLF001 - regression seam
        KrFundamentalsSnapshotBuildRequest(all_symbols=True), universe_count=0
    )

    assert limit is None


@pytest.mark.asyncio
async def test_provider_none_limit_uses_full_fetch_floor(monkeypatch):
    _RecordingTvScreenerService.limits = []
    monkeypatch.setattr(
        provider_mod, "_import_tvscreener", lambda: _FakeTvScreenerModule
    )
    monkeypatch.setattr(provider_mod, "TvScreenerService", _RecordingTvScreenerService)

    rows = await TvScreenerKrFundamentalsProvider().fetch_rows(limit=None)

    assert _RecordingTvScreenerService.limits == [KR_FUNDAMENTALS_FULL_FETCH_MIN_LIMIT]
    assert [row.symbol for row in rows] == [_JOB_SYMBOL]


@pytest.mark.asyncio
async def test_provider_non_positive_limit_returns_no_rows():
    assert await TvScreenerKrFundamentalsProvider().fetch_rows(limit=0) == []


@pytest.mark.asyncio
async def test_guarded_job_blocks_thin_commit(monkeypatch):
    calls: list[bool] = []

    async def _fake_run(request):
        calls.append(request.commit)
        return {"would_upsert": 200, "committed": request.commit}

    monkeypatch.setattr(job, "run_kr_fundamentals_snapshot_build", _fake_run)
    monkeypatch.setattr(job, "active_universe_count", AsyncMock(return_value=4000))

    with pytest.raises(PartialCommitBlocked):
        await run_kr_fundamentals_snapshot_build_guarded(
            KrFundamentalsSnapshotBuildRequest(all_symbols=True, commit=True)
        )

    assert calls == [False]


@pytest.mark.asyncio
async def test_guarded_job_commits_when_coverage_is_healthy(monkeypatch):
    calls: list[bool] = []

    async def _fake_run(request):
        calls.append(request.commit)
        return {"would_upsert": 3000, "committed": request.commit}

    monkeypatch.setattr(job, "run_kr_fundamentals_snapshot_build", _fake_run)
    monkeypatch.setattr(job, "active_universe_count", AsyncMock(return_value=4000))

    result = await run_kr_fundamentals_snapshot_build_guarded(
        KrFundamentalsSnapshotBuildRequest(all_symbols=True, commit=True)
    )

    assert calls == [False, True]
    assert result["committed"] is True


def test_cli_allow_partial_arg() -> None:
    assert cli.parse_args(["--commit", "--allow-partial"]).allow_partial is True
    assert cli.parse_args([]).allow_partial is False


@pytest.mark.asyncio
async def test_cli_routes_guarded_by_default(monkeypatch):
    guarded = AsyncMock(return_value={"committed": True})
    plain = AsyncMock(return_value={"committed": True})
    monkeypatch.setattr(cli, "run_kr_fundamentals_snapshot_build_guarded", guarded)
    monkeypatch.setattr(cli, "run_kr_fundamentals_snapshot_build", plain)

    assert await cli.run(cli.parse_args(["--commit", "--all"])) == 0
    assert guarded.await_count == 1 and plain.await_count == 0

    guarded.reset_mock()
    plain.reset_mock()
    assert await cli.run(cli.parse_args(["--commit", "--allow-partial"])) == 0
    assert plain.await_count == 1 and guarded.await_count == 0


@pytest.mark.asyncio
async def test_cli_returns_2_when_guard_blocks(monkeypatch):
    guarded = AsyncMock(side_effect=PartialCommitBlocked("blocked"))
    plain = AsyncMock(return_value={"committed": True})
    monkeypatch.setattr(cli, "run_kr_fundamentals_snapshot_build_guarded", guarded)
    monkeypatch.setattr(cli, "run_kr_fundamentals_snapshot_build", plain)

    assert await cli.run(cli.parse_args(["--commit"])) == 2
    assert guarded.await_count == 1
    assert plain.await_count == 0

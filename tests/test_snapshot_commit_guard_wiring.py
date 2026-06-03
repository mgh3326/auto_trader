from __future__ import annotations

import datetime as dt
from unittest.mock import AsyncMock

import pytest

from app.jobs import market_quote_snapshots as quote_job
from app.services.snapshot_commit_guard import PartialCommitBlocked


def _quote_result(*, market="kr", built, committed):
    return quote_job.MarketQuoteSnapshotBuildResult(
        market=market,
        symbols_resolved=built,
        snapshots_built=built,
        committed=committed,
        batches=1,
        started_at=dt.datetime(2026, 6, 3, tzinfo=dt.UTC),
        finished_at=dt.datetime(2026, 6, 3, tzinfo=dt.UTC),
    )


@pytest.mark.asyncio
async def test_quote_guarded_wrapper_blocks_thin(monkeypatch):
    calls: list[bool] = []

    async def _fake_run(request):
        calls.append(request.commit)
        return _quote_result(built=20, committed=request.commit)

    monkeypatch.setattr(quote_job, "run_market_quote_snapshot_build", _fake_run)
    monkeypatch.setattr(quote_job, "active_universe_count", AsyncMock(return_value=100))

    req = quote_job.MarketQuoteSnapshotBuildRequest(
        market="kr", all_symbols=True, commit=True
    )
    with pytest.raises(PartialCommitBlocked):
        await quote_job.run_market_quote_snapshot_build_guarded(req)
    assert calls == [False]


@pytest.mark.asyncio
async def test_quote_guarded_wrapper_commits_when_healthy(monkeypatch):
    calls: list[bool] = []

    async def _fake_run(request):
        calls.append(request.commit)
        return _quote_result(built=80, committed=request.commit)

    monkeypatch.setattr(quote_job, "run_market_quote_snapshot_build", _fake_run)
    monkeypatch.setattr(quote_job, "active_universe_count", AsyncMock(return_value=100))

    req = quote_job.MarketQuoteSnapshotBuildRequest(
        market="kr", all_symbols=True, commit=True
    )
    result = await quote_job.run_market_quote_snapshot_build_guarded(req)
    assert calls == [False, True]
    assert result.committed is True


@pytest.mark.asyncio
async def test_quote_guarded_wrapper_skips_crypto(monkeypatch):
    calls: list[bool] = []

    async def _fake_run(request):
        calls.append(request.commit)
        return _quote_result(market="crypto", built=5, committed=request.commit)

    monkeypatch.setattr(quote_job, "run_market_quote_snapshot_build", _fake_run)
    monkeypatch.setattr(
        quote_job,
        "active_universe_count",
        AsyncMock(side_effect=AssertionError("must not be called for crypto")),
    )

    req = quote_job.MarketQuoteSnapshotBuildRequest(
        market="crypto", all_symbols=True, commit=True
    )
    result = await quote_job.run_market_quote_snapshot_build_guarded(req)
    assert calls == [False, True]
    assert result.committed is True


from scripts import build_market_quote_snapshots as quote_cli  # noqa: E402


def test_quote_cli_allow_partial_arg():
    args = quote_cli.parse_args(["--all", "--commit", "--allow-partial"])
    assert args.allow_partial is True
    assert quote_cli.parse_args(["--all"]).allow_partial is False


@pytest.mark.asyncio
async def test_quote_cli_routes_guarded_by_default(monkeypatch):
    guarded = AsyncMock(return_value=_quote_result(built=3000, committed=True))
    plain = AsyncMock(return_value=_quote_result(built=20, committed=True))
    monkeypatch.setattr(quote_job, "run_market_quote_snapshot_build_guarded", guarded)
    monkeypatch.setattr(quote_job, "run_market_quote_snapshot_build", plain)

    await quote_cli.run(quote_cli.parse_args(["--all", "--commit"]))
    assert guarded.await_count == 1 and plain.await_count == 0

    guarded.reset_mock()
    plain.reset_mock()
    await quote_cli.run(quote_cli.parse_args(["--all", "--commit", "--allow-partial"]))
    assert plain.await_count == 1 and guarded.await_count == 0


from app.jobs import market_valuation_snapshots as val_job  # noqa: E402
from scripts import build_market_valuation_snapshots as val_cli  # noqa: E402


def _val_result(*, market="kr", built, committed):
    return val_job.MarketValuationSnapshotBuildResult(
        market=market,
        symbols_resolved=built,
        snapshots_built=built,
        committed=committed,
        batches=1,
        started_at=dt.datetime(2026, 6, 3, tzinfo=dt.UTC),
        finished_at=dt.datetime(2026, 6, 3, tzinfo=dt.UTC),
    )


@pytest.mark.asyncio
async def test_valuation_guarded_wrapper_blocks_thin(monkeypatch):
    calls: list[bool] = []

    async def _fake_run(request):
        calls.append(request.commit)
        return _val_result(built=20, committed=request.commit)

    monkeypatch.setattr(val_job, "run_market_valuation_snapshot_build", _fake_run)
    monkeypatch.setattr(val_job, "active_universe_count", AsyncMock(return_value=100))

    req = val_job.MarketValuationSnapshotBuildRequest(
        market="kr", all_symbols=True, commit=True
    )
    with pytest.raises(PartialCommitBlocked):
        await val_job.run_market_valuation_snapshot_build_guarded(req)
    assert calls == [False]


@pytest.mark.asyncio
async def test_valuation_guarded_wrapper_commits_when_healthy(monkeypatch):
    calls: list[bool] = []

    async def _fake_run(request):
        calls.append(request.commit)
        return _val_result(built=80, committed=request.commit)

    monkeypatch.setattr(val_job, "run_market_valuation_snapshot_build", _fake_run)
    monkeypatch.setattr(val_job, "active_universe_count", AsyncMock(return_value=100))

    req = val_job.MarketValuationSnapshotBuildRequest(
        market="kr", all_symbols=True, commit=True
    )
    result = await val_job.run_market_valuation_snapshot_build_guarded(req)
    assert calls == [False, True]
    assert result.committed is True


def test_valuation_cli_allow_partial_arg():
    assert (
        val_cli.parse_args(["--all", "--commit", "--allow-partial"]).allow_partial
        is True
    )
    assert val_cli.parse_args(["--all"]).allow_partial is False


@pytest.mark.asyncio
async def test_valuation_cli_routes_guarded_by_default(monkeypatch):
    guarded = AsyncMock(return_value=_val_result(built=3000, committed=True))
    plain = AsyncMock(return_value=_val_result(built=20, committed=True))
    monkeypatch.setattr(val_job, "run_market_valuation_snapshot_build_guarded", guarded)
    monkeypatch.setattr(val_job, "run_market_valuation_snapshot_build", plain)

    await val_cli.run(val_cli.parse_args(["--all", "--commit"]))
    assert guarded.await_count == 1 and plain.await_count == 0
    guarded.reset_mock()
    plain.reset_mock()
    await val_cli.run(val_cli.parse_args(["--all", "--commit", "--allow-partial"]))
    assert plain.await_count == 1 and guarded.await_count == 0

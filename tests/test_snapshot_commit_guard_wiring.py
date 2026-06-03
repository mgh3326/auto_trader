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

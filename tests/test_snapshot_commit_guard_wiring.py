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


# Reuse the existing fundamentals test helpers from the fundamentals job test
# module: an async-return monkeypatch helper, a fake DART fetcher, and a
# DB-binding pytest fixture (`bind_job_session`, consumed by parameter name in
# the allow-partial test below — the re-export looks like a redefinition to
# ruff, hence the F811 noqa).
import tests.test_financial_fundamentals_job as _fund_test  # noqa: E402
from app.jobs import financial_fundamentals_snapshots as fund_job  # noqa: E402

_async_return = _fund_test._async_return
_fund_fake_fetcher = _fund_test._fake_fetcher
bind_job_session = _fund_test.bind_job_session  # noqa: F811  (re-exported fixture)


@pytest.mark.asyncio
async def test_fundamentals_commit_blocked_without_allow_partial():
    calls: list[str] = []

    async def _spy_fetcher(symbol, *, include_quarterly):
        calls.append(symbol)
        raise AssertionError("fetcher must not run when commit is blocked")

    with pytest.raises(PartialCommitBlocked):
        await fund_job.run_financial_fundamentals_snapshot_build(
            fund_job.FinancialFundamentalsSnapshotBuildRequest(
                market="kr", symbols=("005930",), commit=True, allow_partial=False
            ),
            fetcher=_spy_fetcher,
        )
    assert calls == []  # blocked BEFORE any DART fetch (0 budget)


@pytest.mark.asyncio
async def test_fundamentals_allow_partial_permits_commit(bind_job_session, monkeypatch):
    monkeypatch.setattr(fund_job, "resolve_symbols", _async_return(["005930"]))

    result = await fund_job.run_financial_fundamentals_snapshot_build(
        fund_job.FinancialFundamentalsSnapshotBuildRequest(
            market="kr", symbols=("005930",), commit=True, allow_partial=True
        ),
        fetcher=_fund_fake_fetcher,
    )
    assert result.committed is True
    assert result.snapshots_built >= 1


from scripts import build_financial_fundamentals_snapshots as fund_cli  # noqa: E402


def test_fundamentals_cli_allow_partial_arg():
    assert (
        fund_cli.parse_args(["--symbol", "005930", "--allow-partial"]).allow_partial
        is True
    )
    assert fund_cli.parse_args(["--symbol", "005930"]).allow_partial is False


from app.jobs import invest_screener_snapshots as screener_job  # noqa: E402
from scripts import build_invest_screener_snapshots as screener_cli  # noqa: E402


def _screener_result(*, built, committed):
    return screener_job.SnapshotBuildResult(
        market="kr",
        symbols_resolved=built,
        snapshots_built=built,
        skipped=0,
        committed=committed,
        batches=1,
        started_at=dt.datetime(2026, 6, 3, tzinfo=dt.UTC),
        finished_at=dt.datetime(2026, 6, 3, tzinfo=dt.UTC),
    )


def test_screener_cli_allow_partial_arg():
    # The screener CLI requires --market (no default), unlike the quote/valuation
    # CLIs, so include it here.
    assert (
        screener_cli.parse_args(
            ["--market", "kr", "--all", "--commit", "--allow-partial"]
        ).allow_partial
        is True
    )
    assert screener_cli.parse_args(["--market", "kr", "--all"]).allow_partial is False


@pytest.mark.asyncio
async def test_screener_cli_routes_guarded_by_default(monkeypatch):
    guarded = AsyncMock(return_value=_screener_result(built=3000, committed=True))
    plain = AsyncMock(return_value=_screener_result(built=20, committed=True))
    monkeypatch.setattr(
        screener_cli.snapshot_job, "run_snapshot_build_guarded", guarded
    )
    monkeypatch.setattr(screener_cli.snapshot_job, "run_snapshot_build", plain)

    await screener_cli.run(
        screener_cli.parse_args(["--market", "kr", "--all", "--commit"])
    )
    assert guarded.await_count == 1 and plain.await_count == 0
    guarded.reset_mock()
    plain.reset_mock()
    await screener_cli.run(
        screener_cli.parse_args(
            ["--market", "kr", "--all", "--commit", "--allow-partial"]
        )
    )
    assert plain.await_count == 1 and guarded.await_count == 0

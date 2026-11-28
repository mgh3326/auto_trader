"""CLI contract of ``scripts.request_market_event_backfill`` (hk 6409)."""

from __future__ import annotations

import json
import sys
from contextlib import asynccontextmanager
from typing import Any

import pytest

from scripts import request_market_event_backfill as cli
from tests.services.market_events.backfill_support import NOW, FakeStore

pytestmark = pytest.mark.unit

ARGS = [
    "--source",
    "dart",
    "--market",
    "kr",
    "--from-date",
    "2026-11-01",
    "--to-date",
    "2026-11-20",
    "--reason-code",
    "gap_fill",
    "--reason",
    "outage",
    "--requested-by",
    "operator",
]


def no_session() -> Any:
    raise AssertionError("no database session may be opened")


@asynccontextmanager
async def fake_session():
    yield object()


def lines(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    return [json.loads(line) for line in capsys.readouterr().out.splitlines()]


async def call(
    argv: list[str], *, store: FakeStore | None = None, runner: Any = None
) -> int:
    kwargs: dict[str, Any] = {"now": NOW}
    if store is None:
        kwargs["session_factory"] = no_session
    else:
        kwargs["session_factory"] = fake_session
        kwargs["store_factory"] = lambda _session: store
    if runner is not None:
        kwargs["runner"] = runner
    return await cli.run(cli.parse_args(argv), **kwargs)


@pytest.mark.parametrize(
    "extra",
    [
        ["--run"],
        ["--commit", "--dry-run"],
        ["--show-collection", "--commit"],
    ],
)
def test_inconsistent_modes_are_refused_by_the_parser(extra: list[str]) -> None:
    with pytest.raises(SystemExit):
        cli.parse_args([*ARGS, *extra])


def test_a_request_needs_its_source_and_audit_fields() -> None:
    with pytest.raises(SystemExit):
        cli.parse_args(ARGS[:8])


@pytest.mark.asyncio
async def test_the_default_is_a_dry_run_without_a_database(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert await call(ARGS) == 0
    (line,) = lines(capsys)
    assert line["status"] == "dry_run"
    assert line["plan"]["collector"][0] == "scripts.ingest_market_events"


@pytest.mark.asyncio
async def test_a_range_violation_exits_2_before_any_session(
    capsys: pytest.CaptureFixture[str],
) -> None:
    argv = list(ARGS)
    argv[argv.index("2026-11-01")] = "2026-09-01"
    assert await call([*argv, "--commit"]) == 2
    assert lines(capsys)[0]["code"] == "window_too_long"


@pytest.mark.asyncio
async def test_a_closed_collector_gate_costs_no_quota(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli.settings, "sec_edgar_user_agent", "")
    argv = list(ARGS)
    argv[1:4] = ["sec", "--market", "us"]
    argv += ["--symbols", "NVDA", "--commit", "--run"]
    assert await call(argv) == 2
    line = lines(capsys)[0]
    assert line["code"] == "producer_gate_closed"
    assert "SEC_EDGAR_USER_AGENT" in line["detail"]


@pytest.mark.asyncio
async def test_commit_records_once_and_run_executes_once(
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = FakeStore()
    calls: list[list[str]] = []

    async def runner(argv: list[str]) -> int:
        calls.append(argv)
        return 0

    assert await call([*ARGS, "--commit"], store=store, runner=runner) == 0
    assert await call([*ARGS, "--commit"], store=store, runner=runner) == 0
    assert [line["status"] for line in lines(capsys)] == ["recorded", "duplicate"]
    assert store.states() == ["requested"] and calls == []

    assert await call([*ARGS, "--commit", "--run"], store=store, runner=runner) == 0
    assert [line["status"] for line in lines(capsys)] == ["duplicate", "done"]
    assert await call([*ARGS, "--commit", "--run"], store=store, runner=runner) == 2
    assert lines(capsys)[0]["code"] == "already_run"
    assert len(calls) == 1
    assert store.states() == ["requested", "started", "done"]


@pytest.mark.asyncio
async def test_a_failed_collector_exits_1(
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def runner(_argv: list[str]) -> int:
        return 2

    store = FakeStore()
    assert await call([*ARGS, "--commit", "--run"], store=store, runner=runner) == 1
    last = lines(capsys)[-1]
    assert (last["status"], last["failure_code"]) == ("failed", "exit_nonzero")


@pytest.mark.asyncio
async def test_the_real_runner_runs_python_modules_and_kills_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert await cli.run_collector(["calendar", "2026"]) == 0
    spawned: list[tuple[Any, ...]] = []
    real = cli.asyncio.create_subprocess_exec

    async def spy(*args: Any, **kwargs: Any) -> Any:
        spawned.append(args)
        return await real(*args, **kwargs)

    monkeypatch.setattr(cli.asyncio, "create_subprocess_exec", spy)
    monkeypatch.setattr(cli, "RUN_TIMEOUT_SECONDS", 0.2)
    # ``http.server`` never ends by itself; the runner must kill it.
    with pytest.raises(TimeoutError):
        await cli.run_collector(["http.server", "--bind", "127.0.0.1", "0"])
    assert spawned[0][:3] == (sys.executable, "-m", "http.server")


@pytest.mark.parametrize(
    ("market", "frm", "to", "code"),
    [
        ("jp", "2026-11-01", "2026-11-02", "unsupported_market"),
        ("kr", "2026-11-02", "2026-11-01", "window_reversed"),
    ],
)
@pytest.mark.asyncio
async def test_a_bad_collection_read_opens_no_session(
    market: str, frm: str, to: str, code: str, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["--show-collection", "--market", market]
    argv += ["--from-date", frm, "--to-date", to]
    assert await call(argv) == 2
    assert lines(capsys)[0]["code"] == code


@pytest.mark.asyncio
async def test_show_collection_reads_in_a_read_only_transaction(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    statements: list[str] = []
    ended: list[str] = []

    class Session:
        async def execute(self, statement: Any) -> None:
            statements.append(str(statement))

        async def rollback(self) -> None:
            ended.append("rollback")

        async def commit(self) -> None:
            raise AssertionError("a read commits nothing")

    @asynccontextmanager
    async def factory():
        yield Session()

    async def fake_read(_session: Any, **kwargs: Any) -> dict[str, Any]:
        return {"market": kwargs["market"]}

    monkeypatch.setattr(cli, "read_collection", fake_read)
    argv = ["--show-collection", "--market", "crypto"]
    argv += ["--from-date", "2026-11-01", "--to-date", "2026-11-02"]
    assert await cli.run(cli.parse_args(argv), session_factory=factory) == 0
    assert statements[0].startswith("SET TRANSACTION")
    assert "READ ONLY" in statements[0]
    assert ended == ["rollback"]
    assert lines(capsys)[0]["collection"] == {"market": "crypto"}

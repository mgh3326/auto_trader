"""#1086 backfill CLI: dry-run never writes or calls Toss; commit path; pacing."""

from __future__ import annotations

import io
import json
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.services.research_candles.dart_minute_trigger import (
    DartRow,
    SymbolIndex,
    UniverseRow,
)
from app.services.research_candles.toss_minute_collector import WriteResult
from scripts import backfill_kr_candles_1m_toss as cli

KST = timezone(timedelta(hours=9))
REPO = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 9, 30, 21, 0, tzinfo=KST)


class _Session:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0
        self.executed = []

    async def execute(self, *args, **kwargs):  # pragma: no cover - must not run
        self.executed.append(args)
        raise AssertionError("unexpected SQL in a CLI unit test")

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


@pytest.fixture
def session(monkeypatch):
    s = _Session()

    async def load_rows(sess, start, end):
        assert sess is s
        return [
            DartRow(
                "1", "단일판매ㆍ공급계약체결", "삼성전자", "유", "2026-08-21 10:00"
            ),
            DartRow(
                "2",
                "임원ㆍ주요주주특정증권등소유상황보고서",
                "삼성전자",
                "유",
                "2026-08-21 10:00",
            ),
            DartRow(
                "3",
                "주요사항보고서(유상증자결정)",
                "DL이앤씨",
                "코",
                "2026-08-21 17:00",
            ),
        ]

    async def load_index(sess):
        return SymbolIndex(
            [
                UniverseRow("005930", "삼성전자", True),
                UniverseRow("375500", "DL이앤씨", True),
            ]
        )

    monkeypatch.setattr(cli, "load_dart_rows", load_rows)
    monkeypatch.setattr(cli, "load_symbol_index", load_index)
    return s


def _factory(s):
    @asynccontextmanager
    async def factory():
        yield s

    return factory


def _no_client():
    raise AssertionError("dry-run must not build a Toss client")


@pytest.mark.asyncio
async def test_dry_run_is_default_prints_plan_and_never_writes(session, monkeypatch):
    async def forbidden(*a, **k):
        raise AssertionError("dry-run must not check or write the target")

    monkeypatch.setattr(cli, "assert_insert_privilege", forbidden)
    out = io.StringIO()
    args = cli.parse_args(["--from-date", "2026-08-21", "--to-date", "2026-08-21"])
    assert args.commit is False
    code = await cli.run(
        args,
        session_factory=_factory(session),
        client_factory=_no_client,
        out=out,
        now=lambda: NOW,
    )
    assert code == 0
    assert session.commits == 0 and session.executed == []
    lines = [json.loads(line) for line in out.getvalue().splitlines()]
    assert lines[0]["mode"] == "dry_run"
    assert lines[0]["target_table"] == "research.kr_candles_1m_toss"
    assert lines[0]["skipped"] == {"non_target_type": 1}
    assert [(p["symbol"], p["d0"], p["d1"]) for p in lines[1:]] == [
        ("005930", "2026-08-21", "2026-08-24"),
        ("375500", "2026-08-21", "2026-08-24"),
    ]


@pytest.mark.asyncio
async def test_type_filter_limits_plan(session):
    out = io.StringIO()
    args = cli.parse_args(
        ["--from-date", "2026-08-21", "--to-date", "2026-08-21", "--types", "RIGHTS"]
    )
    await cli.run(
        args,
        session_factory=_factory(session),
        client_factory=_no_client,
        out=out,
        now=lambda: NOW,
    )
    plans = [json.loads(line) for line in out.getvalue().splitlines()[1:]]
    assert [p["symbol"] for p in plans] == ["375500"]


@pytest.mark.parametrize(
    "argv",
    [
        ["--from-date", "2026-08-22", "--to-date", "2026-08-21"],
        ["--from-date", "2026-08-21", "--to-date", "2026-08-21", "--commit"],
        ["--from-date", "2026-08-21", "--to-date", "2026-08-21", "--types", "OTHER"],
        ["--from-date", "2026-08-21", "--to-date", "2026-08-21", "--max-tps", "0"],
    ],
)
def test_bad_args_rejected(argv):
    with pytest.raises(SystemExit):
        cli.parse_args(argv)


@pytest.mark.asyncio
async def test_commit_checks_privilege_collects_and_commits_per_request(
    session, monkeypatch, tmp_path
):
    calls = []

    async def privilege(sess):
        calls.append("privilege")

    class _Writer:
        def __init__(self, sess):
            assert sess is session

        async def write(self, rows):
            return WriteResult(len(rows), len(rows), 0, 0)

    class _Client:
        closed = False

        async def collect_minute_candles(self, symbol, **kwargs):
            calls.append((symbol, kwargs["adjusted"], kwargs["pace"] is not None))
            return []

        async def aclose(self):
            _Client.closed = True

    async def no_coverage(sess, symbol, days):
        return {}

    monkeypatch.setattr(cli, "assert_insert_privilege", privilege)
    monkeypatch.setattr(cli, "SqlCandleWriter", _Writer)
    monkeypatch.setattr(cli, "regular_bar_counts", no_coverage)
    out = io.StringIO()
    gap_log = tmp_path / "gaps.jsonl"
    args = cli.parse_args(
        [
            "--from-date",
            "2026-08-21",
            "--to-date",
            "2026-08-21",
            "--commit",
            "--gap-log",
            str(gap_log),
        ]
    )
    code = await cli.run(
        args,
        session_factory=_factory(session),
        client_factory=_Client,
        out=out,
        now=lambda: NOW,
    )
    assert code == 0
    assert calls == ["privilege", ("005930", False, True), ("375500", False, True)]
    assert session.commits == 2
    assert _Client.closed
    # both requests returned no bars -> four recorded gaps, none silent
    gaps = [json.loads(line) for line in gap_log.read_text().splitlines()]
    assert sorted((g["symbol"], g["session_date_kst"]) for g in gaps) == [
        ("005930", "2026-08-21"),
        ("005930", "2026-08-24"),
        ("375500", "2026-08-21"),
        ("375500", "2026-08-24"),
    ]
    final = json.loads(out.getvalue().splitlines()[-1])
    assert final["status_counts"] == {"gap": 2}


@pytest.mark.asyncio
async def test_commit_refuses_before_any_toss_call_without_privilege(
    session, monkeypatch, tmp_path
):
    async def privilege(sess):
        raise PermissionError("no insert")

    monkeypatch.setattr(cli, "assert_insert_privilege", privilege)
    args = cli.parse_args(
        [
            "--from-date",
            "2026-08-21",
            "--to-date",
            "2026-08-21",
            "--commit",
            "--gap-log",
            str(tmp_path / "g.jsonl"),
        ]
    )
    with pytest.raises(PermissionError):
        await cli.run(
            args,
            session_factory=_factory(session),
            client_factory=_no_client,
            out=io.StringIO(),
            now=lambda: NOW,
        )


def test_effective_tps_never_exceeds_the_chart_group_limit():
    from app.services.brokers.toss.rate_limiter import _BASE_LIMITS, TossApiGroup

    cap = _BASE_LIMITS[TossApiGroup.MARKET_DATA_CHART]
    assert cli.effective_max_tps(1000.0) == cap
    assert cli.effective_max_tps(2.0) == 2.0


@pytest.mark.asyncio
async def test_pacer_spaces_pages():
    clock = [0.0]
    slept = []

    async def sleep(seconds):
        slept.append(seconds)
        clock[0] += seconds

    pacer = cli.Pacer(2.0, clock=lambda: clock[0], sleep=sleep)
    await pacer()
    await pacer()
    clock[0] += 0.1
    await pacer()
    assert slept == [0.5, pytest.approx(0.4)]


def test_nothing_registers_a_schedule():
    source = (REPO / "scripts" / "backfill_kr_candles_1m_toss.py").read_text().lower()
    for forbidden in ("taskiq", "prefect", "@broker", "crontab", "schedule="):
        assert forbidden not in source
    # the collector modules are not imported by any task/flow module
    for base in ("app/tasks", "app/flows", "app/jobs"):
        root = REPO / base
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            text = path.read_text()
            assert "toss_minute_collector" not in text, path
            assert "dart_minute_trigger" not in text, path


def test_date_is_a_real_date():
    assert cli.parse_args(
        ["--from-date", "2026-07-22", "--to-date", "2026-09-30"]
    ).from_date == date(2026, 7, 22)


@pytest.mark.asyncio
async def test_commit_skips_already_collected_unless_refetch(
    session, monkeypatch, tmp_path
):
    fetched = []

    async def privilege(sess):
        return None

    async def coverage(sess, symbol, days):
        # 005930 fully stored; 375500 only D0 stored
        if symbol == "005930":
            return dict.fromkeys(days, 5)
        return {days[0]: 5}

    class _Client:
        async def collect_minute_candles(self, symbol, **kwargs):
            fetched.append(symbol)
            return []

    monkeypatch.setattr(cli, "assert_insert_privilege", privilege)
    monkeypatch.setattr(cli, "regular_bar_counts", coverage)
    base = [
        "--from-date", "2026-08-21", "--to-date", "2026-08-21",
        "--commit", "--gap-log", str(tmp_path / "g.jsonl"),
    ]  # fmt: skip
    out = io.StringIO()
    await cli.run(
        cli.parse_args(base),
        session_factory=_factory(session),
        client_factory=_Client,
        out=out,
        now=lambda: NOW,
    )
    assert fetched == ["375500"]
    assert json.loads(out.getvalue().splitlines()[-1])["status_counts"] == {
        "already_collected": 1,
        "gap": 1,
    }
    fetched.clear()
    await cli.run(
        cli.parse_args([*base, "--refetch"]),
        session_factory=_factory(session),
        client_factory=_Client,
        out=io.StringIO(),
        now=lambda: NOW,
    )
    assert fetched == ["005930", "375500"]

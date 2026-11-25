"""Run orchestration and the manual CLI: dry run by default (hk 6036)."""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal

import pytest

from app.services.upbit_intraday_shadow import contracts as K
from app.services.upbit_intraday_shadow import writer
from app.services.upbit_intraday_shadow.recorder import run_once
from scripts import upbit_intraday_shadow as cli
from tests.services.upbit_intraday_shadow import support as S

pytestmark = pytest.mark.unit

NOW = S.run_after(400)


def rows(count: int = 401) -> list:
    out = [S.bar(i) for i in range(count)]
    out[400] = S.bar(400, h="106", c="105", q="200000000")
    return out


class FakeSession:
    def __init__(self) -> None:
        self.log: list[str] = []

    async def commit(self) -> None:
        self.log.append("commit")

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *exc) -> None:
        return None


def fake_writer(monkeypatch, session: FakeSession, pending=()):
    seen: dict[str, list] = {}

    async def insert_candidates(_s, candidates, *, run_id):
        session.log.append("insert_candidates")
        seen["candidates"] = list(candidates)
        return len(candidates)

    async def load_pending(_s, markets=None):
        session.log.append("load_pending")
        return list(pending)

    async def insert_virtual_trades(_s, trades, *, run_id):
        session.log.append("insert_trades")
        seen["trades"] = list(trades)
        return len(trades)

    for function in (insert_candidates, load_pending, insert_virtual_trades):
        monkeypatch.setattr(writer, function.__name__, function)
    return seen


def forbid_database(monkeypatch) -> None:
    import app.core.db

    def refuse(*_a, **_k):
        raise AssertionError("dry run touched the database")

    monkeypatch.setattr(app.core.db, "AsyncSessionLocal", refuse)
    for name in ("insert_candidates", "load_pending", "insert_virtual_trades"):
        monkeypatch.setattr(writer, name, refuse)


def old_pending(market: str, index: int) -> K.PendingCandidate:
    return K.PendingCandidate(
        11, market, "A_breakout:N16_X3", 1, "signal", S.at(index), Decimal("0.005")
    )


def run(fake: S.FakeUpbit, **kwargs):
    return asyncio.run(run_once(S.data_at(fake, NOW), run_id="t", now=NOW, **kwargs))


def test_commit_writes_candidates_before_resolving(monkeypatch) -> None:
    session = FakeSession()
    seen = fake_writer(monkeypatch, session)
    report = run(S.FakeUpbit({"KRW-AAA": rows()}), session=session, catchup_bars=1)
    assert session.log == [
        "insert_candidates",
        "commit",
        "load_pending",
        "commit",
        "insert_trades",
        "commit",
    ]
    assert report["mode"] == "commit"
    assert report["inserted_candidates"] == 4 == len(seen["candidates"])
    assert "rows" not in report


def test_an_old_pending_candidate_is_resolved_from_an_older_page(monkeypatch) -> None:
    session = FakeSession()
    # Signal 300 bars ago: outside the 200-row page of this run.
    seen = fake_writer(monkeypatch, session, [old_pending("KRW-AAA", 100)])
    fake = S.FakeUpbit({"KRW-AAA": rows()})
    report = run(fake, session=session, catchup_bars=1)
    assert report["errors"] == []
    assert [(t.horizon_bars, t.missing_reason) for t in seen["trades"]] == [
        (4, None),
        (16, None),
        (96, None),
    ]
    assert len([u for u in fake.urls if "market=KRW-AAA" in u]) == 2


def test_a_delisted_market_ends_as_market_unavailable(monkeypatch) -> None:
    session = FakeSession()
    seen = fake_writer(monkeypatch, session, [old_pending("KRW-GONE", 100)])
    report = run(S.FakeUpbit({"KRW-AAA": rows()}), session=session, catchup_bars=1)
    assert report["errors"] == []
    assert {t.missing_reason for t in seen["trades"]} == {"market_unavailable"}


def test_a_delisted_market_is_asked_once(monkeypatch) -> None:
    session = FakeSession()
    seen = fake_writer(
        monkeypatch,
        session,
        [old_pending("KRW-GONE", 100), old_pending("KRW-GONE", 120)],
    )
    fake = S.FakeUpbit({"KRW-AAA": rows()})
    run(fake, session=session, catchup_bars=1)
    assert len(seen["trades"]) == 6
    assert len([u for u in fake.urls if "market=KRW-GONE" in u]) == 1


def test_a_failed_older_page_does_not_hold_back_newer_candidates(monkeypatch) -> None:
    session = FakeSession()
    seen = fake_writer(
        monkeypatch,
        session,
        [old_pending("KRW-AAA", 100), old_pending("KRW-AAA", 390)],
    )
    fake = S.FakeUpbit({"KRW-AAA": rows()})
    calls: list[str] = []

    def older_page_fails(url: str):
        if "market=KRW-AAA" in url:
            calls.append(url)
            if len(calls) == 2:  # the first call is this run's own page
                raise OSError("older page unreachable")
        return fake(url)

    report = asyncio.run(
        run_once(
            S.data_at(older_page_fails, NOW),
            run_id="t",
            now=NOW,
            session=session,
            catchup_bars=1,
        )
    )
    assert [(e["market"], e["stage"]) for e in report["errors"]] == [
        ("KRW-AAA", "outcome")
    ]
    assert [(t.entry_bar_open_at, t.horizon_bars) for t in seen["trades"]] == [
        (S.at(391), 4)
    ]


@pytest.mark.parametrize("status", [404, 500])
def test_a_listed_market_that_fails_stays_pending(monkeypatch, status) -> None:
    session = FakeSession()
    seen = fake_writer(monkeypatch, session, [old_pending("KRW-BAD", 100)])
    fake = S.FakeUpbit(
        {"KRW-AAA": rows()},
        {"KRW-AAA": S.QUIET, "KRW-BAD": S.QUIET},
        statuses={"KRW-BAD": status},
    )
    report = run(fake, session=session, catchup_bars=1)
    assert seen["trades"] == []
    assert {(e["market"], e["stage"]) for e in report["errors"]} == {
        ("KRW-BAD", "signal"),
        ("KRW-BAD", "outcome"),
    }
    # The other market still produced its rows.
    assert len(seen["candidates"]) == 4


def cli_run(monkeypatch, capsys, fake: S.FakeUpbit, argv: list[str]):
    monkeypatch.setattr(cli, "setup_logging_and_sentry", lambda **_k: None)
    monkeypatch.setattr(cli, "UpbitPublicData", lambda: S.data_at(fake, NOW))
    monkeypatch.setattr(
        "app.services.upbit_intraday_shadow.recorder.utc_now", lambda: NOW
    )
    code = cli.main(argv)
    return code, json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_the_cli_is_a_dry_run_by_default(monkeypatch, capsys) -> None:
    forbid_database(monkeypatch)
    code, report = cli_run(
        monkeypatch, capsys, S.FakeUpbit({"KRW-AAA": rows()}), ["--catchup-bars", "1"]
    )
    assert code == 0
    assert report["mode"] == "dry_run"
    assert len(report["rows"]) == 4
    assert "inserted_candidates" not in report


def test_the_cli_writes_only_with_commit(monkeypatch, capsys) -> None:
    import app.core.db

    session = FakeSession()
    fake_writer(monkeypatch, session)
    monkeypatch.setattr(app.core.db, "AsyncSessionLocal", lambda: session)
    code, report = cli_run(
        monkeypatch, capsys, S.FakeUpbit({"KRW-AAA": rows()}), ["--commit"]
    )
    assert code == 0
    assert report["mode"] == "commit"
    assert "insert_candidates" in session.log


def test_the_cli_exits_one_when_a_market_fails(monkeypatch, capsys) -> None:
    forbid_database(monkeypatch)
    code, report = cli_run(
        monkeypatch,
        capsys,
        S.FakeUpbit({"KRW-AAA": rows()}),
        ["--markets", "KRW-AAA,KRW-NOPE"],
    )
    assert code == 1
    assert report["errors"][0]["market"] == "KRW-NOPE"


def test_the_cli_refuses_changed_rules(monkeypatch, capsys) -> None:
    forbid_database(monkeypatch)
    monkeypatch.setattr(K, "FROZEN_RULES_SHA256", "0" * 64)
    fake = S.FakeUpbit({"KRW-AAA": rows()})
    code, report = cli_run(monkeypatch, capsys, fake, [])
    assert code == 2
    assert report["status"] == "refused"
    assert fake.urls == []


@pytest.mark.parametrize(
    "argv", [["--catchup-bars", "97"], ["--markets", "BTC"], ["--catchup-bars", "0"]]
)
def test_the_cli_rejects_bad_arguments(argv) -> None:
    with pytest.raises(SystemExit) as caught:
        cli.main(argv)
    assert caught.value.code == 2

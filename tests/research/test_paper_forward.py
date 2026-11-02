"""ROB-1473: observable shadow decisions, accounting, persistence and missing data."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import pandas as pd
import pytest

from research.paper_forward import engine, inputs, store
from research.paper_forward.engine import Bar, Signal
from research.strategy_evidence.averaging import average_down_overlay
from research.strategy_evidence.common import ROUND_TRIP_COST
from research.strategy_evidence.rulesets import VARIANTS, simulate
from research.trade_retro.canonical import next_open_session
from research.trade_retro.prices import Bars
from scripts.paper_forward.run import main, run

pytestmark = pytest.mark.unit
FIRST = date(2026, 8, 3)


def fixture(market="crypto", closes=("100", "79", "50", "30"), *, control="90"):
    days = inputs.calendar(market, FIRST, FIRST + timedelta(days=150))
    bars = {
        ("AAA", d): Bar(d, D("100"), D(closes[min(i, len(closes) - 1)]))
        for i, d in enumerate(days)
    }
    stamp = datetime(2026, 8, 2, 12, tzinfo=UTC)
    signal = Signal("signal-1", "AAA", stamp, D("100"), D(control) if control else None)
    return days, bars, [signal]


def advance(market, days, bars, signals, n, previous=None):
    return engine.advance(
        market, days[n], previous, days, bars, signals, D(str(ROUND_TRIP_COST[market]))
    )


@pytest.mark.parametrize(
    "closes",
    [
        ("100", "79", "50", "30"),  # both adds at 79; stop only on the later bar
        ("100", "90", "80", "99"),  # harmonic average-cost target
        ("100", "95", "94", "93"),  # no adds/stop
        ("100", "110", "110", "110"),  # legacy float +10% target boundary
        (
            "30",
            "20",
            "20",
            "20",
        ),  # huge first-bar drop: add both before evaluating stop
    ],
)
def test_b3_replay_and_forward_have_identical_fills_and_exit(closes):
    days, bars, signals = fixture(closes=closes)
    research_bars = Bars.from_rows(
        [(d, 100.0, float(c)) for d, c in zip(days[: len(closes)], closes, strict=True)]
    )
    b3 = next(v for v in VARIANTS if v.name == "B3")
    units, exit_i, exit_price, reason = simulate(research_bars, 0, b3)
    previous = None
    all_actions = []
    for i in range(len(closes)):
        previous = advance("crypto", days, bars, signals, i, previous)
        all_actions.extend(
            a for a in previous["ledger_diff"] if a["arm"] == "candidate"
        )
    buys = [a for a in all_actions if a["side"] == "buy"]
    assert [(a["day"], float(a["price"])) for a in buys] == [
        (str(days[u.i]), u.price) for u in units
    ]
    exits = [a for a in all_actions if a["side"] == "sell"]
    if reason == "open at end":
        assert exits == []
    else:
        assert [(a["day"], float(a["price"]), a["reason"]) for a in exits] == [
            (str(days[exit_i]), exit_price, reason)
        ]


def test_double_add_does_not_mistake_entry_drawdown_for_average_cost_stop():
    days, bars, signals = fixture(closes=("30", "20"))
    first = advance("crypto", days, bars, signals, 0)
    fills = first["episodes"][0]["candidate"]["fills"]
    assert [f["reason"] for f in fills] == ["entry", "add", "add"]
    assert [f["price"] for f in fills] == ["100", "30", "30"]
    second = advance("crypto", days, bars, signals, 1, first)
    assert second["ledger_diff"][-1]["reason"] == "loss-cut"


def test_us_forward_matches_research_first_touch_and_fractional_add():
    days, bars, signals = fixture("us", ("100", "95", "92", "80"))
    rows = [
        {
            "alias": "a",
            "market": "us",
            "symbol": "AAA",
            "episode": 1,
            "known": True,
            "qty": 1.0,
            "avg": 100.0,
            "i": i,
            "day": d,
            "close": float(bars["AAA", d].close),
            "unrealized": float(bars["AAA", d].close / 100 - 1),
        }
        for i, d in enumerate(days[:4])
    ]
    overlay = average_down_overlay(pd.DataFrame(rows), 0.08)
    previous = None
    actions = []
    for i in range(4):
        previous = advance("us", days, bars, signals, i, previous)
        actions.extend(a for a in previous["ledger_diff"] if a["reason"] == "add")
    assert [(a["day"], float(a["qty"]), float(a["price"])) for a in actions] == [
        (str(r.day), r.qty, r.price) for r in overlay.itertuples()
    ]
    assert D(previous["episodes"][0]["candidate"]["qty"]) == D("1.5")
    assert all(a["side"] != "sell" for a in previous["ledger_diff"])


@pytest.mark.parametrize("market,h,E", [("crypto", 60, "300"), ("us", 20, "150.0")])
def test_control_cash_and_fixed_horizon_accounting(market, h, E):
    days, bars, signals = fixture(market, ("100", "100"))
    first = advance(market, days, bars, signals, 0)
    bars["AAA", days[h]] = Bar(days[h], D(100), D(110))
    last = advance(market, days, bars, signals, h, first)
    [episode] = last["episodes"]
    assert D(episode["E"]) == D(E)
    assert episode["h_day"] == str(days[h])
    result = episode["h_result"]
    candidate = D(result["candidate_pnl"])
    control = D(result["control_pnl"])
    assert D(result["D_i"]) == (candidate - control) / D(E)
    if market == "crypto":
        assert control == D("9.9000")  # 1u buy-and-hold, 2u unspent cash
        assert episode["control"]["qty"] == "0"
    else:
        assert control == 0  # frozen DAY limit 90 never touched; cash, no adds
        assert candidate == D("9.1000")


def test_us_control_fills_at_frozen_limit_once_and_splits_preserve_value():
    days, bars, signals = fixture("us", ("95", "90", "45"), control="96")
    first = advance("us", days, bars, signals, 0)
    [fill] = first["episodes"][0]["control"]["fills"]
    assert fill["price"] == "96"
    second = advance("us", days, bars, signals, 1, first)
    bars["AAA", days[2]] = Bar(days[2], D(45), D(45), D(2))
    third = advance("us", days, bars, signals, 2, second)
    episode = third["episodes"][0]
    assert episode["entry_price"] == "50"
    assert D(episode["candidate"]["qty"]) == D("3")  # first add at 90, then 2:1 split
    assert len(episode["control"]["fills"]) == 1
    assert not any(a.get("reason") == "add" for a in third["ledger_diff"])


def test_missing_bar_does_not_shift_H_or_manufacture_zero_comparison():
    days, bars, signals = fixture("us", ("100",))
    first = advance("us", days, bars, signals, 0)
    del bars["AAA", days[10]]
    last = advance("us", days, bars, signals, 20, first)
    [row] = last["episode_report"]
    assert row["state"] == row["comparison_state"] == "unverified"
    assert row["pnl"] is None
    assert row["h_result"] is None
    assert row["h_day"] == str(days[20])
    assert "unverified" in store.markdown({**last, "mode": "shadow", "data_gaps": []})


def test_missing_control_limit_is_not_zero_pnl():
    days, bars, signals = fixture("us", ("100",), control=None)
    first = advance("us", days, bars, signals, 0)
    last = advance("us", days, bars, signals, 20, first)
    result = last["episodes"][0]["h_result"]
    assert result["state"] == "unverified"
    assert result["control_pnl"] is None and result["D_i"] is None


def test_a_closed_candidate_can_start_an_episode_despite_a_control_data_gap():
    days, bars, signals = fixture("crypto", ("100", "111", "100", "100"))
    first = advance("crypto", days, bars, signals, 0)
    closed = advance("crypto", days, bars, signals, 1, first)
    assert closed["episodes"][0]["candidate"]["qty"] == "0"
    del bars["AAA", days[2]]
    gap = advance("crypto", days, bars, signals, 2, closed)
    later = Signal(
        "signal-2", "AAA", datetime.combine(days[2], datetime.min.time(), UTC), D(100)
    )
    result = advance("crypto", days, bars, signals + [later], 3, gap)
    assert result["summary"]["opened"] == 1
    assert len(result["episodes"]) == 2
    assert result["episodes"][0]["unverified"]


def test_missing_signal_feed_is_unverified(tmp_path):
    assert inputs.signal_feed(tmp_path / "absent.json", "us", ["AAA"], FIRST) == (
        [],
        "unverified:missing_signal_feed",
    )


@pytest.mark.parametrize(
    "covers_from,state",
    [
        (None, "unverified:missing_signal_history"),
        ("2026-08-06", "unverified:incomplete_signal_history"),
        ("2026-08-05", "verified"),
    ],
)
def test_feed_requires_coverage_from_first_unledgered_session(
    tmp_path, covers_from, state
):
    path = tmp_path / "signals.json"
    feed = {
        "schema": "paper-forward-signals/v1",
        "market": "crypto",
        "complete_through": "2026-08-06T00:00:00+00:00",
        "signals": [],
    }
    if covers_from is not None:
        feed["covers_from"] = covers_from
    path.write_text(json.dumps(feed))
    assert inputs.signal_feed(path, "crypto", ["AAA"], date(2026, 8, 5)) == ([], state)


@pytest.mark.parametrize(
    "state",
    [
        "unverified:missing_signal_feed",
        "unverified:stale_signal_feed",
        "unverified:missing_bars",
    ],
)
def test_unverified_input_defers_new_episodes_without_consuming_keys(state):
    days, bars, signals = fixture()
    report = engine.advance(
        "crypto", days[0], None, days, bars, signals, D("0.001"), state
    )
    assert report["seen"] == []
    assert report["episodes"] == []
    assert report["signals"][0]["state"] == "deferred_unverified"


def test_a_missing_entry_bar_does_not_claim_a_held_position_forever():
    days, bars, signals = fixture(closes=("100",))
    del bars["AAA", days[0]]
    first = advance("crypto", days, bars, signals, 0)
    assert first["episodes"][0]["candidate"]["fills"] == []
    later = Signal(
        "signal-2", "AAA", datetime.combine(days[0], datetime.min.time(), UTC), D(100)
    )
    report = advance("crypto", days, bars, signals + [later], 1, first)
    assert report["summary"]["opened"] == 1
    assert len(report["episodes"]) == 2
    assert report["episode_report"][0]["state"] == "unverified"


def cli_fixture(tmp_path, monkeypatch):
    days, bars, signals = fixture(closes=("100",))
    bars.update({("BBB", d): Bar(d, D(100), D(100)) for d in days})
    universe = tmp_path / "universe.json"
    universe.write_text(
        json.dumps(
            {
                "schema": "trade-retro-prices/v1",
                "entries": {"crypto/AAA": {}, "crypto/BBB": {}},
                "missing": {},
            }
        )
    )
    feed_path = tmp_path / "signals.json"
    monkeypatch.setattr(inputs, "public_bars", lambda *a: (bars, {}))
    args = argparse.Namespace(
        market="crypto",
        asof=str(days[0]),
        universe=str(universe),
        signals=str(feed_path),
        ledger_dir=str(tmp_path / "output"),
        dry_run=False,
    )
    return days, signals, feed_path, args


def write_feed(path, covers_from, through_day, signals):
    path.write_text(
        json.dumps(
            {
                "schema": "paper-forward-signals/v1",
                "market": "crypto",
                "covers_from": str(covers_from),
                "complete_through": inputs.close_time(
                    "crypto", through_day
                ).isoformat(),
                "signals": [
                    {
                        "key": s.key,
                        "symbol": s.symbol,
                        "at": s.at.isoformat(),
                        "u": str(s.unit),
                    }
                    for s in signals
                ],
            }
        )
    )


def test_incomplete_catchup_is_retryable_with_complete_feed_history(
    tmp_path, monkeypatch
):
    days, signals, path, args = cli_fixture(tmp_path, monkeypatch)
    write_feed(path, days[0], days[0], signals)
    first = run(args)
    second = Signal(
        "signal-2", "BBB", datetime.combine(days[0], datetime.min.time(), UTC), D(100)
    )
    args.asof = str(days[2])
    write_feed(path, days[2], days[2], signals + [second])
    incomplete = run(args)
    assert incomplete["input_state"] == "unverified:incomplete_signal_history"
    assert incomplete["committed"] is False
    assert incomplete["seen"] == first["seen"]
    assert store.latest(Path(args.ledger_dir), "crypto")[0]["asof"] == str(days[0])
    assert list((Path(args.ledger_dir) / "unverified/crypto").glob("*.json"))
    write_feed(path, days[1], days[2], signals + [second])
    complete = run(args)
    assert complete["committed"] is True
    assert complete["seen"] == ["signal-1", "signal-2"]
    assert complete["episodes"][1]["entry_day"] == str(days[1])


def test_first_unverified_attempt_pins_start_for_a_later_retry(tmp_path, monkeypatch):
    days, signals, path, args = cli_fixture(tmp_path, monkeypatch)
    incomplete = run(args)
    assert store.latest(Path(args.ledger_dir), "crypto") == (None, None)
    assert incomplete["committed"] is False
    args.asof = str(days[2])
    write_feed(path, days[0], days[2], signals)
    complete = run(args)
    assert complete["episodes"][0]["entry_day"] == str(days[0])
    assert complete["committed"] is True


@pytest.mark.parametrize(
    "ratio,close,quantity", [("2", "46", "3"), ("0.5", "184", "0.75")]
)
def test_a_first_us_add_after_a_split_stays_inside_fixed_E(ratio, close, quantity):
    days, bars, signals = fixture("us", ("100",))
    first = advance("us", days, bars, signals, 0)
    bars["AAA", days[1]] = Bar(days[1], D(close), D(close), D(ratio))
    result = advance("us", days, bars, signals, 1, first)
    candidate = result["episodes"][0]["candidate"]
    assert D(candidate["qty"]) == D(quantity)
    assert D(candidate["invested"]) == D(146)
    assert D(result["episodes"][0]["E"]) == D(150)


@pytest.mark.parametrize(
    "entry,closes,expected_buys,expected_exit",
    [
        (100.0, [100.0, 110.0], [100.0], (1, 110.0, "open at end")),
        (
            121.0,
            [121.0, 108.9, 96.8, 118.8],
            [121.0, 108.9, 96.8],
            (3, 118.8, "open at end"),
        ),
        (
            121.0,
            [121.0, 108.9, 96.8, 70.2],
            [121.0, 108.9, 96.8],
            (3, 70.2, "loss-cut"),
        ),
        (0.1, [0.1, 0.09, 0.08], [0.1, 0.09, 0.08], (2, 0.08, "open at end")),
        (1.63, [1.63, 1.467], [1.63], (1, 1.467, "open at end")),
        (0.29, [0.29, 0.232], [0.29, 0.232], (1, 0.232, "open at end")),
        (
            0.01,
            [0.01, 0.009, 0.008, 0.0058016528925619835],
            [0.01, 0.009, 0.008],
            (3, 0.0058016528925619835, "loss-cut"),
        ),
    ],
)
def test_b3_exact_boundaries_preserve_pre_refactor_replay(
    entry, closes, expected_buys, expected_exit
):
    # Literal outcomes from simulate() at c86d3c4e2, before shared-rule extraction.
    days, bars, signals = fixture(closes=tuple(str(c) for c in closes))
    bars.update(
        {("AAA", day): Bar(day, D(str(entry)), bars["AAA", day].close) for day in days}
    )
    replay_bars = Bars.from_rows(
        [(day, entry, close) for day, close in zip(days, closes, strict=False)]
    )
    units, i, price, reason = simulate(
        replay_bars, 0, next(v for v in VARIANTS if v.name == "B3")
    )
    assert [u.price for u in units] == expected_buys
    assert (i, price, reason) == expected_exit
    previous, actions = None, []
    for n in range(len(closes)):
        previous = advance("crypto", days, bars, signals, n, previous)
        actions.extend(a for a in previous["ledger_diff"] if a["arm"] == "candidate")
    assert [float(a["price"]) for a in actions if a["side"] == "buy"] == expected_buys
    exits = [
        (a["day"], float(a["price"]), a["reason"])
        for a in actions
        if a["side"] == "sell"
    ]
    assert exits == ([] if reason == "open at end" else [(str(days[i]), price, reason)])


def test_dagu_proposal_passes_required_universe_and_directory_explicitly():
    import yaml

    proposal = yaml.safe_load(
        (
            Path(__file__).resolve().parents[2] / "deploy/dagu/paper-forward.yaml"
        ).read_text()
    )
    for step in proposal["steps"]:
        assert "--universe ${PAPER_FORWARD_UNIVERSE}" in step["command"]
        assert "--ledger-dir ${PAPER_FORWARD_DIR}" in step["command"]


def test_repeated_asof_changes_no_ledger_or_report_bytes(tmp_path, monkeypatch):
    days, bars, signals = fixture()
    universe = tmp_path / "universe.json"
    universe.write_text(
        json.dumps(
            {
                "schema": "trade-retro-prices/v1",
                "entries": {"crypto/AAA": {}},
                "missing": {},
            }
        )
    )
    monkeypatch.setattr(inputs, "signal_feed", lambda *a: (signals, "verified"))
    monkeypatch.setattr(inputs, "public_bars", lambda *a: (bars, {}))
    args = argparse.Namespace(
        market="crypto",
        asof=str(days[0]),
        universe=str(universe),
        signals=None,
        ledger_dir=str(tmp_path / "output"),
        dry_run=True,
    )
    preview = run(args)
    assert preview["summary"]["opened"] == 1
    assert not (tmp_path / "output/ledger").exists()
    args.dry_run = False
    first = run(args)
    files = {p: p.read_bytes() for p in Path(args.ledger_dir).rglob("*") if p.is_file()}
    monkeypatch.setattr(
        inputs, "public_bars", lambda *a: pytest.fail("rerun must not fetch")
    )
    assert run(args) == first
    assert {p: p.read_bytes() for p in files} == files
    store.latest(Path(args.ledger_dir), "crypto")


def test_calendar_uses_real_sessions_and_completed_close():
    assert date(2026, 11, 26) not in inputs.calendar(
        "us", date(2026, 11, 25), date(2026, 11, 27)
    )
    assert inputs.close_time("us", date(2026, 11, 27)).hour == 18  # early close, EST
    assert inputs.last_completed(
        "crypto", datetime(2026, 11, 2, 0, 1, tzinfo=UTC)
    ) == date(2026, 11, 1)


@pytest.mark.parametrize(
    "market,at",
    [
        ("crypto", "2026-08-02T23:59:00+00:00"),
        ("us", "2026-08-03T13:29:59+00:00"),
        ("us", "2026-08-03T13:30:00+00:00"),
        ("us", "2026-11-02T14:30:00+00:00"),
    ],
)
def test_entry_date_is_shared_with_research(market, at):
    stamp = datetime.fromisoformat(at)
    days = inputs.calendar(
        market, stamp.date() - timedelta(days=1), stamp.date() + timedelta(days=7)
    )
    bars = Bars.from_rows([(d, 100.0, 100.0) for d in days])
    i = next_open_session(bars, market, stamp)
    assert i is not None
    assert engine.entry_day(market, stamp, days) == days[i]


def test_forward_calendar_can_extend_beyond_library_default_year():
    assert inputs.calendar("us", date(2027, 11, 4), date(2027, 11, 5)) == [
        date(2027, 11, 4),
        date(2027, 11, 5),
    ]


def test_shadow_modules_have_no_broker_or_database_imports():
    import ast

    repo = Path(__file__).resolve().parents[2]
    paths = list((repo / "research/paper_forward").glob("*.py")) + list(
        (repo / "scripts/paper_forward").glob("*.py")
    )
    for path in paths:
        for node in ast.walk(ast.parse(path.read_text())):
            names = (
                [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else []
            )
            assert all(
                not name.startswith(
                    ("app.services", "app.models", "sqlalchemy", "asyncpg", "psycopg")
                )
                for name in names
            ), path


def test_verified_dry_run_can_preview_corrected_inputs_without_overwriting(
    tmp_path, monkeypatch
):
    days, signals, path, args = cli_fixture(tmp_path, monkeypatch)
    args.dry_run = True
    write_feed(path, days[0], days[0], signals)
    first = run(args)
    files = {
        p: p.read_bytes()
        for p in (Path(args.ledger_dir) / "preview").rglob("*")
        if p.is_file()
    }
    corrected = Signal("corrected", "BBB", signals[0].at, D(200))
    write_feed(path, days[0], days[0], [corrected])
    second = run(args)
    assert first["seen"] == ["signal-1"]
    assert second["seen"] == ["corrected"]
    assert {p: p.read_bytes() for p in files} == files
    previews = list((Path(args.ledger_dir) / "preview/crypto").glob("*.json"))
    assert len(previews) == 2
    assert run(args) == second
    assert list((Path(args.ledger_dir) / "preview/crypto").glob("*.json")) == previews
    assert store.latest(Path(args.ledger_dir), "crypto") == (None, None)


def test_signal_selection_uses_resolved_entry_session_after_long_closure(
    tmp_path, monkeypatch
):
    days, signals, path, args = cli_fixture(tmp_path, monkeypatch)
    args.market = "us"
    universe = Path(args.universe)
    universe.write_text(universe.read_text().replace("crypto/", "us/"))
    # A fixture calendar with a closure longer than the CLI's old 14-day padding.
    sessions = [days[0] + timedelta(days=i) for i in range(60)]
    monkeypatch.setattr(
        inputs,
        "calendar",
        lambda market, start, end: [d for d in sessions if start <= d <= end],
    )
    old_signal = Signal(
        "before-closure", "AAA", signals[0].at - timedelta(days=20), D(100), D(90)
    )
    write_feed(path, days[0], days[0], [old_signal])
    path.write_text(path.read_text().replace('"market": "crypto"', '"market": "us"'))
    report = run(args)
    assert report["input_state"] == "verified"
    assert report["seen"] == ["before-closure"]
    assert report["episodes"][0]["entry_day"] == str(days[0])


@pytest.mark.parametrize("dry_run", [False, True])
def test_cli_unverified_run_signals_noncommit_and_can_recover(
    tmp_path, monkeypatch, capsys, dry_run
):
    days, signals, path, args = cli_fixture(tmp_path, monkeypatch)
    argv = [
        "--market",
        args.market,
        "--asof",
        args.asof,
        "--universe",
        args.universe,
        "--signals",
        args.signals,
        "--ledger-dir",
        args.ledger_dir,
    ]
    if dry_run:
        argv.append("--dry-run")
    assert main(argv) == (0 if dry_run else 3)
    report = json.loads(capsys.readouterr().out)
    assert report["committed"] is False
    assert report["input_state"] == "unverified:missing_signal_feed"
    assert store.latest(Path(args.ledger_dir), "crypto") == (None, None)
    write_feed(path, days[0], days[0], signals)
    assert main(argv) == 0
    assert json.loads(capsys.readouterr().out)["committed"] is (not dry_run)

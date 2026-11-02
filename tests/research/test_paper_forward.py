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
from scripts.paper_forward.run import run

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
        ("100", "110", "110", "110"),  # exact inclusive +10% target
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
    days, bars, signals = fixture("crypto", ("100", "110", "100", "100"))
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

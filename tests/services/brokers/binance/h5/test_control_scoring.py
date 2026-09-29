from __future__ import annotations

import datetime as dt
import json
from dataclasses import asdict
from decimal import Decimal
from unittest.mock import AsyncMock

from app.services.brokers.binance.h5.control import (
    ActualTrip,
    ControlTrip,
    Opportunity,
    build_control_ledger,
)
from app.services.brokers.binance.h5.scoring import NavSample, score_h5
from scripts import binance_h5_weekly_score

_BAR = 4 * 60 * 60 * 1000


def _actual(i: int, *, gross: str = "1", fee: str = "0") -> ActualTrip:
    start = dt.datetime(2026, 1, 5, tzinfo=dt.UTC)
    opened = start + dt.timedelta(hours=4 * i)
    return ActualTrip(
        signal_key=f"signal-{i}",
        symbol="BTCUSDT",
        side="BUY",
        decision_ts=int(opened.timestamp() * 1000),
        entry_notional_usdt=Decimal("200"),
        opened_at=opened,
        closed_at=opened + dt.timedelta(hours=24),
        gross_pnl_usdt=Decimal(gross),
        fees_usdt=Decimal(fee),
    )


def test_fixed_seed_control_uses_predeclared_grid_and_no_client(monkeypatch) -> None:
    from app.services.brokers.binance.h5.client import H5DemoClient

    submit = AsyncMock(side_effect=AssertionError("control must never dispatch"))
    monkeypatch.setattr(H5DemoClient, "submit_order", submit)
    actual = [_actual(0)]
    grid = [
        Opportunity(
            symbol="BTCUSDT",
            decision_ts=actual[0].decision_ts + i * _BAR,
            high=Decimal("102"),
            low=Decimal("98"),
            close=Decimal("100"),
            bid=Decimal("99.9"),
            ask=Decimal("100.1"),
        )
        for i in range(12)
    ]

    class NoControlClient:
        calls = 0

        def submit_order(self):
            self.calls += 1
            raise AssertionError("control must never submit")

    client = NoControlClient()
    first = build_control_ledger(actual, grid)
    second = build_control_ledger(actual, grid)
    assert first == second
    assert len(first) == len(actual)
    assert first[0].symbol == actual[0].symbol
    assert first[0].side == actual[0].side
    assert first[0].control_decision_ts != actual[0].decision_ts
    assert first[0].fees_usdt > 0
    assert client.calls == 0
    assert submit.await_count == 0


def test_offline_snapshot_script_exports_score_and_control_without_db(
    monkeypatch, tmp_path
):
    actual = _actual(0, gross="2", fee="0.1")
    snapshot = {
        "actual_trips": [asdict(actual)],
        "opportunities": [
            {
                "symbol": "BTCUSDT",
                "decision_ts": actual.decision_ts + i * _BAR,
                "high": "102",
                "low": "98",
                "close": "100",
                "bid": "99.9",
                "ask": "100.1",
            }
            for i in range(12)
        ],
        "nav_samples": [
            {"observed_at": actual.opened_at.isoformat(), "nav_usdt": "1000"}
        ],
    }
    source, score, control = (
        tmp_path / "snapshot.json",
        tmp_path / "score.json",
        tmp_path / "control.json",
    )
    source.write_text(
        json.dumps(
            snapshot,
            default=lambda value: (
                value.isoformat() if isinstance(value, dt.datetime) else str(value)
            ),
        )
    )
    db_read = AsyncMock(side_effect=AssertionError("snapshot must not open DB"))
    monkeypatch.setattr(binance_h5_weekly_score, "_read_snapshot_from_db", db_read)
    monkeypatch.setattr(
        "sys.argv",
        [
            "weekly-score",
            "--snapshot-json",
            str(source),
            "--t0",
            actual.opened_at.isoformat(),
            "--as-of",
            (actual.opened_at + dt.timedelta(days=56)).isoformat(),
            "--score-out",
            str(score),
            "--control-out",
            str(control),
        ],
    )
    binance_h5_weekly_score.main()
    assert json.loads(score.read_text())["label"] == "INSUFFICIENT_SAMPLE"
    computed = json.loads(control.read_text())
    assert computed["seed"] == 84720260928 and len(computed["trips"]) == 1
    assert computed["control_error"] is None
    assert db_read.await_count == 0


def _control(i: int, *, gross: str, fee: str = "0") -> ControlTrip:
    actual = _actual(i)
    return ControlTrip(
        signal_key=actual.signal_key,
        symbol=actual.symbol,
        side=actual.side,
        control_decision_ts=actual.decision_ts + _BAR,
        opened_at=actual.opened_at,
        closed_at=actual.closed_at,
        entry_price=Decimal("100"),
        qty=Decimal("2"),
        gross_pnl_usdt=Decimal(gross),
        fees_usdt=Decimal(fee),
        exit_reason="time_exit",
    )


def test_score_pass_and_scope_and_operating_cost() -> None:
    t0 = dt.datetime(2026, 1, 5, tzinfo=dt.UTC)
    actual = [_actual(i, gross="2" if i < 40 else "-1", fee="0.1") for i in range(60)]
    control = [_control(i, gross="1" if i < 30 else "-1", fee="0.1") for i in range(60)]
    nav = [
        NavSample(t0, Decimal("1000")),
        NavSample(t0 + dt.timedelta(days=28), Decimal("950")),
        NavSample(t0 + dt.timedelta(days=56), Decimal("1001")),
    ]
    card = score_h5(
        actual=actual,
        control=control,
        nav_samples=nav,
        t0=t0,
        as_of=t0 + dt.timedelta(days=56),
    )
    assert card.label == "PASS"
    assert card.actual_trip_count == 60 and card.control_trip_count == 60
    assert card.actual_pf > card.control_pf
    assert card.mdd == Decimal("0.05")
    assert "entry-signal value, not envelope value" in card.control_scope_note
    assert "below 10%" in card.operating_cost_note


def test_score_early_risk_and_insufficient_and_efficacy() -> None:
    t0 = dt.datetime(2026, 1, 5, tzinfo=dt.UTC)
    nav = [
        NavSample(t0, Decimal("1000")),
        NavSample(t0 + dt.timedelta(days=56), Decimal("990")),
    ]
    losses = [_actual(i, gross="1" if i < 10 else "-1") for i in range(30)]
    risk = score_h5(
        actual=losses,
        control=[_control(i, gross="0") for i in range(30)],
        nav_samples=nav,
        t0=t0,
        as_of=t0 + dt.timedelta(days=20),
    )
    assert risk.label == "FAIL-RISK"
    mdd_risk = score_h5(
        actual=[],
        control=[],
        nav_samples=[
            NavSample(t0, Decimal("1000")),
            NavSample(t0 + dt.timedelta(days=1), Decimal("840")),
        ],
        t0=t0,
        as_of=t0 + dt.timedelta(days=1),
    )
    assert mdd_risk.label == "FAIL-RISK"
    insufficient = score_h5(
        actual=[_actual(i) for i in range(59)],
        control=[_control(i, gross="0") for i in range(59)],
        nav_samples=nav,
        t0=t0,
        as_of=t0 + dt.timedelta(days=56),
    )
    assert insufficient.label == "INSUFFICIENT_SAMPLE"
    efficacy = score_h5(
        actual=[_actual(i, gross="1" if i < 30 else "-1") for i in range(60)],
        control=[_control(i, gross="2" if i < 40 else "-1") for i in range(60)],
        nav_samples=nav,
        t0=t0,
        as_of=t0 + dt.timedelta(days=56),
    )
    assert efficacy.label == "FAIL-EFFICACY"

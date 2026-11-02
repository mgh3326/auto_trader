"""Run the actual frozen research-list builder and offline CLI on synthetic bars."""

import argparse
import json
import math
from datetime import date, timedelta
from decimal import Decimal

import exchange_calendars as xcals
import pandas as pd
import pytest

from app.services.mock_strategy.us_signals import BASELINES, build_selection
from research.stock_selection.io import digest, write_json
from scripts.mock_strategy import execute, parser


def write_fixture(root):
    calendar = xcals.get_calendar("XNYS")
    last = "2026-10-30"
    first = calendar.session_offset(last, -280)
    days = calendar.sessions_in_range(first, last)
    symbols = [f"F{i:02}" for i in range(24)]
    rows = []
    for i, symbol in enumerate(["__benchmark__", *symbols]):
        for j, day in enumerate(days):
            price = 100 + i * 7 + j * 0.05 + (i + 1) * 0.2 * math.sin(j * 0.7 + i)
            volume = 100000 + i * 1000 + j
            rows.append(
                {
                    "date": str(day.date()),
                    "symbol": symbol,
                    "open": price,
                    "high": price * 1.01,
                    "low": price * 0.99,
                    "close": price,
                    "volume": volume,
                    "traded_value": price * volume,
                    "available_at": (
                        calendar.session_close(day) + timedelta(minutes=30)
                    ).isoformat(),
                    "action": False,
                }
            )
    path = root / "us" / "bars.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(path, index=False)
    write_json(
        path.with_suffix(".json"),
        {"market": "us", "sha256": digest(path), "source": "synthetic-test-only"},
    )
    assets = root / "assets.json"
    write_json(
        assets,
        {
            "account_ref": "fixture-paper",
            "assets": [
                {
                    "id": f"asset-{s}",
                    "symbol": s,
                    "class": "us_equity",
                    "status": "active",
                    "tradable": True,
                    "fractionable": True,
                    "shortable": True,
                    "easy_to_borrow": True,
                }
                for s in symbols
            ],
        },
    )
    return assets


def test_frozen_baselines_and_prefix_only_next_session_signals(tmp_path):
    write_fixture(tmp_path)
    selection = build_selection(tmp_path, date(2026, 11, 2))
    assert BASELINES == {
        1: "high_distance_52w",
        3: "relative_volatility_20",
        5: "relative_volatility_20",
        10: "relative_volatility_20",
    }
    assert selection.signal_date == date(2026, 10, 30)
    assert len(selection.up) == len(selection.down) == 10
    assert not ({r.symbol for r in selection.up} & {r.symbol for r in selection.down})
    path = tmp_path / "us" / "bars.csv"
    frame = pd.read_csv(path, float_precision="round_trip")
    future = frame[frame.date == "2026-10-30"].copy()
    future["date"] = "2026-11-02"
    future["available_at"] = "2026-11-02T21:30:00+00:00"
    future[["open", "high", "low", "close"]] *= 10
    pd.concat([frame, future]).to_csv(path, index=False)
    write_json(path.with_suffix(".json"), {"market": "us", "sha256": digest(path)})
    assert build_selection(tmp_path, date(2026, 11, 2)) == selection


@pytest.mark.asyncio
async def test_actual_cli_dry_run_prints_twenty_orders_without_broker_or_database(
    tmp_path, monkeypatch
):
    from app.services.mock_strategy import recorder, us_broker

    assets = write_fixture(tmp_path)

    def forbidden(*args, **kwargs):
        raise AssertionError("offline dry-run must not touch broker or database")

    monkeypatch.setattr(recorder, "MockStrategyRecorder", forbidden)
    monkeypatch.setattr(us_broker, "UsPaperBroker", forbidden)
    args = parser().parse_args(
        [
            "run",
            "--venue",
            "alpaca_paper",
            "--asof",
            "2026-11-02",
            "--dry-run",
            "--cache-dir",
            str(tmp_path),
            "--asset-fixture",
            str(assets),
        ]
    )
    payload = await execute(args)
    assert payload["dry_run"] is True
    assert len(payload["orders"]) == 20
    assert payload["orders"][0]["account_ref"] == "fixture-paper"
    assert len({r["client_order_id"] for r in payload["orders"]}) == 20
    assert all(
        r["notional"] == "10000" for r in payload["orders"] if r["side"] == "buy"
    )
    assert all(
        Decimal(r["qty"]) == Decimal(r["qty"]).to_integral_value()
        for r in payload["orders"]
        if r["side"] == "short"
    )
    assert payload["orders"][0]["exit_due_at"] == "2026-11-09T14:30:00+00:00"
    assert json.loads(json.dumps(payload)) == payload


@pytest.mark.asyncio
async def test_fixture_cannot_enable_real_orders(tmp_path):
    args = argparse.Namespace(
        top_n=10,
        notional=Decimal("10000"),
        asset_fixture=tmp_path / "assets.json",
        dry_run=False,
        command="run",
    )
    with pytest.raises(ValueError, match="dry-run only"):
        await execute(args)


def test_cli_reports_unresolved_episode_as_failure(monkeypatch, capsys):
    from scripts import mock_strategy

    async def unresolved(args):
        return {
            "orders": [
                {
                    "client_order_id": "synthetic",
                    "status": "filled",
                    "episode_status": "unresolved",
                }
            ]
        }

    monkeypatch.setattr(mock_strategy, "execute", unresolved)
    assert (
        mock_strategy.main(
            [
                "close-due",
                "--venue",
                "alpaca_paper",
                "--asof",
                "2026-11-09",
                "--confirm",
            ]
        )
        == 1
    )
    assert (
        json.loads(capsys.readouterr().out)["orders"][0]["episode_status"]
        == "unresolved"
    )


def test_producer_universe_cutoff_and_observation_provenance(tmp_path):
    from app.services.brokers.alpaca.schemas import Asset
    from app.services.mock_strategy.us import plan_orders

    fixture = write_fixture(tmp_path)
    path = tmp_path / "us/bars.json"
    meta = json.loads(path.read_text()) | {
        "producer": "mock-data-refresh/v1",
        "observation_date": "2026-11-02",
        "data_cutoff": "2026-10-30",
        "fetched_at": "2026-11-02T12:00:00+00:00",
    }
    for cutoff in ("2026-11-02", "2026-10-29"):
        write_json(path, {**meta, "data_cutoff": cutoff})
        with pytest.raises(ValueError, match="universe cutoff"):
            build_selection(tmp_path, date(2026, 11, 2))
    write_json(path, meta)
    selection = build_selection(tmp_path, date(2026, 11, 2))
    assert selection.universe_observed_at.isoformat() == meta["fetched_at"]
    assets = {
        a["symbol"]: Asset.model_validate(a)
        for a in json.loads(fixture.read_text())["assets"]
    }
    plan = plan_orders(
        selection, assets, account_ref="fixture-paper", notional=Decimal("10000")
    )
    assert plan.orders
    assert all(
        o.meta["universe_observed_at"] == meta["fetched_at"] for o in plan.orders
    )

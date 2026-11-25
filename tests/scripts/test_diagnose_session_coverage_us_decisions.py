"""hk 6554: the diagnose script prints each US symbol's decision for a given date.

The database read is replaced by a fake engine that returns no stored minutes; the
run reports are real collector-shaped dicts. Only the decision part is under test.
"""

from __future__ import annotations

import argparse
from datetime import date

import pytest

from scripts import diagnose_session_coverage as diag

pytestmark = pytest.mark.unit


class _Ctx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Conn:
    def begin(self):
        return _Ctx()

    async def execute(self, statement, params=None):
        return []


class _Engine:
    def connect(self):
        conn = _Conn()

        class _ConnCtx(_Ctx):
            async def __aenter__(self):
                return conn

        return _ConnCtx()


def _decision(symbol, decision, tier, reason=None):
    return {
        "symbol": symbol,
        "tier": tier,
        "decision": decision,
        "reason": reason,
        "score": {"best_rank": 3, "dollar_volume": "20000", "liquidity": "below_floor"},
    }


def _us_run(batch_id, trading_day, decisions):
    return {
        "batch_id": batch_id,
        "market": "us",
        "dry_run": False,
        "targeted_symbols": ["URBN"],
        "targets_cuts": [
            {"symbol": "THN01", "tier": "intraday_movers", "reason": "budget_cut"}
        ],
        "targets_decisions": {
            "trading_day": trading_day,
            "dollar_volume_floor": "100000",
            "decisions": decisions,
        },
    }


@pytest.mark.asyncio
async def test_prints_each_symbol_decision_for_the_requested_et_day(
    monkeypatch,
) -> None:
    monkeypatch.setattr(diag, "engine", _Engine())
    day_runs = [
        _us_run(
            "run-a",
            "2026-11-25",
            [
                _decision("URBN", "selected", "intraday_movers"),
                _decision("THN01", "cut", "intraday_movers", "budget_cut"),
                _decision("WRNT", "cut", "intraday_movers", "not_common_or_etf"),
            ],
        ),
        _us_run(
            "run-next", "2026-11-26", [_decision("ZZZ", "selected", "intraday_movers")]
        ),
        # an older US run with no decision record (before hk 6554) is skipped
        {
            "batch_id": "run-old",
            "market": "us",
            "targeted_symbols": [],
            "targets_cuts": [],
        },
    ]
    args = argparse.Namespace(
        market="us", date=date(2026, 11, 25), symbols=None, first_last=False
    )

    report = await diag.run(args, day_runs)

    assert len(report["decisions"]) == 1
    entry = report["decisions"][0]
    assert entry["batch_id"] == "run-a"
    assert entry["trading_day"] == "2026-11-25"
    assert entry["dollar_volume_floor"] == "100000"
    by_symbol = {d["symbol"]: d for d in entry["decisions"]}
    assert by_symbol["URBN"]["decision"] == "selected"
    assert by_symbol["THN01"]["reason"] == "budget_cut"
    assert by_symbol["THN01"]["score"]["dollar_volume"] == "20000"
    assert by_symbol["WRNT"]["reason"] == "not_common_or_etf"
    # the other day's symbol does not leak into this day's answer
    assert "ZZZ" not in {d["symbol"] for d in entry["decisions"]}


@pytest.mark.asyncio
async def test_a_kr_run_has_no_decisions_key_and_no_decisions_section(
    monkeypatch,
) -> None:
    monkeypatch.setattr(diag, "engine", _Engine())
    kr_run = {"batch_id": "kr-run", "market": "kr", "targeted_symbols": ["005930"]}
    args = argparse.Namespace(
        market="kr", date=date(2026, 11, 25), symbols=None, first_last=False
    )

    report = await diag.run(args, [kr_run])

    assert "decisions" not in report

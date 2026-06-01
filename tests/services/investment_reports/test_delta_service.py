# tests/services/investment_reports/test_delta_service.py
from __future__ import annotations

import types

import pytest

from app.services.investment_reports.delta_service import (
    DeltaService,
    _baseline_indices,
    _baseline_pnl_from_bundle_pairs,
    _holdings_pnl_delta,
    _index_delta,
    _levels_delta,
)


def test_levels_delta_filters_to_symbols_and_projects_flags():
    journal = {
        "entries": [
            {
                "symbol": "AAPL",
                "side": "buy",
                "target_price": 230.0,
                "stop_loss": 200.0,
                "current_price": 231.0,
                "pnl_pct_live": 4.1,
                "target_reached": True,
                "stop_reached": False,
            },
            {
                "symbol": "MSFT",
                "side": "buy",
                "target_price": 500.0,
                "stop_loss": 400.0,
                "current_price": 401.0,
                "pnl_pct_live": -1.0,
                "target_reached": False,
                "stop_reached": False,
            },
            {
                "symbol": "ZZZZ",
                "side": "buy",
                "target_price": 1.0,
                "stop_loss": 0.5,
                "current_price": 0.9,
                "pnl_pct_live": 0.0,
                "target_reached": False,
                "stop_reached": False,
            },
        ]
    }
    out = _levels_delta(journal, {"AAPL", "MSFT"}, near_pct=1.0)
    syms = [e["symbol"] for e in out["entries"]]
    assert syms == ["AAPL", "MSFT"]  # ZZZZ filtered out
    aapl = out["entries"][0]
    assert aapl["target_reached"] is True
    assert aapl["pnl_pct_live"] == 4.1
    # MSFT current 401 is within 1% of stop 400 -> near_stop True
    msft = out["entries"][1]
    assert msft["near_stop"] is True
    assert msft["near_target"] is False
    assert out["summary"] == {
        "near_target": 0,
        "near_stop": 1,
        "target_hit": 1,
        "stop_hit": 0,
    }


def test_levels_delta_empty_symbols_keeps_all_entries():
    journal = {
        "entries": [
            {
                "symbol": "AAPL",
                "side": "buy",
                "target_price": None,
                "stop_loss": None,
                "current_price": 10.0,
                "pnl_pct_live": 1.0,
                "target_reached": None,
                "stop_reached": None,
            },
        ]
    }
    out = _levels_delta(journal, set(), near_pct=1.0)
    assert [e["symbol"] for e in out["entries"]] == ["AAPL"]
    assert out["entries"][0]["near_target"] is False  # no target -> not near


def _snap(kind, payload):
    return types.SimpleNamespace(snapshot_kind=kind, payload_json=payload)


def test_baseline_pnl_from_bundle_pairs_reads_portfolio_holdings():
    pairs = [
        (object(), _snap("market", {"indices": {}})),
        (
            object(),
            _snap(
                "portfolio",
                {
                    "holdings": [
                        {"ticker": "AAPL", "pnl_rate": 1.0},
                        {"ticker": "MSFT", "pnl_rate": -2.0},
                        {
                            "ticker": "NOPNL"
                        },  # missing pnl_rate -> skipped, not fabricated
                    ]
                },
            ),
        ),
    ]
    assert _baseline_pnl_from_bundle_pairs(pairs) == {"AAPL": 1.0, "MSFT": -2.0}


def test_baseline_pnl_from_bundle_pairs_none_when_no_portfolio_kind():
    pairs = [(object(), _snap("market", {"indices": {}}))]
    assert _baseline_pnl_from_bundle_pairs(pairs) is None


def test_holdings_pnl_delta_joins_baseline_and_live_missing_not_zero():
    baseline = {"AAPL": 1.0, "MSFT": -2.0, "ONLYBASE": 5.0}
    live = {
        "accounts": [
            {
                "positions": [
                    {"symbol": "AAPL", "profit_rate": 4.1},
                    {"symbol": "MSFT", "profit_rate": -1.0},
                    {"symbol": "ONLYLIVE", "profit_rate": 9.0},
                    {"symbol": "NORATE"},  # missing profit_rate -> skipped
                ]
            },
        ]
    }
    out = _holdings_pnl_delta(baseline, live)
    by_symbol = {e["symbol"]: e for e in out["entries"]}
    assert set(by_symbol) == {"AAPL", "MSFT"}  # only symbols in BOTH
    assert by_symbol["AAPL"]["delta_pp"] == 3.1
    assert by_symbol["MSFT"]["delta_pp"] == 1.0
    assert out["summary"] == {
        "symbols_compared": 2,
        "symbols_baseline_only": 1,
        "symbols_live_only": 1,
    }


def test_baseline_indices_extracts_dict_or_none():
    ok = {"provenance": {}, "baseline": {"indices": {"^GSPC": {"current": 5500.0}}}}
    assert _baseline_indices(ok) == {"^GSPC": {"current": 5500.0}}
    assert _baseline_indices({"status": "unavailable", "reason": "x"}) is None
    assert _baseline_indices({"baseline": {}}) is None  # no indices key
    assert _baseline_indices({}) is None


def test_index_delta_change_pct_and_null_guards():
    baseline = {
        "^GSPC": {"current": 5500.0},
        "^VIX": {"current": 0.0},  # baseline 0 -> change_pct null, no div-by-zero
        "MISSINGLIVE": {"current": 100.0},
    }
    live = {
        "indices": [
            {"symbol": "^GSPC", "current": 5533.0},
            {"symbol": "^VIX", "current": 15.0},
            # MISSINGLIVE absent from live
        ]
    }
    out = _index_delta(baseline, live)
    by_symbol = {e["index_symbol"]: e for e in out["entries"]}
    assert round(by_symbol["^GSPC"]["change_pct"], 4) == 0.6
    assert by_symbol["^VIX"]["change_pct"] is None  # baseline 0 -> null
    assert by_symbol["MISSINGLIVE"]["live_value"] is None
    assert by_symbol["MISSINGLIVE"]["change_pct"] is None


def _baseline(
    *, market="us", symbols=None, market_snapshot=None, baseline_pnl="default"
):
    return {
        "market": market,
        "symbols": symbols if symbols is not None else {"AAPL"},
        "market_snapshot": market_snapshot
        if market_snapshot is not None
        else {"baseline": {"indices": {"^GSPC": {"current": 5500.0}}}},
        "baseline_pnl": {"AAPL": 1.0} if baseline_pnl == "default" else baseline_pnl,
    }


def _service(*, baseline, journal=None, holdings=None, index=None):
    async def loader(_uuid):
        return baseline

    async def journal_fn(*, account_type, market):
        if journal is None:
            raise RuntimeError("journal boom")
        return journal

    async def holdings_fn(*, market):
        if holdings is None:
            raise RuntimeError("holdings boom")
        return holdings

    async def index_fn():
        if index is None:
            raise RuntimeError("index boom")
        return index

    return DeltaService(
        session=None,
        baseline_loader=loader,
        journal_fn=journal_fn,
        holdings_fn=holdings_fn,
        market_index_fn=index_fn,
    )


@pytest.mark.asyncio
async def test_compute_delta_happy_path_all_three():
    svc = _service(
        baseline=_baseline(),
        journal={
            "entries": [
                {
                    "symbol": "AAPL",
                    "side": "buy",
                    "target_price": 230.0,
                    "stop_loss": 200.0,
                    "current_price": 231.0,
                    "pnl_pct_live": 4.1,
                    "target_reached": True,
                    "stop_reached": False,
                }
            ]
        },
        holdings={
            "accounts": [{"positions": [{"symbol": "AAPL", "profit_rate": 4.1}]}]
        },
        index={"indices": [{"symbol": "^GSPC", "current": 5533.0}]},
    )
    out = await svc.compute_delta(
        "11111111-1111-1111-1111-111111111111",
        computed_at_kst="2026-06-01T13:00:00+09:00",
    )
    assert out["success"] is True
    assert out["market"] == "us"
    assert out["computed_at_kst"] == "2026-06-01T13:00:00+09:00"
    assert out["levels_delta"]["summary"]["target_hit"] == 1
    assert out["holdings_pnl_delta"]["entries"][0]["delta_pp"] == 3.1
    assert round(out["index_delta"]["entries"][0]["change_pct"], 4) == 0.6
    assert "unavailable" not in out


@pytest.mark.asyncio
async def test_compute_delta_fail_open_isolates_each_signal():
    # journal raises; holdings + index still populate
    svc = _service(
        baseline=_baseline(),
        journal=None,  # -> RuntimeError
        holdings={
            "accounts": [{"positions": [{"symbol": "AAPL", "profit_rate": 2.0}]}]
        },
        index={"indices": [{"symbol": "^GSPC", "current": 5533.0}]},
    )
    out = await svc.compute_delta("11111111-1111-1111-1111-111111111111")
    assert out["success"] is True
    assert out["levels_delta"] is None
    assert out["unavailable"]["levels"]  # reason string present
    assert out["holdings_pnl_delta"]["entries"][0]["delta_pp"] == 1.0
    assert out["index_delta"]["entries"][0]["live_value"] == 5533.0


@pytest.mark.asyncio
async def test_compute_delta_baseline_absent_marks_unavailable():
    svc = _service(
        baseline=_baseline(
            market_snapshot={"status": "unavailable", "reason": "not_collected"},
            baseline_pnl=None,
        ),
        journal={"entries": []},
        holdings={"accounts": []},
        index={"indices": []},
    )
    out = await svc.compute_delta("11111111-1111-1111-1111-111111111111")
    assert out["success"] is True
    assert out["holdings_pnl_delta"] is None
    assert out["unavailable"]["holdings"] == "baseline_snapshot_absent"
    assert out["index_delta"] is None
    assert out["unavailable"]["index"] == "baseline_snapshot_absent"


@pytest.mark.asyncio
async def test_compute_delta_baseline_not_found():
    async def loader(_uuid):
        return None

    svc = DeltaService(session=None, baseline_loader=loader)
    out = await svc.compute_delta("11111111-1111-1111-1111-111111111111")
    assert out == {"success": False, "error": "baseline_not_found"}

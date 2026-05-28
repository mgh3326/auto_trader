"""ROB-339 — discover.py orchestration (pure core) + CLI parser + artifact write.

The catalog-loading main() is integration (covered at smoke); the pure
discover_from_bars + arg parser + write helper are unit-tested here.
"""

from __future__ import annotations

import json

import pandas as pd
from discover import build_arg_parser, discover_from_bars, write_discovery_artifact


def _bars(start: str = "2026-03-02 13:00") -> pd.DataFrame:
    dt = pd.date_range(start, periods=400, freq="1min", tz="UTC")
    close = pd.Series(range(400), dtype=float) * 0.01 + 100.0
    return pd.DataFrame(
        {
            "dt": dt,
            "open": close,
            "high": close + 0.05,
            "low": close - 0.05,
            "close": close,
            "volume": 10.0,
        }
    )


def test_discover_from_bars_one_symbol_five_hypotheses() -> None:
    art = discover_from_bars({"XRPUSDT": _bars()}, fee_budget_bps=8.0)
    assert art["hypotheses_tested"] == 5
    recs = {h["recommendation"] for h in art["hypotheses"]}
    assert recs <= {"screened_out", "needs_more_data", "promote_to_full_validation"}


def test_discover_from_bars_tags_symbol() -> None:
    art = discover_from_bars({"XRPUSDT": _bars()}, fee_budget_bps=8.0)
    assert all(h["symbol"] == "XRPUSDT" for h in art["hypotheses"])


def test_two_symbols_produce_ten_entries() -> None:
    art = discover_from_bars(
        {"XRPUSDT": _bars(), "BTCUSDT": _bars("2026-03-03 00:00")},
        fee_budget_bps=8.0,
    )
    assert art["hypotheses_tested"] == 10
    assert {"XRPUSDT", "BTCUSDT"} == {h["symbol"] for h in art["hypotheses"]}


def test_arg_parser_defaults() -> None:
    ns = build_arg_parser().parse_args(["--symbols", "XRPUSDT,BTCUSDT"])
    assert ns.symbols == "XRPUSDT,BTCUSDT"
    assert ns.fee_budget_bps == 8.0
    assert ns.min_samples == 200


def test_write_artifact_roundtrip(tmp_path) -> None:
    art = discover_from_bars({"XRPUSDT": _bars()}, fee_budget_bps=8.0)
    out = tmp_path / "discovery.json"
    written = write_discovery_artifact(art, export=out)
    assert written == out
    reloaded = json.loads(out.read_text())
    assert reloaded["schema_version"] == "scalping_discovery.v1"
    assert reloaded["hypotheses_tested"] == 5

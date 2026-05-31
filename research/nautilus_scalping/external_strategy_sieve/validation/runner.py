"""ROB-383 Phase 3 - validation runner and operator CLI.

Default action is a dry run that prints the validation plan without network.
``--run`` performs the bounded public kline fetch, runs clean-room signals,
evaluates the gate at Binance Demo taker fees, and writes counts-only results.
No app, broker, order, scheduler, production env, or secret path is imported.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pit_bars
import pit_klines_fetcher
import pit_universe
import validated_gate
from artifact_paths import resolve_artifact_path
from frozen_config import FROZEN_CONFIG

from external_strategy_sieve.validation import baselines, classify, signals
from external_strategy_sieve.validation.frozen_params import (
    FROZEN_PARAMS,
    PARAMS_VERSION,
    params_hash,
)

_NAUT_ROOT = Path(__file__).resolve().parents[2]
_MANIFEST = str(_NAUT_ROOT / "data_manifests" / "pit_universe.v1.json")
_DEMO_TAKER_BPS = FROZEN_CONFIG.taker_bps
_FEE_GRID = list(FROZEN_CONFIG.fee_grid_bps)
_DEFAULT_SYMBOLS = ["BTCUSDT", "ETHUSDT", "XRPUSDT", "SOLUSDT", "DOGEUSDT"]

_SIGNAL_FNS = {
    "supertrend_trades": signals.supertrend_trades,
    "bbrsi_trades": signals.bbrsi_trades,
    "squeeze_momentum_trades": signals.squeeze_momentum_trades,
    "range_filter_trades": signals.range_filter_trades,
    "chandelier_trades": signals.chandelier_trades,
}


def build_plan(symbols: list[str], from_month: str, to_month: str) -> dict[str, Any]:
    return {
        "params_version": PARAMS_VERSION,
        "params_hash": params_hash(),
        "fee_bps": _DEMO_TAKER_BPS,
        "fee_grid_bps": _FEE_GRID,
        "symbols": symbols,
        "window": {"from_month": from_month, "to_month": to_month},
        "candidates": dict(FROZEN_PARAMS),
    }


def _pooled_trades(
    signal_fn,
    params: dict,
    symbols: list[str],
    interval: str,
    manifest: pit_universe.PITManifest,
    fetch: bool,
    from_month: str,
    to_month: str,
):
    pooled = []
    pooled_breakout = []
    pooled_random = []
    by_symbol = {}
    for symbol in symbols:
        if fetch:
            pit_klines_fetcher.fetch_months(symbol, interval, from_month, to_month)
        bars = pit_bars.load_bars(symbol, interval, manifest)
        if len(bars) < 50:
            by_symbol[symbol] = 0
            continue
        trades = signal_fn(bars, **params)
        pooled.extend(trades)
        pooled_breakout.extend(baselines.breakout_baseline(bars))
        pooled_random.extend(
            baselines.random_entry_trades(bars, n_trades=max(1, len(trades)), hold=5)
        )
        by_symbol[symbol] = len(trades)
    return pooled, pooled_breakout, pooled_random, by_symbol


def run(
    symbols: list[str] | None = None,
    from_month: str = "2023-01",
    to_month: str = "2024-12",
    fetch: bool = True,
) -> dict[str, Any]:
    symbols = symbols or _DEFAULT_SYMBOLS
    manifest = pit_universe.PITManifest.load(_MANIFEST).strict_usdt_perp()
    out: dict[str, Any] = {
        "plan": build_plan(symbols, from_month, to_month),
        "results": {},
    }
    for candidate_id, spec in FROZEN_PARAMS.items():
        signal_fn = _SIGNAL_FNS[spec["signal"]]
        trades, breakout, random_entry, by_symbol = _pooled_trades(
            signal_fn,
            spec["params"],
            symbols,
            spec["interval"],
            manifest,
            fetch,
            from_month,
            to_month,
        )
        report = validated_gate.evaluate_gate(
            candidate_runs={"default": trades},
            baseline_breakout=breakout,
            baseline_random=random_entry,
            fee_bps=_DEMO_TAKER_BPS,
            candidate_name=candidate_id,
            hypothesis=spec.get("caveat", ""),
            symbols=symbols,
            window={"from_month": from_month, "to_month": to_month},
        )
        klass, reasons = classify.classify(report)
        fee_sweep = (
            {
                f"{fee}bps": validated_gate.metrics_at_fee(trades, fee).net_pnl
                for fee in _FEE_GRID
            }
            if trades
            else {}
        )
        out["results"][candidate_id] = {
            "class": klass,
            "reasons": reasons,
            "trade_count": len(trades),
            "trades_by_symbol": by_symbol,
            "verdict": report.verdict,
            "verdict_reasons": report.verdict_reasons,
            "results": report.results,
            "per_fold": report.per_fold,
            "baselines": report.baselines,
            "fee_sweep_net_pnl": fee_sweep,
            "caveat": spec.get("caveat", ""),
        }
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="ROB-383 Phase 3 validation runner")
    parser.add_argument(
        "--run", action="store_true", help="fetch and validate; default is dry-run"
    )
    parser.add_argument("--symbols", nargs="*", default=None)
    parser.add_argument("--from-month", default="2023-01")
    parser.add_argument("--to-month", default="2024-12")
    parser.add_argument(
        "--out",
        default=None,
        help="write JSON; default: discovery/rob383/phase3_validation.json",
    )
    args = parser.parse_args()

    if not args.run:
        plan = build_plan(
            args.symbols or _DEFAULT_SYMBOLS, args.from_month, args.to_month
        )
        print(json.dumps({"mode": "dry-run", "plan": plan}, indent=2))
        return 0

    out = run(args.symbols, args.from_month, args.to_month, fetch=True)
    dest = (
        Path(args.out)
        if args.out
        else resolve_artifact_path("discovery", "rob383", "phase3_validation.json")
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(out, indent=2))
    for candidate_id, result in out["results"].items():
        print(
            f"{candidate_id:32s} {result['class']:22s} "
            f"verdict={result['verdict']:16s} trades={result['trade_count']}"
        )
    print(f"\nwrote {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

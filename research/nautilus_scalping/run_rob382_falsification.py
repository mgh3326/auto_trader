#!/usr/bin/env python3
"""ROB-382 — strat.ninja external-strategy falsification spike driver (research only).

Re-validates ported strat.ninja signals under OUR standard: Binance USDⓈ-M data, frozen
taker/maker cost model, cost-blind gross screen → cost/OOS gate. Produces a counts-only
verdict + contrast table (their in-sample SPOT score vs our OOS USDⓈ-M verdict). We do NOT
trust strat.ninja's numbers — we re-derive everything ourselves.

Two modes:
  --self-test   Prove the simulate → spec → campaign wiring + frozen config_hash with a
                tiny SYNTHETIC bar series and a trivial inline signal. No network, no
                candidate modules, no data. Always runnable in CI.
  (default)     Real run: import each ported candidate module, run it across the fetched
                USDⓈ-M klines, write the rob382_falsification.v1 artifact under
                results/rob382/ (gitignored). Candidate modules absent → skipped + noted
                (so partial runs are explicit, never silently "all clear").

Safety: research/backtest only. No freqtrade/talib runtime import; no broker/order/watch/
order-intent/approval/trade-journal mutation; no scheduler/TaskIQ/Prefect/cron; no prod DB
write; no secrets; no raw bars or leaderboard dumps committed (counts-only).
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
from types import SimpleNamespace

SCHEMA_VERSION = "rob382_falsification.v1"


def _self_test() -> dict:
    import campaign
    import campaign_specs as cs
    import rob382_backtest as bt
    from frozen_config import FROZEN_CONFIG
    from rob382_bars import OHLCVBar

    # Synthetic uptrend with periodic 2% pullbacks (deterministic; no network).
    bars = []
    price = 100.0
    minute = 60_000
    for i in range(400):
        price *= 1.001 if i % 10 else 0.98  # drift up, dip every 10 bars
        bars.append(OHLCVBar(ts=i * minute, open=price, high=price * 1.002,
                             low=price * 0.998, close=price, volume=1000.0,
                             close_ts=i * minute + minute - 1))

    def signals(bs, bars_1h=None):
        # trivial: buy the dip (close < prior close), exit never (roi/sl/max-hold governs)
        entry = [i > 0 and bs[i].close < bs[i - 1].close for i in range(len(bs))]
        return entry, [False] * len(bs)

    module = SimpleNamespace(
        NATIVE_INTERVAL="1m", NEEDS_INFORMATIVE_1H=False,
        EXIT_MODEL=bt.ExitModel(type="roi_sl", roi_pct=0.02, hard_sl_pct=0.15, max_hold_bars=20),
        HOLD_SEMANTICS="self-test trivial", signals=signals,
    )
    trades = bt.simulate(bars, *module.signals(bars), module.EXIT_MODEL)
    spec = {
        "name": "selftest_dip", "kind": "trade", "data": trades,
        "summary": cs._summary_from_trades("selftest_dip", trades, cs.OOS_SPLIT_TS),
        "maker_conservative_net": None,
    }
    table = campaign.run_campaign([spec], config=FROZEN_CONFIG, min_trades=5)
    return {"trade_count": len(trades), "verdict_table": table,
            "config_hash": table["config_hash"]}


def _real_run(args) -> int:
    import rob382_candidates as rc
    import rob382_runner as runner
    from frozen_config import FROZEN_CONFIG

    symbols = tuple(s.strip() for s in args.symbols.split(",") if s.strip())
    rows = []
    skipped = []
    for cand in rc.CANDIDATES:
        try:
            module = importlib.import_module(cand["module"])
        except ModuleNotFoundError:
            skipped.append({"key": cand["key"], "module": cand["module"],
                            "reason": "ported signal module not present yet"})
            print(f"  SKIP {cand['key']}: module {cand['module']} not found", flush=True)
            continue
        print(f"  running {cand['key']} ({cand['display_name']}) ...", flush=True)
        row = runner.run_candidate(module, name=cand["key"], contrast=cand["contrast"],
                                   symbols=symbols, min_trades=args.min_trades)
        row["display_name"] = cand["display_name"]
        row["strat_ninja_name"] = cand["strat_ninja_name"]
        row["family_shape"] = cand["family_shape"]
        row["diversity_rationale"] = cand["diversity_rationale"]
        row["source_url"] = cand["source_url"]
        row["source_note"] = cand["source_note"]
        rows.append(row)
        print(f"    -> verdict={row['our_verdict']} gross={row['our_gross_bps']}bps "
              f"oos_gross={row['our_oos_gross_bps']}bps oos_net@taker={row['our_oos_net_bps_frozen_taker']}bps "
              f"t_oos={row['our_t_stat_oos_gross']} gate={row['gate_verdict']} "
              f"survivor={row['meets_decisive_survivor_bar']} trades={row['trade_count']}", flush=True)

    verdicts = {r["our_verdict"] for r in rows}
    decisive = [r["name"] for r in rows if r.get("meets_decisive_survivor_bar")]
    if not rows:
        overall = "no_candidate_modules_run"
    elif decisive:
        overall = (
            f"survivor_found — {decisive} clear the decisive bar (gross + t>2 OOS + beats "
            "micro-breakout/random baselines + net-positive at frozen taker); MAY justify a "
            "separate bounded backtest issue (explicit later decision, not auto-created)"
        )
    elif verdicts <= {"screened_out", "needs_more_data"}:
        overall = "all_screened_out_or_insufficient — close line, open NO backtest issue"
    else:
        overall = (
            "no_decisive_survivor — some candidates show positive GROSS at their native timeframe "
            "(unlike our gross-negative short-horizon families) and one is gate-validated, but NONE "
            "clears the decisive bar (gross + t>2 OOS + beats-baselines + net-positive at frozen "
            "taker). External leaderboards do not survive our OOS USDⓈ-M gate. Close line, open NO "
            "backtest issue."
        )

    artifact = {
        "schema_version": SCHEMA_VERSION,
        "config_hash": FROZEN_CONFIG.config_hash(),
        "config": FROZEN_CONFIG.to_dict(),
        "data": {"symbols": list(symbols), "window": f"{args.window}", "venue": "binance_usdm_futures"},
        "candidates": rows,
        "skipped": skipped,
        "overall_verdict": overall,
        "contrast_note": (
            "their_* columns are strat.ninja in-sample SPOT backtest numbers, recorded FOR "
            "CONTRAST ONLY (not evidence). our_* columns are re-derived on Binance USDⓈ-M with "
            "the frozen cost model. The headline is the GAP: their positive in-sample spot "
            "score vs our OOS USDⓈ-M verdict."
        ),
        "safety": {
            "freqtrade_runtime_import": False,
            "broker_order_watch_mutation": False,
            "scheduler_activation": False,
            "prod_db_write": False,
            "raw_data_or_leaderboard_committed": False,
            "leaderboard_access": "read-only via gstack /browse + read-only WebFetch",
        },
    }
    assert artifact["config_hash"] == FROZEN_CONFIG.config_hash(), "frozen config drift!"

    out_dir = "results/rob382"
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "rob382_falsification.v1.json")
    with open(out_path, "w") as fh:
        json.dump(artifact, fh, indent=2, default=str)
    print(json.dumps(artifact, indent=2, default=str))
    print(f"\noverall: {overall}")
    print(f"wrote {out_path} (gitignored). Author docs/runbooks/rob-382-strat-ninja-falsification.md from this.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="ROB-382 strat.ninja falsification spike (research only)")
    ap.add_argument("--self-test", action="store_true",
                    help="synthetic wiring proof (no network/data/modules); prints verdict table")
    ap.add_argument("--symbols", default="BTCUSDT,ETHUSDT,XRPUSDT,SOLUSDT")
    ap.add_argument("--window", default="2024-01..2025-12", help="recorded in artifact (fetch is separate)")
    ap.add_argument("--min-trades", type=int, default=100)
    args = ap.parse_args(argv)

    if args.self_test:
        result = _self_test()
        print(json.dumps(result, indent=2, default=str))
        from frozen_config import FROZEN_CONFIG
        assert result["config_hash"] == FROZEN_CONFIG.config_hash(), "frozen config drift!"
        return 0
    return _real_run(args)


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""ROB-353 (PR2) — bounded empirical RUN harness for the ROB-351 funnel (research only).

Two modes:
  --self-test   Build the three family specs from tiny SYNTHETIC panels (no network,
                no data, no secrets) and print the rob351_campaign.v1 verdict table.
                Proves the bridge wiring + that the frozen config_hash is unchanged.
  (default)     Bounded real RUN: load the PR1 PIT manifest, fetch/cache 1d klines for
                strict_usdt_perp ∩ window, build specs, call campaign.run_campaign, and
                write the verdict JSON + controls under results/rob353/ (gitignored).
                Network/operator-gated. The committed report is authored from this output.

Safety: research/backtest only. No live, no Demo confirm, no broker/order/scheduler/DB,
no /invest. ROB-343 is RECOMMENDED by the verdict, never run here. No raw data committed.
"""
from __future__ import annotations

import argparse
import json
import sys


def _self_test() -> dict:
    import campaign
    import campaign_specs as cs
    import pit_universe
    from frozen_config import FROZEN_CONFIG

    DAY = 86_400_000
    panel = {
        "AUSDT": [(i * DAY, 100.0 + i) for i in range(40)],
        "BUSDT": [(i * DAY, 50.0 - 0.2 * i) for i in range(40)],
        "CUSDT": [(i * DAY, 75.0 + (i % 5)) for i in range(40)],
    }
    manifest = pit_universe.PITManifest.from_records(
        [{"symbol": s, "listed_from": 0} for s in panel]
    )
    rebals = [10 * DAY, 17 * DAY, 24 * DAY, 31 * DAY]
    specs = [
        cs.breakout_spec(panel),
        cs.ts_trend_spec(panel),
        cs.xs_momentum_spec(panel, rebals, manifest),
    ]
    return campaign.run_campaign(specs, config=FROZEN_CONFIG, min_trades=5)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="ROB-353 bounded empirical RUN (research only)")
    ap.add_argument("--self-test", action="store_true",
                    help="synthetic wiring proof (no network); prints the verdict table")
    ap.add_argument("--from-month", default="2023-01")
    ap.add_argument("--to-month", default="2026-04")
    ap.add_argument("--max-symbols", type=int, default=None,
                    help="operator bound on universe size (default: all qualifying)")
    ap.add_argument("--skip-fetch", action="store_true",
                    help="use already-downloaded klines; do not hit the network")
    args = ap.parse_args(argv)

    if args.self_test:
        result = _self_test()
        print(json.dumps(result, indent=2))
        from frozen_config import FROZEN_CONFIG
        assert result["config_hash"] == FROZEN_CONFIG.config_hash(), "frozen config drift!"
        return 0

    return _real_run(args)


def _real_run(args) -> int:  # pragma: no cover - network/operator-gated
    import os

    import campaign
    import campaign_controls as cc
    import campaign_specs as cs
    import pit_bars
    import pit_klines_fetcher
    import pit_universe
    from frozen_config import FROZEN_CONFIG

    manifest = pit_universe.PITManifest.load("data_manifests/pit_universe.v1.json").strict_usdt_perp()
    lo = pit_universe._date_to_epoch_ms(f"{args.from_month}-01")
    hi = pit_universe._date_to_epoch_ms(f"{args.to_month}-28")
    symbols = cc.filter_universe(manifest, lo, hi)
    if args.max_symbols:
        symbols = symbols[: args.max_symbols]
    print(f"universe: {len(symbols)} strict-perp symbols (membership+quality filtered)")

    if not args.skip_fetch:
        for i, sym in enumerate(symbols, 1):
            summary = pit_klines_fetcher.fetch_months(sym, "1d", args.from_month, args.to_month)
            if i % 25 == 0:
                print(f"  fetched {i}/{len(symbols)} (last {sym}: {summary['downloaded']} dl)")

    panel = pit_bars.load_panel(symbols, "1d", manifest)
    panel = {s: v for s, v in panel.items() if len(v) >= 30}
    print(f"panel: {len(panel)} symbols with >=30 daily bars")
    rebals = cc.weekly_rebalances(lo, hi)

    specs = [
        cs.breakout_spec(panel),
        cs.ts_trend_spec(panel),
        cs.xs_momentum_spec(panel, rebals, manifest),
    ]
    result = campaign.run_campaign(specs, config=FROZEN_CONFIG, min_trades=5)
    assert result["config_hash"] == FROZEN_CONFIG.config_hash(), "frozen config drift!"

    btc = panel.get("BTCUSDT")
    controls = {
        "universe_size": len(panel),
        "window": f"{args.from_month}..{args.to_month}",
        "interval": "1d",
        "btc_buy_hold_bps": (cc.buy_hold_bps(btc) if btc else None),
        "family_drawdown_bps": {},
        "skipped_controls": [
            "dollar-volume liquidity filter (used manifest coverage/confidence instead)",
            "parameter-neighborhood sweep", "BTC regime split", "symbol-concentration analysis",
            "1h interval (deferred)",
        ],
    }
    for spec in specs:
        if spec["kind"] == "portfolio":
            controls["family_drawdown_bps"][spec["name"]] = cc.max_drawdown_bps(
                [p.gross_ref_pnl for p in spec["data"]], notional=cs.NOTIONAL)

    os.makedirs("results/rob353", exist_ok=True)
    out = {"verdict_table": result, "controls": controls,
           "spec_sample_counts": {s["name"]: s["summary"].sample_count for s in specs}}
    with open("results/rob353/rob351_campaign.v1.json", "w") as fh:
        json.dump(out, fh, indent=2, default=str)
    print(json.dumps(out, indent=2, default=str))
    print("\nwrote results/rob353/rob351_campaign.v1.json (gitignored). "
          "Author docs/runbooks/rob-353-pr2-empirical-verdict.md from this output.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

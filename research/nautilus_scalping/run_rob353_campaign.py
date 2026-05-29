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
    raise SystemExit(
        f"Real RUN is operator-gated and implemented in Task 5 "
        f"(from {args.from_month} to {args.to_month}). "
        "Use --self-test to verify wiring without network/data."
    )


if __name__ == "__main__":
    sys.exit(main())

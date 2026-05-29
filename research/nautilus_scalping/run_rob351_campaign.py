#!/usr/bin/env python3
"""ROB-351 — cost-blind funnel operator entry point (research/backtest only).

Wires PIT universe + family signals + cost-blind screen + gate + 343 label into a
verdict table (see ``campaign.run_campaign``). Two modes:

  --self-test   Run the funnel on tiny SYNTHETIC fixtures and print the verdict
                table. Needs no data/secrets — proves the wiring end to end.

  (default)     Explain what a real RUN needs and exit 0. The empirical campaign
                against Binance USDⓈ-M data is the operator's PR2 step: point
                ``AUTO_TRADER_RESEARCH_ARTIFACT_ROOT`` at a data root holding the
                bar-level OHLCV + a PIT manifest (pit_universe.PITManifest), build
                family specs from it, and call ``campaign.run_campaign``.

Safety boundary (unchanged): no live, no Demo confirm, no broker/order/watch/
order-intent mutation, no scheduler, no prod DB/env/secret, no /invest exposure,
no raw large data committed, no credential logging. ROB-343 is only RECOMMENDED
by the verdict, never run here.
"""

from __future__ import annotations

import argparse
import json
import sys


def _self_test() -> dict:
    # Imported lazily so --help / arg parsing never needs the research deps.
    import campaign
    import families
    from discovery.screen import HypothesisSummary
    from frozen_config import FROZEN_CONFIG

    def trades(gross_each, n):
        return [families.make_taker_trade(gross_each, ts=i, notional=1000.0) for i in range(n)]

    specs = [
        {"name": "family1_breakout_continuation",
         "summary": HypothesisSummary("f1", "demo", 40, 8.0, 4.0, oos_gross_bps=8.0),
         "kind": "trade", "data": trades(5.0, 40), "maker_conservative_net": None},
        {"name": "family2_trend_basket(seed-style cost-binding)",
         "summary": HypothesisSummary("f2", "demo", 40, 6.0, -2.0, oos_gross_bps=6.0),
         "kind": "trade", "data": trades(0.5, 40), "maker_conservative_net": 1.5},
        {"name": "family3_xs_momentum(no gross edge)",
         "summary": HypothesisSummary("f3", "demo", 40, -1.0, -3.0, oos_gross_bps=-1.0),
         "kind": "trade", "data": trades(-2.0, 40), "maker_conservative_net": None},
    ]
    return campaign.run_campaign(specs, config=FROZEN_CONFIG, min_trades=5)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="ROB-351 cost-blind funnel (research only)")
    ap.add_argument("--self-test", action="store_true",
                    help="run the funnel on synthetic fixtures and print the verdict table")
    args = ap.parse_args(argv)

    if args.self_test:
        print(json.dumps(_self_test(), indent=2))
        return 0

    print(
        "ROB-351 cost-blind funnel — no data RUN performed.\n"
        "This PR ships the funnel CODE + tests; the empirical RUN is operator-gated\n"
        "(no market data is committed). To run for real:\n"
        "  1. set AUTO_TRADER_RESEARCH_ARTIFACT_ROOT to a data root (bar OHLCV + PIT manifest)\n"
        "  2. build family specs (families.py) from that data\n"
        "  3. call campaign.run_campaign(specs, FROZEN_CONFIG)\n"
        "Verify the wiring now with:  python run_rob351_campaign.py --self-test\n"
        "Safety: research only; ROB-343 is recommended by the verdict, never run here."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

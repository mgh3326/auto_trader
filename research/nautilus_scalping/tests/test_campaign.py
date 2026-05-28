"""ROB-351 (Stage 3) — end-to-end funnel driver integration.

Wires cost-blind screen (Stage 1) -> gate (Stage 2) -> 343 label (Stage 3) over a
set of family specs and assembles a verdict table. Synthetic data only — proves
the funnel MECHANICS and that the frozen-config hash is recorded; the empirical
RUN on Binance USDⓈ-M data is the operator's PR2 step.
"""

import campaign
import families
from discovery.screen import HypothesisSummary
from frozen_config import FROZEN_CONFIG


def _trades(gross_each, n, notional=1000.0):
    return [families.make_taker_trade(gross_each, ts=i, notional=notional) for i in range(n)]


def test_campaign_produces_verdict_table_with_three_outcomes():
    specs = [
        # net-viable: big gross, survives taker fees -> promote_to_pilot
        {"name": "A_net_viable",
         "summary": HypothesisSummary("A", "c", 40, gross_expectancy_bps=8.0,
                                      fee_adjusted_bps=4.0, oos_gross_bps=8.0),
         "kind": "trade", "data": _trades(5.0, 40), "maker_conservative_net": None},
        # cost-binding + closable: gross>0, taker-net<0, maker-conservative>0 -> 343 candidate
        {"name": "B_cost_binding",
         "summary": HypothesisSummary("B", "c", 40, gross_expectancy_bps=6.0,
                                      fee_adjusted_bps=-2.0, oos_gross_bps=6.0),
         # gross +0.5/trade but taker fee (~0.8/trade) pushes net negative
         "kind": "trade", "data": _trades(0.5, 40), "maker_conservative_net": 1.5},
        # no gross edge -> screened_out at Stage 1, never reaches the gate
        {"name": "C_screened",
         "summary": HypothesisSummary("C", "c", 40, gross_expectancy_bps=-1.0,
                                      fee_adjusted_bps=-3.0, oos_gross_bps=-1.0),
         "kind": "trade", "data": _trades(-2.0, 40), "maker_conservative_net": None},
    ]
    rep = campaign.run_campaign(specs, config=FROZEN_CONFIG, min_trades=5)

    rows = {r["name"]: r for r in rep["families"]}
    assert rows["C_screened"]["screen"] == "screened_out"
    assert rows["C_screened"]["label_343"] is None  # never gated

    assert rows["A_net_viable"]["screen"] == "promote_to_full_validation"
    assert rows["A_net_viable"]["label_343"] == "promote_to_pilot"

    assert rows["B_cost_binding"]["screen"] == "promote_to_full_validation"
    assert rows["B_cost_binding"]["cost_binding_screen"] is True
    assert rows["B_cost_binding"]["label_343"] == "cost_binding_343_candidate"

    # ex-ante evidence recorded
    assert rep["config_hash"] == FROZEN_CONFIG.config_hash()
    assert rep["schema_version"].startswith("rob351_campaign")


def test_campaign_empty_specs_is_safe():
    rep = campaign.run_campaign([], config=FROZEN_CONFIG)
    assert rep["families"] == []
    assert rep["config_hash"] == FROZEN_CONFIG.config_hash()

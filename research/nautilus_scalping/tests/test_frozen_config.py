"""ROB-351 (eng-review ex-ante enforcement) — frozen campaign config + hash.

Thresholds and the achievable-execution envelope are committed in PR1 BEFORE any
PR2 OOS read. The run records ``config_hash``; changing a threshold after the
fact changes the hash, making an ex-post tweak detectable rather than a promise.
"""

import ast
from pathlib import Path

import frozen_config as fc


def test_frozen_default_present_and_documented():
    c = fc.FROZEN_CONFIG
    assert c.economic_triviality_floor_bps > 0.0  # sign>0 is too low (Codex)
    assert c.achievable_maker_bps == 2.0  # Binance USD-M demo maker
    assert c.taker_bps == 4.0  # Binance USD-M demo taker
    assert c.fdr_alpha == 0.05


def test_config_hash_is_stable_across_calls():
    assert fc.FROZEN_CONFIG.config_hash() == fc.FROZEN_CONFIG.config_hash()


def test_changing_a_threshold_changes_the_hash():
    import dataclasses

    base = fc.FROZEN_CONFIG
    tweaked = dataclasses.replace(base, economic_triviality_floor_bps=999.0)
    assert tweaked.config_hash() != base.config_hash()


def test_honest_gate_definitions_are_frozen_in_campaign_hash():
    import dataclasses

    base = fc.FROZEN_CONFIG
    fields = {
        "dsr_probability_threshold": 0.99,
        "pbo_max": 0.4,
        "baseline_names": ("cash",),
        "cost_stress_multipliers": (2.0,),
        "mdd_target_pct": 5.0,
    }
    for name, value in fields.items():
        assert (
            dataclasses.replace(base, **{name: value}).config_hash()
            != base.config_hash()
        )


def test_to_dict_round_trip():
    c = fc.FROZEN_CONFIG
    assert fc.CampaignConfig.from_dict(c.to_dict()) == c


def test_frozen_config_keeps_isolated_stdlib_boundary():
    path = Path(fc.__file__)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }

    assert not {module for module in imported if module.startswith("app.")}

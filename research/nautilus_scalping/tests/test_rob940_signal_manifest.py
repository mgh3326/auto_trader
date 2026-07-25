"""ROB-943 (H3, ROB-940) — exact frozen 24-row signal manifest tests.

Fable Q1=A: the 24 rows below (12 S1 + 12 S2), their order, values, and the
consult doc's ex-ante hypothesis phrases are byte-stable and frozen. Any
mismatch here means the manifest drifted from the approved shortlist and must
fail closed, not silently adapt.
"""

from __future__ import annotations

import dataclasses

import pytest
from rob940_signal_manifest import (
    S1_DOMAINS,
    S2_DOMAINS,
    SYMBOLS,
    FrozenSignalConstants,
    S1Config,
    S2Config,
    get_s1_config,
    get_s2_config,
    resolve_s1_config_for_symbol,
    resolve_s2_config_for_symbol,
    signal_manifest_hash,
    validate_s1_configs,
    validate_s2_configs,
)

EXPECTED_S1 = (
    ("S1-00", 16, 1.25, 1.25, 1.80, "연구 default"),
    ("S1-01", 12, 1.25, 1.25, 1.80, "shorter breakout lookback"),
    ("S1-02", 24, 1.25, 1.25, 1.80, "longer breakout lookback"),
    ("S1-03", 16, 1.00, 1.25, 1.80, "looser volume confirmation"),
    ("S1-04", 16, 1.50, 1.25, 1.80, "stricter volume confirmation"),
    ("S1-05", 16, 1.25, 1.00, 1.80, "tighter ATR stop"),
    ("S1-06", 16, 1.25, 1.50, 1.80, "wider ATR stop"),
    ("S1-07", 16, 1.25, 1.25, 1.50, "lower payoff ratio"),
    ("S1-08", 16, 1.25, 1.25, 2.00, "higher payoff ratio"),
    ("S1-09", 12, 1.50, 1.25, 1.80, "fast breakout requires stronger volume"),
    ("S1-10", 24, 1.00, 1.25, 1.80, "slow breakout tolerates weaker volume"),
    ("S1-11", 16, 1.25, 1.00, 2.00, "tight-stop/high-payoff cost resilience"),
)

EXPECTED_S2 = (
    ("S2-00", 3.00, 2.00, 0.35, 1.25, "연구 default"),
    ("S2-01", 2.75, 2.00, 0.35, 1.25, "lower shock threshold"),
    ("S2-02", 3.25, 2.00, 0.35, 1.25, "higher shock threshold"),
    ("S2-03", 3.00, 1.50, 0.35, 1.25, "looser volume spike"),
    ("S2-04", 3.00, 2.50, 0.35, 1.25, "stricter volume spike"),
    ("S2-05", 3.00, 2.00, 0.25, 1.25, "stricter mean-reversion regime"),
    ("S2-06", 3.00, 2.00, 0.45, 1.25, "looser regime filter"),
    ("S2-07", 3.00, 2.00, 0.35, 1.20, "lower reward floor"),
    ("S2-08", 3.00, 2.00, 0.35, 1.35, "higher reward floor"),
    ("S2-09", 2.75, 1.50, 0.45, 1.20, "permissive/frequency frontier"),
    ("S2-10", 3.25, 2.50, 0.25, 1.35, "selective/quality frontier"),
    ("S2-11", 2.75, 2.50, 0.25, 1.25, "lower z only when volume/regime are strict"),
)


def test_exactly_12_rows_per_strategy_24_total():
    from rob940_signal_manifest import FROZEN_S1_CONFIGS, FROZEN_S2_CONFIGS

    assert len(FROZEN_S1_CONFIGS) == 12
    assert len(FROZEN_S2_CONFIGS) == 12
    assert len(FROZEN_S1_CONFIGS) + len(FROZEN_S2_CONFIGS) == 24


def test_s1_rows_exact_values_and_order():
    from rob940_signal_manifest import FROZEN_S1_CONFIGS

    actual = tuple(
        (c.config_id, c.L, c.q_min, c.k_SL, c.R_TP, c.hypothesis)
        for c in FROZEN_S1_CONFIGS
    )
    assert actual == EXPECTED_S1


def test_s2_rows_exact_values_and_order():
    from rob940_signal_manifest import FROZEN_S2_CONFIGS

    actual = tuple(
        (c.config_id, c.z_min, c.v_min, c.ER_max, c.R_min, c.hypothesis)
        for c in FROZEN_S2_CONFIGS
    )
    assert actual == EXPECTED_S2


def test_get_s1_config_returns_frozen_row():
    cfg = get_s1_config("S1-07")
    assert cfg == S1Config(16, 1.25, 1.25, 1.50, "S1-07", "lower payoff ratio")


def test_get_s2_config_returns_frozen_row():
    cfg = get_s2_config("S2-11")
    assert cfg.z_min == 2.75
    assert cfg.hypothesis == "lower z only when volume/regime are strict"


def test_unregistered_s1_config_id_fails_closed():
    with pytest.raises(KeyError):
        get_s1_config("S1-99")


def test_unregistered_s2_config_id_fails_closed():
    with pytest.raises(KeyError):
        get_s2_config("S2-99")


def test_symbols_are_exactly_four_and_no_btc_only_threshold_path():
    assert SYMBOLS == ("BTCUSDT", "XRPUSDT", "DOGEUSDT", "SOLUSDT")


@pytest.mark.parametrize("symbol", ["BTCUSDT", "XRPUSDT", "DOGEUSDT", "SOLUSDT"])
def test_resolve_s1_config_for_symbol_is_identical_across_symbols(symbol):
    # No symbol-specific override exists: every symbol resolves the SAME
    # config object/values for a given config_id (no BTC-only threshold).
    resolved = resolve_s1_config_for_symbol("S1-00", symbol)
    assert resolved == get_s1_config("S1-00")


def test_resolve_s1_config_for_symbol_rejects_unknown_symbol():
    with pytest.raises(ValueError):
        resolve_s1_config_for_symbol("S1-00", "ETHUSDT")


def test_resolve_s2_config_for_symbol_rejects_unknown_symbol():
    with pytest.raises(ValueError):
        resolve_s2_config_for_symbol("S2-00", "ETHUSDT")


def test_s1_domains_exact():
    assert S1_DOMAINS == {
        "L": (12, 16, 24),
        "q_min": (1.00, 1.25, 1.50),
        "k_SL": (1.00, 1.25, 1.50),
        "R_TP": (1.50, 1.80, 2.00),
    }


def test_s2_domains_exact():
    assert S2_DOMAINS == {
        "z_min": (2.75, 3.00, 3.25),
        "v_min": (1.50, 2.00, 2.50),
        "ER_max": (0.25, 0.35, 0.45),
        "R_min": (1.20, 1.25, 1.35),
    }


def test_validate_s1_configs_rejects_13th_row():
    from rob940_signal_manifest import FROZEN_S1_CONFIGS

    extra = S1Config(16, 1.25, 1.25, 1.80, "S1-12", "unregistered 13th row")
    with pytest.raises(ValueError, match="exactly 12"):
        validate_s1_configs((*FROZEN_S1_CONFIGS, extra))


def test_validate_s2_configs_rejects_13th_row():
    from rob940_signal_manifest import FROZEN_S2_CONFIGS

    extra = S2Config(3.00, 2.00, 0.35, 1.25, "S2-12", "unregistered 13th row")
    with pytest.raises(ValueError, match="exactly 12"):
        validate_s2_configs((*FROZEN_S2_CONFIGS, extra))


def test_validate_s1_configs_rejects_out_of_domain_param():
    from rob940_signal_manifest import FROZEN_S1_CONFIGS

    tampered = dataclasses.replace(FROZEN_S1_CONFIGS[0], L=18)  # not in {12,16,24}
    rows = (tampered, *FROZEN_S1_CONFIGS[1:])
    with pytest.raises(ValueError, match="domain"):
        validate_s1_configs(rows)


def test_validate_s1_configs_rejects_duplicate_config_id():
    from rob940_signal_manifest import FROZEN_S1_CONFIGS

    dup = dataclasses.replace(FROZEN_S1_CONFIGS[1], config_id="S1-00")
    rows = (FROZEN_S1_CONFIGS[0], dup, *FROZEN_S1_CONFIGS[2:])
    with pytest.raises(ValueError, match="duplicate"):
        validate_s1_configs(rows)


def test_frozen_constants_exact():
    c = FrozenSignalConstants
    assert c.ATR_PERIOD == 20
    assert c.VOLUME_MEDIAN_WINDOW == 20
    assert c.A_T_MIN == pytest.approx(0.002)
    assert c.A_T_MAX == pytest.approx(0.012)
    assert c.CHASE_MAX_ATR_MULT == pytest.approx(0.50)
    assert c.S1_SL_CLIP_MIN_BPS == pytest.approx(45.0)
    assert c.S1_SL_CLIP_MAX_BPS == pytest.approx(110.0)
    assert c.S1_TIMEOUT_1M_BARS == 180  # 12 * 15m
    assert c.S1_COOLDOWN_1M_BARS == 60  # 4 * 15m
    assert c.S2_MAD_WINDOW == 288
    assert c.S2_ER_WINDOW == 48
    assert c.S2_SHOCK_ABS_RETURN_MIN == pytest.approx(0.006)
    assert c.S2_SL_CLIP_MIN_BPS == pytest.approx(45.0)
    assert c.S2_SL_CLIP_MAX_BPS == pytest.approx(90.0)
    assert c.S2_TP_MAX_BPS == pytest.approx(120.0)
    assert c.S2_TP_ABS_FLOOR_BPS == pytest.approx(68.0)
    assert c.S2_TIMEOUT_1M_BARS == 30  # 6 * 5m
    assert c.S2_COOLDOWN_1M_BARS == 60  # 1h


def test_signal_manifest_hash_is_deterministic_and_pinned():
    # Pin the exact hash so any future accidental edit to the 24-row
    # manifest (order/values/hypothesis text/symbols) fails closed here
    # instead of silently drifting. Computed once at GREEN time; report as
    # ``signal_manifest_hash`` (NOT an H4 full-campaign hash) if this ever
    # needs to change -- a change here is a new campaign lineage.
    assert (
        signal_manifest_hash
        == "199816d45e79ed52218848dc53c54464754c5befce38dbad6615cf123b628fba"
    )
    assert isinstance(signal_manifest_hash, str)
    assert len(signal_manifest_hash) == 64


def test_signal_manifest_hash_changes_if_any_row_tampered():
    from rob940_signal_manifest import (
        FROZEN_S1_CONFIGS,
        FROZEN_S2_CONFIGS,
        _manifest_payload,
    )

    from research_contracts.canonical_hash import canonical_sha256

    tampered_rows = (
        dataclasses.replace(FROZEN_S1_CONFIGS[0], q_min=1.50),
        *FROZEN_S1_CONFIGS[1:],
    )
    payload = _manifest_payload(tampered_rows, FROZEN_S2_CONFIGS)
    tampered_hash = canonical_sha256(payload)
    assert tampered_hash != signal_manifest_hash

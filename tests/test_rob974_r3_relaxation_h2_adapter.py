from __future__ import annotations

from dataclasses import replace

import pytest
from rob974_h2_dtos import S3Trade, S4PairTrade
from rob974_h4_contracts import exact_h4_folds
from rob974_r3_manifest import FROZEN_R3_ROSTER, R3S3Config, R3S4Config
from rob974_r3_relaxation import RelaxationInputError
from rob974_r3_relaxation_h2_adapter import (
    R3H2CellFoldInput,
    normalize_r3_phase_ledgers,
    normalize_r3_s3_trade,
    normalize_r3_s4_trade,
)


class _S3TradeSubclass(S3Trade):
    pass


def _s3_trade(config: R3S3Config, fold_id: str, signal_ts: int) -> S3Trade:
    return S3Trade(
        symbol="XRPUSDT",
        side="long",
        config_id=config.config_id,
        fold_id=fold_id,
        signal_ts=signal_ts,
        entry_ts=signal_ts + 60_000,
        entry_price=1.0,
        exit_ts=signal_ts + 120_000,
        exit_price=1.01,
        exit_reason="TP",
        mfe_bps=20.0,
        mae_bps=-5.0,
        gross_bps=10.0,
        volatility_percentile=47.5,
    )


def _s4_trade(
    config: R3S4Config,
    fold_id: str,
    signal_ts: int,
    **overrides: object,
) -> S4PairTrade:
    values: dict[str, object] = {
        "pair": ("XRPUSDT", "DOGEUSDT"),
        "side_a": "short",
        "side_b": "long",
        "config_id": config.config_id,
        "fold_id": fold_id,
        "signal_ts": signal_ts,
        "entry_ts": signal_ts + 60_000,
        "weight_a": 0.5,
        "weight_b": 0.5,
        "beta_a": 1.2,
        "beta_b": 0.8,
        "mu": 0.01,
        "sigma": 0.05,
        "z_entry": 1.1,
        "gross_notional": 12.0,
        "entry_price_a": 1.0,
        "entry_price_b": 2.0,
        "exit_ts": signal_ts + 120_000,
        "exit_price_a": 0.99,
        "exit_price_b": 2.02,
        "exit_reason": "MEAN_EXIT",
        "mfe_bps": 30.0,
        "mae_bps": -7.0,
        "gross_bps": 15.0,
        "order_id_a": None,
        "order_id_b": None,
        "pair_exec_status": "historical_atomic_assumption",
        "pair_executor_validated": False,
        "demo_eligible": False,
        "volatility_percentile": None,
        "volatility_percentile_provenance": "not_defined_for_s4",
    }
    values.update(overrides)
    return S4PairTrade(**values)  # type: ignore[arg-type]


def _oos_sources() -> tuple[R3H2CellFoldInput, ...]:
    folds = exact_h4_folds()
    rows: list[R3H2CellFoldInput] = []
    for config in FROZEN_R3_ROSTER:
        for fold in folds:
            signal_ts = fold.oos_start_ms + 60_000
            trades = (
                (_s3_trade(config, fold.fold_id, signal_ts),)
                if type(config) is R3S3Config
                else (_s4_trade(config, fold.fold_id, signal_ts),)
            )
            rows.append(
                R3H2CellFoldInput(
                    config=config,
                    fold_id=fold.fold_id,
                    basket_trade_count=1,
                    trades=trades,
                )
            )
    return tuple(rows)


def test_exact_manifest_major_12x8_builder_normalizes_real_h2_dtos() -> None:
    ledgers = normalize_r3_phase_ledgers(phase="OOS", sources=_oos_sources())
    expected_headers = tuple(
        (config.config_id, fold.fold_id)
        for config in FROZEN_R3_ROSTER
        for fold in exact_h4_folds()
    )
    assert len(ledgers) == 12 * 8 == 96
    assert tuple((row.config_id, row.fold_id) for row in ledgers) == expected_headers

    s3 = ledgers[0].trades[0]
    assert s3.execution.volatility_percentile == 47.5
    assert s3.execution.beta_a is None
    s4 = ledgers[3 * 8].trades[0]
    assert s4.event.direction == "short_a_long_b"
    assert s4.execution.leg_sides == ("short", "long")
    assert s4.execution.gross_notional == 12.0
    assert (
        s4.execution.beta_a,
        s4.execution.beta_b,
        s4.execution.spread_mu,
        s4.execution.spread_sigma,
        s4.execution.observed_z,
    ) == (1.2, 0.8, 0.01, 0.05, 1.1)


def test_config_lineage_is_removed_but_equal_economics_remain_equal() -> None:
    folds = exact_h4_folds()
    fold_id = folds[0].fold_id
    signal_ts = folds[0].oos_start_ms + 60_000
    strict = FROZEN_R3_ROSTER[0]
    loose = FROZEN_R3_ROSTER[2]
    assert type(strict) is type(loose) is R3S3Config
    strict_trade = _s3_trade(strict, fold_id, signal_ts)
    loose_trade = replace(strict_trade, config_id=loose.config_id)
    assert normalize_r3_s3_trade(
        trade=strict_trade, config=strict, fold_id=fold_id
    ) == normalize_r3_s3_trade(trade=loose_trade, config=loose, fold_id=fold_id)


@pytest.mark.parametrize("percentile", (None, -0.1, 100.1))
def test_s3_requires_real_h3_volatility_percentile(
    percentile: float | None,
) -> None:
    config = FROZEN_R3_ROSTER[0]
    assert type(config) is R3S3Config
    fold = exact_h4_folds()[0]
    trade = _s3_trade(config, fold.fold_id, fold.oos_start_ms + 60_000)
    with pytest.raises(RelaxationInputError, match=r"H3 value in \[0,100\]"):
        normalize_r3_s3_trade(
            trade=replace(trade, volatility_percentile=percentile),
            config=config,
            fold_id=fold.fold_id,
        )


def test_adapter_rejects_wrong_family_lineage_order_and_phase_window() -> None:
    sources = _oos_sources()
    with pytest.raises(TypeError, match="exact built-in tuple"):
        normalize_r3_phase_ledgers(phase="OOS", sources=list(sources))
    with pytest.raises(RelaxationInputError, match="manifest-major 12x8"):
        normalize_r3_phase_ledgers(
            phase="OOS", sources=(sources[1], sources[0], *sources[2:])
        )
    with pytest.raises(ValueError, match="TRAIN or OOS"):
        normalize_r3_phase_ledgers(phase="oos", sources=sources)

    s3_source = sources[0]
    s3_trade = s3_source.trades[0]
    assert type(s3_trade) is S3Trade
    with pytest.raises(RelaxationInputError, match="config_id"):
        normalize_r3_s3_trade(
            trade=replace(s3_trade, config_id="S3-R3-02"),
            config=s3_source.config,
            fold_id=s3_source.fold_id,
        )
    with pytest.raises(RelaxationInputError, match="fold_id"):
        normalize_r3_s3_trade(
            trade=s3_trade,
            config=s3_source.config,
            fold_id="fold-01",
        )
    with pytest.raises(TypeError, match="exact H2 S3Trade"):
        normalize_r3_s3_trade(
            trade=sources[3 * 8].trades[0],
            config=s3_source.config,
            fold_id=s3_source.fold_id,
        )
    with pytest.raises(TypeError, match="exact H2 S3Trade"):
        normalize_r3_s3_trade(
            trade=_S3TradeSubclass(**s3_trade.__dict__),
            config=s3_source.config,
            fold_id=s3_source.fold_id,
        )

    bad_time = replace(s3_trade, signal_ts=0, entry_ts=1, exit_ts=2)
    bad_source = replace(s3_source, trades=(bad_time,))
    with pytest.raises(RelaxationInputError, match="outside the exact OOS"):
        normalize_r3_phase_ledgers(phase="OOS", sources=(bad_source, *sources[1:]))

    fold = exact_h4_folds()[0]
    end_exclusive = replace(s3_trade, exit_ts=fold.oos_end_ms)
    end_source = replace(s3_source, trades=(end_exclusive,))
    with pytest.raises(RelaxationInputError, match="outside the exact OOS"):
        normalize_r3_phase_ledgers(phase="OOS", sources=(end_source, *sources[1:]))


def test_s4_pair_direction_order_and_deterministic_notional_fail_closed() -> None:
    config = FROZEN_R3_ROSTER[3]
    assert type(config) is R3S4Config
    fold = exact_h4_folds()[0]
    trade = _s4_trade(config, fold.fold_id, fold.oos_start_ms + 60_000)
    with pytest.raises(RelaxationInputError, match="canonical H3 pair order"):
        normalize_r3_s4_trade(
            trade=replace(trade, pair=("DOGEUSDT", "XRPUSDT")),
            config=config,
            fold_id=fold.fold_id,
        )
    with pytest.raises(RelaxationInputError, match="opposing sides"):
        normalize_r3_s4_trade(
            trade=replace(trade, side_b="short"),
            config=config,
            fold_id=fold.fold_id,
        )
    with pytest.raises(RelaxationInputError, match="observed z sign"):
        normalize_r3_s4_trade(
            trade=replace(trade, z_entry=-1.1),
            config=config,
            fold_id=fold.fold_id,
        )
    with pytest.raises(RelaxationInputError, match="below its R3 cell threshold"):
        normalize_r3_s4_trade(
            trade=replace(trade, z_entry=1.0),
            config=config,
            fold_id=fold.fold_id,
        )
    with pytest.raises(RelaxationInputError, match="deterministic G_min"):
        normalize_r3_s4_trade(
            trade=replace(trade, gross_notional=16.0),
            config=config,
            fold_id=fold.fold_id,
        )


def test_h2_currently_fails_closed_for_sub_one_observed_z_without_clamping() -> None:
    """Document the pre-existing H2 DTO seam; M3 must not rewrite or clamp it."""

    config = FROZEN_R3_ROSTER[8]
    assert type(config) is R3S4Config and config.z_entry == 0.8
    fold = exact_h4_folds()[0]
    with pytest.raises(ValueError, match="magnitude >= 1.0"):
        _s4_trade(
            config,
            fold.fold_id,
            fold.oos_start_ms + 60_000,
            z_entry=0.8,
        )

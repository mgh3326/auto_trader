from __future__ import annotations

import math
from dataclasses import replace
from hashlib import sha256

import pytest
from rob974_r3_manifest import R3_RELAXATION_RAYS as MANIFEST_RELAXATION_RAYS
from rob974_r3_relaxation import (
    BOOTSTRAP_RESAMPLES,
    R3_CONFIG_IDS,
    R3_FOLD_IDS,
    R3_RELAXATION_RAYS,
    CellFoldLedger,
    EconomicEvent,
    RelaxationInputError,
    RelaxationTrade,
    TradeExecution,
    analyze_relaxation_campaign,
)

CAMPAIGN_HASH = "a91d467635a70b10b70b26af0bbc5f72abf16fd6b8f59a794a2c21f6de29a031"
OOS_STARTS_MS = (
    1_761_706_800_000,
    1_764_126_000_000,
    1_766_545_200_000,
    1_768_964_400_000,
    1_771_383_600_000,
    1_773_802_800_000,
    1_776_222_000_000,
    1_778_641_200_000,
)
TRAIN_STARTS_MS = (
    1_751_328_000_000,
    1_753_747_200_000,
    1_756_166_400_000,
    1_758_585_600_000,
    1_761_004_800_000,
    1_763_424_000_000,
    1_765_843_200_000,
    1_768_262_400_000,
)
TRAIN_ENDS_MS = (
    1_761_696_000_000,
    1_764_115_200_000,
    1_766_534_400_000,
    1_768_953_600_000,
    1_771_372_800_000,
    1_773_792_000_000,
    1_776_211_200_000,
    1_778_630_400_000,
)
OVERLAPPING_TRAIN_SIGNAL_START_MS = 1_755_000_000_000


def _trade(
    *,
    family: str,
    instruments: tuple[str, ...],
    fold_index: int,
    event_index: int,
    gross_bps: float,
    signal_start_ms: int | None = None,
) -> RelaxationTrade:
    signal_ts = (
        OOS_STARTS_MS[fold_index] if signal_start_ms is None else signal_start_ms
    ) + event_index * 60_000
    leg_count = len(instruments)
    direction = (
        ("long" if event_index % 2 == 0 else "short")
        if family == "S3"
        else ("long_a_short_b" if event_index % 2 == 0 else "short_a_long_b")
    )
    leg_sides = (
        (direction,)
        if family == "S3"
        else (("long", "short") if direction == "long_a_short_b" else ("short", "long"))
    )
    return RelaxationTrade(
        event=EconomicEvent(
            family=family,
            instruments=instruments,
            signal_ts=signal_ts,
            direction=direction,
        ),
        execution=TradeExecution(
            entry_ts=signal_ts + 60_000,
            exit_ts=signal_ts + 300_000,
            leg_sides=leg_sides,
            entry_prices=tuple(100.0 + leg for leg in range(leg_count)),
            exit_prices=tuple(101.0 + leg for leg in range(leg_count)),
            leg_weights=(1.0,) if leg_count == 1 else (0.5, 0.5),
            gross_notional=None if family == "S3" else 12.0,
            mfe_bps=30.0,
            mae_bps=-10.0,
            gross_bps=gross_bps,
            exit_reason="TP",
            volatility_percentile=50.0 if family == "S3" else None,
            beta_a=None if family == "S3" else 1.0,
            beta_b=None if family == "S3" else 1.0,
            spread_mu=None if family == "S3" else 0.0,
            spread_sigma=None if family == "S3" else 1.0,
            observed_z=(
                None
                if family == "S3"
                else (1.1 if direction == "short_a_long_b" else -1.1)
            ),
        ),
    )


def _cell_trade_map(
    fold_index: int, *, signal_start_ms: int | None = None
) -> dict[str, tuple[RelaxationTrade, ...]]:
    def trades(
        family: str,
        instruments: tuple[str, ...],
        event_indices: tuple[int, ...],
    ) -> tuple[RelaxationTrade, ...]:
        return tuple(
            _trade(
                family=family,
                instruments=instruments,
                fold_index=fold_index,
                event_index=event_index,
                gross_bps=(20.0 if event_index < 5 else -100.0 + 10.0 * fold_index),
                signal_start_ms=signal_start_ms,
            )
            for event_index in event_indices
        )

    base = (0, 1, 2, 3, 4)
    s3_a = (5,)
    s3_b = (6,)
    # S4 is an additive lattice. Relaxing z adds 10/11/12; relaxing d adds
    # 20/21. Every frozen directed edge is therefore an exact set inclusion.
    z_layers = {1.1: (), 1.0: (10,), 0.8: (10, 11), 0.6: (10, 11, 12)}
    d_layers = {180: (), 160: (20,), 140: (20, 21)}
    s4_coordinates = {
        "S4-R3-00": (1.1, 140),
        "S4-R3-01": (1.0, 160),
        "S4-R3-02": (1.0, 140),
        "S4-R3-03": (0.8, 180),
        "S4-R3-04": (0.8, 160),
        "S4-R3-05": (0.8, 140),
        "S4-R3-06": (0.6, 180),
        "S4-R3-07": (0.6, 160),
        "S4-R3-08": (0.6, 140),
    }
    result = {
        "S3-R3-00": trades("S3", ("XRPUSDT",), base + s3_a),
        "S3-R3-01": trades("S3", ("XRPUSDT",), base + s3_b),
        "S3-R3-02": trades("S3", ("XRPUSDT",), base + s3_a + s3_b),
    }
    for config_id, (z_entry, d_min) in s4_coordinates.items():
        result[config_id] = trades(
            "S4",
            ("XRPUSDT", "DOGEUSDT"),
            base + z_layers[z_entry] + d_layers[d_min],
        )
    return result


def _ledgers() -> tuple[CellFoldLedger, ...]:
    by_fold = tuple(_cell_trade_map(index) for index in range(8))
    return tuple(
        CellFoldLedger(
            config_id=config_id,
            fold_id=fold_id,
            basket_trade_count=len(by_fold[fold_index][config_id]),
            trades=by_fold[fold_index][config_id],
        )
        for config_id in R3_CONFIG_IDS
        for fold_index, fold_id in enumerate(R3_FOLD_IDS)
    )


def _train_ledgers_with_real_overlap() -> tuple[CellFoldLedger, ...]:
    by_fold = (
        _cell_trade_map(0, signal_start_ms=OVERLAPPING_TRAIN_SIGNAL_START_MS),
        _cell_trade_map(0, signal_start_ms=OVERLAPPING_TRAIN_SIGNAL_START_MS),
        *(
            _cell_trade_map(
                index,
                signal_start_ms=TRAIN_STARTS_MS[index] + 3_600_000,
            )
            for index in range(2, 8)
        ),
    )
    return tuple(
        CellFoldLedger(
            config_id=config_id,
            fold_id=fold_id,
            basket_trade_count=len(by_fold[fold_index][config_id]),
            trades=by_fold[fold_index][config_id],
        )
        for config_id in R3_CONFIG_IDS
        for fold_index, fold_id in enumerate(R3_FOLD_IDS)
    )


def _replace_ledger(
    ledgers: tuple[CellFoldLedger, ...],
    config_id: str,
    fold_id: str,
    *,
    trades: tuple[RelaxationTrade, ...],
) -> tuple[CellFoldLedger, ...]:
    return tuple(
        CellFoldLedger(
            config_id=row.config_id,
            fold_id=row.fold_id,
            basket_trade_count=len(trades),
            trades=trades,
        )
        if (row.config_id, row.fold_id) == (config_id, fold_id)
        else row
        for row in ledgers
    )


def _replace_event_gross(
    ledgers: tuple[CellFoldLedger, ...],
    *,
    config_ids: tuple[str, ...],
    event_index: int,
    gross_by_fold: tuple[float, ...],
) -> tuple[CellFoldLedger, ...]:
    result = ledgers
    for fold_index, fold_id in enumerate(R3_FOLD_IDS):
        for config_id in config_ids:
            row = next(
                item
                for item in result
                if (item.config_id, item.fold_id) == (config_id, fold_id)
            )
            signal_ts = OOS_STARTS_MS[fold_index] + event_index * 60_000
            changed = tuple(
                replace(
                    trade,
                    execution=replace(
                        trade.execution, gross_bps=gross_by_fold[fold_index]
                    ),
                )
                if trade.event.signal_ts == signal_ts
                else trade
                for trade in row.trades
            )
            assert changed != row.trades
            result = _replace_ledger(result, config_id, fold_id, trades=changed)
    return result


def test_frozen_ray_graph_is_exact_and_caller_cannot_supply_edges() -> None:
    assert R3_RELAXATION_RAYS is MANIFEST_RELAXATION_RAYS
    assert tuple((ray.ray_id, ray.config_ids) for ray in R3_RELAXATION_RAYS) == (
        ("S3-S-M0", ("S3-R3-00", "S3-R3-02")),
        ("S3-M-S0", ("S3-R3-01", "S3-R3-02")),
        (
            "S4-Z-D140",
            ("S4-R3-00", "S4-R3-02", "S4-R3-05", "S4-R3-08"),
        ),
        ("S4-Z-D160", ("S4-R3-01", "S4-R3-04", "S4-R3-07")),
        ("S4-Z-D180", ("S4-R3-03", "S4-R3-06")),
        ("S4-D-Z1.0", ("S4-R3-01", "S4-R3-02")),
        ("S4-D-Z0.8", ("S4-R3-03", "S4-R3-04", "S4-R3-05")),
        ("S4-D-Z0.6", ("S4-R3-06", "S4-R3-07", "S4-R3-08")),
    )


def test_production_shaped_e2e_separates_cohorts_and_monotone_evidence() -> None:
    report = analyze_relaxation_campaign(
        campaign_hash=CAMPAIGN_HASH,
        oos_ledgers=_ledgers(),
    )

    assert report.operational_status == "COMPLETE"
    assert report.train_diagnostic is None
    assert report.oos.fold_ids == tuple(f"fold-{index:02d}" for index in range(8))
    assert tuple(ray.ray_id for ray in report.oos.rays) == tuple(
        ray.ray_id for ray in R3_RELAXATION_RAYS
    )
    d140 = next(ray for ray in report.oos.rays if ray.ray_id == "S4-Z-D140")
    assert d140.all_pooled_deltas_nonpositive is True
    assert d140.two_steps_seven_of_eight_negative is True
    assert d140.all_new_layers_below_strict_core is True
    assert d140.monotone_edge_decay is True
    assert len(d140.steps) == 3
    first_fold = d140.steps[0].folds[0]
    assert len(first_fold.strict_core) == 7
    assert len(first_fold.cumulative_looser) == 8
    assert len(first_fold.new_layer) == 1
    assert first_fold.new_layer[0].execution.gross_bps == -100.0
    assert d140.steps[0].paired_delta_e0_bps < 0.0
    assert d140.steps[0].new_layer_e0_bps < d140.steps[0].strict_core_e0_bps
    assert d140.steps[0].sign_test.negative_count == 8
    assert d140.steps[0].sign_test.tie_count == 0
    assert d140.steps[0].sign_test.effective_n == 8
    assert d140.steps[0].sign_test.p_value_numerator == 1
    assert d140.steps[0].sign_test.p_value_denominator == 256
    assert d140.steps[0].sign_test.p_value == 1 / 256
    assert d140.steps[0].bootstrap.resamples == BOOTSTRAP_RESAMPLES == 10_000
    assert d140.steps[0].bootstrap.resampling_unit == "paired_fold_blocks"
    assert d140.steps[0].bootstrap.fold_block_count == 8

    # One-step rays cannot independently satisfy the frozen two-adjacent-step
    # requirement, even when their sole edge is negative in all eight folds.
    one_step = {"S3-S-M0", "S3-M-S0", "S4-Z-D180", "S4-D-Z1.0"}
    assert all(
        ray.monotone_edge_decay is False
        for ray in report.oos.rays
        if ray.ray_id in one_step
    )
    assert not hasattr(report, "winner")
    assert not hasattr(report.oos, "selected_config_id")


def test_normalized_trade_seam_matches_frozen_h2_production_shapes() -> None:
    ledgers = _ledgers()
    for fold_index, fold_id in enumerate(R3_FOLD_IDS):
        s3 = next(
            row
            for row in ledgers
            if (row.config_id, row.fold_id) == ("S3-R3-00", fold_id)
        ).trades[0]
        s4 = next(
            row
            for row in ledgers
            if (row.config_id, row.fold_id) == ("S4-R3-00", fold_id)
        ).trades[0]
        assert s3.event.signal_ts == OOS_STARTS_MS[fold_index]
        assert s3.event.direction == "long"
        assert s3.execution.leg_sides == ("long",)
        assert s3.execution.gross_notional is None
        assert s4.event.signal_ts == OOS_STARTS_MS[fold_index]
        assert s4.event.direction == "long_a_short_b"
        assert s4.execution.leg_sides == ("long", "short")
        assert s4.execution.gross_notional == 12.0
        assert tuple(
            weight * s4.execution.gross_notional for weight in s4.execution.leg_weights
        ) == (6.0, 6.0)


def test_family_specific_execution_shape_is_fail_closed() -> None:
    s3 = _ledgers()[0].trades[0]
    s4 = next(row for row in _ledgers() if row.config_id == "S4-R3-00").trades[0]
    with pytest.raises(ValueError, match="S3 gross_notional"):
        replace(s3, execution=replace(s3.execution, gross_notional=10.0))
    with pytest.raises(ValueError, match="S4 gross_notional"):
        replace(s4, execution=replace(s4.execution, gross_notional=None))
    with pytest.raises(ValueError, match="S4 direction"):
        replace(s4.event, direction="long")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="direction and opposing"):
        replace(
            s4,
            execution=replace(s4.execution, leg_sides=("short", "long")),
        )
    with pytest.raises(ValueError, match=r"frozen \$6-10"):
        replace(s4, execution=replace(s4.execution, gross_notional=10.0))
    with pytest.raises(ValueError, match="S3 exit_reason"):
        replace(s3, execution=replace(s3.execution, exit_reason="MEAN_EXIT"))
    with pytest.raises(ValueError, match="S4 exit_reason"):
        replace(s4, execution=replace(s4.execution, exit_reason="THESIS_EXIT"))

    # H2 accepts binary-float weight sums within 1e-9 rather than demanding
    # exact equality. Both resulting S4 legs remain inside $6-10.
    tolerated = replace(
        s4.execution,
        leg_weights=(0.5000000004, 0.4999999995),
        gross_notional=12.1,
    )
    assert replace(s4, execution=tolerated).execution == tolerated


def test_train_layers_are_diagnostic_and_never_emit_verdict_flag() -> None:
    report = analyze_relaxation_campaign(
        campaign_hash=CAMPAIGN_HASH,
        oos_ledgers=_ledgers(),
        train_ledgers=_ledgers(),
    )

    assert report.train_diagnostic is not None
    assert report.train_diagnostic.phase == "TRAIN"
    assert all(
        (
            ray.all_pooled_deltas_nonpositive,
            ray.two_steps_seven_of_eight_negative,
            ray.all_new_layers_below_strict_core,
            ray.monotone_edge_decay,
        )
        == (None, None, None, None)
        for ray in report.train_diagnostic.rays
    )
    assert all(ray.monotone_edge_decay is not None for ray in report.oos.rays)


def test_overlapping_train_folds_may_repeat_the_exact_economic_event() -> None:
    train_ledgers = _train_ledgers_with_real_overlap()
    fold_00 = train_ledgers[0]
    fold_01 = train_ledgers[1]
    assert fold_00.config_id == fold_01.config_id == "S3-R3-00"
    assert fold_00.trades == fold_01.trades
    signal_ts = fold_00.trades[0].event.signal_ts
    assert TRAIN_STARTS_MS[0] <= signal_ts < TRAIN_ENDS_MS[0]
    assert TRAIN_STARTS_MS[1] <= signal_ts < TRAIN_ENDS_MS[1]

    report = analyze_relaxation_campaign(
        campaign_hash=CAMPAIGN_HASH,
        oos_ledgers=_ledgers(),
        train_ledgers=train_ledgers,
    )
    assert report.operational_status == "COMPLETE"
    assert report.train_diagnostic is not None
    assert report.train_diagnostic.operational_status == "COMPLETE"


def test_opposite_direction_cannot_be_laundered_as_a_new_layer() -> None:
    ledgers = _ledgers()
    looser = next(
        row
        for row in ledgers
        if (row.config_id, row.fold_id) == ("S3-R3-02", "fold-00")
    )
    shared = looser.trades[0]
    opposite = replace(
        shared,
        event=replace(shared.event, direction="short"),
        execution=replace(shared.execution, leg_sides=("short",)),
    )
    with pytest.raises(RelaxationInputError, match="multiple directions"):
        analyze_relaxation_campaign(
            campaign_hash=CAMPAIGN_HASH,
            oos_ledgers=_replace_ledger(
                ledgers,
                looser.config_id,
                looser.fold_id,
                trades=(*looser.trades, opposite),
            ),
        )


def test_direction_reversal_across_configs_in_one_fold_is_rejected() -> None:
    ledgers = _ledgers()
    looser = next(
        row
        for row in ledgers
        if (row.config_id, row.fold_id) == ("S3-R3-02", "fold-00")
    )
    shared = looser.trades[0]
    reversed_trade = replace(
        shared,
        event=replace(shared.event, direction="short"),
        execution=replace(shared.execution, leg_sides=("short",)),
    )
    with pytest.raises(RelaxationInputError, match="direction drift"):
        analyze_relaxation_campaign(
            campaign_hash=CAMPAIGN_HASH,
            oos_ledgers=_replace_ledger(
                ledgers,
                looser.config_id,
                looser.fold_id,
                trades=(reversed_trade, *looser.trades[1:]),
            ),
        )


def test_core_economic_drift_is_operational_incomplete_not_a_new_layer() -> None:
    ledgers = _ledgers()
    looser = next(
        row
        for row in ledgers
        if (row.config_id, row.fold_id) == ("S4-R3-02", "fold-00")
    )
    shared = looser.trades[0]
    drifted = replace(
        shared,
        execution=replace(shared.execution, gross_bps=21.0),
    )
    mutated = _replace_ledger(
        ledgers,
        "S4-R3-02",
        "fold-00",
        trades=(drifted, *looser.trades[1:]),
    )

    report = analyze_relaxation_campaign(
        campaign_hash=CAMPAIGN_HASH,
        oos_ledgers=mutated,
    )

    assert report.operational_status == "INCOMPLETE"
    assert report.incomplete_reasons == ("core_trade_drift",)
    assert all(ray.monotone_edge_decay is None for ray in report.oos.rays)
    affected = next(ray for ray in report.oos.rays if ray.ray_id == "S4-Z-D140")
    assert affected.steps[0].operational_status == "INCOMPLETE"
    assert affected.steps[0].incomplete_reason == "core_trade_drift"
    assert affected.steps[0].folds[0].new_layer == ()


@pytest.mark.parametrize(
    ("config_id", "field", "value"),
    (
        ("S3-R3-02", "volatility_percentile", 51.0),
        ("S4-R3-02", "beta_a", 1.1),
        ("S4-R3-02", "beta_b", 1.1),
        ("S4-R3-02", "spread_mu", 0.1),
        ("S4-R3-02", "spread_sigma", 1.1),
        ("S4-R3-02", "observed_z", -1.2),
    ),
)
def test_family_entry_economics_are_part_of_core_bytes(
    config_id: str, field: str, value: float
) -> None:
    ledgers = _ledgers()
    row = next(
        item
        for item in ledgers
        if (item.config_id, item.fold_id) == (config_id, "fold-00")
    )
    drifted = replace(
        row.trades[0],
        execution=replace(row.trades[0].execution, **{field: value}),
    )
    report = analyze_relaxation_campaign(
        campaign_hash=CAMPAIGN_HASH,
        oos_ledgers=_replace_ledger(
            ledgers,
            config_id,
            "fold-00",
            trades=(drifted, *row.trades[1:]),
        ),
    )
    assert report.operational_status == "INCOMPLETE"
    assert "core_trade_drift" in report.incomplete_reasons
    assert all(
        (
            ray.all_pooled_deltas_nonpositive,
            ray.two_steps_seven_of_eight_negative,
            ray.all_new_layers_below_strict_core,
            ray.monotone_edge_decay,
        )
        == (None, None, None, None)
        for ray in report.oos.rays
    )


def test_condition_one_has_an_independent_multi_step_mutant() -> None:
    mutated = _replace_event_gross(
        _ledgers(),
        config_ids=("S4-R3-06", "S4-R3-07", "S4-R3-08"),
        event_index=12,
        gross_by_fold=(200.0,) * 8,
    )
    report = analyze_relaxation_campaign(
        campaign_hash=CAMPAIGN_HASH, oos_ledgers=mutated
    )
    ray = next(item for item in report.oos.rays if item.ray_id == "S4-Z-D140")
    assert ray.all_pooled_deltas_nonpositive is False
    assert ray.two_steps_seven_of_eight_negative is True
    assert ray.all_new_layers_below_strict_core is False
    assert ray.monotone_edge_decay is False


def test_condition_two_has_an_independent_multi_step_mutant() -> None:
    mutated = _replace_event_gross(
        _ledgers(),
        config_ids=tuple(f"S4-R3-{index:02d}" for index in range(1, 9)),
        event_index=10,
        gross_by_fold=(30.0, 30.0, -100.0, -100.0, -100.0, -100.0, -100.0, -100.0),
    )
    mutated = _replace_event_gross(
        mutated,
        config_ids=tuple(f"S4-R3-{index:02d}" for index in range(3, 9)),
        event_index=11,
        gross_by_fold=(30.0, 30.0, -100.0, -100.0, -100.0, -100.0, -100.0, -100.0),
    )
    report = analyze_relaxation_campaign(
        campaign_hash=CAMPAIGN_HASH, oos_ledgers=mutated
    )
    ray = next(item for item in report.oos.rays if item.ray_id == "S4-Z-D140")
    assert ray.all_pooled_deltas_nonpositive is True
    assert ray.two_steps_seven_of_eight_negative is False
    assert ray.all_new_layers_below_strict_core is True
    assert ray.monotone_edge_decay is False


def test_condition_three_strict_equality_has_an_independent_mutant() -> None:
    baseline = _ledgers()
    strict_means = tuple(
        sum(
            trade.execution.gross_bps
            for trade in next(
                row
                for row in baseline
                if (row.config_id, row.fold_id) == ("S4-R3-00", fold_id)
            ).trades
        )
        / len(
            next(
                row
                for row in baseline
                if (row.config_id, row.fold_id) == ("S4-R3-00", fold_id)
            ).trades
        )
        for fold_id in R3_FOLD_IDS
    )
    mutated = _replace_event_gross(
        baseline,
        config_ids=tuple(f"S4-R3-{index:02d}" for index in range(1, 9)),
        event_index=10,
        gross_by_fold=strict_means,
    )
    report = analyze_relaxation_campaign(
        campaign_hash=CAMPAIGN_HASH, oos_ledgers=mutated
    )
    ray = next(item for item in report.oos.rays if item.ray_id == "S4-Z-D140")
    assert ray.all_pooled_deltas_nonpositive is True
    assert ray.two_steps_seven_of_eight_negative is True
    assert ray.all_new_layers_below_strict_core is False
    assert ray.monotone_edge_decay is False


def test_core_byte_equivalence_distinguishes_positive_and_negative_zero() -> None:
    ledgers = _ledgers()
    for config_id, gross_bps in (
        ("S3-R3-00", 0.0),
        ("S3-R3-01", -0.0),
        ("S3-R3-02", -0.0),
    ):
        row = next(
            item
            for item in ledgers
            if (item.config_id, item.fold_id) == (config_id, "fold-00")
        )
        changed = replace(
            row.trades[0],
            execution=replace(row.trades[0].execution, gross_bps=gross_bps),
        )
        ledgers = _replace_ledger(
            ledgers,
            config_id,
            "fold-00",
            trades=(changed, *row.trades[1:]),
        )

    report = analyze_relaxation_campaign(
        campaign_hash=CAMPAIGN_HASH,
        oos_ledgers=ledgers,
    )
    assert report.operational_status == "INCOMPLETE"
    assert "core_trade_drift" in report.incomplete_reasons


def test_strict_trade_missing_from_looser_is_direction_drift() -> None:
    ledgers = _ledgers()
    looser = next(
        row
        for row in ledgers
        if (row.config_id, row.fold_id) == ("S3-R3-02", "fold-00")
    )
    mutated = _replace_ledger(
        ledgers,
        "S3-R3-02",
        "fold-00",
        trades=looser.trades[1:],
    )

    report = analyze_relaxation_campaign(
        campaign_hash=CAMPAIGN_HASH,
        oos_ledgers=mutated,
    )
    assert report.operational_status == "INCOMPLETE"
    assert "strict_looser_direction_drift" in report.incomplete_reasons
    assert all(ray.monotone_edge_decay is None for ray in report.oos.rays)


def test_only_both_sample_qualified_folds_enter_paired_statistics() -> None:
    ledgers = _ledgers()
    strict = next(
        row
        for row in ledgers
        if (row.config_id, row.fold_id) == ("S4-R3-00", "fold-00")
    )
    looser = next(
        row
        for row in ledgers
        if (row.config_id, row.fold_id) == ("S4-R3-02", "fold-01")
    )
    strict_fold_01 = next(
        row
        for row in ledgers
        if (row.config_id, row.fold_id) == ("S4-R3-00", "fold-01")
    )
    ledgers = _replace_ledger(
        ledgers,
        "S4-R3-00",
        "fold-00",
        trades=strict.trades[:4],
    )
    ledgers = _replace_ledger(
        ledgers,
        "S4-R3-00",
        "fold-01",
        trades=strict_fold_01.trades[:4],
    )
    ledgers = _replace_ledger(
        ledgers,
        "S4-R3-02",
        "fold-01",
        trades=looser.trades[:4],
    )

    report = analyze_relaxation_campaign(
        campaign_hash=CAMPAIGN_HASH,
        oos_ledgers=ledgers,
    )
    step = next(ray for ray in report.oos.rays if ray.ray_id == "S4-Z-D140").steps[0]
    assert step.comparable_fold_ids == tuple(
        f"fold-{index:02d}" for index in range(2, 8)
    )
    assert tuple((fold.fold_id, fold.exclusion_reason) for fold in step.folds[:2]) == (
        ("fold-00", "strict_basket_trades_below_5"),
        ("fold-01", "both_basket_trades_below_5"),
    )
    assert step.sign_test.effective_n == 6
    assert step.seven_of_eight_negative is False


def test_ties_are_excluded_from_one_sided_less_sign_test() -> None:
    ledgers = _ledgers()
    for fold_id in ("fold-00", "fold-01"):
        strict = next(
            row
            for row in ledgers
            if (row.config_id, row.fold_id) == ("S4-R3-00", fold_id)
        )
        # The cumulative loose ledger equals the strict core, so Delta E0 is
        # exactly zero and must not count as negative.
        ledgers = _replace_ledger(
            ledgers,
            "S4-R3-02",
            fold_id,
            trades=strict.trades,
        )

    report = analyze_relaxation_campaign(
        campaign_hash=CAMPAIGN_HASH,
        oos_ledgers=ledgers,
    )
    step = next(ray for ray in report.oos.rays if ray.ray_id == "S4-Z-D140").steps[0]
    assert step.sign_test.negative_count == 6
    assert step.sign_test.tie_count == 2
    assert step.sign_test.effective_n == 6
    assert step.sign_test.p_value == 1 / 64
    assert step.seven_of_eight_negative is False


def test_bootstrap_seed_and_ci_are_canonical_and_deterministic() -> None:
    first = analyze_relaxation_campaign(
        campaign_hash=CAMPAIGN_HASH,
        oos_ledgers=_ledgers(),
    )
    second = analyze_relaxation_campaign(
        campaign_hash=CAMPAIGN_HASH,
        oos_ledgers=_ledgers(),
    )
    step = next(ray for ray in first.oos.rays if ray.ray_id == "S4-Z-D140").steps[0]
    repeated = next(ray for ray in second.oos.rays if ray.ray_id == "S4-Z-D140").steps[
        0
    ]
    seed_material = (
        "rob974.r3.relaxation.bootstrap.v1\x00"
        f"{CAMPAIGN_HASH}\x00S4-Z-D140\x00S4-R3-00->S4-R3-02"
    ).encode()
    assert step.bootstrap.seed_sha256 == sha256(seed_material).hexdigest()
    assert step.bootstrap == repeated.bootstrap
    assert step.bootstrap.ci_lower_bps <= step.bootstrap.ci_upper_bps
    # Golden bounds come from resampling the eight heterogeneous paired fold
    # blocks. Resampling individual trades produces different, artificially
    # narrower bounds and is therefore caught by this assertion.
    assert step.bootstrap.ci_lower_bps == pytest.approx(-9.040178571428571)
    assert step.bootstrap.ci_upper_bps == pytest.approx(-6.138392857142857)


@pytest.mark.parametrize(
    "campaign_hash",
    ["abc", "A" * 64, "0" * 64, "g" * 64, True],
)
def test_campaign_hash_must_be_a_real_full_lowercase_sha(campaign_hash: object) -> None:
    with pytest.raises((TypeError, ValueError, RelaxationInputError)):
        analyze_relaxation_campaign(
            campaign_hash=campaign_hash,
            oos_ledgers=_ledgers(),
        )


def test_full_ledger_roster_and_order_are_fail_closed() -> None:
    ledgers = _ledgers()
    with pytest.raises(RelaxationInputError, match="exact canonical 12x8 order"):
        analyze_relaxation_campaign(
            campaign_hash=CAMPAIGN_HASH,
            oos_ledgers=(ledgers[1], ledgers[0], *ledgers[2:]),
        )
    with pytest.raises(RelaxationInputError, match="exact canonical 12x8 order"):
        analyze_relaxation_campaign(
            campaign_hash=CAMPAIGN_HASH,
            oos_ledgers=ledgers[:-1],
        )
    non_r3 = replace(ledgers[0], config_id="S3-R3-99")
    with pytest.raises(RelaxationInputError, match="exact canonical 12x8 order"):
        analyze_relaxation_campaign(
            campaign_hash=CAMPAIGN_HASH,
            oos_ledgers=(non_r3, *ledgers[1:]),
        )


def test_duplicate_trade_and_cross_fold_identity_are_rejected() -> None:
    ledgers = _ledgers()
    row = ledgers[0]
    with pytest.raises(RelaxationInputError, match="duplicate trade identity"):
        analyze_relaxation_campaign(
            campaign_hash=CAMPAIGN_HASH,
            oos_ledgers=_replace_ledger(
                ledgers,
                row.config_id,
                row.fold_id,
                trades=(*row.trades, row.trades[0]),
            ),
        )

    target = next(
        item
        for item in ledgers
        if (item.config_id, item.fold_id) == ("S3-R3-00", "fold-01")
    )
    collision = replace(target.trades[0], event=ledgers[0].trades[0].event)
    with pytest.raises(RelaxationInputError, match="cross-fold trade identity"):
        analyze_relaxation_campaign(
            campaign_hash=CAMPAIGN_HASH,
            oos_ledgers=_replace_ledger(
                ledgers,
                target.config_id,
                target.fold_id,
                trades=(collision, *target.trades[1:]),
            ),
        )


def test_numeric_bool_nonfinite_and_config_lineage_in_identity_are_rejected() -> None:
    with pytest.raises(TypeError, match="gross_bps"):
        replace(_ledgers()[0].trades[0].execution, gross_bps=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="gross_bps"):
        replace(_ledgers()[0].trades[0].execution, gross_bps=math.inf)
    with pytest.raises(ValueError, match="frozen instrument"):
        replace(
            _ledgers()[0].trades[0].event,
            instruments=("S3-R3-00",),
        )
    with pytest.raises(TypeError, match="basket_trade_count"):
        replace(_ledgers()[0], basket_trade_count=True)  # type: ignore[arg-type]


def test_basket_count_must_match_the_exact_trade_ledger() -> None:
    row = _ledgers()[0]
    with pytest.raises(RelaxationInputError, match="basket_trade_count"):
        CellFoldLedger(
            config_id=row.config_id,
            fold_id=row.fold_id,
            basket_trade_count=len(row.trades) + 1,
            trades=row.trades,
        )

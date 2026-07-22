"""ROB-974 R3 low-z execution amendment and frozen-R2 parity tests."""

from __future__ import annotations

import dataclasses
import hashlib
import inspect
import math
import textwrap
from pathlib import Path

import pytest
import rob974_h2_s4_engine as frozen_s4_engine
import rob974_r3_s4_engine as r3_s4_engine
from rob974_h2_dtos import (
    MinuteBar,
    S3EngineResult,
    S4PairLegClose,
    S4PairSignalIntent,
)
from rob974_h2_ingress import build_minute_index
from rob974_h3_h2_adapter import adapt_s3_candidate
from rob974_h4_adapter import (
    H4ContractDrift,
    SealedS3Terminal,
    invoke_actual_s3_engine,
    seal_s3_engine_output,
)
from rob974_h4_contracts import exact_h4_folds
from rob974_h4_h6a_adapter import ENGINE_SOURCE_FILES, build_production_h4_plan
from rob974_r3_h3_adapter import (
    adapt_r3_s4_candidate_for_execution,
    evaluate_r3_s3_gates,
    evaluate_r3_s4_gates,
    s3,
    s4,
)
from rob974_r3_h4_s4_adapter import (
    R3_ENGINE_SOURCE_FILES,
    R3S4ParityDrift,
    SealedR3S4Terminal,
    assert_r3_s4_frozen_parity,
    invoke_r3_s4_engine,
    seal_r3_s4_engine_output,
    validate_r3_s4_terminal,
)
from rob974_r3_manifest import (
    FROZEN_R3_ROSTER,
    FROZEN_R3_S4_CONFIGS,
    R3S3Config,
    R3S4Config,
    get_r3_config,
)
from rob974_r3_relaxation_h2_adapter import (
    R3H2CellFoldInput,
    normalize_r3_phase_ledgers,
)
from rob974_r3_s4_dtos import (
    R3S4EngineResult,
    R3S4IncompleteRecord,
    R3S4PairSignalIntent,
)

_MINUTE_MS = 60_000
_PAIR = ("XRPUSDT", "DOGEUSDT")
_CORPUS_END = 100_000_000_000


def _bars(
    symbol: str,
    start_ts: int,
    count: int,
    *,
    overrides: dict[int, tuple[float, float, float, float]] | None = None,
) -> list[MinuteBar]:
    changes = overrides or {}
    rows: list[MinuteBar] = []
    for index in range(count):
        ts = start_ts + index * _MINUTE_MS
        open_, high, low, close = changes.get(index, (1.0, 1.0, 1.0, 1.0))
        rows.append(MinuteBar(symbol, ts, open_, high, low, close))
    return rows


def _intent(
    config_id: str,
    *,
    sign: int = 1,
    observed_z: float | None = None,
    signal_ts: int = 0,
    pair: tuple[str, str] = _PAIR,
    mu: float = 0.0,
    sigma: float = 0.05,
    gross_notional: float = 12.0,
) -> R3S4PairSignalIntent:
    config = get_r3_config(config_id)
    assert type(config) is R3S4Config
    magnitude = config.z_entry if observed_z is None else abs(observed_z)
    signed_z = math.copysign(magnitude, float(sign))
    side_a, side_b = ("short", "long") if sign > 0 else ("long", "short")
    return R3S4PairSignalIntent(
        pair=pair,
        signal_ts=signal_ts,
        side_a=side_a,
        side_b=side_b,
        weight_a=0.5,
        weight_b=0.5,
        beta_a=1.0,
        beta_b=1.0,
        mu=mu,
        sigma=sigma,
        observed_z=signed_z,
        z_threshold=config.z_entry,
        gross_notional=gross_notional,
        entry_sl_distance=0.01,
        entry_tp_distance=0.015,
        config_id=config.config_id,
        fold_id="fold-00",
    )


def _run_r3(
    candidates: list[R3S4PairSignalIntent],
    bars: list[MinuteBar],
    closes: list[S4PairLegClose],
    *,
    horizon_end_ts: int | None = None,
) -> R3S4EngineResult:
    return r3_s4_engine.run_r3_s4_pair_basket_stream(
        candidates,
        build_minute_index(bars),
        {(row.symbol, row.close_ts): row for row in closes},
        corpus_end_ts=_CORPUS_END,
        horizon_end_ts=horizon_end_ts,
    )


@pytest.mark.parametrize("config_id", ("S4-R3-03", "S4-R3-08"))
@pytest.mark.parametrize("sign", (-1, 1))
def test_low_z_dto_uses_exact_registered_threshold_and_one_ulp_guards(
    config_id: str, sign: int
) -> None:
    config = get_r3_config(config_id)
    assert type(config) is R3S4Config and config.z_entry in (0.8, 0.6)
    exact = _intent(
        config_id,
        sign=sign,
        observed_z=math.copysign(config.z_entry, float(sign)),
    )
    assert exact.observed_z == math.copysign(config.z_entry, float(sign))
    assert exact.z_entry == exact.observed_z
    assert exact.z_threshold == config.z_entry

    below = math.nextafter(config.z_entry, 0.0)
    with pytest.raises(ValueError, match="below its registered R3 threshold"):
        _intent(config_id, sign=sign, observed_z=math.copysign(below, float(sign)))
    with pytest.raises(ValueError, match="registered R3 config"):
        dataclasses.replace(exact, z_threshold=math.nextafter(config.z_entry, math.inf))


def _low_z_stall_fixture(
    z_at_boundary: float,
) -> tuple[R3S4PairSignalIntent, list[MinuteBar], list[S4PairLegClose]]:
    # Equal closes give a zero frozen spread. Choosing mu=-z makes
    # z_frozen exactly the requested binary float without a tolerance.
    mu = -z_at_boundary
    candidate = _intent("S4-R3-08", observed_z=0.60, mu=mu, sigma=1.0)
    count = 3 * frozen_s4_engine.FOUR_H_MS // _MINUTE_MS + 1
    bars = _bars("XRPUSDT", 0, count) + _bars("DOGEUSDT", 0, count)
    closes = []
    for boundary in (1, 2):
        ts = boundary * frozen_s4_engine.FOUR_H_MS
        closes.extend(
            (
                S4PairLegClose("XRPUSDT", ts, 1.0),
                S4PairLegClose("DOGEUSDT", ts, 1.0),
            )
        )
    third_ts = 3 * frozen_s4_engine.FOUR_H_MS
    closes.extend(
        (
            # A +0.01 canonical spread raises the exact .51 case to .52,
            # proving the unclamped observed-z threshold stalls at k==3.
            S4PairLegClose("XRPUSDT", third_ts, math.exp(0.02)),
            S4PairLegClose("DOGEUSDT", third_ts, 1.0),
        )
    )
    return candidate, bars, closes


def test_low_z_golden_stall_boundary_uses_observed_085_times_060_exactly() -> None:
    boundary = frozen_s4_engine.STALL_EXIT_Z_FRACTION * 0.60
    assert boundary == 0.51
    candidate, bars, closes = _low_z_stall_fixture(boundary)
    result = _run_r3([candidate], bars, closes)
    assert result.trades[0].exit_reason == "STALL_EXIT"
    assert result.trades[0].exit_ts == 3 * frozen_s4_engine.FOUR_H_MS


@pytest.mark.parametrize(
    "above", (math.nextafter(0.51, math.inf), 0.52), ids=("nextafter", "above")
)
def test_low_z_golden_stall_boundary_above_051_stalls(above: float) -> None:
    candidate, bars, closes = _low_z_stall_fixture(above)
    result = _run_r3([candidate], bars, closes)
    assert result.trades[0].exit_reason == "STALL_EXIT"
    assert result.trades[0].exit_ts == 2 * frozen_s4_engine.FOUR_H_MS


def test_same_low_z_path_proves_clamp_to_one_changes_stall_to_timeout() -> None:
    boundary = 0.51
    mu = -boundary
    true_candidate = _intent("S4-R3-08", observed_z=0.60, mu=mu, sigma=1.0)
    clamped_mutant = dataclasses.replace(true_candidate, observed_z=1.0)
    count = 9 * frozen_s4_engine.FOUR_H_MS // _MINUTE_MS + 1
    bars = _bars("XRPUSDT", 0, count) + _bars("DOGEUSDT", 0, count)
    closes: list[S4PairLegClose] = []
    for k in range(1, 10):
        spread_increment = 0.0 if k <= 2 else 0.01
        closes.extend(
            (
                S4PairLegClose(
                    "XRPUSDT",
                    k * frozen_s4_engine.FOUR_H_MS,
                    math.exp(2.0 * spread_increment),
                ),
                S4PairLegClose("DOGEUSDT", k * frozen_s4_engine.FOUR_H_MS, 1.0),
            )
        )
    true_result = _run_r3([true_candidate], bars, closes)
    mutant_result = _run_r3([clamped_mutant], bars, closes)
    assert (true_result.trades[0].exit_reason, true_result.trades[0].exit_ts) == (
        "STALL_EXIT",
        3 * frozen_s4_engine.FOUR_H_MS,
    )
    assert (mutant_result.trades[0].exit_reason, mutant_result.trades[0].exit_ts) == (
        "TIMEOUT",
        9 * frozen_s4_engine.FOUR_H_MS,
    )


def _parity_case(
    case: str, config_id: str, sign: int
) -> tuple[
    list[R3S4PairSignalIntent],
    list[MinuteBar],
    list[S4PairLegClose],
    int | None,
]:
    candidate = _intent(config_id, sign=sign)
    if case in ("TP", "SL"):
        favorable = case == "TP"
        if sign > 0:
            leg_a, leg_b = (
                ((0.5, 0.5, 0.5, 0.5), (2.0, 2.0, 2.0, 2.0))
                if favorable
                else ((1.5, 1.5, 1.5, 1.5), (0.6, 0.6, 0.6, 0.6))
            )
        else:
            leg_a, leg_b = (
                ((2.0, 2.0, 2.0, 2.0), (0.5, 0.5, 0.5, 0.5))
                if favorable
                else ((0.6, 0.6, 0.6, 0.6), (1.5, 1.5, 1.5, 1.5))
            )
        bars = _bars("XRPUSDT", 0, 2, overrides={1: leg_a}) + _bars(
            "DOGEUSDT", 0, 2, overrides={1: leg_b}
        )
        return [candidate], bars, [], None
    if case == "MEAN_EXIT":
        count = frozen_s4_engine.FOUR_H_MS // _MINUTE_MS + 1
        closes = [
            S4PairLegClose(symbol, frozen_s4_engine.FOUR_H_MS, 1.0) for symbol in _PAIR
        ]
        return (
            [candidate],
            _bars(_PAIR[0], 0, count) + _bars(_PAIR[1], 0, count),
            closes,
            None,
        )
    if case == "STALL_EXIT":
        count = 2 * frozen_s4_engine.FOUR_H_MS // _MINUTE_MS + 1
        closes = [
            S4PairLegClose(symbol, boundary * frozen_s4_engine.FOUR_H_MS, close)
            for boundary in (1, 2)
            for symbol, close in ((_PAIR[0], math.exp(4.0)), (_PAIR[1], 1.0))
        ]
        return (
            [candidate],
            _bars(_PAIR[0], 0, count) + _bars(_PAIR[1], 0, count),
            closes,
            None,
        )
    if case == "TIMEOUT":
        count = 9 * frozen_s4_engine.FOUR_H_MS // _MINUTE_MS + 1
        closes = [
            S4PairLegClose(symbol, boundary * frozen_s4_engine.FOUR_H_MS, close)
            for boundary in range(1, 10)
            for symbol, close in ((_PAIR[0], math.exp(1.0)), (_PAIR[1], 1.0))
        ]
        return (
            [candidate],
            _bars(_PAIR[0], 0, count) + _bars(_PAIR[1], 0, count),
            closes,
            None,
        )
    if case == "NEXT_TICK":
        return (
            [candidate],
            _bars(_PAIR[0], _MINUTE_MS, 1) + _bars(_PAIR[1], 0, 1),
            [],
            None,
        )
    if case == "G_MISMATCH":
        bad = dataclasses.replace(candidate, gross_notional=13.0)
        return [bad], _bars(_PAIR[0], 0, 1) + _bars(_PAIR[1], 0, 1), [], None
    if case == "G_INFEASIBLE":
        bad = dataclasses.replace(
            candidate, weight_a=0.05, weight_b=0.95, gross_notional=120.0
        )
        return [bad], _bars(_PAIR[0], 0, 1) + _bars(_PAIR[1], 0, 1), [], None
    if case == "INCOMPLETE":
        return [candidate], _bars(_PAIR[0], 0, 1) + _bars(_PAIR[1], 0, 1), [], None
    if case == "HORIZON_INCOMPLETE":
        return [candidate], _bars(_PAIR[0], 0, 1) + _bars(_PAIR[1], 0, 1), [], 0
    if case == "CONSERVATIVE_SL":
        if sign > 0:
            leg_a = (1.0, 1.6, 1.0, 1.0)
            leg_b = (1.0, 1.0, 0.6, 1.0)
        else:
            leg_a = (1.0, 1.0, 0.6, 1.0)
            leg_b = (1.0, 1.6, 1.0, 1.0)
        bars = _bars("XRPUSDT", 0, 2, overrides={1: leg_a}) + _bars(
            "DOGEUSDT", 0, 2, overrides={1: leg_b}
        )
        return [candidate], bars, [], None
    if case == "GLOBAL_ORDER":
        second = _intent(
            config_id,
            sign=sign,
            pair=("XRPUSDT", "SOLUSDT"),
        )
        if sign > 0:
            a_change = (0.5, 0.5, 0.5, 0.5)
            other_change = (2.0, 2.0, 2.0, 2.0)
        else:
            a_change = (2.0, 2.0, 2.0, 2.0)
            other_change = (0.5, 0.5, 0.5, 0.5)
        bars = _bars("XRPUSDT", 0, 2, overrides={1: a_change})
        bars += _bars("DOGEUSDT", 0, 2, overrides={1: other_change})
        bars += _bars("SOLUSDT", 0, 2, overrides={1: other_change})
        return [second, candidate], bars, [], None
    if case == "EXACT_REENTRY":
        second = _intent(
            config_id,
            sign=sign,
            signal_ts=_MINUTE_MS,
            pair=("XRPUSDT", "SOLUSDT"),
        )
        if sign > 0:
            a_one, b_one = (0.5, 0.5, 0.5, 0.5), (2.0, 2.0, 2.0, 2.0)
            a_two, c_two = (0.25, 0.25, 0.25, 0.25), (2.0, 2.0, 2.0, 2.0)
        else:
            a_one, b_one = (2.0, 2.0, 2.0, 2.0), (0.5, 0.5, 0.5, 0.5)
            a_two, c_two = (4.0, 4.0, 4.0, 4.0), (0.5, 0.5, 0.5, 0.5)
        bars = _bars("XRPUSDT", 0, 3, overrides={1: a_one, 2: a_two})
        bars += _bars("DOGEUSDT", 0, 3, overrides={1: b_one})
        bars += _bars("SOLUSDT", 0, 3, overrides={2: c_two})
        return [second, candidate], bars, [], None
    raise AssertionError(f"unknown parity case {case}")


@pytest.mark.parametrize(
    "case",
    (
        "TP",
        "SL",
        "MEAN_EXIT",
        "STALL_EXIT",
        "TIMEOUT",
        "NEXT_TICK",
        "G_MISMATCH",
        "G_INFEASIBLE",
        "INCOMPLETE",
        "HORIZON_INCOMPLETE",
        "CONSERVATIVE_SL",
        "GLOBAL_ORDER",
        "EXACT_REENTRY",
    ),
)
@pytest.mark.parametrize("sign", (-1, 1))
@pytest.mark.parametrize(
    "config", FROZEN_R3_S4_CONFIGS[:3], ids=lambda row: row.config_id
)
def test_adversarial_representable_matrix_is_byte_and_economic_identical(
    case: str, sign: int, config: R3S4Config
) -> None:
    candidates, bars, closes, horizon_end_ts = _parity_case(
        case, config.config_id, sign
    )
    evidence = assert_r3_s4_frozen_parity(
        candidates=candidates,
        minute_index=build_minute_index(bars),
        pair_close_index={(row.symbol, row.close_ts): row for row in closes},
        corpus_end_ts=_CORPUS_END,
        horizon_end_ts=horizon_end_ts,
    )
    assert evidence.r2_input_bytes == evidence.r3_input_bytes
    assert evidence.r2_output_bytes == evidence.r3_output_bytes
    assert len(evidence.input_sha256) == len(evidence.output_sha256) == 64


_REPRESENTABLE_MAGNITUDES = tuple(
    (config, magnitude)
    for config in FROZEN_R3_S4_CONFIGS
    for magnitude in (1.0, 1.1, 1.9)
    if magnitude >= config.z_entry
)


@pytest.mark.parametrize(
    ("config", "magnitude"),
    _REPRESENTABLE_MAGNITUDES,
    ids=lambda value: value.config_id if type(value) is R3S4Config else str(value),
)
@pytest.mark.parametrize("sign", (-1, 1))
def test_parity_covers_every_registered_threshold_class_and_observed_magnitude(
    config: R3S4Config, magnitude: float, sign: int
) -> None:
    candidate = _intent(
        config.config_id,
        sign=sign,
        observed_z=math.copysign(magnitude, float(sign)),
    )
    candidates, bars, closes, horizon = _parity_case("TP", config.config_id, sign)
    candidates[0] = candidate
    evidence = assert_r3_s4_frozen_parity(
        candidates=candidates,
        minute_index=build_minute_index(bars),
        pair_close_index={(row.symbol, row.close_ts): row for row in closes},
        corpus_end_ts=_CORPUS_END,
        horizon_end_ts=horizon,
    )
    assert evidence.r2_output_bytes == evidence.r3_output_bytes


def _source_mutant(function: object, old: str, new: str):
    source = textwrap.dedent(inspect.getsource(function))
    assert source.count(old) == 1
    namespace = dict(function.__globals__)  # type: ignore[attr-defined]
    exec(compile(source.replace(old, new), "<r3-parity-mutant>", "exec"), namespace)
    return namespace[function.__name__]  # type: ignore[attr-defined]


def test_parity_detects_real_stall_comparator_gt_to_gte_source_mutant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_z = 1.0
    strict_boundary = frozen_s4_engine.STALL_EXIT_Z_FRACTION * observed_z
    candidate = _intent(
        "S4-R3-01", observed_z=observed_z, mu=-strict_boundary, sigma=1.0
    )
    count = 3 * frozen_s4_engine.FOUR_H_MS // _MINUTE_MS + 1
    bars = _bars("XRPUSDT", 0, count) + _bars("DOGEUSDT", 0, count)
    closes = [
        S4PairLegClose(symbol, k * frozen_s4_engine.FOUR_H_MS, close)
        for k in (1, 2, 3)
        for symbol, close in (
            ("XRPUSDT", 1.0 if k <= 2 else math.exp(0.02)),
            ("DOGEUSDT", 1.0),
        )
    ]
    mutant = _source_mutant(
        frozen_s4_engine._walk_s4_position,
        ") > STALL_EXIT_Z_FRACTION * abs(cand.z_entry)",
        ") >= STALL_EXIT_Z_FRACTION * abs(cand.z_entry)",
    )
    monkeypatch.setattr(r3_s4_engine, "_frozen_walk_s4_position", mutant)
    with pytest.raises(R3S4ParityDrift, match="output canonical bytes"):
        assert_r3_s4_frozen_parity(
            candidates=[candidate],
            minute_index=build_minute_index(bars),
            pair_close_index={(row.symbol, row.close_ts): row for row in closes},
            corpus_end_ts=_CORPUS_END,
        )


def test_parity_matrix_detects_stall_eligibility_two_to_three_walk_mutant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidates, bars, closes, horizon_end_ts = _parity_case("STALL_EXIT", "S4-R3-00", 1)
    original_walk = r3_s4_engine._frozen_walk_s4_position

    def _eligibility_three_mutant(*args: object, **kwargs: object):
        original_eligibility = frozen_s4_engine.STALL_ELIGIBLE_FROM_BOUNDARY
        frozen_s4_engine.STALL_ELIGIBLE_FROM_BOUNDARY = 3
        try:
            return original_walk(*args, **kwargs)
        finally:
            frozen_s4_engine.STALL_ELIGIBLE_FROM_BOUNDARY = original_eligibility

    monkeypatch.setattr(
        r3_s4_engine, "_frozen_walk_s4_position", _eligibility_three_mutant
    )
    with pytest.raises(R3S4ParityDrift, match="output canonical bytes"):
        assert_r3_s4_frozen_parity(
            candidates=candidates,
            minute_index=build_minute_index(bars),
            pair_close_index={(row.symbol, row.close_ts): row for row in closes},
            corpus_end_ts=_CORPUS_END,
            horizon_end_ts=horizon_end_ts,
        )


def test_parity_matrix_detects_global_position_lt_to_lte_mutant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidates, bars, closes, horizon_end_ts = _parity_case(
        "EXACT_REENTRY", "S4-R3-00", 1
    )
    mutant = _source_mutant(
        r3_s4_engine._position_is_open,
        "signal_ts < exit_ts",
        "signal_ts <= exit_ts",
    )
    monkeypatch.setattr(r3_s4_engine, "_position_is_open", mutant)
    with pytest.raises(R3S4ParityDrift, match="output canonical bytes"):
        assert_r3_s4_frozen_parity(
            candidates=candidates,
            minute_index=build_minute_index(bars),
            pair_close_index={(row.symbol, row.close_ts): row for row in closes},
            corpus_end_ts=_CORPUS_END,
            horizon_end_ts=horizon_end_ts,
        )


def test_duplicate_upfront_error_has_exact_type_args_parity() -> None:
    candidate = _intent("S4-R3-00")
    evidence = assert_r3_s4_frozen_parity(
        candidates=[candidate, candidate],
        minute_index=build_minute_index(
            _bars("XRPUSDT", 0, 1) + _bars("DOGEUSDT", 0, 1)
        ),
        pair_close_index={},
        corpus_end_ts=_CORPUS_END,
    )
    assert evidence.r2_output_bytes == evidence.r3_output_bytes


def test_r3_engine_inventory_extends_frozen_inventory_with_manifest_dto_engine() -> (
    None
):
    logical_paths = tuple(path for path, _ in R3_ENGINE_SOURCE_FILES)
    assert R3_ENGINE_SOURCE_FILES[: len(ENGINE_SOURCE_FILES)] == ENGINE_SOURCE_FILES
    assert logical_paths[-3:] == (
        "research/nautilus_scalping/rob974_r3_manifest.py",
        "research/nautilus_scalping/rob974_r3_s4_dtos.py",
        "research/nautilus_scalping/rob974_r3_s4_engine.py",
    )
    assert len(logical_paths) == len(set(logical_paths))


def test_r3_engine_and_h4_reject_frozen_r2_candidate_type() -> None:
    r3_candidate = _intent("S4-R3-00")
    frozen_candidate = S4PairSignalIntent(
        pair=r3_candidate.pair,
        signal_ts=r3_candidate.signal_ts,
        side_a=r3_candidate.side_a,
        side_b=r3_candidate.side_b,
        weight_a=r3_candidate.weight_a,
        weight_b=r3_candidate.weight_b,
        beta_a=r3_candidate.beta_a,
        beta_b=r3_candidate.beta_b,
        mu=r3_candidate.mu,
        sigma=r3_candidate.sigma,
        z_entry=r3_candidate.observed_z,
        gross_notional=r3_candidate.gross_notional,
        entry_sl_distance=r3_candidate.entry_sl_distance,
        entry_tp_distance=r3_candidate.entry_tp_distance,
        config_id=r3_candidate.config_id,
        fold_id=r3_candidate.fold_id,
    )
    minute_index = build_minute_index(_bars(_PAIR[0], 0, 1) + _bars(_PAIR[1], 0, 1))
    with pytest.raises(TypeError, match="exact R3S4PairSignalIntent"):
        r3_s4_engine.run_r3_s4_pair_basket_stream(
            [frozen_candidate],  # type: ignore[list-item]
            minute_index,
            {},
            corpus_end_ts=_CORPUS_END,
        )
    with pytest.raises(TypeError, match="exact R3S4PairSignalIntent"):
        invoke_r3_s4_engine(
            candidates=[frozen_candidate],
            minute_index=minute_index,
            pair_close_index={},
            corpus_end_ts=_CORPUS_END,
            strategy="S4",
            config_id="S4-R3-00",
            fold_id="fold-00",
        )


def _incomplete_for(candidate: R3S4PairSignalIntent) -> R3S4IncompleteRecord:
    return R3S4IncompleteRecord(
        pair=candidate.pair,
        side_a=candidate.side_a,
        side_b=candidate.side_b,
        config_id=candidate.config_id,
        fold_id=candidate.fold_id,
        signal_ts=candidate.signal_ts,
        entry_ts=candidate.signal_ts,
        entry_price_a=1.0,
        entry_price_b=1.0,
        reason="data_gap_in_pair_position",
    )


def test_h4_exact_terminal_rejects_duplicate_multiple_incomplete_and_bad_prefix() -> (
    None
):
    first = _intent("S4-R3-00")
    second = _intent(
        "S4-R3-00",
        signal_ts=_MINUTE_MS,
        pair=("XRPUSDT", "SOLUSDT"),
    )
    with pytest.raises(H4ContractDrift, match="duplicate candidate"):
        validate_r3_s4_terminal(
            [first, first],
            R3S4EngineResult((), (), ()),
            config_id=first.config_id,
            fold_id=first.fold_id,
        )
    with pytest.raises(H4ContractDrift, match="more than one"):
        validate_r3_s4_terminal(
            [first, second],
            R3S4EngineResult((), (), (_incomplete_for(first), _incomplete_for(second))),
            config_id=first.config_id,
            fold_id=first.fold_id,
        )
    bad_side = dataclasses.replace(
        _incomplete_for(first), side_a="long", side_b="short"
    )
    with pytest.raises(H4ContractDrift, match="leg directions"):
        validate_r3_s4_terminal(
            [first, second],
            R3S4EngineResult((), (), (bad_side,)),
            config_id=first.config_id,
            fold_id=first.fold_id,
        )
    with pytest.raises(H4ContractDrift, match="resolved-prefix"):
        validate_r3_s4_terminal(
            [first, second],
            R3S4EngineResult((), (), (_incomplete_for(second),)),
            config_id=first.config_id,
            fold_id=first.fold_id,
        )


def _s3_metrics(config: R3S3Config, decision_ts: int) -> s3.S3Metrics:
    return s3.S3Metrics(
        config_id=config.config_id,
        decision_ts=decision_ts,
        symbol="XRPUSDT",
        R=0.10,
        ER=0.50,
        S=0.10,
        Qplus=0.50,
        Qminus=-0.10,
        close=101.0,
        previous_close=100.0,
        prior_l_high=102.0,
        prior_l_low=90.0,
        atr20=0.6,
        A=0.006,
        vwap12=100.0,
        vwap24=99.0,
        percentile_30d=50.0,
        range24=0.03,
        market_return_24h=0.01,
        current_market_return_4h=0.005,
        bplus=3,
        bminus=0,
    )


def _s4_estimate(config: R3S4Config, decision_ts: int) -> s4.S4Estimate:
    return s4.S4Estimate(
        config_id=config.config_id,
        decision_ts=decision_ts,
        pair="XRP-DOGE",
        symbol_a="XRPUSDT",
        symbol_b="DOGEUSDT",
        beta_a=1.0,
        beta_b=1.0,
        beta_a_first=1.0,
        beta_a_second=1.0,
        beta_b_first=1.0,
        beta_b_second=1.0,
        weight_a=0.5,
        weight_b=0.5,
        spread=0.018,
        mu=0.0,
        mad=0.01,
        effective_mad_scale=0.014826,
        z=config.z_entry,
        prior_beta_a=1.0,
        prior_beta_b=1.0,
        prior_weight_a=0.5,
        prior_weight_b=0.5,
        prior_mu=0.0,
        prior_mad=0.01,
        prior_effective_mad_scale=0.014826,
        z_prior=2.0 * config.z_entry,
        D_fraction=0.03,
        D_bps=180.0,
        rho=0.70,
        phi=0.75,
        half_life_4h_bars=2.409420839653209,
        beta_stability=0.10,
        sigma_pair=0.0,
        pair_return_fraction=-0.001,
        pair_return_bps=-10.0,
        current_market_return_4h=0.50,
    )


def test_production_shaped_all_12_cells_cross_h3_engine_h4_and_m3() -> None:
    fold = exact_h4_folds()[0]
    decision_ts = fold.oos_start_ms + frozen_s4_engine.FOUR_H_MS
    completed: list[str] = []
    executed_terminals: dict[str, SealedS3Terminal | SealedR3S4Terminal] = {}
    for config in FROZEN_R3_ROSTER:
        if type(config) is R3S3Config:
            outcome = evaluate_r3_s3_gates(_s3_metrics(config, decision_ts), config)
            assert outcome.candidate is not None
            intent = adapt_s3_candidate(outcome.candidate, fold_id=fold.fold_id)
            bars = _bars(
                "XRPUSDT",
                decision_ts,
                2,
                overrides={1: (1.0, 1.1, 1.0, 1.05)},
            )
            terminal = invoke_actual_s3_engine(
                candidates=[intent],
                minute_index=build_minute_index(bars),
                close_feature_index={},
                corpus_end_ts=decision_ts + 2 * _MINUTE_MS,
                horizon_end_ts=fold.oos_end_ms,
                strategy="S3",
                config_id=config.config_id,
                fold_id=fold.fold_id,
            )
            assert len(terminal.result.trades) == 1
        else:
            outcome = evaluate_r3_s4_gates(_s4_estimate(config, decision_ts), config)
            assert outcome.candidate is not None
            intent = adapt_r3_s4_candidate_for_execution(
                outcome.candidate, fold_id=fold.fold_id
            )
            bars = _bars(
                "XRPUSDT",
                decision_ts,
                2,
                overrides={1: (0.5, 0.5, 0.5, 0.5)},
            ) + _bars(
                "DOGEUSDT",
                decision_ts,
                2,
                overrides={1: (2.0, 2.0, 2.0, 2.0)},
            )
            terminal = invoke_r3_s4_engine(
                candidates=[intent],
                minute_index=build_minute_index(bars),
                pair_close_index={},
                corpus_end_ts=decision_ts + 2 * _MINUTE_MS,
                horizon_end_ts=fold.oos_end_ms,
                strategy="S4",
                config_id=config.config_id,
                fold_id=fold.fold_id,
            )
            assert type(terminal.result) is R3S4EngineResult
            assert len(terminal.result.trades) == 1
        assert terminal.result.incompletes == ()
        executed_terminals[config.config_id] = terminal
        completed.append(config.config_id)
    assert tuple(completed) == tuple(row.config_id for row in FROZEN_R3_ROSTER)

    sources: list[R3H2CellFoldInput] = []
    for config in FROZEN_R3_ROSTER:
        for current_fold in exact_h4_folds():
            if current_fold.fold_id == fold.fold_id:
                terminal = executed_terminals[config.config_id]
            elif type(config) is R3S3Config:
                empty = S3EngineResult((), (), ())
                terminal = SealedS3Terminal(
                    empty, "a" * 64, seal_s3_engine_output(empty)
                )
            else:
                empty_r3 = R3S4EngineResult((), (), ())
                terminal = SealedR3S4Terminal(
                    empty_r3, "a" * 64, seal_r3_s4_engine_output(empty_r3)
                )
            sources.append(
                R3H2CellFoldInput(
                    config=config,
                    fold_id=current_fold.fold_id,
                    terminal=terminal,
                )
            )
    evidence = normalize_r3_phase_ledgers(phase="OOS", sources=tuple(sources))
    assert evidence.operational_status == "COMPLETE"
    assert len(evidence.ledgers) == 12 * 8
    for config_index, config in enumerate(FROZEN_R3_ROSTER):
        ledger = evidence.ledgers[config_index * 8]
        assert ledger.basket_trade_count == len(ledger.trades) == 1
        assert ledger.trades[0].event.family == config.config_id[:2]
        if type(config) is R3S4Config:
            assert ledger.trades[0].execution.observed_z == config.z_entry


def test_frozen_r2_execution_sources_and_full_identity_are_unchanged() -> None:
    expected = {
        "rob974_h2_dtos.py": "1c0a7f856208fd529c88d2ba88392e25f9cc6530effa1b6e926201c896f827f8",
        "rob974_h2_s4_engine.py": "90c6c82eb2b82b8d66576cad7e3219f0b9a4dc2841704464277fd4488043c87e",
        "rob974_h3_h2_adapter.py": "8316721acdad1522ec428cb15064557125394e0de35b88007e7edc329974089d",
        "rob974_h4_adapter.py": "b58ccf176941f9832a6ac37da0b851b80eeb9e3a09b38ec673319d68646de156",
        "rob974_h4_runner.py": "3759bd3e082e7c3a1daddc426db9135c30dc00f02fbb52e403c38cd6f46a0f22",
    }
    root = Path(__file__).resolve().parents[1]
    assert {
        filename: hashlib.sha256((root / filename).read_bytes()).hexdigest()
        for filename in expected
    } == expected
    assert build_production_h4_plan().full_campaign_hash == (
        "2c47864c7ab661f16be6c414a1140944ec36832bb268e86183555b56c6f85f53"
    )

from __future__ import annotations

import dataclasses
import math
from collections.abc import Iterator, Mapping

import pytest
from rob974_features import Bar4h, MinuteBar
from rob974_h4_contracts import exact_h4_folds
from rob1040_crs24_contracts import (
    FOUR_HOUR_MS,
    HALF_DAY_MS,
    UNIVERSE,
    VOLATILITY_RETURN_COUNT,
    config_for_id,
)
from rob1040_crs24_feasibility import scheduled_cutoffs
from rob1040_crs24_features import (
    ARBITRATION_STRENGTH_TIE,
    COMMON_VOLATILITY_FLOOR,
    INPUT_HISTORY_INCOMPLETE,
    PIT_HISTORY_BELOW_MINIMUM,
    RESIDUAL_VOLATILITY_FLOOR,
    CRSFeature,
    CRSFeatureGenerator,
    FeatureClosed,
    SymbolFormationFeature,
    arbitrate,
    build_joint_returns,
    complete_bars_from_minutes,
    directional_candidates,
    nearest_rank,
)
from rob1040_crs24_synthetic import build_synthetic_fixture


def _sample_volatility(values: tuple[float, ...]) -> float:
    mean = math.fsum(values) / len(values)
    return math.sqrt(
        math.fsum((value - mean) ** 2 for value in values) / (len(values) - 1)
    )


def _bars_from_return_function(
    return_function,
    *,
    count: int = 126,
) -> dict[str, tuple[Bar4h, ...]]:
    output: dict[str, tuple[Bar4h, ...]] = {}
    for symbol_index, symbol in enumerate(UNIVERSE):
        prior = (0.5, 0.2, 150.0)[symbol_index]
        rows: list[Bar4h] = []
        for index in range(count):
            close = prior * math.exp(return_function(index, symbol_index))
            close_ts = (index + 1) * FOUR_HOUR_MS
            rows.append(
                Bar4h(
                    ts=close_ts - FOUR_HOUR_MS,
                    close_ts=close_ts,
                    open=prior,
                    high=max(prior, close) * 1.001,
                    low=min(prior, close) * 0.999,
                    close=close,
                    volume=100.0,
                    is_segment_start=index == 0,
                )
            )
            prior = close
        output[symbol] = tuple(rows)
    return output


def _feature_with_scores(scores: tuple[float, float, float]) -> CRSFeature:
    symbols = tuple(
        SymbolFormationFeature(
            symbol=symbol,
            residual_sum=score * math.sqrt(42),
            residual_sample_volatility=1.0,
            score=score,
            raw_sample_volatility=0.1,
            movement_capacity_bp=(1e4 * math.sqrt(2.0 / math.pi) * 0.1 * math.sqrt(6)),
        )
        for symbol, score in zip(UNIVERSE, scores, strict=True)
    )
    return CRSFeature(
        config_id="CRS-A0",
        cutoff_ms=0,
        common_sum=0.1 * math.sqrt(42),
        common_sample_volatility=1.0,
        common_magnitude=0.1,
        dispersion=max(scores) - min(scores),
        symbols=symbols,
    )


def test_rob974_complete_only_builder_is_the_minute_input_seam() -> None:
    rows: dict[str, tuple[MinuteBar, ...]] = {}
    for symbol in UNIVERSE:
        complete = tuple(
            MinuteBar(
                ts=index * 60_000,
                open=1.0,
                high=1.0,
                low=1.0,
                close=1.0,
                volume=1.0,
            )
            for index in range(240)
        )
        rows[symbol] = complete
    rows["SOLUSDT"] = rows["SOLUSDT"][:-1]
    built = complete_bars_from_minutes(rows)
    assert len(built["XRPUSDT"]) == 1
    assert len(built["DOGEUSDT"]) == 1
    assert built["SOLUSDT"] == ()


def test_feature_formulas_use_config_formation_and_trailing_42_only() -> None:
    fixture = build_synthetic_fixture()
    bars = fixture.bars_by_symbol()
    generator = CRSFeatureGenerator(bars)
    config = config_for_id("CRS-A1")
    cutoff = scheduled_cutoffs(exact_h4_folds()[0])[0]
    feature = generator.feature_at(config, cutoff)
    assert isinstance(feature, CRSFeature)

    indexed = {item.close_ts: item for item in build_joint_returns(bars)}
    formation = tuple(
        indexed[cutoff - offset * FOUR_HOUR_MS]
        for offset in reversed(range(config.formation_return_count))
    )
    volatility = tuple(
        indexed[cutoff - offset * FOUR_HOUR_MS]
        for offset in reversed(range(VOLATILITY_RETURN_COUNT))
    )
    common_sigma = _sample_volatility(tuple(item.common_return for item in volatility))
    common_sum = math.fsum(item.common_return for item in formation)
    assert feature.common_sample_volatility == pytest.approx(common_sigma)
    assert feature.common_sum == pytest.approx(common_sum)
    assert feature.common_magnitude == pytest.approx(
        abs(common_sum) / (common_sigma * math.sqrt(config.formation_return_count))
    )
    expected_scores: list[float] = []
    for index, symbol in enumerate(UNIVERSE):
        residual_sigma = _sample_volatility(
            tuple(item.residual_returns[index] for item in volatility)
        )
        residual_sum = math.fsum(item.residual_returns[index] for item in formation)
        raw_sigma = _sample_volatility(
            tuple(item.raw_returns[index] for item in volatility)
        )
        item = feature.symbol(symbol)
        expected_score = residual_sum / (
            residual_sigma * math.sqrt(config.formation_return_count)
        )
        expected_scores.append(expected_score)
        assert item.residual_sample_volatility == pytest.approx(residual_sigma)
        assert item.score == pytest.approx(expected_score)
        assert item.raw_sample_volatility == pytest.approx(raw_sigma)
        assert item.movement_capacity_bp == pytest.approx(
            1e4 * math.sqrt(2 / math.pi) * raw_sigma * math.sqrt(6)
        )
    assert feature.dispersion == pytest.approx(
        max(expected_scores) - min(expected_scores)
    )


def test_missing_joint_history_and_both_volatility_floors_close() -> None:
    cutoff = 126 * FOUR_HOUR_MS
    assert cutoff % HALF_DAY_MS == 0

    varying_common = _bars_from_return_function(
        lambda index, _symbol_index: 0.001 * math.sin(index * 0.2)
    )
    residual_closed = CRSFeatureGenerator(varying_common).feature_at("CRS-A0", cutoff)
    assert residual_closed == FeatureClosed("CRS-A0", cutoff, RESIDUAL_VOLATILITY_FLOOR)

    zero_common = _bars_from_return_function(
        lambda index, symbol_index: (
            0.001 * math.sin(index * 0.2)
            if symbol_index == 0
            else (
                0.0008 * math.cos(index * 0.17)
                if symbol_index == 1
                else (-0.001 * math.sin(index * 0.2) - 0.0008 * math.cos(index * 0.17))
            )
        )
    )
    common_closed = CRSFeatureGenerator(zero_common).feature_at("CRS-A0", cutoff)
    assert common_closed == FeatureClosed("CRS-A0", cutoff, COMMON_VOLATILITY_FLOOR)

    missing = dict(zero_common)
    missing["SOLUSDT"] = (
        *missing["SOLUSDT"][:-5],
        *missing["SOLUSDT"][-4:],
    )
    incomplete = CRSFeatureGenerator(missing).feature_at("CRS-A2", cutoff)
    assert incomplete == FeatureClosed("CRS-A2", cutoff, INPUT_HISTORY_INCOMPLETE)


def test_prior_60_day_gate_uses_nearest_rank_and_excludes_current() -> None:
    fixture = build_synthetic_fixture()
    generator = CRSFeatureGenerator(fixture.bars_by_symbol())
    cutoff = scheduled_cutoffs(exact_h4_folds()[0])[0]
    gate = generator.gates_at("CRS-A0", cutoff)
    assert gate.prior_valid_observations == 120
    prior = tuple(
        generator.feature_at("CRS-A0", timestamp)
        for timestamp in range(cutoff - 60 * 86_400_000, cutoff, HALF_DAY_MS)
    )
    assert all(isinstance(item, CRSFeature) for item in prior)
    valid = tuple(item for item in prior if isinstance(item, CRSFeature))
    assert all(item.cutoff_ms < cutoff for item in valid)
    assert gate.dispersion_threshold == nearest_rank(
        tuple(item.dispersion for item in valid), 0.50
    )
    assert gate.common_magnitude_threshold == nearest_rank(
        tuple(item.common_magnitude for item in valid), 0.75
    )


def test_gate_closes_below_100_prior_valid_observations() -> None:
    fixture = build_synthetic_fixture()
    cutoff = scheduled_cutoffs(exact_h4_folds()[0])[0]
    truncated = {
        symbol: tuple(bar for bar in bars if bar.close_ts >= cutoff - 55 * 86_400_000)
        for symbol, bars in fixture.bars_by_symbol().items()
    }
    gate = CRSFeatureGenerator(truncated).gates_at("CRS-A0", cutoff)
    assert isinstance(gate.feature, CRSFeature)
    assert gate.prior_valid_observations < 100
    assert gate.joint_pass is False
    assert gate.closed_reason == PIT_HISTORY_BELOW_MINIMUM


def test_future_bar_changes_cannot_change_current_feature_or_gate() -> None:
    fixture = build_synthetic_fixture()
    original_bars = fixture.bars_by_symbol()
    cutoff = scheduled_cutoffs(exact_h4_folds()[0])[0]
    changed_bars = {
        symbol: tuple(
            dataclasses.replace(
                bar,
                open=bar.open * 1.25,
                high=bar.high * 1.25,
                low=bar.low * 1.25,
                close=bar.close * 1.25,
            )
            if bar.close_ts > cutoff
            else bar
            for bar in bars
        )
        for symbol, bars in original_bars.items()
    }
    original = CRSFeatureGenerator(original_bars)
    changed = CRSFeatureGenerator(changed_bars)
    assert original.snapshot_sha256 != changed.snapshot_sha256
    assert original.feature_at("CRS-A2", cutoff) == changed.feature_at("CRS-A2", cutoff)
    assert original.gates_at("CRS-A2", cutoff) == changed.gates_at("CRS-A2", cutoff)
    original_cell = original.evaluate_cutoffs("CRS-A2", (cutoff,))
    changed_cell = changed.evaluate_cutoffs("CRS-A2", (cutoff,))
    assert original_cell == changed_cell
    assert original_cell.causal_source_sha256 == changed_cell.causal_source_sha256


def test_generator_rejects_re_evaluating_mapping_before_any_derivation() -> None:
    fixture = build_synthetic_fixture()
    first = fixture.bars_by_symbol()
    second = {
        symbol: tuple(dataclasses.replace(bar, volume=bar.volume + 1.0) for bar in bars)
        for symbol, bars in first.items()
    }

    class FlippingMapping(Mapping[str, tuple[Bar4h, ...]]):
        def __init__(self) -> None:
            self.accesses = 0

        def __getitem__(self, key: str) -> tuple[Bar4h, ...]:
            self.accesses += 1
            return first[key] if self.accesses <= 3 else second[key]

        def __iter__(self) -> Iterator[str]:
            return iter(UNIVERSE)

        def __len__(self) -> int:
            return len(UNIVERSE)

    source = FlippingMapping()
    with pytest.raises(TypeError, match="exact built-in dict"):
        CRSFeatureGenerator(source)  # type: ignore[arg-type]
    assert source.accesses == 0


def test_nearest_rank_is_one_based_and_fail_closed() -> None:
    values = tuple(float(value) for value in range(1, 101))
    assert nearest_rank(values, 0.50) == 50.0
    assert nearest_rank(values, 0.75) == 75.0
    with pytest.raises(ValueError):
        nearest_rank((), 0.50)
    with pytest.raises(ValueError):
        nearest_rank(values, 0.0)


def test_candidate_tie_order_and_stronger_side_arbitration() -> None:
    feature = _feature_with_scores((2.0, 2.0, -1.0))
    candidates = directional_candidates(feature)
    assert tuple((item.symbol, item.side, item.strength) for item in candidates) == (
        ("XRPUSDT", "LONG", 2.0),
        ("SOLUSDT", "SHORT", 1.0),
    )
    assert arbitrate(feature).winner == candidates[0]

    short_tie = _feature_with_scores((2.0, -1.0, -1.0))
    assert directional_candidates(short_tie)[1].symbol == "DOGEUSDT"


def test_generator_rejects_a_forged_registered_config() -> None:
    fixture = build_synthetic_fixture()
    generator = CRSFeatureGenerator(fixture.bars_by_symbol())
    forged = dataclasses.replace(
        config_for_id("CRS-A0"),
        formation_hours=72,
        formation_return_count=18,
    )
    with pytest.raises(ValueError, match="registered"):
        generator.feature_at(forged, scheduled_cutoffs(exact_h4_folds()[0])[0])


def test_opposing_strength_difference_below_tolerance_has_no_winner() -> None:
    feature = _feature_with_scores((1.0, -(1.0 - 0.5e-12), -0.2))
    arbitration = arbitrate(feature)
    assert arbitration.winner is None
    assert arbitration.closed_reason == ARBITRATION_STRENGTH_TIE

    outside = _feature_with_scores((1.0, -(1.0 - 2e-12), -0.2))
    assert arbitrate(outside).winner is not None

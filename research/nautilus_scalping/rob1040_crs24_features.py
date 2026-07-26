"""Pure CRS-24 complete-4h feature, gate, and arbitration primitives."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from types import MappingProxyType

from rob974_features import Bar4h, MinuteBar, build_complete_4h, symbol_features
from rob1040_crs24_contracts import (
    ARBITRATION_TOLERANCE,
    COMMON_MAGNITUDE_QUANTILE,
    DISPERSION_QUANTILE,
    FOUR_HOUR_MS,
    HALF_DAY_MS,
    PIT_LOOKBACK_MS,
    PIT_MIN_OBSERVATIONS,
    UNIVERSE,
    VOLATILITY_FLOOR,
    VOLATILITY_RETURN_COUNT,
    CRSConfig,
    config_for_id,
)

from research_contracts.canonical_hash import canonical_sha256

INPUT_HISTORY_INCOMPLETE = "input_history_incomplete"
RESIDUAL_VOLATILITY_FLOOR = "residual_volatility_floor"
COMMON_VOLATILITY_FLOOR = "common_volatility_floor"
PIT_HISTORY_BELOW_MINIMUM = "pit_history_below_minimum"
DISPERSION_GATE_CLOSED = "dispersion_gate_closed"
COMMON_MAGNITUDE_GATE_CLOSED = "common_magnitude_gate_closed"
NO_DIRECTIONAL_CANDIDATE = "no_directional_candidate"
ARBITRATION_STRENGTH_TIE = "arbitration_strength_tie"


def _exact_int(value: object, name: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{name} must be built-in int")
    return value


def _finite_float(value: object, name: str) -> float:
    if type(value) is not float:
        raise TypeError(f"{name} must be built-in float")
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    return value


def _sample_volatility(values: Sequence[float]) -> float:
    if len(values) < 2:
        raise ValueError("sample volatility needs at least two values")
    for value in values:
        _finite_float(value, "sample value")
    mean = math.fsum(values) / len(values)
    centered = math.fsum((value - mean) ** 2 for value in values)
    answer = math.sqrt(max(0.0, centered / (len(values) - 1)))
    if not math.isfinite(answer):
        raise ValueError("sample volatility is non-finite")
    return answer


@dataclass(frozen=True, slots=True)
class JointReturn:
    close_ts: int
    raw_returns: tuple[float, float, float]
    common_return: float
    residual_returns: tuple[float, float, float]

    def __post_init__(self) -> None:
        _exact_int(self.close_ts, "close_ts")
        if self.close_ts % FOUR_HOUR_MS:
            raise ValueError("joint return close_ts must be UTC 4h aligned")
        if type(self.raw_returns) is not tuple or len(self.raw_returns) != 3:
            raise TypeError("raw_returns must be an exact three-item tuple")
        if type(self.residual_returns) is not tuple or len(self.residual_returns) != 3:
            raise TypeError("residual_returns must be an exact three-item tuple")
        for value in (*self.raw_returns, self.common_return, *self.residual_returns):
            _finite_float(value, "joint return value")
        if not math.isclose(
            self.common_return,
            math.fsum(self.raw_returns) / 3.0,
            rel_tol=0.0,
            abs_tol=1e-15,
        ):
            raise ValueError("common return is not the equal-weight mean")
        if not math.isclose(
            math.fsum(self.residual_returns),
            0.0,
            rel_tol=0.0,
            abs_tol=1e-15,
        ):
            raise ValueError("joint residual returns do not sum to zero")


@dataclass(frozen=True, slots=True)
class SymbolFormationFeature:
    symbol: str
    residual_sum: float
    residual_sample_volatility: float
    score: float
    raw_sample_volatility: float
    movement_capacity_bp: float

    def __post_init__(self) -> None:
        if type(self.symbol) is not str or self.symbol not in UNIVERSE:
            raise ValueError("symbol feature must use the frozen universe")
        for name in (
            "residual_sum",
            "residual_sample_volatility",
            "score",
            "raw_sample_volatility",
            "movement_capacity_bp",
        ):
            _finite_float(getattr(self, name), name)
        if self.residual_sample_volatility <= VOLATILITY_FLOOR:
            raise ValueError("valid feature cannot cross the residual floor")
        if self.raw_sample_volatility < 0 or self.movement_capacity_bp < 0:
            raise ValueError("raw movement diagnostics must be non-negative")


@dataclass(frozen=True, slots=True)
class CRSFeature:
    config_id: str
    cutoff_ms: int
    common_sum: float
    common_sample_volatility: float
    common_magnitude: float
    dispersion: float
    symbols: tuple[
        SymbolFormationFeature, SymbolFormationFeature, SymbolFormationFeature
    ]

    def __post_init__(self) -> None:
        config_for_id(self.config_id)
        _exact_int(self.cutoff_ms, "cutoff_ms")
        if self.cutoff_ms % HALF_DAY_MS:
            raise ValueError("feature cutoff must be scheduled at 00:00/12:00 UTC")
        for name in (
            "common_sum",
            "common_sample_volatility",
            "common_magnitude",
            "dispersion",
        ):
            _finite_float(getattr(self, name), name)
        if self.common_sample_volatility <= VOLATILITY_FLOOR:
            raise ValueError("valid feature cannot cross the common floor")
        if self.common_magnitude < 0 or self.dispersion < 0:
            raise ValueError("feature magnitudes must be non-negative")
        if (
            type(self.symbols) is not tuple
            or any(type(item) is not SymbolFormationFeature for item in self.symbols)
            or tuple(item.symbol for item in self.symbols) != UNIVERSE
        ):
            raise ValueError("symbol feature order drifted")
        config = config_for_id(self.config_id)
        scale = math.sqrt(config.formation_return_count)
        for item in self.symbols:
            expected_score = item.residual_sum / (
                item.residual_sample_volatility * scale
            )
            if item.score != expected_score:
                raise ValueError("symbol score is not formation-derived")
            expected_movement = (
                1e4
                * math.sqrt(2.0 / math.pi)
                * item.raw_sample_volatility
                * math.sqrt(6)
            )
            if item.movement_capacity_bp != expected_movement:
                raise ValueError("movement capacity is not trailing-volatility-derived")
        scores = tuple(item.score for item in self.symbols)
        if self.dispersion != max(scores) - min(scores):
            raise ValueError("dispersion is not score-derived")
        expected_common_magnitude = abs(self.common_sum) / (
            self.common_sample_volatility * scale
        )
        if self.common_magnitude != expected_common_magnitude:
            raise ValueError("common magnitude is not formation-derived")

    def symbol(self, symbol: str) -> SymbolFormationFeature:
        for item in self.symbols:
            if item.symbol == symbol:
                return item
        raise KeyError(symbol)


@dataclass(frozen=True, slots=True)
class FeatureClosed:
    config_id: str
    cutoff_ms: int
    reason: str

    def __post_init__(self) -> None:
        config_for_id(self.config_id)
        _exact_int(self.cutoff_ms, "cutoff_ms")
        if self.cutoff_ms % HALF_DAY_MS:
            raise ValueError("closed feature cutoff must be scheduled")
        if self.reason not in {
            INPUT_HISTORY_INCOMPLETE,
            RESIDUAL_VOLATILITY_FLOOR,
            COMMON_VOLATILITY_FLOOR,
        }:
            raise ValueError("unknown feature close reason")


@dataclass(frozen=True, slots=True)
class PITGateEvaluation:
    feature: CRSFeature | FeatureClosed
    prior_valid_observations: int
    dispersion_threshold: float | None
    common_magnitude_threshold: float | None
    dispersion_pass: bool
    common_magnitude_pass: bool
    joint_pass: bool
    closed_reason: str | None

    def __post_init__(self) -> None:
        _exact_int(self.prior_valid_observations, "prior_valid_observations")
        if self.prior_valid_observations < 0:
            raise ValueError("prior observation count must be non-negative")
        for value in (self.dispersion_threshold, self.common_magnitude_threshold):
            if value is not None:
                _finite_float(value, "gate threshold")
        for value in (
            self.dispersion_pass,
            self.common_magnitude_pass,
            self.joint_pass,
        ):
            if type(value) is not bool:
                raise TypeError("gate pass values must be exact bool")
        if self.joint_pass != (self.dispersion_pass and self.common_magnitude_pass):
            raise ValueError("joint gate must equal both individual gates")
        if self.joint_pass != (self.closed_reason is None):
            raise ValueError("joint gate and close reason disagree")
        if type(self.feature) is FeatureClosed:
            if (
                self.prior_valid_observations != 0
                or self.dispersion_threshold is not None
                or self.common_magnitude_threshold is not None
                or self.dispersion_pass
                or self.common_magnitude_pass
                or self.closed_reason != self.feature.reason
            ):
                raise ValueError("feature-closed gate state is not truthful")
            return
        if type(self.feature) is not CRSFeature:
            raise TypeError("gate feature must be exact CRSFeature or FeatureClosed")
        if self.prior_valid_observations < PIT_MIN_OBSERVATIONS:
            if (
                self.dispersion_threshold is not None
                or self.common_magnitude_threshold is not None
                or self.dispersion_pass
                or self.common_magnitude_pass
                or self.closed_reason != PIT_HISTORY_BELOW_MINIMUM
            ):
                raise ValueError("PIT-minimum gate state is not truthful")
            return
        if self.dispersion_threshold is None or self.common_magnitude_threshold is None:
            raise ValueError("PIT-ready gate must carry both thresholds")
        if self.dispersion_pass != (
            self.feature.dispersion >= self.dispersion_threshold
        ):
            raise ValueError("dispersion gate result is not feature-derived")
        if self.common_magnitude_pass != (
            self.feature.common_magnitude <= self.common_magnitude_threshold
        ):
            raise ValueError("common-magnitude gate result is not feature-derived")
        expected_reason: str | None
        if not self.dispersion_pass:
            expected_reason = DISPERSION_GATE_CLOSED
        elif not self.common_magnitude_pass:
            expected_reason = COMMON_MAGNITUDE_GATE_CLOSED
        else:
            expected_reason = None
        if self.closed_reason != expected_reason:
            raise ValueError("PIT gate terminal reason is not truthful")


@dataclass(frozen=True, slots=True)
class CellGateRow:
    cutoff_ms: int
    evaluation: PITGateEvaluation

    def __post_init__(self) -> None:
        _exact_int(self.cutoff_ms, "cutoff_ms")
        if self.cutoff_ms % HALF_DAY_MS:
            raise ValueError("cell gate cutoff must be scheduled")
        if type(self.evaluation) is not PITGateEvaluation:
            raise TypeError("cell gate evaluation must be exact PITGateEvaluation")
        if self.evaluation.feature.cutoff_ms != self.cutoff_ms:
            raise ValueError("cell gate row cutoff does not match its feature")


@dataclass(frozen=True, slots=True)
class CellFeatureEvaluation:
    config_id: str
    rows: tuple[CellGateRow, ...]
    causal_source_sha256: str

    def __post_init__(self) -> None:
        config_for_id(self.config_id)
        if type(self.rows) is not tuple or any(
            type(row) is not CellGateRow for row in self.rows
        ):
            raise TypeError("cell feature rows must be an exact CellGateRow tuple")
        if len({row.cutoff_ms for row in self.rows}) != len(self.rows):
            raise ValueError("cell feature rows contain duplicate cutoffs")
        if any(row.evaluation.feature.config_id != self.config_id for row in self.rows):
            raise ValueError("cell feature config identity drifted")
        if (
            type(self.causal_source_sha256) is not str
            or len(self.causal_source_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in self.causal_source_sha256
            )
        ):
            raise ValueError("causal source identity must be lowercase SHA-256")


@dataclass(frozen=True, slots=True)
class DirectionalCandidate:
    symbol: str
    side: str
    strength: float
    score: float

    def __post_init__(self) -> None:
        if self.symbol not in UNIVERSE:
            raise ValueError("candidate symbol is outside the frozen universe")
        if self.side not in {"LONG", "SHORT"}:
            raise ValueError("candidate side must be LONG or SHORT")
        _finite_float(self.strength, "strength")
        _finite_float(self.score, "score")
        if self.strength <= 0:
            raise ValueError("candidate strength must be positive")
        if (self.side == "LONG" and self.score <= 0) or (
            self.side == "SHORT" and self.score >= 0
        ):
            raise ValueError("candidate score sign disagrees with side")
        expected_strength = self.score if self.side == "LONG" else -self.score
        if self.strength != expected_strength:
            raise ValueError("candidate strength is not score-derived")


@dataclass(frozen=True, slots=True)
class Arbitration:
    candidates: tuple[DirectionalCandidate, ...]
    winner: DirectionalCandidate | None
    closed_reason: str | None

    def __post_init__(self) -> None:
        if type(self.candidates) is not tuple or len(self.candidates) > 2:
            raise TypeError("candidates must be an exact tuple of at most two")
        if any(type(item) is not DirectionalCandidate for item in self.candidates):
            raise TypeError("candidate tuple contains an invalid item")
        if self.winner is not None and self.winner not in self.candidates:
            raise ValueError("arbitration winner must be one of the candidates")
        if (self.winner is None) != (self.closed_reason is not None):
            raise ValueError("arbitration terminal state is incomplete")
        if self.closed_reason not in {
            None,
            NO_DIRECTIONAL_CANDIDATE,
            ARBITRATION_STRENGTH_TIE,
        }:
            raise ValueError("unknown arbitration close reason")


def complete_bars_from_minutes(
    rows: dict[str, tuple[MinuteBar, ...]],
) -> dict[str, tuple[Bar4h, ...]]:
    """Reuse ROB-974 H1 complete-only aggregation for the CRS input seam."""
    if type(rows) is not dict:
        raise TypeError("minute input must be an exact built-in dict")
    if set(rows) != set(UNIVERSE) or len(rows) != len(UNIVERSE):
        raise ValueError("minute input must contain the exact frozen universe")
    snapshot: dict[str, tuple[MinuteBar, ...]] = {}
    for symbol in UNIVERSE:
        values = rows[symbol]
        if type(values) is not tuple:
            raise TypeError("minute series must be exact built-in tuples")
        snapshot[symbol] = values
    return {symbol: build_complete_4h(snapshot[symbol]) for symbol in UNIVERSE}


def _return_series(symbol: str, bars: Sequence[Bar4h]) -> dict[int, float]:
    if symbol not in UNIVERSE:
        raise ValueError("unselected symbol")
    if any(type(bar) is not Bar4h for bar in bars):
        raise TypeError("bar sequences must contain exact ROB-974 Bar4h values")
    if any(right.ts <= left.ts for left, right in zip(bars, bars[1:], strict=False)):
        raise ValueError("4h bars must be strictly increasing")
    features = symbol_features(symbol, (), tuple(bars))
    return {item.decision_ts: item.r for item in features if item.r is not None}


@dataclass(frozen=True, slots=True)
class CompleteBarSeries:
    symbol: str
    bars: tuple[Bar4h, ...]

    def __post_init__(self) -> None:
        if self.symbol not in UNIVERSE:
            raise ValueError("complete-bar series symbol is outside the universe")
        if type(self.bars) is not tuple or any(
            type(bar) is not Bar4h for bar in self.bars
        ):
            raise TypeError("complete-bar series must be an exact Bar4h tuple")
        if any(
            right.ts <= left.ts
            for left, right in zip(self.bars, self.bars[1:], strict=False)
        ):
            raise ValueError("complete-bar series must be strictly increasing")


@dataclass(frozen=True, slots=True)
class CompleteBarSnapshot:
    series: tuple[CompleteBarSeries, CompleteBarSeries, CompleteBarSeries]

    def __post_init__(self) -> None:
        if (
            type(self.series) is not tuple
            or any(type(item) is not CompleteBarSeries for item in self.series)
            or tuple(item.symbol for item in self.series) != UNIVERSE
        ):
            raise ValueError("complete-bar snapshot order drifted")

    @property
    def source_sha256(self) -> str:
        return canonical_sha256(
            [
                {
                    "symbol": item.symbol,
                    "bars": [
                        {
                            "ts": bar.ts,
                            "close_ts": bar.close_ts,
                            "open": bar.open,
                            "high": bar.high,
                            "low": bar.low,
                            "close": bar.close,
                            "volume": bar.volume,
                            "is_segment_start": bar.is_segment_start,
                        }
                        for bar in item.bars
                    ],
                }
                for item in self.series
            ]
        )

    def bars(self, symbol: str) -> tuple[Bar4h, ...]:
        if symbol not in UNIVERSE:
            raise ValueError("snapshot lookup symbol is outside the universe")
        return self.series[UNIVERSE.index(symbol)].bars


def snapshot_complete_bars(
    bars_by_symbol: dict[str, tuple[Bar4h, ...]],
) -> CompleteBarSnapshot:
    """Take one exact immutable snapshot; custom/re-evaluating mappings are closed."""
    if type(bars_by_symbol) is not dict:
        raise TypeError("bar source must be an exact built-in dict")
    if set(bars_by_symbol) != set(UNIVERSE) or len(bars_by_symbol) != len(UNIVERSE):
        raise ValueError("4h input must contain the exact frozen universe")
    series: list[CompleteBarSeries] = []
    for symbol in UNIVERSE:
        bars = bars_by_symbol[symbol]
        if type(bars) is not tuple:
            raise TypeError("bar source values must be exact built-in tuples")
        series.append(CompleteBarSeries(symbol, bars))
    return CompleteBarSnapshot(tuple(series))


def _build_joint_returns(snapshot: CompleteBarSnapshot) -> tuple[JointReturn, ...]:
    per_symbol = {
        symbol: _return_series(symbol, snapshot.bars(symbol)) for symbol in UNIVERSE
    }
    common_timestamps = sorted(
        set.intersection(*(set(per_symbol[symbol]) for symbol in UNIVERSE))
    )
    output: list[JointReturn] = []
    for close_ts in common_timestamps:
        raw = tuple(per_symbol[symbol][close_ts] for symbol in UNIVERSE)
        common = math.fsum(raw) / 3.0
        residual = tuple(value - common for value in raw)
        output.append(JointReturn(close_ts, raw, common, residual))
    return tuple(output)


def build_joint_returns(
    bars_by_symbol: dict[str, tuple[Bar4h, ...]],
) -> tuple[JointReturn, ...]:
    return _build_joint_returns(snapshot_complete_bars(bars_by_symbol))


def complete_bar_source_sha256(
    bars_by_symbol: dict[str, tuple[Bar4h, ...]],
) -> str:
    return snapshot_complete_bars(bars_by_symbol).source_sha256


class CRSFeatureGenerator:
    """Deterministic in-memory generator over already-complete 4h bars."""

    __slots__ = ("_cache", "_joint_by_ts", "_snapshot")

    def __init__(self, bars_by_symbol: dict[str, tuple[Bar4h, ...]]) -> None:
        snapshot = snapshot_complete_bars(bars_by_symbol)
        joint = _build_joint_returns(snapshot)
        self._snapshot = snapshot
        self._joint_by_ts = MappingProxyType({item.close_ts: item for item in joint})
        self._cache: dict[tuple[str, int], CRSFeature | FeatureClosed] = {}

    @property
    def snapshot_sha256(self) -> str:
        """Full synthetic-fixture identity; never emitted in a cell payload."""
        return self._snapshot.source_sha256

    @staticmethod
    def _required_return_timestamps(
        config: CRSConfig,
        cutoff_ms: int,
    ) -> tuple[int, ...]:
        required = max(
            config.formation_return_count,
            VOLATILITY_RETURN_COUNT,
        )
        return tuple(
            cutoff_ms - (required - 1 - index) * FOUR_HOUR_MS
            for index in range(required)
        )

    def feature_at(
        self, config: CRSConfig | str, cutoff_ms: int
    ) -> CRSFeature | FeatureClosed:
        return self._feature_at(config, cutoff_ms, None)

    def _feature_at(
        self,
        config: CRSConfig | str,
        cutoff_ms: int,
        source_timestamps: set[int] | None,
    ) -> CRSFeature | FeatureClosed:
        if type(config) is str:
            config = config_for_id(config)
        if type(config) is not CRSConfig:
            raise TypeError("config must be exact CRSConfig or registered ID")
        if config_for_id(config.config_id) != config:
            raise ValueError("config does not match the frozen registered row")
        _exact_int(cutoff_ms, "cutoff_ms")
        if cutoff_ms % HALF_DAY_MS:
            raise ValueError("cutoff must be 00:00/12:00 UTC")
        timestamps = self._required_return_timestamps(config, cutoff_ms)
        if source_timestamps is not None:
            source_timestamps.update(timestamps)
        cache_key = (config.config_id, cutoff_ms)
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        rows = tuple(self._joint_by_ts.get(timestamp) for timestamp in timestamps)
        if any(row is None for row in rows):
            answer: CRSFeature | FeatureClosed = FeatureClosed(
                config.config_id,
                cutoff_ms,
                INPUT_HISTORY_INCOMPLETE,
            )
            self._cache[cache_key] = answer
            return answer
        complete = tuple(row for row in rows if row is not None)
        volatility_rows = complete[-VOLATILITY_RETURN_COUNT:]
        common_sigma = _sample_volatility(
            tuple(row.common_return for row in volatility_rows)
        )
        residual_sigmas = tuple(
            _sample_volatility(
                tuple(row.residual_returns[index] for row in volatility_rows)
            )
            for index in range(3)
        )
        if any(value <= VOLATILITY_FLOOR for value in residual_sigmas):
            answer = FeatureClosed(
                config.config_id,
                cutoff_ms,
                RESIDUAL_VOLATILITY_FLOOR,
            )
            self._cache[cache_key] = answer
            return answer
        if common_sigma <= VOLATILITY_FLOOR:
            answer = FeatureClosed(
                config.config_id,
                cutoff_ms,
                COMMON_VOLATILITY_FLOOR,
            )
            self._cache[cache_key] = answer
            return answer

        formation = complete[-config.formation_return_count :]
        scale = math.sqrt(config.formation_return_count)
        residual_sums = tuple(
            math.fsum(row.residual_returns[index] for row in formation)
            for index in range(3)
        )
        scores = tuple(
            residual_sums[index] / (residual_sigmas[index] * scale)
            for index in range(3)
        )
        raw_sigmas = tuple(
            _sample_volatility(tuple(row.raw_returns[index] for row in volatility_rows))
            for index in range(3)
        )
        symbol_values = tuple(
            SymbolFormationFeature(
                symbol=symbol,
                residual_sum=residual_sums[index],
                residual_sample_volatility=residual_sigmas[index],
                score=scores[index],
                raw_sample_volatility=raw_sigmas[index],
                movement_capacity_bp=(
                    1e4 * math.sqrt(2.0 / math.pi) * raw_sigmas[index] * math.sqrt(6)
                ),
            )
            for index, symbol in enumerate(UNIVERSE)
        )
        common_sum = math.fsum(row.common_return for row in formation)
        common_magnitude = abs(common_sum) / (common_sigma * scale)
        answer = CRSFeature(
            config_id=config.config_id,
            cutoff_ms=cutoff_ms,
            common_sum=common_sum,
            common_sample_volatility=common_sigma,
            common_magnitude=common_magnitude,
            dispersion=max(scores) - min(scores),
            symbols=symbol_values,
        )
        self._cache[cache_key] = answer
        return answer

    def gates_at(self, config: CRSConfig | str, cutoff_ms: int) -> PITGateEvaluation:
        return self._gates_at(config, cutoff_ms, None)

    def _gates_at(
        self,
        config: CRSConfig | str,
        cutoff_ms: int,
        source_timestamps: set[int] | None,
    ) -> PITGateEvaluation:
        current = self._feature_at(config, cutoff_ms, source_timestamps)
        if isinstance(current, FeatureClosed):
            return PITGateEvaluation(
                feature=current,
                prior_valid_observations=0,
                dispersion_threshold=None,
                common_magnitude_threshold=None,
                dispersion_pass=False,
                common_magnitude_pass=False,
                joint_pass=False,
                closed_reason=current.reason,
            )
        prior: list[CRSFeature] = []
        timestamp = cutoff_ms - PIT_LOOKBACK_MS
        while timestamp < cutoff_ms:
            item = self._feature_at(current.config_id, timestamp, source_timestamps)
            if isinstance(item, CRSFeature):
                prior.append(item)
            timestamp += HALF_DAY_MS
        if len(prior) < PIT_MIN_OBSERVATIONS:
            return PITGateEvaluation(
                feature=current,
                prior_valid_observations=len(prior),
                dispersion_threshold=None,
                common_magnitude_threshold=None,
                dispersion_pass=False,
                common_magnitude_pass=False,
                joint_pass=False,
                closed_reason=PIT_HISTORY_BELOW_MINIMUM,
            )
        dispersion_threshold = nearest_rank(
            tuple(item.dispersion for item in prior),
            DISPERSION_QUANTILE,
        )
        common_threshold = nearest_rank(
            tuple(item.common_magnitude for item in prior),
            COMMON_MAGNITUDE_QUANTILE,
        )
        dispersion_pass = current.dispersion >= dispersion_threshold
        common_pass = current.common_magnitude <= common_threshold
        if not dispersion_pass:
            reason = DISPERSION_GATE_CLOSED
        elif not common_pass:
            reason = COMMON_MAGNITUDE_GATE_CLOSED
        else:
            reason = None
        return PITGateEvaluation(
            feature=current,
            prior_valid_observations=len(prior),
            dispersion_threshold=dispersion_threshold,
            common_magnitude_threshold=common_threshold,
            dispersion_pass=dispersion_pass,
            common_magnitude_pass=common_pass,
            joint_pass=dispersion_pass and common_pass,
            closed_reason=reason,
        )

    def evaluate_cutoffs(
        self,
        config: CRSConfig | str,
        cutoffs: tuple[int, ...],
    ) -> CellFeatureEvaluation:
        """Atomically bind cell gate derivation to its exact causal return slice."""
        if type(config) is str:
            config = config_for_id(config)
        if type(config) is not CRSConfig or config_for_id(config.config_id) != config:
            raise TypeError("config must be an exact registered CRSConfig")
        if type(cutoffs) is not tuple or any(
            type(value) is not int for value in cutoffs
        ):
            raise TypeError("cell cutoffs must be an exact built-in int tuple")
        if tuple(sorted(set(cutoffs))) != cutoffs:
            raise ValueError("cell cutoffs must be unique and strictly increasing")
        source_timestamps: set[int] = set()
        rows = tuple(
            CellGateRow(
                cutoff_ms,
                self._gates_at(config, cutoff_ms, source_timestamps),
            )
            for cutoff_ms in cutoffs
        )
        source_payload = [
            {
                "close_ts": timestamp,
                "raw_returns": (
                    None
                    if self._joint_by_ts.get(timestamp) is None
                    else list(self._joint_by_ts[timestamp].raw_returns)
                ),
            }
            for timestamp in sorted(source_timestamps)
        ]
        return CellFeatureEvaluation(
            config.config_id,
            rows,
            canonical_sha256(source_payload),
        )


def nearest_rank(values: Sequence[float], quantile: float) -> float:
    if not values:
        raise ValueError("nearest-rank quantile needs at least one value")
    if type(quantile) is not float or not math.isfinite(quantile):
        raise TypeError("quantile must be a finite built-in float")
    if not 0.0 < quantile <= 1.0:
        raise ValueError("quantile must be in (0, 1]")
    for value in values:
        _finite_float(value, "quantile value")
    ordered = sorted(values)
    return ordered[math.ceil(quantile * len(ordered)) - 1]


def directional_candidates(feature: CRSFeature) -> tuple[DirectionalCandidate, ...]:
    if type(feature) is not CRSFeature:
        raise TypeError("feature must be exact CRSFeature")
    scores = tuple(item.score for item in feature.symbols)
    maximum = max(scores)
    minimum = min(scores)
    candidates: list[DirectionalCandidate] = []
    if maximum > 0:
        index = scores.index(maximum)
        candidates.append(
            DirectionalCandidate(
                symbol=UNIVERSE[index],
                side="LONG",
                strength=maximum,
                score=maximum,
            )
        )
    if minimum < 0:
        index = scores.index(minimum)
        candidates.append(
            DirectionalCandidate(
                symbol=UNIVERSE[index],
                side="SHORT",
                strength=-minimum,
                score=minimum,
            )
        )
    return tuple(candidates)


def arbitrate(feature: CRSFeature) -> Arbitration:
    candidates = directional_candidates(feature)
    if not candidates:
        return Arbitration(candidates, None, NO_DIRECTIONAL_CANDIDATE)
    if len(candidates) == 1:
        return Arbitration(candidates, candidates[0], None)
    long_candidate, short_candidate = candidates
    if abs(long_candidate.strength - short_candidate.strength) < ARBITRATION_TOLERANCE:
        return Arbitration(candidates, None, ARBITRATION_STRENGTH_TIE)
    winner = (
        long_candidate
        if long_candidate.strength > short_candidate.strength
        else short_candidate
    )
    return Arbitration(candidates, winner, None)


__all__ = [
    "ARBITRATION_STRENGTH_TIE",
    "COMMON_MAGNITUDE_GATE_CLOSED",
    "COMMON_VOLATILITY_FLOOR",
    "CRSFeature",
    "CRSFeatureGenerator",
    "CellFeatureEvaluation",
    "CellGateRow",
    "CompleteBarSeries",
    "CompleteBarSnapshot",
    "DirectionalCandidate",
    "DISPERSION_GATE_CLOSED",
    "FeatureClosed",
    "INPUT_HISTORY_INCOMPLETE",
    "JointReturn",
    "NO_DIRECTIONAL_CANDIDATE",
    "PITGateEvaluation",
    "PIT_HISTORY_BELOW_MINIMUM",
    "RESIDUAL_VOLATILITY_FLOOR",
    "SymbolFormationFeature",
    "Arbitration",
    "arbitrate",
    "build_joint_returns",
    "complete_bar_source_sha256",
    "complete_bars_from_minutes",
    "directional_candidates",
    "nearest_rank",
    "snapshot_complete_bars",
]

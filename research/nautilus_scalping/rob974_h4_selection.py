"""ROB-982 H4 post-arbitration, pre-H2-open funding authority.

This seam deliberately cannot receive a ranked candidate list: H3 has already
made one global winner.  A failed exact-entry or funding gate therefore has no
fallback/rerank route before the actual H2 opening adapter is called.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass


def _int(value: object, name: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{name} must be built-in int")
    return value


def _float(value: object, name: str) -> float:
    if type(value) is not float:
        raise TypeError(f"{name} must be built-in float")
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    return value


@dataclass(frozen=True, slots=True)
class PITFundingObservation:
    timestamp_ms: int
    signed_bps: float

    def __post_init__(self) -> None:
        _int(self.timestamp_ms, "timestamp_ms")
        _float(self.signed_bps, "signed_bps")


@dataclass(frozen=True, slots=True)
class FundingGateResult:
    accepted: bool
    reason: str | None
    expected_signed_bps: float | None

    def __post_init__(self) -> None:
        if type(self.accepted) is not bool:
            raise TypeError("accepted must be built-in bool")
        if self.reason not in (
            None,
            "funding_evidence_unavailable",
            "expected_funding_cost_above_3bps",
        ):
            raise ValueError("funding reason is outside the closed taxonomy")
        if self.expected_signed_bps is not None:
            _float(self.expected_signed_bps, "expected_signed_bps")
        if self.accepted != (self.reason is None):
            raise ValueError("funding result acceptance/reason mismatch")


def last_known_pit_funding(*, observations: object, entry_ts: object) -> float | None:
    """Return one last-known signed value at/before entry, fail closed otherwise."""
    entry = _int(entry_ts, "entry_ts")
    if type(observations) is not tuple:
        raise TypeError("observations must be a built-in tuple")
    latest: PITFundingObservation | None = None
    for observation in observations:
        if type(observation) is not PITFundingObservation:
            raise TypeError("observations must be exact PITFundingObservation values")
        if observation.timestamp_ms <= entry and (
            latest is None or observation.timestamp_ms > latest.timestamp_ms
        ):
            latest = observation
    return None if latest is None else latest.signed_bps


def _gate(expected_signed_bps: object) -> FundingGateResult:
    if expected_signed_bps is None:
        return FundingGateResult(False, "funding_evidence_unavailable", None)
    expected = _float(expected_signed_bps, "expected_signed_bps")
    if expected > 3.0:
        return FundingGateResult(False, "expected_funding_cost_above_3bps", expected)
    return FundingGateResult(True, None, expected)


def s3_funding_gate(expected_signed_bps: object) -> FundingGateResult:
    """S3's signed debit limit is strict: exactly 3bp and credits pass."""
    return _gate(expected_signed_bps)


def s4_funding_gate(
    *,
    leg_a_signed_bps: object,
    leg_b_signed_bps: object,
    weight_a: object,
    weight_b: object,
) -> FundingGateResult:
    """Both leg PIT evidence is mandatory; entry-frozen basket cost is once."""
    if leg_a_signed_bps is None or leg_b_signed_bps is None:
        return FundingGateResult(False, "funding_evidence_unavailable", None)
    a = _float(leg_a_signed_bps, "leg_a_signed_bps")
    b = _float(leg_b_signed_bps, "leg_b_signed_bps")
    wa = _float(weight_a, "weight_a")
    wb = _float(weight_b, "weight_b")
    if wa <= 0.0 or wb <= 0.0 or abs((wa + wb) - 1.0) > 1e-12:
        raise ValueError("entry-frozen S4 weights must be positive and sum to one")
    return _gate(wa * a + wb * b)


def invoke_after_arbitration[Winner, Entry, Opened](
    *,
    winner: Winner,
    resolve_exact_entry: Callable[[Winner], Entry | None],
    funding_gate: Callable[[Winner, Entry], FundingGateResult],
    h2_open: Callable[[Winner, Entry], Opened],
) -> tuple[str, FundingGateResult | None, Opened | None]:
    """Enforce H3 winner → exact entry → funding → actual H2 open ordering."""
    entry = resolve_exact_entry(winner)
    if entry is None:
        return ("no_trade_missing_exact_entry", None, None)
    gate = funding_gate(winner, entry)
    if type(gate) is not FundingGateResult:
        raise TypeError("funding_gate must return exact FundingGateResult")
    if not gate.accepted:
        return (gate.reason or "funding_gate_rejected", gate, None)
    return ("opened", gate, h2_open(winner, entry))


@dataclass(frozen=True, slots=True)
class TrainUnitMetric:
    unit: str
    completed_basket_trades: int
    e17_bps: float

    def __post_init__(self) -> None:
        if type(self.unit) is not str or not self.unit:
            raise TypeError("unit must be a non-empty built-in str")
        if _int(self.completed_basket_trades, "completed_basket_trades") < 0:
            raise ValueError("completed_basket_trades must not be negative")
        _float(self.e17_bps, "e17_bps")


@dataclass(frozen=True, slots=True)
class TrainCandidateTrace:
    config_id: str
    units: tuple[TrainUnitMetric, ...]
    pf: float
    pooled_e17_bps: float
    train_input_hash: str
    train_scenario_hash: str

    def __post_init__(self) -> None:
        if type(self.config_id) is not str:
            raise TypeError("config_id must be built-in str")
        if type(self.units) is not tuple or not self.units:
            raise TypeError("units must be a non-empty built-in tuple")
        if any(type(unit) is not TrainUnitMetric for unit in self.units):
            raise TypeError("units must contain exact TrainUnitMetric values")
        if len({unit.unit for unit in self.units}) != len(self.units):
            raise ValueError("TRAIN units must be unique")
        _float(self.pf, "pf")
        _float(self.pooled_e17_bps, "pooled_e17_bps")
        for name in ("train_input_hash", "train_scenario_hash"):
            value = getattr(self, name)
            if (
                type(value) is not str
                or len(value) != 64
                or any(char not in "0123456789abcdef" for char in value)
            ):
                raise ValueError(f"{name} must be lowercase SHA-256")

    @property
    def eligible_units(self) -> tuple[TrainUnitMetric, ...]:
        return tuple(unit for unit in self.units if unit.completed_basket_trades >= 5)

    @property
    def eligible_unit_mean_e17_bps(self) -> float | None:
        eligible = self.eligible_units
        if len(eligible) < 2:
            return None
        return math.fsum(unit.e17_bps for unit in eligible) / len(eligible)


@dataclass(frozen=True, slots=True)
class TrainSelection:
    strategy: str
    traces: tuple[TrainCandidateTrace, ...]
    selected_config_id: str | None
    tie_break: str | None

    def __post_init__(self) -> None:
        if type(self.strategy) is not str or self.strategy not in ("S3", "S4"):
            raise ValueError("strategy must be S3 or S4")
        if type(self.traces) is not tuple or len(self.traces) != 24:
            raise ValueError("selection requires exactly 24 TRAIN traces")
        expected = tuple(f"{self.strategy}-{index:02d}" for index in range(24))
        if tuple(trace.config_id for trace in self.traces) != expected:
            raise ValueError("TRAIN traces must use the exact ordered config roster")
        eligible_ids = {
            trace.config_id
            for trace in self.traces
            if trace.eligible_unit_mean_e17_bps is not None
        }
        if (
            self.selected_config_id is not None
            and self.selected_config_id not in eligible_ids
        ):
            raise ValueError("selected config must be TRAIN-eligible")
        if (self.selected_config_id is None) != (self.tie_break is None):
            raise ValueError("selection/tie-break must be jointly present or absent")


def select_train_config(strategy: object, traces: object) -> TrainSelection:
    """Choose one shared winner from TRAIN metrics only, never pooled E17."""
    if type(strategy) is not str or strategy not in ("S3", "S4"):
        raise ValueError("strategy must be S3 or S4")
    if type(traces) is not tuple or any(
        type(trace) is not TrainCandidateTrace for trace in traces
    ):
        raise TypeError("traces must be a built-in tuple of exact TrainCandidateTrace")
    eligible = [
        trace for trace in traces if trace.eligible_unit_mean_e17_bps is not None
    ]
    if not eligible:
        return TrainSelection(strategy, traces, None, None)
    winner = min(
        eligible,
        key=lambda trace: (
            -float(trace.eligible_unit_mean_e17_bps),
            -trace.pf,
            trace.config_id,
        ),
    )
    return TrainSelection(
        strategy,
        traces,
        winner.config_id,
        "eligible_unit_equal_weight_E17_desc_then_PF_desc_then_config_id_asc",
    )


def run_train_global_configs[Config, Generated, EngineResult](
    *,
    strategy: object,
    configs: object,
    generator: Callable[[Config], Generated],
    fresh_primary_engine: Callable[[], Callable[[Generated], EngineResult]],
) -> tuple[EngineResult, ...]:
    """Invoke one global generator and independently fresh engine per config."""
    if type(strategy) is not str or strategy not in ("S3", "S4"):
        raise ValueError("strategy must be S3 or S4")
    if type(configs) is not tuple or len(configs) != 24:
        raise ValueError("configs must be the exact ordered 24-row tuple")
    expected = tuple(f"{strategy}-{index:02d}" for index in range(24))
    if tuple(getattr(config, "config_id", None) for config in configs) != expected:
        raise ValueError("configs must use the exact canonical roster/order")
    engine_ids: set[int] = set()
    engines: list[Callable[[Generated], EngineResult]] = []
    results: list[EngineResult] = []
    for config in configs:
        engine = fresh_primary_engine()
        if not callable(engine) or id(engine) in engine_ids:
            raise ValueError("each TRAIN config requires an independently fresh engine")
        engine_ids.add(id(engine))
        engines.append(engine)
        results.append(engine(generator(config)))
    return tuple(results)


__all__ = [
    "FundingGateResult",
    "PITFundingObservation",
    "TrainCandidateTrace",
    "TrainSelection",
    "TrainUnitMetric",
    "invoke_after_arbitration",
    "last_known_pit_funding",
    "s3_funding_gate",
    "s4_funding_gate",
    "select_train_config",
    "run_train_global_configs",
]

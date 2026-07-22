"""Strict ROB-974 R3 S4 execution DTO lineage.

R2's historical S4 DTOs deliberately reject ``|z_entry| < 1``.  R3 keeps
those frozen bytes intact and names a separate boundary where the signed
observed entry z and the unsigned registered-cell threshold cannot be
confused.  The read-only ``z_entry`` property exists solely because the
frozen mechanical position walk reads that historical attribute name.

Pure stdlib plus frozen manifest/constant authorities: no persistence,
network, broker, order, fill, scheduler, random, or current-time behavior.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from rob974_h2_dtos import (
    BETA_MAX,
    BETA_MIN,
    PAIR_EXEC_FAIL_NOT_EVALUATED,
    PROMOTION_BLOCKED_PENDING_PAIR_EXECUTOR,
    UNIVERSE,
    S4ExitReason,
    Side,
)
from rob974_r3_manifest import R3S4Config, get_r3_config

__all__ = [
    "R3S4EngineResult",
    "R3S4IncompleteRecord",
    "R3S4NoTradeRecord",
    "R3S4PairSignalIntent",
    "R3S4PairTrade",
]


def _exact_int(value: object, name: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{name} must be an exact built-in int")
    return value


def _finite_float(value: object, name: str) -> float:
    if type(value) is not float:
        raise TypeError(f"{name} must be an exact built-in float")
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    return value


def _positive(value: float, name: str) -> float:
    if value <= 0.0:
        raise ValueError(f"{name} must be positive")
    return value


def _symbol(value: object, name: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{name} must be an exact built-in str")
    if value not in UNIVERSE:
        raise ValueError(f"{name} is outside the frozen universe")
    return value


def _pair(value: object) -> tuple[str, str]:
    if type(value) is not tuple or len(value) != 2:
        raise TypeError("pair must be an exact two-symbol tuple")
    symbol_a = _symbol(value[0], "pair[0]")
    symbol_b = _symbol(value[1], "pair[1]")
    if symbol_a == symbol_b:
        raise ValueError("pair legs must be distinct")
    return symbol_a, symbol_b


def _side(value: object, name: str) -> Side:
    if type(value) is not str or value not in ("long", "short"):
        raise ValueError(f"{name} must be exact 'long' or 'short'")
    return value  # type: ignore[return-value]


def _config(config_id: object, z_threshold: object | None = None) -> R3S4Config:
    if type(config_id) is not str or not config_id:
        raise TypeError("config_id must be a non-empty exact built-in str")
    config = get_r3_config(config_id)
    if type(config) is not R3S4Config:
        raise ValueError("config_id must identify an exact registered R3 S4 cell")
    if z_threshold is not None:
        threshold = _finite_float(z_threshold, "z_threshold")
        if threshold != config.z_entry:
            raise ValueError("z_threshold differs from its registered R3 config")
    return config


def _fold_id(value: object) -> str | None:
    if value is None:
        return None
    if type(value) is not str or not value:
        raise TypeError("fold_id must be None or a non-empty exact built-in str")
    return value


def _validate_entry_z(
    *, observed_z: object, z_threshold: object, config_id: object
) -> tuple[float, float]:
    config = _config(config_id, z_threshold)
    observed = _finite_float(observed_z, "observed_z")
    threshold = _finite_float(z_threshold, "z_threshold")
    if abs(observed) < threshold:
        raise ValueError("observed_z is below its registered R3 threshold")
    if threshold < 0.60:
        raise ValueError("registered R3 S4 threshold is below the frozen 0.60 floor")
    if threshold != config.z_entry:  # pragma: no cover - guarded by _config
        raise AssertionError("registered threshold drifted during validation")
    return observed, threshold


def _validate_direction(observed_z: float, side_a: Side, side_b: Side) -> None:
    expected = ("short", "long") if observed_z > 0.0 else ("long", "short")
    if (side_a, side_b) != expected:
        raise ValueError("signed observed_z disagrees with the pair-leg direction")


def _validate_weights_and_betas(
    *, weight_a: float, weight_b: float, beta_a: float, beta_b: float
) -> None:
    _positive(weight_a, "weight_a")
    _positive(weight_b, "weight_b")
    if abs((weight_a + weight_b) - 1.0) > 1e-9:
        raise ValueError("weight_a+weight_b must equal 1.0 within 1e-9")
    if not BETA_MIN <= beta_a <= BETA_MAX:
        raise ValueError("beta_a is outside the frozen clipped-beta range")
    if not BETA_MIN <= beta_b <= BETA_MAX:
        raise ValueError("beta_b is outside the frozen clipped-beta range")


@dataclass(frozen=True, slots=True)
class R3S4PairSignalIntent:
    """One registered R3 historical pair-basket entry intent."""

    pair: tuple[str, str]
    signal_ts: int
    side_a: Side
    side_b: Side
    weight_a: float
    weight_b: float
    beta_a: float
    beta_b: float
    mu: float
    sigma: float
    observed_z: float
    z_threshold: float
    gross_notional: float
    entry_sl_distance: float
    entry_tp_distance: float
    config_id: str
    fold_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "pair", _pair(self.pair))
        object.__setattr__(self, "signal_ts", _exact_int(self.signal_ts, "signal_ts"))
        object.__setattr__(self, "side_a", _side(self.side_a, "side_a"))
        object.__setattr__(self, "side_b", _side(self.side_b, "side_b"))
        for name in (
            "weight_a",
            "weight_b",
            "beta_a",
            "beta_b",
            "mu",
            "sigma",
            "gross_notional",
            "entry_sl_distance",
            "entry_tp_distance",
        ):
            object.__setattr__(self, name, _finite_float(getattr(self, name), name))
        observed_z, z_threshold = _validate_entry_z(
            observed_z=self.observed_z,
            z_threshold=self.z_threshold,
            config_id=self.config_id,
        )
        object.__setattr__(self, "observed_z", observed_z)
        object.__setattr__(self, "z_threshold", z_threshold)
        _validate_direction(observed_z, self.side_a, self.side_b)
        _validate_weights_and_betas(
            weight_a=self.weight_a,
            weight_b=self.weight_b,
            beta_a=self.beta_a,
            beta_b=self.beta_b,
        )
        _positive(self.sigma, "sigma")
        _positive(self.gross_notional, "gross_notional")
        _positive(self.entry_sl_distance, "entry_sl_distance")
        _positive(self.entry_tp_distance, "entry_tp_distance")
        _config(self.config_id, self.z_threshold)
        object.__setattr__(self, "fold_id", _fold_id(self.fold_id))

    @property
    def z_entry(self) -> float:
        """Frozen-walk compatibility alias for the signed observed value."""

        return self.observed_z


@dataclass(frozen=True, slots=True)
class R3S4PairTrade:
    """One atomic R3 pair-basket result with historical-only posture."""

    pair: tuple[str, str]
    side_a: Side
    side_b: Side
    config_id: str
    fold_id: str | None
    signal_ts: int
    entry_ts: int
    weight_a: float
    weight_b: float
    beta_a: float
    beta_b: float
    mu: float
    sigma: float
    observed_z: float
    z_threshold: float
    gross_notional: float
    entry_price_a: float
    entry_price_b: float
    exit_ts: int
    exit_price_a: float
    exit_price_b: float
    exit_reason: S4ExitReason
    mfe_bps: float
    mae_bps: float
    gross_bps: float
    order_id_a: str | None
    order_id_b: str | None
    pair_exec_status: str
    pair_executor_validated: bool
    demo_eligible: bool
    volatility_percentile: float | None
    volatility_percentile_provenance: str
    pair_exec_fail: str = PAIR_EXEC_FAIL_NOT_EVALUATED
    promotion_status: str = PROMOTION_BLOCKED_PENDING_PAIR_EXECUTOR

    def __post_init__(self) -> None:
        object.__setattr__(self, "pair", _pair(self.pair))
        object.__setattr__(self, "side_a", _side(self.side_a, "side_a"))
        object.__setattr__(self, "side_b", _side(self.side_b, "side_b"))
        object.__setattr__(self, "fold_id", _fold_id(self.fold_id))
        for name in ("signal_ts", "entry_ts", "exit_ts"):
            object.__setattr__(self, name, _exact_int(getattr(self, name), name))
        for name in (
            "weight_a",
            "weight_b",
            "beta_a",
            "beta_b",
            "mu",
            "sigma",
            "gross_notional",
            "entry_price_a",
            "entry_price_b",
            "exit_price_a",
            "exit_price_b",
            "mfe_bps",
            "mae_bps",
            "gross_bps",
        ):
            object.__setattr__(self, name, _finite_float(getattr(self, name), name))
        observed_z, z_threshold = _validate_entry_z(
            observed_z=self.observed_z,
            z_threshold=self.z_threshold,
            config_id=self.config_id,
        )
        object.__setattr__(self, "observed_z", observed_z)
        object.__setattr__(self, "z_threshold", z_threshold)
        _validate_direction(observed_z, self.side_a, self.side_b)
        _validate_weights_and_betas(
            weight_a=self.weight_a,
            weight_b=self.weight_b,
            beta_a=self.beta_a,
            beta_b=self.beta_b,
        )
        _positive(self.sigma, "sigma")
        _positive(self.gross_notional, "gross_notional")
        for name in ("entry_price_a", "entry_price_b", "exit_price_a", "exit_price_b"):
            _positive(getattr(self, name), name)
        if self.exit_ts < self.entry_ts:
            raise ValueError("exit_ts must be >= entry_ts")
        if self.exit_reason not in ("TP", "SL", "MEAN_EXIT", "STALL_EXIT", "TIMEOUT"):
            raise ValueError("exit_reason is outside the frozen S4 vocabulary")
        if self.order_id_a is not None or self.order_id_b is not None:
            raise ValueError("historical R3 S4 order IDs must both be None")
        if (
            type(self.pair_executor_validated) is not bool
            or self.pair_executor_validated
        ):
            raise ValueError("historical pair_executor_validated must be exact False")
        if type(self.demo_eligible) is not bool or self.demo_eligible:
            raise ValueError("historical demo_eligible must be exact False")
        if self.pair_exec_status != "historical_atomic_assumption":
            raise ValueError("historical pair_exec_status authority drifted")
        if self.volatility_percentile is not None:
            raise ValueError("R3 S4 volatility_percentile must be exactly None")
        if self.volatility_percentile_provenance != "not_defined_for_s4":
            raise ValueError("R3 S4 volatility provenance authority drifted")
        if self.pair_exec_fail != PAIR_EXEC_FAIL_NOT_EVALUATED:
            raise ValueError("historical PAIR_EXEC_FAIL must remain not_evaluated")
        if self.promotion_status != PROMOTION_BLOCKED_PENDING_PAIR_EXECUTOR:
            raise ValueError("historical promotion posture authority drifted")
        _config(self.config_id, self.z_threshold)

    @property
    def z_entry(self) -> float:
        """Compatibility alias used only by common economic projections."""

        return self.observed_z


@dataclass(frozen=True, slots=True)
class R3S4NoTradeRecord:
    pair: tuple[str, str]
    config_id: str
    fold_id: str | None
    signal_ts: int
    reason: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "pair", _pair(self.pair))
        _config(self.config_id)
        object.__setattr__(self, "fold_id", _fold_id(self.fold_id))
        object.__setattr__(self, "signal_ts", _exact_int(self.signal_ts, "signal_ts"))
        if type(self.reason) is not str or not self.reason:
            raise TypeError("reason must be a non-empty exact built-in str")


@dataclass(frozen=True, slots=True)
class R3S4IncompleteRecord:
    pair: tuple[str, str]
    side_a: Side
    side_b: Side
    config_id: str
    fold_id: str | None
    signal_ts: int
    entry_ts: int
    entry_price_a: float
    entry_price_b: float
    reason: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "pair", _pair(self.pair))
        object.__setattr__(self, "side_a", _side(self.side_a, "side_a"))
        object.__setattr__(self, "side_b", _side(self.side_b, "side_b"))
        if (self.side_a, self.side_b) not in (
            ("short", "long"),
            ("long", "short"),
        ):
            raise ValueError("R3 S4 incomplete legs must have opposing sides")
        _config(self.config_id)
        object.__setattr__(self, "fold_id", _fold_id(self.fold_id))
        for name in ("signal_ts", "entry_ts"):
            object.__setattr__(self, name, _exact_int(getattr(self, name), name))
        if self.entry_ts < self.signal_ts:
            raise ValueError("R3 S4 incomplete entry_ts must not precede signal_ts")
        for name in ("entry_price_a", "entry_price_b"):
            value = _finite_float(getattr(self, name), name)
            object.__setattr__(self, name, value)
            _positive(value, name)
        allowed = (
            "data_gap_in_pair_position",
            "early_eof",
            "missing_future_data",
            "fold_horizon_rejected",
        )
        if self.reason not in allowed:
            raise ValueError("R3 S4 incomplete reason is outside the frozen vocabulary")


@dataclass(frozen=True, slots=True)
class R3S4EngineResult:
    trades: tuple[R3S4PairTrade, ...]
    no_trades: tuple[R3S4NoTradeRecord, ...]
    incompletes: tuple[R3S4IncompleteRecord, ...]

    def __post_init__(self) -> None:
        expected = (
            ("trades", R3S4PairTrade),
            ("no_trades", R3S4NoTradeRecord),
            ("incompletes", R3S4IncompleteRecord),
        )
        for name, row_type in expected:
            rows = getattr(self, name)
            if type(rows) is not tuple or any(
                type(row) is not row_type for row in rows
            ):
                raise TypeError(
                    f"{name} must be an exact tuple of exact {row_type.__name__}"
                )

"""Pure ROB-974 R3 relaxation-layer and monotone-edge evidence.

The public entry point consumes the complete canonical 12-cell by eight-fold
ledger.  Callers cannot provide edges: the frozen directed rays below are the
only graph authority.  The module has no database, network, broker, filesystem,
or wall-clock dependencies.
"""

from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass, replace
from hashlib import sha256
from typing import Literal

R3_FOLD_IDS: tuple[str, ...] = tuple(f"fold-{index:02d}" for index in range(8))
R3_CONFIG_IDS: tuple[str, ...] = tuple(
    f"S3-R3-{index:02d}" for index in range(3)
) + tuple(f"S4-R3-{index:02d}" for index in range(9))
BOOTSTRAP_RESAMPLES = 10_000
SAMPLE_QUALIFYING_BASKET_TRADES = 5

_SYMBOLS = ("XRPUSDT", "DOGEUSDT", "SOLUSDT")
_S4_PAIRS = (
    ("XRPUSDT", "DOGEUSDT"),
    ("XRPUSDT", "SOLUSDT"),
    ("DOGEUSDT", "SOLUSDT"),
)
_LEG_SIDES = ("long", "short")
_S3_DIRECTIONS = ("long", "short")
_S4_DIRECTIONS = ("long_a_short_b", "short_a_long_b")
_S3_EXIT_REASONS = ("TP", "SL", "THESIS_EXIT", "TIMEOUT")
_S4_EXIT_REASONS = ("TP", "SL", "MEAN_EXIT", "STALL_EXIT", "TIMEOUT")

Phase = Literal["TRAIN", "OOS"]
Family = Literal["S3", "S4"]
Side = Literal["long", "short"]
Direction = Literal["long", "short", "long_a_short_b", "short_a_long_b"]
OperationalStatus = Literal["COMPLETE", "INCOMPLETE"]


class RelaxationInputError(ValueError):
    """The supplied evidence is malformed and cannot be interpreted."""


def _exact_str(value: object, name: str) -> str:
    if type(value) is not str or not value:
        raise TypeError(f"{name} must be a non-empty built-in str")
    return value


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


def _exact_tuple(value: object, name: str) -> tuple[object, ...]:
    if type(value) is not tuple:
        raise TypeError(f"{name} must be an exact built-in tuple")
    return value


def _mean_gross_bps(trades: tuple[RelaxationTrade, ...]) -> float | None:
    if not trades:
        return None
    return math.fsum(trade.execution.gross_bps for trade in trades) / len(trades)


@dataclass(frozen=True, slots=True)
class EconomicEvent:
    """Config-independent identity of one economic signal event.

    Config, row, experiment, phase, and fold lineage are deliberately absent.
    Fold is validated outside the key so reuse of an event across folds is a
    detectable collision rather than two apparently distinct trades.
    """

    family: Family
    instruments: tuple[str, ...]
    signal_ts: int
    direction: Direction

    def __post_init__(self) -> None:
        if type(self.family) is not str or self.family not in ("S3", "S4"):
            raise ValueError("family must be exact R3 family S3 or S4")
        _exact_tuple(self.instruments, "instruments")
        if any(type(symbol) is not str for symbol in self.instruments):
            raise TypeError("every instrument must be a built-in str")
        if any(symbol not in _SYMBOLS for symbol in self.instruments):
            raise ValueError("economic identity contains a non-frozen instrument")
        if self.family == "S3" and (
            len(self.instruments) != 1 or self.instruments[0] not in _SYMBOLS
        ):
            raise ValueError("S3 economic identity requires one frozen instrument")
        if self.family == "S4" and self.instruments not in _S4_PAIRS:
            raise ValueError("S4 economic identity requires one canonical frozen pair")
        _exact_int(self.signal_ts, "signal_ts")
        if self.signal_ts < 0:
            raise ValueError("signal_ts must be non-negative")
        if self.family == "S3" and (
            type(self.direction) is not str or self.direction not in _S3_DIRECTIONS
        ):
            raise ValueError("S3 direction must be long or short")
        if self.family == "S4" and (
            type(self.direction) is not str or self.direction not in _S4_DIRECTIONS
        ):
            raise ValueError("S4 direction must be long_a_short_b or short_a_long_b")


@dataclass(frozen=True, slots=True)
class TradeExecution:
    """Normalized execution/economics bytes with all config lineage removed."""

    entry_ts: int
    exit_ts: int
    leg_sides: tuple[Side, ...]
    entry_prices: tuple[float, ...]
    exit_prices: tuple[float, ...]
    leg_weights: tuple[float, ...]
    gross_notional: float | None
    mfe_bps: float
    mae_bps: float
    gross_bps: float
    exit_reason: str

    def __post_init__(self) -> None:
        _exact_int(self.entry_ts, "entry_ts")
        _exact_int(self.exit_ts, "exit_ts")
        if self.entry_ts < 0 or self.exit_ts < self.entry_ts:
            raise ValueError("execution timestamps must be ordered and non-negative")
        for name in ("leg_sides", "entry_prices", "exit_prices", "leg_weights"):
            _exact_tuple(getattr(self, name), name)
        leg_count = len(self.leg_sides)
        if leg_count not in (1, 2):
            raise ValueError("execution must contain one S3 leg or two S4 legs")
        if not (
            len(self.entry_prices)
            == len(self.exit_prices)
            == len(self.leg_weights)
            == leg_count
        ):
            raise ValueError("all execution leg tuples must have the same length")
        if any(
            type(side) is not str or side not in _LEG_SIDES for side in self.leg_sides
        ):
            raise ValueError("every leg side must be long or short")
        for name in ("entry_prices", "exit_prices", "leg_weights"):
            values = getattr(self, name)
            for index, value in enumerate(values):
                number = _finite_float(value, f"{name}[{index}]")
                if number <= 0.0:
                    raise ValueError(f"{name}[{index}] must be positive")
        if abs(sum(self.leg_weights) - 1.0) > 1e-9:
            raise ValueError("leg_weights must sum to 1.0 within the frozen 1e-9 rule")
        if self.gross_notional is not None:
            _finite_float(self.gross_notional, "gross_notional")
            if self.gross_notional <= 0.0:
                raise ValueError("gross_notional must be positive when present")
        for name in ("mfe_bps", "mae_bps", "gross_bps"):
            _finite_float(getattr(self, name), name)
        _exact_str(self.exit_reason, "exit_reason")


@dataclass(frozen=True, slots=True)
class RelaxationTrade:
    event: EconomicEvent
    execution: TradeExecution

    def __post_init__(self) -> None:
        if type(self.event) is not EconomicEvent:
            raise TypeError("event must be an exact EconomicEvent")
        if type(self.execution) is not TradeExecution:
            raise TypeError("execution must be an exact TradeExecution")
        expected_legs = 1 if self.event.family == "S3" else 2
        if len(self.execution.leg_sides) != expected_legs:
            raise ValueError("event family and execution leg count disagree")
        if self.execution.entry_ts < self.event.signal_ts:
            raise ValueError("entry_ts must not precede the economic signal")
        if self.event.family == "S3":
            if self.execution.gross_notional is not None:
                raise ValueError("S3 gross_notional must be absent/None")
            if self.execution.exit_reason not in _S3_EXIT_REASONS:
                raise ValueError("S3 exit_reason is outside the frozen H2 values")
            if self.execution.leg_sides != (self.event.direction,):
                raise ValueError(
                    "S3 direction must equal its single execution leg side"
                )
            return
        if self.execution.gross_notional is None:
            raise ValueError("S4 gross_notional is required")
        if self.execution.exit_reason not in _S4_EXIT_REASONS:
            raise ValueError("S4 exit_reason is outside the frozen H2 values")
        expected_sides = (
            ("long", "short")
            if self.event.direction == "long_a_short_b"
            else ("short", "long")
        )
        if self.execution.leg_sides != expected_sides:
            raise ValueError("S4 direction and opposing execution leg sides disagree")
        for index, weight in enumerate(self.execution.leg_weights):
            leg_notional = weight * self.execution.gross_notional
            if not 6.0 <= leg_notional <= 10.0:
                raise ValueError(
                    f"S4 leg {index} notional must remain in the frozen $6-10 range"
                )


@dataclass(frozen=True, slots=True)
class CellFoldLedger:
    config_id: str
    fold_id: str
    basket_trade_count: int
    trades: tuple[RelaxationTrade, ...]

    def __post_init__(self) -> None:
        _exact_str(self.config_id, "config_id")
        _exact_str(self.fold_id, "fold_id")
        _exact_int(self.basket_trade_count, "basket_trade_count")
        _exact_tuple(self.trades, "trades")
        if self.basket_trade_count < 0:
            raise RelaxationInputError("basket_trade_count must be non-negative")
        if any(type(trade) is not RelaxationTrade for trade in self.trades):
            raise TypeError("trades must contain exact RelaxationTrade values")
        if self.basket_trade_count != len(self.trades):
            raise RelaxationInputError(
                "basket_trade_count must equal the exact trade ledger length"
            )


@dataclass(frozen=True, slots=True)
class RelaxationRay:
    ray_id: str
    family: Family
    cells: tuple[str, ...]


R3_RELAXATION_RAYS: tuple[RelaxationRay, ...] = (
    RelaxationRay("S3:S@M0", "S3", ("S3-R3-00", "S3-R3-02")),
    RelaxationRay("S3:M@S0", "S3", ("S3-R3-01", "S3-R3-02")),
    RelaxationRay(
        "S4:z@d140",
        "S4",
        ("S4-R3-00", "S4-R3-02", "S4-R3-05", "S4-R3-08"),
    ),
    RelaxationRay("S4:z@d160", "S4", ("S4-R3-01", "S4-R3-04", "S4-R3-07")),
    RelaxationRay("S4:z@d180", "S4", ("S4-R3-03", "S4-R3-06")),
    RelaxationRay("S4:d@z1.0", "S4", ("S4-R3-01", "S4-R3-02")),
    RelaxationRay("S4:d@z0.8", "S4", ("S4-R3-03", "S4-R3-04", "S4-R3-05")),
    RelaxationRay("S4:d@z0.6", "S4", ("S4-R3-06", "S4-R3-07", "S4-R3-08")),
)


@dataclass(frozen=True, slots=True)
class FoldLayerCohort:
    fold_id: str
    strict_basket_trade_count: int
    looser_basket_trade_count: int
    sample_qualified: bool
    exclusion_reason: str | None
    strict_core: tuple[RelaxationTrade, ...]
    cumulative_looser: tuple[RelaxationTrade, ...]
    new_layer: tuple[RelaxationTrade, ...]
    strict_core_e0_bps: float | None
    cumulative_looser_e0_bps: float | None
    new_layer_e0_bps: float | None
    delta_e0_bps: float | None


@dataclass(frozen=True, slots=True)
class ExactSignTest:
    alternative: Literal["less"]
    negative_count: int
    positive_count: int
    tie_count: int
    effective_n: int
    p_value_numerator: int
    p_value_denominator: int
    p_value: float | None
    no_result_reason: str | None


@dataclass(frozen=True, slots=True)
class FoldBlockBootstrap:
    resamples: int
    resampling_unit: Literal["paired_fold_blocks"]
    fold_block_count: int
    seed_sha256: str
    seed: int
    percentile_method: Literal["linear_r7"]
    ci_level: float
    ci_lower_bps: float | None
    ci_upper_bps: float | None
    no_result_reason: str | None


@dataclass(frozen=True, slots=True)
class RelaxationStepAnalysis:
    step_id: str
    strict_config_id: str
    looser_config_id: str
    operational_status: OperationalStatus
    incomplete_reason: str | None
    folds: tuple[FoldLayerCohort, ...]
    comparable_fold_ids: tuple[str, ...]
    excluded_folds: tuple[tuple[str, str], ...]
    paired_delta_e0_bps: float | None
    strict_core_e0_bps: float | None
    cumulative_looser_e0_bps: float | None
    new_layer_e0_bps: float | None
    sign_test: ExactSignTest
    bootstrap: FoldBlockBootstrap
    all_eight_comparable: bool
    seven_of_eight_negative: bool
    new_layer_below_strict_core: bool


@dataclass(frozen=True, slots=True)
class RayAnalysis:
    ray_id: str
    family: Family
    operational_status: OperationalStatus
    incomplete_reasons: tuple[str, ...]
    steps: tuple[RelaxationStepAnalysis, ...]
    monotone_edge_decay: bool | None


@dataclass(frozen=True, slots=True)
class PhaseRelaxationAnalysis:
    phase: Phase
    fold_ids: tuple[str, ...]
    operational_status: OperationalStatus
    incomplete_reasons: tuple[str, ...]
    rays: tuple[RayAnalysis, ...]


@dataclass(frozen=True, slots=True)
class RelaxationCampaignAnalysis:
    schema_version: Literal["rob974.r3.relaxation.v1"]
    campaign_hash: str
    operational_status: OperationalStatus
    incomplete_reasons: tuple[str, ...]
    oos: PhaseRelaxationAnalysis
    train_diagnostic: PhaseRelaxationAnalysis | None


def _campaign_hash(value: object) -> str:
    if type(value) is not str:
        raise TypeError("campaign_hash must be a built-in str")
    if (
        len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
        or value == "0" * 64
    ):
        raise RelaxationInputError(
            "campaign_hash must be a non-placeholder lowercase full SHA-256"
        )
    return value


def _float_bytes(value: float) -> str:
    return value.hex()


def _trade_bytes(trade: RelaxationTrade) -> bytes:
    """Canonical bytes for every normalized event/execution economic field."""
    event = trade.event
    execution = trade.execution
    payload = {
        "direction": event.direction,
        "entry_prices": [_float_bytes(value) for value in execution.entry_prices],
        "entry_ts": execution.entry_ts,
        "exit_prices": [_float_bytes(value) for value in execution.exit_prices],
        "exit_reason": execution.exit_reason,
        "exit_ts": execution.exit_ts,
        "family": event.family,
        "gross_bps": _float_bytes(execution.gross_bps),
        "gross_notional": (
            None
            if execution.gross_notional is None
            else _float_bytes(execution.gross_notional)
        ),
        "instruments": list(event.instruments),
        "leg_sides": list(execution.leg_sides),
        "leg_weights": [_float_bytes(value) for value in execution.leg_weights],
        "mae_bps": _float_bytes(execution.mae_bps),
        "mfe_bps": _float_bytes(execution.mfe_bps),
        "signal_ts": event.signal_ts,
    }
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")


def _validate_ledgers(
    ledgers: object, phase: Phase
) -> dict[tuple[str, str], CellFoldLedger]:
    if type(ledgers) is not tuple:
        raise TypeError(f"{phase} ledgers must be an exact built-in tuple")
    expected_order = tuple(
        (config_id, fold_id) for config_id in R3_CONFIG_IDS for fold_id in R3_FOLD_IDS
    )
    actual_order: list[tuple[str, str]] = []
    event_fold: dict[EconomicEvent, str] = {}
    result: dict[tuple[str, str], CellFoldLedger] = {}
    for row in ledgers:
        if type(row) is not CellFoldLedger:
            raise TypeError(f"{phase} ledgers must contain exact CellFoldLedger values")
        actual_order.append((row.config_id, row.fold_id))
        expected_family = row.config_id[:2]
        local_events: set[EconomicEvent] = set()
        for trade in row.trades:
            if trade.event.family != expected_family:
                raise RelaxationInputError("trade family does not match its R3 config")
            if trade.event in local_events:
                raise RelaxationInputError(
                    f"duplicate trade identity in {row.config_id}/{row.fold_id}"
                )
            local_events.add(trade.event)
            prior_fold = event_fold.get(trade.event)
            if prior_fold is not None and prior_fold != row.fold_id:
                raise RelaxationInputError("cross-fold trade identity collision")
            event_fold[trade.event] = row.fold_id
        result[(row.config_id, row.fold_id)] = row
    if tuple(actual_order) != expected_order:
        raise RelaxationInputError(
            f"{phase} ledgers must have exact canonical 12x8 order"
        )
    return result


def _exclusion_reason(strict_count: int, looser_count: int) -> str | None:
    strict_low = strict_count < SAMPLE_QUALIFYING_BASKET_TRADES
    looser_low = looser_count < SAMPLE_QUALIFYING_BASKET_TRADES
    if strict_low and looser_low:
        return "both_basket_trades_below_5"
    if strict_low:
        return "strict_basket_trades_below_5"
    if looser_low:
        return "looser_basket_trades_below_5"
    return None


def _fold_cohort(
    strict: CellFoldLedger, looser: CellFoldLedger
) -> tuple[FoldLayerCohort, str | None]:
    strict_by_event = {trade.event: trade for trade in strict.trades}
    looser_by_event = {trade.event: trade for trade in looser.trades}
    missing = tuple(event for event in strict_by_event if event not in looser_by_event)
    reason = _exclusion_reason(strict.basket_trade_count, looser.basket_trade_count)
    if missing:
        return (
            FoldLayerCohort(
                strict.fold_id,
                strict.basket_trade_count,
                looser.basket_trade_count,
                False,
                "strict_looser_direction_drift",
                strict.trades,
                looser.trades,
                (),
                _mean_gross_bps(strict.trades),
                _mean_gross_bps(looser.trades),
                None,
                None,
            ),
            "strict_looser_direction_drift",
        )
    for event, strict_trade in strict_by_event.items():
        if _trade_bytes(strict_trade) != _trade_bytes(looser_by_event[event]):
            return (
                FoldLayerCohort(
                    strict.fold_id,
                    strict.basket_trade_count,
                    looser.basket_trade_count,
                    False,
                    "core_trade_drift",
                    strict.trades,
                    looser.trades,
                    (),
                    _mean_gross_bps(strict.trades),
                    _mean_gross_bps(looser.trades),
                    None,
                    None,
                ),
                "core_trade_drift",
            )
    layer = tuple(
        trade for trade in looser.trades if trade.event not in strict_by_event
    )
    strict_e0 = _mean_gross_bps(strict.trades)
    looser_e0 = _mean_gross_bps(looser.trades)
    delta = (
        None
        if strict_e0 is None or looser_e0 is None
        else math.fsum((looser_e0, -strict_e0))
    )
    return (
        FoldLayerCohort(
            strict.fold_id,
            strict.basket_trade_count,
            looser.basket_trade_count,
            reason is None,
            reason,
            strict.trades,
            looser.trades,
            layer,
            strict_e0,
            looser_e0,
            _mean_gross_bps(layer),
            delta,
        ),
        None,
    )


def _sign_test(folds: tuple[FoldLayerCohort, ...]) -> ExactSignTest:
    deltas = tuple(
        fold.delta_e0_bps
        for fold in folds
        if fold.sample_qualified and fold.delta_e0_bps is not None
    )
    negative = sum(delta < 0.0 for delta in deltas)
    positive = sum(delta > 0.0 for delta in deltas)
    ties = sum(delta == 0.0 for delta in deltas)
    effective_n = negative + positive
    if effective_n == 0:
        return ExactSignTest(
            "less", negative, positive, ties, 0, 0, 1, None, "all_comparable_ties"
        )
    numerator = sum(
        math.comb(effective_n, count) for count in range(negative, effective_n + 1)
    )
    denominator = 2**effective_n
    return ExactSignTest(
        "less",
        negative,
        positive,
        ties,
        effective_n,
        numerator,
        denominator,
        numerator / denominator,
        None,
    )


def _seed_sha256(campaign_hash: str, ray_id: str, step_id: str) -> str:
    material = (
        f"rob974.r3.relaxation.bootstrap.v1\x00{campaign_hash}\x00{ray_id}\x00{step_id}"
    ).encode()
    return sha256(material).hexdigest()


def _percentile_linear_r7(sorted_values: tuple[float, ...], p: float) -> float:
    rank = (len(sorted_values) - 1) * p
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return sorted_values[lower]
    fraction = rank - lower
    return math.fsum(
        (
            sorted_values[lower] * (1.0 - fraction),
            sorted_values[upper] * fraction,
        )
    )


def _bootstrap(
    campaign_hash: str,
    ray_id: str,
    step_id: str,
    comparable: tuple[FoldLayerCohort, ...],
) -> FoldBlockBootstrap:
    seed_sha = _seed_sha256(campaign_hash, ray_id, step_id)
    seed = int(seed_sha, 16)
    if not comparable:
        return FoldBlockBootstrap(
            BOOTSTRAP_RESAMPLES,
            "paired_fold_blocks",
            0,
            seed_sha,
            seed,
            "linear_r7",
            0.95,
            None,
            None,
            "no_sample_qualified_paired_folds",
        )
    generator = random.Random(seed)
    estimates: list[float] = []
    block_count = len(comparable)
    for _ in range(BOOTSTRAP_RESAMPLES):
        selected = tuple(generator.randrange(block_count) for _ in range(block_count))
        strict_count = math.fsum(
            float(len(comparable[index].strict_core)) for index in selected
        )
        looser_count = math.fsum(
            float(len(comparable[index].cumulative_looser)) for index in selected
        )
        strict_e0 = (
            math.fsum(
                trade.execution.gross_bps
                for index in selected
                for trade in comparable[index].strict_core
            )
            / strict_count
        )
        looser_e0 = (
            math.fsum(
                trade.execution.gross_bps
                for index in selected
                for trade in comparable[index].cumulative_looser
            )
            / looser_count
        )
        estimates.append(math.fsum((looser_e0, -strict_e0)))
    ordered = tuple(sorted(estimates))
    lower = _percentile_linear_r7(ordered, 0.025)
    upper = _percentile_linear_r7(ordered, 0.975)
    if lower > upper:  # Defensive fail-closed invariant, never normalization.
        raise RelaxationInputError("deterministic bootstrap CI order drift")
    return FoldBlockBootstrap(
        BOOTSTRAP_RESAMPLES,
        "paired_fold_blocks",
        block_count,
        seed_sha,
        seed,
        "linear_r7",
        0.95,
        lower,
        upper,
        None,
    )


def _pooled_e0(cohorts: tuple[FoldLayerCohort, ...], field: str) -> float | None:
    trades = tuple(trade for cohort in cohorts for trade in getattr(cohort, field))
    return _mean_gross_bps(trades)


def _analyze_step(
    *,
    campaign_hash: str,
    ray: RelaxationRay,
    strict_id: str,
    looser_id: str,
    ledgers: dict[tuple[str, str], CellFoldLedger],
) -> RelaxationStepAnalysis:
    step_id = f"{strict_id}->{looser_id}"
    fold_results: list[FoldLayerCohort] = []
    incomplete: str | None = None
    for fold_id in R3_FOLD_IDS:
        cohort, fold_incomplete = _fold_cohort(
            ledgers[(strict_id, fold_id)], ledgers[(looser_id, fold_id)]
        )
        fold_results.append(cohort)
        if incomplete is None and fold_incomplete is not None:
            incomplete = fold_incomplete
    folds = tuple(fold_results)
    comparable = tuple(fold for fold in folds if fold.sample_qualified)
    strict_e0 = _pooled_e0(comparable, "strict_core")
    looser_e0 = _pooled_e0(comparable, "cumulative_looser")
    layer_e0 = _pooled_e0(comparable, "new_layer")
    delta = (
        None
        if strict_e0 is None or looser_e0 is None
        else math.fsum((looser_e0, -strict_e0))
    )
    sign_test = _sign_test(folds)
    bootstrap = _bootstrap(campaign_hash, ray.ray_id, step_id, comparable)
    all_eight = len(comparable) == len(R3_FOLD_IDS) == 8
    seven_negative = all_eight and sign_test.negative_count >= 7
    layer_below = (
        layer_e0 is not None and strict_e0 is not None and layer_e0 < strict_e0
    )
    return RelaxationStepAnalysis(
        step_id,
        strict_id,
        looser_id,
        "INCOMPLETE" if incomplete else "COMPLETE",
        incomplete,
        folds,
        tuple(fold.fold_id for fold in comparable),
        tuple(
            (fold.fold_id, fold.exclusion_reason)
            for fold in folds
            if fold.exclusion_reason is not None
        ),
        delta,
        strict_e0,
        looser_e0,
        layer_e0,
        sign_test,
        bootstrap,
        all_eight,
        seven_negative,
        layer_below,
    )


def _ordered_unique(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _analyze_phase(
    *,
    campaign_hash: str,
    phase: Phase,
    raw_ledgers: object,
) -> PhaseRelaxationAnalysis:
    ledgers = _validate_ledgers(raw_ledgers, phase)
    rays: list[RayAnalysis] = []
    for ray in R3_RELAXATION_RAYS:
        steps = tuple(
            _analyze_step(
                campaign_hash=campaign_hash,
                ray=ray,
                strict_id=strict_id,
                looser_id=looser_id,
                ledgers=ledgers,
            )
            for strict_id, looser_id in zip(ray.cells[:-1], ray.cells[1:], strict=True)
        )
        reasons = _ordered_unique(
            tuple(
                step.incomplete_reason
                for step in steps
                if step.incomplete_reason is not None
            )
        )
        complete = not reasons
        monotone = None
        if phase == "OOS" and complete:
            monotone = (
                all(
                    step.paired_delta_e0_bps is not None
                    and step.paired_delta_e0_bps <= 0.0
                    for step in steps
                )
                and sum(step.seven_of_eight_negative for step in steps) >= 2
                and all(step.new_layer_below_strict_core for step in steps)
            )
        rays.append(
            RayAnalysis(
                ray.ray_id,
                ray.family,
                "COMPLETE" if complete else "INCOMPLETE",
                reasons,
                steps,
                monotone,
            )
        )
    ray_tuple = tuple(rays)
    phase_reasons = _ordered_unique(
        tuple(reason for ray in ray_tuple for reason in ray.incomplete_reasons)
    )
    return PhaseRelaxationAnalysis(
        phase,
        R3_FOLD_IDS,
        "INCOMPLETE" if phase_reasons else "COMPLETE",
        phase_reasons,
        ray_tuple,
    )


def _suppress_oos_verdict_flags(
    phase: PhaseRelaxationAnalysis,
) -> PhaseRelaxationAnalysis:
    return replace(
        phase,
        rays=tuple(replace(ray, monotone_edge_decay=None) for ray in phase.rays),
    )


def analyze_relaxation_campaign(
    *,
    campaign_hash: object,
    oos_ledgers: object,
    train_ledgers: object | None = None,
) -> RelaxationCampaignAnalysis:
    """Build exact R3 §7 cohort/statistical evidence without side effects.

    ``oos_ledgers`` and optional ``train_ledgers`` must each be the canonical
    config-major 12x8 tuple.  TRAIN cohorts are retained only as diagnostics;
    ``monotone_edge_decay`` is always ``None`` for TRAIN.  Any operational
    incompleteness suppresses every OOS verdict flag rather than laundering a
    partial ray into research evidence.
    """

    checked_hash = _campaign_hash(campaign_hash)
    oos = _analyze_phase(
        campaign_hash=checked_hash,
        phase="OOS",
        raw_ledgers=oos_ledgers,
    )
    train = (
        None
        if train_ledgers is None
        else _analyze_phase(
            campaign_hash=checked_hash,
            phase="TRAIN",
            raw_ledgers=train_ledgers,
        )
    )
    reasons = _ordered_unique(
        oos.incomplete_reasons + (() if train is None else train.incomplete_reasons)
    )
    if reasons:
        oos = _suppress_oos_verdict_flags(oos)
    return RelaxationCampaignAnalysis(
        "rob974.r3.relaxation.v1",
        checked_hash,
        "INCOMPLETE" if reasons else "COMPLETE",
        reasons,
        oos,
        train,
    )


__all__ = [
    "BOOTSTRAP_RESAMPLES",
    "R3_CONFIG_IDS",
    "R3_FOLD_IDS",
    "R3_RELAXATION_RAYS",
    "CellFoldLedger",
    "EconomicEvent",
    "ExactSignTest",
    "FoldBlockBootstrap",
    "FoldLayerCohort",
    "PhaseRelaxationAnalysis",
    "RayAnalysis",
    "RelaxationCampaignAnalysis",
    "RelaxationInputError",
    "RelaxationRay",
    "RelaxationStepAnalysis",
    "RelaxationTrade",
    "TradeExecution",
    "analyze_relaxation_campaign",
]

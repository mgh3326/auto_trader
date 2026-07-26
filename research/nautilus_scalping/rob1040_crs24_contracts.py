"""ROB-1040 CRS-24 CORR-1 immutable, outcome-blind research contract.

The module is stdlib/pure-research only. It reuses the frozen ROB-974 symbol
and fold authorities, but does not modify either authority and does not bind
any empirical adapter.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal

from rob974_features import SYMBOLS as _ROB974_SYMBOLS
from rob974_h4_contracts import exact_h4_folds

from research_contracts.canonical_hash import canonical_sha256

SCHEMA_VERSION = "rob1040.crs24.corr1.contract.v1"
BASE_HEAD = "bd169ec3bfed9ae0c281fbc18b14d710ccdc45d9"
PREREGISTRATION_RELATIVE_PATH = (
    "research/nautilus_scalping/contracts/rob1040_crs24_corr1_preregistration.md"
)
PREREGISTRATION_SIZE = 6_883
PREREGISTRATION_SHA256 = (
    "28f07e7379768a17d3fa64305fed2a2f42a1cbee3dabf6c2e48d4033fb70dcc5"
)

UNIVERSE: tuple[str, ...] = _ROB974_SYMBOLS
if UNIVERSE != ("XRPUSDT", "DOGEUSDT", "SOLUSDT"):
    raise ValueError("ROB-974 selected-symbol authority drifted")

FOUR_HOUR_MS = 14_400_000
HALF_DAY_MS = 43_200_000
DAY_MS = 86_400_000
ENTRY_DELAY_MS = 60_000
HOLD_MS = DAY_MS
PIT_LOOKBACK_MS = 60 * DAY_MS
PIT_MIN_OBSERVATIONS = 100
VOLATILITY_RETURN_COUNT = 42
VOLATILITY_FLOOR = 1e-8
DISPERSION_QUANTILE = 0.50
COMMON_MAGNITUDE_QUANTILE = 0.75
ARBITRATION_TOLERANCE = 1e-12
TARGET_REFERENCE_NOTIONAL_USDT = Decimal("8")
MIN_REFERENCE_NOTIONAL_USDT = Decimal("6")
MAX_REFERENCE_NOTIONAL_USDT = Decimal("10")
LEVERAGE = 1
POSITION_MODE = "one_way"

SCHEDULED_PER_FOLD = 56
HORIZON_ELIGIBLE_PER_FOLD = 54
FOLD_HORIZON_CLOSED_PER_FOLD = 2
ALL_SIGNAL_PLANNED_PER_FOLD = 18
ALL_SIGNAL_OCCUPIED_PER_FOLD = 36


def _exact_int(value: object, name: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{name} must be built-in int")
    return value


def _exact_str(value: object, name: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{name} must be built-in str")
    return value


@dataclass(frozen=True, slots=True)
class CRSConfig:
    config_id: str
    formation_hours: int
    formation_return_count: int
    posture: str

    def __post_init__(self) -> None:
        _exact_str(self.config_id, "config_id")
        _exact_int(self.formation_hours, "formation_hours")
        _exact_int(self.formation_return_count, "formation_return_count")
        _exact_str(self.posture, "posture")
        if self.formation_hours != self.formation_return_count * 4:
            raise ValueError("formation hours and complete-return count disagree")


@dataclass(frozen=True, slots=True)
class ConfigSlot:
    slot: int
    identifier: str
    formation_hours: int | None
    formation_return_count: int | None
    posture: str

    def __post_init__(self) -> None:
        _exact_int(self.slot, "slot")
        _exact_str(self.identifier, "identifier")
        _exact_str(self.posture, "posture")
        if self.identifier == "CLOSED_UNUSED":
            if (
                self.slot != 3
                or self.formation_hours is not None
                or self.formation_return_count is not None
                or self.posture != "permanently_closed"
            ):
                raise ValueError("CLOSED_UNUSED must be the permanent fourth slot")
            return
        if type(self.formation_hours) is not int:
            raise TypeError("active formation_hours must be built-in int")
        if type(self.formation_return_count) is not int:
            raise TypeError("active formation_return_count must be built-in int")
        if self.formation_hours != self.formation_return_count * 4:
            raise ValueError("active slot formation contract drifted")


ACTIVE_CONFIGS: tuple[CRSConfig, ...] = (
    CRSConfig("CRS-A0", 168, 42, "primary"),
    CRSConfig("CRS-A1", 72, 18, "sensitivity"),
    CRSConfig("CRS-A2", 336, 84, "sensitivity"),
)
CONFIG_SLOTS: tuple[ConfigSlot, ...] = tuple(
    ConfigSlot(
        index,
        config.config_id,
        config.formation_hours,
        config.formation_return_count,
        config.posture,
    )
    for index, config in enumerate(ACTIVE_CONFIGS)
) + (ConfigSlot(3, "CLOSED_UNUSED", None, None, "permanently_closed"),)


@dataclass(frozen=True, slots=True)
class SymbolFilterFixture:
    symbol: str
    quantity_step: Decimal

    def __post_init__(self) -> None:
        _exact_str(self.symbol, "symbol")
        if self.symbol not in UNIVERSE:
            raise ValueError("filter symbol is outside the frozen universe")
        if type(self.quantity_step) is not Decimal:
            raise TypeError("quantity_step must be exact Decimal")
        if not self.quantity_step.is_finite() or self.quantity_step <= 0:
            raise ValueError("quantity_step must be finite and positive")


SYMBOL_FILTERS: tuple[SymbolFilterFixture, ...] = (
    SymbolFilterFixture("XRPUSDT", Decimal("0.1")),
    SymbolFilterFixture("DOGEUSDT", Decimal("1")),
    SymbolFilterFixture("SOLUSDT", Decimal("0.01")),
)


def config_for_id(config_id: str) -> CRSConfig:
    _exact_str(config_id, "config_id")
    for config in ACTIVE_CONFIGS:
        if config.config_id == config_id:
            return config
    raise KeyError(config_id)


def filter_for_symbol(symbol: str) -> SymbolFilterFixture:
    _exact_str(symbol, "symbol")
    for fixture in SYMBOL_FILTERS:
        if fixture.symbol == symbol:
            return fixture
    raise KeyError(symbol)


def fold_schedule_payload() -> list[dict[str, object]]:
    return [
        {
            "fold_id": fold.fold_id,
            "fold_index": fold.fold_index,
            "oos_start_ms": fold.oos_start_ms,
            "oos_end_ms": fold.oos_end_ms,
        }
        for fold in exact_h4_folds()
    ]


def filter_manifest_payload() -> dict[str, object]:
    return {
        "schema_version": "rob1040.crs24.corr1.filter_fixture.v1",
        "authority": "rob1040_local_static_fixture",
        "universe": list(UNIVERSE),
        "symbols": [
            {
                "symbol": fixture.symbol,
                "quantity_step": str(fixture.quantity_step),
            }
            for fixture in SYMBOL_FILTERS
        ],
        "sizing": {
            "target_reference_notional_usdt": str(TARGET_REFERENCE_NOTIONAL_USDT),
            "accepted_reference_notional_usdt": [
                str(MIN_REFERENCE_NOTIONAL_USDT),
                str(MAX_REFERENCE_NOTIONAL_USDT),
            ],
            "quantity_rounding": "floor_to_step",
            "leverage": LEVERAGE,
            "position_mode": POSITION_MODE,
        },
    }


FILTER_MANIFEST_SHA256 = (
    "fc6950b731f837b578a0666e1011b6cbc05e04b8fad7236a754a82ce0d8b84fa"
)
FOLD_SCHEDULE_SHA256 = (
    "62ca7144ae304d1be162e716eb26956c3a154c087b718811c373587f47da9222"
)


def contract_payload() -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "base_head": BASE_HEAD,
        "preregistration": {
            "relative_path": PREREGISTRATION_RELATIVE_PATH,
            "byte_size": PREREGISTRATION_SIZE,
            "sha256": PREREGISTRATION_SHA256,
        },
        "universe": list(UNIVERSE),
        "config_slots": [
            {
                "slot": slot.slot,
                "identifier": slot.identifier,
                "formation_hours": slot.formation_hours,
                "formation_return_count": slot.formation_return_count,
                "posture": slot.posture,
            }
            for slot in CONFIG_SLOTS
        ],
        "evaluation": {
            "scope": "basket_wide",
            "utc_cutoff_hours": [0, 12],
            "complete_return_hours": 4,
        },
        "features": {
            "common_return": "equal_weight_mean_three_symbols",
            "residual": "raw_return_minus_common_return",
            "residual_formation": "sum_over_config_return_count",
            "residual_sample_volatility_returns": VOLATILITY_RETURN_COUNT,
            "common_sample_volatility_returns": VOLATILITY_RETURN_COUNT,
            "score": "residual_sum/(residual_sigma*sqrt(n_L))",
            "common_magnitude": "abs(common_sum)/(common_sigma*sqrt(n_L))",
            "dispersion": "max_score_minus_min_score",
            "volatility_floor": VOLATILITY_FLOOR,
        },
        "pit_gates": {
            "lookback_calendar_days": 60,
            "current_cutoff_excluded": True,
            "minimum_valid_observations": PIT_MIN_OBSERVATIONS,
            "quantile_method": "nearest_rank",
            "dispersion": {"quantile": DISPERSION_QUANTILE, "comparison": ">="},
            "common_magnitude": {
                "quantile": COMMON_MAGNITUDE_QUANTILE,
                "comparison": "<=",
            },
        },
        "arbitration": {
            "symbol_tie_order": list(UNIVERSE),
            "long": "maximum_positive_score",
            "short": "minimum_negative_score_strength_negated",
            "stronger_side_wins": True,
            "opposing_strength_difference_below": ARBITRATION_TOLERANCE,
            "maximum_winners_per_cutoff": 1,
        },
        "calendar": {
            "authority": "rob974_h4_contracts.exact_h4_folds/rob944_folds",
            "fold_schedule_sha256": FOLD_SCHEDULE_SHA256,
            "folds": fold_schedule_payload(),
            "scheduled_per_fold": SCHEDULED_PER_FOLD,
            "horizon_eligible_per_fold": HORIZON_ELIGIBLE_PER_FOLD,
            "fold_horizon_closed_per_fold": FOLD_HORIZON_CLOSED_PER_FOLD,
        },
        "lifecycle": {
            "entry_delay_ms": ENTRY_DELAY_MS,
            "hold_ms": HOLD_MS,
            "half_open_oos": True,
            "fold_carry": False,
            "one_position": True,
            "equal_exit_and_next_entry_is_occupied": True,
            "queue": False,
            "flip": False,
            "resize": False,
            "all_signal_planned_per_fold": ALL_SIGNAL_PLANNED_PER_FOLD,
            "all_signal_occupied_per_fold": ALL_SIGNAL_OCCUPIED_PER_FOLD,
        },
        "references": {
            "basis": "one_minute_bar_open",
            "entry": "numeric_presence_required",
            "exit": "timestamp_presence_only",
        },
        "filter_fixture_sha256": FILTER_MANIFEST_SHA256,
        "movement_capacity": {
            "source": "selected_symbol_trailing_42_complete_raw_returns",
            "formula": "1e4*sqrt(2/pi)*sigma_raw*sqrt(6)",
            "posture": "trailing_only_diagnostic",
        },
        "launch": {
            "synthetic_and_unit_only": True,
            "empirical_run": "closed_pending_exact_main_refreeze_and_approval",
        },
    }


CONTRACT_SHA256 = "5964e47f6a7635379f5ae57e13a99f750a24fa2c95307e8326198ec6cd3014ad"


def canonical_preregistration_bytes(value: bytes) -> bytes:
    if type(value) is not bytes:
        raise TypeError("preregistration value must be exact bytes")
    if len(value) != PREREGISTRATION_SIZE:
        raise ValueError("preregistration byte size drifted")
    if b"\r" in value or not value.endswith(b"\n") or value.endswith(b"\n\n"):
        raise ValueError("preregistration must use one terminal LF and no CR")
    if any(line.endswith(b" ") for line in value.splitlines()):
        raise ValueError("preregistration contains trailing whitespace")
    if hashlib.sha256(value).hexdigest() != PREREGISTRATION_SHA256:
        raise ValueError("preregistration bytes do not match the frozen digest")
    return value


def validate_contract() -> None:
    if len(ACTIVE_CONFIGS) != 3 or len(CONFIG_SLOTS) != 4:
        raise ValueError("CRS-24 config roster drifted")
    if tuple(slot.slot for slot in CONFIG_SLOTS) != (0, 1, 2, 3):
        raise ValueError("CRS-24 config slot order drifted")
    if tuple(item.symbol for item in SYMBOL_FILTERS) != UNIVERSE:
        raise ValueError("filter fixture order drifted")
    folds = exact_h4_folds()
    if len(folds) != 8:
        raise ValueError("CRS-24 requires the exact eight-fold authority")
    if canonical_sha256(fold_schedule_payload()) != FOLD_SCHEDULE_SHA256:
        raise ValueError("fold schedule digest drifted")
    if canonical_sha256(filter_manifest_payload()) != FILTER_MANIFEST_SHA256:
        raise ValueError("filter fixture digest drifted")
    if canonical_sha256(contract_payload()) != CONTRACT_SHA256:
        raise ValueError("CRS-24 contract digest drifted")


__all__ = [
    "ACTIVE_CONFIGS",
    "ALL_SIGNAL_OCCUPIED_PER_FOLD",
    "ALL_SIGNAL_PLANNED_PER_FOLD",
    "ARBITRATION_TOLERANCE",
    "BASE_HEAD",
    "COMMON_MAGNITUDE_QUANTILE",
    "CONFIG_SLOTS",
    "CONTRACT_SHA256",
    "CRSConfig",
    "ConfigSlot",
    "DAY_MS",
    "DISPERSION_QUANTILE",
    "ENTRY_DELAY_MS",
    "FILTER_MANIFEST_SHA256",
    "FOLD_HORIZON_CLOSED_PER_FOLD",
    "FOLD_SCHEDULE_SHA256",
    "FOUR_HOUR_MS",
    "HALF_DAY_MS",
    "HOLD_MS",
    "HORIZON_ELIGIBLE_PER_FOLD",
    "LEVERAGE",
    "MAX_REFERENCE_NOTIONAL_USDT",
    "MIN_REFERENCE_NOTIONAL_USDT",
    "PIT_LOOKBACK_MS",
    "PIT_MIN_OBSERVATIONS",
    "POSITION_MODE",
    "PREREGISTRATION_RELATIVE_PATH",
    "PREREGISTRATION_SHA256",
    "PREREGISTRATION_SIZE",
    "SCHEMA_VERSION",
    "SCHEDULED_PER_FOLD",
    "SYMBOL_FILTERS",
    "SymbolFilterFixture",
    "TARGET_REFERENCE_NOTIONAL_USDT",
    "UNIVERSE",
    "VOLATILITY_FLOOR",
    "VOLATILITY_RETURN_COUNT",
    "canonical_preregistration_bytes",
    "config_for_id",
    "contract_payload",
    "filter_for_symbol",
    "filter_manifest_payload",
    "fold_schedule_payload",
    "validate_contract",
]

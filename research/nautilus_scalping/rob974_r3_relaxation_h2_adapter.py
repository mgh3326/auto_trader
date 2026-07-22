"""Pure exact-type H2-to-R3 relaxation-ledger normalization.

This boundary consumes the real H2 trade DTOs and the single manifest-owned
R3 roster.  It performs no execution, persistence, discovery, or empirical
work.  In particular, it preserves H2's signed observed ``z_entry`` value;
it never clamps it to an R3 threshold.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal, cast

from rob974_h2_dtos import (
    PAIR_EXEC_FAIL_NOT_EVALUATED,
    PROMOTION_BLOCKED_PENDING_PAIR_EXECUTOR,
    S3Trade,
    S4PairTrade,
)
from rob974_h3_manifest import PAIRS, SYMBOLS
from rob974_h4_contracts import exact_h4_folds
from rob974_r3_manifest import (
    FROZEN_R3_ROSTER,
    R3S3Config,
    R3S4Config,
    assert_registered_r3_config,
)
from rob974_r3_relaxation import (
    CellFoldLedger,
    EconomicEvent,
    Phase,
    RelaxationInputError,
    RelaxationTrade,
    TradeExecution,
)

_FOLDS = exact_h4_folds()
_FOLD_BY_ID = {fold.fold_id: fold for fold in _FOLDS}
_EXPECTED_HEADERS = tuple(
    (config.config_id, fold.fold_id) for config in FROZEN_R3_ROSTER for fold in _FOLDS
)
_CANONICAL_PAIRS = tuple(
    tuple(f"{leg}USDT" for leg in pair.split("-")) for pair in PAIRS
)
if tuple(dict.fromkeys(symbol for pair in _CANONICAL_PAIRS for symbol in pair)) != (
    SYMBOLS[0],
    SYMBOLS[1],
    SYMBOLS[2],
):  # pragma: no cover - import-time authority drift guard
    raise RuntimeError("H3 pair and symbol authorities disagree")


@dataclass(frozen=True, slots=True)
class R3H2CellFoldInput:
    """One exact manifest cell/fold's H2 trade output."""

    config: R3S3Config | R3S4Config
    fold_id: str
    basket_trade_count: int
    trades: tuple[S3Trade, ...] | tuple[S4PairTrade, ...]

    def __post_init__(self) -> None:
        assert_registered_r3_config(self.config)
        if type(self.fold_id) is not str or self.fold_id not in _FOLD_BY_ID:
            raise RelaxationInputError("fold_id is outside the exact H4 folds")
        if type(self.basket_trade_count) is not int:
            raise TypeError("basket_trade_count must be an exact built-in int")
        if self.basket_trade_count < 0:
            raise RelaxationInputError("basket_trade_count must be non-negative")
        if type(self.trades) is not tuple:
            raise TypeError("trades must be an exact built-in tuple")
        if self.basket_trade_count != len(self.trades):
            raise RelaxationInputError(
                "basket_trade_count must equal the exact H2 trade tuple length"
            )
        expected_type = S3Trade if type(self.config) is R3S3Config else S4PairTrade
        if any(type(trade) is not expected_type for trade in self.trades):
            raise TypeError("H2 trades must have the exact config-family DTO type")


def _validate_lineage(
    *,
    trade: S3Trade | S4PairTrade,
    config: R3S3Config | R3S4Config,
    fold_id: str,
) -> None:
    if trade.config_id != config.config_id:
        raise RelaxationInputError("H2 trade config_id differs from its R3 cell")
    if trade.fold_id != fold_id:
        raise RelaxationInputError("H2 trade fold_id differs from its H4 fold")


def _validate_phase_time(
    *, trade: S3Trade | S4PairTrade, fold_id: str, phase: Phase
) -> None:
    fold = _FOLD_BY_ID[fold_id]
    start_ms, end_ms = (
        (fold.train_start_ms, fold.train_end_ms)
        if phase == "TRAIN"
        else (fold.oos_start_ms, fold.oos_end_ms)
    )
    if not (
        start_ms <= trade.signal_ts < end_ms
        and start_ms <= trade.entry_ts < end_ms
        and start_ms <= trade.exit_ts < end_ms
    ):
        raise RelaxationInputError(
            f"H2 trade timestamps are outside the exact {phase} H4 window"
        )


def normalize_r3_s3_trade(
    *, trade: object, config: object, fold_id: object
) -> RelaxationTrade:
    """Normalize one exact H2 S3 trade without retaining config lineage."""

    if type(trade) is not S3Trade:
        raise TypeError("trade must be an exact H2 S3Trade")
    if type(config) is not R3S3Config:
        raise TypeError("config must be an exact R3S3Config")
    assert_registered_r3_config(config)
    if type(fold_id) is not str or fold_id not in _FOLD_BY_ID:
        raise RelaxationInputError("fold_id is outside the exact H4 folds")
    _validate_lineage(trade=trade, config=config, fold_id=fold_id)
    if trade.volatility_percentile is None or not (
        0.0 <= trade.volatility_percentile <= 100.0
    ):
        raise RelaxationInputError(
            "R3 S3 volatility_percentile must be the H3 value in [0,100]"
        )
    return RelaxationTrade(
        event=EconomicEvent("S3", (trade.symbol,), trade.signal_ts, trade.side),
        execution=TradeExecution(
            entry_ts=trade.entry_ts,
            exit_ts=trade.exit_ts,
            leg_sides=(trade.side,),
            entry_prices=(trade.entry_price,),
            exit_prices=(trade.exit_price,),
            leg_weights=(1.0,),
            gross_notional=None,
            mfe_bps=trade.mfe_bps,
            mae_bps=trade.mae_bps,
            gross_bps=trade.gross_bps,
            exit_reason=trade.exit_reason,
            volatility_percentile=trade.volatility_percentile,
        ),
    )


def _validate_s4_economics(trade: S4PairTrade, config: R3S4Config) -> None:
    if trade.pair not in _CANONICAL_PAIRS:
        raise RelaxationInputError("S4 pair is outside canonical H3 pair order")
    expected_direction_and_sign: dict[
        tuple[str, str], tuple[Literal["long_a_short_b", "short_a_long_b"], int]
    ] = {
        ("long", "short"): ("long_a_short_b", -1),
        ("short", "long"): ("short_a_long_b", 1),
    }
    try:
        _, observed_sign = expected_direction_and_sign[(trade.side_a, trade.side_b)]
    except KeyError as exc:
        raise RelaxationInputError("S4 trade legs must have opposing sides") from exc
    if (trade.z_entry > 0.0) != (observed_sign > 0):
        raise RelaxationInputError("S4 observed z sign disagrees with pair direction")
    if abs(trade.z_entry) < config.z_entry:
        raise RelaxationInputError("S4 observed z is below its R3 cell threshold")
    if abs(math.fsum((trade.weight_a, trade.weight_b)) - 1.0) > 1e-9:
        raise RelaxationInputError("S4 H2 weights violate the frozen 1e-9 sum rule")
    g_min = max(6.0 / trade.weight_a, 6.0 / trade.weight_b)
    g_max = min(10.0 / trade.weight_a, 10.0 / trade.weight_b)
    if g_min > g_max:
        raise RelaxationInputError("S4 weights have no feasible $6-10 leg notional")
    if trade.gross_notional != g_min:
        raise RelaxationInputError("S4 gross_notional is not deterministic G_min")
    if (
        trade.order_id_a is not None
        or trade.order_id_b is not None
        or trade.demo_eligible is not False
        or type(trade.pair_executor_validated) is not bool
        or trade.pair_executor_validated
        or trade.pair_exec_status != "historical_atomic_assumption"
        or trade.volatility_percentile is not None
        or trade.volatility_percentile_provenance != "not_defined_for_s4"
        or trade.pair_exec_fail != PAIR_EXEC_FAIL_NOT_EVALUATED
        or trade.promotion_status != PROMOTION_BLOCKED_PENDING_PAIR_EXECUTOR
    ):
        raise RelaxationInputError("S4 historical-only H2 posture drifted")


def normalize_r3_s4_trade(
    *, trade: object, config: object, fold_id: object
) -> RelaxationTrade:
    """Normalize one exact H2 S4 basket trade, preserving entry economics."""

    if type(trade) is not S4PairTrade:
        raise TypeError("trade must be an exact H2 S4PairTrade")
    if type(config) is not R3S4Config:
        raise TypeError("config must be an exact R3S4Config")
    assert_registered_r3_config(config)
    if type(fold_id) is not str or fold_id not in _FOLD_BY_ID:
        raise RelaxationInputError("fold_id is outside the exact H4 folds")
    _validate_lineage(trade=trade, config=config, fold_id=fold_id)
    _validate_s4_economics(trade, config)
    direction = (
        "long_a_short_b"
        if (trade.side_a, trade.side_b) == ("long", "short")
        else "short_a_long_b"
    )
    return RelaxationTrade(
        event=EconomicEvent("S4", trade.pair, trade.signal_ts, direction),
        execution=TradeExecution(
            entry_ts=trade.entry_ts,
            exit_ts=trade.exit_ts,
            leg_sides=(trade.side_a, trade.side_b),
            entry_prices=(trade.entry_price_a, trade.entry_price_b),
            exit_prices=(trade.exit_price_a, trade.exit_price_b),
            leg_weights=(trade.weight_a, trade.weight_b),
            gross_notional=trade.gross_notional,
            mfe_bps=trade.mfe_bps,
            mae_bps=trade.mae_bps,
            gross_bps=trade.gross_bps,
            exit_reason=trade.exit_reason,
            beta_a=trade.beta_a,
            beta_b=trade.beta_b,
            spread_mu=trade.mu,
            spread_sigma=trade.sigma,
            observed_z=trade.z_entry,
        ),
    )


def _normalize_cell_fold(*, source: R3H2CellFoldInput, phase: Phase) -> CellFoldLedger:
    normalized: list[RelaxationTrade] = []
    for trade in source.trades:
        _validate_phase_time(trade=trade, fold_id=source.fold_id, phase=phase)
        if type(source.config) is R3S3Config:
            normalized.append(
                normalize_r3_s3_trade(
                    trade=trade,
                    config=source.config,
                    fold_id=source.fold_id,
                )
            )
        else:
            normalized.append(
                normalize_r3_s4_trade(
                    trade=trade,
                    config=source.config,
                    fold_id=source.fold_id,
                )
            )
    return CellFoldLedger(
        config_id=source.config.config_id,
        fold_id=source.fold_id,
        basket_trade_count=source.basket_trade_count,
        trades=tuple(normalized),
    )


def normalize_r3_phase_ledgers(
    *, phase: object, sources: object
) -> tuple[CellFoldLedger, ...]:
    """Build the exact manifest-major 12x8 ledger from H2 trade outputs."""

    if type(phase) is not str or phase not in ("TRAIN", "OOS"):
        raise ValueError("phase must be exact TRAIN or OOS")
    if type(sources) is not tuple:
        raise TypeError("sources must be an exact built-in tuple")
    if any(type(source) is not R3H2CellFoldInput for source in sources):
        raise TypeError("sources must contain exact R3H2CellFoldInput values")
    actual_headers = tuple(
        (source.config.config_id, source.fold_id) for source in sources
    )
    if actual_headers != _EXPECTED_HEADERS:
        raise RelaxationInputError(
            "H2 sources must have exact manifest-major 12x8 H4 order"
        )
    checked_phase = cast(Phase, phase)
    return tuple(
        _normalize_cell_fold(source=source, phase=checked_phase) for source in sources
    )


__all__ = [
    "R3H2CellFoldInput",
    "normalize_r3_phase_ledgers",
    "normalize_r3_s3_trade",
    "normalize_r3_s4_trade",
]

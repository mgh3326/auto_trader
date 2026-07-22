"""Exact sealed-H4-terminal to R3 relaxation-evidence normalization.

Callers cannot submit a trade prefix or a caller-counted basket total.  Every
config/fold supplies the exact family H4 sealed terminal, whose output seal,
bucket types, lineage, and terminal-halt shape are revalidated here.  Trades
before an incomplete remain forensic ledger rows, while terminal evidence is
carried into ``PhaseLedgerEvidence`` so §7 computes no statistics for that
phase.  Signed S4 observed z is preserved without clamp or substitution.

Pure boundary code: no execution, persistence, discovery, empirical work,
network, broker, order/fill, randomness, or current-time behavior.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal, cast

from rob974_h2_dtos import (
    PAIR_EXEC_FAIL_NOT_EVALUATED,
    PROMOTION_BLOCKED_PENDING_PAIR_EXECUTOR,
    S3EngineResult,
    S3IncompleteRecord,
    S3NoTradeRecord,
    S3Trade,
)
from rob974_h3_manifest import PAIRS, SYMBOLS
from rob974_h4_adapter import SealedS3Terminal, seal_s3_engine_output
from rob974_h4_contracts import exact_h4_folds
from rob974_r3_h4_s4_adapter import (
    SealedR3S4Terminal,
    seal_r3_s4_engine_output,
)
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
    PhaseLedgerEvidence,
    RelaxationInputError,
    RelaxationTrade,
    TerminalIncompleteEvidence,
    TradeExecution,
)
from rob974_r3_s4_dtos import (
    R3S4EngineResult,
    R3S4IncompleteRecord,
    R3S4NoTradeRecord,
    R3S4PairTrade,
)

_FOLDS = exact_h4_folds()
_FOLD_BY_ID = {fold.fold_id: fold for fold in _FOLDS}
_EXPECTED_HEADERS = tuple(
    (config.config_id, fold.fold_id) for config in FROZEN_R3_ROSTER for fold in _FOLDS
)
_CANONICAL_PAIRS = tuple(
    tuple(f"{leg}USDT" for leg in pair.split("-")) for pair in PAIRS
)
_G_TOLERANCE = 1e-9

if tuple(dict.fromkeys(symbol for pair in _CANONICAL_PAIRS for symbol in pair)) != (
    SYMBOLS[0],
    SYMBOLS[1],
    SYMBOLS[2],
):  # pragma: no cover - import-time authority drift guard
    raise RuntimeError("H3 pair and symbol authorities disagree")


def _hex64(value: object, name: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise RelaxationInputError(f"{name} must be lowercase 64-hex")
    return value


def _identity(row: object) -> tuple[object, int]:
    if type(row) in (S3Trade, S3NoTradeRecord, S3IncompleteRecord):
        return row.symbol, row.signal_ts
    if type(row) in (R3S4PairTrade, R3S4NoTradeRecord, R3S4IncompleteRecord):
        return row.pair, row.signal_ts
    raise TypeError("terminal row has an unsupported exact type")


def _terminal_sort_key(identity: tuple[object, int]) -> tuple[object, ...]:
    instrument, signal_ts = identity
    return signal_ts, instrument


def _validate_terminal(source: R3H2CellFoldInput) -> None:
    _hex64(source.terminal.input_seal_sha256, "terminal input seal")
    _hex64(source.terminal.output_seal_sha256, "terminal output seal")
    if type(source.config) is R3S3Config:
        if type(source.terminal) is not SealedS3Terminal:
            raise TypeError("R3 S3 cell requires exact SealedS3Terminal")
        result = source.terminal.result
        if type(result) is not S3EngineResult:
            raise TypeError("sealed S3 terminal must carry exact S3EngineResult")
        expected_buckets: tuple[tuple[str, type], ...] = (
            ("trades", S3Trade),
            ("no_trades", S3NoTradeRecord),
            ("incompletes", S3IncompleteRecord),
        )
        recomputed_seal = seal_s3_engine_output(result)
    else:
        if type(source.terminal) is not SealedR3S4Terminal:
            raise TypeError("R3 S4 cell requires exact SealedR3S4Terminal")
        result = source.terminal.result
        if type(result) is not R3S4EngineResult:
            raise TypeError("sealed R3 S4 terminal must carry exact R3S4EngineResult")
        expected_buckets = (
            ("trades", R3S4PairTrade),
            ("no_trades", R3S4NoTradeRecord),
            ("incompletes", R3S4IncompleteRecord),
        )
        recomputed_seal = seal_r3_s4_engine_output(result)
    if recomputed_seal != source.terminal.output_seal_sha256:
        raise RelaxationInputError("sealed terminal output hash does not match result")

    seen: set[tuple[object, int]] = set()
    for bucket_name, row_type in expected_buckets:
        rows = getattr(result, bucket_name)
        if type(rows) is not tuple or any(type(row) is not row_type for row in rows):
            raise TypeError(
                f"sealed terminal {bucket_name} must be an exact {row_type.__name__} tuple"
            )
        for row in rows:
            if row.config_id != source.config.config_id:
                raise RelaxationInputError(
                    "sealed terminal row config_id differs from its R3 cell"
                )
            if row.fold_id != source.fold_id:
                raise RelaxationInputError(
                    "sealed terminal row fold_id differs from its H4 fold"
                )
            identity = _identity(row)
            if identity in seen:
                raise RelaxationInputError(
                    "sealed terminal duplicates an identity across result buckets"
                )
            seen.add(identity)
    if len(result.incompletes) > 1:
        raise RelaxationInputError(
            "engine permits at most one terminal incomplete per config/fold"
        )
    if result.incompletes:
        incomplete_identity = _identity(result.incompletes[0])
        if seen and _terminal_sort_key(incomplete_identity) != max(
            _terminal_sort_key(identity) for identity in seen
        ):
            raise RelaxationInputError(
                "terminal incomplete must end the emitted resolved prefix"
            )


@dataclass(frozen=True, slots=True)
class R3H2CellFoldInput:
    """One exact config/fold's sealed H4 terminal, never a raw trade prefix."""

    config: R3S3Config | R3S4Config
    fold_id: str
    terminal: SealedS3Terminal | SealedR3S4Terminal

    def __post_init__(self) -> None:
        assert_registered_r3_config(self.config)
        if type(self.fold_id) is not str or self.fold_id not in _FOLD_BY_ID:
            raise RelaxationInputError("fold_id is outside the exact H4 folds")
        _validate_terminal(self)


def _result(source: R3H2CellFoldInput) -> S3EngineResult | R3S4EngineResult:
    return source.terminal.result


def _validate_lineage(
    *,
    trade: S3Trade | R3S4PairTrade,
    config: R3S3Config | R3S4Config,
    fold_id: str,
) -> None:
    if trade.config_id != config.config_id:
        raise RelaxationInputError("H2 trade config_id differs from its R3 cell")
    if trade.fold_id != fold_id:
        raise RelaxationInputError("H2 trade fold_id differs from its H4 fold")


def _validate_phase_time(
    *, trade: S3Trade | R3S4PairTrade, fold_id: str, phase: Phase
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
        and start_ms <= trade.exit_ts <= end_ms
    ):
        raise RelaxationInputError(
            f"H2 trade timestamps are outside the exact {phase} H4 window"
        )


def _validate_terminal_phase(source: R3H2CellFoldInput, phase: Phase) -> None:
    fold = _FOLD_BY_ID[source.fold_id]
    start_ms, end_ms = (
        (fold.train_start_ms, fold.train_end_ms)
        if phase == "TRAIN"
        else (fold.oos_start_ms, fold.oos_end_ms)
    )
    result = _result(source)
    if any(not start_ms <= row.signal_ts < end_ms for row in result.no_trades):
        raise RelaxationInputError(
            f"H2 no-trade signal is outside the exact {phase} H4 window"
        )
    if any(
        not (start_ms <= row.signal_ts < end_ms and start_ms <= row.entry_ts < end_ms)
        for row in result.incompletes
    ):
        raise RelaxationInputError(
            f"H2 terminal incomplete signal/entry is outside the exact {phase} H4 window"
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


def _validate_s4_economics(trade: R3S4PairTrade, config: R3S4Config) -> None:
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
    except KeyError as exc:  # pragma: no cover - DTO independently enforces this
        raise RelaxationInputError("S4 trade legs must have opposing sides") from exc
    if (trade.observed_z > 0.0) != (observed_sign > 0):
        raise RelaxationInputError("S4 observed z sign disagrees with pair direction")
    if trade.z_threshold != config.z_entry:
        raise RelaxationInputError("S4 z threshold differs from its registered R3 cell")
    if abs(trade.observed_z) < config.z_entry:
        raise RelaxationInputError("S4 observed z is below its R3 cell threshold")
    if abs(math.fsum((trade.weight_a, trade.weight_b)) - 1.0) > 1e-9:
        raise RelaxationInputError("S4 H2 weights violate the frozen 1e-9 sum rule")
    g_min = max(6.0 / trade.weight_a, 6.0 / trade.weight_b)
    g_max = min(10.0 / trade.weight_a, 10.0 / trade.weight_b)
    if g_min > g_max:
        raise RelaxationInputError("S4 weights have no feasible $6-10 leg notional")
    if abs(trade.gross_notional - g_min) > _G_TOLERANCE:
        raise RelaxationInputError("S4 gross_notional differs from deterministic G_min")
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
    """Normalize one exact R3 S4 basket trade, preserving entry economics."""

    if type(trade) is not R3S4PairTrade:
        raise TypeError("trade must be an exact R3S4PairTrade")
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
            observed_z=trade.observed_z,
        ),
    )


def _normalize_cell_fold(*, source: R3H2CellFoldInput, phase: Phase) -> CellFoldLedger:
    result = _result(source)
    _validate_terminal_phase(source, phase)
    normalized: list[RelaxationTrade] = []
    for trade in result.trades:
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
        basket_trade_count=len(result.trades),
        trades=tuple(normalized),
    )


def _terminal_evidence(
    *, source: R3H2CellFoldInput, phase: Phase
) -> TerminalIncompleteEvidence | None:
    result = _result(source)
    if not result.incompletes:
        return None
    row = result.incompletes[0]
    if type(row) is S3IncompleteRecord:
        family: Literal["S3", "S4"] = "S3"
        event = EconomicEvent("S3", (row.symbol,), row.signal_ts, row.side)
    else:
        family = "S4"
        direction = (
            "long_a_short_b"
            if (row.side_a, row.side_b) == ("long", "short")
            else "short_a_long_b"
        )
        event = EconomicEvent("S4", row.pair, row.signal_ts, direction)
    return TerminalIncompleteEvidence(
        phase=phase,
        family=family,
        config_id=source.config.config_id,
        fold_id=source.fold_id,
        signal_identity=event,
        entry_ts=row.entry_ts,
        reason=row.reason,
    )


def normalize_r3_phase_ledgers(
    *, phase: object, sources: object
) -> PhaseLedgerEvidence:
    """Build exact canonical 12x8 phase evidence from sealed H4 terminals."""

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
    ledgers = tuple(
        _normalize_cell_fold(source=source, phase=checked_phase) for source in sources
    )
    incompletes = tuple(
        evidence
        for source in sources
        if (evidence := _terminal_evidence(source=source, phase=checked_phase))
        is not None
    )
    return PhaseLedgerEvidence(
        phase=checked_phase,
        ledgers=ledgers,
        terminal_incompletes=incompletes,
    )


__all__ = [
    "R3H2CellFoldInput",
    "normalize_r3_phase_ledgers",
    "normalize_r3_s3_trade",
    "normalize_r3_s4_trade",
]

"""ROB-982 CP6 H4 narrow actual-H2 terminal adapter, dual-seal closure, and
bounded ROB-970 diagnostics.

H4 owns no price/exit/PnL state machine of its own. This module invokes the
ACTUAL merged H2 S3/S4 portfolio engines (``rob974_h2_s3_engine``/
``rob974_h2_s4_engine``), adversarially re-validates their immutable DTO
output against H4-derivable invariants the engine's own constructors do not
enforce (e.g. MAE<=gross<=MFE, exact candidate-identity partition), reseals
the (input, output) pair via two independent canonical hashes, and raises
``H4ContractDrift`` -- never silently accepting or hand-copying a mismatched
field -- when either the engine raises or its output fails resealing.

A first-catch live engine exception is captured as bounded, sanitized
ROB-970 diagnostic evidence (``rob944_diagnostic_evidence``) and attached to
the raised ``H4ContractDrift`` for persistence -- it never participates in
the input/output seals themselves, so diagnostic presence/absence/reordering
cannot move a semantic hash.
"""

from __future__ import annotations

from dataclasses import dataclass

from rob944_diagnostic_evidence import (
    ChildFailureEvidence,
    capture_child_failure_evidence,
)
from rob974_h2_dtos import (
    S3EngineResult,
    S3IncompleteRecord,
    S3NoTradeRecord,
    S3SignalIntent,
    S3Trade,
    S4EngineResult,
    S4IncompleteRecord,
    S4NoTradeRecord,
    S4PairSignalIntent,
    S4PairTrade,
)
from rob974_h2_s3_engine import run_s3_portfolio_stream
from rob974_h2_s4_engine import run_s4_pair_basket_stream

from research_contracts.canonical_hash import canonical_sha256

__all__ = [
    "H4ContractDrift",
    "SealedS3Terminal",
    "SealedS4Terminal",
    "invoke_actual_s3_engine",
    "invoke_actual_s4_engine",
    "seal_s3_engine_input",
    "seal_s3_engine_output",
    "seal_s4_engine_input",
    "seal_s4_engine_output",
    "validate_s3_terminal",
    "validate_s4_terminal",
]


class H4ContractDrift(RuntimeError):
    """Actual H2 engine invocation/output failed H4's adversarial reseal.

    Raised instead of silently accepting, hand-copying, or weakening a
    mismatched field. ``evidence`` is populated only when the underlying
    cause was a live exception from the actual engine call (never for a
    purely structural/output-shape drift, which has no exception to catch).
    """

    def __init__(
        self, message: str, *, evidence: ChildFailureEvidence | None = None
    ) -> None:
        super().__init__(message)
        self.evidence = evidence


@dataclass(frozen=True, slots=True)
class SealedS3Terminal:
    result: S3EngineResult
    input_seal_sha256: str
    output_seal_sha256: str


@dataclass(frozen=True, slots=True)
class SealedS4Terminal:
    result: S4EngineResult
    input_seal_sha256: str
    output_seal_sha256: str


# --- canonical-hashable payload builders (DTOs are not directly hashable) --


def _s3_candidate_payload(candidate: S3SignalIntent) -> dict[str, object]:
    if type(candidate) is not S3SignalIntent:
        raise H4ContractDrift("S3 candidate must be exact S3SignalIntent")
    return {
        "symbol": candidate.symbol,
        "side": candidate.side,
        "signal_ts": candidate.signal_ts,
        "entry_sl_distance": candidate.entry_sl_distance,
        "entry_tp_distance": candidate.entry_tp_distance,
        "config_id": candidate.config_id,
        "fold_id": candidate.fold_id,
        "volatility_percentile": candidate.volatility_percentile,
    }


def _s3_trade_payload(trade: S3Trade) -> dict[str, object]:
    return {
        "symbol": trade.symbol,
        "side": trade.side,
        "config_id": trade.config_id,
        "fold_id": trade.fold_id,
        "signal_ts": trade.signal_ts,
        "entry_ts": trade.entry_ts,
        "entry_price": trade.entry_price,
        "exit_ts": trade.exit_ts,
        "exit_price": trade.exit_price,
        "exit_reason": trade.exit_reason,
        "mfe_bps": trade.mfe_bps,
        "mae_bps": trade.mae_bps,
        "gross_bps": trade.gross_bps,
        "volatility_percentile": trade.volatility_percentile,
    }


def _s3_no_trade_payload(row: S3NoTradeRecord) -> dict[str, object]:
    return {
        "symbol": row.symbol,
        "side": row.side,
        "config_id": row.config_id,
        "fold_id": row.fold_id,
        "signal_ts": row.signal_ts,
        "reason": row.reason,
    }


def _s3_incomplete_payload(row: S3IncompleteRecord) -> dict[str, object]:
    return {
        "symbol": row.symbol,
        "side": row.side,
        "config_id": row.config_id,
        "fold_id": row.fold_id,
        "signal_ts": row.signal_ts,
        "entry_ts": row.entry_ts,
        "entry_price": row.entry_price,
        "reason": row.reason,
    }


def _s4_candidate_payload(candidate: S4PairSignalIntent) -> dict[str, object]:
    if type(candidate) is not S4PairSignalIntent:
        raise H4ContractDrift("S4 candidate must be exact S4PairSignalIntent")
    return {
        "pair": list(candidate.pair),
        "signal_ts": candidate.signal_ts,
        "side_a": candidate.side_a,
        "side_b": candidate.side_b,
        "weight_a": candidate.weight_a,
        "weight_b": candidate.weight_b,
        "beta_a": candidate.beta_a,
        "beta_b": candidate.beta_b,
        "mu": candidate.mu,
        "sigma": candidate.sigma,
        "z_entry": candidate.z_entry,
        "gross_notional": candidate.gross_notional,
        "entry_sl_distance": candidate.entry_sl_distance,
        "entry_tp_distance": candidate.entry_tp_distance,
        "config_id": candidate.config_id,
        "fold_id": candidate.fold_id,
    }


def _s4_trade_payload(trade: S4PairTrade) -> dict[str, object]:
    return {
        "pair": list(trade.pair),
        "side_a": trade.side_a,
        "side_b": trade.side_b,
        "config_id": trade.config_id,
        "fold_id": trade.fold_id,
        "signal_ts": trade.signal_ts,
        "entry_ts": trade.entry_ts,
        "weight_a": trade.weight_a,
        "weight_b": trade.weight_b,
        "beta_a": trade.beta_a,
        "beta_b": trade.beta_b,
        "mu": trade.mu,
        "sigma": trade.sigma,
        "z_entry": trade.z_entry,
        "gross_notional": trade.gross_notional,
        "entry_price_a": trade.entry_price_a,
        "entry_price_b": trade.entry_price_b,
        "exit_ts": trade.exit_ts,
        "exit_price_a": trade.exit_price_a,
        "exit_price_b": trade.exit_price_b,
        "exit_reason": trade.exit_reason,
        "mfe_bps": trade.mfe_bps,
        "mae_bps": trade.mae_bps,
        "gross_bps": trade.gross_bps,
        "order_id_a": trade.order_id_a,
        "order_id_b": trade.order_id_b,
        "pair_exec_status": trade.pair_exec_status,
        "pair_executor_validated": trade.pair_executor_validated,
        "demo_eligible": trade.demo_eligible,
        "volatility_percentile": trade.volatility_percentile,
        "volatility_percentile_provenance": trade.volatility_percentile_provenance,
        "pair_exec_fail": trade.pair_exec_fail,
        "promotion_status": trade.promotion_status,
    }


def _s4_no_trade_payload(row: S4NoTradeRecord) -> dict[str, object]:
    return {
        "pair": list(row.pair),
        "config_id": row.config_id,
        "fold_id": row.fold_id,
        "signal_ts": row.signal_ts,
        "reason": row.reason,
    }


def _s4_incomplete_payload(row: S4IncompleteRecord) -> dict[str, object]:
    return {
        "pair": list(row.pair),
        "side_a": row.side_a,
        "side_b": row.side_b,
        "config_id": row.config_id,
        "fold_id": row.fold_id,
        "signal_ts": row.signal_ts,
        "entry_ts": row.entry_ts,
        "entry_price_a": row.entry_price_a,
        "entry_price_b": row.entry_price_b,
        "reason": row.reason,
    }


def seal_s3_engine_input(
    candidates, *, corpus_end_ts: int, horizon_end_ts: int | None
) -> str:
    payload = {
        "schema_version": "rob974_h4_s3_engine_input_v1",
        "candidates": [_s3_candidate_payload(c) for c in candidates],
        "corpus_end_ts": corpus_end_ts,
        "horizon_end_ts": horizon_end_ts,
    }
    return canonical_sha256(payload)


def seal_s3_engine_output(result: object) -> str:
    if type(result) is not S3EngineResult:
        raise H4ContractDrift("actual H2 S3 engine did not return exact S3EngineResult")
    payload = {
        "schema_version": "rob974_h4_s3_engine_output_v1",
        "trades": [_s3_trade_payload(t) for t in result.trades],
        "no_trades": [_s3_no_trade_payload(n) for n in result.no_trades],
        "incompletes": [_s3_incomplete_payload(i) for i in result.incompletes],
    }
    return canonical_sha256(payload)


def seal_s4_engine_input(
    candidates, *, corpus_end_ts: int, horizon_end_ts: int | None
) -> str:
    payload = {
        "schema_version": "rob974_h4_s4_engine_input_v1",
        "candidates": [_s4_candidate_payload(c) for c in candidates],
        "corpus_end_ts": corpus_end_ts,
        "horizon_end_ts": horizon_end_ts,
    }
    return canonical_sha256(payload)


def seal_s4_engine_output(result: object) -> str:
    if type(result) is not S4EngineResult:
        raise H4ContractDrift("actual H2 S4 engine did not return exact S4EngineResult")
    payload = {
        "schema_version": "rob974_h4_s4_engine_output_v1",
        "trades": [_s4_trade_payload(t) for t in result.trades],
        "no_trades": [_s4_no_trade_payload(n) for n in result.no_trades],
        "incompletes": [_s4_incomplete_payload(i) for i in result.incompletes],
    }
    return canonical_sha256(payload)


# --- adversarial re-validation (H4 re-derives, never trusts blindly) -------


def _identity_from_row(row) -> tuple:
    if hasattr(row, "symbol"):
        return (row.symbol, row.signal_ts)
    return (row.pair, row.signal_ts)


def _validate_identity_partition(candidates, result) -> None:
    identities = {_identity_from_row(c) for c in candidates}
    seen: set[tuple] = set()
    for bucket in (result.trades, result.no_trades, result.incompletes):
        for row in bucket:
            identity = _identity_from_row(row)
            if identity not in identities:
                raise H4ContractDrift(
                    f"engine output referenced an unknown candidate identity {identity!r}"
                )
            if identity in seen:
                raise H4ContractDrift(
                    f"engine output duplicated candidate identity {identity!r} "
                    "across trades/no_trades/incompletes"
                )
            seen.add(identity)
    if not result.incompletes and seen != identities:
        raise H4ContractDrift(
            "engine output did not resolve every candidate identity exactly "
            "once (and no incomplete halt explains the gap)"
        )


def validate_s3_terminal(candidates, result: object) -> None:
    """Adversarially re-derive invariants the DTO constructors do not check."""
    if type(result) is not S3EngineResult:
        raise H4ContractDrift("actual H2 S3 engine did not return exact S3EngineResult")
    _validate_identity_partition(candidates, result)
    for trade in result.trades:
        if trade.exit_reason not in ("TP", "SL", "THESIS_EXIT", "TIMEOUT"):
            raise H4ContractDrift(
                f"S3 exit_reason outside the closed vocabulary: {trade.exit_reason!r}"
            )
        if not (trade.mae_bps <= trade.gross_bps <= trade.mfe_bps):
            raise H4ContractDrift(
                "S3 trade violates MAE<=gross<=MFE "
                f"({trade.mae_bps}, {trade.gross_bps}, {trade.mfe_bps})"
            )
        if trade.mfe_bps < 0.0 or trade.mae_bps > 0.0:
            raise H4ContractDrift(
                "S3 trade MFE/MAE must bracket zero (entry-relative extrema)"
            )


def validate_s4_terminal(candidates, result: object) -> None:
    """Adversarially re-derive invariants the DTO constructors do not check.

    Also re-asserts S4's historical-null execution posture (AC24) even though
    ``S4PairTrade.__post_init__`` already enforces it -- defense in depth: a
    future DTO relaxation must not silently let a promotion-eligible/executed
    row reach H4 unnoticed.
    """
    if type(result) is not S4EngineResult:
        raise H4ContractDrift("actual H2 S4 engine did not return exact S4EngineResult")
    _validate_identity_partition(candidates, result)
    for trade in result.trades:
        if trade.exit_reason not in ("TP", "SL", "MEAN_EXIT", "STALL_EXIT", "TIMEOUT"):
            raise H4ContractDrift(
                f"S4 exit_reason outside the closed vocabulary: {trade.exit_reason!r}"
            )
        if not (trade.mae_bps <= trade.gross_bps <= trade.mfe_bps):
            raise H4ContractDrift(
                "S4 trade violates MAE<=gross<=MFE "
                f"({trade.mae_bps}, {trade.gross_bps}, {trade.mfe_bps})"
            )
        if trade.mfe_bps < 0.0 or trade.mae_bps > 0.0:
            raise H4ContractDrift(
                "S4 trade MFE/MAE must bracket zero (entry-relative extrema)"
            )
        if trade.order_id_a is not None or trade.order_id_b is not None:
            raise H4ContractDrift(
                "S4 trade carries a non-null broker order id -- historical S4 "
                "is screen-only and never executor-validated"
            )
        if trade.demo_eligible is not False:
            raise H4ContractDrift("S4 trade must not claim demo_eligible=True")
        if trade.pair_exec_fail != "not_evaluated":
            raise H4ContractDrift(
                "S4 trade PAIR_EXEC_FAIL must remain 'not_evaluated' -- zero is "
                "never observed evidence for a historical row"
            )


# --- narrow adapter: invoke actual H2, capture first-catch, seal, validate -


def invoke_actual_s3_engine(
    *,
    candidates,
    minute_index,
    close_feature_index,
    corpus_end_ts: int,
    horizon_end_ts: int | None = None,
    strategy: str,
    config_id: str,
    fold_id: str | None = None,
) -> SealedS3Terminal:
    input_seal = seal_s3_engine_input(
        candidates, corpus_end_ts=corpus_end_ts, horizon_end_ts=horizon_end_ts
    )
    try:
        result = run_s3_portfolio_stream(
            candidates,
            minute_index,
            close_feature_index,
            corpus_end_ts=corpus_end_ts,
            horizon_end_ts=horizon_end_ts,
        )
    except Exception as exc:
        evidence = capture_child_failure_evidence(
            exc,
            transport="in_process",
            stage="engine",
            strategy=strategy,
            config_id=config_id,
            fold_id=fold_id,
        )
        raise H4ContractDrift(
            "actual H2 S3 engine raised on invocation", evidence=evidence
        ) from exc
    validate_s3_terminal(candidates, result)
    output_seal = seal_s3_engine_output(result)
    return SealedS3Terminal(result, input_seal, output_seal)


def invoke_actual_s4_engine(
    *,
    candidates,
    minute_index,
    pair_close_index,
    corpus_end_ts: int,
    horizon_end_ts: int | None = None,
    strategy: str,
    config_id: str,
    fold_id: str | None = None,
) -> SealedS4Terminal:
    input_seal = seal_s4_engine_input(
        candidates, corpus_end_ts=corpus_end_ts, horizon_end_ts=horizon_end_ts
    )
    try:
        result = run_s4_pair_basket_stream(
            candidates,
            minute_index,
            pair_close_index,
            corpus_end_ts=corpus_end_ts,
            horizon_end_ts=horizon_end_ts,
        )
    except Exception as exc:
        evidence = capture_child_failure_evidence(
            exc,
            transport="in_process",
            stage="engine",
            strategy=strategy,
            config_id=config_id,
            fold_id=fold_id,
        )
        raise H4ContractDrift(
            "actual H2 S4 engine raised on invocation", evidence=evidence
        ) from exc
    validate_s4_terminal(candidates, result)
    output_seal = seal_s4_engine_output(result)
    return SealedS4Terminal(result, input_seal, output_seal)

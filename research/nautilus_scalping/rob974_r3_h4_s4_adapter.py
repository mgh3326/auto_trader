"""R3 exact-type H4 terminal adapter and frozen-engine parity authority."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import rob974_h2_s4_engine as frozen_s4_engine
from rob944_diagnostic_evidence import capture_child_failure_evidence
from rob974_h2_dtos import (
    S4EngineResult,
    S4IncompleteRecord,
    S4NoTradeRecord,
    S4PairSignalIntent,
    S4PairTrade,
)
from rob974_h4_adapter import H4ContractDrift
from rob974_h4_h6a_adapter import ENGINE_SOURCE_FILES
from rob974_r3_s4_dtos import (
    R3S4EngineResult,
    R3S4IncompleteRecord,
    R3S4NoTradeRecord,
    R3S4PairSignalIntent,
    R3S4PairTrade,
)
from rob974_r3_s4_engine import run_r3_s4_pair_basket_stream

from research_contracts.canonical_hash import canonical_json, canonical_sha256

__all__ = [
    "R3_ENGINE_SOURCE_FILES",
    "R3S4FrozenParityEvidence",
    "R3S4ParityDrift",
    "SealedR3S4Terminal",
    "assert_r3_s4_frozen_parity",
    "invoke_r3_s4_engine",
    "seal_r3_s4_engine_input",
    "seal_r3_s4_engine_output",
    "validate_r3_s4_terminal",
]

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent

# R3 inherits the complete frozen R2 engine inventory and adds only the
# manifest resolver plus its named DTO/engine boundary.  H3/H4 orchestration
# belongs to the separate runner inventory owned by the captain.
R3_ENGINE_SOURCE_FILES: tuple[tuple[str, Path], ...] = (
    *ENGINE_SOURCE_FILES,
    (
        "research/nautilus_scalping/rob974_r3_manifest.py",
        _REPO_ROOT / "research/nautilus_scalping/rob974_r3_manifest.py",
    ),
    (
        "research/nautilus_scalping/rob974_r3_s4_dtos.py",
        _REPO_ROOT / "research/nautilus_scalping/rob974_r3_s4_dtos.py",
    ),
    (
        "research/nautilus_scalping/rob974_r3_s4_engine.py",
        _REPO_ROOT / "research/nautilus_scalping/rob974_r3_s4_engine.py",
    ),
)


class R3S4ParityDrift(H4ContractDrift):
    """R3 execution differs from the frozen engine on representable input."""


@dataclass(frozen=True, slots=True)
class SealedR3S4Terminal:
    result: R3S4EngineResult
    input_seal_sha256: str
    output_seal_sha256: str

    def __post_init__(self) -> None:
        if type(self.result) is not R3S4EngineResult:
            raise TypeError("result must be an exact R3S4EngineResult")
        for name in ("input_seal_sha256", "output_seal_sha256"):
            value = getattr(self, name)
            if (
                type(value) is not str
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
            ):
                raise ValueError(f"{name} must be lowercase 64-hex")


@dataclass(frozen=True, slots=True)
class R3S4FrozenParityEvidence:
    r2_input_bytes: bytes
    r3_input_bytes: bytes
    r2_output_bytes: bytes
    r3_output_bytes: bytes
    input_sha256: str
    output_sha256: str


def _r3_candidate_payload(candidate: object) -> dict[str, object]:
    if type(candidate) is not R3S4PairSignalIntent:
        raise TypeError("candidate must be exact R3S4PairSignalIntent")
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
        "observed_z": candidate.observed_z,
        "z_threshold": candidate.z_threshold,
        "gross_notional": candidate.gross_notional,
        "entry_sl_distance": candidate.entry_sl_distance,
        "entry_tp_distance": candidate.entry_tp_distance,
        "config_id": candidate.config_id,
        "fold_id": candidate.fold_id,
    }


def _r3_trade_payload(trade: R3S4PairTrade) -> dict[str, object]:
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
        "observed_z": trade.observed_z,
        "z_threshold": trade.z_threshold,
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


def _no_trade_payload(row: R3S4NoTradeRecord) -> dict[str, object]:
    return {
        "pair": list(row.pair),
        "config_id": row.config_id,
        "fold_id": row.fold_id,
        "signal_ts": row.signal_ts,
        "reason": row.reason,
    }


def _incomplete_payload(row: R3S4IncompleteRecord) -> dict[str, object]:
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


def seal_r3_s4_engine_input(
    candidates: object, *, corpus_end_ts: int, horizon_end_ts: int | None
) -> str:
    if type(candidates) not in (list, tuple):
        raise TypeError("candidates must be a list or tuple")
    payload = {
        "schema_version": "rob974_r3_h4_s4_engine_input_v1",
        "candidates": [_r3_candidate_payload(candidate) for candidate in candidates],
        "corpus_end_ts": corpus_end_ts,
        "horizon_end_ts": horizon_end_ts,
    }
    return canonical_sha256(payload)


def seal_r3_s4_engine_output(result: object) -> str:
    if type(result) is not R3S4EngineResult:
        raise H4ContractDrift("R3 S4 engine did not return exact R3S4EngineResult")
    payload = {
        "schema_version": "rob974_r3_h4_s4_engine_output_v1",
        "trades": [_r3_trade_payload(row) for row in result.trades],
        "no_trades": [_no_trade_payload(row) for row in result.no_trades],
        "incompletes": [_incomplete_payload(row) for row in result.incompletes],
    }
    return canonical_sha256(payload)


def _identity(row: object) -> tuple[tuple[str, str], int]:
    return row.pair, row.signal_ts  # type: ignore[attr-defined, no-any-return]


def validate_r3_s4_terminal(
    candidates: object, result: object, *, config_id: str, fold_id: str | None
) -> None:
    if type(candidates) not in (list, tuple) or any(
        type(candidate) is not R3S4PairSignalIntent for candidate in candidates
    ):
        raise TypeError("candidates must contain exact R3S4PairSignalIntent values")
    if type(result) is not R3S4EngineResult:
        raise H4ContractDrift("R3 S4 engine did not return exact R3S4EngineResult")
    if any(
        candidate.config_id != config_id or candidate.fold_id != fold_id
        for candidate in candidates
    ):
        raise H4ContractDrift("R3 H4 invocation lineage differs from its candidates")

    candidate_identities = tuple(_identity(candidate) for candidate in candidates)
    if len(candidate_identities) != len(set(candidate_identities)):
        raise H4ContractDrift("R3 H4 input contains duplicate candidate identities")
    by_identity = {_identity(candidate): candidate for candidate in candidates}
    seen: set[tuple[tuple[str, str], int]] = set()
    for bucket in (result.trades, result.no_trades, result.incompletes):
        for row in bucket:
            identity = _identity(row)
            if identity not in by_identity:
                raise H4ContractDrift("R3 engine emitted an unknown candidate identity")
            if identity in seen:
                raise H4ContractDrift("R3 engine duplicated an identity across buckets")
            seen.add(identity)
            if row.config_id != config_id or row.fold_id != fold_id:
                raise H4ContractDrift(
                    "R3 engine output lineage differs from invocation"
                )
    if not result.incompletes and seen != set(by_identity):
        raise H4ContractDrift("R3 engine did not resolve every candidate identity")
    if len(result.incompletes) > 1:
        raise H4ContractDrift("R3 engine emitted more than one terminal incomplete")
    if result.incompletes:
        incomplete = result.incompletes[0]
        candidate = by_identity[_identity(incomplete)]
        if (incomplete.side_a, incomplete.side_b) != (
            candidate.side_a,
            candidate.side_b,
        ):
            raise H4ContractDrift("R3 incomplete changed candidate leg directions")
        ordered_identities = tuple(
            sorted(
                candidate_identities, key=lambda identity: (identity[1], identity[0])
            )
        )
        incomplete_index = ordered_identities.index(_identity(incomplete))
        expected_prefix = set(ordered_identities[: incomplete_index + 1])
        if seen != expected_prefix:
            raise H4ContractDrift(
                "R3 incomplete output is not the exact resolved-prefix partition"
            )

    for trade in result.trades:
        candidate = by_identity[_identity(trade)]
        expected_provenance = (
            candidate.side_a,
            candidate.side_b,
            candidate.weight_a,
            candidate.weight_b,
            candidate.beta_a,
            candidate.beta_b,
            candidate.mu,
            candidate.sigma,
            candidate.observed_z,
            candidate.z_threshold,
            candidate.gross_notional,
        )
        actual_provenance = (
            trade.side_a,
            trade.side_b,
            trade.weight_a,
            trade.weight_b,
            trade.beta_a,
            trade.beta_b,
            trade.mu,
            trade.sigma,
            trade.observed_z,
            trade.z_threshold,
            trade.gross_notional,
        )
        if actual_provenance != expected_provenance:
            raise H4ContractDrift("R3 trade changed entry-frozen candidate provenance")
        if not trade.mae_bps <= trade.gross_bps <= trade.mfe_bps:
            raise H4ContractDrift("R3 trade violates MAE<=gross<=MFE")
        if trade.mfe_bps < 0.0 or trade.mae_bps > 0.0:
            raise H4ContractDrift("R3 trade MFE/MAE does not bracket zero")
        if (
            trade.order_id_a is not None
            or trade.order_id_b is not None
            or trade.pair_executor_validated is not False
            or trade.demo_eligible is not False
            or trade.pair_exec_fail != "not_evaluated"
            or trade.promotion_status != "promotion_blocked_pending_pair_executor"
        ):
            raise H4ContractDrift("R3 trade historical-only posture drifted")


def invoke_r3_s4_engine(
    *,
    candidates: object,
    minute_index: object,
    pair_close_index: object,
    corpus_end_ts: int,
    horizon_end_ts: int | None = None,
    strategy: str,
    config_id: str,
    fold_id: str | None = None,
) -> SealedR3S4Terminal:
    input_seal = seal_r3_s4_engine_input(
        candidates, corpus_end_ts=corpus_end_ts, horizon_end_ts=horizon_end_ts
    )
    try:
        result = run_r3_s4_pair_basket_stream(
            candidates,  # type: ignore[arg-type]
            minute_index,  # type: ignore[arg-type]
            pair_close_index,  # type: ignore[arg-type]
            corpus_end_ts=corpus_end_ts,
            horizon_end_ts=horizon_end_ts,
        )
    except Exception as exc:
        evidence = capture_child_failure_evidence(
            exc,
            transport="in_process",
            stage="r3_engine",
            strategy=strategy,
            config_id=config_id,
            fold_id=fold_id,
        )
        raise H4ContractDrift(
            "R3 S4 engine raised on invocation", evidence=evidence
        ) from exc
    validate_r3_s4_terminal(candidates, result, config_id=config_id, fold_id=fold_id)
    return SealedR3S4Terminal(
        result=result,
        input_seal_sha256=input_seal,
        output_seal_sha256=seal_r3_s4_engine_output(result),
    )


def _frozen_parity_intent(candidate: R3S4PairSignalIntent) -> S4PairSignalIntent:
    if abs(candidate.observed_z) < 1.0:
        raise ValueError("frozen parity requires representable |observed_z| >= 1")
    return S4PairSignalIntent(
        pair=candidate.pair,
        signal_ts=candidate.signal_ts,
        side_a=candidate.side_a,
        side_b=candidate.side_b,
        weight_a=candidate.weight_a,
        weight_b=candidate.weight_b,
        beta_a=candidate.beta_a,
        beta_b=candidate.beta_b,
        mu=candidate.mu,
        sigma=candidate.sigma,
        z_entry=candidate.observed_z,
        gross_notional=candidate.gross_notional,
        entry_sl_distance=candidate.entry_sl_distance,
        entry_tp_distance=candidate.entry_tp_distance,
        config_id=candidate.config_id,
        fold_id=candidate.fold_id,
    )


def _economic_candidate_payload(candidate: object) -> dict[str, object]:
    if type(candidate) is R3S4PairSignalIntent:
        observed_z = candidate.observed_z
    elif type(candidate) is S4PairSignalIntent:
        observed_z = candidate.z_entry
    else:
        raise TypeError("parity candidate has an unsupported exact type")
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
        "z_entry": observed_z,
        "gross_notional": candidate.gross_notional,
        "entry_sl_distance": candidate.entry_sl_distance,
        "entry_tp_distance": candidate.entry_tp_distance,
        "config_id": candidate.config_id,
        "fold_id": candidate.fold_id,
    }


def _economic_row_payload(row: object) -> dict[str, object]:
    if type(row) in (R3S4PairTrade, S4PairTrade):
        observed_z = row.observed_z if type(row) is R3S4PairTrade else row.z_entry
        return {
            "kind": "trade",
            "pair": list(row.pair),
            "side_a": row.side_a,
            "side_b": row.side_b,
            "config_id": row.config_id,
            "fold_id": row.fold_id,
            "signal_ts": row.signal_ts,
            "entry_ts": row.entry_ts,
            "weight_a": row.weight_a,
            "weight_b": row.weight_b,
            "beta_a": row.beta_a,
            "beta_b": row.beta_b,
            "mu": row.mu,
            "sigma": row.sigma,
            "z_entry": observed_z,
            "gross_notional": row.gross_notional,
            "entry_price_a": row.entry_price_a,
            "entry_price_b": row.entry_price_b,
            "exit_ts": row.exit_ts,
            "exit_price_a": row.exit_price_a,
            "exit_price_b": row.exit_price_b,
            "exit_reason": row.exit_reason,
            "mfe_bps": row.mfe_bps,
            "mae_bps": row.mae_bps,
            "gross_bps": row.gross_bps,
            "order_id_a": row.order_id_a,
            "order_id_b": row.order_id_b,
            "pair_exec_status": row.pair_exec_status,
            "pair_executor_validated": row.pair_executor_validated,
            "demo_eligible": row.demo_eligible,
            "volatility_percentile": row.volatility_percentile,
            "volatility_percentile_provenance": row.volatility_percentile_provenance,
            "pair_exec_fail": row.pair_exec_fail,
            "promotion_status": row.promotion_status,
        }
    if type(row) in (R3S4NoTradeRecord, S4NoTradeRecord):
        return {
            "kind": "no_trade",
            "pair": list(row.pair),
            "config_id": row.config_id,
            "fold_id": row.fold_id,
            "signal_ts": row.signal_ts,
            "reason": row.reason,
        }
    if type(row) not in (R3S4IncompleteRecord, S4IncompleteRecord):
        raise TypeError("parity row has an unsupported exact type")
    return {
        "kind": "incomplete",
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


def _economic_result_payload(result: object) -> dict[str, object]:
    if type(result) not in (S4EngineResult, R3S4EngineResult):
        raise TypeError("parity result has an unsupported exact type")
    return {
        "trades": [_economic_row_payload(row) for row in result.trades],
        "no_trades": [_economic_row_payload(row) for row in result.no_trades],
        "incompletes": [_economic_row_payload(row) for row in result.incompletes],
    }


def _canonical_bytes(payload: object) -> bytes:
    return canonical_json(payload).encode("utf-8")


def assert_r3_s4_frozen_parity(
    *,
    candidates: object,
    minute_index: object,
    pair_close_index: object,
    corpus_end_ts: int,
    horizon_end_ts: int | None = None,
) -> R3S4FrozenParityEvidence:
    if type(candidates) not in (list, tuple) or any(
        type(candidate) is not R3S4PairSignalIntent for candidate in candidates
    ):
        raise TypeError("parity candidates must be exact R3S4PairSignalIntent values")
    r3_candidates = list(candidates)
    frozen_candidates = [_frozen_parity_intent(row) for row in r3_candidates]
    envelope = {
        "corpus_end_ts": corpus_end_ts,
        "horizon_end_ts": horizon_end_ts,
    }
    r2_input_bytes = _canonical_bytes(
        {
            **envelope,
            "candidates": [
                _economic_candidate_payload(row) for row in frozen_candidates
            ],
        }
    )
    r3_input_bytes = _canonical_bytes(
        {
            **envelope,
            "candidates": [_economic_candidate_payload(row) for row in r3_candidates],
        }
    )
    if r2_input_bytes != r3_input_bytes:
        raise R3S4ParityDrift("R3/frozen input canonical bytes differ")

    def _capture(callable_, call_candidates: object) -> dict[str, object]:
        try:
            result = callable_(
                call_candidates,
                minute_index,
                pair_close_index,
                corpus_end_ts=corpus_end_ts,
                horizon_end_ts=horizon_end_ts,
            )
        except Exception as exc:
            return {
                "kind": "exception",
                "type_module": type(exc).__module__,
                "type_name": type(exc).__qualname__,
                "args": tuple(exc.args),
            }
        return {"kind": "result", "result": _economic_result_payload(result)}

    frozen_outcome = _capture(
        frozen_s4_engine.run_s4_pair_basket_stream, frozen_candidates
    )
    r3_outcome = _capture(run_r3_s4_pair_basket_stream, r3_candidates)
    r2_output_bytes = _canonical_bytes(frozen_outcome)
    r3_output_bytes = _canonical_bytes(r3_outcome)
    if r2_output_bytes != r3_output_bytes:
        raise R3S4ParityDrift("R3/frozen output canonical bytes differ")
    return R3S4FrozenParityEvidence(
        r2_input_bytes=r2_input_bytes,
        r3_input_bytes=r3_input_bytes,
        r2_output_bytes=r2_output_bytes,
        r3_output_bytes=r3_output_bytes,
        input_sha256=hashlib.sha256(r2_input_bytes).hexdigest(),
        output_sha256=hashlib.sha256(r2_output_bytes).hexdigest(),
    )

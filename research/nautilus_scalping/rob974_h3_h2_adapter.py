"""ROB-980 CP8 -- the sole concrete merged-H2 integration seam.

H3's formula/generator modules remain independent of H2 concrete classes.
This adapter alone verifies the merged ROB-979 schemas, converts accepted H3
payloads without re-estimation, reuses ROB-979's actual H1 bridge, invokes the
real account-global engines, and delegates the base13 ledger to H2's scenario
module.  It has no behavior/funding callback and no fallback/rerank path.
"""

from __future__ import annotations

import dataclasses
import inspect
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import rob974_h2_s3_engine as h2_s3_engine
import rob974_h2_s4_engine as h2_s4_engine
from rob974_h2_dtos import (
    S3EngineResult,
    S3SignalIntent,
    S3Trade,
    S4EngineResult,
    S4PairSignalIntent,
    S4PairTrade,
)
from rob974_h2_h1_bridge import (
    from_h1_close_features,
    from_h1_minute_bars,
    from_h1_pair_leg_closes,
)
from rob974_h2_ingress import build_minute_index
from rob974_h2_scenarios import (
    PATH_SCENARIO_BASE13,
    S3ScenarioTradeRow,
    S4ScenarioTradeRow,
    build_s3_scenario_ledger,
    build_s4_scenario_ledger,
    s3_ledger_hash,
    s4_ledger_hash,
)
from rob974_h3_manifest import SYMBOLS
from rob974_h3_s3 import FeatureContext, S3Candidate, S3GeneratorOutput
from rob974_h3_s4 import S4Candidate, S4GeneratorOutput

from research_contracts.canonical_hash import canonical_sha256

H2_MERGE_SHA = "0b81057c7b450f1539c836fd6cfa5732fb5800c5"
INTEGRATION_SCHEMA_VERSION = "rob974-h3-h2-integration-v1"


class ContractDriftError(RuntimeError):
    """The merged H2 surface no longer matches the reviewed CP8 mapping."""


_EXPECTED_DATACLASS_FIELDS: tuple[tuple[str, object, tuple[str, ...]], ...] = (
    (
        "S3SignalIntent",
        S3SignalIntent,
        (
            "symbol",
            "side",
            "signal_ts",
            "entry_sl_distance",
            "entry_tp_distance",
            "config_id",
            "fold_id",
            "volatility_percentile",
        ),
    ),
    (
        "S4PairSignalIntent",
        S4PairSignalIntent,
        (
            "pair",
            "signal_ts",
            "side_a",
            "side_b",
            "weight_a",
            "weight_b",
            "beta_a",
            "beta_b",
            "mu",
            "sigma",
            "z_entry",
            "gross_notional",
            "entry_sl_distance",
            "entry_tp_distance",
            "config_id",
            "fold_id",
        ),
    ),
    (
        "S3Trade",
        S3Trade,
        (
            "symbol",
            "side",
            "config_id",
            "fold_id",
            "signal_ts",
            "entry_ts",
            "entry_price",
            "exit_ts",
            "exit_price",
            "exit_reason",
            "mfe_bps",
            "mae_bps",
            "gross_bps",
            "volatility_percentile",
        ),
    ),
    (
        "S4PairTrade",
        S4PairTrade,
        (
            "pair",
            "side_a",
            "side_b",
            "config_id",
            "fold_id",
            "signal_ts",
            "entry_ts",
            "weight_a",
            "weight_b",
            "beta_a",
            "beta_b",
            "mu",
            "sigma",
            "z_entry",
            "gross_notional",
            "entry_price_a",
            "entry_price_b",
            "exit_ts",
            "exit_price_a",
            "exit_price_b",
            "exit_reason",
            "mfe_bps",
            "mae_bps",
            "gross_bps",
            "order_id_a",
            "order_id_b",
            "pair_exec_status",
            "pair_executor_validated",
            "demo_eligible",
            "volatility_percentile",
            "volatility_percentile_provenance",
            "pair_exec_fail",
            "promotion_status",
        ),
    ),
    ("S3EngineResult", S3EngineResult, ("trades", "no_trades", "incompletes")),
    ("S4EngineResult", S4EngineResult, ("trades", "no_trades", "incompletes")),
    (
        "S3ScenarioTradeRow",
        S3ScenarioTradeRow,
        (
            "trade",
            "path_scenario",
            "funding_bps",
            "e13_bps",
            "e17_bps",
            "e22_bps",
            "thesis_exit_flag",
            "timeout_flag",
        ),
    ),
    (
        "S4ScenarioTradeRow",
        S4ScenarioTradeRow,
        (
            "trade",
            "path_scenario",
            "funding_bps",
            "e13_bps",
            "e17_bps",
            "e22_bps",
            "thesis_exit_flag",
            "timeout_flag",
        ),
    ),
)

_EXPECTED_CALL_FIELDS = (
    (
        "run_s3_portfolio_stream",
        h2_s3_engine.run_s3_portfolio_stream,
        (
            "candidates",
            "minute_index",
            "close_feature_index",
            "corpus_end_ts",
            "horizon_end_ts",
        ),
    ),
    (
        "run_s4_pair_basket_stream",
        h2_s4_engine.run_s4_pair_basket_stream,
        (
            "candidates",
            "minute_index",
            "pair_close_index",
            "corpus_end_ts",
            "horizon_end_ts",
        ),
    ),
    ("from_h1_minute_bars", from_h1_minute_bars, ("symbol", "rows")),
    (
        "from_h1_close_features",
        from_h1_close_features,
        ("symbol", "bars4h", "snapshots"),
    ),
    (
        "from_h1_pair_leg_closes",
        from_h1_pair_leg_closes,
        ("symbol", "bars4h"),
    ),
)


def _contract_drift(message: str) -> ContractDriftError:
    return ContractDriftError(f"CONTRACT_DRIFT: {message}")


def verify_h2_contract() -> None:
    """Fail closed if any reviewed DTO, bridge, or engine seam has moved."""
    # Resolve the two intent classes dynamically so a runtime replacement or
    # future merged edit cannot be masked by the static seal table above.
    current_types = {
        "S3SignalIntent": S3SignalIntent,
        "S4PairSignalIntent": S4PairSignalIntent,
        "S3Trade": S3Trade,
        "S4PairTrade": S4PairTrade,
        "S3EngineResult": S3EngineResult,
        "S4EngineResult": S4EngineResult,
        "S3ScenarioTradeRow": S3ScenarioTradeRow,
        "S4ScenarioTradeRow": S4ScenarioTradeRow,
    }
    for name, _sealed_type, expected in _EXPECTED_DATACLASS_FIELDS:
        current = current_types[name]
        try:
            actual = tuple(field.name for field in dataclasses.fields(current))
        except TypeError as exc:
            raise _contract_drift(f"{name} is no longer a dataclass") from exc
        if actual != expected:
            raise _contract_drift(
                f"{name} fields changed: expected={expected!r} actual={actual!r}"
            )
    for name, function, expected in _EXPECTED_CALL_FIELDS:
        actual = tuple(inspect.signature(function).parameters)
        if actual != expected:
            raise _contract_drift(
                f"{name} signature changed: expected={expected!r} actual={actual!r}"
            )
    if h2_s3_engine.MAX_HOLD_BARS != 12:
        raise _contract_drift("S3 MAX_HOLD_BARS is no longer 12")
    if h2_s4_engine.MAX_HOLD_BARS != 9:
        raise _contract_drift("S4 MAX_HOLD_BARS is no longer 9")
    if PATH_SCENARIO_BASE13 != "base13":
        raise _contract_drift("H2 base scenario label changed")


def _fold_id(value: object) -> str:
    if type(value) is not str or not value:
        raise TypeError("fold_id must be a non-empty built-in str")
    return value


def _exact_int(value: object, name: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{name} must be exact built-in int")
    return value


def _adapted_or_drift(factory, *, strategy: str):
    try:
        return factory()
    except (TypeError, ValueError) as exc:
        raise _contract_drift(
            f"merged H2 rejected an invariant-valid {strategy} candidate: {exc}"
        ) from exc


def adapt_s3_candidate(candidate: S3Candidate, *, fold_id: str) -> S3SignalIntent:
    verify_h2_contract()
    if type(candidate) is not S3Candidate:
        raise TypeError("candidate must be exact H3 S3Candidate")
    fold_id = _fold_id(fold_id)
    if candidate.max_hold_4h_bars != h2_s3_engine.MAX_HOLD_BARS:
        raise _contract_drift("S3 maximum-hold authority differs between H3 and H2")
    return _adapted_or_drift(
        lambda: S3SignalIntent(
            symbol=candidate.symbol,
            side=candidate.side,
            signal_ts=candidate.decision_ts,
            entry_sl_distance=candidate.d_SL,
            entry_tp_distance=candidate.d_TP,
            config_id=candidate.config_id,
            fold_id=fold_id,
            volatility_percentile=candidate.volatility_percentile,
        ),
        strategy="S3",
    )


def adapt_s4_candidate(candidate: S4Candidate, *, fold_id: str) -> S4PairSignalIntent:
    verify_h2_contract()
    if type(candidate) is not S4Candidate:
        raise TypeError("candidate must be exact H3 S4Candidate")
    fold_id = _fold_id(fold_id)
    if candidate.max_hold_4h_bars != h2_s4_engine.MAX_HOLD_BARS:
        raise _contract_drift("S4 maximum-hold authority differs between H3 and H2")
    if candidate.volatility_percentile is not None or (
        candidate.volatility_percentile_provenance != "not_defined_for_s4"
    ):
        raise _contract_drift("S4 volatility-null provenance differs from merged H2")
    return _adapted_or_drift(
        lambda: S4PairSignalIntent(
            pair=(candidate.symbol_a, candidate.symbol_b),
            signal_ts=candidate.decision_ts,
            side_a=candidate.side_a,
            side_b=candidate.side_b,
            weight_a=candidate.weight_a,
            weight_b=candidate.weight_b,
            beta_a=candidate.beta_a,
            beta_b=candidate.beta_b,
            mu=candidate.mu,
            # H2's historical name `sigma` means the entry-frozen effective
            # MAD spread scale.  It must never receive sigma_pair_risk.
            sigma=candidate.effective_mad_scale,
            # H2's historical name `z_entry` means the signed observed z at
            # entry.  It must never receive the unsigned config threshold.
            z_entry=candidate.observed_z,
            gross_notional=candidate.gross_notional_usd,
            entry_sl_distance=candidate.d_SL,
            entry_tp_distance=candidate.d_TP,
            config_id=candidate.config_id,
            fold_id=fold_id,
        ),
        strategy="S4",
    )


def adapt_s3_pbo_candidate_buffer(
    output: S3GeneratorOutput,
) -> list[S3SignalIntent]:
    """Adapt one H3 invocation into a fresh no-fold PBO candidate buffer.

    A built-in empty tuple is a process-wide singleton in CPython.  Returning
    ``tuple(adapted for ...)`` therefore aliases two configs whenever both H3
    invocations accept no candidates.  PBO requires container independence
    even for an empty result, so this generator-side boundary deliberately
    materializes a new list on every call.  Candidate order and values are
    unchanged; the H2 engine sorts a copy and never mutates this buffer.
    """

    verify_h2_contract()
    if type(output) is not S3GeneratorOutput:
        raise TypeError("output must be exact H3 S3GeneratorOutput")
    return [
        dataclasses.replace(
            adapt_s3_candidate(candidate, fold_id="pbo-full-window"),
            fold_id=None,
        )
        for candidate in output.accepted
    ]


def adapt_s4_pbo_candidate_buffer(
    output: S4GeneratorOutput,
) -> list[S4PairSignalIntent]:
    """Adapt one H3 invocation into a fresh no-fold PBO candidate buffer."""

    verify_h2_contract()
    if type(output) is not S4GeneratorOutput:
        raise TypeError("output must be exact H3 S4GeneratorOutput")
    return [
        dataclasses.replace(
            adapt_s4_candidate(candidate, fold_id="pbo-full-window"),
            fold_id=None,
        )
        for candidate in output.accepted
    ]


def _plain(value: object) -> object:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _plain(getattr(value, field.name))
            for field in dataclasses.fields(value)
        }
    if type(value) is tuple:
        return [_plain(item) for item in value]
    if type(value) is list:
        return [_plain(item) for item in value]
    if type(value) is dict:
        return {key: _plain(item) for key, item in value.items()}
    if value is None or type(value) in (str, int, float, bool):
        return value
    raise TypeError(f"unsupported integration payload type {type(value).__name__}")


@dataclass(frozen=True, slots=True)
class H2IntegrationResult:
    fold_id: str
    s3_intents: tuple[S3SignalIntent, ...]
    s4_intents: tuple[S4PairSignalIntent, ...]
    s3_engine_result: S3EngineResult
    s4_engine_result: S4EngineResult
    s3_scenario_rows: tuple[S3ScenarioTradeRow, ...]
    s4_scenario_rows: tuple[S4ScenarioTradeRow, ...]
    s3_ledger_hash: str
    s4_ledger_hash: str

    def __post_init__(self) -> None:
        _fold_id(self.fold_id)
        if type(self.s3_intents) is not tuple or any(
            type(item) is not S3SignalIntent for item in self.s3_intents
        ):
            raise TypeError("s3_intents must contain exact merged-H2 DTOs")
        if type(self.s4_intents) is not tuple or any(
            type(item) is not S4PairSignalIntent for item in self.s4_intents
        ):
            raise TypeError("s4_intents must contain exact merged-H2 DTOs")
        if type(self.s3_engine_result) is not S3EngineResult:
            raise TypeError("s3_engine_result must be exact merged-H2 result")
        if type(self.s4_engine_result) is not S4EngineResult:
            raise TypeError("s4_engine_result must be exact merged-H2 result")
        if type(self.s3_scenario_rows) is not tuple or any(
            type(item) is not S3ScenarioTradeRow for item in self.s3_scenario_rows
        ):
            raise TypeError("s3_scenario_rows must be exact merged-H2 rows")
        if type(self.s4_scenario_rows) is not tuple or any(
            type(item) is not S4ScenarioTradeRow for item in self.s4_scenario_rows
        ):
            raise TypeError("s4_scenario_rows must be exact merged-H2 rows")
        for name in ("s3_ledger_hash", "s4_ledger_hash"):
            value = getattr(self, name)
            if type(value) is not str or len(value) != 64:
                raise TypeError(f"{name} must be a SHA-256 hex string")

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": INTEGRATION_SCHEMA_VERSION,
            "h2_merge_sha": H2_MERGE_SHA,
            "fold_id": self.fold_id,
            "path_scenario": PATH_SCENARIO_BASE13,
            "funding_authority": "h2_scenario_default_no_lookup",
            "s3_intents": _plain(self.s3_intents),
            "s4_intents": _plain(self.s4_intents),
            "s3_engine_result": _plain(self.s3_engine_result),
            "s4_engine_result": _plain(self.s4_engine_result),
            "s3_scenario_rows": _plain(self.s3_scenario_rows),
            "s4_scenario_rows": _plain(self.s4_scenario_rows),
            "s3_ledger_hash": self.s3_ledger_hash,
            "s4_ledger_hash": self.s4_ledger_hash,
        }

    @property
    def content_hash(self) -> str:
        return canonical_sha256(self.to_payload())

    def to_json_bytes(self) -> bytes:
        payload = {**self.to_payload(), "content_hash": self.content_hash}
        return (
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")


def _require_h1_minutes(
    value: object,
) -> Mapping[str, Sequence[object]]:
    if not isinstance(value, Mapping) or set(value) != set(SYMBOLS):
        raise ValueError("h1_minutes must cover the exact frozen H1 universe")
    for symbol in SYMBOLS:
        if not isinstance(value[symbol], Sequence):
            raise TypeError("each H1 minute history must be a sequence")
    return value


def run_h2_integration(
    s3_output: S3GeneratorOutput,
    s4_output: S4GeneratorOutput,
    h1_minutes: Mapping[str, Sequence[object]],
    feature_context: FeatureContext,
    *,
    fold_id: str,
    corpus_end_ts: int,
    horizon_end_ts: int | None = None,
) -> H2IntegrationResult:
    """Run accepted global H3 candidates through real H2 engines/scenario rows."""
    verify_h2_contract()
    if type(s3_output) is not S3GeneratorOutput:
        raise TypeError("s3_output must be exact H3 S3GeneratorOutput")
    if type(s4_output) is not S4GeneratorOutput:
        raise TypeError("s4_output must be exact H3 S4GeneratorOutput")
    if type(feature_context) is not FeatureContext:
        raise TypeError("feature_context must be exact H3 FeatureContext")
    fold_id = _fold_id(fold_id)
    corpus_end_ts = _exact_int(corpus_end_ts, "corpus_end_ts")
    if horizon_end_ts is not None:
        horizon_end_ts = _exact_int(horizon_end_ts, "horizon_end_ts")
    minutes = _require_h1_minutes(h1_minutes)

    s3_intents = tuple(
        adapt_s3_candidate(candidate, fold_id=fold_id)
        for candidate in s3_output.accepted
    )
    s4_intents = tuple(
        adapt_s4_candidate(candidate, fold_id=fold_id)
        for candidate in s4_output.accepted
    )

    normalized_minutes = tuple(
        row
        for symbol in SYMBOLS
        for row in from_h1_minute_bars(symbol, minutes[symbol])
    )
    minute_index = build_minute_index(normalized_minutes)

    close_features = tuple(
        row
        for symbol in SYMBOLS
        for row in from_h1_close_features(
            symbol, feature_context.bars_for(symbol), feature_context.snapshots
        )
    )
    close_feature_index = {(row.symbol, row.close_ts): row for row in close_features}
    pair_closes = tuple(
        row
        for symbol in SYMBOLS
        for row in from_h1_pair_leg_closes(symbol, feature_context.bars_for(symbol))
    )
    pair_close_index = {(row.symbol, row.close_ts): row for row in pair_closes}

    s3_result = h2_s3_engine.run_s3_portfolio_stream(
        s3_intents,
        minute_index,
        close_feature_index,
        corpus_end_ts=corpus_end_ts,
        horizon_end_ts=horizon_end_ts,
    )
    s4_result = h2_s4_engine.run_s4_pair_basket_stream(
        s4_intents,
        minute_index,
        pair_close_index,
        corpus_end_ts=corpus_end_ts,
        horizon_end_ts=horizon_end_ts,
    )

    # Scenario/funding math remains H2-owned.  No lookup/callback is accepted
    # by this seam; the explicit synthetic integration has no funding rows.
    s3_rows = build_s3_scenario_ledger(s3_result.trades, PATH_SCENARIO_BASE13)
    s4_rows = build_s4_scenario_ledger(s4_result.trades, PATH_SCENARIO_BASE13)
    return H2IntegrationResult(
        fold_id,
        s3_intents,
        s4_intents,
        s3_result,
        s4_result,
        s3_rows,
        s4_rows,
        s3_ledger_hash(s3_rows),
        s4_ledger_hash(s4_rows),
    )


__all__ = [
    "ContractDriftError",
    "H2IntegrationResult",
    "H2_MERGE_SHA",
    "INTEGRATION_SCHEMA_VERSION",
    "adapt_s3_candidate",
    "adapt_s3_pbo_candidate_buffer",
    "adapt_s4_candidate",
    "adapt_s4_pbo_candidate_buffer",
    "run_h2_integration",
    "verify_h2_contract",
]

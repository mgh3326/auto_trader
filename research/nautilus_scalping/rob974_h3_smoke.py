"""Persisted fake-free ROB-974 H3 generator smoke and frozen-fold command.

The synthetic path deliberately traverses the real ROB-941 writer, manifest,
Parquet shards and offline loader before the merged ROB-978 H1 feature plane
and the H3 global generators.  It stops at immutable generator evidence: no
engine, funding gate, order/fill simulation, runtime service, or corpus write
is reachable from the frozen-fold command.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path

import canonical_hash
import rob941_frozen_scope as frozen
import rob941_gaps as gaps
import rob941_kline_schema as schema
import rob941_offline_loader as loader
import rob941_persistence as persistence
from funding_oi_archive import FundingRow
from rob941_manifest import (
    ArchiveProvenance,
    CorpusManifest,
    SymbolEligibility,
    SymbolFundingManifest,
    SymbolKlineManifest,
)
from rob944_folds import generate_frozen_fold_schedule
from rob974_features import (
    FOUR_HOUR_MS,
    MINUTE_MS,
    MinuteBar,
    build_complete_4h,
    symbol_features,
    synchronized_features,
)
from rob974_h3_evidence import (
    UniqueGeneratorEvidence,
    build_unique_generator_evidence,
)
from rob974_h3_manifest import (
    RESEARCH_DOCUMENT_SHA256,
    S3_GENERATOR_REJECTION_TAXONOMY,
    S3_NO_SIGNAL_TAXONOMY,
    S3_STRATEGY_CONTRACT,
    S4_GENERATOR_REJECTION_TAXONOMY,
    S4_NO_SIGNAL_TAXONOMY,
    S4_STRATEGY_CONTRACT,
    SYMBOLS,
    S3Config,
    S4Config,
    get_config,
)
from rob974_h3_s3 import EmitWindow, FeatureContext, S3Candidate, generate_s3_global
from rob974_h3_s4 import S4Candidate, generate_s4_global
from rob974_lineage import DerivedManifest

_SMOKE_4H_BARS = 202
_SMOKE_MINUTES = _SMOKE_4H_BARS * 240
_GAP_BAR_INDEX = 10
_GAP_MINUTE_OFFSET = 100
_S3_SMOKE_CONFIG_ID = "S3-05"
_S4_SMOKE_CONFIG_ID = "S4-01"
_SMOKE_WINDOW_ID = "rob941-shaped-persisted-synthetic"

_WORKTREE = Path("/Users/mgh3326/work/auto_trader.rob-980")
_UV = Path("/Users/mgh3326/.local/bin/uv")
_FROZEN_MANIFEST = (
    _WORKTREE
    / "research/nautilus_scalping/data_manifests/rob941_corpus_manifest.v1.json"
)
_FROZEN_DATA_ROOT = _WORKTREE / "research/nautilus_scalping/data"
_THIS_FILE = _WORKTREE / "research/nautilus_scalping/rob974_h3_smoke.py"


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
    raise TypeError(f"unsupported smoke payload type {type(value).__name__}")


@dataclass(frozen=True, slots=True)
class StrategySmokeResult:
    strategy: str
    config_id: str
    evidence: UniqueGeneratorEvidence
    accepted_payloads: tuple[S3Candidate | S4Candidate, ...]

    def __post_init__(self) -> None:
        if self.strategy not in ("S3", "S4"):
            raise ValueError("smoke strategy must be S3 or S4")
        if type(self.config_id) is not str:
            raise TypeError("smoke config_id must be built-in str")
        if type(self.evidence) is not UniqueGeneratorEvidence:
            raise TypeError("smoke evidence must be exact UniqueGeneratorEvidence")
        if (
            self.evidence.strategy != self.strategy
            or self.evidence.config_id != self.config_id
        ):
            raise ValueError("smoke strategy/evidence mismatch")
        expected = S3Candidate if self.strategy == "S3" else S4Candidate
        if type(self.accepted_payloads) is not tuple or any(
            type(candidate) is not expected for candidate in self.accepted_payloads
        ):
            raise TypeError("smoke accepted payload type mismatch")
        if len(self.accepted_payloads) != self.evidence.generator_accepted:
            raise ValueError("smoke accepted payload count mismatch")

    def to_payload(self) -> dict[str, object]:
        evidence = self.evidence
        return {
            "config_id": self.config_id,
            "global_invocation_count": evidence.global_invocation_count,
            "evaluated_decision_units": evidence.evaluated_decision_units,
            "no_signal": evidence.no_signal,
            "candidate": evidence.candidate,
            "generator_rejected": evidence.generator_rejected,
            "generator_accepted": evidence.generator_accepted,
            "outcome_histogram": dict(evidence.outcome_histogram),
            "no_signal_reason_histogram": dict(evidence.no_signal_reason_histogram),
            "generator_rejection_reason_histogram": dict(
                evidence.generator_rejection_reason_histogram
            ),
            "candidate_side_histogram": dict(evidence.candidate_side_histogram),
            "unique_evidence_hash": evidence.content_hash,
            "accepted_payloads": _plain(self.accepted_payloads),
        }


@dataclass(frozen=True, slots=True)
class GeneratorSmokeResult:
    generator_smoke: str
    actual_h2_engine_integration: str
    persisted_root: str
    strategies: tuple[StrategySmokeResult, StrategySmokeResult]
    gap_symbol: str
    gap_close: int
    recovery_close: int
    gap_close_absent: bool
    other_symbol_gap_close_present: bool
    recovery_close_present: bool
    mapping_permutation_hashes_match: bool
    feature_hash: str
    lineage_hash: str
    content_hash: str

    def __post_init__(self) -> None:
        if self.generator_smoke != "PASS":
            raise ValueError("generator smoke must be PASS")
        if self.actual_h2_engine_integration != "NOT_EVALUATED":
            raise ValueError("H2 integration must remain explicitly unevaluated")
        if not Path(self.persisted_root).is_absolute():
            raise ValueError("persisted smoke root must be absolute")
        if tuple(item.strategy for item in self.strategies) != ("S3", "S4"):
            raise ValueError("smoke strategies must use frozen S3/S4 order")
        if self.gap_symbol != "SOLUSDT":
            raise ValueError("localized smoke gap symbol drift")
        for name in ("gap_close", "recovery_close"):
            if type(getattr(self, name)) is not int:
                raise TypeError(f"{name} must be built-in int")
        for name in (
            "gap_close_absent",
            "other_symbol_gap_close_present",
            "recovery_close_present",
            "mapping_permutation_hashes_match",
        ):
            if type(getattr(self, name)) is not bool:
                raise TypeError(f"{name} must be bool")
        for name in ("feature_hash", "lineage_hash", "content_hash"):
            value = getattr(self, name)
            if (
                type(value) is not str
                or len(value) != 64
                or any(char not in "0123456789abcdef" for char in value)
            ):
                raise ValueError(f"{name} must be lowercase SHA-256")

    def to_payload(self, *, include_root: bool = True) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": "rob974-h3-generator-smoke-v1",
            "generator_smoke": self.generator_smoke,
            "actual_h2_engine_integration": self.actual_h2_engine_integration,
            "authorities": {
                "research_sha256": RESEARCH_DOCUMENT_SHA256,
                "s3_contract_hash": S3_STRATEGY_CONTRACT.contract_hash,
                "s4_contract_hash": S4_STRATEGY_CONTRACT.contract_hash,
            },
            "window": {
                "fold_or_full_window": _SMOKE_WINDOW_ID,
                "phase": "offline_smoke",
            },
            "universe": list(SYMBOLS),
            "gap": {
                "symbol": self.gap_symbol,
                "close_ts": self.gap_close,
                "recovery_close_ts": self.recovery_close,
                "gap_close_absent": self.gap_close_absent,
                "other_symbol_gap_close_present": self.other_symbol_gap_close_present,
                "recovery_close_present": self.recovery_close_present,
            },
            "mapping_permutation_hashes_match": (self.mapping_permutation_hashes_match),
            "feature_hash": self.feature_hash,
            "lineage_hash": self.lineage_hash,
            "content_hash": self.content_hash,
            "strategies": {
                item.strategy: item.to_payload() for item in self.strategies
            },
        }
        if include_root:
            payload["persisted_root"] = self.persisted_root
        return payload

    def to_json_bytes(self) -> bytes:
        return (
            json.dumps(
                self.to_payload(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")


def _archive(root: Path, symbol: str, kind: str) -> ArchiveProvenance:
    data = f"ROB980 persisted synthetic {symbol} {kind}".encode()
    local = persistence.write_raw_archive(root, symbol, kind, 2025, 7, data)
    digest = hashlib.sha256(data).hexdigest()
    return ArchiveProvenance(
        "https://example.invalid/" + local,
        "https://example.invalid/checksum",
        digest,
        local,
    )


def _market_returns() -> tuple[float, ...]:
    daily = (0.018, -0.016, 0.014, -0.012, 0.010, -0.014)
    scales = (0.72, 0.86, 1.00, 1.14, 1.28, 1.42)
    values = [0.0]
    for index in range(1, _SMOKE_4H_BARS):
        values.append(daily[index % 6] * scales[(index // 6) % len(scales)])
    states = _residual_cycle()
    residual_returns = tuple(states[index] - states[index - 1] for index in range(60))
    cycle = [
        (0.024, -0.018, 0.020, -0.016, 0.022, -0.014)[index % 6] for index in range(48)
    ] + [
        0.035,
        -0.008,
        0.034,
        -0.007,
        0.033,
        -0.006,
        0.032,
        -0.005,
        0.030,
        -0.050,
        0.020,
        0.018,
    ]
    projection = math.fsum(
        residual * market
        for residual, market in zip(residual_returns, cycle, strict=True)
    ) / math.fsum(residual_returns[index] ** 2 for index in range(48))
    for index in range(48):
        cycle[index] -= projection * residual_returns[index]
    values[80] = cycle[58]
    values[81] = cycle[59]
    values[82:142] = cycle
    values[142:202] = cycle
    return tuple(values)


def _residual_cycle() -> tuple[float, ...]:
    values = [
        0.008 * math.sin(2.0 * math.pi * float(index) / 12.0) for index in range(60)
    ]
    values[-6:] = [0.025, 0.035, 0.040, 0.060, 0.050, 0.040]
    return tuple(values)


def _residual_states() -> tuple[float, ...]:
    innovations = (0.0024, -0.0016, 0.0011, -0.0020, 0.0018, -0.0012)
    values = [0.0]
    for index in range(1, _SMOKE_4H_BARS):
        values.append(0.82 * values[-1] + innovations[index % len(innovations)])
    cycle = _residual_cycle()
    values[80] = cycle[58]
    values[81] = cycle[59]
    values[82:142] = cycle
    values[142:202] = cycle
    return tuple(values)


def _target_closes(symbol: str) -> tuple[float, ...]:
    bases = {
        "BTCUSDT": 60_000.0,
        "XRPUSDT": 0.50,
        "DOGEUSDT": 0.20,
        "SOLUSDT": 150.0,
    }
    residual_loadings = {
        "BTCUSDT": 0.0,
        "XRPUSDT": 1.0,
        "DOGEUSDT": 0.0,
        "SOLUSDT": -1.0,
    }
    market_level = 0.0
    closes: list[float] = []
    for market_return, residual in zip(
        _market_returns(), _residual_states(), strict=True
    ):
        market_level += market_return
        closes.append(
            bases[symbol]
            * math.exp(market_level + residual_loadings[symbol] * residual)
        )
    return tuple(closes)


def _normalized_rows(
    symbol: str, symbol_index: int
) -> tuple[schema.NormalizedKline, ...]:
    targets = _target_closes(symbol)
    previous = targets[0] / math.exp(_market_returns()[0])
    rows: list[schema.NormalizedKline] = []
    for bar_index, target in enumerate(targets):
        log_delta = math.log(target / previous)
        wick = (
            0.040
            if (bar_index <= 180 and bar_index % 12 == 0) or bar_index == 190
            else (
                0.014
                if bar_index == 201
                else 0.0018 + 0.00035 * float((bar_index * 7) % 5)
            )
        )
        prior_minute_close = previous
        for minute in range(240):
            close = previous * math.exp(log_delta * float(minute + 1) / 240.0)
            open_value = prior_minute_close
            high = max(open_value, close) * (1.0 + wick)
            low = min(open_value, close) * (1.0 - wick)
            volume = 1.0 + float(symbol_index) / 10.0
            open_time = frozen.WINDOW_START_MS + (bar_index * 240 + minute) * MINUTE_MS
            rows.append(
                schema.NormalizedKline(
                    symbol,
                    open_time,
                    open_value,
                    high,
                    low,
                    close,
                    volume,
                    open_time + MINUTE_MS - 1,
                    close * volume,
                    1,
                    0.0,
                    0.0,
                )
            )
            prior_minute_close = close
        previous = target
    if symbol == "SOLUSDT":
        missing = _GAP_BAR_INDEX * 240 + _GAP_MINUTE_OFFSET
        rows.pop(missing)
    return tuple(rows)


def _persist_synthetic_corpus(root: Path) -> CorpusManifest:
    klines: list[SymbolKlineManifest] = []
    funding: list[SymbolFundingManifest] = []
    for symbol_index, symbol in enumerate(frozen.UNIVERSE):
        source = _normalized_rows(symbol, symbol_index)
        relative, physical = persistence.write_kline_shard(root, symbol, source)
        klines.append(
            SymbolKlineManifest(
                symbol,
                "1m",
                (_archive(root, symbol, "klines"),),
                canonical_hash.canonical_sha256([row.__dict__ for row in source]),
                len(source),
                source[0].open_time_ms,
                source[-1].open_time_ms,
                tuple(
                    gaps.detect_gap_ranges(
                        [row.open_time_ms for row in source],
                        frozen.WINDOW_START_MS,
                        frozen.WINDOW_END_MS,
                    )
                ),
                shard_path=relative,
                shard_file_sha256=physical,
            )
        )
        funding_rows = (FundingRow(frozen.WINDOW_START_MS, 8, 0.0),)
        funding_relative, funding_physical = persistence.write_funding_shard(
            root, symbol, funding_rows
        )
        funding.append(
            SymbolFundingManifest(
                symbol,
                (_archive(root, symbol, "fundingRate"),),
                canonical_hash.canonical_sha256([row.__dict__ for row in funding_rows]),
                len(funding_rows),
                funding_rows[0].calc_time,
                funding_rows[-1].calc_time,
                shard_path=funding_relative,
                shard_file_sha256=funding_physical,
            )
        )
    manifest = CorpusManifest(
        frozen.WINDOW_START_ISO,
        frozen.WINDOW_END_ISO,
        frozen.UNIVERSE,
        tuple(
            SymbolEligibility(symbol, **frozen.eligibility(symbol))
            for symbol in frozen.UNIVERSE
        ),
        tuple(klines),
        tuple(funding),
    )
    manifest.save(root / "rob980-smoke-manifest.json")
    return manifest


def _selected_minutes(loaded: dict[str, object]) -> dict[str, tuple[MinuteBar, ...]]:
    klines = loaded["klines"]
    if type(klines) is not dict:
        raise TypeError("offline loader kline result must be a dict")
    return {
        symbol: tuple(
            MinuteBar(
                row.open_time_ms,
                row.open,
                row.high,
                row.low,
                row.close,
                row.base_volume,
            )
            for row in klines[symbol]
        )
        for symbol in SYMBOLS
    }


def _feature_hash(snapshots: tuple[object, ...]) -> str:
    return canonical_hash.canonical_sha256(
        [
            {
                **snapshot.__dict__,
                "features": [feature.__dict__ for feature in snapshot.features],
            }
            for snapshot in snapshots
        ]
    )


def _context(rows: dict[str, tuple[MinuteBar, ...]]) -> tuple[FeatureContext, str]:
    snapshots = synchronized_features(rows)
    bars = {symbol: build_complete_4h(rows[symbol]) for symbol in SYMBOLS}
    return FeatureContext.from_h1(bars, snapshots), _feature_hash(snapshots)


def _run_generators(
    context: FeatureContext, emit_window: EmitWindow
) -> tuple[
    StrategySmokeResult,
    StrategySmokeResult,
]:
    s3_config = get_config(_S3_SMOKE_CONFIG_ID)
    s4_config = get_config(_S4_SMOKE_CONFIG_ID)
    if type(s3_config) is not S3Config or type(s4_config) is not S4Config:
        raise AssertionError("registered smoke config strategy drift")
    s3_output = generate_s3_global(context, emit_window, s3_config)
    s4_output = generate_s4_global(context, emit_window, s4_config)
    s3_evidence = build_unique_generator_evidence(
        s3_output,
        fold_or_full_window=_SMOKE_WINDOW_ID,
        phase="offline_smoke",
    )
    s4_evidence = build_unique_generator_evidence(
        s4_output,
        fold_or_full_window=_SMOKE_WINDOW_ID,
        phase="offline_smoke",
    )
    return (
        StrategySmokeResult("S3", s3_config.config_id, s3_evidence, s3_output.accepted),
        StrategySmokeResult("S4", s4_config.config_id, s4_evidence, s4_output.accepted),
    )


def _smoke_emit_window() -> EmitWindow:
    return EmitWindow(
        frozen.WINDOW_START_MS + (_GAP_BAR_INDEX + 1) * FOUR_HOUR_MS,
        frozen.WINDOW_START_MS + 203 * FOUR_HOUR_MS,
    )


def _result_content_payload(
    strategies: tuple[StrategySmokeResult, StrategySmokeResult],
    *,
    feature_hash: str,
    lineage_hash: str,
    gap_close: int,
    recovery_close: int,
    gap_close_absent: bool,
    other_symbol_gap_close_present: bool,
    recovery_close_present: bool,
) -> dict[str, object]:
    return {
        "schema_version": "rob974-h3-generator-smoke-v1",
        "research_sha256": RESEARCH_DOCUMENT_SHA256,
        "strategy_contract_hashes": (
            S3_STRATEGY_CONTRACT.contract_hash,
            S4_STRATEGY_CONTRACT.contract_hash,
        ),
        "window": _SMOKE_WINDOW_ID,
        "phase": "offline_smoke",
        "universe": SYMBOLS,
        "feature_hash": feature_hash,
        "lineage_hash": lineage_hash,
        "gap": (
            "SOLUSDT",
            gap_close,
            recovery_close,
            gap_close_absent,
            other_symbol_gap_close_present,
            recovery_close_present,
        ),
        "strategy_evidence_hashes": tuple(
            (item.strategy, item.evidence.content_hash) for item in strategies
        ),
    }


def run_persisted_generator_smoke(root: Path) -> GeneratorSmokeResult:
    """Run the authorized local synthetic persistence/H1/H3 generator chain."""
    if not isinstance(root, Path):
        raise TypeError("smoke root must be pathlib.Path")
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    manifest = _persist_synthetic_corpus(root)
    manifest_path = root / "rob980-smoke-manifest.json"
    loaded = loader.load_corpus(CorpusManifest.load(manifest_path), root)
    if manifest.content_hash() != CorpusManifest.load(manifest_path).content_hash():
        raise ValueError("persisted synthetic manifest content changed on reload")
    selected = _selected_minutes(loaded)
    context, feature_hash = _context(selected)
    permuted_rows = {symbol: selected[symbol] for symbol in reversed(SYMBOLS)}
    permuted_context, permuted_feature_hash = _context(permuted_rows)

    emit_window = _smoke_emit_window()
    strategies = _run_generators(context, emit_window)
    permuted_strategies = _run_generators(permuted_context, emit_window)
    mapping_permutation_hashes_match = feature_hash == permuted_feature_hash and tuple(
        item.evidence.content_hash for item in strategies
    ) == tuple(item.evidence.content_hash for item in permuted_strategies)

    funding = loaded["funding"]
    if type(funding) is not dict:
        raise TypeError("offline loader funding result must be a dict")
    funding_coverage = tuple(
        (
            symbol,
            len(funding[symbol]),
            funding[symbol][0].calc_time,
            funding[symbol][-1].calc_time,
        )
        for symbol in SYMBOLS
    )
    derived = DerivedManifest.create(
        rows=selected,
        context_start=frozen.WINDOW_START_MS,
        context_end=frozen.WINDOW_START_MS + _SMOKE_MINUTES * MINUTE_MS,
        funding_coverage=funding_coverage,
        funding_source_sha256=canonical_hash.canonical_sha256(
            {symbol: [row.__dict__ for row in funding[symbol]] for symbol in SYMBOLS}
        ),
        feature_hash=feature_hash,
    )

    gap_close = frozen.WINDOW_START_MS + (_GAP_BAR_INDEX + 1) * FOUR_HOUR_MS
    recovery_close = gap_close + 7 * FOUR_HOUR_MS
    synchronized_closes = {snapshot.decision_ts for snapshot in context.snapshots}
    gap_close_absent = gap_close not in synchronized_closes
    other_symbol_gap_close_present = any(
        feature.decision_ts == gap_close
        for feature in symbol_features("XRPUSDT", selected["XRPUSDT"])
    )
    recovery_close_present = recovery_close in synchronized_closes
    content_hash = canonical_hash.canonical_sha256(
        _result_content_payload(
            strategies,
            feature_hash=feature_hash,
            lineage_hash=derived.hash,
            gap_close=gap_close,
            recovery_close=recovery_close,
            gap_close_absent=gap_close_absent,
            other_symbol_gap_close_present=other_symbol_gap_close_present,
            recovery_close_present=recovery_close_present,
        )
    )
    return GeneratorSmokeResult(
        "PASS",
        "NOT_EVALUATED",
        str(root),
        strategies,
        "SOLUSDT",
        gap_close,
        recovery_close,
        gap_close_absent,
        other_symbol_gap_close_present,
        recovery_close_present,
        mapping_permutation_hashes_match,
        feature_hash,
        derived.hash,
        content_hash,
    )


def deterministic_hash_probe() -> dict[str, str]:
    """Pure synthetic H1/H3 probe used to compare interpreter hash seeds."""
    selected = {
        symbol: tuple(
            MinuteBar(
                row.open_time_ms,
                row.open,
                row.high,
                row.low,
                row.close,
                row.base_volume,
            )
            for row in _normalized_rows(symbol, symbol_index)
        )
        for symbol_index, symbol in enumerate(SYMBOLS, start=1)
    }
    context, feature_hash = _context(selected)
    permuted, permuted_feature_hash = _context(
        {symbol: selected[symbol] for symbol in reversed(SYMBOLS)}
    )
    emit_window = _smoke_emit_window()
    strategies = _run_generators(context, emit_window)
    permuted_strategies = _run_generators(permuted, emit_window)
    hashes = tuple(item.evidence.content_hash for item in strategies)
    if feature_hash != permuted_feature_hash or hashes != tuple(
        item.evidence.content_hash for item in permuted_strategies
    ):
        raise ValueError("synthetic H1/H3 hash probe changed under mapping order")
    return {"feature_hash": feature_hash, "S3": hashes[0], "S4": hashes[1]}


def _closed_histogram_schema(keys: tuple[str, ...]) -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(keys),
        "properties": {key: {"type": "integer", "minimum": 0} for key in keys},
    }


def _fold_strategy_schema(strategy: str) -> dict[str, object]:
    no_signal_keys = (
        S3_NO_SIGNAL_TAXONOMY if strategy == "S3" else S4_NO_SIGNAL_TAXONOMY
    )
    rejection_keys = (
        S3_GENERATOR_REJECTION_TAXONOMY
        if strategy == "S3"
        else S4_GENERATOR_REJECTION_TAXONOMY
    )
    required = [
        "config_id",
        "evaluated_decision_units",
        "no_signal",
        "candidate",
        "generator_rejected",
        "generator_accepted",
        "no_signal_reason_histogram",
        "generator_rejection_reason_histogram",
        "unique_evidence_hash",
    ]
    return {
        "type": "object",
        "additionalProperties": False,
        "required": required,
        "properties": {
            "config_id": {"const": f"{strategy}-00"},
            **{key: {"type": "integer", "minimum": 0} for key in required[1:6]},
            "no_signal_reason_histogram": _closed_histogram_schema(no_signal_keys),
            "generator_rejection_reason_histogram": _closed_histogram_schema(
                rejection_keys
            ),
            "unique_evidence_hash": {
                "type": "string",
                "pattern": "^[0-9a-f]{64}$",
            },
        },
    }


_SHA_SCHEMA = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
_FOLD00_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "ROB-974 H3 bounded fold-00 pure-generator evidence",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "authorities",
        "window",
        "universe",
        "phase",
        "strategies",
    ],
    "properties": {
        "authorities": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "research_sha256",
                "parent_manifest_physical_sha256",
                "parent_manifest_content_hash",
                "feature_hash",
                "strategy_contract_hashes",
            ],
            "properties": {
                "research_sha256": _SHA_SCHEMA,
                "parent_manifest_physical_sha256": _SHA_SCHEMA,
                "parent_manifest_content_hash": _SHA_SCHEMA,
                "feature_hash": _SHA_SCHEMA,
                "strategy_contract_hashes": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["S3", "S4"],
                    "properties": {"S3": _SHA_SCHEMA, "S4": _SHA_SCHEMA},
                },
            },
        },
        "window": {
            "type": "object",
            "additionalProperties": False,
            "required": ["fold_id", "oos_start_ms", "oos_end_ms", "half_open"],
            "properties": {
                "fold_id": {"const": "fold-00"},
                "oos_start_ms": {"type": "integer", "minimum": 0},
                "oos_end_ms": {"type": "integer", "minimum": 0},
                "half_open": {"const": True},
            },
        },
        "universe": {
            "type": "array",
            "minItems": 3,
            "maxItems": 3,
            "prefixItems": [
                {"const": "XRPUSDT"},
                {"const": "DOGEUSDT"},
                {"const": "SOLUSDT"},
            ],
            "items": False,
        },
        "phase": {"const": "selected_oos"},
        "strategies": {
            "type": "object",
            "required": ["S3", "S4"],
            "additionalProperties": False,
            "properties": {
                strategy: _fold_strategy_schema(strategy) for strategy in ("S3", "S4")
            },
        },
    },
}


def prepared_fold00_packet() -> dict[str, str]:
    command = (
        "env -i PATH=/Users/mgh3326/.local/bin:/usr/bin:/bin "
        "PYTHONPATH=/Users/mgh3326/work/auto_trader.rob-980/"
        "research/nautilus_scalping "
        f"{_UV} run python {_THIS_FILE} "
        f"--manifest {_FROZEN_MANIFEST} "
        f"--data-root {_FROZEN_DATA_ROOT} "
        "--fold-id fold-00 --phase selected_oos"
    )
    return {
        "status": "NOT_EXECUTED_AWAITING_ORCH_GO",
        "command": command,
        "json_schema": json.dumps(
            _FOLD00_SCHEMA, sort_keys=True, separators=(",", ":")
        ),
    }


def _frozen_fold_packet(
    manifest_path: Path,
    data_root: Path,
    fold_id: str,
    phase: str,
) -> dict[str, object]:
    if fold_id != "fold-00" or phase != "selected_oos":
        raise ValueError("only the frozen fold-00 selected_oos smoke is permitted")
    physical_manifest_hash = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    manifest = CorpusManifest.load(manifest_path)
    loaded = loader.load_corpus(manifest, data_root)
    selected = _selected_minutes(loaded)
    context, feature_hash = _context(selected)
    fold = generate_frozen_fold_schedule(frozen.WINDOW_START_MS, frozen.WINDOW_END_MS)[
        0
    ]
    emit_window = EmitWindow(fold.oos_start_ms, fold.oos_end_ms)
    s3_config = get_config("S3-00")
    s4_config = get_config("S4-00")
    if type(s3_config) is not S3Config or type(s4_config) is not S4Config:
        raise AssertionError("baseline config strategy drift")
    outputs = (
        generate_s3_global(context, emit_window, s3_config),
        generate_s4_global(context, emit_window, s4_config),
    )
    evidence = tuple(
        build_unique_generator_evidence(
            output, fold_or_full_window=fold_id, phase=phase
        )
        for output in outputs
    )
    return {
        "authorities": {
            "research_sha256": RESEARCH_DOCUMENT_SHA256,
            "parent_manifest_physical_sha256": physical_manifest_hash,
            "parent_manifest_content_hash": manifest.content_hash(),
            "feature_hash": feature_hash,
            "strategy_contract_hashes": {
                "S3": S3_STRATEGY_CONTRACT.contract_hash,
                "S4": S4_STRATEGY_CONTRACT.contract_hash,
            },
        },
        "window": {
            "fold_id": fold.fold_id,
            "oos_start_ms": fold.oos_start_ms,
            "oos_end_ms": fold.oos_end_ms,
            "half_open": True,
        },
        "universe": list(SYMBOLS),
        "phase": phase,
        "strategies": {
            item.strategy: {
                "config_id": item.config_id,
                "evaluated_decision_units": item.evaluated_decision_units,
                "no_signal": item.no_signal,
                "candidate": item.candidate,
                "generator_rejected": item.generator_rejected,
                "generator_accepted": item.generator_accepted,
                "no_signal_reason_histogram": dict(item.no_signal_reason_histogram),
                "generator_rejection_reason_histogram": dict(
                    item.generator_rejection_reason_histogram
                ),
                "unique_evidence_hash": item.content_hash,
            }
            for item in evidence
        },
    }


def _main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--fold-id", required=True)
    parser.add_argument("--phase", required=True)
    arguments = parser.parse_args()
    packet = _frozen_fold_packet(
        arguments.manifest.resolve(),
        arguments.data_root.resolve(),
        arguments.fold_id,
        arguments.phase,
    )
    print(json.dumps(packet, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    _main()


__all__ = [
    "GeneratorSmokeResult",
    "StrategySmokeResult",
    "deterministic_hash_probe",
    "prepared_fold00_packet",
    "run_persisted_generator_smoke",
]

"""ROB-974 H3 scenario-independent unique generator evidence.

One evidence object consumes one whole global S3 or S4 invocation.  Per-unit
callbacks, cost scenarios, funding gates, phase horizons, and engine state are
outside this boundary.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass

from rob974_h3_manifest import (
    PAIRS,
    S3_STRATEGY_CONTRACT,
    S4_STRATEGY_CONTRACT,
    SYMBOLS,
    S3Config,
    S4Config,
    get_config,
)
from rob974_h3_manifest import (
    S3_GENERATOR_REJECTION_TAXONOMY as _S3_GENERATOR_REJECTION_TAXONOMY,
)
from rob974_h3_manifest import (
    S3_NO_SIGNAL_TAXONOMY as _S3_NO_SIGNAL_TAXONOMY,
)
from rob974_h3_manifest import (
    S4_GENERATOR_REJECTION_TAXONOMY as _S4_GENERATOR_REJECTION_TAXONOMY,
)
from rob974_h3_manifest import (
    S4_NO_SIGNAL_TAXONOMY as _S4_NO_SIGNAL_TAXONOMY,
)
from rob974_h3_s3 import S3Candidate, S3GeneratorOutput
from rob974_h3_s4 import S4Candidate, S4GeneratorOutput

from research_contracts.canonical_hash import canonical_sha256

PHASES: tuple[str, ...] = (
    "train",
    "selected_oos",
    "pbo_full_window",
    "offline_smoke",
)
S3_NO_SIGNAL_TAXONOMY: tuple[str, ...] = _S3_NO_SIGNAL_TAXONOMY
S4_NO_SIGNAL_TAXONOMY: tuple[str, ...] = _S4_NO_SIGNAL_TAXONOMY
S3_GENERATOR_REJECTION_TAXONOMY: tuple[str, ...] = _S3_GENERATOR_REJECTION_TAXONOMY
S4_GENERATOR_REJECTION_TAXONOMY: tuple[str, ...] = _S4_GENERATOR_REJECTION_TAXONOMY


def _str(value: object, name: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{name} must be built-in str")
    return value


def _int(value: object, name: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{name} must be built-in int")
    return value


def _sha(value: object, name: str) -> str:
    text = _str(value, name)
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise ValueError(f"{name} must be lowercase SHA-256")
    return text


@dataclass(frozen=True, slots=True)
class GeneratorIdentity:
    strategy: str
    config_id: str
    fold_or_full_window: str
    phase: str
    decision_ts: int
    symbol_or_pair: str
    side: str

    def __post_init__(self) -> None:
        if _str(self.strategy, "strategy") not in ("S3", "S4"):
            raise ValueError("strategy must be S3 or S4")
        config = get_config(_str(self.config_id, "config_id"))
        expected_type = S3Config if self.strategy == "S3" else S4Config
        if (
            not self.config_id.startswith(f"{self.strategy}-")
            or type(config) is not expected_type
        ):
            raise ValueError("identity strategy/config mismatch")
        if not _str(self.fold_or_full_window, "fold_or_full_window"):
            raise ValueError("fold_or_full_window must not be empty")
        if _str(self.phase, "phase") not in PHASES:
            raise ValueError("phase outside the closed generator phase set")
        _int(self.decision_ts, "decision_ts")
        if self.strategy == "S3":
            if self.symbol_or_pair not in SYMBOLS or self.side not in ("long", "short"):
                raise ValueError("invalid S3 candidate identity")
        elif self.symbol_or_pair not in PAIRS or self.side not in (
            "short_a_long_b",
            "long_a_short_b",
        ):
            raise ValueError("invalid S4 candidate identity")

    def as_tuple(self) -> tuple[str, str, str, str, int, str, str]:
        return (
            self.strategy,
            self.config_id,
            self.fold_or_full_window,
            self.phase,
            self.decision_ts,
            self.symbol_or_pair,
            self.side,
        )


def _histogram_keys(strategy: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if strategy == "S3":
        return S3_NO_SIGNAL_TAXONOMY, S3_GENERATOR_REJECTION_TAXONOMY
    return S4_NO_SIGNAL_TAXONOMY, S4_GENERATOR_REJECTION_TAXONOMY


def _side_keys(strategy: str) -> tuple[str, ...]:
    return (
        ("long", "short") if strategy == "S3" else ("short_a_long_b", "long_a_short_b")
    )


@dataclass(frozen=True, slots=True)
class UniqueGeneratorEvidence:
    schema_version: str
    strategy: str
    config_id: str
    strategy_contract_hash: str
    fold_or_full_window: str
    phase: str
    global_invocation_count: int
    evaluated_decision_units: int
    no_signal: int
    candidate: int
    generator_rejected: int
    generator_accepted: int
    outcome_histogram: tuple[tuple[str, int], ...]
    no_signal_reason_histogram: tuple[tuple[str, int], ...]
    generator_rejection_reason_histogram: tuple[tuple[str, int], ...]
    candidate_side_histogram: tuple[tuple[str, int], ...]
    accepted_identities: tuple[GeneratorIdentity, ...]
    rejected_identities: tuple[GeneratorIdentity, ...]
    candidate_payload_hashes: tuple[tuple[GeneratorIdentity, str], ...]

    def __post_init__(self) -> None:
        if (
            _str(self.schema_version, "schema_version")
            != "rob974-h3-generator-evidence-v1"
        ):
            raise ValueError("unique evidence schema drift")
        if _str(self.strategy, "strategy") not in ("S3", "S4"):
            raise ValueError("unique evidence strategy must be S3 or S4")
        _str(self.config_id, "config_id")
        _sha(self.strategy_contract_hash, "strategy_contract_hash")
        expected_contract = (
            S3_STRATEGY_CONTRACT.contract_hash
            if self.strategy == "S3"
            else S4_STRATEGY_CONTRACT.contract_hash
        )
        if self.strategy_contract_hash != expected_contract:
            raise ValueError("unique evidence strategy contract mismatch")
        if not _str(self.fold_or_full_window, "fold_or_full_window"):
            raise ValueError("fold_or_full_window must not be empty")
        if _str(self.phase, "phase") not in PHASES:
            raise ValueError("phase outside closed set")
        for name in (
            "global_invocation_count",
            "evaluated_decision_units",
            "no_signal",
            "candidate",
            "generator_rejected",
            "generator_accepted",
        ):
            value = _int(getattr(self, name), name)
            if value < 0:
                raise ValueError(f"{name} must not be negative")
        if self.global_invocation_count != 1:
            raise ValueError("evidence must represent exactly one global invocation")
        if self.evaluated_decision_units != self.no_signal + self.candidate:
            raise ValueError("evaluated_decision_units equation failed")
        if self.candidate != self.generator_rejected + self.generator_accepted:
            raise ValueError("candidate equation failed")
        expected_outcomes = (
            ("no_signal", self.no_signal),
            ("candidate", self.candidate),
            ("generator_rejected", self.generator_rejected),
            ("generator_accepted", self.generator_accepted),
        )
        if self.outcome_histogram != expected_outcomes:
            raise ValueError("closed outcome histogram mismatch")
        no_signal_keys, rejection_keys = _histogram_keys(self.strategy)
        if tuple(key for key, _ in self.no_signal_reason_histogram) != no_signal_keys:
            raise ValueError("closed no-signal histogram keys/order mismatch")
        if tuple(key for key, _ in self.generator_rejection_reason_histogram) != (
            rejection_keys
        ):
            raise ValueError("closed rejection histogram keys/order mismatch")
        for histogram in (
            self.outcome_histogram,
            self.no_signal_reason_histogram,
            self.generator_rejection_reason_histogram,
            self.candidate_side_histogram,
        ):
            if type(histogram) is not tuple or any(
                type(item) is not tuple
                or len(item) != 2
                or type(item[0]) is not str
                or type(item[1]) is not int
                or item[1] < 0
                for item in histogram
            ):
                raise TypeError("histograms must use exact ordered tuple entries")
        if sum(value for _, value in self.no_signal_reason_histogram) != self.no_signal:
            raise ValueError("no-signal histogram subtotal mismatch")
        if (
            sum(value for _, value in self.generator_rejection_reason_histogram)
            != self.generator_rejected
        ):
            raise ValueError("generator-rejection histogram subtotal mismatch")
        if (
            tuple(key for key, _ in self.candidate_side_histogram)
            != _side_keys(self.strategy)
            or sum(value for _, value in self.candidate_side_histogram)
            != self.candidate
        ):
            raise ValueError("candidate-side histogram mismatch")
        for values, expected_count in (
            (self.accepted_identities, self.generator_accepted),
            (self.rejected_identities, self.generator_rejected),
        ):
            if type(values) is not tuple or any(
                type(identity) is not GeneratorIdentity for identity in values
            ):
                raise TypeError("identity sets must be exact tuples")
            if len(values) != expected_count or len(values) != len(set(values)):
                raise ValueError("identity count/uniqueness mismatch")
        if set(self.accepted_identities) & set(self.rejected_identities):
            raise ValueError("accepted/rejected identity collision")
        all_identities = set(self.accepted_identities) | set(self.rejected_identities)
        if (
            type(self.candidate_payload_hashes) is not tuple
            or len(self.candidate_payload_hashes) != self.candidate
        ):
            raise ValueError("candidate payload-hash count mismatch")
        payload_ids: list[GeneratorIdentity] = []
        for identity, payload_hash in self.candidate_payload_hashes:
            if type(identity) is not GeneratorIdentity:
                raise TypeError("payload hash identity must be exact GeneratorIdentity")
            _sha(payload_hash, "candidate_payload_hash")
            payload_ids.append(identity)
        if set(payload_ids) != all_identities or len(payload_ids) != len(
            set(payload_ids)
        ):
            raise ValueError("candidate payload identities mismatch")

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "strategy": self.strategy,
            "config_id": self.config_id,
            "strategy_contract_hash": self.strategy_contract_hash,
            "fold_or_full_window": self.fold_or_full_window,
            "phase": self.phase,
            "global_invocation_count": self.global_invocation_count,
            "evaluated_decision_units": self.evaluated_decision_units,
            "no_signal": self.no_signal,
            "candidate": self.candidate,
            "generator_rejected": self.generator_rejected,
            "generator_accepted": self.generator_accepted,
            "outcome_histogram": self.outcome_histogram,
            "no_signal_reason_histogram": self.no_signal_reason_histogram,
            "generator_rejection_reason_histogram": self.generator_rejection_reason_histogram,
            "candidate_side_histogram": self.candidate_side_histogram,
            "accepted_identities": tuple(
                identity.as_tuple() for identity in self.accepted_identities
            ),
            "rejected_identities": tuple(
                identity.as_tuple() for identity in self.rejected_identities
            ),
            "candidate_payload_hashes": tuple(
                (identity.as_tuple(), payload_hash)
                for identity, payload_hash in self.candidate_payload_hashes
            ),
        }

    @property
    def content_hash(self) -> str:
        return canonical_sha256(self.to_payload())


def _plain(value: object) -> object:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _plain(getattr(value, field.name))
            for field in dataclasses.fields(value)
        }
    if type(value) is tuple:
        return tuple(_plain(item) for item in value)
    if value is None or type(value) in (str, int, float, bool):
        return value
    raise TypeError(f"unsupported candidate payload type {type(value).__name__}")


def _candidate_identity(
    candidate: S3Candidate | S4Candidate,
    fold_or_full_window: str,
    phase: str,
) -> GeneratorIdentity:
    return GeneratorIdentity(
        candidate.strategy,
        candidate.config_id,
        fold_or_full_window,
        phase,
        candidate.decision_ts,
        candidate.symbol if type(candidate) is S3Candidate else candidate.pair,
        candidate.side,
    )


def _validate_global_units(output: S3GeneratorOutput | S4GeneratorOutput) -> None:
    decisions = output.decisions
    if not decisions or len(decisions) % 3:
        raise ValueError("one global invocation must evaluate three units per close")
    expected_units = SYMBOLS if output.strategy == "S3" else PAIRS
    by_close: dict[int, list[object]] = {}
    for decision in decisions:
        by_close.setdefault(decision.decision_ts, []).append(decision)
    for at_close in by_close.values():
        units = tuple(
            decision.symbol if output.strategy == "S3" else decision.pair
            for decision in at_close
        )
        if len(units) != 3 or set(units) != set(expected_units):
            raise ValueError("global invocation unit coverage mismatch")


def build_unique_generator_evidence(
    output: S3GeneratorOutput | S4GeneratorOutput,
    *,
    fold_or_full_window: str,
    phase: str,
) -> UniqueGeneratorEvidence:
    """Build one immutable evidence set from one whole global invocation."""
    if type(output) not in (S3GeneratorOutput, S4GeneratorOutput):
        raise TypeError("output must be one exact global S3/S4 generator output")
    _str(fold_or_full_window, "fold_or_full_window")
    _str(phase, "phase")
    if phase not in PHASES:
        raise ValueError("phase outside closed set")
    _validate_global_units(output)
    strategy = output.strategy
    config = get_config(output.config_id)
    if (strategy == "S3" and type(config) is not S3Config) or (
        strategy == "S4" and type(config) is not S4Config
    ):
        raise ValueError("global output strategy/config mismatch")
    no_signal_keys, rejection_keys = _histogram_keys(strategy)
    no_signal_counts = dict.fromkeys(no_signal_keys, 0)
    rejection_counts = dict.fromkeys(rejection_keys, 0)
    side_counts = dict.fromkeys(_side_keys(strategy), 0)

    no_signal = candidate_count = rejected_count = accepted_count = 0
    decision_accepted: set[tuple[object, ...]] = set()
    decision_rejected: set[tuple[object, ...]] = set()
    for decision in output.decisions:
        if decision.status == "NO_SIGNAL":
            if decision.candidate is not None or decision.no_signal_reason is None:
                raise ValueError("malformed NO_SIGNAL decision")
            no_signal += 1
            no_signal_counts[decision.no_signal_reason] += 1
        elif decision.status in ("GENERATOR_ACCEPTED", "GENERATOR_REJECTED"):
            if decision.candidate is None or decision.no_signal_reason is not None:
                raise ValueError("malformed candidate decision")
            candidate_count += 1
            side_counts[decision.candidate.side] += 1
            if decision.status == "GENERATOR_ACCEPTED":
                if decision.generator_rejection_reason is not None:
                    raise ValueError("accepted candidate has rejection reason")
                accepted_count += 1
                decision_accepted.add(decision.candidate.identity)
            else:
                if decision.generator_rejection_reason is None:
                    raise ValueError("rejected candidate lacks first-failing reason")
                rejected_count += 1
                rejection_counts[decision.generator_rejection_reason] += 1
                decision_rejected.add(decision.candidate.identity)
        else:
            raise ValueError("unknown generator decision status")

    output_accepted = {candidate.identity for candidate in output.accepted}
    output_rejected = {item.candidate.identity for item in output.rejected}
    if decision_accepted != output_accepted or decision_rejected != output_rejected:
        raise ValueError("decision/output candidate membership mismatch")
    if output_accepted & output_rejected:
        raise ValueError("accepted/rejected candidate collision")

    accepted = tuple(
        sorted(
            (
                _candidate_identity(candidate, fold_or_full_window, phase)
                for candidate in output.accepted
            ),
            key=GeneratorIdentity.as_tuple,
        )
    )
    rejected_candidates = tuple(item.candidate for item in output.rejected)
    rejected = tuple(
        sorted(
            (
                _candidate_identity(candidate, fold_or_full_window, phase)
                for candidate in rejected_candidates
            ),
            key=GeneratorIdentity.as_tuple,
        )
    )
    candidate_by_core_id = {
        candidate.identity: candidate
        for candidate in (*output.accepted, *rejected_candidates)
    }
    identity_by_core_id = {
        candidate.identity: _candidate_identity(candidate, fold_or_full_window, phase)
        for candidate in candidate_by_core_id.values()
    }
    payload_hashes = tuple(
        sorted(
            (
                (
                    identity_by_core_id[core_identity],
                    canonical_sha256(_plain(candidate)),
                )
                for core_identity, candidate in candidate_by_core_id.items()
            ),
            key=lambda item: item[0].as_tuple(),
        )
    )
    contract_hash = (
        S3_STRATEGY_CONTRACT.contract_hash
        if strategy == "S3"
        else S4_STRATEGY_CONTRACT.contract_hash
    )
    return UniqueGeneratorEvidence(
        "rob974-h3-generator-evidence-v1",
        strategy,
        output.config_id,
        contract_hash,
        fold_or_full_window,
        phase,
        1,
        len(output.decisions),
        no_signal,
        candidate_count,
        rejected_count,
        accepted_count,
        (
            ("no_signal", no_signal),
            ("candidate", candidate_count),
            ("generator_rejected", rejected_count),
            ("generator_accepted", accepted_count),
        ),
        tuple((key, no_signal_counts[key]) for key in no_signal_keys),
        tuple((key, rejection_counts[key]) for key in rejection_keys),
        tuple((key, side_counts[key]) for key in _side_keys(strategy)),
        accepted,
        rejected,
        payload_hashes,
    )


__all__ = [
    "GeneratorIdentity",
    "PHASES",
    "S3_GENERATOR_REJECTION_TAXONOMY",
    "S3_NO_SIGNAL_TAXONOMY",
    "S4_GENERATOR_REJECTION_TAXONOMY",
    "S4_NO_SIGNAL_TAXONOMY",
    "UniqueGeneratorEvidence",
    "build_unique_generator_evidence",
]

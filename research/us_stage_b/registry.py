"""Verbatim-bound registry for the three frozen US Stage-B candidates.

The packet YAML is the only configuration authority.  This module reads the
original bytes, verifies the packet digest *before* parsing, and derives each
candidate's contract hash from its raw YAML list-item bytes.  Formula values
are consumed from the parsed binding by the US-only signal engine; there is no
fallback to a shared US signal function.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

import yaml

__all__ = [
    "FROZEN_CANDIDATES_SHA256",
    "US_CANDIDATE_ORDER",
    "CandidateBinding",
    "CandidateRegistry",
    "RegistryStartRejected",
    "mandatory_labels",
]


FROZEN_CANDIDATES_SHA256: Final = (
    "0f5e92bf7d10dd77588fa08ad949811a68004cf71dd7f2efd232306b22d82d85"
)
"""SHA-256 of the frozen ``02-active-candidates.yaml`` packet."""

US_CANDIDATE_ORDER: Final[tuple[str, ...]] = (
    "US-TS-MOM-CONT-Z126-H20-v1",
    "US-TS-REV-SHORT-Z3-T126-H3-v1",
    "US-TS-VOLBREAK-C55-V2-H10-v1",
)

_COMMON_LABELS: Final[tuple[str, ...]] = (
    "EXPLORATORY_FALSIFICATION_ONLY",
    "SURVIVORSHIP_BIASED=TRUE",
    "PIT_DELIST_MISSING",
    "EXECUTION_ENVELOPE_UNBOUND",
)
_VOLBREAK_LABEL: Final = "VOLUME_CA_UNRESOLVED"

# These checks bind the three explicit code paths to the packet's parsed values.
# They are guards, not a second configuration source: signal evaluation fetches
# every value from ``CandidateBinding.parameters`` at runtime.
_EXPECTED_PARAMETERS: Final[Mapping[str, Mapping[str, int | float]]] = {
    "US-TS-MOM-CONT-Z126-H20-v1": {
        "adv20_min_usd": 5_000_000,
        "trend_lookback_sessions": 126,
        "trend_vol_lookback_sessions": 63,
        "trend_z_min": 1.0,
        "confirmation_lookback_sessions": 21,
        "confirmation_return_min": 0.0,
        "sma_lookback_sessions": 63,
        "hold_sessions": 20,
        "fixed_notional_usd": 500,
        "max_positions": 10,
    },
    "US-TS-REV-SHORT-Z3-T126-H3-v1": {
        "adv20_min_usd": 5_000_000,
        "shock_lookback_sessions": 3,
        "shock_vol_lookback_sessions": 60,
        "shock_z_max": -2.0,
        "trend_lookback_sessions": 126,
        "trend_return_min": 0.0,
        "hold_sessions": 3,
        "fixed_notional_usd": 500,
        "max_positions": 10,
    },
    "US-TS-VOLBREAK-C55-V2-H10-v1": {
        "adv20_min_usd": 5_000_000,
        "breakout_lookback_sessions": 55,
        "volume_lookback_sessions": 20,
        "volume_ratio_min": 2.0,
        "volume_ratio_max": 10.0,
        "daily_return_min": 0.0,
        "hold_sessions": 10,
        "fixed_notional_usd": 500,
        "max_positions": 10,
    },
}

_REQUIRED_TEXT: Final[Mapping[str, Mapping[str, tuple[str, ...]]]] = {
    "US-TS-MOM-CONT-Z126-H20-v1": {
        "signal": (
            "R126=",
            "z126=",
            "sigma63",
            "sqrt(126)",
            "R21",
            "SMA63",
            "no_active_position",
        ),
        "exit": ("D+20", "corpus session index"),
        "ranking": ("ADV20_pre_proxy", "수익률·z126·신호강도 횡단면 순위 금지"),
    },
    "US-TS-REV-SHORT-Z3-T126-H3-v1": {
        "signal": ("R3=", "z3=", "sigma60", "sqrt(3)", "R126", "no_active_position"),
        "exit": ("D+3",),
        "ranking": ("ADV20_pre_proxy", "하락폭·z3 우선 금지"),
    },
    "US-TS-VOLBREAK-C55-V2-H10-v1": {
        "signal": (
            "prior_close_high55",
            "volume_ratio20",
            "R1",
            "high/low 미사용",
            "no_active_position",
        ),
        "exit": ("D+10",),
        "ranking": ("ADV20_pre_proxy",),
    },
}


class RegistryStartRejected(RuntimeError):
    """A packet is unsealed, malformed, or drifts from the exact US contract."""


def mandatory_labels(strategy_id: str) -> tuple[str, ...]:
    """Return literal, candidate-scoped caution labels for every output."""

    if strategy_id not in US_CANDIDATE_ORDER:
        raise RegistryStartRejected(
            f"unsupported strategy_id {strategy_id!r}; shared fallback is forbidden"
        )
    if strategy_id == "US-TS-VOLBREAK-C55-V2-H10-v1":
        return (*_COMMON_LABELS, _VOLBREAK_LABEL)
    return _COMMON_LABELS


@dataclass(frozen=True)
class CandidateBinding:
    """One source-parsed US candidate plus its immutable raw-byte identity."""

    strategy_id: str
    contract_hash: str
    source_packet_sha256: str
    source_block: bytes
    family_id: str
    parameters: Mapping[str, int | float]
    side: str
    universe_filter: str
    signal: str
    required_history: str
    missing_data_handling: str
    entry: str
    exit: str
    ranking: str
    tie_break: str
    sizing: str
    falsification: str
    execution_envelope: str

    @property
    def labels(self) -> tuple[str, ...]:
        return mandatory_labels(self.strategy_id)

    def parameter(self, name: str) -> int | float:
        """Read one packet-derived parameter without injecting a fallback."""

        try:
            return self.parameters[name]
        except KeyError as exc:
            raise RegistryStartRejected(
                f"{self.strategy_id} lacks packet parameter {name!r}"
            ) from exc

    def stamp(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Return a JSON-safe payload whose immutable identity cannot be replaced."""

        return {
            **dict(payload),
            "strategy_id": self.strategy_id,
            "contract_hash": self.contract_hash,
            "labels": list(self.labels),
        }

    def to_dict(self) -> dict[str, Any]:
        """Serialize the parsed packet contract with mandatory provenance."""

        return self.stamp(
            {
                "source_packet_sha256": self.source_packet_sha256,
                "family_id": self.family_id,
                "side": self.side,
                "parameter_values": dict(self.parameters),
                "universe_filter": self.universe_filter,
                "signal": self.signal,
                "required_history": self.required_history,
                "missing_data_handling": self.missing_data_handling,
                "entry": self.entry,
                "exit": self.exit,
                "ranking": self.ranking,
                "tie_break": self.tie_break,
                "sizing": self.sizing,
                "falsification": self.falsification,
                "execution_envelope": self.execution_envelope,
            }
        )


@dataclass(frozen=True)
class CandidateRegistry:
    """Exact US-only registry produced from verified original YAML bytes."""

    source_packet_sha256: str
    candidates: Mapping[str, CandidateBinding]

    @classmethod
    def load(
        cls,
        candidates_yaml: Path | str,
        *,
        expected_sha256: str = FROZEN_CANDIDATES_SHA256,
    ) -> CandidateRegistry:
        """Read bytes and bind the parsed candidates only after SHA verification."""

        path = Path(candidates_yaml)
        if not path.is_file():
            raise RegistryStartRejected(f"frozen candidates YAML is not a file: {path}")
        try:
            source = path.read_bytes()
        except OSError as exc:
            raise RegistryStartRejected(
                f"cannot read frozen candidates YAML: {path}"
            ) from exc
        return cls.from_verbatim_bytes(source, expected_sha256=expected_sha256)

    @classmethod
    def from_verbatim_bytes(
        cls,
        source: bytes,
        *,
        expected_sha256: str,
    ) -> CandidateRegistry:
        """Verify and parse the exact byte stream; no hand-transcribed path exists."""

        actual_sha256 = hashlib.sha256(source).hexdigest()
        if actual_sha256 != expected_sha256:
            raise RegistryStartRejected(
                "frozen candidates YAML SHA mismatch: "
                f"expected={expected_sha256} actual={actual_sha256}"
            )
        try:
            parsed = yaml.safe_load(source)
        except yaml.YAMLError as exc:
            raise RegistryStartRejected("frozen candidates YAML parse failed") from exc
        if not isinstance(parsed, Mapping):
            raise RegistryStartRejected("frozen candidates YAML root is not a mapping")
        raw_candidates = parsed.get("candidates")
        if not isinstance(raw_candidates, list):
            raise RegistryStartRejected(
                "frozen candidates YAML lacks a candidates list"
            )
        raw_blocks = _candidate_block_bytes(source)
        if len(raw_candidates) != len(raw_blocks):
            raise RegistryStartRejected("YAML candidate/list-item byte count mismatch")

        candidates: dict[str, CandidateBinding] = {}
        for raw, block in zip(raw_candidates, raw_blocks, strict=True):
            if not isinstance(raw, Mapping):
                raise RegistryStartRejected("candidate YAML item is not a mapping")
            if raw.get("market") != "US":
                continue
            binding = _binding_from_raw(
                raw,
                source_block=block,
                source_packet_sha256=actual_sha256,
            )
            if binding.strategy_id in candidates:
                raise RegistryStartRejected(
                    f"duplicate US strategy_id {binding.strategy_id!r}"
                )
            candidates[binding.strategy_id] = binding

        if tuple(candidates) != US_CANDIDATE_ORDER:
            raise RegistryStartRejected(
                "US candidate order/id drift: "
                f"expected={US_CANDIDATE_ORDER!r} actual={tuple(candidates)!r}"
            )
        for binding in candidates.values():
            _assert_exact_contract(binding)
        return cls(
            source_packet_sha256=actual_sha256,
            candidates=MappingProxyType(candidates),
        )

    @property
    def admitted(self) -> tuple[CandidateBinding, ...]:
        """Return the three frozen candidates in the packet's declared order."""

        return tuple(self.candidates[strategy_id] for strategy_id in US_CANDIDATE_ORDER)

    def binding_for(self, strategy_id: str) -> CandidateBinding:
        """Look up one exact candidate; family/name fallbacks are forbidden."""

        try:
            return self.candidates[strategy_id]
        except KeyError as exc:
            raise RegistryStartRejected(
                f"unsupported strategy_id {strategy_id!r}; shared fallback is forbidden"
            ) from exc


def _candidate_block_bytes(source: bytes) -> tuple[bytes, ...]:
    """Return exact top-level candidate list-item ranges from packet bytes."""

    lines = source.splitlines(keepends=True)
    offsets: list[int] = []
    offset = 0
    for line in lines:
        if line.startswith(b"  - market:"):
            offsets.append(offset)
        offset += len(line)
    if not offsets:
        raise RegistryStartRejected("frozen candidates YAML has no candidate blocks")
    ends = (*offsets[1:], len(source))
    return tuple(source[start:end] for start, end in zip(offsets, ends, strict=True))


def _binding_from_raw(
    raw: Mapping[str, Any],
    *,
    source_block: bytes,
    source_packet_sha256: str,
) -> CandidateBinding:
    strategy_id = raw.get("strategy_id")
    parameters = raw.get("parameter_values")
    required_text_fields = (
        "side",
        "universe_filter",
        "signal",
        "required_history",
        "missing_data_handling",
        "entry",
        "exit",
        "ranking",
        "tie_break",
        "sizing",
        "falsification",
        "execution_envelope",
    )
    if not isinstance(strategy_id, str) or not strategy_id:
        raise RegistryStartRejected("US candidate lacks a non-empty strategy_id")
    if not isinstance(parameters, Mapping):
        raise RegistryStartRejected(f"{strategy_id} lacks parameter_values")
    if any(not isinstance(raw.get(field), str) for field in required_text_fields):
        raise RegistryStartRejected(f"{strategy_id} has a non-string contract field")
    raw_family = raw.get("family_id")
    if (
        not isinstance(raw_family, Mapping)
        or raw_family.get("status") != "SOURCE_PROVIDED"
        or not isinstance(raw_family.get("value"), str)
        or not raw_family["value"]
    ):
        raise RegistryStartRejected(f"{strategy_id} has an invalid family_id binding")

    normalized_parameters: dict[str, int | float] = {}
    for name, value in parameters.items():
        if (
            not isinstance(name, str)
            or isinstance(value, bool)
            or not isinstance(value, int | float)
        ):
            raise RegistryStartRejected(
                f"{strategy_id} has a non-numeric packet parameter {name!r}"
            )
        normalized_parameters[name] = value
    return CandidateBinding(
        strategy_id=strategy_id,
        contract_hash=hashlib.sha256(source_block).hexdigest(),
        source_packet_sha256=source_packet_sha256,
        source_block=source_block,
        family_id=str(raw_family["value"]),
        parameters=MappingProxyType(normalized_parameters),
        side=str(raw["side"]),
        universe_filter=str(raw["universe_filter"]),
        signal=str(raw["signal"]),
        required_history=str(raw["required_history"]),
        missing_data_handling=str(raw["missing_data_handling"]),
        entry=str(raw["entry"]),
        exit=str(raw["exit"]),
        ranking=str(raw["ranking"]),
        tie_break=str(raw["tie_break"]),
        sizing=str(raw["sizing"]),
        falsification=str(raw["falsification"]),
        execution_envelope=str(raw["execution_envelope"]),
    )


def _assert_exact_contract(binding: CandidateBinding) -> None:
    """Reject a parsed packet whose semantics no longer match a code branch."""

    expected_parameters = _EXPECTED_PARAMETERS.get(binding.strategy_id)
    if expected_parameters is None:
        raise RegistryStartRejected(
            f"no exact US implementation exists for {binding.strategy_id!r}"
        )
    if dict(binding.parameters) != dict(expected_parameters):
        raise RegistryStartRejected(
            f"{binding.strategy_id} parameter/code binding mismatch"
        )
    if binding.side != "long" or binding.entry != "t_plus_1_open":
        raise RegistryStartRejected(f"{binding.strategy_id} side/entry contract drift")
    if "run-invalid" not in binding.missing_data_handling:
        raise RegistryStartRejected(
            f"{binding.strategy_id} must keep missing maturity close as run-invalid"
        )
    if "sha256" not in binding.tie_break:
        raise RegistryStartRejected(f"{binding.strategy_id} tie-break contract drift")
    if "ADV20_pre_proxy" not in binding.ranking:
        raise RegistryStartRejected(f"{binding.strategy_id} ranking contract drift")
    for field, tokens in _REQUIRED_TEXT[binding.strategy_id].items():
        value = getattr(binding, field)
        if any(token not in value for token in tokens):
            raise RegistryStartRejected(
                f"{binding.strategy_id} {field} semantic binding mismatch"
            )

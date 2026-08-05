"""Fail-closed, hash-bound registry for the KR-A0 packet candidates.

The registry intentionally has no default artifact location.  A caller must name
all serialized inputs explicitly, and startup verifies every pinned byte stream
before it returns an executable candidate binding.  It does not import the
legacy Stage-B or shadow signal implementations.
"""

from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import yaml

CANDIDATES_SHA256: Final = (
    "0f5e92bf7d10dd77588fa08ad949811a68004cf71dd7f2efd232306b22d82d85"
)
GOLDEN_V6_SHA256: Final = (
    "996a30b9c233320665aa17845991287fd7af71704af6f91a6956d12a91738d4f"
)
CONVENTION_SHA256: Final[dict[str, str]] = {
    "amendment_a1_a9": (
        "5cbe56934b0e53eba8123c81aa8999e6bc5f862c9ee6e0f0d4664b3d2b098092"
    ),
    "amendment_a10_a12": (
        "79ec69c935af9f271e02cb8b01805d727fd4cf7b9ab342aaa0a49995166d30b9"
    ),
    "amendment_a13_a14": (
        "5bdb261504b7d3c298f3a85cc07cef72dc0528adad72c685dd322884b1bd6b59"
    ),
    "amendment_a15": (
        "4b421585db09829964dccc719130d07d7fa11f0b450e536cdd30f8e95f4d3b36"
    ),
    "generator": ("32f15e948eee30d75215716688d4fc421415145618ff4f5637531cb4c3fc6b9f"),
}
BASE_FIXTURE_FILENAMES: Final[tuple[str, ...]] = (
    "fixture_bars.csv",
    "fixture_sessions.csv",
    "fixture_config.csv",
    "fixture_membership.csv",
    "fixture_delist_events.csv",
)
KR_PACKET_ORDER: Final[tuple[str, ...]] = (
    "rev3_reclaim",
    "brk20_confirm",
    "lowvol_up_month",
)
PERCENTILE_CONVENTION: Final = "pct_asc_self_inclusive_ties_per_market"


class RegistryStartRejected(RuntimeError):
    """A pinned input or parsed packet contract cannot be trusted."""


class NeedsUpstream(RuntimeError):
    """A policy convention conflict must be returned to the upstream owner."""

    def __init__(self, item: str) -> None:
        self.item = item
        super().__init__(f"NEEDS_UPSTREAM({item})")


@dataclass(frozen=True)
class ArtifactPaths:
    """All serialized inputs required before registry startup is allowed."""

    candidates_yaml: Path
    golden_v6: Path
    amendment_a1_a9: Path
    amendment_a10_a12: Path
    amendment_a13_a14: Path
    amendment_a15: Path
    generator: Path
    fixture_root: Path

    @classmethod
    def from_fixture_bundle(cls, root: Path | str) -> ArtifactPaths:
        """Build paths for a self-contained golden fixture bundle.

        This is intentionally a convenience for offline verification fixtures,
        not a production default.  Production callers still pass a concrete
        bundle root and therefore cannot silently fall back to stale inputs.
        """

        bundle = Path(root)
        inbox = bundle / "inbox"
        return cls(
            candidates_yaml=bundle / "02-active-candidates.yaml",
            golden_v6=bundle / "golden_v6.json",
            amendment_a1_a9=inbox
            / "amendment-kr-engine-conventions-v2-draft-20260805.md",
            amendment_a10_a12=inbox / "amendment-kr-engine-a10-a12-draft-20260805.md",
            amendment_a13_a14=inbox / "amendment-kr-engine-a13-a14-draft-20260805.md",
            amendment_a15=inbox / "amendment-kr-engine-a15-20260805.md",
            generator=bundle / "reference_generator_v4.py",
            fixture_root=bundle,
        )


@dataclass(frozen=True)
class VerifiedInputs:
    """Recomputed proof material retained by the registry and run outputs."""

    candidates_sha256: str
    golden_sha256: str
    convention_sha256: Mapping[str, str]
    fixture_sha256: Mapping[str, str]
    variant_fixture_sha256: Mapping[str, Mapping[str, str]]

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidates_yaml_sha256": self.candidates_sha256,
            "golden_v6_sha256": self.golden_sha256,
            "convention_sha256": dict(self.convention_sha256),
            "fixture_sha256": dict(self.fixture_sha256),
            "variant_fixture_sha256": {
                name: dict(files) for name, files in self.variant_fixture_sha256.items()
            },
        }


@dataclass(frozen=True)
class CandidateBinding:
    """A parsed KR packet candidate plus its source-byte contract hash."""

    strategy_id: str
    market: str
    contract_hash: str
    source_block: bytes
    parameters: Mapping[str, Any]
    entry: str
    exit: str
    ranking: str
    tie_break: str
    signal: str
    missing_data_handling: str

    @property
    def holding_sessions(self) -> int:
        value = self.parameters.get("holding_sessions")
        if not isinstance(value, int) or value <= 0:
            raise RegistryStartRejected(
                f"candidate {self.strategy_id} has invalid holding_sessions"
            )
        return value

    @property
    def max_positions(self) -> int:
        value = self.parameters.get(
            "max_concurrent_positions", self.parameters.get("max_positions")
        )
        if not isinstance(value, int) or value != 10:
            raise RegistryStartRejected(
                f"candidate {self.strategy_id} max position contract drift"
            )
        return value


@dataclass(frozen=True)
class CandidateRegistry:
    """Executable registry returned only after all exact bindings have passed."""

    candidates: Mapping[str, CandidateBinding]
    inputs: VerifiedInputs

    @classmethod
    def start(
        cls,
        paths: ArtifactPaths,
        *,
        observed_percentile_convention: str | None = None,
    ) -> CandidateRegistry:
        """Verify inputs, parse the source YAML, and reject all unsupported drift."""

        if sys.flags.optimize != 0:
            raise RegistryStartRejected(
                "canonical runtime requires plain Python; -O is forbidden"
            )
        if sys.version_info[:2] != (3, 13):
            raise RegistryStartRejected("canonical runtime requires Python 3.13.x")
        if (
            observed_percentile_convention is not None
            and observed_percentile_convention != PERCENTILE_CONVENTION
        ):
            raise NeedsUpstream("percentile convention 충돌")

        inputs = verify_bound_inputs(paths)
        source = _read_regular_file(paths.candidates_yaml, label="candidates YAML")
        parsed = _parse_yaml_mapping(source, paths.candidates_yaml)
        raw_blocks = _candidate_block_bytes(source)
        raw_candidates = parsed.get("candidates")
        if not isinstance(raw_candidates, list):
            raise RegistryStartRejected("candidates YAML has no candidates list")
        if len(raw_candidates) != len(raw_blocks):
            raise RegistryStartRejected("candidate YAML block parser count mismatch")

        bindings: dict[str, CandidateBinding] = {}
        for raw, block in zip(raw_candidates, raw_blocks, strict=True):
            if not isinstance(raw, Mapping):
                raise RegistryStartRejected("candidate YAML item is not a mapping")
            if raw.get("market") != "KR":
                continue
            binding = _binding_from_candidate(raw, block)
            if binding.strategy_id in bindings:
                raise RegistryStartRejected(
                    f"duplicate KR strategy_id {binding.strategy_id!r}"
                )
            bindings[binding.strategy_id] = binding

        if tuple(bindings) != KR_PACKET_ORDER:
            raise RegistryStartRejected(
                "KR candidate order/id drift: "
                f"expected={KR_PACKET_ORDER!r} actual={tuple(bindings)!r}"
            )
        for binding in bindings.values():
            _assert_binding_implements_packet_contract(binding)
        return cls(candidates=bindings, inputs=inputs)

    def binding_for(self, strategy_id: str) -> CandidateBinding:
        try:
            return self.candidates[strategy_id]
        except KeyError as exc:
            raise RegistryStartRejected(
                f"unsupported strategy_id {strategy_id!r}; shared fallback is forbidden"
            ) from exc

    def stamp(self, strategy_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Attach the immutable candidate identity to a materialized output."""

        binding = self.binding_for(strategy_id)
        return {
            "strategy_id": binding.strategy_id,
            "contract_hash": binding.contract_hash,
            **dict(payload),
        }


def sha256_file(path: Path | str) -> str:
    """Return a file SHA-256 without accepting a directory or a missing input."""

    resolved = Path(path)
    if not resolved.is_file():
        raise RegistryStartRejected(f"required input missing or not a file: {resolved}")
    return hashlib.sha256(resolved.read_bytes()).hexdigest()


def verify_bound_inputs(paths: ArtifactPaths) -> VerifiedInputs:
    """Recompute and cross-check every SHA named by golden v6.

    The artifact root is allowed to contain only the five named fixture files and
    explicitly named variant directories for this verification path.  No corpus
    path, especially no holdout path, is accepted here.
    """

    fixture_root = _safe_fixture_root(paths.fixture_root)
    candidate_sha = _require_sha(
        paths.candidates_yaml, CANDIDATES_SHA256, label="candidate YAML"
    )
    golden_sha = _require_sha(paths.golden_v6, GOLDEN_V6_SHA256, label="golden v6")
    convention_paths = {
        "amendment_a1_a9": paths.amendment_a1_a9,
        "amendment_a10_a12": paths.amendment_a10_a12,
        "amendment_a13_a14": paths.amendment_a13_a14,
        "amendment_a15": paths.amendment_a15,
        "generator": paths.generator,
    }
    recomputed_conventions = {
        name: _require_sha(path, CONVENTION_SHA256[name], label=name)
        for name, path in convention_paths.items()
    }

    golden_raw = _read_regular_file(paths.golden_v6, label="golden v6")
    try:
        golden = json.loads(
            golden_raw, parse_constant=_reject_nonstandard_json_constant
        )
    except json.JSONDecodeError as exc:
        raise RegistryStartRejected("golden v6 is not valid JSON") from exc
    except ValueError as exc:
        raise RegistryStartRejected(
            "golden v6 has a non-standard JSON constant"
        ) from exc
    if not isinstance(golden, Mapping):
        raise RegistryStartRejected("golden v6 root is not an object")
    if golden.get("candidates_yaml_sha256") != candidate_sha:
        raise RegistryStartRejected("golden/candidate SHA binding mismatch")
    if golden.get("convention_sha256") != recomputed_conventions:
        raise RegistryStartRejected("golden/convention SHA binding mismatch")

    fixture_expected = _mapping_of_sha(golden.get("fixture_sha256"), "fixture_sha256")
    if tuple(fixture_expected) != BASE_FIXTURE_FILENAMES:
        raise RegistryStartRejected("golden base fixture filename/order drift")
    fixture_actual = {
        filename: _require_sha(
            _fixture_file(fixture_root, filename), expected, label=filename
        )
        for filename, expected in fixture_expected.items()
    }

    raw_variants = golden.get("variant_fixture_sha256")
    if not isinstance(raw_variants, Mapping) or not raw_variants:
        raise RegistryStartRejected("golden variant fixture SHA mapping missing")
    variant_actual: dict[str, Mapping[str, str]] = {}
    for name, raw_files in raw_variants.items():
        if not isinstance(name, str) or not name:
            raise RegistryStartRejected("golden variant name invalid")
        expected_files = _mapping_of_sha(raw_files, f"variant {name}")
        if tuple(expected_files) != BASE_FIXTURE_FILENAMES:
            raise RegistryStartRejected(f"variant fixture filename/order drift: {name}")
        variant_root = _safe_child(fixture_root / "variants" / name, fixture_root)
        variant_actual[name] = {
            filename: _require_sha(
                _fixture_file(variant_root, filename),
                expected,
                label=f"{name}/{filename}",
            )
            for filename, expected in expected_files.items()
        }

    return VerifiedInputs(
        candidates_sha256=candidate_sha,
        golden_sha256=golden_sha,
        convention_sha256=recomputed_conventions,
        fixture_sha256=fixture_actual,
        variant_fixture_sha256=variant_actual,
    )


def _require_sha(path: Path | str, expected: str, *, label: str) -> str:
    actual = sha256_file(path)
    if actual != expected:
        raise RegistryStartRejected(
            f"{label} SHA mismatch: expected={expected} actual={actual}"
        )
    return actual


def _read_regular_file(path: Path | str, *, label: str) -> bytes:
    resolved = Path(path)
    if not resolved.is_file():
        raise RegistryStartRejected(f"{label} missing or not a file: {resolved}")
    return resolved.read_bytes()


def _reject_nonstandard_json_constant(constant: str) -> None:
    """Keep RFC-strict JSON at the registry boundary (no bare Infinity/NaN)."""

    raise ValueError(f"non-standard JSON constant {constant}")


def _safe_fixture_root(path: Path | str) -> Path:
    root = Path(path).resolve()
    if "holdout" in {part.casefold() for part in root.parts}:
        raise RegistryStartRejected("fixture root may not be a holdout path")
    if not root.is_dir():
        raise RegistryStartRejected(f"fixture root missing or not a directory: {root}")
    return root


def _safe_child(path: Path, root: Path) -> Path:
    resolved = path.resolve()
    if "holdout" in {part.casefold() for part in resolved.parts}:
        raise RegistryStartRejected("fixture path may not include holdout")
    if resolved != root and root not in resolved.parents:
        raise RegistryStartRejected(f"fixture path escapes root: {path}")
    return resolved


def _fixture_file(root: Path, filename: str) -> Path:
    if filename not in BASE_FIXTURE_FILENAMES:
        raise RegistryStartRejected(f"unexpected fixture filename {filename!r}")
    return _safe_child(root / filename, root)


def _mapping_of_sha(raw: object, label: str) -> dict[str, str]:
    if not isinstance(raw, Mapping):
        raise RegistryStartRejected(f"{label} is not a SHA mapping")
    result: dict[str, str] = {}
    for name, value in raw.items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise RegistryStartRejected(f"{label} contains a non-string SHA entry")
        result[name] = value
    return result


def _parse_yaml_mapping(source: bytes, path: Path) -> Mapping[str, Any]:
    try:
        parsed = yaml.safe_load(source)
    except yaml.YAMLError as exc:
        raise RegistryStartRejected(f"candidate YAML parse failed: {path}") from exc
    if not isinstance(parsed, Mapping):
        raise RegistryStartRejected("candidate YAML root is not a mapping")
    return parsed


def _candidate_block_bytes(source: bytes) -> tuple[bytes, ...]:
    """Return raw YAML list-item byte ranges, preserving the exact source bytes.

    The source packet has one top-level ``candidates`` sequence.  A byte range
    begins at the two-space list marker and ends immediately before the next
    marker; this is the only source used for ``contract_hash``.
    """

    lines = source.splitlines(keepends=True)
    offsets: list[int] = []
    offset = 0
    for line in lines:
        if line.startswith(b"  - "):
            offsets.append(offset)
        offset += len(line)
    if not offsets:
        raise RegistryStartRejected("candidate YAML has no raw candidate blocks")
    ends = (*offsets[1:], len(source))
    return tuple(source[start:end] for start, end in zip(offsets, ends, strict=True))


def _binding_from_candidate(raw: Mapping[str, Any], block: bytes) -> CandidateBinding:
    strategy_id = raw.get("strategy_id")
    market = raw.get("market")
    parameters = raw.get("parameter_values")
    required_strings = {
        "entry": raw.get("entry"),
        "exit": raw.get("exit"),
        "ranking": raw.get("ranking"),
        "tie_break": raw.get("tie_break"),
        "signal": raw.get("signal"),
        "missing_data_handling": raw.get("missing_data_handling"),
    }
    if not isinstance(strategy_id, str) or not isinstance(market, str):
        raise RegistryStartRejected("candidate missing strategy_id or market")
    if not isinstance(parameters, Mapping):
        raise RegistryStartRejected(f"candidate {strategy_id} lacks parameter_values")
    if any(not isinstance(value, str) for value in required_strings.values()):
        raise RegistryStartRejected(
            f"candidate {strategy_id} has a non-string contract field"
        )
    return CandidateBinding(
        strategy_id=strategy_id,
        market=market,
        contract_hash=hashlib.sha256(block).hexdigest(),
        source_block=block,
        parameters=dict(parameters),
        entry=required_strings["entry"],
        exit=required_strings["exit"],
        ranking=required_strings["ranking"],
        tie_break=required_strings["tie_break"],
        signal=required_strings["signal"],
        missing_data_handling=required_strings["missing_data_handling"],
    )


def _assert_binding_implements_packet_contract(binding: CandidateBinding) -> None:
    """Reject parser/code semantic drift before a candidate is executable.

    Full file SHA binding catches any serialized-input change.  These additional
    checks make the code's three formula implementations explicit and prevent
    a strategy name from silently reaching a similarly named fallback.
    """

    expected_parameters: Mapping[str, Mapping[str, Any]] = {
        "rev3_reclaim": {
            "liquidity_percentile_min": 0.50,
            "reversal_lookback_sessions": 3,
            "loser_percentile_max": 0.05,
            "close_location_min": 0.65,
            "volume_ratio_min": 1.50,
            "holding_sessions": 5,
            "max_concurrent_positions": 10,
        },
        "brk20_confirm": {
            "liquidity_percentile_min": 0.50,
            "breakout_lookback_sessions": 20,
            "close_location_min": 0.75,
            "volume_ratio_min": 1.25,
            "holding_sessions": 10,
            "max_concurrent_positions": 10,
        },
        "lowvol_up_month": {
            "selection_schedule": "calendar_month_last_xkrx_session",
            "liquidity_percentile_min": 0.50,
            "volatility_lookback_sessions": 20,
            "low_volatility_percentile_max": 0.30,
            "trend_lookback_sessions": 20,
            "trend_return_floor": 0.0,
            "holding_sessions": 20,
            "max_concurrent_positions": 10,
        },
    }
    expected = expected_parameters.get(binding.strategy_id)
    if expected is None:
        raise RegistryStartRejected(
            f"no exact packet implementation for {binding.strategy_id!r}; fallback forbidden"
        )
    if binding.market != "KR":
        raise RegistryStartRejected(f"candidate {binding.strategy_id} market drift")
    if dict(binding.parameters) != dict(expected):
        raise RegistryStartRejected(
            f"candidate {binding.strategy_id} parameter/code binding mismatch"
        )
    if binding.entry != "t_plus_1_open":
        raise RegistryStartRejected(
            f"candidate {binding.strategy_id} entry contract drift"
        )
    if "no-fill" not in binding.missing_data_handling.casefold():
        raise RegistryStartRejected(
            f"candidate {binding.strategy_id} no-fill contract drift"
        )
    if "KOSPI" not in binding.tie_break or "KOSDAQ" not in binding.tie_break:
        raise RegistryStartRejected(
            f"candidate {binding.strategy_id} tie-break contract drift"
        )
    if binding.strategy_id == "rev3_reclaim":
        required = ("r3_pct_t", "clv_t", "volume_ratio_t")
    elif binding.strategy_id == "brk20_confirm":
        required = ("prior_high20_t", "breakout_margin_t", "clv_t")
    else:
        required = ("vol20_t", "vol20_pct_t", "r20_t")
    if any(token not in binding.signal for token in required):
        raise RegistryStartRejected(
            f"candidate {binding.strategy_id} signal/code binding mismatch"
        )


def expected_variant_names(inputs: VerifiedInputs) -> Sequence[str]:
    """Expose the immutable variant set for callers that need full E2E coverage."""

    return tuple(inputs.variant_fixture_sha256)

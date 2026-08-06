"""Artifact-only top-level verdict resynthesis for the frozen CR-S1 results.

This module deliberately consumes completed per-arm artifacts rather than the
Stage-B engine or any corpus input.  It therefore cannot spend the one-time
backtest budget while applying later top-level presentation rules.

The ``< 2`` condition below is *not* a new minimum-sample gate.  The frozen
falsification contract requires the mandatory result ``FRAGILE``: whether
removing the highest-performing contributing year reverses the sign.  That
calculation needs a highest year and a remaining contributing year.  When it
cannot be calculated, a top-level verdict cannot be asserted; the
INCONCLUSIVE predicate is derived from that requirement.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

__all__ = [
    "ArtifactSchemaError",
    "EXPECTED_CR_S1_R2_SHA256",
    "TopLevelResynthesis",
    "apply_inconclusive_predicate",
    "canonical_json_bytes",
    "resynthesize_artifact_files",
    "resynthesize_artifact_directory",
    "resynthesize_pair_artifact",
    "sha256_file",
    "write_json_once",
]


_SCHEMA_VERSION: Final = "cr-s1-b2-top-level-resynthesis-v1"
_MANIFEST_FILENAME: Final = "manifest.json"

# The job relay fixes these seven source bytes.  The production entry point
# checks them before inspecting any artifact payload, while tests can inject a
# small fixture map into ``resynthesize_artifact_directory``.
EXPECTED_CR_S1_R2_SHA256: Final[dict[str, str]] = {
    "manifest.json": "ce0e804069a4fd8f343d8a8ffed64c076ac36cec13f8f9a4b7dc3d5f5b3d9329",
    "cr-spot-etr-01__upbit_krw.json": "7012ed2bb8a1697e45a26b94e3998362135f49e338b97562686a5d9fa596cc26",
    "cr-spot-etr-01__binance_usdt_spot.json": "d8d940625644b0cbc5802cd8e915b0f566d556f49f9adf76a7a6949f654aa63e",
    "cr-spot-ceb-01__upbit_krw.json": "03f61c364c3b5902dd9064efaf7e67b7ec3442e8fe2fd25e5d0fb04ff6efed8e",
    "cr-spot-ceb-01__binance_usdt_spot.json": "a02a2b524b00e236affa6540d6b2f0a777e343401cb1a36bc24dcd045cb7e4b1",
    "cr-spot-tpr-01__upbit_krw.json": "8be71d40b28f00a8bacf1a31f10fe51795fc02c236d0d6297d8c56c5cf791a4c",
    "cr-spot-tpr-01__binance_usdt_spot.json": "75e62782a19004904aa4bb980010a8c56e38683a5e23d3329b76c5d48990cb58",
}


class ArtifactSchemaError(ValueError):
    """A source artifact cannot support a deterministic resynthesis."""


@dataclass(frozen=True)
class TopLevelResynthesis:
    """One candidate × venue top-level result derived from completed arm data."""

    strategy_id: str
    venue: str
    contract_hash: str
    source_artifact_filename: str
    source_artifact_sha256: str
    source_verdict_base: str
    source_verdict_sensitivity: str
    verdict_base: str
    verdict_sensitivity: str
    judgeable_years: int
    fragile: bool
    fragile_evidence: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        """Return one disclosure-first result without altering arm evidence."""
        return {
            "schema_version": _SCHEMA_VERSION,
            "resynthesis_only": True,
            "strategy_id": self.strategy_id,
            "venue": self.venue,
            "contract_hash": self.contract_hash,
            "source_artifact": {
                "filename": self.source_artifact_filename,
                "sha256": self.source_artifact_sha256,
            },
            # This is a headline disclosure, not a separate acceptance gate.
            # ``judgeable_years`` reaches a verdict only through the derived
            # FRAGILE-impossibility predicate in ``apply_inconclusive_predicate``.
            "headline": {
                "verdict_base": self.verdict_base,
                "verdict_sensitivity": self.verdict_sensitivity,
                "judgeable_years": self.judgeable_years,
                "fragile": self.fragile,
            },
            "source_verdict_base": self.source_verdict_base,
            "source_verdict_sensitivity": self.source_verdict_sensitivity,
            "fragile_evidence": dict(self.fragile_evidence),
        }


def apply_inconclusive_predicate(
    existing_verdict: str,
    *,
    judgeable_years: int,
    all_arms_empty: bool,
) -> str:
    """Apply the frozen B2 precedence without weakening existing verdicts.

    ``RUN_INVALID`` has higher priority than a derived INCONCLUSIVE state.
    Otherwise an empty pair of arms is distinguished from fewer than two
    contributing years; only after those two cases does a prior FALSIFIED or
    PASS/(NOT_)FALSIFIED label remain in force.
    """
    if not existing_verdict:
        raise ArtifactSchemaError("existing verdict must be a non-empty label")
    if judgeable_years < 0:
        raise ArtifactSchemaError("judgeable_years must not be negative")
    if existing_verdict.startswith("RUN_INVALID"):
        return existing_verdict
    if all_arms_empty:
        return "INCONCLUSIVE_EMPTY_ALL_ARMS"
    if judgeable_years < 2:
        return "INCONCLUSIVE_INSUFFICIENT_JUDGEABLE_YEARS"
    return existing_verdict


def resynthesize_pair_artifact(
    artifact: Mapping[str, Any],
    *,
    source_artifact_filename: str,
    source_artifact_sha256: str,
) -> TopLevelResynthesis:
    """Resynthesize a candidate × venue result from its existing arm artifact.

    The engine is intentionally not imported here.  A calendar year is
    judgeable only when *both* full and ablation arms have a finite net mean;
    an empty arm cannot make an annual incremental comparison or contribute to
    the mandatory FRAGILE leave-one-best-year calculation.
    """
    source = _normalize_source_artifact(artifact)
    records = _required_sequence(_required_mapping(source, "harness_query"), "records")
    judgeable_years = sum(_is_judgeable_year(record) for record in records)
    all_arms_empty = _all_arms_empty(records)
    fragile_evidence = _required_mapping(source, "fragile")
    fragile = fragile_evidence.get("fragile")
    if not isinstance(fragile, bool):
        raise ArtifactSchemaError("fragile.fragile must be a bool")

    source_verdict_base = _required_string(source, "verdict_base")
    source_verdict_sensitivity = _required_string(source, "verdict_sensitivity")
    return TopLevelResynthesis(
        strategy_id=_required_string(source, "strategy_id"),
        venue=_required_string(source, "venue"),
        contract_hash=_required_string(source, "contract_hash"),
        source_artifact_filename=source_artifact_filename,
        source_artifact_sha256=source_artifact_sha256,
        source_verdict_base=source_verdict_base,
        source_verdict_sensitivity=source_verdict_sensitivity,
        verdict_base=apply_inconclusive_predicate(
            source_verdict_base,
            judgeable_years=judgeable_years,
            all_arms_empty=all_arms_empty,
        ),
        verdict_sensitivity=apply_inconclusive_predicate(
            source_verdict_sensitivity,
            judgeable_years=judgeable_years,
            all_arms_empty=all_arms_empty,
        ),
        judgeable_years=judgeable_years,
        fragile=fragile,
        fragile_evidence=fragile_evidence,
    )


def resynthesize_artifact_files(
    *,
    pair_sources: Mapping[str, Path],
    expected_pair_sha256: Mapping[str, str],
    manifest_sources: Mapping[str, Path],
    expected_manifest_sha256: Mapping[str, str],
) -> dict[str, Any]:
    """Resynthesize a SHA-bound set of pair records from separate directories.

    A correction can supersede only some candidate × venue records while other
    records remain in a separately sealed replay.  This helper binds each
    exact source path before JSON parsing, so that composition never copies,
    alters, or reruns any original arm artifact.
    """
    pair_hashes = _verify_source_files(pair_sources, expected_pair_sha256)
    manifest_hashes = _verify_source_files(manifest_sources, expected_manifest_sha256)

    pair_results = []
    for label in sorted(pair_sources):
        path = pair_sources[label]
        try:
            with path.open(encoding="utf-8") as handle:
                artifact = json.load(handle)
        except json.JSONDecodeError as exc:
            raise ArtifactSchemaError(
                f"source artifact is invalid JSON: {path}"
            ) from exc
        if not isinstance(artifact, Mapping):
            raise ArtifactSchemaError(f"source artifact must be a JSON object: {path}")
        pair_results.append(
            resynthesize_pair_artifact(
                artifact,
                source_artifact_filename=path.name,
                source_artifact_sha256=pair_hashes[label],
            ).to_dict()
        )

    return {
        "schema_version": _SCHEMA_VERSION,
        "resynthesis_only": True,
        "source_manifests": [
            {
                "label": label,
                "filename": manifest_sources[label].name,
                "sha256": manifest_hashes[label],
            }
            for label in sorted(manifest_sources)
        ],
        "source_artifact_sha256": {
            label: pair_hashes[label] for label in sorted(pair_hashes)
        },
        "records": pair_results,
    }


def resynthesize_artifact_directory(
    source_directory: Path,
    *,
    expected_sha256: Mapping[str, str] = EXPECTED_CR_S1_R2_SHA256,
) -> dict[str, Any]:
    """Hash-check fixed source bytes, then make a single no-engine result.

    The expected manifest and six pair artifacts are each required.  Parsing
    occurs only after all SHA-256 checks pass, so source drift cannot be
    mistaken for a new B2 result.
    """
    source_directory = source_directory.resolve()
    if _MANIFEST_FILENAME not in expected_sha256:
        raise ArtifactSchemaError("expected SHA table must include manifest.json")

    source_hashes: dict[str, str] = {}
    for filename, expected_digest in expected_sha256.items():
        path = source_directory / filename
        if not path.is_file():
            raise ArtifactSchemaError(f"required source artifact is missing: {path}")
        actual_digest = sha256_file(path)
        if actual_digest != expected_digest:
            raise ArtifactSchemaError(
                f"source SHA-256 mismatch for {filename}: expected {expected_digest}, "
                f"got {actual_digest}"
            )
        source_hashes[filename] = actual_digest

    pair_results = []
    for filename in sorted(
        name for name in expected_sha256 if name != _MANIFEST_FILENAME
    ):
        path = source_directory / filename
        try:
            artifact = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ArtifactSchemaError(
                f"source artifact is invalid JSON: {path}"
            ) from exc
        if not isinstance(artifact, Mapping):
            raise ArtifactSchemaError(f"source artifact must be a JSON object: {path}")
        pair_results.append(
            resynthesize_pair_artifact(
                artifact,
                source_artifact_filename=filename,
                source_artifact_sha256=source_hashes[filename],
            ).to_dict()
        )

    return {
        "schema_version": _SCHEMA_VERSION,
        "resynthesis_only": True,
        "source_manifest": {
            "filename": _MANIFEST_FILENAME,
            "sha256": source_hashes[_MANIFEST_FILENAME],
        },
        "source_artifact_sha256": {
            filename: source_hashes[filename] for filename in sorted(source_hashes)
        },
        "records": pair_results,
    }


def _normalize_source_artifact(artifact: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return the established B2 shape from a compact or replay record.

    The original R2 artifacts already carry source verdict and FRAGILE fields
    at top level.  The bounded B1/correction replay drivers deliberately
    preserve only arm evidence, so their equivalent source label and FRAGILE
    disclosure are mechanically reconstructed from the frozen pair summaries
    and per-year harness records.  This does not call the engine or create a
    new threshold.
    """
    if {
        "verdict_base",
        "verdict_sensitivity",
        "fragile",
        "harness_query",
    }.issubset(artifact):
        return artifact
    if "pair" not in artifact:
        raise ArtifactSchemaError(
            "source artifact must be a compact B2 artifact or a bounded replay record"
        )

    pair = _required_mapping(artifact, "pair")
    harness_query = _required_mapping(artifact, "harness_query")
    strategy_id = _required_string(artifact, "strategy_id")
    venue = _required_string(artifact, "venue")
    contract_hash = _required_string(pair, "contract_hash")
    _require_matching_pair_identity(
        pair,
        strategy_id=strategy_id,
        venue=venue,
        contract_hash=contract_hash,
    )
    _require_matching_harness_identity(
        harness_query,
        strategy_id=strategy_id,
        venue=venue,
        contract_hash=contract_hash,
    )

    full = _required_mapping(pair, "full")
    ablation = _required_mapping(pair, "ablation")
    return {
        "strategy_id": strategy_id,
        "venue": venue,
        "contract_hash": contract_hash,
        "verdict_base": _source_verdict(
            _optional_finite_number(full, "net_mean_return"),
            _optional_finite_number(ablation, "net_mean_return"),
        ),
        "verdict_sensitivity": _source_verdict(
            _optional_finite_number(full, "sensitivity_net_mean_return"),
            _optional_finite_number(ablation, "sensitivity_net_mean_return"),
        ),
        "fragile": _fragile_evidence_from_harness(
            _required_sequence(harness_query, "records"),
            full_net_mean_return=_optional_finite_number(full, "net_mean_return"),
        ),
        "harness_query": harness_query,
    }


def _require_matching_pair_identity(
    pair: Mapping[str, Any],
    *,
    strategy_id: str,
    venue: str,
    contract_hash: str,
) -> None:
    if _required_string(pair, "strategy_id") != strategy_id:
        raise ArtifactSchemaError("replay pair strategy_id does not match record")
    if _required_string(pair, "venue") != venue:
        raise ArtifactSchemaError("replay pair venue does not match record")
    if _required_string(pair, "contract_hash") != contract_hash:
        raise ArtifactSchemaError("replay pair contract_hash does not match record")


def _require_matching_harness_identity(
    harness_query: Mapping[str, Any],
    *,
    strategy_id: str,
    venue: str,
    contract_hash: str,
) -> None:
    if _required_string(harness_query, "strategy_id") != strategy_id:
        raise ArtifactSchemaError("harness strategy_id does not match record")
    if _required_string(harness_query, "venue") != venue:
        raise ArtifactSchemaError("harness venue does not match record")
    if _required_string(harness_query, "contract_hash") != contract_hash:
        raise ArtifactSchemaError("harness contract_hash does not match record")


def _source_verdict(
    full_net_mean_return: float | None, ablation_net_mean_return: float | None
) -> str:
    """Reconstruct the frozen per-venue label from existing pair summaries."""
    if full_net_mean_return is None or ablation_net_mean_return is None:
        return "INCONCLUSIVE_EMPTY_ARM"
    if full_net_mean_return <= 0.0:
        return "FALSIFIED_NONPOSITIVE_NET_RETURN"
    if full_net_mean_return - ablation_net_mean_return <= 0.0:
        return "FALSIFIED_NO_INCREMENT_OVER_ABLATION"
    return "PASS"


def _fragile_evidence_from_harness(
    records: Sequence[object], *, full_net_mean_return: float | None
) -> dict[str, Any]:
    """Reconstruct mandatory FRAGILE evidence from frozen annual full-arm rows."""
    contributing_years: list[tuple[int, int, float]] = []
    for record in records:
        if not isinstance(record, Mapping):
            raise ArtifactSchemaError("harness record must be an object")
        full = _required_mapping(record, "full")
        net_mean_return = _optional_finite_number(full, "net_mean_return")
        resolved_exit_count = full.get("resolved_exit_count")
        if net_mean_return is None:
            continue
        if (
            not isinstance(resolved_exit_count, int)
            or isinstance(resolved_exit_count, bool)
            or resolved_exit_count <= 0
        ):
            raise ArtifactSchemaError(
                "finite annual full net_mean_return requires positive resolved_exit_count"
            )
        calendar_year = full.get("calendar_year")
        if not isinstance(calendar_year, int) or isinstance(calendar_year, bool):
            raise ArtifactSchemaError("annual full calendar_year must be an int")
        contributing_years.append((calendar_year, resolved_exit_count, net_mean_return))

    if len(contributing_years) < 2 or full_net_mean_return is None:
        return {
            "state": "NOT_EVALUABLE",
            "fragile": False,
            "reason": "fewer than two contributing full-arm years",
        }

    total_count = sum(count for _, count, _ in contributing_years)
    weighted_full_mean = (
        sum(count * net_mean for _, count, net_mean in contributing_years) / total_count
    )
    if not math.isclose(
        weighted_full_mean, full_net_mean_return, rel_tol=0.0, abs_tol=1e-12
    ):
        raise ArtifactSchemaError(
            "annual full-arm rows do not reproduce pair full net_mean_return"
        )
    highest_year_net_mean_return = max(
        net_mean for _, _, net_mean in contributing_years
    )
    highest_years = tuple(
        year
        for year, _, net_mean in contributing_years
        if net_mean == highest_year_net_mean_return
    )
    remaining_net_mean_returns: dict[str, float] = {}
    for highest_year in highest_years:
        remaining = [
            (count, net_mean)
            for year, count, net_mean in contributing_years
            if year != highest_year
        ]
        remaining_count = sum(count for count, _ in remaining)
        if remaining_count == 0:
            return {
                "state": "NOT_EVALUABLE",
                "fragile": False,
                "reason": "no full-arm observations remain after highest year removal",
            }
        remaining_net_mean_returns[str(highest_year)] = (
            sum(count * net_mean for count, net_mean in remaining) / remaining_count
        )

    return {
        "state": "EVALUATED",
        "fragile": full_net_mean_return > 0.0
        and any(value <= 0.0 for value in remaining_net_mean_returns.values()),
        "highest_year_net_mean_return": highest_year_net_mean_return,
        "highest_years": list(highest_years),
        "remaining_net_mean_returns": remaining_net_mean_returns,
    }


def _verify_source_files(
    sources: Mapping[str, Path], expected_sha256: Mapping[str, str]
) -> dict[str, str]:
    if set(sources) != set(expected_sha256):
        raise ArtifactSchemaError(
            "source paths and expected SHA-256 table must have exactly the same labels"
        )
    hashes: dict[str, str] = {}
    for label in sorted(sources):
        path = sources[label]
        if not path.is_file():
            raise ArtifactSchemaError(f"required source artifact is missing: {path}")
        actual_digest = sha256_file(path)
        if actual_digest != expected_sha256[label]:
            raise ArtifactSchemaError(
                f"source SHA-256 mismatch for {label}: expected "
                f"{expected_sha256[label]}, got {actual_digest}"
            )
        hashes[label] = actual_digest
    return hashes


def canonical_json_bytes(payload: object) -> bytes:
    """Render stable bytes suitable for a published evidence artifact."""
    return json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def sha256_file(path: Path) -> str:
    """Return a file's SHA-256 without loading the whole artifact at once."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def write_json_once(path: Path, payload: Mapping[str, Any]) -> str:
    """Atomically publish a new result and refuse to replace an existing one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = canonical_json_bytes(payload)
    digest = hashlib.sha256(rendered).hexdigest()
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.partial"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise FileExistsError(
                f"refusing to overwrite completed resynthesis artifact: {path}"
            ) from exc
        finally:
            temporary.unlink(missing_ok=True)
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return digest


def _is_judgeable_year(record: object) -> bool:
    if not isinstance(record, Mapping):
        raise ArtifactSchemaError("harness record must be an object")
    incremental = _required_mapping(record, "incremental")
    # Count only a complete full-versus-ablation comparison.  Counting calendar
    # years or a year where either arm is empty would invent the very FRAGILE
    # result that the frozen contract says must be calculated, not assumed.
    return (
        _is_finite_number(incremental.get("full_net_mean_return"))
        and _is_finite_number(incremental.get("ablation_net_mean_return"))
        and _is_finite_number(incremental.get("full_minus_ablation_net_mean_return"))
    )


def _all_arms_empty(records: Sequence[object]) -> bool:
    """Whether neither of the two arms has any annual net-return output."""
    for record in records:
        if not isinstance(record, Mapping):
            raise ArtifactSchemaError("harness record must be an object")
        incremental = _required_mapping(record, "incremental")
        if _is_finite_number(incremental.get("full_net_mean_return")):
            return False
        if _is_finite_number(incremental.get("ablation_net_mean_return")):
            return False
    return True


def _required_mapping(container: Mapping[str, Any], field: str) -> Mapping[str, Any]:
    value = container.get(field)
    if not isinstance(value, Mapping):
        raise ArtifactSchemaError(f"{field} must be an object")
    return value


def _required_sequence(container: Mapping[str, Any], field: str) -> Sequence[object]:
    value = container.get(field)
    if not isinstance(value, list):
        raise ArtifactSchemaError(f"{field} must be an array")
    return value


def _required_string(container: Mapping[str, Any], field: str) -> str:
    value = container.get(field)
    if not isinstance(value, str) or not value:
        raise ArtifactSchemaError(f"{field} must be a non-empty string")
    return value


def _optional_finite_number(container: Mapping[str, Any], field: str) -> float | None:
    value = container.get(field)
    if value is None:
        return None
    if not _is_finite_number(value):
        raise ArtifactSchemaError(f"{field} must be a finite number or null")
    return float(value)


def _is_finite_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )

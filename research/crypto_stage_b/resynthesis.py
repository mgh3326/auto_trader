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
    records = _required_sequence(
        _required_mapping(artifact, "harness_query"), "records"
    )
    judgeable_years = sum(_is_judgeable_year(record) for record in records)
    all_arms_empty = _all_arms_empty(records)
    fragile_evidence = _required_mapping(artifact, "fragile")
    fragile = fragile_evidence.get("fragile")
    if not isinstance(fragile, bool):
        raise ArtifactSchemaError("fragile.fragile must be a bool")

    source_verdict_base = _required_string(artifact, "verdict_base")
    source_verdict_sensitivity = _required_string(artifact, "verdict_sensitivity")
    return TopLevelResynthesis(
        strategy_id=_required_string(artifact, "strategy_id"),
        venue=_required_string(artifact, "venue"),
        contract_hash=_required_string(artifact, "contract_hash"),
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


def _is_finite_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )

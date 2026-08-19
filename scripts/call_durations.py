"""Deterministic call-phase-only duration telemetry with freshness validation.

ROB-1295. This is strictly additive to ``.test_durations``:

* ``.test_durations`` keeps being produced exactly as before by pytest-split's
  own ``--store-durations``/``--durations-path`` mechanism, and keeps driving
  shard balancing exactly as before (``scripts/merge_test_durations.py`` is
  unchanged). No consumer of that file requires any change.
* This module instead builds a new sidecar artifact (conventionally
  ``.call_durations.json``) from call-phase-only measurements captured by
  ``tests/_call_duration_plugin.py``, and validates that a candidate/
  committed artifact is still trustworthy for the current tree.

Two operations, deliberately asymmetric:

* ``build`` merges per-shard call-phase measurements into a single artifact
  scoped to exactly the currently collected test set (fail-closed on
  duplicate/missing/malformed input — there is no "stale" concept when
  building fresh from the current shard measurements).
* ``validate`` checks an already-serialized artifact against the *current*
  repository state (collected node ids, HEAD commit sha) and fails closed on
  any drift: duplicate node ids, missing node ids, stale/removed node ids,
  a source-commit mismatch, a collection-hash mismatch, or a malformed
  duration value. Unlike pytest-split's tolerant shard-balancing hint, this
  artifact is meant to be trustworthy provenance data, so drift is a hard
  failure rather than something to silently drop and continue past.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any

from scripts.merge_test_durations import _load_collected_nodes, _load_durations

SCHEMA_VERSION = 1

_REQUIRED_ARTIFACT_FIELDS = (
    "schema_version",
    "source_commit_sha",
    "collection_hash",
    "durations",
)


def compute_collection_hash(collected_nodes: set[str] | frozenset[str]) -> str:
    joined = "\n".join(sorted(collected_nodes))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def merge_call_duration_shards(
    *, shard_paths: list[Path], collected_paths: list[Path]
) -> tuple[dict[str, float], set[str]]:
    """Merge call-phase shard measurements into one fresh, complete map."""
    collected = _load_collected_nodes(collected_paths)
    measured: dict[str, float] = {}

    for shard_path in shard_paths:
        shard = _load_durations(shard_path)
        unexpected = sorted(set(shard) - collected)
        if unexpected:
            raise ValueError(
                f"{shard_path}: call durations contain uncollected tests: "
                f"{unexpected[:10]!r}"
            )
        duplicates = sorted(set(shard) & set(measured))
        if duplicates:
            raise ValueError(
                f"{shard_path}: duplicate node id(s) across call-duration "
                f"shards: {duplicates[:10]!r}"
            )
        measured.update(shard)

    missing = sorted(collected - set(measured))
    if missing:
        raise ValueError(
            "call-duration shards are incomplete; "
            f"{len(missing)} collected tests have no call-phase measurement: "
            f"{missing[:20]!r}"
        )

    return measured, collected


def build_artifact(
    *,
    shard_paths: list[Path],
    collected_paths: list[Path],
    source_commit_sha: str,
) -> dict[str, Any]:
    if not source_commit_sha or not source_commit_sha.strip():
        raise ValueError("source_commit_sha must not be empty")

    measured, collected = merge_call_duration_shards(
        shard_paths=shard_paths, collected_paths=collected_paths
    )
    durations = {node_id: measured[node_id] for node_id in sorted(collected)}
    return {
        "schema_version": SCHEMA_VERSION,
        "source_commit_sha": source_commit_sha,
        "collection_hash": compute_collection_hash(collected),
        "node_count": len(durations),
        "durations": durations,
    }


def serialize_artifact(artifact: dict[str, Any]) -> str:
    return json.dumps(artifact, sort_keys=True, indent=2) + "\n"


class DuplicateArtifactKeyError(ValueError):
    """Raised when the artifact JSON itself contains a duplicate key."""


def _no_duplicate_pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise DuplicateArtifactKeyError(f"duplicate key in artifact JSON: {key!r}")
        seen[key] = value
    return seen


def load_artifact(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    try:
        raw = json.loads(text, object_pairs_hook=_no_duplicate_pairs_hook)
    except DuplicateArtifactKeyError as error:
        raise ValueError(f"{path}: {error}") from error

    if not isinstance(raw, dict):
        raise ValueError(f"{path}: expected a JSON object")
    for field in _REQUIRED_ARTIFACT_FIELDS:
        if field not in raw:
            raise ValueError(f"{path}: missing required field {field!r}")
    if not isinstance(raw["durations"], dict):
        raise ValueError(f"{path}: 'durations' must be a JSON object")
    return raw


def _validate_duration_values(
    durations: dict[str, Any], *, source: str
) -> dict[str, float]:
    validated: dict[str, float] = {}
    for node_id, duration in durations.items():
        if (
            not isinstance(node_id, str)
            or isinstance(duration, bool)
            or not isinstance(duration, int | float)
        ):
            raise ValueError(f"{source}: invalid duration entry {node_id!r}")
        value = float(duration)
        if value < 0 or not math.isfinite(value):
            raise ValueError(f"{source}: invalid duration for {node_id}: {duration!r}")
        validated[node_id] = value
    return validated


def validate_freshness(
    artifact: dict[str, Any],
    *,
    collected_nodes: set[str],
    expected_source_commit_sha: str,
) -> None:
    """Fail closed unless ``artifact`` is trustworthy telemetry for today's tree."""
    source = "call-duration artifact"
    durations = _validate_duration_values(artifact["durations"], source=source)

    if artifact["source_commit_sha"] != expected_source_commit_sha:
        raise ValueError(
            f"{source}: source commit sha mismatch: "
            f"artifact={artifact['source_commit_sha']!r} "
            f"expected={expected_source_commit_sha!r} "
            "— refresh the call-duration artifact"
        )

    expected_hash = compute_collection_hash(collected_nodes)
    if artifact["collection_hash"] != expected_hash:
        raise ValueError(
            f"{source}: collection hash mismatch: "
            f"artifact={artifact['collection_hash']!r} expected={expected_hash!r} "
            "— refresh the call-duration artifact"
        )

    missing = sorted(collected_nodes - set(durations))
    if missing:
        raise ValueError(
            f"{source}: missing measurement(s) for {len(missing)} currently "
            f"collected test(s): {missing[:20]!r} "
            "— refresh the call-duration artifact"
        )

    stale = sorted(set(durations) - collected_nodes)
    if stale:
        raise ValueError(
            f"{source}: {len(stale)} stale/removed node id(s) present: "
            f"{stale[:20]!r} — refresh the call-duration artifact"
        )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    build_parser = subparsers.add_parser(
        "build", help="merge shard measurements into a fresh call-duration artifact"
    )
    build_parser.add_argument("--shard", type=Path, action="append", required=True)
    build_parser.add_argument("--collected", type=Path, action="append", required=True)
    build_parser.add_argument("--source-commit-sha", type=str, required=True)
    build_parser.add_argument("--output", type=Path, required=True)

    validate_parser = subparsers.add_parser(
        "validate", help="validate a call-duration artifact against the current tree"
    )
    validate_parser.add_argument("--artifact", type=Path, required=True)
    validate_parser.add_argument(
        "--collected", type=Path, action="append", required=True
    )
    validate_parser.add_argument("--expected-source-sha", type=str, required=True)

    return parser.parse_args()


def main() -> None:
    args = _parse_args()

    if args.command == "build":
        artifact = build_artifact(
            shard_paths=args.shard,
            collected_paths=args.collected,
            source_commit_sha=args.source_commit_sha,
        )
        args.output.write_text(serialize_artifact(artifact), encoding="utf-8")
        print(
            f"built call-duration artifact: {artifact['node_count']} entries, "
            f"source_commit_sha={artifact['source_commit_sha']}, "
            f"collection_hash={artifact['collection_hash']}"
        )
        return

    if args.command == "validate":
        artifact = load_artifact(args.artifact)
        collected = _load_collected_nodes(args.collected)
        validate_freshness(
            artifact,
            collected_nodes=collected,
            expected_source_commit_sha=args.expected_source_sha,
        )
        print(
            f"call-duration artifact is fresh: {len(collected)} entries match "
            f"collection_hash={artifact['collection_hash']} at "
            f"source_commit_sha={artifact['source_commit_sha']}"
        )
        return


if __name__ == "__main__":
    main()

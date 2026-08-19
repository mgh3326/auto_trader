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

ROB-1295 R1 — collected-but-never-called nodes: a node whose ``setup`` phase
itself skips (``@pytest.mark.skip``, ``skipif``, a skip-raising fixture, ...)
is collected but never reaches the ``call`` phase at all — pytest's own
runtest protocol only invokes ``call`` after ``setup`` passes. Such nodes
have no call-phase cost to measure, so ``tests/_call_duration_plugin.py``
records them separately in ``not_called`` rather than either (a) silently
omitting them, which this module would otherwise fail closed on as a missing
measurement, or (b) coercing them to a ``0.0`` duration, which would be
indistinguishable from a real, very-fast call and would corrupt any later
statistics over ``durations``. Every currently collected node must land in
exactly one of ``durations`` or ``not_called`` — never both, never neither.
A node that skips *inside* its call phase (e.g. ``pytest.skip()`` called
from the test body) is unaffected: pytest still emits a real ``call`` report
with a real duration, so it is captured in ``durations`` as usual.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any

from scripts.merge_test_durations import _load_collected_nodes

SCHEMA_VERSION = 2

_REQUIRED_ARTIFACT_FIELDS = (
    "schema_version",
    "source_commit_sha",
    "collection_hash",
    "durations",
    "not_called",
)


def compute_collection_hash(collected_nodes: set[str] | frozenset[str]) -> str:
    joined = "\n".join(sorted(collected_nodes))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


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


def _validate_not_called_list(raw: Any, *, source: str) -> set[str]:
    if not isinstance(raw, list) or not all(
        isinstance(node_id, str) for node_id in raw
    ):
        raise ValueError(f"{source}: 'not_called' must be a JSON array of strings")
    return set(raw)


def _load_call_duration_shard(path: Path) -> tuple[dict[str, float], set[str]]:
    """Load one shard's ``{"durations": {...}, "not_called": [...]}`` output."""
    raw: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or "durations" not in raw or "not_called" not in raw:
        raise ValueError(
            f"{path}: expected an object with 'durations' and 'not_called' keys"
        )
    if not isinstance(raw["durations"], dict):
        raise ValueError(f"{path}: 'durations' must be a JSON object")

    durations = _validate_duration_values(raw["durations"], source=str(path))
    not_called = _validate_not_called_list(raw["not_called"], source=str(path))

    overlap = sorted(set(durations) & not_called)
    if overlap:
        raise ValueError(
            f"{path}: node id(s) present in both 'durations' and 'not_called': "
            f"{overlap[:10]!r}"
        )
    return durations, not_called


def merge_call_duration_shards(
    *, shard_paths: list[Path], collected_paths: list[Path]
) -> tuple[dict[str, float], set[str], set[str]]:
    """Merge call-phase shard measurements into one fresh, complete record.

    Returns ``(durations, not_called, collected)``. Every id in
    ``collected`` lands in exactly one of ``durations``/``not_called``;
    anything else (missing from both, or duplicated across shards in either
    category) is a fail-closed error.
    """
    collected = _load_collected_nodes(collected_paths)
    measured: dict[str, float] = {}
    not_called: set[str] = set()

    for shard_path in shard_paths:
        shard_durations, shard_not_called = _load_call_duration_shard(shard_path)

        shard_known = set(shard_durations) | shard_not_called
        unexpected = sorted(shard_known - collected)
        if unexpected:
            raise ValueError(
                f"{shard_path}: call durations contain uncollected tests: "
                f"{unexpected[:10]!r}"
            )

        already_known = set(measured) | not_called
        duplicates = sorted(shard_known & already_known)
        if duplicates:
            raise ValueError(
                f"{shard_path}: duplicate node id(s) across call-duration "
                f"shards: {duplicates[:10]!r}"
            )

        measured.update(shard_durations)
        not_called.update(shard_not_called)

    missing = sorted(collected - (set(measured) | not_called))
    if missing:
        raise ValueError(
            "call-duration shards are incomplete; "
            f"{len(missing)} collected tests have no call-phase measurement "
            f"or setup-skip record: {missing[:20]!r}"
        )

    return measured, not_called, collected


def build_artifact(
    *,
    shard_paths: list[Path],
    collected_paths: list[Path],
    source_commit_sha: str,
) -> dict[str, Any]:
    if not source_commit_sha or not source_commit_sha.strip():
        raise ValueError("source_commit_sha must not be empty")

    measured, not_called, collected = merge_call_duration_shards(
        shard_paths=shard_paths, collected_paths=collected_paths
    )
    durations = {node_id: measured[node_id] for node_id in sorted(measured)}
    not_called_sorted = sorted(not_called)
    return {
        "schema_version": SCHEMA_VERSION,
        "source_commit_sha": source_commit_sha,
        "collection_hash": compute_collection_hash(collected),
        "node_count": len(durations) + len(not_called_sorted),
        "durations": durations,
        "not_called": not_called_sorted,
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
    _validate_not_called_list(raw["not_called"], source=str(path))
    return raw


def validate_freshness(
    artifact: dict[str, Any],
    *,
    collected_nodes: set[str],
    expected_source_commit_sha: str,
) -> None:
    """Fail closed unless ``artifact`` is trustworthy telemetry for today's tree."""
    source = "call-duration artifact"
    durations = _validate_duration_values(artifact["durations"], source=source)
    not_called = _validate_not_called_list(
        artifact.get("not_called", []), source=source
    )

    overlap = sorted(set(durations) & not_called)
    if overlap:
        raise ValueError(
            f"{source}: node id(s) present in both 'durations' and "
            f"'not_called': {overlap[:10]!r}"
        )

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

    known = set(durations) | not_called
    missing = sorted(collected_nodes - known)
    if missing:
        raise ValueError(
            f"{source}: missing measurement(s) or setup-skip record(s) for "
            f"{len(missing)} currently collected test(s): {missing[:20]!r} "
            "— refresh the call-duration artifact"
        )

    stale = sorted(known - collected_nodes)
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
            f"built call-duration artifact: {len(artifact['durations'])} measured, "
            f"{len(artifact['not_called'])} not-called, "
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

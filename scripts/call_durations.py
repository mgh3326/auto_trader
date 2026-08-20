"""Deterministic call-phase-only duration telemetry with freshness validation.

ROB-1295. This is strictly additive to ``.test_durations``:

* ``.test_durations`` keeps being produced by pytest-split's own
  ``--store-durations``/``--durations-path`` mechanism in the weekly
  duration-refresh workflow, unchanged. As of ROB-1312 it no longer drives
  *runtime* shard selection in ``test.yml`` -- the four core shards now run
  fixed, committed ``ci_shards/shard-{1..4}.txt`` file manifests
  (``scripts/ci/file_shard_plan.py``) instead of a ``pytest-split
  --splits/--group`` selection -- so it remains a plain telemetry record,
  not a shard-balancing input. ``scripts/merge_test_durations.py`` itself
  changed too: it now enforces a disjoint-per-shard-collection contract
  instead of "all shard manifests are identical" (see that module's
  docstring for the full contract this module reuses).
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
  repository state (collected node ids, expected source-commit sha) and
  fails closed on any drift: duplicate node ids, missing node ids,
  stale/removed node ids, a source-commit mismatch, a collection-hash
  mismatch, an unsupported schema version, or a malformed duration value.
  Unlike pytest-split's tolerant shard-balancing hint, this artifact is
  meant to be trustworthy provenance data, so drift is a hard failure
  rather than something to silently drop and continue past.

``source_commit_sha`` is the sha of the commit the four shards *measured*
(the run's ``github.sha``), not necessarily the commit that ends up
containing the resulting ``.call_durations.json`` file — the weekly refresh
workflow's own self-validate step compares against that same measured sha
(consistent by construction), but ``peter-evans/create-pull-request`` then
commits the file as a new commit on ``ci/test-durations-refresh``, so
``HEAD`` moves past ``source_commit_sha`` the moment that auto-PR merges.
A future consumer must compare against the measured-tree sha it expects,
not assume it equals the artifact's own containing commit.

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

ROB-1295 R2 (post-verify hardening): shard files and artifact files are both
parsed with duplicate-JSON-key rejection (a corrupt/hand-edited file cannot
silently drop data via Python's last-key-wins ``dict`` construction);
``schema_version`` is checked, not just required-present; duplicate entries
inside ``not_called`` are rejected instead of silently deduplicated by
``set()``; and ``node_count`` is cross-checked against the actual
``durations``/``not_called`` sizes rather than trusted as a free-form label.

ROB-1312: the four shards measuring these call-phase durations now run
*disjoint* committed file manifests instead of each independently
collecting/selecting from the whole suite (see
``scripts/merge_test_durations.py`` module docstring for the full disjoint
contract this reuses -- ``build`` requires each shard's
``durations ∪ not_called`` to equal that shard's own collected-node manifest
exactly, the four shard collections to be pairwise disjoint, and their union
to equal a separately captured, independent authoritative collection).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any

from scripts.merge_test_durations import (
    _check_shard_measured_matches_collected,
    load_node_manifest,
    validate_disjoint_shard_collections,
)

SCHEMA_VERSION = 2

_REQUIRED_ARTIFACT_FIELDS = (
    "schema_version",
    "source_commit_sha",
    "collection_hash",
    "node_count",
    "durations",
    "not_called",
)


def compute_collection_hash(collected_nodes: set[str] | frozenset[str]) -> str:
    joined = "\n".join(sorted(collected_nodes))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


class DuplicateJsonKeyError(ValueError):
    """Raised when a JSON object (top-level or nested) has a duplicate key."""


def _no_duplicate_pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise DuplicateJsonKeyError(f"duplicate key in JSON object: {key!r}")
        seen[key] = value
    return seen


def _load_json_no_duplicates(path: Path) -> Any:
    """Parse JSON, fail-closed on any duplicate key at any nesting level."""
    try:
        return json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_no_duplicate_pairs_hook
        )
    except DuplicateJsonKeyError as error:
        raise ValueError(f"{path}: {error}") from error


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
    if len(raw) != len(set(raw)):
        duplicates = sorted({node_id for node_id in raw if raw.count(node_id) > 1})
        raise ValueError(
            f"{source}: 'not_called' contains duplicate entries: {duplicates[:10]!r}"
        )
    return set(raw)


def _validate_schema_version(raw: Any, *, source: str) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int) or raw != SCHEMA_VERSION:
        raise ValueError(
            f"{source}: unsupported schema_version {raw!r}; expected {SCHEMA_VERSION!r}"
        )
    return raw


def _load_call_duration_shard(path: Path) -> tuple[dict[str, float], set[str]]:
    """Load one shard's ``{"durations": {...}, "not_called": [...]}`` output."""
    raw: Any = _load_json_no_duplicates(path)
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
    *,
    shard_paths: list[Path],
    collected_paths: list[Path],
    authoritative_path: Path,
) -> tuple[dict[str, float], set[str], set[str]]:
    """Merge call-phase shard measurements into one fresh, complete record.

    ``shard_paths[i]`` and ``collected_paths[i]`` must describe the same
    shard, in the same order. Returns ``(durations, not_called,
    authoritative)``. Every id in ``authoritative`` lands in exactly one of
    ``durations``/``not_called``, attributed to the one shard that actually
    collected it; a measurement for a node a shard did not collect (even if
    another shard did), a gap no shard collected, or a shard collection not
    disjoint from another is a fail-closed error.
    """
    if len(shard_paths) != len(collected_paths):
        raise ValueError(
            "shard_paths and collected_paths must have matching length/order"
        )

    per_shard_collected = [set(load_node_manifest(p)) for p in collected_paths]
    authoritative = set(load_node_manifest(authoritative_path))

    validate_disjoint_shard_collections(
        per_shard_paths=collected_paths,
        per_shard_collected=per_shard_collected,
        authoritative_path=authoritative_path,
        authoritative=authoritative,
    )

    measured: dict[str, float] = {}
    not_called: set[str] = set()

    for shard_path, collected in zip(shard_paths, per_shard_collected, strict=True):
        shard_durations, shard_not_called = _load_call_duration_shard(shard_path)
        shard_known = set(shard_durations) | shard_not_called
        _check_shard_measured_matches_collected(
            shard_path=shard_path, measured_known=shard_known, collected=collected
        )
        measured.update(shard_durations)
        not_called.update(shard_not_called)

    return measured, not_called, authoritative


def build_artifact(
    *,
    shard_paths: list[Path],
    collected_paths: list[Path],
    authoritative_path: Path,
    source_commit_sha: str,
) -> dict[str, Any]:
    if not source_commit_sha or not source_commit_sha.strip():
        raise ValueError("source_commit_sha must not be empty")

    measured, not_called, collected = merge_call_duration_shards(
        shard_paths=shard_paths,
        collected_paths=collected_paths,
        authoritative_path=authoritative_path,
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


def load_artifact(path: Path) -> dict[str, Any]:
    raw = _load_json_no_duplicates(path)

    if not isinstance(raw, dict):
        raise ValueError(f"{path}: expected a JSON object")
    for field in _REQUIRED_ARTIFACT_FIELDS:
        if field not in raw:
            raise ValueError(f"{path}: missing required field {field!r}")
    if not isinstance(raw["durations"], dict):
        raise ValueError(f"{path}: 'durations' must be a JSON object")
    _validate_schema_version(raw["schema_version"], source=str(path))
    _validate_not_called_list(raw["not_called"], source=str(path))
    return raw


def validate_artifact_structure(
    artifact: dict[str, Any], *, source: str = "call-duration artifact"
) -> tuple[dict[str, float], set[str]]:
    """Structural validation shared by ``validate_freshness`` and consumers
    (e.g. the file-shard planner, ROB-1312) that only need trustworthy
    ``durations``/``not_called`` values -- not a freshness check against
    today's collection. Independent of ``load_artifact``: also re-validates
    duration value types/finiteness and the durations/not_called overlap,
    neither of which ``load_artifact`` alone checks.
    """
    _validate_schema_version(artifact.get("schema_version"), source=source)
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

    expected_node_count = len(durations) + len(not_called)
    if artifact.get("node_count") != expected_node_count:
        raise ValueError(
            f"{source}: node_count mismatch: artifact={artifact.get('node_count')!r} "
            f"expected={expected_node_count!r} (len(durations) + len(not_called)) "
            "— refresh the call-duration artifact"
        )

    return durations, not_called


def validate_freshness(
    artifact: dict[str, Any],
    *,
    collected_nodes: set[str],
    expected_source_commit_sha: str,
) -> None:
    """Fail closed unless ``artifact`` is trustworthy telemetry for today's tree."""
    source = "call-duration artifact"
    durations, not_called = validate_artifact_structure(artifact, source=source)

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
    build_parser.add_argument(
        "--collected",
        type=Path,
        action="append",
        required=True,
        help="per-shard collected-node manifest, same order as --shard",
    )
    build_parser.add_argument(
        "--authoritative",
        type=Path,
        required=True,
        help="independent full-suite `pytest --collect-only` node-id capture",
    )
    build_parser.add_argument("--source-commit-sha", type=str, required=True)
    build_parser.add_argument("--output", type=Path, required=True)

    validate_parser = subparsers.add_parser(
        "validate", help="validate a call-duration artifact against the current tree"
    )
    validate_parser.add_argument("--artifact", type=Path, required=True)
    validate_parser.add_argument(
        "--authoritative",
        type=Path,
        required=True,
        help=(
            "independent full-suite `pytest --collect-only` node-id capture "
            "-- the single authoritative reference this artifact is checked "
            "against, unrelated to how it was sharded when built"
        ),
    )
    validate_parser.add_argument("--expected-source-sha", type=str, required=True)

    return parser.parse_args()


def main() -> None:
    args = _parse_args()

    if args.command == "build":
        artifact = build_artifact(
            shard_paths=args.shard,
            collected_paths=args.collected,
            authoritative_path=args.authoritative,
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
        collected = set(load_node_manifest(args.authoritative))
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

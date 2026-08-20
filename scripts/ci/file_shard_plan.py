#!/usr/bin/env python3
"""Deterministic file-shard manifests + exact-cover guard (ROB-1312).

Replaces runtime ``pytest-split --splits/--group`` shard selection in
``.github/workflows/test.yml`` / ``test-durations-refresh.yml`` with four
committed, version-controlled test-file manifests (``ci_shards/shard-{1..4}.txt``).
Each manifest is a newline-delimited list of test file paths (relative to the
repo root, under ``tests/``). Every file collected by the authoritative
``pytest --collect-only -m "not live" tests/`` run must appear in **exactly
one** manifest -- this module both *generates* that assignment and *checks*
it.

Two subcommands, deliberately asymmetric:

* ``generate`` -- reads ``.call_durations.json`` (ROB-1295 call-phase
  telemetry) and a fresh authoritative collect-only node manifest, sums
  per-node weight into per-file weight, and writes four manifests via a
  deterministic largest-processing-time-first (LPT) bin-packing. Overwrites
  whatever manifests are already on disk.
* ``check`` -- reads the four committed manifests plus a fresh authoritative
  collect-only node manifest and validates exact cover: every authoritative
  file in exactly one manifest, no duplicates (within or across manifests),
  no stale/unexpected entries, no empty shard. Never writes anything. Fails
  closed (non-zero exit, actionable message with the regeneration command)
  on any violation.

Weight fallbacks (explicit, deterministic -- see ``NOT_CALLED_FALLBACK_SECONDS``
and ``_unmeasured_fallback_seconds``):

* A node present in the artifact's ``not_called`` list (its ``setup`` phase
  skipped before reaching ``call``) contributes ``0.0`` -- it has no
  call-phase cost to measure, by construction.
* A node collected today but absent from *both* ``durations`` and
  ``not_called`` (a brand-new test added since the artifact was last
  refreshed, or genuine drift) contributes the mean of all measured
  durations in that same artifact -- a deterministic, reproducible function
  of the committed input, not a hand-tuned magic constant. If the artifact
  has zero measured durations (an empty/bootstrap artifact), the fallback is
  ``UNMEASURED_FALLBACK_DEFAULT_SECONDS``.

Determinism: file weights are summed by iterating the authoritative node list
in sorted order (fixed float-addition order -- reproducible across runs and
platforms), shard assignment sorts files by ``(-weight, path)`` and breaks
ties on the running shard totals by ``(total, shard_index)``. Regenerating
twice from the same inputs produces byte-identical manifests.

The ``ci_shards/*.txt`` manifests are also consumed directly by
``.github/workflows/test.yml`` to build ``pytest`` argv (via a ``mapfile``
read, never shell word-splitting), so ``_load_shard_manifest`` enforces a
stricter format than the internal node-id manifests this module also reads:
no blank/whitespace-padded lines, no absolute paths, no ``..`` traversal, and
every entry must be a ``tests/**/*.py`` path.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from scripts.call_durations import load_artifact as load_call_durations_artifact
from scripts.call_durations import (
    validate_artifact_provenance,
    validate_artifact_structure,
)

#: Nodes recorded in the call-duration artifact's ``not_called`` list never
#: reach the ``call`` phase (their ``setup`` skipped first) -- there is no
#: call-phase cost to attribute, so their weight contribution is exactly
#: zero rather than an estimate.
NOT_CALLED_FALLBACK_SECONDS = 0.0

#: Fallback per-node weight (seconds) used only when the call-duration
#: artifact itself has zero measured durations (empty/bootstrap artifact) --
#: normally unreachable once the artifact has been refreshed at least once.
UNMEASURED_FALLBACK_DEFAULT_SECONDS = 1.0

DEFAULT_SHARD_COUNT = 4

_MANIFEST_FILENAME = "shard-{index}.txt"
_WEIGHTS_FILENAME = "weights.json"
_WEIGHTS_SCHEMA_VERSION = 1


class ShardPlanError(ValueError):
    """Malformed input or a validation failure. Always fail-closed."""


# --------------------------------------------------------------------------
# Node-id manifests (collect-only output; internal plumbing input)
# --------------------------------------------------------------------------


def load_collected_node_manifest(path: Path) -> list[str]:
    """Read a newline-delimited pytest node-id manifest.

    Rejects blank lines and duplicate node ids -- the caller (a
    ``pytest --collect-only`` capture) must not be pre-deduplicated (e.g.
    with ``sort -u``) before reaching this function, or a genuine duplicate
    collection would be silently erased before it could be caught here.
    """
    raw_lines = path.read_text(encoding="utf-8").splitlines()
    lines = [line.strip() for line in raw_lines]
    if any(not line for line in lines):
        raise ShardPlanError(f"{path}: blank line(s) not allowed in a node-id manifest")
    if not lines:
        raise ShardPlanError(f"{path}: node-id manifest must not be empty")
    counts = Counter(lines)
    duplicates = sorted(node for node, count in counts.items() if count > 1)
    if duplicates:
        raise ShardPlanError(
            f"{path}: duplicate node id(s) in manifest: {duplicates[:10]!r}"
        )
    return lines


def file_of_node_id(node_id: str) -> str:
    """Project a pytest node id onto its containing test file path."""
    if "::" not in node_id:
        raise ShardPlanError(f"not a valid pytest node id (missing '::'): {node_id!r}")
    file_path = node_id.split("::", 1)[0]
    if not file_path.startswith("tests/") or not file_path.endswith(".py"):
        raise ShardPlanError(
            f"node id file path is not a tests/**/*.py path: {node_id!r}"
        )
    return file_path


def collected_files_from_nodes(node_ids: list[str]) -> set[str]:
    return {file_of_node_id(node_id) for node_id in node_ids}


# --------------------------------------------------------------------------
# Weight computation
# --------------------------------------------------------------------------


def _unmeasured_fallback_seconds(durations: dict[str, float]) -> float:
    if not durations:
        return UNMEASURED_FALLBACK_DEFAULT_SECONDS
    # Deterministic summation order: iterate keys sorted, not dict/set order.
    total = 0.0
    for node_id in sorted(durations):
        total += durations[node_id]
    return total / len(durations)


def compute_file_weights(
    *,
    collected_node_ids: list[str],
    durations: dict[str, float],
    not_called: set[str],
) -> dict[str, float]:
    """Sum per-node weight into per-file weight.

    ``collected_node_ids`` must be the authoritative node list; weights are
    accumulated by iterating it in sorted order so float summation is
    reproducible regardless of dict/set iteration order upstream.
    """
    fallback = _unmeasured_fallback_seconds(durations)
    weights: dict[str, float] = {}
    for node_id in sorted(collected_node_ids):
        file_path = file_of_node_id(node_id)
        if node_id in durations:
            weight = durations[node_id]
        elif node_id in not_called:
            weight = NOT_CALLED_FALLBACK_SECONDS
        else:
            weight = fallback
        weights[file_path] = weights.get(file_path, 0.0) + weight
    return weights


# --------------------------------------------------------------------------
# Deterministic LPT bin-packing
# --------------------------------------------------------------------------


def assign_files_to_shards(
    file_weights: dict[str, float], shard_count: int
) -> tuple[list[list[str]], list[float]]:
    """Largest-processing-time-first, deterministic tie-breaks throughout.

    Files are visited in descending weight order, ties broken by ascending
    path. Each file goes to the shard with the smallest running total so
    far, ties broken by the lowest shard index. Both tie-breaks are encoded
    directly in sort/min keys, not relied upon via iteration-order
    stability.
    """
    if shard_count < 1:
        raise ShardPlanError(f"shard_count must be >= 1, got {shard_count}")

    ordered_files = sorted(file_weights, key=lambda f: (-file_weights[f], f))
    totals = [0.0] * shard_count
    shards: list[list[str]] = [[] for _ in range(shard_count)]
    for file_path in ordered_files:
        shard_index = min(range(shard_count), key=lambda i: (totals[i], i))
        shards[shard_index].append(file_path)
        totals[shard_index] += file_weights[file_path]
    return shards, totals


# --------------------------------------------------------------------------
# Committed shard-manifest I/O (ci_shards/shard-N.txt)
# --------------------------------------------------------------------------


def _manifest_path(manifest_dir: Path, index: int) -> Path:
    return manifest_dir / _MANIFEST_FILENAME.format(index=index)


def render_shard_manifest(files: list[str]) -> str:
    return "".join(f"{f}\n" for f in sorted(files))


def _load_shard_manifest(path: Path) -> list[str]:
    """Read one committed ``ci_shards/shard-N.txt`` file, strict format.

    This manifest is read into a bash array (``mapfile``) and passed as
    ``pytest`` positional argv in CI, so every line must already be a safe,
    unambiguous relative path: no blank/padded lines, no absolute paths, no
    ``..`` traversal, and it must live under ``tests/`` as a ``.py`` file.
    """
    if not path.is_file():
        raise ShardPlanError(f"{path}: shard manifest does not exist")
    raw_lines = path.read_text(encoding="utf-8").splitlines()
    files: list[str] = []
    for lineno, line in enumerate(raw_lines, start=1):
        if not line.strip():
            raise ShardPlanError(f"{path}:{lineno}: blank line not allowed")
        if line != line.strip():
            raise ShardPlanError(
                f"{path}:{lineno}: leading/trailing whitespace not allowed: {line!r}"
            )
        if line.startswith("/") or line.startswith("~"):
            raise ShardPlanError(
                f"{path}:{lineno}: absolute path not allowed: {line!r}"
            )
        if line.split("/") and ".." in line.split("/"):
            raise ShardPlanError(
                f"{path}:{lineno}: path traversal not allowed: {line!r}"
            )
        if not line.startswith("tests/") or not line.endswith(".py"):
            raise ShardPlanError(
                f"{path}:{lineno}: entry must be a tests/**/*.py path: {line!r}"
            )
        files.append(line)
    counts = Counter(files)
    duplicates = sorted(f for f, count in counts.items() if count > 1)
    if duplicates:
        raise ShardPlanError(
            f"{path}: duplicate entries within manifest: {duplicates[:10]!r}"
        )
    if files != sorted(files):
        raise ShardPlanError(
            f"{path}: entries are not in canonical sorted order (regenerate, "
            "do not hand-edit)"
        )
    return files


def load_all_shard_manifests(
    manifest_dir: Path, shard_count: int
) -> dict[int, list[str]]:
    return {
        index: _load_shard_manifest(_manifest_path(manifest_dir, index))
        for index in range(1, shard_count + 1)
    }


def write_shard_manifests(manifest_dir: Path, shards: list[list[str]]) -> list[Path]:
    manifest_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for offset, files in enumerate(shards):
        path = _manifest_path(manifest_dir, offset + 1)
        path.write_text(render_shard_manifest(files), encoding="utf-8")
        written.append(path)
    return written


# --------------------------------------------------------------------------
# Exact-cover validation
# --------------------------------------------------------------------------


REGENERATE_HINT = (
    "regenerate with: python3 -m scripts.ci.file_shard_plan generate "
    "--call-durations .call_durations.json --collected <fresh collect-only output> "
    "--manifest-dir ci_shards"
)


def validate_exact_cover(
    *, shard_files: dict[int, list[str]], authoritative_files: set[str]
) -> None:
    """Fail closed unless the shards are an exact, disjoint cover.

    Checks (all independent, all reported together): duplicate entries
    within a single manifest, a file assigned to more than one shard, an
    authoritative file missing from every manifest, a manifest entry that is
    not in the current authoritative collection (stale/renamed/deleted), and
    an empty shard.
    """
    errors: list[str] = []
    empty_shards = [index for index, files in shard_files.items() if not files]
    if empty_shards:
        errors.append(f"empty shard(s): {sorted(empty_shards)!r}")

    owner_shards: dict[str, list[int]] = {}
    for index, files in shard_files.items():
        counts = Counter(files)
        within_duplicates = sorted(f for f, count in counts.items() if count > 1)
        if within_duplicates:
            errors.append(
                f"shard {index}: duplicate entries within manifest: "
                f"{within_duplicates[:10]!r}"
            )
        for f in set(files):
            owner_shards.setdefault(f, []).append(index)

    cross_duplicates = {
        f: shards for f, shards in owner_shards.items() if len(shards) > 1
    }
    if cross_duplicates:
        preview = dict(sorted(cross_duplicates.items())[:10])
        errors.append(f"file(s) assigned to more than one shard: {preview!r}")

    union = set(owner_shards)
    missing = sorted(authoritative_files - union)
    if missing:
        errors.append(
            f"{len(missing)} file(s) missing from every manifest: {missing[:20]!r}"
        )

    unexpected = sorted(union - authoritative_files)
    if unexpected:
        errors.append(
            f"{len(unexpected)} manifest entry(ies) not in current collection "
            f"(stale/renamed/deleted): {unexpected[:20]!r}"
        )

    if errors:
        raise ShardPlanError(
            "shard manifest exact-cover check failed:\n- "
            + "\n- ".join(errors)
            + f"\n\n{REGENERATE_HINT}"
        )


# --------------------------------------------------------------------------
# weights.json (audit/reporting only -- not consumed by `check`)
# --------------------------------------------------------------------------


def build_weights_report(
    *,
    shard_count: int,
    file_weights: dict[str, float],
    shards: list[list[str]],
    totals: list[float],
    fallback_unmeasured_seconds: float,
    authoritative_node_count: int,
) -> dict[str, Any]:
    max_total = max(totals) if totals else 0.0
    min_total = min(totals) if totals else 0.0
    # (max - min) / min, NOT / max: dividing by the larger number
    # understates the imbalance (e.g. max=110, min=90 reads as 18.2% via
    # /max but 22.2% via /min) and could pass a real >10% imbalance under
    # the acceptance criterion's "max/min spread <= 10%" reading.
    spread_pct: float | None
    if min_total > 0:
        spread_pct = ((max_total - min_total) / min_total) * 100.0
    elif max_total <= 0:
        spread_pct = 0.0  # degenerate: every shard has zero weight.
    else:
        spread_pct = None  # undefined: some shard has zero weight, another does not.
    return {
        "schema_version": _WEIGHTS_SCHEMA_VERSION,
        "shard_count": shard_count,
        "fallback": {
            "not_called_seconds": NOT_CALLED_FALLBACK_SECONDS,
            "unmeasured_node_seconds": round(fallback_unmeasured_seconds, 6),
        },
        "authoritative_node_count": authoritative_node_count,
        "authoritative_file_count": len(file_weights),
        "shards": [
            {
                "shard": index + 1,
                "file_count": len(files),
                "total_weight_seconds": round(totals[index], 6),
            }
            for index, files in enumerate(shards)
        ],
        "max_min_spread_pct": (
            round(spread_pct, 4) if spread_pct is not None else None
        ),
    }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _cmd_generate(args: argparse.Namespace) -> int:
    # Reuse call_durations.py's own strict loader/structural validator
    # (duplicate-JSON-key rejection, schema_version, node_count cross-check,
    # duration value type/finiteness, not_called duplicate/overlap) rather
    # than reading the artifact with a bare json.loads -- a malformed or
    # hand-tampered .call_durations.json must fail generate closed too, not
    # just call_durations.py's own build/validate paths.
    call_durations_artifact = load_call_durations_artifact(args.call_durations)
    durations, not_called = validate_artifact_structure(
        call_durations_artifact, source=str(args.call_durations)
    )
    # Provenance/self-consistency (ROB-1312 R1): garbage source_commit_sha or
    # a collection_hash that does not actually describe this artifact's own
    # durations/not_called must never silently produce a manifest. This is
    # deliberately NOT a freshness check against today's tree -- a stale but
    # internally-consistent artifact is valid input (see §3 below and the
    # module docstring's fallback-weight design).
    validate_artifact_provenance(
        call_durations_artifact,
        durations=durations,
        not_called=not_called,
        source=str(args.call_durations),
    )

    collected_node_ids = load_collected_node_manifest(args.collected)
    authoritative_files = collected_files_from_nodes(collected_node_ids)

    file_weights = compute_file_weights(
        collected_node_ids=collected_node_ids,
        durations=durations,
        not_called=not_called,
    )
    # Every authoritative file must have a weight entry, even if all of its
    # nodes happened to be `not_called` (weight 0.0) -- compute_file_weights
    # already guarantees this since it iterates every collected node id.
    assert set(file_weights) == authoritative_files

    shards, totals = assign_files_to_shards(file_weights, args.shard_count)

    # Self-check: generate must never write a manifest set it would not also
    # accept from `check` -- this also catches file_count < shard_count
    # (which forces at least one empty shard) before anything is written.
    shard_files_preview = {index + 1: files for index, files in enumerate(shards)}
    validate_exact_cover(
        shard_files=shard_files_preview, authoritative_files=authoritative_files
    )

    written = write_shard_manifests(args.manifest_dir, shards)

    report = build_weights_report(
        shard_count=args.shard_count,
        file_weights=file_weights,
        shards=shards,
        totals=totals,
        fallback_unmeasured_seconds=_unmeasured_fallback_seconds(durations),
        authoritative_node_count=len(collected_node_ids),
    )
    weights_path = args.manifest_dir / _WEIGHTS_FILENAME
    weights_path.write_text(
        json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )

    for path, files in zip(written, shards, strict=True):
        print(f"wrote {path}: {len(files)} file(s)")
    print(
        f"wrote {weights_path}: spread={report['max_min_spread_pct']}% "
        f"across {args.shard_count} shard(s)"
    )
    return 0


def _cmd_check(args: argparse.Namespace) -> int:
    collected_node_ids = load_collected_node_manifest(args.collected)
    authoritative_files = collected_files_from_nodes(collected_node_ids)
    shard_files = load_all_shard_manifests(args.manifest_dir, args.shard_count)

    try:
        validate_exact_cover(
            shard_files=shard_files, authoritative_files=authoritative_files
        )
    except ShardPlanError as error:
        print(f"::error::{error}", file=sys.stderr)
        return 1

    for index in sorted(shard_files):
        print(f"shard {index}: {len(shard_files[index])} file(s)")
    print(
        f"exact-cover OK: {len(authoritative_files)} file(s) across "
        f"{args.shard_count} shard(s), 0 missing, 0 duplicate, 0 unexpected"
    )
    return 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate_parser = subparsers.add_parser(
        "generate",
        help="regenerate the four committed shard manifests from fresh inputs",
    )
    generate_parser.add_argument("--call-durations", type=Path, required=True)
    generate_parser.add_argument(
        "--collected",
        type=Path,
        required=True,
        help="authoritative `pytest --collect-only -m 'not live'` node-id capture",
    )
    generate_parser.add_argument("--manifest-dir", type=Path, required=True)
    generate_parser.add_argument("--shard-count", type=int, default=DEFAULT_SHARD_COUNT)
    generate_parser.set_defaults(func=_cmd_generate)

    check_parser = subparsers.add_parser(
        "check", help="validate the committed shard manifests are an exact cover"
    )
    check_parser.add_argument(
        "--collected",
        type=Path,
        required=True,
        help="authoritative `pytest --collect-only -m 'not live'` node-id capture",
    )
    check_parser.add_argument("--manifest-dir", type=Path, required=True)
    check_parser.add_argument("--shard-count", type=int, default=DEFAULT_SHARD_COUNT)
    check_parser.set_defaults(func=_cmd_check)

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        return args.func(args)
    except ValueError as error:
        # ShardPlanError (this module) and the plain ValueError raised by
        # scripts.call_durations's strict artifact loader/structural
        # validator (a malformed .call_durations.json) must both fail
        # generate/check closed with an actionable message, not an
        # uncaught traceback.
        print(f"::error::{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

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

* ``generate`` -- **duration-refresh workflow only**: reads
  ``.test_durations`` (pytest-split ``--store-durations``: the whole
  per-test protocol, setup + call + teardown) and a fresh authoritative
  collect-only node manifest, sums per-node weight into per-file weight, and
  writes four manifests via a deterministic
  largest-processing-time-first (LPT) bin-packing. Overwrites whatever
  manifests are already on disk. It intentionally has no incremental mode,
  so a normal PR that merely adds a test file must minimally add that sorted
  path to one committed manifest instead of running this command.
* ``check`` -- reads the four committed manifests plus a fresh authoritative
  collect-only node manifest and validates exact cover: every authoritative
  file in exactly one manifest, no duplicates (within or across manifests),
  no stale/unexpected entries, no empty shard. Never writes anything. Fails
  closed (non-zero exit, actionable message for a minimal manifest repair)
  on any violation.

Why the whole protocol, not the call phase: ``.call_durations.json``
(ROB-1295) records only the ``call`` phase. On the 2026-11-03 refresh
artifact that is 3,268.2 s against 3,703.7 s of real per-test pytest time --
**435.5 s (13.3 %) of setup/teardown cost the call-phase weight cannot see**,
mostly session/worker bootstrap (see ``docs/runbooks/ci-file-shard-manifests.md``
§3). ``.test_durations`` is already produced by the same weekly refresh,
already covers every authoritative node (including nodes whose ``setup``
skips), and is what a shard actually pays.

Prediction vs. weight: the shards run ``pytest -n 4 --dist=loadfile``, so a
**file** is the scheduling unit on four workers and the shard's pytest time is
a *makespan*, not a sum -- one 301 s file bounds its shard at 301 s no matter
how balanced the totals are. ``predict_shard_pytest_seconds`` models that
(``--dist=loadfile`` hands units out in the manifest's sorted argv order to
whichever worker is free), and ``weights.json`` reports it next to the raw
weight totals so the number it prints is one a job can actually realise.

Weight fallbacks (explicit, deterministic -- see
``_unmeasured_fallback_seconds``):

* A node collected today but absent from the artifact (a brand-new test added
  since the artifact was last refreshed, or genuine drift) contributes the
  mean of all measured durations in that same artifact -- a deterministic,
  reproducible function of the committed input, not a hand-tuned magic
  constant. If the artifact has zero measured durations (an empty/bootstrap
  artifact), the fallback is ``UNMEASURED_FALLBACK_DEFAULT_SECONDS``.

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
import math
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from scripts.call_durations import load_json_no_duplicate_keys

#: Fallback per-node weight (seconds) used only when the duration artifact
#: itself has zero measured durations (empty/bootstrap artifact) -- normally
#: unreachable once the artifact has been refreshed at least once.
UNMEASURED_FALLBACK_DEFAULT_SECONDS = 1.0

#: Mirrors ``-n 4`` in ``.github/workflows/test.yml`` and
#: ``test-durations-refresh.yml``. Only used for the reported prediction; it
#: is not a packing input.
DEFAULT_WORKERS_PER_SHARD = 4

DEFAULT_SHARD_COUNT = 4

_MANIFEST_FILENAME = "shard-{index}.txt"
_WEIGHTS_FILENAME = "weights.json"
#: 2: weight source is ``.test_durations`` (whole protocol) and the report
#: carries a ``--dist=loadfile`` makespan prediction per shard.
_WEIGHTS_SCHEMA_VERSION = 2


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


def load_test_durations(path: Path) -> dict[str, float]:
    """Read a pytest-split ``.test_durations`` map (node id -> seconds).

    ``--store-durations`` measures the whole per-test protocol, so this is the
    only artifact the refresh produces that contains setup and teardown cost.
    Fails closed on anything a hand-edited or corrupted file could smuggle in:
    duplicate JSON keys (``json.loads`` would otherwise silently keep the
    last), a non-object top level, a non-string node id, and
    non-finite/negative/non-numeric durations.

    This loader deliberately does **not** require the artifact to describe
    today's tree -- a stale but well-formed map is valid input, and nodes it
    lacks fall back to ``_unmeasured_fallback_seconds``. Provenance is
    established upstream: in the refresh workflow ``merge_test_durations.py``
    wrote this file from the four measured shards and validated it against the
    same authoritative capture ``generate`` receives.
    """
    raw = load_json_no_duplicate_keys(path)
    if not isinstance(raw, dict):
        raise ShardPlanError(f"{path}: expected a JSON object of node id -> seconds")
    durations: dict[str, float] = {}
    for node_id, duration in raw.items():
        if not isinstance(node_id, str) or not node_id:
            raise ShardPlanError(f"{path}: invalid node id {node_id!r}")
        if isinstance(duration, bool) or not isinstance(duration, int | float):
            raise ShardPlanError(
                f"{path}: invalid duration for {node_id}: {duration!r}"
            )
        value = float(duration)
        if value < 0 or not math.isfinite(value):
            raise ShardPlanError(
                f"{path}: invalid duration for {node_id}: {duration!r}"
            )
        durations[node_id] = value
    return durations


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
        weight = durations.get(node_id, fallback)
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
# Predicted shard pytest time (--dist=loadfile on N workers)
# --------------------------------------------------------------------------


def predict_shard_pytest_seconds(
    files: list[str],
    file_weights: dict[str, float],
    *,
    workers: int = DEFAULT_WORKERS_PER_SHARD,
) -> float:
    """Predicted pytest wall time for one shard, as a makespan.

    The shards run ``-n 4 --dist=loadfile``: the **file** is the scheduling
    unit, and xdist hands the next collected unit to whichever worker is free.
    pytest collects the manifest's positional arguments in order, and the
    manifest is sorted (``_load_shard_manifest`` enforces it), so the dispatch
    order is that sorted order. This replays exactly that: visit files in
    sorted order, put each on the worker with the smallest accumulated total
    (ties -> lowest worker index).

    The result is therefore never below the shard's largest single file, which
    is the floor the plain weight *sum* hides.
    """
    if workers < 1:
        raise ShardPlanError(f"workers must be >= 1, got {workers}")
    totals = [0.0] * workers
    for file_path in sorted(files):
        worker = min(range(workers), key=lambda i: (totals[i], i))
        totals[worker] += file_weights[file_path]
    return max(totals)


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
            f"{path}: entries are not in canonical sorted order (place the entry at "
            "its LC_ALL=C sorted position, or run LC_ALL=C sort on the affected "
            "file; see docs/runbooks/ci-file-shard-manifests.md §4)"
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


EXACT_COVER_HINT = (
    "for a normal test-file add/remove/rename: minimally edit exactly one "
    "ci_shards/shard-N.txt at its LC_ALL=C sorted position so every collected "
    "file appears once; reserve `file_shard_plan generate` for the duration-"
    "refresh workflow because it recomputes and rewrites all shards"
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
            + f"\n\n{EXACT_COVER_HINT}"
        )


# --------------------------------------------------------------------------
# weights.json (audit/reporting only -- not consumed by `check`)
# --------------------------------------------------------------------------


def _spread_pct(values: list[float]) -> float | None:
    """``(max - min) / min * 100``, or ``None`` when it is undefined.

    Deliberately divided by the **smaller** value, not the larger: dividing by
    the larger number understates the imbalance (``max=110, min=90`` reads as
    18.2% via /max but 22.2% via /min) and could pass a real >10% imbalance
    under a naive reading of the acceptance bar.
    """
    if not values:
        return 0.0
    max_value = max(values)
    min_value = min(values)
    if min_value > 0:
        return ((max_value - min_value) / min_value) * 100.0
    if max_value <= 0:
        return 0.0  # degenerate: every shard has zero weight.
    return None  # undefined: some shard has zero weight, another does not.


def build_weights_report(
    *,
    shard_count: int,
    file_weights: dict[str, float],
    shards: list[list[str]],
    totals: list[float],
    fallback_unmeasured_seconds: float,
    authoritative_node_count: int,
    workers_per_shard: int = DEFAULT_WORKERS_PER_SHARD,
) -> dict[str, Any]:
    predicted = [
        predict_shard_pytest_seconds(files, file_weights, workers=workers_per_shard)
        for files in shards
    ]
    weight_spread_pct = _spread_pct(totals)
    predicted_spread_pct = _spread_pct(predicted)
    return {
        "schema_version": _WEIGHTS_SCHEMA_VERSION,
        "shard_count": shard_count,
        "weight_source": ".test_durations",
        "weight_basis": "setup+call+teardown per test, summed per file",
        "fallback": {
            "unmeasured_node_seconds": round(fallback_unmeasured_seconds, 6),
        },
        "prediction": {
            "model": "xdist_loadfile_list_scheduling",
            "workers_per_shard": workers_per_shard,
            "note": (
                "pytest -n N --dist=loadfile on the manifest's sorted argv "
                "order: a file is the scheduling unit, so a shard cannot finish "
                "before its largest single file and total_weight_seconds / N"
            ),
        },
        "authoritative_node_count": authoritative_node_count,
        "authoritative_file_count": len(file_weights),
        "shards": [
            {
                "shard": index + 1,
                "file_count": len(files),
                "total_weight_seconds": round(totals[index], 6),
                "predicted_pytest_seconds": round(predicted[index], 6),
            }
            for index, files in enumerate(shards)
        ],
        "max_min_spread_pct": (
            round(weight_spread_pct, 4) if weight_spread_pct is not None else None
        ),
        "predicted_max_min_spread_pct": (
            round(predicted_spread_pct, 4) if predicted_spread_pct is not None else None
        ),
    }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _cmd_generate(args: argparse.Namespace) -> int:
    # Strict whole-protocol duration map (setup + call + teardown). In the
    # refresh workflow this file was written by merge_test_durations.py in the
    # same job, from the four measured shards, validated against the very
    # authoritative capture passed here -- so a truncated or tampered map must
    # fail generate closed rather than silently produce a manifest.
    durations = load_test_durations(args.durations)

    collected_node_ids = load_collected_node_manifest(args.collected)
    authoritative_files = collected_files_from_nodes(collected_node_ids)

    file_weights = compute_file_weights(
        collected_node_ids=collected_node_ids,
        durations=durations,
    )
    # Every authoritative file must have a weight entry, even if the artifact
    # had no measurement for it (fallback seconds) -- compute_file_weights
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
        f"wrote {weights_path}: weight spread={report['max_min_spread_pct']}% "
        f"predicted pytest spread={report['predicted_max_min_spread_pct']}% "
        f"across {args.shard_count} shard(s), "
        f"{report['prediction']['workers_per_shard']} worker(s) each"
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
        help="duration-refresh only: fully rebalance manifests from fresh inputs",
    )
    generate_parser.add_argument(
        "--durations",
        type=Path,
        required=True,
        help=(
            "pytest-split `.test_durations` map (node id -> seconds), i.e. the "
            "whole setup+call+teardown protocol"
        ),
    )
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
        # scripts.call_durations's duplicate-key JSON loader (a malformed
        # .test_durations) must both fail generate/check closed with an
        # actionable message, not an uncaught traceback.
        print(f"::error::{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

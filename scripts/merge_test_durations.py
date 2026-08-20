"""Deterministically merge disjoint per-shard test-duration measurements.

ROB-1312: the four shards now run *disjoint* committed file manifests
(``ci_shards/shard-{1..4}.txt``, see ``scripts/ci/file_shard_plan.py``)
instead of each independently collecting and pytest-split-selecting from the
whole suite. The merge contract changed accordingly:

* each shard's own collected-node manifest is the ground truth for what that
  shard measured -- its measured durations must equal that manifest
  *exactly* (no missing, no extra: a duration recorded against a node this
  shard never collected is evidence of a wrong-shard measurement, not a
  bonus);
* the four shards' collected-node manifests must be pairwise disjoint (no
  node id measured by more than one shard);
* their union must equal a separately captured, independent authoritative
  collection (``pytest --collect-only -m "not live" tests/`` over the whole
  suite, captured once, never derived from the shards themselves -- deriving
  "authoritative" from the shards' own union would make a shard that quietly
  under-collects invisible);
* baseline entries absent from the authoritative collection are stale and
  are dropped.

Collected-node manifests (both per-shard and authoritative) must not be
pre-deduplicated (e.g. with ``sort -u``) before reaching this module: a
genuine duplicate collection is a real bug this module needs to see and
reject, not have silently erased upstream.
"""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter
from pathlib import Path
from typing import Any


def _load_durations(path: Path) -> dict[str, float]:
    raw: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: expected a JSON object")

    durations: dict[str, float] = {}
    for node_id, duration in raw.items():
        if (
            not isinstance(node_id, str)
            or isinstance(duration, bool)
            or not isinstance(duration, int | float)
        ):
            raise ValueError(f"{path}: invalid duration entry {node_id!r}")
        value = float(duration)
        if value < 0 or not math.isfinite(value):
            raise ValueError(f"{path}: invalid duration for {node_id}: {duration!r}")
        durations[node_id] = value
    return durations


def load_node_manifest(path: Path) -> list[str]:
    """Read a newline-delimited pytest node-id manifest, fail-closed.

    Rejects blank lines and duplicate node ids. Callers must not pre-dedupe
    the source capture (no ``sort -u``) -- see module docstring.
    """
    raw_lines = path.read_text(encoding="utf-8").splitlines()
    lines = [line.strip() for line in raw_lines]
    if any(not line for line in lines):
        raise ValueError(f"{path}: blank line(s) not allowed in a node-id manifest")
    if not lines:
        raise ValueError(f"{path}: node-id manifest must not be empty")
    counts = Counter(lines)
    duplicates = sorted(node for node, count in counts.items() if count > 1)
    if duplicates:
        raise ValueError(
            f"{path}: duplicate node id(s) in manifest: {duplicates[:10]!r}"
        )
    return lines


def validate_disjoint_shard_collections(
    *,
    per_shard_paths: list[Path],
    per_shard_collected: list[set[str]],
    authoritative_path: Path,
    authoritative: set[str],
) -> None:
    """Fail closed unless the shard collections are an exact, disjoint cover."""
    n = len(per_shard_collected)
    for i in range(n):
        for j in range(i + 1, n):
            overlap = sorted(per_shard_collected[i] & per_shard_collected[j])
            if overlap:
                raise ValueError(
                    f"{per_shard_paths[i]} and {per_shard_paths[j]}: shard collections "
                    f"share node id(s) (not disjoint): {overlap[:10]!r}"
                )

    union: set[str] = set()
    for collected in per_shard_collected:
        union |= collected

    missing = sorted(authoritative - union)
    if missing:
        raise ValueError(
            f"{authoritative_path}: {len(missing)} node id(s) absent from every shard "
            f"collection: {missing[:20]!r}"
        )

    unexpected = sorted(union - authoritative)
    if unexpected:
        raise ValueError(
            f"{authoritative_path}: {len(unexpected)} node id(s) collected by a shard "
            f"but absent from the authoritative collection: {unexpected[:20]!r}"
        )


def _check_shard_measured_matches_collected(
    *, shard_path: Path, measured_known: set[str], collected: set[str]
) -> None:
    missing = sorted(collected - measured_known)
    if missing:
        raise ValueError(
            f"{shard_path}: {len(missing)} node(s) collected by this shard have no "
            f"measurement: {missing[:20]!r}"
        )
    unexpected = sorted(measured_known - collected)
    if unexpected:
        raise ValueError(
            f"{shard_path}: measurement(s) present for {len(unexpected)} node(s) this "
            f"shard did not collect (wrong-shard measurement): {unexpected[:20]!r}"
        )


def merge_duration_shards(
    *,
    baseline_path: Path,
    shard_paths: list[Path],
    collected_paths: list[Path],
    authoritative_path: Path,
) -> tuple[dict[str, float], int]:
    """Return the complete current duration map and number of stale entries.

    ``shard_paths[i]`` and ``collected_paths[i]`` must describe the same
    shard, in the same order.
    """
    if len(shard_paths) != len(collected_paths):
        raise ValueError(
            "shard_paths and collected_paths must have matching length/order"
        )

    baseline = _load_durations(baseline_path)
    per_shard_collected = [set(load_node_manifest(p)) for p in collected_paths]
    authoritative = set(load_node_manifest(authoritative_path))

    validate_disjoint_shard_collections(
        per_shard_paths=collected_paths,
        per_shard_collected=per_shard_collected,
        authoritative_path=authoritative_path,
        authoritative=authoritative,
    )

    measured: dict[str, float] = {}
    for shard_path, collected in zip(shard_paths, per_shard_collected, strict=True):
        shard = _load_durations(shard_path)
        _check_shard_measured_matches_collected(
            shard_path=shard_path, measured_known=set(shard), collected=collected
        )
        measured.update(shard)

    stale_count = len(set(baseline) - authoritative)
    return {
        node_id: measured[node_id] for node_id in sorted(authoritative)
    }, stale_count


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--shard", type=Path, action="append", required=True)
    parser.add_argument(
        "--collected",
        type=Path,
        action="append",
        required=True,
        help="per-shard collected-node manifest, same order as --shard",
    )
    parser.add_argument(
        "--authoritative",
        type=Path,
        required=True,
        help="independent full-suite `pytest --collect-only` node-id capture",
    )
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    merged, stale_count = merge_duration_shards(
        baseline_path=args.baseline,
        shard_paths=args.shard,
        collected_paths=args.collected,
        authoritative_path=args.authoritative,
    )
    args.output.write_text(
        json.dumps(merged, sort_keys=True, indent=4) + "\n", encoding="utf-8"
    )
    print(
        f"merged {len(args.shard)} shards into {len(merged)} entries; "
        f"dropped {stale_count} stale baseline entries"
    )


if __name__ == "__main__":
    main()

"""Deterministically merge pytest-split duration shards.

The current collected-node manifests are authoritative:

* baseline entries absent from collection are stale and are dropped;
* every currently collected test must have a measured shard duration;
* duplicate measurements across shards are rejected;
* all shard manifests must describe the same collection.
"""

from __future__ import annotations

import argparse
import json
import math
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


def _load_collected_nodes(paths: list[Path]) -> set[str]:
    manifests = [
        {
            line.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        }
        for path in paths
    ]
    if not manifests or not manifests[0]:
        raise ValueError("collected-node manifests must not be empty")
    expected = manifests[0]
    for path, manifest in zip(paths[1:], manifests[1:], strict=True):
        if manifest != expected:
            missing = sorted(expected - manifest)[:10]
            extra = sorted(manifest - expected)[:10]
            raise ValueError(
                f"{path}: collection differs from first manifest; "
                f"missing={missing!r}, extra={extra!r}"
            )
    return expected


def merge_duration_shards(
    *,
    baseline_path: Path,
    shard_paths: list[Path],
    collected_paths: list[Path],
) -> tuple[dict[str, float], int]:
    """Return the complete current duration map and number of stale entries."""
    baseline = _load_durations(baseline_path)
    collected = _load_collected_nodes(collected_paths)
    measured: dict[str, float] = {}

    for shard_path in shard_paths:
        shard = _load_durations(shard_path)
        unexpected = sorted(set(shard) - collected)
        if unexpected:
            raise ValueError(
                f"{shard_path}: durations contain uncollected tests: {unexpected[:10]!r}"
            )
        duplicates = sorted(set(shard) & set(measured))
        if duplicates:
            raise ValueError(
                f"{shard_path}: duplicate tests across shards: {duplicates[:10]!r}"
            )
        measured.update(shard)

    missing = sorted(collected - set(measured))
    if missing:
        raise ValueError(
            "duration shards are incomplete; "
            f"{len(missing)} collected tests have no measurement: {missing[:20]!r}"
        )

    stale_count = len(set(baseline) - collected)
    return {node_id: measured[node_id] for node_id in sorted(collected)}, stale_count


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--shard", type=Path, action="append", required=True)
    parser.add_argument("--collected", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    merged, stale_count = merge_duration_shards(
        baseline_path=args.baseline,
        shard_paths=args.shard,
        collected_paths=args.collected,
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

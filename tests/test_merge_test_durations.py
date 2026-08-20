from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.merge_test_durations import merge_duration_shards

pytestmark = pytest.mark.unit


def _write_json(path: Path, value: object) -> Path:
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def _write_manifest(path: Path, *node_ids: str) -> Path:
    path.write_text("\n".join(node_ids) + "\n", encoding="utf-8")
    return path


def test_merge_replaces_measurements_and_drops_stale_entries(tmp_path: Path) -> None:
    first = "tests/test_a.py::test_a"
    second = "tests/test_b.py::test_b"
    stale = "tests/test_removed.py::test_removed"
    baseline = _write_json(
        tmp_path / "baseline.json", {first: 99.0, second: 88.0, stale: 77.0}
    )
    shards = [
        _write_json(tmp_path / "shard-1.json", {first: 1.25}),
        _write_json(tmp_path / "shard-2.json", {second: 2.5}),
    ]
    # ROB-1312: shard collections are disjoint (each shard's own manifest,
    # not an identical copy across shards); union checked against a
    # separately-captured authoritative manifest.
    manifests = [
        _write_manifest(tmp_path / "collected-1.txt", first),
        _write_manifest(tmp_path / "collected-2.txt", second),
    ]
    authoritative = _write_manifest(tmp_path / "authoritative.txt", first, second)

    merged, stale_count = merge_duration_shards(
        baseline_path=baseline,
        shard_paths=shards,
        collected_paths=manifests,
        authoritative_path=authoritative,
    )

    assert merged == {first: 1.25, second: 2.5}
    assert stale_count == 1


def test_merge_fails_when_collected_test_has_no_measurement(tmp_path: Path) -> None:
    first = "tests/test_a.py::test_a"
    second = "tests/test_b.py::test_b"
    baseline = _write_json(tmp_path / "baseline.json", {first: 1.0, second: 2.0})
    manifest = _write_manifest(tmp_path / "collected.txt", first, second)

    with pytest.raises(ValueError, match="have no measurement"):
        merge_duration_shards(
            baseline_path=baseline,
            shard_paths=[_write_json(tmp_path / "shard.json", {first: 1.5})],
            collected_paths=[manifest],
            authoritative_path=manifest,
        )


def test_merge_rejects_duplicate_shard_entries(tmp_path: Path) -> None:
    # ROB-1312: two shards both claiming to have collected the same node id
    # -- collections are no longer required/allowed to overlap.
    node_id = "tests/test_a.py::test_a"
    baseline = _write_json(tmp_path / "baseline.json", {node_id: 1.0})
    authoritative = _write_manifest(tmp_path / "authoritative.txt", node_id)

    with pytest.raises(ValueError, match="not disjoint"):
        merge_duration_shards(
            baseline_path=baseline,
            shard_paths=[
                _write_json(tmp_path / "shard-1.json", {node_id: 1.5}),
                _write_json(tmp_path / "shard-2.json", {node_id: 1.6}),
            ],
            collected_paths=[
                _write_manifest(tmp_path / "collected-1.txt", node_id),
                _write_manifest(tmp_path / "collected-2.txt", node_id),
            ],
            authoritative_path=authoritative,
        )


def test_merge_rejects_different_collection_manifests(tmp_path: Path) -> None:
    # ROB-1312: a shard's own collected manifest containing a node id the
    # independent authoritative capture never saw -- stale/renamed, not
    # something the union can silently absorb.
    first = "tests/test_a.py::test_a"
    second = "tests/test_b.py::test_b"
    baseline = _write_json(tmp_path / "baseline.json", {first: 1.0})

    with pytest.raises(ValueError, match="absent from the authoritative collection"):
        merge_duration_shards(
            baseline_path=baseline,
            shard_paths=[
                _write_json(tmp_path / "shard.json", {first: 1.5, second: 2.5})
            ],
            collected_paths=[
                _write_manifest(tmp_path / "collected.txt", first, second),
            ],
            authoritative_path=_write_manifest(tmp_path / "authoritative.txt", first),
        )

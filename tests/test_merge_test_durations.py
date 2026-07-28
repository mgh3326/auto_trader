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
    manifests = [
        _write_manifest(tmp_path / "collected-1.txt", first, second),
        _write_manifest(tmp_path / "collected-2.txt", second, first),
    ]

    merged, stale_count = merge_duration_shards(
        baseline_path=baseline,
        shard_paths=shards,
        collected_paths=manifests,
    )

    assert merged == {first: 1.25, second: 2.5}
    assert stale_count == 1


def test_merge_fails_when_collected_test_has_no_measurement(tmp_path: Path) -> None:
    first = "tests/test_a.py::test_a"
    second = "tests/test_b.py::test_b"
    baseline = _write_json(tmp_path / "baseline.json", {first: 1.0, second: 2.0})
    manifest = _write_manifest(tmp_path / "collected.txt", first, second)

    with pytest.raises(ValueError, match="1 collected tests have no measurement"):
        merge_duration_shards(
            baseline_path=baseline,
            shard_paths=[_write_json(tmp_path / "shard.json", {first: 1.5})],
            collected_paths=[manifest],
        )


def test_merge_rejects_duplicate_shard_entries(tmp_path: Path) -> None:
    node_id = "tests/test_a.py::test_a"
    baseline = _write_json(tmp_path / "baseline.json", {node_id: 1.0})
    manifest = _write_manifest(tmp_path / "collected.txt", node_id)

    with pytest.raises(ValueError, match="duplicate tests across shards"):
        merge_duration_shards(
            baseline_path=baseline,
            shard_paths=[
                _write_json(tmp_path / "shard-1.json", {node_id: 1.5}),
                _write_json(tmp_path / "shard-2.json", {node_id: 1.6}),
            ],
            collected_paths=[manifest],
        )


def test_merge_rejects_different_collection_manifests(tmp_path: Path) -> None:
    first = "tests/test_a.py::test_a"
    second = "tests/test_b.py::test_b"
    baseline = _write_json(tmp_path / "baseline.json", {first: 1.0})

    with pytest.raises(ValueError, match="collection differs"):
        merge_duration_shards(
            baseline_path=baseline,
            shard_paths=[_write_json(tmp_path / "shard.json", {first: 1.5})],
            collected_paths=[
                _write_manifest(tmp_path / "collected-1.txt", first),
                _write_manifest(tmp_path / "collected-2.txt", first, second),
            ],
        )

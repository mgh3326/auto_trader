from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.merge_test_durations import merge_duration_shards

pytestmark = pytest.mark.unit


def _write(path: Path, value: str) -> Path:
    path.write_text(value, encoding="utf-8")
    return path


def _write_json(path: Path, value: object) -> Path:
    return _write(path, json.dumps(value))


def _write_manifest(path: Path, *node_ids: str) -> Path:
    return _write(path, "\n".join(node_ids) + "\n")


def _merge_with_shard(
    tmp_path: Path, shard: Path, *, collected_node: str
) -> tuple[dict[str, float], int]:
    return merge_duration_shards(
        baseline_path=_write_json(tmp_path / "baseline.json", {collected_node: 1.0}),
        shard_paths=[shard],
        collected_paths=[_write_manifest(tmp_path / "collected.txt", collected_node)],
    )


@pytest.mark.parametrize(
    "contents,error",
    [
        ('{"tests/test_a.py::test_a":', "Expecting value"),
        ('{"tests/test_a.py::test_a": "slow"}', "invalid duration entry"),
    ],
)
def test_merge_rejects_malformed_duration_payload(
    tmp_path: Path, contents: str, error: str
) -> None:
    node_id = "tests/test_a.py::test_a"
    shard = _write(tmp_path / "shard.json", contents)

    with pytest.raises(ValueError, match=error):
        _merge_with_shard(tmp_path, shard, collected_node=node_id)


def test_merge_rejects_uncollected_test(tmp_path: Path) -> None:
    collected = "tests/test_a.py::test_a"
    uncollected = "tests/test_removed.py::test_removed"
    shard = _write_json(
        tmp_path / "shard.json",
        {collected: 1.0, uncollected: 2.0},
    )

    with pytest.raises(ValueError, match="durations contain uncollected tests"):
        _merge_with_shard(tmp_path, shard, collected_node=collected)


@pytest.mark.parametrize("duration", [-0.01, float("nan")], ids=["negative", "nan"])
def test_merge_rejects_negative_or_non_finite_duration(
    tmp_path: Path, duration: float
) -> None:
    node_id = "tests/test_a.py::test_a"
    shard = _write_json(tmp_path / "shard.json", {node_id: duration})

    with pytest.raises(ValueError, match="invalid duration"):
        _merge_with_shard(tmp_path, shard, collected_node=node_id)

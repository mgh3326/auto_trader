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


def _merge_two_shards(
    tmp_path: Path,
    *,
    shard1: dict[str, float],
    shard2: dict[str, float],
    collected1: list[str],
    collected2: list[str],
    authoritative: list[str] | None = None,
    baseline: dict[str, float] | None = None,
) -> tuple[dict[str, float], int]:
    baseline = baseline if baseline is not None else {**shard1, **shard2}
    authoritative_nodes = (
        authoritative if authoritative is not None else collected1 + collected2
    )
    return merge_duration_shards(
        baseline_path=_write_json(tmp_path / "baseline.json", baseline),
        shard_paths=[
            _write_json(tmp_path / "shard1.json", shard1),
            _write_json(tmp_path / "shard2.json", shard2),
        ],
        collected_paths=[
            _write_manifest(tmp_path / "collected1.txt", *collected1),
            _write_manifest(tmp_path / "collected2.txt", *collected2),
        ],
        authoritative_path=_write_manifest(
            tmp_path / "authoritative.txt", *authoritative_nodes
        ),
    )


def test_merge_happy_path_disjoint_shards(tmp_path: Path) -> None:
    merged, stale_count = _merge_two_shards(
        tmp_path,
        shard1={"tests/a.py::test_1": 1.0},
        shard2={"tests/b.py::test_1": 2.0},
        collected1=["tests/a.py::test_1"],
        collected2=["tests/b.py::test_1"],
    )
    assert merged == {"tests/a.py::test_1": 1.0, "tests/b.py::test_1": 2.0}
    assert stale_count == 0


def test_merge_drops_stale_baseline_entries(tmp_path: Path) -> None:
    _, stale_count = _merge_two_shards(
        tmp_path,
        shard1={"tests/a.py::test_1": 1.0},
        shard2={"tests/b.py::test_1": 2.0},
        collected1=["tests/a.py::test_1"],
        collected2=["tests/b.py::test_1"],
        baseline={"tests/a.py::test_1": 1.0, "tests/removed.py::test_x": 9.0},
    )
    assert stale_count == 1


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
    shard1 = _write(tmp_path / "shard1.json", contents)
    shard2 = _write_json(tmp_path / "shard2.json", {})

    with pytest.raises(ValueError, match=error):
        merge_duration_shards(
            baseline_path=_write_json(tmp_path / "baseline.json", {node_id: 1.0}),
            shard_paths=[shard1, shard2],
            collected_paths=[
                _write_manifest(tmp_path / "collected1.txt", node_id),
                _write_manifest(tmp_path / "collected2.txt", "tests/test_b.py::test_b"),
            ],
            authoritative_path=_write_manifest(
                tmp_path / "authoritative.txt", node_id, "tests/test_b.py::test_b"
            ),
        )


@pytest.mark.parametrize("duration", [-0.01, float("nan")], ids=["negative", "nan"])
def test_merge_rejects_negative_or_non_finite_duration(
    tmp_path: Path, duration: float
) -> None:
    node_id = "tests/test_a.py::test_a"
    with pytest.raises(ValueError, match="invalid duration"):
        _merge_two_shards(
            tmp_path,
            shard1={node_id: duration},
            shard2={},
            collected1=[node_id],
            collected2=["tests/test_b.py::test_b"],
        )


def test_merge_rejects_wrong_shard_measurement(tmp_path: Path) -> None:
    # shard1 measures a node it did not collect (it belongs to shard2's
    # collection) -- this must fail even though the node IS in the global
    # authoritative set and even though no shard "duplicated" a collected
    # node.
    with pytest.raises(ValueError, match="wrong-shard measurement"):
        _merge_two_shards(
            tmp_path,
            shard1={"tests/a.py::test_1": 1.0, "tests/b.py::test_1": 9.0},
            shard2={},
            collected1=["tests/a.py::test_1"],
            collected2=["tests/b.py::test_1"],
        )


def test_merge_rejects_shard_missing_measurement_for_its_own_collection(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="have no measurement"):
        _merge_two_shards(
            tmp_path,
            shard1={},
            shard2={"tests/b.py::test_1": 2.0},
            collected1=["tests/a.py::test_1"],
            collected2=["tests/b.py::test_1"],
        )


def test_merge_rejects_inter_shard_collection_overlap(tmp_path: Path) -> None:
    # Both shards claim to have collected the same node id -- not disjoint.
    with pytest.raises(ValueError, match="not disjoint"):
        _merge_two_shards(
            tmp_path,
            shard1={"tests/a.py::test_1": 1.0},
            shard2={"tests/a.py::test_1": 1.0},
            collected1=["tests/a.py::test_1"],
            collected2=["tests/a.py::test_1"],
            authoritative=["tests/a.py::test_1"],
        )


def test_merge_rejects_intra_shard_duplicate_collection_entry(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="duplicate node id"):
        _merge_two_shards(
            tmp_path,
            shard1={"tests/a.py::test_1": 1.0},
            shard2={"tests/b.py::test_1": 1.0},
            collected1=["tests/a.py::test_1", "tests/a.py::test_1"],
            collected2=["tests/b.py::test_1"],
        )


def test_merge_rejects_authoritative_duplicate(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="duplicate node id"):
        _merge_two_shards(
            tmp_path,
            shard1={"tests/a.py::test_1": 1.0},
            shard2={"tests/b.py::test_1": 1.0},
            collected1=["tests/a.py::test_1"],
            collected2=["tests/b.py::test_1"],
            authoritative=[
                "tests/a.py::test_1",
                "tests/b.py::test_1",
                "tests/a.py::test_1",
            ],
        )


def test_merge_rejects_gap_missing_from_every_shard(tmp_path: Path) -> None:
    # authoritative has a node id that neither shard collected at all --
    # a genuine coverage gap, not just an unmeasured-but-collected node.
    with pytest.raises(ValueError, match="absent from every shard collection"):
        _merge_two_shards(
            tmp_path,
            shard1={"tests/a.py::test_1": 1.0},
            shard2={"tests/b.py::test_1": 1.0},
            collected1=["tests/a.py::test_1"],
            collected2=["tests/b.py::test_1"],
            authoritative=[
                "tests/a.py::test_1",
                "tests/b.py::test_1",
                "tests/c.py::test_1",
            ],
        )


def test_merge_rejects_shard_collection_not_in_authoritative(tmp_path: Path) -> None:
    # A shard collected a node the authoritative (independent) capture never
    # saw -- stale/renamed, must not be silently absorbed via the union.
    with pytest.raises(ValueError, match="absent from the authoritative collection"):
        _merge_two_shards(
            tmp_path,
            shard1={"tests/a.py::test_1": 1.0, "tests/removed.py::test_1": 1.0},
            shard2={"tests/b.py::test_1": 1.0},
            collected1=["tests/a.py::test_1", "tests/removed.py::test_1"],
            collected2=["tests/b.py::test_1"],
            authoritative=["tests/a.py::test_1", "tests/b.py::test_1"],
        )


def test_merge_rejects_mismatched_shard_and_collected_lengths(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="matching length/order"):
        merge_duration_shards(
            baseline_path=_write_json(tmp_path / "baseline.json", {}),
            shard_paths=[_write_json(tmp_path / "shard1.json", {})],
            collected_paths=[
                _write_manifest(tmp_path / "c1.txt", "tests/a.py::test_1"),
                _write_manifest(tmp_path / "c2.txt", "tests/b.py::test_1"),
            ],
            authoritative_path=_write_manifest(
                tmp_path / "authoritative.txt",
                "tests/a.py::test_1",
                "tests/b.py::test_1",
            ),
        )

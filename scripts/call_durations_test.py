from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.call_durations import (
    build_artifact,
    compute_collection_hash,
    load_artifact,
    merge_call_duration_shards,
    serialize_artifact,
    validate_freshness,
)

pytestmark = pytest.mark.unit

_NODE_A = "tests/test_a.py::test_a"
_NODE_B = "tests/test_b.py::test_b"


def _write(path: Path, value: str) -> Path:
    path.write_text(value, encoding="utf-8")
    return path


def _write_json(path: Path, value: object) -> Path:
    return _write(path, json.dumps(value))


def _write_manifest(path: Path, *node_ids: str) -> Path:
    return _write(path, "\n".join(node_ids) + "\n")


def _build_two_node_artifact(tmp_path: Path) -> dict[str, object]:
    shard1 = _write_json(tmp_path / "shard1.json", {_NODE_A: 0.01})
    shard2 = _write_json(tmp_path / "shard2.json", {_NODE_B: 0.02})
    collected1 = _write_manifest(tmp_path / "collected1.txt", _NODE_A, _NODE_B)
    return build_artifact(
        shard_paths=[shard1, shard2],
        collected_paths=[collected1],
        source_commit_sha="a" * 40,
    )


# --- build_artifact / merge_call_duration_shards ---------------------------


def test_build_artifact_merges_shards_and_stamps_provenance(tmp_path: Path) -> None:
    artifact = _build_two_node_artifact(tmp_path)

    assert artifact["schema_version"] == 1
    assert artifact["source_commit_sha"] == "a" * 40
    assert artifact["node_count"] == 2
    assert artifact["durations"] == {_NODE_A: 0.01, _NODE_B: 0.02}
    assert artifact["collection_hash"] == compute_collection_hash({_NODE_A, _NODE_B})


def test_build_artifact_output_is_deterministic(tmp_path: Path) -> None:
    first = serialize_artifact(_build_two_node_artifact(tmp_path))
    second = serialize_artifact(_build_two_node_artifact(tmp_path))
    assert first == second


def test_build_artifact_rejects_empty_source_sha(tmp_path: Path) -> None:
    shard = _write_json(tmp_path / "shard.json", {_NODE_A: 0.01})
    collected = _write_manifest(tmp_path / "collected.txt", _NODE_A)

    with pytest.raises(ValueError, match="source_commit_sha must not be empty"):
        build_artifact(
            shard_paths=[shard], collected_paths=[collected], source_commit_sha="  "
        )


def test_merge_rejects_duplicate_across_shards(tmp_path: Path) -> None:
    shard1 = _write_json(tmp_path / "shard1.json", {_NODE_A: 0.01})
    shard2 = _write_json(tmp_path / "shard2.json", {_NODE_A: 0.02})
    collected = _write_manifest(tmp_path / "collected.txt", _NODE_A)

    with pytest.raises(ValueError, match="duplicate node id"):
        merge_call_duration_shards(
            shard_paths=[shard1, shard2], collected_paths=[collected]
        )


def test_merge_rejects_missing_measurement(tmp_path: Path) -> None:
    shard = _write_json(tmp_path / "shard.json", {_NODE_A: 0.01})
    collected = _write_manifest(tmp_path / "collected.txt", _NODE_A, _NODE_B)

    with pytest.raises(ValueError, match="have no call-phase measurement"):
        merge_call_duration_shards(shard_paths=[shard], collected_paths=[collected])


@pytest.mark.parametrize("duration", [-0.01, float("nan")], ids=["negative", "nan"])
def test_merge_rejects_malformed_duration(tmp_path: Path, duration: float) -> None:
    shard = _write_json(tmp_path / "shard.json", {_NODE_A: duration})
    collected = _write_manifest(tmp_path / "collected.txt", _NODE_A)

    with pytest.raises(ValueError, match="invalid duration"):
        merge_call_duration_shards(shard_paths=[shard], collected_paths=[collected])


# --- load_artifact -----------------------------------------------------


def test_load_artifact_rejects_duplicate_key(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "artifact.json",
        '{"schema_version": 1, "source_commit_sha": "a", '
        '"collection_hash": "b", "durations": {"x": 1, "x": 2}}',
    )

    with pytest.raises(ValueError, match="duplicate key"):
        load_artifact(path)


def test_load_artifact_rejects_missing_field(tmp_path: Path) -> None:
    path = _write_json(tmp_path / "artifact.json", {"schema_version": 1})

    with pytest.raises(ValueError, match="missing required field"):
        load_artifact(path)


# --- validate_freshness -------------------------------------------------


def test_validate_freshness_passes_for_freshly_built_artifact(tmp_path: Path) -> None:
    artifact = _build_two_node_artifact(tmp_path)

    validate_freshness(
        artifact,
        collected_nodes={_NODE_A, _NODE_B},
        expected_source_commit_sha="a" * 40,
    )


def test_validate_freshness_rejects_source_sha_mismatch(tmp_path: Path) -> None:
    artifact = _build_two_node_artifact(tmp_path)

    with pytest.raises(ValueError, match="source commit sha mismatch"):
        validate_freshness(
            artifact,
            collected_nodes={_NODE_A, _NODE_B},
            expected_source_commit_sha="b" * 40,
        )


def test_validate_freshness_rejects_collection_hash_mismatch(tmp_path: Path) -> None:
    artifact = _build_two_node_artifact(tmp_path)
    artifact["collection_hash"] = "not-a-real-hash"

    with pytest.raises(ValueError, match="collection hash mismatch"):
        validate_freshness(
            artifact,
            collected_nodes={_NODE_A, _NODE_B},
            expected_source_commit_sha="a" * 40,
        )


def test_validate_freshness_rejects_missing_current_node(tmp_path: Path) -> None:
    # A hash mismatch alone would already fail closed whenever the collected
    # set changes, so isolate "missing" by keeping collection_hash in sync
    # with the new collected set while durations still lack the new node —
    # e.g. a hand-edited or half-written artifact.
    artifact = _build_two_node_artifact(tmp_path)
    node_c = "tests/test_c.py::test_c"
    collected = {_NODE_A, _NODE_B, node_c}
    artifact["collection_hash"] = compute_collection_hash(collected)

    with pytest.raises(ValueError, match="missing measurement"):
        validate_freshness(
            artifact,
            collected_nodes=collected,
            expected_source_commit_sha="a" * 40,
        )


def test_validate_freshness_rejects_stale_node(tmp_path: Path) -> None:
    # Same isolation as above: keep collection_hash in sync with the shrunk
    # collected set so only the stale-entry check can fire.
    artifact = _build_two_node_artifact(tmp_path)
    collected = {_NODE_A}
    artifact["collection_hash"] = compute_collection_hash(collected)

    with pytest.raises(ValueError, match="stale/removed node id"):
        validate_freshness(
            artifact,
            collected_nodes=collected,
            expected_source_commit_sha="a" * 40,
        )


def test_validate_freshness_rejects_malformed_duration(tmp_path: Path) -> None:
    artifact = _build_two_node_artifact(tmp_path)
    artifact["durations"][_NODE_A] = "not-a-number"

    with pytest.raises(ValueError, match="invalid duration entry"):
        validate_freshness(
            artifact,
            collected_nodes={_NODE_A, _NODE_B},
            expected_source_commit_sha="a" * 40,
        )

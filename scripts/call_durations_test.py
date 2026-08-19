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
_NODE_C = "tests/test_c.py::test_c"


def _write(path: Path, value: str) -> Path:
    path.write_text(value, encoding="utf-8")
    return path


def _write_json(path: Path, value: object) -> Path:
    return _write(path, json.dumps(value))


def _shard(
    path: Path, *, durations: dict[str, float] | None = None, not_called=()
) -> Path:
    return _write_json(
        path,
        {"durations": durations or {}, "not_called": list(not_called)},
    )


def _write_manifest(path: Path, *node_ids: str) -> Path:
    return _write(path, "\n".join(node_ids) + "\n")


def _build_two_node_artifact(tmp_path: Path) -> dict[str, object]:
    shard1 = _shard(tmp_path / "shard1.json", durations={_NODE_A: 0.01})
    shard2 = _shard(tmp_path / "shard2.json", durations={_NODE_B: 0.02})
    collected1 = _write_manifest(tmp_path / "collected1.txt", _NODE_A, _NODE_B)
    return build_artifact(
        shard_paths=[shard1, shard2],
        collected_paths=[collected1],
        source_commit_sha="a" * 40,
    )


# --- build_artifact / merge_call_duration_shards ---------------------------


def test_build_artifact_merges_shards_and_stamps_provenance(tmp_path: Path) -> None:
    artifact = _build_two_node_artifact(tmp_path)

    assert artifact["schema_version"] == 2
    assert artifact["source_commit_sha"] == "a" * 40
    assert artifact["node_count"] == 2
    assert artifact["durations"] == {_NODE_A: 0.01, _NODE_B: 0.02}
    assert artifact["not_called"] == []
    assert artifact["collection_hash"] == compute_collection_hash({_NODE_A, _NODE_B})


def test_build_artifact_output_is_deterministic(tmp_path: Path) -> None:
    first = serialize_artifact(_build_two_node_artifact(tmp_path))
    second = serialize_artifact(_build_two_node_artifact(tmp_path))
    assert first == second


def test_build_artifact_rejects_empty_source_sha(tmp_path: Path) -> None:
    shard = _shard(tmp_path / "shard.json", durations={_NODE_A: 0.01})
    collected = _write_manifest(tmp_path / "collected.txt", _NODE_A)

    with pytest.raises(ValueError, match="source_commit_sha must not be empty"):
        build_artifact(
            shard_paths=[shard], collected_paths=[collected], source_commit_sha="  "
        )


def test_build_artifact_records_setup_skip_as_not_called(tmp_path: Path) -> None:
    # ROB-1295 R1: a node whose setup phase itself skips (e.g.
    # @pytest.mark.skip) never gets a "call" report at all — reproduces the
    # weekly-refresh failure against tests/services/daily_candles/
    # test_migration_round_trip.py on the current tree.
    shard = _shard(
        tmp_path / "shard.json", durations={_NODE_A: 0.01}, not_called=[_NODE_B]
    )
    collected = _write_manifest(tmp_path / "collected.txt", _NODE_A, _NODE_B)

    artifact = build_artifact(
        shard_paths=[shard], collected_paths=[collected], source_commit_sha="a" * 40
    )

    assert artifact["durations"] == {_NODE_A: 0.01}
    assert artifact["not_called"] == [_NODE_B]
    assert artifact["node_count"] == 2


def test_load_shard_rejects_overlap_between_durations_and_not_called(
    tmp_path: Path,
) -> None:
    shard = _shard(
        tmp_path / "shard.json", durations={_NODE_A: 0.01}, not_called=[_NODE_A]
    )
    collected = _write_manifest(tmp_path / "collected.txt", _NODE_A)

    with pytest.raises(
        ValueError, match="present in both 'durations' and 'not_called'"
    ):
        merge_call_duration_shards(shard_paths=[shard], collected_paths=[collected])


def test_merge_rejects_duplicate_across_shards(tmp_path: Path) -> None:
    shard1 = _shard(tmp_path / "shard1.json", durations={_NODE_A: 0.01})
    shard2 = _shard(tmp_path / "shard2.json", durations={_NODE_A: 0.02})
    collected = _write_manifest(tmp_path / "collected.txt", _NODE_A)

    with pytest.raises(ValueError, match="duplicate node id"):
        merge_call_duration_shards(
            shard_paths=[shard1, shard2], collected_paths=[collected]
        )


def test_merge_rejects_contradiction_across_shards(tmp_path: Path) -> None:
    # Node measured in one shard, reported not-called in another — a real
    # inconsistency (e.g. flaky infra), not a legitimate setup-skip.
    shard1 = _shard(tmp_path / "shard1.json", durations={_NODE_A: 0.01})
    shard2 = _shard(tmp_path / "shard2.json", not_called=[_NODE_A])
    collected = _write_manifest(tmp_path / "collected.txt", _NODE_A)

    with pytest.raises(ValueError, match="duplicate node id"):
        merge_call_duration_shards(
            shard_paths=[shard1, shard2], collected_paths=[collected]
        )


def test_merge_rejects_missing_measurement(tmp_path: Path) -> None:
    shard = _shard(tmp_path / "shard.json", durations={_NODE_A: 0.01})
    collected = _write_manifest(tmp_path / "collected.txt", _NODE_A, _NODE_B)

    with pytest.raises(ValueError, match="have no call-phase measurement"):
        merge_call_duration_shards(shard_paths=[shard], collected_paths=[collected])


def test_merge_still_fails_closed_when_not_called_present_but_node_wholly_absent(
    tmp_path: Path,
) -> None:
    # A not_called entry for one node must not paper over a genuinely
    # missing measurement for a different collected node (partial shard).
    shard = _shard(tmp_path / "shard.json", not_called=[_NODE_A])
    collected = _write_manifest(tmp_path / "collected.txt", _NODE_A, _NODE_B)

    with pytest.raises(ValueError, match="have no call-phase measurement"):
        merge_call_duration_shards(shard_paths=[shard], collected_paths=[collected])


@pytest.mark.parametrize("duration", [-0.01, float("nan")], ids=["negative", "nan"])
def test_merge_rejects_malformed_duration(tmp_path: Path, duration: float) -> None:
    shard = _shard(tmp_path / "shard.json", durations={_NODE_A: duration})
    collected = _write_manifest(tmp_path / "collected.txt", _NODE_A)

    with pytest.raises(ValueError, match="invalid duration"):
        merge_call_duration_shards(shard_paths=[shard], collected_paths=[collected])


def test_merge_rejects_duplicate_key_in_nested_shard_durations(tmp_path: Path) -> None:
    # ROB-1295 R2 (verifier P2-1): shard files got the same duplicate-key
    # protection as artifact files -- a hand-tampered/corrupt shard cannot
    # silently drop a measurement via Python's last-key-wins dict() parsing.
    shard = _write(
        tmp_path / "shard.json",
        '{"durations": {"a": 1.0, "a": 2.0}, "not_called": []}',
    )
    collected = _write_manifest(tmp_path / "collected.txt", "a")

    with pytest.raises(ValueError, match="duplicate key"):
        merge_call_duration_shards(shard_paths=[shard], collected_paths=[collected])


def test_merge_rejects_duplicate_key_at_shard_top_level(tmp_path: Path) -> None:
    shard = _write(
        tmp_path / "shard.json",
        '{"durations": {}, "not_called": [], "not_called": ["a"]}',
    )
    collected = _write_manifest(tmp_path / "collected.txt", "a")

    with pytest.raises(ValueError, match="duplicate key"):
        merge_call_duration_shards(shard_paths=[shard], collected_paths=[collected])


def test_merge_rejects_duplicate_not_called_entries_in_shard(tmp_path: Path) -> None:
    # ROB-1295 R2 (verifier P3): duplicate not_called list items are a
    # structural error, not silently collapsed by set(raw).
    shard = _shard(tmp_path / "shard.json", not_called=[_NODE_A, _NODE_A])
    collected = _write_manifest(tmp_path / "collected.txt", _NODE_A)

    with pytest.raises(ValueError, match="duplicate entries"):
        merge_call_duration_shards(shard_paths=[shard], collected_paths=[collected])


# --- load_artifact -----------------------------------------------------


def test_load_artifact_rejects_duplicate_key_in_nested_durations(
    tmp_path: Path,
) -> None:
    path = _write(
        tmp_path / "artifact.json",
        '{"schema_version": 2, "source_commit_sha": "a", '
        '"collection_hash": "b", "node_count": 0, "not_called": [], '
        '"durations": {"x": 1, "x": 2}}',
    )

    with pytest.raises(ValueError, match="duplicate key"):
        load_artifact(path)


def test_load_artifact_rejects_duplicate_key_at_top_level(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "artifact.json",
        '{"schema_version": 2, "schema_version": 3, "source_commit_sha": "a", '
        '"collection_hash": "b", "node_count": 0, "not_called": [], "durations": {}}',
    )

    with pytest.raises(ValueError, match="duplicate key"):
        load_artifact(path)


def test_load_artifact_rejects_missing_field(tmp_path: Path) -> None:
    path = _write_json(tmp_path / "artifact.json", {"schema_version": 2})

    with pytest.raises(ValueError, match="missing required field"):
        load_artifact(path)


@pytest.mark.parametrize(
    "schema_version", [1, 99, "2", True, None], ids=["v1", "v99", "str", "bool", "null"]
)
def test_load_artifact_rejects_unsupported_schema_version(
    tmp_path: Path, schema_version: object
) -> None:
    path = _write_json(
        tmp_path / "artifact.json",
        {
            "schema_version": schema_version,
            "source_commit_sha": "a" * 40,
            "collection_hash": "b",
            "node_count": 0,
            "durations": {},
            "not_called": [],
        },
    )

    with pytest.raises(ValueError, match="unsupported schema_version"):
        load_artifact(path)


# --- validate_freshness -------------------------------------------------


def test_validate_freshness_passes_for_freshly_built_artifact(tmp_path: Path) -> None:
    artifact = _build_two_node_artifact(tmp_path)

    validate_freshness(
        artifact,
        collected_nodes={_NODE_A, _NODE_B},
        expected_source_commit_sha="a" * 40,
    )


def test_validate_freshness_passes_with_not_called_entries(tmp_path: Path) -> None:
    shard = _shard(
        tmp_path / "shard.json", durations={_NODE_A: 0.01}, not_called=[_NODE_B]
    )
    collected_nodes = {_NODE_A, _NODE_B}
    artifact = build_artifact(
        shard_paths=[shard],
        collected_paths=[_write_manifest(tmp_path / "collected.txt", *collected_nodes)],
        source_commit_sha="a" * 40,
    )

    validate_freshness(
        artifact,
        collected_nodes=collected_nodes,
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
    # with the new collected set while durations/not_called still lack the
    # new node — e.g. a hand-edited or half-written artifact.
    artifact = _build_two_node_artifact(tmp_path)
    collected = {_NODE_A, _NODE_B, _NODE_C}
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


def test_validate_freshness_rejects_overlap_between_durations_and_not_called(
    tmp_path: Path,
) -> None:
    artifact = _build_two_node_artifact(tmp_path)
    artifact["not_called"] = [_NODE_A]

    with pytest.raises(
        ValueError, match="present in both 'durations' and 'not_called'"
    ):
        validate_freshness(
            artifact,
            collected_nodes={_NODE_A, _NODE_B},
            expected_source_commit_sha="a" * 40,
        )


@pytest.mark.parametrize(
    "schema_version", [1, 99, "2", True, None], ids=["v1", "v99", "str", "bool", "null"]
)
def test_validate_freshness_rejects_unsupported_schema_version(
    tmp_path: Path, schema_version: object
) -> None:
    artifact = _build_two_node_artifact(tmp_path)
    artifact["schema_version"] = schema_version

    with pytest.raises(ValueError, match="unsupported schema_version"):
        validate_freshness(
            artifact,
            collected_nodes={_NODE_A, _NODE_B},
            expected_source_commit_sha="a" * 40,
        )


def test_validate_freshness_rejects_duplicate_not_called_entries(
    tmp_path: Path,
) -> None:
    # build_artifact can never produce this (it always writes sorted(set(...))
    # ), so hand-tamper a freshly built artifact to prove validate_freshness
    # independently rejects a hand-edited duplicate rather than trusting
    # build-time uniqueness.
    shard = _shard(
        tmp_path / "shard.json", durations={_NODE_A: 0.01}, not_called=[_NODE_B]
    )
    collected_nodes = {_NODE_A, _NODE_B}
    artifact = build_artifact(
        shard_paths=[shard],
        collected_paths=[_write_manifest(tmp_path / "collected.txt", *collected_nodes)],
        source_commit_sha="a" * 40,
    )
    artifact["not_called"] = [_NODE_B, _NODE_B]

    with pytest.raises(ValueError, match="duplicate entries"):
        validate_freshness(
            artifact,
            collected_nodes=collected_nodes,
            expected_source_commit_sha="a" * 40,
        )


def test_validate_freshness_rejects_node_count_mismatch(tmp_path: Path) -> None:
    artifact = _build_two_node_artifact(tmp_path)
    artifact["node_count"] = 999

    with pytest.raises(ValueError, match="node_count mismatch"):
        validate_freshness(
            artifact,
            collected_nodes={_NODE_A, _NODE_B},
            expected_source_commit_sha="a" * 40,
        )


def test_build_artifact_node_count_equals_durations_plus_not_called(
    tmp_path: Path,
) -> None:
    shard = _shard(
        tmp_path / "shard.json", durations={_NODE_A: 0.01}, not_called=[_NODE_B]
    )
    collected_nodes = {_NODE_A, _NODE_B}
    artifact = build_artifact(
        shard_paths=[shard],
        collected_paths=[_write_manifest(tmp_path / "collected.txt", *collected_nodes)],
        source_commit_sha="a" * 40,
    )

    assert artifact["node_count"] == len(artifact["durations"]) + len(
        artifact["not_called"]
    )
    assert artifact["node_count"] == len(collected_nodes)

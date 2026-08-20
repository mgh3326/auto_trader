from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.ci.file_shard_plan import (
    ShardPlanError,
    assign_files_to_shards,
    collected_files_from_nodes,
    compute_file_weights,
    file_of_node_id,
    load_all_shard_manifests,
    load_collected_node_manifest,
    main,
    validate_exact_cover,
    write_shard_manifests,
)

pytestmark = pytest.mark.unit


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def _write_nodes(path: Path, *node_ids: str) -> Path:
    return _write(path, "\n".join(node_ids) + "\n")


# --------------------------------------------------------------------------
# file_of_node_id
# --------------------------------------------------------------------------


def test_file_of_node_id_extracts_file_path() -> None:
    assert file_of_node_id("tests/a/test_b.py::TestC::test_d[x]") == "tests/a/test_b.py"


@pytest.mark.parametrize(
    "node_id",
    [
        "tests/a/test_b.py",  # no '::'
        "app/a.py::test_b",  # outside tests/
        "tests/a.txt::test_b",  # not .py
    ],
)
def test_file_of_node_id_rejects_malformed(node_id: str) -> None:
    with pytest.raises(ShardPlanError):
        file_of_node_id(node_id)


# --------------------------------------------------------------------------
# load_collected_node_manifest
# --------------------------------------------------------------------------


def test_load_collected_node_manifest_rejects_duplicates(tmp_path: Path) -> None:
    path = _write_nodes(
        tmp_path / "nodes.txt",
        "tests/a.py::test_x",
        "tests/a.py::test_x",
    )
    with pytest.raises(ShardPlanError, match="duplicate node id"):
        load_collected_node_manifest(path)


def test_load_collected_node_manifest_rejects_blank_lines(tmp_path: Path) -> None:
    path = _write(tmp_path / "nodes.txt", "tests/a.py::test_x\n\ntests/a.py::test_y\n")
    with pytest.raises(ShardPlanError, match="blank line"):
        load_collected_node_manifest(path)


def test_load_collected_node_manifest_rejects_empty_file(tmp_path: Path) -> None:
    path = _write(tmp_path / "nodes.txt", "")
    with pytest.raises(ShardPlanError, match="must not be empty"):
        load_collected_node_manifest(path)


# --------------------------------------------------------------------------
# compute_file_weights (fallback rules)
# --------------------------------------------------------------------------


def test_compute_file_weights_sums_measured_durations() -> None:
    nodes = ["tests/a.py::test_1", "tests/a.py::test_2", "tests/b.py::test_1"]
    weights = compute_file_weights(
        collected_node_ids=nodes,
        durations={
            "tests/a.py::test_1": 1.0,
            "tests/a.py::test_2": 2.5,
            "tests/b.py::test_1": 0.1,
        },
        not_called=set(),
    )
    assert weights == {
        "tests/a.py": pytest.approx(3.5),
        "tests/b.py": pytest.approx(0.1),
    }


def test_compute_file_weights_not_called_is_zero() -> None:
    nodes = ["tests/a.py::test_1", "tests/a.py::test_2"]
    weights = compute_file_weights(
        collected_node_ids=nodes,
        durations={"tests/a.py::test_1": 4.0},
        not_called={"tests/a.py::test_2"},
    )
    assert weights == {"tests/a.py": pytest.approx(4.0)}


def test_compute_file_weights_unmeasured_uses_mean_of_measured() -> None:
    nodes = ["tests/a.py::test_1", "tests/b.py::test_new"]
    weights = compute_file_weights(
        collected_node_ids=nodes,
        durations={"tests/a.py::test_1": 2.0, "tests/other.py::test_x": 6.0},
        not_called=set(),
    )
    # mean of all measured durations in the artifact = (2.0 + 6.0) / 2 = 4.0
    assert weights["tests/b.py"] == pytest.approx(4.0)


def test_compute_file_weights_unmeasured_default_when_artifact_empty() -> None:
    nodes = ["tests/a.py::test_new"]
    weights = compute_file_weights(
        collected_node_ids=nodes, durations={}, not_called=set()
    )
    assert weights["tests/a.py"] == pytest.approx(1.0)


def test_compute_file_weights_is_order_independent_deterministic() -> None:
    nodes_a = ["tests/a.py::test_1", "tests/a.py::test_2", "tests/b.py::test_1"]
    nodes_b = list(reversed(nodes_a))
    durations = {
        "tests/a.py::test_1": 0.1,
        "tests/a.py::test_2": 0.2,
        "tests/b.py::test_1": 0.3,
    }
    w1 = compute_file_weights(
        collected_node_ids=nodes_a, durations=durations, not_called=set()
    )
    w2 = compute_file_weights(
        collected_node_ids=nodes_b, durations=durations, not_called=set()
    )
    assert w1 == w2


# --------------------------------------------------------------------------
# assign_files_to_shards (deterministic LPT)
# --------------------------------------------------------------------------


def test_assign_files_to_shards_is_deterministic_lpt() -> None:
    weights = {"a.py": 5.0, "b.py": 5.0, "c.py": 3.0, "d.py": 1.0}
    shards, totals = assign_files_to_shards(weights, shard_count=2)
    # Visit order (desc weight, path tie-break): a.py, b.py, c.py, d.py.
    # a.py -> shard0 (totals [5.0, 0.0]).
    # b.py -> shard1, the only min (totals [5.0, 5.0]).
    # c.py -> tie on totals -> lowest index -> shard0 (totals [8.0, 5.0]).
    # d.py -> shard1, the min (totals [8.0, 6.0]).
    assert shards[0] == ["a.py", "c.py"]
    assert shards[1] == ["b.py", "d.py"]
    assert totals[0] == pytest.approx(8.0)
    assert totals[1] == pytest.approx(6.0)


def test_assign_files_to_shards_regenerate_twice_is_identical() -> None:
    weights = {f"tests/f{i}.py": float(i % 7) for i in range(50)}
    shards1, totals1 = assign_files_to_shards(weights, shard_count=4)
    shards2, totals2 = assign_files_to_shards(weights, shard_count=4)
    assert shards1 == shards2
    assert totals1 == totals2


def test_assign_files_to_shards_rejects_invalid_shard_count() -> None:
    with pytest.raises(ShardPlanError):
        assign_files_to_shards({"a.py": 1.0}, shard_count=0)


# --------------------------------------------------------------------------
# Committed manifest I/O (ci_shards/shard-N.txt)
# --------------------------------------------------------------------------


def test_write_and_load_shard_manifests_round_trip(tmp_path: Path) -> None:
    shards = [["tests/a.py", "tests/b.py"], ["tests/c.py"]]
    write_shard_manifests(tmp_path, shards)
    loaded = load_all_shard_manifests(tmp_path, shard_count=2)
    assert loaded[1] == ["tests/a.py", "tests/b.py"]
    assert loaded[2] == ["tests/c.py"]


@pytest.mark.parametrize(
    "contents,error",
    [
        ("tests/a.py\n\ntests/b.py\n", "blank line"),
        (" tests/a.py\n", "whitespace"),
        ("/abs/tests/a.py\n", "absolute path"),
        ("tests/../secrets.py\n", "traversal"),
        ("app/a.py\n", "tests/\\*\\*/\\*.py path"),
        ("tests/a.txt\n", "tests/\\*\\*/\\*.py path"),
        ("tests/a.py\ntests/a.py\n", "duplicate entries within manifest"),
        ("tests/b.py\ntests/a.py\n", "not in canonical sorted order"),
    ],
)
def test_load_shard_manifest_rejects_malformed_entries(
    tmp_path: Path, contents: str, error: str
) -> None:
    _write(tmp_path / "shard-1.txt", contents)
    with pytest.raises(ShardPlanError, match=error):
        load_all_shard_manifests(tmp_path, shard_count=1)


def test_load_shard_manifest_missing_file_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(ShardPlanError, match="does not exist"):
        load_all_shard_manifests(tmp_path, shard_count=1)


# --------------------------------------------------------------------------
# validate_exact_cover
# --------------------------------------------------------------------------


def test_validate_exact_cover_accepts_a_true_partition() -> None:
    shard_files = {1: ["tests/a.py"], 2: ["tests/b.py", "tests/c.py"]}
    validate_exact_cover(
        shard_files=shard_files,
        authoritative_files={"tests/a.py", "tests/b.py", "tests/c.py"},
    )


def test_validate_exact_cover_rejects_missing_file() -> None:
    shard_files = {1: ["tests/a.py"], 2: ["tests/b.py"]}
    with pytest.raises(ShardPlanError, match="missing from every manifest"):
        validate_exact_cover(
            shard_files=shard_files,
            authoritative_files={"tests/a.py", "tests/b.py", "tests/c.py"},
        )


def test_validate_exact_cover_rejects_cross_shard_duplicate() -> None:
    shard_files = {1: ["tests/a.py"], 2: ["tests/a.py"]}
    with pytest.raises(ShardPlanError, match="assigned to more than one shard"):
        validate_exact_cover(
            shard_files=shard_files, authoritative_files={"tests/a.py"}
        )


def test_validate_exact_cover_rejects_stale_entry() -> None:
    shard_files = {1: ["tests/a.py"], 2: ["tests/removed.py"]}
    with pytest.raises(ShardPlanError, match="not in current collection"):
        validate_exact_cover(
            shard_files=shard_files, authoritative_files={"tests/a.py"}
        )


def test_validate_exact_cover_rejects_empty_shard() -> None:
    shard_files = {1: ["tests/a.py"], 2: []}
    with pytest.raises(ShardPlanError, match="empty shard"):
        validate_exact_cover(
            shard_files=shard_files, authoritative_files={"tests/a.py"}
        )


def test_validate_exact_cover_reports_all_violations_together() -> None:
    # missing (tests/c.py), stale (tests/removed.py), and empty shard 2 all
    # at once -- the error message must mention every category, not just the
    # first one hit.
    shard_files = {1: ["tests/a.py", "tests/removed.py"], 2: []}
    with pytest.raises(ShardPlanError) as excinfo:
        validate_exact_cover(
            shard_files=shard_files,
            authoritative_files={"tests/a.py", "tests/c.py"},
        )
    message = str(excinfo.value)
    assert "missing from every manifest" in message
    assert "not in current collection" in message
    assert "empty shard" in message


# --------------------------------------------------------------------------
# CLI: generate + check round-trip, mutants
# --------------------------------------------------------------------------


def _call_durations_artifact(
    durations: dict[str, float], not_called: list[str]
) -> dict:
    return {
        "schema_version": 2,
        "source_commit_sha": "deadbeef",
        "collection_hash": "irrelevant-for-planner",
        "node_count": len(durations) + len(not_called),
        "durations": durations,
        "not_called": not_called,
    }


def test_cli_generate_then_check_round_trip(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    nodes = [
        "tests/a.py::test_1",
        "tests/a.py::test_2",
        "tests/b.py::test_1",
        "tests/c.py::test_1",
    ]
    collected_path = _write_nodes(tmp_path / "collected.txt", *nodes)
    call_durations_path = tmp_path / "call_durations.json"
    call_durations_path.write_text(
        json.dumps(
            _call_durations_artifact(
                {
                    "tests/a.py::test_1": 1.0,
                    "tests/a.py::test_2": 1.0,
                    "tests/b.py::test_1": 0.5,
                },
                ["tests/c.py::test_1"],
            )
        ),
        encoding="utf-8",
    )
    manifest_dir = tmp_path / "ci_shards"

    exit_code = main(
        [
            "generate",
            "--call-durations",
            str(call_durations_path),
            "--collected",
            str(collected_path),
            "--manifest-dir",
            str(manifest_dir),
            "--shard-count",
            "2",
        ]
    )
    assert exit_code == 0
    capsys.readouterr()

    check_exit = main(
        [
            "check",
            "--collected",
            str(collected_path),
            "--manifest-dir",
            str(manifest_dir),
            "--shard-count",
            "2",
        ]
    )
    assert check_exit == 0
    out = capsys.readouterr().out
    assert "exact-cover OK" in out

    weights = json.loads((manifest_dir / "weights.json").read_text(encoding="utf-8"))
    assert weights["authoritative_file_count"] == 3
    assert len(weights["shards"]) == 2


def test_cli_generate_twice_is_byte_identical(tmp_path: Path) -> None:
    nodes = [f"tests/f{i}.py::test_{i % 3}" for i in range(30)]
    collected_path = _write_nodes(tmp_path / "collected.txt", *nodes)
    call_durations_path = tmp_path / "call_durations.json"
    call_durations_path.write_text(
        json.dumps(
            _call_durations_artifact(
                {node: float((i * 7) % 11) / 10 for i, node in enumerate(nodes)}, []
            )
        ),
        encoding="utf-8",
    )
    manifest_dir_1 = tmp_path / "ci_shards_1"
    manifest_dir_2 = tmp_path / "ci_shards_2"

    for manifest_dir in (manifest_dir_1, manifest_dir_2):
        assert (
            main(
                [
                    "generate",
                    "--call-durations",
                    str(call_durations_path),
                    "--collected",
                    str(collected_path),
                    "--manifest-dir",
                    str(manifest_dir),
                    "--shard-count",
                    "4",
                ]
            )
            == 0
        )

    for index in range(1, 5):
        a = (manifest_dir_1 / f"shard-{index}.txt").read_bytes()
        b = (manifest_dir_2 / f"shard-{index}.txt").read_bytes()
        assert a == b

    weights_a = (manifest_dir_1 / "weights.json").read_bytes()
    weights_b = (manifest_dir_2 / "weights.json").read_bytes()
    assert weights_a == weights_b


def test_cli_generate_rejects_malformed_call_durations_artifact(
    tmp_path: Path,
) -> None:
    # Not routed through the strict call_durations.py loader would silently
    # accept this (duplicate JSON key, last-key-wins) instead of failing
    # closed.
    nodes = ["tests/a.py::test_1"]
    collected_path = _write_nodes(tmp_path / "collected.txt", *nodes)
    call_durations_path = _write(
        tmp_path / "call_durations.json",
        '{"schema_version": 2, "source_commit_sha": "x", '
        '"collection_hash": "y", "node_count": 1, "not_called": [], '
        '"durations": {"tests/a.py::test_1": 1.0, "tests/a.py::test_1": 2.0}}',
    )

    exit_code = main(
        [
            "generate",
            "--call-durations",
            str(call_durations_path),
            "--collected",
            str(collected_path),
            "--manifest-dir",
            str(tmp_path / "ci_shards"),
            "--shard-count",
            "1",
        ]
    )
    assert exit_code != 0


def test_cli_generate_fails_closed_when_files_fewer_than_shards(
    tmp_path: Path,
) -> None:
    nodes = ["tests/a.py::test_1", "tests/b.py::test_1"]
    collected_path = _write_nodes(tmp_path / "collected.txt", *nodes)
    call_durations_path = tmp_path / "call_durations.json"
    call_durations_path.write_text(
        json.dumps(
            _call_durations_artifact(
                {"tests/a.py::test_1": 1.0, "tests/b.py::test_1": 1.0}, []
            )
        ),
        encoding="utf-8",
    )
    manifest_dir = tmp_path / "ci_shards"

    exit_code = main(
        [
            "generate",
            "--call-durations",
            str(call_durations_path),
            "--collected",
            str(collected_path),
            "--manifest-dir",
            str(manifest_dir),
            "--shard-count",
            "4",  # only 2 files for 4 shards -> at least 2 empty shards
        ]
    )
    assert exit_code != 0
    # Nothing should have been written -- self-check runs before write.
    assert not manifest_dir.exists()


def test_cli_check_fails_closed_on_new_file_mutant(tmp_path: Path) -> None:
    nodes = ["tests/a.py::test_1", "tests/b.py::test_1"]
    manifest_dir = tmp_path / "ci_shards"
    write_shard_manifests(manifest_dir, [["tests/a.py"], ["tests/b.py"]])

    # Mutant: a new test file was added and collected, but no manifest was
    # regenerated to include it.
    mutated_nodes = nodes + ["tests/new_file.py::test_1"]
    mutated_collected = _write_nodes(tmp_path / "mutated_collected.txt", *mutated_nodes)

    exit_code = main(
        [
            "check",
            "--collected",
            str(mutated_collected),
            "--manifest-dir",
            str(manifest_dir),
            "--shard-count",
            "2",
        ]
    )
    assert exit_code != 0


def test_cli_check_fails_closed_on_deleted_file_mutant(tmp_path: Path) -> None:
    manifest_dir = tmp_path / "ci_shards"
    write_shard_manifests(manifest_dir, [["tests/a.py"], ["tests/b.py"]])

    # Mutant: tests/b.py was deleted from the tree but its manifest entry
    # was never removed (stale entry).
    remaining_nodes = ["tests/a.py::test_1"]
    remaining_collected = _write_nodes(
        tmp_path / "remaining_collected.txt", *remaining_nodes
    )

    exit_code = main(
        [
            "check",
            "--collected",
            str(remaining_collected),
            "--manifest-dir",
            str(manifest_dir),
            "--shard-count",
            "2",
        ]
    )
    assert exit_code != 0


def test_cli_check_fails_closed_on_duplicate_assignment_mutant(tmp_path: Path) -> None:
    nodes = ["tests/a.py::test_1", "tests/b.py::test_1"]
    collected_path = _write_nodes(tmp_path / "collected.txt", *nodes)
    manifest_dir = tmp_path / "ci_shards"
    # Mutant: tests/a.py duplicated into both shards.
    write_shard_manifests(manifest_dir, [["tests/a.py"], ["tests/a.py", "tests/b.py"]])

    exit_code = main(
        [
            "check",
            "--collected",
            str(collected_path),
            "--manifest-dir",
            str(manifest_dir),
            "--shard-count",
            "2",
        ]
    )
    assert exit_code != 0


def test_collected_files_from_nodes_projects_unique_files() -> None:
    files = collected_files_from_nodes(
        ["tests/a.py::test_1", "tests/a.py::test_2", "tests/b.py::test_1"]
    )
    assert files == {"tests/a.py", "tests/b.py"}

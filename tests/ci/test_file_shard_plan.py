from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.ci.file_shard_plan import (
    DEFAULT_WORKERS_PER_SHARD,
    ShardPlanError,
    assign_files_to_shards,
    collected_files_from_nodes,
    compute_file_weights,
    file_of_node_id,
    load_all_shard_manifests,
    load_collected_node_manifest,
    load_test_durations,
    main,
    predict_shard_pytest_seconds,
    validate_exact_cover,
    write_shard_manifests,
)

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def _write_nodes(path: Path, *node_ids: str) -> Path:
    return _write(path, "\n".join(node_ids) + "\n")


def _write_durations(path: Path, durations: dict[str, float]) -> Path:
    path.write_text(json.dumps(durations), encoding="utf-8")
    return path


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
# load_test_durations (strict whole-protocol artifact reader)
# --------------------------------------------------------------------------


def test_load_test_durations_reads_the_map(tmp_path: Path) -> None:
    path = _write_durations(
        tmp_path / ".test_durations",
        {"tests/a.py::test_1": 1.5, "tests/a.py::test_2": 2},
    )
    assert load_test_durations(path) == {
        "tests/a.py::test_1": 1.5,
        "tests/a.py::test_2": 2.0,
    }


def test_load_test_durations_allows_an_empty_map(tmp_path: Path) -> None:
    # A bootstrap artifact with no measurements yet is valid input; its nodes
    # fall back to UNMEASURED_FALLBACK_DEFAULT_SECONDS.
    path = _write_durations(tmp_path / ".test_durations", {})
    assert load_test_durations(path) == {}


def test_load_test_durations_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    # json.loads would silently keep the last value; a hand-edited or
    # corrupted artifact must not be able to smuggle that past generate.
    path = _write(
        tmp_path / ".test_durations",
        '{"tests/a.py::test_1": 1.0, "tests/a.py::test_1": 2.0}',
    )
    with pytest.raises(ValueError, match="duplicate key"):
        load_test_durations(path)


@pytest.mark.parametrize(
    "text,error",
    [
        ("[1, 2, 3]", "expected a JSON object"),
        ('{"tests/a.py::test_1": true}', "invalid duration"),
        ('{"tests/a.py::test_1": "1.0"}', "invalid duration"),
        ('{"tests/a.py::test_1": -1.0}', "invalid duration"),
        ('{"tests/a.py::test_1": Infinity}', "invalid duration"),
        ('{"tests/a.py::test_1": NaN}', "invalid duration"),
        ('{"": 1.0}', "invalid node id"),
    ],
)
def test_load_test_durations_rejects_malformed_entries(
    tmp_path: Path, text: str, error: str
) -> None:
    path = _write(tmp_path / ".test_durations", text)
    with pytest.raises(ValueError, match=error):
        load_test_durations(path)


# --------------------------------------------------------------------------
# compute_file_weights
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
    )
    assert weights == {
        "tests/a.py": pytest.approx(3.5),
        "tests/b.py": pytest.approx(0.1),
    }


def test_compute_file_weights_unmeasured_uses_mean_of_measured() -> None:
    nodes = ["tests/a.py::test_1", "tests/b.py::test_new"]
    weights = compute_file_weights(
        collected_node_ids=nodes,
        durations={"tests/a.py::test_1": 2.0, "tests/other.py::test_x": 6.0},
    )
    # mean of all measured durations in the artifact = (2.0 + 6.0) / 2 = 4.0
    assert weights["tests/b.py"] == pytest.approx(4.0)


def test_compute_file_weights_unmeasured_default_when_artifact_empty() -> None:
    nodes = ["tests/a.py::test_new"]
    weights = compute_file_weights(collected_node_ids=nodes, durations={})
    assert weights["tests/a.py"] == pytest.approx(1.0)


def test_compute_file_weights_include_setup_and_teardown_seconds() -> None:
    # The artifact value is the whole per-test protocol (setup + call +
    # teardown), so a node whose call phase was ~0 but whose setup paid the
    # session bootstrap is weighted at its real cost. This is the assertion a
    # "revert the weight input to the call phase" mutant breaks: it would see
    # 0.001 s where this asserts 5.0 s.
    nodes = ["tests/a.py::test_bootstraps_session"]
    weights = compute_file_weights(
        collected_node_ids=nodes,
        durations={"tests/a.py::test_bootstraps_session": 5.0},
    )
    assert weights["tests/a.py"] == pytest.approx(5.0)
    assert weights["tests/a.py"] != pytest.approx(0.001)


def test_compute_file_weights_float_summation_order_is_sorted() -> None:
    # The documented contract is "accumulate in sorted node-id order so float
    # summation is reproducible". 0.1 + 0.2 + 0.3 is 0.6000000000000001 but
    # 0.3 + 0.2 + 0.1 is 0.6, so a mutant that iterates the caller's list
    # order (or an unsorted set) is red here rather than merely unstable.
    values = {
        "tests/a.py::test_1": 0.1,
        "tests/a.py::test_2": 0.2,
        "tests/a.py::test_3": 0.3,
    }
    forward = compute_file_weights(collected_node_ids=list(values), durations=values)
    reverse = compute_file_weights(
        collected_node_ids=list(reversed(values)), durations=values
    )
    assert forward == reverse
    assert forward["tests/a.py"] == 0.1 + 0.2 + 0.3


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
# predict_shard_pytest_seconds (--dist=loadfile makespan)
# --------------------------------------------------------------------------


def test_predict_shard_pytest_seconds_spreads_even_files_over_workers() -> None:
    weights = {f"tests/f{i}.py": 1.0 for i in range(8)}
    assert predict_shard_pytest_seconds(list(weights), weights) == pytest.approx(2.0)


def test_predict_shard_pytest_seconds_is_never_below_the_largest_file() -> None:
    # This is the whole reason `weights.json` reports a prediction: with
    # --dist=loadfile one 301 s file bounds its shard at 301 s however
    # balanced the four-worker totals are.
    weights = {"tests/a.py": 301.0}
    weights.update({f"tests/s{i:02d}.py": 1.0 for i in range(40)})
    assert predict_shard_pytest_seconds(list(weights), weights) >= 301.0


def test_predict_shard_pytest_seconds_single_file_shard_is_that_file() -> None:
    weights = {"tests/a.py": 301.0}
    assert predict_shard_pytest_seconds(["tests/a.py"], weights) == pytest.approx(301.0)


def test_predict_shard_pytest_seconds_one_worker_is_the_sum() -> None:
    weights = {"tests/a.py": 1.0, "tests/b.py": 2.0}
    assert predict_shard_pytest_seconds(
        list(weights), weights, workers=1
    ) == pytest.approx(3.0)


def test_predict_shard_pytest_seconds_rejects_invalid_worker_count() -> None:
    with pytest.raises(ShardPlanError):
        predict_shard_pytest_seconds([], {}, workers=0)


def test_predict_shard_pytest_seconds_defaults_to_the_ci_worker_count() -> None:
    assert DEFAULT_WORKERS_PER_SHARD == 4


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
    assert "minimally edit exactly one" in message
    assert "reserve `file_shard_plan generate` for the duration-refresh" in message


# --------------------------------------------------------------------------
# CLI: generate + check round-trip, mutants
# --------------------------------------------------------------------------


def _run_generate(
    tmp_path: Path,
    *,
    durations: object,
    collected_nodes: list[str],
    shard_count: int = 1,
    manifest_dir: Path | None = None,
) -> int:
    durations_path = tmp_path / ".test_durations"
    durations_path.write_text(json.dumps(durations), encoding="utf-8")
    collected_path = _write_nodes(tmp_path / "collected.txt", *collected_nodes)
    return main(
        [
            "generate",
            "--durations",
            str(durations_path),
            "--collected",
            str(collected_path),
            "--manifest-dir",
            str(manifest_dir or tmp_path / "ci_shards"),
            "--repo-root",
            str(tmp_path),
            "--shard-count",
            str(shard_count),
        ]
    )


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
    durations_path = _write_durations(
        tmp_path / ".test_durations",
        {
            "tests/a.py::test_1": 1.0,
            "tests/a.py::test_2": 1.0,
            "tests/b.py::test_1": 0.5,
            "tests/c.py::test_1": 0.25,
        },
    )
    manifest_dir = tmp_path / "ci_shards"

    exit_code = main(
        [
            "generate",
            "--durations",
            str(durations_path),
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
    assert weights["weight_source"] == ".test_durations"
    assert weights["prediction"]["workers_per_shard"] == DEFAULT_WORKERS_PER_SHARD
    assert "not_called_seconds" not in weights["fallback"]
    # The weight is the measured whole-protocol time, not a fallback constant.
    # A mutant that weights the call phase instead (this artifact holds no
    # call-phase values) reports the fallback for every node and is red here.
    assert sum(s["total_weight_seconds"] for s in weights["shards"]) == pytest.approx(
        2.75
    )


def test_cli_generate_twice_is_byte_identical(tmp_path: Path) -> None:
    nodes = [f"tests/f{i}.py::test_{i % 3}" for i in range(30)]
    collected_path = _write_nodes(tmp_path / "collected.txt", *nodes)
    durations_path = _write_durations(
        tmp_path / ".test_durations",
        {node: float((i * 7) % 11) / 10 for i, node in enumerate(nodes)},
    )
    manifest_dir_1 = tmp_path / "ci_shards_1"
    manifest_dir_2 = tmp_path / "ci_shards_2"

    for manifest_dir in (manifest_dir_1, manifest_dir_2):
        assert (
            main(
                [
                    "generate",
                    "--durations",
                    str(durations_path),
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


def test_cli_generate_is_byte_identical_across_hash_seeds(tmp_path: Path) -> None:
    # Cross-process determinism: an assignment built by iterating an unsorted
    # set varies with PYTHONHASHSEED. Same inputs must give the same bytes.
    nodes = [f"tests/f{i}.py::test_{i % 5}" for i in range(40)]
    collected_path = _write_nodes(tmp_path / "collected.txt", *nodes)
    durations_path = _write_durations(
        tmp_path / ".test_durations",
        {node: float((i * 13) % 17) / 10 for i, node in enumerate(nodes)},
    )

    outputs: list[dict[str, bytes]] = []
    for seed in ("0", "1"):
        manifest_dir = tmp_path / f"ci_shards_seed{seed}"
        env = {**os.environ, "PYTHONHASHSEED": seed}
        completed = subprocess.run(  # noqa: S603 - fixed argv, test-local dirs
            [
                sys.executable,
                "-m",
                "scripts.ci.file_shard_plan",
                "generate",
                "--durations",
                str(durations_path),
                "--collected",
                str(collected_path),
                "--manifest-dir",
                str(manifest_dir),
                "--shard-count",
                "4",
            ],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        outputs.append(
            {
                name: (manifest_dir / name).read_bytes()
                for name in (
                    "shard-1.txt",
                    "shard-2.txt",
                    "shard-3.txt",
                    "shard-4.txt",
                    "weights.json",
                )
            }
        )

    assert outputs[0] == outputs[1]


def test_cli_generate_rejects_malformed_durations_artifact(
    tmp_path: Path,
) -> None:
    nodes = ["tests/a.py::test_1"]
    collected_path = _write_nodes(tmp_path / "collected.txt", *nodes)
    durations_path = _write(
        tmp_path / ".test_durations",
        '{"tests/a.py::test_1": 1.0, "tests/a.py::test_1": 2.0}',
    )

    exit_code = main(
        [
            "generate",
            "--durations",
            str(durations_path),
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
    exit_code = _run_generate(
        tmp_path,
        durations={"tests/a.py::test_1": 1.0, "tests/b.py::test_1": 1.0},
        collected_nodes=["tests/a.py::test_1", "tests/b.py::test_1"],
        shard_count=4,  # only 2 files for 4 shards -> at least 2 empty shards
    )
    assert exit_code != 0
    # Nothing should have been written -- self-check runs before write.
    assert not (tmp_path / "ci_shards").exists()


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


# --- stale-artifact tolerance (positive control) ---------------------------
#
# `generate` must NOT require the artifact's node set to equal today's fresh
# authoritative collection: a stale-but-well-formed map is valid input and a
# brand-new node is covered by deterministic fallback weight. This is what a
# normal PR's one-line manifest edit relies on between refreshes.


def test_cli_generate_tolerates_stale_but_well_formed_artifact(
    tmp_path: Path,
) -> None:
    old_node = "tests/a.py::test_1"
    new_node = "tests/b_new.py::test_1"
    exit_code = _run_generate(
        tmp_path,
        durations={old_node: 1.0},
        collected_nodes=[old_node, new_node],
    )
    assert exit_code == 0
    manifest = (tmp_path / "ci_shards" / "shard-1.txt").read_text()
    assert "tests/a.py" in manifest
    assert "tests/b_new.py" in manifest


def test_cli_generate_stale_artifact_fallback_is_measured_mean_and_check_then_passes(
    tmp_path: Path,
) -> None:
    node_a = "tests/a.py::test_1"
    node_b = "tests/b.py::test_1"
    new_node = "tests/c_new.py::test_1"
    collected_nodes = [node_a, node_b, new_node]
    collected_path = _write_nodes(tmp_path / "collected.txt", *collected_nodes)
    durations_path = _write_durations(
        tmp_path / ".test_durations",
        {node_a: 2.0, node_b: 6.0},  # mean = 4.0
    )
    manifest_dir = tmp_path / "ci_shards"

    generate_exit = main(
        [
            "generate",
            "--durations",
            str(durations_path),
            "--collected",
            str(collected_path),
            "--manifest-dir",
            str(manifest_dir),
            "--shard-count",
            "2",
        ]
    )
    assert generate_exit == 0

    weights = json.loads((manifest_dir / "weights.json").read_text(encoding="utf-8"))
    assert weights["fallback"]["unmeasured_node_seconds"] == pytest.approx(4.0)
    assert weights["authoritative_node_count"] == len(collected_nodes)

    shard_files = load_all_shard_manifests(manifest_dir, shard_count=2)
    owners = [
        index for index, files in shard_files.items() if "tests/c_new.py" in files
    ]
    assert owners == [owners[0]], "new file must be assigned to exactly one shard"

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


def test_cli_generate_writes_nothing_on_rejection(tmp_path: Path) -> None:
    # Fail-closed must mean *nothing written*, not "written then errored" --
    # a partially-written ci_shards/ from a rejected generate would be worse
    # than no ci_shards/ at all.
    exit_code = _run_generate(
        tmp_path,
        durations={"tests/a.py::test_1": -1.0},
        collected_nodes=["tests/a.py::test_1"],
    )
    assert exit_code != 0
    assert not (tmp_path / "ci_shards").exists()


# --- collection completeness (hk-3336) ------------------------------------
#
# A `pytest --collect-only` capture can lose a test file that the duration
# artifact measured while the file is still in the tree: a module that refuses
# to import under this environment raises a collection-time error, and
# `2>/dev/null | grep '^tests/.*::'` throws the ERRORS block away. Packing that
# capture drops the file from every manifest -- exact cover only notices later,
# in CI. The working tree is what separates that from legitimate staleness.


def _write_test_file(repo_root: Path, relative: str) -> Path:
    path = repo_root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("def test_placeholder():\n    assert True\n", encoding="utf-8")
    return path


def test_cli_generate_fails_closed_when_collection_omits_a_measured_file(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    _write_test_file(tmp_path, "tests/services/research/test_uncollectable.py")
    exit_code = _run_generate(
        tmp_path,
        durations={
            "tests/services/research/test_uncollectable.py::test_x": 5.0,
            "tests/collected.py::test_y": 1.0,
        },
        collected_nodes=["tests/collected.py::test_y"],
    )
    assert exit_code != 0
    assert not (tmp_path / "ci_shards").exists()
    stderr = capsys.readouterr().err
    assert "missing 1 test file(s)" in stderr
    assert "tests/services/research/test_uncollectable.py" in stderr


def test_cli_generate_tolerates_a_measured_file_deleted_since_the_refresh(
    tmp_path: Path,
) -> None:
    # The file the artifact measured is gone from the tree: legitimate
    # staleness, absorbed by the mean-duration fallback, not a hard error.
    exit_code = _run_generate(
        tmp_path,
        durations={
            "tests/removed.py::test_x": 5.0,
            "tests/collected.py::test_y": 1.0,
        },
        collected_nodes=["tests/collected.py::test_y"],
    )
    assert exit_code == 0
    assert (
        tmp_path / "ci_shards" / "shard-1.txt"
    ).read_text() == "tests/collected.py\n"


def test_cli_generate_accepts_a_collection_that_covers_every_measured_file(
    tmp_path: Path,
) -> None:
    _write_test_file(tmp_path, "tests/present.py")
    exit_code = _run_generate(
        tmp_path,
        durations={"tests/present.py::test_x": 5.0},
        collected_nodes=["tests/present.py::test_x"],
    )
    assert exit_code == 0

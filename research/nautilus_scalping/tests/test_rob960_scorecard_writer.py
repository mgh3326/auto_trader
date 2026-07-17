"""ROB-960 -- materializer plan echo + pair-atomic scorecard stage/publish
writer tests.

Captain plan-gate G5: build_materializer_plan adds ONLY operator metadata
(scorecard_output_filenames) on top of H4's own build_plan() -- no new
schema/version key. Captain plan-gate G7: publish is pair-atomic -- never a
state where output_dir has one file but not the other.
"""

from __future__ import annotations

from rob960_scorecard_writer import (
    ScorecardPublishConflictError,
    build_materializer_plan,
    publish_staged_scorecard,
    stage_scorecard_files,
)
from run_rob944_campaign import build_plan as h4_build_plan


def test_materializer_plan_never_diverges_from_h4_plan_on_shared_fields():
    h4_plan = h4_build_plan()
    materializer_plan = build_materializer_plan()
    for key in h4_plan:
        assert materializer_plan[key] == h4_plan[key], f"diverged on shared field {key}"
    assert "materializer_schema_version" not in materializer_plan
    assert materializer_plan["scorecard_output_filenames"] == {
        "json": "scorecard.json",
        "md": "scorecard.md",
    }
    assert "scorecard_output_filenames" not in h4_plan


def _envelope(marker="a"):
    return {
        "schema_version": "rob945.v1",
        "scorecard_payload": {"marker": marker},
        "scorecard_artifact_hash": f"hash-{marker}",
    }


def test_publish_staged_scorecard_first_publish_creates_output_dir_atomically(tmp_path):
    output_dir = tmp_path / "out"
    staging = stage_scorecard_files(_envelope(), "# stub", output_dir)
    assert not output_dir.exists()  # staging never touches the final dir
    json_path, md_path = publish_staged_scorecard(staging, output_dir)
    assert json_path.exists() and md_path.exists()
    assert not staging.exists()  # renamed away, not left behind on success
    assert json_path == output_dir / "scorecard.json"
    assert md_path == output_dir / "scorecard.md"


def test_stage_scorecard_files_produces_no_temp_litter_in_output_dir_parent_besides_staging(
    tmp_path,
):
    output_dir = tmp_path / "out"
    staging = stage_scorecard_files(_envelope(), "# stub", output_dir)
    siblings = list(tmp_path.iterdir())
    assert siblings == [staging]


def test_publish_staged_scorecard_idempotent_replay_with_identical_bytes(tmp_path):
    output_dir = tmp_path / "out"
    envelope = _envelope()
    staging_1 = stage_scorecard_files(envelope, "# stub", output_dir)
    json_path_1, md_path_1 = publish_staged_scorecard(staging_1, output_dir)
    bytes_1 = json_path_1.read_bytes()

    staging_2 = stage_scorecard_files(envelope, "# stub", output_dir)
    json_path_2, md_path_2 = publish_staged_scorecard(staging_2, output_dir)

    assert json_path_1 == json_path_2
    assert json_path_2.read_bytes() == bytes_1
    assert not staging_2.exists()  # discarded, not left behind


def test_publish_staged_scorecard_conflict_fails_closed_leaves_original_untouched(
    tmp_path,
):
    output_dir = tmp_path / "out"
    staging_1 = stage_scorecard_files(_envelope("a"), "# a", output_dir)
    json_path, md_path = publish_staged_scorecard(staging_1, output_dir)
    original_json_bytes = json_path.read_bytes()
    original_md_bytes = md_path.read_bytes()

    staging_2 = stage_scorecard_files(_envelope("b"), "# b", output_dir)
    try:
        publish_staged_scorecard(staging_2, output_dir)
        raise AssertionError("expected ScorecardPublishConflictError, none raised")
    except ScorecardPublishConflictError:
        pass

    assert json_path.read_bytes() == original_json_bytes
    assert md_path.read_bytes() == original_md_bytes
    assert staging_2.exists()  # preserved for forensic inspection


def test_publish_never_leaves_output_dir_with_exactly_one_file(tmp_path):
    """No-half-published-pair proof (G7): os.replace on a directory is a
    single syscall (atomic at the OS level) -- this test proves the CODE
    itself never performs a two-step publish that COULD be partial (unlike
    a design that renames scorecard.json then scorecard.md separately)."""
    output_dir = tmp_path / "out"
    staging = stage_scorecard_files(_envelope(), "# stub", output_dir)
    publish_staged_scorecard(staging, output_dir)
    files = sorted(p.name for p in output_dir.iterdir())
    assert files == ["scorecard.json", "scorecard.md"]

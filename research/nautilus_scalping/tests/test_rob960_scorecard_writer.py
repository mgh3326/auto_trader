"""ROB-960 -- materializer plan echo + pair-atomic scorecard stage/publish
writer tests.

Captain plan-gate G5: build_materializer_plan adds ONLY operator metadata
(scorecard_output_filenames) on top of H4's own build_plan() -- no new
schema/version key. Captain plan-gate G7: publish is pair-atomic -- never a
state where output_dir has one file but not the other.
"""

from __future__ import annotations

import json

import pytest
import rob960_scorecard_writer
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


# ---------------------------------------------------------------------------
# Captain Task-4-hardening gate (2026-07-18 RED 6-7/G7): explicit UTF-8
# bytes + terminal newline, non-vacuous re-read verification, fault
# injection on the first-publish rename syscall, and parent-directory
# durability fsync.
# ---------------------------------------------------------------------------


def test_staged_json_is_explicit_utf8_bytes_with_terminal_newline_and_no_nan(tmp_path):
    output_dir = tmp_path / "out"
    envelope = _envelope("utf8-☃")  # snowman -- proves ensure_ascii=False
    staging = stage_scorecard_files(envelope, "# stub", output_dir)
    json_bytes = (staging / "scorecard.json").read_bytes()
    assert json_bytes.endswith(b"\n")
    decoded = json_bytes.decode("utf-8")  # raises UnicodeDecodeError if not valid UTF-8
    assert "☃" in decoded  # non-ASCII survived unescaped
    reparsed = json.loads(decoded)
    assert reparsed == envelope


def test_staged_markdown_is_explicit_utf8_bytes_with_terminal_newline(tmp_path):
    output_dir = tmp_path / "out"
    staging = stage_scorecard_files(
        _envelope(), "# stub, no trailing newline", output_dir
    )
    md_bytes = (staging / "scorecard.md").read_bytes()
    assert md_bytes.endswith(b"\n")
    assert md_bytes.decode("utf-8") == "# stub, no trailing newline\n"


def test_staging_verifies_written_bytes_by_reading_them_back(tmp_path, monkeypatch):
    """Non-vacuous: force the post-write re-read to observe DIFFERENT bytes
    than what was written (simulating silent filesystem corruption) and
    confirm stage_scorecard_files fails closed rather than trusting the
    write blindly."""
    from pathlib import Path

    real_read_bytes = Path.read_bytes

    def _corrupting_read_bytes(self):
        if self.name == "scorecard.json":
            return b"not what was written"
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", _corrupting_read_bytes)
    output_dir = tmp_path / "out"
    with pytest.raises(rob960_scorecard_writer.ScorecardStagingVerificationError):
        stage_scorecard_files(_envelope(), "# stub", output_dir)


def test_first_publish_failure_leaves_no_output_dir_and_preserves_staged_pair(
    tmp_path, monkeypatch
):
    """G7 Step 10: fault-inject the first-publish rename syscall itself --
    output_dir must not exist afterward, and the staging directory (with
    BOTH files still intact) must be preserved, never a half-final state."""
    output_dir = tmp_path / "out"
    staging = stage_scorecard_files(_envelope(), "# stub", output_dir)

    def _failing_replace(src, dst):
        raise OSError("simulated failure injected before the rename syscall completes")

    monkeypatch.setattr(rob960_scorecard_writer.os, "replace", _failing_replace)

    with pytest.raises(OSError):
        publish_staged_scorecard(staging, output_dir)

    assert not output_dir.exists()
    assert staging.exists()
    assert (staging / "scorecard.json").exists()
    assert (staging / "scorecard.md").exists()


def test_successful_first_publish_fsyncs_the_parent_directory(tmp_path, monkeypatch):
    """G7 Step 10 corollary: the new directory entry itself (not just the
    two files' contents) must be made durable via a parent-directory
    fsync after a successful rename."""
    output_dir = tmp_path / "out"
    staging = stage_scorecard_files(_envelope(), "# stub", output_dir)

    fsynced_dirs = []
    real_fsync_dir = rob960_scorecard_writer._fsync_dir

    def _recording_fsync_dir(path):
        fsynced_dirs.append(path)
        return real_fsync_dir(path)

    monkeypatch.setattr(rob960_scorecard_writer, "_fsync_dir", _recording_fsync_dir)
    publish_staged_scorecard(staging, output_dir)
    assert output_dir.parent in fsynced_dirs

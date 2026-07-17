"""ROB-960 -- materializer plan echo + pair-atomic scorecard.json/md
stage/publish writer.

Captain plan-gate G5: build_materializer_plan echoes H4's own build_plan()
verbatim plus ONLY pure operator metadata (scorecard_output_filenames) --
no new schema/version key anywhere.

Captain plan-gate G7: writing is split into stage_scorecard_files (both
files into a fresh sibling staging directory, fsync'd, never touches the
final output_dir) and publish_staged_scorecard (destination absent -> a
single atomic os.replace(staging_dir, output_dir) directory rename;
destination present with identical bytes -> idempotent no-op, staging
discarded; destination present with different bytes -> fail-closed,
existing pair left untouched, staging preserved for forensic inspection).
There is no two-separate-file-rename path anywhere -- failure injection at
any point in publish_staged_scorecard can never leave output_dir with
exactly one of the two files.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path


def build_materializer_plan() -> dict:
    from run_rob944_campaign import build_plan as h4_build_plan

    plan = dict(h4_build_plan())
    plan["scorecard_output_filenames"] = {
        "json": "scorecard.json",
        "md": "scorecard.md",
    }
    return plan


def _fsync_file(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def stage_scorecard_files(envelope: dict, markdown: str, output_dir: Path) -> Path:
    """Writes scorecard.json/scorecard.md into a fresh sibling staging
    directory and fsyncs both files + the directory itself. Never touches
    output_dir -- the caller must separately call publish_staged_scorecard
    to make this staged pair final."""
    output_dir = Path(output_dir)
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging_dir = Path(
        tempfile.mkdtemp(dir=output_dir.parent, prefix=f".{output_dir.name}.staging-")
    )
    json_path = staging_dir / "scorecard.json"
    md_path = staging_dir / "scorecard.md"
    json_path.write_text(json.dumps(envelope, indent=2, sort_keys=True))
    md_path.write_text(markdown)
    _fsync_file(json_path)
    _fsync_file(md_path)
    _fsync_dir(staging_dir)
    return staging_dir


class ScorecardPublishConflictError(RuntimeError):
    """output_dir already holds a DIFFERENT published scorecard pair --
    publish refuses to overwrite it (G7 fail-closed)."""


def publish_staged_scorecard(staging_dir: Path, output_dir: Path) -> tuple[Path, Path]:
    staging_dir = Path(staging_dir)
    output_dir = Path(output_dir)
    json_path = output_dir / "scorecard.json"
    md_path = output_dir / "scorecard.md"

    if not output_dir.exists():
        os.replace(staging_dir, output_dir)
        return json_path, md_path

    staged_json_bytes = (staging_dir / "scorecard.json").read_bytes()
    staged_md_bytes = (staging_dir / "scorecard.md").read_bytes()
    if (
        json_path.exists()
        and md_path.exists()
        and json_path.read_bytes() == staged_json_bytes
        and md_path.read_bytes() == staged_md_bytes
    ):
        shutil.rmtree(staging_dir)
        return json_path, md_path

    raise ScorecardPublishConflictError(
        f"{output_dir} already contains a different published scorecard pair -- "
        f"refusing to overwrite; staging preserved at {staging_dir} for forensic "
        "inspection"
    )

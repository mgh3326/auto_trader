"""ROB-962 permits one exact post-ROB-960 production-source repair.

The ROB-960 merge is the comparison base.  ROB-962 supersedes the two older
historical frozen-byte guards because the cost-model source is intentionally
lineage-changing now.  Test files are reviewable support changes; every
production add/modify/delete/rename/copy/type-change remains in scope.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path, PurePosixPath

from rob944_frozen_campaign import build_production_frozen_campaign_envelope

_REPO_ROOT = Path(__file__).resolve().parents[3]
_ROB960_MERGE_HEAD = "81815cb112269d1cdcb4ac786978d112d2f92a52"
_COST_MODEL_PATH = "research/nautilus_scalping/rob940_cost_model.py"
_COST_MODEL_SHA256 = "a5db97037a4fa3acd3712bbe82ec8d69eea4d3545d926de30d752398a3ea5366"
_FULL_CAMPAIGN_HASH = "fb66d90dfb776ad445040097657fc39f46f208ee46b9526318d4d607f601a8f7"
_AUTHORIZED_PRODUCTION_CHANGE = ("M", (_COST_MODEL_PATH,))
_FROZEN_PATHS = (
    "research/nautilus_scalping",
    "app/services/rob944_campaign_controller.py",
    "app/schemas/research_campaign_bridge.py",
    "app/services/research_campaign_bridge.py",
    "app/services/research_db_write_guard.py",
    "research_contracts",
)
_TEST_SUPPORT_PATHS = frozenset({"research/nautilus_scalping/conftest.py"})


def _parse_name_status_z(raw: str) -> list[tuple[str, tuple[str, ...]]]:
    fields = raw.split("\0")
    if fields and fields[-1] == "":
        fields.pop()

    changes: list[tuple[str, tuple[str, ...]]] = []
    cursor = 0
    while cursor < len(fields):
        status = fields[cursor]
        cursor += 1
        path_count = 2 if status[:1] in {"R", "C"} else 1
        paths = tuple(fields[cursor : cursor + path_count])
        if len(paths) != path_count:
            raise AssertionError(f"malformed git --name-status -z output: {fields!r}")
        cursor += path_count
        changes.append((status, paths))
    return changes


def _changes_since_rob960() -> list[tuple[str, tuple[str, ...]]]:
    diff = subprocess.run(
        (
            "git",
            "diff",
            "--name-status",
            "-z",
            "--find-renames",
            _ROB960_MERGE_HEAD,
            "--",
            *_FROZEN_PATHS,
        ),
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    untracked = subprocess.run(
        (
            "git",
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
            "--",
            *_FROZEN_PATHS,
        ),
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    changes = _parse_name_status_z(diff.stdout)
    changes.extend(("?", (path,)) for path in untracked.stdout.split("\0") if path)
    return sorted(changes)


def _is_test_path(path: str) -> bool:
    return "tests" in PurePosixPath(path).parts or path in _TEST_SUPPORT_PATHS


def test_rob962_repair_is_the_only_post_rob960_production_delta():
    production_changes = [
        change
        for change in _changes_since_rob960()
        if not all(_is_test_path(path) for path in change[1])
    ]
    assert production_changes == [_AUTHORIZED_PRODUCTION_CHANGE]


def test_rob962_cost_model_bytes_and_derived_lineage_are_exactly_refrozen():
    cost_model_bytes = (_REPO_ROOT / _COST_MODEL_PATH).read_bytes()
    assert hashlib.sha256(cost_model_bytes).hexdigest() == _COST_MODEL_SHA256
    assert (
        build_production_frozen_campaign_envelope().full_campaign_hash()
        == _FULL_CAMPAIGN_HASH
    )

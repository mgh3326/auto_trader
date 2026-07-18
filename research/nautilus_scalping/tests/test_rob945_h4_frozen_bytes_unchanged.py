"""ROB-945 (H5) -- frozen H4/H1/H2/H3/H6 source bytes must stay unchanged.

The required ROB-945 starting HEAD (also the ROB-944 merge commit) is
``41725f954399b820a8e0a58f6d2d8a2811cd26ef``. This test proves the working
tree's H1-H6-owned paths are byte-identical to that commit -- H5 must ADD
new, H5-owned pure modules only, never edit a frozen predecessor byte
(explicit scope boundary in the ROB-945 worker brief; H4's own
``full_campaign_hash`` depends on several of these files' exact bytes via
``execution_code_provenance``).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_REQUIRED_STARTING_HEAD = "41725f954399b820a8e0a58f6d2d8a2811cd26ef"

_FROZEN_PATHS = (
    "research/nautilus_scalping",
    "app/services/rob944_campaign_controller.py",
    "app/schemas/research_campaign_bridge.py",
    "app/services/research_campaign_bridge.py",
    "app/services/research_db_write_guard.py",
    "research_contracts",
)


def _git_diff_names(*, diff_filter: str | None) -> list[str]:
    cmd = ["git", "diff", "--name-only"]
    if diff_filter is not None:
        cmd.append(f"--diff-filter={diff_filter}")
    cmd += [_REQUIRED_STARTING_HEAD, "--", *_FROZEN_PATHS]
    result = subprocess.run(
        cmd, cwd=_REPO_ROOT, capture_output=True, text=True, check=True
    )
    return [line for line in result.stdout.splitlines() if line.strip()]


def test_h1_through_h6_owned_paths_are_byte_identical_to_the_required_starting_head():
    # Modified/Deleted/Renamed/Copied/Type-changed only -- deliberately
    # EXCLUDES Added (A), since new H5-owned modules under the same
    # research/nautilus_scalping/ directory are expected and allowed; only
    # a byte change to an EXISTING frozen file is forbidden.
    changed = _git_diff_names(diff_filter="MDRCT")
    assert changed == [], (
        "H5 must never modify/delete/rename a frozen H1-H6 byte; unexpected "
        f"diff vs {_REQUIRED_STARTING_HEAD}: {changed}"
    )


def test_the_diff_filter_is_not_vacuous_new_h5_files_would_otherwise_show_up():
    """Proves the ``MDRCT`` filter above is doing real work, not just
    happening to be empty -- the UNFILTERED diff against the same base and
    paths must be non-empty (it includes this worker's own new, Added H5
    files), so the filtered assertion above is a genuine exclusion, not a
    vacuously-always-passing check."""
    unfiltered = _git_diff_names(diff_filter=None)
    assert unfiltered, (
        "expected the unfiltered diff to be non-empty (new H5 files exist) -- "
        "if this is empty, the filtered test above proves nothing"
    )

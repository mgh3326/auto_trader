"""ROB-960 -- frozen H1-H6/H5 source bytes must stay unchanged.

ROB-970 authorized the H3-bound repair at ``da557e36`` (the S2
confirmation_failed timestamp root fix, plus the new Q2/Q3 diagnostic-
evidence capture/carrier boundary, both Fable-approved per
``orch-fable-answer-rob970-20260719.md``) as the new byte authority and
retained this guard class -- superseding the prior ROB-962 authority
(``237cd38a``, the exact-float ``0.0`` cost-model seed). Only the two
guards' self-referential re-pin metadata are excluded; every other frozen
predecessor byte remains protected.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_FROZEN_BYTE_AUTHORITY = "da557e369f0e99c6013baad6055e5be3022646fe"

_FROZEN_PATHS = (
    "research/nautilus_scalping",
    "app/services/rob944_campaign_controller.py",
    "app/schemas/research_campaign_bridge.py",
    "app/services/research_campaign_bridge.py",
    "app/services/research_db_write_guard.py",
    "research_contracts",
)
_GUARD_REPIN_METADATA_PATHS = (
    "research/nautilus_scalping/tests/test_rob945_h4_frozen_bytes_unchanged.py",
    "research/nautilus_scalping/tests/"
    "test_rob960_h1_through_h5_frozen_bytes_unchanged.py",
    "research/nautilus_scalping/tests/test_rob962_frozen_production_delta.py",
)


def _git_diff_names(*, diff_filter: str | None) -> list[str]:
    cmd = ["git", "diff", "--name-only"]
    if diff_filter is not None:
        cmd.append(f"--diff-filter={diff_filter}")
    cmd += [_FROZEN_BYTE_AUTHORITY, "--", *_FROZEN_PATHS]
    cmd += [f":(exclude){path}" for path in _GUARD_REPIN_METADATA_PATHS]
    result = subprocess.run(
        cmd, cwd=_REPO_ROOT, capture_output=True, text=True, check=True
    )
    return [line for line in result.stdout.splitlines() if line.strip()]


def test_h1_through_h6_and_h5_owned_paths_are_byte_identical_to_the_required_starting_head():
    # Modified/Deleted/Renamed/Copied/Type-changed only -- deliberately
    # EXCLUDES Added (A), since later-lineage files under the same directory
    # are expected and allowed; only a byte change to an EXISTING frozen file
    # is forbidden.
    changed = _git_diff_names(diff_filter="MDRCT")
    assert changed == [], (
        "after the authorized ROB-970 H3-bound repair, no later change may "
        "modify/delete/rename a frozen H1-H6/H5 byte; unexpected diff vs "
        f"{_FROZEN_BYTE_AUTHORITY}: {changed}"
    )


def test_rob962_guard_repin_metadata_exclusions_are_exact_and_exist():
    assert _GUARD_REPIN_METADATA_PATHS == (
        "research/nautilus_scalping/tests/test_rob945_h4_frozen_bytes_unchanged.py",
        "research/nautilus_scalping/tests/"
        "test_rob960_h1_through_h5_frozen_bytes_unchanged.py",
        "research/nautilus_scalping/tests/test_rob962_frozen_production_delta.py",
    )
    assert all((_REPO_ROOT / path).is_file() for path in _GUARD_REPIN_METADATA_PATHS)


def test_the_diff_filter_is_not_vacuous_newer_lineage_files_would_otherwise_show_up():
    """Proves the ``MDRCT`` filter above is doing real work, not just
    happening to be empty. Demonstrated against a STABLE historical prior
    authority (the ROB-962 pin this guard superseded) rather than the
    CURRENT authority: right after a fresh re-pin to the latest tip, there
    is by definition no later-lineage Added file yet, so tying this
    sanity check to "today's" authority would make it trivially fail
    immediately after every re-pin. The historical base permanently shows
    an Added file (this repair's own new diagnostic-evidence module), which
    is exactly what the MDRCT filter above must (and does) exclude."""
    historical_prior_authority = "237cd38a3f9e584ddb1071c74d3d442b53f6bd1f"
    cmd = [
        "git",
        "diff",
        "--name-only",
        historical_prior_authority,
        "--",
        *_FROZEN_PATHS,
    ]
    cmd += [f":(exclude){path}" for path in _GUARD_REPIN_METADATA_PATHS]
    result = subprocess.run(
        cmd, cwd=_REPO_ROOT, capture_output=True, text=True, check=True
    )
    unfiltered = [line for line in result.stdout.splitlines() if line.strip()]
    assert unfiltered, (
        "expected the unfiltered diff against the historical prior authority "
        "to be non-empty (later-lineage files exist) -- if this is empty, "
        "the filtered test above proves nothing"
    )

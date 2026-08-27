"""Regression: KR_ACCOUNT_MAP_COMMIT pins must be live-reachable, not brief-supplied.

Fix (2026-08-27): ``kiwoom_cycle.py`` stamped a stale commit
(``a43e36e9bc50d7a93bba009bea96172e10dc4de8``) that a KR slot artifact still
carried after the brief pinning the account map had already moved to
``cbd8f86``. The brief-level fix (relaying a corrected pin) reproduced the
same bug one level up — it checked a value the brief itself supplied, not
the code's own reachability from the operator repo. These tests instead
check the module constants against the operator repo's own ``origin/main``
(ⓐ), and check that the record the cycle actually stamps carries that same
constant rather than a stale literal duplicated elsewhere in the stamping
path (ⓑ).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts.b0x.kr import cycle as kr_cycle
from scripts.b0x.kr import kiwoom_cycle

#: Same convention as scripts/b0x/table_source.py / scripts/build_policy_table.py.
OPERATOR_REPO_PATH: Path = Path.home() / "services" / "auto_trader-operator"


def _operator_repo_unavailable_reason() -> str | None:
    """None if the repo is usable for a reachability check; else why not."""

    if not OPERATOR_REPO_PATH.is_dir():
        return f"operator repo not checked out at {OPERATOR_REPO_PATH}"
    if not (OPERATOR_REPO_PATH / ".git").exists():
        return f"{OPERATOR_REPO_PATH} exists but is not a git repository"
    verify = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", "origin/main"],
        cwd=OPERATOR_REPO_PATH,
        capture_output=True,
        text=True,
    )
    if verify.returncode != 0:
        return f"{OPERATOR_REPO_PATH} has no resolvable origin/main ref"
    return None


def _is_ancestor_of_origin_main(commit: str) -> bool:
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", commit, "origin/main"],
        cwd=OPERATOR_REPO_PATH,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


@pytest.mark.unit
@pytest.mark.parametrize(
    "module",
    [kiwoom_cycle, kr_cycle],
    ids=["kiwoom_cycle", "cycle"],
)
def test_kr_account_map_commit_is_reachable_from_operator_origin_main(module) -> None:
    """ⓐ the pinned commit — not a brief's copy of it — must exist upstream.

    SKIP POLICY: if this checkout has no ``~/services/auto_trader-operator``
    clone (e.g. a CI runner that never checks out operator repos), the test
    is SKIPPED with an explicit reason string — never silently reported as
    passed. A skip shows up distinctly in the run; it is not green, and it
    is not this test's job to invent a substitute check when the operator
    repo is absent.
    """

    unavailable = _operator_repo_unavailable_reason()
    if unavailable is not None:
        pytest.skip(f"cannot verify against operator repo: {unavailable}")

    commit = module.KR_ACCOUNT_MAP_COMMIT
    assert _is_ancestor_of_origin_main(commit), (
        f"{module.__name__}.KR_ACCOUNT_MAP_COMMIT={commit!r} is not reachable "
        f"from origin/main in {OPERATOR_REPO_PATH} - the pin is stale."
    )


@pytest.mark.unit
def test_kiwoom_cycle_stamps_the_module_constant_not_a_stale_literal() -> None:
    """ⓑ the artifact-stamping path must echo the live module constant."""

    record: dict[str, object] = {}
    kiwoom_cycle._stamp_contract_and_account_map(
        record,
        cycle_status=kiwoom_cycle.ACCEPTANCE_ONLY_STATUS,
        status_label=kiwoom_cycle.ACCEPTANCE_ONLY_STATUS_LABEL,
    )
    assert record["account_map"]["commit"] == kiwoom_cycle.KR_ACCOUNT_MAP_COMMIT


@pytest.mark.unit
def test_kis_mock_cycle_stamps_the_module_constant_not_a_stale_literal() -> None:
    """ⓑ same check on the sibling kis_mock cycle's stamping path."""

    record: dict[str, object] = {}
    kr_cycle._stamp_kr_contract_and_account_map(record)
    assert record["account_map"]["commit"] == kr_cycle.KR_ACCOUNT_MAP_COMMIT

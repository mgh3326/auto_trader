"""hk 3361: an absent at-kis-ws stays absent; a failed wait names itself.

Uses the stateful fake-docker harness of the rollback module. No host, no
Docker daemon.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from tests.scripts.test_deploy_ncp_pull_rollback import (
    KIS_OLD,
    NEW,
    OLD,
    _mutations,
    _run,
)

ABSENT_LINE = "at-kis-ws absent at capture; not created (fillwire holds the KIS stream)"
STAMP = "20261107T000000Z"


@pytest.mark.parametrize("args", ((), ("--skip-kis-ws",)))
def test_absent_kis_is_never_created_by_a_forward_deploy(
    tmp_path: Path, args: tuple[str, ...]
) -> None:
    result, calls, state, _ = _run(tmp_path, args=args, absent_name="at-kis-ws")
    assert result.returncode == 0, result.stderr
    assert _mutations(calls, "at-kis-ws") == []
    assert "at-kis-ws" not in state
    assert ABSENT_LINE in result.stdout
    assert "at-kis-ws\tABSENT\tABSENT\tMATCH" in result.stdout
    assert state["at-worker"] == NEW


@pytest.mark.parametrize("args", (("--dry-run",), ("--dry-run", "--skip-kis-ws")))
def test_dry_run_plan_says_absent_kis_is_not_created(
    tmp_path: Path, args: tuple[str, ...]
) -> None:
    result, calls, _, _ = _run(tmp_path, args=args, absent_name="at-kis-ws")
    assert result.returncode == 0, result.stderr
    assert (
        "  at-kis-ws: absent at capture; not created (fillwire holds the KIS stream)"
        in result.stdout
    )
    assert not any(
        c[0] in {"pull", "run", "rm", "stop", "rename", "kill"} for c in calls
    )


def test_present_running_kis_is_replaced_without_the_flag(tmp_path: Path) -> None:
    result, calls, state, _ = _run(tmp_path)
    assert result.returncode == 0, result.stderr
    assert state["at-kis-ws"] == NEW
    assert ABSENT_LINE not in result.stdout
    assert any(c[0] == "run" and "at-kis-ws" in c for c in calls)


def test_present_kis_with_flag_is_untouched(tmp_path: Path) -> None:
    result, calls, state, _ = _run(tmp_path, args=("--skip-kis-ws",))
    assert result.returncode == 0, result.stderr
    assert state["at-kis-ws"] == KIS_OLD
    assert _mutations(calls, "at-kis-ws") == []
    assert ABSENT_LINE not in result.stdout


def test_absent_kis_stays_out_of_the_rollback_after_a_later_failure(
    tmp_path: Path,
) -> None:
    result, calls, state, _ = _run(
        tmp_path, absent_name="at-kis-ws", fail_name="at-mcp-kiwoom"
    )
    assert result.returncode != 0
    assert "at-kis-ws" not in state
    assert _mutations(calls, "at-kis-ws") == []


def test_wait_ws_timeout_names_the_unit_and_keeps_the_log_tail(
    tmp_path: Path,
) -> None:
    result, calls, state, run_dir = _run(
        tmp_path, fail_health_name="at-upbit-ws", fixed_date=STAMP
    )
    assert result.returncode == 1
    assert "at-upbit-ws did not become ready after 1 attempts" in result.stderr
    evidence = run_dir / f"deploy-failure-at-upbit-ws-{STAMP}.log"
    assert evidence.read_text() == "disconnected\n"
    assert stat.S_IMODE(evidence.stat().st_mode) == 0o600
    copy = ["logs", "--timestamps", "--tail", "60", "at-upbit-ws"]
    assert copy in calls
    # Rollback ran, and only after the evidence was copied.
    assert state["at-upbit-ws"] == OLD
    restore = next(
        i
        for i, c in enumerate(calls)
        if c[0] == "run" and "at-upbit-ws" in c and OLD in c
    )
    assert calls.index(copy) < restore


def test_wait_worker_timeout_names_the_unit_and_keeps_the_log_tail(
    tmp_path: Path,
) -> None:
    result, _, state, run_dir = _run(
        tmp_path, fail_health_name="at-worker-new", fixed_date=STAMP
    )
    assert result.returncode == 1
    assert "at-worker-new did not become ready after 1 attempts" in result.stderr
    assert (run_dir / f"deploy-failure-at-worker-new-{STAMP}.log").read_text() == (
        "disconnected\n"
    )
    assert state["at-worker"] == OLD
    assert "at-worker-new" not in state


def test_failed_evidence_copy_warns_and_still_rolls_back_with_rc_1(
    tmp_path: Path,
) -> None:
    name = f"deploy-failure-at-upbit-ws-{STAMP}.log"
    result, _, state, run_dir = _run(
        tmp_path,
        fail_health_name="at-upbit-ws",
        fixed_date=STAMP,
        block_evidence_file=name,
    )
    assert result.returncode == 1
    assert "at-upbit-ws did not become ready after 1 attempts" in result.stderr
    assert result.stderr.count("warning: could not save failure logs") == 1
    assert (run_dir / name).is_dir()
    for unit, image in state.items():
        if unit.startswith("at-") and unit != "at-haproxy":
            assert image == (KIS_OLD if unit == "at-kis-ws" else OLD), unit

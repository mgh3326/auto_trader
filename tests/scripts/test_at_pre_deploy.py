import json
import os
import subprocess
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "ops/ncp/bin/at-pre-deploy.py"
IMAGE = "example/image@sha256:" + "a" * 64


@pytest.fixture
def setup_fakes(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    calls = tmp_path / "calls"
    fake = """#!/usr/bin/env python3
import os, sys, time
from pathlib import Path
with open(os.environ["CALLS"], "a") as f:
    label = " ".join(sys.argv[sys.argv.index("alembic") + 1:]) if "alembic" in sys.argv else sys.argv[1] if len(sys.argv) > 1 else ""
    f.write(Path(sys.argv[0]).name + " " + label + "\\n")
if Path(sys.argv[0]).name == "docker":
    cmd = sys.argv[sys.argv.index("alembic") + 1:]
    if cmd[0] == "current":
        if os.environ.get("MISMATCH") and Path(os.environ["UPGRADED"]).exists():
            print("intermediate")
            sys.exit(0)
        print(("new (label) (head) (mergepoint)" if os.environ.get("ANNOTATED") else "new (head)") if Path(os.environ["UPGRADED"]).exists() or os.environ.get("EQUAL") else "old (branchpoint)" if os.environ.get("ANNOTATED") else "old")
    elif cmd[0] == "heads": print("new (label) (head)" if os.environ.get("ANNOTATED") else "new (head)")
    elif cmd[0] == "history": print("old, other -> new (head) (mergepoint), migration\\nbase -> old (branchpoint), old migration" if os.environ.get("ANNOTATED") else "old -> new (head), migration\\nbase -> old, old migration")
    elif cmd[0] == "upgrade":
        if os.environ.get("MIGRATE_FAIL"):
            print("RuntimeError: migration role identity mismatch", file=sys.stderr)
            print("SECRET_SENTINEL", file=sys.stderr)
            sys.exit(1)
        Path(os.environ["UPGRADED"]).touch()
    sys.exit(0)
if "show" in sys.argv and os.environ.get("REQUIRE_UTC") and "--timestamp=us+utc" not in sys.argv: sys.exit(1)
if "start" in sys.argv:
    if not os.environ.get("START_FAIL"):
        Path(os.environ["STARTED"]).touch()
        if os.environ.get("TWO_BACKUPS"):
            Path(os.environ["BACKUP"], "backup-aaa.sha256").write_text("a"*64 + "  /backup/auto_trader-aaa.dump\\n")
            time.sleep(0.02)
            Path(os.environ["BACKUP"], "backup-zzz.sha256").write_text("a"*64 + "  /backup/auto_trader-zzz.dump\\n")
        elif not os.environ.get("OLD_CHECKSUM"):
            Path(os.environ["BACKUP"], "backup-new.sha256").write_text("a"*64 + "  /backup/auto_trader-new.dump\\n")
    sys.exit(1 if os.environ.get("START_FAIL") or os.environ.get("STATUS3") else 0)
started = Path(os.environ["STARTED"]).exists()
props = {"InvocationID": "new" if started else "old", "Result": "exit-code" if os.environ.get("STATUS3") else "success", "ExecMainCode": "killed" if os.environ.get("KILLED") else "exited", "ExecMainStatus": "3" if os.environ.get("STATUS3") or os.environ.get("KILLED") else "0", "ExecMainStartTimestamp": "2000-01-01 00:00:00 UTC" if os.environ.get("OLD_START") else "2099-01-01 00:00:00 UTC", "ExecMainExitTimestamp": "2000-01-01 00:00:00 UTC" if os.environ.get("OLD_EXIT") else "2099-01-01 00:00:01 UTC"}
for i, arg in enumerate(sys.argv):
    if arg == "-p": print(sys.argv[i+1] + "=" + props[sys.argv[i+1]])
"""
    for name in ("docker", "systemctl"):
        path = bindir / name
        path.write_text(fake)
        path.chmod(0o755)
    env = os.environ.copy()
    env.update(
        PATH=f"{bindir}:{env['PATH']}",
        CALLS=str(calls),
        BACKUP=str(tmp_path),
        PG_BACKUP_DIRECTORY=str(tmp_path),
        AT_MIGRATION_ENV_FILE="FAKE_INPUT",
        STARTED=str(tmp_path / "started"),
        UPGRADED=str(tmp_path / "upgraded"),
    )

    def run(*args, **extra):
        result = subprocess.run(
            [str(SCRIPT), IMAGE, *args],
            env={**env, **extra},
            capture_output=True,
            text=True,
            check=False,
        )
        return result, calls.read_text() if calls.exists() else ""

    return tmp_path, run


def test_equal_heads_record_none_without_backup_or_migration(setup_fakes):
    _, run = setup_fakes
    result, calls = run(EQUAL="1")
    assert result.returncode == 0
    assert json.loads(result.stdout)["migration"] == "none"
    assert "systemctl" not in calls and "docker upgrade" not in calls


def test_pending_without_permission_exits_78_with_revisions(setup_fakes):
    _, run = setup_fakes
    result, calls = run()
    assert result.returncode == 78
    assert "new" in result.stdout
    assert "systemctl" not in calls


def test_dry_run_only_compares_and_reports_plan(setup_fakes):
    _, run = setup_fakes
    result, calls = run("--migrate", "--dry-run")
    assert result.returncode == 0
    assert json.loads(result.stdout)["migration"] == "planned"
    assert "systemctl" not in calls and "docker upgrade" not in calls


def test_previous_status_three_and_failed_start_refused(setup_fakes):
    _, run = setup_fakes
    result, _ = run(
        "--migrate",
        "--accept-backup-remote-failure",
        "reason",
        STATUS3="1",
        START_FAIL="1",
    )
    assert result.returncode == 1


def test_signal_three_is_not_remote_failure(setup_fakes):
    _, run = setup_fakes
    result, _ = run("--migrate", "--accept-backup-remote-failure", "reason", KILLED="1")
    assert result.returncode == 1


def test_old_checksum_is_refused(setup_fakes):
    directory, run = setup_fakes
    checksum = directory / "backup-old.sha256"
    checksum.write_text("a" * 64 + "  auto_trader-old.dump\n")
    os.utime(checksum, (time.time() - 100, time.time() - 100))
    result, _ = run("--migrate", OLD_CHECKSUM="1")
    assert result.returncode == 1


def test_successful_backup_and_migration_record_names_and_followup(setup_fakes):
    _, run = setup_fakes
    result, _ = run("--migrate")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "backup_file": "auto_trader-new.dump",
        "checksum_file": "backup-new.sha256",
        "migration": "applied",
        "revision_before": ["old"],
        "revision_after": ["new"],
        "operator_followup": "symbol_sync",
    }


def test_remote_failure_requires_reason_and_records_ping(setup_fakes):
    _, run = setup_fakes
    result, _ = run("--migrate", STATUS3="1")
    assert result.returncode == 1
    receipt = json.loads(result.stdout)
    assert receipt["migration"] == "not_run"
    assert receipt["checksum_file"] == "backup-new.sha256"
    assert receipt["backup_file"] == "auto_trader-new.dump"
    assert receipt["backup_healthcheck_ping"] == "not_sent"


def test_remote_failure_with_reason_can_migrate(setup_fakes):
    _, run = setup_fakes
    result, _ = run(
        "--migrate", "--accept-backup-remote-failure", "approved", STATUS3="1"
    )
    assert result.returncode == 0
    receipt = json.loads(result.stdout)
    assert receipt["backup_remote_failure_accepted"] == "approved"
    assert receipt["backup_healthcheck_ping"] == "not_sent"


def test_missing_migration_input_fails_before_docker(setup_fakes):
    _, run = setup_fakes
    result, calls = run(AT_MIGRATION_ENV_FILE="")
    assert result.returncode == 1
    assert not calls


def test_migration_failure_reports_safe_stage_and_partial_receipt(setup_fakes):
    _, run = setup_fakes
    result, _ = run("--migrate", MIGRATE_FAIL="1")
    assert result.returncode == 1
    receipt = json.loads(result.stdout)
    assert receipt["backup_file"] == "auto_trader-new.dump"
    assert receipt["revision_before"] == ["old"]
    assert receipt["revision_after"] == "unknown"
    assert "migration role identity mismatch" in result.stderr
    assert "SECRET_SENTINEL" not in result.stderr


@pytest.mark.parametrize("old_timestamp", ["OLD_START", "OLD_EXIT"])
def test_invocation_with_stale_timestamps_is_refused(setup_fakes, old_timestamp):
    _, run = setup_fakes
    result, calls = run("--migrate", **{old_timestamp: "1"})
    assert result.returncode == 1
    assert "docker upgrade" not in calls


def test_blank_remote_failure_reason_is_refused(setup_fakes):
    _, run = setup_fakes
    result, calls = run("--migrate", "--accept-backup-remote-failure", " ", STATUS3="1")
    assert result.returncode == 1
    assert "docker upgrade" not in calls


@pytest.mark.parametrize("remove_outcome", ["success", "nonzero", "timeout", "oserror"])
def test_migration_timeout_is_30_minutes_and_removes_container(
    monkeypatch, capsys, remove_outcome
):
    import importlib.util

    spec = importlib.util.spec_from_file_location("pre_deploy", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls = []

    def fake_command(args, *, timeout=None, check=True):
        calls.append((args, timeout))
        if "upgrade" in args:
            raise subprocess.TimeoutExpired(args, timeout)
        if args[:3] == ["docker", "rm", "-f"]:
            if remove_outcome == "timeout":
                raise subprocess.TimeoutExpired(args, timeout)
            if remove_outcome == "oserror":
                raise OSError("failed")
            return subprocess.CompletedProcess(
                args, 0 if remove_outcome == "success" else 1, stdout="", stderr=""
            )
        output = (
            "old\n"
            if "current" in args
            else "new (head)\n"
            if "heads" in args
            else "old -> new (head), migration\n"
        )
        return subprocess.CompletedProcess(args, 0, stdout=output, stderr="")

    monkeypatch.setattr(module, "command", fake_command)
    monkeypatch.setattr(module, "backup", lambda *_: {})
    monkeypatch.setattr(module.sys, "argv", [str(SCRIPT), IMAGE, "--migrate"])
    monkeypatch.setenv("AT_MIGRATION_ENV_FILE", "FAKE_INPUT")
    with pytest.raises(RuntimeError, match="timed out"):
        module.main()
    upgrade = next(item for item in calls if "upgrade" in item[0])
    assert upgrade[1] == 1800
    assert any(item[0][:3] == ["docker", "rm", "-f"] for item in calls)
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["revision_before"] == ["old"]
    assert receipt["revision_after"] == "unknown"
    assert receipt["migration_container_cleanup"] == (
        "confirmed" if remove_outcome == "success" else "unconfirmed"
    )
    if remove_outcome != "success":
        assert receipt["migration_container"].startswith("at-pre-deploy-")


def test_annotated_alembic_current_and_heads_compare_equal(setup_fakes):
    _, run = setup_fakes
    result, _ = run(EQUAL="1", ANNOTATED="1")
    assert result.returncode == 0
    assert json.loads(result.stdout)["migration"] == "none"


def test_annotated_history_excludes_current_revision(setup_fakes):
    _, run = setup_fakes
    result, _ = run("--migrate", "--dry-run", ANNOTATED="1")
    assert result.returncode == 0
    assert json.loads(result.stdout)["pending_revisions"] == ["new"]


def test_systemd_timestamps_are_requested_in_utc(setup_fakes):
    _, run = setup_fakes
    result, _ = run("--migrate", REQUIRE_UTC="1")
    assert result.returncode == 0


def test_default_command_timeout_is_bounded(monkeypatch):
    import importlib.util

    spec = importlib.util.spec_from_file_location("pre_deploy_timeout", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    timeouts = []

    def fake_run(args, *, capture_output, text, timeout, check):
        timeouts.append(timeout)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    module.command(["systemctl", "show"])
    assert timeouts == [1800]


def test_latest_new_checksum_is_recorded(setup_fakes):
    _, run = setup_fakes
    result, _ = run("--migrate", TWO_BACKUPS="1")
    assert result.returncode == 0
    assert json.loads(result.stdout)["checksum_file"] == "backup-zzz.sha256"


def test_migration_mismatch_records_measured_revision(setup_fakes):
    _, run = setup_fakes
    result, _ = run("--migrate", MISMATCH="1")
    assert result.returncode == 1
    receipt = json.loads(result.stdout)
    assert receipt["revision_after"] == ["intermediate"]
    assert receipt["migration"] == "failed"

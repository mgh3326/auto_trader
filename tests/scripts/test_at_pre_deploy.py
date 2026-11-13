import json
import os
import subprocess
import sys
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
    elif cmd[0] == "history": print("mid -> new (head), migration\\nold -> mid, migration\\nbase -> old, old migration" if os.environ.get("MULTI") else "old, other -> new (head) (mergepoint), migration\\nbase -> old (branchpoint), old migration" if os.environ.get("ANNOTATED") else "old -> new (head), migration\\nbase -> old, old migration")
    elif cmd[0] == "upgrade":
        if os.environ.get("MIGRATE_FAIL"):
            print("RuntimeError: migration role identity mismatch", file=sys.stderr)
            print("SECRET_SENTINEL", file=sys.stderr)
            sys.exit(1)
        Path(os.environ["UPGRADED"]).touch()
    sys.exit(0)
if "show" in sys.argv and any(a.startswith("--timestamp=") for a in sys.argv): sys.exit(1)
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
props = {"InvocationID": "new" if started else "old", "Result": os.environ.get("BACKUP_RESULT", "exit-code" if os.environ.get("STATUS3") else "success"), "ExecMainCode": os.environ.get("BACKUP_CODE", os.environ.get("KILLED", "1")), "ExecMainStatus": os.environ.get("BACKUP_STATUS", "3" if os.environ.get("STATUS3") or os.environ.get("KILLED") else "0"), "ExecMainStartTimestampMonotonic": "invalid" if os.environ.get("INVALID_START") else "1" if os.environ.get("OLD_START") else str(time.monotonic_ns() // 1000 + 1000000), "ExecMainExitTimestampMonotonic": "invalid" if os.environ.get("INVALID_EXIT") else "1" if os.environ.get("OLD_EXIT") else str(time.monotonic_ns() // 1000 + 2000000)}
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

    def run(*args, legacy=False, **extra):
        if legacy:
            wrapper = """import runpy, subprocess, sys, time
original_run = subprocess.run
def legacy_run(*args, **kwargs):
    if 'capture_output' in kwargs or 'text' in kwargs:
        raise TypeError('Python 3.6 subprocess.run does not accept these arguments')
    return original_run(*args, **kwargs)
subprocess.run = legacy_run
del time.time_ns
del time.monotonic_ns
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name='__main__')
"""
            command = [sys.executable, "-c", wrapper, str(SCRIPT), IMAGE, *args]
        else:
            command = [str(SCRIPT), IMAGE, *args]
        result = subprocess.run(
            command,
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


def test_pending_revisions_are_in_apply_order_in_message_and_plan(setup_fakes):
    _, run = setup_fakes
    result, _ = run(MULTI="1")
    assert result.returncode == 78
    assert "mid, new" in result.stdout

    result, _ = run("--migrate", "--dry-run", MULTI="1")
    assert result.returncode == 0
    assert json.loads(result.stdout)["pending_revisions"] == ["mid", "new"]


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
    result, _ = run(
        "--migrate", "--accept-backup-remote-failure", "reason", KILLED="2", STATUS3="1"
    )
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
        "backup_exec_main_status": "0",
        "backup_result": "success",
        "backup_outcome": "succeeded",
        "migrate_requested": True,
        "accept_backup_remote_failure_used": False,
    }


def test_remote_failure_requires_reason_and_records_ping(setup_fakes):
    _, run = setup_fakes
    result, _ = run("--migrate", BACKUP_RESULT="exit-code", BACKUP_STATUS="0")
    assert result.returncode == 1
    receipt = json.loads(result.stdout)
    assert receipt["migration"] == "not_run"
    assert receipt["checksum_file"] == "backup-new.sha256"
    assert receipt["backup_file"] == "auto_trader-new.dump"
    assert receipt["backup_healthcheck_ping"] == "failed"


def test_remote_failure_with_reason_can_migrate(setup_fakes):
    _, run = setup_fakes
    result, _ = run(
        "--migrate",
        "--accept-backup-remote-failure",
        "approved",
        BACKUP_RESULT="exit-code",
        BACKUP_STATUS="0",
    )
    assert result.returncode == 0
    receipt = json.loads(result.stdout)
    assert receipt["backup_remote_failure_accepted"] == "approved"
    assert receipt["backup_healthcheck_ping"] == "failed"


def test_main_success_with_failed_ping_is_overridable_and_recorded(setup_fakes):
    _, run = setup_fakes
    result, _ = run(
        "--migrate",
        "--accept-backup-remote-failure",
        "approved",
        BACKUP_RESULT="exit-code",
        BACKUP_STATUS="0",
    )
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    assert receipt["backup_exec_main_status"] == "0"
    assert receipt["backup_result"] == "exit-code"
    assert receipt["backup_outcome"] == "succeeded_ping_failed"
    assert receipt["backup_healthcheck_ping"] == "failed"
    assert receipt["accept_backup_remote_failure_used"] is True


def test_main_failure_stays_fail_closed_with_acceptance_reason(setup_fakes):
    _, run = setup_fakes
    result, calls = run(
        "--migrate",
        "--accept-backup-remote-failure",
        "approved",
        BACKUP_RESULT="exit-code",
        BACKUP_STATUS="3",
    )
    assert result.returncode == 1
    assert "docker upgrade" not in calls
    receipt = json.loads(result.stdout)
    assert receipt["backup_exec_main_status"] == "3"
    assert receipt["backup_result"] == "exit-code"
    assert receipt["backup_outcome"] == "failed"
    assert receipt["backup_healthcheck_ping"] == "not_sent"
    assert receipt["accept_backup_remote_failure_used"] is True


@pytest.mark.parametrize("timeout_stage", ["preflight", "start", "status"])
def test_backup_systemctl_timeouts_record_unknown_state_and_lock_warning(
    monkeypatch, capsys, timeout_stage
):
    import importlib.util

    spec = importlib.util.spec_from_file_location("pre_deploy_backup_timeout", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    property_calls = 0

    def fake_properties(*names):
        nonlocal property_calls
        property_calls += 1
        if timeout_stage == "preflight" or (
            timeout_stage == "status" and property_calls == 2
        ):
            raise subprocess.TimeoutExpired(["systemctl", "show"], 1800)
        return {"InvocationID": "old"}

    def fake_command(args, *, timeout=1800, check=True):
        if "systemctl" in args:
            if timeout_stage == "start":
                raise subprocess.TimeoutExpired(args, timeout)
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if "current" in args:
            output = "old\n"
        elif "heads" in args:
            output = "new (head)\n"
        elif "history" in args:
            output = "old -> new (head), migration\n"
        else:
            raise AssertionError(args)
        return subprocess.CompletedProcess(args, 0, stdout=output, stderr="")

    monkeypatch.setattr(module, "properties", fake_properties)
    monkeypatch.setattr(module, "command", fake_command)
    monkeypatch.setattr(module.sys, "argv", [str(SCRIPT), IMAGE, "--migrate"])
    monkeypatch.setenv("AT_MIGRATION_ENV_FILE", "FAKE_INPUT")
    with pytest.raises(RuntimeError, match="시간 초과") as raised:
        module.main()

    receipt = json.loads(capsys.readouterr().out)
    assert receipt["backup_exec_main_status"] == "unknown"
    assert receipt["backup_result"] == "unknown"
    assert receipt["backup_outcome"] == "unknown"
    assert receipt["backup_healthcheck_ping"] == "unknown"
    assert receipt["backup_timeout_lock_warning"] in str(raised.value)
    assert "client_connection_check_interval=0" in str(raised.value)
    assert "잠금을 보유" in str(raised.value)


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
    with pytest.raises(RuntimeError, match="시간 초과") as raised:
        module.main()
    upgrade = next(item for item in calls if "upgrade" in item[0])
    assert upgrade[1] == 1800
    assert any(item[0][:3] == ["docker", "rm", "-f"] for item in calls)
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["migration"] == "unverified_timeout"
    assert receipt["revision_before"] == ["old"]
    assert receipt["revision_after"] == "unknown"
    assert receipt["migration_container_cleanup"] == (
        "confirmed" if remove_outcome == "success" else "unconfirmed"
    )
    assert receipt["migration_timeout_lock_warning"]
    assert "client_connection_check_interval=0" in str(raised.value)
    assert "잠금을 보유" in str(raised.value)
    if remove_outcome != "success":
        assert receipt["migration_container"].startswith("at-pre-deploy-")


def test_post_upgrade_revision_timeout_records_applied_unverified_warning(
    monkeypatch, capsys
):
    import importlib.util

    spec = importlib.util.spec_from_file_location("pre_deploy_revision_timeout", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    current_calls = 0

    def fake_command(args, *, timeout=None, check=True):
        nonlocal current_calls
        if "current" in args:
            current_calls += 1
            if current_calls == 2:
                raise subprocess.TimeoutExpired(args, timeout)
            output = "old\n"
        elif "heads" in args:
            output = "new (head)\n"
        elif "history" in args:
            output = "old -> new (head), migration\n"
        elif "upgrade" in args:
            output = ""
        else:
            raise AssertionError(args)
        return subprocess.CompletedProcess(args, 0, stdout=output, stderr="")

    monkeypatch.setattr(module, "command", fake_command)
    monkeypatch.setattr(module, "backup", lambda *_: {})
    monkeypatch.setattr(module.sys, "argv", [str(SCRIPT), IMAGE, "--migrate"])
    monkeypatch.setenv("AT_MIGRATION_ENV_FILE", "FAKE_INPUT")
    with pytest.raises(
        RuntimeError, match="마이그레이션 후 리비전 확인 시간 초과"
    ) as raised:
        module.main()

    receipt = json.loads(capsys.readouterr().out)
    assert receipt["migration"] == "applied_unverified"
    assert receipt["revision_after"] == "unknown"
    assert "client_connection_check_interval=0" in str(raised.value)
    assert "잠금을 보유" in str(raised.value)
    assert receipt["migration_timeout_lock_warning"] in str(raised.value)


def test_post_upgrade_revision_read_error_stays_applied_unverified(monkeypatch, capsys):
    import importlib.util

    spec = importlib.util.spec_from_file_location("pre_deploy_revision_error", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    current_calls = 0

    def fake_command(args, *, timeout=None, check=True):
        nonlocal current_calls
        if "current" in args:
            current_calls += 1
            output = "old\n" if current_calls == 1 else "invalid output\n"
        elif "heads" in args:
            output = "new (head)\n"
        elif "history" in args:
            output = "old -> new (head), migration\n"
        elif "upgrade" in args:
            output = ""
        else:
            raise AssertionError(args)
        return subprocess.CompletedProcess(args, 0, stdout=output, stderr="")

    monkeypatch.setattr(module, "command", fake_command)
    monkeypatch.setattr(module, "backup", lambda *_: {})
    monkeypatch.setattr(module.sys, "argv", [str(SCRIPT), IMAGE, "--migrate"])
    monkeypatch.setenv("AT_MIGRATION_ENV_FILE", "FAKE_INPUT")
    with pytest.raises(RuntimeError, match="invalid Alembic revision output"):
        module.main()

    receipt = json.loads(capsys.readouterr().out)
    assert receipt["migration"] == "applied_unverified"
    assert receipt["revision_after"] == "unknown"


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


def test_systemd_monotonic_timestamps_need_no_format_option(setup_fakes):
    _, run = setup_fakes
    result, _ = run("--migrate")
    assert result.returncode == 0


def test_default_command_timeout_is_bounded(monkeypatch):
    import importlib.util

    spec = importlib.util.spec_from_file_location("pre_deploy_timeout", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    timeouts = []

    def fake_run(args, *, stdout, stderr, universal_newlines, timeout, check):
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


@pytest.mark.parametrize("invalid_timestamp", ["INVALID_START", "INVALID_EXIT"])
def test_invalid_monotonic_backup_timestamp_is_refused(setup_fakes, invalid_timestamp):
    _, run = setup_fakes
    result, calls = run("--migrate", **{invalid_timestamp: "1"})
    assert result.returncode == 1
    assert "docker upgrade" not in calls


def test_numeric_systemd_exit_code_allows_success(setup_fakes):
    _, run = setup_fakes
    result, _ = run("--migrate")
    assert result.returncode == 0
    assert json.loads(result.stdout)["migration"] == "applied"


def test_legacy_python_runtime_supports_read_only_comparison(setup_fakes):
    _, run = setup_fakes
    result, _ = run(legacy=True, EQUAL="1")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["migration"] == "none"


def test_legacy_python_runtime_supports_migration(setup_fakes):
    _, run = setup_fakes
    result, _ = run("--migrate", legacy=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["migration"] == "applied"


def test_script_uses_python_36_compatible_syntax():
    import ast

    ast.parse(SCRIPT.read_text(), feature_version=(3, 6))


@pytest.mark.parametrize("code", ["2", "3"])
def test_signal_exit_codes_never_authorize_remote_failure(setup_fakes, code):
    _, run = setup_fakes
    result, calls = run(
        "--migrate",
        "--accept-backup-remote-failure",
        "reason",
        KILLED=code,
        STATUS3="1",
    )
    assert result.returncode == 1
    assert "docker upgrade" not in calls

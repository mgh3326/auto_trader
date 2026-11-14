#!/usr/bin/env python3
"""Release migration gate. Run once after the exact-SHA gate, before services.

The old code must remain compatible with the migrated schema; destructive
changes belong in a later release (docs/design/4155-shiftgate-adoption.md).
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

SERVICE = "at-pg-backup.service"
REVISION = re.compile(r"^[A-Za-z0-9_]+$")
TIMEOUT_LOCK_WARNING = (
    "DB 세션이 해당 SQL 문을 계속 실행하며 잠금을 보유할 수 있습니다 "
    "(client_connection_check_interval=0). 즉시 재시도하면 대기될 수 있습니다."
)


def command(args, *, timeout=1800, check=True):
    # Python 3.6 does not support subprocess.run's capture_output/text options.
    capture = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "universal_newlines": True,
    }
    result = subprocess.run(
        args,
        timeout=timeout,
        check=False,
        **capture,
    )
    if check and result.returncode:
        stage = args[args.index("alembic") + 1] if "alembic" in args else args[0]
        message = f"{stage} failed"
        safe_errors = {
            "RuntimeError: AT_MIGRATION_SET_ROLE must be at_migration_owner",
            "RuntimeError: migration role identity mismatch",
            "RuntimeError: migration runner requires AT_MIGRATION_SET_ROLE",
        }
        for line in result.stderr.splitlines():
            if line in safe_errors:
                message += ": " + line
        raise RuntimeError(message)
    return result


def revisions(output):
    result = set()
    for line in output.splitlines():
        match = re.fullmatch(r"\s*([A-Za-z0-9_]+)(?:\s+\([^()]+\))*\s*", line)
        if not match:
            raise RuntimeError("invalid Alembic revision output")
        result.add(match.group(1))
    return result


def properties(*names):
    args = ["systemctl", "show"]
    for name in names:
        args.extend(["-p", name])
    lines = command([*args, SERVICE]).stdout.splitlines()
    result = dict(line.split("=", 1) for line in lines if "=" in line)
    if any(name not in result for name in names):
        raise RuntimeError("incomplete backup status")
    return result


def backup_timeout(receipt, stage):
    receipt.update(
        backup_exec_main_status="unknown",
        backup_result="unknown",
        backup_outcome="unknown",
        backup_healthcheck_ping="unknown",
        backup_timeout_lock_warning=TIMEOUT_LOCK_WARNING,
    )
    raise RuntimeError(stage + ". " + TIMEOUT_LOCK_WARNING) from None


def backup(start_ns, start_monotonic_ns, reason, receipt):
    try:
        old = properties("InvocationID")["InvocationID"]
    except subprocess.TimeoutExpired:
        backup_timeout(receipt, "백업 이전 상태 확인 시간 초과")
    # systemctl returns nonzero for a completed oneshot whose process failed.
    try:
        command(["systemctl", "start", SERVICE], check=False)
    except subprocess.TimeoutExpired:
        backup_timeout(receipt, "백업 시작 요청 시간 초과")
    names = (
        "InvocationID",
        "Result",
        "ExecMainCode",
        "ExecMainStatus",
        "ExecMainStartTimestampMonotonic",
        "ExecMainExitTimestampMonotonic",
    )
    try:
        status = properties(*names)
    except subprocess.TimeoutExpired:
        backup_timeout(receipt, "백업 결과 확인 시간 초과")
    if (
        not status["InvocationID"]
        or status["InvocationID"] == old
        or not status["ExecMainStartTimestampMonotonic"].isdecimal()
        or not status["ExecMainExitTimestampMonotonic"].isdecimal()
        or int(status["ExecMainStartTimestampMonotonic"]) * 1000 <= start_monotonic_ns
        or int(status["ExecMainExitTimestampMonotonic"]) * 1000 <= start_monotonic_ns
    ):
        raise RuntimeError("backup invocation not confirmed")
    outcome = (status["Result"], status["ExecMainCode"], status["ExecMainStatus"])
    receipt.update(
        backup_exec_main_status=status["ExecMainStatus"],
        backup_result=status["Result"],
    )
    # The actual PG_BACKUP_DIRECTORY is a live check; see design §pre_deploy.
    directory = Path(os.environ.get("PG_BACKUP_DIRECTORY", "/var/backups/ncp-pg"))
    checksums = sorted(
        directory.glob("backup-*.sha256"),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    for checksum in checksums:
        if not checksum.is_file() or checksum.stat().st_mtime_ns <= start_ns:
            continue
        for line in checksum.read_text().splitlines():
            match = re.fullmatch(r"[0-9a-fA-F]{64} [ *](.+)", line)
            if match:
                name = Path(match.group(1)).name
                if re.fullmatch(r"auto_trader-[A-Za-z0-9_-]+\.dump", name):
                    receipt.update(backup_file=name, checksum_file=checksum.name)
                    break
        if "backup_file" in receipt:
            break
    main_succeeded = outcome[1] in ("1", "exited") and outcome[2] == "0"
    if outcome[0] == "success" and main_succeeded:
        receipt["backup_outcome"] = "succeeded"
    elif outcome[0] == "exit-code" and main_succeeded:
        receipt["backup_outcome"] = "succeeded_ping_failed"
        receipt["backup_healthcheck_ping"] = "failed"
        if not reason or not reason.strip():
            raise RuntimeError(
                "백업은 성공했지만 ping에 실패했습니다. 승인 사유가 필요합니다"
            )
        receipt["backup_remote_failure_accepted"] = reason
    else:
        receipt["backup_outcome"] = "failed"
        if outcome[2] != "0":
            receipt["backup_healthcheck_ping"] = "not_sent"
        raise RuntimeError("백업 메인 프로세스가 실패했습니다")
    if "backup_file" not in receipt:
        raise RuntimeError("new backup checksum not found")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("image", help="release image pinned by digest")
    parser.add_argument("--migrate", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--accept-backup-remote-failure", metavar="REASON")
    args = parser.parse_args()
    start_ns = int(time.time() * 1_000_000_000)
    start_monotonic_ns = int(time.monotonic() * 1_000_000_000)
    env_file = os.environ.get("AT_MIGRATION_ENV_FILE")
    if not env_file:
        raise RuntimeError("AT_MIGRATION_ENV_FILE is required")
    if not re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", args.image):
        raise RuntimeError("image must be pinned by digest")
    base = ["docker", "run", "--rm", "--network", "host", "--env-file", env_file]

    def alembic(*args):
        return command([*base, args_image, "alembic", *args]).stdout

    args_image = args.image
    current = revisions(alembic("current"))
    heads = revisions(alembic("heads"))
    option_receipt = {
        "migrate_requested": args.migrate,
        "accept_backup_remote_failure_used": (
            args.accept_backup_remote_failure is not None
        ),
    }
    if not heads:
        raise RuntimeError("no image head")
    if current == heads:
        print(
            json.dumps(
                {
                    "migration": "none",
                    "revision_before": sorted(current),
                    "revision_after": sorted(heads),
                    **option_receipt,
                }
            )
        )
        return 0
    if len(current) > 1 or len(heads) > 1:
        raise RuntimeError("multiple migration branches require operator review")
    start = next(iter(current), "base")
    end = next(iter(heads))
    pending_output = alembic("history", "--rev-range", f"{start}:{end}")
    pending = []
    for line in pending_output.splitlines():
        match = re.match(r"^.*?\s+->\s+([A-Za-z0-9_]+)(?:\s+\([^()]+\))*,", line)
        if not match or not REVISION.fullmatch(match.group(1)):
            raise RuntimeError("invalid migration history")
        if match.group(1) != start:
            pending.append(match.group(1))
    if not pending or end not in pending:
        raise RuntimeError("cannot determine pending revisions")
    pending.reverse()
    if not args.migrate:
        print("적용 대기 리비전(오래된 순): " + ", ".join(pending))
        return 78
    if args.dry_run:
        print(
            json.dumps(
                {
                    "pending_revisions": pending,
                    "backup": "planned",
                    "migration": "planned",
                    **option_receipt,
                }
            )
        )
        return 0
    receipt = {
        "migration": "not_run",
        "revision_before": sorted(current),
        "revision_after": sorted(current),
        **option_receipt,
    }
    try:
        backup(start_ns, start_monotonic_ns, args.accept_backup_remote_failure, receipt)
    except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired):
        print(json.dumps(receipt))
        raise
    name = "at-pre-deploy-" + uuid.uuid4().hex
    receipt.update(
        migration="failed",
        revision_before=sorted(current),
        revision_after="unknown",
        operator_followup="symbol_sync",
    )
    try:
        try:
            command(
                [*base, "--name", name, args.image, "alembic", "upgrade", "head"],
                timeout=1800,
            )
        except subprocess.TimeoutExpired:
            receipt["migration"] = "unverified_timeout"
            try:
                removed = command(["docker", "rm", "-f", name], timeout=30, check=False)
                confirmed = removed.returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                confirmed = False
            receipt["migration_container_cleanup"] = (
                "confirmed" if confirmed else "unconfirmed"
            )
            receipt["migration_timeout_lock_warning"] = TIMEOUT_LOCK_WARNING
            error = "마이그레이션 시간 초과. " + TIMEOUT_LOCK_WARNING
            if not confirmed:
                receipt["migration_container"] = name
                error = (
                    "마이그레이션 시간 초과, 컨테이너 제거 미확인. "
                    + TIMEOUT_LOCK_WARNING
                )
            raise RuntimeError(error) from None
        receipt["migration"] = "applied_unverified"
        try:
            after = revisions(alembic("current"))
        except subprocess.TimeoutExpired:
            receipt["migration_timeout_lock_warning"] = TIMEOUT_LOCK_WARNING
            raise RuntimeError(
                "마이그레이션 후 리비전 확인 시간 초과. " + TIMEOUT_LOCK_WARNING
            ) from None
        receipt["revision_after"] = sorted(after)
        if after != heads:
            receipt["migration"] = "failed"
            raise RuntimeError("migration did not reach image head")
    except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired):
        print(json.dumps(receipt))
        raise
    receipt.update(migration="applied", revision_after=sorted(after))
    print(json.dumps(receipt))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as exc:
        print(f"pre-deploy failed: {exc}", file=sys.stderr)
        sys.exit(1)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        print("pre-deploy failed: command or input error", file=sys.stderr)
        sys.exit(1)

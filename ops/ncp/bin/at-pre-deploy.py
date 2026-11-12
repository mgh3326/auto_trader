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


def command(args, *, timeout=1800, check=True):
    result = subprocess.run(
        args, capture_output=True, text=True, timeout=timeout, check=False
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
    args = ["systemctl", "show", "--timestamp=us+utc"]
    for name in names:
        args.extend(["-p", name])
    lines = command([*args, SERVICE]).stdout.splitlines()
    result = dict(line.split("=", 1) for line in lines if "=" in line)
    if any(name not in result for name in names):
        raise RuntimeError("incomplete backup status")
    return result


def timestamp_ns(value):
    output = command(["date", "-d", value, "+%s%N"]).stdout.strip()
    if not output.isdecimal():
        raise RuntimeError("invalid backup timestamp")
    return int(output)


def backup(start_ns, reason):
    old = properties("InvocationID")["InvocationID"]
    # systemctl returns nonzero for a completed oneshot whose process failed.
    command(["systemctl", "start", SERVICE], check=False)
    names = (
        "InvocationID",
        "Result",
        "ExecMainCode",
        "ExecMainStatus",
        "ExecMainStartTimestamp",
        "ExecMainExitTimestamp",
    )
    status = properties(*names)
    if (
        not status["InvocationID"]
        or status["InvocationID"] == old
        or timestamp_ns(status["ExecMainStartTimestamp"]) <= start_ns
        or timestamp_ns(status["ExecMainExitTimestamp"]) <= start_ns
    ):
        raise RuntimeError("backup invocation not confirmed")
    outcome = (status["Result"], status["ExecMainCode"], status["ExecMainStatus"])
    if outcome == ("success", "exited", "0"):
        pass
    elif outcome == ("exit-code", "exited", "3") and reason and reason.strip():
        pass
    else:
        raise RuntimeError("backup failed")
    # The actual PG_BACKUP_DIRECTORY is a live check; see design §pre_deploy.
    directory = Path(os.environ.get("PG_BACKUP_DIRECTORY", "/var/backups/ncp-pg"))
    for checksum in sorted(directory.glob("backup-*.sha256")):
        if not checksum.is_file() or checksum.stat().st_mtime_ns <= start_ns:
            continue
        for line in checksum.read_text().splitlines():
            match = re.fullmatch(r"[0-9a-fA-F]{64} [ *](.+)", line)
            if match:
                name = Path(match.group(1)).name
                if re.fullmatch(r"auto_trader-[A-Za-z0-9_-]+\.dump", name):
                    receipt = {"backup_file": name, "checksum_file": checksum.name}
                    if outcome[2] == "3":
                        receipt.update(
                            backup_remote_failure_accepted=reason,
                            backup_healthcheck_ping="not_sent",
                        )
                    return receipt
    raise RuntimeError("new backup checksum not found")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("image", help="release image pinned by digest")
    parser.add_argument("--migrate", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--accept-backup-remote-failure", metavar="REASON")
    args = parser.parse_args()
    start_ns = time.time_ns()
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
    if not heads:
        raise RuntimeError("no image head")
    if current == heads:
        print(
            json.dumps(
                {
                    "migration": "none",
                    "revision_before": sorted(current),
                    "revision_after": sorted(heads),
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
    if not args.migrate:
        print("pending revisions: " + ", ".join(pending))
        return 78
    if args.dry_run:
        print(
            json.dumps(
                {
                    "pending_revisions": pending,
                    "backup": "planned",
                    "migration": "planned",
                }
            )
        )
        return 0
    receipt = backup(start_ns, args.accept_backup_remote_failure)
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
            command(["docker", "rm", "-f", name], check=False)
            raise RuntimeError("migration timed out") from None
        after = revisions(alembic("current"))
        if after != heads:
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

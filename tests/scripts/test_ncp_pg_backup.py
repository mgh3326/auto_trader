"""Behavioral contracts for the NCP PostgreSQL backup operator script."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKUP = REPO_ROOT / "ops" / "ncp" / "pg-backup.sh"


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(0o755)


def _stub_bin(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(
        bin_dir / "pg_dump",
        """#!/usr/bin/env bash
set -Euo pipefail
database="${!#}"
if [[ "$database" == "${MISSING_DATABASE:-}" ]]; then
  printf 'pg_dump: error: database "%s" does not exist\\n' "$database" >&2
  exit 1
fi
output=""
while (($#)); do
  if [[ "$1" == --file ]]; then output="$2"; shift 2; continue; fi
  shift
done
printf 'dump:%s\\n' "$database" >"$output"
""",
    )
    _write_executable(
        bin_dir / "pg_dumpall",
        """#!/usr/bin/env bash
set -Euo pipefail
printf '%s\\n' '-- globals --'
""",
    )
    _write_executable(
        bin_dir / "rsync",
        """#!/usr/bin/env bash
set -Euo pipefail
[[ "${RSYNC_FAIL:-0}" != 1 ]] || exit 23
source="${@: -2:1}"
mkdir -p "$REMOTE_MIRROR"
cp -a "${source}/." "$REMOTE_MIRROR/"
""",
    )
    _write_executable(
        bin_dir / "ssh",
        """#!/usr/bin/env bash
set -Euo pipefail
[[ "${SSH_FAIL:-0}" != 1 ]] || exit 24
bash -c "${!#}"
""",
    )
    return bin_dir


def _run(tmp_path: Path, **overrides: str) -> subprocess.CompletedProcess[str]:
    backup_dir = tmp_path / "ncp-backups"
    remote_dir = tmp_path / "mac-backups"
    key = tmp_path / "id_pg_backup"
    key.write_text("test key\n")
    key.chmod(0o600)
    env = {
        **os.environ,
        "PATH": f"{_stub_bin(tmp_path)}:{os.environ['PATH']}",
        "PGHOST": "127.0.0.1",
        "PGPORT": "25432",
        "PGUSER": "backup_operator",
        "PGPASSWORD": "test-only-secret",
        "PG_BACKUP_DATABASES": "auto_trader panewire",
        "PG_BACKUP_DIRECTORY": str(backup_dir),
        "PG_BACKUP_REMOTE": f"mgh3326@100.73.173.44:{remote_dir}/",
        "PG_BACKUP_SSH_KEY": str(key),
        "PG_BACKUP_RETENTION_DAYS_LOCAL": "7",
        "PG_BACKUP_RETENTION_DAYS_REMOTE": "30",
        "REMOTE_MIRROR": str(remote_dir),
        **overrides,
    }
    return subprocess.run(
        [str(BACKUP)], check=False, capture_output=True, text=True, env=env
    )


def test_multiple_databases_skip_missing_cleanup_and_mirror(tmp_path: Path) -> None:
    backup_dir = tmp_path / "ncp-backups"
    remote_dir = tmp_path / "mac-backups"
    backup_dir.mkdir()
    expired = backup_dir / "expired.dump"
    expired.write_text("old")
    expired.touch(exist_ok=True)
    # A 10-day-old file must be deleted by the 7-day local retention policy.
    os.utime(expired, (1, 1))
    remote_dir.mkdir()
    remote_expired = remote_dir / "expired-on-mac.dump"
    remote_expired.write_text("old")
    os.utime(remote_expired, (1, 1))

    proc = _run(tmp_path, MISSING_DATABASE="panewire")

    assert proc.returncode == 0, proc.stderr
    assert "database=auto_trader dumped" in proc.stderr
    assert "database=panewire missing; skipped" in proc.stderr
    assert "status=success client=host-pg_dump databases=2" in proc.stderr
    assert not expired.exists(), "retention-removal mutant must turn this red"
    local_names = sorted(path.name for path in backup_dir.iterdir())
    assert len([name for name in local_names if name.endswith(".dump")]) == 1
    assert any(
        name.startswith("globals-") and name.endswith(".sql") for name in local_names
    )
    checksum = next(backup_dir.glob("backup-*.sha256"))
    assert "auto_trader-" in checksum.read_text()
    assert "globals-" in checksum.read_text()
    assert (
        sorted(path.name for path in (tmp_path / "mac-backups").iterdir())
        == local_names
    )
    assert not remote_expired.exists(), "remote find-retention cleanup must run"


def test_remote_failure_keeps_local_dump_and_exits_three(tmp_path: Path) -> None:
    proc = _run(tmp_path, RSYNC_FAIL="1")

    assert proc.returncode == 3, proc.stderr
    assert "status=remote_failed" in proc.stderr
    assert "test-only-secret" not in proc.stderr
    assert list((tmp_path / "ncp-backups").glob("*.dump"))
    assert list((tmp_path / "ncp-backups").glob("*.sha256")), (
        "remote-failure-success mutant must turn this red"
    )


def test_missing_required_environment_fails_closed(tmp_path: Path) -> None:
    proc = _run(tmp_path, PG_BACKUP_REMOTE="")

    assert proc.returncode != 0
    assert "missing required environment variable: PG_BACKUP_REMOTE" in proc.stderr
    assert not (tmp_path / "ncp-backups").exists()


def test_systemd_contract_uses_secret_env_daily_kst_and_optional_healthcheck() -> None:
    service = (REPO_ROOT / "ops/ncp/systemd/at-pg-backup.service").read_text()
    timer = (REPO_ROOT / "ops/ncp/systemd/at-pg-backup.timer").read_text()
    script = BACKUP.read_text()

    assert "EnvironmentFile=/root/at-secrets/.env.pg-backup" in service
    assert "ExecStart=/root/at-run/pg-backup.sh" in service
    assert "PG_BACKUP_HC_URL" in service
    assert "OnCalendar=*-*-* 04:10:00 Asia/Seoul" in timer
    assert "rsync -a -e" in script
    assert "rsync -a --delete" not in script

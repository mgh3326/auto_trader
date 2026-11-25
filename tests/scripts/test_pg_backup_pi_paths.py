"""hk 6447: pg-backup lives under deploy/shiftgate/pi and follow.sh uses it.

Acceptance 2 (amended by hk 6466): the daily Dagu job runs at-pg-backup.service,
follow.sh no longer runs it, and the unit file at the new path runs the moved script.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PI = REPO_ROOT / "deploy" / "shiftgate" / "pi"
FOLLOW = PI / "follow.sh"
SERVICE = PI / "at-pg-backup.service"
TIMER = PI / "at-pg-backup.timer"
SCRIPT = PI / "pg-backup.sh"


def test_verifier_fallback_matches_writer_default() -> None:
    writer = SCRIPT.read_text()
    verifier = (REPO_ROOT / "ops" / "ncp" / "bin" / "at-pre-deploy.py").read_text()
    assert "PG_BACKUP_DIRECTORY:-/var/backups/at-pg" in writer
    assert 'os.environ.get("PG_BACKUP_DIRECTORY", "/var/backups/at-pg")' in verifier


def test_backup_unit_runs_from_dagu_not_follow() -> None:
    dag = (
        REPO_ROOT / "deploy" / "dagu" / "at-maintenance" / "at-pg-backup.yaml"
    ).read_text()
    assert "at-pg-backup.service" in dag
    code = [
        line
        for line in FOLLOW.read_text().splitlines()
        if not line.lstrip().startswith("#")
    ]
    assert not any("at-pg-backup.service" in line for line in code)


def test_unit_file_at_new_path_runs_moved_script() -> None:
    service = SERVICE.read_text()
    assert "ExecStart=/root/at-run/pg-backup.sh" in service
    assert SCRIPT.is_file(), "moved pg-backup.sh must exist at deploy/shiftgate/pi"
    assert TIMER.is_file(), "moved at-pg-backup.timer must exist at deploy/shiftgate/pi"
    assert (REPO_ROOT / "ops" / "ncp" / "pg-backup.sh").exists() is False
    assert (
        REPO_ROOT / "ops" / "ncp" / "systemd" / "at-pg-backup.service"
    ).exists() is False
    assert (
        REPO_ROOT / "ops" / "ncp" / "systemd" / "at-pg-backup.timer"
    ).exists() is False


def test_moved_script_uses_at_pg_default() -> None:
    script = SCRIPT.read_text()
    assert "/var/backups/at-pg" in script
    assert "/var/backups/ncp-pg" not in script

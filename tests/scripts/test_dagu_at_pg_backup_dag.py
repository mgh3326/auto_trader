"""hk 6466: static checks of the daily at-pg-backup Dagu DAG copy (not registered)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2] / "deploy" / "dagu" / "at-maintenance"
DAG = ROOT / "at-pg-backup.yaml"


def test_dag_runs_daily_at_0430_kst_on_pi_one_run_at_a_time() -> None:
    doc = yaml.safe_load(DAG.read_text())
    assert doc["schedule"] == "CRON_TZ=Asia/Seoul 30 4 * * *"
    assert doc["max_active_runs"] == 1
    assert doc["worker_selector"] == {"host": "pi"}


def test_step_starts_the_backup_unit_and_fails_when_the_unit_fails() -> None:
    doc = yaml.safe_load(DAG.read_text())
    (step,) = doc["steps"]
    script = step["script"]
    assert "systemctl start at-pg-backup.service" in script
    # the step's exit code is the unit's: a failing oneshot fails the DAG run
    assert 'exit "$rc"' in script and "|| rc=$?" in script
    assert 0 < step["timeout_sec"] <= 3600


def test_handlers_log_to_journal_in_the_maintenance_style() -> None:
    doc = yaml.safe_load(DAG.read_text())
    success = doc["handler_on"]["success"]["command"]
    failure = doc["handler_on"]["failure"]["command"]
    assert success == 'logger -t at-dagu-maintenance "dag=at-pg-backup status=success"'
    assert failure == 'logger -t at-dagu-maintenance "dag=at-pg-backup status=failure"'


def test_header_says_not_registered() -> None:
    header = DAG.read_text().splitlines()[0:4]
    assert any("저장소가 등록하지 않는다" in line for line in header)

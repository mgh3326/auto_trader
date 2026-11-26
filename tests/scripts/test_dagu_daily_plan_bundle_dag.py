"""Static checks of the unregistered daily-plan bundle DAG copy."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from scripts import build_daily_plan_bundle as cli

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
DAG_DIR = ROOT / "deploy/dagu/at-daily-plan-bundle"
DAG = DAG_DIR / "at-daily-plan-bundle.yaml"


def test_dag_runs_the_cli_on_weekdays_at_0820_kst() -> None:
    dag = yaml.safe_load(DAG.read_text())
    assert dag["schedule"] == "CRON_TZ=Asia/Seoul 20 8 * * 1-5"
    assert dag["max_active_runs"] == 1
    assert dag["worker_selector"] == {"host": "pi"}
    (step,) = dag["steps"]
    command = step["command"]
    assert "--env-file /root/at-secrets/.env.api" in command
    assert '"$(cat /root/at-run/deployed-digest)"' in command
    assert "--rm --init" in command
    assert step["timeout_sec"] <= 300
    assert "${" not in command


def test_dag_arguments_parse_and_match_the_default_cutoff() -> None:
    (step,) = yaml.safe_load(DAG.read_text())["steps"]
    _, arguments = step["command"].split("-m scripts.build_daily_plan_bundle ")
    arguments = arguments.replace("$(TZ=Asia/Seoul date +%F)", "2026-11-27")
    args = cli.parse_args(arguments.split())
    assert args.commit and args.cutoff == cli.DEFAULT_CUTOFF
    assert (args.cutoff.hour, args.cutoff.minute) == (8, 20)


def test_dag_is_documented_as_an_unregistered_copy() -> None:
    assert "저장소가 등록하지 않는다" in DAG.read_text().splitlines()[0]
    assert "저장소가 등록하지 않는다" in (DAG_DIR / "README.md").read_text()

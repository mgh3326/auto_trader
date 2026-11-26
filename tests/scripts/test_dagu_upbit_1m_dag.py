from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
DAG_DIR = ROOT / "deploy/dagu/at-upbit-1m"


def test_dag_is_minutely_24_7_copy_and_runs_manual_collector() -> None:
    dag = yaml.safe_load((DAG_DIR / "at-upbit-1m.yaml").read_text())
    assert dag["schedule"] == ["CRON_TZ=UTC * * * * *"]
    assert dag["max_active_runs"] == 1
    assert dag["worker_selector"] == {"host": "pi"}
    (step,) = dag["steps"]
    assert "-m scripts.collect_upbit_minute_candles --commit" in step["script"]
    assert '"$(cat /root/at-run/deployed-digest)"' in step["script"]
    assert "--env-file /root/at-secrets/.env.api" in step["script"]
    assert "--rm --init" in step["script"]
    assert step["timeout_sec"] <= 300


def test_dag_is_documented_as_unregistered_copy() -> None:
    dag_text = (DAG_DIR / "at-upbit-1m.yaml").read_text().splitlines()[0]
    readme = (DAG_DIR / "README.md").read_text()
    assert "저장소가 등록하지 않는다" in dag_text
    assert "저장소가 등록하지 않는다" in readme

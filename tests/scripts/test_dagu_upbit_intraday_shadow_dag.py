"""hk 6036: static checks of the intraday shadow Dagu DAG copy (not registered)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = (
    Path(__file__).resolve().parents[2] / "deploy" / "dagu" / "at-upbit-intraday-shadow"
)


def test_dag_runs_a_minute_after_each_15m_close() -> None:
    doc = yaml.safe_load((ROOT / "at-upbit-intraday-shadow.yaml").read_text())
    assert doc["schedule"] == ["CRON_TZ=Asia/Seoul 1,16,31,46 * * * *"]
    assert doc["max_active_runs"] == 1
    assert doc["worker_selector"] == {"host": "pi"}
    (step,) = doc["steps"]
    script = step["script"]
    assert "-m scripts.upbit_intraday_shadow --commit" in script
    assert '"$(cat /root/at-run/deployed-digest)"' in script
    assert "--env-file /root/at-secrets/.env.api" in script
    assert "--rm --init" in script
    assert step["timeout_sec"] < 900


def test_header_and_readme_say_not_registered() -> None:
    header = (ROOT / "at-upbit-intraday-shadow.yaml").read_text().splitlines()[0]
    assert "저장소가 등록하지 않는다" in header
    assert "copy, NOT registered" in (ROOT / "README.md").read_text()
    assert {p.name for p in ROOT.glob("*.yaml")} == {"at-upbit-intraday-shadow.yaml"}

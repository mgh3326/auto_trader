"""hk 6154/hk 6510: static checks of the intraday movers Dagu DAG copies (not registered)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2] / "deploy" / "dagu" / "at-intraday-movers"


def test_dag_runs_in_krx_hours_every_five_minutes() -> None:
    doc = yaml.safe_load((ROOT / "at-intraday-movers.yaml").read_text())
    assert doc["schedule"] == [
        "CRON_TZ=Asia/Seoul */5 9-14 * * 1-5",
        "CRON_TZ=Asia/Seoul 0,5,10,15,20,25 15 * * 1-5",
    ]
    assert doc["max_active_runs"] == 1
    assert doc["worker_selector"] == {"host": "pi"}
    (step,) = doc["steps"]
    script = step["script"]
    assert "-m scripts.intraday_movers" in script
    assert '"$(cat /root/at-run/deployed-digest)"' in script
    assert "--env-file /root/at-secrets/.env.api" in script
    assert "--rm --init" in script
    assert "--dry-run" not in script
    assert step["timeout_sec"] < 300


def test_us_dag_runs_across_the_et_extended_session_every_five_minutes() -> None:
    doc = yaml.safe_load((ROOT / "at-us-intraday-movers.yaml").read_text())
    assert doc["schedule"] == [
        "CRON_TZ=America/New_York */5 4-19 * * 1-5",
        "CRON_TZ=America/New_York 0,5,10 20 * * 1-5",
    ]
    assert doc["max_active_runs"] == 1
    assert doc["worker_selector"] == {"host": "pi"}
    (step,) = doc["steps"]
    script = step["script"]
    assert "-m scripts.us_intraday_movers" in script
    assert "-m scripts.intraday_movers " not in script
    assert '"$(cat /root/at-run/deployed-digest)"' in script
    assert "--env-file /root/at-secrets/.env.api" in script
    assert "--rm --init" in script
    assert "--dry-run" not in script
    assert step["timeout_sec"] < 300


def test_header_and_readme_say_not_registered() -> None:
    header = (ROOT / "at-intraday-movers.yaml").read_text().splitlines()[0]
    assert "저장소가 등록하지 않는다" in header
    readme = (ROOT / "README.md").read_text()
    assert "copy, NOT registered" in readme
    assert {p.name for p in ROOT.glob("*.yaml")} == {
        "at-intraday-movers.yaml",
        "at-us-intraday-movers.yaml",
    }
    us_header = (ROOT / "at-us-intraday-movers.yaml").read_text().splitlines()[0]
    assert "저장소가 등록하지 않는다" in us_header

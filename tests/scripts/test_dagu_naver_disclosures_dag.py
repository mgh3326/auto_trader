from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

DAG = (
    Path(__file__).resolve().parents[2]
    / "deploy"
    / "dagu"
    / "at-naver-disclosures"
    / "at-naver-disclosures.yaml"
)


def test_naver_disclosure_dag_is_weekday_five_minute_copy_only() -> None:
    text = DAG.read_text()
    doc = yaml.safe_load(text)
    assert "저장소는 이 Dagu 사본을 등록하지 않는다" in text.splitlines()[0]
    assert doc["schedule"] == [
        "CRON_TZ=Asia/Seoul 30,35,40,45,50,55 7 * * 1-5",
        "CRON_TZ=Asia/Seoul */5 8-19 * * 1-5",
        "CRON_TZ=Asia/Seoul 0 20 * * 1-5",
    ]
    assert doc["max_active_runs"] == 1
    assert doc["worker_selector"] == {"host": "pi"}
    (step,) = doc["steps"]
    assert "-m scripts.collect_naver_disclosures --commit" in step["script"]
    assert '"$(cat /root/at-run/deployed-digest)"' in step["script"]
    assert "--env-file /root/at-secrets/.env.api" in step["script"]
    assert "--rm --init" in step["script"]
    assert step["timeout_sec"] < 300
    assert {path.name for path in DAG.parent.glob("*.yaml")} == {DAG.name}

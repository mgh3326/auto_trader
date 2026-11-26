"""Static contract for the unregistered KRX official daily Dagu copy (hk 6586)."""

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2] / "deploy" / "dagu" / "at-krx-official-daily"


def test_dag_runs_the_previous_day_after_each_weekday_and_is_not_registered() -> None:
    path = ROOT / "at-krx-official-daily.yaml"
    doc = yaml.safe_load(path.read_text())
    assert doc["schedule"] == ["CRON_TZ=Asia/Seoul 30 8 * * 2-6"]
    assert doc["max_active_runs"] == 1
    assert doc["worker_selector"] == {"host": "pi"}
    script = doc["steps"][0]["script"]
    assert "scripts.collect_krx_official_daily --date" in script
    assert "date -d yesterday +%F" in script
    assert "--commit" in script
    header = path.read_text().splitlines()[0]
    assert "Dagu 복사본" in header
    assert "저장소가 등록하지 않는다" in header
    assert {item.name for item in ROOT.glob("*.yaml")} == {path.name}

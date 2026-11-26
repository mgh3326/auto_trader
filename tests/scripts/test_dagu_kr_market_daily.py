"""Static contract for the unregistered KR market daily Dagu copy."""

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2] / "deploy" / "dagu" / "at-kr-market-daily"


def test_dag_has_only_requested_weekday_runs_and_is_not_registered() -> None:
    path = ROOT / "at-kr-market-daily.yaml"
    doc = yaml.safe_load(path.read_text())
    assert doc["schedule"] == [
        "CRON_TZ=Asia/Seoul 40 15 * * 1-5",
        "CRON_TZ=Asia/Seoul 30 18 * * 1-5",
    ]
    assert doc["max_active_runs"] == 1
    assert doc["worker_selector"] == {"host": "pi"}
    script = doc["steps"][0]["script"]
    assert "scripts.collect_kr_market_daily --date" in script
    assert "--commit" in script
    header = path.read_text().splitlines()[0]
    assert "Dagu 복사본" in header
    assert "저장소가 등록하지 않는다" in header
    assert {item.name for item in ROOT.glob("*.yaml")} == {path.name}

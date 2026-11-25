"""hk 6503: static checks of the KR trading flags Dagu DAG copies (not registered)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2] / "deploy" / "dagu" / "at-kr-trading-flags"


def test_intraday_overlay_runs_every_ten_minutes_in_krx_hours() -> None:
    doc = yaml.safe_load((ROOT / "at-kr-trading-flags-intraday.yaml").read_text())
    assert doc["schedule"] == ["CRON_TZ=Asia/Seoul */10 9-15 * * 1-5"]
    assert doc["max_active_runs"] == 1
    assert doc["worker_selector"] == {"host": "pi"}
    (step,) = doc["steps"]
    script = step["script"]
    assert "-m scripts.sync_kr_trading_flags --source naver-intraday" in script
    assert '"$(cat /root/at-run/deployed-digest)"' in script
    assert "--env-file /root/at-secrets/.env.api" in script
    assert "--rm --init" in script
    assert "--dry-run" not in script
    assert step["timeout_sec"] < 300


def test_daily_baseline_runs_weekday_morning_with_kis_master() -> None:
    doc = yaml.safe_load((ROOT / "at-kr-trading-flags-daily.yaml").read_text())
    assert doc["schedule"] == ["CRON_TZ=Asia/Seoul 0 7 * * 1-5"]
    assert doc["max_active_runs"] == 1
    (step,) = doc["steps"]
    assert "-m scripts.sync_kr_trading_flags --source kis-master" in step["script"]
    assert "--dry-run" not in step["script"]


def test_headers_and_readme_say_not_registered() -> None:
    for name in ("at-kr-trading-flags-intraday.yaml", "at-kr-trading-flags-daily.yaml"):
        header = (ROOT / name).read_text().splitlines()[0]
        assert "Dagu 복사본" in header
        assert "저장소가 등록하지 않는다" in header
    readme = (ROOT / "README.md").read_text()
    assert "등록되지 않음" in readme
    assert {p.name for p in ROOT.glob("*.yaml")} == {
        "at-kr-trading-flags-intraday.yaml",
        "at-kr-trading-flags-daily.yaml",
    }

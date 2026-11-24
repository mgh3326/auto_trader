"""hk 6148: static checks of the futures-minute Dagu DAG copies (not registered)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2] / "deploy" / "dagu" / "at-futures-minutes"

EXPECTED = {
    "at-futures-minutes-day.yaml": [
        "CRON_TZ=Asia/Seoul 40-55/5 8 * * 1-5",
        "CRON_TZ=Asia/Seoul */5 9-14 * * 1-5",
        "CRON_TZ=Asia/Seoul 0-50/5 15 * * 1-5",
    ],
    "at-futures-minutes-night.yaml": [
        "CRON_TZ=Asia/Seoul 55 17 * * 1-5",
        "CRON_TZ=Asia/Seoul */5 18-23 * * 1-5",
        "CRON_TZ=Asia/Seoul */5 0-5 * * 2-6",
        "CRON_TZ=Asia/Seoul 0-10/5 6 * * 2-6",
    ],
}


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_dag_schedule_entrypoint_and_pattern(name: str) -> None:
    doc = yaml.safe_load((ROOT / name).read_text())
    assert doc["schedule"] == EXPECTED[name]
    assert doc["max_active_runs"] == 1
    assert doc["worker_selector"] == {"host": "pi"}
    (step,) = doc["steps"]
    script = step["script"]
    assert "-m scripts.collect_futures_minutes" in script
    assert '"$(cat /root/at-run/deployed-digest)"' in script
    assert "--env-file /root/at-secrets/.env.api" in script
    assert "-e KIS_FUTURES_QUOTES_ENABLED=true" in script
    assert "--rm --init" in script
    assert step["timeout_sec"] < 300  # shorter than the 5 minute cadence
    assert "--dry-run" not in script


def test_the_day_schedule_covers_0840_to_1550_kst() -> None:
    day = EXPECTED["at-futures-minutes-day.yaml"]
    assert day[0].endswith("40-55/5 8 * * 1-5")  # 08:40..08:55
    assert day[2].endswith("0-50/5 15 * * 1-5")  # 15:00..15:50


def test_readme_says_copies_not_registered_and_lists_both_dags() -> None:
    readme = (ROOT / "README.md").read_text()
    assert "등록되지 않는 복사본" in readme
    assert "등록하지 않는다" in readme
    assert 'night: "unsupported"' in readme
    for name in EXPECTED:
        assert name.removesuffix(".yaml") in readme
    assert {p.name for p in ROOT.glob("*.yaml")} == set(EXPECTED)


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_dag_header_says_not_registered(name: str) -> None:
    assert "저장소가 등록하지 않는다" in (ROOT / name).read_text().splitlines()[0]

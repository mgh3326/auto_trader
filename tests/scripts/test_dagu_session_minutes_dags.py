"""hk 6145: static checks of the session-minute Dagu DAG copies (not registered)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2] / "deploy" / "dagu" / "at-session-minutes"

EXPECTED = {
    "at-session-minutes-kr.yaml": (
        "kr",
        ["CRON_TZ=Asia/Seoul */5 8-19 * * 1-5", "CRON_TZ=Asia/Seoul 0,5,10 20 * * 1-5"],
    ),
    "at-session-minutes-us.yaml": (
        "us",
        [
            "CRON_TZ=America/New_York */5 4-19 * * 1-5",
            "CRON_TZ=America/New_York 0,5,10 20 * * 1-5",
        ],
    ),
}


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_dag_schedule_entrypoint_and_pattern(name: str) -> None:
    market, schedule = EXPECTED[name]
    doc = yaml.safe_load((ROOT / name).read_text())
    assert doc["schedule"] == schedule
    assert doc["max_active_runs"] == 1
    assert doc["worker_selector"] == {"host": "pi"}
    (step,) = doc["steps"]
    script = step["script"]
    assert f"-m scripts.collect_session_minutes --market {market}" in script
    assert '"$(cat /root/at-run/deployed-digest)"' in script
    assert "--env-file /root/at-secrets/.env.api" in script
    assert "--rm --init" in script
    assert step["timeout_sec"] < 300  # shorter than the 5 minute cadence
    assert "--dry-run" not in script


def test_readme_says_copies_not_registered_and_lists_both_dags() -> None:
    readme = (ROOT / "README.md").read_text()
    assert "등록되지 않는 복사본" in readme
    assert (
        "저장소는 Dagu 에 복사하거나 등록하지 않는다" in readme
        or "등록하지 않는다" in readme
    )
    for name in EXPECTED:
        assert name.removesuffix(".yaml") in readme
    assert {p.name for p in ROOT.glob("*.yaml")} == set(EXPECTED)


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_dag_header_says_not_registered(name: str) -> None:
    assert "저장소가 등록하지 않는다" in (ROOT / name).read_text().splitlines()[0]

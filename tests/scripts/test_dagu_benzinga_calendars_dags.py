"""hk 6513: static checks of the Benzinga calendar Dagu DAG copies (not registered)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2] / "deploy" / "dagu" / "at-benzinga-calendars"

DAGS = {
    "at-benzinga-calendars-premarket.yaml": "30 7 * * 1-5",
    "at-benzinga-calendars-intraday.yaml": "*/30 9-16 * * 1-5",
}


def _step(name: str) -> str:
    doc = yaml.safe_load((ROOT / name).read_text())
    assert doc["max_active_runs"] == 1
    assert doc["worker_selector"] == {"host": "pi"}
    assert doc["schedule"] == f"CRON_TZ=America/New_York {DAGS[name]}"
    (step,) = doc["steps"]
    return step["command"]


@pytest.mark.parametrize("name", sorted(DAGS))
def test_dag_uses_the_eastern_date_and_forces_a_refresh(name: str) -> None:
    """The DAG runs on an ET schedule, so its date argument must be ET too.

    A KST date is one day ahead of the observed US session after 10:00 ET, and
    ``skip_succeeded`` would freeze a day before its own session starts.
    ``--force`` is what makes a later tick re-read an already-succeeded
    partition so provider revisions are captured.
    """
    command = _step(name)
    assert "$(TZ=America/New_York date +%F)" in command
    assert "TZ=Asia/Seoul" not in command
    assert "--force" in command
    assert "--commit" in command
    assert "-e BENZINGA_CALENDARS_ENABLED=true" in command
    assert "-m scripts.ingest_benzinga_calendars" in command
    assert '"$(cat /root/at-run/deployed-digest)"' in command


def test_short_interest_is_not_scheduled_without_a_symbol_list() -> None:
    for name in DAGS:
        assert "short_interest" not in _step(name)


def test_the_intraday_step_targets_a_single_day_and_the_premarket_a_week() -> None:
    intraday = _step("at-benzinga-calendars-intraday.yaml")
    assert "--date " in intraday
    assert "--from-date" not in intraday

    premarket = _step("at-benzinga-calendars-premarket.yaml")
    assert "--from-date $(TZ=America/New_York date +%F)" in premarket
    assert "--to-date $(TZ=America/New_York date -d '7 days' +%F)" in premarket

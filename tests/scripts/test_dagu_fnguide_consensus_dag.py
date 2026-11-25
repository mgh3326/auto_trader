"""hk 6508: static checks of the FnGuide consensus Dagu DAG copy (not registered)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2] / "deploy" / "dagu" / "at-fnguide-consensus"


def test_dag_runs_once_after_the_kr_close_on_pi() -> None:
    doc = yaml.safe_load((ROOT / "at-fnguide-consensus.yaml").read_text())
    assert doc["schedule"] == ["CRON_TZ=Asia/Seoul 40 16 * * 1-5"]
    assert doc["max_active_runs"] == 1
    assert doc["worker_selector"] == {"host": "pi"}
    (step,) = doc["steps"]
    script = step["script"]
    assert "-m scripts.collect_fnguide_consensus" in script
    assert "--commit" in script
    assert "--concurrency 2" in script
    assert '"$(cat /root/at-run/deployed-digest)"' in script
    assert "--env-file /root/at-secrets/.env.api" in script
    assert "--rm --init" in script
    # A full run is about 3 pages x ~2,600 symbols at one request per second (about 2.2 h).
    assert step["timeout_sec"] >= 9000
    assert "-eq 3" in script and "status=attention" in script


def test_header_and_readme_say_not_registered() -> None:
    header = (ROOT / "at-fnguide-consensus.yaml").read_text().splitlines()[0]
    assert "저장소가 등록하지 않는다" in header
    readme = (ROOT / "README.md").read_text()
    assert "NOT registered" in readme
    assert {p.name for p in ROOT.glob("*.yaml")} == {"at-fnguide-consensus.yaml"}

"""hk 6436: static checks for the never-started container cleanup (decision 508(b))."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy/shiftgate/pi/created-gc.sh"
DAG = ROOT / "deploy/dagu/at-maintenance/at-pi-created-gc.yaml"


def test_script_only_targets_never_started_auto_trader_job_containers() -> None:
    text = SCRIPT.read_text()
    assert "ids=$(docker ps -aq --filter status=created)" in text
    assert "exit 1" in text
    assert "ghcr.io/mgh3326/auto_trader@*|ghcr.io/mgh3326/auto_trader:*" in text
    assert (
        "shiftgate-*|at-api*|at-mcp*|at-upbit-ws*|at-worker*|at-scheduler*"
        "|at-pg|at-redis|at-trend-*|factortrail|broker-edge|gatewayd"
    ) in text
    # The old broad at-* exclusion also skipped named job containers; it must be gone.
    assert "shiftgate-*|at-*) continue" not in text
    assert '[ "${started:0:4}" = "0001" ] || continue' in text
    assert "86400" in text
    # Removal only with --apply, one container at a time; never images or volumes.
    assert 'apply=0; [ "${1:-}" = "--apply" ] && apply=1' in text
    assert 'docker rm "$id"' in text
    code = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )
    for forbidden in ("prune", "rmi", "volume", "rm -f", "-v "):
        assert forbidden not in code


def test_dag_runs_daily_on_pi_with_apply() -> None:
    doc = yaml.safe_load(DAG.read_text())
    assert doc["schedule"] == "CRON_TZ=Asia/Seoul 30 5 * * *"
    assert doc["max_active_runs"] == 1
    assert doc["worker_selector"] == {"host": "pi"}
    (step,) = doc["steps"]
    assert (
        "bash /root/at-run/shiftgate-pi/created-gc.sh --apply || rc=$?"
        in step["script"]
    )
    assert step["timeout_sec"] <= 300

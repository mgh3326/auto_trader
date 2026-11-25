"""hk 6277: static checks of the Toss fill poll Dagu DAG copy."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

DAG = (
    Path(__file__).resolve().parents[2]
    / "deploy/dagu/at-reconcile/at-toss-fill-poll.yaml"
)


def test_dag_commits_with_job_scoped_flag_and_never_overlaps() -> None:
    doc = yaml.safe_load(DAG.read_text())
    assert doc["schedule"] == [
        "CRON_TZ=Asia/Seoul */5 9-23 * * 1",
        "CRON_TZ=Asia/Seoul */5 * * * 2-5",
        "CRON_TZ=Asia/Seoul */5 0-10 * * 6",
    ]
    assert doc["max_active_runs"] == 1
    assert doc["worker_selector"] == {"host": "pi"}
    (step,) = doc["steps"]
    script = step["script"]
    assert "-e TOSS_FILL_POLL_ENABLED=true" in script
    assert script.count("docker run --rm --init") == 1
    assert '"$(cat /root/at-run/deployed-digest)"' in script
    assert "--env-file /root/at-secrets/.env.api" in script
    assert "-m scripts.toss_fill_reconcile_poller --commit || rc=$?" in script
    assert "TOSS_FILL_POLL_MARKET_GATE_ENABLED" not in script
    # Overlap is prevented by the container name, not by Dagu's timeout: a
    # previous container still present makes the step fail without starting a
    # pass, and docker refuses a second container with the same name. A
    # booking pass is never signalled (per-row booking commits in separate
    # steps): no in-container timeout, and Dagu's timeout is not proxied.
    lines = script.splitlines()
    guard = next(i for i, line in enumerate(lines) if "docker ps -aq" in line)
    run = next(i for i, line in enumerate(lines) if "docker run" in line)
    assert guard < run
    assert "--filter name=at-toss-fill-poll" in lines[guard]
    assert "status=previous_pass_still_running" in lines[guard]
    assert "exit 1" in lines[guard]
    assert "--sig-proxy=false --name at-toss-fill-poll " in script
    assert (
        '"$(cat /root/at-run/deployed-digest)" /app/.venv/bin/python -m '
        "scripts.toss_fill_reconcile_poller --commit" in script
    )
    assert "timeout " not in lines[run]

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
    # Overlap is prevented by the container, not by Dagu's timeout (which
    # only stops the docker client): a fixed name makes docker refuse a second
    # container, and timeout inside the container ends the poller itself.
    assert "--name at-toss-fill-poll " in script
    assert '"$(cat /root/at-run/deployed-digest)" timeout -k 10 200 /app/.venv/bin/python' in script
    assert 200 + 10 < step["timeout_sec"] < 300

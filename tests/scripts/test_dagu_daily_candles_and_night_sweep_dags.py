from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
DAG_DIR = ROOT / "deploy/dagu/at-batch2"
DAGS = {
    "at-batch2-daily-candles-kr": (
        "CRON_TZ=UTC 30 7 * * 1-5",
        "scripts.sync_daily_candles --market kr",
    ),
    "at-batch2-daily-candles-us": (
        "CRON_TZ=UTC 0 22 * * 1-5",
        "scripts.sync_daily_candles --market us",
    ),
    "at-batch2-daily-candles-crypto": (
        "CRON_TZ=UTC 0 0 * * *",
        "scripts.sync_daily_candles --market crypto",
    ),
    "at-batch2-order-proposal-night-sweep": (
        ["CRON_TZ=UTC 30 7 * * 1-5", "CRON_TZ=UTC 0 22 * * 0-4"],
        "scripts.order_proposal_night_sweep",
    ),
}


DAILY_CANDLE_DAGS = {
    "at-batch2-daily-candles-kr",
    "at-batch2-daily-candles-us",
    "at-batch2-daily-candles-crypto",
}


@pytest.mark.parametrize(("name", "expected"), DAGS.items())
def test_dag_copy_contract(name: str, expected: tuple[object, str]) -> None:
    schedule, module = expected
    path = DAG_DIR / f"{name}.yaml"
    dag = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw = path.read_text(encoding="utf-8")

    assert dag["schedule"] == schedule
    assert dag["worker_selector"] == {"host": "pi"}
    assert dag["max_active_runs"] == 1
    assert len(dag["steps"]) == 1
    step = dag["steps"][0]
    assert step["name"] in {"sync", "sweep"}
    assert "command" not in step
    lines = [line for line in step["script"].splitlines() if line.strip()]
    assert lines[0] == "rc=0"
    assert lines[1].startswith(
        "docker run --rm --init --network host --workdir /app --env-file "
        '/root/at-secrets/.env.api "$(cat /root/at-run/deployed-digest)" '
        "/app/.venv/bin/python -m "
    )
    assert lines[1].endswith(f"{module} || rc=$?")
    if name in DAILY_CANDLE_DAGS:
        # Exit 3 is a partial sync (some symbols failed, the rest were
        # written): log an attention line and let the step succeed, like the
        # terminal reconcile DAG does. Any other non-zero code still fails.
        assert lines[2] == (
            'if [ "$rc" -eq 3 ]; then logger -t at-dagu-batch '
            f'"dag={name} step=sync status=attention" || true; exit 0; fi'
        )
        assert lines[3] == 'exit "$rc"'
        assert len(lines) == 4
        assert raw.count("logger -t at-dagu-batch") == 3
    else:
        assert lines[2] == 'exit "$rc"'
        assert raw.count("logger -t at-dagu-batch") == 2
    assert dag["handler_on"] == {
        "success": {
            "command": (f'logger -t at-dagu-batch "dag={name} status=success"')
        },
        "failure": {
            "command": (f'logger -t at-dagu-batch "dag={name} status=failure"')
        },
    }

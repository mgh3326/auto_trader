"""Static check for the KR valuation Dagu copy (hk 6150).

The DAG is a proposal copy: nothing in the repo registers it. This test pins its
schedule (UTC 07:30 = KST 16:30, Mon-Fri), host, the one command the step runs
(holiday gate plus commit-from-settings, which keep the commit decision inside
the container), the absence of shell ``${VAR}`` (Dagu expands those first), and
the journal handlers. It does not call Dagu, Docker or a database.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
DAG_PATH = ROOT / "deploy/dagu/at-batch2/at-batch2-kr-valuation.yaml"
README = ROOT / "deploy/dagu/at-batch2/README.md"
NAME = "at-batch2-kr-valuation"
COMMAND = (
    "docker run --rm --init --network host --workdir /app --env-file "
    '/root/at-secrets/.env.api "$(cat /root/at-run/deployed-digest)" '
    "/app/.venv/bin/python -m scripts.build_market_valuation_snapshots "
    "--market kr --all --skip-non-session --commit-from-settings || rc=$?"
)


def _dag() -> dict:
    return yaml.safe_load(DAG_PATH.read_text(encoding="utf-8"))


def test_dag_parses_with_the_expected_schedule_and_host() -> None:
    dag = _dag()
    assert dag["schedule"] == "CRON_TZ=UTC 30 7 * * 1-5"
    assert dag["worker_selector"] == {"host": "pi"}
    assert dag["max_active_runs"] == 1


def test_single_step_runs_the_gated_build_and_exits_with_its_code() -> None:
    dag = _dag()
    assert len(dag["steps"]) == 1
    step = dag["steps"][0]
    assert "command" not in step
    assert isinstance(step["timeout_sec"], int) and step["timeout_sec"] > 0
    lines = [line for line in step["script"].splitlines() if line.strip()]
    assert lines == ["rc=0", COMMAND, 'exit "$rc"']


def test_no_shell_variable_expansion_in_commands() -> None:
    raw = DAG_PATH.read_text(encoding="utf-8")
    assert "${" not in raw


def test_handlers_write_one_journal_line_each() -> None:
    dag = _dag()
    assert dag["handler_on"] == {
        "success": {"command": f'logger -t at-dagu-batch "dag={NAME} status=success"'},
        "failure": {"command": f'logger -t at-dagu-batch "dag={NAME} status=failure"'},
    }
    assert len(re.findall(r"logger -t at-dagu-batch", DAG_PATH.read_text())) == 2


def test_readme_lists_the_copy_as_unregistered() -> None:
    text = README.read_text(encoding="utf-8")
    row = next(line for line in text.splitlines() if f"`{NAME}`" in line)
    assert "16:30" in row and "07:30" in row
    assert "--market kr --all --skip-non-session --commit-from-settings" in row
    assert "등록되지 않는 복사본" in text

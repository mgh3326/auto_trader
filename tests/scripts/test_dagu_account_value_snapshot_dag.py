"""Static check for the daily account-value snapshot Dagu DAG proposal (hk 3872).

The DAG in ``deploy/dagu/at-daily/`` is a proposal: nothing registers it, and
registering it is an operator step in ``docs/runbooks/account-value-snapshot-daily.md``.
This test pins its schedule (KST in the header, UTC in ``schedule``), the three
account steps and the one command each may run, that one failing account does not
stop the others while the run still ends failed, the single-flight/timeout/retry
bounds, the failure ping, and the runbook that registers it. It does not call
Dagu, Docker, a broker or a database.

Set ``AT_DAILY_DAG_DIR`` to point the check at a scratch copy (used to show that
a wrong schedule, a fourth step, a missing ``--commit`` or a removed retry fails).
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
DAG_DIR = Path(os.environ.get("AT_DAILY_DAG_DIR") or ROOT / "deploy/dagu/at-daily")
STEM = "at-account-value-snapshot"
RUNBOOK = ROOT / "docs/runbooks/account-value-snapshot-daily.md"
DECISION = "decision/2026-11-11/factortrail-p1"

DOCKER = (
    "docker run --rm --network host --workdir /app "
    "--env-file /root/at-secrets/.env.api "
    '"$(cat /root/at-run/deployed-digest)" /app/.venv/bin/python -m '
)
MODULE = "scripts.account_value_snapshot"
ACCOUNTS = ["kis", "toss", "upbit"]
PING = "AT_BATCH_PING_URL_ACCOUNT_VALUE_SNAPSHOT"
SCHEDULE_UTC = "40 22 * * *"
KST_CLOCK = "07:40"
KST_OFFSET_MIN = 9 * 60


def _load() -> tuple[str, dict]:
    text = (DAG_DIR / f"{STEM}.yaml").read_text()
    return text, yaml.safe_load(text)


def _header(text: str) -> str:
    return text.split("description:", 1)[0]


def test_the_directory_holds_exactly_this_dag():
    assert sorted(p.stem for p in DAG_DIR.glob("*.yaml")) == [STEM]


def test_header_says_proposal_and_names_the_runbook_and_the_decision():
    text, _ = _load()
    header = _header(text)
    assert "PROPOSAL" in header
    assert "등록되어 있지 않다" in header
    assert "registered only via the runbook below" in header
    assert "docs/runbooks/account-value-snapshot-daily.md" in header
    assert DECISION in header
    assert "§5" in header
    # The header must say why continue_on is not used: it would end the run
    # partially_succeeded and fire the success ping.
    assert "continue_on" in header
    assert "partially_succeeded" in header


def test_schedule_is_0740_kst_daily_in_utc():
    text, dag = _load()
    minute, hour, dom, month, dow = dag["schedule"].split()
    assert dag["schedule"] == SCHEDULE_UTC, "the pinned UTC cron changed"
    assert dom == "*" and month == "*" and dow == "*", "the run must be daily"
    # The header's documented KST clock is pinned separately: changing either the
    # header or the cron alone fails here.
    documented = re.search(
        r"^# dagu_schedule_kst: (\d{2}:\d{2}) (매일|daily)$", text, re.M
    )
    assert documented, (
        "the header must document the KST clock on a dagu_schedule_kst line"
    )
    assert documented.group(1) == KST_CLOCK
    kst_hour, kst_minute = (int(x) for x in documented.group(1).split(":"))
    total = kst_hour * 60 + kst_minute - KST_OFFSET_MIN
    _, minute_of_day = divmod(total, 24 * 60)
    assert (minute_of_day % 60, minute_of_day // 60) == (int(minute), int(hour))


def test_exactly_three_account_steps_with_the_allowlisted_command():
    _, dag = _load()
    steps = dag["steps"]
    assert [s["name"] for s in steps] == ACCOUNTS
    assert [s["command"] for s in steps] == [
        DOCKER + f"{MODULE} --account {account} --commit" for account in ACCOUNTS
    ]
    assert (ROOT / (MODULE.replace(".", "/") + ".py")).is_file(), MODULE
    for step in steps:
        command = step["command"]
        assert re.fullmatch(
            r"docker run --rm --network host --workdir /app --env-file /root/at-secrets/\.env\.api \"\$\(cat /root/at-run/deployed-digest\)\" /app/\.venv/bin/python -m scripts\.[a-z0-9_]+ --account (kis|toss|upbit) --commit",
            command,
        ), command
        for forbidden in (
            " -e ",
            " -v ",
            " --volume",
            " --mount",
            " --privileged",
            " --entrypoint",
            " --pid",
            " sudo ",
            "&&",
            "||",
            "|&",
            "`",
            ";",
            " | ",
        ):
            assert forbidden not in command, forbidden
        assert "--dry-run" not in command
        assert "--flow" not in command  # flows stay operator-only
        assert "--flow-id" not in command


def test_a_failing_account_does_not_stop_the_others_and_the_run_ends_failed():
    _, dag = _load()
    for step in dag["steps"]:
        # No dependency edge: Dagu runs dependency-free steps in parallel, so a
        # failed account does not block the other two.
        assert "depends" not in step, step["name"]
        # continue_on would end the run partially_succeeded and fire the success
        # ping instead of the failure ping.
        assert "continue_on" not in step, step["name"]
    handlers = dag["handler_on"]
    assert (
        handlers["failure"]["command"]
        == f'curl -fsS -m 10 --retry 3 -o /dev/null "${{{PING}}}/fail"'
    )
    assert (
        handlers["success"]["command"]
        == f'curl -fsS -m 10 --retry 3 -o /dev/null "${{{PING}}}"'
    )


def test_single_flight_bounded_and_retried():
    _, dag = _load()
    assert dag.get("max_active_runs") == 1
    assert dag["worker_selector"] == {"host": "ncp"}
    assert dag["dotenv"] == "/root/at-secrets/.env.dagu-at-batch"
    for step in dag["steps"]:
        timeout = step.get("timeout_sec")
        assert isinstance(timeout, int) and 0 < timeout <= 1800, step["name"]
        # A retry is safe: capture() is ON CONFLICT DO NOTHING per
        # (account, market, day), so a same-day rerun reports already_present.
        assert step.get("retry_policy", {}).get("limit") == 1, step["name"]


def test_no_url_value_secret_or_env_value_in_the_dag_or_runbook():
    text, _ = _load()
    runbook = RUNBOOK.read_text()
    for body in (text, runbook):
        assert not re.search(r"https?://", body)
        assert f"{PING}=" not in body
    assert PING in runbook
    assert PING in text


def test_runbook_registers_the_dag_and_covers_rollback_and_gaps():
    runbook = RUNBOOK.read_text()
    assert f"deploy/dagu/at-daily/{STEM}.yaml" in runbook
    assert "dagu validate" in runbook
    assert "dagu start" in runbook
    assert "already_present" in runbook
    assert "8행" in runbook
    assert "## 롤백" in runbook
    assert "## 알려진 격차" in runbook
    assert "--flows-complete" in runbook
    assert "전일 종가" in runbook

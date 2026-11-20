"""Static checks for the snapshot-refresh Dagu proposals (hk 5752).

``deploy/dagu/at-snapshots/`` holds five DAGs that refresh analyst consensus,
KR financial fundamentals and the KR/US screener snapshots. They are proposals:
nothing registers them here. The check pins, per DAG, the schedule (UTC, with the
``CRON_TZ=UTC`` prefix), the one allowlisted module, the commit gate, the
journal-then-optional-ping handlers, and the times relative to the data they read
and the data-plane runs. Each command's arguments are parsed by the real CLI
argparser, so a flag the builder does not accept fails here, not on the host.

It does not call Dagu, Docker, a database or any broker.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
DAG_DIR = ROOT / "deploy/dagu/at-snapshots"
DATA_PLANE_DIR = ROOT / "deploy/dagu/data-plane"
CANDLES_DIR = ROOT / "deploy/dagu/at-batch2"
RUNBOOK = ROOT / "docs/runbooks/snapshot-refresh-dagu.md"
JOURNAL_TAG = "at-dagu-batch"
UTC_PREFIX = "CRON_TZ=UTC "

DOCKER = (
    "docker run --rm --init --network host --workdir /app "
    "--env-file /root/at-secrets/.env.api "
    '"$(cat /root/at-run/deployed-digest)" /app/.venv/bin/python -m '
)
CASE_RE = re.compile(
    r"\$\(case \"\$\(printf %s \"\$\{(?P<gate>[A-Z0-9_]+)\}\" \| tr A-Z a-z\)\" in "
    r"1\|true\|yes\|on\) echo (?P<on>[^;]*);; (?:\*\) echo (?P<off>[^;]*);; )?esac\)"
)

# stem -> pinned facts. ``module`` is the one allowlisted entry point; ``on`` and
# ``off`` are the gate's arguments when set and unset.
JOBS: dict[str, dict] = {
    "at-snapshot-screener-kr": {
        "module": "scripts.build_invest_screener_snapshots",
        "args": "--market kr --all --keep-trading-days 5",
        "gate": "AT_SNAPSHOT_SCREENER_KR_COMMIT_ENABLED",
        "on": "--commit",
        "off": None,
        "ping": "AT_BATCH_PING_URL_SNAPSHOT_SCREENER_KR",
        "schedule": "25 11 * * 1-5",
        "timeout": 3600,
    },
    "at-snapshot-screener-us": {
        "module": "scripts.build_invest_screener_snapshots",
        "args": "--market us --all --common-stocks-only --keep-trading-days 5",
        "gate": "AT_SNAPSHOT_SCREENER_US_COMMIT_ENABLED",
        "on": "--commit",
        "off": None,
        "ping": "AT_BATCH_PING_URL_SNAPSHOT_SCREENER_US",
        "schedule": "30 1 * * 2-6",
        "timeout": 3600,
    },
    "at-snapshot-consensus-kr": {
        "module": "scripts.build_analyst_consensus_snapshots",
        "args": "--market kr --batch-size 100 --concurrency 4",
        "gate": "AT_SNAPSHOT_CONSENSUS_KR_COMMIT_ENABLED",
        "on": "--commit",
        "off": None,
        "ping": "AT_BATCH_PING_URL_SNAPSHOT_CONSENSUS_KR",
        "schedule": "30 12 * * 1-5",
        "timeout": 3600,
    },
    "at-snapshot-consensus-us": {
        "module": "scripts.build_analyst_consensus_snapshots",
        "args": "--market us --batch-size 100 --concurrency 4",
        "gate": "AT_SNAPSHOT_CONSENSUS_US_COMMIT_ENABLED",
        "on": "--commit",
        "off": None,
        "ping": "AT_BATCH_PING_URL_SNAPSHOT_CONSENSUS_US",
        "schedule": "0 3 * * 2-6",
        "timeout": 3600,
    },
    "at-snapshot-financial-kr": {
        "module": "scripts.build_financial_fundamentals_snapshots",
        "args": "--market kr --limit 1500 --skip-existing",
        "gate": "AT_SNAPSHOT_FINANCIAL_KR_COMMIT_ENABLED",
        "on": "--commit --allow-partial",
        "off": "--estimate-only",
        "ping": "AT_BATCH_PING_URL_SNAPSHOT_FINANCIAL_KR",
        "schedule": "0 14 * * *",
        "timeout": 7200,
    },
}


def _load(stem: str) -> tuple[str, dict]:
    text = (DAG_DIR / f"{stem}.yaml").read_text()
    return text, yaml.safe_load(text)


def _handler_commands(stem: str, ping: str) -> tuple[str, str]:
    success = (
        f'logger -t {JOURNAL_TAG} "dag={stem} status=success"; '
        f'[ -z "${{{ping}:-}}" ] || curl -fsS -m 10 --retry 3 -o /dev/null "${{{ping}}}"'
    )
    failure = (
        f'logger -t {JOURNAL_TAG} "dag={stem} status=failure"; '
        f'[ -z "${{{ping}:-}}" ] || curl -fsS -m 10 --retry 3 -o /dev/null "${{{ping}}}/fail"'
    )
    return success, failure


def _split_cron(cron: str) -> tuple[int, int, set[int]]:
    minute, hour, dom, month, dow = cron.split()
    assert dom == "*" and month == "*", cron
    days: set[int] = set()
    if dow == "*":
        return int(minute), int(hour), set(range(7))
    for part in dow.split(","):
        if "-" in part:
            lo, hi = part.split("-")
            days.update(range(int(lo), int(hi) + 1))
        else:
            days.add(int(part))
    return int(minute), int(hour), {d % 7 for d in days}


def _utc_slot(schedule: str) -> tuple[int, set[int]]:
    """(minute-of-day in UTC, weekday set with Sunday=0) for a CRON_TZ=UTC schedule."""
    assert schedule.startswith(UTC_PREFIX), schedule
    minute, hour, days = _split_cron(schedule.removeprefix(UTC_PREFIX))
    return hour * 60 + minute, days


def _data_plane_slot(name: str) -> tuple[int, set[int]]:
    cron = yaml.safe_load((DATA_PLANE_DIR / name).read_text())["schedule"]
    minute, hour, days = _split_cron(cron)
    return hour * 60 + minute, days


def _candle_slot(name: str) -> tuple[int, set[int]]:
    return _utc_slot(yaml.safe_load((CANDLES_DIR / name).read_text())["schedule"])


def _parse_command(command: str) -> tuple[str, str, str | None]:
    """Return (module, args-without-gate, gate-substitution text) from a step command."""
    match = CASE_RE.search(command)
    assert match, command
    head = command[: match.start()].rstrip()
    module = re.search(r"-m (scripts\.[a-z0-9_]+)", head).group(1)
    rest = head.split(f"-m {module}", 1)[1].strip()
    return module, rest, match.group(0)


def test_exactly_the_five_documented_dags_exist() -> None:
    assert sorted(p.stem for p in DAG_DIR.glob("*.yaml")) == sorted(JOBS)


@pytest.mark.parametrize("stem", sorted(JOBS))
def test_header_names_the_runbook_and_says_not_registered(stem: str) -> None:
    text, _ = _load(stem)
    assert "저장소는 이 파일을 등록하지 않는다" in text.split("description:")[0]
    assert "docs/runbooks/snapshot-refresh-dagu.md" in text
    assert "시각 근거" in text
    assert JOBS[stem]["gate"] in text
    assert "UTC" in text


@pytest.mark.parametrize("stem", sorted(JOBS))
def test_schedule_is_utc_and_pinned(stem: str) -> None:
    _, dag = _load(stem)
    assert dag["schedule"] == UTC_PREFIX + JOBS[stem]["schedule"]


@pytest.mark.parametrize("stem", sorted(JOBS))
def test_worker_single_flight_and_retry_bounds(stem: str) -> None:
    _, dag = _load(stem)
    assert dag["max_active_runs"] == 1
    assert dag["worker_selector"] == {"host": "pi"}
    assert dag["dotenv"] == "/root/at-secrets/.env.dagu-at-batch"
    assert len(dag["steps"]) == 1
    step = dag["steps"][0]
    assert step["timeout_sec"] == JOBS[stem]["timeout"]
    assert step["retry_policy"]["limit"] == 1
    assert "continue_on" not in step


@pytest.mark.parametrize("stem", sorted(JOBS))
def test_command_is_the_allowlisted_container_form_with_one_gate(stem: str) -> None:
    _, dag = _load(stem)
    job = JOBS[stem]
    command = dag["steps"][0]["command"]
    module, rest, gate_text = _parse_command(command)
    assert module == job["module"]
    assert (ROOT / (module.replace(".", "/") + ".py")).is_file()
    assert command.startswith(DOCKER + module), command
    assert rest == job["args"], rest
    assert gate_text.count(f"${{{job['gate']}}}") == 1
    assert len(CASE_RE.findall(command)) == 1
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
    ):
        assert forbidden not in command, (stem, forbidden)
    # `;` and `|` may appear only inside the one gate substitution.
    assert command.replace(gate_text, "").count(";") == 0
    assert " | " not in command.replace(gate_text, "")


@pytest.mark.parametrize("stem", sorted(JOBS))
def test_gate_arguments_match_the_pinned_on_and_off(stem: str) -> None:
    _, dag = _load(stem)
    job = JOBS[stem]
    match = CASE_RE.search(dag["steps"][0]["command"])
    assert match.group("gate") == job["gate"]
    assert match.group("on") == job["on"]
    assert match.group("off") == job["off"]


@pytest.mark.parametrize("stem", sorted(JOBS))
def test_both_gate_branches_parse_with_the_real_cli(stem: str) -> None:
    """The DAG's arguments must be accepted by the builder's own argparser."""
    _, dag = _load(stem)
    job = JOBS[stem]
    command = dag["steps"][0]["command"]
    match = CASE_RE.search(command)
    if job["module"].endswith("build_invest_screener_snapshots"):
        from scripts import build_invest_screener_snapshots as cli
    elif job["module"].endswith("build_analyst_consensus_snapshots"):
        from scripts import build_analyst_consensus_snapshots as cli
    else:
        from scripts import build_financial_fundamentals_snapshots as cli

    base = shlex.split(job["args"])
    on_args = shlex.split(match.group("on"))
    parsed_on = cli.parse_args(base + on_args)
    assert parsed_on.dry_run is False
    if match.group("off"):
        parsed_off = cli.parse_args(base + shlex.split(match.group("off")))
        assert parsed_off.estimate_only is True
        assert parsed_off.commit is False
    else:
        parsed_off = cli.parse_args(base)
        assert parsed_off.dry_run is True
        assert parsed_off.commit is False


@pytest.mark.parametrize(
    "stem",
    [s for s in sorted(JOBS) if JOBS[s]["module"].endswith("screener_snapshots")],
)
def test_screener_keeps_exactly_five_trading_days(stem: str) -> None:
    _, dag = _load(stem)
    command = dag["steps"][0]["command"]
    assert "--keep-trading-days 5" in command.replace(
        CASE_RE.search(command).group(0), ""
    )


@pytest.mark.parametrize(
    "stem",
    [s for s in sorted(JOBS) if not JOBS[s]["module"].endswith("screener_snapshots")],
)
def test_non_screener_jobs_do_not_prune(stem: str) -> None:
    _, dag = _load(stem)
    assert "--keep-trading-days" not in dag["steps"][0]["command"]


def test_financial_never_fetches_in_its_unset_branch() -> None:
    _, dag = _load("at-snapshot-financial-kr")
    match = CASE_RE.search(dag["steps"][0]["command"])
    assert match.group("off") == "--estimate-only"
    assert "--commit" not in (match.group("off") or "")


@pytest.mark.parametrize("stem", sorted(JOBS))
def test_every_dag_writes_one_journal_line_before_an_optional_ping(stem: str) -> None:
    _, dag = _load(stem)
    job = JOBS[stem]
    success, failure = _handler_commands(stem, job["ping"])
    assert dag["handler_on"]["success"]["command"] == success
    assert dag["handler_on"]["failure"]["command"] == failure


def test_ping_names_are_unique_and_are_only_names() -> None:
    pings = [job["ping"] for job in JOBS.values()]
    assert len(set(pings)) == len(pings) == 5
    gates = [job["gate"] for job in JOBS.values()]
    assert len(set(gates)) == len(gates) == 5
    for text in (p.read_text() for p in DAG_DIR.glob("*.yaml")):
        assert not re.search(r"https?://", text)


@pytest.mark.parametrize("stem", sorted(JOBS))
def test_schedules_do_not_overlap_data_plane_runs(stem: str) -> None:
    """A snapshot run starts at least 30 minutes away from any data-plane start on
    the same UTC weekday."""
    minute, days = _utc_slot(UTC_PREFIX + JOBS[stem]["schedule"])
    for name in ("data-plane-kr.yaml", "data-plane-us.yaml", "data-plane-crypto.yaml"):
        dp_minute, dp_days = _data_plane_slot(name)
        if days & dp_days:
            assert abs(minute - dp_minute) >= 30, (stem, name)


def test_kr_screener_runs_after_the_kr_candles_on_the_same_day() -> None:
    kr_candle_minute, kr_candle_days = _candle_slot("at-batch2-daily-candles-kr.yaml")
    minute, days = _utc_slot(UTC_PREFIX + JOBS["at-snapshot-screener-kr"]["schedule"])
    assert days <= kr_candle_days
    assert minute > kr_candle_minute


def _minutes_after_22_utc(minute_of_day: int) -> int:
    """Elapsed minutes from the previous UTC day's 22:00 candle sync."""
    return (minute_of_day - 22 * 60) % (24 * 60)


def test_us_screener_runs_after_the_us_candles_of_the_previous_utc_day() -> None:
    us_candle_minute, us_candle_days = _candle_slot("at-batch2-daily-candles-us.yaml")
    minute, days = _utc_slot(UTC_PREFIX + JOBS["at-snapshot-screener-us"]["schedule"])
    # Tue–Sat screener runs read the Mon–Fri 22:00 UTC candle sync of the day before.
    assert {(d - 1) % 7 for d in days} <= us_candle_days
    assert _minutes_after_22_utc(minute) > _minutes_after_22_utc(us_candle_minute)


def test_us_screener_runs_after_us_valuation_and_the_us_data_plane() -> None:
    valuation_minute, _ = _utc_slot(
        yaml.safe_load(
            (ROOT / "deploy/dagu/at-batch/at-batch1-us-valuation.yaml").read_text()
        )["schedule"]
    )
    us_plane_minute, _ = _data_plane_slot("data-plane-us.yaml")
    minute, _ = _utc_slot(UTC_PREFIX + JOBS["at-snapshot-screener-us"]["schedule"])
    assert _minutes_after_22_utc(minute) > _minutes_after_22_utc(valuation_minute)
    assert _minutes_after_22_utc(minute) > _minutes_after_22_utc(us_plane_minute)


def test_runbook_covers_the_dry_run_threshold_registration_and_stop() -> None:
    text = RUNBOOK.read_text()
    for stem in JOBS:
        assert stem in text, stem
    assert "--limit 20" in text  # the cheap manual dry-run for financial
    assert "--keep-trading-days 5" in text
    assert "채움률" in text
    assert "정지" in text
    assert "dry-run" in text

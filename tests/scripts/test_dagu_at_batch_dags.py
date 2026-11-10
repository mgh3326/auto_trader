"""Static check for the batch-1 Dagu DAG proposals (hk 3848).

The 11 DAGs in ``deploy/dagu/at-batch/`` replace Prefect deployments. They are
proposals: nothing registers them. This test pins, per DAG, the Prefect
deployment it replaces, its schedule (KST in the header, UTC in ``schedule``),
the one command each step may run, and the failure alert path. It does not call
Dagu, Prefect, Docker or a database.

Set ``AT_BATCH_DAG_DIR`` to point the check at a scratch copy (used to show that
a wrong schedule fails).
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
DAG_DIR = Path(os.environ.get("AT_BATCH_DAG_DIR") or ROOT / "deploy/dagu/at-batch")
DATA_PLANE_DIR = ROOT / "deploy/dagu/data-plane"
RUNBOOK = ROOT / "docs/runbooks/dagu-batch1-cutover.md"

DOCKER = (
    "docker run --rm --network host --workdir /app "
    "--env-file /root/at-secrets/.env.api "
    '"$(cat /root/at-run/deployed-digest)" /app/.venv/bin/python -m '
)
GATE_VAR = "INVEST_SCREENER_SNAPSHOTS_COMMIT_ENABLED"
COMMIT = (
    '$(case "$(printf %s "${' + GATE_VAR + '}" | tr A-Z a-z)" in '
    "1|true|yes|on) echo --commit;; esac)"
)


def _past(days: int) -> str:
    if days == 0:
        return "$(TZ=Asia/Seoul date +%F)"
    return f"$(TZ=Asia/Seoul date -d '{days} days ago' +%F)"


def _ahead(days: int) -> str:
    if days == 0:
        return "$(TZ=Asia/Seoul date +%F)"
    return f"$(TZ=Asia/Seoul date -d '{days} days' +%F)"


# file stem -> pinned facts. ``prefect`` is the cron the Prefect flow file sets
# (robin-prefect-automations@d8f0054) in Asia/Seoul; ``interval`` has no fixed time.
JOBS: dict[str, dict] = {
    "at-batch1-crypto-screener": {
        "deployment": "Invest Crypto Screener Snapshots/daily-kst",
        "prefect": "cron 20 9 * * * Asia/Seoul",
        "shift": 15,
        "kst": "09:35",
        "schedule": "35 0 * * *",
        "gate": "none",
        "ping": "AT_BATCH_PING_URL_CRYPTO_SCREENER",
        "steps": [
            (
                "build",
                "scripts.build_invest_crypto_screener_snapshots",
                "--all " + COMMIT,
                3600,
            )
        ],
    },
    "at-batch1-crypto-insight": {
        "deployment": "Invest Crypto Insight Snapshots/daily-kst",
        "prefect": "cron 20 9 * * * Asia/Seoul",
        "shift": 30,
        "kst": "09:50",
        "schedule": "50 0 * * *",
        "gate": "none",
        "ping": "AT_BATCH_PING_URL_CRYPTO_INSIGHT",
        "steps": [("build", "scripts.build_crypto_insight_snapshots", "", 3600)],
    },
    "at-batch1-kr-investor-flow": {
        "deployment": "KR Investor Flow Snapshots/daily-freshness",
        "prefect": "cron 10 18 * * 1-5 Asia/Seoul",
        "shift": 0,
        "kst": "18:10",
        "schedule": "10 9 * * 1-5",
        "gate": "none",
        "ping": "AT_BATCH_PING_URL_KR_INVESTOR_FLOW",
        "steps": [
            (
                "build",
                "scripts.build_investor_flow_snapshots",
                "--market kr --days 5 --batch-size 100 --concurrency 4 --all --commit",
                1800,
            )
        ],
    },
    "at-batch1-kr-fundamentals": {
        "deployment": "invest_kr_fundamentals_snapshots/daily-kst",
        "prefect": "cron 0 18 * * * Asia/Seoul",
        "shift": 20,
        "kst": "18:20",
        "schedule": "20 9 * * *",
        "gate": "none",
        "ping": "AT_BATCH_PING_URL_KR_FUNDAMENTALS",
        "steps": [
            (
                "build",
                "scripts.build_invest_kr_fundamentals_snapshots",
                "--all " + COMMIT,
                7200,
            )
        ],
    },
    "at-batch1-us-valuation": {
        "deployment": "US Market Valuation Snapshots/daily-post-us-close",
        "prefect": "cron 30 8 * * 2-6 Asia/Seoul",
        "shift": 0,
        "kst": "08:30 Tue-Sat",
        "schedule": "30 23 * * 1-5",
        "gate": "none",
        "ping": "AT_BATCH_PING_URL_US_VALUATION",
        "steps": [
            (
                "build",
                "scripts.build_market_valuation_snapshots",
                "--market us --batch-size 100 --concurrency 4 --all --common-stocks-only "
                + COMMIT,
                10800,
            )
        ],
    },
    "at-batch1-toss-warnings": {
        "deployment": "Toss Warnings Sync/daily-preopen",
        "prefect": "cron 30 7 * * * Asia/Seoul",
        "shift": 0,
        "kst": "07:30",
        "schedule": "30 22 * * *",
        "gate": "none",
        "ping": "AT_BATCH_PING_URL_TOSS_WARNINGS",
        "steps": [("sync", "scripts.sync_toss_warnings", "", 3600)],
    },
    "at-batch1-toss-symbol-master": {
        "deployment": "Toss Symbol Master Sync/weekday-preopen",
        "prefect": "cron 20 7 * * 1-5 Asia/Seoul",
        "shift": 0,
        "kst": "07:20 Mon-Fri",
        "schedule": "20 22 * * 0-4",
        "gate": "none",
        "ping": "AT_BATCH_PING_URL_TOSS_SYMBOL_MASTER",
        "steps": [
            (
                "sync-kr",
                "scripts.sync_toss_symbol_master",
                "--market kr --all --commit",
                700,
            ),
            (
                "sync-us",
                "scripts.sync_toss_symbol_master",
                "--market us --all --commit",
                700,
            ),
        ],
    },
    "at-batch1-us-fundamentals": {
        "deployment": "US Financial Fundamentals Snapshots/weekly-sunday",
        "prefect": "cron 0 9 * * 0 Asia/Seoul",
        "shift": 0,
        "kst": "09:00 Sun",
        "schedule": "0 0 * * 0",
        "gate": "hk 3846",
        "ping": "AT_BATCH_PING_URL_US_FUNDAMENTALS",
        "steps": [
            (
                "build",
                "scripts.build_us_fundamentals_snapshots",
                "--concurrency 4 --all --with-dividends " + COMMIT,
                10800,
            )
        ],
    },
    "at-batch1-market-events-daily": {
        "deployment": "Market Events Daily/daily",
        "prefect": "interval 1d (anchor unknown)",
        "shift": 0,
        "kst": "06:30",
        "schedule": "30 21 * * *",
        "gate": "none",
        "ping": "AT_BATCH_PING_URL_MARKET_EVENTS_DAILY",
        "steps": [
            (
                "us-earnings",
                "scripts.ingest_market_events",
                f"--source finnhub --category earnings --market us --from-date {_past(7)} --to-date {_ahead(60)}",
                900,
            ),
            (
                "kr-disclosures",
                "scripts.ingest_market_events",
                f"--source dart --category disclosure --market kr --from-date {_past(7)} --to-date {_ahead(0)}",
                900,
            ),
        ],
    },
    "at-batch1-dart-0700": {
        "deployment": "Market Events KR Disclosures/dart-0700-kst",
        "prefect": "cron 0 7 * * * Asia/Seoul",
        "shift": 0,
        "kst": "07:00",
        "schedule": "0 22 * * *",
        "gate": "hk 3845",
        "ping": "AT_BATCH_PING_URL_DART_0700",
        "steps": [
            (
                "kr-disclosures",
                "scripts.ingest_market_events",
                f"--source dart --category disclosure --market kr --from-date {_past(1)} --to-date {_ahead(0)}",
                900,
            )
        ],
    },
    "at-batch1-dart-1830": {
        "deployment": "Market Events KR Disclosures/dart-1830-kst",
        "prefect": "cron 30 18 * * * Asia/Seoul",
        "shift": 0,
        "kst": "18:30",
        "schedule": "30 9 * * *",
        "gate": "hk 3845",
        "ping": "AT_BATCH_PING_URL_DART_1830",
        "steps": [
            (
                "kr-disclosures",
                "scripts.ingest_market_events",
                f"--source dart --category disclosure --market kr --from-date {_past(0)} --to-date {_ahead(0)}",
                900,
            )
        ],
    },
}

ALLOWED_MODULES = {step[1] for job in JOBS.values() for step in job["steps"]}
KST_OFFSET_MIN = 9 * 60


def _dow_set(field: str) -> set[int]:
    if field == "*":
        return set(range(7))
    days: set[int] = set()
    for part in field.split(","):
        if "-" in part:
            lo, hi = part.split("-")
            days.update(range(int(lo), int(hi) + 1))
        else:
            days.add(int(part))
    return {d % 7 for d in days}


def _split_cron(cron: str) -> tuple[int, int, str, str, str]:
    minute, hour, dom, month, dow = cron.split()
    assert dom == "*" and month == "*", cron
    return int(minute), int(hour), dom, month, dow


def kst_cron_to_utc(kst_cron: str, shift_minutes: int) -> tuple[int, int, set[int]]:
    """KST cron (fixed minute/hour) + shift -> (UTC minute, UTC hour, UTC weekday set)."""
    minute, hour, _, _, dow = _split_cron(kst_cron)
    total = hour * 60 + minute + shift_minutes - KST_OFFSET_MIN
    day_shift, minute_of_day = divmod(total, 24 * 60)
    days = {(d + day_shift) % 7 for d in _dow_set(dow)}
    return minute_of_day % 60, minute_of_day // 60, days


def _utc_cron(cron: str) -> tuple[int, int, set[int]]:
    minute, hour, _, _, dow = _split_cron(cron)
    return minute, hour, _dow_set(dow)


def _header(text: str) -> dict[str, str]:
    return dict(
        re.findall(
            r"^# (prefect_deployment|prefect_schedule|dagu_shift_minutes|dagu_schedule_kst|cutover_gate): (.+)$",
            text,
            re.M,
        )
    )


def _load(stem: str) -> tuple[str, dict]:
    path = DAG_DIR / f"{stem}.yaml"
    text = path.read_text()
    return text, yaml.safe_load(text)


def test_exactly_the_eleven_documented_dags_exist():
    assert sorted(p.stem for p in DAG_DIR.glob("*.yaml")) == sorted(JOBS)


@pytest.mark.parametrize("stem", sorted(JOBS))
def test_header_names_the_runbook_and_the_prefect_deployment(stem):
    text, _ = _load(stem)
    job = JOBS[stem]
    assert "registered only via the runbook below" in text.split("description:")[0]
    assert "docs/runbooks/dagu-batch1-cutover.md" in text
    header = _header(text)
    assert header["prefect_deployment"] == job["deployment"]
    assert header["prefect_schedule"] == job["prefect"]
    assert int(header["dagu_shift_minutes"]) == job["shift"]
    assert header["dagu_schedule_kst"] == job["kst"]
    assert header["cutover_gate"] == job["gate"]


@pytest.mark.parametrize("stem", sorted(JOBS))
def test_schedule_is_the_prefect_schedule_in_utc(stem):
    text, dag = _load(stem)
    job = JOBS[stem]
    schedule = dag["schedule"]
    assert schedule == job["schedule"], "the pinned UTC cron changed"
    if job["prefect"].startswith("cron "):
        kst_cron = job["prefect"].removeprefix("cron ").removesuffix(" Asia/Seoul")
        expected = kst_cron_to_utc(kst_cron, job["shift"])
    else:  # interval: no fixed Prefect time; the header time is the chosen one
        hour, minute = job["kst"].split(":")
        expected = kst_cron_to_utc(f"{int(minute)} {int(hour)} * * *", 0)
    assert _utc_cron(schedule) == expected
    # The header's documented KST time is the pre-shift time plus the shift.
    kst_clock = job["kst"].split()[0]
    kst_hour, kst_minute = (int(x) for x in kst_clock.split(":"))
    if job["prefect"].startswith("cron "):
        base_minute, base_hour, *_ = _split_cron(kst_cron)
        assert kst_hour * 60 + kst_minute == base_hour * 60 + base_minute + job["shift"]


def test_the_two_overlaps_are_at_least_ten_minutes_from_the_data_plane():
    def slot(path_or_cron: str) -> tuple[int, set[int]]:
        minute, hour, days = _utc_cron(path_or_cron)
        return hour * 60 + minute, days

    crypto_plane, _ = slot(
        yaml.safe_load((DATA_PLANE_DIR / "data-plane-crypto.yaml").read_text())[
            "schedule"
        ]
    )
    kr_plane, kr_days = slot(
        yaml.safe_load((DATA_PLANE_DIR / "data-plane-kr.yaml").read_text())["schedule"]
    )
    for stem in ("at-batch1-crypto-screener", "at-batch1-crypto-insight"):
        start, _ = slot(_load(stem)[1]["schedule"])
        assert start - crypto_plane >= 10, stem
    start, days = slot(_load("at-batch1-kr-fundamentals")[1]["schedule"])
    assert days >= kr_days and start - kr_plane >= 10
    # The shifted crypto jobs also stay apart from each other.
    a, _ = slot(_load("at-batch1-crypto-screener")[1]["schedule"])
    b, _ = slot(_load("at-batch1-crypto-insight")[1]["schedule"])
    assert abs(a - b) >= 10


@pytest.mark.parametrize("stem", sorted(JOBS))
def test_commands_are_the_allowlisted_container_form_and_nothing_else(stem):
    _, dag = _load(stem)
    job = JOBS[stem]
    steps = dag["steps"]
    assert [(s["name"], s["command"], s["timeout_sec"]) for s in steps] == [
        (name, DOCKER + module + (f" {args}" if args else ""), timeout)
        for name, module, args, timeout in job["steps"]
    ]
    for step in steps:
        command = step["command"]
        assert re.fullmatch(
            r"docker run --rm --network host --workdir /app --env-file /root/at-secrets/\.env\.api \"\$\(cat /root/at-run/deployed-digest\)\" /app/\.venv/bin/python -m scripts\.[a-z0-9_]+( .*)?",
            command,
        )
        module = re.search(r"-m (scripts\.[a-z0-9_]+)", command).group(1)
        assert module in ALLOWED_MODULES
        assert (ROOT / (module.replace(".", "/") + ".py")).is_file(), module
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
        # `;` and `|` may only appear inside the one commit-gate substitution.
        assert re.sub(re.escape(COMMIT), "", command).count(";") == 0
        assert " | " not in re.sub(re.escape(COMMIT), "", command)
        assert step["retry_policy"]["limit"] == 1


@pytest.mark.parametrize("stem", sorted(JOBS))
def test_commit_flag_is_literal_only_where_prefect_always_committed(stem):
    _, dag = _load(stem)
    always = {"at-batch1-kr-investor-flow", "at-batch1-toss-symbol-master"}
    gated = {
        "at-batch1-crypto-screener",
        "at-batch1-kr-fundamentals",
        "at-batch1-us-valuation",
        "at-batch1-us-fundamentals",
    }
    for step in dag["steps"]:
        command = step["command"]
        bare = command.replace(COMMIT, "")
        if stem in always:
            assert " --commit" in bare
        else:
            assert "--commit" not in bare, "a literal --commit must not bypass the gate"
        assert (COMMIT in command) == (stem in gated)
        assert "--dry-run" not in command  # Prefect ran dry_run=False


@pytest.mark.parametrize("stem", sorted(JOBS))
def test_every_dag_is_single_flight_bounded_and_alerts_on_failure(stem):
    text, dag = _load(stem)
    job = JOBS[stem]
    assert dag["max_active_runs"] == 1
    assert dag["worker_selector"] == {"host": "ncp"}
    assert dag["dotenv"] == "/root/at-secrets/.env.dagu-at-batch"
    assert all(
        isinstance(s["timeout_sec"], int) and s["timeout_sec"] > 0 for s in dag["steps"]
    )
    ping = job["ping"]
    handlers = dag["handler_on"]
    assert (
        handlers["success"]["command"]
        == f'curl -fsS -m 10 --retry 3 -o /dev/null "${{{ping}}}"'
    )
    assert (
        handlers["failure"]["command"]
        == f'curl -fsS -m 10 --retry 3 -o /dev/null "${{{ping}}}/fail"'
    )
    # continue_on makes Dagu end partially_succeeded and fire the *success* handler.
    assert "continue_on" not in dag["steps"][0]
    assert not re.search(
        r"https?://", text.replace("https://dagu", "")
    )  # no ping URL values in the repo


def test_ping_names_are_unique_and_shared_gate_is_only_a_name():
    pings = [job["ping"] for job in JOBS.values()]
    assert len(set(pings)) == len(pings) == 11
    assert all(re.fullmatch(r"AT_BATCH_PING_URL_[A-Z0-9_]+", p) for p in pings)


def test_runbook_orders_the_gated_jobs_after_their_fixes():
    text = RUNBOOK.read_text()
    for stem, job in JOBS.items():
        assert stem in text, stem
        assert job["deployment"] in text, job["deployment"]
    gated = {stem: job["gate"] for stem, job in JOBS.items() if job["gate"] != "none"}
    assert gated == {
        "at-batch1-us-fundamentals": "hk 3846",
        "at-batch1-dart-0700": "hk 3845",
        "at-batch1-dart-1830": "hk 3845",
    }
    order = text.split("## 전환 순서", 1)[1].split("\n## ", 1)[0]
    assert order.index("hk 3846") > order.index("1차") and "hk 3845" in order
    for stem in gated:
        assert order.index(stem) > order.index("2차"), stem
    assert "prefect deployment schedule pause" in text
    assert "prefect deployment schedule resume" in text

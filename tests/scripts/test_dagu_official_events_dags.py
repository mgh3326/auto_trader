"""Static check of the official-event Dagu copies (hk 6409, design 6400 §3.1).

The four DAGs are copies: nothing in this repository registers them. This pins
their schedules, the one collector command each step may run (parsed by the real
collector ``parse_args``), the journal handlers, and that no scheduler surface
in the repository refers to them or to the manual backfill CLI. It does not call
Dagu, Docker or a database.
"""

from __future__ import annotations

import re
import shlex
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from app.services.market_events import ingestion
from scripts import (
    collect_crypto_notices,
    collect_sec_edgar,
    ingest_market_events,
)

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
DAG_DIR = ROOT / "deploy/dagu/at-official-events"
DOCKER = (
    "docker run --rm --init --network host --workdir /app "
    "--env-file /root/at-secrets/.env.api "
    '"$(cat /root/at-run/deployed-digest)" /app/.venv/bin/python -m '
)
DOTENV = "/root/at-secrets/.env.dagu-at-official-events"


def _kst(days: int) -> str:
    if days == 0:
        return "$(TZ=Asia/Seoul date +%F)"
    if days < 0:
        return f"$(TZ=Asia/Seoul date -d '{-days} days ago' +%F)"
    return f"$(TZ=Asia/Seoul date -d '{days} days' +%F)"


def _et(days: int) -> str:
    if days == 0:
        return "$(TZ=America/New_York date +%F)"
    return f"$(TZ=America/New_York date -d '{-days} days ago' +%F)"


JOBS: dict[str, dict] = {
    "official-events-kr-0700": {
        "schedule": "CRON_TZ=Asia/Seoul 0 7 * * *",
        "ping": "AT_OFFICIAL_EVENTS_PING_URL_KR_0700",
        "steps": [
            (
                "dart-disclosures",
                "scripts.ingest_market_events --source dart --category disclosure "
                f"--market kr --from-date {_kst(-1)} --to-date {_kst(0)}",
            )
        ],
    },
    "official-events-kr-1830": {
        "schedule": "CRON_TZ=Asia/Seoul 30 18 * * *",
        "ping": "AT_OFFICIAL_EVENTS_PING_URL_KR_1830",
        "steps": [
            (
                "dart-disclosures",
                "scripts.ingest_market_events --source dart --category disclosure "
                f"--market kr --from-date {_kst(0)} --to-date {_kst(0)}",
            )
        ],
    },
    "official-events-us": {
        "schedule": "CRON_TZ=Asia/Seoul 30 7 * * *",
        "ping": "AT_OFFICIAL_EVENTS_PING_URL_US",
        "steps": [
            (
                "finnhub-earnings",
                "scripts.ingest_market_events --source finnhub --category earnings "
                f"--market us --from-date {_kst(-7)} --to-date {_kst(60)} --force",
            ),
            (
                "sec-edgar",
                "scripts.collect_sec_edgar --symbols "
                "${AT_OFFICIAL_EVENTS_SEC_SYMBOLS} "
                f"--from-date {_et(-7)} --to-date {_et(0)} --commit",
            ),
        ],
    },
    "official-events-crypto": {
        "schedule": "CRON_TZ=Asia/Seoul 30 6 * * *",
        "ping": "AT_OFFICIAL_EVENTS_PING_URL_CRYPTO",
        "steps": [("exchange-notices", "scripts.collect_crypto_notices --commit")],
    },
}
PARSERS = {
    "scripts.ingest_market_events": ingest_market_events.parse_args,
    "scripts.collect_sec_edgar": collect_sec_edgar.parse_args,
    "scripts.collect_crypto_notices": collect_crypto_notices.parse_args,
}


def _load(stem: str) -> tuple[str, dict]:
    text = (DAG_DIR / f"{stem}.yaml").read_text(encoding="utf-8")
    return text, yaml.safe_load(text)


def test_the_directory_holds_exactly_the_four_copies() -> None:
    assert sorted(p.name for p in DAG_DIR.iterdir()) == sorted(
        f"{stem}.yaml" for stem in JOBS
    )


@pytest.mark.parametrize("stem", sorted(JOBS))
def test_each_dag_is_an_unregistered_copy_with_pinned_steps(stem: str) -> None:
    job = JOBS[stem]
    text, doc = _load(stem)
    assert text.startswith("# 미등록 Dagu 사본(hk 6409")
    assert "저장소는 이 파일을 등록하지 않으며" in text.splitlines()[0]
    assert doc["schedule"] == job["schedule"]
    assert doc["max_active_runs"] == 1
    assert doc["worker_selector"] == {"host": "pi"}
    assert doc["dotenv"] == DOTENV
    assert "env" not in doc and "params" not in doc

    steps = doc["steps"]
    assert [s["name"] for s in steps] == [name for name, _ in job["steps"]]
    for step, (_, command) in zip(steps, job["steps"], strict=True):
        assert step["command"] == DOCKER + command
        assert "continue_on" not in step and "depends" not in step
        assert 0 < step["timeout_sec"] <= 900
    assert "request_market_event_backfill" not in text

    for event, suffix in (("success", ""), ("failure", "/fail")):
        handler = doc["handler_on"][event]["command"]
        assert handler.startswith(
            f'logger -t at-dagu-official-events "dag={stem} status={event}"; '
        )
        assert f'"${{{job["ping"]}}}{suffix}"' in handler
        assert f'[ -z "${{{job["ping"]}:-}}" ] ||' in handler


@pytest.mark.parametrize("stem", sorted(JOBS))
def test_each_command_is_accepted_by_the_collector_parser(stem: str) -> None:
    """The flags exist in the real CLI, and no step writes by default by accident."""
    for step in _load(stem)[1]["steps"]:
        command = step["command"].removeprefix(DOCKER)
        command = re.sub(r"\$\(TZ=[^)]*\)", "2026-11-27", command)
        command = command.replace("${AT_OFFICIAL_EVENTS_SEC_SYMBOLS}", "NVDA AMD")
        module, *argv = shlex.split(command)
        parsed = PARSERS[module](argv)
        if module == "scripts.ingest_market_events":
            assert parsed.dry_run is False
            # only the rolling finnhub window replays collected days
            assert parsed.force is (parsed.source == "finnhub")
            assert parsed.from_date == date(2026, 11, 27)
        else:
            assert parsed.commit is True
        if module == "scripts.collect_sec_edgar":
            assert parsed.symbols == ["NVDA", "AMD"]


@pytest.mark.asyncio
async def test_the_rolling_finnhub_step_rereads_days_already_succeeded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A future day that succeeded empty must receive a later announcement."""
    day = date(2026, 12, 10)
    command = _load("official-events-us")[1]["steps"][0]["command"]
    command = re.sub(r"\$\(TZ=[^)]*\)", day.isoformat(), command.removeprefix(DOCKER))
    parsed = ingest_market_events.parse_args(shlex.split(command)[1:])
    stored: list[str] = []

    class Repository:
        """Every day of the window is already ``succeeded``."""

        def __init__(self, _db: Any) -> None: ...

        async def list_succeeded_partitions_in_range(self, **_: Any) -> set[date]:
            return {day}

        async def get_or_create_partition(self, **_: Any) -> Any:
            return SimpleNamespace()

        async def mark_partition_running(self, _partition: Any) -> None: ...

        async def mark_partition_succeeded(self, _partition: Any, **_: Any) -> None: ...

        async def upsert_event_with_values(self, event: dict, _values: list) -> None:
            stored.append(event["symbol"])

    class Session:
        async def commit(self) -> None: ...

    async def finnhub(*_: Any) -> dict[str, Any]:
        return {"earnings": [{"date": day.isoformat(), "symbol": "NVDA"}]}

    monkeypatch.setattr(ingestion, "MarketEventsRepository", Repository)
    monkeypatch.setattr(ingestion, "fetch_earnings_calendar_finnhub", finnhub)
    code = await ingest_market_events.run_ingest(
        db=Session(),  # type: ignore[arg-type]
        source=parsed.source,
        category=parsed.category,
        market=parsed.market,
        from_date=parsed.from_date,
        to_date=parsed.to_date,
        dry_run=parsed.dry_run,
        force=parsed.force,
    )
    assert code == 0
    assert stored == ["NVDA"]


def test_an_empty_symbol_list_fails_the_sec_step_instead_of_collecting() -> None:
    command = next(
        s["command"]
        for s in _load("official-events-us")[1]["steps"]
        if s["name"] == "sec-edgar"
    )
    command = re.sub(r"\$\(TZ=[^)]*\)", "2026-11-27", command.removeprefix(DOCKER))
    command = command.replace("${AT_OFFICIAL_EVENTS_SEC_SYMBOLS}", "")
    module, *argv = shlex.split(command)
    with pytest.raises(SystemExit):
        PARSERS[module](argv)


def test_nothing_in_the_repository_registers_the_copies() -> None:
    """No code, workflow or unit refers to the DAG copies (hard rule 6)."""
    needles = ("at-official-events", "official-events-")
    offenders = []
    for base in ("app", "scripts", "deploy", ".github", "ops", ".mergewake"):
        root = ROOT / base
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path.parent == DAG_DIR:
                continue
            if path.suffix not in {".py", ".sh", ".yaml", ".yml", ".service", ".timer"}:
                continue
            body = path.read_text(encoding="utf-8", errors="ignore")
            offenders += [
                str(path.relative_to(ROOT)) for needle in needles if needle in body
            ]
    assert offenders == []

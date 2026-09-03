from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from app.services.fill_event_handoff import service as handoff_service
from app.services.fill_event_handoff.service import (
    FillHandoffRunner,
    HandoffConfig,
    dedupe_key,
    in_regular_rep_window,
    next_rep,
)
from scripts import fill_event_handoff as handoff_cli


def _fill(ledger_id: int) -> dict[str, Any]:
    return {
        "ledger_id": ledger_id,
        "event_key": f"execution_ledger:{ledger_id}",
        "broker": "upbit",
        "account_mode": "live",
        "venue": "upbit",
        "market": "crypto",
        "symbol": "BTC",
        "side": "sell",
        "filled_qty": "0.1",
        "filled_price": "100",
        "filled_notional": "10",
        "currency": "KRW",
        "broker_order_id": "same-order",
        "correlation_id": "c-1",
        "filled_at": "2026-09-03T00:00:00+00:00",
    }


def test_dedupe_key_ignores_reconciler_row_identity() -> None:
    assert dedupe_key(_fill(1)) == dedupe_key(_fill(2))


def test_rep_windows_and_next_rep() -> None:
    assert in_regular_rep_window("crypto", datetime(2026, 9, 3, 5, 25, tzinfo=UTC))
    assert not in_regular_rep_window("crypto", datetime(2026, 9, 3, 6, 0, tzinfo=UTC))
    assert next_rep("crypto", datetime(2026, 9, 3, 1, 0, tzinfo=UTC)) == "crypto-1420"


def test_submission_rescues_one_unsubmitted_paste(tmp_path: Path) -> None:
    reads = iter(["[Pasted text]", "prompt_submitted"])
    calls: list[tuple[str, ...]] = []

    def command(argv: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(tuple(argv))
        stdout = next(reads) if "read" in argv else ""
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    runner = FillHandoffRunner(HandoffConfig(state_dir=tmp_path), command=command)
    assert runner._submit("local:operator", "w:p2", "handoff")
    assert sum("send-keys" in call for call in calls) == 1


def test_submission_requires_post_return_reread(tmp_path: Path) -> None:
    """A return key is not a submission receipt while the paste chip remains."""
    reads = iter(["[Pasted text]", "[Pasted text]"])
    calls: list[tuple[str, ...]] = []

    def command(argv: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(tuple(argv))
        stdout = next(reads) if "read" in argv else ""
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    runner = FillHandoffRunner(HandoffConfig(state_dir=tmp_path), command=command)
    assert runner._submit("local:operator", "w:p2", "handoff") is False
    assert sum("read" in call for call in calls) == 2
    assert sum("send-keys" in call for call in calls) == 1


def test_submission_never_returns_into_a_queued_prompt(tmp_path: Path) -> None:
    calls: list[tuple[str, ...]] = []

    def command(argv: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(tuple(argv))
        stdout = (
            "Press up to edit queued messages"
            if "read" in argv and sum("read" in call for call in calls) == 1
            else "prompt_submitted"
        )
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    runner = FillHandoffRunner(HandoffConfig(state_dir=tmp_path), command=command)
    assert runner._submit("local:operator", "w:p2", "handoff") is False
    assert sum("send-keys" in call for call in calls) == 0


def test_kick_is_disabled_by_default(tmp_path: Path) -> None:
    runner = FillHandoffRunner(HandoffConfig(state_dir=tmp_path))
    assert asyncio.run(runner._kick(_fill(1), {"cooldowns": {}})) is None


def test_kick_respects_cooldown_and_creates_prefect_run(tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    async def post(url: str, body: dict[str, Any]) -> dict[str, Any]:
        calls.append((url, body))
        return (
            {"items": [{"id": "deployment-id"}]}
            if url.endswith("/filter")
            else {"id": "flow-id"}
        )

    def now() -> datetime:
        return datetime(2026, 9, 3, 1, 0, tzinfo=UTC)

    runner = FillHandoffRunner(
        HandoffConfig(
            state_dir=tmp_path,
            kick_enabled=True,
            prefect_api_url="http://prefect",
            kick_deployments={"crypto": "crypto-deployment"},
        ),
        now=now,
        http_post=post,
    )
    state = {"cooldowns": {}}
    assert asyncio.run(runner._kick(_fill(7), state)) == "flow-id"
    assert len(calls) == 2
    assert asyncio.run(runner._kick(_fill(8), state)) is None


def test_runner_dedupes_rows_advances_watermark_and_keeps_event_idempotent(
    tmp_path: Path, monkeypatch: Any
) -> None:
    class Repo:
        def __init__(self, _db: object) -> None:
            pass

        async def max_ledger_id(self) -> int:
            return 2

        async def list_recent_fills_for_triage(
            self, **_kwargs: object
        ) -> list[dict[str, Any]]:
            return [_fill(1), _fill(2)]  # websocket + reconciler identity duplicates

    class Context:
        event_keys: set[str] = set()
        appended = 0

        def __init__(self, _db: object) -> None:
            pass

        async def get_open_question_for_event_key(self, key: str) -> object | None:
            return SimpleNamespace(id=1) if key in self.event_keys else None

        async def append_entries(self, entries: list[object]) -> list[object]:
            self.event_keys.add(entries[0].refs.event_key)
            self.__class__.appended += 1
            return [SimpleNamespace(id=1)]

        async def append_fill_handoff_kick_result(self, **_kwargs: object) -> None:
            raise AssertionError("kick is disabled")

    class Db:
        async def commit(self) -> None:
            pass

    monkeypatch.setattr(handoff_service, "ExecutionLedgerRepository", Repo)
    monkeypatch.setattr(handoff_service, "SessionContextService", Context)
    monkeypatch.setattr(handoff_service, "sanitize_fill", lambda row: row)
    config = HandoffConfig(state_dir=tmp_path)
    (tmp_path / "state.json").write_text(
        json.dumps({"version": 1, "watermark": 0, "seen": {}, "cooldowns": {}}),
        encoding="utf-8",
    )
    assert asyncio.run(FillHandoffRunner(config).run(Db()))["durable"] == 1
    state = (tmp_path / "state.json").read_text()
    assert '"watermark": 2' in state
    assert Context.appended == 1
    (tmp_path / "state.json").unlink()  # simulate a state-file recovery
    assert asyncio.run(FillHandoffRunner(config).run(Db()))["durable"] == 0
    assert Context.appended == 1


def test_first_run_seeds_600_historical_rows_without_appending(
    tmp_path: Path, monkeypatch: Any
) -> None:
    historical = [_fill(ledger_id) for ledger_id in range(1, 601)]

    class Repo:
        def __init__(self, _db: object) -> None:
            pass

        async def max_ledger_id(self) -> int:
            return max(row["ledger_id"] for row in historical)

        async def list_recent_fills_for_triage(self, **_kwargs: object) -> list[object]:
            raise AssertionError("a new state file must not backfill historical rows")

    class Db:
        async def commit(self) -> None:
            raise AssertionError("first-run seed must not commit context")

    monkeypatch.setattr(handoff_service, "ExecutionLedgerRepository", Repo)
    result = asyncio.run(FillHandoffRunner(HandoffConfig(state_dir=tmp_path)).run(Db()))
    assert result == {"durable": 0, "pushed": 0, "kicked": 0}
    assert json.loads((tmp_path / "state.json").read_text())["watermark"] == 600


def test_first_run_since_ledger_id_processes_only_later_rows(
    tmp_path: Path, monkeypatch: Any
) -> None:
    calls: list[dict[str, object]] = []

    class Repo:
        def __init__(self, _db: object) -> None:
            pass

        async def max_ledger_id(self) -> int:
            raise AssertionError(
                "an explicit seed must not inspect the high-water mark"
            )

        async def list_recent_fills_for_triage(
            self, **kwargs: object
        ) -> list[dict[str, Any]]:
            calls.append(dict(kwargs))
            return [_fill(600)]

    class Context:
        appended = 0

        def __init__(self, _db: object) -> None:
            pass

        async def get_open_question_for_event_key(self, _key: str) -> None:
            return None

        async def append_entries(self, _entries: list[object]) -> list[object]:
            self.__class__.appended += 1
            return [SimpleNamespace(id=1)]

    class Db:
        async def commit(self) -> None:
            pass

    monkeypatch.setattr(handoff_service, "ExecutionLedgerRepository", Repo)
    monkeypatch.setattr(handoff_service, "SessionContextService", Context)
    monkeypatch.setattr(handoff_service, "sanitize_fill", lambda row: row)
    result = asyncio.run(
        FillHandoffRunner(HandoffConfig(state_dir=tmp_path, since_ledger_id=599)).run(
            Db()
        )
    )
    assert result["durable"] == 1
    assert calls == [{"after_id": 599, "source": None, "limit": 500}]
    assert Context.appended == 1


def test_existing_event_key_is_idempotent_without_seen_state(
    tmp_path: Path, monkeypatch: Any
) -> None:
    class Repo:
        def __init__(self, _db: object) -> None:
            pass

        async def list_recent_fills_for_triage(
            self, **_kwargs: object
        ) -> list[dict[str, Any]]:
            return [_fill(1)]

    class Context:
        def __init__(self, _db: object) -> None:
            pass

        async def get_open_question_for_event_key(self, _key: str) -> object:
            return SimpleNamespace(id=1)

        async def append_entries(self, _entries: list[object]) -> list[object]:
            raise AssertionError(
                "existing event keys must not append a second question"
            )

    class Db:
        async def commit(self) -> None:
            pass

    (tmp_path / "state.json").write_text(
        json.dumps({"version": 1, "watermark": 0, "seen": {}, "cooldowns": {}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(handoff_service, "ExecutionLedgerRepository", Repo)
    monkeypatch.setattr(handoff_service, "SessionContextService", Context)
    monkeypatch.setattr(handoff_service, "sanitize_fill", lambda row: row)
    result = asyncio.run(FillHandoffRunner(HandoffConfig(state_dir=tmp_path)).run(Db()))
    assert result["durable"] == 0


def test_cli_and_unit_layer_settings_environment(tmp_path: Path) -> None:
    args = handoff_cli.parse_args(["--since-ledger-id", "54646", "--dry-run", "--once"])
    assert (args.since_ledger_id, args.dry_run, args.once) == (54646, True, True)

    root = Path(__file__).parents[2]
    unit = (root / "ops/ncp/systemd/fill-event-handoff.service").read_text()
    timer = (root / "ops/ncp/systemd/fill-event-handoff.timer").read_text()
    assert "EnvironmentFile=/root/at-secrets/.env.api" in unit
    assert "EnvironmentFile=/root/at-secrets/.env.fill-handoff" in unit
    assert (
        "--env-file /root/at-secrets/.env.api --env-file /root/at-secrets/.env.fill-handoff"
        in unit
    )
    assert "scripts.fill_event_handoff --once" in unit
    assert "OnBootSec=2min" in timer

    api_environment = {
        "KIS_APP_KEY": "test",
        "KIS_APP_SECRET": "test",
        "OPENDART_API_KEY": "test",
        "UPBIT_ACCESS_KEY": "test",
        "UPBIT_SECRET_KEY": "test",
        "SECRET_KEY": "Test-secret-key-with-1234567890-long",
    }
    handoff_environment = {
        "DATABASE_URL": "postgresql+asyncpg://test:test@127.0.0.1/test",
        "FILL_HANDOFF_STATE_DIR": str(tmp_path),
        "FILL_HANDOFF_HERDR_TARGETS": "",
        "PREFECT_API_URL": "http://127.0.0.1:4200",
        "FILL_HANDOFF_KICK_ENABLED": "false",
        "FILL_HANDOFF_KICK_COOLDOWN_S": "3600",
        "FILL_HANDOFF_KICK_DEPLOYMENTS": "{}",
        "DISCORD_FILL_HANDOFF_WEBHOOK": "",
    }
    # Equivalent to `env -i` with the two unit files layered. The socket guard
    # deliberately rejects an `env -i ... python` child because it would lose
    # the child policy channel, so pass the clean mapping directly instead.
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "from scripts.fill_event_handoff import parse_args; parse_args(['--once'])",
        ],
        cwd=root,
        env={**api_environment, **handoff_environment},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr


def test_no_model_command_tokens_in_runtime_handoff_sources() -> None:
    root = Path(__file__).parents[2]
    source = (root / "app/services/fill_event_handoff/service.py").read_text()
    script = (root / "scripts/fill_event_handoff.py").read_text()
    assert "cl" + "aude" not in source + script
    assert "her" + "mes" not in source + script

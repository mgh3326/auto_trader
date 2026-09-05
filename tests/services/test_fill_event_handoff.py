from __future__ import annotations

import asyncio
import json
import stat
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.services.fill_event_handoff import service as handoff_service
from app.services.fill_event_handoff.service import (
    FillHandoffRunner,
    HandoffConfig,
    dedupe_key,
    handoff_lane_event_text,
    in_regular_rep_window,
    next_rep,
)
from app.services.lane_events import LANE_EVENT_TEXT_LIMIT, LaneEventConfig
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


@pytest.fixture
def lane_emit_binary(tmp_path: Path) -> Path:
    binary = tmp_path / "fake_panewire_emit.py"
    binary.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys
import time

log_path = os.environ.get("LANE_EVENT_TEST_CALL_LOG")
if log_path:
    with open(log_path, "a", encoding="utf-8") as log_file:
        log_file.write("emit\\n")
dump_path = os.environ.get("LANE_EVENT_TEST_ARGV")
if dump_path:
    with open(dump_path, "w", encoding="utf-8") as dump_file:
        json.dump(sys.argv, dump_file)
mode = os.environ.get("LANE_EVENT_TEST_MODE", "success")
if mode == "duplicate":
    print("emit: duplicate event_id", file=sys.stderr)
    raise SystemExit(2)
if mode == "hang":
    time.sleep(30)
""",
        encoding="utf-8",
    )
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    return binary


def _prepare_runner_dependencies(
    monkeypatch: pytest.MonkeyPatch, rows: list[dict[str, Any]]
) -> type[Any]:
    class Repo:
        def __init__(self, _db: object) -> None:
            pass

        async def max_ledger_id(self) -> int:
            return max((int(row["ledger_id"]) for row in rows), default=0)

        async def list_recent_fills_for_triage(
            self, **_kwargs: object
        ) -> list[dict[str, Any]]:
            return rows

    class Context:
        event_keys: set[str] = set()
        kick_results: list[str] = []

        def __init__(self, _db: object) -> None:
            pass

        async def get_open_question_for_event_key(self, key: str) -> object | None:
            return SimpleNamespace(id=1) if key in self.event_keys else None

        async def append_entries(self, entries: list[object]) -> list[object]:
            self.event_keys.add(entries[0].refs.event_key)
            return [SimpleNamespace(id=1)]

        async def append_fill_handoff_kick_result(self, **kwargs: object) -> None:
            self.kick_results.append(str(kwargs["flow_run_id"]))

    monkeypatch.setattr(handoff_service, "ExecutionLedgerRepository", Repo)
    monkeypatch.setattr(handoff_service, "SessionContextService", Context)
    monkeypatch.setattr(handoff_service, "sanitize_fill", lambda row: row)
    return Context


class _Db:
    async def commit(self) -> None:
        pass


def _write_empty_handoff_state(tmp_path: Path) -> None:
    (tmp_path / "state.json").write_text(
        json.dumps({"version": 1, "watermark": 0, "seen": {}, "cooldowns": {}}),
        encoding="utf-8",
    )


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
    assert result == {
        "durable": 0,
        "pushed": 0,
        "kicked": 0,
        "duplicate": 0,
        "fallback": [],
    }
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


def test_handoff_lane_event_text_matches_legacy_prompt_and_compacts_large_fill() -> (
    None
):
    fill = _fill(1)
    expected = (
        "체결 인계: BTC sell 0.1@100 (KRW 10). briefing.session_context의 "
        "fill_handoff=v1 open_question을 같은 refs의 decision으로 닫아라."
    )
    assert handoff_lane_event_text(fill).encode("utf-8") == expected.encode("utf-8")

    large = _fill(12)
    large["symbol"] = "S" * 1000
    large["filled_notional"] = "9" * 1500
    compact = handoff_lane_event_text(large)
    assert len(compact.encode("utf-8")) <= LANE_EVENT_TEXT_LIMIT
    for value in (
        large["symbol"],
        large["side"],
        large["filled_qty"],
        large["filled_price"],
        large["ledger_id"],
    ):
        assert str(value) in compact


def test_lanes_unset_never_starts_emit_and_preserves_herdr_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lane_emit_binary: Path
) -> None:
    _prepare_runner_dependencies(monkeypatch, [_fill(1)])
    _write_empty_handoff_state(tmp_path)
    argv_file = tmp_path / "argv.json"
    monkeypatch.setenv("LANE_EVENT_TEST_ARGV", str(argv_file))
    calls: list[tuple[str, ...]] = []

    def command(argv: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(tuple(argv))
        if argv[1:] == ["agent", "list"]:
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=json.dumps(
                    {
                        "agents": [
                            {
                                "agent_session": {"label": "opa-crypto"},
                                "agent_status": "idle",
                                "pane_id": "w1:p1",
                            }
                        ]
                    }
                ),
                stderr="",
            )
        if "read" in argv:
            return subprocess.CompletedProcess(
                argv, 0, stdout="prompt_submitted", stderr=""
            )
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    result = asyncio.run(
        FillHandoffRunner(
            HandoffConfig(
                state_dir=tmp_path,
                herdr_targets=("local:operator",),
                lane_event=LaneEventConfig(binary=str(lane_emit_binary)),
            ),
            command=command,
        ).run(_Db())
    )
    assert result == {
        "durable": 1,
        "pushed": 1,
        "kicked": 0,
        "duplicate": 0,
        "fallback": [],
    }
    assert not argv_file.exists()
    assert calls[0] == ("herdr", "agent", "list")


def test_lane_event_emitted_skips_herdr_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lane_emit_binary: Path
) -> None:
    _prepare_runner_dependencies(monkeypatch, [_fill(1)])
    _write_empty_handoff_state(tmp_path)
    argv_file = tmp_path / "argv.json"
    monkeypatch.setenv("LANE_EVENT_TEST_ARGV", str(argv_file))

    def command(_argv: list[str]) -> subprocess.CompletedProcess[str]:
        raise AssertionError("lane event success must not discover herdr panes")

    result = asyncio.run(
        FillHandoffRunner(
            HandoffConfig(
                state_dir=tmp_path,
                herdr_targets=("local:operator",),
                lane_events={"crypto": "lane-a"},
                lane_event=LaneEventConfig(binary=str(lane_emit_binary)),
            ),
            command=command,
        ).run(_Db())
    )
    assert result["pushed"] == 1
    assert result["duplicate"] == 0
    assert result["fallback"] == []
    assert json.loads(argv_file.read_text(encoding="utf-8"))[7] == "execution_ledger:1"


def test_lane_event_duplicate_skips_herdr_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lane_emit_binary: Path
) -> None:
    _prepare_runner_dependencies(monkeypatch, [_fill(1)])
    _write_empty_handoff_state(tmp_path)
    monkeypatch.setenv("LANE_EVENT_TEST_MODE", "duplicate")

    def command(_argv: list[str]) -> subprocess.CompletedProcess[str]:
        raise AssertionError("duplicate lane event must not discover herdr panes")

    result = asyncio.run(
        FillHandoffRunner(
            HandoffConfig(
                state_dir=tmp_path,
                herdr_targets=("local:operator",),
                lane_events={"crypto": "lane-a"},
                lane_event=LaneEventConfig(binary=str(lane_emit_binary)),
            ),
            command=command,
        ).run(_Db())
    )
    assert result["duplicate"] == 1
    assert result["pushed"] == 0
    assert result["fallback"] == []


def test_lane_event_timeout_falls_back_to_herdr_before_prefect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lane_emit_binary: Path
) -> None:
    _prepare_runner_dependencies(monkeypatch, [_fill(1)])
    _write_empty_handoff_state(tmp_path)
    call_log = tmp_path / "calls.log"
    monkeypatch.setenv("LANE_EVENT_TEST_MODE", "hang")
    monkeypatch.setenv("LANE_EVENT_TEST_CALL_LOG", str(call_log))

    def append_call(name: str) -> None:
        with call_log.open("a", encoding="utf-8") as log_file:
            log_file.write(f"{name}\n")

    def command(argv: list[str]) -> subprocess.CompletedProcess[str]:
        assert argv == ["herdr", "agent", "list"]
        append_call("herdr-list")
        return subprocess.CompletedProcess(argv, 0, stdout='{"agents": []}', stderr="")

    async def post(url: str, _body: dict[str, Any]) -> dict[str, Any]:
        if url.endswith("/filter"):
            append_call("prefect-filter")
            return {"items": [{"id": "deployment-id"}]}
        append_call("prefect-create")
        return {"id": "flow-id"}

    result = asyncio.run(
        FillHandoffRunner(
            HandoffConfig(
                state_dir=tmp_path,
                herdr_targets=("local:operator",),
                kick_enabled=True,
                prefect_api_url="http://prefect",
                kick_deployments={"crypto": "crypto-deployment"},
                lane_events={"crypto": "lane-a"},
                lane_event=LaneEventConfig(binary=str(lane_emit_binary), timeout_s=3.0),
            ),
            command=command,
            now=lambda: datetime(2026, 9, 3, 1, 0, tzinfo=UTC),
            http_post=post,
        ).run(_Db())
    )
    assert result["fallback"] == ["timeout"]
    assert result["pushed"] == 0
    assert result["kicked"] == 1
    assert call_log.read_text(encoding="utf-8").splitlines() == [
        "emit",
        "herdr-list",
        "prefect-filter",
        "prefect-create",
    ]


def test_lane_event_id_stays_fill_event_key_when_seen_state_is_reset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lane_emit_binary: Path
) -> None:
    _prepare_runner_dependencies(monkeypatch, [_fill(1)])
    _write_empty_handoff_state(tmp_path)
    argv_file = tmp_path / "argv.json"
    monkeypatch.setenv("LANE_EVENT_TEST_ARGV", str(argv_file))
    config = HandoffConfig(
        state_dir=tmp_path,
        lane_events={"crypto": "lane-a"},
        lane_event=LaneEventConfig(binary=str(lane_emit_binary)),
    )
    runner = FillHandoffRunner(config)
    asyncio.run(runner.run(_Db()))
    first_event_id = json.loads(argv_file.read_text(encoding="utf-8"))[7]
    _write_empty_handoff_state(tmp_path)
    asyncio.run(runner.run(_Db()))
    second_event_id = json.loads(argv_file.read_text(encoding="utf-8"))[7]
    assert (first_event_id, second_event_id) == (
        "execution_ledger:1",
        "execution_ledger:1",
    )


def test_dry_run_never_starts_lane_event_emit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lane_emit_binary: Path
) -> None:
    _prepare_runner_dependencies(monkeypatch, [_fill(1)])
    _write_empty_handoff_state(tmp_path)
    argv_file = tmp_path / "argv.json"
    monkeypatch.setenv("LANE_EVENT_TEST_ARGV", str(argv_file))

    asyncio.run(
        FillHandoffRunner(
            HandoffConfig(
                state_dir=tmp_path,
                dry_run=True,
                lane_events={"crypto": "lane-a"},
                lane_event=LaneEventConfig(binary=str(lane_emit_binary)),
            )
        ).run(_Db())
    )
    assert not argv_file.exists()


def test_cli_lane_event_environment_and_validation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    captured: list[HandoffConfig] = []

    class Runner:
        def __init__(self, config: HandoffConfig, **_kwargs: object) -> None:
            captured.append(config)

        async def run(self, _db: object) -> dict[str, Any]:
            return {"ok": True}

    class Session:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(handoff_cli, "FillHandoffRunner", Runner)
    monkeypatch.setattr(handoff_cli, "AsyncSessionLocal", lambda: Session())
    monkeypatch.setenv("FILL_HANDOFF_LANES", '{"crypto":"lane-a","us":"lane-b"}')
    monkeypatch.setenv("FILL_HANDOFF_EMIT_BIN", "fake-panewire")
    monkeypatch.setenv("FILL_HANDOFF_EMIT_HOST", "host-a")
    monkeypatch.setenv("FILL_HANDOFF_EMIT_PANE", "w1:p1")
    monkeypatch.setenv("FILL_HANDOFF_EMIT_INBOX_ROOT", "/tmp/lane-events")
    monkeypatch.setenv("FILL_HANDOFF_EMIT_TIMEOUT_S", "4.5")
    assert asyncio.run(handoff_cli.main_async()) == {"ok": True}
    assert captured[0].lane_events == {"crypto": "lane-a", "us": "lane-b"}
    assert captured[0].lane_event == LaneEventConfig(
        binary="fake-panewire",
        host="host-a",
        pane="w1:p1",
        inbox_root="/tmp/lane-events",
        timeout_s=4.5,
    )

    monkeypatch.setattr(
        handoff_cli,
        "parse_args",
        lambda: SimpleNamespace(since_ledger_id=None, dry_run=False, once=True),
    )
    monkeypatch.setenv("FILL_HANDOFF_LANES", '{"nxt":"lane-a"}')
    assert handoff_cli.main() == 1
    assert "ValueError" in capsys.readouterr().err
    monkeypatch.setenv("FILL_HANDOFF_LANES", "")
    monkeypatch.setenv("FILL_HANDOFF_EMIT_TIMEOUT_S", "1")
    with pytest.raises(ValueError, match="greater than 1"):
        handoff_cli._lane_event_config()


def test_fill_handoff_emitter_namespace_priority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LANE_EVENT_EMIT_BIN", "shared-panewire")
    monkeypatch.setenv("LANE_EVENT_EMIT_HOST", "host-a")
    monkeypatch.setenv("LANE_EVENT_EMIT_PANE", "w1:p1")
    monkeypatch.setenv("LANE_EVENT_EMIT_INBOX_ROOT", "/tmp/shared-inbox")
    monkeypatch.setenv("LANE_EVENT_EMIT_TIMEOUT_S", "4.5")
    assert handoff_cli._lane_event_config() == LaneEventConfig(
        binary="shared-panewire",
        host="host-a",
        pane="w1:p1",
        inbox_root="/tmp/shared-inbox",
        timeout_s=4.5,
    )

    monkeypatch.setenv("FILL_HANDOFF_EMIT_BIN", "fill-panewire")
    monkeypatch.setenv("FILL_HANDOFF_EMIT_HOST", "host-b")
    monkeypatch.setenv("FILL_HANDOFF_EMIT_PANE", "w2:p2")
    monkeypatch.setenv("FILL_HANDOFF_EMIT_INBOX_ROOT", "/tmp/fill-inbox")
    monkeypatch.setenv("FILL_HANDOFF_EMIT_TIMEOUT_S", "5.5")
    assert handoff_cli._lane_event_config() == LaneEventConfig(
        binary="fill-panewire",
        host="host-b",
        pane="w2:p2",
        inbox_root="/tmp/fill-inbox",
        timeout_s=5.5,
    )


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

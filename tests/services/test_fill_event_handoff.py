from __future__ import annotations

import asyncio
import subprocess
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
    assert asyncio.run(FillHandoffRunner(config).run(Db()))["durable"] == 1
    state = (tmp_path / "state.json").read_text()
    assert '"watermark": 2' in state
    assert Context.appended == 1
    (tmp_path / "state.json").unlink()  # simulate a state-file recovery
    assert asyncio.run(FillHandoffRunner(config).run(Db()))["durable"] == 0
    assert Context.appended == 1


def test_no_model_command_tokens_in_runtime_handoff_sources() -> None:
    root = Path(__file__).parents[2]
    source = (root / "app/services/fill_event_handoff/service.py").read_text()
    script = (root / "scripts/fill_event_handoff.py").read_text()
    assert "cl" + "aude" not in source + script
    assert "her" + "mes" not in source + script

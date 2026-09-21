from __future__ import annotations

import asyncio
import inspect
import json
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.routers import execution_ledger_ingest as ingest_module
from app.schemas.execution_ledger import ExecutionLedgerUpsert
from app.schemas.execution_ledger_ingest import ExecutionLedgerFillIngestRequest
from app.services.execution_ledger import fill_ingest as fill_ingest_module
from app.services.execution_ledger.fill_ingest import commit_fill
from app.services.fill_event_handoff import bundle as bundle_module
from app.services.fill_event_handoff.broker_risk import BrokerRiskJudgement
from app.services.fill_event_handoff.bundle import (
    BundleConfig,
    FillHandoffBundleRunner,
    WatchCursor,
)
from scripts.fill_handoff_bundle import _delivery_runtime


def _fill(ledger_id: int, *, order_id: str | None = None) -> dict[str, Any]:
    return {
        "ledger_id": ledger_id,
        "event_key": f"execution_ledger:{ledger_id}",
        "broker": "upbit",
        "account_mode": "live",
        "venue": "upbit",
        "instrument_type": "crypto",
        "market": "crypto",
        "symbol": "BTC",
        "raw_symbol": "KRW-BTC",
        "side": "buy",
        "filled_qty": "0.01",
        "filled_price": str(100 + ledger_id),
        "filled_notional": str(ledger_id + 1),
        "currency": "KRW",
        "broker_order_id": order_id or f"order-{ledger_id}",
        "fill_seq": ledger_id,
        "correlation_id": f"corr-{ledger_id}",
        "source": "websocket",
        "filled_at": "2026-09-18T08:00:00+00:00",
        "trade_day_kst": "2026-09-18",
        "created_at": "2026-09-18T08:00:01+00:00",
    }


def _watch(event_id: int) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "event_uuid": f"00000000-0000-0000-0000-{event_id:012d}",
        "idempotency_key": f"watch-{event_id}:2026-09-18:price",
        "market": "crypto",
        "symbol": "BTC",
        "metric": "price",
        "operator": "above",
        "threshold": "100",
        "threshold_high": None,
        "current_value": "101",
        "outcome": "notified",
        "action_mode": "notify_only",
        "delivered_at": "2026-09-18T08:00:00+00:00",
    }


class _Source:
    def __init__(self, rows: list[dict[str, Any]], id_key: str) -> None:
        self.rows = rows
        self.id_key = id_key

    async def high_watermark(self) -> int:
        return max((int(row[self.id_key]) for row in self.rows), default=0)

    async def list_after(self, after_id: int, *, limit: int) -> list[dict[str, Any]]:
        return [row for row in self.rows if int(row[self.id_key]) > after_id][:limit]


class _FlakyHighWaterSource(_Source):
    def __init__(self, rows: list[dict[str, Any]], id_key: str) -> None:
        super().__init__(rows, id_key)
        self.high_calls = 0

    async def high_watermark(self) -> int:
        self.high_calls += 1
        if self.high_calls == 1:
            raise RuntimeError("synthetic high-water failure")
        return await super().high_watermark()


class _WatchSource:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    async def high_watermark(self) -> WatchCursor:
        if not self.rows:
            return WatchCursor(None, 0)
        row = max(
            self.rows,
            key=lambda value: (str(value["delivered_at"]), int(value["event_id"])),
        )
        return WatchCursor(
            datetime.fromisoformat(str(row["delivered_at"])), int(row["event_id"])
        )

    async def list_after(
        self, cursor: WatchCursor, *, limit: int
    ) -> list[dict[str, Any]]:
        if cursor.delivered_at is None:
            rows = [row for row in self.rows if int(row["event_id"]) > cursor.event_id]
        else:
            marker = (cursor.delivered_at.isoformat(), cursor.event_id)
            rows = [
                row
                for row in self.rows
                if (str(row["delivered_at"]), int(row["event_id"])) > marker
            ]
        return sorted(
            rows, key=lambda value: (str(value["delivered_at"]), value["event_id"])
        )[:limit]


class _Evidence:
    async def list_fills_for_order(self, **_kwargs: object) -> list[dict[str, Any]]:
        return []

    async def list_rungs_for_broker_order(
        self, **_kwargs: object
    ) -> list[dict[str, Any]]:
        return []

    async def list_cancel_proposals_for_target(
        self, **_kwargs: object
    ) -> list[dict[str, Any]]:
        return []


class _Sink:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    async def send(self, lane: str, event_id: str, text: str) -> bool:
        self.calls.append((lane, event_id, text))
        return True


class _Notifier:
    def __init__(self) -> None:
        self.calls: list[BrokerRiskJudgement] = []

    async def push(self, judgement: BrokerRiskJudgement) -> bool:
        self.calls.append(judgement)
        return True


def _write_state(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "state.json").write_text(
        json.dumps(
            {
                "version": 2,
                "watermark": 0,
                "fill_watermark": 0,
                "watch_watermark": 0,
                "watch_delivered_at": None,
                "seen": {},
                "risk_seen": {},
                "cooldowns": {},
            }
        ),
        encoding="utf-8",
    )


@pytest.mark.unit
def test_disabled_gate_does_not_parse_outbound_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FILL_HANDOFF_LANES", "not-json")
    monkeypatch.setenv("FILL_HANDOFF_BATCH_LIMIT", "not-an-int")
    monkeypatch.setenv("FILL_HANDOFF_LOOKBACK_IDS", "not-an-int")
    monkeypatch.setenv("FILL_HANDOFF_SINK_TIMEOUT_S", "not-a-float")

    lanes, _sink, batch_limit, lookback_ids, timeout = _delivery_runtime(False)

    assert lanes == {}
    assert batch_limit == 500
    assert lookback_ids == 256
    assert timeout == 3.0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_disabled_gate_catches_up_without_any_outbound_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FILL_EVENT_HANDOFF_ENABLED", raising=False)
    _write_state(tmp_path)
    sink, notifier = _Sink(), _Notifier()
    runner = FillHandoffBundleRunner(
        BundleConfig(state_dir=tmp_path, lanes={"crypto": "opa-crypto"}),
        sink=sink,
        notifier=notifier,
    )

    result = await runner.run(
        object(),  # type: ignore[arg-type]
        fill_source=_Source([_fill(1), _fill(2), _fill(3)], "ledger_id"),
        watch_source=_WatchSource([_watch(1), _watch(2), _watch(3)]),
        evidence_source=_Evidence(),
    )

    assert result["enabled"] is False
    assert result["fill_watermark"] == 3
    assert result["watch_watermark"] == 3
    assert sink.calls == []
    assert notifier.calls == []
    source = inspect.getsource(bundle_module)
    assert "emit_lane_event" in source
    assert "herdr" not in source.lower(), "bundle path must not inject panes directly"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_enabled_groups_three_fills_and_three_watches_once_and_dedupes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FILL_EVENT_HANDOFF_ENABLED", "true")
    _write_state(tmp_path)
    fills = [_fill(1), _fill(2), _fill(3)]
    watches = [_watch(1), _watch(2), _watch(3)]
    fill_source = _Source(fills, "ledger_id")
    watch_source = _WatchSource(watches)
    sink = _Sink()
    runner = FillHandoffBundleRunner(
        BundleConfig(state_dir=tmp_path, lanes={"crypto": "opa-crypto"}),
        sink=sink,
    )

    first = await runner.run(
        object(),  # type: ignore[arg-type]
        fill_source=fill_source,
        watch_source=watch_source,
        evidence_source=_Evidence(),
    )

    assert first["fill_bundles"] == 1
    assert first["watch_bundles"] == 1
    assert len(sink.calls) == 2
    fill_text = next(text for _, _, text in sink.calls if text.startswith("[fill]"))
    watch_text = next(text for _, _, text in sink.calls if text.startswith("[watch]"))
    assert fill_text.splitlines()[0] == "[fill] crypto 3건"
    assert watch_text.splitlines()[0] == "[watch] crypto 3건"
    fill_keys = [
        line.split("dedupe_key=", 1)[1].split(" ", 1)[0]
        for line in fill_text.splitlines()[1:4]
    ]
    assert len(fill_keys) == len(set(fill_keys)) == 3

    duplicate = dict(fills[0], ledger_id=4, fill_seq=99)
    fill_source.rows.append(duplicate)
    second = await runner.run(
        object(),  # type: ignore[arg-type]
        fill_source=fill_source,
        watch_source=watch_source,
        evidence_source=_Evidence(),
    )
    assert second["fill_bundles"] == 0
    assert second["watch_bundles"] == 0
    assert second["fill_watermark"] == 4
    assert len(sink.calls) == 2, "a duplicate fill must not emit another bundle"


class _RiskDetector:
    async def detect(
        self, fill: dict[str, Any], *, source: object
    ) -> list[BrokerRiskJudgement]:
        del source
        if fill["broker_order_id"] != "risk-order":
            return []
        return [
            BrokerRiskJudgement(
                category="cancel_failed",
                market="crypto",
                symbol="BTC",
                summary="must not push without evidence",
                evidence={},
                dedupe_id="cancel_failed:no-evidence",
            ),
            BrokerRiskJudgement(
                category="cancel_failed",
                market="crypto",
                symbol="BTC",
                summary="cancel dispatch failed",
                evidence={"ledger_id": fill["ledger_id"], "failure_code": "timeout"},
                dedupe_id="cancel_failed:risk-order",
            ),
        ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_only_evidenced_broker_risk_pushes_and_is_deduped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FILL_EVENT_HANDOFF_ENABLED", "true")
    _write_state(tmp_path)
    source = _Source(
        [_fill(1, order_id="risk-order"), _fill(2, order_id="normal-order")],
        "ledger_id",
    )
    notifier = _Notifier()
    runner = FillHandoffBundleRunner(
        BundleConfig(state_dir=tmp_path, lanes={"crypto": "opa-crypto"}),
        sink=_Sink(),
        notifier=notifier,
        detector=_RiskDetector(),  # type: ignore[arg-type]
    )

    result = await runner.run(
        object(),  # type: ignore[arg-type]
        fill_source=source,
        watch_source=_WatchSource([]),
        evidence_source=_Evidence(),
    )
    assert result["risk_pushes"] == 1
    assert len(notifier.calls) == 1
    assert notifier.calls[0].evidence

    source.rows.append(_fill(3, order_id="risk-order"))
    replay = await runner.run(
        object(),  # type: ignore[arg-type]
        fill_source=source,
        watch_source=_WatchSource([]),
        evidence_source=_Evidence(),
    )
    assert replay["risk_pushes"] == 0
    assert len(notifier.calls) == 1


class _ExplodingSink:
    def __init__(self) -> None:
        self.calls = 0

    async def send(self, lane: str, event_id: str, text: str) -> bool:
        del lane, event_id, text
        self.calls += 1
        raise RuntimeError("synthetic sink failure")


class _SleepingSink:
    def __init__(self) -> None:
        self.calls = 0

    async def send(self, lane: str, event_id: str, text: str) -> bool:
        del lane, event_id, text
        self.calls += 1
        await asyncio.sleep(30)
        return True


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("sink", [_ExplodingSink(), _SleepingSink()])
async def test_runner_is_fail_open_for_sink_exception_and_thirty_second_stall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sink: object
) -> None:
    monkeypatch.setenv("FILL_EVENT_HANDOFF_ENABLED", "true")
    _write_state(tmp_path)
    runner = FillHandoffBundleRunner(
        BundleConfig(
            state_dir=tmp_path,
            lanes={"crypto": "opa-crypto"},
            sink_timeout_s=0.01,
        ),
        sink=sink,  # type: ignore[arg-type]
    )
    started = time.monotonic()
    result = await runner.run(
        object(),  # type: ignore[arg-type]
        fill_source=_Source([_fill(1)], "ledger_id"),
        watch_source=_WatchSource([]),
        evidence_source=_Evidence(),
    )
    assert time.monotonic() - started < 0.5
    assert result["fill_watermark"] == 0, "failed delivery remains retryable"


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("writer", ["commit_fill", "http_ingest"])
@pytest.mark.parametrize("sink_kind", ["exception", "stall_30s"])
async def test_ledger_write_result_and_latency_are_independent_of_active_handoff_sink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    writer: str,
    sink_kind: str,
) -> None:
    class Session:
        commits = 0

        async def __aenter__(self) -> Session:
            return self

        async def __aexit__(self, *args: object) -> None:
            del args

        async def commit(self) -> None:
            self.commits += 1

        def begin_nested(self) -> Session:
            return self

    class Repo:
        def __init__(self, db: Session) -> None:
            self.db = db

        async def upsert_fill(self, fill: ExecutionLedgerUpsert) -> tuple[str, int]:
            assert fill.broker_order_id == "order-1"
            return "inserted", 42

    async def no_downstream(**_kwargs: object) -> None:
        return None

    monkeypatch.setenv("FILL_EVENT_HANDOFF_ENABLED", "true")
    monkeypatch.setattr(ingest_module, "ExecutionLedgerRepository", Repo)
    monkeypatch.setattr(ingest_module, "run_post_upsert_downstream", no_downstream)
    _write_state(tmp_path)
    poison_sink = _ExplodingSink() if sink_kind == "exception" else _SleepingSink()
    runner = FillHandoffBundleRunner(
        BundleConfig(
            state_dir=tmp_path,
            lanes={"crypto": "opa-crypto"},
            sink_timeout_s=0.01,
        ),
        sink=poison_sink,
    )
    fill = ExecutionLedgerUpsert(
        broker="upbit",
        account_mode="live",
        venue="upbit",
        instrument_type="crypto",
        symbol="BTC",
        raw_symbol="KRW-BTC",
        side="buy",
        broker_order_id="order-1",
        fill_seq=1,
        filled_qty=Decimal("0.01"),
        filled_price=Decimal("100"),
        filled_at=datetime(2026, 9, 18, 8, tzinfo=UTC),
        currency="KRW",
        source="websocket",
    )

    handoff_task = asyncio.create_task(
        runner.run(
            object(),  # type: ignore[arg-type]
            fill_source=_Source([_fill(1)], "ledger_id"),
            watch_source=_WatchSource([]),
            evidence_source=_Evidence(),
        )
    )
    await asyncio.sleep(0)

    started = time.monotonic()
    if writer == "commit_fill":
        result = await commit_fill(
            fill,
            session_factory=Session,
            repository_cls=Repo,  # type: ignore[arg-type]
        )
    else:
        response = await ingest_module.ingest_execution_ledger_fills(
            ExecutionLedgerFillIngestRequest(
                fills=[fill.model_dump()], source="fillwire"
            ),
            Session(),  # type: ignore[arg-type]
        )
        result = (response.results[0].status, response.results[0].row_id)
    elapsed = time.monotonic() - started

    assert result == ("inserted", 42)
    assert elapsed < 0.1
    await asyncio.wait({handoff_task})
    assert handoff_task.exception() is None
    assert poison_sink.calls == 1
    fill_ingest_source = inspect.getsource(fill_ingest_module)
    assert "fill_event_handoff" not in fill_ingest_source


@pytest.mark.unit
@pytest.mark.asyncio
async def test_explicit_cursors_are_honored_on_first_enabled_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FILL_EVENT_HANDOFF_ENABLED", "true")
    sink = _Sink()
    runner = FillHandoffBundleRunner(
        BundleConfig(
            state_dir=tmp_path,
            lanes={"crypto": "opa-crypto"},
            since_fill_id=0,
            since_watch_id=0,
        ),
        sink=sink,
    )

    result = await runner.run(
        object(),  # type: ignore[arg-type]
        fill_source=_Source([_fill(1)], "ledger_id"),
        watch_source=_WatchSource([_watch(1)]),
        evidence_source=_Evidence(),
    )

    assert result["fill_bundles"] == 1
    assert result["watch_bundles"] == 1
    assert len(sink.calls) == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_new_state_high_water_failure_never_replays_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FILL_EVENT_HANDOFF_ENABLED", "true")
    fills = _FlakyHighWaterSource([_fill(1), _fill(2)], "ledger_id")
    sink = _Sink()
    runner = FillHandoffBundleRunner(
        BundleConfig(
            state_dir=tmp_path,
            lanes={"crypto": "opa-crypto"},
            since_watch_id=0,
        ),
        sink=sink,
    )

    first = await runner.run(
        object(),  # type: ignore[arg-type]
        fill_source=fills,
        watch_source=_WatchSource([]),
        evidence_source=_Evidence(),
    )
    second = await runner.run(
        object(),  # type: ignore[arg-type]
        fill_source=fills,
        watch_source=_WatchSource([]),
        evidence_source=_Evidence(),
    )

    assert first["errors"] == ["fill_high_watermark_failed"]
    assert first["fill_bundles"] == 0
    assert second["fill_watermark"] == 2
    assert second["fill_bundles"] == 0
    assert sink.calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fill_dedupe_is_isolated_by_account_and_venue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FILL_EVENT_HANDOFF_ENABLED", "true")
    _write_state(tmp_path)
    live = _fill(1, order_id="same-order")
    mock = dict(
        live,
        ledger_id=2,
        fill_seq=2,
        account_mode="mock",
        venue="upbit-mock",
    )
    sink = _Sink()
    runner = FillHandoffBundleRunner(
        BundleConfig(state_dir=tmp_path, lanes={"crypto": "opa-crypto"}), sink=sink
    )

    await runner.run(
        object(),  # type: ignore[arg-type]
        fill_source=_Source([live, mock], "ledger_id"),
        watch_source=_WatchSource([]),
        evidence_source=_Evidence(),
    )

    fill_text = next(text for _, _, text in sink.calls if text.startswith("[fill]"))
    assert fill_text.splitlines()[0] == "[fill] crypto 2건"
    assert "live:upbit:" in fill_text
    assert "mock:upbit-mock:" in fill_text


@pytest.mark.unit
@pytest.mark.asyncio
async def test_watch_cursor_does_not_skip_a_lower_id_delivered_later(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FILL_EVENT_HANDOFF_ENABLED", "true")
    _write_state(tmp_path)
    first = dict(_watch(11), delivered_at="2026-09-18T08:00:00+00:00")
    watches = _WatchSource([first])
    sink = _Sink()
    runner = FillHandoffBundleRunner(
        BundleConfig(state_dir=tmp_path, lanes={"crypto": "opa-crypto"}), sink=sink
    )

    await runner.run(
        object(),  # type: ignore[arg-type]
        fill_source=_Source([], "ledger_id"),
        watch_source=watches,
        evidence_source=_Evidence(),
    )
    watches.rows.append(dict(_watch(10), delivered_at="2026-09-18T08:01:00+00:00"))
    second = await runner.run(
        object(),  # type: ignore[arg-type]
        fill_source=_Source([], "ledger_id"),
        watch_source=watches,
        evidence_source=_Evidence(),
    )

    assert second["watch_bundles"] == 1
    assert any("event_id=10" in text for _, _, text in sink.calls)

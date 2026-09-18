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

from app.schemas.execution_ledger import ExecutionLedgerUpsert
from app.services.execution_ledger import fill_ingest as fill_ingest_module
from app.services.execution_ledger.fill_ingest import commit_fill
from app.services.fill_event_handoff import bundle as bundle_module
from app.services.fill_event_handoff.broker_risk import BrokerRiskJudgement
from app.services.fill_event_handoff.bundle import (
    BundleConfig,
    FillHandoffBundleRunner,
)


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


class _Evidence:
    async def list_fills_for_order(self, **_kwargs: object) -> list[dict[str, Any]]:
        return []

    async def list_rungs_for_broker_order(
        self, _broker_order_id: str
    ) -> list[dict[str, Any]]:
        return []

    async def list_cancel_proposals_for_target(
        self, _target_broker_order_id: str
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
                "seen": {},
                "risk_seen": {},
                "cooldowns": {},
            }
        ),
        encoding="utf-8",
    )


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
        watch_source=_Source([_watch(1), _watch(2), _watch(3)], "event_id"),
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
    watch_source = _Source(watches, "event_id")
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
        watch_source=_Source([], "event_id"),
        evidence_source=_Evidence(),
    )
    assert result["risk_pushes"] == 1
    assert len(notifier.calls) == 1
    assert notifier.calls[0].evidence

    await runner.run(
        object(),  # type: ignore[arg-type]
        fill_source=source,
        watch_source=_Source([], "event_id"),
        evidence_source=_Evidence(),
    )
    assert len(notifier.calls) == 1


class _ExplodingSink:
    async def send(self, lane: str, event_id: str, text: str) -> bool:
        del lane, event_id, text
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
        watch_source=_Source([], "event_id"),
        evidence_source=_Evidence(),
    )
    assert time.monotonic() - started < 0.5
    assert result["fill_watermark"] == 0, "failed delivery remains retryable"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_commit_fill_result_and_latency_are_independent_of_handoff_sink() -> None:
    class Session:
        commits = 0

        async def __aenter__(self) -> Session:
            return self

        async def __aexit__(self, *args: object) -> None:
            del args

        async def commit(self) -> None:
            self.commits += 1

    class Repo:
        def __init__(self, db: Session) -> None:
            self.db = db

        async def upsert_fill(self, fill: ExecutionLedgerUpsert) -> tuple[str, int]:
            assert fill.broker_order_id == "order-1"
            return "inserted", 42

    poison_sink = _SleepingSink()
    _unused_runner = FillHandoffBundleRunner(
        BundleConfig(state_dir=Path("unused"), lanes={"crypto": "opa-crypto"}),
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

    started = time.monotonic()
    result = await commit_fill(
        fill,
        session_factory=Session,
        repository_cls=Repo,  # type: ignore[arg-type]
    )
    elapsed = time.monotonic() - started

    assert result == ("inserted", 42)
    assert elapsed < 0.1
    assert poison_sink.calls == 0
    fill_ingest_source = inspect.getsource(fill_ingest_module)
    assert "fill_event_handoff" not in fill_ingest_source

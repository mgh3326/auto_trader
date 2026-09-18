from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from app.services.fill_event_handoff.broker_risk import BrokerRiskJudgement
from app.services.fill_event_handoff.bundle import (
    BundleConfig,
    FillHandoffBundleRunner,
    WatchCursor,
    handoff_enabled,
)
from app.services.fill_event_handoff.service import DEDUP_WINDOW


def _fill(ledger_id: int, **overrides: Any) -> dict[str, Any]:
    row = {
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
        "filled_price": str(1000 + ledger_id),
        "filled_notional": "10000",
        "currency": "KRW",
        "broker_order_id": f"order-{ledger_id}",
        "fill_seq": ledger_id,
        "correlation_id": f"corr-{ledger_id}",
        "source": "websocket",
        "filled_at": "2026-09-18T08:00:00+00:00",
        "trade_day_kst": "2026-09-18",
        "created_at": "2026-09-18T08:00:01+00:00",
    }
    row.update(overrides)
    return row


def _watch(event_id: int, *, delivered_at: str) -> dict[str, Any]:
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
        "delivered_at": delivered_at,
    }


class _FillSource:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.list_calls: list[int] = []

    async def high_watermark(self) -> int:
        return max((int(row["ledger_id"]) for row in self.rows), default=0)

    async def list_after(self, after_id: int, *, limit: int) -> list[dict[str, Any]]:
        self.list_calls.append(after_id)
        return [row for row in self.rows if int(row["ledger_id"]) > after_id][:limit]


class _WatchSource:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.list_calls: list[WatchCursor] = []

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
        self.list_calls.append(cursor)
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


class _SelectiveSink(_Sink):
    def __init__(self, blocked_ids: set[int]) -> None:
        super().__init__()
        self.blocked_ids = blocked_ids

    async def send(self, lane: str, event_id: str, text: str) -> bool:
        self.calls.append((lane, event_id, text))
        return not any(f"ledger_id={row_id}" in text for row_id in self.blocked_ids)


class _Notifier:
    def __init__(self) -> None:
        self.calls: list[BrokerRiskJudgement] = []

    async def push(self, judgement: BrokerRiskJudgement) -> bool:
        self.calls.append(judgement)
        return True


class _RiskDetector:
    async def detect(
        self, fill: dict[str, Any], *, source: object
    ) -> list[BrokerRiskJudgement]:
        del source
        return [
            BrokerRiskJudgement(
                category="cancel_failed",
                market="crypto",
                symbol="BTC",
                summary="cancel failed",
                evidence={"ledger_id": fill["ledger_id"]},
                dedupe_id="cancel_failed:one-root",
            )
        ]


class _Clock:
    def __init__(self, moment: datetime) -> None:
        self.moment = moment

    def __call__(self) -> datetime:
        return self.moment


def _write_state(path: Path, **extra: Any) -> None:
    path.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 3,
        "fill_watermark": 0,
        "watch_watermark": 0,
        "watch_delivered_at": None,
        "fill_initialized": True,
        "watch_initialized": True,
        "seen": {},
        "risk_seen": {},
        "cooldowns": {},
    }
    payload.update(extra)
    (path / "state.json").write_text(json.dumps(payload), encoding="utf-8")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_late_visible_lower_fill_id_is_emitted_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FILL_EVENT_HANDOFF_ENABLED", "true")
    _write_state(tmp_path)
    source = _FillSource([_fill(11)])
    sink = _Sink()
    runner = FillHandoffBundleRunner(
        BundleConfig(state_dir=tmp_path, lanes={"crypto": "opa-crypto"}), sink=sink
    )

    await runner.run(
        object(),  # type: ignore[arg-type]
        fill_source=source,
        watch_source=_WatchSource([]),
        evidence_source=_Evidence(),
    )
    source.rows.insert(0, _fill(10))
    second = await runner.run(
        object(),  # type: ignore[arg-type]
        fill_source=source,
        watch_source=_WatchSource([]),
        evidence_source=_Evidence(),
    )
    third = await runner.run(
        object(),  # type: ignore[arg-type]
        fill_source=source,
        watch_source=_WatchSource([]),
        evidence_source=_Evidence(),
    )

    texts = "\n".join(text for _, _, text in sink.calls)
    assert texts.count("ledger_id=10") == 1
    assert second["fill_bundles"] == 1
    assert third["fill_bundles"] == 0
    assert any(after_id < 11 for after_id in source.list_calls)


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("lookback_ids", [0, 1, 256])
async def test_never_sent_lower_fill_id_is_emitted_once_during_hol(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    lookback_ids: int,
) -> None:
    monkeypatch.setenv("FILL_EVENT_HANDOFF_ENABLED", "true")
    legacy_floor = {"fill": 100, "watch": 100}
    _write_state(tmp_path, seen_floor=legacy_floor)
    source = _FillSource(
        [
            _fill(1000, market="crypto"),
            _fill(1001, market="kr", symbol="005930"),
        ]
    )
    sink = _SelectiveSink({1000})
    runner = FillHandoffBundleRunner(
        BundleConfig(
            state_dir=tmp_path,
            lanes={"crypto": "opa-crypto", "kr": "opa-kr", "us": "opa-us"},
            lookback_ids=lookback_ids,
        ),
        sink=sink,
    )

    first = await runner.run(
        object(),  # type: ignore[arg-type]
        fill_source=source,
        watch_source=_WatchSource([]),
        evidence_source=_Evidence(),
    )
    source.rows.insert(0, _fill(50, market="us", symbol="AAPL"))
    second = await runner.run(
        object(),  # type: ignore[arg-type]
        fill_source=source,
        watch_source=_WatchSource([]),
        evidence_source=_Evidence(),
    )
    third = await runner.run(
        object(),  # type: ignore[arg-type]
        fill_source=source,
        watch_source=_WatchSource([]),
        evidence_source=_Evidence(),
    )

    texts = "\n".join(text for _, _, text in sink.calls)
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert first["fill_watermark"] == 0
    assert texts.count("ledger_id=50 ") == 1
    assert second["fill_bundles"] == 1
    assert third["fill_bundles"] == 0
    assert state["seen_floor"] == legacy_floor


@pytest.mark.unit
@pytest.mark.asyncio
async def test_same_timestamp_lower_watch_id_is_emitted_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FILL_EVENT_HANDOFF_ENABLED", "true")
    _write_state(tmp_path)
    stamp = "2026-09-18T08:00:00+00:00"
    source = _WatchSource([_watch(11, delivered_at=stamp)])
    sink = _Sink()
    runner = FillHandoffBundleRunner(
        BundleConfig(state_dir=tmp_path, lanes={"crypto": "opa-crypto"}), sink=sink
    )

    await runner.run(
        object(),  # type: ignore[arg-type]
        fill_source=_FillSource([]),
        watch_source=source,
        evidence_source=_Evidence(),
    )
    source.rows.insert(0, _watch(10, delivered_at=stamp))
    second = await runner.run(
        object(),  # type: ignore[arg-type]
        fill_source=_FillSource([]),
        watch_source=source,
        evidence_source=_Evidence(),
    )
    third = await runner.run(
        object(),  # type: ignore[arg-type]
        fill_source=_FillSource([]),
        watch_source=source,
        evidence_source=_Evidence(),
    )

    texts = "\n".join(text for _, _, text in sink.calls)
    assert texts.count("event_id=10") == 1
    assert second["watch_bundles"] == 1
    assert third["watch_bundles"] == 0
    assert any(
        cursor.delivered_at is None and cursor.event_id < 11
        for cursor in source.list_calls
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fill_seen_id_survives_24h_while_row_remains_in_lookback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FILL_EVENT_HANDOFF_ENABLED", "true")
    _write_state(tmp_path)
    clock = _Clock(datetime(2026, 9, 18, 8, tzinfo=UTC))
    source = _FillSource([_fill(10), _fill(11)])
    sink = _Sink()
    runner = FillHandoffBundleRunner(
        BundleConfig(state_dir=tmp_path, lanes={"crypto": "opa-crypto"}),
        sink=sink,
        now=clock,
    )

    first = await runner.run(
        object(),  # type: ignore[arg-type]
        fill_source=source,
        watch_source=_WatchSource([]),
        evidence_source=_Evidence(),
    )
    clock.moment += DEDUP_WINDOW + timedelta(seconds=1)
    second = await runner.run(
        object(),  # type: ignore[arg-type]
        fill_source=source,
        watch_source=_WatchSource([]),
        evidence_source=_Evidence(),
    )

    texts = "\n".join(text for _, _, text in sink.calls)
    assert first["fill_bundles"] == 1
    assert second["fill_bundles"] == 0
    assert texts.count("ledger_id=10") == 1
    assert texts.count("ledger_id=11") == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_watch_seen_id_survives_24h_while_row_remains_in_lookback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FILL_EVENT_HANDOFF_ENABLED", "true")
    _write_state(tmp_path)
    clock = _Clock(datetime(2026, 9, 18, 8, tzinfo=UTC))
    source = _WatchSource([_watch(10, delivered_at="2026-09-18T08:00:00+00:00")])
    sink = _Sink()
    runner = FillHandoffBundleRunner(
        BundleConfig(state_dir=tmp_path, lanes={"crypto": "opa-crypto"}),
        sink=sink,
        now=clock,
    )

    await runner.run(
        object(),  # type: ignore[arg-type]
        fill_source=_FillSource([]),
        watch_source=source,
        evidence_source=_Evidence(),
    )
    clock.moment += DEDUP_WINDOW + timedelta(seconds=1)
    second = await runner.run(
        object(),  # type: ignore[arg-type]
        fill_source=_FillSource([]),
        watch_source=source,
        evidence_source=_Evidence(),
    )

    texts = "\n".join(text for _, _, text in sink.calls)
    assert second["watch_bundles"] == 0
    assert texts.count("event_id=10") == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_risk_seen_id_survives_24h_while_fill_remains_in_lookback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FILL_EVENT_HANDOFF_ENABLED", "true")
    _write_state(tmp_path)
    clock = _Clock(datetime(2026, 9, 18, 8, tzinfo=UTC))
    notifier = _Notifier()
    source = _FillSource([_fill(10)])
    runner = FillHandoffBundleRunner(
        BundleConfig(state_dir=tmp_path, lanes={"crypto": "opa-crypto"}),
        sink=_Sink(),
        notifier=notifier,
        detector=_RiskDetector(),  # type: ignore[arg-type]
        now=clock,
    )

    first = await runner.run(
        object(),  # type: ignore[arg-type]
        fill_source=source,
        watch_source=_WatchSource([]),
        evidence_source=_Evidence(),
    )
    clock.moment += DEDUP_WINDOW + timedelta(seconds=1)
    second = await runner.run(
        object(),  # type: ignore[arg-type]
        fill_source=source,
        watch_source=_WatchSource([]),
        evidence_source=_Evidence(),
    )

    assert first["risk_pushes"] == 1
    assert second["risk_pushes"] == 0
    assert len(notifier.calls) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_head_of_line_stall_is_visible_without_reemitting_later_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FILL_EVENT_HANDOFF_ENABLED", "true")
    _write_state(tmp_path)
    clock = _Clock(datetime(2026, 9, 18, 8, tzinfo=UTC))
    source = _FillSource(
        [
            _fill(10, market="crypto"),
            _fill(11, market="kr", symbol="005930"),
        ]
    )
    sink = _SelectiveSink({10})
    runner = FillHandoffBundleRunner(
        BundleConfig(
            state_dir=tmp_path,
            lanes={"crypto": "opa-crypto", "kr": "opa-kr"},
        ),
        sink=sink,
        now=clock,
    )

    results = []
    for pass_number in range(3):
        if pass_number == 1:
            clock.moment += DEDUP_WINDOW + timedelta(seconds=1)
        results.append(
            await runner.run(
                object(),  # type: ignore[arg-type]
                fill_source=source,
                watch_source=_WatchSource([]),
                evidence_source=_Evidence(),
            )
        )

    texts = "\n".join(text for _, _, text in sink.calls)
    assert results[-1]["fill_watermark"] == 0
    assert results[-1]["stalled_passes"] == 3
    assert results[-1]["stalled_head_id"] == 10
    assert results[-1]["stall_notices"] == 1
    assert texts.count("ledger_id=11") == 1
    assert texts.count("ledger_id=10") == 3
    assert texts.count("[fill] 보류 1건 id=10 (3패스 연속 미해결)") == 1

    sink.blocked_ids.clear()
    recovered = await runner.run(
        object(),  # type: ignore[arg-type]
        fill_source=source,
        watch_source=_WatchSource([]),
        evidence_source=_Evidence(),
    )
    assert recovered["fill_watermark"] >= 11


@pytest.mark.unit
@pytest.mark.asyncio
async def test_legacy_float_seen_entries_use_ttl_but_structured_entries_use_id_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FILL_EVENT_HANDOFF_ENABLED", "true")
    moment = datetime(2026, 9, 18, 8, tzinfo=UTC)
    now_ts = moment.timestamp()
    _write_state(
        tmp_path,
        fill_watermark=10,
        seen={
            "legacy-expired": now_ts - DEDUP_WINDOW.total_seconds() - 1,
            "legacy-fresh": now_ts,
            "structured": {"id": 10, "ts": 0},
        },
        risk_seen={
            "risk-expired": now_ts - DEDUP_WINDOW.total_seconds() - 1,
            "risk-structured": {"id": 10, "ts": 0},
        },
    )
    runner = FillHandoffBundleRunner(
        BundleConfig(state_dir=tmp_path, lanes={}), now=_Clock(moment)
    )

    await runner.run(
        object(),  # type: ignore[arg-type]
        fill_source=_FillSource([]),
        watch_source=_WatchSource([]),
        evidence_source=_Evidence(),
    )
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))

    assert "legacy-expired" not in state["seen"]
    assert isinstance(state["seen"]["legacy-fresh"], float)
    assert state["seen"]["structured"] == {"id": 10, "ts": 0}
    assert "risk-expired" not in state["risk_seen"]
    assert state["risk_seen"]["risk-structured"] == {"id": 10, "ts": 0}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_inversion_wider_than_lookback_is_a_known_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 알려진 한계: 정수 ID 역전 폭이 lookback보다 크면 낮은 ID는 누락된다.
    monkeypatch.setenv("FILL_EVENT_HANDOFF_ENABLED", "true")
    _write_state(tmp_path)
    source = _FillSource([_fill(row_id) for row_id in range(257, 557)])
    sink = _Sink()
    runner = FillHandoffBundleRunner(
        BundleConfig(state_dir=tmp_path, lanes={"crypto": "opa-crypto"}), sink=sink
    )

    first = await runner.run(
        object(),  # type: ignore[arg-type]
        fill_source=source,
        watch_source=_WatchSource([]),
        evidence_source=_Evidence(),
    )
    source.rows.insert(0, _fill(1))
    second = await runner.run(
        object(),  # type: ignore[arg-type]
        fill_source=source,
        watch_source=_WatchSource([]),
        evidence_source=_Evidence(),
    )

    texts = "\n".join(text for _, _, text in sink.calls)
    assert first["fill_watermark"] == 556
    assert second["fill_bundles"] == 0
    assert texts.count("ledger_id=1") == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_bundle_rejects_legacy_runner_state_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FILL_EVENT_HANDOFF_ENABLED", "true")
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "state.json").write_text(
        json.dumps({"version": 1, "watermark": 7, "seen": {}, "cooldowns": {}}),
        encoding="utf-8",
    )
    runner = FillHandoffBundleRunner(BundleConfig(state_dir=tmp_path))

    with pytest.raises(
        RuntimeError,
        match=r"/var/lib/fill-event-handoff.*?/var/lib/fill-handoff-bundle",
    ):
        await runner.run(
            object(),  # type: ignore[arg-type]
            fill_source=_FillSource([]),
            watch_source=_WatchSource([]),
            evidence_source=_Evidence(),
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_seen_pressure_is_reported_without_eviction_or_bundle_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FILL_EVENT_HANDOFF_ENABLED", "true")
    _write_state(tmp_path)
    source = _FillSource(
        [_fill(1, market="crypto")]
        + [_fill(row_id, market="kr") for row_id in range(2, 6)]
    )
    sink = _SelectiveSink({1})
    runner = FillHandoffBundleRunner(
        BundleConfig(
            state_dir=tmp_path,
            lanes={"crypto": "opa-crypto", "kr": "opa-kr"},
            lookback_ids=1,
        ),
        sink=sink,
    )

    first = await runner.run(
        object(),  # type: ignore[arg-type]
        fill_source=source,
        watch_source=_WatchSource([]),
        evidence_source=_Evidence(),
    )
    first_state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))

    assert first["seen_size"] == {"fill": 4, "watch": 0}
    assert first["errors"] == []
    assert len(first_state["seen"]) == 4
    assert "seen_floor" not in first_state

    source.rows.append(_fill(6, market="kr"))
    second = await runner.run(
        object(),  # type: ignore[arg-type]
        fill_source=source,
        watch_source=_WatchSource([]),
        evidence_source=_Evidence(),
    )
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    warning = "[fill] 경고 seen_size=5 (lookback_ids*4=4 초과; 억제 기록 유지)"
    texts = [text for _, _, text in sink.calls]
    regular_text = next(text for text in texts if "ledger_id=6 " in text)

    assert second["seen_size"] == {"fill": 5, "watch": 0}
    assert second["errors"] == ["fill_seen_size_exceeded"]
    assert texts.count(warning) == 1
    assert regular_text.startswith("[fill] kr 1건\n")
    assert regular_text.endswith(
        "지난 창 이후 체결을 검토하고 조정·추가 여부를 판단하라."
    )
    assert len(state["seen"]) == 5
    assert "seen_floor" not in state


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, False),
        ("", False),
        ("0", False),
        ("false", False),
        ("FALSE", False),
        ("off", False),
        ("1", True),
        ("true", True),
        ("TRUE", True),
        ("yes", True),
        ("on", True),
    ],
)
def test_handoff_enabled_token_matrix(
    monkeypatch: pytest.MonkeyPatch, value: str | None, expected: bool
) -> None:
    monkeypatch.delenv("FILL_EVENT_HANDOFF_ENABLED", raising=False)
    if value is not None:
        monkeypatch.setenv("FILL_EVENT_HANDOFF_ENABLED", value)
    assert handoff_enabled() is expected

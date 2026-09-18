from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from app.services.fill_event_handoff.bundle import (
    BundleConfig,
    FillHandoffBundleRunner,
    WatchCursor,
    handoff_enabled,
)


def _fill(ledger_id: int) -> dict[str, Any]:
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


def _write_state(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "state.json").write_text(
        json.dumps(
            {
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
        ),
        encoding="utf-8",
    )


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

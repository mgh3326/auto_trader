"""#931 shadow mode for the bundled fill/watch handoff: evaluate everything,
send nothing, and keep shadow state away from the production state_dir.

Every test is fixture-only — no DB, no Prefect, no panewire, no Telegram.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

import app.core.db as app_db
import scripts.fill_handoff_bundle as bundle_script
from app.services.fill_event_handoff import bundle as bundle_module
from app.services.fill_event_handoff import shadow as shadow_module
from app.services.fill_event_handoff.broker_risk import BrokerRiskJudgement
from app.services.fill_event_handoff.bundle import (
    BUNDLE_SHADOW_STATE_DIR,
    BundleConfig,
    FillHandoffBundleRunner,
    NullLaneEventSink,
    NullRiskPushNotifier,
    WatchCursor,
    shadow_enabled,
)
from app.services.fill_event_handoff.shadow import ShadowKickConfig
from app.services.fill_event_handoff.watch_kick import WatchKickCursor

# Outside every crypto rep window (KST 10:00); crypto is always tradable.
NOW = datetime(2026, 9, 3, 1, 0, tzinfo=UTC)


def _fill(ledger_id: int, **overrides: Any) -> dict[str, Any]:
    fill: dict[str, Any] = {
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
        "filled_notional": "250000",
        "currency": "KRW",
        "broker_order_id": f"order-{ledger_id}",
        "fill_seq": ledger_id,
        "correlation_id": f"corr-{ledger_id}",
        "source": "websocket",
        "filled_at": "2026-09-03T00:00:00+00:00",
        "trade_day_kst": "2026-09-03",
        "created_at": "2026-09-03T00:00:01+00:00",
    }
    fill.update(overrides)
    return fill


def _watch(event_id: int, **overrides: Any) -> dict[str, Any]:
    event: dict[str, Any] = {
        "event_id": event_id,
        "event_uuid": f"00000000-0000-0000-0000-{event_id:012d}",
        "idempotency_key": f"watch-{event_id}:2026-09-03:price",
        "market": "crypto",
        "symbol": "BTC",
        "metric": "price",
        "operator": "above",
        "threshold": "100",
        "threshold_high": None,
        "current_value": "101",
        "outcome": "notified",
        "action_mode": "notify_only",
        "delivered_at": "2026-09-03T00:55:00+00:00",
    }
    event.update(overrides)
    return event


def _kick_watch(event_id: int, **overrides: Any) -> dict[str, Any]:
    """A delivered watch row as the kick source projects it (max_action join)."""
    event = _watch(event_id)
    event.update(
        {
            "alert_id": event_id,
            "intent": "buy_review",
            "action_mode": "approval_required",
            "kst_date": "2026-09-03",
            "correlation_id": f"corr-{event_id}",
            "alert_max_action": {"side": "buy", "quantity": "0.01"},
        }
    )
    event.update(overrides)
    return event


class _Source:
    def __init__(self, rows: list[dict[str, Any]], id_key: str) -> None:
        self.rows = rows
        self.id_key = id_key

    async def high_watermark(self) -> int:
        return max((int(row[self.id_key]) for row in self.rows), default=0)

    async def list_after(self, after_id: int, *, limit: int) -> list[dict[str, Any]]:
        return [row for row in self.rows if int(row[self.id_key]) > after_id][:limit]


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


class _KickSource:
    """Delivered watch events with the alert max_action projection."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    async def high_watermark(self) -> WatchKickCursor:
        if not self.rows:
            return WatchKickCursor(None, 0)
        row = max(
            self.rows,
            key=lambda value: (str(value["delivered_at"]), int(value["event_id"])),
        )
        return WatchKickCursor(
            datetime.fromisoformat(str(row["delivered_at"])), int(row["event_id"])
        )

    async def list_after(
        self, cursor: WatchKickCursor, *, limit: int
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


class _Positions:
    def __init__(self, qty_before: str = "0", rows_before: int = 1) -> None:
        self.qty_before = qty_before
        self.rows_before = rows_before
        self.calls: list[dict[str, Any]] = []

    async def position_before_fill(self, **kwargs: Any) -> tuple[Decimal, int]:
        self.calls.append(kwargs)
        return Decimal(self.qty_before), self.rows_before


class _Sink:
    """A recording transport — under shadow it must never be called."""

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
                summary="no evidence — never push-eligible",
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


def _write_shadow_state(path: Path, **extra: Any) -> None:
    """An armed shadow state file (as a prior shadow run would leave it)."""
    path.mkdir(parents=True, exist_ok=True)
    state: dict[str, Any] = {
        "version": 4,
        "shadow": True,
        "watermark": 0,
        "fill_watermark": 0,
        "watch_watermark": 0,
        "watch_delivered_at": None,
        "watch_kick_watermark": 0,
        "watch_kick_delivered_at": None,
        "seen": {},
        "risk_seen": {},
        "cooldowns": {},
    }
    state.update(extra)
    (path / "state.json").write_text(json.dumps(state), encoding="utf-8")


def _kick_knobs(**overrides: Any) -> ShadowKickConfig:
    values: dict[str, Any] = {
        "kick_enabled": True,
        "prefect_api_url": "http://prefect.invalid",
        "kick_deployments": {"crypto": "crypto-deployment"},
        "kick_cooldown_seconds": 0,
        "kick_daily_cap": 2,
    }
    values.update(overrides)
    return ShadowKickConfig(**values)


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "FILL_EVENT_HANDOFF_ENABLED",
        "FILL_EVENT_HANDOFF_SHADOW",
        "FILL_HANDOFF_LANES",
        "FILL_HANDOFF_BUNDLE_STATE_DIR",
        "FILL_HANDOFF_BUNDLE_SHADOW_STATE_DIR",
        "FILL_HANDOFF_STATE_DIR",
        "FILL_HANDOFF_KICK_ENABLED",
        "FILL_HANDOFF_KICK_DEPLOYMENTS",
        "FILL_HANDOFF_KICK_DAILY_CAP",
        "FILL_HANDOFF_KICK_COOLDOWN_S",
        "FILL_HANDOFF_KICK_PARKING_SYMBOLS",
        "FILL_HANDOFF_KICK_SMALL_BUY_NOTIONAL",
        "FILL_HANDOFF_KICK_MIN_POSITION_FRACTION",
        "PREFECT_API_URL",
    ):
        monkeypatch.delenv(name, raising=False)


async def _run_shadow(
    tmp_path: Path,
    *,
    fills: list[dict[str, Any]] | None = None,
    watches: list[dict[str, Any]] | None = None,
    kick_watches: list[dict[str, Any]] | None = None,
    lanes: dict[str, str] | None = None,
    knobs: ShadowKickConfig | None = None,
    positions: _Positions | None = None,
    sink: _Sink | None = None,
    notifier: _Notifier | None = None,
    detector: Any | None = None,
    kick_source: Any | None = None,
) -> tuple[FillHandoffBundleRunner, dict[str, Any], _Positions]:
    runner = FillHandoffBundleRunner(
        BundleConfig(
            state_dir=tmp_path,
            lanes={"crypto": "opa-crypto"} if lanes is None else lanes,
            shadow=True,
            shadow_kick=knobs or ShadowKickConfig(),
        ),
        sink=sink,
        notifier=notifier,
        detector=detector,
        now=lambda: NOW,
    )
    position_source = positions or _Positions()
    result = await runner.run(
        object(),  # type: ignore[arg-type]
        fill_source=_Source(fills or [], "ledger_id"),
        watch_source=_WatchSource(watches or []),
        evidence_source=_Evidence(),
        position_source=position_source,
        watch_kick_source=kick_source or _KickSource(kick_watches or []),
    )
    return runner, result, position_source


@pytest.mark.unit
@pytest.mark.asyncio
async def test_shadow_evaluates_everything_and_sends_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_env(monkeypatch)  # FILL_EVENT_HANDOFF_ENABLED stays unset
    _write_shadow_state(tmp_path)
    sink, notifier = _Sink(), _Notifier()
    runner, result, _ = await _run_shadow(
        tmp_path,
        fills=[_fill(1), _fill(2)],
        watches=[_watch(1)],
        sink=sink,
        notifier=notifier,
    )

    # The transports were forcibly replaced and never called.
    assert isinstance(runner.sink, NullLaneEventSink)
    assert isinstance(runner.notifier, NullRiskPushNotifier)
    assert sink.calls == []
    assert notifier.calls == []
    # ...but the full evaluation path ran and recorded counts.
    assert result["enabled"] is False
    shadow = result["shadow"]
    assert shadow["fills_read"] == 2
    assert shadow["watches_read"] == 1
    assert shadow["bundles_formed"] == {"opa-crypto": 2}
    assert shadow["lane_sends"] == {"opa-crypto": 2}
    assert shadow["duplicates_suppressed"] == 0
    assert result["fill_bundles"] == 1
    assert result["watch_bundles"] == 1
    assert result["fill_watermark"] == 2
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["shadow"] is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_shadow_wins_over_enabled_and_still_sends_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("FILL_EVENT_HANDOFF_ENABLED", "true")
    _write_shadow_state(tmp_path)
    sink, notifier = _Sink(), _Notifier()
    runner, result, _ = await _run_shadow(
        tmp_path,
        fills=[_fill(1), _fill(2, broker_order_id="risk-order")],
        watches=[_watch(1)],
        sink=sink,
        notifier=notifier,
        detector=_RiskDetector(),
        knobs=_kick_knobs(),
    )

    assert result["enabled"] is True
    assert sink.calls == []
    assert notifier.calls == []
    assert isinstance(runner.sink, NullLaneEventSink)
    assert isinstance(runner.notifier, NullRiskPushNotifier)
    shadow = result["shadow"]
    assert shadow["risk_judgements"] == 2
    assert shadow["risk_would_push"] == 1
    assert result["risk_pushes"] == 1  # the would-push count


@pytest.mark.unit
@pytest.mark.asyncio
async def test_shadow_never_constructs_real_transports_in_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("FILL_EVENT_HANDOFF_SHADOW", "1")
    monkeypatch.setenv("FILL_EVENT_HANDOFF_ENABLED", "true")
    monkeypatch.setenv("FILL_HANDOFF_LANES", '{"crypto":"opa-crypto"}')

    class _ExplodingTransport:
        constructed = 0

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            type(self).constructed += 1
            raise AssertionError("real transport constructed under shadow")

    monkeypatch.setattr(bundle_script, "TradeNotifierRiskPush", _ExplodingTransport)
    monkeypatch.setattr(bundle_script, "PanewireLaneEventSink", _ExplodingTransport)

    captured: dict[str, Any] = {}

    class _Runner:
        def __init__(self, config: BundleConfig, **kwargs: Any) -> None:
            captured["config"] = config
            captured["sink"] = kwargs.get("sink")
            captured["notifier"] = kwargs.get("notifier")

        async def run(self, _db: object) -> dict[str, Any]:
            return {"enabled": True, "errors": []}

    class _Session:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(bundle_script, "FillHandoffBundleRunner", _Runner)
    monkeypatch.setattr(app_db, "AsyncSessionLocal", lambda: _Session())

    result = await bundle_script.main_async(state_dir=tmp_path)

    assert _ExplodingTransport.constructed == 0
    config = captured["config"]
    assert config.shadow is True
    assert config.lanes == {"crypto": "opa-crypto"}
    assert isinstance(captured["sink"], NullLaneEventSink)
    assert captured["notifier"] is None
    assert result["transport"] == "shadow"


@pytest.mark.unit
def test_shadow_delivery_runtime_forces_null_sink(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("FILL_EVENT_HANDOFF_ENABLED", "true")
    monkeypatch.setenv("FILL_HANDOFF_LANES", '{"crypto":"opa-crypto"}')
    lanes, sink, *_ = bundle_script._delivery_runtime(True, shadow=True)
    assert lanes == {"crypto": "opa-crypto"}
    assert isinstance(sink, NullLaneEventSink)
    assert not isinstance(sink, bundle_script.PanewireLaneEventSink)


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_dir",
    [
        Path("/var/lib/fill-handoff-bundle"),
        Path("/var/lib/fill-event-handoff"),
    ],
)
async def test_shadow_refuses_production_state_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_dir: Path
) -> None:
    _clear_env(monkeypatch)
    runner = FillHandoffBundleRunner(BundleConfig(state_dir=bad_dir, shadow=True))
    with pytest.raises(RuntimeError, match="production state_dir"):
        await runner.run(
            object(),  # type: ignore[arg-type]
            fill_source=_Source([], "ledger_id"),
            watch_source=_WatchSource([]),
            evidence_source=_Evidence(),
            position_source=_Positions(),
            watch_kick_source=_KickSource([]),
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_shadow_refuses_env_configured_production_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("FILL_HANDOFF_BUNDLE_STATE_DIR", str(tmp_path))
    runner = FillHandoffBundleRunner(BundleConfig(state_dir=tmp_path, shadow=True))
    with pytest.raises(RuntimeError, match="production state_dir"):
        await runner.run(
            object(),  # type: ignore[arg-type]
            fill_source=_Source([], "ledger_id"),
            watch_source=_WatchSource([]),
            evidence_source=_Evidence(),
            position_source=_Positions(),
            watch_kick_source=_KickSource([]),
        )
    assert not (tmp_path / "state.json").exists()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_shadow_refuses_state_written_by_production_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "state.json").write_text(
        json.dumps(
            {
                "version": 4,
                "watermark": 0,
                "fill_watermark": 7,
                "watch_watermark": 0,
                "watch_delivered_at": None,
                "seen": {},
                "risk_seen": {},
                "cooldowns": {},
            }
        ),
        encoding="utf-8",
    )
    runner = FillHandoffBundleRunner(BundleConfig(state_dir=tmp_path, shadow=True))
    with pytest.raises(RuntimeError, match="non-shadow"):
        await runner.run(
            object(),  # type: ignore[arg-type]
            fill_source=_Source([], "ledger_id"),
            watch_source=_WatchSource([]),
            evidence_source=_Evidence(),
            position_source=_Positions(),
            watch_kick_source=_KickSource([]),
        )
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["fill_watermark"] == 7, "production watermark must be untouched"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_production_run_refuses_shadow_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    _write_shadow_state(tmp_path, fill_watermark=9)
    runner = FillHandoffBundleRunner(BundleConfig(state_dir=tmp_path))
    with pytest.raises(RuntimeError, match="shadow"):
        await runner.run(
            object(),  # type: ignore[arg-type]
            fill_source=_Source([], "ledger_id"),
            watch_source=_WatchSource([]),
            evidence_source=_Evidence(),
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_shadow_kick_classes_and_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    _write_shadow_state(tmp_path)
    # Three buy fills on a proven-flat book → buy_new_position ×3; cap is 2.
    runner, result, positions = await _run_shadow(
        tmp_path,
        fills=[_fill(1), _fill(2), _fill(3), _fill(4, symbol="SGOV")],
        knobs=_kick_knobs(),
    )
    shadow = result["shadow"]
    assert shadow["kick"]["kick"] == 2
    assert shadow["kick"]["capped"] == 1
    assert shadow["kick"]["queue_only"] == 1
    assert shadow["kick"]["by_reason"]["buy_new_position"] == 2
    assert shadow["kick"]["by_reason"]["daily_cap"] == 1
    assert shadow["kick"]["by_reason"]["parking_etf"] == 1
    assert len(positions.calls) == 3, "parking_etf skips the position read"
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["kick_days"]["crypto"] == {"date": "20260903", "count": 2}
    assert state["cooldowns"]["crypto"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_shadow_kick_unconfigured_is_queue_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    _write_shadow_state(tmp_path)
    _, result, _ = await _run_shadow(tmp_path, fills=[_fill(1)])
    shadow = result["shadow"]
    assert shadow["kick"]["queue_only"] == 1
    assert shadow["kick"]["by_reason"]["kick_not_configured"] == 1
    state = json.loads((tmp_path / "state.json").read_text())
    assert "kick_days" not in state or state["kick_days"] == {}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_shadow_rep_window_is_queue_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    _write_shadow_state(tmp_path)
    rep_now = datetime(2026, 9, 2, 23, 25, tzinfo=UTC)  # KST 08:25, in rep window
    runner = FillHandoffBundleRunner(
        BundleConfig(
            state_dir=tmp_path,
            lanes={"crypto": "opa-crypto"},
            shadow=True,
            shadow_kick=_kick_knobs(),
        ),
        now=lambda: rep_now,
    )
    result = await runner.run(
        object(),  # type: ignore[arg-type]
        fill_source=_Source([_fill(1)], "ledger_id"),
        watch_source=_WatchSource([]),
        evidence_source=_Evidence(),
        position_source=_Positions(),
        watch_kick_source=_KickSource([]),
    )
    shadow = result["shadow"]
    assert shadow["kick"]["queue_only"] == 1
    assert shadow["kick"]["by_reason"]["rep_window"] == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_shadow_counts_do_not_double_count_across_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    _write_shadow_state(tmp_path)
    fills = _Source([_fill(1), _fill(2, broker_order_id="risk-order")], "ledger_id")
    runner = FillHandoffBundleRunner(
        BundleConfig(
            state_dir=tmp_path,
            lanes={"crypto": "opa-crypto"},
            shadow=True,
            shadow_kick=_kick_knobs(kick_daily_cap=10),
        ),
        detector=_RiskDetector(),
        now=lambda: NOW,
    )
    positions = _Positions()

    async def run() -> dict[str, Any]:
        return await runner.run(
            object(),  # type: ignore[arg-type]
            fill_source=fills,
            watch_source=_WatchSource([_watch(1)]),
            evidence_source=_Evidence(),
            position_source=positions,
            watch_kick_source=_KickSource([]),
        )

    first = (await run())["shadow"]
    assert first["fills_read"] == 2
    assert first["kick"]["kick"] == 2
    assert first["bundles_formed"] == {"opa-crypto": 2}
    assert first["risk_would_push"] == 1

    second = (await run())["shadow"]
    # Same rows re-read through the lookback window, but nothing re-counts.
    assert second["fills_read"] == 2
    assert second["watches_read"] == 1
    assert second["duplicates_suppressed"] == 3  # 2 fills + 1 watch
    assert second["bundles_formed"] == {}
    assert second["kick"] == {
        "by_reason": {},
        "capped": 0,
        "kick": 0,
        "queue_only": 0,
    }
    assert second["risk_judgements"] == 2
    assert second["risk_deduped"] == 1
    assert second["risk_would_push"] == 0

    # A genuinely new fill is a fresh candidate — not a duplicate.
    fills.rows.append(_fill(3))
    third = (await run())["shadow"]
    assert third["fills_read"] == 3
    assert third["duplicates_suppressed"] == 3
    assert third["kick"]["kick"] == 1
    assert third["bundles_formed"] == {"opa-crypto": 1}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_shadow_watch_kick_cursor_seed_then_evaluate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    # Armed bundle state without the kick cursor → first run seeds it.
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "state.json").write_text(
        json.dumps(
            {
                "version": 4,
                "shadow": True,
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
    kick_rows = [_kick_watch(7)]
    runner = FillHandoffBundleRunner(
        BundleConfig(
            state_dir=tmp_path,
            lanes={"crypto": "opa-crypto"},
            shadow=True,
            shadow_kick=_kick_knobs(),
        ),
        now=lambda: NOW,
    )

    async def run() -> dict[str, Any]:
        return await runner.run(
            object(),  # type: ignore[arg-type]
            fill_source=_Source([], "ledger_id"),
            watch_source=_WatchSource([]),
            evidence_source=_Evidence(),
            position_source=_Positions(),
            watch_kick_source=_KickSource(kick_rows),
        )

    seeded = (await run())["shadow"]
    assert seeded["watch_kick"]["kick"] == 0
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["watch_kick_watermark"] == 7

    kick_rows.append(_kick_watch(8))
    evaluated = (await run())["shadow"]
    assert evaluated["watch_kick_rows"] == 1
    assert evaluated["watch_kick"]["kick"] == 1
    assert evaluated["watch_kick"]["by_reason"]["buy_review"] == 1
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["watch_kick_watermark"] == 8
    assert state["seen"]["watchkick:8"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_shadow_watch_ladder_groups_one_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    _write_shadow_state(tmp_path)
    _, result, _ = await _run_shadow(
        tmp_path,
        kick_watches=[
            _kick_watch(1),
            _kick_watch(2),  # same market+symbol rung → ladder_grouped
            _kick_watch(3, symbol="ETH"),
            _kick_watch(4, action_mode="notify_only"),
        ],
        knobs=_kick_knobs(kick_daily_cap=10),
    )
    shadow = result["shadow"]
    assert shadow["watch_kick"]["kick"] == 2
    assert shadow["watch_kick"]["queue_only"] == 2
    assert shadow["watch_kick"]["by_reason"]["ladder_grouped"] == 1
    assert shadow["watch_kick"]["by_reason"]["action_mode_notify_only"] == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_shadow_lane_missing_resolves_but_counts_undeliverable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    _write_shadow_state(tmp_path)
    _, result, _ = await _run_shadow(tmp_path, fills=[_fill(1), _fill(2)], lanes={})
    shadow = result["shadow"]
    assert shadow["bundles_formed"] == {}
    assert shadow["bundles_undeliverable"] == {"crypto": 1}
    assert result["errors"] == ["fill_lane_missing:crypto"]
    # Shadow bookkeeping resolves evaluated rows so the run does not wedge.
    assert result["fill_watermark"] == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_shadow_emits_one_structured_log_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _clear_env(monkeypatch)
    _write_shadow_state(tmp_path)
    with caplog.at_level(logging.INFO, logger=bundle_module.__name__):
        _, result, _ = await _run_shadow(tmp_path, fills=[_fill(1)])
    records = [
        record
        for record in caplog.records
        if "fill_handoff_bundle_shadow" in record.getMessage()
    ]
    assert len(records) == 1
    payload = records[0].getMessage().split("fill_handoff_bundle_shadow ", 1)[1]
    parsed = json.loads(payload)
    assert parsed["fills_read"] == 1
    assert parsed["kick"]["queue_only"] == 1
    assert parsed["errors"] == result["errors"] == []


@pytest.mark.unit
def test_shadow_enabled_reader() -> None:
    os.environ.pop("FILL_EVENT_HANDOFF_SHADOW", None)
    assert shadow_enabled() is False
    try:
        os.environ["FILL_EVENT_HANDOFF_SHADOW"] = "true"
        assert shadow_enabled() is True
        os.environ["FILL_EVENT_HANDOFF_SHADOW"] = "0"
        assert shadow_enabled() is False
    finally:
        os.environ.pop("FILL_EVENT_HANDOFF_SHADOW", None)


@pytest.mark.unit
def test_shadow_default_state_dir_is_distinct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("FILL_EVENT_HANDOFF_SHADOW", "1")
    assert bundle_script._default_state_dir(shadow=True) == Path(
        BUNDLE_SHADOW_STATE_DIR
    )
    assert bundle_script._default_state_dir(shadow=True) != Path(
        "/var/lib/fill-handoff-bundle"
    )
    monkeypatch.setenv("FILL_HANDOFF_BUNDLE_SHADOW_STATE_DIR", "/tmp/shadow-x")
    assert bundle_script._default_state_dir(shadow=True) == Path("/tmp/shadow-x")


@pytest.mark.unit
def test_shadow_kick_env_parsing_matches_legacy_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_env(monkeypatch)
    knobs = shadow_module.shadow_kick_config_from_env()
    assert knobs.kick_enabled is False
    assert knobs.kick_daily_cap == 2
    monkeypatch.setenv("FILL_HANDOFF_KICK_ENABLED", "true")
    monkeypatch.setenv(
        "FILL_HANDOFF_KICK_DEPLOYMENTS", '{"crypto":"dep-a","kr":"dep-kr"}'
    )
    monkeypatch.setenv("PREFECT_API_URL", "http://prefect.invalid")
    monkeypatch.setenv("FILL_HANDOFF_KICK_DAILY_CAP", "5")
    knobs = shadow_module.shadow_kick_config_from_env()
    assert knobs.kick_enabled is True
    assert knobs.kick_deployments == {"crypto": "dep-a", "kr": "dep-kr"}
    assert knobs.prefect_api_url == "http://prefect.invalid"
    assert knobs.kick_daily_cap == 5


@pytest.mark.unit
def test_delivery_runtime_shadow_parses_lanes_like_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("FILL_HANDOFF_LANES", "not-json")
    with pytest.raises(ValueError):
        bundle_script._delivery_runtime(False, shadow=True)

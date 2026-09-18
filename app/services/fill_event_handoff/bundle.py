"""Scheduleless pull runner for bundled fill/watch lane handoff (task416).

The execution ledger and delivered watch-event table are the durable sources.
This module is intentionally absent from ``fill_ingest.commit_fill``: polling
keeps lane delivery latency and failure outside the authoritative ledger write
call stack.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.investment_reports import InvestmentWatchEvent
from app.services.execution_ledger.fill_event_sanitizer import sanitize_fill
from app.services.execution_ledger.repository import ExecutionLedgerRepository
from app.services.lane_events import LaneEventConfig, emit_lane_event

from .broker_risk import (
    RISK_CATEGORIES,
    BrokerRiskDetector,
    BrokerRiskEvidenceSource,
    BrokerRiskJudgement,
    RiskPushNotifier,
    SqlAlchemyEvidenceSource,
)
from .service import DEDUP_WINDOW, dedupe_key
from .state import HandoffState

FILL_EVENT_HANDOFF_ENABLED = "FILL_EVENT_HANDOFF_ENABLED"
# Accepted true tokens are intentionally explicit and test-pinned. Everything
# else (including unset, empty, 0, false, and off) is disabled.
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_STATE_VERSION = 3

# PostgreSQL sequence ids can be allocated before a transaction becomes
# visible. Re-reading the preceding 256 ids covers up to 256 concurrent
# allocation/commit inversions while remaining below the 500-row new-event
# batch. Lookback and new rows are queried separately, so neither starves the
# other. Deployments may tune this down to zero or up to the repository cap.
DEFAULT_LOOKBACK_IDS = 256


def handoff_enabled() -> bool:
    """Read the master gate at call time; unset and malformed values are off."""
    return (
        os.environ.get(FILL_EVENT_HANDOFF_ENABLED, "").strip().lower() in _TRUE_VALUES
    )


class LaneEventSink(Protocol):
    """Transport seam.  Implementations must report durable acceptance."""

    async def send(self, lane: str, event_id: str, text: str) -> bool: ...


class NullLaneEventSink:
    """Safe default: no transport and no acknowledgement."""

    async def send(self, lane: str, event_id: str, text: str) -> bool:
        del lane, event_id, text
        return False


class PanewireLaneEventSink:
    """Local-inbox panewire adapter; direct hub/operator-token transport is absent."""

    def __init__(self, config: LaneEventConfig) -> None:
        self._config = config

    async def send(self, lane: str, event_id: str, text: str) -> bool:
        result = await asyncio.to_thread(
            emit_lane_event,
            lane,
            event_id,
            text,
            config=self._config,
        )
        return result.outcome in {"emitted", "duplicate"}


class FillEventSource(Protocol):
    async def high_watermark(self) -> int: ...

    async def list_after(
        self, after_id: int, *, limit: int
    ) -> Sequence[Mapping[str, Any]]: ...


class WatchAlertSource(Protocol):
    async def high_watermark(self) -> WatchCursor: ...

    async def list_after(
        self, cursor: WatchCursor, *, limit: int
    ) -> Sequence[Mapping[str, Any]]: ...


@dataclass(frozen=True)
class WatchCursor:
    """Delivery-order cursor; ``delivered_at=None`` supports legacy id seeds."""

    delivered_at: datetime | None
    event_id: int


class DbFillEventSource:
    """Read websocket-origin fills through the existing ledger repository."""

    def __init__(self, db: AsyncSession) -> None:
        self._repository = ExecutionLedgerRepository(db)

    async def high_watermark(self) -> int:
        return await self._repository.max_ledger_id()

    async def list_after(
        self, after_id: int, *, limit: int
    ) -> Sequence[Mapping[str, Any]]:
        rows = await self._repository.list_recent_fills_for_triage(
            after_id=after_id,
            source="websocket",
            limit=limit,
        )
        return [sanitize_fill(row) for row in rows]


class DbWatchAlertSource:
    """Read-only projection of watch events in durable delivery order."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def high_watermark(self) -> WatchCursor:
        result = await self._db.execute(
            select(InvestmentWatchEvent.delivered_at, InvestmentWatchEvent.id)
            .where(
                InvestmentWatchEvent.delivery_status == "delivered",
                InvestmentWatchEvent.delivered_at.is_not(None),
            )
            .order_by(
                InvestmentWatchEvent.delivered_at.desc(),
                InvestmentWatchEvent.id.desc(),
            )
            .limit(1)
        )
        row = result.first()
        if row is None:
            return WatchCursor(None, 0)
        return WatchCursor(row.delivered_at, int(row.id))

    async def list_after(
        self, cursor: WatchCursor, *, limit: int
    ) -> Sequence[Mapping[str, Any]]:
        if cursor.delivered_at is None:
            after_cursor = InvestmentWatchEvent.id > cursor.event_id
        else:
            after_cursor = or_(
                InvestmentWatchEvent.delivered_at > cursor.delivered_at,
                and_(
                    InvestmentWatchEvent.delivered_at == cursor.delivered_at,
                    InvestmentWatchEvent.id > cursor.event_id,
                ),
            )
        result = await self._db.execute(
            select(InvestmentWatchEvent)
            .where(
                after_cursor,
                InvestmentWatchEvent.delivery_status == "delivered",
                InvestmentWatchEvent.delivered_at.is_not(None),
            )
            .order_by(
                InvestmentWatchEvent.delivered_at.asc(),
                InvestmentWatchEvent.id.asc(),
            )
            .limit(max(1, min(int(limit), 500)))
        )
        return [_watch_dict(row) for row in result.scalars().all()]


class NullRiskPushNotifier:
    async def push(self, judgement: BrokerRiskJudgement) -> bool:
        del judgement
        return False


@dataclass(frozen=True)
class BundleConfig:
    state_dir: Path
    lanes: Mapping[str, str] = field(default_factory=dict)
    batch_limit: int = 500
    lookback_ids: int = DEFAULT_LOOKBACK_IDS
    sink_timeout_s: float = 3.0
    since_fill_id: int | None = None
    since_watch_id: int | None = None


def _watch_dict(row: Any) -> dict[str, Any]:
    return {
        "event_id": int(row.id),
        "event_uuid": str(row.event_uuid),
        "idempotency_key": row.idempotency_key,
        "market": row.market,
        "symbol": row.symbol,
        "metric": row.metric,
        "operator": row.operator,
        "threshold": str(row.threshold),
        "threshold_high": (
            None if row.threshold_high is None else str(row.threshold_high)
        ),
        "current_value": None if row.current_value is None else str(row.current_value),
        "outcome": row.outcome,
        "action_mode": row.action_mode,
        "delivered_at": (
            None if row.delivered_at is None else row.delivered_at.isoformat()
        ),
    }


def _watch_dedupe_key(event: Mapping[str, Any]) -> str:
    return f"watch:{event['idempotency_key']}"


def _fill_dedupe_key(fill: Mapping[str, Any]) -> str:
    """Retain the existing content key while isolating account and venue."""
    return f"{fill['account_mode']}:{fill['venue']}:{dedupe_key(fill)}"


def _event_id(kind: str, market: str, keys: Sequence[str]) -> str:
    material = "\n".join(keys).encode("utf-8")
    digest = hashlib.sha256(material).hexdigest()[:16]
    return f"{kind}-bundle:{market}:{digest}"


def _render_fill_bundle(market: str, fills: Sequence[Mapping[str, Any]]) -> str:
    lines = [f"[fill] {market} {len(fills)}건"]
    lines.extend(
        "- ledger_id={ledger_id} dedupe_key={key} {symbol} {side} "
        "{filled_qty}@{filled_price} {currency} {filled_notional}".format(
            key=_fill_dedupe_key(fill), **fill
        )
        for fill in fills
    )
    lines.append("지난 창 이후 체결을 검토하고 조정·추가 여부를 판단하라.")
    return "\n".join(lines)


def _render_watch_bundle(market: str, events: Sequence[Mapping[str, Any]]) -> str:
    lines = [f"[watch] {market} {len(events)}건"]
    lines.extend(
        "- event_id={event_id} dedupe_key={key} {symbol} {metric} {operator} "
        "threshold={threshold} current={current_value} outcome={outcome}".format(
            key=_watch_dedupe_key(event), **event
        )
        for event in events
    )
    lines.append("지난 창 이후 watch 발화를 검토하고 조정·추가 여부를 판단하라.")
    return "\n".join(lines)


def _safe_warning(message: str, *args: object) -> None:
    """Logging is observation only and must not break the pull runner."""
    try:
        import logging

        logging.getLogger(__name__).warning(message, *args, exc_info=True)
    except Exception:  # noqa: BLE001 - even broken handlers are fail-open
        pass


class FillHandoffBundleRunner:
    """Pull durable rows, bundle by market, and best-effort deliver them."""

    def __init__(
        self,
        config: BundleConfig,
        *,
        sink: LaneEventSink | None = None,
        notifier: RiskPushNotifier | None = None,
        detector: BrokerRiskDetector | None = None,
        now: Any | None = None,
    ) -> None:
        if config.batch_limit < 1 or config.batch_limit > 500:
            raise ValueError("batch_limit must be between 1 and 500")
        if config.lookback_ids < 0 or config.lookback_ids > 500:
            raise ValueError("lookback_ids must be between 0 and 500")
        if config.sink_timeout_s <= 0:
            raise ValueError("sink_timeout_s must be positive")
        self.config = config
        self.sink = sink or NullLaneEventSink()
        self.notifier = notifier or NullRiskPushNotifier()
        self.detector = detector or BrokerRiskDetector()
        self.now = now or (lambda: datetime.now(UTC))

    async def _send(self, lane: str, event_id: str, text: str) -> bool:
        try:
            return bool(
                await asyncio.wait_for(
                    self.sink.send(lane, event_id, text),
                    timeout=self.config.sink_timeout_s,
                )
            )
        except Exception:  # noqa: BLE001 - delivery never owns source durability
            _safe_warning("fill handoff sink failed: event_id=%s", event_id)
            return False

    async def _push(self, judgement: BrokerRiskJudgement) -> bool:
        if judgement.category not in RISK_CATEGORIES or not judgement.evidence:
            return False
        try:
            return bool(
                await asyncio.wait_for(
                    self.notifier.push(judgement),
                    timeout=self.config.sink_timeout_s,
                )
            )
        except Exception:  # noqa: BLE001 - risk notification is best effort
            _safe_warning(
                "BROKER_RISK notifier failed: dedupe_id=%s", judgement.dedupe_id
            )
            return False

    @staticmethod
    def _purge_seen(state: dict[str, Any], now_ts: float) -> None:
        cutoff = now_ts - DEDUP_WINDOW.total_seconds()
        for name in ("seen", "risk_seen"):
            values = state.setdefault(name, {})
            state[name] = {
                str(key): float(value)
                for key, value in values.items()
                if float(value) >= cutoff
            }

    async def _seed_or_catch_up(
        self,
        state: dict[str, Any],
        *,
        fill_source: FillEventSource,
        watch_source: WatchAlertSource,
        new_state: bool,
        enabled: bool,
    ) -> list[str]:
        errors: list[str] = []
        if new_state and self.config.since_fill_id is not None:
            state["fill_watermark"] = self.config.since_fill_id
            state["fill_initialized"] = True
            state["fill_lookback_armed"] = True
        elif not enabled or not state["fill_initialized"]:
            try:
                fill_high = await fill_source.high_watermark()
            except Exception:  # noqa: BLE001 - one source cannot break the other
                errors.append("fill_high_watermark_failed")
                _safe_warning("fill handoff high-water read failed: source=fill")
            else:
                state["fill_watermark"] = max(
                    int(state.get("fill_watermark", 0)), int(fill_high)
                )
                state["fill_initialized"] = True
                state["fill_lookback_armed"] = False

        if new_state and self.config.since_watch_id is not None:
            state["watch_watermark"] = self.config.since_watch_id
            state["watch_delivered_at"] = None
            state["watch_initialized"] = True
            state["watch_lookback_armed"] = True
        elif not enabled or not state["watch_initialized"]:
            try:
                watch_high = await watch_source.high_watermark()
            except Exception:  # noqa: BLE001 - one source cannot break the other
                errors.append("watch_high_watermark_failed")
                _safe_warning("fill handoff high-water read failed: source=watch")
            else:
                state["watch_watermark"] = int(watch_high.event_id)
                state["watch_delivered_at"] = (
                    None
                    if watch_high.delivered_at is None
                    else watch_high.delivered_at.isoformat()
                )
                state["watch_initialized"] = True
                state["watch_lookback_armed"] = False
        return errors

    async def run(
        self,
        db: AsyncSession,
        *,
        fill_source: FillEventSource | None = None,
        watch_source: WatchAlertSource | None = None,
        evidence_source: BrokerRiskEvidenceSource | None = None,
    ) -> dict[str, Any]:
        fills = fill_source or DbFillEventSource(db)
        watches = watch_source or DbWatchAlertSource(db)
        evidence = evidence_source or SqlAlchemyEvidenceSource(db)
        enabled = handoff_enabled()
        outcome: dict[str, Any] = {
            "enabled": enabled,
            "fill_bundles": 0,
            "watch_bundles": 0,
            "risk_pushes": 0,
            "errors": [],
        }

        with HandoffState(self.config.state_dir) as locked:
            state = locked.data
            state["version"] = _STATE_VERSION
            state.setdefault("fill_watermark", 0)
            state.setdefault("watch_watermark", 0)
            state.setdefault("watch_delivered_at", None)
            state.setdefault("fill_initialized", not locked.is_new)
            state.setdefault("watch_initialized", not locked.is_new)
            state.setdefault("fill_lookback_armed", not locked.is_new)
            state.setdefault("watch_lookback_armed", not locked.is_new)
            state.setdefault("risk_seen", {})
            self._purge_seen(state, self.now().timestamp())
            outcome["errors"].extend(
                await self._seed_or_catch_up(
                    state,
                    fill_source=fills,
                    watch_source=watches,
                    new_state=locked.is_new,
                    enabled=enabled,
                )
            )
            if not enabled:
                locked.save()
                outcome["fill_watermark"] = int(state["fill_watermark"])
                outcome["watch_watermark"] = int(state["watch_watermark"])
                return outcome

            if state["fill_initialized"] and state["fill_lookback_armed"]:
                await self._run_fills(state, outcome, fills, evidence)
            elif state["fill_initialized"]:
                state["fill_lookback_armed"] = True
            if state["watch_initialized"] and state["watch_lookback_armed"]:
                await self._run_watches(state, outcome, watches)
            elif state["watch_initialized"]:
                state["watch_lookback_armed"] = True
            locked.save()
            outcome["fill_watermark"] = int(state["fill_watermark"])
            outcome["watch_watermark"] = int(state["watch_watermark"])
            return outcome

    async def _run_fills(
        self,
        state: dict[str, Any],
        outcome: dict[str, Any],
        source: FillEventSource,
        evidence: BrokerRiskEvidenceSource,
    ) -> None:
        try:
            watermark = int(state["fill_watermark"])
            lookback_rows = (
                await source.list_after(
                    max(0, watermark - self.config.lookback_ids),
                    limit=self.config.lookback_ids,
                )
                if self.config.lookback_ids
                else ()
            )
            new_rows = await source.list_after(watermark, limit=self.config.batch_limit)
            rows = _merge_rows(lookback_rows, new_rows, id_key="ledger_id")
        except Exception:  # noqa: BLE001 - watch processing must still proceed
            outcome["errors"].append("fill_read_failed")
            _safe_warning("fill handoff fill read failed")
            return

        now_ts = self.now().timestamp()
        risk_seen: dict[str, float] = state["risk_seen"]
        for fill in rows:
            try:
                judgements = await self.detector.detect(fill, source=evidence)
            except Exception:  # noqa: BLE001 - detector implementations are injected
                outcome["errors"].append("risk_detection_failed")
                _safe_warning("BROKER_RISK detection failed")
                continue
            for judgement in judgements:
                if judgement.dedupe_id in risk_seen:
                    continue
                if await self._push(judgement):
                    risk_seen[judgement.dedupe_id] = now_ts
                    outcome["risk_pushes"] += 1

        resolved: set[int] = set()
        grouped: dict[str, dict[str, list[Mapping[str, Any]]]] = defaultdict(
            lambda: defaultdict(list)
        )
        seen: dict[str, float] = state["seen"]
        for fill in rows:
            row_id = int(fill["ledger_id"])
            key = _fill_dedupe_key(fill)
            if key in seen:
                resolved.add(row_id)
            else:
                grouped[str(fill["market"])][key].append(fill)

        for market, by_key in grouped.items():
            lane = self.config.lanes.get(market)
            if not lane:
                outcome["errors"].append(f"fill_lane_missing:{market}")
                continue
            representatives = [values[0] for values in by_key.values()]
            keys = list(by_key)
            if await self._send(
                lane,
                _event_id("fill", market, keys),
                _render_fill_bundle(market, representatives),
            ):
                outcome["fill_bundles"] += 1
                for key, values in by_key.items():
                    seen[key] = now_ts
                    resolved.update(int(value["ledger_id"]) for value in values)

        state["fill_watermark"] = _advance_watermark(
            int(state["fill_watermark"]), rows, resolved, id_key="ledger_id"
        )

    async def _run_watches(
        self,
        state: dict[str, Any],
        outcome: dict[str, Any],
        source: WatchAlertSource,
    ) -> None:
        try:
            delivered_at = state.get("watch_delivered_at")
            cursor = WatchCursor(
                None if delivered_at is None else datetime.fromisoformat(delivered_at),
                int(state["watch_watermark"]),
            )
            lookback_rows = (
                await source.list_after(
                    WatchCursor(
                        None,
                        max(0, cursor.event_id - self.config.lookback_ids),
                    ),
                    limit=self.config.lookback_ids,
                )
                if self.config.lookback_ids
                else ()
            )
            new_rows = await source.list_after(cursor, limit=self.config.batch_limit)
            rows = _merge_rows(lookback_rows, new_rows, id_key="event_id")
            rows.sort(
                key=lambda value: (
                    str(value.get("delivered_at") or ""),
                    int(value["event_id"]),
                )
            )
        except Exception:  # noqa: BLE001 - fill delivery remains committed
            outcome["errors"].append("watch_read_failed")
            _safe_warning("fill handoff watch read failed")
            return

        now_ts = self.now().timestamp()
        resolved: set[int] = set()
        grouped: dict[str, dict[str, list[Mapping[str, Any]]]] = defaultdict(
            lambda: defaultdict(list)
        )
        seen: dict[str, float] = state["seen"]
        for event in rows:
            row_id = int(event["event_id"])
            key = _watch_dedupe_key(event)
            if key in seen:
                resolved.add(row_id)
            else:
                grouped[str(event["market"])][key].append(event)

        for market, by_key in grouped.items():
            lane = self.config.lanes.get(market)
            if not lane:
                outcome["errors"].append(f"watch_lane_missing:{market}")
                continue
            representatives = [values[0] for values in by_key.values()]
            keys = list(by_key)
            if await self._send(
                lane,
                _event_id("watch", market, keys),
                _render_watch_bundle(market, representatives),
            ):
                outcome["watch_bundles"] += 1
                for key, values in by_key.items():
                    seen[key] = now_ts
                    resolved.update(int(value["event_id"]) for value in values)

        advanced = _advance_watch_cursor(cursor, rows, resolved)
        state["watch_watermark"] = advanced.event_id
        state["watch_delivered_at"] = (
            None if advanced.delivered_at is None else advanced.delivered_at.isoformat()
        )


def _advance_watermark(
    current: int,
    rows: Sequence[Mapping[str, Any]],
    resolved: set[int],
    *,
    id_key: str,
) -> int:
    watermark = current
    for row in sorted(rows, key=lambda value: int(value[id_key])):
        row_id = int(row[id_key])
        if row_id not in resolved:
            break
        watermark = max(watermark, row_id)
    return watermark


def _merge_rows(
    lookback_rows: Sequence[Mapping[str, Any]],
    new_rows: Sequence[Mapping[str, Any]],
    *,
    id_key: str,
) -> list[Mapping[str, Any]]:
    """Merge overlapping reads by durable row id without filtering content."""
    by_id = {int(row[id_key]): row for row in (*lookback_rows, *new_rows)}
    return [by_id[row_id] for row_id in sorted(by_id)]


def _advance_watch_cursor(
    current: WatchCursor,
    rows: Sequence[Mapping[str, Any]],
    resolved: set[int],
) -> WatchCursor:
    cursor = current
    for row in rows:
        row_id = int(row["event_id"])
        if row_id not in resolved:
            break
        delivered_at = row.get("delivered_at")
        if delivered_at is None:
            break
        candidate = WatchCursor(datetime.fromisoformat(str(delivered_at)), row_id)
        if cursor.delivered_at is None or (
            candidate.delivered_at,
            candidate.event_id,
        ) > (cursor.delivered_at, cursor.event_id):
            cursor = candidate
    return cursor


__all__ = [
    "BundleConfig",
    "DbFillEventSource",
    "DbWatchAlertSource",
    "DEFAULT_LOOKBACK_IDS",
    "FillEventSource",
    "FillHandoffBundleRunner",
    "LaneEventSink",
    "NullLaneEventSink",
    "PanewireLaneEventSink",
    "WatchAlertSource",
    "WatchCursor",
    "handoff_enabled",
]

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

from sqlalchemy import func, select
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
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_STATE_VERSION = 2


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
    async def high_watermark(self) -> int: ...

    async def list_after(
        self, after_id: int, *, limit: int
    ) -> Sequence[Mapping[str, Any]]: ...


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
    """Read-only projection of delivered watch events, keyed by row id."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def high_watermark(self) -> int:
        result = await self._db.execute(
            select(func.max(InvestmentWatchEvent.id)).where(
                InvestmentWatchEvent.delivery_status == "delivered"
            )
        )
        return int(result.scalar_one() or 0)

    async def list_after(
        self, after_id: int, *, limit: int
    ) -> Sequence[Mapping[str, Any]]:
        result = await self._db.execute(
            select(InvestmentWatchEvent)
            .where(
                InvestmentWatchEvent.id > after_id,
                InvestmentWatchEvent.delivery_status == "delivered",
            )
            .order_by(InvestmentWatchEvent.id.asc())
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


def _event_id(kind: str, market: str, keys: Sequence[str]) -> str:
    material = "\n".join(keys).encode("utf-8")
    digest = hashlib.sha256(material).hexdigest()[:16]
    return f"{kind}-bundle:{market}:{digest}"


def _render_fill_bundle(market: str, fills: Sequence[Mapping[str, Any]]) -> str:
    lines = [f"[fill] {market} {len(fills)}건"]
    lines.extend(
        "- ledger_id={ledger_id} dedupe_key={key} {symbol} {side} "
        "{filled_qty}@{filled_price} {currency} {filled_notional}".format(
            key=dedupe_key(fill), **fill
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
        if enabled and not new_state:
            return errors
        for name, source, configured in (
            ("fill", fill_source, self.config.since_fill_id),
            ("watch", watch_source, self.config.since_watch_id),
        ):
            key = f"{name}_watermark"
            if new_state and configured is not None:
                state[key] = configured
                continue
            try:
                high_watermark = await source.high_watermark()
            except Exception:  # noqa: BLE001 - one source cannot break the other
                errors.append(f"{name}_high_watermark_failed")
                _safe_warning("fill handoff high-water read failed: source=%s", name)
                continue
            if not enabled or new_state:
                state[key] = max(int(state.get(key, 0)), int(high_watermark))
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
            if locked.is_new or not enabled:
                locked.save()
                outcome["fill_watermark"] = int(state["fill_watermark"])
                outcome["watch_watermark"] = int(state["watch_watermark"])
                return outcome

            await self._run_fills(state, outcome, fills, evidence)
            await self._run_watches(state, outcome, watches)
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
            rows = list(
                await source.list_after(
                    int(state["fill_watermark"]), limit=self.config.batch_limit
                )
            )
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
            key = dedupe_key(fill)
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
            rows = list(
                await source.list_after(
                    int(state["watch_watermark"]), limit=self.config.batch_limit
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

        state["watch_watermark"] = _advance_watermark(
            int(state["watch_watermark"]), rows, resolved, id_key="event_id"
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


__all__ = [
    "BundleConfig",
    "DbFillEventSource",
    "DbWatchAlertSource",
    "FillEventSource",
    "FillHandoffBundleRunner",
    "LaneEventSink",
    "NullLaneEventSink",
    "PanewireLaneEventSink",
    "WatchAlertSource",
    "handoff_enabled",
]

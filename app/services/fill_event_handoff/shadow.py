"""Shadow evaluation helpers for the bundled fill/watch handoff (#931).

The bundle runner's shadow mode evaluates everything the enabled path would —
risk detection, grouping, dedupe, and the #825/#865 kick classifiers — while
the transports are forced to the Null implementations, so nothing leaves the
process.  Kick gating mirrors ``FillHandoffRunner._gated_kick`` minus the
Prefect lookup/create calls: a ``kick`` decision is a would-kick that reserves
its cap/cooldown slot in the *shadow* state file only.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol

from .kick_filter import (
    DEFAULT_KICK_DAILY_CAP,
    DEFAULT_MIN_POSITION_FRACTION,
    DEFAULT_PARKING_SYMBOLS,
    DEFAULT_SMALL_BUY_NOTIONAL,
    FillPositionFacts,
    KickDecision,
    KickVerdict,
    classify_fill_for_kick,
    classify_without_position,
)
from .service import (
    DEDUP_WINDOW,
    KST,
    WATCH_KICK_BATCH_LIMIT,
    _advance_watch_kick_cursor,
    _exact_int,
    _watch_kick_cursor_from_state,
    _watch_row_order_key,
    in_regular_rep_window,
)
from .watch_kick import (
    WatchKickCursor,
    classify_watch_for_kick,
    is_tradable_now,
)


class PositionFactsSource(Protocol):
    """Read-only position lookup for the shadow kick filter."""

    async def position_before_fill(
        self,
        *,
        broker: str,
        account_mode: str,
        venue: str,
        instrument_type: Any,
        symbol: str,
        currency: str,
        filled_at: datetime,
        ledger_id: int,
    ) -> tuple[Any, Any]: ...


class WatchKickSource(Protocol):
    """Read-only delivered-watch source carrying ``alert_max_action``."""

    async def high_watermark(self) -> WatchKickCursor: ...

    async def list_after(
        self, cursor: WatchKickCursor, *, limit: int
    ) -> Sequence[Mapping[str, Any]]: ...


@dataclass(frozen=True)
class ShadowKickConfig:
    """Kick-gate knobs a shadow run mirrors from the enabled deployment.

    Parsing/normalization matches ``HandoffConfig`` so the counts answer "what
    would the current kick configuration have done".  ``prefect_api_url`` and
    ``kick_deployments`` are presence checks only — shadow never resolves a
    deployment name or creates a flow run.
    """

    kick_enabled: bool = False
    prefect_api_url: str | None = None
    kick_deployments: Mapping[str, str] | None = None
    kick_daily_cap: int = DEFAULT_KICK_DAILY_CAP
    kick_cooldown_seconds: int = 3600
    kick_parking_symbols: frozenset[str] | None = None
    kick_small_buy_notional: Mapping[str, Decimal] | None = None
    kick_min_position_fraction: Decimal = DEFAULT_MIN_POSITION_FRACTION

    def __post_init__(self) -> None:
        if self.kick_daily_cap < 0:
            raise ValueError("kick_daily_cap must be non-negative")
        if self.kick_min_position_fraction <= 0:
            raise ValueError("kick_min_position_fraction must be positive")
        parking = (
            DEFAULT_PARKING_SYMBOLS
            if self.kick_parking_symbols is None
            else frozenset(
                str(item).strip().upper()
                for item in self.kick_parking_symbols
                if str(item).strip()
            )
        )
        object.__setattr__(self, "kick_parking_symbols", parking)
        notionals = (
            dict(DEFAULT_SMALL_BUY_NOTIONAL)
            if self.kick_small_buy_notional is None
            else {
                str(k).upper(): Decimal(str(v))
                for k, v in self.kick_small_buy_notional.items()
            }
        )
        if any(not value.is_finite() or value < 0 for value in notionals.values()):
            raise ValueError("kick_small_buy_notional values must be non-negative")
        object.__setattr__(self, "kick_small_buy_notional", notionals)


def _env_enabled(value: str | None) -> bool:
    """Match the legacy runner's kick-gate parsing exactly (``true`` only)."""
    return value is not None and value.strip().lower() == "true"


def _env_map(value: str | None, env_name: str) -> dict[str, str]:
    if not value:
        return {}
    parsed = json.loads(value)
    if not isinstance(parsed, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in parsed.items()
    ):
        raise ValueError(f"{env_name} must be a string map")
    return parsed


def _env_decimal_map(value: str | None, env_name: str) -> dict[str, Decimal] | None:
    if value is None or not value.strip():
        return None
    parsed = json.loads(value)
    if not isinstance(parsed, dict) or not all(isinstance(k, str) for k in parsed):
        raise ValueError(f"{env_name} must be a currency-to-notional map")
    result: dict[str, Decimal] = {}
    for key, amount in parsed.items():
        if isinstance(amount, bool) or not isinstance(amount, (str, int, float)):
            raise ValueError(f"{env_name} values must be numeric")
        result[key] = Decimal(str(amount))
    return result


def _env_parking(value: str | None) -> frozenset[str] | None:
    if value is None or not value.strip():
        return None
    return frozenset(item.strip() for item in value.split(",") if item.strip())


def shadow_kick_config_from_env(
    env: Mapping[str, str] = os.environ,
) -> ShadowKickConfig:
    """Read the same ``FILL_HANDOFF_KICK_*``/``PREFECT_API_URL`` knobs the
    enabled legacy runner would use, so shadow dispositions mirror it."""
    return ShadowKickConfig(
        kick_enabled=_env_enabled(env.get("FILL_HANDOFF_KICK_ENABLED")),
        prefect_api_url=env.get("PREFECT_API_URL") or None,
        kick_deployments=_env_map(
            env.get("FILL_HANDOFF_KICK_DEPLOYMENTS"), "FILL_HANDOFF_KICK_DEPLOYMENTS"
        ),
        kick_daily_cap=int(
            env.get("FILL_HANDOFF_KICK_DAILY_CAP", str(DEFAULT_KICK_DAILY_CAP))
        ),
        kick_cooldown_seconds=int(env.get("FILL_HANDOFF_KICK_COOLDOWN_S", "3600")),
        kick_parking_symbols=_env_parking(env.get("FILL_HANDOFF_KICK_PARKING_SYMBOLS")),
        kick_small_buy_notional=_env_decimal_map(
            env.get("FILL_HANDOFF_KICK_SMALL_BUY_NOTIONAL"),
            "FILL_HANDOFF_KICK_SMALL_BUY_NOTIONAL",
        ),
        kick_min_position_fraction=Decimal(
            env.get("FILL_HANDOFF_KICK_MIN_POSITION_FRACTION", "0.25")
        ),
    )


async def classify_shadow_fill(
    fill: Mapping[str, Any],
    positions: PositionFactsSource,
    *,
    knobs: ShadowKickConfig,
) -> KickVerdict:
    """Mirror of ``FillHandoffRunner._classify_fill`` over an injected source."""
    early = classify_without_position(
        fill,
        parking_symbols=knobs.kick_parking_symbols or frozenset(),
        small_buy_notional=knobs.kick_small_buy_notional or {},
    )
    if early is not None:
        return early
    try:
        filled_at = datetime.fromisoformat(str(fill["filled_at"]))
    except (KeyError, TypeError, ValueError):
        return KickVerdict(False, "fill_malformed")
    if filled_at.tzinfo is None or filled_at.utcoffset() is None:
        return KickVerdict(False, "fill_malformed")
    facts: FillPositionFacts | None
    try:
        qty_before, rows_before = await positions.position_before_fill(
            broker=str(fill["broker"]),
            account_mode=str(fill["account_mode"]),
            venue=str(fill["venue"]),
            instrument_type=str(fill.get("instrument_type")),
            symbol=str(fill["symbol"]),
            currency=str(fill["currency"]),
            filled_at=filled_at,
            ledger_id=int(fill["ledger_id"]),
        )
        facts = FillPositionFacts(
            qty_before=Decimal(str(qty_before)), rows_before=int(rows_before)
        )
    except Exception:  # noqa: BLE001 - a failed read must never guess
        facts = None
    try:
        return classify_fill_for_kick(
            fill,
            facts,
            parking_symbols=knobs.kick_parking_symbols or frozenset(),
            small_buy_notional=knobs.kick_small_buy_notional or {},
            min_position_fraction=knobs.kick_min_position_fraction,
        )
    except Exception:  # noqa: BLE001 - classification must never break a run
        return KickVerdict(False, "classification_failed")


def classify_shadow_watch(event: Mapping[str, Any], now: datetime) -> KickVerdict:
    """Mirror of ``FillHandoffRunner._classify_watch`` including staleness."""
    tradable = is_tradable_now(str(event.get("market") or ""), now)
    verdict = classify_watch_for_kick(
        event,
        event.get("alert_max_action"),
        tradable=tradable,
    )
    if not verdict.eligible:
        return verdict
    try:
        delivered_at = datetime.fromisoformat(str(event.get("delivered_at")))
        stale = (
            delivered_at.tzinfo is None
            or delivered_at.tzinfo.utcoffset(delivered_at) is None
            or now - delivered_at >= DEDUP_WINDOW
        )
    except (TypeError, ValueError):
        stale = True
    if stale:
        return KickVerdict(False, "stale_event")
    return verdict


def gate_shadow_kick(
    market: str,
    verdict: KickVerdict | None,
    *,
    state: dict[str, Any],
    now: datetime,
    knobs: ShadowKickConfig,
) -> KickDecision:
    """Mirror of ``FillHandoffRunner._gated_kick`` minus the Prefect calls.

    The gate order is identical to the live path; a ``kick`` class means the
    run would have created a flow run, and the cap/cooldown slot is reserved
    in shadow state exactly as the live path reserves it before sending.
    """
    if (
        not knobs.kick_enabled
        or not knobs.prefect_api_url
        or not knobs.kick_deployments
    ):
        return KickDecision("queue_only", "kick_not_configured")
    if verdict is None or not verdict.eligible:
        return KickDecision(
            "queue_only", verdict.reason if verdict is not None else "unclassified"
        )
    if in_regular_rep_window(market, now):
        return KickDecision("queue_only", "rep_window")
    kick_days = state.setdefault("kick_days", {})
    today = f"{now.astimezone(KST):%Y%m%d}"
    day = kick_days.get(market)
    if not isinstance(day, dict) or day.get("date") != today:
        day = {"date": today, "count": 0}
        kick_days[market] = day
    if int(day.get("count") or 0) >= knobs.kick_daily_cap:
        return KickDecision("capped", "daily_cap")
    previous = float(state["cooldowns"].get(market) or 0)
    if now.timestamp() - previous < knobs.kick_cooldown_seconds:
        return KickDecision("queue_only", "cooldown")
    if not knobs.kick_deployments.get(market):
        return KickDecision("queue_only", "deployment_unmapped")
    state["cooldowns"][market] = now.timestamp()
    day["count"] = int(day.get("count") or 0) + 1
    return KickDecision("kick", verdict.reason)


def record_decision(bucket: dict[str, Any], decision: KickDecision) -> None:
    """Count one kick-gate disposition by class and by reason."""
    bucket[decision.klass] = int(bucket.get(decision.klass) or 0) + 1
    reasons = bucket.setdefault("by_reason", {})
    reasons[decision.reason] = int(reasons.get(decision.reason) or 0) + 1


async def record_fill_kicks(
    grouped: Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]],
    positions: PositionFactsSource,
    counts: dict[str, Any],
    *,
    state: dict[str, Any],
    now: datetime,
    knobs: ShadowKickConfig,
) -> None:
    """Classify each new fill dedupe key and apply the shared shadow gate.

    ``grouped`` holds only rows that were not already in ``seen``, so each
    dedupe key is judged exactly once — a suppressed duplicate can never be
    re-counted as a candidate on a later run.
    """
    bucket = counts["kick"]
    for market, by_key in grouped.items():
        for values in by_key.values():
            verdict = await classify_shadow_fill(values[0], positions, knobs=knobs)
            try:
                decision = gate_shadow_kick(
                    str(market), verdict, state=state, now=now, knobs=knobs
                )
            except Exception:  # noqa: BLE001 - the record is canonical
                decision = KickDecision("queue_only", "kick_error")
            record_decision(bucket, decision)


def _seen_mark_timestamp(value: Any) -> float:
    """Tolerate both encodings: bundle ``{"id","ts"}`` dicts and plain floats."""
    try:
        if isinstance(value, dict):
            return float(value.get("ts", 0))
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


async def record_watch_kicks(
    state: dict[str, Any],
    counts: dict[str, Any],
    *,
    source: WatchKickSource,
    now: datetime,
    knobs: ShadowKickConfig,
) -> None:
    """Mirror of ``FillHandoffRunner._run_watch_kicks`` minus Prefect sends.

    Keeps the kick pass's own ``watch_kick_*`` cursor in shadow state, seeds it
    to the delivered high-water mark on first sight, groups ladder fires by
    (market, symbol), and draws from the same ``kick_days``/``cooldowns`` maps
    the fill gate uses — all without a single external call.
    """
    errors = counts["errors"]
    if "watch_kick_watermark" not in state:
        # Install boundary: the delivered backlog is history, never a queue.
        try:
            high = await source.high_watermark()
        except Exception:  # noqa: BLE001 - seeding must not break the run
            errors.append("watch_kick_seed_failed")
            return
        state["watch_kick_watermark"] = int(high.event_id)
        state["watch_kick_delivered_at"] = (
            None if high.delivered_at is None else high.delivered_at.isoformat()
        )
        return
    cursor = _watch_kick_cursor_from_state(state)
    if cursor is None:
        errors.append("watch_kick_cursor_corrupt")
        try:
            high = await source.high_watermark()
        except Exception:  # noqa: BLE001 - a reseed failure keeps the state
            errors.append("watch_kick_seed_failed")
            return
        state["watch_kick_watermark"] = int(high.event_id)
        state["watch_kick_delivered_at"] = (
            None if high.delivered_at is None else high.delivered_at.isoformat()
        )
        return
    try:
        rows = sorted(
            await source.list_after(cursor, limit=WATCH_KICK_BATCH_LIMIT),
            key=_watch_row_order_key,
        )
    except Exception:  # noqa: BLE001 - the bundle pass must not be wedged
        errors.append("watch_kick_read_failed")
        return
    counts["watch_kick_rows"] += len(rows)
    clean: list[Mapping[str, Any]] = []
    for event in rows:
        try:
            if _exact_int(event.get("event_id")) is None:
                raise ValueError("malformed event_id")
            delivered = event.get("delivered_at")
            if delivered is None:
                raise ValueError("missing delivered_at")
            parsed = datetime.fromisoformat(str(delivered))
            if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
                raise ValueError("naive delivered_at")
        except (KeyError, TypeError, ValueError):
            errors.append("watch_kick_event_malformed")
            continue
        clean.append(event)
    resolved: set[int] = set()
    candidates: set[tuple[str, str]] = set()
    bucket = counts["watch_kick"]
    for event in clean:
        event_id = int(event["event_id"])
        group_key = (
            str(event.get("market") or "").strip().lower(),
            str(event.get("symbol") or "").strip().upper(),
        )
        try:
            verdict = classify_shadow_watch(event, now)
        except Exception:  # noqa: BLE001 - a classifier bug resolves too
            verdict = KickVerdict(False, "classification_failed")
        if not verdict.eligible:
            decision = KickDecision("queue_only", verdict.reason)
        elif group_key in candidates:
            decision = KickDecision("queue_only", "ladder_grouped")
        else:
            candidates.add(group_key)
            # Crash-replay mark, mirroring the live path: written to shadow
            # ``seen`` before the gate so a re-run cannot re-gate the event.
            kick_seen = f"watchkick:{event_id}"
            seen_at = _seen_mark_timestamp(state["seen"].get(kick_seen))
            if now.timestamp() - seen_at < DEDUP_WINDOW.total_seconds():
                decision = KickDecision("queue_only", "already_kicked")
            else:
                state["seen"][kick_seen] = now.timestamp()
                try:
                    decision = gate_shadow_kick(
                        group_key[0],
                        verdict,
                        state=state,
                        now=now,
                        knobs=knobs,
                    )
                except Exception:  # noqa: BLE001 - record is canonical
                    decision = KickDecision("queue_only", "kick_error")
        record_decision(bucket, decision)
        resolved.add(event_id)
    advanced = _advance_watch_kick_cursor(cursor, clean, resolved)
    state["watch_kick_watermark"] = advanced.event_id
    state["watch_kick_delivered_at"] = (
        None if advanced.delivered_at is None else advanced.delivered_at.isoformat()
    )


__all__ = [
    "PositionFactsSource",
    "ShadowKickConfig",
    "WatchKickSource",
    "classify_shadow_fill",
    "classify_shadow_watch",
    "gate_shadow_kick",
    "record_decision",
    "record_fill_kicks",
    "record_watch_kicks",
    "shadow_kick_config_from_env",
]

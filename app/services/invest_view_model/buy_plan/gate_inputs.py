"""Discovery-gate metric readers for the 매수 계획 board (§144차).

``config/trading_policy.yaml`` → ``market_rules.crypto.recovery_gate`` names
two metrics by id. This module resolves exactly those two and nothing else:

``alt_breadth_24h`` (``upbit_alt_breadth_24h``)
    Share of KRW-quoted alts outperforming KRW-BTC over 24h, from the official
    Upbit Open API ticker via :func:`app.services.external.upbit_index
    .fetch_upbit_altseason`. That function's own docstring defines breadth in
    exactly the policy's terms, so no re-derivation happens here.

``btc_long_short_ratio``
    Binance ``globalLongShortAccountRatio`` and ``topLongShortPositionRatio``.
    The policy says "both report inputs should remain at or below the
    threshold", so the resolved value is the **maximum** of the two legs —
    comparing that single number with ``lte`` reproduces the two-leg rule. If
    either leg is missing the value is ``None``, never the surviving leg.

Both readers are fail-open to ``None``. The policy's
``missing_or_null_threshold: do_not_infer_or_count_as_met`` then makes a
``None`` an ``unavailable`` condition, which cannot count toward the gate — so
a dead upstream leaves the gate un-passable rather than silently open.

task-792 C1 freshness: each reading also carries ``observed_at``, the epoch
time of the oldest contributing observation (oldest swept ticker trade
timestamp / oldest Binance report bucket). The gate layer compares it with the
condition's ``stale_after_seconds`` and a stale input resolves to ``hold`` —
it is never trusted and never counted as an inferred miss either.

The fetch/parse split is deliberate: ``parse_*`` are pure payload-to-reading
functions shared with ``scripts/policy_table/adapters/crypto.py`` so the
advisory table resolves the same inputs through the same extraction — the
metric is defined once here, not re-derived per consumer.

The board is a read surface, so results are cached for
``GATE_CACHE_TTL_SECONDS`` to keep a page refresh from fanning out.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Final, Literal

logger = logging.getLogger(__name__)

GATE_CACHE_TTL_SECONDS: Final = 180

ALT_BREADTH_SOURCE: Final = "upbit_open_api_ticker_derived"
LONG_SHORT_SOURCE: Final = "binance_global_account+binance_top_trader_position"


@dataclass(frozen=True, slots=True)
class GateMetricReading:
    """One resolved (or unresolved) gate metric.

    ``observed_at`` is the epoch-second timestamp of the newest underlying
    observation, or ``None`` when the upstream payload cannot be dated. A
    condition that declares ``stale_after_seconds`` cannot be proven fresh
    without it — freshness unproven is not trusted.
    """

    metric: str
    value: Decimal | None
    source: str
    note: str | None = None
    observed_at: float | None = None


_cache: dict[str, tuple[float, GateMetricReading]] = {}
_cache_lock = asyncio.Lock()


def _reset_cache_for_tests() -> None:
    _cache.clear()


def _to_decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _iso_to_epoch(value: object) -> float | None:
    """Parse an ISO-8601 timestamp to epoch seconds; ``None`` if undatable."""

    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.timestamp()


async def _cached(
    key: str,
    producer: Callable[[], Awaitable[GateMetricReading]],
    *,
    now: float,
) -> GateMetricReading:
    """Serve a fresh-enough reading, else produce one and store it.

    ``producer`` is a factory rather than an already-created coroutine so a
    cache hit does not leave an un-awaited coroutine behind.
    """

    async with _cache_lock:
        hit = _cache.get(key)
        if hit is not None and now - hit[0] < GATE_CACHE_TTL_SECONDS:
            return hit[1]
    reading = await producer()
    async with _cache_lock:
        _cache[key] = (now, reading)
    return reading


def parse_alt_breadth_reading(payload: dict[str, Any] | None) -> GateMetricReading:
    """Pure: ``fetch_upbit_altseason`` payload → gate reading.

    ``breadth.alts_beating_btc_pct`` is a 0..1 fraction converted once to
    percent (the policy threshold's unit); ``breadth.latest_trade_at`` dates
    the observation for the stale check.
    """

    breadth = (payload or {}).get("breadth") if isinstance(payload, dict) else None
    fraction = _to_decimal((breadth or {}).get("alts_beating_btc_pct"))
    if fraction is None:
        return GateMetricReading(
            metric="upbit_alt_breadth_24h",
            value=None,
            source=ALT_BREADTH_SOURCE,
            note="Upbit 티커에서 breadth를 산출하지 못했습니다.",
        )
    total = (breadth or {}).get("alts_total")
    beating = (breadth or {}).get("alts_beating_btc")
    return GateMetricReading(
        metric="upbit_alt_breadth_24h",
        value=fraction * Decimal(100),
        source=ALT_BREADTH_SOURCE,
        note=f"{beating}/{total} alts > KRW-BTC (24h)"
        if total is not None and beating is not None
        else None,
        observed_at=_iso_to_epoch((breadth or {}).get("latest_trade_at")),
    )


def parse_btc_long_short_reading(payload: dict[str, Any] | None) -> GateMetricReading:
    """Pure: ``handle_get_long_short_ratio`` payload → gate reading.

    ``observed_at`` is the OLDER of the two legs' newest report buckets —
    the pair is only as fresh as its stalest leg.
    """

    if not isinstance(payload, dict) or payload.get("error"):
        return GateMetricReading(
            metric="btc_long_short_ratio",
            value=None,
            source=LONG_SHORT_SOURCE,
            note="Binance 롱숏 비율 응답을 해석하지 못했습니다.",
        )

    legs = [payload.get(key) or {} for key in ("global_account", "top_position")]
    ratios = [_to_decimal(leg.get("ratio")) for leg in legs]
    if any(ratio is None for ratio in ratios):
        # Deliberately not "use whichever leg answered": the policy asks both
        # reports to sit at or below the threshold, so one leg cannot stand in
        # for the pair without weakening the gate.
        return GateMetricReading(
            metric="btc_long_short_ratio",
            value=None,
            source=LONG_SHORT_SOURCE,
            note="두 리포트 중 하나가 비어 있어 판정 불가(한쪽만으로 대체하지 않음).",
        )

    observed: list[float | None] = []
    for leg in legs:
        history = leg.get("history")
        newest = (
            history[-1].get("time") if isinstance(history, list) and history else None
        )
        epoch = _iso_to_epoch(newest)
        observed.append(epoch)

    resolved = max(ratio for ratio in ratios if ratio is not None)
    return GateMetricReading(
        metric="btc_long_short_ratio",
        value=resolved,
        source=LONG_SHORT_SOURCE,
        note="global_account / top_position 중 더 높은 값",
        observed_at=min(observed) if all(ts is not None for ts in observed) else None,
    )


def _compare(operator: str, value: Decimal, threshold: Decimal) -> bool:
    if operator == "gt":
        return value > threshold
    if operator == "gte":
        return value >= threshold
    if operator == "lt":
        return value < threshold
    if operator == "lte":
        return value <= threshold
    if operator == "eq":
        return value == threshold
    # An operator this surface does not implement must not silently pass.
    return False


@dataclass(frozen=True, slots=True)
class GateConditionVerdict:
    """One gate condition's verdict against its reading and freshness bound."""

    condition_id: str
    metric: str
    state: Literal["met", "not_met", "unavailable", "stale"]
    value: Decimal | None
    threshold: Decimal | None
    observed_at: float | None
    note: str | None
    sources: tuple[str, ...]


def evaluate_gate_conditions(
    gate: Any,
    *,
    readings: Mapping[str, GateMetricReading],
    now_epoch: float,
) -> tuple[list[GateConditionVerdict], int, int, int]:
    """Score every declared condition; return (verdicts, met, unavail, stale).

    A condition is ``unavailable`` when its reading/threshold/operator is
    missing, or when it declares ``stale_after_seconds`` but the reading
    cannot be dated or is in the future (freshness unproven is not trusted).
    It is ``stale`` when the dated observation is older than that bound.
    """

    verdicts: list[GateConditionVerdict] = []
    met = 0
    unavailable = 0
    stale = 0
    for condition in gate.conditions:
        reading = readings.get(condition.metric)
        try:
            threshold = (
                None
                if condition.threshold is None
                else Decimal(str(condition.threshold))
            )
        except (InvalidOperation, TypeError, ValueError):
            threshold = None
        value = reading.value if reading is not None else None
        observed_at = reading.observed_at if reading is not None else None
        stale_after = getattr(condition, "stale_after_seconds", None)
        note = (
            reading.note
            if reading is not None
            else "이 지표를 읽는 소스가 배선돼 있지 않습니다."
        )
        if value is None or threshold is None or not condition.operator:
            state = "unavailable"
            unavailable += 1
        elif stale_after is not None and (
            observed_at is None
            or observed_at > now_epoch
            or now_epoch - observed_at > stale_after
        ):
            if observed_at is None or observed_at > now_epoch:
                state = "unavailable"
                unavailable += 1
                if observed_at is not None:
                    note = "관측 시간이 미래이므로 신선도를 증명할 수 없습니다."
            else:
                state = "stale"
                stale += 1
                note = "입력이 stale입니다 — 신선한 관측이 확인될 때까지 판정 보류."
        else:
            passed = _compare(condition.operator, value, threshold)
            state = "met" if passed else "not_met"
            if passed:
                met += 1
        verdicts.append(
            GateConditionVerdict(
                condition_id=condition.id,
                metric=condition.metric,
                state=state,
                value=value,
                threshold=threshold,
                observed_at=observed_at,
                note=note,
                sources=tuple(condition.sources or ()),
            )
        )
    return verdicts, met, unavailable, stale


def resolve_market_state_coefficient(
    spec: Any, *, met_count: int, unresolved_count: int
) -> tuple[Literal["resolved", "hold"], Decimal | None]:
    """C1 (task-792): m by met count; hold whenever an input is missing/stale.

    The met count alone never produces an inferred verdict — any unreadable
    or stale input keeps the decision at ``hold`` regardless of how many
    other conditions happened to pass.
    """

    if unresolved_count:
        return "hold", None
    arm = spec.by_met_count.get(met_count)
    if arm is None:
        return "hold", None
    return "resolved", _to_decimal(arm)


async def read_alt_breadth_24h(*, now: float | None = None) -> GateMetricReading:
    """Percent of KRW alts outperforming BTC over 24h, or ``None``."""

    return await _cached(
        "alt_breadth_24h", _read_alt_breadth_24h, now=now or time.monotonic()
    )


async def _read_alt_breadth_24h() -> GateMetricReading:
    try:
        from app.services.external.upbit_index import fetch_upbit_altseason

        payload = await fetch_upbit_altseason()
    except Exception as exc:  # noqa: BLE001 — fail-open by contract
        logger.warning("buy_plan: alt breadth unavailable: %s", exc)
        return GateMetricReading(
            metric="upbit_alt_breadth_24h",
            value=None,
            source=ALT_BREADTH_SOURCE,
            note=f"조회 실패: {exc}",
        )
    return parse_alt_breadth_reading(payload)


async def read_btc_long_short_ratio(*, now: float | None = None) -> GateMetricReading:
    """The worse (higher) of the two Binance long/short legs, or ``None``."""

    return await _cached(
        "btc_long_short_ratio",
        _read_btc_long_short_ratio,
        now=now or time.monotonic(),
    )


async def _read_btc_long_short_ratio() -> GateMetricReading:
    try:
        from app.mcp_server.tooling.fundamentals._crypto import (
            handle_get_long_short_ratio,
        )

        payload = await handle_get_long_short_ratio("BTC", "1h", 1)
    except Exception as exc:  # noqa: BLE001 — fail-open by contract
        logger.warning("buy_plan: long/short ratio unavailable: %s", exc)
        return GateMetricReading(
            metric="btc_long_short_ratio",
            value=None,
            source=LONG_SHORT_SOURCE,
            note=f"조회 실패: {exc}",
        )
    return parse_btc_long_short_reading(payload)

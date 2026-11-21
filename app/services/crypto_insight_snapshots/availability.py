"""Cutoff-safe reads of gate-input snapshots (hk 5932, dots review B2).

A gate-input row carries ``raw_payload["timing"]`` (see the contract comment in
``app.services.external.crypto_insights``). A value is usable at a cutoff only if
both its event time and the time we first received it are at or before the cutoff,
so a value collected later can never appear in a past cutoff's view. Rows with no
readable timing are not usable: unknown is not available.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from app.services.external.crypto_insights import (
    FUNDING_PRINT_COUNT,
    TIMING_KEY,
    funding_period_seconds,
)


@dataclass(frozen=True)
class InsightTiming:
    event_at: dt.datetime
    source_at: dt.datetime | None
    first_available_at: dt.datetime


def _parse_aware(value: Any) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.astimezone(dt.UTC) if parsed.tzinfo is not None else None


def timing_of(raw_payload: dict[str, Any] | None) -> InsightTiming | None:
    """The timing record, or None when absent or not fully readable."""
    timing = raw_payload.get(TIMING_KEY) if isinstance(raw_payload, dict) else None
    if not isinstance(timing, dict):
        return None
    event_at = _parse_aware(timing.get("event_at"))
    first_available_at = _parse_aware(timing.get("first_available_at"))
    if event_at is None or first_available_at is None:
        return None
    return InsightTiming(
        event_at=event_at,
        source_at=_parse_aware(timing.get("source_at")),
        first_available_at=first_available_at,
    )


def usable_at_cutoff(raw_payload: dict[str, Any] | None, cutoff: dt.datetime) -> bool:
    if cutoff.tzinfo is None:
        raise ValueError("cutoff must be timezone-aware")
    timing = timing_of(raw_payload)
    return (
        timing is not None
        and timing.event_at <= cutoff
        and timing.first_available_at <= cutoff
    )


@dataclass(frozen=True)
class FundingWindow:
    """Exactly FUNDING_PRINT_COUNT consecutive prints, oldest first."""

    rows: tuple[Any, ...]
    settlement_period_seconds: int


@dataclass(frozen=True)
class FundingWindowResult:
    window: FundingWindow | None
    # None when a window exists; otherwise why the gate must treat the input as unknown.
    unavailable_reason: str | None = None


def select_funding_window(
    rows: Iterable[Any], cutoff: dt.datetime
) -> FundingWindowResult:
    """The latest three prints usable at ``cutoff``, or why there are not three.

    Fewer than three usable prints is "missing" (never padded, never zero); three
    usable prints that are not consecutive on one settlement period is
    "period_changed". Rows need ``raw_payload`` (a snapshot row or a payload object).
    """
    usable: list[tuple[InsightTiming, Any]] = []
    for row in rows:
        raw = getattr(row, "raw_payload", None)
        if usable_at_cutoff(raw, cutoff):
            timing = timing_of(raw)
            assert timing is not None
            usable.append((timing, row))
    if len(usable) < FUNDING_PRINT_COUNT:
        return FundingWindowResult(None, "missing")
    usable.sort(key=lambda item: item[0].event_at)
    latest = usable[-FUNDING_PRINT_COUNT:]
    periods = {
        funding_period_seconds(
            (later[0].event_at - earlier[0].event_at).total_seconds()
        )
        for earlier, later in zip(latest, latest[1:], strict=False)
    }
    if len(periods) != 1 or None in periods:
        return FundingWindowResult(None, "period_changed")
    return FundingWindowResult(
        FundingWindow(
            rows=tuple(row for _, row in latest),
            settlement_period_seconds=next(iter(periods)),
        )
    )


def filter_usable_at(rows: Sequence[Any], cutoff: dt.datetime) -> list[Any]:
    return [
        r for r in rows if usable_at_cutoff(getattr(r, "raw_payload", None), cutoff)
    ]

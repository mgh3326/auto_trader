"""Immutable ROB-1331 Q6 collection epoch and readiness clock.

The epoch is selected before the first durable B\\A record.  Its start and
28-calendar-day boundary never depend on ``first_valid_record_at``; that value
is a nullable observation only.  This module is pure and has no DB, broker,
proposal, watch, scheduler, or network surface.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Literal, cast
from zoneinfo import ZoneInfo

from app.services.buy_gate_ab_shadow.spec import (
    ACTIVATION_EPOCH_ADDENDUM,
    ACTIVATION_EPOCH_ADDENDUM_VERSION,
    EXPERIMENT_ID,
    PINNED_POLICY_PROJECTION_SHA256,
    PINNED_SPEC_SHA256,
    policy_projection_sha256,
    spec_sha256,
)

CollectionStatus = Literal[
    "COLLECTION_OPEN",
    "AWAITING_EVENT_MATURITY",
    "INSUFFICIENT_SAMPLE",
    "SCORING_READY",
]
CollectionOutcome = Literal["NO_FIRING"]

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_EPOCH_PAYLOAD = ACTIVATION_EPOCH_ADDENDUM["collection_epoch"]


class CollectionEpochError(ValueError):
    """The sealed epoch or a collection observation is invalid."""


@dataclass(frozen=True, slots=True)
class CollectionEpochMarker:
    """One immutable activation marker shared by code and the durable row."""

    experiment_id: str
    epoch_id: str
    addendum_version: str
    collection_armed_at: datetime
    collection_start: date
    collection_end_exclusive: date
    collection_calendar_days: int
    collection_clock_timezone: str
    market_session_timezones: tuple[tuple[str, str], ...]
    policy_projection_sha256: str
    preregistration_spec_sha256: str

    def __post_init__(self) -> None:
        for field, value in (
            ("experiment_id", self.experiment_id),
            ("epoch_id", self.epoch_id),
            ("addendum_version", self.addendum_version),
            ("collection_clock_timezone", self.collection_clock_timezone),
        ):
            if type(value) is not str or not value:
                raise CollectionEpochError(f"{field} must be a non-empty exact str")
        if self.collection_armed_at.tzinfo is None:
            raise CollectionEpochError("collection_armed_at must be timezone-aware")
        if type(self.collection_calendar_days) is not int:  # bool is not an int here
            raise CollectionEpochError("collection_calendar_days must be an exact int")
        if self.collection_calendar_days != 28:
            raise CollectionEpochError("collection epoch must be exactly 28 days")
        expected_end = self.collection_start + timedelta(
            days=self.collection_calendar_days
        )
        if self.collection_end_exclusive != expected_end:
            raise CollectionEpochError(
                "collection_end_exclusive must be start plus 28 calendar days"
            )
        clock = ZoneInfo(self.collection_clock_timezone)
        armed_date = self.collection_armed_at.astimezone(clock).date()
        if self.collection_start <= armed_date:
            raise CollectionEpochError(
                "collection_start must be after collection_armed_at"
            )
        markets = dict(self.market_session_timezones)
        if tuple(markets) != ("kr", "us"):
            raise CollectionEpochError("market session timezones must be exact kr/us")
        for timezone_name in markets.values():
            ZoneInfo(timezone_name)
        for field, value in (
            ("policy_projection_sha256", self.policy_projection_sha256),
            ("preregistration_spec_sha256", self.preregistration_spec_sha256),
        ):
            if _SHA256_RE.fullmatch(value) is None:
                raise CollectionEpochError(f"{field} must be lowercase SHA-256")

    @property
    def collection_last_date(self) -> date:
        return self.collection_end_exclusive - timedelta(days=1)

    def session_date(self, market: str, observed_at: datetime) -> date:
        """Return an event's market-local session label date."""

        if observed_at.tzinfo is None:
            raise CollectionEpochError("observed_at must be timezone-aware")
        timezones = dict(self.market_session_timezones)
        if market not in timezones:
            raise CollectionEpochError("market must be kr or us")
        return observed_at.astimezone(ZoneInfo(timezones[market])).date()

    def contains_event(self, *, market: str, observed_at: datetime) -> bool:
        session_date = self.session_date(market, observed_at)
        return self.collection_start <= session_date < self.collection_end_exclusive

    def as_dict(self) -> dict[str, Any]:
        return {
            "experiment_id": self.experiment_id,
            "epoch_id": self.epoch_id,
            "addendum_version": self.addendum_version,
            "collection_armed_at": self.collection_armed_at.isoformat(),
            "collection_start": self.collection_start.isoformat(),
            "collection_last_date": self.collection_last_date.isoformat(),
            "collection_end_exclusive": self.collection_end_exclusive.isoformat(),
            "collection_calendar_days": self.collection_calendar_days,
            "collection_clock_timezone": self.collection_clock_timezone,
            "market_session_timezones": dict(self.market_session_timezones),
            "policy_projection_sha256": self.policy_projection_sha256,
            "preregistration_spec_sha256": self.preregistration_spec_sha256,
        }


COLLECTION_EPOCH = CollectionEpochMarker(
    experiment_id=EXPERIMENT_ID,
    epoch_id=cast(str, _EPOCH_PAYLOAD["epoch_id"]),
    addendum_version=ACTIVATION_EPOCH_ADDENDUM_VERSION,
    collection_armed_at=datetime.fromisoformat(
        cast(str, _EPOCH_PAYLOAD["collection_armed_at"])
    ),
    collection_start=date.fromisoformat(cast(str, _EPOCH_PAYLOAD["collection_start"])),
    collection_end_exclusive=date.fromisoformat(
        cast(str, _EPOCH_PAYLOAD["collection_end_exclusive"])
    ),
    collection_calendar_days=cast(int, _EPOCH_PAYLOAD["collection_calendar_days"]),
    collection_clock_timezone=cast(str, _EPOCH_PAYLOAD["collection_clock_timezone"]),
    market_session_timezones=tuple(
        cast(dict[str, str], _EPOCH_PAYLOAD["market_session_timezones"]).items()
    ),
    policy_projection_sha256=PINNED_POLICY_PROJECTION_SHA256,
    preregistration_spec_sha256=PINNED_SPEC_SHA256,
)


def assert_epoch_seal(marker: CollectionEpochMarker = COLLECTION_EPOCH) -> None:
    """Fail closed if any spec, projection, or marker identity drifted."""

    if policy_projection_sha256() != PINNED_POLICY_PROJECTION_SHA256:
        raise CollectionEpochError("policy projection hash does not match its pin")
    if spec_sha256() != PINNED_SPEC_SHA256:
        raise CollectionEpochError("pre-registration hash does not match its pin")
    if marker != COLLECTION_EPOCH:
        raise CollectionEpochError("collection epoch differs from the armed marker")
    if (
        ACTIVATION_EPOCH_ADDENDUM["policy_projection_sha256"]
        != marker.policy_projection_sha256
    ):
        raise CollectionEpochError("addendum policy projection seal mismatch")


@dataclass(frozen=True, slots=True)
class CollectionReadiness:
    """Projection of the fixed clock plus nullable record observations."""

    as_of: datetime
    first_valid_record_at: datetime | None
    event_count: int
    collection_window_closed: bool
    all_events_matured: bool
    scoring_ready: bool
    status: CollectionStatus
    outcome: CollectionOutcome | None

    def as_dict(self) -> dict[str, Any]:
        return {
            **COLLECTION_EPOCH.as_dict(),
            "as_of": self.as_of.isoformat(),
            "first_valid_record_at": (
                None
                if self.first_valid_record_at is None
                else self.first_valid_record_at.isoformat()
            ),
            "first_valid_record_role": "nullable_observation_only_not_a_boundary",
            "event_count": self.event_count,
            "collection_window_closed": self.collection_window_closed,
            "collection_complete": self.collection_window_closed,
            "all_events_matured": self.all_events_matured,
            "scoring_ready": self.scoring_ready,
            "status": self.status,
            "outcome": self.outcome,
        }


def assess_collection_readiness(
    *,
    as_of: datetime,
    event_count: int,
    all_events_matured: bool,
    first_valid_record_at: datetime | None = None,
    marker: CollectionEpochMarker = COLLECTION_EPOCH,
) -> CollectionReadiness:
    """Apply the verdict clock without using first-record timing as a boundary."""

    assert_epoch_seal(marker)
    if as_of.tzinfo is None:
        raise CollectionEpochError("as_of must be timezone-aware")
    if first_valid_record_at is not None and first_valid_record_at.tzinfo is None:
        raise CollectionEpochError("first_valid_record_at must be timezone-aware")
    if type(event_count) is not int or event_count < 0:
        raise CollectionEpochError("event_count must be a non-negative exact int")
    if type(all_events_matured) is not bool:
        raise CollectionEpochError("all_events_matured must be an exact bool")

    clock = ZoneInfo(marker.collection_clock_timezone)
    window_closed = as_of.astimezone(clock).date() >= marker.collection_end_exclusive
    # Universal quantification over an empty event set is true.  This is what
    # lets a zero-firing collection close instead of waiting forever.
    matured = True if event_count == 0 else all_events_matured
    scoring_ready = window_closed and matured
    if not window_closed:
        status: CollectionStatus = "COLLECTION_OPEN"
        outcome: CollectionOutcome | None = None
    elif not matured:
        status = "AWAITING_EVENT_MATURITY"
        outcome = None
    elif event_count == 0:
        status = "INSUFFICIENT_SAMPLE"
        outcome = "NO_FIRING"
    else:
        status = "SCORING_READY"
        outcome = None
    return CollectionReadiness(
        as_of=as_of,
        first_valid_record_at=first_valid_record_at,
        event_count=event_count,
        collection_window_closed=window_closed,
        all_events_matured=matured,
        scoring_ready=scoring_ready,
        status=status,
        outcome=outcome,
    )


__all__ = [
    "COLLECTION_EPOCH",
    "CollectionEpochError",
    "CollectionEpochMarker",
    "CollectionReadiness",
    "assert_epoch_seal",
    "assess_collection_readiness",
]

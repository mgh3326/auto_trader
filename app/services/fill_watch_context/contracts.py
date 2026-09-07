"""Strict, supplied-artifact contract for Phase 0 context consumption.

The existing lane transport carries a ``lane.event`` envelope.  Context is a
separate supplied artifact because this Phase 0 consumer intentionally does
not fetch accounts, brokers, markets, watches, or routes on its own.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "ContextArtifact",
    "ContextSnapshot",
    "LaneEventEnvelope",
    "RouteSnapshot",
    "SnapshotFreshness",
    "WatchContextSnapshot",
]


class _StrictArtifactModel(BaseModel):
    """Reject fields the documented artifact boundary does not own."""

    model_config = ConfigDict(extra="forbid", frozen=True)


def _require_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value


class LaneEventEnvelope(_StrictArtifactModel):
    """The producer-facing subset of the existing ``lane.event`` shape."""

    kind: Literal["lane.event"]
    lane: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
    )
    event_id: str = Field(min_length=1, max_length=512)
    text: str = Field(min_length=1, max_length=2048)
    label: str | None = Field(default=None, max_length=256)
    host: str | None = Field(default=None, max_length=256)
    pane: str | None = Field(default=None, max_length=256)

    @field_validator("event_id")
    @classmethod
    def _require_canonical_uuid(cls, value: str) -> str:
        """Keep the persistent dedupe identity exact, not merely UUID-like."""
        try:
            parsed = uuid.UUID(value)
        except ValueError as exc:
            raise ValueError("event_id must be a canonical UUID") from exc
        if str(parsed) != value:
            raise ValueError("event_id must use canonical lowercase UUID form")
        return value


class SnapshotFreshness(_StrictArtifactModel):
    """A supplied snapshot's as-of timestamp and producer-declared freshness."""

    as_of: datetime
    freshness: Literal["fresh", "stale"]

    _as_of_is_aware = field_validator("as_of")(_require_aware)


class RouteSnapshot(_StrictArtifactModel):
    """A supplied route observation; the consumer never probes a route."""

    availability: Literal["available", "unavailable"]


class WatchContextSnapshot(_StrictArtifactModel):
    """Minimal watch semantics supplied by the producer-side artifact."""

    close_condition: bool = False


class ContextSnapshot(_StrictArtifactModel):
    """Non-economic context supplied alongside one lane event."""

    event_kind: Literal["fill", "watch"]
    input_as_of: datetime
    economic_root_ref: str = Field(min_length=1, max_length=512)
    order_refs: tuple[str, ...] = Field(min_length=1, max_length=64)
    market_snapshot: SnapshotFreshness
    position_snapshot: SnapshotFreshness
    route: RouteSnapshot
    artifact_health: Literal["complete", "failed"] = "complete"
    watch: WatchContextSnapshot | None = None
    sample_origin: Literal["synthetic", "observed"] = "synthetic"

    _input_as_of_is_aware = field_validator("input_as_of")(_require_aware)

    @field_validator("economic_root_ref")
    @classmethod
    def _nonblank_root(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("economic_root_ref must not be blank")
        return value

    @field_validator("order_refs")
    @classmethod
    def _nonblank_order_refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() for item in value):
            raise ValueError("order_refs must not contain blank values")
        if len(set(value)) != len(value):
            raise ValueError("order_refs must be unique within an artifact")
        return value


class ContextArtifact(_StrictArtifactModel):
    """One transport envelope plus the context supplied for it."""

    lane_event: LaneEventEnvelope
    context: ContextSnapshot

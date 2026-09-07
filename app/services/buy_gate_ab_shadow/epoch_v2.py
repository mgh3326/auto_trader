"""Unarmed epoch-builder contract for the ROB-1351 v2 registration.

No marker is selected here.  A later operator-owned activation may call
``build_marker`` after choosing a real timestamp and the next eligible session.
"""

from __future__ import annotations

from datetime import date, datetime

from app.services.buy_gate_ab_shadow.epoch import (
    CollectionEpochError,
    CollectionEpochMarker,
)
from app.services.buy_gate_ab_shadow.spec_v2 import (
    EXPERIMENT_ID_V2,
    PINNED_POLICY_PROJECTION_SHA256_V2,
    PINNED_SPEC_SHA256_V2,
    policy_projection_sha256_v2,
    spec_sha256_v2,
)


class CollectionEpochV2Error(CollectionEpochError):
    """The unarmed v2 epoch builder or its sealed registration is invalid."""


def assert_v2_seal() -> None:
    """Fail closed if either v2 registration payload differs from its pin."""

    if spec_sha256_v2() != PINNED_SPEC_SHA256_V2:
        raise CollectionEpochV2Error("v2 pre-registration hash does not match its pin")
    if policy_projection_sha256_v2() != PINNED_POLICY_PROJECTION_SHA256_V2:
        raise CollectionEpochV2Error("v2 policy projection hash does not match its pin")


def build_marker(
    *,
    epoch_id: str,
    addendum_version: str,
    collection_armed_at: datetime,
    collection_start: date,
    collection_end_exclusive: date,
    collection_calendar_days: int,
    collection_clock_timezone: str,
    market_session_timezones: tuple[tuple[str, str], ...],
) -> CollectionEpochMarker:
    """Build a future v2 marker only after its operator-owned activation.

    The shared marker dataclass supplies the exact 28-day, post-arm-date,
    KR/US-timezone, and SHA-format contract.  This builder supplies only the
    v2 experiment identity and its two pinned hashes.
    """

    assert_v2_seal()
    try:
        return CollectionEpochMarker(
            experiment_id=EXPERIMENT_ID_V2,
            epoch_id=epoch_id,
            addendum_version=addendum_version,
            collection_armed_at=collection_armed_at,
            collection_start=collection_start,
            collection_end_exclusive=collection_end_exclusive,
            collection_calendar_days=collection_calendar_days,
            collection_clock_timezone=collection_clock_timezone,
            market_session_timezones=market_session_timezones,
            policy_projection_sha256=PINNED_POLICY_PROJECTION_SHA256_V2,
            preregistration_spec_sha256=PINNED_SPEC_SHA256_V2,
        )
    except CollectionEpochError as exc:
        raise CollectionEpochV2Error(str(exc)) from exc


__all__ = [
    "CollectionEpochMarker",
    "CollectionEpochV2Error",
    "assert_v2_seal",
    "build_marker",
]

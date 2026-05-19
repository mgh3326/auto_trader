# app/services/investment_snapshots/freshness.py
"""Freshness classifier for snapshot artifacts (ROB-269 Phase 1).

Pre-plan Decision 3: policy_snapshot_json is frozen per-run. Each snapshot
kind carries its own (soft_ttl, hard_ttl); this module is the deterministic
mapping from (as_of, now, policy) → status. Generator + DB CHECK consume
the result (Decision 4 three-layer stale gate).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Literal

FreshnessStatus = Literal["fresh", "soft_stale", "hard_stale", "partial", "unavailable"]

# Clock skew tolerance — collectors and DB can disagree by a few seconds.
_CLOCK_SKEW = dt.timedelta(seconds=5)


@dataclass(frozen=True)
class FreshnessPolicy:
    soft_ttl: dt.timedelta
    hard_ttl: dt.timedelta


def classify_freshness(
    *,
    as_of: dt.datetime | None,
    now: dt.datetime,
    policy: FreshnessPolicy,
) -> FreshnessStatus:
    if as_of is None:
        return "unavailable"
    if as_of.tzinfo is None or now.tzinfo is None:
        raise ValueError("classify_freshness requires tz-aware datetimes")
    if as_of > now + _CLOCK_SKEW:
        raise ValueError(f"as_of {as_of.isoformat()} is in the future of now {now.isoformat()}")
    age = max(now - as_of, dt.timedelta(0))
    if age <= policy.soft_ttl:
        return "fresh"
    if age <= policy.hard_ttl:
        return "soft_stale"
    return "hard_stale"

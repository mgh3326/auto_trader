"""ROB-1303 — daily spike cause attribution (observation-only).

Assembles material this repo already stores — news relevance (ROB-491), DART /
earnings market events (ROB-128), investor flow, sector map (ROB-512) — into a
per-spike record that says which documents *could* have caused a session move,
which could not (and why), and which materials could not be read at all.

When nothing explains the move the record says ``unattributed`` in fixed
wording. That is the point of it: an honest blank is worth more than a
plausible-sounding cause.

Two hooks hang off the record, wiring only — no scheduler, no auto-run:

* :mod:`~app.services.spike_attribution.catalyst_basis` feeds the
  ``momentum_spike_profit_ladder`` tier's ``catalyst_basis`` evidence slot, and
  cannot report sufficiency for an unattributed spike.
* :mod:`~app.services.spike_attribution.forecast_tag` /
  :mod:`~app.services.spike_attribution.scoring` pre-register per-type
  follow-through so "does an X-driven spike hold?" is scoreable later without
  re-reading the formula favourably.

Phase 2 (§120차 부기 2) adds a pre-attribution cache so a session does not
recompute at decision time. Its hazard is the mirror of stage 1's: a cold or
stale cache read as "no catalyst" is a different claim from ``unattributed``.
:mod:`~app.services.spike_attribution.cache` therefore answers every lookup with
an explicit ``fresh`` / ``stale(age)`` / ``missing`` state, a computed negative
counts as a real ``fresh`` answer, and
:mod:`~app.services.spike_attribution.precompute` never lets a failed refresh
overwrite the last good entry.

Nothing in this package writes a database row, touches a broker, reaches an
order, approval, or watch surface, or registers a schedule.
"""

from app.services.spike_attribution.attribute import (
    UNATTRIBUTED,
    UNATTRIBUTED_PHRASE,
    AttributionError,
    build_attribution,
    record_summary,
    rule_eligibility,
    scored_class,
)
from app.services.spike_attribution.cache import (
    STATE_FRESH,
    STATE_MISSING,
    STATE_STALE,
    CacheEntry,
    CacheRead,
    classify_state,
)
from app.services.spike_attribution.cache import lookup as cache_lookup
from app.services.spike_attribution.catalyst_basis import build_catalyst_basis
from app.services.spike_attribution.contract import (
    DailyBar,
    EvidenceItem,
    MaterialAvailability,
    SpikeAttribution,
    SpikeEvent,
    SpikeMaterials,
)
from app.services.spike_attribution.detect import (
    SpikeDetectionError,
    classify_bar,
    detect_spikes,
    session_close_at,
)
from app.services.spike_attribution.forecast_tag import build_prereg_forecasts
from app.services.spike_attribution.precompute import (
    PrecomputeRun,
    precompute_session,
    refresh_symbol,
)
from app.services.spike_attribution.scoring import (
    FollowThroughScore,
    aggregate_by_class,
    score_event,
)
from app.services.spike_attribution.spec import (
    ATTRIBUTION_TYPES,
    EXPERIMENT_ID,
    FORBIDDEN,
    PINNED_SPEC_SHA256,
    PRE_REGISTRATION,
    spec_sha256,
)

__all__ = [
    "ATTRIBUTION_TYPES",
    "STATE_FRESH",
    "STATE_MISSING",
    "STATE_STALE",
    "CacheEntry",
    "CacheRead",
    "PrecomputeRun",
    "cache_lookup",
    "classify_state",
    "precompute_session",
    "refresh_symbol",
    "EXPERIMENT_ID",
    "FORBIDDEN",
    "PINNED_SPEC_SHA256",
    "PRE_REGISTRATION",
    "UNATTRIBUTED",
    "UNATTRIBUTED_PHRASE",
    "AttributionError",
    "DailyBar",
    "EvidenceItem",
    "FollowThroughScore",
    "MaterialAvailability",
    "SpikeAttribution",
    "SpikeDetectionError",
    "SpikeEvent",
    "SpikeMaterials",
    "aggregate_by_class",
    "build_attribution",
    "build_catalyst_basis",
    "build_prereg_forecasts",
    "classify_bar",
    "detect_spikes",
    "record_summary",
    "rule_eligibility",
    "score_event",
    "scored_class",
    "session_close_at",
    "spec_sha256",
]

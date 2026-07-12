"""ROB-269 Phase 2 — Snapshot freshness policy constants.

The ``intraday_action_report_v1`` policy below is the only policy registered
in Phase 2. It is frozen per-run via ``run.policy_snapshot_json`` (Phase 1
Decision 3) so a later policy version change does not retroactively
invalidate historical bundles.

Pre-plan reference for TTL choices:
``docs/superpowers/plans/2026-05-19-rob-269-pre-plan.md`` §3 (Phase 3 table).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from app.services.investment_snapshots.freshness import FreshnessPolicy


@dataclass(frozen=True)
class SnapshotKindPolicy:
    """Per-kind freshness policy + collector behaviour for a snapshot bundle."""

    snapshot_kind: str
    freshness: FreshnessPolicy
    required: bool
    """Required kinds, when unavailable, force the bundle to ``failed`` or
    ``stale_fallback`` and block executable action language downstream
    (Decision 4). Optional kinds degrade to ``partial`` but never block."""

    collector_timeout: dt.timedelta
    """Bounded timeout for the collector call. Phase 2 collectors registry
    is empty by default; the timeout matters only when a caller (test or
    Phase 3 collector) actually does work."""


@dataclass(frozen=True)
class BundlePolicy:
    policy_version: str
    kinds: tuple[SnapshotKindPolicy, ...]
    bundle_ttl: FreshnessPolicy
    """Bundle-level TTL. A bundle older than ``hard_ttl`` is recreated even
    if individual kind TTLs are still soft. ``soft_ttl`` triggers an
    advisory ``soft_stale`` bundle status but still allows reuse."""

    def required_kinds(self) -> tuple[str, ...]:
        return tuple(k.snapshot_kind for k in self.kinds if k.required)

    def optional_kinds(self) -> tuple[str, ...]:
        return tuple(k.snapshot_kind for k in self.kinds if not k.required)

    def kind_policy(self, snapshot_kind: str) -> SnapshotKindPolicy | None:
        for k in self.kinds:
            if k.snapshot_kind == snapshot_kind:
                return k
        return None

    def to_snapshot_json(self) -> dict[str, object]:
        """Serialise to the JSONB shape stored on ``run.policy_snapshot_json``."""
        return {
            "policy_version": self.policy_version,
            "bundle_ttl_seconds": {
                "soft": int(self.bundle_ttl.soft_ttl.total_seconds()),
                "hard": int(self.bundle_ttl.hard_ttl.total_seconds()),
            },
            "kinds": [
                {
                    "snapshot_kind": k.snapshot_kind,
                    "required": k.required,
                    "soft_ttl_seconds": int(k.freshness.soft_ttl.total_seconds()),
                    "hard_ttl_seconds": int(k.freshness.hard_ttl.total_seconds()),
                    "collector_timeout_seconds": int(
                        k.collector_timeout.total_seconds()
                    ),
                }
                for k in self.kinds
            ],
        }


def _seconds(s: int) -> dt.timedelta:
    return dt.timedelta(seconds=s)


INTRADAY_ACTION_REPORT_V1 = BundlePolicy(
    policy_version="intraday_action_report_v1",
    bundle_ttl=FreshnessPolicy(soft_ttl=_seconds(180), hard_ttl=_seconds(300)),
    kinds=(
        # Required — failure blocks executable action language.
        SnapshotKindPolicy(
            snapshot_kind="portfolio",
            freshness=FreshnessPolicy(soft_ttl=_seconds(180), hard_ttl=_seconds(300)),
            required=True,
            collector_timeout=_seconds(10),
        ),
        SnapshotKindPolicy(
            snapshot_kind="journal",
            freshness=FreshnessPolicy(soft_ttl=_seconds(300), hard_ttl=_seconds(900)),
            required=True,
            collector_timeout=_seconds(5),
        ),
        SnapshotKindPolicy(
            snapshot_kind="watch_context",
            freshness=FreshnessPolicy(soft_ttl=_seconds(300), hard_ttl=_seconds(900)),
            required=True,
            collector_timeout=_seconds(5),
        ),
        SnapshotKindPolicy(
            snapshot_kind="market",
            freshness=FreshnessPolicy(soft_ttl=_seconds(180), hard_ttl=_seconds(600)),
            required=True,
            collector_timeout=_seconds(10),
        ),
        # Optional — failure degrades bundle to 'partial', does not block.
        SnapshotKindPolicy(
            snapshot_kind="symbol",
            freshness=FreshnessPolicy(soft_ttl=_seconds(60), hard_ttl=_seconds(180)),
            required=False,
            collector_timeout=_seconds(5),
        ),
        SnapshotKindPolicy(
            snapshot_kind="candidate_universe",
            freshness=FreshnessPolicy(soft_ttl=_seconds(900), hard_ttl=_seconds(3600)),
            required=False,
            collector_timeout=_seconds(15),
        ),
        SnapshotKindPolicy(
            snapshot_kind="kr_market_ranking",
            freshness=FreshnessPolicy(soft_ttl=_seconds(900), hard_ttl=_seconds(3600)),
            required=False,
            collector_timeout=_seconds(15),
        ),
        SnapshotKindPolicy(
            snapshot_kind="investor_flow",
            freshness=FreshnessPolicy(soft_ttl=_seconds(900), hard_ttl=_seconds(86400)),
            required=False,
            collector_timeout=_seconds(10),
        ),
        SnapshotKindPolicy(
            snapshot_kind="news",
            freshness=FreshnessPolicy(soft_ttl=_seconds(900), hard_ttl=_seconds(7200)),
            required=False,
            collector_timeout=_seconds(10),
        ),
        SnapshotKindPolicy(
            snapshot_kind="naver_remote_debug",
            freshness=FreshnessPolicy(soft_ttl=_seconds(300), hard_ttl=_seconds(1800)),
            required=False,
            collector_timeout=_seconds(8),
        ),
        SnapshotKindPolicy(
            snapshot_kind="toss_remote_debug",
            freshness=FreshnessPolicy(soft_ttl=_seconds(300), hard_ttl=_seconds(1800)),
            required=False,
            collector_timeout=_seconds(8),
        ),
        SnapshotKindPolicy(
            snapshot_kind="browser_probe",
            freshness=FreshnessPolicy(soft_ttl=_seconds(300), hard_ttl=_seconds(1800)),
            required=False,
            collector_timeout=_seconds(8),
        ),
        SnapshotKindPolicy(
            snapshot_kind="invest_page",
            freshness=FreshnessPolicy(soft_ttl=_seconds(300), hard_ttl=_seconds(1800)),
            required=False,
            collector_timeout=_seconds(8),
        ),
        SnapshotKindPolicy(
            snapshot_kind="pending_orders",
            freshness=FreshnessPolicy(soft_ttl=_seconds(60), hard_ttl=_seconds(300)),
            required=False,
            collector_timeout=_seconds(8),
        ),
    ),
)


ANALYSIS_SNAPSHOT_BUNDLE_V1 = BundlePolicy(
    policy_version="analysis_snapshot_bundle_v1",
    bundle_ttl=FreshnessPolicy(soft_ttl=_seconds(180), hard_ttl=_seconds(300)),
    kinds=(
        SnapshotKindPolicy(
            snapshot_kind="llm_input_frozen",
            freshness=FreshnessPolicy(soft_ttl=_seconds(180), hard_ttl=_seconds(300)),
            required=True,
            collector_timeout=_seconds(60),
        ),
    ),
)


POLICIES: dict[str, BundlePolicy] = {
    INTRADAY_ACTION_REPORT_V1.policy_version: INTRADAY_ACTION_REPORT_V1,
    ANALYSIS_SNAPSHOT_BUNDLE_V1.policy_version: ANALYSIS_SNAPSHOT_BUNDLE_V1,
}


def get_policy(policy_version: str) -> BundlePolicy:
    """Return the named policy; raises ``KeyError`` if unknown."""
    try:
        return POLICIES[policy_version]
    except KeyError as exc:
        known = ", ".join(sorted(POLICIES))
        raise KeyError(
            f"unknown policy_version {policy_version!r}; known: {known}"
        ) from exc

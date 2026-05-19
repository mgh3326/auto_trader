"""ROB-269 Phase 3 — Shared critical-kind allowlist.

The four snapshot kinds whose staleness/unavailability forces the report
generator + stale gate into a degraded mode, regardless of the bundle's
overall ``status`` / ``freshness_summary['overall']`` value.

Imported by both:
* ``app.services.action_report.common.generator_constraints`` — layer (ii),
  pre-LLM directives.
* ``app.services.action_report.common.stale_gate`` — layer (iii), post-gen
  deterministic linter.

The two layers MUST agree on this allowlist. Optional kinds (news,
naver_remote_debug, toss_remote_debug, browser_probe, invest_page,
candidate_universe, symbol) being unavailable degrades the bundle to
``partial`` but does NOT block executable action language as long as the
critical kinds are present. This mirrors the Phase 2 policy in
``app/services/investment_snapshots/policy.py`` (``required`` set).

Promoting this to its own module — rather than re-exporting from one of
the gate layers — keeps both layers' dependency graph flat and signals
intent: this constant is the contract, not a private detail of either.
"""

from __future__ import annotations

# Order doesn't matter; tuple makes the allowlist explicitly immutable.
CRITICAL_SNAPSHOT_KINDS: tuple[str, ...] = (
    "portfolio",
    "journal",
    "watch_context",
    "market",
)

# Snapshot statuses that degrade a critical kind to "cannot trust executable
# action language sourced from this kind". Optional kinds may share the same
# statuses but their presence does not block.
CRITICAL_KIND_DEGRADING_STATUSES: frozenset[str] = frozenset(
    {"hard_stale", "unavailable", "failed"}
)

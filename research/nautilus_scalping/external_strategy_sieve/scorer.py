"""ROB-383 — pure scorer for the external crypto strategy sieve.

Consumes catalog metadata only (NO Phase-3 validation results — R7). Derives
scores via the frozen rubric, applies hard gate caps independently of the
composite (so a high score cannot launder a martingale into the shortlist — R5),
assigns a pre-validation disposition, and buckets cards by integrity status.
Leaderboard rank / popularity never enters scoring (R8) — it is not a card field.
"""

from __future__ import annotations

from dataclasses import dataclass

from external_strategy_sieve.rubric import (
    MAX_POSITIVE,
    MIN_COMPOSITE,
    Rubric,
    derive_scores,
)
from external_strategy_sieve.schema import CandidateCard

# Disposition ordering for "cap = the lower of two".
_DISPOSITION_RANK = {"reject": 0, "shadow_only": 1, "keep": 2}
_RANK_DISPOSITION = {v: k for k, v in _DISPOSITION_RANK.items()}


@dataclass(frozen=True)
class ScoredCandidate:
    candidate_id: str
    score_status: str
    criterion_scores: tuple[tuple[str, int], ...]
    tail_severity: int
    composite_raw: int
    composite_normalized: float
    gates_triggered: tuple[str, ...]
    disposition: str
    eligible_for_shortlist: bool
    strategy_family: str


def _lower(a: str, b: str) -> str:
    return a if _DISPOSITION_RANK[a] <= _DISPOSITION_RANK[b] else b


def score_card(card: CandidateCard, rubric: Rubric) -> ScoredCandidate:
    weights = dict(rubric.weights)
    derived = derive_scores(card)
    positive = derived["positive"]
    tail_severity = derived["tail_severity"]

    composite_raw = sum(weights[k] * v for k, v in positive.items())
    composite_raw -= rubric.tail_risk_weight * tail_severity
    composite_normalized = round(
        (composite_raw - MIN_COMPOSITE) / (MAX_POSITIVE - MIN_COMPOSITE) * 100, 1
    )

    # Band disposition from the normalized composite.
    if composite_normalized >= rubric.keep_threshold:
        band = "keep"
    elif composite_normalized >= rubric.shadow_threshold:
        band = "shadow_only"
    else:
        band = "reject"

    # Hard gate caps (applied independently of the composite).
    gates: list[str] = []
    cap = "keep"
    if positive["license_safety"] <= 1:
        gates.append("G1_license")
        cap = _lower(cap, "shadow_only")
    if card.code_availability in ("opaque", "code_not_confirmed"):
        gates.append("G2_code_opaque")
        cap = _lower(cap, "shadow_only")
    if tail_severity >= 3:
        gates.append("G3_severe_tail_risk")
        cap = _lower(cap, "shadow_only")
    if card.lookahead_repaint_risk == "high":
        gates.append("G4_repaint")
        cap = _lower(cap, "shadow_only")
    if card.expected_cost_sensitivity == "high":
        gates.append("G5_cost")
        cap = _lower(cap, "shadow_only")

    disposition = _lower(band, cap)

    # R1: only a source-verified, `verified`, non-rejected card is eligible.
    # keep AND shadow_only are validation candidates (shadow_only = clean-room /
    # cost-caveat path) — RUBRIC_VERSION v2 rationale in the runbook. Public crypto
    # strategies are almost all GPL/unclear-license (G1) or cost-sensitive (G5), so
    # a keep-only shortlist would be structurally empty for this domain.
    eligible = (
        card.source_verified
        and card.score_status == "verified"
        and disposition != "reject"
    )

    return ScoredCandidate(
        candidate_id=card.candidate_id,
        score_status=card.score_status,
        criterion_scores=tuple(sorted(positive.items())),
        tail_severity=tail_severity,
        composite_raw=composite_raw,
        composite_normalized=composite_normalized,
        gates_triggered=tuple(gates),
        disposition=disposition,
        eligible_for_shortlist=eligible,
        strategy_family=card.strategy_family,
    )


# Map score_status -> output bucket (code_not_confirmed folds into taxonomy_only).
_STATUS_BUCKET = {
    "verified": "verified_ranked",
    "unverified_seed": "unverified_seed",
    "taxonomy_only": "taxonomy_only",
    "code_not_confirmed": "taxonomy_only",
    "source_unavailable": "source_unavailable",
    "reject": "reject",
}
_BUCKET_NAMES = (
    "verified_ranked",
    "unverified_seed",
    "taxonomy_only",
    "source_unavailable",
    "reject",
)


@dataclass(frozen=True)
class ShortlistResult:
    shortlist: tuple[ScoredCandidate, ...]
    gaps: tuple[str, ...]


def bucketize(scored: list[ScoredCandidate]) -> dict[str, list[ScoredCandidate]]:
    """Group scored cards by integrity status; verified_ranked is sorted by
    composite desc with a candidate_id ascending tie-break (R3, R6)."""
    buckets: dict[str, list[ScoredCandidate]] = {name: [] for name in _BUCKET_NAMES}
    for s in scored:
        buckets[_STATUS_BUCKET[s.score_status]].append(s)
    buckets["verified_ranked"].sort(
        key=lambda s: (-s.composite_normalized, s.candidate_id)
    )
    return buckets


def freeze_shortlist(scored: list[ScoredCandidate], rubric: Rubric) -> ShortlistResult:
    """Freeze a family-diverse shortlist drawn ONLY from source-verified keep
    candidates (R1, R4). Surfaces gaps rather than padding with near-duplicates."""
    buckets = bucketize(scored)
    pool = [s for s in buckets["verified_ranked"] if s.eligible_for_shortlist]

    gaps: list[str] = []
    if not pool:
        gaps.append(
            "No source-verified, non-rejected candidates available for shortlist."
        )
        return ShortlistResult(shortlist=(), gaps=tuple(gaps))

    shortlist: list[ScoredCandidate] = []
    family_counts: dict[str, int] = {}
    for s in pool:  # already sorted desc by composite
        if len(shortlist) >= rubric.shortlist_max:
            break
        if family_counts.get(s.strategy_family, 0) >= rubric.max_per_family:
            continue
        shortlist.append(s)
        family_counts[s.strategy_family] = family_counts.get(s.strategy_family, 0) + 1

    # R1 belt-and-suspenders: refuse if anything non-verified slipped in.
    bad = [s.candidate_id for s in shortlist if s.score_status != "verified"]
    if bad:
        raise ValueError(f"shortlist integrity violation: non-verified {bad}")

    if len(shortlist) < rubric.shortlist_min:
        gaps.append(
            f"Only {len(shortlist)} eligible candidates; shortlist_min is {rubric.shortlist_min}."
        )
    distinct = len({s.strategy_family for s in shortlist})
    if distinct < rubric.min_distinct_families:
        gaps.append(
            f"Only {distinct} distinct families in shortlist; "
            f"min_distinct_families is {rubric.min_distinct_families}."
        )
    return ShortlistResult(shortlist=tuple(shortlist), gaps=tuple(gaps))

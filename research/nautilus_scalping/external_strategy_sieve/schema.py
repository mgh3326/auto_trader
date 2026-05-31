"""ROB-383 — candidate-card schema for the external crypto strategy sieve.

A card holds qualitative, source-pointer metadata only — never a raw page dump.
``validate`` returns a list of human-readable errors (it does not raise) so a
whole catalog can be checked in one pass.
"""

from __future__ import annotations

from dataclasses import dataclass, field

SOURCE_BUCKETS = frozenset({
    "freqtrade_github", "large_public_bot", "tradingview",
    "quantconnect", "commercial_marketplace",
})
CODE_AVAILABILITY = frozenset({"open", "partial", "opaque", "code_not_confirmed"})
STRATEGY_FAMILIES = frozenset({
    "trend", "mean_reversion", "breakout", "atr_trail", "grid_dca",
    "market_making", "volatility", "regime_filter", "other",
})
SPOT_OR_FUTURES = frozenset({"spot", "futures", "both"})
LONG_SHORT = frozenset({"long_only", "short_only", "both"})
DATA_REQUIREMENTS = frozenset({
    "ohlcv", "funding", "oi", "orderbook", "liquidation", "fundamentals", "other",
})
TAIL_RISK_FLAGS = frozenset({
    "dca", "martingale", "grid", "unlimited_averaging", "leverage", "no_stoploss",
})
RISK_LEVELS = frozenset({"none", "low", "medium", "high"})
COMPLEXITY_LEVELS = frozenset({"low", "medium", "high"})
NOVELTY_LEVELS = frozenset({"duplicate", "adjacent", "novel"})
COST_SENSITIVITY = frozenset({"low", "medium", "high"})
SCORE_STATUSES = frozenset({
    "unverified_seed", "verified", "taxonomy_only",
    "source_unavailable", "code_not_confirmed", "reject",
})
PRE_VALIDATION_DISPOSITIONS = frozenset({"keep", "shadow_only", "reject"})

# Fields that must be non-blank for a card claiming score_status == "verified".
_VERIFIED_REQUIRED = ("source_url", "license", "code_availability", "strategy_family")


@dataclass(frozen=True)
class CandidateCard:
    candidate_id: str
    source_url: str
    source_bucket: str
    license: str
    code_availability: str
    strategy_family: str
    spot_or_futures: str
    long_short: str
    timeframe: str
    holding_horizon: str
    entry_exit_summary: str
    data_requirements: tuple[str, ...]
    tail_risk_flags: tuple[str, ...]
    lookahead_repaint_risk: str
    implementation_complexity: str
    novelty_vs_failed_families: str
    expected_cost_sensitivity: str
    source_verified: bool
    score_status: str
    recommended_disposition_pre_validation: str


def _check_enum(name: str, value: str, allowed: frozenset[str], errors: list[str]) -> None:
    if value not in allowed:
        errors.append(f"{name}={value!r} not in {sorted(allowed)}")


def validate(card: CandidateCard) -> list[str]:
    """Return a list of validation errors; empty means the card is well-formed."""
    errors: list[str] = []
    if not card.candidate_id:
        errors.append("candidate_id is blank")
    _check_enum("source_bucket", card.source_bucket, SOURCE_BUCKETS, errors)
    _check_enum("code_availability", card.code_availability, CODE_AVAILABILITY, errors)
    _check_enum("strategy_family", card.strategy_family, STRATEGY_FAMILIES, errors)
    _check_enum("spot_or_futures", card.spot_or_futures, SPOT_OR_FUTURES, errors)
    _check_enum("long_short", card.long_short, LONG_SHORT, errors)
    _check_enum("lookahead_repaint_risk", card.lookahead_repaint_risk, RISK_LEVELS, errors)
    _check_enum("implementation_complexity", card.implementation_complexity, COMPLEXITY_LEVELS, errors)
    _check_enum("novelty_vs_failed_families", card.novelty_vs_failed_families, NOVELTY_LEVELS, errors)
    _check_enum("expected_cost_sensitivity", card.expected_cost_sensitivity, COST_SENSITIVITY, errors)
    _check_enum("score_status", card.score_status, SCORE_STATUSES, errors)
    _check_enum(
        "recommended_disposition_pre_validation",
        card.recommended_disposition_pre_validation,
        PRE_VALIDATION_DISPOSITIONS,
        errors,
    )
    for req in card.data_requirements:
        if req not in DATA_REQUIREMENTS:
            errors.append(f"data_requirements has {req!r} not in {sorted(DATA_REQUIREMENTS)}")
    for flag in card.tail_risk_flags:
        if flag not in TAIL_RISK_FLAGS:
            errors.append(f"tail_risk_flags has {flag!r} not in {sorted(TAIL_RISK_FLAGS)}")
    # R2: a card claiming `verified` must carry the evidence fields.
    if card.score_status == "verified":
        for fname in _VERIFIED_REQUIRED:
            if not getattr(card, fname):
                errors.append(f"{fname} is blank but score_status is verified")
        if not card.source_verified:
            errors.append("source_verified is False but score_status is verified")
    return errors

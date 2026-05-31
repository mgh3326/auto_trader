"""ROB-383 — frozen scoring rubric for the external crypto strategy sieve.

Weights, gate rules, disposition thresholds, and the metadata->score derivation
tables are committed HERE before any Phase-3 validation result exists. ``RUBRIC``
carries a ``config_hash()`` (mirroring ``frozen_config.py``); a later tweak
changes the hash, so an ex-post adjustment is detectable, not silent. Scores are
DERIVED from observable card metadata — not hand-assigned numbers — so the same
catalog always yields the same scores.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass

from external_strategy_sieve.schema import CandidateCard

RUBRIC_VERSION = "rob383.sieve.v1"

# Positive criteria and their weights.
WEIGHTS: dict[str, int] = {
    "source_hygiene_reproducibility": 3,
    "license_safety": 3,
    "faithful_port_feasibility": 3,
    "data_availability_auto_trader": 3,
    "cost_fee_survivability_potential": 3,
    "market_fit_binance_demo": 2,
    "novelty_vs_failed_families": 2,
    "expected_daily_review_usefulness": 1,
}

# Derivation tables (metadata -> 0..3).
_CODE_AVAIL_SCORE = {"open": 3, "partial": 2, "opaque": 1, "code_not_confirmed": 0}
_LICENSE_SCORE = {
    "permissive": 3,
    "weak_copyleft": 2,
    "strong_copyleft": 1,
    "unknown": 0,
}
_COST_SCORE = {"low": 3, "medium": 2, "high": 0}
_NOVELTY_SCORE = {"duplicate": 0, "adjacent": 2, "novel": 3}
_MARKET_FIT = {"both": 3, "futures": 3, "spot": 2}
_COMPLEXITY_PENALTY = {"low": 0, "medium": 1, "high": 2}
_REPAINT_PENALTY = {"none": 0, "low": 0, "medium": 1, "high": 1}
_TAIL_SEVERITY = {
    "martingale": 3,
    "unlimited_averaging": 3,
    "no_stoploss": 2,
    "dca": 2,
    "grid": 2,
    "leverage": 1,
}

# Data buckets available inside auto_trader (klines + ROB-356 funding/oi archive).
_AVAILABLE_DATA = frozenset({"ohlcv", "funding", "oi"})
_PARTIAL_DATA = frozenset({"orderbook"})  # aggTrades give partial microstructure

# License keyword classification (checked in priority order).
_LICENSE_KEYWORDS = (
    ("weak_copyleft", ("lgpl", "mpl", "epl", "cddl")),
    ("strong_copyleft", ("agpl", "gpl")),
    ("permissive", ("mit", "bsd", "apache", "isc", "unlicense", "wtfpl", "zlib")),
)


def classify_license(license_str: str) -> str:
    s = (license_str or "").lower()
    for category, keywords in _LICENSE_KEYWORDS:
        if any(k in s for k in keywords):
            return category
    return "unknown"


def _horizon_score(holding_horizon: str) -> int:
    s = (holding_horizon or "").lower()
    if any(k in s for k in ("scalp", "intraday", "minute", "min", "hour", "hr")):
        return 3
    if "day" in s:
        return 2
    if any(k in s for k in ("swing", "week")):
        return 1
    return 0  # position / month / unknown


def _data_availability_score(data_requirements: tuple[str, ...]) -> int:
    reqs = set(data_requirements)
    if reqs <= {"ohlcv"}:
        return 3
    if reqs <= _AVAILABLE_DATA:
        return 2
    if reqs <= (_AVAILABLE_DATA | _PARTIAL_DATA):
        return 1
    return 0  # needs liquidation / fundamentals / other not held by auto_trader


def derive_scores(card: CandidateCard) -> dict:
    """Derive the 8 positive criterion scores and the tail-risk severity."""
    port = (
        _CODE_AVAIL_SCORE[card.code_availability]
        - _COMPLEXITY_PENALTY[card.implementation_complexity]
        - _REPAINT_PENALTY[card.lookahead_repaint_risk]
    )
    positive = {
        "source_hygiene_reproducibility": _CODE_AVAIL_SCORE[card.code_availability],
        "license_safety": _LICENSE_SCORE[classify_license(card.license)],
        "faithful_port_feasibility": max(0, port),
        "data_availability_auto_trader": _data_availability_score(
            card.data_requirements
        ),
        "cost_fee_survivability_potential": _COST_SCORE[card.expected_cost_sensitivity],
        "market_fit_binance_demo": _MARKET_FIT[card.spot_or_futures],
        "novelty_vs_failed_families": _NOVELTY_SCORE[card.novelty_vs_failed_families],
        "expected_daily_review_usefulness": _horizon_score(card.holding_horizon),
    }
    tail_severity = max(
        (_TAIL_SEVERITY.get(f, 0) for f in card.tail_risk_flags), default=0
    )
    return {"positive": positive, "tail_severity": tail_severity}


@dataclass(frozen=True)
class Rubric:
    version: str = RUBRIC_VERSION
    weights: tuple[tuple[str, int], ...] = tuple(sorted(WEIGHTS.items()))
    tail_risk_weight: int = 3
    keep_threshold: float = 65.0
    shadow_threshold: float = 45.0
    max_per_family: int = 2
    min_distinct_families: int = 3
    shortlist_min: int = 6
    shortlist_max: int = 8
    code_avail_score: tuple[tuple[str, int], ...] = tuple(
        sorted(_CODE_AVAIL_SCORE.items())
    )
    license_score: tuple[tuple[str, int], ...] = tuple(sorted(_LICENSE_SCORE.items()))
    cost_score: tuple[tuple[str, int], ...] = tuple(sorted(_COST_SCORE.items()))
    novelty_score: tuple[tuple[str, int], ...] = tuple(sorted(_NOVELTY_SCORE.items()))
    market_fit: tuple[tuple[str, int], ...] = tuple(sorted(_MARKET_FIT.items()))
    tail_severity: tuple[tuple[str, int], ...] = tuple(sorted(_TAIL_SEVERITY.items()))

    def to_dict(self) -> dict:
        d = asdict(self)
        for k, v in d.items():
            if isinstance(v, list):
                d[k] = [
                    list(item) if isinstance(item, (list, tuple)) else item
                    for item in v
                ]
        return d

    def config_hash(self) -> str:
        """SHA-256 over the sorted-key JSON of the rubric (reproducible)."""
        return hashlib.sha256(
            json.dumps(self.to_dict(), sort_keys=True).encode()
        ).hexdigest()


# The frozen rubric committed before any Phase-3 validation read exists.
RUBRIC = Rubric()

# Maximum achievable positive weighted sum (all criteria at 3): used to normalise.
MAX_POSITIVE = sum(w for _, w in RUBRIC.weights) * 3  # = 60
MIN_COMPOSITE = -RUBRIC.tail_risk_weight * 3  # = -9

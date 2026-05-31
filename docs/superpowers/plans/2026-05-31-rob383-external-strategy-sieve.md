# External Crypto Strategy Sieve (ROB-383 Phase 1–2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the deterministic, web-free infrastructure of an external crypto strategy sieve — candidate-card schema, frozen scoring rubric, pure stdlib scorer (score → disposition → bucketed output → shortlist freeze), a seed catalog, tests, a survey-plan handoff, and a runbook skeleton.

**Architecture:** A small pure-stdlib package `research/nautilus_scalping/external_strategy_sieve/`. `schema.py` defines the candidate card and validation. `rubric.py` holds the frozen weights/gates/thresholds/derivation tables with a `config_hash()` freeze guard. `scorer.py` derives 0–3 criterion scores from card metadata, computes a weighted composite minus a tail-risk penalty, applies hard gate caps, assigns a pre-validation disposition, buckets cards by integrity status, and freezes a family-diverse shortlist drawn ONLY from source-verified candidates. `catalog.py` loads a JSON catalog with integrity guards. No network, no `app` import, runnable with `uv run --no-project`.

**Tech Stack:** Python 3.13 stdlib only (`dataclasses`, `enum`/frozensets, `hashlib`, `json`), pytest. JSON (not YAML — PyYAML is absent from the research venv).

**Spec:** `docs/superpowers/specs/2026-05-31-rob383-external-strategy-sieve-design.md`

**Working dir for all commands:** `research/nautilus_scalping/` (the rootdir `conftest.py` puts this dir + repo root on `sys.path`). Run tests with `uv run --no-project pytest ...`.

---

## File structure

| File | Responsibility |
|------|----------------|
| `external_strategy_sieve/__init__.py` | package marker (empty) |
| `external_strategy_sieve/schema.py` | enums (allowed-value frozensets), `CandidateCard` frozen dataclass, `validate(card) -> list[str]` |
| `external_strategy_sieve/rubric.py` | frozen `Rubric` (weights, gate flags, thresholds, derivation tables), `RUBRIC_VERSION`, `config_hash()`, `classify_license()`, `derive_scores(card)` |
| `external_strategy_sieve/scorer.py` | `score_card(card, rubric) -> ScoredCandidate`, `bucketize(scored) -> dict`, `freeze_shortlist(scored, rubric) -> ShortlistResult` |
| `external_strategy_sieve/catalog.py` | `load_catalog(path) -> CatalogLoad` (JSON → cards + errors, duplicate-id guard) |
| `external_strategy_sieve/candidates.json` | seed 8–12 cards, all `source_verified=false` / `score_status="unverified_seed"` |
| `external_strategy_sieve/tests/test_schema.py` | schema validation behavior |
| `external_strategy_sieve/tests/test_rubric.py` | derivation tables, hash freeze guard |
| `external_strategy_sieve/tests/test_scorer.py` | composite/gates/disposition, bucketize, shortlist + integrity rules R1/R4/R5/R6/R8 |
| `external_strategy_sieve/tests/test_catalog.py` | catalog load + dup-id guard + seed-catalog integrity (R1 end-to-end) |
| `docs/plans/ROB-383-external-strategy-survey-plan.md` | read-only web-survey handoff for codex/gemini/agy |
| `docs/runbooks/external-crypto-strategy-sieve.md` | human report + safety boundaries + rubric pre-registration record |

---

## Task 1: Package scaffold + candidate-card schema

**Files:**
- Create: `external_strategy_sieve/__init__.py`
- Create: `external_strategy_sieve/schema.py`
- Test: `external_strategy_sieve/tests/__init__.py`, `external_strategy_sieve/tests/test_schema.py`

- [ ] **Step 1: Create the package marker and test package marker**

Create `external_strategy_sieve/__init__.py`:

```python
"""ROB-383 — external crypto strategy sieve (Phase 1–2, pure stdlib)."""
```

Create `external_strategy_sieve/tests/__init__.py`:

```python
```

- [ ] **Step 2: Write the failing schema test**

Create `external_strategy_sieve/tests/test_schema.py`:

```python
from external_strategy_sieve.schema import CandidateCard, validate


def _good_card(**overrides):
    base = dict(
        candidate_id="freqtrade_bbrsi",
        source_url="https://github.com/freqtrade/freqtrade-strategies",
        source_bucket="freqtrade_github",
        license="GPL-3.0",
        code_availability="open",
        strategy_family="mean_reversion",
        spot_or_futures="spot",
        long_short="long_only",
        timeframe="5m",
        holding_horizon="intraday",
        entry_exit_summary="BB lower touch + RSI<30 entry, BB mid exit",
        data_requirements=("ohlcv",),
        tail_risk_flags=(),
        lookahead_repaint_risk="low",
        implementation_complexity="low",
        novelty_vs_failed_families="adjacent",
        expected_cost_sensitivity="high",
        source_verified=False,
        score_status="unverified_seed",
        recommended_disposition_pre_validation="shadow_only",
    )
    base.update(overrides)
    return CandidateCard(**base)


def test_good_card_validates_clean():
    assert validate(_good_card()) == []


def test_bad_enum_is_reported():
    errors = validate(_good_card(source_bucket="reddit"))
    assert any("source_bucket" in e for e in errors)


def test_bad_data_requirement_is_reported():
    errors = validate(_good_card(data_requirements=("ohlcv", "twitter")))
    assert any("data_requirements" in e for e in errors)


def test_verified_card_missing_source_url_is_reported():
    # R2: a `verified` card must carry the evidence fields.
    errors = validate(_good_card(score_status="verified", source_verified=True, source_url=""))
    assert any("source_url" in e and "verified" in e for e in errors)


def test_unverified_seed_with_blank_url_is_allowed():
    # Seeds may carry a tentative pointer; they are simply not promotable yet.
    assert validate(_good_card(source_url="")) == []
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `uv run --no-project pytest external_strategy_sieve/tests/test_schema.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'external_strategy_sieve.schema'`

- [ ] **Step 4: Implement `schema.py`**

Create `external_strategy_sieve/schema.py`:

```python
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
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `uv run --no-project pytest external_strategy_sieve/tests/test_schema.py -q`
Expected: PASS (5 passed)

- [ ] **Step 6: Commit**

```bash
git add research/nautilus_scalping/external_strategy_sieve/__init__.py \
        research/nautilus_scalping/external_strategy_sieve/schema.py \
        research/nautilus_scalping/external_strategy_sieve/tests/__init__.py \
        research/nautilus_scalping/external_strategy_sieve/tests/test_schema.py
git commit -m "feat(ROB-383): candidate-card schema + validation for strategy sieve

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: Frozen scoring rubric + metadata→score derivation

**Files:**
- Create: `external_strategy_sieve/rubric.py`
- Test: `external_strategy_sieve/tests/test_rubric.py`

- [ ] **Step 1: Write the failing rubric test**

Create `external_strategy_sieve/tests/test_rubric.py`:

```python
import dataclasses

from external_strategy_sieve.rubric import RUBRIC, classify_license, derive_scores
from external_strategy_sieve.tests.test_schema import _good_card


def test_classify_license():
    assert classify_license("MIT") == "permissive"
    assert classify_license("Apache-2.0") == "permissive"
    assert classify_license("GPL-3.0") == "strong_copyleft"
    assert classify_license("AGPLv3") == "strong_copyleft"
    assert classify_license("LGPL-2.1") == "weak_copyleft"
    assert classify_license("proprietary, source hidden") == "unknown"
    assert classify_license("") == "unknown"


def test_derive_scores_open_ohlcv_card():
    scores = derive_scores(_good_card(
        license="MIT", code_availability="open",
        data_requirements=("ohlcv",), expected_cost_sensitivity="low",
        spot_or_futures="both", novelty_vs_failed_families="novel",
        holding_horizon="intraday", implementation_complexity="low",
        lookahead_repaint_risk="none", tail_risk_flags=(),
    ))
    assert scores["positive"]["license_safety"] == 3
    assert scores["positive"]["source_hygiene_reproducibility"] == 3
    assert scores["positive"]["faithful_port_feasibility"] == 3
    assert scores["positive"]["data_availability_auto_trader"] == 3
    assert scores["positive"]["cost_fee_survivability_potential"] == 3
    assert scores["positive"]["market_fit_binance_demo"] == 3
    assert scores["positive"]["novelty_vs_failed_families"] == 3
    assert scores["positive"]["expected_daily_review_usefulness"] == 3
    assert scores["tail_severity"] == 0


def test_derive_scores_penalises_complexity_and_repaint_for_port():
    scores = derive_scores(_good_card(
        code_availability="open", implementation_complexity="high",
        lookahead_repaint_risk="high",
    ))
    # open(3) - complexity_high(2) - repaint_high(1) floored at 0
    assert scores["positive"]["faithful_port_feasibility"] == 0


def test_derive_scores_data_availability_tiers():
    ohlcv_only = derive_scores(_good_card(data_requirements=("ohlcv",)))
    funding_oi = derive_scores(_good_card(data_requirements=("ohlcv", "funding", "oi")))
    orderbook = derive_scores(_good_card(data_requirements=("ohlcv", "orderbook")))
    fundamentals = derive_scores(_good_card(data_requirements=("ohlcv", "fundamentals")))
    assert ohlcv_only["positive"]["data_availability_auto_trader"] == 3
    assert funding_oi["positive"]["data_availability_auto_trader"] == 2
    assert orderbook["positive"]["data_availability_auto_trader"] == 1
    assert fundamentals["positive"]["data_availability_auto_trader"] == 0


def test_tail_severity_takes_max_flag():
    s = derive_scores(_good_card(tail_risk_flags=("leverage", "martingale")))
    assert s["tail_severity"] == 3  # martingale dominates


def test_rubric_hash_is_deterministic():
    assert RUBRIC.config_hash() == RUBRIC.config_hash()


def test_rubric_hash_changes_when_a_weight_is_tweaked():
    tweaked = dataclasses.replace(RUBRIC, keep_threshold=70.0)
    assert tweaked.config_hash() != RUBRIC.config_hash()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run --no-project pytest external_strategy_sieve/tests/test_rubric.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'external_strategy_sieve.rubric'`

- [ ] **Step 3: Implement `rubric.py`**

Create `external_strategy_sieve/rubric.py`:

```python
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
from dataclasses import asdict, dataclass, field

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
_LICENSE_SCORE = {"permissive": 3, "weak_copyleft": 2, "strong_copyleft": 1, "unknown": 0}
_COST_SCORE = {"low": 3, "medium": 2, "high": 0}
_NOVELTY_SCORE = {"duplicate": 0, "adjacent": 2, "novel": 3}
_MARKET_FIT = {"both": 3, "futures": 3, "spot": 2}
_COMPLEXITY_PENALTY = {"low": 0, "medium": 1, "high": 2}
_REPAINT_PENALTY = {"none": 0, "low": 0, "medium": 1, "high": 1}
_TAIL_SEVERITY = {
    "martingale": 3, "unlimited_averaging": 3,
    "no_stoploss": 2, "dca": 2, "grid": 2,
    "leverage": 1,
}

# Data buckets available inside auto_trader (klines + ROB-356 funding/oi archive).
_AVAILABLE_DATA = frozenset({"ohlcv", "funding", "oi"})
_PARTIAL_DATA = frozenset({"orderbook"})  # aggTrades give partial microstructure

# License keyword classification (checked in priority order).
_LICENSE_KEYWORDS = (
    ("strong_copyleft", ("agpl", "gpl")),
    ("weak_copyleft", ("lgpl", "mpl", "epl", "cddl")),
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
        "data_availability_auto_trader": _data_availability_score(card.data_requirements),
        "cost_fee_survivability_potential": _COST_SCORE[card.expected_cost_sensitivity],
        "market_fit_binance_demo": _MARKET_FIT[card.spot_or_futures],
        "novelty_vs_failed_families": _NOVELTY_SCORE[card.novelty_vs_failed_families],
        "expected_daily_review_usefulness": _horizon_score(card.holding_horizon),
    }
    tail_severity = max((_TAIL_SEVERITY.get(f, 0) for f in card.tail_risk_flags), default=0)
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
    code_avail_score: tuple[tuple[str, int], ...] = tuple(sorted(_CODE_AVAIL_SCORE.items()))
    license_score: tuple[tuple[str, int], ...] = tuple(sorted(_LICENSE_SCORE.items()))
    cost_score: tuple[tuple[str, int], ...] = tuple(sorted(_COST_SCORE.items()))
    novelty_score: tuple[tuple[str, int], ...] = tuple(sorted(_NOVELTY_SCORE.items()))
    market_fit: tuple[tuple[str, int], ...] = tuple(sorted(_MARKET_FIT.items()))
    tail_severity: tuple[tuple[str, int], ...] = tuple(sorted(_TAIL_SEVERITY.items()))

    def to_dict(self) -> dict:
        d = asdict(self)
        for k, v in d.items():
            if isinstance(v, list):
                d[k] = [list(item) if isinstance(item, (list, tuple)) else item for item in v]
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
MIN_COMPOSITE = -RUBRIC.tail_risk_weight * 3          # = -9
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run --no-project pytest external_strategy_sieve/tests/test_rubric.py -q`
Expected: PASS (7 passed)

- [ ] **Step 5: Record the frozen rubric hash for the runbook**

Run: `uv run --no-project python -c "from external_strategy_sieve.rubric import RUBRIC, RUBRIC_VERSION; print(RUBRIC_VERSION, RUBRIC.config_hash())"`
Expected: prints `rob383.sieve.v1 <64-hex-hash>`. Keep this value — it is pasted into the runbook in Task 7 as the pre-registration record.

- [ ] **Step 6: Commit**

```bash
git add research/nautilus_scalping/external_strategy_sieve/rubric.py \
        research/nautilus_scalping/external_strategy_sieve/tests/test_rubric.py
git commit -m "feat(ROB-383): frozen scoring rubric + metadata-derived scores

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: Scorer — composite, gates, disposition, eligibility

**Files:**
- Create: `external_strategy_sieve/scorer.py`
- Test: `external_strategy_sieve/tests/test_scorer.py`

- [ ] **Step 1: Write the failing scorer test (per-card scoring)**

Create `external_strategy_sieve/tests/test_scorer.py`:

```python
from external_strategy_sieve.scorer import score_card
from external_strategy_sieve.rubric import RUBRIC
from external_strategy_sieve.tests.test_schema import _good_card


def _strong_verified(**overrides):
    base = dict(
        license="MIT", code_availability="open", data_requirements=("ohlcv",),
        expected_cost_sensitivity="low", spot_or_futures="both",
        novelty_vs_failed_families="novel", holding_horizon="intraday",
        implementation_complexity="low", lookahead_repaint_risk="none",
        tail_risk_flags=(), source_verified=True, score_status="verified",
        recommended_disposition_pre_validation="keep",
    )
    base.update(overrides)
    return _good_card(**base)


def test_strong_card_scores_keep_and_is_eligible():
    s = score_card(_strong_verified(), RUBRIC)
    assert s.disposition == "keep"
    assert s.composite_normalized == 100.0
    assert s.eligible_for_shortlist is True
    assert s.gates_triggered == []


def test_unverified_seed_is_never_eligible():
    # R1: even a metadata-strong seed cannot be shortlist-eligible.
    s = score_card(_strong_verified(source_verified=False, score_status="unverified_seed"), RUBRIC)
    assert s.eligible_for_shortlist is False


def test_high_cost_card_cannot_be_keep():
    # G5 cost cap.
    s = score_card(_strong_verified(expected_cost_sensitivity="high"), RUBRIC)
    assert "G5_cost" in s.gates_triggered
    assert s.disposition != "keep"
    assert s.eligible_for_shortlist is False


def test_martingale_card_is_capped_below_keep():
    # R5 / G3: a high-composite card with martingale cannot be keep.
    s = score_card(_strong_verified(tail_risk_flags=("martingale",)), RUBRIC)
    assert "G3_severe_tail_risk" in s.gates_triggered
    assert s.disposition != "keep"


def test_gpl_card_triggers_license_gate():
    # G1: strong copyleft (license_safety<=1) cannot be keep.
    s = score_card(_strong_verified(license="GPL-3.0"), RUBRIC)
    assert "G1_license" in s.gates_triggered
    assert s.disposition != "keep"


def test_opaque_code_triggers_gate():
    # G2: opaque/code_not_confirmed cannot be keep.
    s = score_card(_strong_verified(code_availability="opaque"), RUBRIC)
    assert "G2_code_opaque" in s.gates_triggered
    assert s.disposition != "keep"


def test_weak_card_lands_reject_by_band():
    s = score_card(_good_card(
        license="proprietary", code_availability="opaque",
        data_requirements=("ohlcv", "fundamentals"), expected_cost_sensitivity="high",
        spot_or_futures="spot", novelty_vs_failed_families="duplicate",
        holding_horizon="position", implementation_complexity="high",
        lookahead_repaint_risk="high", tail_risk_flags=("martingale",),
        source_verified=True, score_status="verified",
    ), RUBRIC)
    assert s.disposition == "reject"


def test_scoring_is_deterministic():
    card = _strong_verified()
    assert score_card(card, RUBRIC) == score_card(card, RUBRIC)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run --no-project pytest external_strategy_sieve/tests/test_scorer.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'external_strategy_sieve.scorer'`

- [ ] **Step 3: Implement the per-card scoring half of `scorer.py`**

Create `external_strategy_sieve/scorer.py`:

```python
"""ROB-383 — pure scorer for the external crypto strategy sieve.

Consumes catalog metadata only (NO Phase-3 validation results — R7). Derives
scores via the frozen rubric, applies hard gate caps independently of the
composite (so a high score cannot launder a martingale into the shortlist — R5),
assigns a pre-validation disposition, and buckets cards by integrity status.
Leaderboard rank / popularity never enters scoring (R8) — it is not a card field.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from external_strategy_sieve.rubric import (
    MAX_POSITIVE,
    MIN_COMPOSITE,
    Rubric,
    classify_license,
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

    # R1: only a source-verified, `verified`, keep-disposition card is eligible.
    eligible = (
        card.source_verified
        and card.score_status == "verified"
        and disposition == "keep"
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
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run --no-project pytest external_strategy_sieve/tests/test_scorer.py -q`
Expected: PASS (8 passed)

- [ ] **Step 5: Commit**

```bash
git add research/nautilus_scalping/external_strategy_sieve/scorer.py \
        research/nautilus_scalping/external_strategy_sieve/tests/test_scorer.py
git commit -m "feat(ROB-383): per-card scorer with composite + hard gate caps

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: Scorer — bucketize + family-diverse shortlist freeze

**Files:**
- Modify: `external_strategy_sieve/scorer.py` (append `bucketize` and `freeze_shortlist`)
- Modify: `external_strategy_sieve/tests/test_scorer.py` (append bucket + shortlist tests)

- [ ] **Step 1: Append the failing bucket + shortlist tests**

Append to `external_strategy_sieve/tests/test_scorer.py`:

```python
from external_strategy_sieve.scorer import bucketize, freeze_shortlist


def _verified_keep(cid, family):
    return _strong_verified(candidate_id=cid, strategy_family=family)


def test_bucketize_separates_by_status():
    cards = [
        _strong_verified(candidate_id="v1"),
        _good_card(candidate_id="seed1"),  # unverified_seed
        _good_card(candidate_id="tax1", score_status="taxonomy_only"),
        _good_card(candidate_id="cnc1", score_status="code_not_confirmed"),
        _good_card(candidate_id="ua1", score_status="source_unavailable"),
        _good_card(candidate_id="rej1", score_status="reject"),
    ]
    scored = [score_card(c, RUBRIC) for c in cards]
    buckets = bucketize(scored)
    assert [s.candidate_id for s in buckets["verified_ranked"]] == ["v1"]
    assert [s.candidate_id for s in buckets["unverified_seed"]] == ["seed1"]
    assert {s.candidate_id for s in buckets["taxonomy_only"]} == {"tax1", "cnc1"}
    assert [s.candidate_id for s in buckets["source_unavailable"]] == ["ua1"]
    assert [s.candidate_id for s in buckets["reject"]] == ["rej1"]


def test_verified_ranked_is_sorted_desc_with_id_tiebreak():
    a = score_card(_verified_keep("bbb", "trend"), RUBRIC)
    b = score_card(_verified_keep("aaa", "breakout"), RUBRIC)
    buckets = bucketize([a, b])
    # equal composite -> tie-break ascending candidate_id
    assert [s.candidate_id for s in buckets["verified_ranked"]] == ["aaa", "bbb"]


def test_freeze_shortlist_refuses_unverified():
    # R1 end-to-end: a non-verified card may never enter the shortlist.
    scored = [score_card(_good_card(candidate_id=f"seed{i}"), RUBRIC) for i in range(8)]
    result = freeze_shortlist(scored, RUBRIC)
    assert result.shortlist == ()
    assert "no source-verified keep candidates" in " ".join(result.gaps).lower()


def test_freeze_shortlist_enforces_family_diversity():
    # 5 trend + 2 breakout + 1 volatility, all verified-keep.
    families = ["trend"] * 5 + ["breakout", "breakout", "volatility"]
    scored = [score_card(_verified_keep(f"c{i}", fam), RUBRIC) for i, fam in enumerate(families)]
    result = freeze_shortlist(scored, RUBRIC)
    fam_counts = {}
    for s in result.shortlist:
        fam_counts[s.strategy_family] = fam_counts.get(s.strategy_family, 0) + 1
    assert all(c <= RUBRIC.max_per_family for c in fam_counts.values())
    assert len(set(s.strategy_family for s in result.shortlist)) >= RUBRIC.min_distinct_families


def test_freeze_shortlist_reports_gap_when_too_few_families():
    # Only 2 distinct families available but min is 3.
    families = ["trend", "trend", "breakout", "breakout"]
    scored = [score_card(_verified_keep(f"c{i}", fam), RUBRIC) for i, fam in enumerate(families)]
    result = freeze_shortlist(scored, RUBRIC)
    assert any("distinct families" in g.lower() for g in result.gaps)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run --no-project pytest external_strategy_sieve/tests/test_scorer.py -q`
Expected: FAIL — `ImportError: cannot import name 'bucketize'`

- [ ] **Step 3: Append `bucketize` and `freeze_shortlist` to `scorer.py`**

Append to `external_strategy_sieve/scorer.py`:

```python
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
    "verified_ranked", "unverified_seed", "taxonomy_only",
    "source_unavailable", "reject",
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
    buckets["verified_ranked"].sort(key=lambda s: (-s.composite_normalized, s.candidate_id))
    return buckets


def freeze_shortlist(scored: list[ScoredCandidate], rubric: Rubric) -> ShortlistResult:
    """Freeze a family-diverse shortlist drawn ONLY from source-verified keep
    candidates (R1, R4). Surfaces gaps rather than padding with near-duplicates."""
    buckets = bucketize(scored)
    pool = [s for s in buckets["verified_ranked"] if s.eligible_for_shortlist]

    gaps: list[str] = []
    if not pool:
        gaps.append("No source-verified keep candidates available for shortlist.")
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
    distinct = len(set(s.strategy_family for s in shortlist))
    if distinct < rubric.min_distinct_families:
        gaps.append(
            f"Only {distinct} distinct families in shortlist; "
            f"min_distinct_families is {rubric.min_distinct_families}."
        )
    return ShortlistResult(shortlist=tuple(shortlist), gaps=tuple(gaps))
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run --no-project pytest external_strategy_sieve/tests/test_scorer.py -q`
Expected: PASS (13 passed)

- [ ] **Step 5: Commit**

```bash
git add research/nautilus_scalping/external_strategy_sieve/scorer.py \
        research/nautilus_scalping/external_strategy_sieve/tests/test_scorer.py
git commit -m "feat(ROB-383): bucketize + family-diverse shortlist freeze (R1/R4/R6)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: Catalog loader (JSON) with integrity guards

**Files:**
- Create: `external_strategy_sieve/catalog.py`
- Test: `external_strategy_sieve/tests/test_catalog.py`

- [ ] **Step 1: Write the failing catalog test**

Create `external_strategy_sieve/tests/test_catalog.py`:

```python
import json

from external_strategy_sieve.catalog import load_catalog


def _card_dict(cid):
    return {
        "candidate_id": cid,
        "source_url": "https://example.com/x",
        "source_bucket": "freqtrade_github",
        "license": "MIT",
        "code_availability": "open",
        "strategy_family": "trend",
        "spot_or_futures": "spot",
        "long_short": "long_only",
        "timeframe": "1h",
        "holding_horizon": "intraday",
        "entry_exit_summary": "x",
        "data_requirements": ["ohlcv"],
        "tail_risk_flags": [],
        "lookahead_repaint_risk": "low",
        "implementation_complexity": "low",
        "novelty_vs_failed_families": "adjacent",
        "expected_cost_sensitivity": "medium",
        "source_verified": False,
        "score_status": "unverified_seed",
        "recommended_disposition_pre_validation": "shadow_only",
    }


def test_load_valid_catalog(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps([_card_dict("a"), _card_dict("b")]))
    load = load_catalog(str(p))
    assert load.errors == []
    assert [c.candidate_id for c in load.cards] == ["a", "b"]


def test_duplicate_id_is_an_error(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps([_card_dict("dup"), _card_dict("dup")]))
    load = load_catalog(str(p))
    assert any("duplicate" in e.lower() and "dup" in e for e in load.errors)


def test_bad_enum_propagates_as_error(tmp_path):
    bad = _card_dict("a")
    bad["source_bucket"] = "reddit"
    p = tmp_path / "c.json"
    p.write_text(json.dumps([bad]))
    load = load_catalog(str(p))
    assert any("source_bucket" in e for e in load.errors)


def test_unknown_field_is_an_error(tmp_path):
    bad = _card_dict("a")
    bad["surprise"] = 1
    p = tmp_path / "c.json"
    p.write_text(json.dumps([bad]))
    load = load_catalog(str(p))
    assert any("surprise" in e for e in load.errors)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run --no-project pytest external_strategy_sieve/tests/test_catalog.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'external_strategy_sieve.catalog'`

- [ ] **Step 3: Implement `catalog.py`**

Create `external_strategy_sieve/catalog.py`:

```python
"""ROB-383 — JSON catalog loader for candidate cards.

JSON (not YAML) because PyYAML is absent from the research venv; this matches the
existing ``data_manifests/*.json`` convention. ``load_catalog`` never raises on
malformed input — it returns the cards it could build plus a list of errors so
the whole file can be reviewed at once.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, fields

from external_strategy_sieve.schema import CandidateCard, validate

_CARD_FIELDS = {f.name for f in fields(CandidateCard)}
_TUPLE_FIELDS = ("data_requirements", "tail_risk_flags")


@dataclass(frozen=True)
class CatalogLoad:
    cards: tuple[CandidateCard, ...]
    errors: tuple[str, ...]


def load_catalog(path: str) -> CatalogLoad:
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)

    cards: list[CandidateCard] = []
    errors: list[str] = []
    seen: set[str] = set()

    if not isinstance(raw, list):
        return CatalogLoad(cards=(), errors=("catalog root must be a JSON array",))

    for idx, entry in enumerate(raw):
        if not isinstance(entry, dict):
            errors.append(f"entry[{idx}] is not an object")
            continue
        unknown = set(entry) - _CARD_FIELDS
        if unknown:
            errors.append(f"entry[{idx}] has unknown fields {sorted(unknown)}")
        missing = _CARD_FIELDS - set(entry)
        if missing:
            errors.append(f"entry[{idx}] missing fields {sorted(missing)}")
            continue
        kw = {k: entry[k] for k in _CARD_FIELDS}
        for tf in _TUPLE_FIELDS:
            kw[tf] = tuple(kw[tf])
        card = CandidateCard(**kw)
        cards.append(card)
        for err in validate(card):
            errors.append(f"{card.candidate_id}: {err}")
        if card.candidate_id in seen:
            errors.append(f"duplicate candidate_id {card.candidate_id!r}")
        seen.add(card.candidate_id)

    return CatalogLoad(cards=tuple(cards), errors=tuple(errors))
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run --no-project pytest external_strategy_sieve/tests/test_catalog.py -q`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add research/nautilus_scalping/external_strategy_sieve/catalog.py \
        research/nautilus_scalping/external_strategy_sieve/tests/test_catalog.py
git commit -m "feat(ROB-383): JSON catalog loader with integrity guards

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 6: Seed catalog (8–12 unverified seeds) + end-to-end R1 guard

**Files:**
- Create: `external_strategy_sieve/candidates.json`
- Modify: `external_strategy_sieve/tests/test_catalog.py` (append seed-catalog test)

All seeds are `source_verified=false` / `score_status="unverified_seed"`. They are
cold-start pointers for the survey session; the scorer must mark NONE of them
shortlist-eligible until a survey session verifies them. License/code fields are
best-effort and re-verified during the survey.

- [ ] **Step 1: Create `external_strategy_sieve/candidates.json`**

```json
[
  {
    "candidate_id": "freqtrade_bbrsi_naive",
    "source_url": "https://github.com/freqtrade/freqtrade-strategies",
    "source_bucket": "freqtrade_github",
    "license": "GPL-3.0",
    "code_availability": "open",
    "strategy_family": "mean_reversion",
    "spot_or_futures": "spot",
    "long_short": "long_only",
    "timeframe": "5m",
    "holding_horizon": "intraday",
    "entry_exit_summary": "Bollinger lower-band touch + RSI oversold entry, mid-band exit",
    "data_requirements": ["ohlcv"],
    "tail_risk_flags": [],
    "lookahead_repaint_risk": "low",
    "implementation_complexity": "low",
    "novelty_vs_failed_families": "adjacent",
    "expected_cost_sensitivity": "high",
    "source_verified": false,
    "score_status": "unverified_seed",
    "recommended_disposition_pre_validation": "shadow_only"
  },
  {
    "candidate_id": "freqtrade_supertrend",
    "source_url": "https://github.com/freqtrade/freqtrade-strategies",
    "source_bucket": "freqtrade_github",
    "license": "GPL-3.0",
    "code_availability": "open",
    "strategy_family": "atr_trail",
    "spot_or_futures": "both",
    "long_short": "both",
    "timeframe": "1h",
    "holding_horizon": "swing",
    "entry_exit_summary": "Supertrend (ATR-band) flip entry; opposite flip exit",
    "data_requirements": ["ohlcv"],
    "tail_risk_flags": ["leverage"],
    "lookahead_repaint_risk": "low",
    "implementation_complexity": "low",
    "novelty_vs_failed_families": "adjacent",
    "expected_cost_sensitivity": "medium",
    "source_verified": false,
    "score_status": "unverified_seed",
    "recommended_disposition_pre_validation": "shadow_only"
  },
  {
    "candidate_id": "freqtrade_godstra_ga",
    "source_url": "https://github.com/freqtrade/freqtrade-strategies",
    "source_bucket": "freqtrade_github",
    "license": "GPL-3.0",
    "code_availability": "open",
    "strategy_family": "other",
    "spot_or_futures": "spot",
    "long_short": "long_only",
    "timeframe": "1h",
    "holding_horizon": "swing",
    "entry_exit_summary": "GA-evolved indicator-combination rules (overfit risk)",
    "data_requirements": ["ohlcv"],
    "tail_risk_flags": [],
    "lookahead_repaint_risk": "medium",
    "implementation_complexity": "medium",
    "novelty_vs_failed_families": "adjacent",
    "expected_cost_sensitivity": "medium",
    "source_verified": false,
    "score_status": "unverified_seed",
    "recommended_disposition_pre_validation": "reject"
  },
  {
    "candidate_id": "nfi_nostalgia_for_infinity",
    "source_url": "https://github.com/iterativv/NostalgiaForInfinity",
    "source_bucket": "large_public_bot",
    "license": "GPL-3.0",
    "code_availability": "open",
    "strategy_family": "grid_dca",
    "spot_or_futures": "spot",
    "long_short": "long_only",
    "timeframe": "5m",
    "holding_horizon": "days",
    "entry_exit_summary": "Multi-condition dip-buy with staged DCA averaging; many sub-strategies",
    "data_requirements": ["ohlcv"],
    "tail_risk_flags": ["dca", "unlimited_averaging"],
    "lookahead_repaint_risk": "low",
    "implementation_complexity": "high",
    "novelty_vs_failed_families": "adjacent",
    "expected_cost_sensitivity": "medium",
    "source_verified": false,
    "score_status": "unverified_seed",
    "recommended_disposition_pre_validation": "reject"
  },
  {
    "candidate_id": "tv_ut_bot_alerts",
    "source_url": "https://www.tradingview.com/script/",
    "source_bucket": "tradingview",
    "license": "unknown",
    "code_availability": "open",
    "strategy_family": "atr_trail",
    "spot_or_futures": "both",
    "long_short": "both",
    "timeframe": "15m",
    "holding_horizon": "intraday",
    "entry_exit_summary": "ATR-trailing-stop crossover signal (UT Bot)",
    "data_requirements": ["ohlcv"],
    "tail_risk_flags": [],
    "lookahead_repaint_risk": "medium",
    "implementation_complexity": "low",
    "novelty_vs_failed_families": "adjacent",
    "expected_cost_sensitivity": "high",
    "source_verified": false,
    "score_status": "unverified_seed",
    "recommended_disposition_pre_validation": "shadow_only"
  },
  {
    "candidate_id": "tv_lorentzian_classification",
    "source_url": "https://www.tradingview.com/script/",
    "source_bucket": "tradingview",
    "license": "unknown",
    "code_availability": "open",
    "strategy_family": "regime_filter",
    "spot_or_futures": "both",
    "long_short": "both",
    "timeframe": "5m",
    "holding_horizon": "intraday",
    "entry_exit_summary": "kNN (Lorentzian-distance) regime classifier signal",
    "data_requirements": ["ohlcv"],
    "tail_risk_flags": [],
    "lookahead_repaint_risk": "high",
    "implementation_complexity": "high",
    "novelty_vs_failed_families": "novel",
    "expected_cost_sensitivity": "high",
    "source_verified": false,
    "score_status": "unverified_seed",
    "recommended_disposition_pre_validation": "reject"
  },
  {
    "candidate_id": "tv_range_filter",
    "source_url": "https://www.tradingview.com/script/",
    "source_bucket": "tradingview",
    "license": "unknown",
    "code_availability": "open",
    "strategy_family": "trend",
    "spot_or_futures": "both",
    "long_short": "both",
    "timeframe": "1h",
    "holding_horizon": "swing",
    "entry_exit_summary": "Smoothed range filter band; trend-direction entries",
    "data_requirements": ["ohlcv"],
    "tail_risk_flags": [],
    "lookahead_repaint_risk": "medium",
    "implementation_complexity": "low",
    "novelty_vs_failed_families": "adjacent",
    "expected_cost_sensitivity": "medium",
    "source_verified": false,
    "score_status": "unverified_seed",
    "recommended_disposition_pre_validation": "shadow_only"
  },
  {
    "candidate_id": "qc_crypto_xs_momentum",
    "source_url": "https://www.quantconnect.com/research",
    "source_bucket": "quantconnect",
    "license": "unknown",
    "code_availability": "partial",
    "strategy_family": "trend",
    "spot_or_futures": "spot",
    "long_short": "long_only",
    "timeframe": "1d",
    "holding_horizon": "weekly",
    "entry_exit_summary": "Cross-sectional momentum, top-k crypto by trailing return, weekly rebalance",
    "data_requirements": ["ohlcv"],
    "tail_risk_flags": [],
    "lookahead_repaint_risk": "low",
    "implementation_complexity": "medium",
    "novelty_vs_failed_families": "duplicate",
    "expected_cost_sensitivity": "low",
    "source_verified": false,
    "score_status": "unverified_seed",
    "recommended_disposition_pre_validation": "shadow_only"
  },
  {
    "candidate_id": "tv_squeeze_momentum",
    "source_url": "https://www.tradingview.com/script/",
    "source_bucket": "tradingview",
    "license": "unknown",
    "code_availability": "open",
    "strategy_family": "volatility",
    "spot_or_futures": "both",
    "long_short": "both",
    "timeframe": "1h",
    "holding_horizon": "intraday",
    "entry_exit_summary": "TTM-squeeze (BB inside Keltner) release + momentum-histogram direction",
    "data_requirements": ["ohlcv"],
    "tail_risk_flags": [],
    "lookahead_repaint_risk": "low",
    "implementation_complexity": "low",
    "novelty_vs_failed_families": "novel",
    "expected_cost_sensitivity": "medium",
    "source_verified": false,
    "score_status": "unverified_seed",
    "recommended_disposition_pre_validation": "shadow_only"
  },
  {
    "candidate_id": "marketplace_3commas_dca_template",
    "source_url": "https://3commas.io/",
    "source_bucket": "commercial_marketplace",
    "license": "unknown",
    "code_availability": "opaque",
    "strategy_family": "grid_dca",
    "spot_or_futures": "spot",
    "long_short": "long_only",
    "timeframe": "5m",
    "holding_horizon": "days",
    "entry_exit_summary": "Marketplace DCA bot template; averaging-down safety orders",
    "data_requirements": ["ohlcv"],
    "tail_risk_flags": ["dca", "grid", "martingale"],
    "lookahead_repaint_risk": "none",
    "implementation_complexity": "medium",
    "novelty_vs_failed_families": "duplicate",
    "expected_cost_sensitivity": "high",
    "source_verified": false,
    "score_status": "unverified_seed",
    "recommended_disposition_pre_validation": "reject"
  }
]
```

- [ ] **Step 2: Append the seed-catalog end-to-end test**

Append to `external_strategy_sieve/tests/test_catalog.py`:

```python
import os

from external_strategy_sieve.rubric import RUBRIC
from external_strategy_sieve.scorer import freeze_shortlist, score_card

_SEED_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "candidates.json")


def test_seed_catalog_loads_clean():
    load = load_catalog(_SEED_PATH)
    assert load.errors == [] or load.errors == ()
    assert 8 <= len(load.cards) <= 12


def test_seed_catalog_covers_at_least_four_buckets():
    load = load_catalog(_SEED_PATH)
    buckets = {c.source_bucket for c in load.cards}
    assert len(buckets) >= 4


def test_all_seeds_are_unverified_and_not_shortlist_eligible():
    # R1: cold-start seeds must never be promotable before verification.
    load = load_catalog(_SEED_PATH)
    assert all(c.score_status == "unverified_seed" for c in load.cards)
    assert all(c.source_verified is False for c in load.cards)
    scored = [score_card(c, RUBRIC) for c in load.cards]
    assert all(not s.eligible_for_shortlist for s in scored)
    result = freeze_shortlist(scored, RUBRIC)
    assert result.shortlist == ()
```

- [ ] **Step 3: Run the seed tests to verify they pass**

Run: `uv run --no-project pytest external_strategy_sieve/tests/test_catalog.py -q`
Expected: PASS (7 passed)

- [ ] **Step 4: Commit**

```bash
git add research/nautilus_scalping/external_strategy_sieve/candidates.json \
        research/nautilus_scalping/external_strategy_sieve/tests/test_catalog.py
git commit -m "feat(ROB-383): seed catalog (10 unverified seeds, 5 buckets) + R1 e2e test

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 7: Runbook (report skeleton + rubric pre-registration record)

**Files:**
- Create: `docs/runbooks/external-crypto-strategy-sieve.md`

- [ ] **Step 1: Get the frozen rubric hash**

Run: `cd research/nautilus_scalping && uv run --no-project python -c "from external_strategy_sieve.rubric import RUBRIC, RUBRIC_VERSION; print(RUBRIC_VERSION, RUBRIC.config_hash())"`
Expected: `rob383.sieve.v1 <64-hex-hash>` — use the printed hash in Step 2 where it says `<PASTE_RUBRIC_HASH>`.

- [ ] **Step 2: Write the runbook**

Create `docs/runbooks/external-crypto-strategy-sieve.md` with this content (replace `<PASTE_RUBRIC_HASH>` with the value from Step 1):

````markdown
# External Crypto Strategy Sieve (ROB-383)

Bounded sieve turning public crypto strategy sources into a ranked, pre-registered
shortlist for *possible* Binance Demo Spot/Futures observation. **This is candidate
discovery + scoring + classification — NOT Demo activation.** Phase 1–2 (schema,
rubric, scorer, seed catalog) is implemented here; Phase 3 validation and the
Phase 4 strategy-pack recommendation are follow-ups under the same issue.

Primary deliverable: a reusable sieve pipeline + an evidence-backed reject catalog.
Given prior negative results (ROB-382 `no_decisive_survivor`, ROB-342/353), a run
producing 0 `demo_ready` candidates is still a success.

## Safety boundaries

No live trading. No Binance Demo confirmed order placement. No
broker/order/watch/order-intent/trade-journal mutation. No scheduler / TaskIQ /
Prefect / cron / launchd / daemon activation. No prod DB writes/backfills/deletes.
No prod env/secret changes. No secret printing/copying/committing. No raw
market-data or raw web/leaderboard dumps committed. No direct import of external
strategy runtimes (Freqtrade / Pine / QuantConnect / bot) into auto_trader
execution paths. No direct GPL/unclear-license code copy — clean-room specs only.
Public ranking/popularity is never alpha proof. No Demo activation/backtest issue
opened automatically.

## Pipeline (pure stdlib, `uv run --no-project`)

Package: `research/nautilus_scalping/external_strategy_sieve/`

- `schema.py` — candidate-card fields + `validate()`.
- `rubric.py` — frozen weights/gates/thresholds + metadata→score derivation +
  `config_hash()`.
- `scorer.py` — `score_card`, `bucketize`, `freeze_shortlist`.
- `catalog.py` — `load_catalog(path)` (JSON; integrity guards).
- `candidates.json` — seed catalog (cold-start, all `unverified_seed`).

Run all tests:

```bash
cd research/nautilus_scalping
uv run --no-project pytest external_strategy_sieve/tests/ -q
```

## Frozen rubric (pre-registration record)

Recorded BEFORE any Phase-3 validation result exists. A later weight tweak changes
the hash, so an ex-post adjustment is detectable.

- `RUBRIC_VERSION`: `rob383.sieve.v1`
- `config_hash()`: `<PASTE_RUBRIC_HASH>`

Weighted criteria (each derived 0–3 from card metadata):

| criterion | weight | derived from |
|-----------|:------:|--------------|
| source_hygiene_reproducibility | 3 | code_availability |
| license_safety | 3 | license class (G1 gate if ≤1) |
| faithful_port_feasibility | 3 | code_availability − complexity − repaint |
| data_availability_auto_trader | 3 | data_requirements vs {ohlcv, funding, oi} |
| cost_fee_survivability_potential | 3 | expected_cost_sensitivity (G5 gate if high) |
| market_fit_binance_demo | 2 | spot_or_futures |
| novelty_vs_failed_families | 2 | novelty field |
| expected_daily_review_usefulness | 1 | holding_horizon |
| tail_risk_dca_dependence | −3 | tail_risk_flags severity (G3 gate if severe) |

Hard gates cap disposition independent of composite: G1 license, G2 opaque code,
G3 severe tail-risk (martingale/unlimited_averaging/no_stoploss), G4 high repaint,
G5 high cost. Disposition bands: keep ≥ 65, shadow_only ≥ 45, else reject.

Integrity rules: only source-verified `verified` keep candidates are
shortlist-eligible (R1); shortlist is family-diverse (≤2/family, ≥3 families, R4);
output is bucketed verified_ranked / unverified_seed / taxonomy_only /
source_unavailable / reject (R6); popularity never enters scoring (R8).

## Candidate catalog summary

_Filled after the survey session (`docs/plans/ROB-383-external-strategy-survey-plan.md`)._
Counts-only; no raw dumps. Seed catalog ships 10 `unverified_seed` cards across 5
source buckets as cold-start pointers.

## Frozen shortlist

_Filled after verification: 6–8 candidates from the verified pool, with diversity
rationale and explicit exclusions._

## Phase 3 data reuse (no duplicate fetch)

Validation reuses existing seams; ROB-383 adds no new fetcher.

- **SEAM 2 (primary, pure):** `pit_bars.load_panel(symbols, interval, manifest)` →
  one shared panel; add a bar-based generator in `families.py` + a spec in
  `campaign_specs.py`. Runs with `uv run --no-project` (no `nautilus_trader`).
- **SEAM 1 (Nautilus tick):** register in `candidates.py` REGISTRY +
  `backtest_runner._run_single()`. Needs the `nautilus_trader` venv (ROB-316).
- Point `AUTO_TRADER_RESEARCH_ARTIFACT_ROOT` at the root used by prior campaigns so
  `pit_klines_fetcher.fetch_months()` reuses cached klines (`if csv_path.exists():
  continue`) and downloads only missing months.
- All trades/periods recorded at `cost_model.REF_FEE_BPS=10`/leg; cost sensitivity
  must include the Demo envelope (maker 2.0 / taker 4.0 bps).
- Counts-only outputs → `resolve_artifact_path('discovery', 'rob383', ...)`
  (gitignored). Never commit raw klines / dumps.

## Disposition definitions (Phase 3–4 classes)

- `demo_ready_candidate` — small Binance Demo observation may be justified later,
  with separate operator approval.
- `shadow_candidate` — signal-only / dry-run observation candidate.
- `research_candidate` — worth preserving, not ready for Demo.
- `reject` — weak evidence, cost sensitivity, overfit, lookahead/repaint,
  tail-risk/DCA dependence, license risk, or implementation mismatch.

Counts-only, no alpha claim. Demo activation requires a separate operator-approved
issue.
````

- [ ] **Step 3: Commit**

```bash
git add docs/runbooks/external-crypto-strategy-sieve.md
git commit -m "docs(ROB-383): runbook + frozen rubric pre-registration record

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 8: Survey-plan handoff for the web-survey session

**Files:**
- Create: `docs/plans/ROB-383-external-strategy-survey-plan.md`

- [ ] **Step 1: Write the survey-plan handoff**

Create `docs/plans/ROB-383-external-strategy-survey-plan.md`:

````markdown
# ROB-383 — External Strategy Survey Plan (web-survey session handoff)

**Read-only web survey for codex / gemini / agy workers.** This session does NOT
edit the repo. Workers produce structured candidate-card proposals (JSON objects
matching `research/nautilus_scalping/external_strategy_sieve/schema.py`). Repo
edits — merging into `candidates.json`, running the scorer, freezing the shortlist
— are done ONLY by the Claude Code main/integrator.

## Objective

1. Verify the 8–12 cold-start seeds in `external_strategy_sieve/candidates.json`.
2. Per-bucket broad live survey to ≥15 (ideally 25–40) structured candidates.
3. Cover ≥4 source buckets.
4. Separate verified vs unverified / taxonomy-only / reject.
5. The integrator freezes a 6–8 shortlist from verified candidates only.

## Card status discipline (critical)

- Seeds ship as `source_verified=false` / `score_status="unverified_seed"`.
- Promote a card to `score_status="verified"` (+ `source_verified=true`) ONLY after
  you have actually confirmed, from the source: `source_url`, `license`,
  `code_availability`, and `strategy_family`.
- If you cannot reach/confirm a source: set `score_status="source_unavailable"` or
  `"code_not_confirmed"`. Never fabricate or estimate metadata.
- Taxonomy/trend-only finds (no reproducible source): `score_status="taxonomy_only"`.
- The scorer treats only `verified` cards as shortlist-eligible — unverified seeds
  cannot enter the shortlist no matter how good their metadata looks.

## Per-bucket survey

| bucket | where | what to record / cautions |
|--------|-------|---------------------------|
| `freqtrade_github` | freqtrade/freqtrade-strategies, other open Freqtrade repos, strat.ninja leaderboard | family, timeframe, entry/exit, license (most are GPL → clean-room only). Don't re-test ROB-382's ichi/vwap/elliot/cluc as new. |
| `large_public_bot` | iterativv/NostalgiaForInfinity | tail-risk/DCA audit FIRST; treat as black-box feasibility, not code adoption. |
| `tradingview` | top/trending open-source Pine | distinguish `indicator()` vs `strategy()`; flag repaint / lookahead / HTF `request.security` risk; license is often unclear. |
| `quantconnect` | community research | taxonomy / clean-room idea extraction; crypto-relevant only if practical. |
| `commercial_marketplace` | Cryptohopper / 3Commas | taxonomy/trend scanning only unless source + reproducible assumptions exist. |

## Verification protocol

- Default: gstack `/browse` or plain fetch.
- **Chrome remote-debug fallback (read-only)** — only for JS-rendered pages,
  TradingView code panels, or accessibility issues:

  ```bash
  open -na "Google Chrome" --args \
    --remote-debugging-address=127.0.0.1 \
    --remote-debugging-port=9222 \
    --user-data-dir="$HOME/.hermes/chrome-toss-debug"
  ```

  Use it strictly to read metadata. Do not log in to or mutate any site.

## Hard rules

- No raw HTML / raw Pine / raw leaderboard dumps saved or committed.
- No full source-code copy. Record metadata only: URL, license, code availability,
  indicator-vs-strategy, repaint/lookahead risk, family, timeframe, data needs,
  tail-risk flags.
- Public ranking / GitHub stars / marketplace popularity is candidate-universe
  input only — never alpha proof.
- Never touch broker / order / DB / scheduler / secret paths. No prod anything.

## Output format

Emit a JSON array of card objects (same fields as `schema.py`), e.g.:

```json
{
  "candidate_id": "freqtrade_<name>",
  "source_url": "https://github.com/...",
  "source_bucket": "freqtrade_github",
  "license": "GPL-3.0",
  "code_availability": "open",
  "strategy_family": "breakout",
  "spot_or_futures": "both",
  "long_short": "both",
  "timeframe": "15m",
  "holding_horizon": "intraday",
  "entry_exit_summary": "...",
  "data_requirements": ["ohlcv"],
  "tail_risk_flags": [],
  "lookahead_repaint_risk": "low",
  "implementation_complexity": "low",
  "novelty_vs_failed_families": "adjacent",
  "expected_cost_sensitivity": "medium",
  "source_verified": true,
  "score_status": "verified",
  "recommended_disposition_pre_validation": "keep"
}
```

Hand the array to the integrator. The integrator validates with `load_catalog`,
merges into `candidates.json`, runs the scorer, and freezes the shortlist.
````

- [ ] **Step 2: Commit**

```bash
git add docs/plans/ROB-383-external-strategy-survey-plan.md
git commit -m "docs(ROB-383): read-only web-survey handoff plan for sieve catalog

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 9: Final verification + spec correction

**Files:**
- Modify: `docs/superpowers/specs/2026-05-31-rob383-external-strategy-sieve-design.md` (yaml → json)

- [ ] **Step 1: Run the full sieve test suite**

Run: `cd research/nautilus_scalping && uv run --no-project pytest external_strategy_sieve/tests/ -q`
Expected: PASS — all tests across test_schema.py (5), test_rubric.py (7), test_scorer.py (13), test_catalog.py (7). Confirm the summary line shows `32 passed` and 0 failed.

- [ ] **Step 2: Correct the spec's YAML references to JSON**

In `docs/superpowers/specs/2026-05-31-rob383-external-strategy-sieve-design.md`, replace the two `candidates.yaml` mentions (file-layout block and §4) with `candidates.json`, and note JSON is used because PyYAML is absent from the research venv. Use Edit to change `candidates.yaml  # seed 8–12 cards` → `candidates.json  # seed 8–12 cards` and the `candidates.yaml` mention in §8 likewise.

- [ ] **Step 3: Confirm no raw data / secrets staged**

Run: `git status --short && git check-ignore research/nautilus_scalping/data research/nautilus_scalping/results || true`
Expected: only the intended source/doc files are modified; `data/` and `results/` are gitignored. No `.csv`/`.zip`/`.parquet` staged.

- [ ] **Step 4: Commit the spec correction**

```bash
git add docs/superpowers/specs/2026-05-31-rob383-external-strategy-sieve-design.md
git commit -m "docs(ROB-383): correct catalog format yaml->json (research venv has no PyYAML)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-review notes

- **Spec coverage:** schema (Task 1 = spec §4), rubric (Task 2 = §5), scorer
  composite/gates (Task 3 = §5/§6), bucketize + shortlist + R1/R4/R6 (Task 4 =
  §6), catalog loader (Task 5), seed catalog + R1 e2e (Task 6 = §3/§4), runbook +
  pre-registration (Task 7 = §9), survey-plan handoff (Task 8 = §8), data-flow
  reuse is documented in the runbook (§7) — exercised only in Phase 3 follow-up,
  so no code task here (correct per scope split). Integrity rules R1, R4, R5, R6,
  R8 have explicit tests; R2 tested in schema; R3 (determinism) tested in
  scorer/rubric; R7 (pre-registration) enforced structurally (scorer takes no
  validation input + runbook records the hash).
- **Placeholder scan:** the only fill-in is the rubric hash, obtained by a given
  command (Task 7 Step 1) — concrete, not vague.
- **Type consistency:** `CandidateCard` field names are identical across schema,
  rubric (`derive_scores`), scorer, and catalog. `ScoredCandidate` / `ShortlistResult`
  field names match between scorer implementation and tests. `score_status` and
  disposition enum strings are consistent everywhere.
- **YAGNI:** no CLI, no network, no Phase-3 code in this PR — scope is exactly the
  deterministic infrastructure + handoff.
````

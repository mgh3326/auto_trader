"""ROB-983 (H5, CP5) -- S4 falsification gates, pair-executor historical
state, direct-verdict tri-state, and campaign decision.

S4-specific falsification mirrors S3's structure (CP4) with its own
thresholds and predicates: pooled/per-fold timeout ceilings, an ``M_t>+3%``
upward subbook (strict positivity), ``abs(Corr(pair gross return,
M_t))<=0.15`` (pooled, price-only ``gross_bps`` against ``market_return_4h``
-- a zero-variance denominator is structurally undefined, never a free
pass), pair concentration (conditional -- dominant-pair share ``>0.70`` of
the positive pool AND the other-two-pairs' pooled E17 ``<=0``, both
predicates required), and slow-only failure (the ``[8h,32h)`` bucket
``<=0`` AND the ``[32h,48h]`` bucket ``>0`` -- edge concentrated entirely in
the slow tail).

``S4PairExecutorState`` is a fixed literal describing S4's historical-
screen-only nature: ``volatility_percentile``/order/residual/PAIR_EXEC_FAIL
counts are exactly ``None`` (never a numeric zero), ``demo_eligible`` is
exactly ``False``.

Direct verdicts (``compute_direct_verdict``) are incomplete-first, then
hard-gate-fail, then pass. The campaign decision table
(``compute_campaign_decision``) is a separate, additive view over the two
independently-computed direct verdicts -- it never overwrites them.
Observable S4 superiority and both-pass ranking are report-only and never
change the campaign decision or promote S4 to a demo candidate.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from rob974_h5_contracts import FOLD_IDS, S4_PAIRS, H5InputError, MetricTrade
from rob974_h5_gates import validate_selected_oos_membership

__all__ = [
    "S4_CORR_MAX",
    "S4_FOLD_TIMEOUT_MAX",
    "S4_HISTORICAL_PAIR_EXECUTOR_STATE",
    "S4_M_T_THRESHOLD",
    "S4_PAIR_CONCENTRATION_MAX",
    "S4_POOLED_TIMEOUT_MAX",
    "S4_SLOW_BUCKET_END_MINUTES",
    "S4_SLOW_BUCKET_MID_MINUTES",
    "S4_SLOW_BUCKET_START_MINUTES",
    "CampaignDecisionResult",
    "S4FalsificationResult",
    "S4PairExecutorState",
    "StrategyRankMetrics",
    "compute_campaign_decision",
    "compute_direct_verdict",
    "evaluate_s4_falsification",
    "rank_both_pass",
    "s4_shows_observable_superiority",
]

S4_POOLED_TIMEOUT_MAX = 0.20
S4_FOLD_TIMEOUT_MAX = 0.30
S4_M_T_THRESHOLD = 0.03
S4_CORR_MAX = 0.15
S4_PAIR_CONCENTRATION_MAX = 0.70
S4_SLOW_BUCKET_START_MINUTES = 480.0  # 8h
S4_SLOW_BUCKET_MID_MINUTES = 1920.0  # 32h
S4_SLOW_BUCKET_END_MINUTES = 2880.0  # 48h

# Correlation is a derived statistic (not a directly-supplied registered
# constant like the CP3 thresholds); a tiny epsilon absorbs float
# summation noise around the boundary without weakening the semantic
# `<=0.15` rule.
_CORR_BOUNDARY_EPSILON = 1e-9

REASON_POOLED_TIMEOUT_ABOVE = "s4_pooled_timeout_above_20pct"
REASON_FOLD_TIMEOUT_ABOVE = "s4_fold_timeout_above_30pct"
REASON_HIGH_M_E22_NOT_POSITIVE = "s4_high_market_return_e22_not_positive"
REASON_CORRELATION_ABOVE = "s4_correlation_above_15pct"
REASON_PAIR_CONCENTRATION_ABOVE = "s4_pair_concentration_above_70pct"
REASON_SLOW_ONLY_FAILURE = "s4_slow_only_failure"

INCOMPLETE_POOLED_TIMEOUT_UNDEFINED = "s4_pooled_timeout_undefined"
INCOMPLETE_CORRELATION_UNDEFINED = "s4_correlation_undefined"
INCOMPLETE_PAIR_EVIDENCE_MISSING = "s4_pair_evidence_missing"
INCOMPLETE_SLOW_BUCKET_EVIDENCE_MISSING = "s4_slow_bucket_evidence_missing"

DIRECT_VERDICT_INCOMPLETE = "incomplete"
DIRECT_VERDICT_PASS = "historical_pass"
DIRECT_VERDICT_FAIL = "historical_fail"

CAMPAIGN_DECISION_INCOMPLETE = "incomplete"
CAMPAIGN_DECISION_BOTH_FAIL = "both_fail"
CAMPAIGN_DECISION_S3_ONLY = "s3_only"
CAMPAIGN_DECISION_S4_ONLY_NO_DEMO = "s4_only_no_demo"
CAMPAIGN_DECISION_BOTH_PASS_S3_DEMO_CANDIDATE = "both_pass_s3_demo_candidate"

_S4_SUPERIORITY_MIN_POOLED_E17_GAP_BPS = 5.0
_S4_SUPERIORITY_MAX_TIMEOUT_GAP = 0.05


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise H5InputError(reason)


@dataclass(frozen=True, slots=True)
class S4FalsificationResult:
    passed: bool
    reasons: tuple[str, ...]
    incomplete_reasons: tuple[str, ...]
    pooled_timeout_ratio: float
    fold_timeout_ratios: dict[str, float]
    high_market_return_e22_bps: float | None
    correlation: float | None
    pair_concentration: float | None
    attribution: dict[str, dict[str, dict[str, float | int | None]]]


def _profit_factor(net_values: list[float]) -> float | None:
    if not net_values:
        return None
    profit = sum(v for v in net_values if v > 0)
    loss = -sum(v for v in net_values if v < 0)
    if loss == 0.0:
        return math.inf if profit > 0 else float("nan")
    return profit / loss


def _bucket(trades: tuple[MetricTrade, ...]) -> dict[str, float | int | None]:
    net_values = [t.net_bps for t in trades]
    gross_values = [t.gross_bps for t in trades]
    holding_values = [t.holding_minutes for t in trades]
    return {
        "trades": len(trades),
        "e17_bps": sum(net_values) / len(net_values) if net_values else None,
        "e0_bps": sum(gross_values) / len(gross_values) if gross_values else None,
        "pf": _profit_factor(net_values),
        "avg_holding_minutes": (
            sum(holding_values) / len(holding_values) if holding_values else None
        ),
    }


def _pearson_corr(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n == 0:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    var_x = sum((x - mx) ** 2 for x in xs) / n
    var_y = sum((y - my) ** 2 for y in ys) / n
    if var_x == 0.0 or var_y == 0.0:
        return None
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / n
    return cov / math.sqrt(var_x * var_y)


def evaluate_s4_falsification(
    *,
    primary_trades: tuple[MetricTrade, ...],
    upward_trades: tuple[MetricTrade, ...],
) -> S4FalsificationResult:
    validate_selected_oos_membership(
        primary_trades=primary_trades,
        upward_trades=upward_trades,
        authority="s4_falsification",
        expected_strategy="S4",
    )

    reasons: set[str] = set()
    incomplete_reasons: set[str] = set()

    # -- Pooled timeout ratio (AC: pooled <=20%). --------------------------
    total = len(primary_trades)
    timeout_count = sum(1 for t in primary_trades if t.exit_reason == "TIMEOUT")
    if total == 0:
        pooled_timeout_ratio = 0.0
        incomplete_reasons.add(INCOMPLETE_POOLED_TIMEOUT_UNDEFINED)
    else:
        pooled_timeout_ratio = timeout_count / total
        if pooled_timeout_ratio > S4_POOLED_TIMEOUT_MAX:
            reasons.add(REASON_POOLED_TIMEOUT_ABOVE)

    # -- Per-fold timeout ratio (AC: every fold <=30%). --------------------
    fold_counts: dict[str, int] = dict.fromkeys(FOLD_IDS, 0)
    fold_timeouts: dict[str, int] = dict.fromkeys(FOLD_IDS, 0)
    for t in primary_trades:
        fold_counts[t.fold_id] += 1
        if t.exit_reason == "TIMEOUT":
            fold_timeouts[t.fold_id] += 1
    fold_timeout_ratios: dict[str, float] = {}
    for fold_id in FOLD_IDS:
        if fold_counts[fold_id] == 0:
            continue
        ratio = fold_timeouts[fold_id] / fold_counts[fold_id]
        fold_timeout_ratios[fold_id] = ratio
        if ratio > S4_FOLD_TIMEOUT_MAX:
            reasons.add(REASON_FOLD_TIMEOUT_ABOVE)

    # -- High-M_t upward subbook (AC: M_t>3% strict >0). --------------------
    high_m_net = [
        t.net_bps
        for t in upward_trades
        if t.market_return_4h is not None and t.market_return_4h > S4_M_T_THRESHOLD
    ]
    high_market_return_e22_bps = (
        sum(high_m_net) / len(high_m_net) if high_m_net else None
    )
    if high_market_return_e22_bps is None or not (high_market_return_e22_bps > 0.0):
        reasons.add(REASON_HIGH_M_E22_NOT_POSITIVE)

    # -- Correlation (AC: abs(Corr(gross_bps, M_t))<=0.15). ------------------
    corr_xs = [
        t.market_return_4h for t in primary_trades if t.market_return_4h is not None
    ]
    corr_ys = [t.gross_bps for t in primary_trades if t.market_return_4h is not None]
    correlation = _pearson_corr(corr_xs, corr_ys)
    if correlation is None:
        incomplete_reasons.add(INCOMPLETE_CORRELATION_UNDEFINED)
    elif abs(correlation) > S4_CORR_MAX + _CORR_BOUNDARY_EPSILON:
        reasons.add(REASON_CORRELATION_ABOVE)

    # -- Pair concentration (AC: conditional >0.70 AND others pooled<=0). ---
    pair_trades: dict[str, list[MetricTrade]] = {p: [] for p in S4_PAIRS}
    for t in primary_trades:
        if t.dimension in pair_trades:
            pair_trades[t.dimension].append(t)
    missing_pairs = [p for p in S4_PAIRS if not pair_trades[p]]
    pair_concentration: float | None = None
    if missing_pairs:
        incomplete_reasons.add(INCOMPLETE_PAIR_EVIDENCE_MISSING)
    else:
        # D12 fix (adversarial verify R1, finding 3): concentration share is
        # the SUM of net_bps per pair, never a per-pair MEAN -- a mean-based
        # share is skewed by unequal trade counts across pairs (the
        # authoritative definition is "max positive pair net / sum positive
        # pair net").
        pair_net_sum = {
            p: sum(t.net_bps for t in trades) for p, trades in pair_trades.items()
        }
        positive_pairs = {p: v for p, v in pair_net_sum.items() if v > 0.0}
        if positive_pairs:
            total_positive = sum(positive_pairs.values())
            dominant_pair = max(positive_pairs, key=lambda p: positive_pairs[p])
            pair_concentration = positive_pairs[dominant_pair] / total_positive
            if pair_concentration > S4_PAIR_CONCENTRATION_MAX:
                others = [
                    t for p in S4_PAIRS if p != dominant_pair for t in pair_trades[p]
                ]
                others_pooled_e17 = (
                    sum(t.net_bps for t in others) / len(others) if others else 0.0
                )
                if others_pooled_e17 <= 0.0:
                    reasons.add(REASON_PAIR_CONCENTRATION_ABOVE)

    # -- Slow-only failure ([8h,32h)<=0 AND [32h,48h]>0). --------------------
    mid_bucket_net = [
        t.net_bps
        for t in primary_trades
        if S4_SLOW_BUCKET_START_MINUTES
        <= t.holding_minutes
        < S4_SLOW_BUCKET_MID_MINUTES
    ]
    slow_bucket_net = [
        t.net_bps
        for t in primary_trades
        if S4_SLOW_BUCKET_MID_MINUTES <= t.holding_minutes <= S4_SLOW_BUCKET_END_MINUTES
    ]
    if not mid_bucket_net or not slow_bucket_net:
        incomplete_reasons.add(INCOMPLETE_SLOW_BUCKET_EVIDENCE_MISSING)
    else:
        mid_bucket_e17 = sum(mid_bucket_net) / len(mid_bucket_net)
        slow_bucket_e17 = sum(slow_bucket_net) / len(slow_bucket_net)
        if mid_bucket_e17 <= 0.0 and slow_bucket_e17 > 0.0:
            reasons.add(REASON_SLOW_ONLY_FAILURE)

    # -- Attribution: exit-reason and pair breakdowns. -----------------------
    exit_groups: dict[str, list[MetricTrade]] = {}
    for t in primary_trades:
        exit_groups.setdefault(t.exit_reason, []).append(t)
    by_exit_reason = {
        reason: _bucket(tuple(trades)) for reason, trades in exit_groups.items()
    }
    by_pair = {p: _bucket(tuple(trades)) for p, trades in pair_trades.items() if trades}

    return S4FalsificationResult(
        passed=not reasons,
        reasons=tuple(sorted(reasons)),
        incomplete_reasons=tuple(sorted(incomplete_reasons)),
        pooled_timeout_ratio=pooled_timeout_ratio,
        fold_timeout_ratios=fold_timeout_ratios,
        high_market_return_e22_bps=high_market_return_e22_bps,
        correlation=correlation,
        pair_concentration=pair_concentration,
        attribution={"by_exit_reason": by_exit_reason, "by_pair": by_pair},
    )


# ---------------------------------------------------------------------------
# S4 pair-executor historical state -- a fixed literal (AC: historical-
# screen-only, never a live-eligible/numeric-zero masquerade).
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class S4PairExecutorState:
    volatility_percentile: None
    volatility_percentile_provenance: str
    pair_executor_state: str
    order_count: None
    residual_count: None
    pair_exec_fail_count: None
    readiness: str
    demo_eligible: bool

    def __post_init__(self) -> None:
        _require(
            self.volatility_percentile is None,
            "s4_pair_executor_volatility_percentile_must_be_none",
        )
        _require(
            self.volatility_percentile_provenance == "not_defined_for_s4",
            "s4_pair_executor_volatility_provenance_mismatch",
        )
        _require(
            self.pair_executor_state == "not_evaluated",
            "s4_pair_executor_state_mismatch",
        )
        _require(self.order_count is None, "s4_pair_executor_order_count_must_be_none")
        _require(
            self.residual_count is None, "s4_pair_executor_residual_count_must_be_none"
        )
        _require(
            self.pair_exec_fail_count is None,
            "s4_pair_executor_fail_count_must_be_none",
        )
        _require(
            self.readiness == "historical_screen_only",
            "s4_pair_executor_readiness_mismatch",
        )
        _require(
            type(self.demo_eligible) is bool and self.demo_eligible is False,
            "s4_pair_executor_demo_eligible_must_be_false",
        )


S4_HISTORICAL_PAIR_EXECUTOR_STATE = S4PairExecutorState(
    volatility_percentile=None,
    volatility_percentile_provenance="not_defined_for_s4",
    pair_executor_state="not_evaluated",
    order_count=None,
    residual_count=None,
    pair_exec_fail_count=None,
    readiness="historical_screen_only",
    demo_eligible=False,
)


# ---------------------------------------------------------------------------
# Direct verdict tri-state (incomplete-first, then fail, then pass).
# ---------------------------------------------------------------------------


def compute_direct_verdict(
    *, incomplete_reasons: tuple[str, ...], hard_gate_reasons: tuple[str, ...]
) -> str:
    if incomplete_reasons:
        return DIRECT_VERDICT_INCOMPLETE
    if hard_gate_reasons:
        return DIRECT_VERDICT_FAIL
    return DIRECT_VERDICT_PASS


# ---------------------------------------------------------------------------
# Campaign decision -- a separate, additive view; never overwrites the
# independently-computed direct verdicts.
# ---------------------------------------------------------------------------


_VALID_DIRECT_VERDICTS = (
    DIRECT_VERDICT_INCOMPLETE,
    DIRECT_VERDICT_FAIL,
    DIRECT_VERDICT_PASS,
)
_VALID_CAMPAIGN_DECISIONS = (
    CAMPAIGN_DECISION_INCOMPLETE,
    CAMPAIGN_DECISION_BOTH_FAIL,
    CAMPAIGN_DECISION_S3_ONLY,
    CAMPAIGN_DECISION_S4_ONLY_NO_DEMO,
    CAMPAIGN_DECISION_BOTH_PASS_S3_DEMO_CANDIDATE,
)


@dataclass(frozen=True, slots=True)
class CampaignDecisionResult:
    campaign_decision: str
    campaign_historical_verdict: str
    s3_direct_verdict: str
    s4_direct_verdict: str
    demo_candidate: str | None
    historical_preferred: str | None
    s4_observable_superiority: bool | None

    def __post_init__(self) -> None:
        # Primitive/enum validation belongs on the DTO boundary.  Cross-field
        # branch consistency is deliberately enforced later by the canonical
        # scorecard authority, which has the gates/rank evidence needed to
        # recompute the entire result rather than trusting these labels.
        _require(
            type(self.campaign_decision) is str
            and self.campaign_decision in _VALID_CAMPAIGN_DECISIONS,
            "campaign_decision_label_unknown",
        )
        _require(
            type(self.campaign_historical_verdict) is str
            and self.campaign_historical_verdict in _VALID_DIRECT_VERDICTS,
            "campaign_historical_verdict_unknown",
        )
        _require(
            type(self.s3_direct_verdict) is str
            and self.s3_direct_verdict in _VALID_DIRECT_VERDICTS,
            "campaign_decision_s3_verdict_unknown",
        )
        _require(
            type(self.s4_direct_verdict) is str
            and self.s4_direct_verdict in _VALID_DIRECT_VERDICTS,
            "campaign_decision_s4_verdict_unknown",
        )
        _require(
            self.demo_candidate is None
            or type(self.demo_candidate) is str
            and self.demo_candidate in ("S3", "S4"),
            "campaign_demo_candidate_unknown",
        )
        _require(
            self.historical_preferred is None
            or type(self.historical_preferred) is str
            and self.historical_preferred in ("S3", "S4"),
            "campaign_historical_preferred_unknown",
        )
        _require(
            self.s4_observable_superiority is None
            or type(self.s4_observable_superiority) is bool,
            "campaign_s4_observable_superiority_malformed",
        )


def compute_campaign_decision(
    *,
    s3_direct_verdict: str,
    s4_direct_verdict: str,
    s4_observable_superiority: bool | None = None,
    s3_rank_metrics: StrategyRankMetrics | None = None,
    s4_rank_metrics: StrategyRankMetrics | None = None,
) -> CampaignDecisionResult:
    # D13 fix (adversarial verify R1, finding 4): reject a non-closed-enum
    # verdict string rather than silently falling through to a default
    # branch.
    _require(
        s3_direct_verdict in _VALID_DIRECT_VERDICTS,
        "campaign_decision_s3_verdict_unknown",
    )
    _require(
        s4_direct_verdict in _VALID_DIRECT_VERDICTS,
        "campaign_decision_s4_verdict_unknown",
    )

    if (
        s3_direct_verdict == DIRECT_VERDICT_INCOMPLETE
        or s4_direct_verdict == DIRECT_VERDICT_INCOMPLETE
    ):
        return CampaignDecisionResult(
            campaign_decision=CAMPAIGN_DECISION_INCOMPLETE,
            campaign_historical_verdict=DIRECT_VERDICT_INCOMPLETE,
            s3_direct_verdict=s3_direct_verdict,
            s4_direct_verdict=s4_direct_verdict,
            demo_candidate=None,
            historical_preferred=None,
            s4_observable_superiority=None,
        )
    if (
        s3_direct_verdict == DIRECT_VERDICT_PASS
        and s4_direct_verdict == DIRECT_VERDICT_PASS
    ):
        # D13 fix (finding 5, AC44): historical_preferred is the both-pass
        # literal rank -- report-only, never changes campaign_decision or
        # the (operationally forced) S3 demo candidate.
        _require(
            s3_rank_metrics is not None and s4_rank_metrics is not None,
            "campaign_decision_both_pass_requires_rank_metrics",
        )
        historical_preferred = rank_both_pass(
            s3_metrics=s3_rank_metrics, s4_metrics=s4_rank_metrics
        )
        recomputed_superiority = s4_shows_observable_superiority(
            pooled_e17_s3=s3_rank_metrics.pooled_e17,
            pooled_e17_s4=s4_rank_metrics.pooled_e17,
            min_fold_e17_s3=s3_rank_metrics.min_fold_e17,
            min_fold_e17_s4=s4_rank_metrics.min_fold_e17,
            pooled_timeout_s3=s3_rank_metrics.timeout_ratio,
            pooled_timeout_s4=s4_rank_metrics.timeout_ratio,
        )
        if s4_observable_superiority is not None:
            _require(
                type(s4_observable_superiority) is bool
                and s4_observable_superiority is recomputed_superiority,
                "campaign_s4_observable_superiority_forged_or_stale",
            )
        return CampaignDecisionResult(
            campaign_decision=CAMPAIGN_DECISION_BOTH_PASS_S3_DEMO_CANDIDATE,
            campaign_historical_verdict=DIRECT_VERDICT_PASS,
            s3_direct_verdict=s3_direct_verdict,
            s4_direct_verdict=s4_direct_verdict,
            demo_candidate="S3",
            historical_preferred=historical_preferred,
            s4_observable_superiority=recomputed_superiority,
        )
    if s3_direct_verdict == DIRECT_VERDICT_PASS:
        return CampaignDecisionResult(
            campaign_decision=CAMPAIGN_DECISION_S3_ONLY,
            campaign_historical_verdict=DIRECT_VERDICT_PASS,
            s3_direct_verdict=s3_direct_verdict,
            s4_direct_verdict=s4_direct_verdict,
            demo_candidate="S3",
            historical_preferred="S3",
            s4_observable_superiority=None,
        )
    if s4_direct_verdict == DIRECT_VERDICT_PASS:
        return CampaignDecisionResult(
            campaign_decision=CAMPAIGN_DECISION_S4_ONLY_NO_DEMO,
            campaign_historical_verdict=DIRECT_VERDICT_PASS,
            s3_direct_verdict=s3_direct_verdict,
            s4_direct_verdict=s4_direct_verdict,
            demo_candidate=None,
            historical_preferred="S4",
            s4_observable_superiority=None,
        )
    return CampaignDecisionResult(
        campaign_decision=CAMPAIGN_DECISION_BOTH_FAIL,
        campaign_historical_verdict=DIRECT_VERDICT_FAIL,
        s3_direct_verdict=s3_direct_verdict,
        s4_direct_verdict=s4_direct_verdict,
        demo_candidate=None,
        historical_preferred=None,
        s4_observable_superiority=None,
    )


def s4_shows_observable_superiority(
    *,
    pooled_e17_s3: float,
    pooled_e17_s4: float,
    min_fold_e17_s3: float,
    min_fold_e17_s4: float,
    pooled_timeout_s3: float,
    pooled_timeout_s4: float,
) -> bool:
    """Report-only: never flips ``campaign_decision`` or promotes S4 to a
    demo candidate -- full promotion always remains ``not_evaluated``."""
    return (
        pooled_e17_s4 >= pooled_e17_s3 + _S4_SUPERIORITY_MIN_POOLED_E17_GAP_BPS
        and min_fold_e17_s4 >= min_fold_e17_s3
        and pooled_timeout_s4 <= pooled_timeout_s3 + _S4_SUPERIORITY_MAX_TIMEOUT_GAP
    )


@dataclass(frozen=True, slots=True)
class StrategyRankMetrics:
    min_fold_e17: float
    pooled_e17: float
    monthly_concentration: float
    timeout_ratio: float


def rank_both_pass(
    *, s3_metrics: StrategyRankMetrics, s4_metrics: StrategyRankMetrics
) -> str:
    """Literal both-pass historical rank: higher minimum-fold E17, then
    higher pooled E17, then lower monthly concentration, then lower
    timeout, then lower operational complexity (S3's single-symbol
    fixed-notional position is always simpler than S4's two-leg basket --
    the final tiebreak always favors S3)."""
    comparisons: tuple[tuple[float, float, bool], ...] = (
        (s3_metrics.min_fold_e17, s4_metrics.min_fold_e17, True),
        (s3_metrics.pooled_e17, s4_metrics.pooled_e17, True),
        (s3_metrics.monthly_concentration, s4_metrics.monthly_concentration, False),
        (s3_metrics.timeout_ratio, s4_metrics.timeout_ratio, False),
    )
    for s3_val, s4_val, higher_is_better in comparisons:
        if s3_val == s4_val:
            continue
        if higher_is_better:
            return "S3" if s3_val > s4_val else "S4"
        return "S3" if s3_val < s4_val else "S4"
    return "S3"

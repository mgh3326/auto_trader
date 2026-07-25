"""ROB-945 (H5) -- historical verdict authority.

Allowed verdicts: ``historical_pass | historical_fail | incomplete``;
readiness is always ``historical_screen_only`` (never conflated with
ROB-905's runtime ``validated`` label; this module creates no validated
gate artifact/path). ``historical_pass`` requires ALL FIVE frozen criteria
(orch approval 2026-07-17, boundary values inclusive except @22):

1. primary @17 OOS expectancy >= 5 bps/trade
2. primary @17 OOS PF >= 1.15
3. at least 4 positive @17 OOS folds
4. @17 positive-month concentration <= 0.50
5. independent @22 OOS expectancy > 0 (STRICT)

Structural/accounting/gap/evidence invalidity (a zero-trade symbol, an
incomplete H6 accounting cross-check, ...) is ``incomplete`` -- never
``historical_fail``. A complete, valid campaign that simply misses a
performance threshold is ``historical_fail``. ``no_positive_months`` is a
FAIL reason (not incomplete): a positive-month sum of zero means net PnL is
already <= 0, so the concentration criterion is unmet, not undecidable
(orch D5 ruling, 2026-07-17).
"""

from __future__ import annotations

from dataclasses import dataclass

from rob945_scenario_metrics import StrategyScenarioAggregate

HISTORICAL_PASS = "historical_pass"
HISTORICAL_FAIL = "historical_fail"
INCOMPLETE = "incomplete"
READINESS = "historical_screen_only"

EXPECTANCY_THRESHOLD_BPS = 5.0
PROFIT_FACTOR_THRESHOLD = 1.15
MIN_POSITIVE_OOS_FOLDS = 4
MAX_MONTHLY_CONCENTRATION = 0.50

REASON_EXPECTANCY_BELOW_THRESHOLD = "expectancy_below_5bp_threshold"
REASON_PF_BELOW_THRESHOLD = "profit_factor_below_1_15_threshold"
REASON_INSUFFICIENT_POSITIVE_FOLDS = "insufficient_positive_oos_folds"
REASON_CONCENTRATION_ABOVE_THRESHOLD = "monthly_concentration_above_50_percent"
REASON_UPWARD_STRESS_NOT_POSITIVE = "upward_stress_expectancy_not_positive"


@dataclass(frozen=True)
class VerdictResult:
    verdict: str
    readiness: str
    reason_codes: tuple[str, ...]


def evaluate_historical_verdict(
    *,
    primary_stress: StrategyScenarioAggregate,
    upward_stress: StrategyScenarioAggregate,
    positive_oos_fold_count: int,
    accounting_complete: bool,
    accounting_incomplete_reason: str | None = None,
) -> VerdictResult:
    incomplete_reasons: set[str] = set()
    if not accounting_complete:
        incomplete_reasons.add(
            accounting_incomplete_reason or "h6_accounting_incomplete"
        )
    if primary_stress.incomplete and primary_stress.incomplete_reason:
        incomplete_reasons.add(primary_stress.incomplete_reason)
    if upward_stress.incomplete and upward_stress.incomplete_reason:
        incomplete_reasons.add(upward_stress.incomplete_reason)

    if incomplete_reasons:
        return VerdictResult(
            verdict=INCOMPLETE,
            readiness=READINESS,
            reason_codes=tuple(sorted(incomplete_reasons)),
        )

    fail_reasons: set[str] = set()

    expectancy = primary_stress.net_expectancy_bps
    if expectancy is None or expectancy < EXPECTANCY_THRESHOLD_BPS:
        fail_reasons.add(REASON_EXPECTANCY_BELOW_THRESHOLD)

    profit_factor = primary_stress.profit_factor
    if not (profit_factor >= PROFIT_FACTOR_THRESHOLD):  # NaN-safe: NaN >= x is False
        fail_reasons.add(REASON_PF_BELOW_THRESHOLD)

    if positive_oos_fold_count < MIN_POSITIVE_OOS_FOLDS:
        fail_reasons.add(REASON_INSUFFICIENT_POSITIVE_FOLDS)

    if primary_stress.monthly_concentration_reason == "no_positive_months":
        fail_reasons.add("no_positive_months")
    elif primary_stress.monthly_concentration is None or (
        primary_stress.monthly_concentration > MAX_MONTHLY_CONCENTRATION
    ):
        fail_reasons.add(REASON_CONCENTRATION_ABOVE_THRESHOLD)

    upward_expectancy = upward_stress.net_expectancy_bps
    if upward_expectancy is None or not (upward_expectancy > 0):
        fail_reasons.add(REASON_UPWARD_STRESS_NOT_POSITIVE)

    if fail_reasons:
        return VerdictResult(
            verdict=HISTORICAL_FAIL,
            readiness=READINESS,
            reason_codes=tuple(sorted(fail_reasons)),
        )

    return VerdictResult(verdict=HISTORICAL_PASS, readiness=READINESS, reason_codes=())

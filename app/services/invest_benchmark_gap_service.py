"""ROB-271 — adapter that turns existing /invest coverage + readiness state into
the product-facing Toss/Naver benchmark gap matrix.

Read-only. No broker/order/watch/scheduler side effects. Never imports broker or
order modules. Never writes to the DB.
"""

from __future__ import annotations

from app.schemas.invest_benchmark_gap import CoverageProductStatus
from app.schemas.invest_coverage import CoverageState

_COVERAGE_TO_PRODUCT: dict[CoverageState, CoverageProductStatus] = {
    "fresh": "covered",
    "stale": "stale",
    "partial": "partial",
    "missing": "missing",
    "unsupported": "unsupported",
    "error": "blocked_by_auth_or_policy",
    "provider_unwired": "candidate_unwired",
}


def coverage_state_to_product_status(state: CoverageState) -> CoverageProductStatus:
    """Map legacy CoverageState into the new product-facing status vocabulary.

    Raises ValueError for unknown values so callers fail loud rather than
    silently emit a default. Two product statuses have no legacy source and are
    only assignable explicitly by a row author:
        - benchmark_only
        - intentionally_excluded
    """
    if state not in _COVERAGE_TO_PRODUCT:
        raise ValueError(f"unknown coverage state: {state!r}")
    return _COVERAGE_TO_PRODUCT[state]

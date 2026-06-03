"""ROB-426 PR2b — shared commit-time coverage guard for snapshot builds.

Blocks committing a thin partition (e.g. a --limit 20 smoke run) to production
unless the operator passes --allow-partial. Distinct from the screener-specific
absolute floors in invest_screener_snapshots/guards.py (which PR2b leaves
unchanged). The denominator is the active universe count
(app.services.invest_screener_snapshots.partition_health.active_universe_count),
computed by the caller and passed in as an int — this module is pure arithmetic.

Threshold is locked here; changing it is a separate telemetry-backed PR.
"""

from __future__ import annotations

import math
from typing import Literal

_MIN_COMMIT_COVERAGE_RATIO = 0.60


class PartialCommitBlocked(RuntimeError):
    """Raised when a commit would persist fewer rows than the coverage floor.

    Carries context for the CLI to print and for Stage-6-style alerting.
    """

    def __init__(
        self,
        message: str,
        *,
        count: int | None = None,
        universe_count: int | None = None,
        min_ratio: float | None = None,
        market: str | None = None,
        metric: str = "rows",
        reason: str | None = None,
    ) -> None:
        super().__init__(message)
        self.count = count
        self.universe_count = universe_count
        self.min_ratio = min_ratio
        self.market = market
        self.metric = metric
        self.reason = reason


def assert_min_coverage(
    count: int,
    universe_count: int,
    *,
    market: Literal["kr", "us"] | str,
    min_ratio: float = _MIN_COMMIT_COVERAGE_RATIO,
    metric: str = "rows",
) -> None:
    """Raise :class:`PartialCommitBlocked` when ``count`` is below the floor.

    floor = ceil(universe_count * min_ratio). When ``universe_count <= 0`` the
    gate is disabled (returns without raising) — never block on a missing
    universe denominator (consistent with PR2a fail-open).
    """
    if universe_count <= 0:
        return
    floor = math.ceil(universe_count * min_ratio)
    if count < floor:
        raise PartialCommitBlocked(
            f"{market} commit blocked: built {count} {metric} < floor {floor} "
            f"({min_ratio:.0%} of active universe {universe_count}); "
            f"pass --allow-partial to commit a partial backfill",
            count=count,
            universe_count=universe_count,
            min_ratio=min_ratio,
            market=market,
            metric=metric,
        )

"""ROB-281 Stage 5 — Commit-time guards for invest screener snapshot builds.

Guards run on the dry-run distribution before any commit. They protect against:

* Mixed-partition distributions (e.g., 90% today + 10% yesterday from a builder
  bug, where committing would create an authoritative-looking minority
  partition that is actually noise).
* Suspiciously small builds (e.g., the data source returned 50 rows instead
  of the expected ~3000 because of a partial upstream outage).

A failed guard raises a typed exception that the caller is expected to route
to Stage 6 Discord alerts before re-raising to TaskIQ.

Thresholds are locked in this module so a future implementer or reviewer can
see them at a glance:

* Dominant partition: at least 70% of rows in a single ``snapshot_date``.
* Min row count: KR ≥ 2500 (observed 3867 on 2026-05-19), US ≥ 3500
  (observed 5116 on 2026-05-19).

Adjusting these floors should be a separate PR with telemetry-backed
justification — do not silently soften during implementation.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal


class SuspiciousDistributionError(RuntimeError):
    """Raised when no single ``snapshot_date`` holds the dominant majority.

    Carries the offending distribution as ``distribution`` for downstream
    alerting (Stage 6 Discord embed).
    """

    distribution: dict[str, int]

    def __init__(
        self,
        message: str,
        *,
        distribution: dict[str, int] | None = None,
    ) -> None:
        super().__init__(message)
        self.distribution = dict(distribution or {})


class InsufficientRowsError(RuntimeError):
    """Raised when ``snapshots_built`` is below the market's row-count floor.

    Carries ``count``, ``market``, and (after enrichment by the guarded
    wrapper) the dry-run ``distribution`` so Stage 6 alerts can surface the
    full context to operators.
    """

    distribution: dict[str, int]
    count: int | None
    market: str | None

    def __init__(
        self,
        message: str,
        *,
        distribution: dict[str, int] | None = None,
        count: int | None = None,
        market: str | None = None,
    ) -> None:
        super().__init__(message)
        self.distribution = dict(distribution or {})
        self.count = count
        self.market = market


_DOMINANT_PARTITION_THRESHOLD = 0.70

_MIN_ROW_THRESHOLD: dict[str, int] = {"kr": 2500, "us": 3500}


def assert_dominant_partition(
    distribution: Mapping[str, int],
    *,
    threshold: float = _DOMINANT_PARTITION_THRESHOLD,
) -> str:
    """Validate that the distribution has a dominant ``snapshot_date``.

    A dominant partition holds at least ``threshold`` fraction (default 70%)
    of all rows. Otherwise the distribution is treated as suspicious and the
    caller refuses to commit.

    Returns the dominant date on success. Raises
    :class:`SuspiciousDistributionError` on violation, with the full
    distribution embedded in the message for operator triage.
    """
    if not distribution:
        raise SuspiciousDistributionError("empty snapshot_date distribution")
    total = sum(distribution.values())
    if total <= 0:
        raise SuspiciousDistributionError(f"non-positive total row count: {total}")
    dominant_date, dominant_count = max(distribution.items(), key=lambda kv: kv[1])
    ratio = dominant_count / total
    if ratio < threshold:
        raise SuspiciousDistributionError(
            f"no dominant partition: top={dominant_date} "
            f"({dominant_count}/{total} = {ratio:.2%}) below "
            f"{threshold:.0%} threshold; distribution={dict(distribution)}",
            distribution=dict(distribution),
        )
    return dominant_date


def assert_min_row_count(count: int, market: Literal["kr", "us"]) -> None:
    """Validate ``snapshots_built`` meets the per-market floor.

    Raises :class:`InsufficientRowsError` when ``count`` is below the
    locked floor for ``market``. Locked floors live in
    :data:`_MIN_ROW_THRESHOLD`; revising them requires a separate PR.
    """
    floor = _MIN_ROW_THRESHOLD.get(market)
    if floor is None:
        raise ValueError(f"unknown market for min-row guard: {market}")
    if count < floor:
        raise InsufficientRowsError(
            f"{market} snapshots_built={count} below floor={floor}",
            count=count,
            market=market,
        )

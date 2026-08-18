"""ROB-1284 — write gates and truncation surfacing.

Two properties:
  1. Flipping ``dry_run`` alone must not produce a write; ``confirm=True`` is a
     separate, explicit act.
  2. A bounded candidate scan must say what it did not look at. "Reconciled N"
     with M rows never scanned reads as full coverage, and that is how a
     phantom population stays invisible for weeks.
"""

from __future__ import annotations

import datetime

import pytest

from app.mcp_server.tooling.proposal_rung_convergence import candidate_scan_coverage
from app.services.order_proposals.resting_sweep_service import (
    RestingRungSweepService,
    SweepNotConfirmed,
)

pytestmark = pytest.mark.unit

NOW = datetime.datetime(2026, 8, 18, 1, 0, tzinfo=datetime.UTC)


class _NoCandidates(RestingRungSweepService):
    """Zero candidates, so `apply` exercises the gates and nothing else."""

    def __init__(self) -> None:
        super().__init__(session=None)  # type: ignore[arg-type]

    async def plan(self, *, now):  # type: ignore[override]
        return []


@pytest.mark.asyncio
async def test_dry_run_is_the_default():
    result = await _NoCandidates().apply(now=NOW)
    assert result["dry_run"] is True
    assert result["applied"] == 0


@pytest.mark.asyncio
async def test_dry_run_false_alone_is_refused():
    """The dangerous flag is not sufficient on its own."""
    with pytest.raises(SweepNotConfirmed):
        await _NoCandidates().apply(now=NOW, dry_run=False)


@pytest.mark.asyncio
async def test_dry_run_false_with_confirm_is_allowed():
    result = await _NoCandidates().apply(now=NOW, dry_run=False, confirm=True)
    assert result["dry_run"] is False


@pytest.mark.asyncio
async def test_confirm_alone_still_plans_only():
    """confirm without dry_run=False must not write either."""
    result = await _NoCandidates().apply(now=NOW, dry_run=True, confirm=True)
    assert result["dry_run"] is True
    assert result["applied"] == 0


def test_truncated_scan_reports_the_shortfall():
    cov = candidate_scan_coverage(scanned=100, open_total=133, limit=100)
    assert cov["truncated"] is True
    assert cov["unscanned"] == 33
    assert "never scanned" in cov["note"]


def test_complete_scan_is_marked_untruncated_without_a_note():
    cov = candidate_scan_coverage(scanned=7, open_total=7, limit=100)
    assert cov["truncated"] is False
    assert cov["unscanned"] == 0
    assert "note" not in cov


def test_unknown_open_total_is_reported_as_unknown_not_as_complete():
    """Not knowing the denominator must never be rendered as full coverage."""
    cov = candidate_scan_coverage(scanned=5, open_total=None, limit=100)
    assert cov["truncated"] is None

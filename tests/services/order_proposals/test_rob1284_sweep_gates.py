"""ROB-1284 — write gates and truncation surfacing.

Two properties:
  1. Flipping ``dry_run`` alone must not produce a write; ``confirm=True`` is a
     separate, explicit act.
  2. A bounded candidate scan must say what it did not look at. "Reconciled N"
     with M rows never scanned reads as full coverage, and that is how a
     phantom population stays invisible for weeks.

**Every gate assertion here runs with candidates > 0.** A gate exercised against
an empty candidate list is not a gate test: ``applied == 0`` is then true for a
service that writes on every row, and a mutant that deletes the ``dry_run``
early return survives it. The ``_NoCandidates`` fixture is kept only for the
``SweepNotConfirmed`` raise, which is candidate-independent by construction; the
write-suppression properties use ``_OneTransition`` / ``_EvidenceReadFails``,
whose plans contain rows the sweep *would* write if a gate were removed.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Any

import pytest

from app.mcp_server.tooling.proposal_rung_convergence import candidate_scan_coverage
from app.services.order_proposals.resting_sweep import (
    LedgerEvidence,
    RungCandidate,
    RungVerdict,
)
from app.services.order_proposals.resting_sweep_service import (
    RestingRungSweepService,
    SweepNotConfirmed,
)

pytestmark = pytest.mark.unit

NOW = datetime.datetime(2026, 8, 18, 1, 0, tzinfo=datetime.UTC)


def _candidate(**over) -> RungCandidate:
    """A rung whose keys and state make it eligible for a real transition."""
    base = {
        "proposal_id": "6e13b685-0000-0000-0000-000000000000",
        "rung_id": 1,
        "rung_index": 1,
        "state": "resting",
        "side": "sell",
        "symbol": "257720",
        "market": "equity_kr",
        "account_mode": "kis_live",
        "broker_order_id": "0023769000",
        "idempotency_key": "idem-1",
        "correlation_id": "corr-1",
        "quantity": Decimal("10"),
        "limit_price": Decimal("43800"),
    }
    base.update(over)
    return RungCandidate(**base)


def _terminal_evidence() -> LedgerEvidence:
    return LedgerEvidence(
        ledger_table="review.kis_live_order_ledger",
        ledger_id=150,
        status="expired",
        match_key="broker_order_id",
        broker_order_id="0023769000",
    )


class _EmptyResult:
    """Whatever a write path asks of a result, it gets nothing back."""

    def one_or_none(self) -> None:
        return None

    def scalar_one_or_none(self) -> None:
        return None

    def first(self) -> None:
        return None

    def scalars(self) -> _EmptyResult:
        return self

    def all(self) -> list[Any]:
        return []


class _RecordingSession:
    """A session that records every DB verb instead of performing it.

    This is the write detector. ``RestingRungSweepService.apply`` reaches the
    database only through ``OrderProposalsService``, whose very first act in
    ``record_fill_evidence_for_rung`` is a ``session.execute``. So an empty
    ``calls`` list is a direct statement that no DB work was attempted — it does
    not depend on monkeypatching the service, and it stays true if the write
    path is later rerouted through ``add``/``flush``/``commit``.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def execute(self, *args: Any, **kwargs: Any) -> _EmptyResult:
        self.calls.append("execute")
        return _EmptyResult()

    async def commit(self) -> None:
        self.calls.append("commit")

    async def flush(self) -> None:
        self.calls.append("flush")

    async def refresh(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append("refresh")

    def add(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append("add")


class _NoCandidates(RestingRungSweepService):
    """Zero candidates, so `apply` exercises the gates and nothing else."""

    def __init__(self) -> None:
        super().__init__(session=None)  # type: ignore[arg-type]

    async def plan(self, *, now):  # type: ignore[override]
        return []


class _OneTransition(RestingRungSweepService):
    """One candidate carrying committed terminal evidence.

    Its plan contains a genuine ``TRANSITION``, so the only thing between
    ``apply`` and a database write is the gate under test.
    """

    def __init__(self) -> None:
        self.session = _RecordingSession()
        super().__init__(session=self.session)  # type: ignore[arg-type]

    async def collect_candidates(self):  # type: ignore[override]
        return [_candidate()]

    async def fetch_evidence(self, candidate):  # type: ignore[override]
        return (_terminal_evidence(),)

    async def verify_ownership(self, candidate, evidence):  # type: ignore[override]
        return evidence, None


class _EvidenceReadFails(RestingRungSweepService):
    """Candidates exist; reading their ledger evidence raises.

    Same candidate shape as ``_OneTransition`` — had the read succeeded these
    rows would have transitioned — so the only reason no transition appears is
    that an unreadable ledger is refused as evidence.
    """

    def __init__(self, n: int = 2) -> None:
        self.session = _RecordingSession()
        super().__init__(session=self.session)  # type: ignore[arg-type]
        self._n = n
        self.reads = 0

    async def collect_candidates(self):  # type: ignore[override]
        return [_candidate(rung_id=i, rung_index=i) for i in range(1, self._n + 1)]

    async def fetch_evidence(self, candidate):  # type: ignore[override]
        self.reads += 1
        raise TimeoutError("ledger read timed out")


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


# ------------------------------------------------ B1: unreadable evidence (r2)


@pytest.mark.asyncio
async def test_evidence_read_failure_never_yields_a_transition():
    """An unreadable ledger is not permission to expire the rung.

    The failure mode this pins: a read timeout gets absorbed as "no terminal
    evidence found, so it must be dead" and the sweep writes a terminal state
    off an exception. Candidates are non-empty and would otherwise transition,
    so `TRANSITION == 0` here is a statement about the branch, not about an
    empty plan.
    """
    svc = _EvidenceReadFails(n=2)
    decisions = await svc.plan(now=NOW)

    assert len(decisions) == 2  # candidates > 0 — the fixture has teeth
    assert svc.reads == 2  # ...and the failing branch is the one that ran
    assert [d for d in decisions if d.verdict is RungVerdict.TRANSITION] == []
    assert {d.verdict for d in decisions} == {RungVerdict.CONFLICT}
    assert {d.reason_code for d in decisions} == {"evidence_read_failed"}
    assert all(d.target_state is None for d in decisions)


@pytest.mark.asyncio
async def test_evidence_read_failure_writes_nothing_even_when_confirmed():
    """Both gates open, candidates present, ledger unreadable: still zero writes."""
    svc = _EvidenceReadFails(n=2)
    result = await svc.apply(now=NOW, dry_run=False, confirm=True)

    assert result["summary"]["candidates"] == 2
    assert result["summary"]["by_verdict"]["TRANSITION"] == 0
    assert svc.session.calls == []  # no DB verb was attempted
    assert result["applied"] == 0
    assert result["failed"] == 0  # not "tried and failed" — never tried


# --------------------------------------------- B2: dry_run with live candidates


@pytest.mark.asyncio
async def test_dry_run_writes_nothing_with_transitionable_candidates():
    """The dry_run gate, tested where it can actually fail.

    The plan holds a real TRANSITION, so removing the ``if dry_run: return``
    early exit sends this straight into ``record_fill_evidence_for_rung`` and
    the recording session sees ``execute``.
    """
    svc = _OneTransition()
    result = await svc.apply(now=NOW)  # dry_run defaults to True

    assert result["dry_run"] is True
    assert result["summary"]["candidates"] == 1  # candidates > 0
    assert result["summary"]["by_verdict"]["TRANSITION"] == 1  # ...and writable
    assert svc.session.calls == []  # DB writes: zero
    assert result["applied"] == 0
    assert result["failed"] == 0


@pytest.mark.asyncio
async def test_confirm_with_dry_run_still_writes_nothing_with_live_candidates():
    """`confirm=True` is not an override of `dry_run=True`, even with a target."""
    svc = _OneTransition()
    result = await svc.apply(now=NOW, dry_run=True, confirm=True)

    assert result["dry_run"] is True
    assert result["summary"]["by_verdict"]["TRANSITION"] == 1
    assert svc.session.calls == []
    assert result["applied"] == 0


@pytest.mark.asyncio
async def test_dry_run_false_alone_is_refused_with_live_candidates():
    """The refusal must hold when there is something real to write, too."""
    svc = _OneTransition()
    with pytest.raises(SweepNotConfirmed):
        await svc.apply(now=NOW, dry_run=False)
    assert svc.session.calls == []


@pytest.mark.asyncio
async def test_report_is_read_only_with_transitionable_candidates():
    """`report` is the operator's read surface; it must never write either."""
    svc = _OneTransition()
    payload = await svc.report(now=NOW)

    assert payload["dry_run"] is True
    assert payload["summary"]["by_verdict"]["TRANSITION"] == 1
    assert svc.session.calls == []


# ------------------------------------------------- S1: dropped-row conflicts


class _PartialOwnership(RestingRungSweepService):
    """Ownership keeps one terminal row and refuses another.

    The refused row is exactly the shape ``verify_ownership`` exists to catch —
    a ledger row whose keys resolve to some other rung. What is under test is
    what happens to the *fact of the refusal* once a clean-looking subset
    survives it.
    """

    def __init__(self) -> None:
        self.session = _RecordingSession()
        super().__init__(session=self.session)  # type: ignore[arg-type]

    async def collect_candidates(self):  # type: ignore[override]
        return [_candidate()]

    async def fetch_evidence(self, candidate):  # type: ignore[override]
        return (_terminal_evidence(),)

    async def verify_ownership(self, candidate, evidence):  # type: ignore[override]
        # One row survives; a second, refused row is reported as a conflict.
        return evidence, "proposal_evidence_conflict"


@pytest.mark.asyncio
async def test_dropped_row_conflict_is_not_lost_behind_surviving_evidence():
    """A conflict observed and then dropped is a conflict nobody ever sees.

    Before this, ``plan`` kept the conflict only when *every* row was dropped:
    if any owned row survived, the surviving subset was classified as though
    the refusal had never happened — and a TRANSITION written off it.
    """
    svc = _PartialOwnership()
    decisions = await svc.plan(now=NOW)

    assert len(decisions) == 1
    d = decisions[0]
    assert d.ownership_conflict == "proposal_evidence_conflict"  # fact survives
    assert d.verdict is RungVerdict.CONFLICT  # ...and blocks the write
    assert d.reason_code == "ownership_conflict_with_partial_evidence"
    assert d.target_state is None
    assert d.as_row()["ownership_conflict"] == "proposal_evidence_conflict"


@pytest.mark.asyncio
async def test_partially_attributable_evidence_writes_nothing_when_confirmed():
    svc = _PartialOwnership()
    result = await svc.apply(now=NOW, dry_run=False, confirm=True)

    assert result["summary"]["candidates"] == 1
    assert result["summary"]["by_verdict"]["TRANSITION"] == 0
    assert result["summary"]["ownership_conflicts"] == 1  # visible in the total
    assert svc.session.calls == []
    assert result["applied"] == 0


@pytest.mark.asyncio
async def test_clean_ownership_still_transitions():
    """The conflict guard must not swallow the honest case (no regression)."""
    svc = _OneTransition()
    decisions = await svc.plan(now=NOW)

    assert len(decisions) == 1
    assert decisions[0].verdict is RungVerdict.TRANSITION
    assert decisions[0].ownership_conflict is None
    assert decisions[0].target_state == "expired"


# ------------------------------------------------------ S2: starvation surfacing


def _scan(**over):
    base = {
        "scanned": 100,
        "open_total": 133,
        "limit": 100,
        "oldest_scanned_at": datetime.datetime(2026, 7, 17, tzinfo=datetime.UTC),
        "newest_scanned_at": datetime.datetime(2026, 8, 13, tzinfo=datetime.UTC),
        "now": NOW,
    }
    base.update(over)
    return candidate_scan_coverage(**base)


def test_shortfall_names_the_frontier_nothing_newer_is_ever_reached():
    """The unreached rows are identifiable, not just countable."""
    cov = _scan()
    assert cov["unscanned"] == 33
    assert cov["unreached_created_after"] == "2026-08-13T00:00:00+00:00"
    assert cov["scan_order"] == "created_at ASC (oldest-first)"


def test_shortfall_reports_how_long_the_slot_holder_has_been_sitting():
    """A slot holder measured in weeks is what makes the shortfall permanent."""
    cov = _scan()
    assert cov["oldest_scanned_age_days"] == 32  # 2026-07-17 -> 2026-08-18


def test_shortfall_is_described_as_persistent_not_as_a_backlog():
    """ "Backlog" implies it drains next pass. Oldest-first means it does not."""
    cov = _scan()
    assert "persistent" in cov["note"]
    assert "not a draining backlog" in cov["note"]


def test_starvation_fields_are_omitted_when_the_caller_cannot_supply_them():
    """Unknown must read as unknown — never as zero days / no frontier."""
    cov = candidate_scan_coverage(scanned=100, open_total=133, limit=100)
    assert cov["unscanned"] == 33
    assert cov["unreached_created_after"] is None
    assert cov["oldest_scanned_age_days"] is None


def test_complete_scan_reports_no_starvation_fields():
    cov = _scan(scanned=133, open_total=133)
    assert cov["truncated"] is False
    assert "unreached_created_after" not in cov
    assert "note" not in cov

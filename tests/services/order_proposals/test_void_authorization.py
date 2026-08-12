"""ROB-1238 proposal lifecycle safety.

Named mutants, each with the change it is meant to catch:

* ``BSX`` -- a loss-guard-violating proposal survives ``proposed`` behind a live
  approval button and cannot be retired.
* ``phantom-27`` -- dead proposals block, or are blocked by, a healthy one.
* ``cross-lane`` -- one session voids another lane's live proposal. Highest
  severity: proposals are a shared surface.
* ``atomicity`` -- the proposal is retired between the last gate and the submit,
  and the submit proceeds anyway off the stale snapshot.
* ``scheduler`` -- convergence is implemented by registering a recurring job.
"""

from __future__ import annotations

import ast
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.services.order_proposals import OrderProposalsService
from app.services.order_proposals.errors import (
    OrderProposalDispatchNoLongerAuthorized,
    OrderProposalVoidNotAuthorized,
)
from app.services.order_proposals.service import RungInput
from app.services.order_proposals.void_authorization import (
    LOSS_GUARD_VERDICT_KEY,
    SERVER_LOSS_GUARD_SOURCE,
    authorize_void,
    extract_creator_agent_id,
    extract_loss_guard_violation,
    is_server_confirmed_expired,
)

_OWNER = "agent-owner"
_OTHER_LANE = "agent-other-lane"
_NOW = datetime(2026, 8, 11, 5, 0, tzinfo=UTC)


async def _create(
    db_session,
    *,
    creator_agent_id: str | None = _OWNER,
    valid_until: datetime | None = None,
    symbol: str = "BSX",
    side: str = "sell",
    now: datetime = _NOW,
):
    service = OrderProposalsService(db_session)
    group = await service.create_proposal(
        symbol=symbol,
        market="equity_us",
        account_mode="kis_live",
        side=side,
        order_type="limit",
        proposer="session",
        rungs=[RungInput(0, side, Decimal("3"), Decimal("48.40"), None)],
        valid_until=valid_until or (now + timedelta(days=1)),
        creator_agent_id=creator_agent_id,
        now=now,
    )
    await db_session.commit()
    return service, group


# --------------------------------------------------------------------------
# mutant: cross-lane (highest severity)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_other_lane_cannot_void_a_live_proposal(db_session):
    """MUTANT cross-lane: another agent must not retire a live proposal."""
    service, group = await _create(db_session)

    with pytest.raises(OrderProposalVoidNotAuthorized) as excinfo:
        await service.void_proposal(
            group.proposal_id,
            reason="cleaning up someone else's row",
            now=_NOW,
            requester_agent_id=_OTHER_LANE,
        )

    assert excinfo.value.decision.reason_code == "void_not_authorized"
    assert excinfo.value.decision.authority is None
    refreshed, rungs = await service.get_proposal(group.proposal_id)
    # Untouched: still live, still approvable, nonce intact.
    assert refreshed.lifecycle_state == "proposed"
    assert refreshed.no_resubmit is False
    assert refreshed.void_reason is None
    assert [rung.state for rung in rungs] == ["pending_approval"]


@pytest.mark.asyncio
async def test_unidentified_caller_cannot_void_a_live_proposal(db_session):
    """MUTANT cross-lane: no caller identity must not mean owner identity."""
    service, group = await _create(db_session)

    with pytest.raises(OrderProposalVoidNotAuthorized):
        await service.void_proposal(
            group.proposal_id,
            reason="anonymous cleanup",
            now=_NOW,
            requester_agent_id=None,
        )

    refreshed, _ = await service.get_proposal(group.proposal_id)
    assert refreshed.lifecycle_state == "proposed"


@pytest.mark.asyncio
async def test_legacy_row_without_creator_is_not_owned_by_anyone(db_session):
    """MUTANT cross-lane: a missing creator must not match a missing requester.

    The 27 phantom rows predate ownership recording. ``None == None`` must not
    hand every agent owner authority over all of them.
    """
    service, group = await _create(db_session, creator_agent_id=None)

    with pytest.raises(OrderProposalVoidNotAuthorized):
        await service.void_proposal(
            group.proposal_id,
            reason="legacy cleanup",
            now=_NOW,
            requester_agent_id=None,
        )
    assert extract_creator_agent_id(group.source_asof) is None


@pytest.mark.asyncio
async def test_owner_may_void_its_own_live_proposal(db_session):
    service, group = await _create(db_session)

    rows = await service.void_proposal(
        group.proposal_id,
        reason="thesis invalidated",
        now=_NOW,
        requester_agent_id=_OWNER,
    )

    assert [row.state for row in rows] == ["voided"]
    refreshed, _ = await service.get_proposal(group.proposal_id)
    assert refreshed.lifecycle_state == "voided"
    assert "authority=self_created" in refreshed.void_reason


@pytest.mark.asyncio
async def test_caller_supplied_creator_id_cannot_forge_ownership(db_session):
    """A caller-injected ``source_asof`` must not pre-seed someone else's id."""
    service = OrderProposalsService(db_session)
    group = await service.create_proposal(
        symbol="BSX",
        market="equity_us",
        account_mode="kis_live",
        side="sell",
        order_type="limit",
        proposer="session",
        rungs=[RungInput(0, "sell", Decimal("3"), Decimal("48.40"), None)],
        valid_until=_NOW + timedelta(days=1),
        source_asof={"creator_agent_id": _OWNER},
        creator_agent_id=None,
        now=_NOW,
    )
    await db_session.commit()

    assert extract_creator_agent_id(group.source_asof) is None
    with pytest.raises(OrderProposalVoidNotAuthorized):
        await service.void_proposal(
            group.proposal_id,
            reason="forged ownership",
            now=_NOW,
            requester_agent_id=_OWNER,
        )


# --------------------------------------------------------------------------
# mutant: BSX
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bsx_loss_guard_violation_makes_proposal_retirable(db_session):
    """MUTANT BSX: a server-confirmed loss-guard violation must be retirable.

    Reproduces dd7a68d7 -- a sell limit below avg x 1.01 that stayed ``proposed``
    for three weeks with a tappable approval button while the session could only
    leave a warning.
    """
    service, group = await _create(db_session)
    await service.set_approval_nonce(group.proposal_id, "bsx-nonce")

    # Before the server sees the violation, another session still cannot touch it.
    with pytest.raises(OrderProposalVoidNotAuthorized):
        await service.void_proposal(
            group.proposal_id,
            reason="looks wrong",
            now=_NOW,
            requester_agent_id=_OTHER_LANE,
        )

    await service.record_loss_guard_verdict(
        group.proposal_id,
        rung_index=0,
        error="Sell price 48.40 below minimum (avg_buy_price * 1.01 = 48.48)",
        now=_NOW,
    )

    rows = await service.void_proposal(
        group.proposal_id,
        reason="loss guard violation",
        now=_NOW,
        requester_agent_id=_OTHER_LANE,
    )

    assert [row.state for row in rows] == ["voided"]
    refreshed, _ = await service.get_proposal(group.proposal_id)
    assert "authority=server_loss_guard_invalid" in refreshed.void_reason
    # The approval button is dead: no nonce survives the retirement.
    assert refreshed.approval_nonce is None
    assert refreshed.no_resubmit is True


@pytest.mark.asyncio
async def test_bsx_loss_guard_violation_blocks_a_late_dispatch(db_session):
    """MUTANT BSX: a late approval tap must not dispatch the violating order."""
    service, group = await _create(db_session)
    await service.set_approval_nonce(group.proposal_id, "bsx-nonce")
    await service.record_loss_guard_verdict(
        group.proposal_id,
        rung_index=0,
        error="Sell price 48.40 below minimum (avg_buy_price * 1.01 = 48.48)",
        now=_NOW,
    )

    with pytest.raises(OrderProposalDispatchNoLongerAuthorized) as excinfo:
        await service.assert_dispatch_still_authorized(
            group.proposal_id,
            now=_NOW,
            expected_nonce="bsx-nonce",
        )

    assert excinfo.value.reason == "proposal_loss_guard_invalid"
    refreshed, _ = await service.get_proposal(group.proposal_id)
    assert refreshed.approval_nonce is None


@pytest.mark.asyncio
async def test_transient_guard_blocks_do_not_grant_void_authority(db_session):
    """A guard block that is not the loss guard must not open the void path.

    Insufficient balance / opposite-pending / cooldown are account conditions,
    not proof that the proposal itself is invalid.
    """
    service, group = await _create(db_session)
    for envelope in (
        {"violated": False, "source": SERVER_LOSS_GUARD_SOURCE},
        {"violated": True, "source": "caller_supplied"},
        {"violated": "yes", "source": SERVER_LOSS_GUARD_SOURCE},
        {"source": SERVER_LOSS_GUARD_SOURCE},
        "insufficient balance",
    ):
        assert extract_loss_guard_violation({LOSS_GUARD_VERDICT_KEY: envelope}) is None

    with pytest.raises(OrderProposalVoidNotAuthorized):
        await service.void_proposal(
            group.proposal_id,
            reason="transient guard",
            now=_NOW,
            requester_agent_id=_OTHER_LANE,
        )


# --------------------------------------------------------------------------
# mutant: phantom-27
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expired_phantom_converges_to_expired_not_voided(db_session):
    """MUTANT phantom-27: the lazy convergence path retires dead rows.

    Expiry is what the server proved, so the rungs land in ``expired``. Calling
    them ``voided`` would misreport why they died.
    """
    created_at = _NOW - timedelta(days=21)
    service, group = await _create(
        db_session,
        valid_until=created_at + timedelta(days=1),
        now=created_at,
    )
    await service.set_approval_nonce(group.proposal_id, "phantom-nonce")

    rows = await service.void_proposal(
        group.proposal_id,
        reason="phantom cleanup",
        now=_NOW,
        requester_agent_id=_OTHER_LANE,
    )

    assert [row.state for row in rows] == ["expired"]
    refreshed, _ = await service.get_proposal(group.proposal_id)
    assert refreshed.lifecycle_state == "expired"
    assert "authority=server_expired" in refreshed.void_reason
    assert refreshed.approval_nonce is None


@pytest.mark.asyncio
async def test_phantoms_do_not_block_a_healthy_proposal_dispatch(db_session):
    """MUTANT phantom-27: dead rows must not gate an unrelated live proposal."""
    stale_at = _NOW - timedelta(days=21)
    service, phantom = await _create(
        db_session,
        valid_until=stale_at + timedelta(days=1),
        now=stale_at,
        symbol="PHANTOM",
    )
    _, healthy = await _create(db_session, symbol="LIVE")
    await service.set_approval_nonce(healthy.proposal_id, "healthy-nonce")

    await service.void_proposal(
        phantom.proposal_id,
        reason="phantom cleanup",
        now=_NOW,
        requester_agent_id=_OTHER_LANE,
    )

    # The healthy proposal is untouched and still dispatchable.
    group = await service.assert_dispatch_still_authorized(
        healthy.proposal_id,
        now=_NOW,
        expected_nonce="healthy-nonce",
    )
    assert group.lifecycle_state == "proposed"
    assert group.approval_nonce == "healthy-nonce"


# --------------------------------------------------------------------------
# mutant: atomicity
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_gate_refuses_after_a_concurrent_void(db_session):
    """MUTANT atomicity: a retirement landing after the last gate must abort."""
    service, group = await _create(db_session)
    await service.set_approval_nonce(group.proposal_id, "race-nonce")

    # Passes while the proposal is live...
    await service.assert_dispatch_still_authorized(
        group.proposal_id, now=_NOW, expected_nonce="race-nonce"
    )
    # ...then the owner retires it in the gap before the submit.
    await service.void_proposal(
        group.proposal_id,
        reason="pulled",
        now=_NOW,
        requester_agent_id=_OWNER,
    )

    with pytest.raises(OrderProposalDispatchNoLongerAuthorized) as excinfo:
        await service.assert_dispatch_still_authorized(
            group.proposal_id, now=_NOW, expected_nonce="race-nonce"
        )
    assert excinfo.value.reason == "proposal_terminal:voided"


@pytest.mark.asyncio
async def test_dispatch_gate_refuses_when_valid_until_elapsed(db_session):
    service, group = await _create(db_session)
    await service.set_approval_nonce(group.proposal_id, "late-nonce")

    with pytest.raises(OrderProposalDispatchNoLongerAuthorized) as excinfo:
        await service.assert_dispatch_still_authorized(
            group.proposal_id,
            now=_NOW + timedelta(days=2),
            expected_nonce="late-nonce",
        )
    assert excinfo.value.reason == "proposal_now_at_or_after_valid_until"


@pytest.mark.asyncio
async def test_dispatch_gate_refuses_a_superseded_approval_token(db_session):
    service, group = await _create(db_session)
    await service.set_approval_nonce(group.proposal_id, "current-nonce")

    with pytest.raises(OrderProposalDispatchNoLongerAuthorized) as excinfo:
        await service.assert_dispatch_still_authorized(
            group.proposal_id, now=_NOW, expected_nonce="stale-nonce"
        )
    assert excinfo.value.reason == "approval_nonce_superseded"


@pytest.mark.asyncio
async def test_dispatch_gate_burns_the_token_when_it_refuses(db_session):
    """A refused dispatch must not leave a replayable approval token behind."""
    service, group = await _create(db_session)
    await service.set_approval_nonce(group.proposal_id, "burn-nonce")
    await service.record_loss_guard_verdict(
        group.proposal_id,
        rung_index=0,
        error="Sell price 48.40 below minimum (avg_buy_price * 1.01 = 48.48)",
        now=_NOW,
    )

    with pytest.raises(OrderProposalDispatchNoLongerAuthorized):
        await service.assert_dispatch_still_authorized(
            group.proposal_id, now=_NOW, expected_nonce="burn-nonce"
        )

    refreshed, _ = await service.get_proposal(group.proposal_id)
    assert refreshed.approval_nonce is None
    assert refreshed.approval_nonce_used_at is not None


# --------------------------------------------------------------------------
# mutant: scheduler
# --------------------------------------------------------------------------


_ROB1238_MODULES = (
    "app/services/order_proposals/void_authorization.py",
    "app/services/order_proposals/service.py",
    "app/services/order_proposals/revalidation.py",
    "app/mcp_server/tooling/order_proposal_tools.py",
)
_SCHEDULER_TOKENS = (
    "schedule",
    "crontab",
    "cron",
    "apscheduler",
    "prefect",
    "launchd",
    "taskiq",
    "AsyncIOScheduler",
    "BackgroundScheduler",
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


@pytest.mark.parametrize("relative_path", _ROB1238_MODULES)
def test_no_scheduler_registration_in_lifecycle_modules(relative_path):
    """MUTANT scheduler: convergence must stay lazy, never a recurring job.

    The design is dispatch-time / on-demand: nothing here may register a cron,
    launchd job, Prefect deployment or APScheduler trigger. Import lines are
    what would wire one up, so those are what we scan.
    """
    source = (_repo_root() / relative_path).read_text()
    tree = ast.parse(source)
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)

    offenders = [
        name
        for name in imported
        for token in _SCHEDULER_TOKENS
        if token.lower() in name.lower()
    ]
    assert offenders == [], f"{relative_path} imports a scheduler: {offenders}"


def test_void_authorization_is_pure_stdlib():
    """The authorization rule must stay testable without DB, clock or broker."""
    source = (
        _repo_root() / "app/services/order_proposals/void_authorization.py"
    ).read_text()
    tree = ast.parse(source)
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)

    assert all(not name.startswith("app.") for name in imported), imported
    forbidden = ("sqlalchemy", "httpx", "redis", "requests", "aiohttp")
    assert not [n for n in imported if n.split(".")[0] in forbidden], imported


# --------------------------------------------------------------------------
# pure-rule coverage
# --------------------------------------------------------------------------


def test_authorize_void_precedence_and_terminal_states():
    expired_at = _NOW - timedelta(hours=1)

    owner = authorize_void(
        requester_agent_id=_OWNER,
        creator_agent_id=_OWNER,
        valid_until=expired_at,
        now=_NOW,
    )
    assert (owner.authority, owner.terminal_state) == ("self_created", "voided")

    guard = authorize_void(
        requester_agent_id=_OTHER_LANE,
        creator_agent_id=_OWNER,
        valid_until=_NOW + timedelta(hours=1),
        now=_NOW,
        loss_guard_violation={"loss_guard_source": SERVER_LOSS_GUARD_SOURCE},
    )
    assert (guard.authority, guard.terminal_state) == (
        "server_loss_guard_invalid",
        "voided",
    )

    expired = authorize_void(
        requester_agent_id=_OTHER_LANE,
        creator_agent_id=_OWNER,
        valid_until=expired_at,
        now=_NOW,
    )
    assert (expired.authority, expired.terminal_state) == ("server_expired", "expired")

    blocked = authorize_void(
        requester_agent_id=_OTHER_LANE,
        creator_agent_id=_OWNER,
        valid_until=_NOW + timedelta(hours=1),
        now=_NOW,
    )
    assert blocked.allowed is False
    assert blocked.authority is None


def test_missing_valid_until_is_not_evidence_of_expiry():
    assert is_server_confirmed_expired(None, now=_NOW) is False
    decision = authorize_void(
        requester_agent_id=_OTHER_LANE,
        creator_agent_id=_OWNER,
        valid_until=None,
        now=_NOW,
    )
    assert decision.allowed is False


def test_valid_until_boundary_is_inclusive():
    """``now == valid_until`` counts as expired, matching valid_until_block."""
    assert is_server_confirmed_expired(_NOW, now=_NOW) is True
    assert is_server_confirmed_expired(_NOW + timedelta(microseconds=1), now=_NOW) is (
        False
    )


def test_ownership_comparison_is_exact_after_stripping():
    assert (
        authorize_void(
            requester_agent_id=f"  {_OWNER} ",
            creator_agent_id=_OWNER,
            valid_until=_NOW + timedelta(hours=1),
            now=_NOW,
        ).authority
        == "self_created"
    )
    for impostor in (f"{_OWNER}-2", _OWNER.upper(), "", "   "):
        assert (
            authorize_void(
                requester_agent_id=impostor,
                creator_agent_id=_OWNER,
                valid_until=_NOW + timedelta(hours=1),
                now=_NOW,
            ).allowed
            is False
        ), impostor


def test_naive_datetimes_are_rejected_rather_than_silently_compared():
    with pytest.raises(ValueError):
        is_server_confirmed_expired(datetime(2026, 8, 11, 5, 0), now=_NOW)


@pytest.mark.asyncio
async def test_void_of_unknown_proposal_still_raises_not_found(db_session):
    from app.services.order_proposals.errors import OrderProposalNotFound

    service = OrderProposalsService(db_session)
    with pytest.raises(OrderProposalNotFound):
        await service.void_proposal(
            uuid.uuid4(),
            reason="missing",
            now=_NOW,
            requester_agent_id=_OWNER,
        )

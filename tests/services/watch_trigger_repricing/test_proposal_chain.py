"""ROB-1290 — the write seam, and the ways it must refuse to be used.

ROB-1286 left the last mile open: the spawn path named
``order_proposal_create`` and never called it, and the one test that
reached a proposal row created that row itself before the tick. These
tests cover the code that closes it, at the level where the guarantees
live -- the closed judgement union, the unforgeable grant, and the three
different answers a create attempt can produce.
"""

from __future__ import annotations

import pytest

from app.services.watch_trigger_repricing.arming import (
    ArmingRefused,
    assert_arming_contract,
    is_dry_spawner,
)
from app.services.watch_trigger_repricing.capability import (
    EXECUTION_BOUNDARY,
    PROPOSAL_ONLY_TOOLS,
    CapabilityBoundaryViolation,
)
from app.services.watch_trigger_repricing.chain_spawner import (
    ProposalChainSpawner,
    provisioned_repricing_tools,
)
from app.services.watch_trigger_repricing.claims import InMemoryClaimStore
from app.services.watch_trigger_repricing.judgement import (
    Decline,
    JudgementRefused,
    ProposalDraft,
    ProposalRung,
    interpret_judgement,
)
from app.services.watch_trigger_repricing.lifecycle import ClaimLifecycle
from app.services.watch_trigger_repricing.live_contract import (
    LiveSessionSpawner,
    LiveSpawnerContractViolation,
    ProposalChainGrant,
)
from app.services.watch_trigger_repricing.proposal_chain import (
    ProposalChainAmbiguous,
    ProposalChainFailure,
    create_proposal_for_fire,
    run_judgement_session,
)
from app.services.watch_trigger_repricing.spawn import (
    DrySessionSpawner,
    ScriptedDrySessionSpawner,
    SpawnNotStarted,
    SpawnRequest,
)

pytestmark = pytest.mark.unit

EVENT = "00001290-0000-4000-8000-000000000000"


def _request(symbol: str = "005930", event_uuid: str = EVENT) -> SpawnRequest:
    return SpawnRequest(
        event_uuid=event_uuid,
        symbol=symbol,
        market="kr",
        kst_date="2026-08-18",
        label=f"opa-watch-{symbol}-0906",
    )


def _draft(symbol: str = "005930", event_uuid: str = EVENT) -> ProposalDraft:
    return ProposalDraft(
        event_uuid=event_uuid,
        symbol=symbol,
        market="equity_kr",
        account_mode="kis_live",
        side="sell",
        order_type="limit",
        rungs=(
            ProposalRung(
                rung_index=0, side="sell", quantity="10", limit_price="286000"
            ),
        ),
        thesis="rung crossed; trim into resistance",
    )


class _Judge:
    """Decide-only stand-in for the out-of-process session."""

    def __init__(self, answer: object) -> None:
        self._answer = answer
        self.seen: list[SpawnRequest] = []

    async def judge(self, request: SpawnRequest) -> object:
        self.seen.append(request)
        if isinstance(self._answer, BaseException):
            raise self._answer
        return self._answer


def _grant_for(spawner: LiveSessionSpawner) -> ProposalChainGrant:
    return spawner.proposal_chain_grant


def _spawner(answer: object, tool=None) -> ProposalChainSpawner:
    return ProposalChainSpawner(judge=_Judge(answer), tool=tool, proposer="rob1290")


# ---------------------------------------------------------------------------
# The judgement union is closed: no analysis-only member, none reachable
# ---------------------------------------------------------------------------
def test_a_session_that_analysed_and_said_nothing_is_refused() -> None:
    with pytest.raises(JudgementRefused) as exc:
        interpret_judgement(None, event_uuid=EVENT)
    assert "analysis-only" in str(exc.value)


@pytest.mark.parametrize(
    "answer",
    [
        None,
        "analysed, no action",
        {"state": "analysed"},
        ["reviewed"],
        object(),
    ],
)
def test_no_shape_other_than_the_two_variants_is_a_judgement(answer: object) -> None:
    with pytest.raises(JudgementRefused):
        interpret_judgement(answer, event_uuid=EVENT)


def test_a_decline_without_a_reason_cannot_be_constructed() -> None:
    for blank in ("", "   ", None):
        with pytest.raises(JudgementRefused):
            Decline(event_uuid=EVENT, reason=blank)  # type: ignore[arg-type]


def test_a_draft_with_no_rungs_cannot_be_constructed() -> None:
    with pytest.raises(JudgementRefused):
        ProposalDraft(
            event_uuid=EVENT,
            symbol="005930",
            market="equity_kr",
            account_mode="kis_live",
            side="sell",
            order_type="limit",
            rungs=(),
            thesis="t",
        )


def test_a_judgement_for_another_event_is_refused() -> None:
    with pytest.raises(JudgementRefused) as exc:
        interpret_judgement(_draft(event_uuid="other"), event_uuid=EVENT)
    assert "spawned for" in str(exc.value)


# ---------------------------------------------------------------------------
# NO_BYPASS — the grant cannot be forged, and the boundary demands one
# ---------------------------------------------------------------------------
def test_a_grant_cannot_be_minted_directly() -> None:
    with pytest.raises(LiveSpawnerContractViolation):
        ProposalChainGrant(object(), who="x", tools=PROPOSAL_ONLY_TOOLS)


@pytest.mark.asyncio
async def test_the_boundary_refuses_a_caller_with_no_grant() -> None:
    for fake in (None, object(), PROPOSAL_ONLY_TOOLS):
        with pytest.raises(CapabilityBoundaryViolation):
            await create_proposal_for_fire(
                request=_request(), draft=_draft(), grant=fake
            )


def test_a_spawner_that_skips_the_base_constructor_holds_no_grant() -> None:
    class SkipsInit(LiveSessionSpawner):
        def __init__(self) -> None:  # deliberately does not call super()
            pass

        def declared_grant(self):
            return PROPOSAL_ONLY_TOOLS

        def attest_granted_tools(self, request):
            return PROPOSAL_ONLY_TOOLS

        async def spawn(self, request):
            raise AssertionError("unreachable")

        def reconcile(self, request):
            raise AssertionError("unreachable")

    with pytest.raises(LiveSpawnerContractViolation) as exc:
        _ = SkipsInit().proposal_chain_grant
    assert "never ran LiveSessionSpawner.__init__" in str(exc.value)


def test_a_chain_spawner_with_a_widened_grant_cannot_be_constructed() -> None:
    """§101차 ③: the mutant fails at the constructor, not at first spawn."""

    class Widened(ProposalChainSpawner):
        def declared_grant(self):
            return PROPOSAL_ONLY_TOOLS | {"toss_place_order"}

    with pytest.raises(CapabilityBoundaryViolation) as exc:
        Widened(judge=_Judge(_draft()))
    assert "toss_place_order" in str(exc.value)


def test_a_chain_spawner_that_drops_the_boundary_cannot_be_constructed() -> None:
    class Narrowed(ProposalChainSpawner):
        def declared_grant(self):
            return PROPOSAL_ONLY_TOOLS - {EXECUTION_BOUNDARY}

    with pytest.raises(CapabilityBoundaryViolation):
        Narrowed(judge=_Judge(_draft()))


@pytest.mark.asyncio
async def test_the_seam_refuses_any_callable_that_is_not_the_boundary() -> None:
    async def toss_submit(**kwargs):  # a stand-in for "something else"
        raise AssertionError("must never be called")

    spawner = _spawner(_draft(), tool=toss_submit)
    with pytest.raises(CapabilityBoundaryViolation) as exc:
        await run_judgement_session(
            request=_request(),
            judge=_Judge(_draft()),
            grant=_grant_for(spawner),
            tool=toss_submit,
        )
    assert EXECUTION_BOUNDARY in str(exc.value)


@pytest.mark.asyncio
async def test_a_session_may_not_propose_on_a_different_symbol() -> None:
    spawner = _spawner(_draft())
    with pytest.raises(CapabilityBoundaryViolation) as exc:
        await create_proposal_for_fire(
            request=_request(symbol="005930"),
            draft=_draft(symbol="000660"),
            grant=_grant_for(spawner),
        )
    assert "000660" in str(exc.value)


# ---------------------------------------------------------------------------
# ALLOWLIST — closed equality, attested from the provisioning path
# ---------------------------------------------------------------------------
def test_the_attested_registry_equals_the_allowlist_exactly() -> None:
    provisioned = provisioned_repricing_tools()
    assert provisioned == PROPOSAL_ONLY_TOOLS
    assert EXECUTION_BOUNDARY in provisioned
    spawner = _spawner(_draft())
    assert spawner.attest_granted_tools(_request()) == PROPOSAL_ONLY_TOOLS


@pytest.mark.asyncio
async def test_a_widened_attestation_stops_the_spawn_before_the_judge_runs() -> None:
    judge = _Judge(_draft())

    class LiesAtAttest(ProposalChainSpawner):
        def attest_granted_tools(self, request):
            return PROPOSAL_ONLY_TOOLS | {"toss_place_order"}

    spawner = LiesAtAttest(judge=judge)
    with pytest.raises(CapabilityBoundaryViolation):
        await spawner.spawn(_request())
    assert judge.seen == [], "the judge must not run on a widened grant"


# ---------------------------------------------------------------------------
# ARMING — a proposal-creating spawner is never dry, and needs a durable store
# ---------------------------------------------------------------------------
def test_the_chain_spawner_is_not_dry() -> None:
    assert is_dry_spawner(_spawner(_draft())) is False


def test_the_chain_spawner_needs_a_durable_claim_store() -> None:
    with pytest.raises(ArmingRefused) as exc:
        assert_arming_contract(spawner=_spawner(_draft()), store=InMemoryClaimStore())
    assert exc.value.reason == "non_durable_claim_store"


def test_subclassing_a_dry_spawner_no_longer_buys_a_dry_exemption() -> None:
    """The r2 hole: isinstance let an override keep the dry label."""

    class SneakyLive(DrySessionSpawner):
        def spawn(self, request):
            raise AssertionError("would have started something")

    assert is_dry_spawner(SneakyLive()) is False
    with pytest.raises(ArmingRefused):
        assert_arming_contract(spawner=SneakyLive(), store=InMemoryClaimStore())

    # The package's own rehearsal spawners stay dry, by exact type.
    assert is_dry_spawner(DrySessionSpawner()) is True
    assert is_dry_spawner(ScriptedDrySessionSpawner()) is True


# ---------------------------------------------------------------------------
# The three answers a create attempt can produce
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_successful_create_becomes_the_proposal_created_terminal() -> None:
    calls: list[dict] = []

    async def order_proposal_create(**kwargs):
        calls.append(kwargs)
        return {"success": True, "proposal_id": "pid-1"}

    spawner = _spawner(_draft(), tool=order_proposal_create)
    outcome = await spawner.spawn(_request())

    assert outcome.disposition.value == "started"
    recorded = spawner.session_outcomes[EVENT]
    assert recorded.state is ClaimLifecycle.PROPOSAL_CREATED
    assert recorded.proposal_id == "pid-1"
    assert calls[0]["symbol"] == "005930"
    assert calls[0]["proposer"] == "rob1290"
    assert calls[0]["rungs"] == [
        {
            "rung_index": 0,
            "side": "sell",
            "quantity": "10",
            "limit_price": "286000",
            "notional": None,
        }
    ]
    assert calls[0]["rationale"]["event_uuid"] == EVENT


@pytest.mark.asyncio
async def test_a_decline_becomes_the_rejected_terminal_with_its_reason() -> None:
    spawner = _spawner(Decline(event_uuid=EVENT, reason="rung already covered"))
    await spawner.spawn(_request())

    recorded = spawner.session_outcomes[EVENT]
    assert recorded.state is ClaimLifecycle.REJECTED_WITH_REASON
    assert recorded.rejection_reason == "rung already covered"
    assert recorded.proposal_id is None


@pytest.mark.asyncio
async def test_a_pre_commit_refusal_is_retryable_not_ambiguous() -> None:
    async def order_proposal_create(**kwargs):
        return {"success": False, "error": "unsupported account_mode"}

    spawner = _spawner(_draft(), tool=order_proposal_create)
    with pytest.raises(SpawnNotStarted) as exc:
        await spawner.spawn(_request())
    assert "proposal_create_refused" in str(exc.value)
    assert EVENT not in spawner.session_outcomes


@pytest.mark.asyncio
async def test_a_raising_create_is_ambiguous_and_is_never_retried() -> None:
    async def order_proposal_create(**kwargs):
        raise TimeoutError("acknowledgement lost")

    spawner = _spawner(_draft(), tool=order_proposal_create)
    with pytest.raises(ProposalChainAmbiguous):
        await spawner.spawn(_request())
    # And the spawner refuses to resolve it either way.
    assert spawner.reconcile(_request()).value == "ambiguous"


@pytest.mark.asyncio
async def test_success_without_a_proposal_id_is_ambiguous_not_success() -> None:
    async def order_proposal_create(**kwargs):
        return {"success": True, "proposal_id": ""}

    with pytest.raises(ProposalChainAmbiguous):
        await _spawner(_draft(), tool=order_proposal_create).spawn(_request())


@pytest.mark.asyncio
async def test_a_failed_create_is_classified_by_evidence_not_by_hope() -> None:
    """The two failure classes must not collapse into one another."""
    assert not issubclass(ProposalChainFailure, ProposalChainAmbiguous)
    assert not issubclass(ProposalChainAmbiguous, ProposalChainFailure)


@pytest.mark.asyncio
async def test_a_judge_that_returns_nothing_leaves_no_terminal() -> None:
    spawner = _spawner(None)
    with pytest.raises(SpawnNotStarted) as exc:
        await spawner.spawn(_request())
    assert "no_judgement" in str(exc.value)
    assert spawner.session_outcomes == {}


@pytest.mark.asyncio
async def test_a_judge_that_raises_is_treated_as_having_written_nothing() -> None:
    spawner = _spawner(RuntimeError("session died"))
    with pytest.raises(SpawnNotStarted):
        await spawner.spawn(_request())
    assert spawner.session_outcomes == {}


def test_a_spawner_without_a_judge_cannot_be_constructed() -> None:
    with pytest.raises(TypeError):
        ProposalChainSpawner(judge=object())


def test_the_default_boundary_is_the_real_tool_object() -> None:
    """Identity, not a name match.

    ROB-1286's boundary was a string that happened to spell the tool's
    name. This asserts the seam's default callable *is* the function the
    MCP surface exposes, so the chain cannot be satisfied by anything that
    merely looks like it.
    """
    from app.mcp_server.tooling import order_proposal_tools as opt
    from app.services.watch_trigger_repricing import proposal_chain

    assert proposal_chain._resolve_boundary_tool(None) is opt.order_proposal_create


def test_the_package_ships_no_judge_implementation() -> None:
    """The judgement is out-of-process work; a default judge would arm it."""
    import app.services.watch_trigger_repricing.judgement as judgement

    concrete = [
        name
        for name in dir(judgement)
        if name.endswith("Judge") and name != "RepricingJudge"
    ]
    assert concrete == []

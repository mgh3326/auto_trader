"""ROB-1290 — the execution boundary, actually crossed.

The gap this closes
-------------------
ROB-1286 shipped poll, gate, claim, fencing, lifecycle and the capability
allowlist as real code, and then stopped one step short: the spawn path
*named* ``order_proposal_create`` in a string, a docstring and an
allowlist, and never called it. The only test that reached a proposal row
created that row itself, before the tick, and handed the id to a scripted
spawner. So the operator's completion criterion -- every fire ends as a
proposal or an attributed reason -- could not be satisfied by the shipping
chain, only narrated about.

This module is the missing mile. It is the **write seam**: the one file in
the package allowed to import the boundary tool, exactly as
:mod:`.event_source` is the one file allowed to import the read side. The
package invariants enforce both halves, so widening the write surface is a
diff in a file that has a test watching it rather than an import somebody
adds in passing.

What it refuses
---------------
``a grant it cannot verify``
    :func:`create_proposal_for_fire` takes a
    :class:`~.live_contract.ProposalChainGrant`, which only
    ``LiveSessionSpawner.__init__`` can mint and only after checking the
    spawner's grant equals :data:`~.capability.PROPOSAL_ONLY_TOOLS`. A
    spawner that skipped the base class has nothing to pass.
``a substitute callable passed as an argument`` (r2 / BLOCKER 1)
    r1 took the boundary as an optional argument and validated it by
    ``__name__``, which is the "이름 매칭 ≠ 의미 강제" mistake in its purest
    form: any callable renamed ``order_proposal_create`` -- including one
    that submits to a broker -- passed the check and then ran. A name is a
    label the untrusted side chooses.

    So the argument is gone. This module calls the module-global it
    imported at the top of the file, and no parameter anywhere in the
    package accepts a callable.
``a different symbol``
    A session spawned for one fire may not propose on another symbol. The
    per-symbol concurrency rule is meaningless if the session can wander.

Three answers, not two
----------------------
The tool's own contract makes the distinction that matters for retry
safety. ``order_proposal_create`` freezes its success at the commit
boundary and runs every later step behind a never-raise boundary, so:

* ``success: False`` is returned only from the pre-commit validation
  handlers -- **no row exists**, and the fire is safe to re-judge. That is
  :class:`ProposalChainFailure`.
* an exception escaping the tool means the commit may or may not have
  happened -- **unknown**, and re-judging could put a second proposal in
  front of the approval lane. That is :class:`ProposalChainAmbiguous`, and
  the orchestrator quarantines it for an operator instead of guessing.

Reading "unknown" as "no" is the double-proposal direction, which is the
one failure this whole feature must not create.

What this seam does **not** buy (r3, measured)
----------------------------------------------
It would be comfortable to write that the boundary is now unreachable by
anything but the real tool. That is false, and the false version was in
this docstring until r3 measured it.

The seam is sound *given that it is used*: with a judgement in hand, only
``order_proposal_create`` is called, with arguments validated against the
fire. What the seam cannot do is compel anyone to go through it. The
judge :mod:`.chain_spawner` runs is ordinary Python in this interpreter,
and an in-process caller can:

* rebind ``proposal_chain.order_proposal_create`` -- and rebind any
  module-private capture of it just as easily, so "hide the name" is not
  a fix (measured: ``H1_PRIVATE_CAPTURE=DEFEATED``);
* import :data:`~.live_contract._GRANT_ISSUER` and mint a grant
  (measured: ``H2_GRANT_SENTINEL=MINTED``);
* skip this module altogether and import a broker order tool directly
  (measured: ``H3_SEAM_IS_OPTIONAL`` -- ``toss_place_order`` is two lines
  of ``importlib`` away from any judge).

The third is decisive and no amount of hardening here answers it. **The
approval boundary this feature needs is a process boundary, not a
function call.** See :mod:`.chain_spawner` for the guarantee this package
actually provides today, stated exactly.
"""

from __future__ import annotations

import inspect
import logging

from app.mcp_server.caller_identity import caller_agent_id_var
from app.mcp_server.tooling.order_proposal_tools import order_proposal_create
from app.services.watch_trigger_repricing.capability import (
    EXECUTION_BOUNDARY,
    CapabilityBoundaryViolation,
)
from app.services.watch_trigger_repricing.judgement import (
    Decline,
    JudgementRefused,
    ProposalDraft,
    interpret_judgement,
)
from app.services.watch_trigger_repricing.lifecycle import (
    SessionOutcome,
    proposal_created,
    rejected,
)
from app.services.watch_trigger_repricing.live_contract import (
    ProposalChainGrant,
    assert_exact_grant,
)
from app.services.watch_trigger_repricing.spawn import SpawnRequest

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_PROPOSER",
    "ProposalChainAmbiguous",
    "ProposalChainFailure",
    "create_proposal_for_fire",
    "interpret_create_response",
    "run_judgement_session",
]

# The ``proposer`` text carried on every proposal this lane creates, so an
# operator reading the approval queue can see which lane produced it.
DEFAULT_PROPOSER = "watch-trigger-repricing"

# Server-recorded caller identity prefix. Ownership checks elsewhere use
# this rather than the free-text ``proposer``.
_AGENT_PREFIX = "watch-trigger-repricing"


class ProposalChainFailure(RuntimeError):
    """The create provably did not happen. Safe to hand the fire back."""


class ProposalChainAmbiguous(RuntimeError):
    """Unknown whether a proposal row was committed. Never retry blindly."""


def interpret_create_response(response: object, *, event_uuid: str) -> SessionOutcome:
    """Classify what the boundary said, by evidence.

    Split out as a pure function so the three classifications can be
    tested without anything resembling a substitutable tool. r1 tested
    them by injecting fake callables, which is exactly the seam r2 found
    was a bypass -- the tests were paying for a hole in the design.
    """
    if not isinstance(response, dict):
        raise ProposalChainAmbiguous(
            f"{EXECUTION_BOUNDARY} returned {type(response).__name__}, not a "
            "result mapping; the commit state cannot be read from it"
        )
    if response.get("success") is not True:
        # Pre-commit refusal. The tool's success snapshot is frozen at the
        # commit boundary and every later step is never-raise, so this is
        # positive evidence that no row exists.
        raise ProposalChainFailure(
            f"{EXECUTION_BOUNDARY} refused event {event_uuid}: "
            f"{response.get('error')!r}"
        )
    proposal_id = str(response.get("proposal_id") or "").strip()
    if not proposal_id:
        raise ProposalChainAmbiguous(
            f"{EXECUTION_BOUNDARY} reported success without a proposal_id for "
            f"event {event_uuid}"
        )
    return proposal_created(proposal_id)


def _check_grant(grant: object, *, request: SpawnRequest) -> ProposalChainGrant:
    if not isinstance(grant, ProposalChainGrant):
        raise CapabilityBoundaryViolation(
            "reaching the execution boundary requires a ProposalChainGrant minted "
            "by LiveSessionSpawner.__init__; got "
            f"{type(grant).__name__} for event {request.event_uuid}"
        )
    # Re-checked here, not merely at mint time, so a grant object that was
    # somehow mutated between construction and use still fails closed.
    assert_exact_grant(grant.tools, who=f"proposal chain for {grant.who}")
    return grant


async def create_proposal_for_fire(
    *,
    request: SpawnRequest,
    draft: ProposalDraft,
    grant: object,
    proposer: str = DEFAULT_PROPOSER,
) -> SessionOutcome:
    """Cross the boundary for one fire and return the terminal it earned.

    Returns :data:`~.lifecycle.ClaimLifecycle.PROPOSAL_CREATED` carrying the
    real ``review.order_proposals`` id, or raises. It never returns a
    "created nothing" success -- that shape is what the completion criterion
    exists to catch.
    """
    _check_grant(grant, request=request)
    if draft.symbol != request.symbol:
        raise CapabilityBoundaryViolation(
            f"a session spawned for {request.symbol!r} may not propose on "
            f"{draft.symbol!r}"
        )

    identity = caller_agent_id_var.set(f"{_AGENT_PREFIX}:{request.event_uuid}")
    try:
        response = await order_proposal_create(
            symbol=draft.symbol,
            market=draft.market,
            account_mode=draft.account_mode,
            side=draft.side,
            order_type=draft.order_type,
            proposer=proposer,
            rungs=[rung.as_tool_argument() for rung in draft.rungs],
            thesis=draft.thesis,
            strategy=draft.strategy,
            valid_until=draft.valid_until,
            rationale={
                "source": "watch_trigger_repricing",
                "event_uuid": request.event_uuid,
                "spawn_key": request.spawn_key,
                "kst_date": request.kst_date,
                "session_label": request.label,
            },
        )
    except Exception as exc:  # noqa: BLE001 - unknown is not "no"
        logger.exception(
            "watch_trigger_repricing: %s raised for event %s (symbol=%s); whether "
            "a proposal row was committed is UNKNOWN",
            EXECUTION_BOUNDARY,
            request.event_uuid,
            request.symbol,
        )
        raise ProposalChainAmbiguous(
            f"{EXECUTION_BOUNDARY} raised for event {request.event_uuid}: {exc!r}"
        ) from exc
    finally:
        caller_agent_id_var.reset(identity)

    outcome = interpret_create_response(response, event_uuid=request.event_uuid)
    logger.info(
        "watch_trigger_repricing: event %s (symbol=%s) produced proposal %s",
        request.event_uuid,
        request.symbol,
        outcome.proposal_id,
    )
    return outcome


async def run_judgement_session(
    *,
    request: SpawnRequest,
    judge: object,
    grant: object,
    proposer: str = DEFAULT_PROPOSER,
) -> SessionOutcome:
    """Ask the judge, then act on its answer. Exactly one terminal, or raise.

    Judge-side failures are collapsed into :class:`~.judgement.JudgementRefused`
    on purpose. The judge holds no grant and this module is the sole writer,
    so "the judge blew up" is positive evidence that nothing was written --
    which lets the caller hand the fire straight back instead of parking it
    in a quarantine that needs an operator.
    """
    try:
        answer = judge.judge(request)  # type: ignore[attr-defined]
        if inspect.isawaitable(answer):
            answer = await answer
        judgement = interpret_judgement(answer, event_uuid=request.event_uuid)
    except JudgementRefused:
        raise
    except Exception as exc:  # noqa: BLE001 - nothing was written; say so plainly
        raise JudgementRefused(
            f"the judge raised before producing a judgement for event "
            f"{request.event_uuid}: {exc!r}"
        ) from exc

    if isinstance(judgement, ProposalDraft):
        return await create_proposal_for_fire(
            request=request,
            draft=judgement,
            grant=grant,
            proposer=proposer,
        )
    if isinstance(judgement, Decline):
        return rejected(judgement.reason)
    # Unreachable while ``Judgement`` stays closed. Kept so that adding a
    # third variant fails loudly here instead of silently returning None --
    # a None terminal is precisely the analysis-only outcome §101차 ⑤ bans.
    raise JudgementRefused(
        f"unhandled judgement variant {type(judgement).__name__} for event "
        f"{request.event_uuid}"
    )

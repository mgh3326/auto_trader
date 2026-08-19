"""ROB-1290 — the spawner that actually reaches the boundary.

What this is, precisely
-----------------------
:class:`ProposalChainSpawner` runs the re-judgement session **in this
process**: it asks an injected :class:`~.judgement.RepricingJudge` for a
decision and hands a draft straight to :mod:`.proposal_chain`, which calls
``order_proposal_create``. So the chain a tick executes is
poll -> claim -> spawn -> create -> terminal, with no scripted id and no
row planted ahead of time.

The part that is still outside the repo is the *judgement*, and it has to
be: deciding whether a fired watch level still deserves an order is LLM
work, and this runtime owns no in-process LLM provider. That seam was
always going to be injected. What was missing -- and what this closes --
is everything downstream of the decision.

Why it is a live spawner, not a dry one
---------------------------------------
Because it creates proposals. :mod:`.arming` therefore requires it to
satisfy the full live contract before a tick may run it: the base
constructor checks its grant is exactly
:data:`~.capability.PROPOSAL_ONLY_TOOLS`, it must be able to reconcile, and
the claim store must be durable. There is no configuration in which a
proposal-creating spawner runs against a store that forgets, which is what
would let one fire become two proposals across flow runs.

Attestation is a readback, not an echo
--------------------------------------
r2's escape was a spawner that passed a clean profile in the *request* and
provisioned something else. :meth:`attest_granted_tools` does not repeat
the request: it builds the shipping ``watch_repricing`` MCP profile through
:func:`~app.mcp_server.tooling.watch_repricing_registration.register_watch_repricing_tools`
and returns the names that registration actually produced. That set is
compared with closed equality **on every spawn**, before the judge is
asked, so widening the profile stops the flow rather than widening the
session.

What this class guarantees, exactly (r3)
----------------------------------------
Stated precisely, because two earlier rounds stated it too strongly and a
verifier had to measure the difference each time.

**It guarantees**: given a judgement, the only write performed is
``order_proposal_create``, with arguments bound to the fire that was
claimed (symbol checked, event id carried), and the terminal recorded
matches what the tool actually returned. No parameter of this class, or
of the write seam, accepts a callable -- the ``tool=`` argument r1 had is
gone, and passing one is a ``TypeError``. Arming additionally accepts
only the concrete types in :func:`~.arming.live_spawner_types`, by exact
type, so an external subclass cannot reintroduce that argument in its own
constructor and arm.

**It does not guarantee** that a judge stays inside those rails. The
judge runs in this interpreter with this process's full privileges. It
can rebind the seam's module global, mint a capability grant from the
module-private sentinel, or ignore this package entirely and import a
broker order tool -- all measured in r3, none of them closable from
inside the process. CPython offers no way to revoke import, attribute
assignment, or subclassing from code already running in it.

So the honest summary is: **accident prevention and static detectability,
not a security boundary.** Every route above is a deliberate act that
shows up in review as an import or an assignment somebody had to write.

The boundary this feature actually needs is the **process** boundary the
``watch_repricing`` MCP profile already describes: a session in a separate
process, reaching only the tools that profile registers, unable to import
anything else because it is not in this interpreter. This class runs the
judge in-process and therefore stands *inside* that boundary rather than
providing it -- which is why it is a rehearsal harness and why nothing in
this repo arms it (see ``RUNS_JUDGE_IN_PROCESS``).

Retry safety
------------
The three failure shapes get the three different answers their evidence
supports (see :mod:`.proposal_chain` for why the middle one exists):

``JudgementRefused``   nothing was written -> ``SpawnNotStarted``, retried next tick.
``ProposalChainFailure`` the tool refused pre-commit -> ``SpawnNotStarted``, retried.
``ProposalChainAmbiguous`` unknown -> propagated, so the tick quarantines it.

:meth:`reconcile` answers ``STARTED`` only for a fire this spawner holds a
recorded outcome for. Otherwise it stays ``AMBIGUOUS`` -- guessing
``NOT_STARTED`` after an unknown create is the double-proposal direction.
"""

from __future__ import annotations

import logging
from typing import Any, cast

from app.mcp_server.tooling.watch_repricing_registration import (
    register_watch_repricing_tools,
    watch_repricing_tool_names,
)
from app.services.watch_trigger_repricing.capability import PROPOSAL_ONLY_TOOLS
from app.services.watch_trigger_repricing.judgement import JudgementRefused
from app.services.watch_trigger_repricing.lifecycle import SessionOutcome
from app.services.watch_trigger_repricing.live_contract import (
    LiveSessionSpawner,
    assert_exact_grant,
)
from app.services.watch_trigger_repricing.proposal_chain import (
    DEFAULT_PROPOSER,
    ProposalChainFailure,
    run_judgement_session,
)
from app.services.watch_trigger_repricing.spawn import (
    SpawnDisposition,
    SpawnNotStarted,
    SpawnOutcome,
    SpawnRequest,
)

logger = logging.getLogger(__name__)

__all__ = ["ProposalChainSpawner", "provisioned_repricing_tools"]


class _ProbeRegistry:
    """Collects the names a registration pass actually registers."""

    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self, *args: Any, **kwargs: Any) -> Any:
        name = kwargs.get("name") or (args[0] if args else None)

        def decorator(func: Any) -> Any:
            self.tools[str(name)] = func
            return func

        return decorator


def provisioned_repricing_tools() -> frozenset[str]:
    """Names the shipping watch-repricing profile registration produces.

    A readback of the provisioning path rather than a restatement of the
    allowlist: if the two ever drift, this is the side that changes.
    """
    probe = _ProbeRegistry()
    register_watch_repricing_tools(cast("Any", probe))
    return watch_repricing_tool_names(probe)


class ProposalChainSpawner(LiveSessionSpawner):
    """Judge one fire, create the proposal it earns, record the terminal.

    Read the module docstring for the exact guarantee this carries, and
    for the one it does not.
    """

    # r3: declared, not inferred. True means the judgement runs in this
    # interpreter, so this spawner cannot bound what the judge does and is
    # a rehearsal harness rather than a production approval boundary. An
    # out-of-process spawner would set this False and would be the first
    # thing in this package that could honestly claim otherwise; the
    # arming tests pin the current value so that flip cannot be silent.
    RUNS_JUDGE_IN_PROCESS = True

    def __init__(
        self,
        *,
        judge: object,
        proposer: str = DEFAULT_PROPOSER,
    ) -> None:
        # r2 / BLOCKER 1: there is deliberately no ``tool`` parameter. r1
        # accepted one and checked its ``__name__``, so a callable renamed
        # ``order_proposal_create`` -- a broker submit, say -- constructed
        # cleanly, armed cleanly, and then ran. Passing one is now a
        # TypeError, which is what §101차 ③ means by enforcing the boundary
        # at the constructor rather than validating it at use.
        if not hasattr(judge, "judge"):
            raise TypeError(
                "ProposalChainSpawner requires a RepricingJudge; this package "
                "ships none, because the judgement is out-of-process work"
            )
        self._judge = judge
        self._proposer = proposer
        # event_uuid -> terminal, read by the entrypoint's fenced finalise.
        self.session_outcomes: dict[str, SessionOutcome] = {}
        self.requests: list[SpawnRequest] = []
        # Last, so the grant is minted over a fully built object.
        super().__init__()

    # -- live contract -------------------------------------------------
    def declared_grant(self) -> frozenset[str]:
        return PROPOSAL_ONLY_TOOLS

    def attest_granted_tools(self, request: SpawnRequest) -> frozenset[str]:
        return provisioned_repricing_tools()

    # -- the chain -----------------------------------------------------
    async def spawn(self, request: SpawnRequest) -> SpawnOutcome:
        assert_exact_grant(
            self.attest_granted_tools(request),
            who=f"{type(self).__name__}.attest_granted_tools()",
        )
        self.requests.append(request)
        try:
            outcome = await run_judgement_session(
                request=request,
                judge=self._judge,
                grant=self.proposal_chain_grant,
                proposer=self._proposer,
            )
        except JudgementRefused as exc:
            raise SpawnNotStarted(f"no_judgement: {exc}") from exc
        except ProposalChainFailure as exc:
            raise SpawnNotStarted(f"proposal_create_refused: {exc}") from exc

        self.session_outcomes[request.event_uuid] = outcome
        return SpawnOutcome(
            request=request,
            disposition=SpawnDisposition.STARTED,
            detail=f"judged:{outcome.state}",
        )

    def reconcile(self, request: SpawnRequest) -> SpawnDisposition:
        if request.event_uuid in self.session_outcomes:
            return SpawnDisposition.STARTED
        # The session ran but left no terminal, which is exactly the case
        # where "did a row get written?" is the open question. Saying
        # NOT_STARTED here would release the claim and re-judge the fire.
        return SpawnDisposition.AMBIGUOUS

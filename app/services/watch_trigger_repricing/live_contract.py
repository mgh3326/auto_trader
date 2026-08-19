"""ROB-1286 §101차 ③④ — what a live spawner must prove before it may run.

r2's finding, restated
----------------------
r1/r2 validated the *request*: a ``SpawnRequest`` could not carry a profile
containing an order-mutation tool. That is not the same as bounding the
session. r2 built a stand-in that passed a clean profile in the request and
then reported granting ``toss_place_order``, and the orchestrator accepted
it:

    B4_IGNORED_PROFILE status=ok spawned=1
    request_has_toss=False actual_has_toss=True

The gap was that ``SessionSpawner`` only required ``is_dry`` and ``spawn``.
Everything else -- reconcile, capability provisioning -- was a runbook
sentence, and a sentence does not stop a future implementer.

What this module changes
------------------------
Two enforcement points, both structural:

:class:`LiveSessionSpawner`
    An abstract base whose ``__init__`` *runs the checks*. A subclass whose
    attested grant is not exactly the proposal-only set cannot be
    constructed -- not "fails later", cannot exist. Since the arming gate
    requires an instance of this class, there is no way to attach a live
    spawner that skipped the check.
:func:`assert_live_spawner_contract`
    The gate's own re-check, so a duck-typed object that never called the
    base ``__init__`` is refused at the boundary too.

Exact equality, not containment
-------------------------------
:func:`assert_exact_grant` compares with ``==`` against
:data:`~.capability.PROPOSAL_ONLY_TOOLS`. Subset would let a spawner drop
``order_proposal_create`` and hand back a session that can only read --
which produces the analysis-only outcome §101차 forbids. Superset is the
r2 escape. Only closed equality rules out both.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Final, Protocol, final, runtime_checkable

from app.services.watch_trigger_repricing.capability import (
    PROPOSAL_ONLY_TOOLS,
    CapabilityBoundaryViolation,
)
from app.services.watch_trigger_repricing.spawn import (
    SpawnDisposition,
    SpawnOutcome,
    SpawnRequest,
)

__all__ = [
    "LiveSessionSpawner",
    "LiveSpawnerContractViolation",
    "ProposalChainGrant",
    "assert_exact_grant",
    "assert_live_spawner_contract",
]


class LiveSpawnerContractViolation(RuntimeError):
    """A live-capable spawner did not prove what it must prove."""


def assert_exact_grant(granted: frozenset[str] | set[str], *, who: str) -> None:
    """Refuse anything but the exact proposal-only tool set.

    The failure message names both directions because they are different
    bugs: an extra tool is a capability escape, a missing one is a session
    that cannot finish its job.
    """
    granted = frozenset(granted)
    if granted == PROPOSAL_ONLY_TOOLS:
        return
    extra = sorted(granted - PROPOSAL_ONLY_TOOLS)
    missing = sorted(PROPOSAL_ONLY_TOOLS - granted)
    raise CapabilityBoundaryViolation(
        f"{who} attested a tool grant that is not the proposal-only set; "
        f"extra={extra} missing={missing}"
    )


# Minted nowhere but :meth:`LiveSessionSpawner.__init__`. A module-private
# sentinel is what makes :class:`ProposalChainGrant` unforgeable *by
# accident*: writing ``ProposalChainGrant(object())`` raises, so a spawner
# that skipped the base class cannot hand one to the write seam. It is not
# a defence against deliberate private access, and does not claim to be --
# what it buys is that the bypass cannot be written without reaching into
# another module's underscore name, which is a reviewable act rather than
# an oversight.
_GRANT_ISSUER: Final[object] = object()


@final
class ProposalChainGrant:
    """Proof that a spawner's tool grant was checked at construction.

    §101차 ③ asked for the attestation to be enforced "in the protocol or
    the constructor" rather than in a runbook. This is the value that makes
    that enforcement reach further than the spawner object itself: the
    execution boundary (:mod:`.proposal_chain`) requires one, and the only
    way to obtain one is to construct a :class:`LiveSessionSpawner`
    subclass, whose ``__init__`` refuses any grant that is not exactly
    :data:`~.capability.PROPOSAL_ONLY_TOOLS`.

    So "wire a spawner that creates proposals while skipping the grant
    check" is not a runtime risk to be tested for -- it is code that cannot
    be written without failing at the constructor.
    """

    __slots__ = ("_tools", "_who")

    def __init__(
        self, issuer: object, *, who: str, tools: frozenset[str] | set[str]
    ) -> None:
        if issuer is not _GRANT_ISSUER:
            raise LiveSpawnerContractViolation(
                "ProposalChainGrant is minted only by LiveSessionSpawner.__init__; "
                "constructing one directly is exactly the bypass that check exists "
                "to prevent"
            )
        assert_exact_grant(tools, who=who)
        self._who = who
        self._tools = frozenset(tools)

    @property
    def who(self) -> str:
        return self._who

    @property
    def tools(self) -> frozenset[str]:
        return self._tools

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"ProposalChainGrant(who={self._who!r}, tools={len(self._tools)})"


@runtime_checkable
class ReconcilingLiveSpawner(Protocol):
    """The shape the arming gate insists on for anything not dry."""

    @property
    def is_dry(self) -> bool: ...

    def spawn(self, request: SpawnRequest) -> SpawnOutcome: ...

    def reconcile(self, request: SpawnRequest) -> SpawnDisposition: ...

    def attest_granted_tools(self, request: SpawnRequest) -> frozenset[str]: ...


class LiveSessionSpawner(ABC):
    """Base every live spawner must inherit.

    The constructor is the enforcement point (§101차 ③: "protocol/constructor
    에서 강제"). It calls the subclass's own attestation and refuses to
    finish building the object unless the grant is exactly right, so a
    mis-provisioned live spawner is not a runtime risk -- it is an object
    that cannot be instantiated.
    """

    def __init__(self) -> None:
        who = f"{type(self).__name__}.declared_grant()"
        declared = frozenset(self.declared_grant())
        assert_exact_grant(declared, who=who)
        self._proposal_chain_grant = ProposalChainGrant(
            _GRANT_ISSUER, who=who, tools=declared
        )

    @property
    def proposal_chain_grant(self) -> ProposalChainGrant:
        """The capability token :mod:`.proposal_chain` demands.

        Raises rather than returning ``None`` when a subclass skipped
        ``super().__init__()``: a spawner whose grant was never checked must
        not be able to reach the execution boundary by being falsy in the
        right place.
        """
        grant = getattr(self, "_proposal_chain_grant", None)
        if grant is None:
            raise LiveSpawnerContractViolation(
                f"{type(self).__name__} never ran LiveSessionSpawner.__init__, so "
                "its tool grant was never checked and it holds no proposal-chain "
                "grant"
            )
        return grant

    @property
    def is_dry(self) -> bool:
        """Live by definition. Subclasses must not soften this.

        r2 NEW BLOCKER 3: the arming gate trusted ``is_dry`` self-reporting,
        so a live-capable stand-in answering ``True`` slipped past the
        durable-store requirement. Anything inheriting from this class is
        live, full stop.
        """
        return False

    @abstractmethod
    def declared_grant(self) -> frozenset[str]:
        """The tool set this spawner will provision. Checked at construction."""

    @abstractmethod
    def attest_granted_tools(self, request: SpawnRequest) -> frozenset[str]:
        """The tool set the backend *actually* provisioned for this session.

        Read back from the session, not echoed from the request -- the
        request is the ask, this is the answer, and the orchestrator
        compares them.
        """

    @abstractmethod
    def spawn(self, request: SpawnRequest) -> SpawnOutcome: ...

    @abstractmethod
    def reconcile(self, request: SpawnRequest) -> SpawnDisposition: ...


def assert_live_spawner_contract(spawner: object) -> None:
    """Gate check for a non-dry spawner.

    Structural *and* nominal: the protocol check catches a duck-typed object
    that implements the methods, and the base-class check catches one that
    implements them without ever running the constructor's grant check.
    """
    if not isinstance(spawner, ReconcilingLiveSpawner):
        missing = [
            name
            for name in ("is_dry", "spawn", "reconcile", "attest_granted_tools")
            if not hasattr(spawner, name)
        ]
        raise LiveSpawnerContractViolation(
            f"{type(spawner).__name__} is not a reconciling live spawner "
            f"(missing: {missing}); a spawner that cannot reconcile turns an "
            "ambiguous start into a permanently unjudged fire"
        )
    if not isinstance(spawner, LiveSessionSpawner):
        raise LiveSpawnerContractViolation(
            f"{type(spawner).__name__} must inherit LiveSessionSpawner so its "
            "tool grant is checked at construction; implementing the methods "
            "without the base class skips that check"
        )

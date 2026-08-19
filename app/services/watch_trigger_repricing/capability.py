"""ROB-1286 B4 — the spawned session's capability profile.

Why a profile and not a sentence
--------------------------------
r1 named ``order_proposal_create`` as the execution boundary in a string
field on the spawn request. A string is a *statement of intent*: nothing
downstream reads it, and the MCP profile a live spawner would most
naturally reuse -- ``tradingcodex_execution`` -- registers the repo's
direct broker submit and cancel tools (the exact names are asserted
against the live registries in ``test_capability_profile.py``; this file
deliberately does not spell them, so the package-wide token invariant can
keep proving none of them appears here). A live spawner built on r1 would
therefore have handed the session a direct broker mutation surface while
the request still *said* "order_proposal_create". A declaration cannot
break that chain.

This module makes the boundary a capability instead.

Allowlist, never a deny-list
----------------------------
:data:`PROPOSAL_ONLY_TOOLS` enumerates every tool a re-judgement session
may hold. Anything absent is refused -- including tools that do not exist
yet. A deny-list would have the opposite failure mode: a broker tool added
next quarter would be permitted by default, and this package would not
even fail to compile. The allowlist also means this file never has to name
an order-mutation tool, so the ROB-1286 invariant test can keep asserting
that no such token appears anywhere in the package.

The enforcement point is :class:`SpawnRequest` construction (see
:mod:`.spawn`): a request carrying a profile with an unlisted tool raises
before any spawner sees it. There is no code path that builds a spawn
request without going through that check.

What the allowlist deliberately excludes
----------------------------------------
Beyond the obvious broker tools, three ``order_proposals`` tools are left
out even though they are not brokers:

``order_proposal_redispatch``
    Re-sends a proposal to the approval lane. A session that could
    redispatch could push an already-declined proposal back in front of
    the auto-approve machinery.
``order_proposal_void``
    Mutates another lane's proposal. Out of scope for a symbol re-judge.
``support_reserve_net_consume``
    Consumes reserve budget -- a sizing input, not an analysis read.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "EXECUTION_BOUNDARY",
    "PROPOSAL_ONLY_PROFILE",
    "PROPOSAL_ONLY_TOOLS",
    "CapabilityBoundaryViolation",
    "CapabilityProfile",
    "assert_proposal_only",
]

# The one write the session is allowed to perform. Named once, here, so
# widening it is a one-line diff in a file with its own test.
EXECUTION_BOUNDARY = "order_proposal_create"

# Every tool a spawned re-judgement session may hold. Reads that let it
# form a view, plus the single proposal-create write and the two proposal
# reads it needs to see its own output. Nothing else.
PROPOSAL_ONLY_TOOLS: frozenset[str] = frozenset(
    {
        # -- market / position reads -------------------------------------
        "get_quote",
        "get_indicators",
        "get_support_resistance",
        "get_market_index",
        "get_news",
        "get_holdings",
        "get_fx_rate",
        # -- policy / routing advisory -----------------------------------
        "get_trading_policy",
        "suggest_order_account",
        "route_request",
        # -- the fire it was spawned for ---------------------------------
        "list_active_watches",
        "investment_watch_events_list_recent",
        # -- the boundary, and reading back what it wrote ----------------
        EXECUTION_BOUNDARY,
        "order_proposal_get",
        "order_proposal_list",
    }
)


class CapabilityBoundaryViolation(RuntimeError):
    """A profile asked for a capability outside the proposal-only set."""


@dataclass(frozen=True)
class CapabilityProfile:
    """The exact tool set a spawned session is granted."""

    name: str
    tools: frozenset[str]


def assert_proposal_only(profile: CapabilityProfile) -> None:
    """Raise unless ``profile`` is inside the proposal-only boundary.

    Two conditions, and both matter:

    * no tool outside :data:`PROPOSAL_ONLY_TOOLS` -- the boundary is a
      ceiling, so an order-mutation tool is refused by absence rather than
      by being listed as forbidden;
    * :data:`EXECUTION_BOUNDARY` present -- a profile that cannot create a
      proposal would make the session silently useless, which is the
      ROB-1286 accident (a fire that produces nothing) in a new costume.
    """
    outside = profile.tools - PROPOSAL_ONLY_TOOLS
    if outside:
        raise CapabilityBoundaryViolation(
            f"capability profile {profile.name!r} requests tools outside the "
            f"proposal-only boundary: {sorted(outside)}"
        )
    if EXECUTION_BOUNDARY not in profile.tools:
        raise CapabilityBoundaryViolation(
            f"capability profile {profile.name!r} omits the execution "
            f"boundary {EXECUTION_BOUNDARY!r}; a session that cannot create a "
            "proposal would consume the fire and produce nothing"
        )


PROPOSAL_ONLY_PROFILE = CapabilityProfile(
    name="rob1286-proposal-only",
    tools=PROPOSAL_ONLY_TOOLS,
)

# Constructed at import time so an edit that widens the set past the
# boundary fails on import, not on the first spawn.
assert_proposal_only(PROPOSAL_ONLY_PROFILE)

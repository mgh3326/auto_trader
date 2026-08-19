"""ROB-1286 B4 + §101차 ③④ — a live spawner must prove its grant.

r2's escape, verbatim:

    B4_IGNORED_PROFILE status=ok spawned=1
    request_has_toss=False actual_has_toss=True

A stand-in passed a clean profile in the request and reported granting
``toss_place_order``. The orchestrator accepted it, because the spawner
protocol only required ``is_dry`` and ``spawn``.

The §101차 goal also moved: the session **may** create proposals now. What
must be impossible is reaching a broker without the approval machinery. So
these tests check the grant is exactly the proposal-only set -- which
contains ``order_proposal_create`` and no submit/approve tool -- and that
the check cannot be skipped.
"""

from __future__ import annotations

import pytest

from app.mcp_server.tooling.watch_repricing_registration import (
    WATCH_REPRICING_TOOL_NAMES,
    register_watch_repricing_tools,
    watch_repricing_tool_names,
)
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
from app.services.watch_trigger_repricing.claims import InMemoryClaimStore
from app.services.watch_trigger_repricing.db_claim_store import DatabaseClaimStore
from app.services.watch_trigger_repricing.live_contract import (
    LiveSessionSpawner,
    LiveSpawnerContractViolation,
    assert_exact_grant,
    assert_live_spawner_contract,
)
from app.services.watch_trigger_repricing.spawn import (
    DrySessionSpawner,
    SpawnDisposition,
    SpawnOutcome,
)

pytestmark = pytest.mark.unit


class _DurableStub:
    is_durable = True


def _good_live_spawner(grant=None):
    class Good(LiveSessionSpawner):
        def declared_grant(self):
            return grant if grant is not None else PROPOSAL_ONLY_TOOLS

        def attest_granted_tools(self, request):
            return self.declared_grant()

        def spawn(self, request):
            return SpawnOutcome(
                request=request, disposition=SpawnDisposition.STARTED, detail="ok"
            )

        def reconcile(self, request):
            return SpawnDisposition.STARTED

    return Good


# ---------------------------------------------------------------------------
# ④ The provisioned registry equals the allowlist -- closed equality
# ---------------------------------------------------------------------------
class _Registry:
    def __init__(self) -> None:
        self.tools: dict[str, object] = {}

    def tool(self, *args, **kwargs):
        name = kwargs.get("name") or (args[0] if args else None)

        def decorator(func):
            self.tools[str(name)] = func
            return func

        return decorator


def test_the_real_profile_provisions_exactly_the_allowlist() -> None:
    """Not a subset, not a superset -- the same set."""
    registry = _Registry()
    register_watch_repricing_tools(registry)

    provisioned = watch_repricing_tool_names(registry)
    assert provisioned == WATCH_REPRICING_TOOL_NAMES
    assert provisioned == PROPOSAL_ONLY_TOOLS


def test_the_profile_grants_proposal_create_and_no_submit_path() -> None:
    """§101차 B4 restated: proposing is allowed, bypassing approval is not."""
    registry = _Registry()
    register_watch_repricing_tools(registry)
    provisioned = watch_repricing_tool_names(registry)

    assert EXECUTION_BOUNDARY in provisioned
    for beyond in (
        "order_proposal_redispatch",
        "order_proposal_void",
        "place_order",
        "kis_live_place_order",
        "toss_place_order",
        "toss_modify_order",
        "toss_cancel_order",
        "upbit_place_order",
        "kis_mock_place_order",
        "kiwoom_mock_place_order",
    ):
        assert beyond not in provisioned


def test_the_profile_is_disjoint_from_every_order_mutation_registry() -> None:
    from app.mcp_server.tooling.orders_kis_variants import (
        KIS_LIVE_ORDER_TOOL_NAMES,
        KIS_MOCK_ORDER_TOOL_NAMES,
    )
    from app.mcp_server.tooling.orders_kiwoom_variants import KIWOOM_MOCK_TOOL_NAMES
    from app.mcp_server.tooling.orders_registration import ORDER_TOOL_NAMES
    from app.mcp_server.tooling.orders_toss_variants import TOSS_LIVE_ORDER_TOOL_NAMES

    mutations = (
        set(ORDER_TOOL_NAMES)
        | set(KIS_LIVE_ORDER_TOOL_NAMES)
        | set(KIS_MOCK_ORDER_TOOL_NAMES)
        | set(KIWOOM_MOCK_TOOL_NAMES)
        | set(TOSS_LIVE_ORDER_TOOL_NAMES)
    )
    # toss_get_positions is a read that lives in the Toss order registry.
    assert PROPOSAL_ONLY_TOOLS & mutations == set()


def test_registering_the_profile_through_the_real_registry_is_closed() -> None:
    from typing import cast

    from app.mcp_server.profiles import McpProfile
    from app.mcp_server.tooling.registry import register_all_tools

    registry = _Registry()
    register_all_tools(cast("object", registry), profile=McpProfile.WATCH_REPRICING)

    assert set(registry.tools) == PROPOSAL_ONLY_TOOLS


# ---------------------------------------------------------------------------
# ③ The grant check cannot be skipped
# ---------------------------------------------------------------------------
def test_exact_grant_rejects_a_superset() -> None:
    with pytest.raises(CapabilityBoundaryViolation) as exc:
        assert_exact_grant(PROPOSAL_ONLY_TOOLS | {"toss_place_order"}, who="x")
    assert "toss_place_order" in str(exc.value)


def test_exact_grant_rejects_a_subset() -> None:
    """A session that cannot propose produces the analysis-only outcome."""
    with pytest.raises(CapabilityBoundaryViolation) as exc:
        assert_exact_grant(PROPOSAL_ONLY_TOOLS - {EXECUTION_BOUNDARY}, who="x")
    assert EXECUTION_BOUNDARY in str(exc.value)


def test_a_live_spawner_with_a_wrong_grant_cannot_be_constructed() -> None:
    """§101차 ③: enforced in the constructor, not at first use."""
    Bad = _good_live_spawner(grant=PROPOSAL_ONLY_TOOLS | {"toss_place_order"})
    with pytest.raises(CapabilityBoundaryViolation):
        Bad()


def test_a_correct_live_spawner_constructs() -> None:
    assert _good_live_spawner()() is not None


# ---------------------------------------------------------------------------
# The r2 escape, replayed
# ---------------------------------------------------------------------------
def test_r2_ignored_profile_stand_in_is_now_refused() -> None:
    """The exact shape r2 got through: clean request, dirty actual grant."""

    class IgnoresProfile:
        is_dry = False

        def spawn(self, request):
            return SpawnOutcome(
                request=request, disposition=SpawnDisposition.STARTED, detail="ok"
            )

        def reconcile(self, request):
            return SpawnDisposition.STARTED

        def attest_granted_tools(self, request):
            return PROPOSAL_ONLY_TOOLS | {"toss_place_order"}

    with pytest.raises(LiveSpawnerContractViolation):
        assert_live_spawner_contract(IgnoresProfile())

    with pytest.raises(ArmingRefused) as exc:
        assert_arming_contract(spawner=IgnoresProfile(), store=_DurableStub())
    assert exc.value.reason == "live_spawner_contract_unmet"


def test_a_live_subclass_without_reconcile_cannot_be_instantiated() -> None:
    """§101차 ③: reconcile is abstract, so the class itself is unusable."""

    class NoReconcile(LiveSessionSpawner):
        def declared_grant(self):
            return PROPOSAL_ONLY_TOOLS

        def attest_granted_tools(self, request):
            return PROPOSAL_ONLY_TOOLS

        def spawn(self, request):
            return SpawnOutcome(
                request=request, disposition=SpawnDisposition.STARTED, detail="ok"
            )

    with pytest.raises(TypeError) as exc:
        NoReconcile()
    assert "reconcile" in str(exc.value)


def test_a_duck_typed_live_spawner_without_reconcile_is_refused() -> None:
    """And going around the base class does not help."""

    class DuckTyped:
        is_dry = False

        def spawn(self, request):
            return SpawnOutcome(
                request=request, disposition=SpawnDisposition.STARTED, detail="ok"
            )

        def attest_granted_tools(self, request):
            return PROPOSAL_ONLY_TOOLS

    with pytest.raises(ArmingRefused) as exc:
        assert_arming_contract(spawner=DuckTyped(), store=_DurableStub())
    assert exc.value.reason == "live_spawner_contract_unmet"


def test_implementing_every_method_without_the_base_class_is_still_refused() -> None:
    """The base class is where the construction-time grant check lives, so
    satisfying the protocol structurally is not enough."""

    class FullyDuckTyped:
        is_dry = False

        def spawn(self, request):
            return SpawnOutcome(
                request=request, disposition=SpawnDisposition.STARTED, detail="ok"
            )

        def reconcile(self, request):
            return SpawnDisposition.STARTED

        def attest_granted_tools(self, request):
            return PROPOSAL_ONLY_TOOLS

    with pytest.raises(LiveSpawnerContractViolation) as exc:
        assert_live_spawner_contract(FullyDuckTyped())
    assert "LiveSessionSpawner" in str(exc.value)


# ---------------------------------------------------------------------------
# r2 NEW BLOCKER 3 — dryness is a type, not a self-report
# ---------------------------------------------------------------------------
def test_self_attested_dryness_no_longer_bypasses_the_durability_rule() -> None:
    """r2's SELF_ATTESTED_DRY escape."""

    class LiveButClaimsDry:
        is_dry = True  # the lie

        def spawn(self, request):
            return SpawnOutcome(
                request=request, disposition=SpawnDisposition.STARTED, detail="ok"
            )

    assert is_dry_spawner(LiveButClaimsDry()) is False
    with pytest.raises(ArmingRefused):
        assert_arming_contract(spawner=LiveButClaimsDry(), store=InMemoryClaimStore())


def test_a_correct_live_spawner_still_needs_a_durable_store() -> None:
    spawner = _good_live_spawner()()

    with pytest.raises(ArmingRefused) as exc:
        assert_arming_contract(spawner=spawner, store=InMemoryClaimStore())
    assert exc.value.reason == "non_durable_claim_store"

    # And is accepted with one.
    assert_arming_contract(
        spawner=spawner, store=DatabaseClaimStore(session_factory=None)
    )


def test_the_dry_spawner_is_dry_by_type() -> None:
    assert is_dry_spawner(DrySessionSpawner()) is True
    assert_arming_contract(spawner=DrySessionSpawner(), store=InMemoryClaimStore())

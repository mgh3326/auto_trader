"""Production owner wiring for the Kiwoom coordination adapter.

This module only resolves a signed lane entry and constructs the already
implemented lane adapter.  It does not create a scheduler, open a broker
client, or authorize a mutation.  The default production factory returns a
grant-only owner canary; bounded send remains a later G3 change.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final
from weakref import WeakKeyDictionary

from app.services.mock_integration.coordination import DurableSendClaimAdapter
from app.services.mock_integration.lineage import MockLineageFactory
from app.services.mock_lane_registry import (
    LaneGuardError,
    LaneRegistryEntry,
    get_lane_registry_entry,
)
from scripts.b0x.kr.kiwoom_ordering import (
    InMemoryDispatchEvidence,
    InMemoryLineagePersistence,
    InMemoryReservationPort,
    InMemoryUncertaintyGate,
    KiwoomCoordinationAdapter,
    KiwoomCoordinationPorts,
    KiwoomSendNotAuthorized,
    require_j2a_physical_account_id,
)

KIWOOM_KR_LANE_ID: Final[str] = "kr.kiwoom.mock"
KIWOOM_US_LANE_ID: Final[str] = "us.kiwoom.mock"
KIWOOM_LANE_IDS: Final[Mapping[str, str]] = MappingProxyType(
    {"kr": KIWOOM_KR_LANE_ID, "us": KIWOOM_US_LANE_ID}
)
KIWOOM_COORDINATION_OWNER_TYPE_REJECTED: Final[str] = "coordination_owner_type_rejected"
KIWOOM_COORDINATION_OWNER_PORTS_REJECTED: Final[str] = (
    "coordination_owner_ports_rejected"
)
KIWOOM_COORDINATION_OWNER_LANE_MISMATCH: Final[str] = "coordination_owner_lane_mismatch"
KIWOOM_COORDINATION_OWNER_ENTRY_MISMATCH: Final[str] = (
    "coordination_owner_entry_mismatch"
)
KIWOOM_COORDINATION_OWNER_ACCOUNT_MISMATCH: Final[str] = (
    "coordination_owner_account_mismatch"
)
KIWOOM_COORDINATION_OWNER_CONTRACT_MISMATCH: Final[str] = (
    "coordination_owner_contract_mismatch"
)
KIWOOM_COORDINATION_OWNER_PROVENANCE_REJECTED: Final[str] = (
    "coordination_owner_provenance_rejected"
)


@dataclass(frozen=True, slots=True)
class _KiwoomCoordinationEntryProvenance:
    """Evidence linking an adapter port to the registry row it was pinned from."""

    canonical_entry: LaneRegistryEntry
    pinned_entry: LaneRegistryEntry


@dataclass(frozen=True, slots=True)
class _KiwoomOwnerConstructionProof:
    """Non-copyable-by-value proof of the approved adapter construction path."""

    ports: KiwoomCoordinationPorts
    provenance: _KiwoomCoordinationEntryProvenance
    constructed_type: type[KiwoomCoordinationAdapter]
    grant_only: bool


_OWNER_CONSTRUCTION_PROOFS: WeakKeyDictionary[
    KiwoomCoordinationAdapter, _KiwoomOwnerConstructionProof
] = WeakKeyDictionary()


class KiwoomCoordinationOwnerRejected(RuntimeError):
    """Explicit, report-safe rejection of a non-nominated owner instance."""

    def __init__(self, code: str, *, lane_id: str | None = None) -> None:
        self.code = code
        self.lane_id = lane_id
        suffix = "" if lane_id is None else f": lane={lane_id}"
        super().__init__(f"{code}{suffix}")


def _assert_kiwoom_lane_entry(
    entry: object, *, expected_lane_id: str | None = None
) -> LaneRegistryEntry:
    """Accept only an exact mock Kiwoom registry entry for KR or US."""

    if type(entry) is not LaneRegistryEntry:
        raise KiwoomCoordinationOwnerRejected(
            KIWOOM_COORDINATION_OWNER_ENTRY_MISMATCH,
            lane_id=expected_lane_id,
        )
    lane_id = entry.lane_id
    if lane_id not in KIWOOM_LANE_IDS.values():
        raise KiwoomCoordinationOwnerRejected(
            KIWOOM_COORDINATION_OWNER_LANE_MISMATCH, lane_id=lane_id
        )
    if expected_lane_id is not None and lane_id != expected_lane_id:
        raise KiwoomCoordinationOwnerRejected(
            KIWOOM_COORDINATION_OWNER_LANE_MISMATCH, lane_id=lane_id
        )
    expected_market = "kr" if lane_id == KIWOOM_KR_LANE_ID else "us"
    if (
        entry.market != expected_market
        or entry.broker != "kiwoom"
        or entry.account_profile != "mock"
        or entry.account_mode.value != "mock"
        or entry.endpoint_class.value != "mock"
    ):
        raise KiwoomCoordinationOwnerRejected(
            KIWOOM_COORDINATION_OWNER_ENTRY_MISMATCH, lane_id=lane_id
        )
    return entry


def resolve_kiwoom_lane_entry(lane_id: str = KIWOOM_KR_LANE_ID) -> LaneRegistryEntry:
    """Resolve the canonical lane row; unknown identity remains a hard stop."""

    entry = get_lane_registry_entry(lane_id)
    return _assert_kiwoom_lane_entry(entry, expected_lane_id=lane_id)


def _entry_provenance(entry: LaneRegistryEntry) -> _KiwoomCoordinationEntryProvenance:
    return _KiwoomCoordinationEntryProvenance(
        canonical_entry=get_lane_registry_entry(entry.lane_id),
        pinned_entry=entry,
    )


def _register_approved_adapter(
    ports: KiwoomCoordinationPorts, *, grant_only: bool
) -> KiwoomCoordinationAdapter:
    """Construct and register an adapter from a provenance-bearing port set."""

    provenance = getattr(ports, "coordination_provenance", None)
    if type(provenance) is not _KiwoomCoordinationEntryProvenance:
        raise KiwoomCoordinationOwnerRejected(
            KIWOOM_COORDINATION_OWNER_PROVENANCE_REJECTED,
            lane_id=getattr(getattr(ports, "entry", None), "lane_id", None),
        )
    if provenance.pinned_entry is not ports.entry:
        raise KiwoomCoordinationOwnerRejected(
            KIWOOM_COORDINATION_OWNER_PROVENANCE_REJECTED,
            lane_id=ports.entry.lane_id,
        )
    canonical_entry = get_lane_registry_entry(ports.entry.lane_id)
    if provenance.canonical_entry is not canonical_entry:
        raise KiwoomCoordinationOwnerRejected(
            KIWOOM_COORDINATION_OWNER_PROVENANCE_REJECTED,
            lane_id=ports.entry.lane_id,
        )

    adapter = KiwoomCoordinationAdapter(ports, grant_only=grant_only)
    _OWNER_CONSTRUCTION_PROOFS[adapter] = _KiwoomOwnerConstructionProof(
        ports=ports,
        provenance=provenance,
        constructed_type=type(adapter),
        grant_only=grant_only,
    )
    return adapter


def _assert_owner_provenance(
    owner: KiwoomCoordinationAdapter,
    ports: KiwoomCoordinationPorts,
    entry: LaneRegistryEntry,
) -> _KiwoomOwnerConstructionProof:
    """Require both registry provenance and the approved construction record."""

    supplied = getattr(ports, "coordination_provenance", None)
    canonical_entry = get_lane_registry_entry(entry.lane_id)
    proof = _OWNER_CONSTRUCTION_PROOFS.get(owner)
    if (
        type(supplied) is not _KiwoomCoordinationEntryProvenance
        or supplied.pinned_entry is not entry
        or supplied.canonical_entry is not canonical_entry
        or proof is None
        or proof.ports is not ports
        or proof.provenance is not supplied
        or proof.constructed_type is not KiwoomCoordinationAdapter
        or getattr(owner, "_class_assignment_tainted", False) is True
    ):
        raise KiwoomCoordinationOwnerRejected(
            KIWOOM_COORDINATION_OWNER_PROVENANCE_REJECTED,
            lane_id=entry.lane_id,
        )
    return proof


def assert_kiwoom_coordination_owner(
    owner: object,
    *,
    expected_lane_id: str = KIWOOM_KR_LANE_ID,
    expected_entry: LaneRegistryEntry | None = None,
    _allow_legacy_unpinned: bool = False,
) -> KiwoomCoordinationAdapter:
    """Validate the exact nominated adapter and its pinned account identity.

    ``type(...) is`` is deliberate.  Duck-typing, a recovery-owner string,
    and subclasses are not nominations.  If ``expected_entry`` is supplied,
    the adapter must carry that exact entry object, not an equal-looking copy.
    """

    if type(owner) is not KiwoomCoordinationAdapter:
        raise KiwoomCoordinationOwnerRejected(
            KIWOOM_COORDINATION_OWNER_TYPE_REJECTED, lane_id=expected_lane_id
        )
    ports = getattr(owner, "ports", None)
    if type(ports) is not KiwoomCoordinationPorts:
        raise KiwoomCoordinationOwnerRejected(
            KIWOOM_COORDINATION_OWNER_PORTS_REJECTED, lane_id=expected_lane_id
        )
    entry = _assert_kiwoom_lane_entry(
        getattr(ports, "entry", None), expected_lane_id=expected_lane_id
    )
    if expected_entry is not None:
        if entry.physical_account_id != expected_entry.physical_account_id:
            raise KiwoomCoordinationOwnerRejected(
                KIWOOM_COORDINATION_OWNER_ACCOUNT_MISMATCH,
                lane_id=entry.lane_id,
            )
        if entry is not expected_entry:
            raise KiwoomCoordinationOwnerRejected(
                KIWOOM_COORDINATION_OWNER_ENTRY_MISMATCH, lane_id=entry.lane_id
            )

    # The legacy escape is only for the pre-existing offline ordering fixture:
    # it has no provenance field and is send-capable.  Any G1/G2 canary, or
    # any adapter carrying the new provenance surface, stays on the strict
    # path even if a caller forgot the entry pin.
    strict_owner = (
        not _allow_legacy_unpinned
        or owner.grant_only is True
        or getattr(ports, "coordination_provenance", None) is not None
    )
    if strict_owner:
        proof = _assert_owner_provenance(owner, ports, entry)
        if (
            expected_entry is None
            and proof.provenance.pinned_entry is not proof.provenance.canonical_entry
        ):
            raise KiwoomCoordinationOwnerRejected(
                KIWOOM_COORDINATION_OWNER_PROVENANCE_REJECTED,
                lane_id=entry.lane_id,
            )
        if owner.grant_only is not True or proof.grant_only is not True:
            raise KiwoomCoordinationOwnerRejected(
                KIWOOM_COORDINATION_OWNER_CONTRACT_MISMATCH,
                lane_id=entry.lane_id,
            )
    try:
        expected_physical_account_id = require_j2a_physical_account_id(entry)
    except LaneGuardError as exc:
        raise KiwoomCoordinationOwnerRejected(exc.code, lane_id=entry.lane_id) from exc
    if (
        type(getattr(owner, "physical_account_id", None)) is not str
        or owner.physical_account_id != expected_physical_account_id
    ):
        raise KiwoomCoordinationOwnerRejected(
            KIWOOM_COORDINATION_OWNER_ACCOUNT_MISMATCH, lane_id=entry.lane_id
        )

    from scripts.b0x.kr import kiwoom_ordering as ordering_support

    contract = ordering_support.KIWOOM_LANE_RECOVERY_CONTRACT
    contract_values = (
        (owner.recovery_owner, contract["recovery_owner"]),
        (owner.restart_trigger, contract["restart_trigger"]),
        (owner.readback_operation, contract["readback_operation"]),
        (owner.release_if_matches_condition, contract["release_if_matches"]),
        (owner.blocked_state, contract["blocked_state"]),
    )
    if any(actual != expected for actual, expected in contract_values):
        raise KiwoomCoordinationOwnerRejected(
            KIWOOM_COORDINATION_OWNER_CONTRACT_MISMATCH,
            lane_id=entry.lane_id,
        )
    return owner


def build_kiwoom_coordination_factory(
    *,
    entry: LaneRegistryEntry,
    ports_factory: Callable[[LaneRegistryEntry], KiwoomCoordinationPorts],
) -> Callable[[], KiwoomCoordinationAdapter]:
    """Build a factory pinned to one exact lane entry and account identity."""

    pinned_entry = _assert_kiwoom_lane_entry(entry)
    # Validate identity before returning a callable.  A factory cannot be
    # considered production-wired while the physical account is unknown.
    try:
        require_j2a_physical_account_id(pinned_entry)
    except LaneGuardError:
        raise

    def _factory() -> KiwoomCoordinationAdapter:
        ports = ports_factory(pinned_entry)
        if type(ports) is not KiwoomCoordinationPorts:
            raise KiwoomCoordinationOwnerRejected(
                KIWOOM_COORDINATION_OWNER_PORTS_REJECTED,
                lane_id=pinned_entry.lane_id,
            )
        if ports.entry is not pinned_entry:
            raise KiwoomCoordinationOwnerRejected(
                KIWOOM_COORDINATION_OWNER_ENTRY_MISMATCH,
                lane_id=pinned_entry.lane_id,
            )
        adapter = _register_approved_adapter(ports, grant_only=True)
        return assert_kiwoom_coordination_owner(
            adapter,
            expected_lane_id=pinned_entry.lane_id,
            expected_entry=pinned_entry,
        )

    return _factory


async def _grant_only_connection() -> object:
    """Never expose a connection capable of reaching the send path."""

    raise KiwoomSendNotAuthorized("grant_only_send_not_authorized")


def make_grant_only_kiwoom_coordination_adapter(
    entry: LaneRegistryEntry,
) -> KiwoomCoordinationAdapter:
    """Create an exact owner instance for a no-send canary only.

    The in-memory ports are intentionally not a production lifecycle store.
    They make the owner identity observable without opening a socket, DB
    session, claim lease, or order mutation. G3 must supply durable ports.
    """

    pinned_entry = _assert_kiwoom_lane_entry(entry)
    require_j2a_physical_account_id(pinned_entry)
    ports = KiwoomCoordinationPorts(
        persistence=InMemoryLineagePersistence(),
        dispatch_evidence=InMemoryDispatchEvidence(),
        uncertainty_gate=InMemoryUncertaintyGate(),
        claims=DurableSendClaimAdapter(InMemoryReservationPort()),
        connection_factory=_grant_only_connection,
        registry=(pinned_entry,),
        lineage_factory=MockLineageFactory(),
        entry=pinned_entry,
        coordination_provenance=_entry_provenance(pinned_entry),
    )
    return _register_approved_adapter(ports, grant_only=True)


def production_kiwoom_coordination_factory(
    lane_id: str = KIWOOM_KR_LANE_ID,
) -> Callable[[], KiwoomCoordinationAdapter]:
    """Return the manual production entrypoint's owner-only factory.

    The canonical registry is consulted on every factory call.  With the
    current inert registry this raises ``physical_account_identity_unknown``;
    no local account summary is promoted into J2A identity.
    """

    def _factory() -> KiwoomCoordinationAdapter:
        entry = resolve_kiwoom_lane_entry(lane_id)
        return make_grant_only_kiwoom_coordination_adapter(entry)

    return _factory


__all__ = [
    "KIWOOM_COORDINATION_OWNER_ACCOUNT_MISMATCH",
    "KIWOOM_COORDINATION_OWNER_CONTRACT_MISMATCH",
    "KIWOOM_COORDINATION_OWNER_ENTRY_MISMATCH",
    "KIWOOM_COORDINATION_OWNER_LANE_MISMATCH",
    "KIWOOM_COORDINATION_OWNER_PORTS_REJECTED",
    "KIWOOM_COORDINATION_OWNER_PROVENANCE_REJECTED",
    "KIWOOM_COORDINATION_OWNER_TYPE_REJECTED",
    "KIWOOM_KR_LANE_ID",
    "KIWOOM_LANE_IDS",
    "KIWOOM_US_LANE_ID",
    "KiwoomCoordinationOwnerRejected",
    "assert_kiwoom_coordination_owner",
    "build_kiwoom_coordination_factory",
    "make_grant_only_kiwoom_coordination_adapter",
    "production_kiwoom_coordination_factory",
    "resolve_kiwoom_lane_entry",
]

"""Production PostgreSQL ports for the bounded KR Kiwoom owner.

The module-level :func:`build_ports` symbol is the reviewed CLI target.  Merely
importing or calling it opens no database connection, broker socket, or seal;
all I/O remains behind the coordination operation selected by the separately
sealed bounded-send factory.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.brokers.kiwoom.coordination_store import (
    KiwoomCoordinationStore,
    KiwoomDurableSendClaimAdapter,
    KiwoomOrderSendIntentPort,
)
from app.services.mock_integration.coordination import (
    SqlAlchemyLockAuthority,
    physical_account_scope_for_entry,
)
from app.services.mock_integration.lineage import MockLineageFactory
from app.services.mock_lane_registry import CANONICAL_LANE_REGISTRY, LaneRegistryEntry
from scripts.b0x.kr.kiwoom_coordination import _entry_provenance
from scripts.b0x.kr.kiwoom_ordering import (
    KiwoomCoordinationPorts,
    require_j2a_physical_account_id,
)


async def _open_lock_authority() -> SqlAlchemyLockAuthority:
    """Open one dedicated PostgreSQL session with an independent observer."""

    from app.core import db

    engine = db.engine
    connection = await engine.connect()
    return SqlAlchemyLockAuthority(
        connection,
        observer_factory=lambda: engine.connect(),
    )


def _new_session() -> AsyncSession:
    """Resolve the shared sessionmaker lazily, after module/factory loading."""

    from app.core.db import AsyncSessionLocal

    return AsyncSessionLocal()


def build_ports(entry: LaneRegistryEntry) -> KiwoomCoordinationPorts:
    """Return exact durable ports pinned to the bounded factory's registry row."""

    physical_account_id = require_j2a_physical_account_id(entry)
    scope = physical_account_scope_for_entry(entry)
    store = KiwoomCoordinationStore(
        session_factory=_new_session,
        lane_id=entry.lane_id,
        physical_account_id=physical_account_id,
        claim_account_scope=scope.claim_account_scope,
    )
    intents = KiwoomOrderSendIntentPort(_new_session)
    claims = KiwoomDurableSendClaimAdapter(intents, store=store)
    return KiwoomCoordinationPorts(
        persistence=store,
        dispatch_evidence=store,
        uncertainty_gate=store,
        claims=claims,
        connection_factory=_open_lock_authority,
        registry=CANONICAL_LANE_REGISTRY,
        lineage_factory=MockLineageFactory(),
        entry=entry,
        # Owner construction deliberately requires this exact private proof
        # type.  The bounded factory still consumes and validates the seal; this
        # only binds the returned ports to its canonical registry object.
        coordination_provenance=_entry_provenance(entry),
    )


__all__ = ["build_ports"]

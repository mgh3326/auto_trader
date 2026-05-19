"""Production collector registry for the snapshot-backed report generator.

This module assembles a :class:`SnapshotCollectorRegistry` populated with
the read-only collectors in this package. It is *separate* from
:func:`app.services.investment_snapshots.collectors.default_collector_registry`,
which intentionally remains empty (Phase 2 invariant) so existing callers
that rely on the bundle service for unrelated purposes are unaffected.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.action_report.snapshot_backed.collectors.journal import (
    JournalSnapshotCollector,
)
from app.services.action_report.snapshot_backed.collectors.market import (
    MarketEventsSnapshotCollector,
)
from app.services.action_report.snapshot_backed.collectors.news import (
    NewsSnapshotCollector,
)
from app.services.action_report.snapshot_backed.collectors.optional_stubs import (
    BrowserProbeStubCollector,
    CandidateUniverseStubCollector,
    InvestPageStubCollector,
    NaverRemoteDebugStubCollector,
    SymbolStubCollector,
    TossRemoteDebugStubCollector,
)
from app.services.action_report.snapshot_backed.collectors.portfolio import (
    PortfolioSnapshotCollector,
)
from app.services.action_report.snapshot_backed.collectors.watch_context import (
    WatchContextSnapshotCollector,
)
from app.services.investment_snapshots.collectors import SnapshotCollectorRegistry


def production_collector_registry(session: AsyncSession) -> SnapshotCollectorRegistry:
    """Return a populated registry for the snapshot-backed generator.

    Required-kind collectors are wired to read-only DB-backed services.
    Optional-kind collectors are either thin DB readers (news) or
    fail-open stubs. Adding a new collector here is the single place
    needed to expose it to the generator.
    """
    registry = SnapshotCollectorRegistry()

    # Required kinds — DB-backed, read-only.
    registry.register(PortfolioSnapshotCollector(session))
    registry.register(JournalSnapshotCollector(session))
    registry.register(WatchContextSnapshotCollector(session))
    registry.register(MarketEventsSnapshotCollector(session))

    # Optional kinds — DB-backed where possible, stubs otherwise.
    registry.register(NewsSnapshotCollector(session))
    registry.register(SymbolStubCollector())
    registry.register(CandidateUniverseStubCollector())
    registry.register(InvestPageStubCollector())
    registry.register(NaverRemoteDebugStubCollector())
    registry.register(TossRemoteDebugStubCollector())
    registry.register(BrowserProbeStubCollector())

    return registry

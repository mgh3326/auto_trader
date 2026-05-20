"""Production collector registry for the snapshot-backed report generator.

This module assembles a :class:`SnapshotCollectorRegistry` populated with
the read-only collectors in this package. It is *separate* from
:func:`app.services.investment_snapshots.collectors.default_collector_registry`,
which intentionally remains empty (Phase 2 invariant) so existing callers
that rely on the bundle service for unrelated purposes are unaffected.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.action_report.snapshot_backed.collectors.candidate_universe import (
    CandidateUniverseSnapshotCollector,
)
from app.services.action_report.snapshot_backed.collectors.invest_page import (
    InvestPageSnapshotCollector,
)
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
    NaverRemoteDebugStubCollector,
    TossRemoteDebugStubCollector,
)
from app.services.action_report.snapshot_backed.collectors.pending_orders import (
    PendingOrdersSnapshotCollector,
)
from app.services.action_report.snapshot_backed.collectors.portfolio import (
    PortfolioSnapshotCollector,
)
from app.services.action_report.snapshot_backed.collectors.symbol import (
    SymbolSnapshotCollector,
)
from app.services.action_report.snapshot_backed.collectors.watch_context import (
    WatchContextSnapshotCollector,
)
from app.services.brokers.kis.client import KISClient
from app.services.brokers.upbit.orders import (
    fetch_open_orders as _upbit_fetch_open_orders,
)
from app.services.investment_snapshots.collectors import SnapshotCollectorRegistry


class _UpbitOpenOrdersAdapter:
    """Read-only adapter exposing only ``fetch_open_orders``.

    The Upbit broker module also exports order placement/cancellation
    functions. Wrapping just the read function here keeps the registry
    wiring intentionally narrow — the collector cannot reach mutation
    paths via the bound client.
    """

    @staticmethod
    async def fetch_open_orders(market: str | None = None) -> list[dict[str, Any]]:
        return await _upbit_fetch_open_orders(market=market)


def _build_kis_client_safely() -> KISClient | None:
    """Construct the KIS client used by the pending-orders collector.

    ``KISClient()`` reads credentials lazily and does not perform network
    I/O at construction time, but if settings are misconfigured the
    constructor could still raise. Returning ``None`` on failure keeps
    the registry usable; the collector falls back to ``unavailable``.
    """
    try:
        return KISClient()
    except Exception:  # noqa: BLE001 — registry must not raise on wiring
        return None


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

    # Optional kinds — DB-backed where possible.
    registry.register(NewsSnapshotCollector(session))
    registry.register(SymbolSnapshotCollector(session))
    registry.register(CandidateUniverseSnapshotCollector(session))
    registry.register(InvestPageSnapshotCollector(session))
    # Remote-debug probes remain fail-open stubs — they are operator-driven
    # only, and automated wiring is intentionally out of scope.
    registry.register(NaverRemoteDebugStubCollector())
    registry.register(TossRemoteDebugStubCollector())
    registry.register(BrowserProbeStubCollector())

    # ROB-274 — optional/fail-open. Wires the KIS client + a narrow Upbit
    # read-only adapter (``fetch_open_orders`` only). Construction is
    # wrapped to keep the registry usable when broker credentials are
    # absent or misconfigured; the collector then emits ``unavailable``.
    registry.register(
        PendingOrdersSnapshotCollector(
            kis_client=_build_kis_client_safely(),
            upbit_client=_UpbitOpenOrdersAdapter(),
        )
    )

    return registry

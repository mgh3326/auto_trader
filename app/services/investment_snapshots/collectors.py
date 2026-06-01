"""ROB-269 Phase 2 — Snapshot collector protocol + registry.

The registry is **empty by default in Phase 2**. Production collectors
(KIS / journal / market / news) register here in Phase 3. Tests register
fakes. This keeps Phase 2 free of any live HTTP surface — the registry
is the single seam where external data enters the snapshot pipeline.

A collector is an object that knows how to fetch fresh data for **one**
``snapshot_kind`` and return one or more ``SnapshotCollectResult`` rows
(typically multiple when the kind is ``symbol`` — one result per requested
symbol). The ensure service persists each result as one snapshot artifact.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.investment_snapshots import (
    FreshnessStatus,
    SnapshotAccountScope,
    SnapshotKind,
    SnapshotMarket,
    SourceKind,
)


class SnapshotCollectResult(BaseModel):
    """One snapshot artifact's worth of data, as returned by a collector.

    Caller (the ensure service) augments this with ``run_uuid``, hashes,
    and freshness/idempotency before insert.
    """

    model_config = ConfigDict(extra="forbid")

    snapshot_kind: SnapshotKind
    market: SnapshotMarket
    account_scope: SnapshotAccountScope | None = None
    symbol: str | None = None

    source_table: str | None = None
    source_id: int | None = None
    source_uri: str | None = None
    source_kind: SourceKind

    payload_json: dict[str, Any] = Field(default_factory=dict)
    source_timestamps_json: dict[str, Any] = Field(default_factory=dict)
    coverage_json: dict[str, Any] = Field(default_factory=dict)
    errors_json: dict[str, Any] = Field(default_factory=dict)

    as_of: dt.datetime
    valid_until: dt.datetime | None = None
    freshness_status: FreshnessStatus = "fresh"

    @model_validator(mode="after")
    def _source_ref_triple_consistent(self) -> SnapshotCollectResult:
        nulls = sum(
            1 for v in (self.source_table, self.source_id, self.source_uri) if v is None
        )
        if nulls not in (0, 3):
            raise ValueError(
                "SnapshotCollectResult.source_table/source_id/source_uri must all be "
                "set or all None"
            )
        return self

    @model_validator(mode="after")
    def _domain_ref_requires_triple(self) -> SnapshotCollectResult:
        if self.source_kind == "domain_ref" and self.source_table is None:
            raise ValueError("source_kind='domain_ref' requires the source_ref triple")
        return self


class CollectorRequest(BaseModel):
    """Per-collect call context. Passed by the ensure service."""

    model_config = ConfigDict(extra="forbid")

    market: SnapshotMarket
    account_scope: SnapshotAccountScope | None = None
    symbols: list[str] | None = None
    """Caller-supplied symbol filter. ``None`` = collector's own discretion."""
    candidate_limit: int | None = None
    """For ``candidate_universe`` collectors. ``None`` = collector default."""
    policy_snapshot: dict[str, Any]
    """The frozen policy as stored on the run row (read-only)."""
    user_id: int | None = None
    """ROB-278 — explicit operator user_id for collectors that need to call
    live-account read endpoints (e.g. KIS holdings/cash). ``None`` means the
    caller did not supply one; broker-backed collectors must then fail
    closed (``unavailable``) rather than invent a default."""
    market_session: str | None = None
    """ROB-390 — venue/session context ("regular"/"nxt"/...). ``None`` = unset.
    Collectors that switch venue by trading session (e.g. NXT orderbook) read
    this; left ``None`` for callers that do not distinguish sessions."""


@runtime_checkable
class SnapshotCollectorProtocol(Protocol):
    """Contract every snapshot collector implements.

    Phase 2 ships **no** concrete production collector. Phase 3 will add
    KIS / journal / market / news collectors that implement this protocol.
    """

    @property
    def snapshot_kind(self) -> str:  # actually ``SnapshotKind`` enum string
        """The ``snapshot_kind`` this collector produces. One collector per kind."""
        ...

    async def collect(self, request: CollectorRequest) -> list[SnapshotCollectResult]:
        """Fetch and return zero or more snapshot artifacts.

        Returning an empty list is legal — the ensure service interprets that as
        ``unavailable`` for the kind. Raising is also legal; the ensure service
        catches and records it as ``errors_json`` on a synthetic ``unavailable``
        snapshot. Collector MUST not perform any broker/order/watch mutation.
        """
        ...


class SnapshotCollectorRegistry:
    """Per-process registry mapping ``snapshot_kind`` → collector instance.

    The default registry is created empty by ``default_collector_registry``.
    Tests construct their own registry to inject fakes; production wiring
    (Phase 3) will register collectors on app startup.
    """

    def __init__(self) -> None:
        self._collectors: dict[str, SnapshotCollectorProtocol] = {}

    def register(self, collector: SnapshotCollectorProtocol) -> None:
        kind = collector.snapshot_kind
        if kind in self._collectors:
            raise ValueError(f"collector for snapshot_kind={kind!r} already registered")
        self._collectors[kind] = collector

    def get(self, snapshot_kind: str) -> SnapshotCollectorProtocol | None:
        return self._collectors.get(snapshot_kind)

    def list_kinds(self) -> set[str]:
        return set(self._collectors)

    def __len__(self) -> int:
        return len(self._collectors)


def default_collector_registry() -> SnapshotCollectorRegistry:
    """Return an empty registry — Phase 2 ships no production collectors."""
    return SnapshotCollectorRegistry()

"""ROB-269 Phase 2 — MCP/API caller-facing request/response DTOs.

These DTOs are separate from ``app.schemas.investment_snapshots`` (which
holds DB-shape DTOs incl. canonical_hash / idempotency_key composition).
Keeping the surfaces split prevents internal trace fields from leaking
through MCP/HTTP responses and keeps mutation-prone fields off the public
contract.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.investment_reports import MarketSessionLiteral
from app.schemas.investment_snapshots import (
    BundleItemRole,
    BundleStatus,
    FreshnessStatus,
    SnapshotAccountScope,
    SnapshotKind,
    SnapshotMarket,
    SourceKind,
)
from app.services.investment_snapshots.collectors import SnapshotCollectResult

EnsureMode = Literal["ensure_fresh", "reuse_only", "create_new"]


# ---------------------------------------------------------------------------
# investment_snapshot_bundle_ensure
# ---------------------------------------------------------------------------
class EnsureBundleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    purpose: str = Field(min_length=1, max_length=64)
    market: SnapshotMarket
    account_scope: SnapshotAccountScope | None = None
    policy_version: str = Field(min_length=1)
    mode: EnsureMode = "ensure_fresh"
    symbols: list[str] | None = None
    market_session: MarketSessionLiteral | None = None
    candidate_limit: Annotated[int | None, Field(ge=1, le=100)] = None
    manual_snapshots: dict[SnapshotKind, list[SnapshotCollectResult]] | None = None
    """Caller-supplied pre-collected snapshots keyed by ``snapshot_kind``.

    In Phase 2 this is the **primary** way to populate a bundle because the
    collector registry is empty in production. Phase 3 will populate the
    registry and this field will become optional fallback.
    """
    requested_by: Literal["hermes", "user", "scheduler", "claude_code", "reviewer"] = (
        "user"
    )
    user_id: int | None = None
    """ROB-278 — explicit operator user_id forwarded to collectors that read
    live-account state. ``None`` keeps broker-backed collectors in fail-closed
    (``unavailable``) mode rather than inventing a default user."""


class EnsureBundleResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bundle_uuid: uuid.UUID | None = None
    """``None`` only when ``mode='reuse_only'`` and no fresh bundle existed —
    in every other outcome (incl. ensure_fresh failures) we still create a
    bundle row for audit and return its UUID here."""

    status: BundleStatus | Literal["reused"]
    """``reused`` means a fresh bundle already existed — no new run was made.
    Other values mirror ``investment_snapshot_bundles.status``."""

    created: bool
    """``True`` if a new bundle row was inserted; ``False`` if reused."""

    coverage_summary: dict[str, Any] = Field(default_factory=dict)
    freshness_summary: dict[str, Any] = Field(default_factory=dict)
    missing_sources: list[str] = Field(default_factory=list)
    """List of ``snapshot_kind`` (or ``snapshot_kind:symbol``) that were
    expected by policy but came back unavailable. Required missing sources
    drive ``status='failed'`` / ``'stale_fallback'``; optional ones drive
    ``'partial'``."""

    warnings: list[str] = Field(default_factory=list)
    run_uuid: uuid.UUID | None = None
    """``None`` when ``status='reused'``; populated for any newly created bundle."""


# ---------------------------------------------------------------------------
# investment_snapshot_bundle_get
# ---------------------------------------------------------------------------
class BundleHeaderView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bundle_uuid: uuid.UUID
    purpose: str
    market: SnapshotMarket
    account_scope: SnapshotAccountScope | None
    policy_version: str
    as_of: dt.datetime
    status: BundleStatus
    coverage_summary: dict[str, Any]
    freshness_summary: dict[str, Any]
    created_at: dt.datetime


class BundleItemView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_uuid: uuid.UUID
    role: BundleItemRole
    snapshot_kind: SnapshotKind
    market: SnapshotMarket
    symbol: str | None
    account_scope: SnapshotAccountScope | None
    freshness_status: FreshnessStatus
    source_kind: SourceKind
    source_table: str | None
    source_id: int | None
    source_uri: str | None
    as_of: dt.datetime


class GetBundleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bundle_uuid: uuid.UUID
    include_payload_preview: bool = False
    """If True, each item carries up to 2KB of its ``payload_json`` as
    ``payload_preview``. Off by default to keep responses small and
    prevent accidental payload exfiltration through list/get surfaces."""


class GetBundleResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bundle: BundleHeaderView
    items: list[BundleItemView]
    payload_previews: dict[uuid.UUID, str] | None = None
    """Keyed by ``snapshot_uuid``; only present when ``include_payload_preview=True``."""


# ---------------------------------------------------------------------------
# investment_snapshot_list — snapshots
# ---------------------------------------------------------------------------
class ListSnapshotsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    market: SnapshotMarket | None = None
    symbol: str | None = None
    snapshot_kind: SnapshotKind | None = None
    source_kind: SourceKind | None = None
    freshness_status: FreshnessStatus | None = None
    since: dt.datetime | None = None
    limit: Annotated[int, Field(ge=1, le=100)] = 20


class SnapshotMetadataView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_uuid: uuid.UUID
    snapshot_kind: SnapshotKind
    market: SnapshotMarket
    symbol: str | None
    account_scope: SnapshotAccountScope | None
    as_of: dt.datetime
    freshness_status: FreshnessStatus
    source_kind: SourceKind
    source_table: str | None
    source_id: int | None
    source_uri: str | None


class ListSnapshotsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshots: list[SnapshotMetadataView]
    limit: int


# ---------------------------------------------------------------------------
# Bundles list (router only — MCP uses bundle_get for individual lookup)
# ---------------------------------------------------------------------------
class ListBundlesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    purpose: str | None = None
    market: SnapshotMarket | None = None
    account_scope: SnapshotAccountScope | None = None
    status: BundleStatus | None = None
    limit: Annotated[int, Field(ge=1, le=100)] = 20


class ListBundlesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bundles: list[BundleHeaderView]
    limit: int


# ---------------------------------------------------------------------------
# investment_snapshot_refresh_request
# ---------------------------------------------------------------------------
class RefreshRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=512)
    purpose: Literal["manual_refresh", "reviewer_requested"] = "manual_refresh"
    market: SnapshotMarket
    account_scope: SnapshotAccountScope | None = None
    symbols: list[str] | None = None
    snapshot_kinds: list[SnapshotKind] | None = None
    policy_version: str = "intraday_action_report_v1"
    requested_by: Literal["hermes", "user", "scheduler", "claude_code", "reviewer"] = (
        "user"
    )


class RefreshResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_uuid: uuid.UUID
    status: Literal["running"]
    """Always ``'running'`` in Phase 2 — the row is inserted but no
    collection happens. Phase 3 schedulers will pick the run up and
    transition it to ``completed`` / ``partial`` / ``failed``."""

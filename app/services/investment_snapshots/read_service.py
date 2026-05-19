"""ROB-269 Phase 2 — Read service for snapshot bundles and snapshots.

Pure read path — wraps ``InvestmentSnapshotsRepository`` and transforms ORM
rows into MCP/API DTOs. Never writes. Never calls collectors.

Used by:
* MCP tools ``investment_snapshot_bundle_get`` and ``investment_snapshot_list``.
* HTTP GET endpoints under ``/trading/api/investment-snapshots/``.
"""

from __future__ import annotations

import json
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.investment_snapshots_mcp import (
    BundleHeaderView,
    BundleItemView,
    GetBundleResponse,
    ListBundlesRequest,
    ListBundlesResponse,
    ListSnapshotsRequest,
    ListSnapshotsResponse,
    SnapshotMetadataView,
)
from app.services.investment_snapshots.repository import (
    InvestmentSnapshotsRepository,
)

_PAYLOAD_PREVIEW_BYTES = 2048


class SnapshotBundleNotFoundError(LookupError):
    """Raised when a bundle UUID does not resolve. Caller maps to HTTP 404."""


class SnapshotBundleReadService:
    def __init__(
        self,
        session: AsyncSession,
        repository: InvestmentSnapshotsRepository | None = None,
    ) -> None:
        self._session = session
        self._repo = repository or InvestmentSnapshotsRepository(session)

    # ------------------------------------------------------------------
    # Single bundle (MCP get + HTTP /bundles/{uuid})
    # ------------------------------------------------------------------
    async def get_bundle(
        self,
        *,
        bundle_uuid: uuid.UUID,
        include_payload_preview: bool = False,
    ) -> GetBundleResponse:
        bundle = await self._repo.get_bundle_by_uuid(bundle_uuid)
        if bundle is None:
            raise SnapshotBundleNotFoundError(str(bundle_uuid))

        pairs = await self._repo.list_bundle_items_with_snapshots(bundle.id)
        items = [
            BundleItemView(
                snapshot_uuid=snap.snapshot_uuid,
                role=item.role,  # type: ignore[arg-type]
                snapshot_kind=snap.snapshot_kind,  # type: ignore[arg-type]
                market=snap.market,  # type: ignore[arg-type]
                symbol=snap.symbol,
                account_scope=snap.account_scope,  # type: ignore[arg-type]
                freshness_status=snap.freshness_status,  # type: ignore[arg-type]
                source_kind=snap.source_kind,  # type: ignore[arg-type]
                source_table=snap.source_table,
                source_id=snap.source_id,
                source_uri=snap.source_uri,
                as_of=snap.as_of,
            )
            for item, snap in pairs
        ]

        payload_previews: dict[uuid.UUID, str] | None = None
        if include_payload_preview:
            payload_previews = {
                snap.snapshot_uuid: _serialise_preview(snap.payload_json)
                for _item, snap in pairs
            }

        return GetBundleResponse(
            bundle=_bundle_to_view(bundle),
            items=items,
            payload_previews=payload_previews,
        )

    # ------------------------------------------------------------------
    # Listing bundles (HTTP /bundles)
    # ------------------------------------------------------------------
    async def list_bundles(self, request: ListBundlesRequest) -> ListBundlesResponse:
        rows = await self._repo.list_bundles(
            purpose=request.purpose,
            market=request.market,
            account_scope=request.account_scope,
            status=request.status,
            limit=request.limit,
        )
        return ListBundlesResponse(
            bundles=[_bundle_to_view(b) for b in rows],
            limit=request.limit,
        )

    # ------------------------------------------------------------------
    # Listing snapshots (MCP list + HTTP /snapshots)
    # ------------------------------------------------------------------
    async def list_snapshots(
        self, request: ListSnapshotsRequest
    ) -> ListSnapshotsResponse:
        rows = await self._repo.list_snapshots(
            market=request.market,
            symbol=request.symbol,
            snapshot_kind=request.snapshot_kind,
            source_kind=request.source_kind,
            freshness_status=request.freshness_status,
            since=request.since,
            limit=request.limit,
        )
        return ListSnapshotsResponse(
            snapshots=[
                SnapshotMetadataView(
                    snapshot_uuid=r.snapshot_uuid,
                    snapshot_kind=r.snapshot_kind,  # type: ignore[arg-type]
                    market=r.market,  # type: ignore[arg-type]
                    symbol=r.symbol,
                    account_scope=r.account_scope,  # type: ignore[arg-type]
                    as_of=r.as_of,
                    freshness_status=r.freshness_status,  # type: ignore[arg-type]
                    source_kind=r.source_kind,  # type: ignore[arg-type]
                    source_table=r.source_table,
                    source_id=r.source_id,
                    source_uri=r.source_uri,
                )
                for r in rows
            ],
            limit=request.limit,
        )


def _bundle_to_view(bundle) -> BundleHeaderView:  # noqa: ANN001 — ORM row
    return BundleHeaderView(
        bundle_uuid=bundle.bundle_uuid,
        purpose=bundle.purpose,
        market=bundle.market,
        account_scope=bundle.account_scope,
        policy_version=bundle.policy_version,
        as_of=bundle.as_of,
        status=bundle.status,
        coverage_summary=bundle.coverage_summary,
        freshness_summary=bundle.freshness_summary,
        created_at=bundle.created_at,
    )


def _serialise_preview(payload_json: dict[str, object]) -> str:
    """Return at most ``_PAYLOAD_PREVIEW_BYTES`` bytes of compact JSON.

    Truncation is byte-level and may produce invalid JSON intentionally —
    the preview is meant for human eyeballing in MCP responses, not for
    machine parsing.
    """
    encoded = json.dumps(payload_json, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) <= _PAYLOAD_PREVIEW_BYTES:
        return encoded
    truncated = encoded.encode("utf-8")[:_PAYLOAD_PREVIEW_BYTES]
    return truncated.decode("utf-8", errors="ignore") + "…"

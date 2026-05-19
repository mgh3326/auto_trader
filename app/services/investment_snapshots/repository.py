# app/services/investment_snapshots/repository.py
"""ROB-269 Phase 1 — DAO over investment_snapshot_* tables.

Append-only invariant: ``insert_*`` and ``link_*`` are the only writes.
``UPDATE`` and ``DELETE`` are intentionally absent. A separate test
(``test_append_only.py``) verifies this is enforced.

Hash + idempotency_key composition lives here (not in the schema) so the
dedup UNIQUE constraint can rely on a deterministic input.
"""

from __future__ import annotations

import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.investment_snapshots import (
    InvestmentSnapshot,
    InvestmentSnapshotBundle,
    InvestmentSnapshotBundleItem,
    InvestmentSnapshotRun,
)
from app.schemas.investment_snapshots import (
    BundleCreate,
    BundleItemCreate,
    SnapshotCreate,
    SnapshotRunCreate,
)
from app.services.action_report.common.canonicalize import canonical_payload_hash


class InvestmentSnapshotsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ------------------------------------------------------------------
    # Runs
    # ------------------------------------------------------------------
    async def insert_run(self, payload: SnapshotRunCreate) -> InvestmentSnapshotRun:
        data: dict[str, Any] = payload.model_dump()
        # metadata is a reserved keyword in some contexts, model uses run_metadata for the field 'metadata'
        data["run_metadata"] = data.pop("run_metadata")
        row = InvestmentSnapshotRun(**data)
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return row

    async def get_run_by_uuid(
        self, run_uuid: uuid.UUID
    ) -> InvestmentSnapshotRun | None:
        return await self._session.scalar(
            sa.select(InvestmentSnapshotRun).where(
                InvestmentSnapshotRun.run_uuid == run_uuid
            )
        )

    # ------------------------------------------------------------------
    # Snapshots
    # ------------------------------------------------------------------
    async def insert_snapshot(self, payload: SnapshotCreate) -> InvestmentSnapshot:
        """Insert (or reuse) an immutable snapshot artifact.

        Dedup semantics ("first writer wins"):
        The UNIQUE constraint ``(canonical_payload_hash, snapshot_kind, market,
        account_scope)`` deliberately omits ``run_id`` so that an identical
        payload collected in a later run reuses the existing row instead of
        creating a duplicate. As a consequence, the row returned by the dedup
        branch carries the **first** writer's ``run_id`` / ``idempotency_key``
        — not the current call's. Run-membership for the current call must be
        recorded via ``link_bundle_item`` (the bundle linkage is the
        authoritative "this run consumed that snapshot" record). Callers
        wanting to assert "this snapshot is from my run" should check
        ``snapshot.run_id == my_run.id`` and treat ``!=`` as a normal reuse,
        not an error.
        """
        # 1. Resolve run.
        run = await self.get_run_by_uuid(payload.run_uuid)
        if run is None:
            raise ValueError(f"run not found: {payload.run_uuid}")

        # 2. Compute canonical hash + idempotency key for the *new-row* path.
        #    Note: if dedup short-circuits below, the returned row's
        #    idempotency_key reflects the first writer, not this composition.
        canonical_hash = canonical_payload_hash(payload.payload_json)
        symbol_component = payload.symbol or "_"
        idempotency_key = (
            f"{run.run_uuid}:{payload.snapshot_kind}:"
            f"{symbol_component}:{canonical_hash[:12]}"
        )

        # 3. Dedup short-circuit — same canonical payload reuses the existing
        #    row across runs (intentional, see docstring above).
        existing = await self._session.scalar(
            sa.select(InvestmentSnapshot).where(
                InvestmentSnapshot.canonical_payload_hash == canonical_hash,
                InvestmentSnapshot.snapshot_kind == payload.snapshot_kind,
                InvestmentSnapshot.market == payload.market,
                InvestmentSnapshot.account_scope == payload.account_scope,
            )
        )
        if existing is not None:
            return existing

        # 4. Insert.
        data = payload.model_dump(exclude={"run_uuid"})
        row = InvestmentSnapshot(
            run_id=run.id,
            canonical_payload_hash=canonical_hash,
            idempotency_key=idempotency_key,
            **data,
        )
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return row

    async def get_snapshot_by_uuid(
        self, snapshot_uuid: uuid.UUID
    ) -> InvestmentSnapshot | None:
        return await self._session.scalar(
            sa.select(InvestmentSnapshot).where(
                InvestmentSnapshot.snapshot_uuid == snapshot_uuid
            )
        )

    # ------------------------------------------------------------------
    # Bundles
    # ------------------------------------------------------------------
    async def insert_bundle(self, payload: BundleCreate) -> InvestmentSnapshotBundle:
        # Bundle idempotency_key default: deterministic over identity tuple.
        idempotency_key = (
            f"bundle:{payload.purpose}:{payload.market}:"
            f"{payload.account_scope or '_'}:{payload.policy_version}:"
            f"{payload.as_of.isoformat()}"
        )
        existing = await self._session.scalar(
            sa.select(InvestmentSnapshotBundle).where(
                InvestmentSnapshotBundle.idempotency_key == idempotency_key
            )
        )
        if existing is not None:
            return existing
        data = payload.model_dump()
        row = InvestmentSnapshotBundle(idempotency_key=idempotency_key, **data)
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return row

    async def link_bundle_item(
        self, *, bundle_uuid: uuid.UUID, item: BundleItemCreate
    ) -> InvestmentSnapshotBundleItem:
        bundle = await self._session.scalar(
            sa.select(InvestmentSnapshotBundle).where(
                InvestmentSnapshotBundle.bundle_uuid == bundle_uuid
            )
        )
        if bundle is None:
            raise ValueError(f"bundle not found: {bundle_uuid}")
        snapshot = await self.get_snapshot_by_uuid(item.snapshot_uuid)
        if snapshot is None:
            raise ValueError(f"snapshot not found: {item.snapshot_uuid}")
        # Reuse if same (bundle, snapshot) already linked.
        existing = await self._session.scalar(
            sa.select(InvestmentSnapshotBundleItem).where(
                InvestmentSnapshotBundleItem.bundle_id == bundle.id,
                InvestmentSnapshotBundleItem.snapshot_id == snapshot.id,
            )
        )
        if existing is not None:
            return existing
        row = InvestmentSnapshotBundleItem(
            bundle_id=bundle.id, snapshot_id=snapshot.id, role=item.role
        )
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return row

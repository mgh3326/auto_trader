"""ROB-269 Phase 2 — Snapshot refresh-request service.

Records an operator/reviewer/scheduler request to refresh snapshots. The
service writes **one** ``investment_snapshot_runs`` row and returns. It
does **not** collect data — Phase 3 schedulers will discover the row and
transition it to ``completed`` / ``partial`` / ``failed``.

Allowed because:
* It is the only write surface that does not require fresh collection.
* The run row is the audit trail for "who asked, why, when".
* No broker/order mutation; only snapshot domain write.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.investment_snapshots import SnapshotRunCreate
from app.schemas.investment_snapshots_mcp import RefreshRequest, RefreshResponse
from app.services.investment_snapshots.policy import get_policy
from app.services.investment_snapshots.repository import (
    InvestmentSnapshotsRepository,
)


class SnapshotRefreshRequestService:
    def __init__(
        self,
        session: AsyncSession,
        repository: InvestmentSnapshotsRepository | None = None,
    ) -> None:
        self._session = session
        self._repo = repository or InvestmentSnapshotsRepository(session)

    async def record(self, request: RefreshRequest) -> RefreshResponse:
        policy = get_policy(request.policy_version)
        run = await self._repo.insert_run(
            SnapshotRunCreate(
                purpose=request.purpose,
                market=request.market,
                account_scope=request.account_scope,
                requested_by=request.requested_by,
                policy_version=policy.policy_version,
                policy_snapshot_json=policy.to_snapshot_json(),
                refresh_reason=request.reason,
                run_metadata={
                    "symbols_filter": request.symbols,
                    "snapshot_kinds_filter": request.snapshot_kinds,
                },
            )
        )
        return RefreshResponse(run_uuid=run.run_uuid, status="running")

"""ROB-269 Phase 2 — SnapshotBundleEnsureService.

The core service that turns an ``EnsureBundleRequest`` into a persisted
bundle. Reuses a fresh bundle if one exists for the identity tuple;
otherwise creates a new run, collects per-kind data (manual snapshots
or via the collector registry), and assembles a bundle whose status
reflects required-vs-optional outcomes.

Phase 2 invariants:
* Only writes to ``review.investment_snapshot_*`` tables.
* No external HTTP. Collectors are an injectable seam; the production
  registry is empty in Phase 2 (Phase 3 wires KIS / journal / market /
  news collectors). Tests register fakes.
* Run.status stays at the default ``'running'`` — append-only repository
  contract from Phase 1 means no UPDATE path. Bundle.status is the
  authoritative outcome record.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.investment_snapshots import (
    BundleCreate,
    BundleItemCreate,
    SnapshotCreate,
    SnapshotRunCreate,
)
from app.schemas.investment_snapshots_mcp import (
    EnsureBundleRequest,
    EnsureBundleResponse,
)
from app.services.investment_snapshots.collectors import (
    CollectorRequest,
    SnapshotCollectorRegistry,
    SnapshotCollectResult,
    default_collector_registry,
)
from app.services.investment_snapshots.freshness import (
    FreshnessStatus,
    classify_freshness,
)
from app.services.investment_snapshots.policy import (
    SnapshotKindPolicy,
    get_policy,
)
from app.services.investment_snapshots.repository import (
    InvestmentSnapshotsRepository,
)


def _utcnow() -> dt.datetime:
    return dt.datetime.now(tz=dt.UTC)


class SnapshotBundleEnsureService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        repository: InvestmentSnapshotsRepository | None = None,
        collectors: SnapshotCollectorRegistry | None = None,
        clock=None,  # callable[[], datetime] for tests
    ) -> None:
        self._session = session
        self._repo = repository or InvestmentSnapshotsRepository(session)
        self._collectors = collectors or default_collector_registry()
        self._clock = clock or _utcnow

    async def ensure(self, request: EnsureBundleRequest) -> EnsureBundleResponse:
        policy = get_policy(request.policy_version)
        now = self._clock()

        # 1. Reuse path: most recent bundle within bundle_ttl wins.
        latest = await self._repo.find_latest_bundle(
            purpose=request.purpose,
            market=request.market,
            account_scope=request.account_scope,
            policy_version=policy.policy_version,
        )
        if latest is not None:
            bundle_freshness = classify_freshness(
                as_of=latest.as_of, now=now, policy=policy.bundle_ttl
            )
            if bundle_freshness in ("fresh", "soft_stale"):
                return EnsureBundleResponse(
                    bundle_uuid=latest.bundle_uuid,
                    status="reused",
                    created=False,
                    coverage_summary=latest.coverage_summary,
                    freshness_summary=latest.freshness_summary,
                    missing_sources=[],
                    warnings=(
                        []
                        if bundle_freshness == "fresh"
                        else [f"bundle is {bundle_freshness} but within hard TTL"]
                    ),
                    run_uuid=None,
                )

        # 2. reuse_only with no fresh bundle is a soft failure — no DB write.
        if request.mode == "reuse_only":
            return EnsureBundleResponse(
                bundle_uuid=None,
                status="failed",
                created=False,
                warnings=["reuse_only requested but no fresh bundle exists"],
                run_uuid=None,
            )

        # 3. ensure_fresh path — create a run + collect + persist.
        run = await self._repo.insert_run(
            SnapshotRunCreate(
                purpose="report_generation",
                market=request.market,
                account_scope=request.account_scope,
                requested_by=request.requested_by,
                policy_version=policy.policy_version,
                policy_snapshot_json=policy.to_snapshot_json(),
                refresh_reason=f"ensure_bundle purpose={request.purpose}",
                run_metadata={
                    "ensure_request": {
                        "purpose": request.purpose,
                        "mode": request.mode,
                        "symbols": request.symbols,
                        "candidate_limit": request.candidate_limit,
                        "manual_snapshot_kinds": (
                            sorted((request.manual_snapshots or {}).keys())
                        ),
                    }
                },
            )
        )

        coverage: dict[str, dict[str, str]] = {"required": {}, "optional": {}}
        freshness_summary: dict[str, dict[str, str]] = {}
        missing_sources: list[str] = []
        warnings: list[str] = []
        linked_items: list[tuple[Any, str]] = []  # (snapshot_uuid, role)

        for kind_policy in policy.kinds:
            results, kind_warnings, attempted = await self._collect_for_kind(
                kind_policy=kind_policy,
                request=request,
                policy_snapshot=policy.to_snapshot_json(),
            )
            warnings.extend(kind_warnings)
            bucket = "required" if kind_policy.required else "optional"
            role = "required" if kind_policy.required else "optional"

            if not results:
                # Required kinds always count as 'unavailable' when empty
                # (caller expected the data; absence is a real gap).
                # Optional kinds split:
                #   - attempted (manual passed empty OR collector failed/timed out)
                #     → 'unavailable', contributes to bundle=partial.
                #   - not attempted (no manual + no collector registered)
                #     → silent skip; bundle status unaffected. In Phase 2 the
                #     production registry is empty so most optional kinds
                #     fall here when callers don't supply manual data.
                if kind_policy.required or attempted:
                    coverage[bucket][kind_policy.snapshot_kind] = "unavailable"
                    freshness_summary[kind_policy.snapshot_kind] = {
                        "status": "unavailable"
                    }
                    missing_sources.append(kind_policy.snapshot_kind)
                continue

            kind_statuses: list[str] = []
            last_as_of: dt.datetime | None = None
            for result in results:
                # Collectors run after ``now`` is captured for the reuse gate.
                # Live collectors can legitimately stamp results a few seconds
                # after the ensure started, so classify against the post-collect
                # clock instead of treating long collection time as future data.
                classification_now = self._clock()
                computed_status: FreshnessStatus = classify_freshness(
                    as_of=result.as_of,
                    now=classification_now,
                    policy=kind_policy.freshness,
                )
                # Caller-supplied status (e.g. 'partial' or 'unavailable') can
                # downgrade but never upgrade past the policy-classified one.
                effective_status = _worse_of(result.freshness_status, computed_status)
                snap = await self._repo.insert_snapshot(
                    SnapshotCreate(
                        run_uuid=run.run_uuid,
                        snapshot_kind=result.snapshot_kind,
                        market=result.market,
                        account_scope=result.account_scope,
                        symbol=result.symbol,
                        source_table=result.source_table,
                        source_id=result.source_id,
                        source_uri=result.source_uri,
                        source_kind=result.source_kind,
                        payload_json=result.payload_json,
                        source_timestamps_json=result.source_timestamps_json,
                        coverage_json=result.coverage_json,
                        errors_json=result.errors_json,
                        as_of=result.as_of,
                        valid_until=classification_now + kind_policy.freshness.hard_ttl,
                        freshness_status=effective_status,
                    )
                )
                linked_items.append((snap.snapshot_uuid, role))
                kind_statuses.append(effective_status)
                last_as_of = result.as_of

            worst_status = _worst_status(kind_statuses)
            coverage[bucket][kind_policy.snapshot_kind] = worst_status
            freshness_summary[kind_policy.snapshot_kind] = {
                "status": worst_status,
                "as_of": last_as_of.isoformat() if last_as_of else None,
                "result_count": str(len(results)),
            }

        bundle_status = _derive_bundle_status(coverage)

        bundle = await self._repo.insert_bundle(
            BundleCreate(
                purpose=request.purpose,
                market=request.market,
                account_scope=request.account_scope,
                policy_version=policy.policy_version,
                policy_snapshot_json=policy.to_snapshot_json(),
                as_of=now,
                status=bundle_status,
                coverage_summary=coverage,
                freshness_summary=freshness_summary,
            )
        )

        for snapshot_uuid, role in linked_items:
            await self._repo.link_bundle_item(
                bundle_uuid=bundle.bundle_uuid,
                item=BundleItemCreate(snapshot_uuid=snapshot_uuid, role=role),
            )

        return EnsureBundleResponse(
            bundle_uuid=bundle.bundle_uuid,
            status=bundle_status,
            created=True,
            coverage_summary=coverage,
            freshness_summary=freshness_summary,
            missing_sources=missing_sources,
            warnings=warnings,
            run_uuid=run.run_uuid,
        )

    async def _collect_for_kind(
        self,
        *,
        kind_policy: SnapshotKindPolicy,
        request: EnsureBundleRequest,
        policy_snapshot: dict[str, Any],
    ) -> tuple[list[SnapshotCollectResult], list[str], bool]:
        """Return (results, warnings, attempted).

        ``attempted=False`` means there was no manual data AND no collector
        registered for this kind — caller didn't ask, the system didn't have
        a way to ask. Optional kinds in this state are silently skipped.
        """
        kind = kind_policy.snapshot_kind
        manual = (request.manual_snapshots or {}).get(kind)
        if manual is not None:
            # Manual list is considered an attempt even if empty.
            return list(manual), [], True

        collector = self._collectors.get(kind)
        if collector is None:
            return [], [], False

        collect_request = CollectorRequest(
            market=request.market,
            account_scope=request.account_scope,
            symbols=request.symbols,
            candidate_limit=request.candidate_limit,
            policy_snapshot=policy_snapshot,
            user_id=request.user_id,
        )
        try:
            results = await asyncio.wait_for(
                collector.collect(collect_request),
                timeout=kind_policy.collector_timeout.total_seconds(),
            )
            return list(results), [], True
        except TimeoutError:
            return [], [f"{kind}: collector timed out"], True
        except Exception as exc:  # noqa: BLE001 — collector failures must not crash ensure
            return (
                [],
                [f"{kind}: collector raised {type(exc).__name__}: {exc}"],
                True,
            )


# ---------------------------------------------------------------------------
# Status derivation helpers
# ---------------------------------------------------------------------------
_STATUS_RANK: dict[str, int] = {
    "fresh": 0,
    "soft_stale": 1,
    "partial": 2,
    "hard_stale": 3,
    "unavailable": 4,
}


def _worse_of(a: str, b: str) -> str:
    """Return the worse (numerically higher) of two freshness labels."""
    return a if _STATUS_RANK.get(a, 99) >= _STATUS_RANK.get(b, 99) else b


def _worst_status(statuses: list[str]) -> str:
    if not statuses:
        return "unavailable"
    return max(statuses, key=lambda s: _STATUS_RANK.get(s, 99))


def _derive_bundle_status(
    coverage: dict[str, dict[str, str]],
) -> str:
    required_statuses = set(coverage.get("required", {}).values())
    optional_statuses = set(coverage.get("optional", {}).values())
    all_statuses = required_statuses | optional_statuses

    if not required_statuses:
        # No required kinds in policy — should not happen in v1, but fall through safely.
        return (
            "complete" if not all_statuses or all_statuses == {"fresh"} else "partial"
        )

    if "unavailable" in required_statuses:
        return "failed"
    if "hard_stale" in required_statuses:
        return "stale_fallback"
    if (
        "soft_stale" in required_statuses
        or "partial" in required_statuses
        or "unavailable" in optional_statuses
        or "soft_stale" in optional_statuses
        or "partial" in optional_statuses
        or "hard_stale" in optional_statuses
    ):
        return "partial"
    return "complete"

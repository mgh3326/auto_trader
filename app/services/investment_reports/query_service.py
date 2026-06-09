"""ROB-265 — Read-only query service over the investment_* tables.

Wraps the repository with the higher-level read shapes the next layers
(MCP/API in Plan 3, frontend in Plan 5) need. ``get_bundle`` returns a
single report with all its nested context. ``previous_report_context``
implements locked refinement #7 — context retrieval is a *query* over
prior reports, not a single-link traversal via ``previous_report_uuid``.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.investment_reports import (
    InvestmentReport,
    InvestmentReportItem,
    InvestmentReportItemDecision,
    InvestmentWatchAlert,
    InvestmentWatchEvent,
)
from app.schemas.investment_reports import (
    ReportSnapshotBundleItemView,
    ReportSnapshotBundleResponse,
    ReportSnapshotBundleSummaryView,
    ReportSnapshotDetailResponse,
)
from app.services.investment_reports.repository import InvestmentReportsRepository
from app.services.investment_snapshots.repository import (
    InvestmentSnapshotsRepository,
)

# ROB-375 Bug 1 — advisory reports persist as status="draft" (no publish step),
# but so does smoke/CI boilerplate. ``created_by_profile`` is the reliable
# discriminator: the Hermes advisory composition path hard-codes
# "HERMES_ADVISOR" (investment_hermes_handlers / hermes_ingest), and no
# smoke/test/operator profile ("t", "test", "schedule", "pilot-operator", …)
# uses it. So a draft counts as an advisory prior iff it carries an advisory
# profile.
#
# ROB-459 P3 — Claude-authored advisory reports use "CLAUDE_ADVISOR"; add it to
# the default set so they chain as baseline too. Operators may extend the set
# via INVESTMENT_ADVISORY_DRAFT_PROFILES (UNION only — see _advisory_draft_profiles).
_DEFAULT_ADVISORY_DRAFT_PROFILES: frozenset[str] = frozenset(
    {"HERMES_ADVISOR", "CLAUDE_ADVISOR"}
)

# Allowed draft policies for ``previous_report_context``. There is intentionally
# NO "all" policy — admitting every draft would re-introduce smoke boilerplate.
DRAFT_POLICY_EXCLUDE = "exclude"
DRAFT_POLICY_ADVISORY_ONLY = "advisory_only"
_VALID_DRAFT_POLICIES: frozenset[str] = frozenset(
    {DRAFT_POLICY_EXCLUDE, DRAFT_POLICY_ADVISORY_ONLY}
)


def _advisory_draft_profiles() -> frozenset[str]:
    """Runtime advisory-draft whitelist: built-in defaults UNION operator config.

    Union-only / fail-closed: operators may ADD genuine advisory profiles via
    ``INVESTMENT_ADVISORY_DRAFT_PROFILES`` but cannot drop a default or admit
    every draft (there is still no "all" policy). Smoke/test profiles ("t",
    "test", …) stay excluded unless explicitly listed.
    """
    from app.core.config import settings

    extra = frozenset(settings.INVESTMENT_ADVISORY_DRAFT_PROFILES or [])
    return _DEFAULT_ADVISORY_DRAFT_PROFILES | extra


def _is_advisory_draft(report: InvestmentReport) -> bool:
    """True when a draft report is a genuine advisory baseline (not smoke)."""
    return report.created_by_profile in _advisory_draft_profiles()


class InvestmentReportQueryService:
    """Read-only queries — list / get / latest / previous-context."""

    def __init__(
        self,
        session: AsyncSession,
        repository: InvestmentReportsRepository | None = None,
        snapshot_repository: InvestmentSnapshotsRepository | None = None,
    ) -> None:
        self._session = session
        self._repo = repository or InvestmentReportsRepository(session)
        self._snap_repo = snapshot_repository or InvestmentSnapshotsRepository(session)

    async def list_reports(
        self,
        *,
        market: str | None = None,
        market_session: str | None = None,
        account_scope: str | None = None,
        status: str | None = None,
        report_type: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[InvestmentReport]:
        return await self._repo.list_reports(
            market=market,
            market_session=market_session,
            account_scope=account_scope,
            status=status,
            report_type=report_type,
            limit=limit,
            offset=offset,
        )

    async def latest_report(
        self,
        *,
        market: str | None = None,
        market_session: str | None = None,
        account_scope: str | None = None,
        status: str | None = None,
        report_type: str | None = None,
    ) -> InvestmentReport | None:
        return await self._repo.latest_report(
            market=market,
            market_session=market_session,
            account_scope=account_scope,
            status=status,
            report_type=report_type,
        )

    async def get_bundle(self, report_uuid: UUID) -> dict[str, Any] | None:
        """Return the report + nested items, decisions, alerts, recent events.

        Returns ``None`` if the report doesn't exist.
        """
        report = await self._repo.get_report_by_uuid(report_uuid)
        if report is None:
            return None

        items = await self._repo.list_items_for_report(report.id)
        item_ids = [it.id for it in items]
        decisions = await self._repo.list_decisions_for_items(item_ids)
        alerts = await self._repo.list_alerts_for_source_reports([report.report_uuid])
        events = await self._repo.list_events_for_source_reports([report.report_uuid])
        citations = await self._repo.list_news_citations_for_report(report.report_uuid)

        decisions_by_item: dict[int, list[InvestmentReportItemDecision]] = {
            it.id: [] for it in items
        }
        for d in decisions:
            decisions_by_item.setdefault(d.item_id, []).append(d)

        return {
            "report": report,
            "items": items,
            "decisions_by_item": decisions_by_item,
            "alerts": alerts,
            "events": events,
            "news_citations": citations,
        }

    # ------------------------------------------------------------------
    # ROB-275 — Report-centric snapshot evidence read paths.
    # ------------------------------------------------------------------
    async def get_report_snapshot_bundle(
        self, report_uuid: UUID
    ) -> ReportSnapshotBundleResponse | None:
        """Return the bundle + linked items for a report, or a legacy shape.

        Returns:
          * ``None`` if the report doesn't exist (router → 404).
          * ``ReportSnapshotBundleResponse(legacy_no_snapshot=True, ...)``
            if the report exists but has no ``snapshot_bundle_uuid`` (router → 200).
          * ``ReportSnapshotBundleResponse(legacy_no_snapshot=False, bundle=...,
            items=[...], ...)`` with full bundle and item views otherwise.

        Note: ``unavailable_sources`` and ``source_conflicts`` come from
        the report row, never from the bundle — they describe what the
        report's generator observed at write time, not what is *linked*
        to this bundle. UI renders them in separate sections.
        """
        report = await self._repo.get_report_by_uuid(report_uuid)
        if report is None:
            return None
        if report.snapshot_bundle_uuid is None:
            return ReportSnapshotBundleResponse(
                legacy_no_snapshot=True,
                unavailable_sources=report.unavailable_sources,
                source_conflicts=report.source_conflicts,
            )

        bundle = await self._snap_repo.get_bundle_by_uuid(report.snapshot_bundle_uuid)
        if bundle is None:
            # Defensive: report.snapshot_bundle_uuid is a logical link
            # (no FK), so a deleted bundle is possible in theory. Treat
            # as legacy/no-snapshot rather than failing the page.
            return ReportSnapshotBundleResponse(
                legacy_no_snapshot=True,
                unavailable_sources=report.unavailable_sources,
                source_conflicts=report.source_conflicts,
            )

        pairs = await self._snap_repo.list_bundle_items_with_snapshots(bundle.id)
        item_views = [
            ReportSnapshotBundleItemView(
                snapshot_uuid=snap.snapshot_uuid,
                role=item.role,  # type: ignore[arg-type]
                snapshot_kind=snap.snapshot_kind,  # type: ignore[arg-type]
                source_kind=snap.source_kind,  # type: ignore[arg-type]
                market=snap.market,  # type: ignore[arg-type]
                symbol=snap.symbol,
                account_scope=snap.account_scope,  # type: ignore[arg-type]
                freshness_status=snap.freshness_status,  # type: ignore[arg-type]
                as_of=snap.as_of,
                valid_until=snap.valid_until,
                source_table=snap.source_table,
                source_id=snap.source_id,
                source_uri=snap.source_uri,
                payload_size_bytes=_payload_size_bytes(snap.payload_json),
            )
            for item, snap in pairs
        ]
        bundle_view = ReportSnapshotBundleSummaryView(
            bundle_uuid=bundle.bundle_uuid,
            purpose=bundle.purpose,
            market=bundle.market,  # type: ignore[arg-type]
            account_scope=bundle.account_scope,  # type: ignore[arg-type]
            policy_version=bundle.policy_version,
            status=bundle.status,  # type: ignore[arg-type]
            as_of=bundle.as_of,
            coverage_summary=bundle.coverage_summary,
            freshness_summary=bundle.freshness_summary,
            created_at=bundle.created_at,
        )
        return ReportSnapshotBundleResponse(
            legacy_no_snapshot=False,
            bundle=bundle_view,
            items=item_views,
            unavailable_sources=report.unavailable_sources,
            source_conflicts=report.source_conflicts,
        )

    async def get_report_snapshot_detail(
        self, report_uuid: UUID, snapshot_uuid: UUID
    ) -> ReportSnapshotDetailResponse | None:
        """Return one snapshot's payload + bundle role/context for a report.

        Membership-checked: returns ``None`` (router → 404) when any of:
          * the report does not exist
          * the report has no ``snapshot_bundle_uuid``
          * the snapshot is not a member of this report's bundle
        Snapshots that exist globally but belong to a different bundle
        always return None — they are not addressable via this report's
        URL even though the underlying ``investment_snapshots`` row is
        globally reusable.
        """
        report = await self._repo.get_report_by_uuid(report_uuid)
        if report is None or report.snapshot_bundle_uuid is None:
            return None
        pair = await self._snap_repo.get_bundle_item_with_snapshot(
            bundle_uuid=report.snapshot_bundle_uuid,
            snapshot_uuid=snapshot_uuid,
        )
        if pair is None:
            return None
        item, snap = pair
        return ReportSnapshotDetailResponse(
            snapshot_uuid=snap.snapshot_uuid,
            role=item.role,  # type: ignore[arg-type]
            snapshot_kind=snap.snapshot_kind,  # type: ignore[arg-type]
            source_kind=snap.source_kind,  # type: ignore[arg-type]
            market=snap.market,  # type: ignore[arg-type]
            symbol=snap.symbol,
            account_scope=snap.account_scope,  # type: ignore[arg-type]
            source_table=snap.source_table,
            source_id=snap.source_id,
            source_uri=snap.source_uri,
            freshness_status=snap.freshness_status,  # type: ignore[arg-type]
            as_of=snap.as_of,
            valid_until=snap.valid_until,
            source_timestamps_json=snap.source_timestamps_json,
            coverage_json=snap.coverage_json,
            errors_json=snap.errors_json,
            payload_json=snap.payload_json,
        )

    async def previous_report_context(
        self,
        *,
        market: str,
        market_session: str | None = None,
        account_scope: str | None = None,
        report_type: str | None = None,
        exclude_report_uuid: UUID | None = None,
        n_prior: int = 3,
        events_since: datetime | None = None,
        draft_policy: str = DRAFT_POLICY_EXCLUDE,
    ) -> dict[str, Any]:
        """Locked refinement #7 — previous context is a query, not a single FK.

        Returns the most recent N prior reports matching the filters plus
        the unresolved/deferred items, active watches, triggered watch
        events, and recent decisions that span those reports.
        """
        if draft_policy not in _VALID_DRAFT_POLICIES:
            raise ValueError(
                f"draft_policy must be one of {sorted(_VALID_DRAFT_POLICIES)}; "
                f"got {draft_policy!r}"
            )

        # ROB-352 Slice B / ROB-375 Bug 1 — fetch a buffer so dropping excluded
        # drafts + the excluded uuid still yields up to n_prior kept rows. NOTE:
        # silently under-fills (returns < n_prior) if more than
        # _DRAFT_FETCH_BUFFER consecutive dropped drafts precede the last wanted
        # row; raise the buffer if smoke density grows.
        _DRAFT_FETCH_BUFFER = 5
        prior_reports: list[InvestmentReport] = await self._repo.list_reports(
            market=market,
            market_session=market_session,
            account_scope=account_scope,
            report_type=report_type,
            limit=n_prior + 1 + _DRAFT_FETCH_BUFFER,
        )
        if exclude_report_uuid is not None:
            prior_reports = [
                r for r in prior_reports if r.report_uuid != exclude_report_uuid
            ]
        # Draft handling: 'exclude' drops every draft (default); 'advisory_only'
        # keeps advisory drafts (HERMES_ADVISOR) but still drops smoke drafts.
        if draft_policy == DRAFT_POLICY_ADVISORY_ONLY:
            prior_reports = [
                r for r in prior_reports if r.status != "draft" or _is_advisory_draft(r)
            ]
        else:  # DRAFT_POLICY_EXCLUDE
            prior_reports = [r for r in prior_reports if r.status != "draft"]
        prior_reports = prior_reports[:n_prior]

        prior_report_uuids = [r.report_uuid for r in prior_reports]
        prior_report_ids = [r.id for r in prior_reports]

        # Items: deferred-status only, across the prior report set.
        unresolved_deferred_items: list[InvestmentReportItem] = []
        for r in prior_reports:
            r_items = await self._repo.list_items_for_report(r.id)
            unresolved_deferred_items.extend(
                it for it in r_items if it.status == "deferred"
            )

        # Active watches sourced from those reports.
        active_watches: list[
            InvestmentWatchAlert
        ] = await self._repo.list_alerts_for_source_reports(
            prior_report_uuids, status="active"
        )

        # Recent triggered events linked to those source reports.
        triggered_events: list[
            InvestmentWatchEvent
        ] = await self._repo.list_events_for_source_reports(
            prior_report_uuids, since=events_since
        )

        # Recent decisions on items in those reports.
        all_item_ids: list[int] = []
        for r in prior_reports:
            r_items = await self._repo.list_items_for_report(r.id)
            all_item_ids.extend(it.id for it in r_items)
        recent_decisions: list[
            InvestmentReportItemDecision
        ] = await self._repo.list_decisions_for_items(all_item_ids)

        # `prior_report_ids` is consumed indirectly above; keep it in the
        # return shape for callers that want the integer IDs.
        return {
            "prior_reports": prior_reports,
            "prior_report_ids": prior_report_ids,
            "unresolved_deferred_items": unresolved_deferred_items,
            "active_watches": active_watches,
            "triggered_events": triggered_events,
            "recent_decisions": recent_decisions,
        }


def _payload_size_bytes(payload_json: dict[str, Any] | None) -> int | None:
    """Cheap UTF-8 byte count of a JSON-serialised payload. ``None`` if missing."""
    if payload_json is None:
        return None
    return len(
        json.dumps(payload_json, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
    )

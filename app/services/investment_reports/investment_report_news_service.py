# app/services/investment_reports/investment_report_news_service.py
"""ROB-423 PR2 — persist Hermes-marked news citations + fetch_run audit.

auto_trader-side: validation + persistence only (no LLM, no fetch). Reads the
bundle's news snapshot ``articles``/``fetch_records`` (written by the PR1
collector), matches Hermes ``news_citations`` against them, and writes fetch_run
+ citation rows keyed by ``report_uuid``. Unmatched refs are dropped + recorded
in the report's ``unavailable_sources`` metadata (fail-open).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from app.models.investment_reports import InvestmentReport
from app.schemas.hermes_composition import HermesCompositionResult
from app.services.investment_reports.news_persistence import build_news_persistence
from app.services.investment_reports.repository import InvestmentReportsRepository

_INSTRUMENT_BY_MARKET = {"kr": "equity_kr", "us": "equity_us", "crypto": "crypto"}


class InvestmentReportNewsService:
    def __init__(self, repo: InvestmentReportsRepository) -> None:
        self._repo = repo

    async def persist_from_composition(
        self,
        *,
        report: InvestmentReport,
        composition: HermesCompositionResult,
        news_payloads: list[dict[str, Any]],
    ) -> int:
        """Returns the number of citations written. Never raises on matching
        gaps (fail-open). Call inside the ingest transaction after the report +
        items are persisted."""
        if not composition.news_citations:
            return 0

        # client_item_key -> item_uuid via insertion-order (id.asc()) zip.
        persisted = await self._repo.list_items_for_report_ordered_by_id(report.id)
        item_uuid_by_client_key: dict[str, UUID] = {}
        if len(persisted) == len(composition.items):
            for comp_item, row in zip(composition.items, persisted, strict=True):
                item_uuid_by_client_key[comp_item.client_item_key] = row.item_uuid

        instrument_type = _INSTRUMENT_BY_MARKET.get(report.market, "equity_us")
        plan = build_news_persistence(
            news_payloads=news_payloads,
            citations=composition.news_citations,
            item_uuid_by_client_key=item_uuid_by_client_key,
            instrument_type=instrument_type,
        )

        # fetch_runs first (citations FK them by (symbol, provider)).
        fetch_run_id_by_key: dict[tuple[str, str], int] = {}
        for run in plan.fetch_runs:
            key = run.pop("_fetch_key")
            row = await self._repo.insert_news_fetch_run(
                report_uuid=report.report_uuid,
                fetched_at=_utcnow(),
                **run,
            )
            fetch_run_id_by_key[key] = row.id

        written = 0
        for cit in plan.citations:
            key = cit.pop("_fetch_key")
            await self._repo.insert_news_citation(
                report_uuid=report.report_uuid,
                fetch_run_id=fetch_run_id_by_key.get(key),
                fetched_at=_utcnow(),
                **cit,
            )
            written += 1

        if plan.unmatched:
            # record on the report's metadata (fail-open, no fabrication).
            await self._repo.merge_report_unavailable_sources(
                report.id,
                {"news_citations_unmatched": plan.unmatched},
            )
        return written

    async def copy_for_mock(
        self, *, live_report_uuid: UUID, mock_report: InvestmentReport
    ) -> int:
        """Copy a live report's news citations onto the mock report (report-level,
        report_item_uuid=NULL). No re-fetch, no re-judgment. Returns count."""
        live_citations = await self._repo.list_news_citations_for_report(
            live_report_uuid
        )
        count = 0
        for c in live_citations:
            await self._repo.insert_news_citation(
                report_uuid=mock_report.report_uuid,
                report_item_uuid=None,
                section_key=c.section_key,
                fetch_run_id=None,
                market=c.market,
                symbol=c.symbol,
                provider=c.provider,
                external_article_id=c.external_article_id,
                canonical_url=c.canonical_url,
                source_name=c.source_name,
                title=c.title,
                summary_snapshot=c.summary_snapshot,
                published_at=c.published_at,
                fetched_at=c.fetched_at,
                relevance=c.relevance,
                role=c.role,
                decision_impact=c.decision_impact,
                selection_reason=c.selection_reason,
                confidence=c.confidence,
                metadata_json={"copied_from_report_uuid": str(live_report_uuid)},
            )
            count += 1
        return count


def _utcnow():
    from datetime import UTC, datetime

    return datetime.now(tz=UTC)

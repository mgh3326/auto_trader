"""ROB-405 Slice E — link closed+verdict watch events to a follow-up report
item so the retrospective feeds the next cycle. Builds a thin follow-up report
via the ingestion service (atomic + idempotent report_key); sets the event FK
via repository.update_event_follow_up. Idempotent (FK-null filter). Default off.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

from sqlalchemy import select

from app.core.config import settings
from app.models.investment_reports import InvestmentWatchEvent
from app.models.review import TradeJournalCounterfactual, TradeJournalReview
from app.models.trade_journal import TradeJournal
from app.schemas.investment_reports import IngestReportItem, IngestReportRequest
from app.services.investment_reports.ingestion import InvestmentReportIngestionService
from app.services.investment_reports.repository import InvestmentReportsRepository

logger = logging.getLogger(__name__)


async def sync_watch_follow_up_items(db, *, force: bool = False) -> dict[str, Any]:
    if not force and not settings.WATCH_FOLLOW_UP_LINK_ENABLED:
        return {"status": "disabled", "linked": 0}

    events = (
        await db.execute(
            select(InvestmentWatchEvent).where(
                InvestmentWatchEvent.follow_up_report_item_id.is_(None),
                InvestmentWatchEvent.correlation_id.is_not(None),
            )
        )
    ).scalars().all()
    if not events:
        return {"status": "ok", "linked": 0}

    cids = list({e.correlation_id for e in events})
    journals = (
        await db.execute(
            select(TradeJournal).where(
                TradeJournal.account_type == "mock",
                TradeJournal.status == "closed",
                TradeJournal.correlation_id.in_(cids),
            )
        )
    ).scalars().all()
    journal_by_cid = {j.correlation_id: j for j in journals}

    verdict_by_jid: dict[int, str] = {}
    if journals:
        for r in (
            await db.execute(
                select(TradeJournalReview).where(
                    TradeJournalReview.journal_id.in_([j.id for j in journals])
                )
            )
        ).scalars().all():
            verdict_by_jid.setdefault(r.journal_id, r.verdict)

    cf_by_cid: dict[str, TradeJournalCounterfactual] = {}
    for c in (
        await db.execute(
            select(TradeJournalCounterfactual).where(
                TradeJournalCounterfactual.correlation_id.in_(cids)
            )
        )
    ).scalars().all():
        cf_by_cid[c.correlation_id] = c

    groups: dict[tuple[str, str], list] = defaultdict(list)
    for e in events:
        j = journal_by_cid.get(e.correlation_id)
        if j is None:
            continue
        verdict = verdict_by_jid.get(j.id)
        if verdict is None:
            continue
        groups[(e.kst_date, e.market)].append((e, j, verdict, cf_by_cid.get(e.correlation_id)))

    if not groups:
        return {"status": "ok", "linked": 0}

    ingest = InvestmentReportIngestionService(db)
    repo = InvestmentReportsRepository(db)
    linked = 0
    for (kst_date, market), tuples in groups.items():
        items = []
        for e, j, verdict, cf in tuples:
            rationale = f"auto follow-up: verdict={verdict}, pnl_pct={j.pnl_pct}"
            if cf is not None:
                rationale += (
                    f", fill_vs_trigger={cf.fill_vs_trigger_pct}, "
                    f"no_action_vs_fill={cf.no_action_vs_fill_pct}"
                )
            items.append(
                IngestReportItem(
                    client_item_key=e.correlation_id,
                    item_kind="watch",
                    operation="review",
                    symbol=e.symbol,
                    intent="trend_recovery_review",
                    target_kind="asset",
                    rationale=rationale,
                    evidence_snapshot={
                        "correlation_id": e.correlation_id,
                        "verdict": verdict,
                        "pnl_pct": (str(j.pnl_pct) if j.pnl_pct is not None else None),
                        "fill_vs_trigger_pct": (
                            str(cf.fill_vs_trigger_pct)
                            if cf and cf.fill_vs_trigger_pct is not None
                            else None
                        ),
                        "no_action_vs_fill_pct": (
                            str(cf.no_action_vs_fill_pct)
                            if cf and cf.no_action_vs_fill_pct is not None
                            else None
                        ),
                    },
                )
            )
        req = IngestReportRequest(
            report_type="mock_loop_followup",
            market=market,
            account_scope="kis_mock",
            execution_mode="mock_preview",
            created_by_profile="rob405_followup",
            title=f"mock loop follow-up {kst_date}",
            summary="auto-generated retrospective follow-up",
            kst_date=kst_date,
            status="draft",
            items=items,
        )
        report, _reused, _count = await ingest.ingest_with_outcome(req)
        item_id_by_cid: dict[str, int] = {}
        for it in await repo.list_items_for_report(report.id):
            cid = (it.evidence_snapshot or {}).get("correlation_id")
            if cid:
                item_id_by_cid[cid] = it.id
        for e, _j, _v, _cf in tuples:
            item_id = item_id_by_cid.get(e.correlation_id)
            if item_id is not None:
                await repo.update_event_follow_up(
                    e.id, follow_up_report_item_id=item_id
                )
                linked += 1
    await db.commit()
    return {"status": "ok", "linked": linked}

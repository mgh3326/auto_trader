import datetime as dt
import uuid

import pytest

from app.models.investment_snapshots import InvestmentSnapshotBundle
from app.models.investment_stages import InvestmentStageRun
from app.models.research_reports import ResearchReport
from app.schemas.investment_snapshots import (
    BundleCreate,
    BundleItemCreate,
    SnapshotCreate,
    SnapshotRunCreate,
)
from app.services.investment_snapshots.repository import (
    InvestmentSnapshotsRepository,
)
from app.services.investment_stages.hermes_context import HermesContextExporter


async def _clear(db_session):
    from sqlalchemy import text

    await db_session.execute(text("DELETE FROM research_reports"))
    await db_session.commit()


def _report(dedup_key, *, published_at, title, symbols):
    return ResearchReport(
        dedup_key=dedup_key,
        report_type="research-reports.v1",
        source="naver_research",
        title=title,
        analyst="홍길동",
        summary_text="요약",
        detail_excerpt="발췌",
        published_at=published_at,
        published_at_text=published_at.isoformat(),
        symbol_candidates=[
            {"symbol": s, "market": "kr", "source": "naver_research"} for s in symbols
        ],
    )


@pytest.mark.asyncio
async def test_exporter_attaches_news_evidence_bundle(db_session) -> None:
    await _clear(db_session)

    # Seed a bundle for KR market
    bundle = InvestmentSnapshotBundle(
        bundle_uuid=uuid.uuid4(),
        purpose="report_generation",
        market="kr",
        account_scope=None,
        policy_version="intraday_action_report_v1",
        as_of=dt.datetime.now(tz=dt.UTC),
        status="complete",
        coverage_summary={},
        freshness_summary={},
        idempotency_key=str(uuid.uuid4()),
    )
    db_session.add(bundle)
    await db_session.commit()

    # Seed some recent research reports
    now = dt.datetime.now(tz=dt.UTC)
    db_session.add(
        _report(
            "k_context_1",
            published_at=now - dt.timedelta(hours=2),
            title="삼성전자 실적 전망",
            symbols=["005930"],
        )
    )
    await db_session.commit()

    # Seed an investment stage run associated with the bundle
    run = InvestmentStageRun(
        run_uuid=uuid.uuid4(),
        snapshot_bundle_uuid=bundle.bundle_uuid,
        market="kr",
        account_scope=None,
        policy_version="v1",
        generator_version="v1",
        status="running",
        started_at=dt.datetime.now(tz=dt.UTC),
    )
    db_session.add(run)
    await db_session.commit()

    exporter = HermesContextExporter(db_session, stages=[])
    payload = await exporter.export(snapshot_bundle_uuid=bundle.bundle_uuid)

    assert "news" in payload.dimension_evidence
    news_ev = payload.dimension_evidence["news"]
    assert news_ev["market"] == "kr"
    assert news_ev["count"] == 1
    assert news_ev["citations"][0]["title"] == "삼성전자 실적 전망"
    assert news_ev["citations"][0]["symbol_candidates"][0]["symbol"] == "005930"
    assert news_ev["freshness"]["status"] == "fresh"
    # No news-article snapshot present -> research_reports fallback path.
    assert news_ev["data_health"]["source"] == "research_reports"


@pytest.mark.asyncio
async def test_news_dimension_reads_article_snapshot_not_db(db_session) -> None:
    """ROB-374 B3 — when the bundle has a news-article snapshot, the dimension is
    built from it (matching stage_inputs.news), not from research_reports.

    A research report is also seeded to prove the snapshot wins over the DB.
    """
    await _clear(db_session)
    now = dt.datetime.now(tz=dt.UTC)
    db_session.add(
        _report("db1", published_at=now, title="DB-only report", symbols=["IBM"])
    )
    await db_session.commit()

    repo = InvestmentSnapshotsRepository(db_session)
    run = await repo.insert_run(
        SnapshotRunCreate(
            purpose="report_generation",
            market="us",
            account_scope="kis_live",
            requested_by="user",
            policy_version="intraday_action_report_v1",
            policy_snapshot_json={},
        )
    )
    news_snap = await repo.insert_snapshot(
        SnapshotCreate(
            run_uuid=run.run_uuid,
            snapshot_kind="news",
            market="us",
            source_kind="news_ingestor",
            payload_json={
                "source": "news_articles",
                "count": 2,
                "articles": [
                    {
                        "title": "Apple climbs on earnings",
                        "source": "finnhub",
                        "summary": "Beat estimates.",
                        "stock_symbol": "AAPL",
                        "stock_name": "Apple Inc.",
                        "published_at": now.isoformat(),
                    },
                    {
                        "title": "Nvidia rallies",
                        "source": "finnhub",
                        "summary": "AI demand.",
                        "stock_symbol": "NVDA",
                        "stock_name": "NVIDIA",
                        "published_at": now.isoformat(),
                    },
                ],
            },
            as_of=now,
            freshness_status="fresh",
        )
    )
    bundle = await repo.insert_bundle(
        BundleCreate(
            purpose="report_generation",
            market="us",
            account_scope="kis_live",
            policy_version="intraday_action_report_v1",
            as_of=now,
            status="complete",
            coverage_summary={},
            freshness_summary={},
        )
    )
    await repo.link_bundle_item(
        bundle_uuid=bundle.bundle_uuid,
        item=BundleItemCreate(snapshot_uuid=news_snap.snapshot_uuid, role="optional"),
    )
    await db_session.commit()

    exporter = HermesContextExporter(db_session, stages=[])
    payload = await exporter.export(snapshot_bundle_uuid=bundle.bundle_uuid)

    news_ev = payload.dimension_evidence["news"]
    assert news_ev["count"] == 2
    assert {c["title"] for c in news_ev["citations"]} == {
        "Apple climbs on earnings",
        "Nvidia rallies",
    }
    # The seeded DB report must NOT appear — the snapshot is authoritative.
    assert all(c["title"] != "DB-only report" for c in news_ev["citations"])
    assert news_ev["data_health"]["source"] == "news_articles"
    assert news_ev["citations"][0]["symbol_candidates"][0]["market"] == "us"

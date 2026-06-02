import datetime as dt

import pytest

from app.models.research_reports import ResearchReport
from app.services.investment_dimensions.news_evidence import build_news_evidence
from app.services.research_reports.query_service import ResearchReportsQueryService


async def _clear(db_session):
    from sqlalchemy import text

    await db_session.execute(text("DELETE FROM research_reports"))
    await db_session.commit()


def _report(dedup_key, *, published_at, title, symbols, market="kr"):
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
            {"symbol": s, "market": market, "source": "naver_research"} for s in symbols
        ],
    )


@pytest.mark.asyncio
async def test_build_news_evidence_fresh(db_session):
    await _clear(db_session)
    now = dt.datetime(2026, 5, 24, 12, 0, tzinfo=dt.UTC)
    db_session.add(
        _report(
            "k1",
            published_at=now - dt.timedelta(hours=2),
            title="삼성전자 목표가 상향",
            symbols=["005930"],
        )
    )
    await db_session.commit()

    bundle = await build_news_evidence(
        ResearchReportsQueryService(db_session), market="kr", now=now
    )
    assert bundle["market"] == "kr"
    assert bundle["count"] == 1
    assert bundle["citations"][0]["title"] == "삼성전자 목표가 상향"
    assert bundle["citations"][0]["symbol_candidates"][0]["symbol"] == "005930"
    assert bundle["freshness"]["status"] == "fresh"


@pytest.mark.asyncio
async def test_build_news_evidence_stale_when_old(db_session):
    await _clear(db_session)
    now = dt.datetime(2026, 5, 24, 12, 0, tzinfo=dt.UTC)
    db_session.add(
        _report(
            "k_old",
            published_at=now - dt.timedelta(days=5),
            title="오래된 리포트",
            symbols=["000660"],
        )
    )
    await db_session.commit()
    bundle = await build_news_evidence(
        ResearchReportsQueryService(db_session),
        market="kr",
        lookback_hours=24,
        now=now,
    )
    assert bundle["count"] == 1
    assert bundle["freshness"]["status"] == "stale"


@pytest.mark.asyncio
async def test_build_news_evidence_scopes_to_market_no_kr_bleed(db_session):
    # ROB-366 B8: a US bundle must NOT surface KR research (kis_truefriend bleed).
    await _clear(db_session)
    now = dt.datetime(2026, 5, 24, 12, 0, tzinfo=dt.UTC)
    db_session.add(
        _report(
            "kr1",
            published_at=now - dt.timedelta(hours=1),
            title="엔비디아 KR 노트",
            symbols=["NVDA"],
            market="kr",
        )
    )
    db_session.add(
        _report(
            "us1",
            published_at=now - dt.timedelta(hours=2),
            title="Apple US note",
            symbols=["AAPL"],
            market="us",
        )
    )
    await db_session.commit()

    bundle = await build_news_evidence(
        ResearchReportsQueryService(db_session), market="us", now=now
    )
    assert bundle["count"] == 1
    assert bundle["citations"][0]["title"] == "Apple US note"
    # The newer KR report must be excluded despite being more recent.
    assert all(c["title"] != "엔비디아 KR 노트" for c in bundle["citations"])


@pytest.mark.asyncio
async def test_build_news_evidence_empty_is_unavailable(db_session):
    await _clear(db_session)
    bundle = await build_news_evidence(
        ResearchReportsQueryService(db_session),
        market="us",
        now=dt.datetime(2026, 5, 24, tzinfo=dt.UTC),
    )
    assert bundle["count"] == 0
    assert bundle["citations"] == []
    assert bundle["freshness"]["status"] == "unavailable"


# --- ROB-374 B3: snapshot-article-preferred source --------------------------


def _article(title, *, symbol=None, name=None, published_at=None, summary="snippet"):
    return {
        "title": title,
        "url": f"https://news.example/{title}",
        "source": "finnhub",
        "feed_source": "rss_finnhub",
        "summary": summary,
        "stock_symbol": symbol,
        "stock_name": name,
        "published_at": published_at.isoformat() if published_at else None,
    }


@pytest.mark.asyncio
async def test_build_news_evidence_prefers_snapshot_articles_over_db(db_session):
    # The live ROB-374 mismatch: stage reads N article snapshots while the
    # dimension queried an empty research_reports table and reported 0. With a
    # news snapshot present the dimension must reflect it — even if the DB also
    # has rows, the snapshot wins (so it can never disagree with the stage).
    await _clear(db_session)
    now = dt.datetime(2026, 5, 24, 12, 0, tzinfo=dt.UTC)
    db_session.add(
        _report("db1", published_at=now, title="DB-only report", symbols=["IBM"])
    )
    await db_session.commit()

    snapshot_payload = {
        "articles": [
            _article("Apple climbs", published_at=now - dt.timedelta(hours=1)),
            _article("Nvidia rallies", published_at=now - dt.timedelta(hours=3)),
        ]
    }
    bundle = await build_news_evidence(
        ResearchReportsQueryService(db_session),
        market="us",
        snapshot_payload=snapshot_payload,
        now=now,
    )
    assert bundle["count"] == 2
    assert {c["title"] for c in bundle["citations"]} == {
        "Apple climbs",
        "Nvidia rallies",
    }
    assert all(c["title"] != "DB-only report" for c in bundle["citations"])
    assert bundle["data_health"]["source"] == "symbol_news"
    assert bundle["freshness"]["status"] == "fresh"


@pytest.mark.asyncio
async def test_build_news_evidence_empty_snapshot_is_authoritative_no_db_bleed(
    db_session,
):
    # A present-but-empty article snapshot ("queried, nothing in window") must
    # NOT fall back to research_reports — that would re-introduce the divergence.
    await _clear(db_session)
    now = dt.datetime(2026, 5, 24, 12, 0, tzinfo=dt.UTC)
    db_session.add(
        _report("db1", published_at=now, title="Recent DB report", symbols=["IBM"])
    )
    await db_session.commit()

    bundle = await build_news_evidence(
        ResearchReportsQueryService(db_session),
        market="us",
        snapshot_payload={"articles": []},
        now=now,
    )
    assert bundle["count"] == 0
    assert bundle["freshness"]["status"] == "unavailable"
    assert bundle["data_health"]["source"] == "symbol_news"


@pytest.mark.asyncio
async def test_build_news_evidence_snapshot_articles_stale_when_old(db_session):
    now = dt.datetime(2026, 5, 24, 12, 0, tzinfo=dt.UTC)
    snapshot_payload = {
        "articles": [_article("Old news", published_at=now - dt.timedelta(days=5))]
    }
    bundle = await build_news_evidence(
        ResearchReportsQueryService(db_session),
        market="us",
        snapshot_payload=snapshot_payload,
        lookback_hours=24,
        now=now,
    )
    assert bundle["count"] == 1
    assert bundle["freshness"]["status"] == "stale"


@pytest.mark.asyncio
async def test_build_news_evidence_snapshot_maps_symbol_and_excerpt(db_session):
    now = dt.datetime(2026, 5, 24, 12, 0, tzinfo=dt.UTC)
    snapshot_payload = {
        "articles": [
            _article(
                "Apple earnings",
                symbol="AAPL",
                name="Apple Inc.",
                summary="Apple beat estimates.",
                published_at=now - dt.timedelta(hours=1),
            )
        ]
    }
    bundle = await build_news_evidence(
        ResearchReportsQueryService(db_session),
        market="us",
        snapshot_payload=snapshot_payload,
        now=now,
    )
    citation = bundle["citations"][0]
    assert citation["excerpt"] == "Apple beat estimates."
    assert citation["analyst"] is None
    assert citation["symbol_candidates"][0]["symbol"] == "AAPL"
    assert citation["symbol_candidates"][0]["market"] == "us"
    assert citation["symbol_candidates"][0]["name"] == "Apple Inc."


@pytest.mark.asyncio
async def test_build_news_evidence_falls_back_to_db_when_no_snapshot(db_session):
    # No article snapshot -> the research_reports query path is used (back-compat).
    await _clear(db_session)
    now = dt.datetime(2026, 5, 24, 12, 0, tzinfo=dt.UTC)
    db_session.add(
        _report(
            "k1",
            published_at=now - dt.timedelta(hours=2),
            title="삼성전자 목표가 상향",
            symbols=["005930"],
        )
    )
    await db_session.commit()
    bundle = await build_news_evidence(
        ResearchReportsQueryService(db_session), market="kr", now=now
    )
    assert bundle["count"] == 1
    assert bundle["citations"][0]["title"] == "삼성전자 목표가 상향"
    assert bundle["data_health"]["source"] == "research_reports"

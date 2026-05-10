"""Tests for ResearchReportsQueryService.find_feed_page (ROB-179)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest

_UNSET = object()


async def _seed(
    db_session,
    dedup_key: str,
    *,
    source: str = "naver_research",
    symbol: str = "AAPL",
    market: str = "us",
    analyst: str | None = None,
    category: str | None = None,
    title: str | None = None,
    published_at: datetime | None | object = _UNSET,
    detail_excerpt: str | None = "excerpt body",
    summary_text: str | None = "summary",
):
    from app.models.research_reports import ResearchReport

    effective_published_at = (
        datetime.now(UTC) if published_at is _UNSET else published_at
    )
    row = ResearchReport(
        dedup_key=dedup_key,
        report_type="equity_research",
        source=source,
        title=title or f"Title {dedup_key}",
        analyst=analyst,
        category=category,
        summary_text=summary_text,
        detail_url=f"https://example.com/{dedup_key}",
        detail_excerpt=detail_excerpt,
        pdf_url=f"https://example.com/{dedup_key}.pdf",
        symbol_candidates=[{"symbol": symbol, "market": market, "source": "t"}],
        attribution_publisher="test_publisher",
        attribution_copyright_notice="© Test",
        attribution_full_text_exported=False,
        attribution_pdf_body_exported=False,
        published_at=effective_published_at,
    )
    db_session.add(row)
    await db_session.commit()
    return row


@pytest.mark.integration
@pytest.mark.asyncio
async def test_returns_rows_in_published_at_desc_id_desc_order(db_session):
    from app.services.research_reports.query_service import ResearchReportsQueryService

    source = f"test_order_{uuid4()}"
    fixed_at = datetime(2026, 5, 10, 0, 0, 0, tzinfo=UTC)
    rows = []
    for i in range(5):
        r = await _seed(
            db_session, f"ord-{i}-{uuid4()}", source=source, published_at=fixed_at
        )
        rows.append(r)

    svc = ResearchReportsQueryService(db_session)
    result_rows, _ = await svc.find_feed_page(limit=10, cursor=None, source=source)
    ids = [r.id for r in result_rows]
    assert ids == sorted(ids, reverse=True), (
        "Expected id DESC ordering within same published_at"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cursor_pagination_disjoint(db_session):
    from app.services.research_reports.query_service import ResearchReportsQueryService

    source = f"test_cursor_disjoint_{uuid4()}"
    base_dt = datetime(2026, 5, 10, 0, 0, 0, tzinfo=UTC)
    for i in range(5):
        await _seed(
            db_session,
            f"pg-{i}-{uuid4()}",
            source=source,
            published_at=base_dt - timedelta(hours=i),
        )

    svc = ResearchReportsQueryService(db_session)
    page1, cursor1 = await svc.find_feed_page(limit=2, cursor=None, source=source)
    assert len(page1) == 2
    assert cursor1 is not None

    page2, cursor2 = await svc.find_feed_page(limit=2, cursor=cursor1, source=source)
    assert len(page2) == 2

    ids1 = {r.id for r in page1}
    ids2 = {r.id for r in page2}
    assert ids1.isdisjoint(ids2), "Pages must not overlap"

    all_rows = list(page1) + list(page2)
    all_pts = [r.published_at for r in all_rows]
    assert all_pts == sorted(all_pts, reverse=True), (
        "Combined pages must be in published_at DESC order"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cursor_pagination_nulls_last(db_session):
    from app.services.research_reports.query_service import ResearchReportsQueryService

    source = f"test_nulls_last_{uuid4()}"
    base_dt = datetime(2026, 5, 10, 0, 0, 0, tzinfo=UTC)
    for i in range(2):
        await _seed(
            db_session,
            f"nl-dated-{i}-{uuid4()}",
            source=source,
            published_at=base_dt - timedelta(hours=i),
        )
    for i in range(2):
        await _seed(
            db_session, f"nl-null-{i}-{uuid4()}", source=source, published_at=None
        )

    svc = ResearchReportsQueryService(db_session)
    page1, cursor1 = await svc.find_feed_page(limit=2, cursor=None, source=source)
    # First page should be the dated rows (nulls last)
    assert all(r.published_at is not None for r in page1)

    page2, _ = await svc.find_feed_page(limit=2, cursor=cursor1, source=source)
    assert all(r.published_at is None for r in page2)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_source_filter(db_session):
    from app.services.research_reports.query_service import ResearchReportsQueryService

    source_a = f"src_a_{uuid4()}"
    source_b = f"src_b_{uuid4()}"
    await _seed(db_session, f"sf-a-{uuid4()}", source=source_a)
    await _seed(db_session, f"sf-b-{uuid4()}", source=source_b)

    svc = ResearchReportsQueryService(db_session)
    rows, _ = await svc.find_feed_page(limit=10, cursor=None, source=source_a)
    assert len(rows) == 1
    assert rows[0].source == source_a


@pytest.mark.integration
@pytest.mark.asyncio
async def test_symbol_jsonb_at_filter(db_session):
    from app.services.research_reports.query_service import ResearchReportsQueryService

    source = f"test_sym_filter_{uuid4()}"
    await _seed(
        db_session, f"sym-a-{uuid4()}", source=source, symbol="AAPL", market="us"
    )
    await _seed(
        db_session, f"sym-m-{uuid4()}", source=source, symbol="MSFT", market="us"
    )

    svc = ResearchReportsQueryService(db_session)
    rows, _ = await svc.find_feed_page(
        limit=10, cursor=None, source=source, symbol="AAPL"
    )
    assert len(rows) == 1
    assert rows[0].symbol_candidates[0]["symbol"] == "AAPL"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_analyst_ilike(db_session):
    from app.services.research_reports.query_service import ResearchReportsQueryService

    source = f"test_analyst_{uuid4()}"
    await _seed(db_session, f"an-a-{uuid4()}", source=source, analyst="Kim Analyst")
    await _seed(db_session, f"an-b-{uuid4()}", source=source, analyst="Park Researcher")

    svc = ResearchReportsQueryService(db_session)
    rows, _ = await svc.find_feed_page(
        limit=10, cursor=None, source=source, analyst="kim"
    )
    assert len(rows) == 1
    assert "Kim" in rows[0].analyst


@pytest.mark.integration
@pytest.mark.asyncio
async def test_category_exact_match(db_session):
    from app.services.research_reports.query_service import ResearchReportsQueryService

    source = f"test_cat_{uuid4()}"
    await _seed(db_session, f"cat-a-{uuid4()}", source=source, category="기업분석")
    await _seed(db_session, f"cat-b-{uuid4()}", source=source, category="산업분석")

    svc = ResearchReportsQueryService(db_session)
    rows, _ = await svc.find_feed_page(
        limit=10, cursor=None, source=source, category="기업분석"
    )
    assert len(rows) == 1
    assert rows[0].category == "기업분석"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_query_ilike_across_title_summary_excerpt(db_session):
    from app.services.research_reports.query_service import ResearchReportsQueryService

    source = f"test_query_{uuid4()}"
    unique_term_title = f"UniqueTitleTerm{uuid4().hex[:6]}"
    unique_term_summary = f"UniqueSummaryTerm{uuid4().hex[:6]}"
    unique_term_excerpt = f"UniqueExcerptTerm{uuid4().hex[:6]}"

    await _seed(
        db_session,
        f"q-t-{uuid4()}",
        source=source,
        title=unique_term_title,
        summary_text="regular",
        detail_excerpt="regular",
    )
    await _seed(
        db_session,
        f"q-s-{uuid4()}",
        source=source,
        title="regular",
        summary_text=unique_term_summary,
        detail_excerpt="regular",
    )
    await _seed(
        db_session,
        f"q-e-{uuid4()}",
        source=source,
        title="regular",
        summary_text="regular",
        detail_excerpt=unique_term_excerpt,
    )

    svc = ResearchReportsQueryService(db_session)
    for term in [unique_term_title, unique_term_summary, unique_term_excerpt]:
        rows, _ = await svc.find_feed_page(
            limit=10, cursor=None, source=source, query=term
        )
        assert len(rows) == 1, f"Expected 1 row matching {term}, got {len(rows)}"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_from_to_date_bounds(db_session):
    from app.services.research_reports.query_service import ResearchReportsQueryService

    source = f"test_date_bounds_{uuid4()}"
    t0 = datetime(2026, 5, 1, tzinfo=UTC)
    await _seed(
        db_session,
        f"db-old-{uuid4()}",
        source=source,
        published_at=t0 - timedelta(days=10),
    )
    await _seed(db_session, f"db-in-{uuid4()}", source=source, published_at=t0)
    await _seed(
        db_session,
        f"db-new-{uuid4()}",
        source=source,
        published_at=t0 + timedelta(days=10),
    )

    svc = ResearchReportsQueryService(db_session)
    rows, _ = await svc.find_feed_page(
        limit=10,
        cursor=None,
        source=source,
        from_date=date(2026, 4, 25),
        to_date=date(2026, 5, 5),
    )
    assert len(rows) == 1
    assert rows[0].published_at.date() == date(2026, 5, 1)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_market_filter_kr_us(db_session):
    from app.services.research_reports.query_service import ResearchReportsQueryService

    source = f"test_market_{uuid4()}"
    await _seed(
        db_session, f"mkt-kr-{uuid4()}", source=source, symbol="005930", market="kr"
    )
    await _seed(
        db_session, f"mkt-us-{uuid4()}", source=source, symbol="AAPL", market="us"
    )

    svc = ResearchReportsQueryService(db_session)
    kr_rows, _ = await svc.find_feed_page(
        limit=10, cursor=None, source=source, market_filter="kr"
    )
    assert len(kr_rows) == 1
    assert kr_rows[0].symbol_candidates[0]["market"] == "kr"

    us_rows, _ = await svc.find_feed_page(
        limit=10, cursor=None, source=source, market_filter="us"
    )
    assert len(us_rows) == 1
    assert us_rows[0].symbol_candidates[0]["market"] == "us"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_symbol_in_filter(db_session):
    from app.services.research_reports.query_service import ResearchReportsQueryService

    source = f"test_sym_in_{uuid4()}"
    await _seed(
        db_session, f"si-a-{uuid4()}", source=source, symbol="AAPL", market="us"
    )
    await _seed(
        db_session, f"si-m-{uuid4()}", source=source, symbol="MSFT", market="us"
    )
    await _seed(
        db_session, f"si-g-{uuid4()}", source=source, symbol="GOOG", market="us"
    )

    svc = ResearchReportsQueryService(db_session)
    rows, _ = await svc.find_feed_page(
        limit=10, cursor=None, source=source, symbol_in=["AAPL", "MSFT"]
    )
    symbols = {r.symbol_candidates[0]["symbol"] for r in rows}
    assert symbols == {"AAPL", "MSFT"}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_limit_clamped_1_to_100(db_session):
    from app.services.research_reports.query_service import ResearchReportsQueryService

    svc = ResearchReportsQueryService(db_session)
    with pytest.raises(ValueError):
        await svc.find_feed_page(limit=0, cursor=None)
    with pytest.raises(ValueError):
        await svc.find_feed_page(limit=-1, cursor=None)

    source = f"test_clamp_{uuid4()}"
    for i in range(3):
        await _seed(db_session, f"cl-{i}-{uuid4()}", source=source)
    rows, _ = await svc.find_feed_page(limit=100, cursor=None, source=source)
    assert len(rows) == 3


@pytest.mark.integration
@pytest.mark.asyncio
async def test_next_cursor_null_on_last_page(db_session):
    from app.services.research_reports.query_service import ResearchReportsQueryService

    source = f"test_last_page_{uuid4()}"
    for i in range(3):
        await _seed(db_session, f"lp-{i}-{uuid4()}", source=source)

    svc = ResearchReportsQueryService(db_session)
    rows, next_cursor = await svc.find_feed_page(limit=10, cursor=None, source=source)
    assert len(rows) == 3
    assert next_cursor is None

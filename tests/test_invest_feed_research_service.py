"""Tests for build_feed_research service (ROB-179)."""

from __future__ import annotations

from datetime import UTC, datetime
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
    published_at: datetime | None | object = _UNSET,
):
    from app.models.research_reports import ResearchReport

    effective_published_at = (
        datetime.now(UTC) if published_at is _UNSET else published_at
    )
    row = ResearchReport(
        dedup_key=dedup_key,
        report_type="equity_research",
        source=source,
        title=f"Title {dedup_key}",
        analyst="Test Analyst",
        category="기업분석",
        summary_text="summary",
        detail_url=f"https://example.com/{dedup_key}",
        detail_excerpt="excerpt body",
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


def _make_resolver(*, held=None, watch=None):
    from app.services.invest_view_model.relation_resolver import RelationResolver

    r = RelationResolver()
    if held:
        r.held = {(m.lower(), s.upper()) for m, s in held}
    if watch:
        r.watch = {(m.lower(), s.upper()) for m, s in watch}
    return r


@pytest.mark.integration
@pytest.mark.asyncio
async def test_top_tab_falls_back_to_latest_when_resolver_empty(db_session):
    from app.schemas.invest_feed_research import FeedResearchFilters
    from app.services.invest_view_model.feed_research_service import build_feed_research

    source = f"test_top_fallback_{uuid4()}"
    await _seed(db_session, f"tf-1-{uuid4()}", source=source)

    resolver = _make_resolver()
    resp = await build_feed_research(
        db=db_session,
        resolver=resolver,
        tab="top",
        limit=10,
        cursor_str=None,
        filters=FeedResearchFilters(source=source),
    )
    assert len(resp.items) == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mine_tab_filters_to_holdings_symbols(db_session):
    from app.schemas.invest_feed_research import FeedResearchFilters
    from app.services.invest_view_model.feed_research_service import build_feed_research

    source = f"test_mine_tab_{uuid4()}"
    await _seed(
        db_session, f"mt-a-{uuid4()}", source=source, symbol="AAPL", market="us"
    )
    await _seed(
        db_session, f"mt-m-{uuid4()}", source=source, symbol="MSFT", market="us"
    )

    resolver = _make_resolver(held=[("us", "AAPL")])
    resp = await build_feed_research(
        db=db_session,
        resolver=resolver,
        tab="mine",
        limit=10,
        cursor_str=None,
        filters=FeedResearchFilters(source=source),
    )
    assert len(resp.items) == 1
    assert resp.items[0].symbolCandidates[0].symbol == "AAPL"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_watchlist_tab_filters_to_watch_symbols(db_session):
    from app.schemas.invest_feed_research import FeedResearchFilters
    from app.services.invest_view_model.feed_research_service import build_feed_research

    source = f"test_watchlist_{uuid4()}"
    await _seed(
        db_session, f"wl-a-{uuid4()}", source=source, symbol="AAPL", market="us"
    )
    await _seed(
        db_session, f"wl-m-{uuid4()}", source=source, symbol="MSFT", market="us"
    )

    resolver = _make_resolver(watch=[("us", "MSFT")])
    resp = await build_feed_research(
        db=db_session,
        resolver=resolver,
        tab="watchlist",
        limit=10,
        cursor_str=None,
        filters=FeedResearchFilters(source=source),
    )
    assert len(resp.items) == 1
    assert resp.items[0].symbolCandidates[0].symbol == "MSFT"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_kr_tab_returns_only_kr_market_rows(db_session):
    from app.schemas.invest_feed_research import FeedResearchFilters
    from app.services.invest_view_model.feed_research_service import build_feed_research

    source = f"test_kr_tab_{uuid4()}"
    await _seed(
        db_session, f"kr-a-{uuid4()}", source=source, symbol="005930", market="kr"
    )
    await _seed(
        db_session, f"kr-b-{uuid4()}", source=source, symbol="AAPL", market="us"
    )

    resolver = _make_resolver()
    resp = await build_feed_research(
        db=db_session,
        resolver=resolver,
        tab="kr",
        limit=10,
        cursor_str=None,
        filters=FeedResearchFilters(source=source),
    )
    assert len(resp.items) == 1
    assert resp.items[0].symbolCandidates[0].market == "kr"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_relation_stamped_mine_takes_precedence_over_watch(db_session):
    from app.schemas.invest_feed_research import FeedResearchFilters
    from app.services.invest_view_model.feed_research_service import build_feed_research

    source = f"test_rel_prec_{uuid4()}"
    await _seed(
        db_session, f"rp-a-{uuid4()}", source=source, symbol="AAPL", market="us"
    )

    resolver = _make_resolver(held=[("us", "AAPL")], watch=[("us", "AAPL")])
    resp = await build_feed_research(
        db=db_session,
        resolver=resolver,
        tab="latest",
        limit=10,
        cursor_str=None,
        filters=FeedResearchFilters(source=source),
    )
    assert len(resp.items) == 1
    assert resp.items[0].relation == "mine"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_relation_stamped_none_when_no_match(db_session):
    from app.schemas.invest_feed_research import FeedResearchFilters
    from app.services.invest_view_model.feed_research_service import build_feed_research

    source = f"test_rel_none_{uuid4()}"
    await _seed(
        db_session, f"rn-a-{uuid4()}", source=source, symbol="AAPL", market="us"
    )

    resolver = _make_resolver()
    resp = await build_feed_research(
        db=db_session,
        resolver=resolver,
        tab="latest",
        limit=10,
        cursor_str=None,
        filters=FeedResearchFilters(source=source),
    )
    assert len(resp.items) == 1
    assert resp.items[0].relation == "none"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_market_derived_from_first_candidate(db_session):
    from app.schemas.invest_feed_research import FeedResearchFilters
    from app.services.invest_view_model.feed_research_service import build_feed_research

    source = f"test_mkt_derive_{uuid4()}"
    await _seed(
        db_session, f"md-a-{uuid4()}", source=source, symbol="005930", market="kr"
    )

    resolver = _make_resolver()
    resp = await build_feed_research(
        db=db_session,
        resolver=resolver,
        tab="latest",
        limit=10,
        cursor_str=None,
        filters=FeedResearchFilters(source=source),
    )
    assert len(resp.items) == 1
    assert resp.items[0].market == "kr"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_market_null_when_candidates_empty(db_session):
    from app.models.research_reports import ResearchReport
    from app.schemas.invest_feed_research import FeedResearchFilters
    from app.services.invest_view_model.feed_research_service import build_feed_research

    source = f"test_mkt_null_{uuid4()}"
    row = ResearchReport(
        dedup_key=f"mn-{uuid4()}",
        report_type="equity_research",
        source=source,
        title="No candidates",
        summary_text="summary",
        symbol_candidates=[],
        attribution_full_text_exported=False,
        attribution_pdf_body_exported=False,
        published_at=datetime.now(UTC),
    )
    db_session.add(row)
    await db_session.commit()

    resolver = _make_resolver()
    resp = await build_feed_research(
        db=db_session,
        resolver=resolver,
        tab="latest",
        limit=10,
        cursor_str=None,
        filters=FeedResearchFilters(source=source),
    )
    assert len(resp.items) == 1
    assert resp.items[0].market is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_response_meta_echoes_applied_filters(db_session):
    from app.schemas.invest_feed_research import FeedResearchFilters
    from app.services.invest_view_model.feed_research_service import build_feed_research

    source = f"test_meta_echo_{uuid4()}"
    resolver = _make_resolver()
    resp = await build_feed_research(
        db=db_session,
        resolver=resolver,
        tab="latest",
        limit=30,
        cursor_str=None,
        filters=FeedResearchFilters(source=source, symbol="AAPL"),
    )
    assert resp.meta.appliedFilters.source == source
    assert resp.meta.appliedFilters.symbol == "AAPL"
    assert resp.meta.limit == 30


@pytest.mark.integration
@pytest.mark.asyncio
async def test_invalid_cursor_raises_value_error(db_session):
    from app.schemas.invest_feed_research import FeedResearchFilters
    from app.services.invest_view_model.feed_research_service import build_feed_research

    resolver = _make_resolver()
    with pytest.raises(ValueError):
        await build_feed_research(
            db=db_session,
            resolver=resolver,
            tab="latest",
            limit=10,
            cursor_str="garbage-not-base64!!!",
            filters=FeedResearchFilters(),
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_next_cursor_null_when_under_limit(db_session):
    from app.schemas.invest_feed_research import FeedResearchFilters
    from app.services.invest_view_model.feed_research_service import build_feed_research

    source = f"test_no_cursor_{uuid4()}"
    for i in range(3):
        await _seed(db_session, f"nc-{i}-{uuid4()}", source=source)

    resolver = _make_resolver()
    resp = await build_feed_research(
        db=db_session,
        resolver=resolver,
        tab="latest",
        limit=10,
        cursor_str=None,
        filters=FeedResearchFilters(source=source),
    )
    assert len(resp.items) == 3
    assert resp.nextCursor is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_next_cursor_round_trip(db_session):
    from app.schemas.invest_feed_research import FeedResearchFilters
    from app.services.invest_view_model.feed_research_service import build_feed_research

    source = f"test_cursor_rt_{uuid4()}"
    base_dt = datetime(2026, 5, 10, 0, 0, 0, tzinfo=UTC)
    from datetime import timedelta

    for i in range(5):
        await _seed(
            db_session,
            f"crt-{i}-{uuid4()}",
            source=source,
            published_at=base_dt - timedelta(hours=i),
        )

    resolver = _make_resolver()
    resp1 = await build_feed_research(
        db=db_session,
        resolver=resolver,
        tab="latest",
        limit=2,
        cursor_str=None,
        filters=FeedResearchFilters(source=source),
    )
    assert resp1.nextCursor is not None

    resp2 = await build_feed_research(
        db=db_session,
        resolver=resolver,
        tab="latest",
        limit=2,
        cursor_str=resp1.nextCursor,
        filters=FeedResearchFilters(source=source),
    )
    ids1 = {item.id for item in resp1.items}
    ids2 = {item.id for item in resp2.items}
    assert ids1.isdisjoint(ids2)

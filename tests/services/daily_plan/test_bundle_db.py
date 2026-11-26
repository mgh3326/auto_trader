"""CI-only: the reader against the real tables, and one stored bundle per content."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select, text

from app.models.account_value import AccountValueSnapshot
from app.models.daily_plan import bundles
from app.models.invest_screener_snapshot import InvestScreenerSnapshot
from app.models.investor_flow_snapshot import InvestorFlowSnapshot
from app.models.kr_symbol_universe import KRSymbolUniverse
from app.models.market_events import MarketEvent
from app.models.market_valuation_snapshot import MarketValuationSnapshot
from app.models.news import NewsArticle
from app.models.screener_pick_log import ScreenerPickLog
from app.models.symbol_news_relevance import SymbolNewsRelevance
from app.services.daily_plan.bundle import bundle_sha256, canonical_bundle
from app.services.daily_plan.bundle_builder import AccountBinding, input_tallies
from app.services.daily_plan.bundle_reader import assemble_bundle
from scripts import build_daily_plan_bundle as cli

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

KST = ZoneInfo("Asia/Seoul")
# Dates no other test writes to; the seeded rows are rolled back.
DAY = date(2019, 7, 10)
PREVIOUS = date(2019, 7, 9)
FLOW_SESSIONS = [date(2019, 7, d) for d in (3, 4, 5, 8, 9)]
CLI_DAY = date(2019, 5, 15)
HELD = "999901"
PICKED = "999902"
SERIES = "kis:kis_live:" + "e" * 64
REF = "acct_" + "e" * 32


def at(day, hour, minute=0):
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=KST)


def naive(value):
    return value.astimezone(UTC).replace(tzinfo=None)


async def seed(db):
    evening = at(PREVIOUS, 17)
    stamped = {"created_at": evening, "updated_at": evening}
    db.add(
        KRSymbolUniverse(
            symbol=HELD, name="held", exchange="KOSPI", security_type="STOCK"
        )
    )
    days = [PREVIOUS - timedelta(days=n) for n in range(120)]
    days = sorted(day for day in days if day.weekday() < 5)
    for symbol, base in ((HELD, 50000), (PICKED, 80000)):
        for index, day in enumerate(days):
            close = base + ((index * 7) % 23 - 11) * 200
            await db.execute(
                text("""
                    INSERT INTO public.kr_candles_1d
                        (time, symbol, venue, open, high, low, close, volume,
                         value, source, ingested_at)
                    VALUES (:time, :symbol, 'KRX', :close, :high, :low, :close,
                            :volume, 0, 'kis', :ingested)
                """),
                {
                    "time": datetime(day.year, day.month, day.day, tzinfo=UTC),
                    "symbol": symbol,
                    "close": close,
                    "high": close + 300,
                    "low": close - 300,
                    "volume": 1000 + index % 5,
                    "ingested": at(DAY, 7, 10),
                },
            )
        db.add(
            InvestScreenerSnapshot(
                market="kr",
                symbol=symbol,
                snapshot_date=PREVIOUS,
                latest_close=Decimal(base),
                closes_window=[],
                daily_turnover=Decimal("123456789.00"),
                source="kis",
                computed_at=evening,
                **stamped,
            )
        )
        db.add(
            MarketValuationSnapshot(
                market="kr",
                symbol=symbol,
                snapshot_date=PREVIOUS,
                source="naver_finance",
                pbr=Decimal("0.9100"),
                computed_at=evening,
                created_at=evening,
                # One symbol's valuation was rewritten after it was created.
                updated_at=evening if symbol == HELD else at(PREVIOUS, 19),
            )
        )
        for day in FLOW_SESSIONS:
            db.add(
                InvestorFlowSnapshot(
                    market="kr",
                    symbol=symbol,
                    snapshot_date=day,
                    foreign_net=100,
                    double_buy=False,
                    double_sell=False,
                    source="naver_finance",
                    collected_at=at(day, 18, 10),
                    created_at=at(day, 18, 10),
                    updated_at=at(day, 18, 10),
                )
            )
    article = NewsArticle(
        url=f"https://example.invalid/daily-plan-bundle/{uuid4()}",
        title="fixture",
        market="kr",
        published_at=at(PREVIOUS, 16),
        published_precision="second",
        first_received_at=at(PREVIOUS, 16, 1),
        scraped_at=naive(at(PREVIOUS, 16, 1)),
        created_at=naive(at(PREVIOUS, 16, 1)),
    )
    db.add(article)
    await db.flush()
    seen = naive(at(PREVIOUS, 16, 1))
    db.add(
        SymbolNewsRelevance(
            article_id=article.id,
            market="kr",
            symbol=HELD,
            feed_source="naver_item_news",
            first_seen_at=seen,
            created_at=seen,
            updated_at=seen,
        )
    )
    db.add(
        MarketEvent(
            category="disclosure",
            market="kr",
            symbol=HELD,
            event_date=PREVIOUS,
            source="naver_disclosure",
            source_event_id=f"daily-plan-bundle-{uuid4()}",
            provider_created_at=evening,
            first_received_at=evening,
            **stamped,
        )
    )
    db.add(
        ScreenerPickLog(
            call_id=uuid4(),
            recorded_at=at(PREVIOUS, 10),
            recorded_at_kst=at(PREVIOUS, 10).isoformat(),
            market="kr",
            source="screen_stocks",
            family="screen_stocks_live",
            kind="candidate",
            symbol=PICKED,
            admission="admitted",
            selection_seq=1,
            fanout_version="test",
            fanout_code_sha256="0" * 64,
            source_params={},
        )
    )
    db.add(
        AccountValueSnapshot(
            account=SERIES,
            market="account",
            day=DAY,
            observed_at=at(DAY, 7, 40),
            created_at=at(DAY, 7, 41),
            holdings=[{"symbol": HELD, "quantity": "2", "currency": "KRW"}],
            cash={"KRW": "0"},
            reasons=[],
            fx={},
        )
    )
    await db.flush()


async def test_same_database_rows_give_the_same_bundle(db_session):
    bindings = {SERIES: AccountBinding(REF, "kis_live")}
    kwargs = {
        "day": DAY,
        "closed_at": at(DAY, 8, 20),
        "cutoff_at": at(DAY, 8, 30),
        "bindings": bindings,
    }
    async with db_session.begin():
        await seed(db_session)
        # Both reads come from the database, not from the objects just added.
        db_session.expire_all()
        first = await assemble_bundle(db_session, **kwargs)
        db_session.expire_all()
        second = await assemble_bundle(db_session, **kwargs)
        await db_session.rollback()

    assert canonical_bundle(first) == canonical_bundle(second)
    assert bundle_sha256(first) == bundle_sha256(second)
    rows = {row["symbol"]: row for row in first["rows"]}
    assert set(rows) == {HELD, PICKED}
    assert rows[HELD]["candidate_sources"] == [
        "holdings",
        "screener:turnover_leaders",
    ]
    assert "pick_log:screen_stocks" in rows[PICKED]["candidate_sources"]
    assert rows[HELD]["held_account_refs"] == [REF]
    assert rows[HELD]["security_type"] == "STOCK"
    held = rows[HELD]["ranking_inputs"]
    assert rows[HELD]["last_close"] and held["rsi_14"]["value"]
    assert held["daily_turnover"]["value"] == "123456789"
    assert held["pbr"]["value"] == "0.91"
    assert held["foreign_net"]["value"] == "500"
    assert len(held["news"]["items"]) == len(held["disclosures"]["items"]) == 1
    picked = rows[PICKED]["ranking_inputs"]
    assert picked["pbr"]["unavailable"] == "observed_time_unproven"
    assert picked["news"]["unavailable"] == "input_missing"
    [account] = first["accounts"]
    assert account["cash"]["value"] == "0"
    assert input_tallies(first)["cash"] == {"present": 1}


async def _count(db_session, *where):
    async with db_session.begin():
        return await db_session.scalar(
            select(func.count()).select_from(bundles).where(*where)
        )


async def test_cli_stores_one_bundle_and_a_rerun_returns_it(db_session, monkeypatch):
    from app.core.db import AsyncSessionLocal

    read_only = []
    assemble = cli.assemble_bundle

    async def spy(db, **kwargs):
        read_only.append(await db.scalar(text("SHOW transaction_read_only")))
        return await assemble(db, **kwargs)

    monkeypatch.setattr(cli, "assemble_bundle", spy)
    config = SimpleNamespace(
        SECRET_KEY="Test-Only-Secret-Key-0123456789abcdef",
        kis_account_no=None,
        kis_app_key=None,
        kis_app_secret=None,
        toss_api_account_seq=None,
    )

    async def run(mode):
        code, report = await cli.run(
            cli.parse_args(["--date", CLI_DAY.isoformat(), mode]),
            session_factory=AsyncSessionLocal,
            config=config,
            now=at(CLI_DAY, 8, 25),
        )
        assert code == 0, report
        return report

    before = await _count(db_session)
    preview = await run("--dry-run")
    assert preview["status"] == "built" and "bundle_id" not in preview
    assert await _count(db_session) == before
    first = await run("--commit")
    second = await run("--commit")
    assert first["bundle_id"] == second["bundle_id"]
    assert first["bundle_sha256"] == second["bundle_sha256"]
    assert preview["bundle_sha256"] == first["bundle_sha256"]
    digest = bundles.c.sha256 == first["bundle_sha256"]
    assert await _count(db_session, digest) == 1
    assert read_only == ["on", "on", "on"]

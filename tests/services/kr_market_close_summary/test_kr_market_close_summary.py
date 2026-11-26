from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.services.kr_market_close_summary.formatter import format_summary
from app.services.kr_market_close_summary.service import SECTIONS, build_summary
from app.services.kr_market_close_summary.sources import (
    _breadth,
    _load_disclosures,
    _load_news,
    confirmed_flow_evidence,
)
from app.services.kr_market_close_summary.types import Evidence
from scripts.run_kr_market_close_summary import parse_args

REPO = Path(__file__).resolve().parents[3]
MIGRATION_PATH = REPO / "alembic/versions/20261126_hk6750_kr_close_summary.py"

pytestmark = pytest.mark.unit

SESSION_DATE = date(2026, 11, 25)
AS_OF = "2026-11-25T18:10:00+09:00"


class FixtureSource:
    async def load(self, session_date: date) -> dict[str, Any]:
        assert session_date == SESSION_DATE
        return {
            "indices": {
                "KOSPI": Evidence(
                    {"close": "2600.10", "change_pct": "0.25"},
                    "fixture:krx_index_close",
                    AS_OF,
                ).to_dict(),
                "KOSDAQ": Evidence(None, None, None, "index_close_missing").to_dict(),
            },
            "investor_flows": Evidence(
                {"foreign_net": "100", "institution_net": "-20"},
                "fixture:confirmed_daily_flow",
                AS_OF,
            ).to_dict(),
            "market_breadth": Evidence(
                {"advancers": 3, "decliners": 2, "unchanged": 1},
                "fixture:krx_daily_bars",
                AS_OF,
            ).to_dict(),
            "sectors": Evidence(
                [{"sector": "반도체", "change_pct": "1.20"}],
                "fixture:symbol_sectors+krx_daily_bars",
                AS_OF,
            ).to_dict(),
            "top_movers": Evidence(
                [{"symbol": "005930", "change_pct": "5.10"}],
                "fixture:krx_daily_bars",
                AS_OF,
            ).to_dict(),
            "disclosures": Evidence(
                [{"symbol": "005930", "title": "공시", "url": "fixture://dart/1"}],
                "fixture:market_events",
                AS_OF,
            ).to_dict(),
            "news": Evidence(
                [{"title": "뉴스", "provider_published_at": AS_OF}],
                "fixture:news_articles.published_at",
                AS_OF,
            ).to_dict(),
            "macro": Evidence(None, None, None, "macro_fact_not_stored").to_dict(),
            "_commit_readiness": {
                "session_confirmed": True,
                "session_is_past": True,
                "data_settled": True,
                "market_coverage_complete": True,
                "flow_coverage_complete": True,
                "disclosures_complete": True,
            },
        }


class FakeStore:
    def __init__(self) -> None:
        self.rows: dict[date, dict[str, Any]] = {}

    async def insert_once(self, summary) -> bool:  # noqa: ANN001
        if summary.session_date in self.rows:
            return False
        self.rows[summary.session_date] = summary.to_dict()
        return True


@pytest.mark.asyncio
async def test_preview_prints_every_section_with_provenance_and_missing_reason() -> (
    None
):
    summary, stored = await build_summary(
        session_date=SESSION_DATE,
        source=FixtureSource(),
    )

    output = format_summary(summary, commit=False, stored=stored)
    assert stored is None
    for section in SECTIONS:
        assert f'"{section}"' in output
        section_facts = summary.facts[section]
        evidence_items = (
            list(section_facts.values()) if section == "indices" else [section_facts]
        )
        for evidence in evidence_items:
            assert "source" in evidence
            assert "as_of" in evidence
    assert '"value": null' in output
    assert '"reason": "index_close_missing"' in output
    assert '"reason": "macro_fact_not_stored"' in output
    assert '"mode": "preview"' in output
    assert '"next_session_points": null' in output


def test_cli_defaults_to_preview() -> None:
    assert parse_args([]).commit is False


def test_migration_downgrade_locks_before_checking_and_dropping() -> None:
    source = MIGRATION_PATH.read_text(encoding="utf-8")
    lock_at = source.index(
        "LOCK TABLE public.kr_market_close_summaries IN ACCESS EXCLUSIVE MODE"
    )
    count_at = source.index("SELECT count(*) FROM public.kr_market_close_summaries")
    drop_at = source.index('op.drop_table("kr_market_close_summaries")')
    assert lock_at < count_at < drop_at


@pytest.mark.asyncio
async def test_commit_is_one_record_per_session_and_rerun_is_noop() -> None:
    store = FakeStore()
    first, inserted_first = await build_summary(
        session_date=SESSION_DATE,
        source=FixtureSource(),
        store=store,
        commit=True,
    )
    second, inserted_second = await build_summary(
        session_date=SESSION_DATE,
        source=FixtureSource(),
        store=store,
        commit=True,
    )

    assert inserted_first is True
    assert inserted_second is False
    assert len(store.rows) == 1
    assert store.rows[SESSION_DATE] == first.to_dict() == second.to_dict()


@pytest.mark.asyncio
async def test_commit_refuses_when_readiness_or_required_coverage_is_incomplete() -> (
    None
):
    store = FakeStore()

    class IncompleteSource(FixtureSource):
        async def load(self, session_date: date) -> dict[str, Any]:
            result = await super().load(session_date)
            result["_commit_readiness"]["data_settled"] = False
            result["market_breadth"] = {
                "value": None,
                "source": "fixture:krx_daily_bars",
                "as_of": AS_OF,
                "reason": "session_bar_coverage_incomplete",
            }
            return result

    with pytest.raises(ValueError, match="summary_not_ready_to_commit"):
        await build_summary(
            session_date=SESSION_DATE,
            source=IncompleteSource(),
            store=store,
            commit=True,
        )

    assert store.rows == {}


def test_provisional_kis_flow_is_never_used_as_confirmed() -> None:
    evidence = confirmed_flow_evidence(
        [
            {
                "source": "kis",
                "foreign_net": 900,
                "institution_net": 800,
                "individual_net": -1700,
                "collected_at": datetime(2026, 11, 25, 5, tzinfo=UTC),
            }
        ]
    )

    assert evidence["value"] is None
    assert evidence["reason"] == "confirmed_daily_flow_missing"


def test_confirmed_flows_count_symbol_directions_without_adding_share_units() -> None:
    evidence = confirmed_flow_evidence(
        [
            {
                "source": "naver_finance",
                "foreign_net": 100,
                "institution_net": -20,
                "individual_net": -80,
                "collected_at": datetime(2026, 11, 25, 9, tzinfo=UTC),
            },
            {
                "source": "naver_finance",
                "foreign_net": -500,
                "institution_net": 700,
                "individual_net": -200,
                "collected_at": datetime(2026, 11, 25, 10, tzinfo=UTC),
            },
        ]
    )

    assert evidence["value"]["foreign_net_buy_symbols"] == 1
    assert evidence["value"]["foreign_net_sell_symbols"] == 1
    assert evidence["value"]["quantity_unit"].startswith("shares per symbol")
    assert evidence["reason"] == "confirmed_flow_coverage_incomplete"


def test_breadth_is_null_with_coverage_counts_below_threshold() -> None:
    evidence = _breadth(
        [{"change_pct": 1}],
        SESSION_DATE,
        {
            "covered_symbols": 1,
            "universe_symbols": 100,
            "ratio": "0.01",
            "minimum_ratio": "0.95",
            "complete": False,
        },
    )

    assert evidence["value"] is None
    assert evidence["reason"] == "session_bar_coverage_incomplete"
    assert evidence["coverage"]["covered_symbols"] == 1
    assert evidence["coverage"]["universe_symbols"] == 100


@pytest.mark.asyncio
async def test_disclosures_stay_pending_until_post_close_partition_is_complete() -> (
    None
):
    class Session:
        query: Any = None

        async def scalar(self, query: Any) -> Any:
            self.query = query
            return SimpleNamespace(
                status="succeeded",
                finished_at=datetime(2026, 11, 25, 9, 29, tzinfo=UTC),
            )

        async def scalars(self, query: Any) -> Any:
            raise AssertionError("incomplete partition must not read events")

    evidence, complete = await _load_disclosures(Session(), SESSION_DATE, ["005930"])

    assert complete is False
    assert evidence["value"] is None
    assert evidence["reason"] == "disclosure_ingestion_pending"


@pytest.mark.asyncio
async def test_disclosure_query_includes_dart_earnings_categories() -> None:
    class Session:
        statement: Any = None

        async def scalar(self, query: Any) -> Any:
            return SimpleNamespace(
                status="succeeded",
                finished_at=datetime(2026, 11, 25, 9, 31, tzinfo=UTC),
            )

        async def scalars(self, query: Any) -> Any:
            self.statement = query
            return SimpleNamespace(
                all=lambda: [
                    SimpleNamespace(
                        symbol="005930",
                        title="잠정실적",
                        source="dart",
                        source_url="https://example.invalid/report",
                        release_time_utc=AS_OF,
                        first_received_at=None,
                    )
                ]
            )

    session = Session()
    evidence, complete = await _load_disclosures(session, SESSION_DATE, ["005930"])
    statement = str(
        session.statement.compile(compile_kwargs={"literal_binds": True})
    ).lower()

    assert complete is True
    assert evidence["value"][0]["title"] == "잠정실적"
    assert "earnings" in statement
    assert "disclosure" in statement
    assert "dart" in statement


@pytest.mark.asyncio
async def test_news_query_unions_relevance_links_and_deduplicates_before_limit() -> (
    None
):
    article = SimpleNamespace(
        id=7,
        title="news",
        feed_source="naver_item_news",
        source="naver",
        url="https://example.invalid/news",
        published_at=datetime(2026, 11, 25, 9, tzinfo=UTC),
    )

    class Session:
        statement: Any = None

        async def execute(self, statement: Any) -> Any:
            self.statement = statement
            return SimpleNamespace(all=lambda: [(article, "005930")])

    session = Session()
    evidence = await _load_news(session, SESSION_DATE, ["005930"])
    statement = str(
        session.statement.compile(compile_kwargs={"literal_binds": True})
    ).lower()

    assert evidence["value"] == [
        {
            "symbol": "005930",
            "title": "news",
            "source": "naver_item_news",
            "url": "https://example.invalid/news",
            "provider_published_at": article.published_at.isoformat(),
        }
    ]
    assert "symbol_news_relevance" in statement
    assert "news_article_related_symbols" in statement
    assert "distinct" in statement
    assert "limit 50" in statement

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest

from app.services.kr_market_close_summary.formatter import format_summary
from app.services.kr_market_close_summary.service import SECTIONS, build_summary
from app.services.kr_market_close_summary.sources import confirmed_flow_evidence
from app.services.kr_market_close_summary.types import Evidence
from scripts.run_kr_market_close_summary import parse_args

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

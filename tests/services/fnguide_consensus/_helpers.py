"""Shared helpers for the FnGuide collector tests (fixtures are saved HTML, no network)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

from app.services.fnguide_consensus.parser import ParsedPage, parse_page

FIXTURES = Path(__file__).parent / "fixtures"
OBSERVED = date(2026, 11, 25)
FETCHED = datetime(2026, 11, 25, 7, 0, tzinfo=UTC)


def load_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def parse_fixture(name: str, *, symbol: str, page: str = "Consensus") -> ParsedPage:
    return parse_page(
        load_fixture(name),
        symbol=symbol,
        page=page,
        observed_date=OBSERVED,
        fetched_at=FETCHED,
        source_url=f"https://wcomp.fnguide.com/CompanyInfo/{page}?cmp_cd={symbol}",
    )

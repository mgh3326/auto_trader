from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any, Protocol

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.kr_market_close_summary import KRMarketCloseSummary
from app.services.kr_market_close_summary.sources import load_facts
from app.services.kr_market_close_summary.types import CloseSummary, Evidence

SECTIONS = (
    "indices",
    "investor_flows",
    "market_breadth",
    "sectors",
    "top_movers",
    "disclosures",
    "news",
    "macro",
)


class SummarySource(Protocol):
    async def load(self, session_date: date) -> Mapping[str, Any]: ...


class SummaryStore(Protocol):
    async def insert_once(self, summary: CloseSummary) -> bool: ...


class SqlAlchemySummarySource:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def load(self, session_date: date) -> Mapping[str, Any]:
        return await load_facts(self._session, session_date)


class SqlAlchemySummaryStore:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def insert_once(self, summary: CloseSummary) -> bool:
        payload = summary.to_dict()
        statement = (
            insert(KRMarketCloseSummary)
            .values(
                session_date=summary.session_date,
                schema_version=payload["schema_version"],
                facts=summary.facts,
                interpretation=summary.interpretation,
            )
            .on_conflict_do_nothing(index_elements=[KRMarketCloseSummary.session_date])
        )
        result = await self._session.execute(statement)
        return bool(result.rowcount)


async def build_summary(
    *,
    session_date: date,
    source: SummarySource,
    store: SummaryStore | None = None,
    commit: bool = False,
) -> tuple[CloseSummary, bool | None]:
    if commit and store is None:
        raise ValueError("store is required when commit=True")
    raw_facts = await source.load(session_date)
    facts = {
        section: raw_facts.get(section)
        or Evidence(None, None, None, "source_not_available").to_dict()
        for section in SECTIONS
    }
    summary = CloseSummary(
        session_date=session_date,
        facts=facts,
        interpretation={
            "summary": None,
            "next_session_points": None,
            "reason": "operator_interpretation_not_generated",
        },
    )
    stored = await store.insert_once(summary) if commit and store is not None else None
    return summary, stored

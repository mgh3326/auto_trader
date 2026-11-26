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
COMMIT_REQUIRED_SECTIONS = (
    "investor_flows",
    "market_breadth",
    "sectors",
    "top_movers",
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
    readiness = raw_facts.get("_commit_readiness")
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
    if commit:
        failures = _commit_readiness_failures(facts, readiness)
        if failures:
            raise ValueError("summary_not_ready_to_commit: " + ", ".join(failures))
    stored = await store.insert_once(summary) if commit and store is not None else None
    return summary, stored


def _commit_readiness_failures(facts: Mapping[str, Any], readiness: Any) -> list[str]:
    required_readiness = (
        "session_confirmed",
        "session_is_past",
        "data_settled",
        "market_coverage_complete",
        "flow_coverage_complete",
        "disclosures_complete",
    )
    if not isinstance(readiness, Mapping):
        return ["readiness_unavailable"]
    failures = [key for key in required_readiness if readiness.get(key) is not True]
    for section in COMMIT_REQUIRED_SECTIONS:
        evidence = facts[section]
        if evidence["value"] is None or evidence["reason"] is not None:
            failures.append(f"{section}_incomplete")
    return failures

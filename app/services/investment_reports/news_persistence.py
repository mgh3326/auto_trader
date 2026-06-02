# app/services/investment_reports/news_persistence.py
"""ROB-423 PR2 — pure news-citation persistence planner (no DB I/O).

Matches Hermes-supplied news citations against the bundle's news snapshot
articles and produces the fetch_run + citation rows to insert. Unmatched refs
are dropped and reported (fail-open, no fabrication). Kept pure so the matching
logic is unit-testable without a database.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from app.schemas.hermes_composition import HermesNewsCitation

_SUMMARY_MAX = 1000


@dataclass(frozen=True)
class NewsPersistencePlan:
    fetch_runs: list[dict[str, Any]] = field(default_factory=list)
    citations: list[dict[str, Any]] = field(default_factory=list)
    unmatched: list[str] = field(default_factory=list)


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _truncate(text: str | None) -> str | None:
    if text is None:
        return None
    return text[:_SUMMARY_MAX]


def build_news_persistence(
    *,
    news_payloads: list[dict[str, Any]],
    citations: list[HermesNewsCitation],
    item_uuid_by_client_key: dict[str, UUID],
    instrument_type: str,
) -> NewsPersistencePlan:
    by_external: dict[str, dict[str, Any]] = {}
    by_url: dict[str, dict[str, Any]] = {}
    market = "us"
    for payload in news_payloads:
        market = payload.get("market") or market
        for art in payload.get("articles", []):
            ext = art.get("external_article_id")
            url = art.get("url")
            if ext:
                by_external.setdefault(ext, art)
            if url:
                by_url.setdefault(url, art)

    # per (symbol, provider) used_count tally
    used_by_key: dict[tuple[str, str], int] = {}

    citation_rows: list[dict[str, Any]] = []
    unmatched: list[str] = []
    for cit in citations:
        art = None
        ref = cit.external_article_id or cit.canonical_url or ""
        if cit.external_article_id:
            art = by_external.get(cit.external_article_id)
        if art is None and cit.canonical_url:
            art = by_url.get(cit.canonical_url)
        if art is None:
            unmatched.append(ref)
            continue

        sym = art.get("symbol") or cit.symbol
        provider = art.get("provider") or "unknown"
        used_by_key[(sym, provider)] = used_by_key.get((sym, provider), 0) + 1

        item_uuid = (
            item_uuid_by_client_key.get(cit.client_item_key)
            if cit.client_item_key
            else None
        )
        citation_rows.append(
            {
                "report_item_uuid": item_uuid,
                "section_key": cit.section_key,
                "market": market,
                "symbol": sym,
                "provider": provider,
                "external_article_id": art.get("external_article_id"),
                "canonical_url": art.get("url") or cit.canonical_url or "",
                "source_name": art.get("source"),
                "title": art.get("title") or "",
                "summary_snapshot": _truncate(art.get("summary")),
                "published_at": _parse_dt(art.get("published_at")),
                "relevance": cit.relevance,
                "role": cit.role,
                "decision_impact": cit.decision_impact,
                "selection_reason": cit.selection_reason,
                "confidence": cit.confidence,
                "_fetch_key": (sym, provider),  # internal: link to fetch_run
            }
        )

    fetch_runs: list[dict[str, Any]] = []
    for payload in news_payloads:
        for rec in payload.get("fetch_records", []):
            sym = rec.get("symbol") or ""
            provider = rec.get("provider") or "unknown"
            fetch_runs.append(
                {
                    "market": market,
                    "symbol": sym,
                    "instrument_type": instrument_type,
                    "provider": provider,
                    "requested_limit": int(rec.get("requested_limit") or 0),
                    "returned_count": int(rec.get("returned_count") or 0),
                    "used_count": used_by_key.get((sym, provider), 0),
                    "status": rec.get("status") or "ok",
                    "error_code": rec.get("error_code"),
                    "_fetch_key": (sym, provider),  # internal: citation linkage
                }
            )

    return NewsPersistencePlan(
        fetch_runs=fetch_runs, citations=citation_rows, unmatched=unmatched
    )
